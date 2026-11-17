from contextlib import asynccontextmanager

import pytest

from omnisearch.fetchers import (
    CamoufoxFetcher,
    FetchMode,
    FetchRequest,
    PatchrightFetcher,
    StealthBrowserUnavailable,
)


class FakeResponse:
    status = 200

    async def all_headers(self):
        return {"content-type": "text/html; charset=utf-8"}


class FakeLocator:
    async def inner_text(self, timeout):
        return "Search evidence"


class FakePage:
    url = "https://example.com/final"

    async def route(self, _pattern, _handler):
        return None

    async def goto(self, _url, **_kwargs):
        return FakeResponse()

    async def content(self):
        return "<html><body>Search evidence</body></html>"

    def locator(self, _selector):
        return FakeLocator()


class FakeContext:
    pages = [FakePage()]


@pytest.mark.asyncio
async def test_patchright_fetcher_returns_normalized_document(tmp_path, monkeypatch) -> None:
    fetcher = PatchrightFetcher(channel="chrome", headless=True)

    @asynccontextmanager
    async def fake_context(_request, profile, _proxy):
        assert profile == tmp_path / "profile" / "patchright"
        yield FakeContext()

    monkeypatch.setattr(fetcher, "_context", fake_context)
    document = await fetcher.fetch(
        FetchRequest(
            url="https://example.com",
            mode=FetchMode.CHROME_MANAGED,
            chrome_user_data_dir=str(tmp_path / "profile"),
        )
    )

    assert document.status_code == 200
    assert document.markdown == "Search evidence"
    assert document.metadata["engine"] == "patchright_chrome"


@pytest.mark.asyncio
async def test_camoufox_reports_missing_isolated_runtime(tmp_path) -> None:
    fetcher = CamoufoxFetcher(tmp_path / "missing-python.exe")
    with pytest.raises(StealthBrowserUnavailable, match="runtime is missing"):
        await fetcher.fetch(
            FetchRequest(
                url="https://example.com",
                mode=FetchMode.CHROME_MANAGED,
                chrome_user_data_dir=str(tmp_path / "profile"),
            )
        )
