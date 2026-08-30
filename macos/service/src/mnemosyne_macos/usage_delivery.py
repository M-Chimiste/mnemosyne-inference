"""Durable local usage recording and retry-safe Postgres delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Iterable, Sequence

try:  # Optional at import time so a disabled sidecar needs no Postgres wheel.
    import psycopg
except ImportError:  # pragma: no cover - exercised by host environments without psycopg
    psycopg = None  # type: ignore[assignment]

from .config import TokenSidecarConfig
from .sidecar_discovery import (
    ReportingIdentity,
    resolve_reporting_dsn,
    resolve_reporting_identity,
)
from .usage import UsageEvent
from .usage_store import (
    UsageEventDuplicate,
    UsageEventReservation,
    UsageOutboxFull,
    UsageReservationConflict,
    UsageStore,
)


_INSERT_SQL = (
    "INSERT INTO public.token_usage "
    "(event_id, timestamp, node_id, model, prompt_tokens, completion_tokens, "
    " total_tokens, response_ms, endpoint, status_code) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (event_id) DO NOTHING"
)


async def _store_call_outcome(function, *args, **kwargs):
    task = asyncio.create_task(
        asyncio.to_thread(function, *args, **kwargs),
        name="mnemosyne-usage-reservation-sqlite",
    )
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    result = task.result()
    return result, cancellation


async def _await_store_call(function, *args, **kwargs):
    """Let one SQLite transition reach a known outcome under cancellation."""

    result, cancellation = await _store_call_outcome(function, *args, **kwargs)
    if cancellation is not None:
        raise cancellation
    return result


class UsageReservationLease:
    """Cancellation-safe ownership of one durable pre-work event reservation."""

    def __init__(
        self,
        service: "UsageService",
        reservation: UsageEventReservation,
    ) -> None:
        self._service = service
        self._reservation = reservation
        self._lock = asyncio.Lock()
        self._finished = False

    @property
    def event_id(self) -> str:
        return self._reservation.event_id

    @property
    def fleet_route(self) -> bool:
        return self._reservation.fleet_route

    async def mark_started(self) -> None:
        """Fence replay immediately before bytes may reach the selected engine."""

        async with self._lock:
            if self._finished:
                raise UsageReservationConflict(
                    "a finished usage reservation cannot start again"
                )
            await _await_store_call(
                self._service.store.mark_event_started,
                self._reservation,
            )

    async def record(self, event: UsageEvent) -> None:
        """Atomically commit usage and consume this reservation exactly once."""

        async with self._lock:
            if self._finished:
                raise UsageReservationConflict(
                    "a finished usage reservation cannot record again"
                )
            await _await_store_call(
                self._service.store.finalize_reserved_event,
                self._reservation,
                event,
            )
            self._finished = True

    async def finish(self) -> None:
        """Release safe pre-work state or retain a started Fleet replay fence."""

        async with self._lock:
            if self._finished:
                return
            await _await_store_call(
                self._service.store.finish_reserved_event,
                self._reservation,
            )
            self._finished = True


class PgUsageWriter:
    def __init__(self, *, dsn: str, node_id: str, connect_timeout: float) -> None:
        self.dsn = dsn
        self.node_id = node_id
        self.connect_timeout = connect_timeout
        self._connection: Any | None = None

    async def _ensure(self) -> Any:
        if psycopg is None:
            raise RuntimeError(
                "psycopg is required when the Postgres token sidecar is enabled"
            )
        if self._connection is None or self._connection.closed:
            self._connection = await psycopg.AsyncConnection.connect(
                self.dsn,
                autocommit=True,
                connect_timeout=self.connect_timeout,
            )
        return self._connection

    async def write_batch(self, rows: Iterable[Sequence]) -> int:
        batch = list(rows)
        if not batch:
            return 0
        params = [self._params(row) for row in batch]
        try:
            connection = await self._ensure()
            async with connection.cursor() as cursor:
                await cursor.executemany(_INSERT_SQL, params)
        except Exception as exc:
            if psycopg is not None and isinstance(exc, psycopg.OperationalError):
                await self.close()
            raise
        return len(batch)

    def _params(self, row: Sequence) -> tuple:
        if hasattr(row, "keys"):
            return (
                row["event_id"],
                datetime.fromtimestamp(float(row["ts"]), tz=timezone.utc),
                self.node_id,
                row["alias"] or row["requested_model"] or "unknown",
                int(row["prompt_tokens"]),
                int(row["completion_tokens"]),
                int(row["total_tokens"]),
                float(row["response_ms"]),
                row["endpoint"],
                int(row["status_code"]),
            )
        (
            _row_id,
            event_id,
            timestamp,
            requested_model,
            alias,
            _backend,
            endpoint,
            _streamed,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            response_ms,
            status_code,
        ) = row
        return (
            event_id,
            datetime.fromtimestamp(float(timestamp), tz=timezone.utc),
            self.node_id,
            alias or requested_model or "unknown",
            int(prompt_tokens),
            int(completion_tokens),
            int(total_tokens),
            float(response_ms),
            endpoint,
            int(status_code),
        )

    async def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass


class UsageService:
    """Persist every event locally, then opportunistically drain its outbox."""

    def __init__(
        self,
        store: UsageStore,
        config: TokenSidecarConfig,
        *,
        writer: PgUsageWriter | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.identity: ReportingIdentity = resolve_reporting_identity(config.node_id)
        dsn = resolve_reporting_dsn()
        self.last_error: str | None = None
        self.writer = writer
        if self.writer is None and config.enabled and dsn:
            self.writer = PgUsageWriter(
                dsn=dsn,
                node_id=self.identity.node_id,
                connect_timeout=config.connect_timeout_seconds,
            )
        elif self.writer is None and config.enabled:
            self.last_error = (
                "The Postgres usage ledger is not configured; usage remains "
                "durable in the local outbox"
            )
        self._task: asyncio.Task[None] | None = None
        self._flush_lock = asyncio.Lock()
        self.last_flush_at: float | None = None
        self.last_flush_count = 0
        self.last_error_code: str | None = None
        self._started = False

    @classmethod
    def open(cls, path: str | Path, config: TokenSidecarConfig) -> "UsageService":
        return cls(UsageStore.open(path), config)

    async def start(self) -> None:
        if self._started:
            return
        await _await_store_call(self.store.recover_abandoned_reservations)
        self._started = True
        if self._task is None and self.config.enabled:
            self._task = asyncio.create_task(
                self._flush_loop(), name="mnemosyne-macos-usage-outbox"
            )

    async def reserve(
        self,
        event_id: str,
        *,
        fleet_route: bool,
        requires_accounting: bool,
    ) -> UsageReservationLease:
        """Atomically reserve replay authority and any required outbox slot."""

        reserve_outbox = bool(self.config.enabled and requires_accounting)
        try:
            reservation, cancellation = await _store_call_outcome(
                self.store.reserve_event,
                event_id,
                fleet_route=fleet_route,
                reserve_outbox=reserve_outbox,
                max_outbox_rows=(
                    self.config.max_outbox_rows if reserve_outbox else None
                ),
            )
        except UsageOutboxFull:
            self._set_outbox_full()
            raise
        lease = UsageReservationLease(self, reservation)
        if cancellation is not None:
            # The SQLite insert may have committed after caller cancellation.
            # Release the still-pre-work row before replaying cancellation so
            # an ownerless reservation cannot consume capacity until restart.
            await _await_store_call(
                self.store.finish_reserved_event,
                reservation,
            )
            raise cancellation
        return lease

    async def record(
        self,
        event: UsageEvent,
        *,
        reservation: UsageReservationLease | None = None,
    ) -> None:
        if reservation is not None:
            await reservation.record(event)
            return
        try:
            await _await_store_call(
                self.store.record_batch,
                [event],
                enqueue_outbox=self.config.enabled,
                max_outbox_rows=(
                    self.config.max_outbox_rows if self.config.enabled else None
                ),
            )
        except UsageOutboxFull:
            self._set_outbox_full()
            raise

    async def ensure_recording_capacity(self) -> None:
        """Fail before inference work when durable delivery has no headroom."""

        if not self.config.enabled:
            return
        available = await asyncio.to_thread(
            self.store.outbox_has_capacity,
            maximum=self.config.max_outbox_rows,
        )
        if not available:
            self._set_outbox_full()
            raise UsageOutboxFull(
                "durable usage outbox is at its configured capacity"
            )

    def _set_outbox_full(self) -> None:
        self.last_error_code = "outbox_full"
        self.last_error = (
            "The durable usage outbox is full; new accounted inference "
            "is paused until delivery succeeds."
        )

    async def flush_once(self) -> int:
        if self.writer is None:
            return 0
        async with self._flush_lock:
            rows = await asyncio.to_thread(
                self.store.peek_outbox,
                limit=self.config.batch_size,
            )
            if not rows:
                self.last_flush_at = time.time()
                self.last_flush_count = 0
                self.last_error = None
                self.last_error_code = None
                return 0
            try:
                count = await self.writer.write_batch(rows)
                await asyncio.to_thread(
                    self.store.ack_outbox,
                    [int(row["id"]) for row in rows],
                )
            except Exception as exc:
                self.last_error = str(exc)
                self.last_error_code = "delivery_failed"
                self.last_flush_at = time.time()
                self.last_flush_count = 0
                raise
            self.last_flush_at = time.time()
            self.last_flush_count = count
            self.last_error = None
            self.last_error_code = None
            return count

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.flush_interval_seconds)
            try:
                await self.flush_once()
            except Exception:
                continue

    async def status(self) -> dict:
        if self.config.enabled:
            pending, reserved, recording_capacity_available = (
                await asyncio.to_thread(
                    self.store.outbox_capacity_status,
                    maximum=self.config.max_outbox_rows,
                )
            )
        else:
            pending = await asyncio.to_thread(self.store.count_outbox)
            reserved = 0
            recording_capacity_available = True
        outbox_full = bool(
            self.config.enabled and not recording_capacity_available
        )
        if outbox_full:
            self.last_error_code = "outbox_full"
        elif self.last_error_code == "outbox_full":
            # Capacity may have been restored by another service instance or
            # an administrative repair between polls. Do not leave a stale
            # admission-blocked signal after the durable store has headroom.
            self.last_error_code = None
            self.last_error = None
        return {
            "enabled": self.config.enabled,
            "node_id": self.identity.node_id,
            "node_id_source": self.identity.source,
            "writer_ready": self.writer is not None,
            "outbox_pending": pending,
            "outbox_depth": pending,
            "outbox_reserved": reserved,
            "outbox_capacity": self.config.max_outbox_rows,
            "outbox_full": outbox_full,
            "recording_capacity_available": recording_capacity_available,
            "last_flush_at": self.last_flush_at,
            "last_flush_count": self.last_flush_count,
            "last_error_code": self.last_error_code,
            "last_error": self.last_error,
        }

    async def list_usage(self, *, limit: int = 100) -> list[dict]:
        rows = await asyncio.to_thread(self.store.list_request_usage, limit=limit)
        return [dict(row) for row in rows]

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.writer is not None:
            try:
                await self.flush_once()
            except Exception:
                pass
            await self.writer.close()
        await asyncio.to_thread(self.store.close)


__all__ = [
    "PgUsageWriter",
    "UsageEventDuplicate",
    "UsageOutboxFull",
    "UsageReservationConflict",
    "UsageReservationLease",
    "UsageService",
]
