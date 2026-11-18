import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx
from lxml import html

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult
from omnisearch.fetchers import ContentFetcher, FetchMode, FetchRequest
from omnisearch.providers.base import ProviderAdapter, ProviderError


class BrowserSearchAdapter(ProviderAdapter):
    source_id: str
    adapter_id: str
    category = SourceCategory.WEB
    result_type = "web_page"
    search_capabilities = {"web"}
    attempt_timeout_ms = 12_000
    wait_after_load_ms = 1_200
    prefer_ego_before_camoufox = False
    preferred_backends: tuple[str, ...] = ()
    stop_on_access_denied = False

    def __init__(
        self,
        *,
        chrome_fetcher: ContentFetcher,
        ego_fetcher: ContentFetcher,
        chrome_user_data_dir: str,
        browser_fallback_fetchers: tuple[ContentFetcher, ...] = (),
    ) -> None:
        self.chrome_fetcher = chrome_fetcher
        self.browser_fetchers = (chrome_fetcher, *browser_fallback_fetchers)
        self.ego_fetcher = ego_fetcher
        self.chrome_user_data_dir = str(Path(chrome_user_data_dir) / self.source_id)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id=self.source_id,
            adapter_id=self.adapter_id,
            category=self.category,
            channel=Channel.BROWSER,
            supported_languages={"*"},
            search_capabilities=self.search_capabilities,
            supports_pagination=False,
            supports_date_filter=False,
            supports_domain_filter=True,
            max_concurrency=1,
            backend="+".join(
                "ego_browser"
                if mode is FetchMode.EGO_BROWSER
                else getattr(fetcher, "backend_name", type(fetcher).__name__)
                for fetcher, mode in self._fetcher_sequence()
            ),
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        failures: list[str] = []
        access_denied = False
        document = None
        parsed_items: list[dict[str, str]] = []
        for url, fetcher, mode in self._attempts(request):
            try:
                fetch_timeout_ms = (
                    30_000
                    if getattr(fetcher, "backend_name", "") == "camoufox"
                    else self.attempt_timeout_ms
                )
                async with asyncio.timeout((fetch_timeout_ms + 3_000) / 1_000):
                    document = await fetcher.fetch(
                        FetchRequest(
                            url=url,
                            mode=mode,
                            timeout_ms=fetch_timeout_ms,
                            chrome_user_data_dir=(
                                self.chrome_user_data_dir
                                if mode is FetchMode.CHROME_MANAGED
                                else None
                            ),
                            ego_task_name=f"OmniSearch {self.source_id} search",
                            solve_cloudflare=True,
                            network_idle=False,
                            wait_after_load_ms=self.wait_after_load_ms,
                            main_content_only=False,
                        )
                    )
                if document.raw_html:
                    if self._is_access_denied(document):
                        access_denied = True
                        failures.append(f"{mode.value} {url}: access challenge or rate limit")
                        if self.stop_on_access_denied:
                            break
                        continue
                    candidates = self._parse(document.raw_html)
                    prepared_candidates = await asyncio.gather(
                        *(
                            self._prepare_with_timeout(item)
                            for item in candidates[: max(request.max_results * 2, 10)]
                        )
                    )
                    parsed_items = [
                        prepared for prepared in prepared_candidates if prepared is not None
                    ]
                if parsed_items:
                    break
                failures.append(f"{mode.value} {url}: no search results found in returned page")
            except Exception as error:
                failures.append(f"{mode.value} {url}: {error}")
            if parsed_items:
                break
        if document is None or not parsed_items:
            raise ProviderError(
                "ACCESS_DENIED" if access_denied else "BROWSER_SEARCH_FAILED",
                "; ".join(failures) or f"{self.source_id} returned no HTML",
                retryable=True,
            )
        seen: set[str] = set()
        for rank, item in enumerate(parsed_items, start=1):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            yield SearchResult(
                source_id=self.source_id,
                adapter_id=self.adapter_id,
                channel=Channel.BROWSER,
                result_type=self.result_type,
                title=item["title"],
                url=item["url"],
                original_language=request.language,
                snippet=item.get("snippet"),
                external_identifiers=(
                    {item["external_id_name"]: item["external_id"]}
                    if item.get("external_id_name") and item.get("external_id")
                    else {}
                ),
                raw_payload={
                    "rank": rank,
                    "browser_engine": document.metadata.get("engine"),
                    "fallback_used": bool(failures),
                    "failed_attempts": failures[:10],
                },
            )
            if rank >= request.max_results:
                break

    def _search_url(self, request: SearchRequest) -> str:
        raise NotImplementedError

    def _search_urls(self, request: SearchRequest) -> list[str]:
        return [self._search_url(request)]

    def _attempts(self, request: SearchRequest) -> list[tuple[str, ContentFetcher, FetchMode]]:
        return [
            (url, fetcher, mode)
            for url in self._search_urls(request)
            for fetcher, mode in self._fetcher_sequence()
        ]

    def _fetcher_sequence(self) -> list[tuple[ContentFetcher, FetchMode]]:
        candidates = [
            *((fetcher, FetchMode.CHROME_MANAGED) for fetcher in self.browser_fetchers),
            (self.ego_fetcher, FetchMode.EGO_BROWSER),
        ]
        if self.preferred_backends:
            priorities = {
                backend: index for index, backend in enumerate(self.preferred_backends)
            }
            return sorted(
                candidates,
                key=lambda item: priorities.get(self._backend_name(*item), len(priorities)),
            )
        if self.prefer_ego_before_camoufox:
            fast = [
                (fetcher, FetchMode.CHROME_MANAGED)
                for fetcher in self.browser_fetchers
                if getattr(fetcher, "backend_name", "") != "camoufox"
            ]
            camoufox = [
                (fetcher, FetchMode.CHROME_MANAGED)
                for fetcher in self.browser_fetchers
                if getattr(fetcher, "backend_name", "") == "camoufox"
            ]
            return [*fast, (self.ego_fetcher, FetchMode.EGO_BROWSER), *camoufox]
        return candidates

    @staticmethod
    def _backend_name(fetcher: ContentFetcher, mode: FetchMode) -> str:
        if mode is FetchMode.EGO_BROWSER:
            return "ego_browser"
        return str(getattr(fetcher, "backend_name", type(fetcher).__name__))

    def _parse(self, source: str) -> list[dict[str, str]]:
        raise NotImplementedError

    async def _prepare_item(self, item: dict[str, str]) -> dict[str, str] | None:
        return item

    async def _prepare_with_timeout(self, item: dict[str, str]) -> dict[str, str] | None:
        try:
            async with asyncio.timeout(6):
                return await self._prepare_item(item)
        except (TimeoutError, httpx.HTTPError):
            return None

    @staticmethod
    def _is_access_denied(document) -> bool:
        if document.status_code in {403, 429}:
            return True
        final_url = str(document.final_url).casefold()
        if "google.com/sorry/" in final_url or "sorry/index" in final_url:
            return True
        source = (document.raw_html or "").casefold()
        markers = (
            "our systems have detected unusual traffic",
            "unusual traffic from your computer network",
            "verify you are human",
            "captcha-delivery.com",
            "g-recaptcha",
            "recaptcha/api",
            "cf-chl-",
        )
        return any(marker in source for marker in markers)


class GoogleBrowserAdapter(BrowserSearchAdapter):
    source_id = "google"
    adapter_id = "google_chrome"
    min_interval_seconds = 2.0
    access_denied_cooldown_seconds = 900.0
    prefer_ego_before_camoufox = True
    preferred_backends = (
        "ego_browser",
        "patchright_chrome",
        "scrapling_chrome",
        "camoufox",
    )

    def _search_url(self, request: SearchRequest) -> str:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        language = (request.language or "en").split("-", 1)[0]
        return f"https://www.google.com/search?q={quote_plus(query)}&hl={language}&num=20"

    def _parse(self, source: str) -> list[dict[str, str]]:
        tree = html.fromstring(source)
        records: list[dict[str, str]] = []
        for anchor in tree.xpath("//a[.//h3]"):
            href = anchor.get("href", "")
            if href.startswith("/url?"):
                href = parse_qs(urlparse(href).query).get("q", [""])[0]
            parsed = urlparse(href)
            is_google_redirect = href.startswith("/goto?")
            if not is_google_redirect and (
                parsed.scheme not in {"http", "https"} or "google." in parsed.netloc
            ):
                continue
            title = " ".join(anchor.xpath(".//h3//text()"))
            if title:
                records.append({"title": title.strip(), "url": href})
        return records

    async def _prepare_item(self, item: dict[str, str]) -> dict[str, str] | None:
        href = item["url"]
        if not href.startswith("/goto?"):
            return item
        try:
            async with (
                httpx.AsyncClient(follow_redirects=True, trust_env=False, timeout=20) as client,
                client.stream("GET", "https://www.google.com" + href) as response,
            ):
                final_url = str(response.url)
        except httpx.HTTPError:
            return None
        if "google." in urlparse(final_url).netloc:
            return None
        return {**item, "url": final_url}


class DuckDuckGoBrowserAdapter(BrowserSearchAdapter):
    source_id = "duckduckgo"
    adapter_id = "duckduckgo_chrome"
    preferred_backends = (
        "camoufox",
        "patchright_chrome",
        "scrapling_chrome",
        "ego_browser",
    )

    def _search_url(self, request: SearchRequest) -> str:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        return f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

    def _search_urls(self, request: SearchRequest) -> list[str]:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        encoded = quote_plus(query)
        return [
            f"https://html.duckduckgo.com/html/?q={encoded}",
            f"https://duckduckgo.com/?q={encoded}&ia=web",
        ]

    def _parse(self, source: str) -> list[dict[str, str]]:
        tree = html.fromstring(source)
        records: list[dict[str, str]] = []
        anchors = tree.cssselect('[data-testid="result-title-a"], a.result__a')
        for anchor in anchors:
            href = anchor.get("href", "")
            if "uddg=" in href:
                href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
            title = " ".join(anchor.itertext()).strip()
            container = anchor.getparent()
            while (
                container is not None
                and container.tag != "article"
                and "result" not in (container.get("class") or "")
            ):
                container = container.getparent()
            snippet = ""
            if container is not None:
                snippets = container.cssselect(
                    '.result__snippet, [data-result="snippet"], [data-testid="result-snippet"]'
                )
                if snippets:
                    snippet = " ".join(snippets[0].itertext()).strip()
            if title and href.startswith(("http://", "https://")):
                records.append({"title": title, "url": href, "snippet": snippet})
        return records


class BraveBrowserAdapter(BrowserSearchAdapter):
    source_id = "brave"
    adapter_id = "brave_browser"

    def _search_url(self, request: SearchRequest) -> str:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        language = (request.language or "en").split("-", 1)[0]
        return f"https://search.brave.com/search?q={quote_plus(query)}&source=web&lang={language}"

    def _parse(self, source: str) -> list[dict[str, str]]:
        tree = html.fromstring(source)
        records: list[dict[str, str]] = []
        selectors = (
            '[data-type="web"] a[href]',
            '.snippet a[href]',
            '.result a[href]',
            'a[data-testid="result-title-a"]',
        )
        anchors = []
        for selector in selectors:
            anchors.extend(tree.cssselect(selector))
        for anchor in anchors:
            href = anchor.get("href", "")
            parsed = urlparse(href)
            if parsed.scheme not in {"http", "https"} or parsed.netloc.endswith("brave.com"):
                continue
            title_nodes = anchor.cssselect("h2, h3, .title")
            title = " ".join(
                (title_nodes[0] if title_nodes else anchor).itertext()
            ).strip()
            if not title:
                continue
            container = anchor
            for _ in range(4):
                if container is None:
                    break
                if "snippet" in (container.get("class") or ""):
                    break
                container = container.getparent()
            snippet = ""
            if container is not None:
                nodes = container.cssselect('.description, [class*="snippet-description"]')
                if nodes:
                    snippet = " ".join(nodes[0].itertext()).strip()
            records.append({"title": title, "url": href, "snippet": snippet})
        return records


class RedditBrowserAdapter(BrowserSearchAdapter):
    source_id = "reddit"
    adapter_id = "reddit_browser"
    category = SourceCategory.COMMUNITY
    result_type = "post"
    search_capabilities = {"post", "comment", "community"}

    def _search_urls(self, request: SearchRequest) -> list[str]:
        query = quote_plus(request.query)
        indexed = quote_plus(request.query + " site:reddit.com/comments")
        return [
            f"https://www.reddit.com/search/?q={query}&sort=relevance",
            f"https://duckduckgo.com/?q={indexed}&ia=web",
        ]

    def _search_url(self, request: SearchRequest) -> str:
        return self._search_urls(request)[0]

    def _parse(self, source: str) -> list[dict[str, str]]:
        tree = html.fromstring(source)
        records: list[dict[str, str]] = []
        for anchor in tree.cssselect('a[href*="/comments/"], a.result__a'):
            href = anchor.get("href", "")
            if "uddg=" in href:
                href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
            if href.startswith("/"):
                href = "https://www.reddit.com" + href
            parsed = urlparse(href)
            if "reddit.com" not in parsed.netloc or "/comments/" not in parsed.path:
                continue
            title = " ".join(anchor.itertext()).strip()
            if not title:
                continue
            records.append({"title": title, "url": href, "snippet": ""})
        return records


class YouTubeBrowserAdapter(BrowserSearchAdapter):
    source_id = "youtube"
    adapter_id = "youtube_browser"
    result_type = "video"
    search_capabilities = {"video", "channel", "first_party_statement"}
    wait_after_load_ms = 1_800

    def _search_url(self, request: SearchRequest) -> str:
        return f"https://www.youtube.com/results?search_query={quote_plus(request.query)}"

    def _attempts(self, request: SearchRequest) -> list[tuple[str, ContentFetcher, FetchMode]]:
        direct_url = self._search_url(request)
        google_url = (
            "https://www.google.com/search?q="
            f"{quote_plus(request.query + ' site:youtube.com/watch')}&num=20"
        )
        duckduckgo_url = (
            "https://duckduckgo.com/?q="
            f"{quote_plus(request.query + ' site:youtube.com/watch')}&ia=web"
        )
        return [
            (url, fetcher, FetchMode.CHROME_MANAGED)
            for url in (direct_url, google_url, duckduckgo_url)
            for fetcher in self.browser_fetchers
        ]

    def _parse(self, source: str) -> list[dict[str, str]]:
        tree = html.fromstring(source)
        records: list[dict[str, str]] = []
        anchors = tree.cssselect('a#video-title[href*="watch?v="], a[href*="/watch?v="]')
        anchors.extend(tree.xpath("//a[.//h3]"))
        for anchor in anchors:
            href = anchor.get("href", "")
            if href.startswith("/url?"):
                href = parse_qs(urlparse(href).query).get("q", [""])[0]
            if "uddg=" in href:
                href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
            if href.startswith("/"):
                href = "https://www.youtube.com" + href
            parsed = urlparse(href)
            if "youtube.com" not in parsed.netloc or "/watch" not in parsed.path:
                continue
            title = (anchor.get("title") or " ".join(anchor.itertext())).strip()
            if title and "watch?v=" in href:
                video_id = parse_qs(parsed.query).get("v", [""])[0]
                records.append(
                    {
                        "title": title,
                        "url": href,
                        "snippet": "",
                        "external_id_name": "youtube_video_id",
                        "external_id": video_id,
                    }
                )
        return records


class XBrowserAdapter(BrowserSearchAdapter):
    source_id = "x"
    adapter_id = "x_browser"
    category = SourceCategory.COMMUNITY
    result_type = "social_post"
    search_capabilities = {"post", "author", "conversation", "recent"}

    def _search_url(self, request: SearchRequest) -> str:
        return f"https://x.com/search?q={quote_plus(request.query)}&src=typed_query&f=live"

    def _search_urls(self, request: SearchRequest) -> list[str]:
        return [
            self._search_url(request),
            "https://duckduckgo.com/?q="
            f"{quote_plus(request.query + ' site:x.com')}&ia=web",
            "https://www.google.com/search?q="
            f"{quote_plus(request.query + ' site:x.com/status')}&num=20",
        ]

    def _attempts(self, request: SearchRequest) -> list[tuple[str, ContentFetcher, FetchMode]]:
        direct_url, indexed_url, google_url = self._search_urls(request)
        return [
            *((url, fetcher, FetchMode.CHROME_MANAGED)
              for url in (indexed_url, google_url)
              for fetcher in self.browser_fetchers),
            (direct_url, self.ego_fetcher, FetchMode.EGO_BROWSER),
            (indexed_url, self.ego_fetcher, FetchMode.EGO_BROWSER),
            (google_url, self.ego_fetcher, FetchMode.EGO_BROWSER),
        ]

    def _parse(self, source: str) -> list[dict[str, str]]:
        tree = html.fromstring(source)
        records: list[dict[str, str]] = []
        for article in tree.cssselect('article[data-testid="tweet"]'):
            status_links = article.cssselect('a[href*="/status/"]')
            if not status_links:
                continue
            href = status_links[0].get("href", "")
            text_nodes = article.cssselect('[data-testid="tweetText"]')
            text = " ".join(text_nodes[0].itertext()).strip() if text_nodes else ""
            username = href.split("/", 2)[1] if href.startswith("/") else "x"
            if href.startswith("/"):
                href = "https://x.com" + href
            if href.startswith("https://x.com/"):
                records.append(
                    {
                        "title": f"@{username}: {text[:140] or 'X post'}",
                        "url": href,
                        "snippet": text,
                        "external_id_name": "x_post_id",
                        "external_id": href.rsplit("/", 1)[-1],
                    }
                )
        if records:
            return records
        for anchor in tree.xpath("//a[.//h3]"):
            href = anchor.get("href", "")
            if href.startswith("/url?"):
                href = parse_qs(urlparse(href).query).get("q", [""])[0]
            if "uddg=" in href:
                href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
            parsed = urlparse(href)
            if parsed.netloc.removeprefix("www.") not in {"x.com", "twitter.com"}:
                continue
            if "/status/" not in parsed.path:
                continue
            title = " ".join(anchor.xpath(".//h3//text()")).strip()
            records.append(
                {
                    "title": title or "X post",
                    "url": href,
                    "snippet": "",
                    "external_id_name": "x_post_id",
                    "external_id": parsed.path.rsplit("/", 1)[-1],
                }
            )
        return records


class GoogleScholarBrowserAdapter(BrowserSearchAdapter):
    source_id = "google_scholar"
    adapter_id = "google_scholar_chrome"
    category = SourceCategory.ACADEMIC
    result_type = "paper"
    search_capabilities = {"paper", "author", "citation"}
    min_interval_seconds = 30.0
    access_denied_cooldown_seconds = 21_600.0
    prefer_ego_before_camoufox = True
    preferred_backends = (
        "ego_browser",
        "patchright_chrome",
        "scrapling_chrome",
        "camoufox",
    )
    stop_on_access_denied = True

    def _search_url(self, request: SearchRequest) -> str:
        language = (request.language or "en").split("-", 1)[0]
        return (
            f"https://scholar.google.com/scholar?q={quote_plus(request.query)}&hl={language}&num=20"
        )

    def _parse(self, source: str) -> list[dict[str, str]]:
        tree = html.fromstring(source)
        records: list[dict[str, str]] = []
        for result in tree.cssselect(".gs_ri"):
            anchors = result.cssselect(".gs_rt a")
            if not anchors:
                continue
            anchor = anchors[0]
            title = " ".join(anchor.itertext()).strip()
            href = anchor.get("href", "")
            snippets = result.cssselect(".gs_rs")
            snippet = " ".join(snippets[0].itertext()).strip() if snippets else ""
            if title and href.startswith(("http://", "https://")):
                records.append({"title": title, "url": href, "snippet": snippet})
        return records


class GooglePatentsBrowserAdapter(BrowserSearchAdapter):
    source_id = "google_patents"
    adapter_id = "google_patents_chrome"
    category = SourceCategory.PATENT
    result_type = "patent"
    search_capabilities = {"patent", "inventor", "assignee", "citation"}
    prefer_ego_before_camoufox = True

    def _search_url(self, request: SearchRequest) -> str:
        return (
            f"https://patents.google.com/?q=({quote_plus(request.query)})"
            f"&oq={quote_plus(request.query)}"
        )

    def _parse(self, source: str) -> list[dict[str, str]]:
        tree = html.fromstring(source)
        records: list[dict[str, str]] = []
        for item in tree.cssselect("search-result-item"):
            state = item.cssselect("state-modifier[data-result]")
            titles = item.cssselect("h3")
            if not state or not titles:
                continue
            data_result = state[0].get("data-result", "")
            if not data_result.startswith("patent/"):
                continue
            patent_id = data_result.split("/", 2)[1]
            raw_nodes = item.cssselect("div.abstract raw-html")
            snippet = " ".join(raw_nodes[-1].itertext()).strip() if raw_nodes else ""
            records.append(
                {
                    "title": " ".join(titles[0].itertext()).strip(),
                    "url": f"https://patents.google.com/{data_result}",
                    "snippet": snippet,
                    "external_id_name": "patent",
                    "external_id": patent_id,
                }
            )
        return records
