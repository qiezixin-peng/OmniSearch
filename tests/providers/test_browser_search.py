from uuid import uuid4

import pytest

from omnisearch.domain.models import SearchRequest
from omnisearch.fetchers import ContentFetcher, FetchedDocument
from omnisearch.providers.browser import (
    DuckDuckGoBrowserAdapter,
    GoogleBrowserAdapter,
    GooglePatentsBrowserAdapter,
    GoogleScholarBrowserAdapter,
    XBrowserAdapter,
)


class FakeFetcher(ContentFetcher):
    def __init__(self, source: str) -> None:
        self.source = source

    async def fetch(self, request):
        return FetchedDocument(
            url=request.url,
            final_url=request.url,
            mode=request.mode,
            status_code=200,
            markdown="",
            raw_html=self.source,
            metadata={"engine": "fake"},
        )


def search_request() -> SearchRequest:
    return SearchRequest(query_id=uuid4(), query="audio watermark", max_results=5)


@pytest.mark.asyncio
async def test_google_result_parser() -> None:
    source = """<html><body><div><a href="https://example.org/paper">
    <h3>Important Paper</h3></a></div></body></html>"""
    fetcher = FakeFetcher(source)
    adapter = GoogleBrowserAdapter(
        chrome_fetcher=fetcher,
        ego_fetcher=fetcher,
        chrome_user_data_dir="C:/dedicated",
    )
    results = [item async for item in adapter.search(search_request())]
    assert results[0].title == "Important Paper"
    assert str(results[0].url) == "https://example.org/paper"


@pytest.mark.asyncio
async def test_duckduckgo_redirect_and_snippet_parser() -> None:
    source = """<html><body><div class="result results_links">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Frepo">
    Useful Repository</a><a class="result__snippet">Implementation details</a>
    </div></body></html>"""
    fetcher = FakeFetcher(source)
    adapter = DuckDuckGoBrowserAdapter(
        chrome_fetcher=fetcher,
        ego_fetcher=fetcher,
        chrome_user_data_dir="C:/dedicated",
    )
    results = [item async for item in adapter.search(search_request())]
    assert str(results[0].url) == "https://example.org/repo"
    assert results[0].snippet == "Implementation details"


@pytest.mark.asyncio
async def test_browser_search_falls_back_to_ego() -> None:
    class BrokenFetcher(ContentFetcher):
        async def fetch(self, request):
            raise RuntimeError("Chrome unavailable")

    ego = FakeFetcher('<a href="https://example.org" class="result__a">Fallback result</a>')
    adapter = DuckDuckGoBrowserAdapter(
        chrome_fetcher=BrokenFetcher(),
        ego_fetcher=ego,
        chrome_user_data_dir="C:/dedicated",
    )
    results = [item async for item in adapter.search(search_request())]
    assert results[0].raw_payload["browser_engine"] == "fake"
    assert results[0].channel.value == "browser"


@pytest.mark.asyncio
async def test_google_scholar_parser_preserves_paper_snippet() -> None:
    source = """<html><body><div class="gs_ri">
    <h3 class="gs_rt"><a href="https://example.org/paper.pdf">Neural Watermark</a></h3>
    <div class="gs_rs">A robust detection and localization method.</div>
    </div></body></html>"""
    fetcher = FakeFetcher(source)
    adapter = GoogleScholarBrowserAdapter(
        chrome_fetcher=fetcher,
        ego_fetcher=fetcher,
        chrome_user_data_dir="C:/dedicated",
    )

    results = [item async for item in adapter.search(search_request())]

    assert results[0].result_type == "paper"
    assert results[0].snippet == "A robust detection and localization method."


@pytest.mark.asyncio
async def test_google_patents_parser_resolves_state_modifier_result() -> None:
    source = """<search-result-item><article class="result">
    <state-modifier data-result="patent/US8923548B2/en">
    <a><h3>Watermark extraction</h3></a></state-modifier>
    <div class="abstract"><div class="flex"><raw-html>Inventor</raw-html>
    <raw-html>Detailed patent abstract.</raw-html></div></div>
    </article></search-result-item>"""
    fetcher = FakeFetcher(source)
    adapter = GooglePatentsBrowserAdapter(
        chrome_fetcher=fetcher,
        ego_fetcher=fetcher,
        chrome_user_data_dir="C:/dedicated",
    )

    results = [item async for item in adapter.search(search_request())]

    assert str(results[0].url) == "https://patents.google.com/patent/US8923548B2/en"
    assert results[0].external_identifiers == {"patent": "US8923548B2"}
    assert results[0].result_type == "patent"


@pytest.mark.asyncio
async def test_x_browser_parser_supports_authenticated_rendered_timeline() -> None:
    source = """<article data-testid="tweet">
    <a href="/researcher/status/123"><time>now</time></a>
    <div data-testid="tweetText">Primary technical announcement</div>
    </article>"""
    fetcher = FakeFetcher(source)
    adapter = XBrowserAdapter(
        chrome_fetcher=fetcher,
        ego_fetcher=fetcher,
        chrome_user_data_dir="C:/dedicated",
    )

    results = [item async for item in adapter.search(search_request())]

    assert results[0].source_id == "x"
    assert results[0].snippet == "Primary technical announcement"
    assert results[0].external_identifiers == {"x_post_id": "123"}


@pytest.mark.asyncio
async def test_x_browser_parser_accepts_indexed_post_fallback() -> None:
    source = """<a href="https://x.com/researcher/status/456">
    <h3>Indexed primary announcement</h3></a>"""
    fetcher = FakeFetcher(source)
    adapter = XBrowserAdapter(
        chrome_fetcher=fetcher,
        ego_fetcher=fetcher,
        chrome_user_data_dir="C:/dedicated",
    )

    results = [item async for item in adapter.search(search_request())]

    assert str(results[0].url) == "https://x.com/researcher/status/456"
    assert results[0].external_identifiers == {"x_post_id": "456"}


@pytest.mark.asyncio
async def test_x_uses_only_authenticated_direct_then_indexed_fallback() -> None:
    calls: list[tuple[str, str]] = []

    class RecordingFetcher(FakeFetcher):
        async def fetch(self, request):
            calls.append((request.mode.value, str(request.url)))
            return await super().fetch(request)

    empty = RecordingFetcher("<html><body>No posts</body></html>")
    indexed = RecordingFetcher(
        '<a href="https://x.com/researcher/status/789"><h3>Indexed post</h3></a>'
    )
    adapter = XBrowserAdapter(
        chrome_fetcher=indexed,
        ego_fetcher=empty,
        chrome_user_data_dir="C:/dedicated",
    )

    results = [item async for item in adapter.search(search_request())]

    assert len(calls) == 2
    assert calls[0][0] == "ego_browser"
    assert calls[0][1].startswith("https://x.com/search")
    assert calls[1][0] == "chrome_managed"
    assert calls[1][1].startswith("https://www.google.com/search")
    assert results[0].external_identifiers == {"x_post_id": "789"}
