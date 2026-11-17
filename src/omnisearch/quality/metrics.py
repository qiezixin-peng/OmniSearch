from collections import Counter

from pydantic import Field

from omnisearch.domain.models import StrictModel


class QualityEvaluation(StrictModel):
    precision_at_k: float = Field(ge=0, le=1)
    recall_at_k: float = Field(ge=0, le=1)
    source_diversity: int = Field(ge=0)
    language_diversity: int = Field(ge=0)
    duplicate_rate: float = Field(ge=0, le=1)
    primary_source_rate: float = Field(ge=0, le=1)
    citation_accuracy: float | None = Field(default=None, ge=0, le=1)


def evaluate_quality(
    retrieved_keys: list[str],
    relevant_keys: set[str],
    *,
    source_ids: list[str] | None = None,
    languages: list[str] | None = None,
    primary_flags: list[bool] | None = None,
    citation_checks: list[bool] | None = None,
    k: int = 10,
) -> QualityEvaluation:
    selected = retrieved_keys[:k]
    relevant_found = sum(key in relevant_keys for key in selected)
    unique_relevant_found = len(set(selected) & relevant_keys)
    precision = relevant_found / len(selected) if selected else 0.0
    recall = unique_relevant_found / len(relevant_keys) if relevant_keys else 1.0
    counts = Counter(retrieved_keys)
    duplicates = sum(count - 1 for count in counts.values())
    primary = (primary_flags or [])[:k]
    citations = citation_checks or []
    return QualityEvaluation(
        precision_at_k=precision,
        recall_at_k=min(1.0, recall),
        source_diversity=len(set((source_ids or [])[:k])),
        language_diversity=len(set((languages or [])[:k])),
        duplicate_rate=duplicates / len(retrieved_keys) if retrieved_keys else 0.0,
        primary_source_rate=sum(primary) / len(primary) if primary else 0.0,
        citation_accuracy=(sum(citations) / len(citations) if citations else None),
    )
