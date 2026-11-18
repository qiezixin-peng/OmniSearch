from omnisearch.domain.enums import Channel, SourceCategory
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
        "ddgs",
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
