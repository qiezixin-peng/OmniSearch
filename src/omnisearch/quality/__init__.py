"""Quality APIs with lazy benchmark imports to keep Agent startup small."""

from importlib import import_module
from typing import Any

from omnisearch.quality.metrics import QualityEvaluation, evaluate_quality

__all__ = [
    "BenchmarkCase",
    "BenchmarkModeResult",
    "BenchmarkReport",
    "BenchmarkSuite",
    "QualityEvaluation",
    "evaluate_quality",
    "load_suite",
    "run_benchmark",
    "save_report",
]

_BENCHMARK_EXPORTS = {
    "BenchmarkCase",
    "BenchmarkModeResult",
    "BenchmarkReport",
    "BenchmarkSuite",
    "load_suite",
    "run_benchmark",
    "save_report",
}


def __getattr__(name: str) -> Any:
    if name not in _BENCHMARK_EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module("omnisearch.quality.benchmark"), name)
    globals()[name] = value
    return value
