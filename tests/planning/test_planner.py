import pytest

from omnisearch.core.planning import SearchPlanner
from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ResearchBudget
from omnisearch.models import ModelResearchAnalysis, ProposedSubQuestion
from omnisearch.providers.catalog import build_default_registry


def test_plans_chinese_technical_research_in_original_and_english() -> None:
    planner = SearchPlanner(build_default_registry())

    plan = planner.plan("Suno AI 隐藏音频水印是什么算法，有没有开源项目和论文？")

    assert plan.original_language == "zh"
    assert {"zh", "en", "ja", "de"} <= {query.language for query in plan.queries}
    english_queries = " ".join(
        query.query for query in plan.queries if query.language == "en"
    ).lower()
    assert "suno" in english_queries
    assert "audio" in english_queries
    assert "watermark" in english_queries
    assert "algorithm" in english_queries
    categories = set().union(*(question.categories for question in plan.sub_questions))
    assert categories >= {
        SourceCategory.WEB,
        SourceCategory.CODE,
        SourceCategory.ACADEMIC,
    }


def test_selects_sources_appropriate_to_each_sub_question() -> None:
    planner = SearchPlanner(build_default_registry())

    plan = planner.plan("水印开源项目与论文")

    sources_by_categories = {
        frozenset(question.categories): set(
            source
            for query in plan.queries
            if query.sub_question_id == question.id
            for source in query.sources
        )
        for question in plan.sub_questions
    }
    assert sources_by_categories[frozenset({SourceCategory.WEB})] >= {
        "google",
        "duckduckgo",
    }
    assert "github" in sources_by_categories[frozenset({SourceCategory.CODE})]
    assert sources_by_categories[frozenset({SourceCategory.ACADEMIC})] >= {
        "arxiv",
        "crossref",
        "openalex",
    }


def test_respects_question_query_language_and_source_budgets() -> None:
    planner = SearchPlanner(build_default_registry())
    budget = ResearchBudget(
        max_sub_questions=2,
        max_queries=2,
        max_languages=1,
        max_sources_per_query=1,
    )

    plan = planner.plan("音频水印开源项目与论文", budget)

    assert len(plan.sub_questions) == 2
    assert len(plan.queries) == 2
    assert {query.language for query in plan.queries} == {"zh"}
    assert all(len(query.sources) <= 1 for query in plan.queries)
    assert {query.sub_question_id for query in plan.queries} == {
        question.id for question in plan.sub_questions
    }


def test_query_budget_covers_sub_questions_before_extra_languages() -> None:
    planner = SearchPlanner(build_default_registry())
    budget = ResearchBudget(max_queries=6, max_languages=4)

    plan = planner.plan(
        "Suno AI 隐藏音频水印如何检测和定位，有哪些开源 GitHub 实现、论文和限制？",
        budget,
    )

    assert len(plan.sub_questions) == 6
    assert len(plan.queries) == 6
    assert {query.language for query in plan.queries} == {"zh"}
    assert {query.sub_question_id for query in plan.queries} == {
        question.id for question in plan.sub_questions
    }


def test_planner_excludes_runtime_sources_without_credentials() -> None:
    planner = SearchPlanner(
        build_default_registry(),
        available_sources={"google", "duckduckgo", "arxiv", "crossref", "openalex"},
    )

    plan = planner.plan("音频水印论文和官方资料")

    selected = {source for query in plan.queries for source in query.sources}
    assert "brave" not in selected
    assert "core" not in selected
    assert selected <= {"google", "duckduckgo", "arxiv", "crossref", "openalex"}


def test_every_planned_language_retains_direct_browser_discovery() -> None:
    planner = SearchPlanner(build_default_registry())

    plan = planner.plan("音频水印开源项目与论文")

    all_languages = {query.language for query in plan.queries}
    browser_languages = {
        query.language
        for query in plan.queries
        if any(planner.source_channels.get(source) is Channel.BROWSER for source in query.sources)
    }
    assert browser_languages == all_languages


@pytest.mark.asyncio
async def test_model_gateway_can_add_structured_sub_questions() -> None:
    class Gateway:
        async def analyze(self, question: str) -> ModelResearchAnalysis:
            return ModelResearchAnalysis(
                canonical_terms=["spread spectrum"],
                sub_questions=[
                    ProposedSubQuestion(
                        objective="调查扩频水印在重编码后的鲁棒性",
                        categories={SourceCategory.ACADEMIC},
                        evidence_requirements=["重编码实验"],
                    )
                ],
            )

    planner = SearchPlanner(build_default_registry(), model_gateway=Gateway())

    plan = await planner.plan_async("Suno 音频水印检测")

    added = [item for item in plan.sub_questions if "扩频水印" in item.objective]
    assert len(added) == 1
    assert any(query.sub_question_id == added[0].id for query in plan.queries)
