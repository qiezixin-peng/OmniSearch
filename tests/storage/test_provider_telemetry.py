from datetime import UTC, datetime

from omnisearch.storage.database import Database
from omnisearch.storage.telemetry import ProviderTelemetryRepository


def _event(**overrides):
    event = {
        "occurred_at": datetime.now(UTC),
        "source_id": "openalex",
        "adapter_id": "openalex_api",
        "channel": "api",
        "outcome": "success",
        "code": None,
        "message": None,
        "retryable": False,
        "latency_ms": 125,
        "result_count": 3,
        "query_hash": "a" * 64,
        "query_language": "en",
        "process_id": 123,
    }
    event.update(overrides)
    return event


def test_provider_attempts_are_persisted_and_aggregated(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'telemetry.db').as_posix()}")
    database.create_schema()
    telemetry = ProviderTelemetryRepository(database)

    telemetry.record(_event())
    telemetry.record(
        _event(
            outcome="failure",
            code="RATE_LIMITED",
            message="upstream rate limited",
            retryable=True,
            result_count=0,
        )
    )
    telemetry.record(
        _event(
            outcome="circuit_skip",
            code="CIRCUIT_OPEN",
            message="temporarily paused",
            retryable=True,
            latency_ms=0,
            result_count=0,
        )
    )

    report = telemetry.report(hours=24)

    assert report["sources"] == [
        {
            "source_id": "openalex",
            "attempts": 3,
            "successes": 1,
            "recovered_attempts": 0,
            "failed_attempts": 1,
            "circuit_skips": 1,
            "primary_success_rate": 0.3333,
            "effective_completion_rate": 0.3333,
            "average_latency_ms": 83.3,
        }
    ]
    assert {item["code"] for item in report["failure_summary"]} == {
        "RATE_LIMITED",
        "CIRCUIT_OPEN",
    }
    assert len(report["recent_events"]) == 2
