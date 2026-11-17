import asyncio
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from omnisearch.domain.models import ResearchBudget, ResearchCreate, StrictModel
from omnisearch.quality.metrics import QualityEvaluation, evaluate_quality
from omnisearch.services.research import run_with_runtime_budget
from omnisearch.services.search import QuickSearchCommand, QuickSearchResponse

if TYPE_CHECKING:
    from omnisearch.runtime import OmniRuntime


BenchmarkMode = Literal["single_source", "multi_source", "multilingual", "deep_research"]
_PRIMARY_TYPES = {"paper", "repository", "code", "patent", "standard", "official_document"}


class BenchmarkCase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    question: str = Field(min_length=3)
    relevant_entities: list[list[str]] = Field(min_length=1)
    queries: dict[str, list[str]] = Field(min_length=1)
    sources: list[str] = Field(min_length=1)
    k: int = Field(default=10, ge=1, le=100)
    max_results_per_source: int = Field(default=10, ge=1, le=100)
    deep_budget: ResearchBudget = Field(
        default_factory=lambda: ResearchBudget(
            max_rounds=2,
            max_queries=24,
            max_pages_to_fetch=8,
            max_total_runtime_minutes=10,
        )
    )

    @model_validator(mode="after")
    def require_queries(self) -> "BenchmarkCase":
        if not any(self.queries.values()):
            raise ValueError("at least one benchmark query is required")
        if any(not aliases for aliases in self.relevant_entities):
            raise ValueError("each relevant entity requires at least one canonical-key alias")
        return self

    def normalize_keys(self, keys: Iterable[str]) -> tuple[list[str], set[str]]:
        aliases = {
            alias: f"entity:{self.case_id}:{index}"
            for index, entity_aliases in enumerate(self.relevant_entities)
            for alias in entity_aliases
        }
        relevant = {
            f"entity:{self.case_id}:{index}"
            for index in range(len(self.relevant_entities))
        }
        return [aliases.get(key, key) for key in keys], relevant


class BenchmarkSuite(StrictModel):
    name: str = Field(min_length=1)
    cases: list[BenchmarkCase] = Field(min_length=1)


class BenchmarkModeResult(StrictModel):
    mode: BenchmarkMode
    duration_seconds: float = Field(ge=0)
    retrieved_keys: list[str]
    failures: list[str]
    metrics: QualityEvaluation


class BenchmarkCaseResult(StrictModel):
    case_id: str
    question: str
    modes: list[BenchmarkModeResult]


class BenchmarkReport(StrictModel):
    suite: str
    generated_at: datetime
    cases: list[BenchmarkCaseResult]


def load_suite(path: Path) -> BenchmarkSuite:
    return BenchmarkSuite.model_validate_json(path.read_text(encoding="utf-8-sig"))


def save_report(report: BenchmarkReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _responses_to_metrics(
    case: BenchmarkCase,
    responses: Iterable[QuickSearchResponse],
    duration: float,
    mode: BenchmarkMode,
) -> BenchmarkModeResult:
    keys: list[str] = []
    sources: list[str] = []
    languages: list[str] = []
    primary_flags: list[bool] = []
    failures: list[str] = []
    seen: set[str] = set()
    for response in responses:
        failures.extend(f"{item.source_id}:{item.code}" for item in response.failures)
        for merged in response.results:
            if merged.canonical_key in seen:
                continue
            seen.add(merged.canonical_key)
            keys.append(merged.canonical_key)
            sources.append(merged.primary.source_id)
            languages.append(merged.primary.original_language or "unknown")
            primary_flags.append(merged.primary.result_type in _PRIMARY_TYPES)
    normalized_keys, relevant = case.normalize_keys(keys)
    return BenchmarkModeResult(
        mode=mode,
        duration_seconds=duration,
        retrieved_keys=keys,
        failures=failures,
        metrics=evaluate_quality(
            normalized_keys,
            relevant,
            source_ids=sources,
            languages=languages,
            primary_flags=primary_flags,
            k=case.k,
        ),
    )


async def _quick_mode(
    runtime: "OmniRuntime", case: BenchmarkCase, mode: BenchmarkMode
) -> BenchmarkModeResult:
    query_pairs = [
        (language, query)
        for language, queries in case.queries.items()
        for query in queries
    ]
    if mode != "multilingual":
        query_pairs = query_pairs[:1]
    sources = case.sources[:1] if mode == "single_source" else case.sources
    started = monotonic()
    responses = await asyncio.gather(
        *(
            runtime.search.search(
                QuickSearchCommand(
                    query=query,
                    language=language,
                    sources=sources,
                    max_results_per_source=case.max_results_per_source,
                )
            )
            for language, query in query_pairs
        )
    )
    return _responses_to_metrics(case, responses, monotonic() - started, mode)


async def _deep_mode(runtime: "OmniRuntime", case: BenchmarkCase) -> BenchmarkModeResult:
    started = monotonic()
    task = runtime.coordinator.create(
        ResearchCreate(question=case.question, budget=case.deep_budget)
    )
    report = await run_with_runtime_budget(runtime.research, runtime.repository, task.id)
    records = runtime.repository.result_records(task.id, max(case.k, 100))
    keys = [record["canonical_key"] for record in records]
    normalized_keys, relevant = case.normalize_keys(keys)
    return BenchmarkModeResult(
        mode="deep_research",
        duration_seconds=monotonic() - started,
        retrieved_keys=keys,
        failures=(list(report.acquisition_failures) if report else ["runtime_budget_exhausted"]),
        metrics=evaluate_quality(
            normalized_keys,
            relevant,
            source_ids=[record["source_id"] for record in records],
            languages=[record["original_language"] or "unknown" for record in records],
            primary_flags=[record["result_type"] in _PRIMARY_TYPES for record in records],
            k=case.k,
        ),
    )


async def run_benchmark(
    runtime: "OmniRuntime",
    suite: BenchmarkSuite,
    modes: list[BenchmarkMode] | None = None,
) -> BenchmarkReport:
    selected = modes or ["single_source", "multi_source", "multilingual", "deep_research"]
    case_results: list[BenchmarkCaseResult] = []
    for case in suite.cases:
        mode_results: list[BenchmarkModeResult] = []
        for mode in selected:
            if mode == "deep_research":
                mode_results.append(await _deep_mode(runtime, case))
            else:
                mode_results.append(await _quick_mode(runtime, case, mode))
        case_results.append(
            BenchmarkCaseResult(case_id=case.case_id, question=case.question, modes=mode_results)
        )
    return BenchmarkReport(
        suite=suite.name,
        generated_at=datetime.now(UTC),
        cases=case_results,
    )
