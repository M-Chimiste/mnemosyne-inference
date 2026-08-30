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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator
from uuid import uuid4

from .usage import UsageEvent


DEFAULT_COMPLETED_FLEET_ROUTE_LIMIT = 10_000


class UsageOutboxFull(RuntimeError):
    """Raised before mutation when a new event would exceed the durable cap."""


class UsageEventDuplicate(RuntimeError):
    """Raised when a Fleet route id is already active or durably complete."""

    def __init__(self, state: str) -> None:
        self.state = state
        super().__init__(f"usage event is already {state}")


class UsageReservationConflict(RuntimeError):
    """Raised when a caller does not own the named durable reservation."""


@dataclass(frozen=True, slots=True)
class PersistResult:
    analytics_inserted: int
    outbox_inserted: int


@dataclass(frozen=True, slots=True)
class UsageEventReservation:
    event_id: str
    reservation_id: str
    fleet_route: bool
    reserves_outbox: bool


class UsageStore:
    """Thread-safe owner of the native service's usage tables."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        completed_fleet_route_limit: int = DEFAULT_COMPLETED_FLEET_ROUTE_LIMIT,
    ) -> None:
        if (
            isinstance(completed_fleet_route_limit, bool)
            or not isinstance(completed_fleet_route_limit, int)
            or not 1 <= completed_fleet_route_limit <= 100_000
        ):
            raise ValueError(
                "completed_fleet_route_limit must be between 1 and 100000"
            )
        self._conn = connection
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        self._completed_fleet_route_limit = completed_fleet_route_limit
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # In-memory databases and some read/write test configurations do
            # not support WAL. SQLite transactions still provide atomicity.
            pass
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._bootstrap()

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        completed_fleet_route_limit: int = DEFAULT_COMPLETED_FLEET_ROUTE_LIMIT,
    ) -> "UsageStore":
        db_path = os.fspath(path)
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            db_path = str(Path(db_path).expanduser())
        connection = sqlite3.connect(db_path, check_same_thread=False)
        try:
            return cls(
                connection,
                completed_fleet_route_limit=completed_fleet_route_limit,
            )
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

                CREATE TABLE IF NOT EXISTS usage_event_reservations (
                  event_id           TEXT    PRIMARY KEY,
                  reservation_id     TEXT    NOT NULL UNIQUE,
                  fleet_route        INTEGER NOT NULL CHECK(fleet_route IN (0, 1)),
                  state              TEXT    NOT NULL CHECK(
                    state IN ('reserved', 'started', 'completed')
                  ),
                  reserves_outbox    INTEGER NOT NULL CHECK(
                    reserves_outbox IN (0, 1)
                  ),
                  admitted_at        REAL    NOT NULL,
                  started_at         REAL,
                  completed_at       REAL
                );

                CREATE INDEX IF NOT EXISTS idx_request_usage_alias_ts
                  ON request_usage(alias, ts);
                CREATE INDEX IF NOT EXISTS idx_pg_usage_outbox_event
                  ON pg_usage_outbox(event_id);
                CREATE INDEX IF NOT EXISTS idx_usage_event_reservations_state
                  ON usage_event_reservations(state, reserves_outbox);
                """
            )

    @contextmanager
    def _immediate_transaction(self) -> Iterator[None]:
        """Serialize capacity decisions with every related SQLite mutation.

        ``BEGIN IMMEDIATE`` matters when two service connections briefly
        overlap during launch/recovery: each contender must re-read capacity
        only after the previous writer commits, instead of both observing the
        final free slot under deferred read transactions.
        """

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

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
        max_outbox_rows: int | None = None,
    ) -> PersistResult:
        """Atomically persist analytics and optional delivery-outbox rows.

        Both inserts use ``event_id`` de-duplication. Replaying a previously
        committed event is a no-op; replaying it with ``enqueue_outbox=True``
        can also heal an analytics-only event by adding its missing outbox row.
        """

        batch = list(events)
        if not batch:
            return PersistResult(analytics_inserted=0, outbox_inserted=0)

        if max_outbox_rows is not None and (
            isinstance(max_outbox_rows, bool) or max_outbox_rows < 1
        ):
            raise ValueError("max_outbox_rows must be a positive integer")

        analytics_inserted = 0
        outbox_inserted = 0
        with self._immediate_transaction():
            event_ids = tuple(dict.fromkeys(event.event_id for event in batch))
            placeholders = ",".join("?" for _ in event_ids)
            active_match = self._conn.execute(
                "SELECT 1 FROM usage_event_reservations "
                f"WHERE event_id IN ({placeholders}) "
                "AND state IN ('reserved', 'started') LIMIT 1",
                event_ids,
            ).fetchone()
            if active_match is not None:
                raise UsageReservationConflict(
                    "an active reservation must be finalized through its owner"
                )

            if enqueue_outbox and max_outbox_rows is not None:
                # Capacity is checked under the same store lock and SQLite
                # transaction as the inserts. Exact replays already present
                # in the outbox consume no additional capacity and remain
                # idempotent. A new event fails before either analytics or
                # outbox state is mutated; undelivered rows are never pruned.
                current = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM pg_usage_outbox"
                ).fetchone()
                pending = int(current["count"])
                reserved_row = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM usage_event_reservations "
                    "WHERE reserves_outbox=1 "
                    "AND state IN ('reserved', 'started')"
                ).fetchone()
                reserved = int(reserved_row["count"])
                new_event_ids: set[str] = set()
                for event in batch:
                    if event.event_id in new_event_ids:
                        continue
                    exists = self._conn.execute(
                        "SELECT 1 FROM pg_usage_outbox WHERE event_id=? LIMIT 1",
                        (event.event_id,),
                    ).fetchone()
                    if exists is None:
                        new_event_ids.add(event.event_id)
                if pending + reserved + len(new_event_ids) > max_outbox_rows:
                    raise UsageOutboxFull(
                        "durable usage outbox is at its configured capacity"
                    )

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

    def reserve_event(
        self,
        event_id: str,
        *,
        fleet_route: bool,
        reserve_outbox: bool,
        max_outbox_rows: int | None,
    ) -> UsageEventReservation:
        """Atomically fence one event id and, when needed, one outbox slot."""

        if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
            raise ValueError("event_id must be a non-empty bounded string")
        if not isinstance(fleet_route, bool) or not isinstance(reserve_outbox, bool):
            raise ValueError("reservation flags must be boolean")
        if reserve_outbox and (
            isinstance(max_outbox_rows, bool)
            or not isinstance(max_outbox_rows, int)
            or max_outbox_rows < 1
        ):
            raise ValueError(
                "max_outbox_rows must be a positive integer when reserving"
            )

        reservation_id = uuid4().hex
        admitted_at = time.time()
        with self._immediate_transaction():
            existing = self._conn.execute(
                "SELECT state FROM usage_event_reservations WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                state = str(existing["state"])
                raise UsageEventDuplicate(
                    "active" if state in {"reserved", "started"} else "completed"
                )

            historical = self._conn.execute(
                "SELECT 1 FROM request_usage WHERE event_id=? "
                "UNION ALL "
                "SELECT 1 FROM pg_usage_outbox WHERE event_id=? LIMIT 1",
                (event_id, event_id),
            ).fetchone()
            if historical is not None:
                raise UsageEventDuplicate("completed")

            if reserve_outbox:
                pending_row = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM pg_usage_outbox"
                ).fetchone()
                reserved_row = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM usage_event_reservations "
                    "WHERE reserves_outbox=1 "
                    "AND state IN ('reserved', 'started')"
                ).fetchone()
                if (
                    int(pending_row["count"]) + int(reserved_row["count"])
                    >= int(max_outbox_rows)
                ):
                    raise UsageOutboxFull(
                        "durable usage outbox is at its configured capacity"
                    )

            self._conn.execute(
                """
                INSERT INTO usage_event_reservations (
                  event_id, reservation_id, fleet_route, state,
                  reserves_outbox, admitted_at, started_at, completed_at
                ) VALUES (?, ?, ?, 'reserved', ?, ?, NULL, NULL)
                """,
                (
                    event_id,
                    reservation_id,
                    1 if fleet_route else 0,
                    1 if reserve_outbox else 0,
                    admitted_at,
                ),
            )

        return UsageEventReservation(
            event_id=event_id,
            reservation_id=reservation_id,
            fleet_route=fleet_route,
            reserves_outbox=reserve_outbox,
        )

    def mark_event_started(self, reservation: UsageEventReservation) -> None:
        """Durably record the last point before request bytes may reach an engine."""

        with self._immediate_transaction():
            row = self._owned_reservation_row(reservation)
            state = str(row["state"])
            if state == "completed":
                raise UsageReservationConflict(
                    "a completed reservation cannot start again"
                )
            if state == "started":
                return
            self._conn.execute(
                "UPDATE usage_event_reservations "
                "SET state='started', started_at=? "
                "WHERE event_id=? AND reservation_id=?",
                (time.time(), reservation.event_id, reservation.reservation_id),
            )

    def finalize_reserved_event(
        self,
        reservation: UsageEventReservation,
        event: UsageEvent,
    ) -> PersistResult:
        """Atomically replace a reserved slot with analytics and outbox state."""

        if event.event_id != reservation.event_id:
            raise UsageReservationConflict(
                "usage event id does not match its durable reservation"
            )

        analytics_inserted = 0
        outbox_inserted = 0
        with self._immediate_transaction():
            row = self._owned_reservation_row(
                reservation,
                allow_missing_if_recorded=True,
            )
            if row is None or str(row["state"]) == "completed":
                return PersistResult(analytics_inserted=0, outbox_inserted=0)

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
            analytics_inserted = max(0, analytics.rowcount)

            if bool(row["reserves_outbox"]):
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
                outbox_inserted = max(0, outbox.rowcount)

            # ``request_usage`` is the permanent completed replay fence for a
            # successfully accounted event. Keeping a second Fleet tombstone
            # would only duplicate that authority and grow without bound.
            self._conn.execute(
                "DELETE FROM usage_event_reservations "
                "WHERE event_id=? AND reservation_id=?",
                (reservation.event_id, reservation.reservation_id),
            )

        return PersistResult(
            analytics_inserted=analytics_inserted,
            outbox_inserted=outbox_inserted,
        )

    def finish_reserved_event(self, reservation: UsageEventReservation) -> None:
        """Release pre-work state or retain a post-dispatch Fleet replay fence."""

        with self._immediate_transaction():
            row = self._owned_reservation_row(
                reservation,
                allow_missing=True,
            )
            if row is None or str(row["state"]) == "completed":
                return
            if bool(row["fleet_route"]) and str(row["state"]) == "started":
                self._conn.execute(
                    "UPDATE usage_event_reservations "
                    "SET state='completed', reserves_outbox=0, completed_at=? "
                    "WHERE event_id=? AND reservation_id=?",
                    (time.time(), reservation.event_id, reservation.reservation_id),
                )
                self._prune_completed_fleet_routes()
            else:
                self._conn.execute(
                    "DELETE FROM usage_event_reservations "
                    "WHERE event_id=? AND reservation_id=?",
                    (reservation.event_id, reservation.reservation_id),
                )

    def recover_abandoned_reservations(self) -> tuple[int, int]:
        """Resolve reservations whose owning service process no longer exists.

        A Fleet request durably marked ``started`` may have reached its engine,
        so restart keeps a completed replay fence. Reserved pre-work rows and
        all standalone rows are safe to release.
        """

        with self._immediate_transaction():
            completed = self._conn.execute(
                "UPDATE usage_event_reservations "
                "SET state='completed', reserves_outbox=0, completed_at=? "
                "WHERE fleet_route=1 AND state='started'",
                (time.time(),),
            ).rowcount
            released = self._conn.execute(
                "DELETE FROM usage_event_reservations "
                "WHERE state IN ('reserved', 'started')",
            ).rowcount
            self._prune_completed_fleet_routes()
        return max(0, completed), max(0, released)

    def _prune_completed_fleet_routes(self) -> None:
        """Bound no-usage replay fences without ever touching active rows.

        Successful accounted routes are already fenced by ``request_usage``
        and do not need a duplicate row. The remaining completed rows represent
        image requests, upstream failures, cancellation, or a missing-usage
        contract failure. Retain the newest history under the same transaction
        that completed the route, matching Nyx's default bounded route history.
        """

        self._conn.execute(
            "DELETE FROM usage_event_reservations "
            "WHERE state='completed' "
            "AND EXISTS ("
            "  SELECT 1 FROM request_usage "
            "  WHERE request_usage.event_id=usage_event_reservations.event_id"
            ")"
        )
        self._conn.execute(
            "DELETE FROM usage_event_reservations "
            "WHERE state='completed' AND event_id IN ("
            "  SELECT event_id FROM usage_event_reservations "
            "  WHERE state='completed' "
            "  ORDER BY completed_at DESC, admitted_at DESC, event_id DESC "
            "  LIMIT -1 OFFSET ?"
            ")",
            (self._completed_fleet_route_limit,),
        )

    def _owned_reservation_row(
        self,
        reservation: UsageEventReservation,
        *,
        allow_missing: bool = False,
        allow_missing_if_recorded: bool = False,
    ) -> sqlite3.Row | None:
        row = self._conn.execute(
            "SELECT reservation_id, fleet_route, state, reserves_outbox "
            "FROM usage_event_reservations WHERE event_id=?",
            (reservation.event_id,),
        ).fetchone()
        if row is None:
            if allow_missing:
                return None
            if allow_missing_if_recorded:
                recorded = self._conn.execute(
                    "SELECT 1 FROM request_usage WHERE event_id=? LIMIT 1",
                    (reservation.event_id,),
                ).fetchone()
                if recorded is not None:
                    return None
            raise UsageReservationConflict("durable reservation is missing")
        if str(row["reservation_id"]) != reservation.reservation_id:
            raise UsageReservationConflict("durable reservation owner changed")
        return row

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

    def count_active_reservations(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM usage_event_reservations "
                "WHERE state IN ('reserved', 'started')"
            ).fetchone()
        return int(row["count"])

    def count_completed_reservations(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM usage_event_reservations "
                "WHERE state='completed'"
            ).fetchone()
        return int(row["count"])

    def reservation_state(self, event_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM usage_event_reservations WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return str(row["state"]) if row is not None else None

    def outbox_capacity_status(self, *, maximum: int) -> tuple[int, int, bool]:
        """Return pending, reserved, and one-new-event availability atomically."""

        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise ValueError("maximum must be a positive integer")
        with self._lock:
            row = self._conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM pg_usage_outbox) AS pending, "
                "(SELECT COUNT(*) FROM usage_event_reservations "
                " WHERE reserves_outbox=1 "
                " AND state IN ('reserved', 'started')) AS reserved"
            ).fetchone()
            pending = int(row["pending"])
            reserved = int(row["reserved"])
        return pending, reserved, pending + reserved < maximum

    def outbox_has_capacity(self, *, maximum: int) -> bool:
        """Return whether at least one new event can be durably admitted."""

        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise ValueError("maximum must be a positive integer")
        _pending, _reserved, available = self.outbox_capacity_status(
            maximum=maximum
        )
        return available

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


__all__ = [
    "DEFAULT_COMPLETED_FLEET_ROUTE_LIMIT",
    "PersistResult",
    "UsageEventDuplicate",
    "UsageEventReservation",
    "UsageOutboxFull",
    "UsageReservationConflict",
    "UsageStore",
]
