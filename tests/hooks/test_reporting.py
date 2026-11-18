from omnisearch.hooks.reporting import build_report


def _event(payload: dict) -> dict:
    return {
        "tool_name": "mcp__omnisearch__search",
        "tool_response": {
            "isError": False,
            "content": [{"type": "text", "text": __import__("json").dumps(payload)}],
        },
    }


def test_successful_search_is_silent() -> None:
    assert build_report(_event({"results": [{"source_id": "google"}], "failures": []})) is None


def test_provider_failure_requires_user_visible_report() -> None:
    report = build_report(
        _event(
            {
                "results": [{"source_id": "github"}],
                "failures": [
                    {
                        "source_id": "google",
                        "adapter_id": "google_chrome",
                        "code": "TIMEOUT",
                        "message": "source exceeded latency budget",
                    }
                ],
            }
        )
    )

    assert report is not None
    assert "google" in report["systemMessage"]
    assert "TIMEOUT" in report["systemMessage"]
    assert "必须在本轮对话" in report["hookSpecificOutput"]["additionalContext"]


def test_partial_research_and_quality_failure_are_reported() -> None:
    report = build_report(
        _event(
            {
                "status": "partial",
                "quality": {
                    "gate_passed": False,
                    "issues": ["missing_channel_evidence:browser"],
                },
                "retrieval_failures": [
                    {"source_id": "duckduckgo", "code": "ACCESS_DENIED", "count": 2}
                ],
            }
        )
    )

    assert report is not None
    assert "partial" in report["systemMessage"]
    assert "quality gate failed" in report["systemMessage"]
    assert "duckduckgo" in report["systemMessage"]


def test_unrelated_tool_is_ignored() -> None:
    event = _event({"failures": [{"code": "TIMEOUT"}]})
    event["tool_name"] = "mcp__other__search"
    assert build_report(event) is None


def test_degraded_source_health_requires_user_visible_report() -> None:
    report = build_report(
        {
            "tool_name": "mcp__omnisearch__get_source_health",
            "tool_response": [
                {"source_id": "github", "status": "healthy"},
                {"source_id": "openalex", "status": "degraded"},
            ],
        }
    )

    assert report is not None
    assert "openalex" in report["systemMessage"]
    assert "degraded" in report["systemMessage"]
