from abc import ABC, abstractmethod

from omnisearch.fetchers.models import FetchedDocument, FetchRequest


class FetchPolicyError(ValueError):
    """A request violates an explicit OmniSearch acquisition policy."""


class ContentFetcher(ABC):
    @abstractmethod
    async def fetch(self, request: FetchRequest) -> FetchedDocument:
        raise NotImplementedError

    async def healthcheck(self) -> bool:
        return True
