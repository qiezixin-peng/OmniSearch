import socket

import httpx
import pytest

from omnisearch.security.url_validation import UnsafeUrlError, validate_public_url


@pytest.mark.asyncio
async def test_rejects_literal_private_ip() -> None:
    with pytest.raises(UnsafeUrlError):
        await validate_public_url("http://127.0.0.1/admin")


@pytest.mark.asyncio
async def test_verifies_proxy_fake_ip_with_public_doh(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.5", 443))],
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"Answer": [{"name": "example.com", "type": 1, "data": "93.184.216.34"}]},
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "omnisearch.security.url_validation.httpx.AsyncClient",
        lambda *args, **kwargs: original(transport=httpx.MockTransport(handler)),
    )

    await validate_public_url("https://example.com/article")


@pytest.mark.asyncio
async def test_falls_back_to_second_doh_resolver(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.8", 443))
        ],
    )
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "cloudflare-dns.com":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"Answer": [{"name": "example.com", "type": 1, "data": "93.184.216.34"}]},
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "omnisearch.security.url_validation.httpx.AsyncClient",
        lambda *args, **kwargs: original(transport=httpx.MockTransport(handler)),
    )

    await validate_public_url("https://example.com/article")

    assert calls == ["cloudflare-dns.com", "dns.google"]
