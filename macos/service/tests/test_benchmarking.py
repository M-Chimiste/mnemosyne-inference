from dataclasses import replace
import time
from types import SimpleNamespace

import httpx
import pytest

from mnemosyne_macos.benchmarking import (
    BENCHMARK_SUITE_VERSION,
    MAX_RECORDS_PER_ALIAS,
    BenchmarkRecord,
    BenchmarkStore,
    BenchmarkSuite,
    choose_target,
    system_fingerprint,
    target_fingerprint,
)
from mnemosyne_macos.config import ModelProfile, ModelSelectionConfig
from mnemosyne_macos.engines.base import Deadline, EngineAdapter
from mnemosyne_macos.models import (
    Endpoint,
    EngineName,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
)
from mnemosyne_macos.runtime import NativeRuntime


def _targets():
    primary = ModelProfile(
        alias="qwen",
        engine=EngineName.OMLX,
        model="qwen",
    ).resolve()
    alternative = ModelProfile(
        alias="qwen",
        engine=EngineName.MLXCEL,
        model="/models/qwen",
    ).resolve()
    return primary, alternative


def _record(target, *, runtime: str, ttft: float, tps: float) -> BenchmarkRecord:
    return BenchmarkRecord(
        created_at=time.time(),
        alias=target.alias,
        endpoint="chat/completions",
        engine=target.key.engine.value,
        target_fingerprint=target_fingerprint(target),
        runtime_fingerprint=runtime,
        system_fingerprint=system_fingerprint(),
        config_revision="a" * 64,
        suite_version=BENCHMARK_SUITE_VERSION,
        successful_samples=3,
        failed_samples=0,
        p50_ttft_ms=ttft,
        p50_total_ms=1000,
        p50_output_tps=tps,
    )


def test_benchmark_store_upserts_content_free_records(tmp_path) -> None:
    store = BenchmarkStore(tmp_path / "state.db")
    primary, _alternative = _targets()
    first = _record(primary, runtime="runtime-a", ttft=100, tps=20)
    store.record(first)
    store.record(replace(first, p50_ttft_ms=80))
    values = store.list(alias="qwen")
    assert len(values) == 1
    assert values[0].p50_ttft_ms == 80
    assert "prompt" not in values[0].to_dict()
    assert store.clear_alias("qwen") == 1


def test_benchmark_store_bounds_upgrade_history_per_alias(tmp_path) -> None:
    store = BenchmarkStore(tmp_path / "state.db")
    primary, _alternative = _targets()
    for index in range(MAX_RECORDS_PER_ALIAS + 5):
        value = _record(
            primary,
            runtime=f"runtime-{index}",
            ttft=100 + index,
            tps=20,
        )
        store.record(replace(value, created_at=float(index + 1)))

    values = store.list(alias="qwen", limit=MAX_RECORDS_PER_ALIAS + 10)
    assert len(values) == MAX_RECORDS_PER_ALIAS
    assert values[0].runtime_fingerprint == (
        f"runtime-{MAX_RECORDS_PER_ALIAS + 4}"
    )


@pytest.mark.asyncio
async def test_runtime_update_check_refreshes_selection_identities() -> None:
    class Updates:
        async def check(self, *, refresh: bool):
            return {"refresh": refresh}

    class Adapter:
        async def runtime_fingerprint(self, *, deadline: Deadline) -> str:
            del deadline
            return "new-runtime"

    runtime = object.__new__(NativeRuntime)
    runtime.runtime_updates = Updates()  # type: ignore[assignment]
    runtime.adapters = {EngineName.MLXCEL: Adapter()}  # type: ignore[assignment]
    runtime._runtime_fingerprints = {EngineName.MLXCEL: "old-runtime"}

    result = await runtime.check_runtime_updates(refresh=True)

    assert result == {"refresh": True}
    assert runtime._runtime_fingerprints == {
        EngineName.MLXCEL: "new-runtime"
    }


@pytest.mark.asyncio
async def test_request_falls_back_after_external_preview_binary_changes() -> None:
    primary, alternative = _targets()

    class Adapter:
        async def runtime_fingerprint(self, *, deadline: Deadline) -> str:
            del deadline
            return "new-runtime"

    async def validate(_target) -> None:
        return None

    runtime = object.__new__(NativeRuntime)
    runtime.benchmark_decision = lambda _alias: (alternative, {})  # type: ignore[method-assign]
    runtime.resolve = lambda _alias: primary  # type: ignore[method-assign]
    runtime._profile = lambda _alias: SimpleNamespace(  # type: ignore[method-assign]
        selection=ModelSelectionConfig(mode="benchmark")
    )
    runtime.is_engine_alternative = lambda _target: True  # type: ignore[method-assign]
    runtime._validate_target_storage = validate  # type: ignore[method-assign]
    runtime.adapters = {EngineName.MLXCEL: Adapter()}  # type: ignore[assignment]
    runtime._runtime_fingerprints = {EngineName.MLXCEL: "old-runtime"}

    selected = await runtime.resolve_target("qwen", Endpoint.CHAT_COMPLETIONS)

    assert selected is primary
    assert runtime._runtime_fingerprints[EngineName.MLXCEL] == "new-runtime"


@pytest.mark.asyncio
async def test_user_pin_does_not_require_benchmark_runtime_identity() -> None:
    primary, alternative = _targets()

    async def validate(_target) -> None:
        return None

    runtime = object.__new__(NativeRuntime)
    runtime.benchmark_decision = lambda _alias: (alternative, {})  # type: ignore[method-assign]
    runtime.resolve = lambda _alias: primary  # type: ignore[method-assign]
    runtime._profile = lambda _alias: SimpleNamespace(  # type: ignore[method-assign]
        selection=ModelSelectionConfig(
            mode="pinned",
            pinned_engine=EngineName.MLXCEL,
        )
    )
    runtime.is_engine_alternative = lambda _target: True  # type: ignore[method-assign]
    runtime._validate_target_storage = validate  # type: ignore[method-assign]
    runtime.adapters = {}
    runtime._runtime_fingerprints = {}

    selected = await runtime.resolve_target("qwen", Endpoint.CHAT_COMPLETIONS)

    assert selected is alternative


def test_failed_automatic_choice_does_not_clear_an_explicit_user_pin() -> None:
    class Store:
        calls = 0

        def clear_alias(self, _alias: str) -> int:
            self.calls += 1
            return 2

    runtime = object.__new__(NativeRuntime)
    runtime._profile = lambda _alias: SimpleNamespace(  # type: ignore[method-assign]
        selection=ModelSelectionConfig(
            mode="pinned",
            pinned_engine=EngineName.MLXCEL,
        )
    )
    runtime.benchmark_store = Store()
    runtime._benchmark_records = {"qwen": (object(),)}

    assert runtime.invalidate_automatic_selection("qwen") == 0
    assert runtime.benchmark_store.calls == 0
    assert runtime._benchmark_records["qwen"]


def test_selection_requires_preview_consent_and_exact_fresh_primary_baseline() -> None:
    primary, alternative = _targets()
    records = [
        _record(primary, runtime="omlx-v1", ttft=200, tps=20),
        _record(alternative, runtime="mlxcel-v1", ttft=50, tps=50),
    ]
    fingerprints = {
        EngineName.OMLX: "omlx-v1",
        EngineName.MLXCEL: "mlxcel-v1",
    }
    policy = ModelSelectionConfig(mode="benchmark", objective="latency")
    selected, _decision = choose_target(
        alias="qwen",
        candidates=(primary, alternative),
        policy=policy,
        records=records,
        runtime_fingerprints=fingerprints,
        config_revision="a" * 64,
    )
    assert selected is primary

    selected, decision = choose_target(
        alias="qwen",
        candidates=(primary, alternative),
        policy=policy.model_copy(update={"allow_preview": True}),
        records=records,
        runtime_fingerprints=fingerprints,
        config_revision="a" * 64,
    )
    assert selected is alternative
    assert decision.fallback_engine == "omlx"

    stale_runtime = dict(fingerprints)
    stale_runtime[EngineName.MLXCEL] = "mlxcel-v2"
    selected, _decision = choose_target(
        alias="qwen",
        candidates=(primary, alternative),
        policy=policy.model_copy(update={"allow_preview": True}),
        records=records,
        runtime_fingerprints=stale_runtime,
        config_revision="a" * 64,
    )
    assert selected is primary


def test_user_pin_bypasses_benchmark_ranking_and_preview_consent() -> None:
    primary, alternative = _targets()

    selected, decision = choose_target(
        alias="qwen",
        candidates=(primary, alternative),
        policy=ModelSelectionConfig(
            mode="pinned",
            pinned_engine=EngineName.MLXCEL,
        ),
        records=(),
        runtime_fingerprints={},
        config_revision="a" * 64,
    )

    assert selected is alternative
    assert decision.selected_engine == "mlxcel"
    assert decision.fallback_engine == "omlx"
    assert decision.reason == "user-pinned engine"


def test_unavailable_user_pin_uses_the_original_fallback() -> None:
    primary, _alternative = _targets()

    selected, decision = choose_target(
        alias="qwen",
        candidates=(primary,),
        policy=ModelSelectionConfig(
            mode="pinned",
            pinned_engine=EngineName.MLXCEL,
        ),
        records=(),
        runtime_fingerprints={},
        config_revision="a" * 64,
    )

    assert selected is primary
    assert decision.reason == "pinned engine unavailable; using fixed fallback"


def test_explicit_preview_fallback_can_compare_against_a_stable_alternative() -> None:
    stable, preview = _targets()
    records = [
        _record(preview, runtime="mlxcel-v1", ttft=200, tps=20),
        _record(stable, runtime="omlx-v1", ttft=50, tps=50),
    ]
    selected, decision = choose_target(
        alias="qwen",
        candidates=(preview, stable),
        policy=ModelSelectionConfig(mode="benchmark", objective="latency"),
        records=records,
        runtime_fingerprints={
            EngineName.MLXCEL: "mlxcel-v1",
            EngineName.OMLX: "omlx-v1",
        },
        config_revision="a" * 64,
    )
    assert selected is stable
    assert decision.fallback_engine == "mlxcel"


@pytest.mark.asyncio
async def test_candidate_suite_holds_one_lease_across_warmup_and_samples(
    tmp_path,
) -> None:
    primary, _alternative = _targets()

    class FakeLease:
        releases = 0

        def route(self, endpoint: Endpoint) -> ProxyRoute:
            assert endpoint == Endpoint.CHAT_COMPLETIONS
            return ProxyRoute(
                base_url="http://benchmark.test",
                path="/v1/chat/completions",
                wire_model="qwen",
            )

        async def release(self) -> None:
            self.releases += 1

    class FakeCoordinator:
        acquires = 0
        lease = FakeLease()

        async def acquire(self, target):
            assert target is primary
            self.acquires += 1
            return self.lease

    class FakeAdapter(EngineAdapter):
        engine = EngineName.OMLX

        async def runtime_fingerprint(self, *, deadline: Deadline) -> str | None:
            del deadline
            return "omlx-v1"

        async def inspect(self, *, deadline: Deadline):
            raise AssertionError("not used")

        async def validate_control(self, *, deadline: Deadline):
            raise AssertionError("not used")

        async def load(self, target, *, deadline: Deadline) -> LoadedHandle:
            raise AssertionError("not used")

        async def unload(self, instance: ResidentInstance, *, deadline: Deadline):
            raise AssertionError("not used")

        def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
            raise AssertionError("not used")

        async def aclose(self) -> None:
            return None

    body = (
        'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":8,'
        '"completion_tokens":4,"total_tokens":12}}\n\n'
        "data: [DONE]\n\n"
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=body,
            )
        )
    )
    store = BenchmarkStore(tmp_path / "state.db")
    coordinator = FakeCoordinator()
    suite = BenchmarkSuite(
        store,
        coordinator=coordinator,  # type: ignore[arg-type]
        client=client,
    )
    try:
        record = await suite.run_candidate(
            primary,
            adapter=FakeAdapter(),
            config_revision="a" * 64,
            warmup_runs=1,
            sample_runs=3,
            max_tokens=128,
        )
    finally:
        await client.aclose()

    assert coordinator.acquires == 1
    assert coordinator.lease.releases == 1
    assert record.successful_samples == 3
    assert record.failed_samples == 0
    assert record.p50_ttft_ms is not None
    assert record.p50_output_tps is not None
