from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import or_, select, update

from omnisearch.domain.models import StrictModel
from omnisearch.storage.database import Database
from omnisearch.storage.tables import WorkerJobRow


class JobLease(StrictModel):
    id: UUID
    job_type: str
    payload: dict[str, Any]
    worker_id: str
    fencing_token: int = Field(ge=1)
    lease_expires_at: datetime
    attempts: int = Field(ge=1)


class StaleLeaseError(RuntimeError):
    pass


class JobQueue:
    def __init__(self, database: Database) -> None:
        self.database = database

    def enqueue(self, job_type: str, payload: dict[str, Any], idempotency_key: str) -> UUID:
        now = datetime.now(UTC)
        with self.database.session_factory.begin() as session:
            existing = session.scalar(
                select(WorkerJobRow).where(
                    WorkerJobRow.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                return UUID(existing.id)
            identifier = uuid4()
            session.add(
                WorkerJobRow(
                    id=str(identifier),
                    job_type=job_type,
                    payload=payload,
                    status="queued",
                    idempotency_key=idempotency_key,
                    fencing_token=0,
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            return identifier

    def claim(self, worker_id: str, lease_seconds: int = 60) -> JobLease | None:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        with self.database.session_factory.begin() as session:
            candidate = session.scalar(
                select(WorkerJobRow)
                .where(
                    or_(
                        WorkerJobRow.status == "queued",
                        (WorkerJobRow.status == "running")
                        & (WorkerJobRow.lease_expires_at < now),
                    )
                )
                .order_by(WorkerJobRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if candidate is None:
                return None
            candidate.status = "running"
            candidate.lease_owner = worker_id
            candidate.lease_expires_at = expires
            candidate.fencing_token += 1
            candidate.attempts += 1
            candidate.updated_at = now
            session.flush()
            return JobLease(
                id=UUID(candidate.id),
                job_type=candidate.job_type,
                payload=candidate.payload,
                worker_id=worker_id,
                fencing_token=candidate.fencing_token,
                lease_expires_at=expires,
                attempts=candidate.attempts,
            )

    def heartbeat(self, lease: JobLease, lease_seconds: int = 60) -> JobLease:
        expires = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        updated = self._fenced_update(
            lease,
            {"lease_expires_at": expires, "updated_at": datetime.now(UTC)},
        )
        if not updated:
            raise StaleLeaseError(str(lease.id))
        return lease.model_copy(update={"lease_expires_at": expires})

    def complete(self, lease: JobLease) -> None:
        if not self._fenced_update(
            lease,
            {
                "status": "completed",
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": datetime.now(UTC),
            },
        ):
            raise StaleLeaseError(str(lease.id))

    def fail(self, lease: JobLease, error: str, *, retry: bool = True) -> None:
        if not self._fenced_update(
            lease,
            {
                "status": "queued" if retry else "failed",
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": error[:4000],
                "updated_at": datetime.now(UTC),
            },
        ):
            raise StaleLeaseError(str(lease.id))

    def _fenced_update(self, lease: JobLease, values: dict[str, Any]) -> bool:
        with self.database.session_factory.begin() as session:
            result = session.execute(
                update(WorkerJobRow)
                .where(
                    WorkerJobRow.id == str(lease.id),
                    WorkerJobRow.status == "running",
                    WorkerJobRow.lease_owner == lease.worker_id,
                    WorkerJobRow.fencing_token == lease.fencing_token,
                )
                .values(**values)
            )
            return result.rowcount == 1
