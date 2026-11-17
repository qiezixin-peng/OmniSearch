from urllib.parse import urljoin

import httpx

from omnisearch.fetchers.base import ContentFetcher, FetchPolicyError
from omnisearch.fetchers.models import FetchedDocument, FetchMode, FetchRequest
from omnisearch.security import validate_public_url


class SafeHttpFetcher(ContentFetcher):
    """Bounded HTTP fetcher that validates every redirect before following it."""

    _redirects = {301, 302, 303, 307, 308}

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        max_redirects: int = 10,
    ) -> None:
        self._client = client
        self.max_redirects = max_redirects

    async def fetch(self, request: FetchRequest) -> FetchedDocument:
        if request.mode is not FetchMode.HTTP:
            raise FetchPolicyError("SafeHttpFetcher only accepts HTTP mode")
        if self._client is not None:
            return await self._fetch(self._client, request)
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=request.timeout_ms / 1000,
            trust_env=False,
            headers={"User-Agent": "OmniSearch/0.1 secure acquisition"},
        ) as client:
            return await self._fetch(client, request)

    async def _fetch(
        self, client: httpx.AsyncClient, request: FetchRequest
    ) -> FetchedDocument:
        current = str(request.url)
        for redirect_count in range(self.max_redirects + 1):
            await validate_public_url(current)
            async with client.stream("GET", current) as response:
                if response.status_code in self._redirects:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchPolicyError("redirect response has no Location header")
                    if redirect_count >= self.max_redirects:
                        raise FetchPolicyError("HTTP redirect limit exceeded")
                    current = urljoin(current, location)
                    await validate_public_url(current)
                    continue
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > request.max_bytes:
                        raise FetchPolicyError("HTTP response exceeds configured size limit")
                    chunks.append(chunk)
                content = b"".join(chunks)
                content_type = response.headers.get("content-type")
                is_pdf = "application/pdf" in (content_type or "").lower() or content.startswith(
                    b"%PDF-"
                )
                encoding = response.encoding or "utf-8"
                text = "" if is_pdf else content.decode(encoding, errors="replace")
                return FetchedDocument(
                    url=request.url,
                    final_url=str(response.url),
                    mode=FetchMode.HTTP,
                    status_code=response.status_code,
                    content_type=content_type,
                    markdown="",
                    raw_html=text or None,
                    raw_bytes=content if is_pdf else None,
                    metadata={
                        "engine": "safe-httpx",
                        "redirects": redirect_count,
                        "response_bytes": size,
                    },
                )
        raise FetchPolicyError("HTTP redirect limit exceeded")
