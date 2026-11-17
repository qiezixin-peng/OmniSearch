import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from omnisearch.core.planning import PlannedQuery, PlannedSubQuestion, ResearchPlan
from omnisearch.domain.enums import ResearchStatus
from omnisearch.domain.models import (
    ResearchBudget,
    ResearchEvent,
    ResearchTask,
    SubQuestion,
)
from omnisearch.services.search import QuickSearchResponse
from omnisearch.storage.database import Database
from omnisearch.storage.tables import (
    QueryResultDiscoveryRow,
    QueryVariantRow,
    QuestionDependencyRow,
    ResearchEventRow,
    ResearchTaskRow,
    ResultDocumentRow,
    SearchResultRow,
    SubQuestionRow,
)


class ResearchNotFound(LookupError):
    pass


class SqlResearchRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, task: ResearchTask, initial_event: ResearchEvent) -> None:
        row = ResearchTaskRow(
            id=str(task.id),
            question=task.question,
            goal=task.goal,
            original_language=task.original_language,
            status=task.status.value,
            budget=task.budget.model_dump(mode="json"),
            current_round=task.current_round,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
        row.sub_questions = [self._sub_question_to_row(item) for item in task.sub_questions]
        row.events = [self._event_to_row(initial_event)]
        with self.database.session_factory.begin() as session:
            session.add(row)
            session.add_all(
                QuestionDependencyRow(
                    sub_question_id=str(item.id), depends_on_id=str(dependency_id)
                )
                for item in task.sub_questions
                for dependency_id in item.depends_on
            )

    def get(self, research_id: UUID) -> ResearchTask:
        with self.database.session_factory() as session:
            statement = (
                select(ResearchTaskRow)
                .where(ResearchTaskRow.id == str(research_id))
                .options(selectinload(ResearchTaskRow.sub_questions))
            )
            row = session.scalar(statement)
            if row is None:
                raise ResearchNotFound(str(research_id))
            question_ids = [item.id for item in row.sub_questions]
            dependency_rows = session.scalars(
                select(QuestionDependencyRow).where(
                    QuestionDependencyRow.sub_question_id.in_(question_ids)
                )
            ).all()
            dependencies: dict[str, list[UUID]] = {item_id: [] for item_id in question_ids}
            for dependency in dependency_rows:
                dependencies[dependency.sub_question_id].append(UUID(dependency.depends_on_id))
            return self._to_domain(row, dependencies)

    def transition(
        self, task: ResearchTask, event_type: str, payload: dict
    ) -> ResearchEvent:
        with self.database.session_factory.begin() as session:
            row = session.get(ResearchTaskRow, str(task.id))
            if row is None:
                raise ResearchNotFound(str(task.id))
            sequence = session.scalar(
                select(func.coalesce(func.max(ResearchEventRow.sequence), 0)).where(
                    ResearchEventRow.research_id == str(task.id)
                )
            )
            event = ResearchEvent(
                research_id=task.id,
                event_type=event_type,
                sequence=int(sequence) + 1,
                payload=payload,
            )
            row.status = task.status.value
            row.updated_at = task.updated_at
            session.add(self._event_to_row(event))
            return event

    def apply_plan(self, research_id: UUID, plan: ResearchPlan) -> None:
        with self.database.session_factory.begin() as session:
            row = session.get(ResearchTaskRow, str(research_id))
            if row is None:
                raise ResearchNotFound(str(research_id))
            if row.sub_questions:
                raise ValueError("research task already has a persisted plan")
            row.original_language = plan.original_language
            row.status = ResearchStatus.PLANNED.value
            row.sub_questions = [
                SubQuestionRow(
                    id=str(item.id),
                    research_id=str(research_id),
                    ordinal=index,
                    objective=item.objective,
                    evidence_requirements=item.evidence_requirements,
                    target_languages=sorted(
                        {
                            query.language
                            for query in plan.queries
                            if query.sub_question_id == item.id
                        }
                    ),
                    target_sources=list(
                        dict.fromkeys(
                            source
                            for query in plan.queries
                            if query.sub_question_id == item.id
                            for source in query.sources
                        )
                    ),
                    completed=False,
                    unresolved_questions=[],
                )
                for index, item in enumerate(plan.sub_questions)
            ]
            session.add_all(
                QuestionDependencyRow(
                    sub_question_id=str(item.id), depends_on_id=str(dependency_id)
                )
                for item in plan.sub_questions
                for dependency_id in item.depends_on
            )
            session.add_all(
                QueryVariantRow(
                    id=str(query.id),
                    research_id=str(research_id),
                    sub_question_id=str(query.sub_question_id),
                    language=query.language,
                    query=query.query,
                    expansion_round=1,
                    canonical_terms=[],
                    sources=query.sources,
                    purpose=query.purpose,
                )
                for query in plan.queries
            )
            self._append_event(
                session,
                research_id,
                "SEARCH_PLAN_CREATED",
                {
                    "sub_questions_count": len(plan.sub_questions),
                    "queries_count": len(plan.queries),
                },
            )

    def persist_search_response(
        self, query_id: UUID, response: QuickSearchResponse
    ) -> None:
        with self.database.session_factory.begin() as session:
            query = session.get(QueryVariantRow, str(query_id))
            if query is None:
                raise LookupError(f"query variant not found: {query_id}")
            for rank, merged in enumerate(response.results, start=1):
                existing = session.scalar(
                    select(SearchResultRow).where(
                        SearchResultRow.canonical_key == merged.canonical_key
                    )
                )
                if existing is None:
                    result = merged.primary
                    existing = SearchResultRow(
                        id=str(result.id),
                        canonical_key=merged.canonical_key,
                        canonical_url=str(result.url),
                        source_id=result.source_id,
                        adapter_id=result.adapter_id,
                        channel=result.channel.value,
                        result_type=result.result_type,
                        title=result.title,
                        original_language=result.original_language,
                        snippet=result.snippet,
                        published_at=result.published_at,
                        retrieved_at=result.retrieved_at,
                        authors=result.authors,
                        external_identifiers=result.external_identifiers,
                        raw_payload={
                            "primary": result.raw_payload,
                            "occurrences": [
                                occurrence.model_dump(mode="json")
                                for occurrence in merged.occurrences
                            ],
                        },
                    )
                    session.add(existing)
                    session.flush()
                discovery = session.get(
                    QueryResultDiscoveryRow, (str(query_id), existing.id)
                )
                if discovery is None:
                    session.add(
                        QueryResultDiscoveryRow(
                            query_id=str(query_id), result_id=existing.id, rank=rank
                        )
                    )
            query.status = (
                "failed" if not response.results and response.failures else "completed"
            )
            query.result_count = len(response.results)
            query.failure_count = len(response.failures)
            query.completed_at = datetime.now(UTC)

    def mark_query_started(self, query_id: UUID) -> None:
        with self.database.session_factory.begin() as session:
            query = session.get(QueryVariantRow, str(query_id))
            if query is None:
                raise LookupError(f"query variant not found: {query_id}")
            query.status = "running"
            query.attempts += 1
            query.completed_at = None

    def pending_query_ids(self, research_id: UUID) -> set[UUID]:
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(QueryVariantRow.id).where(
                    QueryVariantRow.research_id == str(research_id),
                    QueryVariantRow.status != "completed",
                )
            ).all()
            return {UUID(identifier) for identifier in rows}

    def query_progress(self, research_id: UUID) -> dict[str, int]:
        with self.database.session_factory() as session:
            rows = session.execute(
                select(QueryVariantRow.status, func.count(QueryVariantRow.id))
                .where(QueryVariantRow.research_id == str(research_id))
                .group_by(QueryVariantRow.status)
            ).all()
            counts = {status: int(count) for status, count in rows}
            with_results = session.scalar(
                select(func.count(QueryVariantRow.id)).where(
                    QueryVariantRow.research_id == str(research_id),
                    QueryVariantRow.result_count > 0,
                )
            )
            return {
                "total": sum(counts.values()),
                "pending": counts.get("pending", 0),
                "running": counts.get("running", 0),
                "completed": counts.get("completed", 0),
                "failed": counts.get("failed", 0),
                "cancelled": counts.get("cancelled", 0),
                "with_results": int(with_results or 0),
            }

    def cancel_unfinished_queries(self, research_id: UUID) -> None:
        with self.database.session_factory.begin() as session:
            session.execute(
                update(QueryVariantRow)
                .where(
                    QueryVariantRow.research_id == str(research_id),
                    QueryVariantRow.status.in_({"pending", "running", "failed"}),
                )
                .values(status="cancelled", completed_at=datetime.now(UTC))
            )

    def reconcile_cancelled_queries(self) -> int:
        """Repair unfinished query markers left by a process exit during cancellation."""
        with self.database.session_factory.begin() as session:
            cancelled_research = select(ResearchTaskRow.id).where(
                ResearchTaskRow.status == ResearchStatus.CANCELLED.value
            )
            result = session.execute(
                update(QueryVariantRow)
                .where(
                    QueryVariantRow.research_id.in_(cancelled_research),
                    QueryVariantRow.status.in_({"pending", "running", "failed"}),
                )
                .values(status="cancelled", completed_at=datetime.now(UTC))
            )
            return int(result.rowcount or 0)

    def reset_running_queries(self, research_id: UUID) -> int:
        """Return interrupted in-flight queries to pending for a later continuation."""
        with self.database.session_factory.begin() as session:
            result = session.execute(
                update(QueryVariantRow)
                .where(
                    QueryVariantRow.research_id == str(research_id),
                    QueryVariantRow.status == "running",
                )
                .values(status="pending", completed_at=None)
            )
            return int(result.rowcount or 0)

    def covered_sub_question_ids(self, research_id: UUID) -> set[UUID]:
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(QueryVariantRow.sub_question_id)
                .join(
                    QueryResultDiscoveryRow,
                    QueryResultDiscoveryRow.query_id == QueryVariantRow.id,
                )
                .where(QueryVariantRow.research_id == str(research_id))
                .distinct()
            ).all()
            return {UUID(identifier) for identifier in rows}

    def update_current_round(self, research_id: UUID, round_number: int) -> None:
        with self.database.session_factory.begin() as session:
            row = session.get(ResearchTaskRow, str(research_id))
            if row is None:
                raise ResearchNotFound(str(research_id))
            row.current_round = round_number
            row.updated_at = datetime.now(UTC)

    def add_query_variant(
        self, research_id: UUID, query: PlannedQuery, expansion_round: int
    ) -> None:
        with self.database.session_factory.begin() as session:
            session.add(
                QueryVariantRow(
                    id=str(query.id),
                    research_id=str(research_id),
                    sub_question_id=str(query.sub_question_id),
                    language=query.language,
                    query=query.query,
                    expansion_round=expansion_round,
                    canonical_terms=[],
                    sources=query.sources,
                    purpose=query.purpose,
                )
            )

    def load_plan(self, research_id: UUID) -> ResearchPlan:
        task = self.get(research_id)
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(QueryVariantRow)
                .where(QueryVariantRow.research_id == str(research_id))
                .order_by(QueryVariantRow.expansion_round, QueryVariantRow.id)
            ).all()
        categories_by_id = {
            question.id: set()
            for question in task.sub_questions
        }
        return ResearchPlan(
            question=task.question,
            original_language=task.original_language,
            sub_questions=[
                PlannedSubQuestion(
                    id=question.id,
                    objective=question.objective,
                    categories=categories_by_id[question.id],
                    evidence_requirements=question.evidence_requirements,
                    depends_on=question.depends_on,
                )
                for question in task.sub_questions
            ],
            queries=[
                PlannedQuery(
                    id=UUID(row.id),
                    sub_question_id=UUID(row.sub_question_id),
                    language=row.language,
                    query=row.query,
                    sources=row.sources,
                    purpose=row.purpose,
                )
                for row in rows
            ],
            budget=task.budget,
        )

    def search_summary(self, research_id: UUID) -> dict:
        with self.database.session_factory() as session:
            query_count = session.scalar(
                select(func.count(QueryVariantRow.id)).where(
                    QueryVariantRow.research_id == str(research_id)
                )
            )
            result_count = session.scalar(
                select(func.count(func.distinct(QueryResultDiscoveryRow.result_id)))
                .join(QueryVariantRow, QueryVariantRow.id == QueryResultDiscoveryRow.query_id)
                .where(QueryVariantRow.research_id == str(research_id))
            )
            return {"queries": int(query_count or 0), "unique_results": int(result_count or 0)}

    def result_urls(self, research_id: UUID, limit: int) -> list[str]:
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(SearchResultRow)
                .join(
                    QueryResultDiscoveryRow,
                    QueryResultDiscoveryRow.result_id == SearchResultRow.id,
                )
                .join(QueryVariantRow, QueryVariantRow.id == QueryResultDiscoveryRow.query_id)
                .where(QueryVariantRow.research_id == str(research_id))
                .order_by(QueryResultDiscoveryRow.rank)
            ).all()
            return list(dict.fromkeys(row.canonical_url for row in rows))[:limit]

    def acquisition_candidates(self, research_id: UUID, limit: int) -> list[dict]:
        """Prefer original papers, code and patents that have not been fetched yet."""
        with self.database.session_factory() as session:
            acquired = set(
                session.scalars(
                    select(ResultDocumentRow.result_id).where(
                        ResultDocumentRow.research_id == str(research_id)
                    )
                ).all()
            )
            task = session.get(ResearchTaskRow, str(research_id))
            queries = session.scalars(
                select(QueryVariantRow.query).where(
                    QueryVariantRow.research_id == str(research_id)
                )
            ).all()
        research_text = " ".join([task.question if task else "", *queries])
        terms = self._relevance_terms(research_text)
        records = self.result_records(research_id, 10000)
        type_priority = {
            "paper": 0,
            "repository": 0,
            "patent": 0,
            "documentation": 1,
            "web": 2,
            "discussion": 3,
        }
        source_priority = {
            "arxiv": 0,
            "github": 0,
            "gitlab": 0,
            "google_patents": 0,
            "crossref": 1,
            "openalex": 1,
            "semantic_scholar": 1,
            "google_scholar": 1,
        }
        pending = [record for record in records if record["result_id"] not in acquired]

        pending.sort(
            key=lambda record: (
                -self._relevance_score(record, terms),
                type_priority.get(record["result_type"], 2),
                source_priority.get(record["source_id"], 2),
                record.get("best_rank") or 10000,
            )
        )
        for record in pending:
            record["relevance_score"] = self._relevance_score(record, terms)
        return pending[:limit]

    @staticmethod
    def _relevance_score(record: dict, terms: set[str]) -> int:
        text = f"{record['title']} {record.get('snippet') or ''}".lower()
        matched = {term for term in terms if term in text}
        return sum(min(len(term), 12) for term in matched) + len(matched) ** 2 * 8

    @staticmethod
    def _relevance_terms(text: str) -> set[str]:
        stopwords = {
            "about",
            "algorithm",
            "documentation",
            "github",
            "implementation",
            "official",
            "open",
            "paper",
            "research",
            "source",
            "technical",
            "technology",
            "using",
            "with",
        }
        lowered = text.lower()
        terms = {
            token
            for token in re.findall(r"[a-z][a-z0-9_.+-]{2,}", lowered)
            if token not in stopwords
        }
        cjk_stopwords = {"什么", "如何", "哪些", "使用", "相关", "实现", "技术"}
        for sequence in re.findall(r"[\u3400-\u9fff]{2,}", lowered):
            for size in (2, 3, 4):
                terms.update(
                    sequence[index : index + size]
                    for index in range(max(0, len(sequence) - size + 1))
                    if sequence[index : index + size] not in cjk_stopwords
                )
        return terms

    def result_records(self, research_id: UUID, limit: int) -> list[dict]:
        with self.database.session_factory() as session:
            rows = session.execute(
                select(SearchResultRow, QueryResultDiscoveryRow)
                .join(
                    QueryResultDiscoveryRow,
                    QueryResultDiscoveryRow.result_id == SearchResultRow.id,
                )
                .join(QueryVariantRow, QueryVariantRow.id == QueryResultDiscoveryRow.query_id)
                .where(QueryVariantRow.research_id == str(research_id))
                .order_by(QueryResultDiscoveryRow.rank)
            ).all()
            records: dict[str, dict] = {}
            for row, discovery in rows:
                record = records.setdefault(
                    row.id,
                    {
                        "result_id": row.id,
                        "canonical_key": row.canonical_key,
                        "url": row.canonical_url,
                        "title": row.title,
                        "snippet": row.snippet,
                        "source_id": row.source_id,
                        "adapter_id": row.adapter_id,
                        "channel": row.channel,
                        "result_type": row.result_type,
                        "original_language": row.original_language,
                        "published_at": (
                            row.published_at.isoformat() if row.published_at else None
                        ),
                        "authors": row.authors,
                        "external_identifiers": row.external_identifiers,
                        "best_rank": discovery.rank,
                        "query_ids": [],
                    },
                )
                if discovery.rank is not None:
                    current_rank = record["best_rank"]
                    record["best_rank"] = (
                        discovery.rank
                        if current_rank is None
                        else min(current_rank, discovery.rank)
                    )
                record["query_ids"].append(discovery.query_id)
            return list(records.values())[:limit]

    def record_event(self, research_id: UUID, event_type: str, payload: dict) -> None:
        with self.database.session_factory.begin() as session:
            if session.get(ResearchTaskRow, str(research_id)) is None:
                raise ResearchNotFound(str(research_id))
            self._append_event(session, research_id, event_type, payload)

    def update_sub_question_coverage(
        self, research_id: UUID, covered_ids: set[UUID]
    ) -> None:
        with self.database.session_factory.begin() as session:
            rows = session.scalars(
                select(SubQuestionRow).where(
                    SubQuestionRow.research_id == str(research_id)
                )
            ).all()
            for row in rows:
                row.completed = UUID(row.id) in covered_ids
                row.unresolved_questions = (
                    [] if row.completed else ["本轮未找到足够的独立有效结果"]
                )

    def coverage_matrix(self, research_id: UUID) -> list[dict]:
        task = self.get(research_id)
        return [
            {
                "sub_question_id": str(question.id),
                "objective": question.objective,
                "languages": question.target_languages,
                "sources": question.target_sources,
                "completed": question.completed,
                "unresolved_questions": question.unresolved_questions,
            }
            for question in task.sub_questions
        ]

    @staticmethod
    def _append_event(session, research_id: UUID, event_type: str, payload: dict) -> None:
        sequence = session.scalar(
            select(func.coalesce(func.max(ResearchEventRow.sequence), 0)).where(
                ResearchEventRow.research_id == str(research_id)
            )
        )
        session.add(
            SqlResearchRepository._event_to_row(
                ResearchEvent(
                    research_id=research_id,
                    event_type=event_type,
                    sequence=int(sequence) + 1,
                    payload=payload,
                )
            )
        )

    def events(self, research_id: UUID) -> list[ResearchEvent]:
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(ResearchEventRow)
                .where(ResearchEventRow.research_id == str(research_id))
                .order_by(ResearchEventRow.sequence)
            ).all()
            return [
                ResearchEvent(
                    id=UUID(row.id),
                    research_id=UUID(row.research_id),
                    event_type=row.event_type,
                    sequence=row.sequence,
                    occurred_at=row.occurred_at,
                    payload=row.payload,
                )
                for row in rows
            ]

    @staticmethod
    def _sub_question_to_row(item: SubQuestion) -> SubQuestionRow:
        return SubQuestionRow(
            id=str(item.id),
            research_id=str(item.research_id),
            ordinal=item.ordinal,
            objective=item.objective,
            evidence_requirements=item.evidence_requirements,
            target_languages=item.target_languages,
            target_sources=item.target_sources,
            completed=item.completed,
            unresolved_questions=item.unresolved_questions,
        )

    @staticmethod
    def _event_to_row(event: ResearchEvent) -> ResearchEventRow:
        return ResearchEventRow(
            id=str(event.id),
            research_id=str(event.research_id),
            event_type=event.event_type,
            sequence=event.sequence,
            occurred_at=event.occurred_at,
            payload=event.payload,
        )

    @staticmethod
    def _to_domain(
        row: ResearchTaskRow, dependencies: dict[str, list[UUID]]
    ) -> ResearchTask:
        return ResearchTask(
            id=UUID(row.id),
            question=row.question,
            goal=row.goal,
            original_language=row.original_language,
            status=ResearchStatus(row.status),
            budget=ResearchBudget.model_validate(row.budget),
            current_round=row.current_round,
            created_at=row.created_at,
            updated_at=row.updated_at,
            sub_questions=[
                SubQuestion(
                    id=UUID(item.id),
                    research_id=UUID(item.research_id),
                    ordinal=item.ordinal,
                    objective=item.objective,
                    evidence_requirements=item.evidence_requirements,
                    target_languages=item.target_languages,
                    target_sources=item.target_sources,
                    depends_on=dependencies[item.id],
                    completed=item.completed,
                    unresolved_questions=item.unresolved_questions,
                )
                for item in row.sub_questions
            ],
        )
