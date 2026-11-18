from omnisearch.domain.enums import AdapterReliability, Channel, SourceCategory
from omnisearch.providers.catalog import build_default_registry


def test_default_pool_contains_required_sources() -> None:
    registry = build_default_registry()
    source_ids = {source.source_id for source in registry.list()}
    assert {
        "google",
        "duckduckgo",
        "github",
        "arxiv",
        "crossref",
        "openalex",
        "brave",
    } <= source_ids


def test_google_has_chrome_and_ego_adapters() -> None:
    google = build_default_registry().get("google")
    assert {adapter.backend for adapter in google.adapters} == {
        "patchright_chrome+scrapling_chrome+ego_browser+camoufox",
        "ego_browser",
    }


def test_academic_api_selection_prefers_p0_sources() -> None:
    selected = build_default_registry().select(
        {SourceCategory.ACADEMIC}, channels={Channel.API}, limit=3
    )
    assert {source.source_id for source in selected} == {"arxiv", "crossref", "openalex"}


def test_browser_and_api_adapters_publish_distinct_reliability_classes() -> None:
    registry = build_default_registry()
    assert registry.get("google").adapters[0].reliability_class is (
        AdapterReliability.BROWSER_BEST_EFFORT
    )
    assert registry.get("crossref").adapters[0].reliability_class is (AdapterReliability.PUBLIC_API)
    assert registry.get("core").adapters[0].reliability_class is (
        AdapterReliability.CREDENTIALED_API
    )
    assert any(
        adapter.reliability_class is AdapterReliability.SOURCE_CLIENT
        for adapter in registry.get("youtube").adapters
    )


def test_unconfigured_or_paid_only_sources_are_not_auto_selected() -> None:
    registry = build_default_registry()
    enabled = {source.source_id for source in registry.list()}
    assert "semantic_scholar" not in enabled
    assert "x" not in enabled
    assert "reddit" not in enabled
    assert "youtube" in enabled
