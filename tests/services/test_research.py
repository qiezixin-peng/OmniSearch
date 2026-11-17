import asyncio
from collections.abc import AsyncIterator

import pytest

from omnisearch.core.planning import SearchPlanner
from omnisearch.core.research import ResearchCoordinator
from omnisearch.domain.enums import Channel, ResearchStatus, SourceCategory, SourceRole
from omnisearch.domain.models import (
    ProviderCapabilities,
    ResearchBudget,
    ResearchCreate,
    SearchRequest,
    SearchResult,
    SourceDefinition,
)
from omnisearch.providers.base import ProviderAdapter
from omnisearch.providers.registry import SourceRegistry
from omnisearch.services.acquisition import AcquiredDocument
from omnisearch.services.research import ResearchCancelled, ResearchExecutionService
from omnisearch.services.search import MultiSourceSearchService, QuickSearchCommand
from omnisearch.storage.database import Database
from omnisearch.storage.documents import DocumentRepository
from omnisearch.storage.repository import SqlResearchRepository


class AlwaysFindsAdapter(ProviderAdapter):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="test_web",
            adapter_id="test_web_api",
            category=SourceCategory.WEB,
            channel=Channel.API,
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        self.calls += 1
        yield SearchResult(
            source_id="test_web",
            adapter_id="test_web_api",
            channel=Channel.API,
            result_type="web",
            title=f"Evidence for {request.query}",
            url=f"https://example.test/{request.query_id}",
        )


class FakeAcquisition:
    async def fetch(self, command) -> AcquiredDocument:
        url = str(command.url)
        return AcquiredDocument(
            url=url,
            final_url=url,
            content_type="text/html",
            acquisition_mode="fake",
            text="Detailed primary evidence. " * 100,
        )


class BlockingAdapter(AlwaysFindsAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        yield SearchResult(
            source_id="test_web",
            adapter_id="test_web_api",
            channel=Channel.API,
            result_type="web",
            title="Evidence",
            url=f"https://example.test/{request.query_id}",
        )

def make_registry() -> SourceRegistry:
    return SourceRegistry(
        [
            SourceDefinition(
                source_id="test_web",
                display_name="Test Web",
                category=SourceCategory.WEB,
                role=SourceRole.DISCOVERY,
                priority=0,
                adapters=[AlwaysFindsAdapter().capabilities],
            )
        ]
    )


@pytest.mark.asyncio
async def test_executes_and_persists_an_automatic_research_round(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'research.db').as_posix()}")
    database.create_schema()
    repository = SqlResearchRepository(database)
    coordinator = ResearchCoordinator(repository)
    created = coordinator.create(
        ResearchCreate(
            question="audio watermark mechanism",
            budget=ResearchBudget(max_results_per_source=2, max_rounds=1),
        )
    )
    executor = ResearchExecutionService(
        coordinator,
        repository,
        SearchPlanner(make_registry()),
        MultiSourceSearchService([AlwaysFindsAdapter()]),
    )

    report = await executor.run(created.id)

    assert report.status is ResearchStatus.COMPLETED
    assert report.query_coverage == 1
    assert report.unique_results == len(report.query_executions)
    restored = coordinator.get(created.id)
    assert restored.status is ResearchStatus.COMPLETED
    assert restored.sub_questions
    assert all(question.completed for question in restored.sub_questions)
    assert report.sub_question_coverage == 1
    assert repository.search_summary(created.id) == {
        "queries": len(report.query_executions),
        "unique_results": report.unique_results,
    }
    assert [event.event_type for event in coordinator.events(created.id)] == [
        "RESEARCH_CREATED",
        "SEARCH_PLAN_CREATED",
        "SEARCH_STARTED",
        "RESULTS_MERGED",
        "RESEARCH_COMPLETED",
    ]


@pytest.mark.asyncio
async def test_runs_a_gap_driven_follow_up_round(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'recursive.db').as_posix()}")
    database.create_schema()
    repository = SqlResearchRepository(database)
    coordinator = ResearchCoordinator(repository)
    created = coordinator.create(
        ResearchCreate(
            question="audio watermark mechanism",
            budget=ResearchBudget(max_rounds=3, max_queries=30, max_pages_to_fetch=0),
        )
    )
    executor = ResearchExecutionService(
        coordinator,
        repository,
        SearchPlanner(make_registry()),
        MultiSourceSearchService([AlwaysFindsAdapter()]),
    )

    report = await executor.run(created.id)

    assert len(report.plan.queries) > 2
    events = [event.event_type for event in coordinator.events(created.id)]
    assert "GAP_IDENTIFIED" in events
    assert "NEXT_ROUND_COMPLETED" in events


@pytest.mark.asyncio
async def test_continues_a_partial_research_from_persisted_plan(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'continue.db').as_posix()}")
    database.create_schema()
    repository = SqlResearchRepository(database)
    coordinator = ResearchCoordinator(repository)
    created = coordinator.create(
        ResearchCreate(
            question="audio watermark mechanism",
            budget=ResearchBudget(max_rounds=1, max_pages_to_fetch=0),
        )
    )
    executor = ResearchExecutionService(
        coordinator,
        repository,
        SearchPlanner(make_registry()),
        MultiSourceSearchService([]),
    )
    first = await executor.run(created.id)
    assert first.status is ResearchStatus.PARTIAL

    executor.search = MultiSourceSearchService([AlwaysFindsAdapter()])
    continued = await executor.run(created.id)

    assert continued.status is ResearchStatus.COMPLETED
    events = [event.event_type for event in coordinator.events(created.id)]
    assert events.count("SEARCH_STARTED") == 2


@pytest.mark.asyncio
async def test_acquired_documents_are_linked_to_research_evidence(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'evidence.db').as_posix()}")
    database.create_schema()
    repository = SqlResearchRepository(database)
    documents = DocumentRepository(database)
    coordinator = ResearchCoordinator(repository)
    created = coordinator.create(
        ResearchCreate(
            question="audio watermark mechanism",
            budget=ResearchBudget(
                max_rounds=1,
                max_pages_to_fetch=2,
                max_results_per_source=2,
            ),
        )
    )
    executor = ResearchExecutionService(
        coordinator,
        repository,
        SearchPlanner(make_registry()),
        MultiSourceSearchService([AlwaysFindsAdapter()]),
        acquisition=FakeAcquisition(),
        documents=documents,
    )

    report = await executor.run(created.id)
    evidence = documents.evidence_for_research(created.id)

    assert report.documents_acquired == 2
    assert len(evidence) == 2
    assert all(item["content_length"] > 1000 for item in evidence.values())


@pytest.mark.asyncio
async def test_planned_research_skips_checkpointed_queries(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'checkpoint.db').as_posix()}")
    database.create_schema()
    repository = SqlResearchRepository(database)
    coordinator = ResearchCoordinator(repository)
    created = coordinator.create(
        ResearchCreate(
            question="audio watermark mechanism",
            budget=ResearchBudget(max_rounds=1, max_pages_to_fetch=0),
        )
    )
    planner = SearchPlanner(make_registry())
    plan = planner.plan(created.question, created.budget)
    repository.apply_plan(created.id, plan)
    adapter = AlwaysFindsAdapter()
    search = MultiSourceSearchService([adapter])
    first = plan.queries[0]
    repository.mark_query_started(first.id)
    repository.persist_search_response(
        first.id,
        await search.search(
            QuickSearchCommand(
                query=first.query,
                sources=first.sources,
                language=first.language,
            )
        ),
    )

    executor = ResearchExecutionService(coordinator, repository, planner, search)
    report = await executor.run(created.id)

    assert report.status is ResearchStatus.COMPLETED
    assert adapter.calls == len(plan.queries)
    assert repository.query_progress(created.id) == {
        "total": len(plan.queries),
        "pending": 0,
        "running": 0,
        "completed": len(plan.queries),
        "failed": 0,
        "cancelled": 0,
        "with_results": len(plan.queries),
    }


@pytest.mark.asyncio
async def test_running_research_stops_scheduling_after_cancellation(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'cancel.db').as_posix()}")
    database.create_schema()
    repository = SqlResearchRepository(database)
    coordinator = ResearchCoordinator(repository)
    created = coordinator.create(
        ResearchCreate(
            question="audio watermark mechanism",
            budget=ResearchBudget(max_rounds=1, max_pages_to_fetch=0),
        )
    )
    adapter = BlockingAdapter()
    executor = ResearchExecutionService(
        coordinator,
        repository,
        SearchPlanner(make_registry()),
        MultiSourceSearchService([adapter]),
        max_concurrency=1,
    )

    running = asyncio.create_task(executor.run(created.id))
    await asyncio.wait_for(adapter.started.wait(), timeout=2)
    coordinator.change_status(created.id, ResearchStatus.CANCELLED)
    adapter.release.set()

    with pytest.raises(ResearchCancelled):
        await running
    assert coordinator.get(created.id).status is ResearchStatus.CANCELLED
    assert adapter.calls == 1
    progress = repository.query_progress(created.id)
    assert progress["running"] == 0
    assert progress["pending"] == 0
    assert progress["cancelled"] == progress["total"] - progress["completed"]
