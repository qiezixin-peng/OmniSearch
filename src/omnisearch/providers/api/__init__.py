from omnisearch.providers.api.academic import ArxivAdapter, CrossrefAdapter, OpenAlexAdapter
from omnisearch.providers.api.brave import BraveSearchAdapter
from omnisearch.providers.api.ddgs import DDGSFederatedAdapter
from omnisearch.providers.api.discovery import (
    BlueskyAdapter,
    EuropePmcAdapter,
    GdeltAdapter,
    HackerNewsAdapter,
    HuggingFaceAdapter,
    InternetArchiveAdapter,
    XSearchAdapter,
    YouTubeAdapter,
)
from omnisearch.providers.api.github import GitHubSearchAdapter
from omnisearch.providers.api.more import (
    CoreAdapter,
    GitLabAdapter,
    RedditAdapter,
    SemanticScholarAdapter,
    StackExchangeAdapter,
)
from omnisearch.providers.api.rsshub import RssHubAdapter

__all__ = [
    "ArxivAdapter",
    "BraveSearchAdapter",
    "BlueskyAdapter",
    "CrossrefAdapter",
    "CoreAdapter",
    "DDGSFederatedAdapter",
    "EuropePmcAdapter",
    "GdeltAdapter",
    "GitLabAdapter",
    "GitHubSearchAdapter",
    "HackerNewsAdapter",
    "HuggingFaceAdapter",
    "InternetArchiveAdapter",
    "OpenAlexAdapter",
    "RedditAdapter",
    "RssHubAdapter",
    "SemanticScholarAdapter",
    "StackExchangeAdapter",
    "XSearchAdapter",
    "YouTubeAdapter",
]
