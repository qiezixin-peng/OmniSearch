import json

from omnisearch.apps.worker import _write_heartbeat


def test_worker_heartbeat_is_atomic_and_identifies_process(tmp_path) -> None:
    heartbeat = tmp_path / "worker.heartbeat.json"

    _write_heartbeat(heartbeat)

    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert payload["pid"] > 0
    assert payload["host"]
    assert payload["timestamp"] > 0
    assert list(tmp_path.glob("*.tmp")) == []
