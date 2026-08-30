from __future__ import annotations

import asyncio
from threading import Event

import pytest

from mnemosyne_macos.config import TokenSidecarConfig
from mnemosyne_macos.usage import NormalizedUsage, UsageEvent
from mnemosyne_macos.usage_delivery import (
    UsageEventDuplicate,
    UsageOutboxFull,
    UsageService,
)
from mnemosyne_macos.usage_store import UsageStore


class FakeWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.batches: list[list] = []
        self.closed = False

    async def write_batch(self, rows) -> int:
        batch = list(rows)
        self.batches.append(batch)
        if self.fail:
            raise ConnectionError("central ledger unavailable")
        return len(batch)

    async def close(self) -> None:
        self.closed = True


def _event(event_id: str = "event-1") -> UsageEvent:
    return UsageEvent(
        usage=NormalizedUsage(7, 3, 10, {"prompt_tokens": 7}),
        endpoint="/v1/chat/completions",
        engine="omlx",
        requested_model="mlx/model",
        alias="frontier",
        response_ms=12.5,
        event_id=event_id,
        timestamp=100.0,
    )


@pytest.mark.asyncio
async def test_successful_delivery_acknowledges_durable_rows() -> None:
    writer = FakeWriter()
    store = UsageStore.open(":memory:")
    service = UsageService(
        store,
        TokenSidecarConfig(enabled=True, node_id="mac", flush_interval_seconds=60),
        writer=writer,  # type: ignore[arg-type]
    )

    await service.record(_event())
    assert store.count_request_usage() == 1
    assert store.count_outbox() == 1
    assert await service.flush_once() == 1
    assert store.count_outbox() == 0
    assert writer.batches[0][0]["event_id"] == "event-1"

    await service.close()
    assert writer.closed is True


@pytest.mark.asyncio
async def test_delivery_failure_keeps_rows_for_retry() -> None:
    writer = FakeWriter(fail=True)
    store = UsageStore.open(":memory:")
    service = UsageService(
        store,
        TokenSidecarConfig(enabled=True, node_id="mac"),
        writer=writer,  # type: ignore[arg-type]
    )
    await service.record(_event())

    with pytest.raises(ConnectionError, match="unavailable"):
        await service.flush_once()
    assert store.count_outbox() == 1
    assert "unavailable" in (await service.status())["last_error"]

    writer.fail = False
    assert await service.flush_once() == 1
    assert store.count_outbox() == 0
    await service.close()


@pytest.mark.asyncio
async def test_disabled_sidecar_keeps_local_analytics_without_outbox() -> None:
    store = UsageStore.open(":memory:")
    service = UsageService(store, TokenSidecarConfig(enabled=False))
    await service.record(_event())
    assert store.count_request_usage() == 1
    assert store.count_outbox() == 0
    await service.close()


@pytest.mark.asyncio
async def test_enabled_sidecar_reports_missing_dsn_and_keeps_outbox(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("TOKEN_SIDECAR_POSTGRES_DSN", raising=False)
    monkeypatch.setenv(
        "MNEMOSYNE_TOKEN_SIDECAR_LAUNCH_AGENT",
        str(tmp_path / "missing.plist"),
    )
    store = UsageStore.open(":memory:")
    service = UsageService(
        store,
        TokenSidecarConfig(enabled=True, node_id="mac"),
    )
    await service.record(_event())

    status = await service.status()
    assert status["writer_ready"] is False
    assert "not configured" in status["last_error"]
    assert status["outbox_depth"] == 1
    await service.close()


@pytest.mark.asyncio
async def test_full_outbox_applies_backpressure_without_dropping_oldest() -> None:
    store = UsageStore.open(":memory:")
    service = UsageService(
        store,
        TokenSidecarConfig(enabled=True, node_id="mac", max_outbox_rows=1),
    )
    await service.record(_event("retained"))

    with pytest.raises(UsageOutboxFull, match="configured capacity"):
        await service.ensure_recording_capacity()
    with pytest.raises(UsageOutboxFull, match="configured capacity"):
        await service.record(_event("refused"))

    status = await service.status()
    assert status["outbox_depth"] == 1
    assert status["outbox_capacity"] == 1
    assert status["outbox_full"] is True
    assert status["last_error_code"] == "outbox_full"
    assert [row["event_id"] for row in store.peek_outbox(limit=10)] == [
        "retained"
    ]
    assert [row["event_id"] for row in store.list_request_usage(limit=10)] == [
        "retained"
    ]

    store.ack_outbox([int(store.peek_outbox(limit=1)[0]["id"])])
    recovered = await service.status()
    assert recovered["outbox_full"] is False
    assert recovered["last_error_code"] is None
    assert recovered["last_error"] is None
    await service.close()


@pytest.mark.asyncio
async def test_reservation_consumes_capacity_until_atomic_finalization() -> None:
    store = UsageStore.open(":memory:")
    service = UsageService(
        store,
        TokenSidecarConfig(enabled=True, node_id="mac", max_outbox_rows=1),
    )

    reservation = await service.reserve(
        "fleet-route",
        fleet_route=True,
        requires_accounting=True,
    )
    reserved = await service.status()
    assert reserved["outbox_depth"] == 0
    assert reserved["outbox_reserved"] == 1
    assert reserved["recording_capacity_available"] is False

    with pytest.raises(UsageOutboxFull):
        await service.reserve(
            "other-route",
            fleet_route=True,
            requires_accounting=True,
        )

    await reservation.mark_started()
    await service.record(_event("fleet-route"), reservation=reservation)
    finalized = await service.status()
    assert finalized["outbox_depth"] == 1
    assert finalized["outbox_reserved"] == 0
    assert finalized["recording_capacity_available"] is False
    assert store.reservation_state("fleet-route") is None

    with pytest.raises(UsageEventDuplicate):
        await service.reserve(
            "fleet-route",
            fleet_route=True,
            requires_accounting=True,
        )

    store.ack_outbox([int(store.peek_outbox(limit=1)[0]["id"])])
    assert (await service.status())["recording_capacity_available"] is True
    await service.close()


@pytest.mark.asyncio
async def test_start_recovers_abandoned_reservations_without_replaying_started_fleet(
    tmp_path,
) -> None:
    path = tmp_path / "usage.db"
    first = UsageStore.open(path)
    reserved = first.reserve_event(
        "safe-prework",
        fleet_route=True,
        reserve_outbox=True,
        max_outbox_rows=2,
    )
    started = first.reserve_event(
        "maybe-worked",
        fleet_route=True,
        reserve_outbox=True,
        max_outbox_rows=2,
    )
    first.mark_event_started(started)
    assert reserved.event_id == "safe-prework"
    first.close()

    reopened = UsageStore.open(path)
    service = UsageService(
        reopened,
        TokenSidecarConfig(enabled=True, node_id="mac", max_outbox_rows=2),
    )
    await service.start()
    assert reopened.reservation_state("safe-prework") is None
    assert reopened.reservation_state("maybe-worked") == "completed"
    assert reopened.count_active_reservations() == 0
    with pytest.raises(UsageEventDuplicate) as replay:
        await service.reserve(
            "maybe-worked",
            fleet_route=True,
            requires_accounting=True,
        )
    assert replay.value.state == "completed"
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_reserve_cannot_strand_an_ownerless_outbox_slot() -> None:
    store = UsageStore.open(":memory:")
    service = UsageService(
        store,
        TokenSidecarConfig(enabled=True, node_id="mac", max_outbox_rows=1),
    )
    entered = Event()
    proceed = Event()
    original = store.reserve_event

    def blocked_reserve(*args, **kwargs):
        entered.set()
        assert proceed.wait(timeout=5)
        return original(*args, **kwargs)

    store.reserve_event = blocked_reserve  # type: ignore[method-assign]
    task = asyncio.create_task(
        service.reserve(
            "cancelled-route",
            fleet_route=True,
            requires_accounting=True,
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.count_active_reservations() == 0
    assert store.outbox_capacity_status(maximum=1) == (0, 0, True)
    await service.close()
