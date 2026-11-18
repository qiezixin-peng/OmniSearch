import re
from collections.abc import AsyncIterator
from datetime import datetime
from xml.etree import ElementTree

import httpx

from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult
from omnisearch.providers.api.base import HttpProvider
from omnisearch.providers.base import ProviderAdapter, ProviderError

_ARXIV_ID = re.compile(
    r"(?<![\d.])((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))(?:v\d+)?\b",
    re.IGNORECASE,
)


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ArxivAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://export.arxiv.org/api/query"

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(client=client, user_agent="OmniSearch/0.1 arXiv research client")

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="arxiv",
            adapter_id="arxiv_api",
            category=SourceCategory.ACADEMIC,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"paper", "author", "category"},
            supports_pagination=True,
            supports_date_filter=False,
            max_concurrency=1,
            backend="arxiv",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        start = int(request.cursor or 0)
        identifier_match = _ARXIV_ID.search(request.query)
        params: dict[str, object] = {
            "start": start,
            "max_results": request.max_results,
            "sortBy": "relevance",
        }
        if identifier_match:
            params["id_list"] = identifier_match.group(1)
        else:
            params["search_query"] = f"all:{request.query}"
        xml = await self._get_text(
            self.endpoint,
            params=params,
            headers={"Accept": "application/atom+xml"},
        )
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as error:
            raise ProviderError("INVALID_RESPONSE", "invalid arXiv Atom response") from error
        atom = "{http://www.w3.org/2005/Atom}"
        arxiv = "{http://arxiv.org/schemas/atom}"
        for entry in root.findall(f"{atom}entry"):
            entry_url = (entry.findtext(f"{atom}id") or "").strip()
            if not entry_url:
                continue
            versioned_identifier = entry_url.rsplit("/", 1)[-1]
            identifier = re.sub(r"v\d+$", "", versioned_identifier, flags=re.IGNORECASE)
            yield SearchResult(
                source_id="arxiv",
                adapter_id="arxiv_api",
                channel=Channel.API,
                result_type="paper",
                title=" ".join((entry.findtext(f"{atom}title") or "").split()),
                url=entry_url,
                original_language="en",
                snippet=" ".join((entry.findtext(f"{atom}summary") or "").split()),
                published_at=_date(entry.findtext(f"{atom}published")),
                authors=[
                    author.findtext(f"{atom}name") or ""
                    for author in entry.findall(f"{atom}author")
                ],
                external_identifiers={"arxiv": identifier},
                raw_payload={
                    "versioned_arxiv_id": versioned_identifier,
                    "categories": [
                        node.attrib.get("term", "")
                        for node in entry.findall(f"{atom}category")
                    ],
                    "doi": entry.findtext(f"{arxiv}doi"),
                },
            )


class CrossrefAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://api.crossref.org/works"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="crossref",
            adapter_id="crossref_api",
            category=SourceCategory.ACADEMIC,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"paper", "doi", "author", "reference"},
            supports_pagination=True,
            supports_date_filter=True,
            max_concurrency=3,
            backend="crossref",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        params: dict[str, object] = {
            "query.bibliographic": request.query,
            "rows": request.max_results,
            "select": "DOI,title,URL,author,published,abstract,type,subject",
        }
        if request.cursor:
            params["cursor"] = request.cursor
        payload = await self._get_json(self.endpoint, params=params)
        message = payload.get("message", {})
        for item in message.get("items", []):
            title_values = item.get("title") or []
            title = title_values[0] if title_values else item.get("DOI", "Untitled")
            url = item.get("URL") or f"https://doi.org/{item.get('DOI', '')}"
            date_parts = ((item.get("published") or {}).get("date-parts") or [[]])[0]
            published = None
            if date_parts:
                padded = [*date_parts, 1, 1][:3]
                published = datetime(padded[0], padded[1], padded[2])
            authors = [
                " ".join(filter(None, [author.get("given"), author.get("family")]))
                for author in item.get("author", [])
            ]
            yield SearchResult(
                source_id="crossref",
                adapter_id="crossref_api",
                channel=Channel.API,
                result_type=item.get("type", "paper"),
                title=title,
                url=url,
                original_language=None,
                snippet=item.get("abstract"),
                published_at=published,
                authors=authors,
                external_identifiers={"doi": item["DOI"]} if item.get("DOI") else {},
                raw_payload={"subjects": item.get("subject", [])},
            )


class OpenAlexAdapter(HttpProvider, ProviderAdapter):
    endpoint = "https://api.openalex.org/works"

    def __init__(
        self, *, client: httpx.AsyncClient | None = None, api_key: str | None = None
    ) -> None:
        # Keep retries inside SearchService's 20-second API budget. OpenAlex can
        # return a long Retry-After on 429; three attempts would otherwise be
        # misreported by the outer deadline as TIMEOUT instead of RATE_LIMITED.
        super().__init__(client=client, timeout_seconds=12.0, max_attempts=2)
        self.api_key = api_key

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source_id="openalex",
            adapter_id="openalex_api",
            category=SourceCategory.ACADEMIC,
            channel=Channel.API,
            supported_languages={"*"},
            search_capabilities={"paper", "author", "institution", "citation"},
            supports_pagination=True,
            supports_date_filter=True,
            max_concurrency=3,
            backend="openalex",
        )

    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        params: dict[str, object] = {
            "search": request.query,
            "per-page": request.max_results,
        }
        if request.cursor:
            params["cursor"] = request.cursor
        if self.api_key:
            params["api_key"] = self.api_key
        payload = await self._get_json(self.endpoint, params=params)
        for item in payload.get("results", []):
            doi = (item.get("doi") or "").removeprefix("https://doi.org/")
            primary = item.get("primary_location") or {}
            url = item.get("doi") or primary.get("landing_page_url") or item.get("id")
            if not url:
                continue
            yield SearchResult(
                source_id="openalex",
                adapter_id="openalex_api",
                channel=Channel.API,
                result_type=item.get("type", "paper"),
                title=item.get("display_name") or "Untitled",
                url=url,
                original_language=item.get("language"),
                published_at=_date(item.get("publication_date")),
                authors=[
                    (authorship.get("author") or {}).get("display_name", "")
                    for authorship in item.get("authorships", [])
                ],
                external_identifiers={
                    key: value
                    for key, value in {
                        "doi": doi,
                        "openalex": (item.get("id") or "").rsplit("/", 1)[-1],
                    }.items()
                    if value
                },
                raw_payload={
                    "cited_by_count": item.get("cited_by_count"),
                    "open_access": item.get("open_access"),
                    "topics": item.get("topics", []),
                },
            )
