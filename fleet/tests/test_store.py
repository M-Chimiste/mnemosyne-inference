from __future__ import annotations

import sqlite3

from mnemosyne_fleet.store import FleetStore


async def test_legacy_route_history_gains_static_enrollment_identity(tmp_path) -> None:
    database = tmp_path / "fleet.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE routes (
                route_id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                completed_at REAL,
                public_model TEXT NOT NULL,
                deployment_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                queue_ms REAL NOT NULL,
                response_ms REAL,
                status_code INTEGER,
                failure_code TEXT
            );
            INSERT INTO routes(
                route_id, started_at, completed_at, public_model,
                deployment_id, node_id, instance_id, endpoint, queue_ms,
                response_ms, status_code, failure_code
            ) VALUES (
                'legacy-route', 1.0, 2.0, 'qwen', 'sha256:deployment',
                'reporting-node', 'instance', '/v1/responses', 3.0,
                1000.0, 200, NULL
            );
            """
        )

    store = FleetStore(database)
    await store.initialize(
        node_ids=("reporting-node",),
        models=(("qwen", "sha256:deployment"),),
    )

    routes = await store.recent_routes()
    assert routes == [
        {
            "route_id": "legacy-route",
            "started_at": 1.0,
            "completed_at": 2.0,
            "public_model": "qwen",
            "deployment_id": "sha256:deployment",
            "node_id": "reporting-node",
            "reporting_node_id": "reporting-node",
            "enrollment_id": "reporting-node",
            "endpoint": "/v1/responses",
            "queue_ms": 3.0,
            "response_ms": 1000.0,
            "status_code": 200,
            "failure_code": None,
        }
    ]
    with sqlite3.connect(database) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(routes)").fetchall()
        }
        migrated = connection.execute(
            "SELECT enrollment_id FROM routes WHERE route_id='legacy-route'"
        ).fetchone()
    assert "enrollment_id" in columns
    assert migrated == ("reporting-node",)
