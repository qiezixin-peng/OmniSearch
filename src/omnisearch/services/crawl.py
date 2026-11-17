import asyncio
from collections import deque
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from pydantic import Field, HttpUrl

from omnisearch.domain.models import StrictModel
from omnisearch.fetchers import FetchMode, FetchRequest
from omnisearch.services.acquisition import (
    AcquiredDocument,
    AcquisitionRequest,
    DocumentAcquisitionService,
)
from omnisearch.storage.documents import DocumentRepository, StoredDocument


class CrawlRequest(StrictModel):
    seed_url: HttpUrl
    max_pages: int = Field(default=20, ge=1, le=200)
    max_depth: int = Field(default=2, ge=0, le=5)
    concurrency: int = Field(default=4, ge=1, le=12)
    same_host: bool = True
    allowed_domains: list[str] = Field(default_factory=list, max_length=20)
    use_browser_fallback: bool = True
    solve_cloudflare: bool = True
    respect_robots_txt: bool = True


class CrawledPage(StrictModel):
    url: HttpUrl
    depth: int = Field(ge=0)
    document: AcquiredDocument | None = None
    stored: StoredDocument | None = None
    error: str | None = None


class CrawlReport(StrictModel):
    seed_url: HttpUrl
    pages: list[CrawledPage]
    discovered_urls: int = Field(ge=0)
    fetched_pages: int = Field(ge=0)
    failed_pages: int = Field(ge=0)
    skipped_by_robots: int = Field(default=0, ge=0)
    stopped_reason: str


class SiteCrawler:
    """Budgeted breadth-first crawler for deep acquisition of one public site."""

    def __init__(
        self,
        acquisition: DocumentAcquisitionService,
        documents: DocumentRepository,
    ) -> None:
        self.acquisition = acquisition
        self.documents = documents

    async def crawl(self, command: CrawlRequest) -> CrawlReport:
        seed = str(command.seed_url)
        seed_host = (urlsplit(seed).hostname or "").lower()
        allowed = {domain.lower().lstrip(".") for domain in command.allowed_domains}
        queue: deque[tuple[str, int]] = deque([(seed, 0)])
        scheduled = {seed}
        pages: list[CrawledPage] = []
        robots: dict[str, RobotFileParser | None] = {}
        skipped_by_robots = 0

        if command.respect_robots_txt and not await self._can_fetch(seed, robots):
            return CrawlReport(
                seed_url=command.seed_url,
                pages=[],
                discovered_urls=1,
                fetched_pages=0,
                failed_pages=0,
                skipped_by_robots=1,
                stopped_reason="robots_denied",
            )

        while queue and len(pages) < command.max_pages:
            remaining = command.max_pages - len(pages)
            batch_size = min(command.concurrency, remaining, len(queue))
            batch = [queue.popleft() for _ in range(batch_size)]
            outcomes = await asyncio.gather(
                *(self._fetch(url, depth, command) for url, depth in batch)
            )
            pages.extend(outcomes)
            for page in outcomes:
                if page.document is None or page.depth >= command.max_depth:
                    continue
                for link in page.document.metadata.get("links", []):
                    if link in scheduled or not self._allowed(link, seed_host, allowed, command):
                        continue
                    scheduled.add(link)
                    if command.respect_robots_txt and not await self._can_fetch(
                        link, robots
                    ):
                        skipped_by_robots += 1
                        continue
                    queue.append((link, page.depth + 1))

        stopped_reason = "page_budget" if queue and len(pages) >= command.max_pages else "exhausted"
        return CrawlReport(
            seed_url=command.seed_url,
            pages=pages,
            discovered_urls=len(scheduled),
            fetched_pages=sum(page.document is not None for page in pages),
            failed_pages=sum(page.error is not None for page in pages),
            skipped_by_robots=skipped_by_robots,
            stopped_reason=stopped_reason,
        )

    async def _can_fetch(
        self,
        url: str,
        cache: dict[str, RobotFileParser | None],
    ) -> bool:
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if origin not in cache:
            cache[origin] = await self._load_robots(origin)
        parser = cache[origin]
        return parser is None or parser.can_fetch("OmniSearch", url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        fetcher = getattr(self.acquisition, "http_fetcher", None)
        if fetcher is None:
            return None
        robots_url = f"{origin}/robots.txt"
        try:
            fetched = await fetcher.fetch(
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

    async def _fetch(
        self,
        url: str,
        depth: int,
        command: CrawlRequest,
    ) -> CrawledPage:
        try:
            document = await self.acquisition.fetch(
                AcquisitionRequest(
                    url=url,
                    use_browser_fallback=command.use_browser_fallback,
                    solve_cloudflare=command.solve_cloudflare,
                )
            )
            return CrawledPage(
                url=url,
                depth=depth,
                document=document,
                stored=self.documents.store(document),
            )
        except Exception as error:
            return CrawledPage(
                url=url,
                depth=depth,
                error=f"{type(error).__name__}: {error}",
            )

    @staticmethod
    def _allowed(
        url: str,
        seed_host: str,
        allowed_domains: set[str],
        command: CrawlRequest,
    ) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if command.same_host and host != seed_host:
            return False
        return not allowed_domains or any(
            host == domain or host.endswith(f".{domain}") for domain in allowed_domains
        )
