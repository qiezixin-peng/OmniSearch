from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import datetime
from typing import Any
from urllib.parse import quote

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult
from omnisearch.providers.api.base import HttpProvider
from omnisearch.providers.base import ProviderAdapter, ProviderError


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    with suppress(ValueError):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


class XSearchAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://api.x.com/2/tweets/search/recent"

    def __init__(self, *, bearer_token: str | None = None, client=None) -> None:
        super().__init__(client=client)
        self.bearer_token = bearer_token

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="x",
            adapter_id="x_api",
            category=SourceCategory.COMMUNITY,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"post", "author", "conversation", "recent"},
            supports_pagination=True,
            supports_date_filter=True,
            max_concurrency=2,
            requires_credentials=True,
            backend="x_api_v2",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        if not self.bearer_token:
            raise ProviderError("NOT_CONFIGURED", "X API bearer token is not configured")
        params: dict[str, Any] = {
            "query": request.query,
            "max_results": max(10, min(100, request.max_results)),
            "expansions": "author_id",
            "tweet.fields": "created_at,lang,public_metrics,conversation_id,author_id",
            "user.fields": "username,name,verified",
        }
        if request.cursor:
            params["next_token"] = request.cursor
        if request.date_from:
            params["start_time"] = request.date_from.isoformat()
        if request.date_to:
            params["end_time"] = request.date_to.isoformat()
        payload = await self._get_json(
            self.endpoint,
            params=params,
            headers={"Authorization": f"Bearer {self.bearer_token}"},
        )
        users = {
            item["id"]: item
            for item in (payload.get("includes") or {}).get("users", [])
            if item.get("id")
        }
        for item in payload.get("data", [])[: request.max_results]:
            post_id = item.get("id")
            text = item.get("text") or ""
            if not post_id:
                continue
            author = users.get(item.get("author_id"), {})
            username = author.get("username") or "i"
            yield SearchResult(
                source_id="x",
                adapter_id="x_api",
                channel=Channel.API,
                result_type="social_post",
                title=f"@{username}: {text[:140] or 'X post'}",
                url=f"https://x.com/{username}/status/{post_id}",
                original_language=item.get("lang"),
                snippet=text or None,
                published_at=_iso(item.get("created_at")),
                authors=[author.get("name") or username],
                external_identifiers={"x_post_id": post_id},
                raw_payload={
                    "public_metrics": item.get("public_metrics") or {},
                    "conversation_id": item.get("conversation_id"),
                    "verified_author": author.get("verified"),
                },
            )


class BlueskyAdapter(HttpProvider, ProviderAdapter):
    # The public AppView hostname currently rejects post search while the app
    # AppView exposes the same unauthenticated lexicon method.
    endpoint = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="bluesky",
            adapter_id="bluesky_public_api",
            category=SourceCategory.COMMUNITY,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"post", "author", "conversation"},
            supports_pagination=True,
            max_concurrency=2,
            backend="atproto_public_api",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        params: dict[str, Any] = {"q": request.query, "limit": request.max_results}
        if request.cursor:
            params["cursor"] = request.cursor
        payload = await self._get_json(self.endpoint, params=params)
        for item in payload.get("posts", []):
            uri = item.get("uri") or ""
            record = item.get("record") or {}
            author = item.get("author") or {}
            handle = author.get("handle")
            rkey = uri.rsplit("/", 1)[-1] if "/" in uri else ""
            text = record.get("text") or ""
            if not handle or not rkey:
                continue
            yield SearchResult(
                source_id="bluesky",
                adapter_id="bluesky_public_api",
                channel=Channel.API,
                result_type="social_post",
                title=f"@{handle}: {text[:140] or 'Bluesky post'}",
                url=f"https://bsky.app/profile/{handle}/post/{rkey}",
                original_language=(record.get("langs") or [None])[0],
                snippet=text or None,
                published_at=_iso(record.get("createdAt") or item.get("indexedAt")),
                authors=[author.get("displayName") or handle],
                external_identifiers={"at_uri": uri},
                raw_payload={
                    "reply_count": item.get("replyCount"),
                    "repost_count": item.get("repostCount"),
                    "like_count": item.get("likeCount"),
                },
            )


class HackerNewsAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://hn.algolia.com/api/v1/search"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="hacker_news",
            adapter_id="hacker_news_algolia",
            category=SourceCategory.COMMUNITY,
            channel=Channel.API,
            supported_languages={"en"},
            search_capabilities={"story", "comment", "author"},
            supports_pagination=True,
            supports_date_filter=True,
            max_concurrency=3,
            backend="hn_algolia",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        params: dict[str, Any] = {
            "query": request.query,
            "hitsPerPage": request.max_results,
            "page": int(request.cursor or 0),
            "tags": "story",
        }
        filters = []
        if request.date_from:
            filters.append(f"created_at_i>{int(request.date_from.timestamp())}")
        if request.date_to:
            filters.append(f"created_at_i<{int(request.date_to.timestamp())}")
        if filters:
            params["numericFilters"] = ",".join(filters)
        payload = await self._get_json(self.endpoint, params=params)
        for item in payload.get("hits", []):
            object_id = str(item.get("objectID") or "")
            title = item.get("title") or item.get("story_title")
            if not object_id or not title:
                continue
            yield SearchResult(
                source_id="hacker_news",
                adapter_id="hacker_news_algolia",
                channel=Channel.API,
                result_type="discussion",
                title=title,
                url=f"https://news.ycombinator.com/item?id={object_id}",
                original_language="en",
                snippet=item.get("story_text") or None,
                published_at=_iso(item.get("created_at")),
                authors=[item.get("author", "")],
                external_identifiers={"hn_item_id": object_id},
                raw_payload={
                    "outbound_url": item.get("url"),
                    "points": item.get("points"),
                    "comments": item.get("num_comments"),
                },
            )


class HuggingFaceAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://huggingface.co/api/models"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="huggingface",
            adapter_id="huggingface_hub_api",
            category=SourceCategory.CODE,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"model", "repository", "model_card"},
            supports_pagination=False,
            max_concurrency=3,
            backend="huggingface_hub",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        response = await self._request(
            self.endpoint,
            params={
                "search": request.query,
                "limit": request.max_results,
                "sort": "downloads",
                "direction": -1,
                "full": "true",
            },
            headers=None,
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ProviderError("INVALID_RESPONSE", "unexpected Hugging Face response")
        for item in payload:
            model_id = item.get("modelId") or item.get("id")
            if not model_id:
                continue
            card = item.get("cardData") or {}
            yield SearchResult(
                source_id="huggingface",
                adapter_id="huggingface_hub_api",
                channel=Channel.API,
                result_type="model_repository",
                title=model_id,
                url=f"https://huggingface.co/{model_id}",
                original_language=None,
                snippet=card.get("description") or item.get("description"),
                authors=[model_id.split("/", 1)[0]] if "/" in model_id else [],
                external_identifiers={"huggingface_model": model_id},
                raw_payload={
                    "pipeline_tag": item.get("pipeline_tag"),
                    "downloads": item.get("downloads"),
                    "likes": item.get("likes"),
                    "last_modified": item.get("lastModified"),
                    "tags": item.get("tags") or [],
                },
            )


class EuropePmcAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="europe_pmc",
            adapter_id="europe_pmc_api",
            category=SourceCategory.ACADEMIC,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"paper", "biomedical", "full_text", "citation"},
            supports_pagination=True,
            supports_date_filter=True,
            max_concurrency=3,
            backend="europe_pmc",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        payload = await self._get_json(
            self.endpoint,
            params={
                "query": request.query,
                "format": "json",
                "pageSize": request.max_results,
                "cursorMark": request.cursor or "*",
                "resultType": "core",
            },
        )
        for item in (payload.get("resultList") or {}).get("result", []):
            identifier = item.get("pmcid") or item.get("pmid") or item.get("id")
            if not identifier:
                continue
            doi = item.get("doi")
            yield SearchResult(
                source_id="europe_pmc",
                adapter_id="europe_pmc_api",
                channel=Channel.API,
                result_type="paper",
                title=item.get("title") or "Untitled biomedical paper",
                url=f"https://europepmc.org/article/{item.get('source', 'MED')}/{identifier}",
                original_language=None,
                snippet=item.get("abstractText"),
                published_at=_iso(item.get("firstPublicationDate")),
                authors=[
                    name.strip()
                    for name in (item.get("authorString") or "").split(",")
                    if name.strip()
                ],
                external_identifiers={
                    key: value
                    for key, value in {
                        "doi": doi,
                        "pmid": item.get("pmid"),
                        "pmcid": item.get("pmcid"),
                    }.items()
                    if value
                },
                raw_payload={
                    "journal": item.get("journalTitle"),
                    "cited_by_count": item.get("citedByCount"),
                    "open_access": item.get("isOpenAccess"),
                    "full_text": item.get("inEPMC"),
                },
            )


class GdeltAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="gdelt",
            adapter_id="gdelt_doc_api",
            category=SourceCategory.WEB,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"news", "event", "global_media"},
            supports_date_filter=True,
            max_concurrency=2,
            backend="gdelt_doc_2",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        payload = await self._get_json(
            self.endpoint,
            params={
                "query": request.query,
                "mode": "artlist",
                "format": "json",
                "sort": "HybridRel",
                "maxrecords": min(250, request.max_results),
            },
        )
        for item in payload.get("articles", [])[: request.max_results]:
            url = item.get("url")
            if not url:
                continue
            yield SearchResult(
                source_id="gdelt",
                adapter_id="gdelt_doc_api",
                channel=Channel.API,
                result_type="news_article",
                title=item.get("title") or "Untitled article",
                url=url,
                original_language=item.get("language"),
                snippet=None,
                published_at=_iso(item.get("seendate")),
                external_identifiers={},
                raw_payload={
                    "domain": item.get("domain"),
                    "source_country": item.get("sourcecountry"),
                    "social_image": item.get("socialimage"),
                },
            )


class InternetArchiveAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://archive.org/advancedsearch.php"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="internet_archive",
            adapter_id="internet_archive_api",
            category=SourceCategory.WEB,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"archive", "historical_document", "media", "metadata"},
            supports_pagination=True,
            max_concurrency=2,
            backend="internet_archive_advanced_search",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        payload = await self._get_json(
            self.endpoint,
            params={
                "q": request.query,
                "fl[]": ["identifier", "title", "description", "creator", "date", "mediatype"],
                "rows": request.max_results,
                "page": int(request.cursor or 1),
                "output": "json",
            },
        )
        for item in (payload.get("response") or {}).get("docs", []):
            identifier = item.get("identifier")
            if not identifier:
                continue
            creator = item.get("creator") or []
            if isinstance(creator, str):
                creator = [creator]
            description = item.get("description")
            if isinstance(description, list):
                description = " ".join(str(value) for value in description)
            yield SearchResult(
                source_id="internet_archive",
                adapter_id="internet_archive_api",
                channel=Channel.API,
                result_type=item.get("mediatype") or "archive_item",
                title=item.get("title") or identifier,
                url=f"https://archive.org/details/{quote(identifier, safe='')}",
                original_language=None,
                snippet=str(description)[:2000] if description else None,
                published_at=_iso(item.get("date")),
                authors=[str(value) for value in creator],
                external_identifiers={"internet_archive": identifier},
                raw_payload={"media_type": item.get("mediatype")},
            )


class YouTubeAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://www.googleapis.com/youtube/v3/search"

    def __init__(self, *, api_key: str | None = None, client=None) -> None:
        super().__init__(client=client)
        self.api_key = api_key

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="youtube",
            adapter_id="youtube_data_api",
            category=SourceCategory.WEB,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"video", "channel", "first_party_statement"},
            supports_pagination=True,
            supports_date_filter=True,
            max_concurrency=2,
            requires_credentials=True,
            backend="youtube_data_api_v3",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        if not self.api_key:
            raise ProviderError("NOT_CONFIGURED", "YouTube API key is not configured")
        params: dict[str, Any] = {
            "key": self.api_key,
            "part": "snippet",
            "q": request.query,
            "type": "video",
            "maxResults": min(50, request.max_results),
            "order": "relevance",
        }
        if request.cursor:
            params["pageToken"] = request.cursor
        if request.date_from:
            params["publishedAfter"] = request.date_from.isoformat()
        if request.date_to:
            params["publishedBefore"] = request.date_to.isoformat()
        payload = await self._get_json(self.endpoint, params=params)
        for item in payload.get("items", []):
            video_id = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not video_id:
                continue
            yield SearchResult(
                source_id="youtube",
                adapter_id="youtube_data_api",
                channel=Channel.API,
                result_type="video",
                title=snippet.get("title") or "Untitled video",
                url=f"https://www.youtube.com/watch?v={video_id}",
                original_language=None,
                snippet=snippet.get("description"),
                published_at=_iso(snippet.get("publishedAt")),
                authors=[snippet.get("channelTitle", "")],
                external_identifiers={"youtube_video_id": video_id},
                raw_payload={"channel_id": snippet.get("channelId")},
            )
