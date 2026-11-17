import asyncio

import pytest

from omnisearch.core.research import ResearchCoordinator
from omnisearch.domain.enums import ResearchStatus
from omnisearch.domain.models import ResearchCreate
from omnisearch.services.research import run_with_runtime_budget
from omnisearch.storage.database import Database
from omnisearch.storage.repository import SqlResearchRepository
from omnisearch.workers import JobQueue, ResearchWorker


class SlowExecution:
    def __init__(self, coordinator: ResearchCoordinator) -> None:
        self.coordinator = coordinator

    async def run(self, research_id) -> None:
        self.coordinator.change_status(research_id, ResearchStatus.PLANNED)
        self.coordinator.change_status(research_id, ResearchStatus.RUNNING)
        await asyncio.sleep(60)


@pytest.mark.asyncio
async def test_worker_runtime_budget_marks_research_partial(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'worker-timeout.db').as_posix()}")
    database.create_schema()
    repository = SqlResearchRepository(database)
    coordinator = ResearchCoordinator(repository)
    created = coordinator.create(ResearchCreate(question="long-running research"))
    queue = JobQueue(database)
    queue.enqueue(
        "research.execute",
        {"research_id": str(created.id)},
        f"research.execute:{created.id}",
    )
    worker = ResearchWorker(
        "test-worker",
        queue,
        SlowExecution(coordinator),
        repository,
        max_runtime_seconds=0.02,
    )

    assert await worker.run_once() is True
    assert coordinator.get(created.id).status is ResearchStatus.PARTIAL
    events = coordinator.events(created.id)
    assert any(event.event_type == "BUDGET_EXHAUSTED" for event in events)
    assert queue.claim("other-worker") is None


@pytest.mark.asyncio
async def test_shared_runtime_budget_can_protect_direct_execution(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'direct-timeout.db').as_posix()}")
    database.create_schema()
    repository = SqlResearchRepository(database)
    coordinator = ResearchCoordinator(repository)
    created = coordinator.create(ResearchCreate(question="direct long-running research"))

    report = await run_with_runtime_budget(
        SlowExecution(coordinator), repository, created.id, timeout_seconds=0.02
    )

    assert report is None
    assert coordinator.get(created.id).status is ResearchStatus.PARTIAL
