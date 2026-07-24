"""SQLite-backed analytics and durable Postgres usage outbox.

Each usage batch is written to ``request_usage`` and, when enabled, to
``pg_usage_outbox`` in one SQLite transaction.  Consumers only *peek* rows;
they acknowledge SQLite ids after the remote write commits.  Combined with a
stable event id and the central writer's ``ON CONFLICT DO NOTHING``, retrying a
batch after an ambiguous network failure is safe.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .usage import UsageEvent


@dataclass(frozen=True, slots=True)
class PersistResult:
    analytics_inserted: int
    outbox_inserted: int


class UsageStore:
    """Thread-safe owner of the native service's usage tables."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # In-memory databases and some read/write test configurations do
            # not support WAL. SQLite transactions still provide atomicity.
            pass
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._bootstrap()

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "UsageStore":
        db_path = os.fspath(path)
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            db_path = str(Path(db_path).expanduser())
        connection = sqlite3.connect(db_path, check_same_thread=False)
        try:
            return cls(connection)
        except BaseException:
            connection.close()
            raise

    def _bootstrap(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS request_usage (
                  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id           TEXT    NOT NULL UNIQUE,
                  ts                 REAL    NOT NULL,
                  requested_model    TEXT,
                  alias              TEXT,
                  backend            TEXT    NOT NULL,
                  endpoint           TEXT    NOT NULL,
                  streamed           INTEGER NOT NULL DEFAULT 0,
                  prompt_tokens      INTEGER NOT NULL DEFAULT 0 CHECK(prompt_tokens >= 0),
                  completion_tokens  INTEGER NOT NULL DEFAULT 0 CHECK(completion_tokens >= 0),
                  total_tokens       INTEGER NOT NULL DEFAULT 0 CHECK(total_tokens >= 0),
                  usage_json         TEXT,
                  response_ms        REAL    NOT NULL DEFAULT 0 CHECK(response_ms >= 0),
                  status_code        INTEGER NOT NULL DEFAULT 200
                );

                CREATE TABLE IF NOT EXISTS pg_usage_outbox (
                  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id           TEXT    NOT NULL UNIQUE,
                  ts                 REAL    NOT NULL,
                  requested_model    TEXT,
                  alias              TEXT,
                  backend            TEXT    NOT NULL,
                  endpoint           TEXT    NOT NULL,
                  streamed           INTEGER NOT NULL DEFAULT 0,
                  prompt_tokens      INTEGER NOT NULL DEFAULT 0 CHECK(prompt_tokens >= 0),
                  completion_tokens  INTEGER NOT NULL DEFAULT 0 CHECK(completion_tokens >= 0),
                  total_tokens       INTEGER NOT NULL DEFAULT 0 CHECK(total_tokens >= 0),
                  response_ms        REAL    NOT NULL DEFAULT 0 CHECK(response_ms >= 0),
                  status_code        INTEGER NOT NULL DEFAULT 200
                );

                CREATE INDEX IF NOT EXISTS idx_request_usage_alias_ts
                  ON request_usage(alias, ts);
                CREATE INDEX IF NOT EXISTS idx_pg_usage_outbox_event
                  ON pg_usage_outbox(event_id);
                """
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "UsageStore":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def record_batch(
        self,
        events: Iterable[UsageEvent],
        *,
        enqueue_outbox: bool,
    ) -> PersistResult:
        """Atomically persist analytics and optional delivery-outbox rows.

        Both inserts use ``event_id`` de-duplication. Replaying a previously
        committed event is a no-op; replaying it with ``enqueue_outbox=True``
        can also heal an analytics-only event by adding its missing outbox row.
        """

        batch = list(events)
        if not batch:
            return PersistResult(analytics_inserted=0, outbox_inserted=0)

        analytics_inserted = 0
        outbox_inserted = 0
        with self._lock, self._conn:
            for event in batch:
                usage = event.usage
                analytics = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO request_usage (
                      event_id, ts, requested_model, alias, backend, endpoint,
                      streamed, prompt_tokens, completion_tokens, total_tokens,
                      usage_json, response_ms, status_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.timestamp,
                        event.requested_model,
                        event.alias,
                        event.backend,
                        event.normalized_endpoint,
                        1 if event.streamed else 0,
                        usage.prompt_tokens,
                        usage.completion_tokens,
                        usage.total_tokens,
                        usage.raw_json(),
                        event.response_ms,
                        event.status_code,
                    ),
                )
                analytics_inserted += max(0, analytics.rowcount)

                if enqueue_outbox:
                    outbox = self._conn.execute(
                        """
                        INSERT OR IGNORE INTO pg_usage_outbox (
                          event_id, ts, requested_model, alias, backend, endpoint,
                          streamed, prompt_tokens, completion_tokens, total_tokens,
                          response_ms, status_code
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            event.timestamp,
                            event.requested_model,
                            event.alias,
                            event.backend,
                            event.normalized_endpoint,
                            1 if event.streamed else 0,
                            usage.prompt_tokens,
                            usage.completion_tokens,
                            usage.total_tokens,
                            event.response_ms,
                            event.status_code,
                        ),
                    )
                    outbox_inserted += max(0, outbox.rowcount)

        return PersistResult(
            analytics_inserted=analytics_inserted,
            outbox_inserted=outbox_inserted,
        )

    def list_request_usage(self, *, limit: int = 100) -> list[sqlite3.Row]:
        if limit <= 0:
            return []
        with self._lock:
            return self._conn.execute(
                "SELECT id, event_id, ts, requested_model, alias, backend, "
                "endpoint, streamed, prompt_tokens, completion_tokens, "
                "total_tokens, usage_json, response_ms, status_code "
                "FROM request_usage ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()

    def count_request_usage(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM request_usage"
            ).fetchone()
        return int(row["count"])

    def peek_outbox(self, *, limit: int) -> list[sqlite3.Row]:
        """Return, but do not remove, the oldest rows in writer-compatible order."""

        if limit <= 0:
            return []
        with self._lock:
            return self._conn.execute(
                "SELECT id, event_id, ts, requested_model, alias, backend, "
                "endpoint, streamed, prompt_tokens, completion_tokens, "
                "total_tokens, response_ms, status_code "
                "FROM pg_usage_outbox ORDER BY id LIMIT ?",
                (int(limit),),
            ).fetchall()

    # Compatibility name used by the CUDA-side PgWriter integration.
    def peek_pg_outbox(self, limit: int) -> list[sqlite3.Row]:
        return self.peek_outbox(limit=limit)

    def dequeue_outbox(self, *, limit: int) -> list[sqlite3.Row]:
        """Lease a delivery batch without deleting it.

        The name mirrors queue terminology, but intentionally has the same
        non-destructive semantics as ``peek_outbox``: callers acknowledge ids
        only after the central write commits.
        """

        return self.peek_outbox(limit=limit)

    def ack_outbox(self, ids: Iterable[int]) -> int:
        """Acknowledge SQLite row ids only after the remote transaction commits."""

        unique_ids = list(dict.fromkeys(int(row_id) for row_id in ids))
        if not unique_ids:
            return 0
        placeholders = ",".join("?" for _ in unique_ids)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"DELETE FROM pg_usage_outbox WHERE id IN ({placeholders})",
                unique_ids,
            )
            return max(0, cursor.rowcount)

    def delete_pg_outbox(self, ids: Iterable[int]) -> int:
        return self.ack_outbox(ids)

    def count_outbox(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM pg_usage_outbox"
            ).fetchone()
        return int(row["count"])

    def count_pg_outbox(self) -> int:
        return self.count_outbox()

    def prune_outbox(self, *, keep: int) -> int:
        """Drop oldest rows until only the newest ``keep`` remain."""

        keep = max(0, int(keep))
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM pg_usage_outbox WHERE id IN ("
                "  SELECT id FROM pg_usage_outbox "
                "  ORDER BY id DESC LIMIT -1 OFFSET ?"
                ")",
                (keep,),
            )
            return max(0, cursor.rowcount)

    def prune_pg_outbox(self, keep: int) -> int:
        return self.prune_outbox(keep=keep)


__all__ = ["PersistResult", "UsageStore"]
