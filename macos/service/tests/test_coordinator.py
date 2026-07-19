from __future__ import annotations

import asyncio

import pytest

from mnemosyne_macos.config import MacConfig
from mnemosyne_macos.coordinator import (
    CoordinatorError,
    CoordinatorState,
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
            "models": [
                {"alias": "studio", "engine": "lmstudio", "model": "org/studio"},
                {"alias": "glm", "engine": "omlx", "model": "org/glm"},
            ]
        }
    ).profiles()
    return profiles["studio"], profiles["glm"]


def _coordinator(events: list[str]) -> tuple[ResidencyCoordinator, dict[EngineName, FakeAdapter]]:
    adapters = {
        engine: FakeAdapter(engine, events)
        for engine in (EngineName.LMSTUDIO, EngineName.OMLX, EngineName.DS4)
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

    assert adapters[EngineName.LMSTUDIO].load_count == 1
    status = await coordinator.status()
    assert status.inflight == 2
    assert status.state == CoordinatorState.READY

    await first.release()
    await second.release()


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
    assert not any(event.startswith("unload:lmstudio") for event in events)

    await studio_lease.release()
    glm_lease = await asyncio.wait_for(glm_task, timeout=1)
    unload_index = next(
        index for index, event in enumerate(events) if event.startswith("unload:lmstudio")
    )
    load_index = next(
        index for index, event in enumerate(events) if event == "load:omlx:glm"
    )
    assert unload_index < load_index
    await glm_lease.release()


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
    assert events.count("load:lmstudio:studio") == 2
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
    assert adapters[EngineName.LMSTUDIO].load_count == 0


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
    adapters[EngineName.LMSTUDIO].fail_load = True

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
    adapters[EngineName.LMSTUDIO].fail_unload = True

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
    adapters[EngineName.LMSTUDIO].loaded_ready = False

    with pytest.raises(CoordinatorError, match="unusable"):
        await coordinator.acquire(studio)
    assert adapters[EngineName.LMSTUDIO].residents == []
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
    adapters[EngineName.LMSTUDIO].load_gate = load_gate

    acquire_task = asyncio.create_task(coordinator.acquire(studio))
    await asyncio.wait_for(adapters[EngineName.LMSTUDIO].load_started.wait(), timeout=1)

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
    assert adapters[EngineName.LMSTUDIO].residents == []
    assert (await coordinator.status()).state == CoordinatorState.IDLE


@pytest.mark.asyncio
async def test_same_alias_with_changed_load_digest_reloads() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    await coordinator.initialize()
    first = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "studio",
                    "engine": "lmstudio",
                    "model": "org/studio",
                    "load": {"context_length": 4096},
                }
            ]
        }
    ).profiles()["studio"]
    second = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "studio",
                    "engine": "lmstudio",
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
    assert adapters[EngineName.LMSTUDIO].load_count == 2


@pytest.mark.asyncio
async def test_transition_has_a_hard_timeout() -> None:
    events: list[str] = []
    coordinator, adapters = _coordinator(events)
    studio, _glm = _targets()
    await coordinator.initialize()
    coordinator.transition_timeout_seconds = 0.01
    adapters[EngineName.LMSTUDIO].load_gate = asyncio.Event()

    with pytest.raises(CoordinatorError, match="transition.*timed out"):
        await coordinator.acquire(studio)
    assert (await coordinator.status()).state == CoordinatorState.DEGRADED


@pytest.mark.asyncio
async def test_shutdown_deadline_cancels_blocked_transition_and_attempts_cleanup() -> None:
    events: list[str] = []
    adapters = {
        engine: FakeAdapter(engine, events)
        for engine in (EngineName.LMSTUDIO, EngineName.OMLX, EngineName.DS4)
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
    studio_adapter = adapters[EngineName.LMSTUDIO]
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
        for engine in (EngineName.LMSTUDIO, EngineName.OMLX, EngineName.DS4)
    }
    coordinator = ResidencyCoordinator(
        adapters,
        queue_timeout_seconds=2,
        transition_timeout_seconds=900,
        cleanup_timeout_seconds=2,
    )
    studio, _glm = _targets()
    await coordinator.initialize()
    studio_adapter = adapters[EngineName.LMSTUDIO]
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
    adapters[EngineName.LMSTUDIO].residents = []

    assert await coordinator.audit() is False
    status = await coordinator.status()
    assert status.state == CoordinatorState.IDLE
    assert status.resident_alias is None
