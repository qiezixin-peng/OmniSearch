from pathlib import Path

import pytest

from omnisearch.extractors import extract_links
from omnisearch.fetchers import FetchedDocument, FetchMode
from omnisearch.services.acquisition import AcquiredDocument
from omnisearch.services.crawl import CrawlRequest, SiteCrawler
from omnisearch.storage.database import Database
from omnisearch.storage.documents import DocumentRepository


class FakeAcquisition:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, command):
        url = str(command.url)
        self.calls.append(url)
        links = {
            "https://example.com/": [
                "https://example.com/a",
                "https://example.com/b",
                "https://outside.test/ignored",
            ],
            "https://example.com/a": ["https://example.com/c"],
            "https://example.com/b": ["https://example.com/c"],
        }.get(url, [])
        return AcquiredDocument(
            url=url,
            final_url=url,
            content_type="text/html",
            acquisition_mode="fake",
            text=f"Content for {url}",
            metadata={"links": links},
        )


class RobotsFetcher:
    async def fetch(self, command):
        return FetchedDocument(
            url=command.url,
            final_url=command.url,
            mode=FetchMode.HTTP,
            status_code=200,
            content_type="text/plain",
            markdown="",
            raw_html="User-agent: *\nDisallow: /b\n",
        )


def test_extract_links_canonicalizes_and_removes_tracking() -> None:
    html = """
    <a href="/guide?utm_source=x&id=1#part">Guide</a>
    <a href="https://EXAMPLE.com:443/guide?id=1">Duplicate</a>
    <a href="mailto:test@example.com">Mail</a>
    """
    assert extract_links(html, "https://example.com/start") == [
        "https://example.com/guide?id=1"
    ]


@pytest.mark.asyncio
async def test_crawler_enforces_host_depth_and_page_budget(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'crawl.db').as_posix()}")
    database.create_schema()
    acquisition = FakeAcquisition()
    crawler = SiteCrawler(acquisition, DocumentRepository(database))

    report = await crawler.crawl(
        CrawlRequest(seed_url="https://example.com", max_pages=3, max_depth=2)
    )

    assert report.fetched_pages == 3
    assert report.failed_pages == 0
    assert report.stopped_reason == "page_budget"
    assert all("outside.test" not in url for url in acquisition.calls)
    assert {page.depth for page in report.pages} == {0, 1}
    database.engine.dispose()


@pytest.mark.asyncio
async def test_crawler_respects_robots_rules_by_default(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'robots.db').as_posix()}")
    database.create_schema()
    acquisition = FakeAcquisition()
    acquisition.http_fetcher = RobotsFetcher()
    crawler = SiteCrawler(acquisition, DocumentRepository(database))

    report = await crawler.crawl(
        CrawlRequest(seed_url="https://example.com", max_pages=10, max_depth=1)
    )

    assert report.skipped_by_robots == 1
    assert "https://example.com/b" not in acquisition.calls
    assert "https://example.com/a" in acquisition.calls
    database.engine.dispose()
