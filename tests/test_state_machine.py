import pytest

from omnisearch.core.research.state_machine import InvalidTransition, ResearchStateMachine
from omnisearch.domain.enums import ResearchStatus


def test_happy_path() -> None:
    status = ResearchStatus.DRAFT
    for target in (
        ResearchStatus.PLANNED,
        ResearchStatus.RUNNING,
        ResearchStatus.COMPLETED,
    ):
        status = ResearchStateMachine.transition(status, target)
    assert status is ResearchStatus.COMPLETED


def test_completed_is_terminal() -> None:
    with pytest.raises(InvalidTransition):
        ResearchStateMachine.transition(ResearchStatus.COMPLETED, ResearchStatus.RUNNING)
