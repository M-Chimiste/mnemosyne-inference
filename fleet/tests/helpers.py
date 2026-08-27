from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from mnemosyne_fleet.config import (
    FleetConfig,
    LedgerConfig,
    ModelConfig,
    NodeConfig,
    ServerConfig,
)


def identity(
    *,
    capabilities: tuple[str, ...] = (
        "chat/completions",
        "completions",
        "responses",
    ),
    revision: str | None = "a" * 40,
    content_digest: str | None = None,
    quantization: str | None = "Q4_K_M",
) -> tuple[dict[str, object], str]:
    value: dict[str, object] = {
        "protocol": 1,
        "engine": "llama.cpp",
        "upstream_model": "org/qwen-coder",
        "resolved_revision": revision,
        "artifact": {
            "format": "gguf",
            "selected_files": ["model.Q4_K_M.gguf"],
            "quantization": quantization,
            "content_digest": content_digest,
        },
        "kind": "language",
        "capabilities": list(capabilities),
        "load_config_digest": "sha256:" + "b" * 64,
    }
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return value, "sha256:" + hashlib.sha256(canonical).hexdigest()


def capacity(
    *,
    limit: int = 2,
    active: int = 0,
    queued: int = 0,
    available: int | None = None,
) -> dict[str, object]:
    return {
        "derived_limit": limit,
        "configured_max_concurrency": None,
        "effective_limit": limit,
        "active": active,
        "queued": queued,
        "available": max(0, limit - active) if available is None else available,
        "source": "test",
        "confidence": "authoritative",
        "saturation": active / limit,
    }


def snapshot_payload(
    node_id: str,
    *,
    sequence: int = 1,
    instance_id: str = "instance-1",
    warm: bool = True,
    alias: str | None = None,
    deployment_id: str | None = None,
    identity_value: dict[str, object] | None = None,
    accepting: bool = True,
    queue_depth: int = 0,
    queue_limit: int = 8,
    deployment_capacity: dict[str, object] | None = None,
    queued_by_deployment: dict[str, int] | None = None,
) -> dict[str, object]:
    if identity_value is None or deployment_id is None:
        identity_value, deployment_id = identity()
    local_alias = alias or f"{node_id}-qwen"
    queued_by_deployment = (
        ({deployment_id: queue_depth} if queue_depth else {})
        if queued_by_deployment is None
        else queued_by_deployment
    )
    top_capacity = capacity(
        queued=queue_depth,
        available=None if accepting and warm else 0,
    )
    return {
        "schema_version": 1,
        "snapshot_sequence": sequence,
        "observed_at": time.time(),
        "node": {
            "node_id": node_id,
            "instance_id": instance_id,
            "platform": "cuda",
            "version": "test",
        },
        "health": {
            "state": "ready" if accepting and warm else (
                "idle" if accepting else "draining"
            ),
            "accepting": accepting,
            "authoritative": True,
            "diagnostic_code": None,
        },
        "residency": {
            "alias": local_alias if warm else None,
            "deployment_id": deployment_id if warm else None,
            "engine": "llama.cpp" if warm else None,
            "epoch": 1,
            "transition_target": None,
        },
        "admission": {
            "queue_depth": queue_depth,
            "queue_limit": queue_limit,
            "queued_by_deployment": queued_by_deployment,
        },
        "capacity": top_capacity,
        "deployments": [
            {
                "alias": local_alias,
                "deployment_id": deployment_id,
                "identity": identity_value,
                "identity_confidence": "authoritative",
                "fleet_eligible": True,
                "loadable": True,
                "warm": warm,
                "capacity": deployment_capacity
                or capacity(queued=queued_by_deployment.get(deployment_id, 0)),
            }
        ],
        "usage_delivery": {
            "enabled": True,
            "writer_ready": True,
            "outbox_pending": 0,
            "last_flush_at": None,
            "last_error_code": None,
        },
    }


def fleet_config(
    tmp_path: Path,
    *,
    nodes: tuple[NodeConfig, ...] | None = None,
    queue_depth: int = 4,
    queue_timeout_seconds: float = 0.2,
) -> FleetConfig:
    _, deployment_id = identity()
    nodes = nodes or (
        NodeConfig(
            node_id="node-a",
            url="http://node-a",
            fleet_token="fleet-a",
            inference_token="infer-a",
        ),
    )
    return FleetConfig(
        server=ServerConfig(
            host="127.0.0.1",
            port=17400,
            api_key="client-key",
            admin_api_key="admin-key",
            database_path=tmp_path / "fleet.db",
            request_timeout_seconds=30,
            max_body_bytes=1024 * 1024,
            route_history_limit=1000,
            poll_interval_seconds=0.05,
            snapshot_ttl_seconds=1,
        ),
        nodes=nodes,
        models=(
            ModelConfig(
                name="qwen-coder",
                deployment_id=deployment_id,
                capabilities=frozenset(
                    {"chat/completions", "completions", "responses"}
                ),
                queue_depth=queue_depth,
                queue_timeout_seconds=queue_timeout_seconds,
            ),
        ),
        ledger=LedgerConfig(),
    )
