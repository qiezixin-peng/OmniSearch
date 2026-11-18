import asyncio
from collections.abc import AsyncIterator
from typing import Any

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult
from omnisearch.providers.base import ProviderAdapter, ProviderError


class DDGSFederatedAdapter(ProviderAdapter):
    """Free HTTP metasearch path with explicit per-backend provenance."""

    source_id = "ddgs"
    adapter_id = "ddgs_federated"
    min_interval_seconds = 2.0

    def __init__(
        self,
        *,
        backends: tuple[str, ...] = ("startpage", "yahoo"),
        timeout_seconds: int = 8,
    ) -> None:
        self.backends = tuple(dict.fromkeys(backends))
        self.timeout_seconds = timeout_seconds

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id=self.source_id,
            adapter_id=self.adapter_id,
            category=SourceCategory.WEB,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"web"},
            supports_pagination=False,
            supports_date_filter=False,
            supports_domain_filter=True,
            max_concurrency=1,
            requires_credentials=False,
            backend="ddgs:" + "+".join(self.backends),
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        query = request.query + (f" site:{request.domain}" if request.domain else "")
        outcomes = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self._search_backend,
                    backend,
                    query,
                    request.max_results,
                )
                for backend in self.backends
            ),
            return_exceptions=True,
        )
        failures: list[str] = []
        records: list[tuple[str, dict[str, Any]]] = []
        for backend, outcome in zip(self.backends, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failures.append(f"{backend}: {type(outcome).__name__}: {outcome}")
                continue
            if not outcome:
                failures.append(f"{backend}: no results")
                continue
            records.extend((backend, item) for item in outcome)
        if not records:
            raise ProviderError(
                "UPSTREAM_ERROR",
                "all DDGS backends failed; " + "; ".join(failures),
                retryable=True,
            )

        seen: set[str] = set()
        rank = 0
        for backend, item in records:
            url = str(item.get("href") or item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url.startswith(("http://", "https://")) or not title or url in seen:
                continue
            seen.add(url)
            rank += 1
            yield SearchResult(
                source_id=self.source_id,
                adapter_id=self.adapter_id,
                channel=Channel.API,
                result_type=self._result_type_for_url(url),
                title=title,
                url=url,
                original_language=request.language,
                snippet=str(item.get("body") or item.get("description") or "") or None,
                raw_payload={
                    "rank": rank,
                    "search_backend": backend,
                    "fallback_used": bool(failures),
                    "failed_attempts": failures,
                },
            )
            if rank >= request.max_results:
                break

    def _search_backend(
        self, backend: str, query: str, max_results: int
    ) -> list[dict[str, Any]]:
        from ddgs import DDGS

        return DDGS(timeout=self.timeout_seconds).text(
            query,
            backend=backend,
            max_results=max_results,
        )

    @staticmethod
    def _result_type_for_url(url: str) -> str:
        lowered = url.casefold()
        if "github.com/" in lowered or "gitlab.com/" in lowered:
            return "repository"
        if "arxiv.org/" in lowered or lowered.endswith(".pdf"):
            return "paper"
        return "web_page"
