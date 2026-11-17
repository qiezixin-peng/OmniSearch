from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from omnisearch.domain.models import ProviderCapabilities, SearchRequest, SearchResult


class ProviderError(RuntimeError):
    """Typed boundary error raised by a source adapter."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ProviderAdapter(ABC):
    """Contract shared by API and browser search adapters."""

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    async def search(self, request: SearchRequest) -> AsyncIterator[SearchResult]:
        """Yield normalized results while preserving provider payload in raw_payload."""
        raise NotImplementedError

    async def healthcheck(self) -> bool:
        return True
