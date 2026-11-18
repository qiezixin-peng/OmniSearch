import subprocess
import sys


def test_mcp_contract_keeps_heavy_runtime_lazy() -> None:
    script = (
        "import sys; import omnisearch.apps.mcp.server; "
        "print('omnisearch.runtime' in sys.modules); "
        "print('omnisearch.workers.research_worker' in sys.modules); "
        "print('omnisearch.services.acquisition' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.splitlines() == ["False", "False", "False"]


def test_quality_package_keeps_benchmark_runner_lazy() -> None:
    script = (
        "import sys; import omnisearch.quality; "
        "print('omnisearch.quality.benchmark' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "False"
