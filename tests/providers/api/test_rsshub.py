import asyncio
from uuid import uuid4

import httpx
import pytest

from omnisearch.domain.models import SearchRequest
from omnisearch.providers.api import RssHubAdapter


@pytest.mark.asyncio
async def test_rsshub_searches_configured_feed_entries() -> None:
    feed = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Research updates</title>
      <item><title>Robust audio watermark localization released</title>
      <link>https://example.org/watermark</link>
      <description>Open source detector with segment-level results.</description>
      <pubDate>Tue, 17 Nov 2026 04:00:00 GMT</pubDate></item>
      <item><title>Unrelated article</title><link>https://example.org/other</link></item>
    </channel></rss>"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/research"
        return httpx.Response(200, text=feed)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RssHubAdapter(
            base_url="http://127.0.0.1:1200",
            routes=["/research"],
            client=client,
        )
        results = [
            item
            async for item in adapter.search(
                SearchRequest(query_id=uuid4(), query="audio watermark detection", max_results=5)
            )
        ]

    assert len(results) == 1
    assert results[0].title == "Robust audio watermark localization released"
    assert results[0].raw_payload["feed_title"] == "Research updates"


@pytest.mark.asyncio
async def test_rsshub_supports_atom_links() -> None:
    feed = """<feed xmlns="http://www.w3.org/2005/Atom"><title>Releases</title>
      <entry><title>OmniSearch release</title><link href="https://example.org/release" />
      <summary>Agent research engine</summary><updated>2026-11-17T04:00:00Z</updated></entry>
    </feed>"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=feed)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = [
            item
            async for item in RssHubAdapter(routes=["/release"], client=client).search(
                SearchRequest(query_id=uuid4(), query="OmniSearch agent", max_results=5)
            )
        ]

    assert str(results[0].url) == "https://example.org/release"


@pytest.mark.asyncio
async def test_rsshub_fetches_routes_concurrently() -> None:
    both_started = asyncio.Event()
    started = 0
    feed = """<rss><channel><title>Feed</title><item><title>Agent research</title>
    <link>https://example.org/item</link></item></channel></rss>"""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        return httpx.Response(200, text=feed)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = [
            item
            async for item in RssHubAdapter(
                routes=["/first", "/second"], client=client
            ).search(SearchRequest(query_id=uuid4(), query="agent", max_results=5))
        ]

    assert started == 2
    assert results
