import httpx
import pytest

from omnisearch.providers.api.base import HttpProvider


@pytest.mark.asyncio
async def test_http_provider_retries_rate_limit_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpProvider(client=client, retry_base_seconds=0)

    assert await provider._get_json("https://example.test/search") == {"ok": True}
    assert calls == 2
    await client.aclose()
