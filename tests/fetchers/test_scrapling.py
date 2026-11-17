from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from omnisearch.fetchers import FetchMode, FetchPolicyError, FetchRequest, ScraplingFetcher


@dataclass
class FakeResponse:
    url: str = "https://example.com/final"
    status: int = 200
    headers: dict | None = None
    html_content: str = "<html><body><article>Evidence</article></body></html>"

    def markdown(self, *, css_selector=None, main_content_only=False) -> str:
        assert css_selector is None
        assert main_content_only is True
        return "Evidence"


@pytest.mark.asyncio
async def test_http_fetch_normalizes_document() -> None:
    calls = []

    async def fake_get(url: str, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(headers={"content-type": "text/html"})

    fetcher = ScraplingFetcher(http_fetch=fake_get)
    document = await fetcher.fetch(FetchRequest(url="https://example.com"))
    assert document.markdown == "Evidence"
    assert str(document.final_url) == "https://example.com/final"
    assert document.metadata["engine"] == "scrapling"
    assert calls[0][1] == {"timeout": 30_000, "proxy": None, "impersonate": "chrome"}


@pytest.mark.asyncio
async def test_managed_chrome_preserves_full_scrapling_capabilities(tmp_path) -> None:
    captured = {}

    async def fake_chrome(url: str, **kwargs):
        captured.update(kwargs)
        return FakeResponse(headers={})

    fetcher = ScraplingFetcher(chrome_fetch=fake_chrome)
    await fetcher.fetch(
        FetchRequest(
            url="https://example.com/app",
            mode=FetchMode.CHROME_MANAGED,
            chrome_user_data_dir=str(tmp_path / "dedicated-profile"),
            solve_cloudflare=True,
            hide_canvas=True,
            pierce_shadow=True,
        )
    )
    assert captured["real_chrome"] is True
    assert captured["solve_cloudflare"] is True
    assert captured["hide_canvas"] is True
    assert captured["pierce_shadow"] is True


@pytest.mark.asyncio
async def test_ego_mode_cannot_leak_into_scrapling() -> None:
    fetcher = ScraplingFetcher()
    with pytest.raises(FetchPolicyError, match="dedicated ego adapter"):
        await fetcher.fetch(
            FetchRequest(url="https://example.com", mode=FetchMode.EGO_BROWSER)
        )


def test_cdp_requires_explicit_endpoint() -> None:
    with pytest.raises(ValidationError, match="explicit authorized endpoint"):
        FetchRequest(url="https://example.com", mode=FetchMode.CHROME_CDP)


@pytest.mark.asyncio
async def test_proxy_credentials_are_resolved_outside_task_payload(tmp_path) -> None:
    captured = {}

    async def fake_chrome(url: str, **kwargs):
        captured.update(kwargs)
        return FakeResponse(headers={})

    fetcher = ScraplingFetcher(
        chrome_fetch=fake_chrome,
        secret_resolver=lambda ref: {"server": "http://proxy.test", "username": ref},
    )
    await fetcher.fetch(
        FetchRequest(
            url="https://example.com",
            mode=FetchMode.CHROME_MANAGED,
            chrome_user_data_dir=str(tmp_path / "profile"),
            proxy_secret_ref="vault://proxy/research",
        )
    )
    assert captured["proxy"]["username"] == "vault://proxy/research"
