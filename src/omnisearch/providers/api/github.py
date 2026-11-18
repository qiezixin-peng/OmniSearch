from collections.abc import AsyncIterator

import httpx

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult
from omnisearch.providers.api.base import HttpProvider
from omnisearch.providers.base import ProviderAdapter


class GitHubSearchAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://api.github.com/search/repositories"
    min_interval_seconds = 6.5

    def __init__(
        self, *, client: httpx.AsyncClient | None = None, token: str | None = None
    ) -> None:
        # Two bounded attempts fit inside SearchService's 20 second API
        # budget. Respect the user's automatically changing Windows proxy;
        # direct GitHub routing is not consistently reachable on every node.
        super().__init__(
            client=client,
            user_agent="OmniSearch/0.1 GitHub research client",
            timeout_seconds=8.0,
            trust_environment_proxy=True,
            max_attempts=2,
        )
        self.token = token

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="github",
            adapter_id="github_rest",
            category=SourceCategory.CODE,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"repository"},
            supports_pagination=True,
            supports_date_filter=True,
            max_concurrency=1,
            requires_credentials=False,
            backend="github_rest",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        page = int(request.cursor or 1)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = await self._get_json(
            self.endpoint,
            params={
                "q": self._normalized_query(request.query),
                "per_page": request.max_results,
                "page": page,
                "sort": "stars",
                "order": "desc",
            },
            headers=headers,
        )
        for item in payload.get("items", []):
            yield SearchResult(
                source_id="github",
                adapter_id="github_rest",
                channel=Channel.API,
                result_type="repository",
                title=item.get("full_name") or item.get("name") or "Untitled repository",
                url=item["html_url"],
                original_language="en",
                snippet=item.get("description"),
                authors=[(item.get("owner") or {}).get("login", "")],
                external_identifiers={"github_repo_id": str(item.get("id"))},
                raw_payload={
                    "stars": item.get("stargazers_count"),
                    "forks": item.get("forks_count"),
                    "language": item.get("language"),
                    "topics": item.get("topics", []),
                    "license": (item.get("license") or {}).get("spdx_id"),
                    "archived": item.get("archived", False),
                    "updated_at": item.get("updated_at"),
                },
            )

    @staticmethod
    def _normalized_query(query: str) -> str:
        # GitHub repository search rejects queries longer than 256 characters.
        # Preserve complete terms rather than sending a request known to return 422.
        normalized = " ".join(query.split())
        if len(normalized) <= 256:
            return normalized
        shortened = normalized[:256].rsplit(" ", 1)[0].strip()
        return shortened or normalized[:256]
