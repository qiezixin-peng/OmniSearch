import argparse
import asyncio
import os
import socket

from omnisearch.runtime import build_runtime
from omnisearch.workers import ResearchWorker


async def serve(poll_interval: float) -> None:
    runtime = build_runtime()
    worker = ResearchWorker(
        f"{socket.gethostname()}-{os.getpid()}",
        runtime.jobs,
        runtime.research,
        runtime.repository,
    )
    try:
        while True:
            worked = await worker.run_once()
            if not worked:
                await asyncio.sleep(poll_interval)
    finally:
        runtime.close()


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()
    asyncio.run(serve(args.poll_interval))


if __name__ == "__main__":
    run()
