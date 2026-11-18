import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from omnisearch.fetchers.base import ContentFetcher, FetchPolicyError
from omnisearch.fetchers.models import FetchedDocument, FetchMode, FetchRequest


class ScraplingUnavailable(RuntimeError):
    pass


class ScraplingFetcher(ContentFetcher):
    """Scrapling adapter with explicit browser isolation and secret resolution."""

    backend_name = "scrapling_chrome"

    def __init__(
        self,
        *,
        http_fetch: Callable[..., Awaitable[Any]] | None = None,
        chrome_fetch: Callable[..., Awaitable[Any]] | None = None,
        cdp_session_factory: Callable[..., Any] | None = None,
        secret_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self._http_fetch = http_fetch
        self._chrome_fetch = chrome_fetch
        self._cdp_session_factory = cdp_session_factory
        self._secret_resolver = secret_resolver

    async def fetch(self, request: FetchRequest) -> FetchedDocument:
        self._validate_profile(request)
        proxy = await asyncio.to_thread(
            self._resolve_proxy, request.proxy_secret_ref
        )
        if request.mode is FetchMode.HTTP:
            response = await self._get_http_fetch()(
                str(request.url),
                timeout=request.timeout_ms,
                **self._http_proxy_options(proxy),
                impersonate="chrome",
                retries=request.retries,
                retry_delay=max(0, request.retry_delay_ms // 1000),
                follow_redirects="safe",
            )
        elif request.mode is FetchMode.CHROME_MANAGED:
            response = await self._get_chrome_fetch()(
                str(request.url),
                **self._browser_options(request, proxy),
                real_chrome=True,
                user_data_dir=request.chrome_user_data_dir,
            )
        elif request.mode is FetchMode.CHROME_CDP:
            factory = self._get_cdp_session_factory()
            async with factory(
                request.cdp_url,
                new_context=request.cdp_new_context,
                locale=request.locale,
                proxy=proxy,
            ) as session:
                response = await session.fetch(
                    str(request.url),
                    timeout=request.timeout_ms,
                    network_idle=request.network_idle,
                    pierce_shadow=request.pierce_shadow,
                )
        else:
            raise FetchPolicyError("ego-browser requests must use the dedicated ego adapter")
        return self._to_document(request, response)

    @staticmethod
    def _http_proxy_options(proxy: Any) -> dict[str, Any]:
        """Translate Playwright proxy dictionaries for Scrapling's HTTP engine."""
        if not isinstance(proxy, dict):
            return {"proxy": proxy}
        server = proxy.get("server")
        if not isinstance(server, str) or not server:
            raise FetchPolicyError("a resolved HTTP proxy requires a server URL")
        options: dict[str, Any] = {"proxy": server}
        username = proxy.get("username")
        password = proxy.get("password")
        if username is not None or password is not None:
            options["proxy_auth"] = (str(username or ""), str(password or ""))
        return options

    @staticmethod
    def _browser_options(request: FetchRequest, proxy: Any) -> dict[str, Any]:
        return {
            "timeout": request.timeout_ms,
            "locale": request.locale,
            "proxy": proxy,
            # Scrapling's browser engine requires at least one attempt, while
            # OmniSearch uses zero to disable backend-level *extra* retries.
            "retries": max(1, request.retries),
            "retry_delay": request.retry_delay_ms / 1000,
            "solve_cloudflare": request.solve_cloudflare,
            "block_ads": request.block_ads,
            "disable_resources": request.disable_resources,
            "network_idle": request.network_idle,
            "hide_canvas": request.hide_canvas,
            "block_webrtc": request.block_webrtc,
            "allow_webgl": request.allow_webgl,
            "pierce_shadow": request.pierce_shadow,
            "headless": request.headless,
            "dns_over_https": request.dns_over_https,
            "wait_selector": request.wait_selector,
            "wait": request.wait_after_load_ms,
            "record_requests": request.record_requests,
            "max_recorded_requests": request.max_recorded_requests,
        }

    @staticmethod
    def _validate_profile(request: FetchRequest) -> None:
        if request.mode is not FetchMode.CHROME_MANAGED:
            return
        path = Path(request.chrome_user_data_dir or "").expanduser().resolve()
        normalized = str(path).replace("/", "\\").lower().rstrip("\\")
        if normalized.endswith("\\google\\chrome\\user data"):
            raise FetchPolicyError("the daily Chrome User Data directory is not allowed")

    def _resolve_proxy(self, secret_ref: str | None) -> Any:
        if secret_ref is None:
            return None
        if self._secret_resolver is None:
            raise FetchPolicyError("a proxy secret reference requires a Secret Store resolver")
        return self._secret_resolver(secret_ref)

    @staticmethod
    def _to_document(request: FetchRequest, response: Any) -> FetchedDocument:
        headers = dict(getattr(response, "headers", {}) or {})
        content_type = headers.get("content-type") or headers.get("Content-Type")
        markdown = response.markdown(
            css_selector=request.css_selector,
            main_content_only=request.main_content_only,
        )
        html = getattr(response, "html_content", None)
        if html is not None and not isinstance(html, str):
            html = str(html)
        return FetchedDocument(
            url=request.url,
            final_url=str(getattr(response, "url", request.url)),
            mode=request.mode,
            status_code=int(getattr(response, "status", 0)),
            content_type=content_type,
            markdown=markdown,
            raw_html=html,
            metadata={
                "engine": "scrapling",
                "solve_cloudflare": request.solve_cloudflare,
                "proxy_used": request.proxy_secret_ref is not None,
                "pierce_shadow": request.pierce_shadow,
                "record_requests": request.record_requests,
                "max_recorded_requests": request.max_recorded_requests,
            },
        )

    def _get_http_fetch(self) -> Callable[..., Awaitable[Any]]:
        if self._http_fetch is None:
            try:
                from scrapling.fetchers import AsyncFetcher
            except ImportError as error:
                raise ScraplingUnavailable(
                    "install OmniSearch with: pip install -e .[browser]"
                ) from error
            self._http_fetch = AsyncFetcher.get
        return self._http_fetch

    def _get_chrome_fetch(self) -> Callable[..., Awaitable[Any]]:
        if self._chrome_fetch is None:
            try:
                from scrapling.fetchers import StealthyFetcher
            except ImportError as error:
                raise ScraplingUnavailable(
                    "install OmniSearch with: pip install -e .[browser]"
                ) from error
            self._chrome_fetch = StealthyFetcher.async_fetch
        return self._chrome_fetch

    def _get_cdp_session_factory(self) -> Callable[..., Any]:
        if self._cdp_session_factory is None:
            try:
                from scrapling.engines._browsers._remote import AsyncRemoteSession
            except ImportError as error:
                raise ScraplingUnavailable(
                    "install OmniSearch with: pip install -e .[browser]"
                ) from error
            self._cdp_session_factory = AsyncRemoteSession
        return self._cdp_session_factory
