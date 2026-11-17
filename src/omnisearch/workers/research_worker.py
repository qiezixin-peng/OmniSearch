import asyncio
from uuid import UUID

from omnisearch.domain.enums import ResearchStatus
from omnisearch.services.research import (
    ResearchCancelled,
    ResearchExecutionService,
    run_with_runtime_budget,
)
from omnisearch.storage.repository import SqlResearchRepository
from omnisearch.workers.queue import JobQueue


class ResearchWorker:
    def __init__(
        self,
        worker_id: str,
        queue: JobQueue,
        execution: ResearchExecutionService,
        repository: SqlResearchRepository,
        max_runtime_seconds: float | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.queue = queue
        self.execution = execution
        self.repository = repository
        self.max_runtime_seconds = max_runtime_seconds

    async def run_once(self) -> bool:
        lease = self.queue.claim(self.worker_id, lease_seconds=120)
        if lease is None:
            return False
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(lease, stop_heartbeat))
        try:
            if lease.job_type != "research.execute":
                raise ValueError(f"unsupported job type: {lease.job_type}")
            research_id = UUID(lease.payload["research_id"])
            task = self.repository.get(research_id)
            if task.status not in {ResearchStatus.COMPLETED, ResearchStatus.CANCELLED}:
                timeout = self.max_runtime_seconds or (
                    task.budget.max_total_runtime_minutes * 60
                )
                await run_with_runtime_budget(
                    self.execution,
                    self.repository,
                    research_id,
                    timeout_seconds=timeout,
                )
            self.queue.complete(lease)
        except ResearchCancelled:
            self.queue.complete(lease)
        except Exception as error:
            self.queue.fail(lease, f"{type(error).__name__}: {error}", retry=lease.attempts < 3)
        finally:
            stop_heartbeat.set()
            await heartbeat
        return True

    async def _heartbeat(self, lease, stop: asyncio.Event) -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
                return
            except TimeoutError:
                self.queue.heartbeat(lease, lease_seconds=120)
