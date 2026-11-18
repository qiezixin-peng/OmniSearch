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


class FakeChromium:
    def __init__(self) -> None:
        self.options = None

    async def launch_persistent_context(self, **options):
        self.options = options
        return ClosableFakeContext()


class ClosableFakeContext(FakeContext):
    async def close(self):
        return None


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()


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


@pytest.mark.asyncio
async def test_patchright_can_bypass_rotating_system_proxy(tmp_path, monkeypatch) -> None:
    fetcher = PatchrightFetcher(channel="chrome", headless=True)
    playwright = FakePlaywright()

    class FakeManager:
        async def __aenter__(self):
            return playwright

        async def __aexit__(self, *_args):
            return None

    class FakePatchrightModule:
        @staticmethod
        def async_playwright():
            return FakeManager()

    monkeypatch.setitem(
        __import__("sys").modules,
        "patchright.async_api",
        FakePatchrightModule(),
    )
    request = FetchRequest(
        url="https://scholar.google.com",
        mode=FetchMode.CHROME_MANAGED,
        chrome_user_data_dir=str(tmp_path / "profile"),
        bypass_system_proxy=True,
    )

    async with fetcher._context(request, tmp_path / "profile", None):
        pass

    assert playwright.chromium.options["args"] == [
        "--no-proxy-server",
        "--proxy-bypass-list=*",
        "--disable-quic",
    ]
