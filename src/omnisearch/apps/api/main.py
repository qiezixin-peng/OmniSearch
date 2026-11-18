from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel

from omnisearch.config import get_settings
from omnisearch.core.evidence import EvidenceEvaluator, EvidenceItem
from omnisearch.core.research.coordinator import ResearchCoordinator
from omnisearch.core.research.state_machine import InvalidTransition
from omnisearch.domain.enums import Channel, ResearchStatus, SourceCategory
from omnisearch.domain.models import ResearchCreate, ResearchEvent, ResearchTask, SourceDefinition
from omnisearch.providers.registry import SourceRegistry
from omnisearch.runtime import build_runtime
from omnisearch.services.acquisition import (
    AcquiredDocument,
    AcquisitionRequest,
    DocumentAcquisitionService,
)
from omnisearch.services.crawl import CrawlReport, CrawlRequest, SiteCrawler
from omnisearch.services.research import (
    ResearchExecutionService,
    ResearchRunReport,
    run_with_runtime_budget,
)
from omnisearch.services.search import (
    MultiSourceSearchService,
    QuickSearchCommand,
    QuickSearchResponse,
    SourceHealth,
)
from omnisearch.storage.documents import DocumentRepository, StoredDocument
from omnisearch.storage.repository import ResearchNotFound


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = build_runtime()
    app.state.runtime = runtime
    app.state.database = runtime.database
    app.state.coordinator = runtime.coordinator
    app.state.source_registry = runtime.sources
    app.state.search_service = runtime.search
    app.state.acquisition_service = runtime.acquisition
    app.state.crawler = runtime.crawler
    app.state.document_repository = runtime.documents
    app.state.research_execution_service = runtime.research
    yield
    await runtime.aclose()


app = FastAPI(
    title="OmniSearch API",
    version="0.1.0",
    description="Agent-first multilingual research search and acquisition API",
    lifespan=lifespan,
)
FastAPIInstrumentor.instrument_app(app)


@app.middleware("http")
async def require_service_api_key(request: Request, call_next):
    configured = get_settings().service_api_key
    expected = configured.get_secret_value() if configured else ""
    if not expected or request.url.path == "/health":
        return await call_next(request)
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    provided = request.headers.get("x-omnisearch-key", "") or bearer
    if not provided or not compare_digest(provided, expected):
        return JSONResponse(
            status_code=401,
            content={"detail": "valid OmniSearch service credentials are required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


def coordinator(request: Request) -> ResearchCoordinator:
    return request.app.state.coordinator


CoordinatorDep = Annotated[ResearchCoordinator, Depends(coordinator)]


def source_registry(request: Request) -> SourceRegistry:
    return request.app.state.source_registry


SourceRegistryDep = Annotated[SourceRegistry, Depends(source_registry)]


def search_service(request: Request) -> MultiSourceSearchService:
    return request.app.state.search_service


SearchServiceDep = Annotated[MultiSourceSearchService, Depends(search_service)]


def research_execution_service(request: Request) -> ResearchExecutionService:
    return request.app.state.research_execution_service


ResearchExecutionDep = Annotated[
    ResearchExecutionService, Depends(research_execution_service)
]


def acquisition_service(request: Request) -> DocumentAcquisitionService:
    return request.app.state.acquisition_service


AcquisitionDep = Annotated[DocumentAcquisitionService, Depends(acquisition_service)]


def crawler_service(request: Request) -> SiteCrawler:
    return request.app.state.crawler


CrawlerDep = Annotated[SiteCrawler, Depends(crawler_service)]


def document_repository(request: Request) -> DocumentRepository:
    return request.app.state.document_repository


DocumentRepositoryDep = Annotated[DocumentRepository, Depends(document_repository)]


class FetchPageResponse(BaseModel):
    document: AcquiredDocument
    stored: StoredDocument


class SearchDocumentsRequest(BaseModel):
    query: str
    research_id: UUID | None = None
    limit: int = 20


class RuntimeBudgetResponse(BaseModel):
    research_id: UUID
    status: str = "partial"
    reason: str = "runtime_budget_exhausted"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/v1/sources", response_model=list[SourceDefinition])
def list_sources(
    registry: SourceRegistryDep,
    category: SourceCategory | None = None,
    channel: Channel | None = None,
) -> list[SourceDefinition]:
    return registry.list(category=category, channel=channel)


@app.get("/v1/sources/{source_id}", response_model=SourceDefinition)
def get_source(source_id: str, registry: SourceRegistryDep) -> SourceDefinition:
    try:
        return registry.get(source_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail="search source not found") from error


@app.get("/v1/source-health", response_model=list[SourceHealth])
async def get_source_health(service: SearchServiceDep) -> list[SourceHealth]:
    return await service.health()


@app.post("/v1/search", response_model=QuickSearchResponse)
async def quick_search(
    command: QuickSearchCommand, service: SearchServiceDep
) -> QuickSearchResponse:
    return await service.search(command)


@app.post("/v1/fetch-page", response_model=FetchPageResponse)
async def fetch_page(
    command: AcquisitionRequest,
    service: AcquisitionDep,
    documents: DocumentRepositoryDep,
) -> FetchPageResponse:
    document = await service.fetch(command)
    return FetchPageResponse(document=document, stored=documents.store(document))


@app.post("/v1/crawl-site", response_model=CrawlReport)
async def crawl_site(command: CrawlRequest, crawler: CrawlerDep) -> CrawlReport:
    return await crawler.crawl(command)


@app.post("/v1/search-documents")
def search_documents(
    command: SearchDocumentsRequest,
    documents: DocumentRepositoryDep,
) -> list[dict]:
    return documents.search_chunks(
        command.query,
        research_id=command.research_id,
        limit=command.limit,
    )


@app.post("/v1/research", response_model=ResearchTask, status_code=status.HTTP_201_CREATED)
def create_research(
    command: ResearchCreate,
    service: CoordinatorDep,
) -> ResearchTask:
    return service.create(command)


@app.get("/v1/research/{research_id}", response_model=ResearchTask)
def get_research(
    research_id: UUID,
    service: CoordinatorDep,
) -> ResearchTask:
    try:
        return service.get(research_id)
    except ResearchNotFound as error:
        raise HTTPException(status_code=404, detail="research task not found") from error


@app.get("/v1/research/{research_id}/events", response_model=list[ResearchEvent])
def get_events(
    research_id: UUID,
    service: CoordinatorDep,
) -> list[ResearchEvent]:
    try:
        return service.events(research_id)
    except ResearchNotFound as error:
        raise HTTPException(status_code=404, detail="research task not found") from error


@app.get("/v1/research/{research_id}/result")
def get_research_result(
    research_id: UUID,
    service: CoordinatorDep,
    documents: DocumentRepositoryDep,
    limit: int = 100,
) -> dict:
    try:
        task = service.get(research_id)
    except ResearchNotFound as error:
        raise HTTPException(status_code=404, detail="research task not found") from error
    repository = service.repository
    merged_event = next(
        (
            event
            for event in reversed(service.events(research_id))
            if event.event_type == "RESULTS_MERGED"
        ),
        None,
    )
    acquired = documents.evidence_for_research(research_id)
    evaluator = EvidenceEvaluator()
    results = []
    for record in repository.result_records(research_id, limit):
        document = acquired.get(record["result_id"])
        assessment = evaluator.evaluate(
            EvidenceItem(
                source_id=record["source_id"],
                result_type=record["result_type"],
                url=record["url"],
                content_length=document["content_length"] if document else 0,
            )
        )
        results.append(
            {
                **record,
                "document": document,
                "evidence_assessment": assessment.model_dump(mode="json"),
            }
        )
    return {
        "research_id": str(research_id),
        "status": task.status.value,
        "summary": repository.search_summary(research_id),
        "query_progress": repository.query_progress(research_id),
        "coverage_matrix": repository.coverage_matrix(research_id),
        "quality": (
            {
                "gate_passed": merged_event.payload.get("quality_gate_passed"),
                "issues": merged_event.payload.get("quality_issues", []),
            }
            if merged_event
            else None
        ),
        "retrieval_failures": (
            merged_event.payload.get("source_failure_breakdown", []) if merged_event else []
        ),
        "acquisition_failures": (
            merged_event.payload.get("acquisition_failure_details", []) if merged_event else []
        ),
        "results": results,
    }


def _transition(
    research_id: UUID,
    target: ResearchStatus,
    service: ResearchCoordinator,
) -> ResearchTask:
    try:
        return service.change_status(research_id, target)
    except ResearchNotFound as error:
        raise HTTPException(status_code=404, detail="research task not found") from error
    except InvalidTransition as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/v1/research/{research_id}/plan", response_model=ResearchTask)
def plan_research(
    research_id: UUID,
    service: CoordinatorDep,
) -> ResearchTask:
    return _transition(research_id, ResearchStatus.PLANNED, service)


@app.post("/v1/research/{research_id}/start", response_model=ResearchTask)
def start_research(
    research_id: UUID,
    service: CoordinatorDep,
) -> ResearchTask:
    return _transition(research_id, ResearchStatus.RUNNING, service)


@app.post(
    "/v1/research/{research_id}/execute",
    response_model=ResearchRunReport | RuntimeBudgetResponse,
)
async def execute_research(
    research_id: UUID,
    service: ResearchExecutionDep,
) -> ResearchRunReport | RuntimeBudgetResponse:
    try:
        report = await run_with_runtime_budget(service, service.repository, research_id)
        return report or RuntimeBudgetResponse(research_id=research_id)
    except ResearchNotFound as error:
        raise HTTPException(status_code=404, detail="research task not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/v1/research/{research_id}/complete", response_model=ResearchTask)
def complete_research(
    research_id: UUID,
    service: CoordinatorDep,
) -> ResearchTask:
    return _transition(research_id, ResearchStatus.COMPLETED, service)


@app.post("/v1/research/{research_id}/cancel", response_model=ResearchTask)
def cancel_research(
    research_id: UUID,
    service: CoordinatorDep,
) -> ResearchTask:
    return _transition(research_id, ResearchStatus.CANCELLED, service)


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "omnisearch.apps.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
    )
