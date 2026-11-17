import argparse
import asyncio
import os
import socket
from typing import Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer

from omnisearch.core.evidence import EvidenceEvaluator, EvidenceItem
from omnisearch.domain.enums import ResearchStatus
from omnisearch.domain.models import ResearchBudget, ResearchCreate
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
        "Use search for a fast lookup. Use research_start for complex questions, then poll "
        "research_status and retrieve research_get_result. Use fetch_page for deep acquisition."
    ),
)

_runtime: OmniRuntime | None = None
_background_tasks: set[asyncio.Task[Any]] = set()


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


@mcp.tool(description="Create and asynchronously execute a complete research task.")
async def research_start(
    question: str,
    original_language: str = "auto",
    max_rounds: int = 3,
    max_queries: int = 40,
    max_pages_to_fetch: int = 30,
) -> dict[str, Any]:
    engine = runtime()
    task = engine.coordinator.create(
        ResearchCreate(
            question=question,
            original_language=original_language,
            budget=ResearchBudget(
                max_rounds=max_rounds,
                max_queries=max_queries,
                max_pages_to_fetch=max_pages_to_fetch,
            ),
        )
    )
    job_id = engine.jobs.enqueue(
        "research.execute",
        {"research_id": str(task.id)},
        idempotency_key=f"research.execute:{task.id}",
    )
    _schedule_worker()
    return {"research_id": str(task.id), "job_id": str(job_id), "status": "scheduled"}


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


@mcp.tool(description="Retrieve persisted search results for a research task.")
def research_get_result(research_id: str, limit: int = 100) -> dict[str, Any]:
    engine = runtime()
    identifier = UUID(research_id)
    task = engine.coordinator.get(identifier)
    return {
        "research_id": research_id,
        "status": task.status.value,
        "summary": engine.repository.search_summary(identifier),
        "query_progress": engine.repository.query_progress(identifier),
        "coverage_matrix": engine.repository.coverage_matrix(identifier),
        "results": _research_records_with_evidence(engine, identifier, limit),
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
