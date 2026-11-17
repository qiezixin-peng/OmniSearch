import pytest

from omnisearch.storage.database import Database
from omnisearch.workers.queue import JobQueue, StaleLeaseError


def make_queue(tmp_path) -> JobQueue:
    database = Database(f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    database.create_schema()
    return JobQueue(database)


def test_enqueue_is_idempotent(tmp_path) -> None:
    queue = make_queue(tmp_path)

    first = queue.enqueue("research.execute", {"research_id": "1"}, "research:1")
    second = queue.enqueue("research.execute", {"research_id": "1"}, "research:1")

    assert first == second


def test_expired_lease_is_reclaimed_and_old_fencing_token_is_rejected(tmp_path) -> None:
    queue = make_queue(tmp_path)
    queue.enqueue("research.execute", {"research_id": "1"}, "research:1")
    old = queue.claim("worker-old", lease_seconds=-1)
    assert old is not None

    current = queue.claim("worker-current", lease_seconds=60)

    assert current is not None
    assert current.id == old.id
    assert current.fencing_token == old.fencing_token + 1
    with pytest.raises(StaleLeaseError):
        queue.complete(old)
    queue.complete(current)
    assert queue.claim("another-worker") is None
