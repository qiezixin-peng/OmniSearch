import asyncio
from collections.abc import AsyncIterator
from time import monotonic

import pytest

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult
from omnisearch.providers.base import ProviderAdapter, ProviderError
from omnisearch.services.search import MultiSourceSearchService, QuickSearchCommand


class FakeAdapter(ProviderAdapter):
    def __init__(self, source_id: str, results=None, error: ProviderError | None = None):
        self.source_id = source_id
        self.results = results or []
        self.error = error

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id=self.source_id,
            adapter_id=f"{self.source_id}_api",
            category=SourceCategory.ACADEMIC,
            channel=Channel.API,
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        if self.error:
            raise self.error
        for result in self.results:
            yield result


class FakeBrowserAdapter(FakeAdapter):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id=self.source_id,
            adapter_id=f"{self.source_id}_browser",
            category=SourceCategory.WEB,
            channel=Channel.BROWSER,
        )

def paper(source: str, url: str) -> SearchResult:
    return SearchResult(
        source_id=source,
        adapter_id=f"{source}_api",
        channel=Channel.API,
        result_type="paper",
        title="Same paper",
        url=url,
        external_identifiers={"doi": "10.1000/same"},
    )


def test_arxiv_versions_share_a_canonical_entity() -> None:
    result = paper("arxiv", "https://arxiv.org/abs/1706.03762v7")
    result.external_identifiers = {"arxiv": "1706.03762v7"}
    assert MultiSourceSearchService._canonical_key(result) == "arxiv:1706.03762"


def test_arxiv_browser_pdf_url_merges_with_api_entity() -> None:
    result = paper("google", "https://arxiv.org/pdf/1706.03762v7.pdf")
    result.external_identifiers = {}
    assert MultiSourceSearchService._canonical_key(result) == "arxiv:1706.03762"


def test_bearer_token_marks_credentialed_source_configured() -> None:
    class BearerAdapter(FakeAdapter):
        bearer_token = "configured"

        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                source_id="x",
                adapter_id="x_api",
                category=SourceCategory.COMMUNITY,
                channel=Channel.API,
                requires_credentials=True,
            )

    service = MultiSourceSearchService([BearerAdapter("x")])

    assert service.configured_sources == {"x"}


@pytest.mark.asyncio
async def test_parallel_search_merges_cross_source_doi_and_isolates_failure() -> None:
    service = MultiSourceSearchService(
        [
            FakeAdapter("crossref", [paper("crossref", "https://doi.org/10.1000/same")]),
            FakeAdapter("openalex", [paper("openalex", "https://openalex.org/W1")]),
            FakeAdapter("arxiv", error=ProviderError("RATE_LIMITED", "slow down", retryable=True)),
        ]
    )
    response = await service.search(
        QuickSearchCommand(query="paper", sources=["crossref", "openalex", "arxiv"])
    )
    assert len(response.results) == 1
    assert len(response.results[0].occurrences) == 2
    assert response.failures[0].code == "RATE_LIMITED"
    assert response.failures[0].retryable is True

    second = await service.search(QuickSearchCommand(query="paper again", sources=["arxiv"]))
    assert second.failures[0].code == "CIRCUIT_OPEN"


@pytest.mark.asyncio
async def test_identical_searches_use_ttl_cache() -> None:
    adapter = FakeAdapter("crossref", [paper("crossref", "https://doi.org/10.1000/same")])
    calls = 0
    original_search = adapter.search

    async def counted(request: SearchRequest) -> AsyncIterator[SearchResult]:
        nonlocal calls
        calls += 1
        async for result in original_search(request):
            yield result

    adapter.search = counted  # type: ignore[method-assign]
    service = MultiSourceSearchService([adapter], cache_ttl_seconds=60)
    command = QuickSearchCommand(query="same query", sources=["crossref"])

    first = await service.search(command)
    second = await service.search(command)

    assert calls == 1
    assert first.results == second.results


@pytest.mark.asyncio
async def test_concurrent_identical_searches_are_coalesced() -> None:
    class SlowAdapter(FakeAdapter):
        calls = 0

        async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
            self.calls += 1
            await asyncio.sleep(0.02)
            yield paper(self.source_id, "https://doi.org/10.1000/same")

    adapter = SlowAdapter("crossref")
    service = MultiSourceSearchService([adapter], cache_ttl_seconds=60)
    command = QuickSearchCommand(query="same query", sources=["crossref"])

    first, second = await asyncio.gather(service.search(command), service.search(command))

    assert adapter.calls == 1
    assert first.results == second.results


@pytest.mark.asyncio
async def test_slow_source_is_bounded_by_channel_timeout() -> None:
    class SlowAdapter(FakeAdapter):
        async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
            await asyncio.sleep(0.1)
            yield paper(self.source_id, "https://doi.org/10.1000/same")

    service = MultiSourceSearchService([SlowAdapter("crossref")], api_timeout_seconds=0.01)

    response = await service.search(QuickSearchCommand(query="slow query", sources=["crossref"]))

    assert response.results == []
    assert response.failures[0].code == "TIMEOUT"


@pytest.mark.asyncio
async def test_health_reports_recent_circuit_failure() -> None:
    adapter = FakeAdapter(
        "arxiv", error=ProviderError("RATE_LIMITED", "slow down", retryable=True)
    )
    service = MultiSourceSearchService([adapter])

    await service.search(QuickSearchCommand(query="paper", sources=["arxiv"]))
    health = (await service.health())[0]

    assert health.healthy is False
    assert health.status == "circuit_open"
    assert health.last_failure_code == "RATE_LIMITED"
    assert health.consecutive_failures == 1


@pytest.mark.asyncio
async def test_persistent_zero_success_failures_seed_circuit() -> None:
    class CountedFailureAdapter(FakeAdapter):
        calls = 0

        async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
            self.calls += 1
            raise ProviderError("ACCESS_DENIED", "blocked", retryable=True)
            yield  # pragma: no cover

    adapter = CountedFailureAdapter("google_scholar")
    service = MultiSourceSearchService([adapter])
    service.seed_persistent_health(
        {
            "sources": [
                {
                    "source_id": adapter.capabilities.source_id,
                    "failed_attempts": 3,
                    "successes": 0,
                    "recovered_attempts": 0,
                }
            ],
            "failure_summary": [
                {
                    "source_id": adapter.capabilities.source_id,
                    "code": "ACCESS_DENIED",
                    "count": 3,
                }
            ],
        }
    )

    response = await service.search(
        QuickSearchCommand(query="audio watermark", sources=[adapter.capabilities.source_id])
    )

    assert response.failures[0].code == "CIRCUIT_OPEN"
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_browser_failure_does_not_open_provider_wide_circuit() -> None:
    adapter = FakeBrowserAdapter(
        "google", error=ProviderError("ACCESS_DENIED", "rotating proxy", retryable=True)
    )
    calls = 0
    original_search = adapter.search

    async def counted(request: SearchRequest) -> AsyncIterator[SearchResult]:
        nonlocal calls
        calls += 1
        async for result in original_search(request):
            yield result

    adapter.search = counted  # type: ignore[method-assign]
    service = MultiSourceSearchService([adapter])

    first = await service.search(QuickSearchCommand(query="first", sources=["google"]))
    second = await service.search(QuickSearchCommand(query="second", sources=["google"]))

    assert first.failures[0].code == "ACCESS_DENIED"
    assert second.failures[0].code == "ACCESS_DENIED"
    assert calls == 2


@pytest.mark.asyncio
async def test_persistent_browser_failure_does_not_seed_circuit() -> None:
    adapter = FakeBrowserAdapter(
        "google_scholar",
        error=ProviderError("ACCESS_DENIED", "rotating proxy", retryable=True),
    )
    service = MultiSourceSearchService([adapter])
    service.seed_persistent_health(
        {
            "sources": [
                {
                    "source_id": "google_scholar",
                    "failed_attempts": 20,
                    "successes": 0,
                    "recovered_attempts": 0,
                }
            ]
        }
    )

    response = await service.search(
        QuickSearchCommand(query="audio watermark", sources=["google_scholar"])
    )

    assert response.failures[0].code == "ACCESS_DENIED"


@pytest.mark.asyncio
async def test_live_failure_can_recover_exact_query_from_persisted_cache() -> None:
    adapter = FakeBrowserAdapter(
        "google", error=ProviderError("ACCESS_DENIED", "blocked route", retryable=True)
    )
    cached = paper("google", "https://example.org/cached")
    cached.channel = Channel.BROWSER
    service = MultiSourceSearchService(
        [adapter],
        stale_fallback_loader=lambda source_id, command: (
            [cached.model_copy(deep=True)]
            if source_id == "google" and command.query == "exact query"
            else []
        ),
    )

    response = await service.search(
        QuickSearchCommand(query="exact query", sources=["google"])
    )

    assert len(response.results) == 1
    assert response.failures[0].code == "STALE_CACHE_USED"
    assert response.results[0].primary.raw_payload["live_failure_code"] == "ACCESS_DENIED"


@pytest.mark.asyncio
async def test_degraded_browser_uses_persisted_result_before_another_live_timeout() -> None:
    adapter = FakeBrowserAdapter(
        "google", error=ProviderError("ACCESS_DENIED", "blocked route", retryable=True)
    )
    calls = 0
    original_search = adapter.search

    async def counted(request: SearchRequest) -> AsyncIterator[SearchResult]:
        nonlocal calls
        calls += 1
        async for result in original_search(request):
            yield result

    adapter.search = counted  # type: ignore[method-assign]
    cached = paper("google", "https://example.org/cached")
    cached.channel = Channel.BROWSER
    service = MultiSourceSearchService(
        [adapter],
        stale_fallback_loader=lambda source_id, command: (
            [cached.model_copy(deep=True)] if command.query == "repeated" else []
        ),
    )

    await service.search(QuickSearchCommand(query="failure one", sources=["google"]))
    await service.search(QuickSearchCommand(query="failure two", sources=["google"]))
    response = await service.search(QuickSearchCommand(query="repeated", sources=["google"]))

    assert calls == 2
    assert response.failures[0].code == "STALE_CACHE_USED"
    assert len(response.results) == 1


@pytest.mark.asyncio
async def test_records_success_failure_and_circuit_skip() -> None:
    events: list[dict] = []
    adapter = FakeAdapter(
        "arxiv", error=ProviderError("RATE_LIMITED", "slow down", retryable=True)
    )
    service = MultiSourceSearchService([adapter], attempt_recorder=events.append)

    await service.search(QuickSearchCommand(query="first", sources=["arxiv"]))
    await service.search(QuickSearchCommand(query="second", sources=["arxiv"]))

    assert [item["outcome"] for item in events] == ["failure", "circuit_skip"]
    assert events[0]["code"] == "RATE_LIMITED"
    assert events[0]["query_hash"] != events[1]["query_hash"]
    assert all(len(item["query_hash"]) == 64 for item in events)


@pytest.mark.asyncio
async def test_recovered_fallback_is_not_counted_as_primary_success_and_is_cached() -> None:
    events: list[dict] = []
    adapter = FakeAdapter("google", [paper("google", "https://example.org/paper")])
    adapter.results[0].raw_payload = {
        "fallback_used": True,
        "failed_attempts": ["patchright_chrome: no results"],
        "browser_engine": "ego-browser",
    }
    calls = 0
    original_search = adapter.search

    async def counted(request: SearchRequest) -> AsyncIterator[SearchResult]:
        nonlocal calls
        calls += 1
        async for result in original_search(request):
            yield result

    adapter.search = counted  # type: ignore[method-assign]
    service = MultiSourceSearchService(
        [adapter], cache_ttl_seconds=60, attempt_recorder=events.append
    )
    command = QuickSearchCommand(query="watermark", sources=["google"])

    first = await service.search(command)
    second = await service.search(command)

    assert calls == 1
    assert first.failures[0].code == "FALLBACK_USED"
    assert second.failures[0].code == "FALLBACK_USED"
    assert events[0]["outcome"] == "recovered"


@pytest.mark.asyncio
async def test_rate_limit_start_times_are_serialized_even_with_provider_concurrency() -> None:
    class ConcurrentAdapter(FakeAdapter):
        min_interval_seconds = 0.02

        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                source_id=self.source_id,
                adapter_id=f"{self.source_id}_api",
                category=SourceCategory.ACADEMIC,
                channel=Channel.API,
                max_concurrency=3,
            )

        async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
            starts.append(monotonic())
            yield paper(self.source_id, f"https://example.org/{request.query}")

    starts: list[float] = []
    service = MultiSourceSearchService([ConcurrentAdapter("openalex")])

    await asyncio.gather(
        *(service.search(QuickSearchCommand(query=str(i), sources=["openalex"])) for i in range(3))
    )

    assert len(starts) == 3
    assert starts[1] - starts[0] >= 0.015
    assert starts[2] - starts[1] >= 0.015
