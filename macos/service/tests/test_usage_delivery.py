from __future__ import annotations

import pytest

from mnemosyne_macos.config import TokenSidecarConfig
from mnemosyne_macos.usage import NormalizedUsage, UsageEvent
from mnemosyne_macos.usage_delivery import UsageService
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
) -> None:
    monkeypatch.delenv("TOKEN_SIDECAR_POSTGRES_DSN", raising=False)
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
