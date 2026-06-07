"""Mnemosyne Inference — Postgres token-usage writer.

Single-connection async writer. Lazy connect, reconnect on operational
errors, batch INSERT with `ON CONFLICT (event_id) DO NOTHING` for natural
de-dup on retries. Knows nothing about SQLite or the catalog — caller
hands in rows, writer either commits them or raises.

Target schema (introspected 2026-05-20, see
scripts/probe_token_sidecar_schema.py):

    public.token_usage (
      event_id          text         primary key,
      timestamp         timestamptz  not null,
      node_id           text         not null,
      model             text         not null,
      prompt_tokens     integer      not null default 0,
      completion_tokens integer      not null default 0,
      total_tokens      integer      not null default 0,
      response_ms       double precision not null,
      endpoint          text         not null default '/v1/unknown',
      status_code       integer      not null default 200,
      ingested_at       timestamptz  not null default now()
    )

The local SQLite outbox carries strictly more fields than the postgres
table (`requested_model`, `alias`, `backend`, `streamed`, `usage_json`).
Those stay local for debugging and don't ship upstream.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Sequence

import psycopg


INSERT_SQL = (
    "INSERT INTO public.token_usage "
    "(event_id, timestamp, node_id, model, prompt_tokens, completion_tokens, "
    " total_tokens, response_ms, endpoint, status_code) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (event_id) DO NOTHING"
)


class PgWriter:
    """Async writer with a single managed connection.

    Concurrency: only the background `_pg_flush_loop` calls into this, so a
    single connection is sufficient. Callers MUST NOT share a writer
    instance across concurrent tasks — there's no internal lock.
    """

    def __init__(
        self,
        *,
        dsn: str,
        node_id: str,
        connect_timeout: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._dsn = dsn
        self._node_id = node_id
        self._connect_timeout = connect_timeout
        self._log = logger or logging.getLogger("vllm-manager.pg-writer")
        self._conn: psycopg.AsyncConnection | None = None

    @property
    def node_id(self) -> str:
        return self._node_id

    async def _ensure(self) -> psycopg.AsyncConnection:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = await psycopg.AsyncConnection.connect(
            self._dsn,
            autocommit=True,
            connect_timeout=self._connect_timeout,
        )
        return self._conn

    async def write_batch(self, rows: Iterable[Sequence]) -> int:
        """Insert one batch. Each row is the tuple `peek_pg_outbox` returns
        (rich `sqlite3.Row` with: event_id, ts, requested_model, alias,
        backend, endpoint, streamed, prompt_tokens, completion_tokens,
        total_tokens, response_ms, status_code).

        Returns the number of rows attempted (NOT the postgres `rowcount`,
        which would under-count when `ON CONFLICT DO NOTHING` fires). Errors
        bubble; on `OperationalError`, the connection is closed so the next
        call reconnects.
        """
        payload = list(rows)
        if not payload:
            return 0
        params = [self._row_to_params(r) for r in payload]
        try:
            conn = await self._ensure()
            async with conn.cursor() as cur:
                await cur.executemany(INSERT_SQL, params)
            return len(payload)
        except psycopg.OperationalError:
            await self._reset()
            raise

    async def _reset(self) -> None:
        """Drop the current connection so `_ensure` reconnects next call.
        Idempotent and exception-safe."""
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            await conn.close()
        except Exception:
            pass

    async def close(self) -> None:
        await self._reset()

    def _row_to_params(self, r: Sequence) -> tuple:
        """Map outbox row → postgres tuple.

        Indexing matches `Catalog.peek_pg_outbox` column order. `model` is
        the stable alias (catalog requires it to be non-null on enqueue);
        we fall back to `requested_model` defensively. `timestamp` becomes
        a timezone-aware UTC datetime to match `timestamptz`.
        """
        # Support both sqlite3.Row (key access) and plain tuples (positional)
        # so tests can hand in fakes without binding sqlite3 in the loop.
        if hasattr(r, "keys"):
            ts = float(r["ts"])
            model = r["alias"] or r["requested_model"] or "unknown"
            return (
                r["event_id"],
                datetime.fromtimestamp(ts, tz=timezone.utc),
                self._node_id,
                model,
                int(r["prompt_tokens"]),
                int(r["completion_tokens"]),
                int(r["total_tokens"]),
                float(r["response_ms"]),
                r["endpoint"],
                int(r["status_code"]),
            )
        # Positional tuple (id, event_id, ts, requested_model, alias, backend,
        # endpoint, streamed, prompt, completion, total, response_ms, status_code)
        (
            _id, event_id, ts, requested_model, alias, _backend, endpoint,
            _streamed, prompt, completion, total, response_ms, status_code,
        ) = r
        return (
            event_id,
            datetime.fromtimestamp(float(ts), tz=timezone.utc),
            self._node_id,
            alias or requested_model or "unknown",
            int(prompt),
            int(completion),
            int(total),
            float(response_ms),
            endpoint,
            int(status_code),
        )
