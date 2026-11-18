import pytest
from mcp import Client

from omnisearch.apps.mcp import server
from omnisearch.config import get_settings
from omnisearch.domain.enums import ResearchStatus
from omnisearch.domain.models import ResearchBudget, ResearchCreate
from omnisearch.runtime import build_runtime


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
            "get_provider_diagnostics",
            "evaluate_search_quality",
        } <= names
        research_tool = next(tool for tool in listed.tools if tool.name == "research_start")
        assert {"sub_questions", "queries"} <= set(
            research_tool.input_schema["properties"]
        )
        result = await client.call_tool("list_sources", {})
        source_ids = {item["source_id"] for item in result.structured_content["result"]}
        assert {"google", "duckduckgo", "github", "arxiv", "crossref", "openalex"} <= source_ids

    assert server._runtime is not None
    server._runtime.close()
    server._runtime = None


def test_agent_can_supply_complete_research_plan(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "OMNISEARCH_DATABASE_URL", f"sqlite:///{(tmp_path / 'agent-plan.db').as_posix()}"
    )
    monkeypatch.setenv("OMNISEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    get_settings.cache_clear()
    engine = build_runtime()
    try:
        sub_questions = [
            server.AgentSubQuestion(
                key="principle",
                objective="Confirm the published watermark mechanism",
                categories={"web", "academic"},
                evidence_requirements=["primary source"],
            ),
            server.AgentSubQuestion(
                key="implementation",
                objective="Find open-source implementations",
                categories={"code"},
                depends_on=["principle"],
            ),
        ]
        queries = [
            server.AgentPlannedQuery(
                sub_question_key="principle",
                language="en",
                query="audio watermark mechanism paper",
                sources=["arxiv", "core"],
            ),
            server.AgentPlannedQuery(
                sub_question_key="implementation",
                language="en",
                query="audio watermark detector open source",
                sources=["github"],
            ),
        ]
        plan = server._build_agent_plan(
            engine,
            "Research audio watermark detection",
            "en",
            ResearchBudget(max_queries=10),
            sub_questions,
            queries,
        )
        assert len(plan.sub_questions) == 2
        assert plan.sub_questions[1].depends_on == [plan.sub_questions[0].id]
        assert plan.queries[0].sources[:2] == ["arxiv", "core"]
        assert {
            item.value for item in engine.research.planner.channel_coverage(plan)
        } == {"api", "browser"}
        task = engine.coordinator.create(
            ResearchCreate(
                question=plan.question,
                original_language=plan.original_language,
                budget=plan.budget,
            )
        )
        engine.repository.apply_plan(task.id, plan)
        restored = engine.coordinator.get(task.id)
        persisted_plan = engine.repository.load_plan(task.id)
        assert restored.status is ResearchStatus.PLANNED
        assert len(persisted_plan.queries) == 2
    finally:
        engine.close()
        get_settings.cache_clear()
