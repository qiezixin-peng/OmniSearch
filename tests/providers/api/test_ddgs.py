from uuid import uuid4

import pytest

from omnisearch.domain.models import SearchRequest
from omnisearch.providers.api import DDGSFederatedAdapter


def request() -> SearchRequest:
    return SearchRequest(
        query_id=uuid4(),
        query="audio watermark",
        domain="example.org",
        max_results=5,
    )


@pytest.mark.asyncio
async def test_ddgs_preserves_backend_provenance_and_deduplicates(monkeypatch) -> None:
    adapter = DDGSFederatedAdapter(backends=("startpage", "yahoo"))

    def search_backend(backend: str, query: str, _limit: int):
        assert query == "audio watermark site:example.org"
        return [
            {
                "title": f"Result from {backend}",
                "href": "https://example.org/paper",
                "body": "Evidence",
            }
        ]

    monkeypatch.setattr(adapter, "_search_backend", search_backend)

    results = [item async for item in adapter.search(request())]

    assert len(results) == 1
    assert results[0].raw_payload["search_backend"] == "startpage"
    assert results[0].raw_payload["fallback_used"] is False
    assert results[0].result_type == "web_page"


@pytest.mark.asyncio
async def test_ddgs_recovers_when_one_backend_fails(monkeypatch) -> None:
    adapter = DDGSFederatedAdapter(backends=("startpage", "yahoo"))

    def search_backend(backend: str, _query: str, _limit: int):
        if backend == "startpage":
            raise RuntimeError("challenge")
        return [
            {
                "title": "Yahoo result",
                "href": "https://github.com/example/project",
                "body": "Repository",
            }
        ]

    monkeypatch.setattr(adapter, "_search_backend", search_backend)

    results = [item async for item in adapter.search(request())]

    assert results[0].raw_payload["search_backend"] == "yahoo"
    assert results[0].raw_payload["fallback_used"] is True
    assert "startpage: RuntimeError: challenge" in results[0].raw_payload["failed_attempts"]
    assert results[0].result_type == "repository"
