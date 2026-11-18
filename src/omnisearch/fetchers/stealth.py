import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from omnisearch.fetchers.base import ContentFetcher, FetchPolicyError
from omnisearch.fetchers.models import FetchedDocument, FetchMode, FetchRequest


class StealthBrowserUnavailable(RuntimeError):
    pass


class PatchrightFetcher(ContentFetcher):
    """Use the installed Chrome through Patchright without sharing the daily profile."""

    backend_name = "patchright_chrome"

    def __init__(
        self,
        *,
        channel: str = "chrome",
        headless: bool = True,
        secret_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.channel = channel
        self.headless = headless
        self._secret_resolver = secret_resolver

    async def fetch(self, request: FetchRequest) -> FetchedDocument:
        self._validate(request)
        started_at = asyncio.get_running_loop().time()
        proxy = self._resolve_proxy(request.proxy_secret_ref)
        profile = Path(request.chrome_user_data_dir or "") / "patchright"
        profile.mkdir(parents=True, exist_ok=True)
        async with self._context(request, profile, proxy) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            if request.block_ads or request.disable_resources:
                await page.route("**/*", self._route_handler(request))
            response = await page.goto(
                str(request.url),
                wait_until="domcontentloaded",
                timeout=request.timeout_ms,
            )
            elapsed_ms = int((asyncio.get_running_loop().time() - started_at) * 1000)
            await self._settle(page, request, max(250, request.timeout_ms - elapsed_ms))
            raw_html = await page.content()
            markdown = await self._body_text(page)
            final_url = page.url
            status = response.status if response else 200
            headers = await response.all_headers() if response else {}
        return FetchedDocument(
            url=request.url,
            final_url=final_url,
            mode=request.mode,
            status_code=status,
            content_type=headers.get("content-type"),
            markdown=markdown,
            raw_html=raw_html,
            metadata={
                "engine": self.backend_name,
                "channel": self.channel,
                "headless": self.headless,
                "proxy_used": request.proxy_secret_ref is not None,
                "system_proxy_bypassed": request.bypass_system_proxy,
            },
        )

    @asynccontextmanager
    async def _context(
        self, request: FetchRequest, profile: Path, proxy: Any
    ) -> AsyncIterator[Any]:
        try:
            from patchright.async_api import async_playwright
        except ImportError as error:
            raise StealthBrowserUnavailable(
                "Patchright is not installed; install OmniSearch with the browser extra"
            ) from error
        manager = async_playwright()
        playwright = await manager.__aenter__()
        options: dict[str, Any] = {
            "user_data_dir": str(profile),
            "channel": self.channel,
            "headless": self.headless,
        }
        if request.bypass_system_proxy:
            # Chromium otherwise inherits the Windows proxy configuration. A
            # latency-selected proxy group may change exit nodes between
            # requests, which Google treats as a suspicious session change.
            options["args"] = [
                "--no-proxy-server",
                "--proxy-bypass-list=*",
                "--disable-quic",
            ]
        if request.locale:
            options["locale"] = request.locale
        if proxy:
            options["proxy"] = proxy
        context = None
        try:
            context = await playwright.chromium.launch_persistent_context(**options)
            yield context
        finally:
            if context is not None:
                await context.close()
            await manager.__aexit__(None, None, None)

    @staticmethod
    async def _settle(page: Any, request: FetchRequest, remaining_ms: int) -> None:
        if request.wait_selector:
            with suppress(Exception):
                await page.wait_for_selector(
                    request.wait_selector, timeout=min(5_000, remaining_ms)
                )
        if request.network_idle:
            with suppress(Exception):
                await page.wait_for_load_state(
                    "networkidle", timeout=min(request.timeout_ms, 10_000)
                )
        if request.wait_after_load_ms:
            await page.wait_for_timeout(request.wait_after_load_ms)

    @staticmethod
    async def _body_text(page: Any) -> str:
        try:
            return await page.locator("body").inner_text(timeout=5_000)
        except Exception:
            return ""

    @staticmethod
    def _route_handler(request: FetchRequest):
        async def handle(route: Any) -> None:
            resource_type = route.request.resource_type
            url = route.request.url.casefold()
            blocked_type = request.disable_resources and resource_type in {"image", "media", "font"}
            blocked_ad = request.block_ads and any(
                marker in url
                for marker in ("doubleclick.net", "googlesyndication.com", "/ads/", "adservice.")
            )
            await (route.abort() if blocked_type or blocked_ad else route.continue_())

        return handle

    @staticmethod
    def _validate(request: FetchRequest) -> None:
        if request.mode is not FetchMode.CHROME_MANAGED:
            raise FetchPolicyError("PatchrightFetcher only accepts managed browser mode")
        if not request.chrome_user_data_dir:
            raise FetchPolicyError("Patchright requires a dedicated user data directory")

    def _resolve_proxy(self, secret_ref: str | None) -> Any:
        if secret_ref is None:
            return None
        if self._secret_resolver is None:
            raise FetchPolicyError("a proxy secret reference requires a Secret Store resolver")
        return self._secret_resolver(secret_ref)


class CamoufoxFetcher(ContentFetcher):
    """Run Camoufox in an isolated Python environment to avoid Playwright version conflicts."""

    backend_name = "camoufox"
    _result_marker = "OMNISEARCH_CAMOUFOX_RESULT:"

    def __init__(
        self,
        python_executable: Path,
        *,
        headless: bool = True,
        secret_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.python_executable = Path(python_executable)
        self.headless = headless
        self._secret_resolver = secret_resolver

    async def fetch(self, request: FetchRequest) -> FetchedDocument:
        PatchrightFetcher._validate(request)
        if not self.python_executable.is_file():
            raise StealthBrowserUnavailable(
                f"Camoufox runtime is missing: {self.python_executable}"
            )
        proxy = self._resolve_proxy(request.proxy_secret_ref)
        profile = Path(request.chrome_user_data_dir or "") / "camoufox"
        profile.mkdir(parents=True, exist_ok=True)
        payload = {
            "url": str(request.url),
            "timeout_ms": request.timeout_ms,
            "locale": request.locale,
            "proxy": proxy,
            "profile": str(profile),
            "headless": self.headless,
            "network_idle": request.network_idle,
            "wait_selector": request.wait_selector,
            "wait_after_load_ms": request.wait_after_load_ms,
            "block_images": request.disable_resources,
            "block_webrtc": request.block_webrtc,
        }
        bridge = Path(__file__).parents[1] / "bridges" / "camoufox_worker.py"
        process = await asyncio.create_subprocess_exec(
            str(self.python_executable),
            str(bridge),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                timeout=(request.timeout_ms / 1000) + 30,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise StealthBrowserUnavailable("Camoufox bridge timed out") from None
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        output = stdout.decode("utf-8", errors="replace")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise StealthBrowserUnavailable(detail or "Camoufox bridge failed")
        result_line = next(
            (
                line
                for line in reversed(output.splitlines())
                if line.startswith(self._result_marker)
            ),
            None,
        )
        if result_line is None:
            raise StealthBrowserUnavailable("Camoufox bridge returned no result payload")
        result = json.loads(result_line.removeprefix(self._result_marker))
        return FetchedDocument(
            url=request.url,
            final_url=result["final_url"],
            mode=request.mode,
            status_code=result["status_code"],
            content_type=result.get("content_type"),
            markdown=result.get("text", ""),
            raw_html=result.get("html"),
            metadata={
                "engine": self.backend_name,
                "headless": self.headless,
                "proxy_used": request.proxy_secret_ref is not None,
            },
        )

    def _resolve_proxy(self, secret_ref: str | None) -> Any:
        if secret_ref is None:
            return None
        if self._secret_resolver is None:
            raise FetchPolicyError("a proxy secret reference requires a Secret Store resolver")
        return self._secret_resolver(secret_ref)
