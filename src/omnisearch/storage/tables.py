from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from omnisearch.storage.database import Base


class ResearchTaskRow(Base):
    __tablename__ = "research_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_language: Mapped[str] = mapped_column(String(35))
    status: Mapped[str] = mapped_column(String(20), index=True)
    budget: Mapped[dict] = mapped_column(JSON)
    current_round: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sub_questions: Mapped[list["SubQuestionRow"]] = relationship(
        back_populates="research", cascade="all, delete-orphan", order_by="SubQuestionRow.ordinal"
    )
    events: Mapped[list["ResearchEventRow"]] = relationship(
        back_populates="research",
        cascade="all, delete-orphan",
        order_by="ResearchEventRow.sequence",
    )


class SubQuestionRow(Base):
    __tablename__ = "sub_questions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    research_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    objective: Mapped[str] = mapped_column(Text)
    evidence_requirements: Mapped[list] = mapped_column(JSON, default=list)
    target_languages: Mapped[list] = mapped_column(JSON, default=list)
    target_sources: Mapped[list] = mapped_column(JSON, default=list)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    unresolved_questions: Mapped[list] = mapped_column(JSON, default=list)
    research: Mapped[ResearchTaskRow] = relationship(back_populates="sub_questions")

    __table_args__ = (UniqueConstraint("research_id", "ordinal"),)


class QuestionDependencyRow(Base):
    __tablename__ = "question_dependencies"

    sub_question_id: Mapped[str] = mapped_column(
        ForeignKey("sub_questions.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_id: Mapped[str] = mapped_column(
        ForeignKey("sub_questions.id", ondelete="CASCADE"), primary_key=True
    )


class ResearchEventRow(Base):
    __tablename__ = "research_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    research_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    research: Mapped[ResearchTaskRow] = relationship(back_populates="events")

    __table_args__ = (UniqueConstraint("research_id", "sequence"),)


class SearchSourceRow(Base):
    __tablename__ = "search_sources"

    source_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    category: Mapped[str] = mapped_column(String(30))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    terms_policy: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceAdapterRow(Base):
    __tablename__ = "source_adapters"

    adapter_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("search_sources.source_id"), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    capabilities: Mapped[dict] = mapped_column(JSON)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class QueryVariantRow(Base):
    __tablename__ = "query_variants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    research_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.id"), index=True)
    sub_question_id: Mapped[str] = mapped_column(ForeignKey("sub_questions.id"), index=True)
    language: Mapped[str] = mapped_column(String(35))
    query: Mapped[str] = mapped_column(Text)
    expansion_round: Mapped[int] = mapped_column(Integer, default=1)
    canonical_terms: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    purpose: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SearchResultRow(Base):
    __tablename__ = "search_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_key: Mapped[str] = mapped_column(Text, unique=True)
    canonical_url: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(String(80), index=True)
    adapter_id: Mapped[str] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(20))
    result_type: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(Text)
    original_language: Mapped[str | None] = mapped_column(String(35), nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    authors: Mapped[list] = mapped_column(JSON, default=list)
    external_identifiers: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)


class QueryResultDiscoveryRow(Base):
    __tablename__ = "query_result_discoveries"

    query_id: Mapped[str] = mapped_column(
        ForeignKey("query_variants.id"), primary_key=True
    )
    result_id: Mapped[str] = mapped_column(
        ForeignKey("search_results.id"), primary_key=True
    )
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentVersionRow(Base):
    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    final_url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    acquisition_mode: Mapped[str] = mapped_column(String(40))
    object_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    pages: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("document_id", "content_hash"),)


class DocumentChunkRow(Base):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)

    __table_args__ = (UniqueConstraint("version_id", "ordinal"),)


class ResultDocumentRow(Base):
    __tablename__ = "result_documents"

    research_id: Mapped[str] = mapped_column(
        ForeignKey("research_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    result_id: Mapped[str] = mapped_column(
        ForeignKey("search_results.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkerJobRow(Base):
    __tablename__ = "worker_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(60), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
