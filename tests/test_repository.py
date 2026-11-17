from omnisearch.core.research.coordinator import ResearchCoordinator
from omnisearch.domain.enums import ResearchStatus
from omnisearch.domain.models import ResearchCreate, SubQuestionCreate
from omnisearch.storage.database import Database
from omnisearch.storage.repository import SqlResearchRepository


def make_service() -> ResearchCoordinator:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    return ResearchCoordinator(SqlResearchRepository(database))


def test_create_and_persist_research() -> None:
    service = make_service()
    task = service.create(
        ResearchCreate(
            question="Suno 的隐藏水印如何检测和定位？",
            original_language="zh-CN",
            sub_questions=[
                SubQuestionCreate(
                    objective="确认公开披露的水印技术原理",
                    target_languages=["zh-CN", "en"],
                    evidence_requirements=["原始论文或官方资料"],
                )
            ],
        )
    )
    restored = service.get(task.id)
    assert restored.question == task.question
    assert restored.status is ResearchStatus.DRAFT
    assert len(restored.sub_questions) == 1


def test_transition_writes_ordered_events() -> None:
    service = make_service()
    task = service.create(ResearchCreate(question="研究音频水印定位算法"))
    service.change_status(task.id, ResearchStatus.PLANNED)
    service.change_status(task.id, ResearchStatus.RUNNING)
    service.change_status(task.id, ResearchStatus.COMPLETED)
    events = service.events(task.id)
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert [event.event_type for event in events] == [
        "RESEARCH_CREATED",
        "SEARCH_PLAN_CREATED",
        "SEARCH_STARTED",
        "RESEARCH_COMPLETED",
    ]


def test_sub_question_dependency_is_normalized_and_restored() -> None:
    service = make_service()
    principle = SubQuestionCreate(objective="确认水印技术原理")
    detection = SubQuestionCreate(
        objective="调查与原理匹配的检测算法", depends_on=[principle.id]
    )
    task = service.create(
        ResearchCreate(
            question="研究音频水印原理与检测算法",
            sub_questions=[principle, detection],
        )
    )
    restored = service.get(task.id)
    assert restored.sub_questions[1].depends_on == [restored.sub_questions[0].id]


def test_relevance_prefers_domain_terms_over_generic_ai_title() -> None:
    text = (
        "Suno AI 隐藏音频水印检测定位 "
        "Suno audio watermark detection localization hidden paper research"
    )
    terms = SqlResearchRepository._relevance_terms(text)
    relevant = SqlResearchRepository._relevance_score(
        {
            "title": "Detecting and localizing hidden audio watermarks in Suno music",
            "snippet": "Audio watermark detector",
        },
        terms,
    )
    irrelevant = SqlResearchRepository._relevance_score(
        {
            "title": "AI exposure and changing job responsibilities",
            "snippet": "A broad artificial intelligence labor study",
        },
        terms,
    )

    assert relevant > irrelevant
    assert {"suno", "audio", "watermark", "detection", "localization"} <= terms
