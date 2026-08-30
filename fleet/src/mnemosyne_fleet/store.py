from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RouteRecord:
    route_id: str
    started_at: float
    completed_at: float | None
    public_model: str
    deployment_id: str
    node_id: str
    enrollment_id: str
    instance_id: str
    endpoint: str
    queue_ms: float
    response_ms: float | None
    status_code: int | None
    failure_code: str | None

    @property
    def reporting_node_id(self) -> str:
        return self.node_id


class FleetStore:
    """Secret-free Fleet metadata in a database separate from token usage."""

    def __init__(self, path: Path, *, history_limit: int = 10_000) -> None:
        self._path = path
        self._history_limit = history_limit
        self._lock = asyncio.Lock()

    async def initialize(
        self,
        *,
        node_ids: tuple[str, ...],
        models: tuple[tuple[str, str], ...],
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync, node_ids, models)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize_sync(
        self,
        node_ids: tuple[str, ...],
        models: tuple[tuple[str, str], ...],
    ) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS enrolled_nodes (
                    node_id TEXT PRIMARY KEY,
                    configured_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS logical_models (
                    public_model TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    configured_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS routes (
                    route_id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    public_model TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    enrollment_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    queue_ms REAL NOT NULL,
                    response_ms REAL,
                    status_code INTEGER,
                    failure_code TEXT
                );
                CREATE INDEX IF NOT EXISTS routes_started_idx
                    ON routes(started_at DESC);
                CREATE INDEX IF NOT EXISTS routes_node_idx
                    ON routes(node_id, started_at DESC);
                """
            )
            route_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(routes)").fetchall()
            }
            if "enrollment_id" not in route_columns:
                # Pre-pairing route history used the reporting node ID as its
                # membership authority. Static enrollment preserves exactly
                # that identity, so it is the only safe migration value.
                conn.execute("ALTER TABLE routes ADD COLUMN enrollment_id TEXT")
            conn.execute(
                """
                UPDATE routes
                   SET enrollment_id=node_id
                 WHERE enrollment_id IS NULL OR enrollment_id=''
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS routes_enrollment_idx
                    ON routes(enrollment_id, started_at DESC)
                """
            )
            now = time.time()
            conn.execute(
                """
                UPDATE routes
                SET completed_at=?,
                    response_ms=(? - started_at) * 1000.0,
                    failure_code='gateway_restarted'
                WHERE completed_at IS NULL
                """,
                (now, now),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO enrolled_nodes(node_id, configured_at) VALUES (?, ?)",
                ((node_id, now) for node_id in node_ids),
            )
            conn.executemany(
                """
                INSERT INTO logical_models(public_model, deployment_id, configured_at)
                VALUES (?, ?, ?)
                ON CONFLICT(public_model) DO UPDATE SET
                    deployment_id=excluded.deployment_id,
                    configured_at=excluded.configured_at
                """,
                ((name, deployment_id, now) for name, deployment_id in models),
            )
            placeholders = ",".join("?" for _ in node_ids)
            if placeholders:
                conn.execute(
                    f"DELETE FROM enrolled_nodes WHERE node_id NOT IN ({placeholders})",
                    node_ids,
                )
            model_names = tuple(row[0] for row in models)
            placeholders = ",".join("?" for _ in model_names)
            if placeholders:
                conn.execute(
                    f"DELETE FROM logical_models WHERE public_model NOT IN ({placeholders})",
                    model_names,
                )

    async def start_route(self, record: RouteRecord) -> None:
        async with self._lock:
            await asyncio.to_thread(self._start_route_sync, record)

    def _start_route_sync(self, record: RouteRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO routes(
                    route_id, started_at, completed_at, public_model,
                    deployment_id, node_id, enrollment_id, instance_id,
                    endpoint, queue_ms,
                    response_ms, status_code, failure_code
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    record.route_id,
                    record.started_at,
                    record.public_model,
                    record.deployment_id,
                    record.node_id,
                    record.enrollment_id,
                    record.instance_id,
                    record.endpoint,
                    record.queue_ms,
                ),
            )
            conn.execute(
                """
                DELETE FROM routes WHERE route_id IN (
                    SELECT route_id FROM routes
                    ORDER BY started_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self._history_limit,),
            )

    async def finish_route(
        self,
        route_id: str,
        *,
        status_code: int | None,
        failure_code: str | None,
        completed_at: float | None = None,
    ) -> None:
        ended = time.time() if completed_at is None else completed_at
        async with self._lock:
            await asyncio.to_thread(
                self._finish_route_sync,
                route_id,
                status_code,
                failure_code,
                ended,
            )

    def _finish_route_sync(
        self,
        route_id: str,
        status_code: int | None,
        failure_code: str | None,
        completed_at: float,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE routes
                SET completed_at=?,
                    response_ms=(? - started_at) * 1000.0,
                    status_code=?,
                    failure_code=?
                WHERE route_id=?
                """,
                (completed_at, completed_at, status_code, failure_code, route_id),
            )

    async def recent_routes(self, *, limit: int = 100) -> list[dict[str, object]]:
        bounded = max(1, min(limit, 1000))
        async with self._lock:
            return await asyncio.to_thread(self._recent_routes_sync, bounded)

    def _recent_routes_sync(self, limit: int) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT route_id, started_at, completed_at, public_model,
                       deployment_id, node_id,
                       node_id AS reporting_node_id, enrollment_id, endpoint,
                       queue_ms, response_ms,
                       status_code, failure_code
                FROM routes
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
