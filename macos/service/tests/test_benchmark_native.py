from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_native.py"
SPEC = importlib.util.spec_from_file_location("benchmark_native", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
benchmark_native = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark_native
SPEC.loader.exec_module(benchmark_native)


def test_benchmark_summary_reports_percentiles_without_content() -> None:
    samples = [
        benchmark_native.Sample(200, 10, 20, 100, 5, 10, 125.0),
        benchmark_native.Sample(200, 12, 30, 200, 5, 10, 58.8),
        benchmark_native.Sample(
            500,
            5,
            None,
            25,
            None,
            None,
            None,
            "HTTPStatusError",
        ),
    ]

    result = benchmark_native.summarize(samples)

    assert result["requests"] == 3
    assert result["successful"] == 2
    assert result["errors"] == 1
    assert result["ttft_ms"] == {"average": 25.0, "p50": 20, "p95": 30}
    assert result["total_ms"] == {"average": 150.0, "p50": 100, "p95": 200}
    assert "prompt" not in result
    assert "response" not in result
