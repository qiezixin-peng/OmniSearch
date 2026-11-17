import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from omnisearch.providers.base import ProviderError


class HttpProvider:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
        user_agent: str = "OmniSearch/0.1",
        trust_environment_proxy: bool = False,
        max_attempts: int = 3,
        retry_base_seconds: float = 0.5,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds
        self._trust_environment_proxy = trust_environment_proxy
        self._max_attempts = max(1, max_attempts)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._request(url, params=params, headers=headers)
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderError("INVALID_RESPONSE", f"invalid JSON from {url}") from error
        if not isinstance(payload, dict):
            raise ProviderError("INVALID_RESPONSE", f"unexpected JSON shape from {url}")
        return payload

    async def _get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        return (await self._request(url, params=params, headers=headers)).text

    async def _request(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        merged_headers = {**self._headers, **(headers or {})}
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            try:
                response = await self._http_client().get(
                    url, params=params, headers=merged_headers
                )
            except httpx.TimeoutException as error:
                if attempt + 1 >= self._max_attempts:
                    raise ProviderError(
                        "TIMEOUT", f"request timed out: {url}", retryable=True
                    ) from error
                await asyncio.sleep(self._retry_base_seconds * (2**attempt))
                continue
            except httpx.HTTPError as error:
                if attempt + 1 >= self._max_attempts:
                    message = str(error) or repr(error)
                    raise ProviderError("NETWORK_ERROR", message, retryable=True) from error
                await asyncio.sleep(self._retry_base_seconds * (2**attempt))
                continue
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            if attempt + 1 >= self._max_attempts:
                break
            await asyncio.sleep(self._retry_delay(response, attempt))
        assert response is not None
        if response.status_code == 429:
            raise ProviderError("RATE_LIMITED", f"rate limited by {url}", retryable=True)
        if response.status_code == 402:
            raise ProviderError("PAYMENT_REQUIRED", f"credits or payment required by {url}")
        if response.status_code in {401, 403}:
            raise ProviderError("ACCESS_DENIED", f"access denied by {url}")
        if response.status_code >= 500:
            raise ProviderError(
                "UPSTREAM_ERROR", f"upstream returned {response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise ProviderError("BAD_RESPONSE", f"upstream returned {response.status_code}")
        return response

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        fallback = self._retry_base_seconds * (2**attempt)
        value = response.headers.get("Retry-After")
        if not value:
            return fallback
        try:
            return min(10.0, max(0.0, float(value)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return min(10.0, max(0.0, (retry_at - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                return fallback

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                trust_env=self._trust_environment_proxy,
                limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
