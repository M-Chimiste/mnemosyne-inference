from __future__ import annotations

import asyncio

import pytest

from cuda_residency import (
    CapacitySpec,
    CoordinatorState,
    CudaResidencyCoordinator,
    QueueFull,
    QueueTimeout,
)


class Harness:
    def __init__(self, *, limit: int = 2, start_gate: asyncio.Event | None = None):
        self.limit = limit
        self.start_gate = start_gate
        self.started = asyncio.Event()
        self.loads: list[str] = []
        self.stops = 0

    async def start(self, profile: str) -> None:
        self.loads.append(profile)
        self.started.set()
        if self.start_gate is not None:
            await self.start_gate.wait()

    def stop(self) -> None:
        self.stops += 1

    def capacity(self, _profile: str) -> CapacitySpec:
        return CapacitySpec(self.limit, "test-adapter", "authoritative")

    def coordinator(
        self,
        *,
        configured_max: int | None = None,
        queue_depth: int = 4,
    ) -> CudaResidencyCoordinator:
        return CudaResidencyCoordinator(
            start_engine=self.start,
            stop_engine=self.stop,
            derive_capacity=self.capacity,
            configured_max_concurrency=configured_max,
            max_queue_depth=queue_depth,
        )


@pytest.mark.asyncio
async def test_warm_slots_bypass_queue_but_waiters_remain_bounded():
    harness = Harness(limit=2)
    coordinator = harness.coordinator(queue_depth=1)

    first = await coordinator.acquire("a", "sha256:" + "a" * 64, timeout_seconds=1)
    second = await coordinator.acquire("a", "sha256:" + "a" * 64, timeout_seconds=1)
    third_task = asyncio.create_task(
        coordinator.acquire("a", "sha256:" + "a" * 64, timeout_seconds=1)
    )
    await asyncio.sleep(0)
    with pytest.raises(QueueFull):
        await coordinator.acquire("a", "sha256:" + "a" * 64, timeout_seconds=1)

    status = await coordinator.status()
    assert status.active == 2
    assert status.queued == 1
    assert status.queue_limit == 1

    await first.release()
    third = await asyncio.wait_for(third_task, timeout=1)
    await second.release()
    await third.release()
    assert (await coordinator.status()).active == 0


@pytest.mark.asyncio
async def test_fifo_target_switch_drains_epoch_and_old_target_cannot_bypass():
    harness = Harness(limit=1)
    coordinator = harness.coordinator(queue_depth=4)
    a_id = "sha256:" + "a" * 64
    b_id = "sha256:" + "b" * 64

    first_a = await coordinator.acquire("a", a_id, timeout_seconds=1)
    b_task = asyncio.create_task(
        coordinator.acquire("b", b_id, timeout_seconds=1)
    )
    await asyncio.sleep(0)
    late_a_task = asyncio.create_task(
        coordinator.acquire("a", a_id, timeout_seconds=1)
    )
    await asyncio.sleep(0.01)

    assert harness.loads == ["a"]
    assert (await coordinator.status()).transition_target == b_id
    await first_a.release()
    b_lease = await asyncio.wait_for(b_task, timeout=1)
    assert harness.loads == ["a", "b"]
    assert not late_a_task.done()

    # Releasing an old lease twice cannot decrement the new epoch.
    await first_a.release()
    assert (await coordinator.status()).active == 1
    await b_lease.release()
    late_a = await asyncio.wait_for(late_a_task, timeout=1)
    assert harness.loads == ["a", "b", "a"]
    await late_a.release()


@pytest.mark.asyncio
async def test_configured_max_is_only_a_ceiling():
    harness = Harness(limit=8)
    coordinator = harness.coordinator(configured_max=2, queue_depth=2)
    lease = await coordinator.acquire(
        "a", "sha256:" + "a" * 64, timeout_seconds=1
    )
    status = await coordinator.status()
    assert status.capacity.derived_limit == 8
    assert status.effective_limit == 2
    await lease.release()


@pytest.mark.asyncio
async def test_cancelled_waiter_frees_queue_slot():
    harness = Harness(limit=1)
    coordinator = harness.coordinator(queue_depth=1)
    deployment_id = "sha256:" + "a" * 64
    lease = await coordinator.acquire("a", deployment_id, timeout_seconds=1)
    waiter = asyncio.create_task(
        coordinator.acquire("a", deployment_id, timeout_seconds=5)
    )
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (await coordinator.status()).queued == 0
    await lease.release()


@pytest.mark.asyncio
async def test_transition_drain_timeout_fails_closed_with_diagnostic():
    gate = asyncio.Event()
    harness = Harness(limit=1, start_gate=gate)
    coordinator = harness.coordinator(queue_depth=1)
    acquire_task = asyncio.create_task(
        coordinator.acquire(
            "a", "sha256:" + "a" * 64, timeout_seconds=5
        )
    )
    await harness.started.wait()

    with pytest.raises(QueueTimeout):
        await coordinator.unload(timeout_seconds=0.01)
    status = await coordinator.status()
    assert status.state == CoordinatorState.DEGRADED
    assert status.authoritative is False
    assert status.accepting is False
    assert status.diagnostic_code == "transition_drain_timeout"

    gate.set()
    with pytest.raises(Exception):
        await acquire_task


@pytest.mark.asyncio
async def test_concurrent_maintenance_barriers_are_fully_serialized():
    stop_calls = 0
    block_maintenance = False
    first_stop_entered = asyncio.Event()
    release_first_stop = asyncio.Event()

    async def start(_profile: str) -> None:
        return None

    async def stop() -> None:
        nonlocal stop_calls
        stop_calls += 1
        if block_maintenance and stop_calls == 2:
            first_stop_entered.set()
            await release_first_stop.wait()

    coordinator = CudaResidencyCoordinator(
        start_engine=start,
        stop_engine=stop,
        derive_capacity=lambda _profile: CapacitySpec(
            1, "test-adapter", "authoritative"
        ),
        configured_max_concurrency=None,
        max_queue_depth=2,
    )
    lease = await coordinator.acquire(
        "a", "sha256:" + "a" * 64, timeout_seconds=1
    )
    await lease.release()
    assert stop_calls == 1  # initial empty-before-load transition

    block_maintenance = True
    first = asyncio.create_task(coordinator.unload(timeout_seconds=1))
    await first_stop_entered.wait()
    second = asyncio.create_task(coordinator.unload(timeout_seconds=1))
    await asyncio.sleep(0.01)
    assert not second.done()
    assert stop_calls == 2

    release_first_stop.set()
    await asyncio.gather(first, second)
    assert stop_calls == 3
