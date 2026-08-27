"""Secret-free, content-free inference performance telemetry."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import math
import time
from typing import Iterable


@dataclass(frozen=True)
class PerformanceSample:
    observed_at: float
    alias: str
    engine: str
    endpoint: str
    streamed: bool
    cold_start: bool
    status_code: int
    error_code: str | None
    admission_ms: float | None
    upstream_headers_ms: float | None
    first_byte_ms: float | None
    total_ms: float
    completion_tokens: int | None
    output_tokens_per_second: float | None


def _mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(sum(present) / len(present), 3) if present else None


def _percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return round(ordered[index], 3)


class RequestPerformanceTimer:
    def __init__(
        self,
        tracker: "PerformanceTracker",
        *,
        alias: str,
        engine: str,
        endpoint: str,
        streamed: bool,
    ) -> None:
        self._tracker = tracker
        self.alias = alias
        self.engine = engine
        self.endpoint = endpoint
        self.streamed = streamed
        self.started_at = time.time()
        self.started_monotonic = time.monotonic()
        self.cold_start = False
        self.admission_ms: float | None = None
        self.upstream_headers_ms: float | None = None
        self.first_byte_ms: float | None = None
        self._finished = False

    def _elapsed_ms(self) -> float:
        return max(0.0, (time.monotonic() - self.started_monotonic) * 1000)

    def mark_admitted(self, *, cold_start: bool) -> None:
        self.cold_start = cold_start
        self.admission_ms = self._elapsed_ms()

    def mark_upstream_headers(self) -> None:
        self.upstream_headers_ms = self._elapsed_ms()

    def mark_first_byte(self) -> None:
        if self.first_byte_ms is None:
            self.first_byte_ms = self._elapsed_ms()

    def finish(
        self,
        *,
        status_code: int,
        error_code: str | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        total_ms = self._elapsed_ms()
        decode_ms = (
            max(0.0, total_ms - self.first_byte_ms)
            if self.first_byte_ms is not None
            else None
        )
        output_tokens_per_second = (
            completion_tokens / (decode_ms / 1000)
            if completion_tokens is not None
            and completion_tokens >= 0
            and decode_ms is not None
            and decode_ms > 0
            and self.streamed
            else None
        )
        self._tracker.record(
            PerformanceSample(
                observed_at=self.started_at,
                alias=self.alias,
                engine=self.engine,
                endpoint=self.endpoint,
                streamed=self.streamed,
                cold_start=self.cold_start,
                status_code=status_code,
                error_code=error_code,
                admission_ms=(
                    round(self.admission_ms, 3)
                    if self.admission_ms is not None
                    else None
                ),
                upstream_headers_ms=(
                    round(self.upstream_headers_ms, 3)
                    if self.upstream_headers_ms is not None
                    else None
                ),
                first_byte_ms=(
                    round(self.first_byte_ms, 3)
                    if self.first_byte_ms is not None
                    else None
                ),
                total_ms=round(total_ms, 3),
                completion_tokens=completion_tokens,
                output_tokens_per_second=(
                    round(output_tokens_per_second, 3)
                    if output_tokens_per_second is not None
                    else None
                ),
            )
        )


class PerformanceTracker:
    """Bounded metadata-only performance window for status and tuning."""

    def __init__(self, *, max_samples: int = 512) -> None:
        if max_samples < 1:
            raise ValueError("max_samples must be positive")
        self.max_samples = max_samples
        self._samples: deque[PerformanceSample] = deque(maxlen=max_samples)

    def start(
        self,
        *,
        alias: str,
        engine: str,
        endpoint: str,
        streamed: bool,
    ) -> RequestPerformanceTimer:
        return RequestPerformanceTimer(
            self,
            alias=alias,
            engine=engine,
            endpoint=endpoint,
            streamed=streamed,
        )

    def record(self, sample: PerformanceSample) -> None:
        self._samples.append(sample)

    def snapshot(self) -> dict:
        samples = list(self._samples)
        groups: dict[tuple[str, str], list[PerformanceSample]] = {}
        for sample in samples:
            groups.setdefault((sample.alias, sample.engine), []).append(sample)

        by_model: list[dict] = []
        for (alias, engine), values in sorted(groups.items()):
            totals = [item.total_ms for item in values]
            by_model.append(
                {
                    "alias": alias,
                    "engine": engine,
                    "requests": len(values),
                    "errors": sum(item.error_code is not None for item in values),
                    "cold_starts": sum(item.cold_start for item in values),
                    "average_admission_ms": _mean(
                        item.admission_ms for item in values
                    ),
                    "average_upstream_headers_ms": _mean(
                        item.upstream_headers_ms for item in values
                    ),
                    "average_first_byte_ms": _mean(
                        item.first_byte_ms for item in values
                    ),
                    "average_total_ms": _mean(totals),
                    "average_output_tokens_per_second": _mean(
                        item.output_tokens_per_second for item in values
                    ),
                    "p50_total_ms": _percentile(totals, 0.50),
                    "p95_total_ms": _percentile(totals, 0.95),
                }
            )

        return {
            "window_limit": self.max_samples,
            "sample_count": len(samples),
            "oldest_observed_at": samples[0].observed_at if samples else None,
            "newest_observed_at": samples[-1].observed_at if samples else None,
            "by_model": by_model,
            "recent": [asdict(item) for item in samples[-20:]],
        }


__all__ = [
    "PerformanceSample",
    "PerformanceTracker",
    "RequestPerformanceTimer",
]
