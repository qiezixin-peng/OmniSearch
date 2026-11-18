import argparse
import asyncio
import json
import os
import socket
import time
from contextlib import suppress
from pathlib import Path

from omnisearch.runtime import build_runtime
from omnisearch.workers import ResearchWorker


def _write_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "timestamp": time.time(),
            }
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


async def _heartbeat(path: Path, interval: float = 5.0) -> None:
    while True:
        await asyncio.to_thread(_write_heartbeat, path)
        await asyncio.sleep(interval)


async def serve(poll_interval: float, heartbeat_file: Path | None = None) -> None:
    runtime = build_runtime()
    worker = ResearchWorker(
        f"{socket.gethostname()}-{os.getpid()}",
        runtime.jobs,
        runtime.research,
        runtime.repository,
    )
    heartbeat_task = (
        asyncio.create_task(_heartbeat(heartbeat_file)) if heartbeat_file is not None else None
    )
    try:
        while True:
            worked = await worker.run_once()
            if not worked:
                await asyncio.sleep(poll_interval)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            heartbeat_file.unlink(missing_ok=True)
        runtime.close()


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--heartbeat-file", type=Path)
    args = parser.parse_args()
    asyncio.run(serve(args.poll_interval, args.heartbeat_file))


if __name__ == "__main__":
    run()
