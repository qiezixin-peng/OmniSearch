from urllib.parse import urlsplit

import httpx
import pytest

from omnisearch.fetchers import FetchMode, FetchPolicyError, FetchRequest, SafeHttpFetcher
from omnisearch.security import UnsafeUrlError


@pytest.mark.asyncio
async def test_rejects_unsafe_redirect_before_following(monkeypatch) -> None:
    requests: list[str] = []

    async def validate(url: str) -> None:
        if urlsplit(url).hostname == "127.0.0.1":
            raise UnsafeUrlError("private redirect")

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})

    monkeypatch.setattr("omnisearch.fetchers.http.validate_public_url", validate)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeHttpFetcher(client=client)

    with pytest.raises(UnsafeUrlError, match="private redirect"):
        await fetcher.fetch(FetchRequest(url="https://example.com/start"))
    assert requests == ["https://example.com/start"]
    await client.aclose()


@pytest.mark.asyncio
async def test_detects_pdf_by_content_type_without_pdf_suffix(monkeypatch) -> None:
    async def allow(_url: str) -> None:
        return None

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=b"%PDF-1.7 fake test document",
        )

    monkeypatch.setattr("omnisearch.fetchers.http.validate_public_url", allow)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetched = await SafeHttpFetcher(client=client).fetch(
        FetchRequest(url="https://example.com/download?id=1", mode=FetchMode.HTTP)
    )

    assert fetched.raw_bytes == b"%PDF-1.7 fake test document"
    assert fetched.raw_html is None
    await client.aclose()


@pytest.mark.asyncio
async def test_enforces_response_size_limit(monkeypatch) -> None:
    async def allow(_url: str) -> None:
        return None

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    monkeypatch.setattr("omnisearch.fetchers.http.validate_public_url", allow)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(FetchPolicyError, match="size limit"):
        await SafeHttpFetcher(client=client).fetch(
            FetchRequest(url="https://example.com/large", max_bytes=1024)
        )
    await client.aclose()
