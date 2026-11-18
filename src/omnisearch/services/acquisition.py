import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from pydantic import Field, HttpUrl

from omnisearch.domain.models import StrictModel
from omnisearch.fetchers import ContentFetcher, FetchMode, FetchPolicyError, FetchRequest
from omnisearch.observability import documents_fetched, tracer
from omnisearch.security import UnsafeUrlError, validate_public_url


def extract_pdf(content: bytes):
    """Lazy compatibility boundary so service startup does not import PyMuPDF."""
    from omnisearch.extractors.pdf import extract_pdf as parse_pdf

    return parse_pdf(content)


class AcquisitionRequest(StrictModel):
    url: HttpUrl
    max_bytes: int = Field(default=30 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)
    min_content_chars: int = Field(default=100, ge=0, le=10000)
    use_browser_fallback: bool = True
    solve_cloudflare: bool = True
    respect_robots_txt: bool = False


class AcquiredDocument(StrictModel):
    url: HttpUrl
    final_url: HttpUrl
    content_type: str | None = None
    acquisition_mode: str
    text: str
    pages: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AcquisitionError(RuntimeError):
    pass


class DocumentAcquisitionService:
    def __init__(
        self,
        http_fetcher: ContentFetcher,
        chrome_fetcher: ContentFetcher,
        ego_fetcher: ContentFetcher,
        chrome_profile_root: Path,
        browser_fallback_fetchers: tuple[ContentFetcher, ...] = (),
    ) -> None:
        self.http_fetcher = http_fetcher
        self.chrome_fetcher = chrome_fetcher
        self.browser_fetchers = (chrome_fetcher, *browser_fallback_fetchers)
        self.ego_fetcher = ego_fetcher
        self.chrome_profile_root = chrome_profile_root
        self._robots_cache: dict[str, RobotFileParser | None] = {}

    async def fetch(self, command: AcquisitionRequest) -> AcquiredDocument:
        url = str(command.url)
        with tracer.start_as_current_span("document.fetch", attributes={"document.url": url}):
            document = await self._fetch(command, url)
            documents_fetched.add(1, {"acquisition.mode": document.acquisition_mode})
            return document

    async def _fetch(self, command: AcquisitionRequest, url: str) -> AcquiredDocument:
        await validate_public_url(url)
        if command.respect_robots_txt and not await self._can_fetch(url):
            raise FetchPolicyError(f"robots.txt disallows OmniSearch from fetching {url}")
        if self._looks_like_pdf(url):
            return await self._fetch_pdf(command)

        attempts: list[str] = []
        for mode, fetcher in self._fetch_chain(command):
            try:
                profile = None
                if mode is FetchMode.CHROME_MANAGED:
                    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
                    profile = str(self.chrome_profile_root / digest)
                fetched = await fetcher.fetch(
                    FetchRequest(
                        url=command.url,
                        mode=mode,
                        max_bytes=command.max_bytes,
                        chrome_user_data_dir=profile,
                        solve_cloudflare=command.solve_cloudflare,
                        network_idle=mode is not FetchMode.HTTP,
                        pierce_shadow=mode is not FetchMode.HTTP,
                    )
                )
                await validate_public_url(str(fetched.final_url))
                if fetched.status_code >= 400:
                    raise AcquisitionError(f"HTTP status {fetched.status_code}")
                if fetched.raw_bytes is not None and self._is_pdf_content(
                    fetched.content_type, fetched.raw_bytes
                ):
                    return self._acquired_pdf(command, fetched, attempts)
                # HTML parsing pulls in lxml/trafilatura. Keep it out of API, MCP and
                # worker startup until a document really needs to be extracted.
                from omnisearch.extractors.html import extract_html, extract_links

                text, metadata = extract_html(
                    fetched.raw_html or "", fetched.markdown
                ) if fetched.raw_html else (fetched.markdown.strip(), {})
                if len(text) < command.min_content_chars:
                    raise AcquisitionError("extracted content is too short")
                return AcquiredDocument(
                    url=command.url,
                    final_url=fetched.final_url,
                    content_type=fetched.content_type,
                    acquisition_mode=mode.value,
                    text=text,
                    metadata={
                        **fetched.metadata,
                        **metadata,
                        "links": extract_links(fetched.raw_html, str(fetched.final_url))
                        if fetched.raw_html
                        else [],
                        "attempts": attempts,
                    },
                )
            except (UnsafeUrlError, FetchPolicyError):
                raise
            except Exception as error:
                attempts.append(f"{mode.value}: {type(error).__name__}: {error}")
        raise AcquisitionError("all acquisition modes failed; " + " | ".join(attempts))

    async def _can_fetch(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if origin not in self._robots_cache:
            self._robots_cache[origin] = await self._load_robots(origin)
        parser = self._robots_cache[origin]
        return parser is None or parser.can_fetch("OmniSearch", url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        robots_url = f"{origin}/robots.txt"
        try:
            fetched = await self.http_fetcher.fetch(
                FetchRequest(
                    url=robots_url,
                    mode=FetchMode.HTTP,
                    max_bytes=1024 * 1024,
                )
            )
        except Exception:
            return None
        parser = RobotFileParser()
        parser.set_url(robots_url)
        if fetched.status_code in {401, 403}:
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser
        if fetched.status_code >= 400 or not fetched.raw_html:
            return None
        parser.parse(fetched.raw_html.splitlines())
        return parser

    def _fetch_chain(self, command: AcquisitionRequest):
        yield FetchMode.HTTP, self.http_fetcher
        if command.use_browser_fallback:
            for fetcher in self.browser_fetchers:
                yield FetchMode.CHROME_MANAGED, fetcher
            yield FetchMode.EGO_BROWSER, self.ego_fetcher

    async def _fetch_pdf(self, command: AcquisitionRequest) -> AcquiredDocument:
        fetched = await self.http_fetcher.fetch(
            FetchRequest(
                url=command.url,
                mode=FetchMode.HTTP,
                max_bytes=command.max_bytes,
            )
        )
        await validate_public_url(str(fetched.final_url))
        if fetched.status_code >= 400:
            raise AcquisitionError(f"HTTP status {fetched.status_code}")
        if fetched.raw_bytes is None:
            raise AcquisitionError("PDF fetcher returned no binary content")
        return self._acquired_pdf(command, fetched, [])

    @staticmethod
    def _is_pdf_content(content_type: str | None, content: bytes) -> bool:
        return "application/pdf" in (content_type or "").lower() or content.startswith(
            b"%PDF-"
        )

    @staticmethod
    def _acquired_pdf(command, fetched, attempts: list[str]) -> AcquiredDocument:
        text, pages, metadata = extract_pdf(fetched.raw_bytes)
        return AcquiredDocument(
            url=command.url,
            final_url=fetched.final_url,
            content_type=fetched.content_type,
            acquisition_mode="pdf_http",
            text=text,
            pages=pages,
            metadata={**fetched.metadata, **metadata, "attempts": attempts},
        )

    @staticmethod
    def _looks_like_pdf(url: str) -> bool:
        return url.lower().split("?", 1)[0].endswith(".pdf")
