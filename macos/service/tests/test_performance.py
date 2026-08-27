from __future__ import annotations

from mnemosyne_macos.performance import PerformanceSample, PerformanceTracker


def _sample(index: int, *, cold: bool = False, error: str | None = None):
    return PerformanceSample(
        observed_at=float(index),
        alias="frontier",
        engine="omlx",
        endpoint="/v1/chat/completions",
        streamed=True,
        cold_start=cold,
        status_code=502 if error else 200,
        error_code=error,
        admission_ms=float(index),
        upstream_headers_ms=float(index + 1),
        first_byte_ms=float(index + 2),
        total_ms=float(index * 10),
        completion_tokens=index * 5,
        output_tokens_per_second=float(index),
    )


def test_performance_tracker_is_bounded_and_aggregates_metadata_only() -> None:
    tracker = PerformanceTracker(max_samples=3)
    for index in range(1, 5):
        tracker.record(
            _sample(
                index,
                cold=index == 2,
                error="upstream_transport" if index == 4 else None,
            )
        )

    snapshot = tracker.snapshot()
    assert snapshot["sample_count"] == 3
    assert snapshot["oldest_observed_at"] == 2.0
    assert snapshot["newest_observed_at"] == 4.0
    assert snapshot["by_model"] == [
        {
            "alias": "frontier",
            "engine": "omlx",
            "requests": 3,
            "errors": 1,
            "cold_starts": 1,
            "average_admission_ms": 3.0,
            "average_upstream_headers_ms": 4.0,
            "average_first_byte_ms": 5.0,
            "average_total_ms": 30.0,
            "average_output_tokens_per_second": 3.0,
            "p50_total_ms": 30.0,
            "p95_total_ms": 40.0,
        }
    ]
    assert set(snapshot["recent"][0]) == {
        "observed_at",
        "alias",
        "engine",
        "endpoint",
        "streamed",
        "cold_start",
        "status_code",
        "error_code",
        "admission_ms",
        "upstream_headers_ms",
        "first_byte_ms",
        "total_ms",
        "completion_tokens",
        "output_tokens_per_second",
    }


def test_request_timer_finishes_once() -> None:
    tracker = PerformanceTracker()
    timer = tracker.start(
        alias="frontier",
        engine="omlx",
        endpoint="/v1/responses",
        streamed=False,
    )
    timer.mark_admitted(cold_start=True)
    timer.mark_upstream_headers()
    timer.mark_first_byte()
    timer.finish(status_code=200)
    timer.finish(status_code=500, error_code="late_failure")

    recent = tracker.snapshot()["recent"]
    assert len(recent) == 1
    assert recent[0]["status_code"] == 200
    assert recent[0]["cold_start"] is True
