from uuid import uuid4

import httpx
import pytest

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult
from omnisearch.providers.api import (
    ArxivAdapter,
    BlueskyAdapter,
    CoreAdapter,
    CrossrefAdapter,
    EuropePmcAdapter,
    GdeltAdapter,
    GitHubSearchAdapter,
    GitLabAdapter,
    HackerNewsAdapter,
    HuggingFaceAdapter,
    InternetArchiveAdapter,
    OpenAlexAdapter,
    RedditAdapter,
    SemanticScholarAdapter,
    StackExchangeAdapter,
    XSearchAdapter,
    YouTubeAdapter,
)
from omnisearch.providers.base import ProviderAdapter


def request() -> SearchRequest:
    return SearchRequest(query_id=uuid4(), query="audio watermark", max_results=5)


@pytest.mark.asyncio
async def test_github_normalization() -> None:
    async def handler(incoming: httpx.Request) -> httpx.Response:
        assert len(incoming.url.params["q"]) <= 256
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


def test_github_long_query_is_shortened_on_a_word_boundary() -> None:
    query = "audio watermark " * 40

    normalized = GitHubSearchAdapter._normalized_query(query)

    assert len(normalized) <= 256
    assert not normalized.endswith(" ")
    assert normalized.split()[-1] in {"audio", "watermark"}


def test_github_network_retries_fit_outer_service_budget() -> None:
    adapter = GitHubSearchAdapter()

    assert adapter._timeout == 8.0
    assert adapter._max_attempts == 2
    assert adapter._trust_environment_proxy is True


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
async def test_arxiv_uses_live_search_when_export_api_is_rate_limited() -> None:
    html = """
    <ol><li class="arxiv-result">
      <p class="list-title"><a href="https://arxiv.org/abs/2401.17264">arXiv</a></p>
      <p class="title">Proactive Detection of Voice Cloning</p>
      <p class="authors"><a>Robin Researcher</a></p>
      <span class="abstract-full">Localized audio watermarking.</span>
    </li></ol>
    """

    async def handler(incoming: httpx.Request) -> httpx.Response:
        if incoming.url.host == "export.arxiv.org":
            return httpx.Response(429, text="rate limited")
        assert incoming.url.host == "arxiv.org"
        return httpx.Response(200, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = [item async for item in ArxivAdapter(client=client).search(request())]

    assert results[0].external_identifiers["arxiv"] == "2401.17264"
    assert results[0].raw_payload == {
        "fallback_used": True,
        "fallback_path": "arxiv_live_search",
        "api_failure_code": "RATE_LIMITED",
    }


@pytest.mark.asyncio
async def test_arxiv_prefers_live_openalex_index_before_html_fallback() -> None:
    async def arxiv_handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    async def index_handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "doi": "https://doi.org/10.48550/arxiv.2401.17264",
                        "display_name": "Localized Watermarking",
                        "type": "preprint",
                        "language": "en",
                        "authorships": [],
                        "open_access": {"oa_url": "https://arxiv.org/pdf/2401.17264"},
                    }
                ]
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(arxiv_handler)) as arxiv_client,
        httpx.AsyncClient(transport=httpx.MockTransport(index_handler)) as index_client,
    ):
        adapter = ArxivAdapter(
            client=arxiv_client,
            index_fallback=OpenAlexAdapter(client=index_client),
        )
        results = [item async for item in adapter.search(request())]

    assert str(results[0].url) == "https://arxiv.org/abs/2401.17264"
    assert results[0].raw_payload["fallback_path"] == "openalex_arxiv_live_index"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_factory", "payload", "expected_source"),
    [
        (
            BlueskyAdapter,
            {
                "posts": [
                    {
                        "uri": "at://did:plc:abc/app.bsky.feed.post/r1",
                        "author": {"handle": "researcher.test", "displayName": "Researcher"},
                        "record": {"text": "New watermark research", "langs": ["en"]},
                    }
                ]
            },
            "bluesky",
        ),
        (
            HackerNewsAdapter,
            {
                "hits": [
                    {
                        "objectID": "42",
                        "title": "Audio watermark release",
                        "author": "dev",
                    }
                ]
            },
            "hacker_news",
        ),
        (
            EuropePmcAdapter,
            {
                "resultList": {
                    "result": [
                        {
                            "pmid": "123",
                            "source": "MED",
                            "title": "Clinical watermark study",
                            "doi": "10.1/example",
                        }
                    ]
                }
            },
            "europe_pmc",
        ),
        (
            GdeltAdapter,
            {
                "articles": [
                    {
                        "url": "https://news.example/research",
                        "title": "Research announcement",
                        "language": "English",
                    }
                ]
            },
            "gdelt",
        ),
        (
            InternetArchiveAdapter,
            {
                "response": {
                    "docs": [
                        {
                            "identifier": "historic-audio-paper",
                            "title": "Historic Audio Paper",
                            "mediatype": "texts",
                        }
                    ]
                }
            },
            "internet_archive",
        ),
    ],
)
async def test_high_value_discovery_adapters_normalize_results(
    adapter_factory, payload, expected_source
) -> None:
    async def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = [item async for item in adapter_factory(client=client).search(request())]

    assert results[0].source_id == expected_source
    assert str(results[0].url).startswith("https://")


@pytest.mark.asyncio
async def test_x_youtube_and_huggingface_normalization() -> None:
    payloads = {
        "api.x.com": {
            "data": [
                {
                    "id": "100",
                    "text": "Official product announcement",
                    "author_id": "u1",
                    "lang": "en",
                }
            ],
            "includes": {"users": [{"id": "u1", "username": "company"}]},
        },
        "googleapis.com": {
            "items": [
                {
                    "id": {"videoId": "v1"},
                    "snippet": {"title": "Technical talk", "channelTitle": "Lab"},
                }
            ]
        },
        "huggingface.co": [
            {"modelId": "lab/watermark-detector", "downloads": 50, "likes": 3}
        ],
    }

    async def handler(incoming: httpx.Request) -> httpx.Response:
        host = incoming.url.host
        key = "googleapis.com" if "googleapis.com" in host else host
        return httpx.Response(200, json=payloads[key])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        x_results = [
            item
            async for item in XSearchAdapter(
                bearer_token="token", client=client
            ).search(request())
        ]
        youtube_results = [
            item
            async for item in YouTubeAdapter(api_key="key", client=client).search(request())
        ]
        huggingface_results = [
            item async for item in HuggingFaceAdapter(client=client).search(request())
        ]

    assert x_results[0].external_identifiers == {"x_post_id": "100"}
    assert youtube_results[0].external_identifiers == {"youtube_video_id": "v1"}
    assert huggingface_results[0].title == "lab/watermark-detector"


@pytest.mark.asyncio
async def test_x_api_falls_back_to_browser_when_credits_are_depleted() -> None:
    class BrowserFallback(ProviderAdapter):
        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                source_id="x",
                adapter_id="x_browser",
                category=SourceCategory.COMMUNITY,
                channel=Channel.BROWSER,
            )

        async def search(self, incoming: SearchRequest):
            yield SearchResult(
                source_id="x",
                adapter_id="x_browser",
                channel=Channel.BROWSER,
                result_type="social_post",
                title="Indexed X post",
                url="https://x.com/example/status/1",
            )

    async def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "credits depleted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = [
            item
            async for item in XSearchAdapter(
                bearer_token="token", fallback=BrowserFallback(), client=client
            ).search(request())
        ]

    assert results[0].channel is Channel.BROWSER
    assert results[0].raw_payload["api_fallback_reason"] == "PAYMENT_REQUIRED"
