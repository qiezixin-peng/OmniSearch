import asyncio
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin
from xml.etree import ElementTree

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult
from omnisearch.providers.api.base import HttpProvider
from omnisearch.providers.base import ProviderAdapter, ProviderError

_TAG = re.compile(r"<[^>]+>")
_LATIN_TERM = re.compile(r"[a-z0-9][a-z0-9_.+-]+", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")


def _text(node: ElementTree.Element, *names: str) -> str:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] in names:
            if child.text and child.text.strip():
                return child.text.strip()
            href = child.attrib.get("href")
            if href:
                return href.strip()
    return ""


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", value))).strip()


def _date(value: str) -> datetime | None:
    if not value:
        return None
    with suppress(ValueError, TypeError, OverflowError):
        return parsedate_to_datetime(value)
    with suppress(ValueError):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _terms(query: str) -> set[str]:
    lowered = query.casefold()
    terms = {term.casefold() for term in _LATIN_TERM.findall(lowered)}
    for run in _CJK_RUN.findall(lowered):
        if len(run) <= 2:
            terms.add(run)
        else:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return {term for term in terms if len(term) > 1}


class RssHubAdapter(HttpProvider, ProviderAdapter):
    """Search recent entries from a configurable set of local RSSHub routes."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:1200",
        routes: Iterable[str] = (),
        client=None,
    ) -> None:
        super().__init__(client=client, timeout_seconds=35.0, max_attempts=2)
        self.base_url = base_url.rstrip("/") + "/"
        self.routes = tuple(route.strip() for route in routes if route.strip())

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="rsshub",
            adapter_id="rsshub_local",
            category=SourceCategory.WEB,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"feed", "news", "release", "continuous_monitoring"},
            max_concurrency=2,
            backend="rsshub_local",
        )

    async def healthcheck(self) -> bool:
        try:
            await self._get_text(
                urljoin(self.base_url, "healthz"), headers={"Accept": "text/plain"}
            )
        except ProviderError:
            return False
        return True

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        if not self.routes:
            raise ProviderError("NOT_CONFIGURED", "RSSHub has no configured routes")
        terms = _terms(request.query)
        candidates: list[tuple[int, SearchResult]] = []
        failures = 0

        async def load(route: str) -> tuple[str, ElementTree.Element] | None:
            feed_url = (
                route
                if route.startswith(("http://", "https://"))
                else urljoin(self.base_url, route.lstrip("/"))
            )
            try:
                async with asyncio.timeout(15):
                    payload = await self._get_text(
                        feed_url,
                        headers={
                            "Accept": "application/rss+xml, application/atom+xml, application/xml"
                        },
                    )
                return feed_url, ElementTree.fromstring(payload)
            except (ProviderError, ElementTree.ParseError, TimeoutError):
                return None

        for loaded in await asyncio.gather(*(load(route) for route in self.routes)):
            if loaded is None:
                failures += 1
                continue
            feed_url, root = loaded
            feed_title = _clean(_text(root, "title"))
            entries = [
                node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"}
            ]
            for entry in entries:
                title = _clean(_text(entry, "title"))
                description = _clean(_text(entry, "description", "summary", "content"))
                link = _text(entry, "link", "guid", "id")
                if not title or not link.startswith(("http://", "https://")):
                    continue
                haystack = f"{title} {description}".casefold()
                score = sum(term in haystack for term in terms)
                if request.query.casefold() in haystack:
                    score += 5
                if terms and score == 0:
                    continue
                published = _date(_text(entry, "pubDate", "published", "updated", "date"))
                if request.date_from and published and published < request.date_from:
                    continue
                if request.date_to and published and published > request.date_to:
                    continue
                candidates.append(
                    (
                        score,
                        SearchResult(
                            source_id="rsshub",
                            adapter_id="rsshub_local",
                            channel=Channel.API,
                            result_type="feed_entry",
                            title=title,
                            url=link,
                            snippet=description[:2000] or None,
                            published_at=published,
                            authors=[author]
                            if (author := _clean(_text(entry, "author", "creator")))
                            else [],
                            raw_payload={"feed_url": feed_url, "feed_title": feed_title},
                        ),
                    )
                )
        if failures == len(self.routes):
            raise ProviderError(
                "UPSTREAM_ERROR", "all configured RSSHub routes failed", retryable=True
            )
        candidates.sort(
            key=lambda item: (
                item[0],
                item[1].published_at.timestamp() if item[1].published_at else 0.0,
            ),
            reverse=True,
        )
        for _, result in candidates[: request.max_results]:
            yield result
