import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from omnisearch.fetchers.base import ContentFetcher, FetchPolicyError
from omnisearch.fetchers.models import FetchedDocument, FetchMode, FetchRequest


class EgoBrowserUnavailable(RuntimeError):
    pass


Runner = Callable[[Sequence[str]], Awaitable[tuple[int, str, str]]]


class EgoBrowserFetcher(ContentFetcher):
    """Bridge to Ego Lite's documented CLI, TaskSpace, and Page APIs."""

    _result_marker = "OMNISEARCH_EGO_RESULT:"

    def __init__(
        self,
        script_root: Path,
        *,
        executable: str = "ego-browser",
        runner: Runner | None = None,
    ) -> None:
        self.script_root = script_root.resolve()
        self.executable = executable
        self._runner = runner or self._run_process

    async def fetch(self, request: FetchRequest) -> FetchedDocument:
        if request.mode is not FetchMode.EGO_BROWSER:
            raise FetchPolicyError("EgoBrowserFetcher only accepts ego_browser mode")
        self.script_root.mkdir(parents=True, exist_ok=True)
        script_path = self.script_root / f"fetch-{uuid4().hex}.mjs"
        script_path.write_text(self._render_script(request), encoding="utf-8")
        try:
            entry = f"await import({json.dumps(script_path.as_uri())})"
            code, stdout, stderr = await self._runner(
                [self.executable, "nodejs", "-e", entry]
            )
        finally:
            script_path.unlink(missing_ok=True)
        if code != 0:
            raise EgoBrowserUnavailable(stderr.strip() or "ego-browser execution failed")
        # Ego Lite writes console output to stderr on Windows even on success.
        payload = self._parse_result(f"{stdout}\n{stderr}")
        return FetchedDocument(
            url=request.url,
            final_url=payload["finalUrl"],
            mode=request.mode,
            status_code=200,
            content_type=payload.get("contentType"),
            markdown=payload.get("text", ""),
            raw_html=payload.get("html"),
            metadata={
                "engine": "ego-browser",
                "task_space_id": payload.get("taskSpaceId"),
                "title": payload.get("title"),
            },
        )

    def _render_script(self, request: FetchRequest) -> str:
        space_id = json.dumps(request.ego_task_space_id)
        task_name = json.dumps(request.ego_task_name, ensure_ascii=False)
        url = json.dumps(str(request.url))
        timeout = request.timeout_ms
        wait_after_load = request.wait_after_load_ms
        marker = json.dumps(self._result_marker)
        return f"""const SPACE_ID = {space_id};
const entry = SPACE_ID === null
  ? undefined
  : (await listTaskSpaces()).find((item) => item.id === SPACE_ID);
const task = !entry
  ? await taskSpace({task_name})
  : entry.ownership === "user"
    ? await claimTaskSpace(SPACE_ID)
    : entry.ownership === "agentDelegatedToUser"
      ? await takeOverTaskSpace(SPACE_ID)
      : await taskSpace(SPACE_ID);
const pages = await task.pages();
const page = pages[0] ?? await task.adopt((await task.tabs())[0].page);
try {{
  await page.goto({url}, {{ timeout: {timeout} }});
  await page.waitForLoadState("domcontentloaded");
  if ({wait_after_load} > 0) await page.waitForTimeout({wait_after_load});
  const content = await page.evaluate(() => {{
    const root = document.querySelector("main, article") ?? document.body;
    return {{
      finalUrl: location.href,
      title: document.title,
      contentType: document.contentType,
      html: root?.innerHTML ?? "",
      text: root?.innerText ?? "",
    }};
  }});
  console.log({marker} + JSON.stringify({{ ...content, taskSpaceId: task.spaceId }}));
}} finally {{
  await task.finish({{ keep: [] }});
}}
"""

    def _parse_result(self, stdout: str) -> dict[str, Any]:
        for line in reversed(stdout.splitlines()):
            if line.startswith(self._result_marker):
                return json.loads(line[len(self._result_marker) :])
        raise EgoBrowserUnavailable("ego-browser returned no OmniSearch result payload")

    @staticmethod
    async def _run_process(args: Sequence[str]) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise EgoBrowserUnavailable("ego-browser executable was not found") from error
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
            raise
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
