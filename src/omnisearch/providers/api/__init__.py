from omnisearch.providers.api.academic import ArxivAdapter, CrossrefAdapter, OpenAlexAdapter
from omnisearch.providers.api.brave import BraveSearchAdapter
from omnisearch.providers.api.github import GitHubSearchAdapter
from omnisearch.providers.api.more import (
    CoreAdapter,
    GitLabAdapter,
    RedditAdapter,
    SemanticScholarAdapter,
    StackExchangeAdapter,
)

__all__ = [
    "ArxivAdapter",
    "BraveSearchAdapter",
    "CrossrefAdapter",
    "CoreAdapter",
    "GitLabAdapter",
    "GitHubSearchAdapter",
    "OpenAlexAdapter",
    "RedditAdapter",
    "SemanticScholarAdapter",
    "StackExchangeAdapter",
]
