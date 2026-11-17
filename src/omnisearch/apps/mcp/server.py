import argparse
import asyncio
import os
import socket
from typing import Any
from uuid import UUID, uuid4

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from omnisearch.core.evidence import EvidenceEvaluator, EvidenceItem
from omnisearch.core.planning import PlannedQuery, PlannedSubQuestion, ResearchPlan
from omnisearch.domain.enums import ResearchStatus, SourceCategory
from omnisearch.domain.models import ResearchBudget, ResearchCreate, StrictModel
from omnisearch.quality import evaluate_quality
from omnisearch.runtime import OmniRuntime, build_runtime
from omnisearch.services.acquisition import AcquisitionRequest
from omnisearch.services.crawl import CrawlRequest
from omnisearch.services.search import QuickSearchCommand
from omnisearch.workers import ResearchWorker

mcp = MCPServer(
    "OmniSearch",
    description="Agent-first multilingual, multi-source research and acquisition engine",
    version="0.1.0",
    instructions=(
        "Use search for a fast lookup. For complex questions, the calling Agent must decompose "
        "the problem, create multilingual queries, and call research_start. Research plans use "
        "both API providers and direct browser search by default. Poll research_status, retrieve "
        "research_get_result and get_evidence, then synthesize a cited answer with conflicts and "
        "unresolved gaps; never dump raw search results as the final answer."
    ),
)

_runtime: OmniRuntime | None = None
_background_tasks: set[asyncio.Task[Any]] = set()


class AgentSubQuestion(StrictModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    objective: str = Field(min_length=3, max_length=1000)
    categories: set[SourceCategory] = Field(default_factory=lambda: {SourceCategory.WEB})
    evidence_requirements: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class AgentPlannedQuery(StrictModel):
    sub_question_key: str
    language: str = Field(min_length=2, max_length=35)
    query: str = Field(min_length=1, max_length=2000)
    sources: list[str] = Field(min_length=1)
    purpose: str = Field(default="agent-supplied research query", max_length=1000)


def _build_agent_plan(
    engine: OmniRuntime,
    question: str,
    original_language: str,
    budget: ResearchBudget,
    sub_questions: list[AgentSubQuestion],
    queries: list[AgentPlannedQuery],
) -> ResearchPlan:
    if not sub_questions or not queries:
        raise ValueError("agent-supplied planning requires both sub_questions and queries")
    if len(sub_questions) > budget.max_sub_questions:
        raise ValueError("agent plan exceeds max_sub_questions")
    if len(queries) > budget.max_queries:
        raise ValueError("agent plan exceeds max_queries")
    keys = [item.key for item in sub_questions]
    if len(keys) != len(set(keys)):
        raise ValueError("agent sub-question keys must be unique")
    key_ids = {key: uuid4() for key in keys}
    known = set(key_ids)
    for item in sub_questions:
        unknown = set(item.depends_on) - known
        if unknown:
            raise ValueError(f"unknown sub-question dependencies: {sorted(unknown)}")
        if item.key in item.depends_on:
            raise ValueError(f"sub-question cannot depend on itself: {item.key}")
    query_keys = {item.sub_question_key for item in queries}
    unknown_query_keys = query_keys - known
    if unknown_query_keys:
        raise ValueError(f"queries reference unknown sub-questions: {sorted(unknown_query_keys)}")
    missing_queries = known - query_keys
    if missing_queries:
        raise ValueError(f"sub-questions without queries: {sorted(missing_queries)}")
    requested_sources = {source for query in queries for source in query.sources}
    unknown_sources = requested_sources - engine.search.available_sources
    if unknown_sources:
        raise ValueError(f"unknown search sources: {sorted(unknown_sources)}")
    unconfigured_sources = requested_sources - engine.search.configured_sources
    if unconfigured_sources:
        raise ValueError(f"search sources are not configured: {sorted(unconfigured_sources)}")
    language = (
        original_language
        if original_language != "auto"
        else engine.research.planner.analyzer.detect_language(question)
    )
    plan = ResearchPlan(
        question=question,
        original_language=language,
        budget=budget,
        sub_questions=[
            PlannedSubQuestion(
                id=key_ids[item.key],
                objective=item.objective,
                categories=item.categories,
                evidence_requirements=item.evidence_requirements,
                depends_on=[key_ids[key] for key in item.depends_on],
            )
            for item in sub_questions
        ],
        queries=[
            PlannedQuery(
                sub_question_id=key_ids[item.sub_question_key],
                language=item.language,
                query=item.query,
                sources=list(dict.fromkeys(item.sources)),
                purpose=item.purpose,
            )
            for item in queries
        ],
    )
    return engine.research.planner.ensure_dual_channel(plan)


def _plan_channel_summary(engine: OmniRuntime, plan: ResearchPlan) -> dict[str, list[str]]:
    summary = {"api": [], "browser": []}
    for source in dict.fromkeys(
        source for query in plan.queries for source in query.sources
    ):
        channel = engine.search.source_channels.get(source)
        if channel is not None:
            summary[channel.value].append(source)
    return summary


def runtime() -> OmniRuntime:
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


def _schedule_worker() -> None:
    engine = runtime()
    worker = ResearchWorker(
        f"{socket.gethostname()}-{os.getpid()}",
        engine.jobs,
        engine.research,
        engine.repository,
    )
    background = asyncio.create_task(worker.run_once())
    _background_tasks.add(background)
    background.add_done_callback(_background_tasks.discard)


def _research_records_with_evidence(
    engine: OmniRuntime, research_id: UUID, limit: int
) -> list[dict[str, Any]]:
    evaluator = EvidenceEvaluator()
    documents = engine.documents.evidence_for_research(research_id)
    enriched: list[dict[str, Any]] = []
    for record in engine.repository.result_records(research_id, limit):
        document = documents.get(record["result_id"])
        assessment = evaluator.evaluate(
            EvidenceItem(
                source_id=record["source_id"],
                result_type=record["result_type"],
                url=record["url"],
                content_length=document["content_length"] if document else 0,
            )
        )
        enriched.append(
            {
                **record,
                "document": document,
                "evidence_assessment": assessment.model_dump(mode="json"),
            }
        )
    return enriched


@mcp.tool(description="Run a fast parallel search across selected real sources.")
async def search(
    query: str,
    sources: list[str] | None = None,
    language: str | None = None,
    max_results_per_source: int = 10,
) -> dict[str, Any]:
    response = await runtime().search.search(
        QuickSearchCommand(
            query=query,
            sources=sources or ["google", "duckduckgo", "github", "arxiv", "crossref", "openalex"],
            language=language,
            max_results_per_source=max_results_per_source,
        )
    )
    return response.model_dump(mode="json")


@mcp.tool(description="Run several independent queries concurrently across the search pool.")
async def search_batch(
    queries: list[str],
    sources: list[str] | None = None,
    language: str | None = None,
    max_results_per_source: int = 10,
) -> list[dict[str, Any]]:
    if not 1 <= len(queries) <= 50:
        raise ValueError("queries must contain between 1 and 50 items")
    selected = sources or ["google", "duckduckgo", "github", "arxiv", "crossref", "openalex"]
    semaphore = asyncio.Semaphore(6)

    async def run_query(query: str) -> dict[str, Any]:
        async with semaphore:
            response = await runtime().search.search(
                QuickSearchCommand(
                    query=query,
                    sources=selected,
                    language=language,
                    max_results_per_source=max_results_per_source,
                )
            )
            return response.model_dump(mode="json")

    return await asyncio.gather(*(run_query(query) for query in queries))


@mcp.tool(description="Search an explicit subset of registered sources.")
async def search_sources(
    query: str,
    sources: list[str],
    language: str | None = None,
    max_results_per_source: int = 10,
) -> dict[str, Any]:
    if not sources:
        raise ValueError("at least one source is required")
    return await search(query, sources, language, max_results_per_source)


@mcp.tool(
    description=(
        "Create a multilingual research task. The plan is forced to cover both real APIs and "
        "direct browser search; after completion the calling Agent must synthesize the evidence."
    )
)
async def research_start(
    question: str,
    original_language: str = "auto",
    max_rounds: int = 3,
    max_queries: int = 40,
    max_pages_to_fetch: int = 30,
    sub_questions: list[AgentSubQuestion] | None = None,
    queries: list[AgentPlannedQuery] | None = None,
) -> dict[str, Any]:
    engine = runtime()
    if (sub_questions is None) != (queries is None):
        raise ValueError("provide both sub_questions and queries, or neither")
    budget = ResearchBudget(
        max_rounds=max_rounds,
        max_queries=max_queries,
        max_pages_to_fetch=max_pages_to_fetch,
    )
    task = engine.coordinator.create(
        ResearchCreate(
            question=question,
            original_language=original_language,
            budget=budget,
        )
    )
    planning_mode = "heuristic_fallback"
    if sub_questions is not None and queries is not None:
        plan = _build_agent_plan(
            engine,
            question,
            original_language,
            budget,
            sub_questions,
            queries,
        )
        engine.repository.apply_plan(task.id, plan)
        planning_mode = "agent_supplied"
    else:
        plan = await engine.research.planner.plan_async(question, budget)
        engine.repository.apply_plan(task.id, plan)
    job_id = engine.jobs.enqueue(
        "research.execute",
        {"research_id": str(task.id)},
        idempotency_key=f"research.execute:{task.id}",
    )
    _schedule_worker()
    return {
        "research_id": str(task.id),
        "job_id": str(job_id),
        "status": "scheduled",
        "planning_mode": planning_mode,
        "sub_questions_count": len(plan.sub_questions),
        "queries_count": len(plan.queries),
        "channel_strategy": "required_dual_channel",
        "channel_sources": _plan_channel_summary(engine, plan),
        "agent_next_action": (
            "Poll research_status until completed/partial, retrieve research_get_result and "
            "get_evidence, then answer the user with cited synthesis, conflicts and open gaps."
        ),
    }


@mcp.tool(description="Continue a partially completed persisted research task.")
async def research_continue(research_id: str) -> dict[str, Any]:
    engine = runtime()
    identifier = UUID(research_id)
    task = engine.coordinator.get(identifier)
    if task.status.value != "partial":
        raise ValueError("only a partial research task can be continued")
    event_count = len(engine.coordinator.events(identifier))
    job_id = engine.jobs.enqueue(
        "research.execute",
        {"research_id": research_id},
        idempotency_key=f"research.continue:{research_id}:{event_count}",
    )
    _schedule_worker()
    return {"research_id": research_id, "job_id": str(job_id), "status": "scheduled"}


@mcp.tool(description="Cancel a queued or running research task and stop new provider work.")
def research_cancel(research_id: str) -> dict[str, Any]:
    engine = runtime()
    identifier = UUID(research_id)
    task = engine.coordinator.change_status(identifier, ResearchStatus.CANCELLED)
    return {
        "research_id": research_id,
        "status": task.status.value,
        "cancelled_at": task.updated_at.isoformat(),
    }


@mcp.tool(description="Inspect persisted research state, coverage summary and events.")
async def research_status(research_id: str) -> dict[str, Any]:
    engine = runtime()
    _schedule_worker()
    identifier = UUID(research_id)
    task = engine.coordinator.get(identifier)
    return {
        "task": task.model_dump(mode="json"),
        "search_summary": engine.repository.search_summary(identifier),
        "query_progress": engine.repository.query_progress(identifier),
        "coverage_matrix": engine.repository.coverage_matrix(identifier),
        "events": [
            event.model_dump(mode="json") for event in engine.coordinator.events(identifier)
        ],
    }


@mcp.tool(
    description=(
        "Retrieve persisted evidence for synthesis. The calling Agent must turn it into a direct "
        "cited answer, including conflicts and unresolved gaps."
    )
)
def research_get_result(research_id: str, limit: int = 100) -> dict[str, Any]:
    engine = runtime()
    identifier = UUID(research_id)
    task = engine.coordinator.get(identifier)
    merged_event = next(
        (
            event
            for event in reversed(engine.coordinator.events(identifier))
            if event.event_type == "RESULTS_MERGED"
        ),
        None,
    )
    return {
        "research_id": research_id,
        "status": task.status.value,
        "summary": engine.repository.search_summary(identifier),
        "query_progress": engine.repository.query_progress(identifier),
        "coverage_matrix": engine.repository.coverage_matrix(identifier),
        "quality": (
            {
                "gate_passed": merged_event.payload.get("quality_gate_passed"),
                "issues": merged_event.payload.get("quality_issues", []),
            }
            if merged_event
            else None
        ),
        "results": _research_records_with_evidence(engine, identifier, limit),
        "agent_next_action": (
            "Synthesize a direct answer for the user from these records; cite supporting sources, "
            "state material conflicts, and identify unresolved gaps."
        ),
    }


@mcp.tool(description="Rate a research task's result sources from E1 to E5.")
def get_evidence(research_id: str, limit: int = 100) -> dict[str, Any]:
    engine = runtime()
    identifier = UUID(research_id)
    engine.coordinator.get(identifier)
    return {
        "research_id": research_id,
        "evidence": _research_records_with_evidence(engine, identifier, limit),
    }


@mcp.tool(description="Cross-check an explicit claim against Agent-selected evidence stances.")
def verify_claim(claim: str, evidence: list[EvidenceItem]) -> dict[str, Any]:
    return EvidenceEvaluator().verify_claim(claim, evidence).model_dump(mode="json")


@mcp.tool(description="Compute reproducible retrieval and citation quality metrics.")
def evaluate_search_quality(
    retrieved_keys: list[str],
    relevant_keys: list[str],
    source_ids: list[str] | None = None,
    languages: list[str] | None = None,
    primary_flags: list[bool] | None = None,
    citation_checks: list[bool] | None = None,
    k: int = 10,
) -> dict[str, Any]:
    return evaluate_quality(
        retrieved_keys,
        set(relevant_keys),
        source_ids=source_ids,
        languages=languages,
        primary_flags=primary_flags,
        citation_checks=citation_checks,
        k=k,
    ).model_dump(mode="json")


@mcp.tool(description="Fetch, extract, version and chunk a public web page or PDF.")
async def fetch_page(
    url: str,
    use_browser_fallback: bool = True,
    solve_cloudflare: bool = True,
    respect_robots_txt: bool = True,
) -> dict[str, Any]:
    engine = runtime()
    document = await engine.acquisition.fetch(
        AcquisitionRequest(
            url=url,
            use_browser_fallback=use_browser_fallback,
            solve_cloudflare=solve_cloudflare,
            respect_robots_txt=respect_robots_txt,
        )
    )
    stored = engine.documents.store(document)
    return {
        "document": document.model_dump(mode="json"),
        "stored": stored.model_dump(mode="json"),
    }


@mcp.tool(description="Fetch and persist several public pages or PDFs concurrently.")
async def fetch_batch(
    urls: list[str],
    use_browser_fallback: bool = True,
    solve_cloudflare: bool = True,
) -> list[dict[str, Any]]:
    if not 1 <= len(urls) <= 50:
        raise ValueError("urls must contain between 1 and 50 items")
    engine = runtime()
    semaphore = asyncio.Semaphore(4)

    async def fetch_one(url: str) -> dict[str, Any]:
        async with semaphore:
            try:
                document = await engine.acquisition.fetch(
                    AcquisitionRequest(
                        url=url,
                        use_browser_fallback=use_browser_fallback,
                        solve_cloudflare=solve_cloudflare,
                    )
                )
                stored = engine.documents.store(document)
                return {
                    "url": url,
                    "document": document.model_dump(mode="json"),
                    "stored": stored.model_dump(mode="json"),
                }
            except Exception as error:
                return {"url": url, "error": f"{type(error).__name__}: {error}"}

    return await asyncio.gather(*(fetch_one(url) for url in urls))


@mcp.tool(
    description=(
        "Recursively fetch and persist a public site's pages with explicit page, depth and "
        "concurrency budgets. External domains are excluded by default."
    )
)
async def crawl_site(
    seed_url: str,
    max_pages: int = 20,
    max_depth: int = 2,
    concurrency: int = 4,
    same_host: bool = True,
    allowed_domains: list[str] | None = None,
    use_browser_fallback: bool = True,
    solve_cloudflare: bool = True,
) -> dict[str, Any]:
    report = await runtime().crawler.crawl(
        CrawlRequest(
            seed_url=seed_url,
            max_pages=max_pages,
            max_depth=max_depth,
            concurrency=concurrency,
            same_host=same_host,
            allowed_domains=allowed_domains or [],
            use_browser_fallback=use_browser_fallback,
            solve_cloudflare=solve_cloudflare,
        )
    )
    return report.model_dump(mode="json")


@mcp.tool(description="Read the latest persisted version and chunks of a document.")
def get_document(document_id: str) -> dict[str, Any]:
    return runtime().documents.get(UUID(document_id))


@mcp.tool(
    description=(
        "Search persisted full-text document chunks and return exact source passages, URLs and "
        "offsets. Optionally restrict results to one research task."
    )
)
def search_documents(
    query: str,
    research_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    identifier = UUID(research_id) if research_id else None
    return runtime().documents.search_chunks(query, research_id=identifier, limit=limit)


@mcp.tool(description="List registered search sources and their Agent-visible capabilities.")
def list_sources() -> list[dict[str, Any]]:
    return [source.model_dump(mode="json") for source in runtime().sources.list()]


@mcp.tool(description="Report runtime configuration and health for active source adapters.")
async def get_source_health() -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in await runtime().search.health()]


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            stateless_http=True,
            json_response=True,
        )


if __name__ == "__main__":
    run()
