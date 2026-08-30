"""Durable, Mac-local admission control for Fleet-routed requests.

Pairing a Mac with Nyx and allowing that Mac to accept Fleet work are
deliberately separate decisions.  This store owns only the latter preference;
it does not change inference configuration, model residency, or storage.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from enum import StrEnum
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, TypeVar


T = TypeVar("T")


async def _await_blocking_outcome(
    function: Callable[..., T],
    *args: Any,
) -> tuple[T, asyncio.CancelledError | None]:
    """Finish one SQLite call before replaying caller cancellation."""

    task = asyncio.create_task(
        asyncio.to_thread(function, *args),
        name="mnemosyne-fleet-participation-sqlite",
    )
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    return task.result(), cancellation


class FleetParticipationState(StrEnum):
    JOINED = "joined"
    DRAINING = "draining"
    PAUSED = "paused"


class FleetParticipationUnavailable(RuntimeError):
    """Raised when a paused or draining Mac refuses new Fleet work."""


class FleetParticipationClosed(RuntimeError):
    """Raised when admission is attempted after the store has closed."""


@dataclass(frozen=True, slots=True)
class FleetParticipationStatus:
    state: FleetParticipationState
    joined: bool
    active_fleet_requests: int
    updated_at: float
    state_changed_at: float
    joined_at: float | None
    pause_requested_at: float | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload


class FleetParticipationLease:
    """One Fleet request retained through its complete response stream."""

    def __init__(self, owner: "FleetParticipationStore") -> None:
        self._owner = owner
        self._release_task: asyncio.Task[None] | None = None
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        if self._release_task is None:
            self._release_task = asyncio.create_task(
                self._owner._release(),
                name="mnemosyne-fleet-participation-release",
            )
        try:
            await asyncio.shield(self._release_task)
        except asyncio.CancelledError:
            # The independently owned decrement continues. A later cleanup
            # attempt can await the same task without double-releasing.
            raise
        except BaseException:
            self._release_task = None
            raise
        self._released = True


class FleetParticipationStore:
    """SQLite-backed singleton preference plus in-memory active leases."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._closed = False
        self._active = 0
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        self._bootstrap()
        row = self._read_row()
        self._joined = bool(row["joined"])
        self._updated_at = float(row["updated_at"])
        self._joined_at = (
            float(row["joined_at"]) if row["joined_at"] is not None else None
        )
        self._pause_requested_at = (
            float(row["pause_requested_at"])
            if row["pause_requested_at"] is not None
            else None
        )
        self._state_changed_at = self._updated_at

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "FleetParticipationStore":
        database_path = os.fspath(path)
        if database_path != ":memory:":
            expanded = Path(database_path).expanduser()
            expanded.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            database_path = str(expanded)
        connection = sqlite3.connect(database_path, check_same_thread=False)
        try:
            return cls(connection)
        except BaseException:
            connection.close()
            raise

    def _bootstrap(self) -> None:
        now = time.time()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS native_fleet_participation (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    joined INTEGER NOT NULL CHECK (joined IN (0, 1)),
                    updated_at REAL NOT NULL,
                    joined_at REAL,
                    pause_requested_at REAL
                )
                """
            )
            # Existing installations remain joined until their owner
            # explicitly pauses them, preserving the current Fleet behavior.
            self._connection.execute(
                """
                INSERT OR IGNORE INTO native_fleet_participation (
                    singleton, joined, updated_at, joined_at,
                    pause_requested_at
                ) VALUES (1, 1, ?, ?, NULL)
                """,
                (now, now),
            )

    def _read_row(self) -> sqlite3.Row:
        row = self._connection.execute(
            """
            SELECT joined, updated_at, joined_at, pause_requested_at
              FROM native_fleet_participation
             WHERE singleton = 1
            """
        ).fetchone()
        if row is None:  # pragma: no cover - protected by the bootstrap txn
            raise RuntimeError("Fleet participation preference is missing")
        return row

    def _state(self) -> FleetParticipationState:
        if self._joined:
            return FleetParticipationState.JOINED
        if self._active:
            return FleetParticipationState.DRAINING
        return FleetParticipationState.PAUSED

    def _status(self) -> FleetParticipationStatus:
        return FleetParticipationStatus(
            state=self._state(),
            joined=self._joined,
            active_fleet_requests=self._active,
            updated_at=self._updated_at,
            state_changed_at=self._state_changed_at,
            joined_at=self._joined_at,
            pause_requested_at=self._pause_requested_at,
        )

    def _persist_preference(
        self,
        joined: bool,
        updated_at: float,
        joined_at: float | None,
        pause_requested_at: float | None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE native_fleet_participation
                   SET joined = ?, updated_at = ?, joined_at = ?,
                       pause_requested_at = ?
                 WHERE singleton = 1
                """,
                (
                    1 if joined else 0,
                    updated_at,
                    joined_at,
                    pause_requested_at,
                ),
            )

    async def status(self) -> FleetParticipationStatus:
        async with self._lock:
            return self._status()

    async def set_joined(self, joined: bool) -> FleetParticipationStatus:
        async with self._lock:
            if self._closed:
                raise FleetParticipationClosed(
                    "Fleet participation store is closed"
                )
            if self._joined == joined:
                return self._status()

            previous_state = self._state()
            now = time.time()
            joined_at = now if joined else self._joined_at
            pause_requested_at = now if not joined else self._pause_requested_at
            _, cancellation = await _await_blocking_outcome(
                self._persist_preference,
                joined,
                now,
                joined_at,
                pause_requested_at,
            )
            self._joined = joined
            self._updated_at = now
            self._joined_at = joined_at
            self._pause_requested_at = pause_requested_at
            if self._state() != previous_state:
                self._state_changed_at = now
            status = self._status()
            if cancellation is not None:
                raise cancellation
            return status

    async def acquire(self) -> FleetParticipationLease:
        async with self._lock:
            if self._closed:
                raise FleetParticipationClosed(
                    "Fleet participation store is closed"
                )
            if not self._joined:
                raise FleetParticipationUnavailable(
                    "this Mac is not accepting new Fleet requests"
                )
            self._active += 1
            return FleetParticipationLease(self)

    async def _release(self) -> None:
        async with self._lock:
            if self._active <= 0:
                raise RuntimeError("Fleet participation lease underflow")
            previous_state = self._state()
            self._active -= 1
            if self._state() != previous_state:
                self._state_changed_at = time.time()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            _, cancellation = await _await_blocking_outcome(
                self._connection.close
            )
            self._closed = True
            if cancellation is not None:
                raise cancellation


__all__ = [
    "FleetParticipationClosed",
    "FleetParticipationLease",
    "FleetParticipationState",
    "FleetParticipationStatus",
    "FleetParticipationStore",
    "FleetParticipationUnavailable",
]
