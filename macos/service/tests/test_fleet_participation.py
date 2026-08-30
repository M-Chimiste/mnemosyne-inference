from __future__ import annotations

import asyncio
import threading

import pytest

from mnemosyne_macos.fleet_participation import (
    FleetParticipationClosed,
    FleetParticipationState,
    FleetParticipationStore,
    FleetParticipationUnavailable,
)


@pytest.mark.asyncio
async def test_participation_defaults_joined_and_persists_pause(tmp_path) -> None:
    database = tmp_path / "state.db"
    store = FleetParticipationStore.open(database)
    initial = await store.status()
    assert initial.state == FleetParticipationState.JOINED
    assert initial.joined is True
    assert initial.active_fleet_requests == 0
    assert initial.joined_at is not None
    assert initial.pause_requested_at is None

    paused = await store.set_joined(False)
    assert paused.state == FleetParticipationState.PAUSED
    assert paused.joined is False
    assert paused.pause_requested_at is not None
    await store.close()
    await store.close()

    reopened = FleetParticipationStore.open(database)
    restored = await reopened.status()
    assert restored.state == FleetParticipationState.PAUSED
    assert restored.joined is False
    assert restored.updated_at == paused.updated_at
    assert restored.joined_at == paused.joined_at
    assert restored.pause_requested_at == paused.pause_requested_at
    with pytest.raises(FleetParticipationUnavailable):
        await reopened.acquire()

    joined = await reopened.set_joined(True)
    assert joined.state == FleetParticipationState.JOINED
    assert joined.joined is True
    assert joined.joined_at is not None
    assert joined.joined_at >= initial.joined_at
    await reopened.close()


@pytest.mark.asyncio
async def test_pause_drains_existing_leases_and_rejects_new_leases(tmp_path) -> None:
    store = FleetParticipationStore.open(tmp_path / "state.db")
    lease = await store.acquire()
    assert (await store.status()).active_fleet_requests == 1

    draining = await store.set_joined(False)
    assert draining.state == FleetParticipationState.DRAINING
    assert draining.active_fleet_requests == 1
    with pytest.raises(FleetParticipationUnavailable):
        await store.acquire()

    await lease.release()
    await lease.release()
    paused = await store.status()
    assert paused.state == FleetParticipationState.PAUSED
    assert paused.active_fleet_requests == 0

    await store.close()
    with pytest.raises(FleetParticipationClosed):
        await store.set_joined(True)
    with pytest.raises(FleetParticipationClosed):
        await store.acquire()


@pytest.mark.asyncio
async def test_active_lease_can_finish_after_idempotent_store_close(tmp_path) -> None:
    store = FleetParticipationStore.open(tmp_path / "state.db")
    lease = await store.acquire()
    await store.close()
    await store.close()

    await lease.release()
    assert (await store.status()).active_fleet_requests == 0


@pytest.mark.asyncio
async def test_cancelled_preference_write_reaches_one_atomic_outcome(tmp_path) -> None:
    database = tmp_path / "state.db"
    store = FleetParticipationStore.open(database)
    entered = threading.Event()
    allow_write = threading.Event()
    persist = store._persist_preference  # noqa: SLF001

    def blocked_persist(*args) -> None:
        entered.set()
        assert allow_write.wait(timeout=1)
        persist(*args)

    store._persist_preference = blocked_persist  # type: ignore[method-assign]  # noqa: SLF001
    update = asyncio.create_task(store.set_joined(False))
    assert await asyncio.to_thread(entered.wait, 1)
    update.cancel()
    allow_write.set()
    with pytest.raises(asyncio.CancelledError):
        await update

    assert (await store.status()).state == FleetParticipationState.PAUSED
    await store.close()
    reopened = FleetParticipationStore.open(database)
    assert (await reopened.status()).state == FleetParticipationState.PAUSED
    await reopened.close()
