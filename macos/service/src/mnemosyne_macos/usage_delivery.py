"""Durable local usage recording and retry-safe Postgres delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import time
from typing import Any, Iterable, Sequence

try:  # Optional at import time so a disabled sidecar needs no Postgres wheel.
    import psycopg
except ImportError:  # pragma: no cover - exercised by host environments without psycopg
    psycopg = None  # type: ignore[assignment]

from .config import TokenSidecarConfig
from .usage import UsageEvent
from .usage_store import UsageStore


_INSERT_SQL = (
    "INSERT INTO public.token_usage "
    "(event_id, timestamp, node_id, model, prompt_tokens, completion_tokens, "
    " total_tokens, response_ms, endpoint, status_code) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (event_id) DO NOTHING"
)


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
        dsn = os.environ.get("TOKEN_SIDECAR_POSTGRES_DSN", "").strip()
        self.last_error: str | None = None
        self.writer = writer
        if self.writer is None and config.enabled and dsn:
            self.writer = PgUsageWriter(
                dsn=dsn,
                node_id=config.node_id,
                connect_timeout=config.connect_timeout_seconds,
            )
        elif self.writer is None and config.enabled:
            self.last_error = (
                "TOKEN_SIDECAR_POSTGRES_DSN is not configured; usage remains "
                "durable in the local outbox"
            )
        self._task: asyncio.Task[None] | None = None
        self._flush_lock = asyncio.Lock()
        self.last_flush_at: float | None = None
        self.last_flush_count = 0

    @classmethod
    def open(cls, path: str | Path, config: TokenSidecarConfig) -> "UsageService":
        return cls(UsageStore.open(path), config)

    async def start(self) -> None:
        if self._task is None and self.config.enabled:
            self._task = asyncio.create_task(
                self._flush_loop(), name="mnemosyne-macos-usage-outbox"
            )

    async def record(self, event: UsageEvent) -> None:
        await asyncio.to_thread(
            self.store.record_batch,
            [event],
            enqueue_outbox=self.config.enabled,
        )
        if self.config.enabled:
            await asyncio.to_thread(
                self.store.prune_outbox,
                keep=self.config.max_outbox_rows,
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
                return 0
            try:
                count = await self.writer.write_batch(rows)
                await asyncio.to_thread(
                    self.store.ack_outbox,
                    [int(row["id"]) for row in rows],
                )
            except Exception as exc:
                self.last_error = str(exc)
                self.last_flush_at = time.time()
                self.last_flush_count = 0
                raise
            self.last_flush_at = time.time()
            self.last_flush_count = count
            self.last_error = None
            return count

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.flush_interval_seconds)
            try:
                await self.flush_once()
            except Exception:
                continue

    async def status(self) -> dict:
        pending = await asyncio.to_thread(self.store.count_outbox)
        return {
            "enabled": self.config.enabled,
            "node_id": self.config.node_id,
            "writer_ready": self.writer is not None,
            "outbox_pending": pending,
            "outbox_depth": pending,
            "last_flush_at": self.last_flush_at,
            "last_flush_count": self.last_flush_count,
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


__all__ = ["PgUsageWriter", "UsageService"]
