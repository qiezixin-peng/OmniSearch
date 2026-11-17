from collections.abc import Iterable

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import SourceDefinition


class DuplicateSource(ValueError):
    pass


class UnknownSource(LookupError):
    pass


class SourceRegistry:
    """Typed capability registry used by the search planner and Agent introspection."""

    def __init__(self, sources: Iterable[SourceDefinition] = ()) -> None:
        self._sources: dict[str, SourceDefinition] = {}
        for source in sources:
            self.register(source)

    def register(self, source: SourceDefinition) -> None:
        if source.source_id in self._sources:
            raise DuplicateSource(source.source_id)
        self._sources[source.source_id] = source

    def get(self, source_id: str) -> SourceDefinition:
        try:
            return self._sources[source_id]
        except KeyError as error:
            raise UnknownSource(source_id) from error

    def list(
        self,
        *,
        category: SourceCategory | None = None,
        channel: Channel | None = None,
        enabled_only: bool = True,
    ) -> list[SourceDefinition]:
        sources = self._sources.values()
        if enabled_only:
            sources = (source for source in sources if source.enabled_by_default)
        if category is not None:
            sources = (source for source in sources if source.category is category)
        if channel is not None:
            sources = (
                source
                for source in sources
                if any(adapter.channel is channel for adapter in source.adapters)
            )
        return sorted(sources, key=lambda source: (source.priority, source.source_id))

    def select(
        self,
        categories: set[SourceCategory],
        *,
        channels: set[Channel] | None = None,
        limit: int = 8,
    ) -> list[SourceDefinition]:
        candidates = [
            source
            for source in self.list()
            if source.category in categories
            and (
                channels is None
                or any(adapter.channel in channels for adapter in source.adapters)
            )
        ]
        return candidates[:limit]
