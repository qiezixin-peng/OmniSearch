from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime

import httpx

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult
from omnisearch.providers.api.base import HttpProvider
from omnisearch.providers.base import ProviderAdapter, ProviderError


def _unix(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value else None


class SemanticScholarAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://api.semanticscholar.org/graph/v1/paper/search"
    min_interval_seconds = 1.1
    rate_limit_cooldown_seconds = 300.0

    def __init__(self, *, api_key: str | None = None, client=None) -> None:
        super().__init__(client=client, timeout_seconds=12.0, max_attempts=2)
        self.api_key = api_key
        self.min_interval_seconds = 1.1 if api_key else 3.0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="semantic_scholar",
            adapter_id="semantic_scholar_api",
            category=SourceCategory.ACADEMIC,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"paper", "author", "citation"},
            supports_pagination=True,
            max_concurrency=1,
            requires_credentials=True,
            backend="semantic_scholar",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        headers = {"x-api-key": self.api_key} if self.api_key else None
        payload = await self._get_json(
            self.endpoint,
            params={
                "query": request.query,
                "limit": request.max_results,
                "offset": int(request.cursor or 0),
                "fields": (
                    "paperId,title,abstract,url,authors,year,publicationDate,"
                    "externalIds,citationCount,openAccessPdf"
                ),
            },
            headers=headers,
        )
        for item in payload.get("data", []):
            identifiers = item.get("externalIds") or {}
            doi = identifiers.get("DOI")
            paper_id = item.get("paperId")
            url = item.get("url") or (
                f"https://www.semanticscholar.org/paper/{paper_id}"
                if paper_id
                else None
            )
            if not url:
                continue
            published = None
            if item.get("publicationDate"):
                with suppress(ValueError):
                    published = datetime.fromisoformat(item["publicationDate"])
            yield SearchResult(
                source_id="semantic_scholar",
                adapter_id="semantic_scholar_api",
                channel=Channel.API,
                result_type="paper",
                title=item.get("title") or "Untitled",
                url=url,
                original_language=None,
                snippet=item.get("abstract"),
                published_at=published,
                authors=[author.get("name", "") for author in item.get("authors", [])],
                external_identifiers={
                    key: value
                    for key, value in {"doi": doi, "semantic_scholar": paper_id}.items()
                    if value
                },
                raw_payload={
                    "citation_count": item.get("citationCount"),
                    "open_access_pdf": item.get("openAccessPdf"),
                    "external_ids": identifiers,
                },
            )


class StackExchangeAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://api.stackexchange.com/2.3/search/advanced"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="stack_exchange",
            adapter_id="stack_exchange_api",
            category=SourceCategory.COMMUNITY,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"question", "answer", "tag"},
            supports_pagination=True,
            supports_date_filter=True,
            max_concurrency=2,
            backend="stack_exchange",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        params: dict[str, object] = {
            "q": request.query,
            "site": "stackoverflow",
            "pagesize": request.max_results,
            "page": int(request.cursor or 1),
            "order": "desc",
            "sort": "relevance",
        }
        if request.date_from:
            params["fromdate"] = int(request.date_from.timestamp())
        if request.date_to:
            params["todate"] = int(request.date_to.timestamp())
        payload = await self._get_json(self.endpoint, params=params)
        for item in payload.get("items", []):
            yield SearchResult(
                source_id="stack_exchange",
                adapter_id="stack_exchange_api",
                channel=Channel.API,
                result_type="question",
                title=item.get("title") or "Untitled question",
                url=item["link"],
                original_language="en",
                published_at=_unix(item.get("creation_date")),
                authors=[(item.get("owner") or {}).get("display_name", "")],
                external_identifiers={"stack_question_id": str(item.get("question_id"))},
                raw_payload={
                    "score": item.get("score"),
                    "answer_count": item.get("answer_count"),
                    "is_answered": item.get("is_answered"),
                    "tags": item.get("tags", []),
                },
            )


class GitLabAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://gitlab.com/api/v4/projects"

    def __init__(self, *, token: str | None = None, client=None) -> None:
        super().__init__(client=client)
        self.token = token

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="gitlab",
            adapter_id="gitlab_api",
            category=SourceCategory.CODE,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"repository"},
            supports_pagination=True,
            max_concurrency=2,
            requires_credentials=False,
            backend="gitlab",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        headers = {"PRIVATE-TOKEN": self.token} if self.token else None
        payload = await self._request(
            self.endpoint,
            params={
                "search": request.query,
                "simple": "true",
                "order_by": "star_count",
                "sort": "desc",
                "per_page": request.max_results,
                "page": int(request.cursor or 1),
            },
            headers=headers,
        )
        items = payload.json()
        if not isinstance(items, list):
            raise ProviderError("INVALID_RESPONSE", "unexpected GitLab response")
        for item in items:
            yield SearchResult(
                source_id="gitlab",
                adapter_id="gitlab_api",
                channel=Channel.API,
                result_type="repository",
                title=item.get("path_with_namespace") or item.get("name") or "Untitled repository",
                url=item["web_url"],
                original_language=None,
                snippet=item.get("description"),
                authors=[(item.get("namespace") or {}).get("full_path", "")],
                external_identifiers={"gitlab_project_id": str(item.get("id"))},
                raw_payload={
                    "stars": item.get("star_count"),
                    "forks": item.get("forks_count"),
                    "topics": item.get("topics", []),
                    "updated_at": item.get("last_activity_at"),
                },
            )


class RedditAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://www.reddit.com/search.json"

    min_interval_seconds = 1.0

    def __init__(self, *, fallback: ProviderAdapter | None = None, client=None) -> None:
        super().__init__(client=client, user_agent="OmniSearch/0.1 research engine")
        self.fallback = fallback

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="reddit",
            adapter_id="reddit_api",
            category=SourceCategory.COMMUNITY,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"post", "community"},
            supports_pagination=True,
            max_concurrency=1,
            backend="reddit",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        try:
            payload = await self._get_json(
                self.endpoint,
                params={
                    "q": request.query,
                    "limit": request.max_results,
                    "sort": "relevance",
                    "raw_json": 1,
                    **({"after": request.cursor} if request.cursor else {}),
                },
            )
        except ProviderError as error:
            if self.fallback is None or error.code not in {
                "ACCESS_DENIED",
                "RATE_LIMITED",
                "TIMEOUT",
                "UPSTREAM_ERROR",
            }:
                raise
            async for result in self.fallback.search(request):
                result.raw_payload["api_fallback_reason"] = error.code
                yield result
            return
        children = (payload.get("data") or {}).get("children", [])
        if not children and self.fallback is not None:
            async for result in self.fallback.search(request):
                result.raw_payload["api_fallback_reason"] = "EMPTY_RESPONSE"
                yield result
            return
        for child in children:
            item = child.get("data") or {}
            permalink = item.get("permalink")
            if not permalink:
                continue
            yield SearchResult(
                source_id="reddit",
                adapter_id="reddit_api",
                channel=Channel.API,
                result_type="post",
                title=item.get("title") or "Untitled post",
                url=f"https://www.reddit.com{permalink}",
                original_language=None,
                snippet=(item.get("selftext") or "")[:1000] or None,
                published_at=_unix(item.get("created_utc")),
                authors=[item.get("author", "")],
                external_identifiers={"reddit_post_id": item.get("name", "")},
                raw_payload={
                    "subreddit": item.get("subreddit"),
                    "score": item.get("score"),
                    "num_comments": item.get("num_comments"),
                },
            )


class CoreAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://api.core.ac.uk/v3/search/works"

    def __init__(self, *, api_key: str | None = None, client=None) -> None:
        super().__init__(client=client)
        self.api_key = api_key

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="core",
            adapter_id="core_api",
            category=SourceCategory.ACADEMIC,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"paper", "full_text"},
            supports_pagination=True,
            max_concurrency=2,
            requires_credentials=True,
            backend="core",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        if not self.api_key:
            raise ProviderError("NOT_CONFIGURED", "CORE API key is not configured")
        try:
            response = await self._http_client().post(
                self.endpoint,
                params={"limit": request.max_results, "offset": int(request.cursor or 0)},
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"q": request.query},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            raise ProviderError("NETWORK_ERROR", str(error), retryable=True) from error
        for item in payload.get("results", [])[: request.max_results]:
            language = item.get("language")
            if isinstance(language, dict):
                language = language.get("code") or language.get("name")
            authors = [
                author.get("name", "") if isinstance(author, dict) else str(author)
                for author in item.get("authors", [])
            ]
            fulltext_urls = item.get("sourceFulltextUrls") or []
            url = item.get("downloadUrl") or (fulltext_urls[0] if fulltext_urls else None)
            if not url:
                url = f"https://core.ac.uk/works/{item.get('id')}"
            yield SearchResult(
                source_id="core",
                adapter_id="core_api",
                channel=Channel.API,
                result_type="paper",
                title=item.get("title") or "Untitled",
                url=url,
                original_language=language or None,
                snippet=item.get("abstract"),
                authors=authors,
                external_identifiers=(
                    {"core": str(item["id"])} if item.get("id") is not None else {}
                ),
                raw_payload={"doi": item.get("doi"), "year": item.get("yearPublished")},
            )
