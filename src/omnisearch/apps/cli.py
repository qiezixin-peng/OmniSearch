import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from omnisearch.domain.models import ResearchBudget, ResearchCreate
from omnisearch.quality import load_suite, run_benchmark, save_report
from omnisearch.runtime import build_runtime
from omnisearch.services.acquisition import AcquisitionRequest
from omnisearch.services.research import run_with_runtime_budget
from omnisearch.services.search import QuickSearchCommand


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


async def execute(args: argparse.Namespace) -> None:
    runtime = build_runtime()
    try:
        if args.command == "search":
            response = await runtime.search.search(
                QuickSearchCommand(
                    query=args.query,
                    sources=args.sources,
                    language=args.language,
                    max_results_per_source=args.max_results,
                )
            )
            _print(response.model_dump(mode="json"))
        elif args.command == "research":
            task = runtime.coordinator.create(
                ResearchCreate(
                    question=args.question,
                    budget=ResearchBudget(
                        max_rounds=args.max_rounds,
                        max_pages_to_fetch=args.max_pages,
                    ),
                )
            )
            report = await run_with_runtime_budget(runtime.research, runtime.repository, task.id)
            _print(
                report.model_dump(mode="json")
                if report
                else {
                    "research_id": str(task.id),
                    "status": "partial",
                    "reason": "runtime_budget_exhausted",
                }
            )
        elif args.command == "fetch":
            document = await runtime.acquisition.fetch(AcquisitionRequest(url=args.url))
            stored = runtime.documents.store(document)
            _print(
                {
                    "document": document.model_dump(mode="json"),
                    "stored": stored.model_dump(mode="json"),
                }
            )
        elif args.command == "sources":
            _print([source.model_dump(mode="json") for source in runtime.sources.list()])
        elif args.command == "benchmark":
            report = await run_benchmark(runtime, load_suite(args.suite), args.modes)
            if args.output:
                save_report(report, args.output)
            _print(report.model_dump(mode="json"))
    finally:
        runtime.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="omnisearch")
    commands = root.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument(
        "--sources",
        nargs="+",
        default=["google", "duckduckgo", "github", "arxiv", "crossref", "openalex"],
    )
    search.add_argument("--language")
    search.add_argument("--max-results", type=int, default=10)
    research = commands.add_parser("research")
    research.add_argument("question")
    research.add_argument("--max-rounds", type=int, default=3)
    research.add_argument("--max-pages", type=int, default=30)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("url")
    commands.add_parser("sources")
    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("suite", type=Path)
    benchmark.add_argument("--output", type=Path)
    benchmark.add_argument(
        "--modes",
        nargs="+",
        choices=["single_source", "multi_source", "multilingual", "deep_research"],
    )
    return root


def run() -> None:
    asyncio.run(execute(parser().parse_args()))


if __name__ == "__main__":
    run()
