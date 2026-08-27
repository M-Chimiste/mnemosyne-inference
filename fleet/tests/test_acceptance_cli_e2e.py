"""Simulated wire-level acceptance for the Fleet CLI.

This test deliberately uses protocol-compatible node and ledger fakes. It
proves the real Fleet HTTP service and ``scripts/fleet_acceptance.py`` process
work together across two authenticated loopback nodes, but it does not replace
Nyx, CUDA, macOS, engine, durable-outbox, or Postgres target-host acceptance.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

import mnemosyne_fleet.app as fleet_app_module
from mnemosyne_fleet.config import (
    FleetConfig,
    LedgerConfig,
    ModelConfig,
    NodeConfig,
    ServerConfig,
)

from .helpers import capacity, identity, snapshot_payload


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _SimulatedNode:
    def __init__(
        self,
        *,
        node_id: str,
        platform: str,
        snapshot_key: str,
        inference_key: str,
        identity_value: dict[str, object],
        deployment_id: str,
        release_responses: asyncio.Event,
    ) -> None:
        self.node_id = node_id
        self.platform = platform
        self.snapshot_key = snapshot_key
        self.inference_key = inference_key
        self.identity_value = identity_value
        self.deployment_id = deployment_id
        self.release_responses = release_responses
        self.sequence = 0
        self.active = 0
        self.peak_active = 0
        self.usage_count = 0
        self.entered = asyncio.Event()
        self.app = FastAPI()
        self.app.get("/fleet/v1/snapshot")(self.snapshot)
        self.app.post("/v1/responses")(self.responses)

    async def snapshot(
        self,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        if authorization != f"Bearer {self.snapshot_key}":
            raise HTTPException(status_code=401)
        self.sequence += 1
        payload = snapshot_payload(
            self.node_id,
            sequence=self.sequence,
            instance_id=f"{self.node_id}-instance",
            identity_value=self.identity_value,
            deployment_id=self.deployment_id,
            deployment_capacity=capacity(limit=1, active=self.active),
        )
        payload["node"]["platform"] = self.platform
        payload["capacity"] = capacity(limit=1, active=self.active)
        return payload

    async def responses(
        self,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ):
        if authorization != f"Bearer {self.inference_key}":
            raise HTTPException(status_code=401)
        if body.get("model") != f"{self.node_id}-qwen":
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "wrong_node_alias"}},
            )

        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        self.entered.set()
        try:
            # Both nodes must receive work before either response is allowed
            # to complete. This makes the fan-out assertion deterministic
            # without relying on scheduler timing or arbitrary sleeps.
            await asyncio.wait_for(self.release_responses.wait(), timeout=5)
            self.usage_count += 1
            return {
                "id": f"{self.node_id}-response",
                "object": "response",
                "output": [],
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "total_tokens": 3,
                },
            }
        finally:
            self.active -= 1


class _SimulatedUsageReader:
    """Read-only aggregate surface backed by simulated node completions."""

    configured = True

    def __init__(self, nodes: list[_SimulatedNode]) -> None:
        self.nodes = nodes

    async def aggregate(self, *, hours: int) -> list[dict[str, object]]:
        assert hours == 1
        return [
            {
                "node_id": node.node_id,
                "model": f"{node.node_id}-qwen",
                "request_count": node.usage_count,
                "prompt_tokens": node.usage_count * 2,
                "completion_tokens": node.usage_count,
                "total_tokens": node.usage_count * 3,
                "avg_response_ms": 1.0,
            }
            for node in self.nodes
            if node.usage_count
        ]


def _listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    return listener


async def _wait_for_servers(
    servers: list[uvicorn.Server],
    tasks: list[asyncio.Task[None]],
) -> None:
    async with asyncio.timeout(5):
        while not all(server.started for server in servers):
            failed = [task for task in tasks if task.done()]
            if failed:
                await asyncio.gather(*failed)
                raise AssertionError("a loopback acceptance server stopped early")
            await asyncio.sleep(0.01)


async def _stop_servers(
    servers: list[uvicorn.Server],
    tasks: list[asyncio.Task[None]],
) -> None:
    for server in servers:
        server.should_exit = True
    done, pending = await asyncio.wait(tasks, timeout=5)
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)


async def _release_after_both_nodes_enter(
    nodes: list[_SimulatedNode],
    release_responses: asyncio.Event,
) -> None:
    await asyncio.gather(*(node.entered.wait() for node in nodes))
    release_responses.set()


async def test_simulated_loopback_acceptance_cli_fans_out_and_counts_usage(
    tmp_path,
    monkeypatch,
) -> None:
    """Exercise the real acceptance process without claiming hardware proof."""

    identity_value, deployment_id = identity(capabilities=("responses",))
    release_responses = asyncio.Event()
    nodes = [
        _SimulatedNode(
            node_id="mac-node",
            platform="macos",
            snapshot_key="snapshot-mac",
            inference_key="inference-mac",
            identity_value=identity_value,
            deployment_id=deployment_id,
            release_responses=release_responses,
        ),
        _SimulatedNode(
            node_id="cuda-node",
            platform="cuda",
            snapshot_key="snapshot-cuda",
            inference_key="inference-cuda",
            identity_value=identity_value,
            deployment_id=deployment_id,
            release_responses=release_responses,
        ),
    ]
    listeners = [_listener(), _listener(), _listener()]
    ports = [int(listener.getsockname()[1]) for listener in listeners]
    database_path = tmp_path / "fleet.db"
    config = FleetConfig(
        server=ServerConfig(
            host="127.0.0.1",
            port=ports[2],
            api_key="local-client-key",
            admin_api_key="local-admin-key",
            database_path=database_path,
            request_timeout_seconds=10,
            max_body_bytes=1024 * 1024,
            route_history_limit=100,
            poll_interval_seconds=0.05,
            snapshot_ttl_seconds=2,
        ),
        nodes=tuple(
            NodeConfig(
                node_id=node.node_id,
                url=f"http://127.0.0.1:{ports[index]}",
                fleet_token=node.snapshot_key,
                inference_token=node.inference_key,
            )
            for index, node in enumerate(nodes)
        ),
        models=(
            ModelConfig(
                name="local-qwen",
                deployment_id=deployment_id,
                capabilities=frozenset({"responses"}),
                queue_depth=2,
                queue_timeout_seconds=2,
            ),
        ),
        ledger=LedgerConfig(dsn="postgresql://simulated.invalid/ledger"),
    )
    usage = _SimulatedUsageReader(nodes)
    monkeypatch.setattr(
        fleet_app_module,
        "UsageReader",
        lambda _dsn: usage,
    )
    gateway = fleet_app_module.create_app(config)

    servers = [
        uvicorn.Server(
            uvicorn.Config(
                node.app,
                access_log=False,
                log_level="warning",
                timeout_graceful_shutdown=1,
            )
        )
        for node in nodes
    ]
    servers.append(
        uvicorn.Server(
            uvicorn.Config(
                gateway,
                access_log=False,
                log_level="warning",
                timeout_graceful_shutdown=1,
            )
        )
    )
    server_tasks = [
        asyncio.create_task(server.serve(sockets=[listener]))
        for server, listener in zip(servers, listeners, strict=True)
    ]
    release_task = asyncio.create_task(
        _release_after_both_nodes_enter(nodes, release_responses)
    )
    process: asyncio.subprocess.Process | None = None
    try:
        await _wait_for_servers(servers, server_tasks)
        async with asyncio.timeout(5):
            while len(gateway.state.registry.live_records()) != 2:
                await asyncio.sleep(0.01)

        environment = dict(os.environ)
        environment["MNEMOSYNE_FLEET_CLIENT_KEY"] = "local-client-key"
        environment["MNEMOSYNE_FLEET_ADMIN_KEY"] = "local-admin-key"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "fleet_acceptance.py"),
            "--url",
            f"http://127.0.0.1:{ports[2]}",
            "--model",
            "local-qwen",
            "--endpoint",
            "/v1/responses",
            "--requests",
            "2",
            "--min-eligible-nodes",
            "2",
            "--min-routed-nodes",
            "2",
            "--require-node",
            "mac-node",
            "--require-node",
            "cuda-node",
            "--require-platform",
            "macos",
            "--require-platform",
            "cuda",
            "--metadata-timeout",
            "5",
            "--usage-timeout",
            "5",
            "--usage-hours",
            "1",
            "--request-timeout",
            "10",
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=20,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise AssertionError("the simulated Fleet acceptance CLI timed out")

        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        result = json.loads(stdout)
        assert result["status"] == "passed"
        assert result["eligible_nodes"] == ["cuda-node", "mac-node"]
        assert result["eligible_platforms"] == ["cuda", "macos"]
        assert result["routed_nodes"] == ["cuda-node", "mac-node"]
        assert result["routed_platforms"] == ["cuda", "macos"]
        assert result["http_statuses"] == [200, 200]
        assert result["usage_increment"] == {
            "cuda-node": 1,
            "mac-node": 1,
        }
        assert {node.node_id: node.peak_active for node in nodes} == {
            "mac-node": 1,
            "cuda-node": 1,
        }

        with sqlite3.connect(database_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(routes)"
                ).fetchall()
            }
            routes = connection.execute(
                """
                SELECT node_id,
                       public_model,
                       endpoint,
                       status_code,
                       failure_code
                FROM routes
                ORDER BY node_id
                """
            ).fetchall()
            database_dump = "\n".join(connection.iterdump())

        assert routes == [
            ("cuda-node", "local-qwen", "/v1/responses", 200, None),
            ("mac-node", "local-qwen", "/v1/responses", 200, None),
        ]
        assert columns.isdisjoint({"prompt", "output", "request", "response"})
        for forbidden_value in (
            "Reply with OK.",
            "mac-node-response",
            "cuda-node-response",
            "local-client-key",
            "local-admin-key",
            "snapshot-mac",
            "snapshot-cuda",
            "inference-mac",
            "inference-cuda",
            "postgresql://simulated.invalid/ledger",
        ):
            assert forbidden_value not in database_dump
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        release_responses.set()
        release_task.cancel()
        await asyncio.gather(release_task, return_exceptions=True)
        await _stop_servers(servers, server_tasks)
        for listener in listeners:
            listener.close()
