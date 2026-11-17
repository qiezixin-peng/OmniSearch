from collections.abc import AsyncIterator

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
            FakeAdapter(
                "arxiv", error=ProviderError("RATE_LIMITED", "slow down", retryable=True)
            ),
        ]
    )
    response = await service.search(
        QuickSearchCommand(
            query="paper", sources=["crossref", "openalex", "arxiv"]
        )
    )
    assert len(response.results) == 1
    assert len(response.results[0].occurrences) == 2
    assert response.failures[0].code == "RATE_LIMITED"
    assert response.failures[0].retryable is True

    second = await service.search(
        QuickSearchCommand(query="paper again", sources=["arxiv"])
    )
    assert second.failures[0].code == "CIRCUIT_OPEN"
