import json

import pytest

from omnisearch.fetchers import EgoBrowserFetcher, FetchMode, FetchRequest


@pytest.mark.asyncio
async def test_ego_bridge_uses_documented_taskspace_protocol(tmp_path) -> None:
    captured = {}

    async def runner(args):
        captured["args"] = args
        marker = EgoBrowserFetcher._result_marker
        payload = {
            "taskSpaceId": 42,
            "finalUrl": "https://example.com/result",
            "title": "Result",
            "contentType": "text/html",
            "html": "<p>Evidence</p>",
            "text": "Evidence",
        }
        return 0, marker + json.dumps(payload), ""

    fetcher = EgoBrowserFetcher(tmp_path, runner=runner)
    document = await fetcher.fetch(
        FetchRequest(
            url="https://example.com",
            mode=FetchMode.EGO_BROWSER,
            ego_task_space_id=42,
        )
    )
    assert document.markdown == "Evidence"
    assert document.metadata["task_space_id"] == 42
    assert captured["args"][:3] == ["ego-browser", "nodejs", "-e"]
    assert not list(tmp_path.glob("*.mjs"))


@pytest.mark.asyncio
async def test_ego_bridge_rejects_other_modes(tmp_path) -> None:
    fetcher = EgoBrowserFetcher(tmp_path)
    with pytest.raises(ValueError, match="only accepts"):
        await fetcher.fetch(FetchRequest(url="https://example.com"))


@pytest.mark.asyncio
async def test_ego_bridge_accepts_windows_stderr_console_output(tmp_path) -> None:
    async def runner(args):
        marker = EgoBrowserFetcher._result_marker
        payload = {
            "taskSpaceId": 7,
            "finalUrl": "https://example.com",
            "text": "from stderr",
        }
        return 0, "", marker + json.dumps(payload)

    document = await EgoBrowserFetcher(tmp_path, runner=runner).fetch(
        FetchRequest(url="https://example.com", mode=FetchMode.EGO_BROWSER)
    )
    assert document.markdown == "from stderr"
