from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from omnisearch.domain.enums import Channel, ResearchStatus, SourceCategory, SourceRole


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ResearchBudget(StrictModel):
    max_rounds: int = Field(default=5, ge=1, le=20)
    max_sub_questions: int = Field(default=30, ge=1, le=200)
    max_queries: int = Field(default=120, ge=1, le=2000)
    max_languages: int = Field(default=6, ge=1, le=30)
    max_sources_per_query: int = Field(default=8, ge=1, le=50)
    max_results_per_source: int = Field(default=20, ge=1, le=100)
    max_pages_to_fetch: int = Field(default=200, ge=0, le=10000)
    max_total_runtime_minutes: int = Field(default=60, ge=1, le=10080)


class SubQuestionCreate(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    objective: str = Field(min_length=3, max_length=1000)
    evidence_requirements: list[str] = Field(default_factory=list)
    target_languages: list[str] = Field(default_factory=list)
    target_sources: list[str] = Field(default_factory=list)
    depends_on: list[UUID] = Field(default_factory=list)


class SubQuestion(SubQuestionCreate):
    research_id: UUID
    ordinal: int = Field(ge=0)
    completed: bool = False
    unresolved_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_self_dependency(self) -> "SubQuestion":
        if self.id in self.depends_on:
            raise ValueError("a sub-question cannot depend on itself")
        return self


class ResearchCreate(StrictModel):
    question: str = Field(min_length=3, max_length=10000)
    goal: str | None = Field(default=None, max_length=5000)
    original_language: str = Field(default="auto", min_length=2, max_length=35)
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    sub_questions: list[SubQuestionCreate] = Field(default_factory=list)


class ResearchTask(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    question: str
    goal: str | None = None
    original_language: str
    status: ResearchStatus = ResearchStatus.DRAFT
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    current_round: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    sub_questions: list[SubQuestion] = Field(default_factory=list)


class QueryVariant(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    research_id: UUID
    sub_question_id: UUID
    language: str = Field(min_length=2, max_length=35)
    query: str = Field(min_length=1, max_length=2000)
    expansion_round: int = Field(default=1, ge=1)
    canonical_terms: list[str] = Field(default_factory=list)


class ProviderCapabilities(StrictModel):
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    adapter_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    category: SourceCategory
    channel: Channel
    supported_languages: set[str] = Field(default_factory=set)
    search_capabilities: set[str] = Field(default_factory=set)
    supports_pagination: bool = False
    supports_date_filter: bool = False
    supports_domain_filter: bool = False
    max_concurrency: int = Field(default=1, ge=1, le=100)
    requires_credentials: bool = False
    backend: str | None = None
    terms_policy: str | None = None


class SourceDefinition(StrictModel):
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    display_name: str = Field(min_length=1, max_length=100)
    category: SourceCategory
    role: SourceRole
    priority: int = Field(default=1, ge=0, le=2)
    enabled_by_default: bool = True
    adapters: list[ProviderCapabilities] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_adapters(self) -> "SourceDefinition":
        adapter_ids = [adapter.adapter_id for adapter in self.adapters]
        if len(adapter_ids) != len(set(adapter_ids)):
            raise ValueError("adapter IDs must be unique within a source")
        if any(adapter.source_id != self.source_id for adapter in self.adapters):
            raise ValueError("every adapter must reference its containing source")
        if any(adapter.category != self.category for adapter in self.adapters):
            raise ValueError("every adapter must use its source category")
        return self


class SearchRequest(StrictModel):
    query_id: UUID
    query: str = Field(min_length=1, max_length=2000)
    language: str | None = None
    max_results: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    domain: str | None = None


class SearchResult(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    source_id: str
    adapter_id: str
    channel: Channel
    result_type: str
    title: str
    url: HttpUrl
    original_language: str | None = None
    snippet: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    authors: list[str] = Field(default_factory=list)
    external_identifiers: dict[str, str] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class ResearchEvent(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    research_id: UUID
    event_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    sequence: int = Field(ge=1)
    occurred_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
