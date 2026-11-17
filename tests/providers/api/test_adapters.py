from uuid import uuid4

import httpx
import pytest

from omnisearch.domain.models import SearchRequest
from omnisearch.providers.api import (
    ArxivAdapter,
    CoreAdapter,
    CrossrefAdapter,
    GitHubSearchAdapter,
    GitLabAdapter,
    OpenAlexAdapter,
    RedditAdapter,
    SemanticScholarAdapter,
    StackExchangeAdapter,
)


def request() -> SearchRequest:
    return SearchRequest(query_id=uuid4(), query="audio watermark", max_results=5)


@pytest.mark.asyncio
async def test_github_normalization() -> None:
    async def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 7,
                        "name": "detector",
                        "full_name": "org/detector",
                        "html_url": "https://github.com/org/detector",
                        "description": "Finds watermarks",
                        "owner": {"login": "org"},
                        "stargazers_count": 42,
                        "forks_count": 3,
                        "language": "Python",
                        "topics": ["watermark"],
                        "license": {"spdx_id": "MIT"},
                        "archived": False,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = [item async for item in GitHubSearchAdapter(client=client).search(request())]
    assert results[0].title == "org/detector"
    assert results[0].external_identifiers == {"github_repo_id": "7"}


@pytest.mark.asyncio
async def test_arxiv_atom_normalization() -> None:
    atom = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>https://arxiv.org/abs/2601.12345</id><title>Audio Watermark</title>
      <summary>Detection method</summary><published>2026-01-02T00:00:00Z</published>
      <author><name>A. Researcher</name></author><category term="eess.AS"/></entry>
    </feed>"""

    async def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=atom)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = [item async for item in ArxivAdapter(client=client).search(request())]
    assert results[0].external_identifiers["arxiv"] == "2601.12345"
    assert results[0].authors == ["A. Researcher"]


@pytest.mark.asyncio
async def test_arxiv_id_query_is_exact_and_version_is_normalized() -> None:
    atom = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>https://arxiv.org/abs/1706.03762v7</id><title>Attention Is All You Need</title>
      <summary>Transformer</summary><published>2017-06-12T00:00:00Z</published></entry>
    </feed>"""

    async def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.url.params["id_list"] == "1706.03762"
        assert "search_query" not in incoming.url.params
        return httpx.Response(200, text=atom)

    exact = SearchRequest(
        query_id=uuid4(), query="Attention Is All You Need arXiv 1706.03762", max_results=5
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = [item async for item in ArxivAdapter(client=client).search(exact)]
    assert results[0].external_identifiers["arxiv"] == "1706.03762"
    assert results[0].raw_payload["versioned_arxiv_id"] == "1706.03762v7"


@pytest.mark.asyncio
async def test_crossref_and_openalex_expose_doi_for_cross_source_dedup() -> None:
    async def crossref_handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.1000/example",
                            "title": ["A Paper"],
                            "URL": "https://doi.org/10.1000/example",
                            "author": [{"given": "A", "family": "B"}],
                            "published": {"date-parts": [[2025, 2, 3]]},
                            "type": "journal-article",
                        }
                    ]
                }
            },
        )

    async def openalex_handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "doi": "https://doi.org/10.1000/example",
                        "display_name": "A Paper",
                        "type": "article",
                        "authorships": [],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(crossref_handler)
    ) as crossref_client:
        crossref = [
            item async for item in CrossrefAdapter(client=crossref_client).search(request())
        ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(openalex_handler)
    ) as openalex_client:
        openalex = [
            item async for item in OpenAlexAdapter(client=openalex_client).search(request())
        ]
    assert crossref[0].external_identifiers["doi"] == "10.1000/example"
    assert openalex[0].external_identifiers["doi"] == "10.1000/example"


@pytest.mark.asyncio
async def test_core_normalizes_structured_language_and_mixed_authors() -> None:
    async def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 123,
                        "title": "Audio Watermark Detection",
                        "downloadUrl": "https://example.org/paper.pdf",
                        "language": {"code": "en", "name": "English"},
                        "authors": [{"name": "A. Researcher"}, "B. Author"],
                    }
                ]
            },
        )

    limited_request = request().model_copy(update={"max_results": 1})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = [
            item
            async for item in CoreAdapter(api_key="test-key", client=client).search(
                limited_request
            )
        ]
    assert len(results) == 1
    assert results[0].original_language == "en"
    assert results[0].authors == ["A. Researcher", "B. Author"]
    assert results[0].external_identifiers == {"core": "123"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_factory", "payload", "expected_source"),
    [
        (
            SemanticScholarAdapter,
            {
                "data": [
                    {
                        "paperId": "S1",
                        "title": "Watermark Paper",
                        "url": "https://www.semanticscholar.org/paper/S1",
                        "externalIds": {"DOI": "10.1/test"},
                        "authors": [{"name": "Researcher"}],
                    }
                ]
            },
            "semantic_scholar",
        ),
        (
            StackExchangeAdapter,
            {
                "items": [
                    {
                        "question_id": 1,
                        "title": "How to detect a watermark?",
                        "link": "https://stackoverflow.com/questions/1",
                        "owner": {"display_name": "Developer"},
                    }
                ]
            },
            "stack_exchange",
        ),
        (
            GitLabAdapter,
            [
                {
                    "id": 2,
                    "name": "detector",
                    "path_with_namespace": "lab/detector",
                    "web_url": "https://gitlab.com/lab/detector",
                }
            ],
            "gitlab",
        ),
        (
            RedditAdapter,
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "name": "t3_abc",
                                "title": "Watermark discussion",
                                "permalink": "/r/audio/comments/abc/watermark/",
                            }
                        }
                    ]
                }
            },
            "reddit",
        ),
    ],
)
async def test_additional_pool_adapters_normalize_results(
    adapter_factory, payload, expected_source
) -> None:
    async def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = [item async for item in adapter_factory(client=client).search(request())]

    assert results[0].source_id == expected_source
    assert str(results[0].url).startswith("https://")
