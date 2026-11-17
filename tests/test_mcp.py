import pytest
from mcp import Client

from omnisearch.apps.mcp import server
from omnisearch.config import get_settings


@pytest.mark.asyncio
async def test_mcp_exposes_agent_first_tools(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "OMNISEARCH_DATABASE_URL", f"sqlite:///{(tmp_path / 'mcp.db').as_posix()}"
    )
    monkeypatch.setenv("OMNISEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv(
        "OMNISEARCH_CHROME_USER_DATA_DIR", str(tmp_path / "chrome-profile")
    )
    monkeypatch.setenv("OMNISEARCH_EGO_SCRIPT_DIR", str(tmp_path / "ego-scripts"))
    get_settings.cache_clear()
    server._runtime = None

    async with Client(server.mcp) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {
            "search",
            "search_batch",
            "search_sources",
            "research_start",
            "research_continue",
            "research_cancel",
            "research_status",
            "research_get_result",
            "fetch_page",
            "fetch_batch",
            "crawl_site",
            "get_document",
            "search_documents",
            "list_sources",
            "get_evidence",
            "verify_claim",
            "get_source_health",
            "evaluate_search_quality",
        } <= names
        result = await client.call_tool("list_sources", {})
        source_ids = {item["source_id"] for item in result.structured_content["result"]}
        assert {"google", "duckduckgo", "github", "arxiv", "crossref", "openalex"} <= source_ids

    assert server._runtime is not None
    server._runtime.close()
    server._runtime = None
