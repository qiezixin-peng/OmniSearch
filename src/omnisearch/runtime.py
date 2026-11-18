from dataclasses import dataclass

from omnisearch.config import Settings, get_settings
from omnisearch.core.planning import SearchPlanner
from omnisearch.core.research import ResearchCoordinator
from omnisearch.fetchers import (
    CamoufoxFetcher,
    EgoBrowserFetcher,
    PatchrightFetcher,
    SafeHttpFetcher,
    ScraplingFetcher,
)
from omnisearch.models import OpenAICompatibleModelGateway
from omnisearch.network import StickyProxyManager
from omnisearch.observability import configure_observability
from omnisearch.providers.api import (
    ArxivAdapter,
    BlueskyAdapter,
    BraveSearchAdapter,
    CoreAdapter,
    CrossrefAdapter,
    EuropePmcAdapter,
    GitHubSearchAdapter,
    GitLabAdapter,
    HackerNewsAdapter,
    HuggingFaceAdapter,
    InternetArchiveAdapter,
    OpenAlexAdapter,
    RssHubAdapter,
    SemanticScholarAdapter,
    StackExchangeAdapter,
    XSearchAdapter,
    YouTubeAdapter,
)
from omnisearch.providers.browser import (
    BraveBrowserAdapter,
    DuckDuckGoBrowserAdapter,
    GoogleBrowserAdapter,
    GooglePatentsBrowserAdapter,
    GoogleScholarBrowserAdapter,
    RedditBrowserAdapter,
    XBrowserAdapter,
    YouTubeBrowserAdapter,
)
from omnisearch.providers.catalog import build_default_registry
from omnisearch.providers.registry import SourceRegistry
from omnisearch.services.acquisition import DocumentAcquisitionService
from omnisearch.services.crawl import SiteCrawler
from omnisearch.services.research import ResearchExecutionService
from omnisearch.services.search import MultiSourceSearchService
from omnisearch.storage.database import Database
from omnisearch.storage.documents import DocumentRepository
from omnisearch.storage.objects import LocalObjectStore, S3ObjectStore
from omnisearch.storage.repository import SqlResearchRepository
from omnisearch.storage.telemetry import ProviderTelemetryRepository
from omnisearch.workers.queue import JobQueue


@dataclass
class OmniRuntime:
    database: Database
    repository: SqlResearchRepository
    documents: DocumentRepository
    coordinator: ResearchCoordinator
    sources: SourceRegistry
    search: MultiSourceSearchService
    acquisition: DocumentAcquisitionService
    crawler: SiteCrawler
    research: ResearchExecutionService
    jobs: JobQueue
    provider_telemetry: ProviderTelemetryRepository

    def close(self) -> None:
        self.database.engine.dispose()

    async def aclose(self) -> None:
        await self.search.aclose()
        self.close()


def build_runtime(settings: Settings | None = None) -> OmniRuntime:
    settings = settings or get_settings()
    configure_observability(endpoint=settings.otel_exporter_otlp_endpoint)
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    settings.ego_script_dir.mkdir(parents=True, exist_ok=True)
    settings.chrome_user_data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_url)
    database.create_schema()
    repository = SqlResearchRepository(database)
    provider_telemetry = ProviderTelemetryRepository(database)
    repository.reconcile_cancelled_queries()
    if settings.object_store_backend == "s3":
        object_store = S3ObjectStore(
            settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            access_key=(
                settings.s3_access_key.get_secret_value() if settings.s3_access_key else None
            ),
            secret_key=(
                settings.s3_secret_key.get_secret_value() if settings.s3_secret_key else None
            ),
            region=settings.s3_region,
        )
    else:
        object_store = LocalObjectStore(settings.object_store_path)
    documents = DocumentRepository(database, object_store=object_store)
    coordinator = ResearchCoordinator(repository)
    sources = build_default_registry()
    sticky_proxy = StickyProxyManager(
        runtime_dir=settings.runtime_dir,
        clash_config_dir=settings.clash_verge_config_dir,
        mihomo_executable=settings.mihomo_executable,
        proxy_port=settings.sticky_proxy_port,
        controller_port=settings.sticky_proxy_controller_port,
        enabled=settings.sticky_proxy_enabled,
    )
    scrapling = ScraplingFetcher(secret_resolver=sticky_proxy.resolve)
    patchright = PatchrightFetcher(
        channel=settings.patchright_channel,
        headless=settings.browser_headless,
        secret_resolver=sticky_proxy.resolve,
    )
    browser_fallbacks = [scrapling]
    camoufox_python = settings.camoufox_python.expanduser().resolve()
    if settings.camoufox_enabled and camoufox_python.is_file():
        browser_fallbacks.append(
            CamoufoxFetcher(
                camoufox_python,
                headless=settings.browser_headless,
                secret_resolver=sticky_proxy.resolve,
            )
        )
    browser_fallback_tuple = tuple(browser_fallbacks)
    safe_http = SafeHttpFetcher()
    ego = EgoBrowserFetcher(settings.ego_script_dir)
    acquisition = DocumentAcquisitionService(
        safe_http,
        patchright,
        ego,
        settings.chrome_user_data_dir / "acquisition",
        browser_fallback_fetchers=browser_fallback_tuple,
    )
    crawler = SiteCrawler(acquisition, documents)
    github_token = settings.github_token.get_secret_value() if settings.github_token else None
    brave_key = (
        settings.brave_search_api_key.get_secret_value() if settings.brave_search_api_key else None
    )
    openalex_key = (
        settings.openalex_api_key.get_secret_value() if settings.openalex_api_key else None
    )
    semantic_scholar_key = (
        settings.semantic_scholar_api_key.get_secret_value()
        if settings.semantic_scholar_api_key
        else None
    )
    gitlab_token = settings.gitlab_token.get_secret_value() if settings.gitlab_token else None
    core_key = settings.core_api_key.get_secret_value() if settings.core_api_key else None
    x_token = settings.x_bearer_token.get_secret_value() if settings.x_bearer_token else None
    youtube_key = settings.youtube_api_key.get_secret_value() if settings.youtube_api_key else None
    browser_adapter_options = {
        "chrome_fetcher": patchright,
        "browser_fallback_fetchers": browser_fallback_tuple,
        "ego_fetcher": ego,
        "chrome_user_data_dir": str(settings.chrome_user_data_dir),
        "route_rotator": sticky_proxy.rotate,
    }
    x_browser = XBrowserAdapter(
        **browser_adapter_options,
    )
    x_adapter = XSearchAdapter(bearer_token=x_token, fallback=x_browser) if x_token else x_browser
    brave_browser = BraveBrowserAdapter(
        **browser_adapter_options,
    )
    brave_adapter = (
        BraveSearchAdapter(api_key=brave_key, fallback=brave_browser)
        if brave_key
        else brave_browser
    )
    youtube_browser = YouTubeBrowserAdapter(
        **browser_adapter_options,
    )
    youtube_adapter = (
        YouTubeAdapter(api_key=youtube_key, fallback=youtube_browser)
        if youtube_key
        else youtube_browser
    )
    reddit_browser = RedditBrowserAdapter(
        **browser_adapter_options,
    )
    openalex_adapter = OpenAlexAdapter(api_key=openalex_key)
    crossref_adapter = CrossrefAdapter()
    rsshub_routes = [route.strip() for route in settings.rsshub_routes.split(",") if route.strip()]
    provider_adapters = [
        GitHubSearchAdapter(token=github_token),
        ArxivAdapter(index_fallback=openalex_adapter),
        crossref_adapter,
        openalex_adapter,
        StackExchangeAdapter(),
        GitLabAdapter(token=gitlab_token),
        reddit_browser,
        CoreAdapter(api_key=core_key),
        x_adapter,
        BlueskyAdapter(),
        HackerNewsAdapter(),
        HuggingFaceAdapter(),
        EuropePmcAdapter(),
        RssHubAdapter(base_url=settings.rsshub_base_url, routes=rsshub_routes),
        InternetArchiveAdapter(),
        youtube_adapter,
        brave_adapter,
        GoogleBrowserAdapter(
            **browser_adapter_options,
        ),
        DuckDuckGoBrowserAdapter(
            **browser_adapter_options,
        ),
        GoogleScholarBrowserAdapter(
            index_fallback=crossref_adapter,
            **browser_adapter_options,
        ),
        GooglePatentsBrowserAdapter(
            **browser_adapter_options,
        ),
    ]
    if semantic_scholar_key:
        provider_adapters.append(SemanticScholarAdapter(api_key=semantic_scholar_key))
    search = MultiSourceSearchService(
        provider_adapters,
        api_timeout_seconds=settings.search_api_timeout_seconds,
        browser_timeout_seconds=settings.search_browser_timeout_seconds,
        browser_max_concurrency=settings.browser_global_concurrency,
        cache_ttl_seconds=settings.search_cache_ttl_seconds,
        cache_max_entries=settings.search_cache_max_entries,
        attempt_recorder=provider_telemetry.record,
        stale_fallback_loader=repository.recent_exact_search_results,
    )
    search.seed_persistent_health(provider_telemetry.report(hours=1, limit=1))
    model_gateway = None
    if settings.model_base_url and settings.model_name:
        model_gateway = OpenAICompatibleModelGateway(
            settings.model_base_url,
            settings.model_name,
            api_key=(settings.model_api_key.get_secret_value() if settings.model_api_key else None),
        )
    research = ResearchExecutionService(
        coordinator,
        repository,
        SearchPlanner(
            sources,
            model_gateway=model_gateway,
            available_sources=search.configured_sources,
            source_channels=search.configured_source_channels,
            require_dual_channel=True,
        ),
        search,
        max_concurrency=settings.research_query_concurrency,
        acquisition=acquisition,
        documents=documents,
        max_acquisition_concurrency=settings.acquisition_concurrency,
    )
    jobs = JobQueue(database)
    return OmniRuntime(
        database=database,
        repository=repository,
        documents=documents,
        coordinator=coordinator,
        sources=sources,
        search=search,
        acquisition=acquisition,
        crawler=crawler,
        research=research,
        jobs=jobs,
        provider_telemetry=provider_telemetry,
    )
