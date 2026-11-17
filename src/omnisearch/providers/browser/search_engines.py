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

    def __init__(
        self,
        *,
        chrome_fetcher: ContentFetcher,
        ego_fetcher: ContentFetcher,
        chrome_user_data_dir: str,
    ) -> None:
        self.chrome_fetcher = chrome_fetcher
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
            backend="scrapling_chrome+ego_browser",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        failures: list[str] = []
        document = None
        parsed_items: list[dict[str, str]] = []
        for url in self._search_urls(request):
            for fetcher, mode in (
                (self.chrome_fetcher, FetchMode.CHROME_MANAGED),
                (self.ego_fetcher, FetchMode.EGO_BROWSER),
            ):
                try:
                    document = await fetcher.fetch(
                        FetchRequest(
                            url=url,
                            mode=mode,
                            chrome_user_data_dir=(
                                self.chrome_user_data_dir
                                if mode is FetchMode.CHROME_MANAGED
                                else None
                            ),
                            ego_task_name=f"OmniSearch {self.source_id} search",
                            solve_cloudflare=True,
                            network_idle=True,
                            main_content_only=False,
                        )
                    )
                    if document.raw_html:
                        candidates = self._parse(document.raw_html)
                        parsed_items = [
                            prepared
                            for item in candidates
                            if (prepared := await self._prepare_item(item)) is not None
                        ]
                    if parsed_items:
                        break
                    failures.append(
                        f"{mode.value} {url}: no search results found in returned page"
                    )
                except Exception as error:
                    failures.append(f"{mode.value} {url}: {error}")
            if parsed_items:
                break
        if document is None or not parsed_items:
            raise ProviderError(
                "BROWSER_SEARCH_FAILED",
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
                raw_payload={"rank": rank, "browser_engine": document.metadata.get("engine")},
            )
            if rank >= request.max_results:
                break

    def _search_url(self, request: SearchRequest) -> str:
        raise NotImplementedError

    def _search_urls(self, request: SearchRequest) -> list[str]:
        return [self._search_url(request)]

    def _parse(self, source: str) -> list[dict[str, str]]:
        raise NotImplementedError

    async def _prepare_item(self, item: dict[str, str]) -> dict[str, str] | None:
        return item


class GoogleBrowserAdapter(BrowserSearchAdapter):
    source_id = "google"
    adapter_id = "google_chrome"

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
            async with httpx.AsyncClient(
                follow_redirects=True, trust_env=False, timeout=20
            ) as client, client.stream(
                "GET", "https://www.google.com" + href
            ) as response:
                final_url = str(response.url)
        except httpx.HTTPError:
            return None
        if "google." in urlparse(final_url).netloc:
            return None
        return {**item, "url": final_url}


class DuckDuckGoBrowserAdapter(BrowserSearchAdapter):
    source_id = "duckduckgo"
    adapter_id = "duckduckgo_chrome"

    def _search_url(self, request: SearchRequest) -> str:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        return f"https://duckduckgo.com/?q={quote_plus(query)}&ia=web"

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
            while container is not None and container.tag != "article" and "result" not in (
                container.get("class") or ""
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


class XBrowserAdapter(BrowserSearchAdapter):
    source_id = "x"
    adapter_id = "x_browser"
    category = SourceCategory.COMMUNITY
    result_type = "social_post"
    search_capabilities = {"post", "author", "conversation", "recent"}

    def _search_url(self, request: SearchRequest) -> str:
        return f"https://x.com/search?q={quote_plus(request.query)}&src=typed_query&f=live"

    def _search_urls(self, request: SearchRequest) -> list[str]:
        language = (request.language or "en").split("-", 1)[0]
        return [
            self._search_url(request),
            (
                "https://www.google.com/search?q="
                f"{quote_plus(request.query + ' site:x.com/*/status')}&hl={language}&num=20"
            ),
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

    def _search_url(self, request: SearchRequest) -> str:
        language = (request.language or "en").split("-", 1)[0]
        return (
            f"https://scholar.google.com/scholar?q={quote_plus(request.query)}"
            f"&hl={language}&num=20"
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
