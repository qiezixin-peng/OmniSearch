import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
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
    attempt_timeout_ms = 15_000
    slow_backend_timeout_ms = 20_000
    max_attempts: int | None = 4
    # Every adapter uses one persistent profile per source. Concurrent launches
    # against the same directory corrupt the session and close each other's
    # browser contexts, so parallelism belongs across sources, not within one.
    max_concurrency = 1
    wait_after_load_ms = 1_200
    wait_selector: str | None = None
    prefer_ego_before_camoufox = False
    preferred_backends: tuple[str, ...] = ()
    stop_on_access_denied = False
    managed_browser_bypass_system_proxy = False
    managed_browser_proxy_ref: str | None = None

    def __init__(
        self,
        *,
        chrome_fetcher: ContentFetcher,
        ego_fetcher: ContentFetcher,
        chrome_user_data_dir: str,
        browser_fallback_fetchers: tuple[ContentFetcher, ...] = (),
        route_rotator: Callable[[str], str | None] | None = None,
    ) -> None:
        self.chrome_fetcher = chrome_fetcher
        self.browser_fetchers = (chrome_fetcher, *browser_fallback_fetchers)
        self.ego_fetcher = ego_fetcher
        self.chrome_user_data_dir = str(Path(chrome_user_data_dir) / self.source_id)
        self.route_rotator = route_rotator

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
            max_concurrency=self.max_concurrency,
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
                fetch_timeout_ms = self._timeout_for_attempt(fetcher, mode)
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
                            ego_task_name=(f"OmniSearch {self.source_id} {request.query_id}"),
                            solve_cloudflare=True,
                            # The adapter owns the fallback chain. Backend-level
                            # retries make one failed endpoint consume the whole
                            # source budget and prevent the next independent path.
                            retries=0,
                            retry_delay_ms=0,
                            network_idle=False,
                            wait_selector=self.wait_selector,
                            wait_after_load_ms=self.wait_after_load_ms,
                            main_content_only=False,
                            bypass_system_proxy=(
                                self.managed_browser_bypass_system_proxy
                                and mode is FetchMode.CHROME_MANAGED
                            ),
                            proxy_secret_ref=(
                                self.managed_browser_proxy_ref
                                if mode in {FetchMode.CHROME_MANAGED, FetchMode.HTTP}
                                else None
                            ),
                        )
                    )
                if document.raw_html:
                    if self._is_access_denied(document):
                        access_denied = True
                        failures.append(f"{mode.value} {url}: access challenge or rate limit")
                        if mode is not FetchMode.EGO_BROWSER and self.route_rotator:
                            try:
                                self.route_rotator(f"{self.source_id}:access_denied")
                            except Exception as error:
                                failures.append(f"proxy rotation failed: {error}")
                        if self.stop_on_access_denied:
                            break
                        continue
                    candidates = self._filter_candidates(
                        self._parse(document.raw_html), request, url
                    )
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
                result_type=self._result_type_for_url(item["url"]),
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
        urls = self._search_urls(request)
        fetchers = self._fetcher_sequence()
        # Diversify both the endpoint and browser backend early. URL-major
        # ordering used to spend the entire latency budget retrying one blocked
        # endpoint and never reached DuckDuckGo's dynamic page or an indexed
        # community fallback.
        attempts: list[tuple[str, ContentFetcher, FetchMode]] = []
        seen: set[tuple[int, int]] = set()
        for fetcher_index, (fetcher, mode) in enumerate(fetchers):
            url_index = fetcher_index % len(urls)
            attempts.append((urls[url_index], fetcher, mode))
            seen.add((url_index, fetcher_index))
        for url_index, url in enumerate(urls):
            for fetcher_index, (fetcher, mode) in enumerate(fetchers):
                if (url_index, fetcher_index) not in seen:
                    attempts.append((url, fetcher, mode))
        return attempts[: self.max_attempts] if self.max_attempts is not None else attempts

    def _fetcher_sequence(self) -> list[tuple[ContentFetcher, FetchMode]]:
        candidates = [
            *((fetcher, FetchMode.CHROME_MANAGED) for fetcher in self.browser_fetchers),
            (self.ego_fetcher, FetchMode.EGO_BROWSER),
        ]
        if self.preferred_backends:
            priorities = {backend: index for index, backend in enumerate(self.preferred_backends)}
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

    def _timeout_for_attempt(self, fetcher: ContentFetcher, mode: FetchMode) -> int:
        if getattr(fetcher, "backend_name", "") == "camoufox":
            return self.slow_backend_timeout_ms
        return self.attempt_timeout_ms

    @staticmethod
    def _backend_name(fetcher: ContentFetcher, mode: FetchMode) -> str:
        if mode is FetchMode.EGO_BROWSER:
            return "ego_browser"
        return str(getattr(fetcher, "backend_name", type(fetcher).__name__))

    def _parse(self, source: str) -> list[dict[str, str]]:
        raise NotImplementedError

    def _filter_candidates(
        self,
        candidates: list[dict[str, str]],
        request: SearchRequest,
        attempt_url: str,
    ) -> list[dict[str, str]]:
        return candidates

    def _result_type_for_url(self, url: str) -> str:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            host.removeprefix("www.") in {"github.com", "gitlab.com", "codeberg.org"}
            and len(path_parts) >= 2
        ):
            return "repository"
        if host == "arxiv.org" and path_parts and path_parts[0] in {"abs", "pdf"}:
            return "paper"
        return self.result_type

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
            "anomaly-modal",
            "unfortunately, bots use duckduckgo too",
            "error-lite+",
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
    # Keep Ego on the user's automatically selected proxy, but make managed
    # browser fallbacks independent of node changes.
    managed_browser_proxy_ref = "omnisearch://sticky-proxy"
    max_attempts = 3
    attempt_timeout_ms = 8_000

    def _attempts(self, request: SearchRequest) -> list[tuple[str, ContentFetcher, FetchMode]]:
        primary, text_only = self._search_urls(request)
        relaxed = self._relaxed_search_url(request)
        if relaxed != primary:
            # Google sometimes returns an empty shell for site: filters even
            # though the same terms have useful results. Retry once without
            # the operator before spending time on another browser engine.
            return [
                (primary, self.ego_fetcher, FetchMode.EGO_BROWSER),
                (primary, self.chrome_fetcher, FetchMode.CHROME_MANAGED),
                (relaxed, self.chrome_fetcher, FetchMode.CHROME_MANAGED),
            ]
        return [
            (primary, self.ego_fetcher, FetchMode.EGO_BROWSER),
            (primary, self.chrome_fetcher, FetchMode.CHROME_MANAGED),
            (text_only, self.chrome_fetcher, FetchMode.CHROME_MANAGED),
        ]

    def _search_url(self, request: SearchRequest) -> str:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        language = (request.language or "en").split("-", 1)[0]
        return f"https://www.google.com/search?q={quote_plus(query)}&hl={language}&num=20"

    def _search_urls(self, request: SearchRequest) -> list[str]:
        primary = self._search_url(request)
        separator = "&" if "?" in primary else "?"
        return [primary, f"{primary}{separator}udm=14&filter=0"]

    def _relaxed_search_url(self, request: SearchRequest) -> str:
        query = re.sub(r"(?i)(?:^|\s)site:\S+", " ", request.query)
        query = " ".join(query.split())
        if query == request.query and request.domain is None:
            return self._search_url(request)
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
                parsed.scheme not in {"http", "https"}
                or self._is_google_search_host(parsed.hostname)
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
        if self._is_google_search_host(urlparse(final_url).hostname):
            return None
        return {**item, "url": final_url}

    @staticmethod
    def _is_google_search_host(hostname: str | None) -> bool:
        """Reject Google search navigation without dropping Google-owned sources."""
        host = (hostname or "").lower().rstrip(".")
        return host == "google.com" or host.startswith("www.google.")


class DuckDuckGoBrowserAdapter(BrowserSearchAdapter):
    source_id = "duckduckgo"
    adapter_id = "duckduckgo_chrome"
    preferred_backends = (
        "scrapling_chrome",
        "patchright_chrome",
        "ego_browser",
        "camoufox",
    )
    max_attempts = 3
    # DuckDuckGo aggressively challenges bursts even when each request is
    # serialized. Space query starts so a multilingual research batch does
    # not turn into a self-inflicted temporary ban.
    min_interval_seconds = 5.0
    # Three 12s attempts plus wrapper cleanup could never fit inside the
    # service's 35s source budget. Keep the direct HTTP path short and leave
    # enough time for a rotated-route retry and one real browser fallback.
    attempt_timeout_ms = 6_000
    wait_after_load_ms = 2_000
    wait_selector = '[data-testid="result-title-a"], a.result__a'
    managed_browser_proxy_ref = "omnisearch://sticky-proxy"
    managed_browser_bypass_system_proxy = True

    def _search_url(self, request: SearchRequest) -> str:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        return f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"

    def _search_urls(self, request: SearchRequest) -> list[str]:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        encoded = quote_plus(query)
        return [
            f"https://lite.duckduckgo.com/lite/?q={encoded}",
            f"https://duckduckgo.com/?q={encoded}&ia=web",
        ]

    def _attempts(self, request: SearchRequest) -> list[tuple[str, ContentFetcher, FetchMode]]:
        html_url, dynamic_url = self._search_urls(request)
        relaxed_url = self._relaxed_search_url(request)
        attempts: list[tuple[str, ContentFetcher, FetchMode]] = []
        scrapling = next(
            (
                fetcher
                for fetcher in self.browser_fetchers
                if getattr(fetcher, "backend_name", "") == "scrapling_chrome"
            ),
            None,
        )
        if scrapling is not None:
            # The official Lite endpoint normally completes in about one second.
            # On a challenge, BrowserSearchAdapter rotates the sticky route; retry
            # the same low-cost endpoint once on the new egress before launching
            # Chrome. Ego used to sit between these attempts and repeatedly spent
            # 12s on the same challenge page without improving result coverage.
            attempts.extend(
                [
                    (
                        relaxed_url if relaxed_url != html_url else html_url,
                        scrapling,
                        FetchMode.HTTP,
                    ),
                    (html_url, scrapling, FetchMode.HTTP),
                    (dynamic_url, self.chrome_fetcher, FetchMode.CHROME_MANAGED),
                ]
            )
        else:
            attempts.extend(
                [
                    (dynamic_url, self.ego_fetcher, FetchMode.EGO_BROWSER),
                    (dynamic_url, self.chrome_fetcher, FetchMode.CHROME_MANAGED),
                ]
            )
        if scrapling is None:
            fallback = next(
                (
                    fetcher
                    for fetcher in self.browser_fetchers
                    if fetcher is not self.chrome_fetcher
                ),
                None,
            )
            if fallback is not None:
                attempts.append((dynamic_url, fallback, FetchMode.CHROME_MANAGED))
        return attempts[: self.max_attempts]

    def _timeout_for_attempt(self, fetcher: ContentFetcher, mode: FetchMode) -> int:
        # Including the outer 3s cancellation/cleanup allowance, the worst-case
        # chain is 8s + 8s + 9s = 25s, safely below the 35s source deadline.
        return 5_000 if mode is FetchMode.HTTP else 6_000

    def _relaxed_search_url(self, request: SearchRequest) -> str:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        scopes = re.findall(r"(?i)(?:^|\s)site:([^\s]+)", query)
        if not scopes:
            return self._search_url(request)
        query = re.sub(r"(?i)(?:^|\s)site:[^\s]+", " ", query)
        query = " ".join(query.split())
        scope_terms = " ".join(self._scope_terms(scope) for scope in scopes)
        return f"https://lite.duckduckgo.com/lite/?q={quote_plus(query + ' ' + scope_terms)}"

    @staticmethod
    def _scope_terms(scope: str) -> str:
        host, _, path = scope.casefold().removeprefix("www.").partition("/")
        labels = host.split(".")
        # Search by recognizable site name rather than the literal hostname.
        # DuckDuckGo commonly returns no results for "dribbble.com" while the
        # equivalent "dribbble shots" query has the expected indexed pages.
        host_terms = labels[:-1] if len(labels) > 1 else labels
        return " ".join([*host_terms, *path.split("/")]).strip()

    def _filter_candidates(
        self,
        candidates: list[dict[str, str]],
        request: SearchRequest,
        attempt_url: str,
    ) -> list[dict[str, str]]:
        if attempt_url != self._relaxed_search_url(request):
            return candidates
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        scopes = re.findall(r"(?i)(?:^|\s)site:([^\s]+)", query)
        if not scopes:
            return candidates

        def matches_scope(candidate: dict[str, str]) -> bool:
            parsed = urlparse(candidate["url"])
            host = (parsed.hostname or "").casefold().removeprefix("www.")
            path = parsed.path.casefold().rstrip("/")
            for scope in scopes:
                normalized = scope.casefold().removeprefix("www.").rstrip("/")
                scope_host, _, scope_path = normalized.partition("/")
                if host != scope_host and not host.endswith("." + scope_host):
                    continue
                if not scope_path or path.startswith("/" + scope_path):
                    return True
            return False

        return [candidate for candidate in candidates if matches_scope(candidate)]

    def _parse(self, source: str) -> list[dict[str, str]]:
        tree = html.fromstring(source)
        records: list[dict[str, str]] = []
        anchors = tree.cssselect('[data-testid="result-title-a"], a.result__a, a.result-link')
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
    preferred_backends = (
        "scrapling_chrome",
        "ego_browser",
        "patchright_chrome",
        "camoufox",
    )
    max_attempts = 3
    attempt_timeout_ms = 10_000

    def _search_url(self, request: SearchRequest) -> str:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        language = (request.language or "en").split("-", 1)[0]
        return f"https://search.brave.com/search?q={quote_plus(query)}&source=web&lang={language}"

    def _parse(self, source: str) -> list[dict[str, str]]:
        tree = html.fromstring(source)
        records: list[dict[str, str]] = []
        selectors = (
            '[data-type="web"] a[href]',
            ".snippet a[href]",
            ".result a[href]",
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
            title = " ".join((title_nodes[0] if title_nodes else anchor).itertext()).strip()
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
    preferred_backends = (
        "scrapling_chrome",
        "patchright_chrome",
        "ego_browser",
        "camoufox",
    )
    max_attempts = 4
    attempt_timeout_ms = 12_000
    wait_selector = 'a[data-testid="post-title-text"], a[href*="/comments/"]'

    def _search_urls(self, request: SearchRequest) -> list[str]:
        query = quote_plus(request.query)
        indexed = quote_plus(request.query + " site:reddit.com/comments")
        return [
            f"https://www.google.com/search?q={indexed}&num=20",
            f"https://html.duckduckgo.com/html/?q={indexed}",
            f"https://www.reddit.com/search/?q={query}&sort=relevance",
        ]

    def _attempts(self, request: SearchRequest) -> list[tuple[str, ContentFetcher, FetchMode]]:
        direct_url = self._search_urls(request)[2]
        attempts: list[tuple[str, ContentFetcher, FetchMode]] = [
            (direct_url, self.ego_fetcher, FetchMode.EGO_BROWSER)
        ]
        scrapling = next(
            (
                fetcher
                for fetcher in self.browser_fetchers
                if getattr(fetcher, "backend_name", "") == "scrapling_chrome"
            ),
            None,
        )
        if scrapling is not None:
            attempts.append((self._search_urls(request)[1], scrapling, FetchMode.HTTP))
        attempts.extend(
            attempt for attempt in super()._attempts(request) if attempt not in attempts
        )
        return attempts[: self.max_attempts]

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
            f"https://duckduckgo.com/?q={quote_plus(request.query + ' site:x.com')}&ia=web",
            "https://www.google.com/search?q="
            f"{quote_plus(request.query + ' site:x.com/status')}&num=20",
        ]

    def _attempts(self, request: SearchRequest) -> list[tuple[str, ContentFetcher, FetchMode]]:
        direct_url, indexed_url, google_url = self._search_urls(request)
        return [
            *(
                (url, fetcher, FetchMode.CHROME_MANAGED)
                for url in (indexed_url, google_url)
                for fetcher in self.browser_fetchers
            ),
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
    # Official Scholar probes have their own 30-minute challenge cooldown.
    # Keep the adapter-level interval short so live Crossref recovery is not
    # forced to sleep for a full minute between independent queries.
    min_interval_seconds = 1.0
    access_denied_cooldown_seconds = 21_600.0
    prefer_ego_before_camoufox = True
    preferred_backends = (
        "ego_browser",
        "patchright_chrome",
        "scrapling_chrome",
        "camoufox",
    )
    managed_browser_proxy_ref = "omnisearch://sticky-proxy"
    stop_on_access_denied = False
    max_attempts = 2
    max_concurrency = 1
    official_probe_cooldown_seconds = 1_800.0

    def __init__(
        self,
        *,
        index_fallback: ProviderAdapter | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.index_fallback = index_fallback
        self._official_retry_after = 0.0

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        now = asyncio.get_running_loop().time()
        failure = ProviderError(
            "ACCESS_DENIED",
            "official Google Scholar probe is cooling down after an access challenge",
            retryable=True,
        )
        if now >= self._official_retry_after:
            try:
                async for item in super().search(request):
                    yield item
                return
            except ProviderError as scholar_error:
                if self.index_fallback is None:
                    raise
                failure = scholar_error
                self._official_retry_after = now + self.official_probe_cooldown_seconds
        elif self.index_fallback is None:
            raise failure
        found = False
        async for item in self.index_fallback.search(request):
            found = True
            yield item.model_copy(
                update={
                    "source_id": self.source_id,
                    "adapter_id": "google_scholar_live_index",
                    "raw_payload": {
                        **item.raw_payload,
                        "fallback_used": True,
                        "fallback_path": f"{item.source_id}_live_academic_index",
                        "scholar_failure_code": failure.code,
                    },
                }
            )
        if not found:
            raise failure

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
    max_attempts = 3

    def _attempts(self, request: SearchRequest) -> list[tuple[str, ContentFetcher, FetchMode]]:
        attempts: list[tuple[str, ContentFetcher, FetchMode]] = []
        scrapling = next(
            (
                fetcher
                for fetcher in self.browser_fetchers
                if getattr(fetcher, "backend_name", "") == "scrapling_chrome"
            ),
            None,
        )
        if scrapling is not None:
            attempts.append((self._xhr_url(request), scrapling, FetchMode.HTTP))
        attempts.extend(super()._attempts(request))
        return attempts[: self.max_attempts]

    def _search_url(self, request: SearchRequest) -> str:
        return (
            f"https://patents.google.com/?q=({quote_plus(request.query)})"
            f"&oq={quote_plus(request.query)}"
        )

    @staticmethod
    def _xhr_url(request: SearchRequest) -> str:
        encoded_query = quote_plus(f"q={request.query}")
        return f"https://patents.google.com/xhr/query?url={encoded_query}&exp="

    def _parse(self, source: str) -> list[dict[str, str]]:
        json_source = source.lstrip()
        if not json_source.startswith("{") and "application/json" not in json_source[:200]:
            try:
                extracted = html.fromstring(source).text_content().strip()
            except (TypeError, ValueError):
                extracted = ""
            if extracted.startswith("{"):
                json_source = extracted
        if json_source.startswith("{"):
            try:
                payload = json.loads(json_source)
            except json.JSONDecodeError:
                payload = {}
            records: list[dict[str, str]] = []
            for cluster in (payload.get("results") or {}).get("cluster", []):
                for result in cluster.get("result", []):
                    data_result = result.get("id", "")
                    patent = result.get("patent") or {}
                    if not data_result.startswith("patent/"):
                        continue
                    patent_id = data_result.split("/", 2)[1]
                    title = self._fragment_text(patent.get("title", ""))
                    if not title:
                        continue
                    records.append(
                        {
                            "title": title,
                            "url": f"https://patents.google.com/{data_result}",
                            "snippet": self._fragment_text(patent.get("snippet", "")),
                            "external_id_name": "patent",
                            "external_id": patent_id,
                        }
                    )
            return records
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

    @staticmethod
    def _fragment_text(value: str) -> str:
        if not value:
            return ""
        return " ".join(html.fromstring(f"<div>{value}</div>").text_content().split())
