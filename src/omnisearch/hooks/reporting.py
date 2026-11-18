import json
import re
import sys
from collections.abc import Iterable
from typing import Any

FAILURE_LIST_KEYS = {
    "failures",
    "errors",
    "issues",
    "retrieval_failures",
    "acquisition_failures",
    "acquisition_failure_details",
    "quality_issues",
    "source_failure_breakdown",
}
FAILURE_COUNT_KEYS = {
    "failure_count",
    "source_failures",
    "source_circuit_skips",
}
FAILED_STATUSES = {
    "access_denied",
    "circuit_open",
    "degraded",
    "error",
    "failed",
    "partial",
    "rate_limited",
    "timeout",
    "unavailable",
    "unhealthy",
}


def _short(value: Any, limit: int = 280) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _failure_item(value: Any) -> str:
    if not isinstance(value, dict):
        return _short(value)
    labels = []
    for key in ("source_id", "adapter_id", "channel", "code", "status", "count"):
        item = value.get(key)
        if item not in (None, "", 0, False):
            labels.append(f"{key}={_short(item, 80)}")
    message = value.get("message") or value.get("reason") or value.get("detail")
    if message:
        labels.append(f"message={_short(message, 160)}")
    return ", ".join(labels) if labels else _short(value)


def _items(value: Any) -> Iterable[Any]:
    if isinstance(value, list):
        return value
    return [value]


def collect_failures(value: Any, path: str = "result") -> list[str]:
    found: list[str] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(collect_failures(item, f"{path}[{index}]"))
        return found
    if not isinstance(value, dict):
        return found

    if value.get("isError") is True:
        found.append(f"{path}: MCP tool returned isError=true")
    if value.get("healthy") is False:
        identity = value.get("source_id") or value.get("adapter_id") or path
        status = value.get("status") or "unhealthy"
        found.append(f"{identity}: status={status}")
    status = value.get("status")
    if isinstance(status, str) and status.lower() in FAILED_STATUSES:
        identity = value.get("source_id") or value.get("adapter_id") or path
        found.append(f"{identity}: status={status}")
    if value.get("quality_gate_passed") is False or value.get("gate_passed") is False:
        found.append(f"{path}: quality gate failed")

    for key, child in value.items():
        child_path = f"{path}.{key}"
        if key in FAILURE_LIST_KEYS and child:
            for item in _items(child):
                found.append(f"{child_path}: {_failure_item(item)}")
            continue
        if key in FAILURE_COUNT_KEYS and isinstance(child, (int, float)) and child > 0:
            found.append(f"{child_path}: count={child}")
            continue
        if key == "error" and child:
            found.append(f"{child_path}: {_failure_item(child)}")
            continue
        found.extend(collect_failures(child, child_path))
    return found


def _decode_text(value: str) -> Any | None:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def response_payloads(response: Any) -> list[Any]:
    if isinstance(response, str):
        decoded = _decode_text(response)
        return [decoded] if decoded is not None else [response]
    if not isinstance(response, dict):
        return [response]

    structured = response.get("structuredContent")
    if structured is not None:
        # MCP often mirrors the same value into structuredContent, content and
        # a compatibility result field. Prefer the authoritative structured
        # payload so one provider failure is not reported three times.
        return [structured]
    payloads = []
    for block in response.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        decoded = _decode_text(block.get("text", ""))
        if decoded is not None:
            payloads.append(decoded)
    return payloads or [response]


def _failure_identity(value: str) -> tuple[str, ...]:
    labels = dict(
        re.findall(
            r"(source_id|adapter_id|channel|code|status)=([^,；]+)", value
        )
    )
    if labels:
        return tuple(labels.values())
    return (value,)


def build_report(event: dict[str, Any]) -> dict[str, Any] | None:
    tool_name = str(event.get("tool_name") or "")
    if not tool_name.startswith("mcp__omnisearch__"):
        return None
    failures: list[str] = []
    for payload in response_payloads(event.get("tool_response")):
        failures.extend(collect_failures(payload))
    unique_by_identity: dict[tuple[str, ...], str] = {}
    for failure in failures:
        unique_by_identity.setdefault(_failure_identity(failure), failure)
    unique = list(unique_by_identity.values())
    if not unique:
        return None

    visible = unique[:10]
    if len(unique) > len(visible):
        visible.append(f"另有 {len(unique) - len(visible)} 项异常")
    summary = "；".join(visible)
    instruction = (
        "OmniSearch 检测到搜索或采集路径异常。当前 Agent 必须在本轮对话中以简短的"
        "‘搜索状态’主动告知用户：失败的来源/通道、是否已回退、以及对覆盖范围或结论的影响；"
        "不得因为其他来源成功而省略。即使随后恢复，也要说明曾失败并已恢复。异常摘要："
        f"{summary}"
    )
    return {
        "systemMessage": f"OmniSearch 路径异常：{summary}",
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": instruction,
        },
    }


def main() -> None:
    try:
        event = json.load(sys.stdin)
        report = build_report(event)
    except Exception:
        # Reporting must never hide or replace a valid OmniSearch tool result.
        return
    if report is not None:
        # ASCII JSON avoids Windows console-codepage ambiguity; Codex decodes the escapes.
        json.dump(report, sys.stdout, ensure_ascii=True, separators=(",", ":"))


if __name__ == "__main__":
    main()
