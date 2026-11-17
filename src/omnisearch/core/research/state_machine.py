from omnisearch.domain.enums import ResearchStatus


class InvalidTransition(ValueError):
    def __init__(self, current: ResearchStatus, target: ResearchStatus) -> None:
        super().__init__(f"invalid research state transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


class ResearchStateMachine:
    _transitions: dict[ResearchStatus, frozenset[ResearchStatus]] = {
        ResearchStatus.DRAFT: frozenset({ResearchStatus.PLANNED, ResearchStatus.CANCELLED}),
        ResearchStatus.PLANNED: frozenset(
            {ResearchStatus.RUNNING, ResearchStatus.CANCELLED, ResearchStatus.FAILED}
        ),
        ResearchStatus.RUNNING: frozenset(
            {
                ResearchStatus.PAUSED,
                ResearchStatus.PARTIAL,
                ResearchStatus.COMPLETED,
                ResearchStatus.FAILED,
                ResearchStatus.CANCELLED,
            }
        ),
        ResearchStatus.PAUSED: frozenset(
            {ResearchStatus.RUNNING, ResearchStatus.CANCELLED, ResearchStatus.FAILED}
        ),
        ResearchStatus.PARTIAL: frozenset(
            {ResearchStatus.RUNNING, ResearchStatus.COMPLETED, ResearchStatus.CANCELLED}
        ),
        ResearchStatus.COMPLETED: frozenset(),
        ResearchStatus.FAILED: frozenset({ResearchStatus.PLANNED, ResearchStatus.CANCELLED}),
        ResearchStatus.CANCELLED: frozenset(),
    }

    @classmethod
    def can_transition(cls, current: ResearchStatus, target: ResearchStatus) -> bool:
        return target in cls._transitions[current]

    @classmethod
    def transition(cls, current: ResearchStatus, target: ResearchStatus) -> ResearchStatus:
        if not cls.can_transition(current, target):
            raise InvalidTransition(current, target)
        return target
