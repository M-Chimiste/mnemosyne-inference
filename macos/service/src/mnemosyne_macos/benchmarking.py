"""Durable, content-free cross-engine benchmarks and selection policy."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sqlite3
import statistics
import time
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from .config import ModelSelectionConfig
from .coordinator import ModelLease, ResidencyCoordinator
from .engines.base import Deadline, EngineAdapter
from .models import ENGINE_RELEASE_TIER, Endpoint, EngineName, ResolvedTarget, TargetKey
from .usage import normalize_usage


BENCHMARK_SUITE_VERSION = 1
CONTEXT_SUITE_VERSION = 1
BENCHMARK_ENDPOINT = Endpoint.CHAT_COMPLETIONS
MAX_RECORDS_PER_ALIAS = 64
MAX_RECORDS_TOTAL = 2048


@lru_cache(maxsize=1)
def system_fingerprint() -> str:
    """Identify the relevant local hardware/OS class without a hostname."""

    material = json.dumps(
        {
            "machine": platform.machine(),
            "macos": platform.mac_ver()[0],
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def target_fingerprint(target: ResolvedTarget) -> str:
    """Hash model identity so local paths never enter benchmark rows."""

    material = json.dumps(
        {
            "engine": target.key.engine.value,
            "model": target.key.canonical_model_id,
            "load": target.key.load_config_digest,
            "capabilities": sorted(item.value for item in target.capabilities),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def context_target_fingerprint(target: ResolvedTarget) -> str:
    """Hash model/load identity while excluding the context value being tested."""

    load = dict(target.load_options)
    load.pop("context_length", None)
    material = json.dumps(
        {
            "engine": target.key.engine.value,
            "model": target.key.canonical_model_id,
            "load_without_context": load,
            "native_context_length": target.native_context_length,
            "capabilities": sorted(item.value for item in target.capabilities),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def target_with_context_window(target: ResolvedTarget, tokens: int) -> ResolvedTarget:
    """Return an exact candidate whose resident/load identity owns ``tokens``."""

    load = dict(target.load_options)
    if target.key.engine in {
        EngineName.LLAMA_CPP,
        EngineName.DS4,
        EngineName.MLXCEL,
    }:
        load["context_length"] = tokens
    material = json.dumps(
        {
            "load": load,
            "context_mode": target.context_mode,
            "requested_context": tokens,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return replace(
        target,
        key=TargetKey(
            engine=target.key.engine,
            canonical_model_id=target.key.canonical_model_id,
            load_config_digest=digest,
        ),
        load_options=load,
        requested_context_length=tokens,
    )


def candidate_set_fingerprint(candidates: Sequence[ResolvedTarget]) -> str:
    """Fingerprint only configuration that can affect this comparison.

    Unrelated credential, reporting, UI, or other-model edits should not force
    an expensive benchmark rerun. Candidate order is significant because the
    first target is the explicit fallback baseline.
    """

    material = json.dumps(
        {
            "endpoint": BENCHMARK_ENDPOINT.value,
            "candidates": [target_fingerprint(item) for item in candidates],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BenchmarkRecord:
    created_at: float
    alias: str
    endpoint: str
    engine: str
    target_fingerprint: str
    runtime_fingerprint: str
    system_fingerprint: str
    config_revision: str
    suite_version: int
    successful_samples: int
    failed_samples: int
    p50_ttft_ms: float | None
    p50_total_ms: float | None
    p50_output_tps: float | None

    @property
    def success_rate(self) -> float:
        total = self.successful_samples + self.failed_samples
        return self.successful_samples / total if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["success_rate"] = self.success_rate
        return value


@dataclass(frozen=True, slots=True)
class ContextBenchmarkRecord:
    created_at: float
    alias: str
    engine: str
    target_fingerprint: str
    runtime_fingerprint: str
    system_fingerprint: str
    suite_version: int
    requested_tokens: int
    verified_tokens: int
    prompt_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BenchmarkDecision:
    alias: str
    mode: str
    selected_engine: str
    selected_target_fingerprint: str
    fallback_engine: str
    reason: str
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Sample:
    ttft_ms: float
    total_ms: float
    output_tps: float | None


class BenchmarkStore:
    """Small SQLite store sharing the native state database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS native_engine_benchmarks (
                    created_at REAL NOT NULL,
                    alias TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    target_fingerprint TEXT NOT NULL,
                    runtime_fingerprint TEXT NOT NULL,
                    system_fingerprint TEXT NOT NULL,
                    config_revision TEXT NOT NULL,
                    suite_version INTEGER NOT NULL,
                    successful_samples INTEGER NOT NULL,
                    failed_samples INTEGER NOT NULL,
                    p50_ttft_ms REAL,
                    p50_total_ms REAL,
                    p50_output_tps REAL,
                    PRIMARY KEY (
                        alias, endpoint, engine, target_fingerprint,
                        runtime_fingerprint, system_fingerprint,
                        config_revision, suite_version
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS native_context_benchmarks (
                    created_at REAL NOT NULL,
                    alias TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    target_fingerprint TEXT NOT NULL,
                    runtime_fingerprint TEXT NOT NULL,
                    system_fingerprint TEXT NOT NULL,
                    suite_version INTEGER NOT NULL,
                    requested_tokens INTEGER NOT NULL,
                    verified_tokens INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    PRIMARY KEY (
                        alias, engine, target_fingerprint,
                        runtime_fingerprint, system_fingerprint, suite_version
                    )
                )
                """
            )

    def record(self, value: BenchmarkRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO native_engine_benchmarks (
                    created_at, alias, endpoint, engine, target_fingerprint,
                    runtime_fingerprint, system_fingerprint, config_revision,
                    suite_version, successful_samples, failed_samples,
                    p50_ttft_ms, p50_total_ms, p50_output_tps
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    alias, endpoint, engine, target_fingerprint,
                    runtime_fingerprint, system_fingerprint,
                    config_revision, suite_version
                ) DO UPDATE SET
                    created_at=excluded.created_at,
                    successful_samples=excluded.successful_samples,
                    failed_samples=excluded.failed_samples,
                    p50_ttft_ms=excluded.p50_ttft_ms,
                    p50_total_ms=excluded.p50_total_ms,
                    p50_output_tps=excluded.p50_output_tps
                """,
                (
                    value.created_at,
                    value.alias,
                    value.endpoint,
                    value.engine,
                    value.target_fingerprint,
                    value.runtime_fingerprint,
                    value.system_fingerprint,
                    value.config_revision,
                    value.suite_version,
                    value.successful_samples,
                    value.failed_samples,
                    value.p50_ttft_ms,
                    value.p50_total_ms,
                    value.p50_output_tps,
                ),
            )
            # Runtime and candidate changes intentionally create new evidence
            # keys. Keep that history useful but bounded so an upgrade-heavy
            # workstation cannot grow this metadata table indefinitely.
            connection.execute(
                """
                DELETE FROM native_engine_benchmarks
                WHERE alias = ? AND rowid NOT IN (
                    SELECT rowid FROM native_engine_benchmarks
                    WHERE alias = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                )
                """,
                (value.alias, value.alias, MAX_RECORDS_PER_ALIAS),
            )
            connection.execute(
                """
                DELETE FROM native_engine_benchmarks
                WHERE rowid NOT IN (
                    SELECT rowid FROM native_engine_benchmarks
                    ORDER BY created_at DESC
                    LIMIT ?
                )
                """,
                (MAX_RECORDS_TOTAL,),
            )

    def list(self, *, alias: str | None = None, limit: int = 200) -> list[BenchmarkRecord]:
        where = "WHERE alias = ?" if alias is not None else ""
        parameters: tuple[Any, ...] = (alias, limit) if alias is not None else (limit,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT created_at, alias, endpoint, engine, target_fingerprint,
                       runtime_fingerprint, system_fingerprint, config_revision,
                       suite_version, successful_samples, failed_samples,
                       p50_ttft_ms, p50_total_ms, p50_output_tps
                FROM native_engine_benchmarks
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [BenchmarkRecord(**dict(row)) for row in rows]

    def clear_alias(self, alias: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM native_engine_benchmarks WHERE alias = ?",
                (alias,),
            )
            return max(0, cursor.rowcount)

    def record_context(self, value: ContextBenchmarkRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO native_context_benchmarks (
                    created_at, alias, engine, target_fingerprint,
                    runtime_fingerprint, system_fingerprint, suite_version,
                    requested_tokens, verified_tokens, prompt_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    alias, engine, target_fingerprint,
                    runtime_fingerprint, system_fingerprint, suite_version
                ) DO UPDATE SET
                    created_at=excluded.created_at,
                    requested_tokens=excluded.requested_tokens,
                    verified_tokens=excluded.verified_tokens,
                    prompt_tokens=excluded.prompt_tokens
                """,
                (
                    value.created_at,
                    value.alias,
                    value.engine,
                    value.target_fingerprint,
                    value.runtime_fingerprint,
                    value.system_fingerprint,
                    value.suite_version,
                    value.requested_tokens,
                    value.verified_tokens,
                    value.prompt_tokens,
                ),
            )
            connection.execute(
                """
                DELETE FROM native_context_benchmarks
                WHERE alias = ? AND rowid NOT IN (
                    SELECT rowid FROM native_context_benchmarks
                    WHERE alias = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                )
                """,
                (value.alias, value.alias, MAX_RECORDS_PER_ALIAS),
            )
            connection.execute(
                """
                DELETE FROM native_context_benchmarks
                WHERE rowid NOT IN (
                    SELECT rowid FROM native_context_benchmarks
                    ORDER BY created_at DESC
                    LIMIT ?
                )
                """,
                (MAX_RECORDS_TOTAL,),
            )

    def list_context(
        self, *, alias: str | None = None, limit: int = 200
    ) -> list[ContextBenchmarkRecord]:
        where = "WHERE alias = ?" if alias is not None else ""
        parameters: tuple[Any, ...] = (alias, limit) if alias is not None else (limit,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT created_at, alias, engine, target_fingerprint,
                       runtime_fingerprint, system_fingerprint, suite_version,
                       requested_tokens, verified_tokens, prompt_tokens
                FROM native_context_benchmarks
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [ContextBenchmarkRecord(**dict(row)) for row in rows]

    def clear_context_alias(self, alias: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM native_context_benchmarks WHERE alias = ?",
                (alias,),
            )
            return max(0, cursor.rowcount)


def _first_output_delta(payload: Mapping[str, Any]) -> bool:
    event_type = payload.get("type")
    if event_type == "response.output_text.delta":
        return bool(payload.get("delta"))
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            delta = choice.get("delta")
            if isinstance(delta, Mapping) and delta.get("content"):
                return True
            if choice.get("text"):
                return True
    return False


async def _stream_sample(
    *,
    client: httpx.AsyncClient,
    lease: ModelLease,
    max_tokens: int,
) -> _Sample:
    response: httpx.Response | None = None
    started = time.monotonic()
    first_output_at: float | None = None
    completion_tokens: int | None = None
    try:
        route = lease.route(BENCHMARK_ENDPOINT)
        request = client.build_request(
            "POST",
            f"{route.base_url}{route.path}",
            headers={"content-type": "application/json", **dict(route.headers)},
            json={
                "model": route.wire_model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Give a concise explanation of why deterministic "
                            "benchmarking needs both latency and throughput."
                        ),
                    }
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        response = await client.send(request, stream=True)
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            now = time.monotonic()
            if first_output_at is None and _first_output_delta(payload):
                first_output_at = now
            usage = normalize_usage(
                payload,
                endpoint=f"/v1/{BENCHMARK_ENDPOINT.value}",
            )
            if usage is not None:
                completion_tokens = usage.completion_tokens
        finished = time.monotonic()
        if first_output_at is None:
            raise RuntimeError("benchmark stream contained no output delta")
        decode_seconds = max(finished - first_output_at, 0.001)
        output_tps = (
            completion_tokens / decode_seconds
            if completion_tokens is not None and completion_tokens > 0
            else None
        )
        return _Sample(
            ttft_ms=(first_output_at - started) * 1000,
            total_ms=(finished - started) * 1000,
            output_tps=output_tps,
        )
    finally:
        if response is not None:
            await response.aclose()


async def _context_probe(
    *,
    client: httpx.AsyncClient,
    lease: ModelLease,
    target_tokens: int,
) -> int:
    """Verify a large local prefill and return the engine-reported prompt size."""

    route = lease.route(BENCHMARK_ENDPOINT)
    # ``x `` is one token on the common tokenizer families, but the usage
    # block remains authoritative. Retry with a bounded correction when a
    # tokenizer encodes it differently. The synthetic text is never stored.
    repeats = max(1, target_tokens - 512)
    minimum = max(1, target_tokens - max(2048, target_tokens // 50))
    for _ in range(3):
        response = await client.post(
            f"{route.base_url}{route.path}",
            headers={"content-type": "application/json", **dict(route.headers)},
            json={
                "model": route.wire_model,
                "messages": [{"role": "user", "content": "x " * repeats}],
                "temperature": 0,
                "max_tokens": 1,
                "stream": False,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("context probe returned a non-object response")
        usage = normalize_usage(
            payload,
            endpoint=f"/v1/{BENCHMARK_ENDPOINT.value}",
        )
        if usage is None or usage.prompt_tokens is None:
            raise RuntimeError("context probe response omitted prompt token usage")
        prompt_tokens = usage.prompt_tokens
        if prompt_tokens >= minimum:
            return prompt_tokens
        repeats = min(
            target_tokens * 4,
            max(repeats + 1, math.ceil(repeats * target_tokens / prompt_tokens)),
        )
    raise RuntimeError("context probe could not reach the requested token window")


class BenchmarkSuite:
    """Run exact candidates sequentially through coordinator-owned leases."""

    def __init__(
        self,
        store: BenchmarkStore,
        *,
        coordinator: ResidencyCoordinator,
        client: httpx.AsyncClient,
    ) -> None:
        self.store = store
        self.coordinator = coordinator
        self.client = client

    async def run_candidate(
        self,
        target: ResolvedTarget,
        *,
        adapter: EngineAdapter,
        config_revision: str,
        warmup_runs: int,
        sample_runs: int,
        max_tokens: int,
    ) -> BenchmarkRecord:
        runtime = await adapter.runtime_fingerprint(deadline=Deadline.after(5))
        if runtime is None:
            raise RuntimeError(
                f"{target.key.engine.value} does not expose a stable runtime identity"
            )
        samples: list[_Sample] = []
        failures = 0
        warmup_failed = False
        lease = await self.coordinator.acquire(target)
        try:
            # One lease spans the whole candidate run. A different model
            # cannot evict it between samples and turn a warm measurement into
            # an accidental cold-load comparison.
            for _ in range(warmup_runs):
                try:
                    await _stream_sample(
                        client=self.client,
                        lease=lease,
                        max_tokens=max_tokens,
                    )
                except Exception:
                    warmup_failed = True
                    failures = sample_runs
                    break
            if not warmup_failed:
                for _ in range(sample_runs):
                    try:
                        samples.append(
                            await _stream_sample(
                                client=self.client,
                                lease=lease,
                                max_tokens=max_tokens,
                            )
                        )
                    except Exception:
                        # Persist only a fixed counter. Arbitrary engine
                        # diagnostics, prompts, and generated content never
                        # enter benchmark state.
                        failures += 1
        finally:
            await lease.release()
        throughput = [item.output_tps for item in samples if item.output_tps is not None]
        record = BenchmarkRecord(
            created_at=time.time(),
            alias=target.alias,
            endpoint=BENCHMARK_ENDPOINT.value,
            engine=target.key.engine.value,
            target_fingerprint=target_fingerprint(target),
            runtime_fingerprint=runtime,
            system_fingerprint=system_fingerprint(),
            config_revision=config_revision,
            suite_version=BENCHMARK_SUITE_VERSION,
            successful_samples=len(samples),
            failed_samples=failures,
            p50_ttft_ms=(statistics.median(item.ttft_ms for item in samples) if samples else None),
            p50_total_ms=(statistics.median(item.total_ms for item in samples) if samples else None),
            p50_output_tps=(statistics.median(throughput) if throughput else None),
        )
        self.store.record(record)
        return record

    async def profile_context_candidate(
        self,
        target: ResolvedTarget,
        *,
        adapter: EngineAdapter,
        requested_tokens: int,
    ) -> ContextBenchmarkRecord:
        """Find the largest preset up to ``requested_tokens`` that really prefills."""

        runtime = await adapter.runtime_fingerprint(deadline=Deadline.after(5))
        if runtime is None:
            raise RuntimeError(
                f"{target.key.engine.value} does not expose a stable runtime identity"
            )
        initial_hint = None
        try:
            initial_hint = await adapter.context_window(
                target,
                deadline=Deadline.after(5),
            )
        except Exception:
            pass
        presets = {
            16_384,
            32_768,
            65_536,
            131_072,
            262_144,
            524_288,
            1_048_576,
            requested_tokens,
        }
        native = target.native_context_length
        ceiling = min(requested_tokens, native) if native is not None else requested_tokens
        levels = sorted((value for value in presets if value <= ceiling), reverse=True)
        if not levels:
            levels = [ceiling]

        failure: Exception | None = None
        for level in levels:
            candidate = target_with_context_window(target, level)
            lease: ModelLease | None = None
            try:
                lease = await self.coordinator.acquire(candidate)
                prompt_tokens = await _context_probe(
                    client=self.client,
                    lease=lease,
                    target_tokens=level,
                )
                verified = max(1, min(level, (prompt_tokens // 1024) * 1024))
                record = ContextBenchmarkRecord(
                    created_at=time.time(),
                    alias=target.alias,
                    engine=target.key.engine.value,
                    target_fingerprint=context_target_fingerprint(target),
                    runtime_fingerprint=runtime,
                    system_fingerprint=system_fingerprint(),
                    suite_version=CONTEXT_SUITE_VERSION,
                    requested_tokens=requested_tokens,
                    verified_tokens=verified,
                    prompt_tokens=prompt_tokens,
                )
                self.store.record_context(record)
                return record
            except Exception as exc:
                failure = exc
            finally:
                if lease is not None:
                    await lease.release()

        # An oMLX trial changes its explicit per-model setting. Restore the
        # previously observed value when none of the probes succeeded.
        restore = getattr(adapter, "set_context_window", None)
        if (
            restore is not None
            and initial_hint is not None
            and initial_hint.effective_tokens is not None
        ):
            try:
                await restore(
                    target,
                    initial_hint.effective_tokens,
                    deadline=Deadline.after(10),
                )
            except Exception:
                pass
        raise RuntimeError("candidate could not verify a usable context window") from failure


def choose_target(
    *,
    alias: str,
    candidates: Sequence[ResolvedTarget],
    policy: ModelSelectionConfig,
    records: Sequence[BenchmarkRecord],
    runtime_fingerprints: Mapping[EngineName, str | None],
    config_revision: str,
    context_limits: Mapping[str, int | None] | None = None,
    required_context_tokens: int | None = None,
    now: float | None = None,
) -> tuple[ResolvedTarget, BenchmarkDecision]:
    """Select only from fresh exact evidence, retaining the primary fallback."""

    if not candidates:
        raise KeyError(f"unknown model alias '{alias}'")
    primary = candidates[0]
    fixed = BenchmarkDecision(
        alias=alias,
        mode=policy.mode,
        selected_engine=primary.key.engine.value,
        selected_target_fingerprint=target_fingerprint(primary),
        fallback_engine=primary.key.engine.value,
        reason=(
            "fixed profile"
            if policy.mode == "fixed"
            else "pinned engine unavailable; using fixed fallback"
            if policy.mode == "pinned"
            else "benchmark evidence unavailable"
        ),
    )
    if policy.mode == "pinned":
        pinned = next(
            (
                candidate
                for candidate in candidates
                if candidate.key.engine == policy.pinned_engine
            ),
            None,
        )
        if pinned is None:
            return primary, fixed
        return pinned, BenchmarkDecision(
            alias=alias,
            mode=policy.mode,
            selected_engine=pinned.key.engine.value,
            selected_target_fingerprint=target_fingerprint(pinned),
            fallback_engine=primary.key.engine.value,
            reason="user-pinned engine",
        )
    if policy.mode != "benchmark":
        return primary, fixed

    timestamp = time.time() if now is None else now
    cutoff = timestamp - policy.max_benchmark_age_hours * 3600
    machine = system_fingerprint()
    eligible: dict[str, tuple[ResolvedTarget, BenchmarkRecord]] = {}
    for candidate in candidates:
        if (
            candidate is not primary
            and ENGINE_RELEASE_TIER[candidate.key.engine] == "preview"
            and not policy.allow_preview
        ):
            continue
        runtime = runtime_fingerprints.get(candidate.key.engine)
        if runtime is None:
            continue
        fingerprint = target_fingerprint(candidate)
        if required_context_tokens is not None:
            candidate_context = (
                context_limits.get(fingerprint)
                if context_limits is not None
                else candidate.requested_context_length
            )
            if candidate_context is None or candidate_context < required_context_tokens:
                continue
        match = next(
            (
                record
                for record in records
                if record.alias == alias
                and record.endpoint == BENCHMARK_ENDPOINT.value
                and record.engine == candidate.key.engine.value
                and record.target_fingerprint == fingerprint
                and record.runtime_fingerprint == runtime
                and record.system_fingerprint == machine
                and record.config_revision == config_revision
                and record.suite_version == BENCHMARK_SUITE_VERSION
                and record.created_at >= cutoff
                and record.successful_samples >= policy.minimum_samples
                and record.success_rate >= 0.95
                and record.p50_ttft_ms is not None
            ),
            None,
        )
        if match is not None:
            eligible[fingerprint] = (candidate, match)

    primary_pair = eligible.get(target_fingerprint(primary))
    if primary_pair is None or len(eligible) < 2:
        return primary, fixed
    pairs = list(eligible.values())
    if policy.objective == "latency":
        scored = [
            (1.0 / max(record.p50_ttft_ms or math.inf, 0.001), candidate, record)
            for candidate, record in pairs
        ]
    elif policy.objective == "throughput":
        scored = [
            (record.p50_output_tps or 0.0, candidate, record)
            for candidate, record in pairs
        ]
    else:
        max_tps = max((record.p50_output_tps or 0.0) for _, record in pairs)
        min_ttft = min(record.p50_ttft_ms or math.inf for _, record in pairs)
        scored = [
            (
                0.5 * (min_ttft / max(record.p50_ttft_ms or math.inf, 0.001))
                + 0.5 * ((record.p50_output_tps or 0.0) / max(max_tps, 0.001)),
                candidate,
                record,
            )
            for candidate, record in pairs
        ]
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best, _best_record = scored[0]
    primary_score = next(
        score for score, candidate, _record in scored if candidate is primary_pair[0]
    )
    improvement = (
        ((best_score - primary_score) / primary_score) * 100
        if primary_score > 0
        else 100.0
    )
    if best is primary or improvement < policy.minimum_improvement_percent:
        return primary, BenchmarkDecision(
            **{
                **fixed.to_dict(),
                "reason": "primary remains within the benchmark improvement threshold",
                "score": primary_score,
            }
        )
    return best, BenchmarkDecision(
        alias=alias,
        mode=policy.mode,
        selected_engine=best.key.engine.value,
        selected_target_fingerprint=target_fingerprint(best),
        fallback_engine=primary.key.engine.value,
        reason=f"fresh {policy.objective} benchmark winner",
        score=best_score,
    )


__all__ = [
    "BENCHMARK_ENDPOINT",
    "BENCHMARK_SUITE_VERSION",
    "CONTEXT_SUITE_VERSION",
    "BenchmarkDecision",
    "BenchmarkRecord",
    "BenchmarkStore",
    "BenchmarkSuite",
    "ContextBenchmarkRecord",
    "candidate_set_fingerprint",
    "choose_target",
    "context_target_fingerprint",
    "system_fingerprint",
    "target_fingerprint",
    "target_with_context_window",
]
