from uuid import uuid4

import httpx
import pytest

from omnisearch.domain.models import SearchRequest
from omnisearch.fetchers import ContentFetcher, FetchedDocument
from omnisearch.providers.api import OpenAlexAdapter
from omnisearch.providers.base import ProviderError
from omnisearch.providers.browser import (
    BraveBrowserAdapter,
    DuckDuckGoBrowserAdapter,
    GoogleBrowserAdapter,
    GooglePatentsBrowserAdapter,
    GoogleScholarBrowserAdapter,
    RedditBrowserAdapter,
    XBrowserAdapter,
    YouTubeBrowserAdapter,
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


def test_google_restrictive_query_rotates_managed_route_before_relaxed_retry() -> None:
    chrome = FakeFetcher("")
    ego = FakeFetcher("")
    adapter = GoogleBrowserAdapter(
        chrome_fetcher=chrome,
        ego_fetcher=ego,
        chrome_user_data_dir="C:/dedicated",
    )
    request = SearchRequest(
        query_id=uuid4(),
        query="site:cloud.google.com/free Google Cloud credits",
        max_results=5,
    )

    attempts = adapter._attempts(request)

    assert len(attempts) == 3
    assert attempts[0][1] is ego
    assert attempts[1][1] is chrome
    assert "site%3Acloud.google.com%2Ffree" in attempts[1][0]
    assert attempts[2][1] is chrome
    assert "site%3A" not in attempts[2][0]
    assert "Google+Cloud+credits" in attempts[2][0]


@pytest.mark.asyncio
async def test_google_retries_after_challenge_using_rotated_managed_route() -> None:
    rotations: list[str] = []

    class EmptyEgo(FakeFetcher):
        pass

    class ChallengeThenSuccess(ContentFetcher):
        calls = 0

        async def fetch(self, request):
            self.calls += 1
            source = (
                "<html>Our systems have detected unusual traffic</html>"
                if self.calls == 1
                else '<a href="https://cloud.google.com/free"><h3>Google Cloud Free</h3></a>'
            )
            return FetchedDocument(
                url=request.url,
                final_url=request.url,
                mode=request.mode,
                status_code=200,
                markdown="",
                raw_html=source,
                metadata={"engine": "fake"},
            )

    chrome = ChallengeThenSuccess()
    adapter = GoogleBrowserAdapter(
        chrome_fetcher=chrome,
        ego_fetcher=EmptyEgo("<html>No matching results</html>"),
        chrome_user_data_dir="C:/dedicated",
        route_rotator=lambda reason: rotations.append(reason) or "alternate",
    )
    request = SearchRequest(
        query_id=uuid4(),
        query="site:cloud.google.com/free Google Cloud credits",
        max_results=5,
    )

    results = [item async for item in adapter.search(request)]

    assert chrome.calls == 2
    assert rotations == ["google:access_denied"]
    assert str(results[0].url) == "https://cloud.google.com/free"
    assert results[0].raw_payload["fallback_used"] is True


@pytest.mark.asyncio
async def test_browser_discovery_classifies_repository_url_as_code_evidence() -> None:
    source = """<a href="https://github.com/facebookresearch/audioseal">
    <h3>AudioSeal repository</h3></a>"""
    fetcher = FakeFetcher(source)
    adapter = GoogleBrowserAdapter(
        chrome_fetcher=fetcher,
        ego_fetcher=fetcher,
        chrome_user_data_dir="C:/dedicated",
    )

    results = [item async for item in adapter.search(search_request())]

    assert results[0].result_type == "repository"


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
    assert adapter._search_urls(search_request())[0].startswith(
        "https://html.duckduckgo.com/html/"
    )


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
async def test_duckduckgo_reaches_camoufox_after_lighter_backends_fail() -> None:
    calls: list[str] = []

    class BrokenFetcher(ContentFetcher):
        async def fetch(self, request):
            calls.append("patchright")
            raise RuntimeError("Chrome unavailable")

    class CamoufoxFake(FakeFetcher):
        backend_name = "camoufox"

        async def fetch(self, request):
            calls.append("camoufox")
            document = await super().fetch(request)
            document.metadata["engine"] = self.backend_name
            return document

    class BrokenEgo(ContentFetcher):
        async def fetch(self, request):
            calls.append("ego")
            raise RuntimeError("Ego unavailable")

    fallback = CamoufoxFake(
        '<a href="https://example.org" class="result__a">Fallback result</a>'
    )
    adapter = DuckDuckGoBrowserAdapter(
        chrome_fetcher=BrokenFetcher(),
        browser_fallback_fetchers=(fallback,),
        ego_fetcher=BrokenEgo(),
        chrome_user_data_dir="C:/dedicated",
    )
    results = [item async for item in adapter.search(search_request())]

    assert calls == ["ego", "patchright", "camoufox"]
    assert results[0].raw_payload["browser_engine"] == "camoufox"
    assert results[0].raw_payload["fallback_used"] is True


def test_duckduckgo_attempts_diversify_endpoint_and_backend_early() -> None:
    class NamedFetcher(FakeFetcher):
        def __init__(self, name: str) -> None:
            super().__init__("")
            self.backend_name = name

    chrome = NamedFetcher("patchright_chrome")
    camoufox = NamedFetcher("camoufox")
    ego = NamedFetcher("ego")
    adapter = DuckDuckGoBrowserAdapter(
        chrome_fetcher=chrome,
        browser_fallback_fetchers=(camoufox,),
        ego_fetcher=ego,
        chrome_user_data_dir="C:/dedicated",
    )

    attempts = adapter._attempts(search_request())

    assert len(attempts) == 3
    assert "duckduckgo.com/?" in attempts[0][0]
    assert attempts[0][1] is ego
    assert attempts[1][1] is chrome
    assert attempts[2][1] is camoufox


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
async def test_google_scholar_cools_off_official_probe_but_keeps_live_index() -> None:
    class ChallengeFetcher(FakeFetcher):
        calls = 0

        async def fetch(self, request):
            self.calls += 1
            return await super().fetch(request)

    async def index_handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "display_name": "Audio Watermarking",
                        "type": "article",
                        "doi": "https://doi.org/10.1000/example",
                        "authorships": [],
                    }
                ]
            },
        )

    challenge = ChallengeFetcher("<div class='g-recaptcha'>challenge</div>")
    async with httpx.AsyncClient(transport=httpx.MockTransport(index_handler)) as client:
        adapter = GoogleScholarBrowserAdapter(
            chrome_fetcher=challenge,
            ego_fetcher=challenge,
            chrome_user_data_dir="C:/dedicated",
            index_fallback=OpenAlexAdapter(client=client),
        )
        first = [item async for item in adapter.search(search_request())]
        second = [item async for item in adapter.search(search_request())]

    assert challenge.calls == 2
    assert first[0].raw_payload["fallback_path"] == "openalex_live_academic_index"
    assert second[0].raw_payload["fallback_path"] == "openalex_live_academic_index"


def test_google_patents_parser_accepts_scrapling_wrapped_xhr_json() -> None:
    source = """<html><body><p>{"results":{"cluster":[{"result":[{
      "id":"patent/US123B2/en","patent":{"title":" <b>Audio watermark</b>",
      "snippet":"Robust <b>localization</b> method"}}]}]}}</p></body></html>"""
    fetcher = FakeFetcher("")
    adapter = GooglePatentsBrowserAdapter(
        chrome_fetcher=fetcher,
        ego_fetcher=fetcher,
        chrome_user_data_dir="C:/dedicated",
    )

    results = adapter._parse(source)

    assert results[0]["title"] == "Audio watermark"
    assert results[0]["external_id"] == "US123B2"


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
async def test_x_prefers_stable_indexed_search_before_authenticated_session() -> None:
    calls: list[tuple[str, str]] = []

    class EmptyFetcher(FakeFetcher):
        async def fetch(self, request):
            calls.append((request.mode.value, str(request.url)))
            return await super().fetch(request)

    class IndexedOnlyFetcher(ContentFetcher):
        async def fetch(self, request):
            url = str(request.url)
            calls.append((request.mode.value, url))
            source = (
                '<a href="https://x.com/researcher/status/789"><h3>Indexed post</h3></a>'
                if "duckduckgo.com/" in url
                else "<html><body>No posts</body></html>"
            )
            return FetchedDocument(
                url=request.url,
                final_url=request.url,
                mode=request.mode,
                status_code=200,
                markdown="",
                raw_html=source,
                metadata={"engine": "fake"},
            )

    empty = EmptyFetcher("<html><body>No posts</body></html>")
    indexed = IndexedOnlyFetcher()
    adapter = XBrowserAdapter(
        chrome_fetcher=indexed,
        ego_fetcher=empty,
        chrome_user_data_dir="C:/dedicated",
    )

    results = [item async for item in adapter.search(search_request())]

    assert len(calls) == 1
    assert calls[0][0] == "chrome_managed"
    assert calls[0][1].startswith("https://duckduckgo.com/")
    assert results[0].external_identifiers == {"x_post_id": "789"}


@pytest.mark.asyncio
async def test_brave_browser_parser_extracts_external_results() -> None:
    source = """<div class="snippet" data-type="web">
    <a href="https://example.org/research"><div class="title">Primary research</div></a>
    <div class="description">Technical details</div></div>"""
    fetcher = FakeFetcher(source)
    adapter = BraveBrowserAdapter(
        chrome_fetcher=fetcher, ego_fetcher=fetcher, chrome_user_data_dir="C:/dedicated"
    )

    results = [item async for item in adapter.search(search_request())]

    assert str(results[0].url) == "https://example.org/research"
    assert results[0].snippet == "Technical details"


@pytest.mark.asyncio
async def test_reddit_browser_parser_accepts_indexed_redirect() -> None:
    source = """<a class="result__a"
    href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.reddit.com%2Fr%2Ftest%2Fcomments%2Fabc%2Ftopic%2F">
    Useful discussion</a>"""
    fetcher = FakeFetcher(source)
    adapter = RedditBrowserAdapter(
        chrome_fetcher=fetcher, ego_fetcher=fetcher, chrome_user_data_dir="C:/dedicated"
    )

    results = [item async for item in adapter.search(search_request())]

    assert results[0].source_id == "reddit"
    assert "/comments/abc/" in str(results[0].url)


@pytest.mark.asyncio
async def test_youtube_browser_parser_extracts_video_id() -> None:
    source = """<a id="video-title" href="/watch?v=abc123" title="Technical talk">
    Technical talk</a>"""
    fetcher = FakeFetcher(source)
    adapter = YouTubeBrowserAdapter(
        chrome_fetcher=fetcher, ego_fetcher=fetcher, chrome_user_data_dir="C:/dedicated"
    )

    results = [item async for item in adapter.search(search_request())]

    assert results[0].external_identifiers == {"youtube_video_id": "abc123"}


@pytest.mark.asyncio
async def test_youtube_uses_indexed_google_without_opening_direct_ego_session() -> None:
    class IndexedFetcher(ContentFetcher):
        async def fetch(self, request):
            source = (
                '<a href="https://www.youtube.com/watch?v=indexed" title="Indexed talk">x</a>'
                if "google.com/search" in str(request.url)
                else "<html><body>No videos</body></html>"
            )
            return FetchedDocument(
                url=request.url,
                final_url=request.url,
                mode=request.mode,
                status_code=200,
                markdown="",
                raw_html=source,
                metadata={"engine": "fake"},
            )

    class ForbiddenEgo(ContentFetcher):
        async def fetch(self, request):
            raise AssertionError("direct YouTube Ego session should not be needed")

    adapter = YouTubeBrowserAdapter(
        chrome_fetcher=IndexedFetcher(),
        ego_fetcher=ForbiddenEgo(),
        chrome_user_data_dir="C:/dedicated",
    )

    results = [item async for item in adapter.search(search_request())]

    assert results[0].external_identifiers == {"youtube_video_id": "indexed"}


@pytest.mark.asyncio
async def test_youtube_accepts_duckduckgo_index_redirects() -> None:
    source = """<a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Dabc789">
    <h3>Indexed video</h3></a>"""
    fetcher = FakeFetcher(source)
    adapter = YouTubeBrowserAdapter(
        chrome_fetcher=fetcher,
        ego_fetcher=fetcher,
        chrome_user_data_dir="C:/dedicated",
    )

    results = [item async for item in adapter.search(search_request())]

    assert results[0].external_identifiers == {"youtube_video_id": "abc789"}


@pytest.mark.asyncio
async def test_browser_challenge_is_reported_as_access_denied() -> None:
    class ChallengeFetcher(ContentFetcher):
        calls = 0

        async def fetch(self, request):
            self.calls += 1
            return FetchedDocument(
                url=request.url,
                final_url=request.url,
                mode=request.mode,
                status_code=429,
                markdown="",
                raw_html="<html>Our systems have detected unusual traffic</html>",
                metadata={"engine": "fake"},
            )

    fetcher = ChallengeFetcher()
    adapter = GoogleScholarBrowserAdapter(
        chrome_fetcher=fetcher, ego_fetcher=fetcher, chrome_user_data_dir="C:/dedicated"
    )

    with pytest.raises(ProviderError) as raised:
        _ = [item async for item in adapter.search(search_request())]

    assert raised.value.code == "ACCESS_DENIED"
    assert fetcher.calls == 2
