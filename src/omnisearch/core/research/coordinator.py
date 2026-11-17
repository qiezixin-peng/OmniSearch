from datetime import UTC, datetime
from uuid import UUID

from omnisearch.core.research.state_machine import ResearchStateMachine
from omnisearch.domain.enums import ResearchStatus
from omnisearch.domain.models import ResearchCreate, ResearchEvent, ResearchTask, SubQuestion
from omnisearch.storage.repository import SqlResearchRepository


class ResearchCoordinator:
    def __init__(self, repository: SqlResearchRepository) -> None:
        self.repository = repository

    def create(self, command: ResearchCreate) -> ResearchTask:
        task = ResearchTask(
            question=command.question,
            goal=command.goal,
            original_language=command.original_language,
            budget=command.budget,
        )
        task.sub_questions = [
            SubQuestion(
                research_id=task.id,
                ordinal=index,
                **item.model_dump(),
            )
            for index, item in enumerate(command.sub_questions)
        ]
        known_ids = {item.id for item in task.sub_questions}
        for item in task.sub_questions:
            unknown = set(item.depends_on) - known_ids
            if unknown:
                raise ValueError(f"unknown sub-question dependencies: {sorted(map(str, unknown))}")
        event = ResearchEvent(
            research_id=task.id,
            event_type="RESEARCH_CREATED",
            sequence=1,
            payload={"sub_questions_count": len(task.sub_questions)},
        )
        self.repository.add(task, event)
        return task

    def get(self, research_id: UUID) -> ResearchTask:
        return self.repository.get(research_id)

    def change_status(self, research_id: UUID, target: ResearchStatus) -> ResearchTask:
        task = self.repository.get(research_id)
        previous = task.status
        task.status = ResearchStateMachine.transition(previous, target)
        task.updated_at = datetime.now(UTC)
        self.repository.transition(
            task,
            event_type=self._event_name(target),
            payload={"from": previous.value, "to": target.value},
        )
        if target is ResearchStatus.CANCELLED:
            self.repository.cancel_unfinished_queries(research_id)
        return task

    def events(self, research_id: UUID) -> list[ResearchEvent]:
        self.repository.get(research_id)
        return self.repository.events(research_id)

    @staticmethod
    def _event_name(status: ResearchStatus) -> str:
        names = {
            ResearchStatus.PLANNED: "SEARCH_PLAN_CREATED",
            ResearchStatus.RUNNING: "SEARCH_STARTED",
            ResearchStatus.PAUSED: "RESEARCH_PAUSED",
            ResearchStatus.PARTIAL: "RESEARCH_PARTIALLY_COMPLETED",
            ResearchStatus.COMPLETED: "RESEARCH_COMPLETED",
            ResearchStatus.FAILED: "RESEARCH_FAILED",
            ResearchStatus.CANCELLED: "RESEARCH_CANCELLED",
        }
        return names[status]
