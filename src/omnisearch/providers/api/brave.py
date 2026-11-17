from collections.abc import AsyncIterator

import httpx

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult
from omnisearch.providers.api.base import HttpProvider
from omnisearch.providers.base import ProviderAdapter, ProviderError


class BraveSearchAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(
        self,
        *,
        api_key: str | None,
        fallback: ProviderAdapter | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(client=client)
        self.api_key = api_key
        self.fallback = fallback

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="brave",
            adapter_id="brave_api",
            category=SourceCategory.WEB,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"web"},
            supports_pagination=True,
            supports_date_filter=True,
            supports_domain_filter=True,
            max_concurrency=5,
            requires_credentials=True,
            backend="brave",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        if not self.api_key:
            raise ProviderError("NOT_CONFIGURED", "Brave Search API key is not configured")
        offset = int(request.cursor or 0)
        query = request.query
        if request.domain:
            query = f"{query} site:{request.domain}"
        try:
            payload = await self._get_json(
                self.endpoint,
                params={
                    "q": query,
                    "count": min(request.max_results, 20),
                    "offset": offset,
                    "search_lang": request.language or "en",
                    "safesearch": "moderate",
                },
                headers={"X-Subscription-Token": self.api_key},
            )
        except ProviderError as error:
            if self.fallback is None or error.code not in {
                "PAYMENT_REQUIRED",
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
        for item in (payload.get("web") or {}).get("results", []):
            yield SearchResult(
                source_id="brave",
                adapter_id="brave_api",
                channel=Channel.API,
                result_type="web_page",
                title=item.get("title") or "Untitled",
                url=item["url"],
                original_language=request.language,
                snippet=item.get("description"),
                raw_payload={
                    "profile": item.get("profile"),
                    "age": item.get("age"),
                    "extra_snippets": item.get("extra_snippets", []),
                },
            )
