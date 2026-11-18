import asyncio
import re
from collections import Counter
from uuid import UUID

from pydantic import Field

from omnisearch.core.planning import PlannedQuery, ResearchPlan, SearchPlanner
from omnisearch.core.research import ResearchCoordinator
from omnisearch.core.research.gaps import GapAnalyzer
from omnisearch.domain.enums import ResearchStatus, SourceCategory
from omnisearch.domain.models import StrictModel
from omnisearch.services.acquisition import AcquisitionRequest, DocumentAcquisitionService
from omnisearch.services.search import (
    MultiSourceSearchService,
    QuickSearchCommand,
    QuickSearchResponse,
    SourceFailure,
)
from omnisearch.storage.documents import DocumentRepository
from omnisearch.storage.repository import SqlResearchRepository


class QueryExecution(StrictModel):
    query_id: UUID
    sub_question_id: UUID
    language: str
    query: str
    sources: list[str]
    unique_results: int
    failures: list[SourceFailure]


class ResearchRunReport(StrictModel):
    research_id: UUID
    status: ResearchStatus
    plan: ResearchPlan
    query_executions: list[QueryExecution]
    unique_results: int = Field(ge=0)
    query_coverage: float = Field(ge=0, le=1)
    sub_question_coverage: float = Field(ge=0, le=1)
    documents_acquired: int = Field(default=0, ge=0)
    acquisition_failures: list[str] = Field(default_factory=list)
    quality_gate_passed: bool = True
    quality_issues: list[str] = Field(default_factory=list)


class ResearchCancelled(RuntimeError):
    pass


async def run_with_runtime_budget(
    execution: "ResearchExecutionService",
    repository: SqlResearchRepository,
    research_id: UUID,
    *,
    timeout_seconds: float | None = None,
) -> ResearchRunReport | None:
    task = repository.get(research_id)
    timeout = timeout_seconds or task.budget.max_total_runtime_minutes * 60
    try:
        return await asyncio.wait_for(execution.run(research_id), timeout=timeout)
    except TimeoutError:
        reset = repository.reset_running_queries(research_id)
        repository.record_event(
            research_id,
            "BUDGET_EXHAUSTED",
            {
                "budget": "max_total_runtime_minutes",
                "limit_seconds": timeout,
                "queries_reset": reset,
            },
        )
        current = repository.get(research_id)
        if current.status is ResearchStatus.RUNNING:
            execution.coordinator.change_status(research_id, ResearchStatus.PARTIAL)
        return None


class ResearchExecutionService:
    """Plans and runs a persisted first research round with bounded concurrency."""

    def __init__(
        self,
        coordinator: ResearchCoordinator,
        repository: SqlResearchRepository,
        planner: SearchPlanner,
        search: MultiSourceSearchService,
        *,
        max_concurrency: int = 4,
        acquisition: DocumentAcquisitionService | None = None,
        documents: DocumentRepository | None = None,
        max_acquisition_concurrency: int = 3,
        gap_analyzer: GapAnalyzer | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.repository = repository
        self.planner = planner
        self.search = search
        self.max_concurrency = max_concurrency
        self.acquisition = acquisition
        self.documents = documents
        self.max_acquisition_concurrency = max_acquisition_concurrency
        self.gap_analyzer = gap_analyzer or GapAnalyzer(search.source_channels)

    async def run(self, research_id: UUID) -> ResearchRunReport:
        task = self.coordinator.get(research_id)
        if task.status is ResearchStatus.DRAFT:
            plan = await self.planner.plan_async(task.question, task.budget)
            self.repository.apply_plan(research_id, plan)
            self.coordinator.change_status(research_id, ResearchStatus.RUNNING)
        elif task.status is ResearchStatus.PLANNED:
            plan = self.repository.load_plan(research_id)
            self.coordinator.change_status(research_id, ResearchStatus.RUNNING)
        elif task.status is ResearchStatus.RUNNING:
            plan = self.repository.load_plan(research_id)
        elif task.status is ResearchStatus.PARTIAL:
            plan = self.repository.load_plan(research_id)
            self.coordinator.change_status(research_id, ResearchStatus.RUNNING)
        else:
            raise ValueError(f"research task cannot execute from status {task.status.value}")

        self._restore_plan_categories(plan)

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def execute(query: PlannedQuery) -> tuple[QueryExecution, QuickSearchResponse]:
            async with semaphore:
                self._ensure_active(research_id)
                self.repository.mark_query_started(query.id)
                response = await self.search.search(
                    QuickSearchCommand(
                        query=query.query,
                        sources=query.sources,
                        language=query.language,
                        max_results_per_source=task.budget.max_results_per_source,
                    )
                )
                self.repository.persist_search_response(query.id, response)
                return (
                    QueryExecution(
                        query_id=query.id,
                        sub_question_id=query.sub_question_id,
                        language=query.language,
                        query=query.query,
                        sources=query.sources,
                        unique_results=len(response.results),
                        failures=response.failures,
                    ),
                    response,
                )

        self.repository.update_current_round(research_id, 1)
        pending_ids = self.repository.pending_query_ids(research_id)
        initial_queries = [query for query in plan.queries if query.id in pending_ids]
        outcomes = await asyncio.gather(*(execute(query) for query in initial_queries))
        executions = [outcome[0] for outcome in outcomes]
        response_history = [
            (query, outcome[1]) for query, outcome in zip(initial_queries, outcomes, strict=True)
        ]
        for round_number in range(2, task.budget.max_rounds + 1):
            self._ensure_active(research_id)
            remaining = task.budget.max_queries - len(plan.queries)
            next_queries = self.gap_analyzer.next_queries(
                plan, response_history, remaining=remaining
            )
            if not next_queries:
                break
            self.repository.record_event(
                research_id,
                "GAP_IDENTIFIED",
                {"round": round_number, "new_queries": len(next_queries)},
            )
            self.repository.update_current_round(research_id, round_number)
            for query in next_queries:
                self.repository.add_query_variant(research_id, query, round_number)
            before = self.repository.search_summary(research_id)["unique_results"]
            plan.queries.extend(next_queries)
            round_outcomes = await asyncio.gather(*(execute(query) for query in next_queries))
            executions.extend(outcome[0] for outcome in round_outcomes)
            response_history.extend(
                (query, outcome[1])
                for query, outcome in zip(next_queries, round_outcomes, strict=True)
            )
            after = self.repository.search_summary(research_id)["unique_results"]
            self.repository.record_event(
                research_id,
                "NEXT_ROUND_COMPLETED",
                {"round": round_number, "new_unique_results": after - before},
            )
            if after == before:
                break
        progress = self.repository.query_progress(research_id)
        coverage = progress["with_results"] / progress["total"] if progress["total"] else 0.0
        strict_quality = self.planner.require_dual_channel
        evidence_coverage = self.repository.evidence_coverage(research_id)
        covered_sub_questions = (
            {
                sub_question_id
                for sub_question_id, evidence in evidence_coverage.items()
                if len(evidence["sources"]) >= 2
            }
            if strict_quality
            else self.repository.covered_sub_question_ids(research_id)
        )
        sub_question_coverage = (
            len(covered_sub_questions) / len(plan.sub_questions) if plan.sub_questions else 0.0
        )
        self.repository.update_sub_question_coverage(research_id, covered_sub_questions)
        summary = self.repository.search_summary(research_id)
        adaptive_page_limit = min(
            task.budget.max_pages_to_fetch,
            max(6, len(plan.sub_questions) * 2),
        )
        documents_acquired, acquisition_failures = await self._acquire_documents(
            research_id, adaptive_page_limit
        )
        self._ensure_active(research_id)
        quality_issues = (
            self._quality_issues(
                plan,
                evidence_coverage,
                acquired_coverage=self.repository.acquired_evidence_coverage(research_id),
                query_coverage=coverage,
                documents_acquired=documents_acquired,
                require_documents=task.budget.max_pages_to_fetch > 0,
            )
            if strict_quality
            else []
        )
        if strict_quality and any(
            failure.code == "STALE_CACHE_USED"
            for execution in executions
            for failure in execution.failures
        ):
            quality_issues.append("live_provider_path_not_recovered")
        final_status = (
            ResearchStatus.COMPLETED
            if summary["unique_results"] > 0
            and sub_question_coverage >= (1.0 if strict_quality else 0.75)
            and not quality_issues
            else ResearchStatus.PARTIAL
        )
        failure_breakdown = Counter(
            (failure.source_id, failure.code)
            for execution in executions
            for failure in execution.failures
        )
        self.repository.record_event(
            research_id,
            "RESULTS_MERGED",
            {
                **summary,
                "query_coverage": coverage,
                "sub_question_coverage": sub_question_coverage,
                "source_failures": sum(
                    count
                    for (source_id, code), count in failure_breakdown.items()
                    if code != "CIRCUIT_OPEN"
                ),
                "source_circuit_skips": sum(
                    count
                    for (source_id, code), count in failure_breakdown.items()
                    if code == "CIRCUIT_OPEN"
                ),
                "source_failure_breakdown": [
                    {"source_id": source_id, "code": code, "count": count}
                    for (source_id, code), count in sorted(failure_breakdown.items())
                ],
                "documents_acquired": documents_acquired,
                "documents_requested": task.budget.max_pages_to_fetch,
                "documents_selected": adaptive_page_limit,
                "acquisition_failures": len(acquisition_failures),
                "acquisition_failure_details": acquisition_failures[:20],
                "quality_gate_passed": not quality_issues,
                "quality_issues": quality_issues,
            },
        )
        completed = self.coordinator.change_status(research_id, final_status)
        return ResearchRunReport(
            research_id=research_id,
            status=completed.status,
            plan=plan,
            query_executions=executions,
            unique_results=summary["unique_results"],
            query_coverage=coverage,
            sub_question_coverage=sub_question_coverage,
            documents_acquired=documents_acquired,
            acquisition_failures=acquisition_failures,
            quality_gate_passed=not quality_issues,
            quality_issues=quality_issues,
        )

    @staticmethod
    def _quality_issues(
        plan: ResearchPlan,
        coverage: dict[UUID, dict[str, set[str]]],
        *,
        acquired_coverage: dict[UUID, dict[str, set[str]]],
        query_coverage: float,
        documents_acquired: int,
        require_documents: bool,
    ) -> list[str]:
        issues: list[str] = []
        observed_channels = {
            channel for evidence in coverage.values() for channel in evidence["channels"]
        }
        for channel in ("api", "browser"):
            if channel not in observed_channels:
                issues.append(f"missing_channel_evidence:{channel}")
        if query_coverage < 0.8:
            issues.append(f"insufficient_query_coverage:{query_coverage:.3f}")
        for sub_question in plan.sub_questions:
            evidence = coverage.get(sub_question.id, {})
            source_count = len(evidence.get("sources", set()))
            if source_count < 2:
                issues.append(f"insufficient_independent_sources:{sub_question.id}:{source_count}")
            if SourceCategory.CODE in sub_question.categories and not (
                {"repository", "code"} & evidence.get("result_types", set())
            ):
                issues.append(f"missing_code_evidence:{sub_question.id}")
            acquired = acquired_coverage.get(
                sub_question.id, {"result_types": set(), "hosts": set()}
            )
            if SourceCategory.CODE in sub_question.categories and not (
                {"repository", "code"} & acquired["result_types"]
            ):
                issues.append(f"no_acquired_code_document:{sub_question.id}")
            entity_tokens = {
                token.lower()
                for token in re.findall(
                    r"(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9_-]{2,})",
                    sub_question.objective,
                )
                if token.lower() not in {"api", "pdf"}
            }
            requires_official = any(
                marker in requirement.lower()
                for requirement in sub_question.evidence_requirements
                for marker in ("官方", "一手", "first-party", "primary source")
            )
            hosts = evidence.get("hosts", set())
            if requires_official and entity_tokens and not any(
                token in host.split(".") for token in entity_tokens for host in hosts
            ):
                issues.append(f"missing_official_entity_source:{sub_question.id}")
            acquired_hosts = acquired["hosts"]
            if requires_official and entity_tokens and not any(
                token in host.split(".")
                for token in entity_tokens
                for host in acquired_hosts
            ):
                issues.append(f"no_acquired_official_document:{sub_question.id}")
            if require_documents and SourceCategory.ACADEMIC in sub_question.categories:
                acquired_types = acquired["result_types"]
                if not any(
                    result_type == "paper"
                    or "article" in result_type
                    or result_type in {"posted-content", "proceedings-article"}
                    for result_type in acquired_types
                ):
                    issues.append(f"no_acquired_academic_document:{sub_question.id}")
        if require_documents and documents_acquired == 0:
            issues.append("no_primary_documents_acquired")
        return issues

    def _restore_plan_categories(self, plan: ResearchPlan) -> None:
        """Rebuild categories because legacy persisted sub-questions omit them."""
        queries_by_question: dict = {}
        for query in plan.queries:
            queries_by_question.setdefault(query.sub_question_id, []).extend(query.sources)
        for sub_question in plan.sub_questions:
            if sub_question.categories:
                continue
            for source_id in queries_by_question.get(sub_question.id, []):
                try:
                    sub_question.categories.add(self.planner.registry.get(source_id).category)
                except LookupError:
                    continue

    def _ensure_active(self, research_id: UUID) -> None:
        if self.coordinator.get(research_id).status is ResearchStatus.CANCELLED:
            raise ResearchCancelled(str(research_id))

    async def _acquire_documents(self, research_id: UUID, limit: int) -> tuple[int, list[str]]:
        if self.acquisition is None or self.documents is None or limit == 0:
            return 0, []
        candidates = self.repository.acquisition_candidates(research_id, limit)
        semaphore = asyncio.Semaphore(self.max_acquisition_concurrency)

        async def acquire(candidate: dict) -> str | None:
            async with semaphore:
                self._ensure_active(research_id)
                url = candidate["url"]
                try:
                    timeout_seconds = (
                        30
                        if candidate["result_type"] in {"paper", "repository", "patent"}
                        or "article" in candidate["result_type"]
                        else 15
                    )
                    if url.startswith("http://arxiv.org/"):
                        url = "https://arxiv.org/" + url.removeprefix("http://arxiv.org/")
                    async with asyncio.timeout(timeout_seconds):
                        document = await self.acquisition.fetch(AcquisitionRequest(url=url))
                    self.documents.store(
                        document,
                        research_id=research_id,
                        result_id=UUID(candidate["result_id"]),
                    )
                    return None
                except Exception as error:
                    return f"{url}: {type(error).__name__}: {error}"

        outcomes = await asyncio.gather(*(acquire(candidate) for candidate in candidates))
        failures = [failure for failure in outcomes if failure is not None]
        return len(candidates) - len(failures), failures
