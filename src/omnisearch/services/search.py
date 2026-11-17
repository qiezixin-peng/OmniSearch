import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from pydantic import Field

from omnisearch.domain.enums import Channel
from omnisearch.domain.models import SearchRequest, SearchResult, StrictModel
from omnisearch.observability import search_failures, search_requests, tracer
from omnisearch.providers.base import ProviderAdapter, ProviderError


@dataclass
class _SourceState:
    consecutive_failures: int = 0
    open_until: float = 0.0


@dataclass
class _CacheEntry:
    expires_at: float
    records: list[SearchResult]


class QuickSearchCommand(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    sources: list[str] = Field(default_factory=lambda: ["github", "arxiv", "crossref", "openalex"])
    language: str | None = None
    max_results_per_source: int = Field(default=10, ge=1, le=100)
    domain: str | None = None


class SourceFailure(StrictModel):
    source_id: str
    adapter_id: str
    code: str
    message: str
    retryable: bool = False


class MergedSearchResult(StrictModel):
    canonical_key: str
    primary: SearchResult
    occurrences: list[SearchResult]


class QuickSearchResponse(StrictModel):
    query: str
    results: list[MergedSearchResult]
    failures: list[SourceFailure]


class SourceHealth(StrictModel):
    source_id: str
    adapter_id: str
    configured: bool
    healthy: bool


class MultiSourceSearchService:
    def __init__(
        self,
        adapters: Iterable[ProviderAdapter],
        *,
        api_timeout_seconds: float = 20.0,
        browser_timeout_seconds: float = 40.0,
        cache_ttl_seconds: float = 900.0,
        cache_max_entries: int = 2048,
    ) -> None:
        self._adapters = {adapter.capabilities.source_id: adapter for adapter in adapters}
        self._semaphores = {
            source_id: asyncio.Semaphore(adapter.capabilities.max_concurrency)
            for source_id, adapter in self._adapters.items()
        }
        self._states = {source_id: _SourceState() for source_id in self._adapters}
        self.api_timeout_seconds = api_timeout_seconds
        self.browser_timeout_seconds = browser_timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self.cache_max_entries = cache_max_entries
        self._cache: dict[tuple, _CacheEntry] = {}
        self._inflight: dict[
            tuple, asyncio.Task[tuple[list[SearchResult], SourceFailure | None]]
        ] = {}
        self._cache_lock = asyncio.Lock()

    @property
    def available_sources(self) -> set[str]:
        return set(self._adapters)

    @property
    def configured_sources(self) -> set[str]:
        return {
            source_id
            for source_id, adapter in self._adapters.items()
            if self._is_configured(adapter)
        }

    @property
    def source_channels(self) -> dict[str, Channel]:
        """Return the channel actually used by each runtime adapter."""
        return {
            source_id: adapter.capabilities.channel for source_id, adapter in self._adapters.items()
        }

    @property
    def configured_source_channels(self) -> dict[str, Channel]:
        return {
            source_id: channel
            for source_id, channel in self.source_channels.items()
            if source_id in self.configured_sources
        }

    async def aclose(self) -> None:
        closers = [
            closer()
            for adapter in self._adapters.values()
            if callable(closer := getattr(adapter, "aclose", None))
        ]
        if closers:
            await asyncio.gather(*closers, return_exceptions=True)

    @staticmethod
    def _is_configured(adapter: ProviderAdapter) -> bool:
        return not adapter.capabilities.requires_credentials or any(
            bool(getattr(adapter, name, None)) for name in ("api_key", "token", "bearer_token")
        )

    async def health(self) -> list[SourceHealth]:
        async def inspect(adapter: ProviderAdapter) -> SourceHealth:
            configured = self._is_configured(adapter)
            healthy = configured and await adapter.healthcheck()
            return SourceHealth(
                source_id=adapter.capabilities.source_id,
                adapter_id=adapter.capabilities.adapter_id,
                configured=configured,
                healthy=healthy,
            )

        return await asyncio.gather(*(inspect(adapter) for adapter in self._adapters.values()))

    async def search(self, command: QuickSearchCommand) -> QuickSearchResponse:
        tasks = [
            self._search_one(source_id, command) for source_id in dict.fromkeys(command.sources)
        ]
        outcomes = await asyncio.gather(*tasks)
        records: list[SearchResult] = []
        failures: list[SourceFailure] = []
        for source_records, failure in outcomes:
            records.extend(source_records)
            if failure is not None:
                failures.append(failure)
        return QuickSearchResponse(
            query=command.query,
            results=self._merge(records),
            failures=failures,
        )

    async def _search_one(
        self, source_id: str, command: QuickSearchCommand
    ) -> tuple[list[SearchResult], SourceFailure | None]:
        adapter = self._adapters.get(source_id)
        if adapter is None:
            return [], SourceFailure(
                source_id=source_id,
                adapter_id="unknown",
                code="NOT_AVAILABLE",
                message="source has no configured runtime adapter",
            )
        key = self._cache_key(source_id, command)
        now = monotonic()
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > now:
                return list(cached.records), None
            if cached is not None:
                self._cache.pop(key, None)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._search_uncached(source_id, command, adapter))
                self._inflight[key] = task
        try:
            outcome = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._cache_lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)
        records, failure = outcome
        if failure is None and self.cache_ttl_seconds > 0:
            async with self._cache_lock:
                self._prune_cache()
                if key not in self._cache and len(self._cache) >= self.cache_max_entries:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = _CacheEntry(
                    expires_at=monotonic() + self.cache_ttl_seconds,
                    records=list(records),
                )
        return list(records), failure

    def _prune_cache(self) -> None:
        now = monotonic()
        expired = [key for key, entry in self._cache.items() if entry.expires_at <= now]
        for key in expired:
            self._cache.pop(key, None)

    async def _search_uncached(
        self,
        source_id: str,
        command: QuickSearchCommand,
        adapter: ProviderAdapter,
    ) -> tuple[list[SearchResult], SourceFailure | None]:
        state = self._states[source_id]
        if state.open_until > monotonic():
            return [], SourceFailure(
                source_id=source_id,
                adapter_id=adapter.capabilities.adapter_id,
                code="CIRCUIT_OPEN",
                message="source is temporarily paused after repeated provider failures",
                retryable=True,
            )
        request = SearchRequest(
            query_id=uuid4(),
            query=command.query,
            language=command.language,
            max_results=command.max_results_per_source,
            domain=command.domain,
        )
        async with self._semaphores[source_id]:
            if state.open_until > monotonic():
                return [], SourceFailure(
                    source_id=source_id,
                    adapter_id=adapter.capabilities.adapter_id,
                    code="CIRCUIT_OPEN",
                    message="source is temporarily paused after repeated provider failures",
                    retryable=True,
                )
            try:
                with tracer.start_as_current_span(
                    "provider.search",
                    attributes={
                        "source.id": source_id,
                        "adapter.id": adapter.capabilities.adapter_id,
                    },
                ):
                    search_requests.add(1, {"source.id": source_id})
                    timeout = (
                        self.browser_timeout_seconds
                        if adapter.capabilities.channel is Channel.BROWSER
                        else self.api_timeout_seconds
                    )
                    async with asyncio.timeout(timeout):
                        records = [item async for item in adapter.search(request)]
                state.consecutive_failures = 0
                state.open_until = 0.0
                return records, None
            except TimeoutError:
                self._record_failure(source_id, "TIMEOUT", True)
                search_failures.add(1, {"source.id": source_id, "error.code": "TIMEOUT"})
                return [], SourceFailure(
                    source_id=source_id,
                    adapter_id=adapter.capabilities.adapter_id,
                    code="TIMEOUT",
                    message=f"source exceeded its {timeout:g}s latency budget",
                    retryable=True,
                )
            except ProviderError as error:
                self._record_failure(source_id, error.code, error.retryable)
                search_failures.add(1, {"source.id": source_id, "error.code": error.code})
                return [], SourceFailure(
                    source_id=source_id,
                    adapter_id=adapter.capabilities.adapter_id,
                    code=error.code,
                    message=str(error),
                    retryable=error.retryable,
                )
            except Exception as error:  # isolate an adapter bug from other sources
                self._record_failure(source_id, "ADAPTER_ERROR", False)
                search_failures.add(1, {"source.id": source_id, "error.code": "ADAPTER_ERROR"})
                return [], SourceFailure(
                    source_id=source_id,
                    adapter_id=adapter.capabilities.adapter_id,
                    code="ADAPTER_ERROR",
                    message=str(error),
                )

    @staticmethod
    def _cache_key(source_id: str, command: QuickSearchCommand) -> tuple:
        return (
            source_id,
            " ".join(command.query.casefold().split()),
            (command.language or "").casefold(),
            command.max_results_per_source,
            (command.domain or "").casefold(),
        )

    def _record_failure(self, source_id: str, code: str, retryable: bool) -> None:
        state = self._states[source_id]
        state.consecutive_failures += 1
        immediate = code in {"RATE_LIMITED", "ACCESS_DENIED", "NOT_CONFIGURED"}
        if immediate or not retryable or state.consecutive_failures >= 2:
            cooldown = 300.0 if code in {"ACCESS_DENIED", "NOT_CONFIGURED"} else 60.0
            state.open_until = monotonic() + cooldown

    @classmethod
    def _merge(cls, records: list[SearchResult]) -> list[MergedSearchResult]:
        buckets: dict[str, list[SearchResult]] = {}
        order: list[str] = []
        for record in records:
            key = cls._canonical_key(record)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(record)
        return [
            MergedSearchResult(
                canonical_key=key,
                primary=buckets[key][0],
                occurrences=buckets[key],
            )
            for key in order
        ]

    @staticmethod
    def _canonical_key(record: SearchResult) -> str:
        identifiers = record.external_identifiers
        for name in ("doi", "arxiv", "github_repo_id", "openalex", "patent"):
            value = identifiers.get(name)
            if value:
                if name == "arxiv":
                    value = re.sub(r"v\d+$", "", value, flags=re.IGNORECASE)
                return f"{name}:{value.lower()}"
        parts = urlsplit(str(record.url))
        host = parts.netloc.lower().removeprefix("www.")
        if host == "arxiv.org":
            match = re.match(
                r"/(?:abs|pdf)/((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))",
                parts.path,
                flags=re.IGNORECASE,
            )
            if match:
                return f"arxiv:{match.group(1).lower()}"
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in {"fbclid", "gclid", "ref", "source"}
        ]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))
