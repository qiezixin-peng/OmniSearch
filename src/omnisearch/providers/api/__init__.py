from omnisearch.providers.api.academic import ArxivAdapter, CrossrefAdapter, OpenAlexAdapter
from omnisearch.providers.api.brave import BraveSearchAdapter
from omnisearch.providers.api.discovery import (
    BlueskyAdapter,
    EuropePmcAdapter,
    GdeltAdapter,
    HackerNewsAdapter,
    HuggingFaceAdapter,
    InternetArchiveAdapter,
    XSearchAdapter,
    YouTubeAdapter,
    YtDlpYouTubeAdapter,
)
from omnisearch.providers.api.github import GitHubSearchAdapter
from omnisearch.providers.api.more import (
    CoreAdapter,
    GitLabAdapter,
    PrawRedditAdapter,
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
    "EuropePmcAdapter",
    "GdeltAdapter",
    "GitLabAdapter",
    "GitHubSearchAdapter",
    "HackerNewsAdapter",
    "HuggingFaceAdapter",
    "InternetArchiveAdapter",
    "OpenAlexAdapter",
    "PrawRedditAdapter",
    "RedditAdapter",
    "RssHubAdapter",
    "SemanticScholarAdapter",
    "StackExchangeAdapter",
    "XSearchAdapter",
    "YtDlpYouTubeAdapter",
    "YouTubeAdapter",
]
