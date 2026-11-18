from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import case, delete, desc, func, select

from omnisearch.storage.database import Database
from omnisearch.storage.tables import ProviderAttemptRow


class ProviderTelemetryRepository:
    """Durable provider-attempt journal shared by every Agent-facing process."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._writes = 0

    def record(self, event: dict[str, Any]) -> None:
        row = ProviderAttemptRow(id=str(uuid4()), **event)
        with self.database.session_factory.begin() as session:
            session.add(row)
            self._writes += 1
            if self._writes % 500 == 0:
                session.execute(
                    delete(ProviderAttemptRow).where(
                        ProviderAttemptRow.occurred_at
                        < datetime.now(UTC) - timedelta(days=90)
                    )
                )

    def report(self, *, hours: int = 24, limit: int = 100) -> dict[str, Any]:
        since = datetime.now(UTC) - timedelta(hours=hours)
        with self.database.session_factory() as session:
            source_rows = session.execute(
                select(
                    ProviderAttemptRow.source_id,
                    func.count().label("attempts"),
                    func.sum(
                        case((ProviderAttemptRow.outcome == "success", 1), else_=0)
                    ).label("successes"),
                    func.sum(
                        case((ProviderAttemptRow.outcome == "failure", 1), else_=0)
                    ).label("failures"),
                    func.sum(
                        case((ProviderAttemptRow.outcome == "circuit_skip", 1), else_=0)
                    ).label("circuit_skips"),
                    func.avg(ProviderAttemptRow.latency_ms).label("average_latency_ms"),
                )
                .where(ProviderAttemptRow.occurred_at >= since)
                .group_by(ProviderAttemptRow.source_id)
                .order_by(desc("failures"), desc("circuit_skips"))
            ).all()
            code_rows = session.execute(
                select(
                    ProviderAttemptRow.source_id,
                    ProviderAttemptRow.code,
                    func.count().label("count"),
                )
                .where(
                    ProviderAttemptRow.occurred_at >= since,
                    ProviderAttemptRow.outcome != "success",
                )
                .group_by(ProviderAttemptRow.source_id, ProviderAttemptRow.code)
                .order_by(desc("count"))
            ).all()
            recent = session.scalars(
                select(ProviderAttemptRow)
                .where(
                    ProviderAttemptRow.occurred_at >= since,
                    ProviderAttemptRow.outcome != "success",
                )
                .order_by(ProviderAttemptRow.occurred_at.desc())
                .limit(limit)
            ).all()
        return {
            "window_hours": hours,
            "sources": [
                {
                    "source_id": row.source_id,
                    "attempts": int(row.attempts or 0),
                    "successes": int(row.successes or 0),
                    "failed_attempts": int(row.failures or 0),
                    "circuit_skips": int(row.circuit_skips or 0),
                    "success_rate": round((row.successes or 0) / row.attempts, 4)
                    if row.attempts
                    else 0.0,
                    "average_latency_ms": round(float(row.average_latency_ms or 0), 1),
                }
                for row in source_rows
            ],
            "failure_summary": [
                {
                    "source_id": row.source_id,
                    "code": row.code or "UNKNOWN",
                    "count": int(row.count),
                }
                for row in code_rows
            ],
            "recent_events": [
                {
                    "occurred_at": row.occurred_at.isoformat(),
                    "source_id": row.source_id,
                    "adapter_id": row.adapter_id,
                    "channel": row.channel,
                    "outcome": row.outcome,
                    "code": row.code,
                    "message": row.message,
                    "retryable": row.retryable,
                    "latency_ms": row.latency_ms,
                    "query_hash": row.query_hash,
                    "process_id": row.process_id,
                }
                for row in recent
            ],
        }
