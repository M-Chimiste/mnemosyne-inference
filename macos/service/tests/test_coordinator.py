from __future__ import annotations

import asyncio

import pytest

from mnemosyne_macos.config import MacConfig
from mnemosyne_macos.coordinator import (
    CoordinatorError,
    CoordinatorState,
    QueueFull,
    QueueTimeout,
    ResidencyCoordinator,
)
from mnemosyne_macos.engines.base import Deadline, EngineAdapter
from mnemosyne_macos.models import (
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
    ServiceState,
)


class FakeAdapter(EngineAdapter):
    ownership = "fake"

    def __init__(self, engine: EngineName, events: list[str]) -> None:
        self.engine = engine
        self.events = events
        self.residents: list[ResidentInstance] = []
        self.authoritative = True
        self.load_count = 0
        self.fail_load = False
        self.fail_unload = False
        self.loaded_ready = True
        self.loaded_managed = True
        self.load_gate: asyncio.Event | None = None
        self.load_started = asyncio.Event()
        self.close_count = 0

    async def validate_control(self, *, deadline: Deadline) -> EngineSnapshot:
        return await self.inspect(deadline=deadline)

    async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
        del deadline
        self.events.append(f"inspect:{self.engine}")
        return EngineSnapshot(
            engine=self.engine,
            residents=tuple(self.residents),
            authoritative=self.authoritative,
            service_state=(ServiceState.READY if self.authoritative else ServiceState.UNREACHABLE),
            diagnostic=None if self.authoritative else "uncertain",
        )

    async def load(self, target: ResolvedTarget, *, deadline: Deadline) -> LoadedHandle:
        del deadline
        self.events.append(f"load:{self.engine}:{target.alias}")
        self.load_started.set()
        if self.fail_load:
            raise CoordinatorError("synthetic load failure")
        if self.load_gate is not None:
            await self.load_gate.wait()
        self.load_count += 1
        resident = ResidentInstance(
            engine=self.engine,
            canonical_model_id=target.key.canonical_model_id,
            instance_id=f"{self.engine}-{self.load_count}",
            ready=self.loaded_ready,
            managed=self.loaded_managed,
        )
        self.residents = [resident]
        return LoadedHandle(
            target=target,
            instance=resident,
            base_url=f"http://{self.engine}",
            wire_model=target.wire_model,
        )

    async def unload(
        self,
        instance: ResidentInstance,
        *,
        deadline: Deadline,
    ) -> None:
        del deadline
        self.events.append(f"unload:{self.engine}:{instance.canonical_model_id}")
        if self.fail_unload:
            raise CoordinatorError("synthetic unload failure")
        self.residents = [
            resident for resident in self.residents if resident != instance
        ]

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        return ProxyRoute(
            base_url=handle.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model=handle.wire_model,
        )

    async def aclose(self) -> None:
        self.close_count += 1
        self.events.append(f"close:{self.engine}")


def _targets() -> tuple[ResolvedTarget, ResolvedTarget]:
    profiles = MacConfig.model_validate(
        {
            "engines": {
                "llama_cpp": {"enabled": True},
                "omlx": {"enabled": True},
            },
            "models": [
                {
                    "alias": "studio",
                    "engine": "llama.cpp",
                    "model": "org/studio",
                    "load": {"parallel": 4},
                },
                {"alias": "glm", "engine": "omlx", "model": "org/glm"},
            ]
        }
    ).profiles()
    return profiles["studio"], profiles["glm"]


def _coordinator(events: list[str]) -> tuple[ResidencyCoordinator, dict[EngineName, FakeAdapter]]:
    adapters = {
        engine: FakeAdapter(engine, events)
        for engine in (EngineName.LLAMA_CPP, EngineName.OMLX, EngineName.DS4)
    }
    coordinator = ResidencyCoordinator(
        adapters,
        queue_timeout_seconds=2,
        transition_timeout_seconds=2,
        cleanup_timeout_seconds=2,
    )
    return coordinator, adapters


@pytest.mark.asyncio
async def test_concurrent_same_target_coalesces_one_load() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()

    first_task = asyncio.create_task(coordinator.acquire(studio))
    second_task = asyncio.create_task(coordinator.acquire(studio))
    first, second = await asyncio.gather(first_task, second_task)

    assert adapters[EngineName.LLAMA_CPP].load_count == 1
    status = await coordinator.status()
    assert status.inflight == 2
    assert status.state == CoordinatorState.READY

    await first.release()
    await second.release()


@pytest.mark.asyncio
async def test_warm_fast_path_uses_free_permits_before_bounding_waiters() -> None:
    events: list[str] = []
    adapters = {
        engine: FakeAdapter(engine, events)
        for engine in (EngineName.LLAMA_CPP, EngineName.OMLX, EngineName.DS4)
    }
    coordinator = ResidencyCoordinator(
        adapters,
        queue_timeout_seconds=2,
        transition_timeout_seconds=2,
        cleanup_timeout_seconds=2,
        configured_max_concurrency=2,
        max_queue_depth=1,
    )
    studio, _glm = _targets()
    await coordinator.initialize()

    first = await coordinator.acquire(studio)
    # Both warm requests fit the effective limit. Neither consumes the sole
    # waiter slot, even when they arrive without yielding to the driver.
    second = await coordinator.acquire(studio)
    queued_task = asyncio.create_task(coordinator.acquire(studio))
    await asyncio.sleep(0)

    status = await coordinator.status()
    assert status.inflight == 2
    assert status.queued == 1
    assert status.capacity is not None
    assert status.capacity.effective_limit == 2
    assert status.capacity.available == 0

    with pytest.raises(QueueFull, match="1 waiters"):
        await coordinator.acquire(studio)

    await first.release()
    third = await asyncio.wait_for(queued_task, timeout=1)
    assert (await coordinator.status()).inflight == 2
    await second.release()
    await third.release()


@pytest.mark.asyncio
async def test_abort_fences_queued_successor_before_unloading_resident() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, glm = _targets()
    await coordinator.initialize()
    lease = await coordinator.acquire(studio)

    successor = asyncio.create_task(coordinator.acquire(glm))
    for _ in range(50):
        if (await coordinator.status()).queued == 1:
            break
        await asyncio.sleep(0)

    await lease.abort()

    with pytest.raises(CoordinatorError, match="abort in progress"):
        await successor
    status = await coordinator.status()
    assert status.state == CoordinatorState.IDLE
    assert status.inflight == 0
    assert status.queued == 0
    assert status.resident_alias is None
    assert adapters[EngineName.LLAMA_CPP].residents == []
    assert "load:omlx:glm" not in events

    recovered = await coordinator.acquire(glm)
    await recovered.release()


@pytest.mark.asyncio
async def test_different_target_waits_for_stream_lease_to_drain() -> None:
    events: list[str] = []
    coordinator, _adapters = _coordinator(events)
    studio, glm = _targets()
    await coordinator.initialize()
    studio_lease = await coordinator.acquire(studio)

    glm_task = asyncio.create_task(coordinator.acquire(glm))
    for _ in range(50):
        if (await coordinator.status()).state == CoordinatorState.DRAINING:
            break
        await asyncio.sleep(0)
    assert not glm_task.done()
    assert not any(event.startswith("unload:llama.cpp") for event in events)

    await studio_lease.release()
    glm_lease = await asyncio.wait_for(glm_task, timeout=1)
    unload_index = next(
        index for index, event in enumerate(events) if event.startswith("unload:llama.cpp")
    )
    load_index = next(
        index for index, event in enumerate(events) if event == "load:omlx:glm"
    )
    assert unload_index < load_index
    await glm_lease.release()


@pytest.mark.asyncio
async def test_empty_maintenance_drains_leases_and_reopens_admission() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    lease = await coordinator.acquire(studio)
    maintenance_ran = asyncio.Event()

    async def operation(_deadline: Deadline) -> None:
        assert all(not adapter.residents for adapter in adapters.values())
        events.append("maintenance")
        maintenance_ran.set()

    task = asyncio.create_task(
        coordinator.run_empty_maintenance(operation, name="inventory refresh")
    )
    await asyncio.sleep(0)
    assert not maintenance_ran.is_set()
    await lease.release()
    await task

    assert "maintenance" in events
    assert (await coordinator.status()).state == CoordinatorState.IDLE
    new_lease = await coordinator.acquire(studio)
    await new_lease.release()


@pytest.mark.asyncio
async def test_empty_maintenance_drain_timeout_fails_closed_and_reconciles() -> None:
    events: list[str] = []
    coordinator, _adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    lease = await coordinator.acquire(studio)
    coordinator.transition_timeout_seconds = 0.01
    maintenance_ran = False

    async def operation(_deadline: Deadline) -> None:
        nonlocal maintenance_ran
        maintenance_ran = True

    with pytest.raises(QueueTimeout, match="timed out draining leases"):
        await coordinator.run_empty_maintenance(
            operation,
            name="runtime activation",
        )

    status = await coordinator.status()
    assert maintenance_ran is False
    assert status.state == CoordinatorState.DEGRADED
    assert status.resident_alias == "studio"
    assert status.inflight == 1
    assert status.diagnostic == (
        "timed out draining leases for runtime activation"
    )
    with pytest.raises(CoordinatorError, match="not initialized"):
        await coordinator.acquire(studio)

    await lease.release()
    coordinator.transition_timeout_seconds = 2
    assert await coordinator.reconcile() is True
    recovered = await coordinator.status()
    assert recovered.state == CoordinatorState.READY
    assert recovered.resident_alias == "studio"
    assert recovered.diagnostic is None

    new_lease = await coordinator.acquire(studio)
    await new_lease.release()


@pytest.mark.asyncio
async def test_fifo_switch_prevents_old_target_starvation() -> None:
    events: list[str] = []
    coordinator, _adapters = _coordinator(events)
    studio, glm = _targets()
    await coordinator.initialize()
    first_studio = await coordinator.acquire(studio)

    glm_task = asyncio.create_task(coordinator.acquire(glm))
    await asyncio.sleep(0)
    later_studio_task = asyncio.create_task(coordinator.acquire(studio))
    await asyncio.sleep(0)
    await first_studio.release()

    glm_lease = await asyncio.wait_for(glm_task, timeout=1)
    assert not later_studio_task.done()
    await glm_lease.release()

    later_studio = await asyncio.wait_for(later_studio_task, timeout=1)
    assert events.count("load:omlx:glm") == 1
    assert events.count("load:llama.cpp:studio") == 2
    await later_studio.release()


@pytest.mark.asyncio
async def test_uncertain_adapter_state_fails_closed_before_load() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    adapters[EngineName.OMLX].authoritative = False

    with pytest.raises(CoordinatorError, match="uncertain"):
        await coordinator.initialize()
    with pytest.raises(CoordinatorError, match="not initialized"):
        await coordinator.acquire(studio)
    assert adapters[EngineName.LLAMA_CPP].load_count == 0


@pytest.mark.asyncio
async def test_release_is_idempotent() -> None:
    events: list[str] = []
    coordinator, _adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    lease = await coordinator.acquire(studio)
    await lease.release()
    await lease.release()
    assert (await coordinator.status()).inflight == 0


@pytest.mark.asyncio
async def test_cancelled_release_can_be_retried_without_leaking_lease() -> None:
    events: list[str] = []
    coordinator, _adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    lease = await coordinator.acquire(studio)

    await coordinator._condition.acquire()  # noqa: SLF001 - cancellation race injection
    try:
        first_release = asyncio.create_task(lease.release())
        await asyncio.sleep(0)
        first_release.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_release
    finally:
        coordinator._condition.release()  # noqa: SLF001

    await lease.release()
    assert (await coordinator.status()).inflight == 0


@pytest.mark.asyncio
async def test_acquire_is_rejected_before_initialization() -> None:
    events: list[str] = []
    coordinator, _adapters = _coordinator(events)
    studio, _glm = _targets()
    with pytest.raises(CoordinatorError, match="not initialized"):
        await coordinator.acquire(studio)


@pytest.mark.asyncio
async def test_transition_failure_remains_degraded() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    adapters[EngineName.LLAMA_CPP].fail_load = True

    with pytest.raises(CoordinatorError, match="synthetic load failure"):
        await coordinator.acquire(studio)
    await asyncio.sleep(0)
    status = await coordinator.status()
    assert status.state == CoordinatorState.DEGRADED
    assert "synthetic load failure" in (status.diagnostic or "")


@pytest.mark.asyncio
async def test_failed_manual_unload_fails_closed() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    lease = await coordinator.acquire(studio)
    await lease.release()
    adapters[EngineName.LLAMA_CPP].fail_unload = True

    with pytest.raises(CoordinatorError, match="synthetic unload failure"):
        await coordinator.unload()
    assert (await coordinator.status()).state == CoordinatorState.DEGRADED
    with pytest.raises(CoordinatorError, match="not initialized"):
        await coordinator.acquire(studio)


@pytest.mark.asyncio
async def test_unusable_post_load_resident_is_rejected_and_cleaned() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    adapters[EngineName.LLAMA_CPP].loaded_ready = False

    with pytest.raises(CoordinatorError, match="unusable"):
        await coordinator.acquire(studio)
    assert adapters[EngineName.LLAMA_CPP].residents == []
    assert (await coordinator.status()).state == CoordinatorState.DEGRADED


@pytest.mark.asyncio
async def test_reconcile_unloads_external_drift() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    await coordinator.initialize()
    adapters[EngineName.OMLX].residents = [
        ResidentInstance(
            engine=EngineName.OMLX,
            canonical_model_id="externally-loaded",
            instance_id="external-1",
        )
    ]

    assert await coordinator.reconcile() is False
    assert adapters[EngineName.OMLX].residents == []
    assert (await coordinator.status()).state == CoordinatorState.IDLE


@pytest.mark.asyncio
async def test_reconcile_is_a_barrier_for_queued_model_switches() -> None:
    events: list[str] = []
    coordinator, _adapters = _coordinator(events)
    studio, glm = _targets()
    await coordinator.initialize()
    studio_lease = await coordinator.acquire(studio)
    glm_task = asyncio.create_task(coordinator.acquire(glm))
    for _ in range(50):
        if (await coordinator.status()).queued == 1:
            break
        await asyncio.sleep(0)

    reconcile_task = asyncio.create_task(coordinator.reconcile())
    await asyncio.sleep(0)
    with pytest.raises(CoordinatorError, match="reconciliation in progress"):
        await glm_task
    await studio_lease.release()

    assert await reconcile_task is True
    assert "load:omlx:glm" not in events
    assert (await coordinator.status()).resident_alias == "studio"


@pytest.mark.asyncio
async def test_failed_audit_rejects_queued_switch_and_stops_driver() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, glm = _targets()
    await coordinator.initialize()
    studio_lease = await coordinator.acquire(studio)
    glm_task = asyncio.create_task(coordinator.acquire(glm))
    for _ in range(50):
        if (await coordinator.status()).queued == 1:
            break
        await asyncio.sleep(0)
    adapters[EngineName.DS4].authoritative = False

    with pytest.raises(CoordinatorError, match="uncertain"):
        await coordinator.audit()
    with pytest.raises(CoordinatorError, match="audit could not establish"):
        await glm_task
    await studio_lease.release()
    await asyncio.sleep(0)

    status = await coordinator.status()
    assert status.state == CoordinatorState.DEGRADED
    assert status.queued == 0
    assert "load:omlx:glm" not in events


@pytest.mark.asyncio
async def test_audit_skips_long_running_transition() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    load_gate = asyncio.Event()
    adapters[EngineName.LLAMA_CPP].load_gate = load_gate

    acquire_task = asyncio.create_task(coordinator.acquire(studio))
    await asyncio.wait_for(adapters[EngineName.LLAMA_CPP].load_started.wait(), timeout=1)

    assert (await coordinator.status()).state == CoordinatorState.LOADING
    assert await asyncio.wait_for(coordinator.audit(), timeout=0.1) is True
    assert (await coordinator.status()).state == CoordinatorState.LOADING
    assert not acquire_task.done()

    load_gate.set()
    lease = await asyncio.wait_for(acquire_task, timeout=1)
    assert (await coordinator.status()).state == CoordinatorState.READY
    await lease.release()


@pytest.mark.asyncio
async def test_idle_eviction_uses_verified_global_unload() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    lease = await coordinator.acquire(studio)
    await lease.release()

    assert await coordinator.evict_if_idle(0) is True
    assert adapters[EngineName.LLAMA_CPP].residents == []
    assert (await coordinator.status()).state == CoordinatorState.IDLE


@pytest.mark.asyncio
async def test_same_alias_with_changed_load_digest_reloads() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    await coordinator.initialize()
    first = MacConfig.model_validate(
        {
            "engines": {"llama_cpp": {"enabled": True}},
            "models": [
                {
                    "alias": "studio",
                    "engine": "llama.cpp",
                    "model": "org/studio",
                    "load": {"context_length": 4096},
                }
            ]
        }
    ).profiles()["studio"]
    second = MacConfig.model_validate(
        {
            "engines": {"llama_cpp": {"enabled": True}},
            "models": [
                {
                    "alias": "studio",
                    "engine": "llama.cpp",
                    "model": "org/studio",
                    "load": {"context_length": 8192},
                }
            ]
        }
    ).profiles()["studio"]

    first_lease = await coordinator.acquire(first)
    await first_lease.release()
    second_lease = await coordinator.acquire(second)
    await second_lease.release()
    assert adapters[EngineName.LLAMA_CPP].load_count == 2


@pytest.mark.asyncio
async def test_same_model_with_changed_served_name_reloads() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    await coordinator.initialize()
    profiles = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "first",
                    "engine": "llama.cpp",
                    "model": "org/studio",
                    "served_model_name": "served-first",
                },
                {
                    "alias": "second",
                    "engine": "llama.cpp",
                    "model": "org/studio",
                    "served_model_name": "served-second",
                },
            ]
        }
    ).profiles()
    assert profiles["first"].key == profiles["second"].key

    first = await coordinator.acquire(profiles["first"])
    await first.release()
    second = await coordinator.acquire(profiles["second"])

    assert adapters[EngineName.LLAMA_CPP].load_count == 2
    assert second.handle.wire_model == "served-second"
    await second.release()


@pytest.mark.asyncio
async def test_llama_capability_launch_modes_do_not_share_a_resident() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    await coordinator.initialize()
    profiles = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "generation",
                    "engine": "llama.cpp",
                    "model": "org/shared",
                    "served_model_name": "shared-wire",
                },
                {
                    "alias": "embeddings",
                    "engine": "llama.cpp",
                    "model": "org/shared",
                    "served_model_name": "shared-wire",
                    "capabilities": ["embeddings"],
                },
                {
                    "alias": "rerank",
                    "engine": "llama.cpp",
                    "model": "org/shared",
                    "served_model_name": "shared-wire",
                    "capabilities": ["rerank"],
                },
            ]
        }
    ).profiles()
    assert len({target.key for target in profiles.values()}) == 1

    for alias in ("generation", "embeddings", "rerank"):
        lease = await coordinator.acquire(profiles[alias])
        await lease.release()

    assert adapters[EngineName.LLAMA_CPP].load_count == 3


@pytest.mark.asyncio
async def test_transition_has_a_hard_timeout() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    coordinator.transition_timeout_seconds = 0.01
    adapters[EngineName.LLAMA_CPP].load_gate = asyncio.Event()

    with pytest.raises(CoordinatorError, match="transition.*timed out"):
        await coordinator.acquire(studio)
    assert (await coordinator.status()).state == CoordinatorState.DEGRADED


@pytest.mark.asyncio
async def test_shutdown_deadline_cancels_blocked_transition_and_attempts_cleanup() -> None:
    events: list[str] = []
    adapters = {
        engine: FakeAdapter(engine, events)
        for engine in (EngineName.LLAMA_CPP, EngineName.OMLX, EngineName.DS4)
    }
    coordinator = ResidencyCoordinator(
        adapters,
        queue_timeout_seconds=2,
        transition_timeout_seconds=900,
        cleanup_timeout_seconds=900,
        shutdown_grace_seconds=0.1,
    )
    studio, _glm = _targets()
    await coordinator.initialize()
    studio_adapter = adapters[EngineName.LLAMA_CPP]
    studio_adapter.load_gate = asyncio.Event()
    acquire_task = asyncio.create_task(coordinator.acquire(studio))
    await asyncio.wait_for(studio_adapter.load_started.wait(), timeout=1)
    inspections_before_shutdown = sum(
        event.startswith("inspect:") for event in events
    )

    started = asyncio.get_running_loop().time()
    await coordinator.shutdown()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed <= coordinator.shutdown_grace_seconds + 0.05
    with pytest.raises(CoordinatorError, match="coordinator is stopping"):
        await acquire_task
    assert (
        sum(event.startswith("inspect:") for event in events)
        > inspections_before_shutdown
    )
    assert all(adapter.close_count == 1 for adapter in adapters.values())
    assert (await coordinator.status()).state == CoordinatorState.STOPPING


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["unload", "reconcile"])
async def test_control_barrier_deadline_bounds_blocked_driver(operation: str) -> None:
    events: list[str] = []
    adapters = {
        engine: FakeAdapter(engine, events)
        for engine in (EngineName.LLAMA_CPP, EngineName.OMLX, EngineName.DS4)
    }
    coordinator = ResidencyCoordinator(
        adapters,
        queue_timeout_seconds=2,
        transition_timeout_seconds=900,
        cleanup_timeout_seconds=2,
    )
    studio, _glm = _targets()
    await coordinator.initialize()
    studio_adapter = adapters[EngineName.LLAMA_CPP]
    studio_adapter.load_gate = asyncio.Event()
    acquire_task = asyncio.create_task(coordinator.acquire(studio))
    await asyncio.wait_for(studio_adapter.load_started.wait(), timeout=1)
    # The already-running transition captured the original long deadline;
    # the control operation must still obey its own current deadline.
    coordinator.transition_timeout_seconds = 0.05

    started = asyncio.get_running_loop().time()
    with pytest.raises(QueueTimeout, match="transition driver"):
        await getattr(coordinator, operation)()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed <= coordinator.transition_timeout_seconds + 0.05
    expected_barrier = (
        "manual unload in progress"
        if operation == "unload"
        else "reconciliation in progress"
    )
    with pytest.raises(CoordinatorError, match=expected_barrier):
        await acquire_task
    await asyncio.sleep(0)
    assert studio_adapter.load_count == 0
    assert (await coordinator.status()).state == CoordinatorState.DEGRADED


@pytest.mark.asyncio
async def test_audit_detects_and_repairs_missing_resident() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    lease = await coordinator.acquire(studio)
    await lease.release()
    adapters[EngineName.LLAMA_CPP].residents = []

    assert await coordinator.audit() is False
    status = await coordinator.status()
    assert status.state == CoordinatorState.IDLE
    assert status.resident_alias is None
