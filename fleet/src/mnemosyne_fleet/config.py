from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CAPABILITIES = frozenset(
    {
        "chat/completions",
        "completions",
        "embeddings",
        "images/generations",
        "messages",
        "rerank",
        "responses",
    }
)


class ConfigError(ValueError):
    """Raised when Fleet configuration is unsafe or incomplete."""


def _bounded_int(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _positive_float(value: Any, *, name: str, maximum: float) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise ConfigError(f"{name} must be a finite number")
    if parsed <= 0 or parsed > maximum:
        raise ConfigError(f"{name} must be greater than zero and at most {maximum}")
    return parsed


def _secret(env: dict[str, str], name: Any, *, field_name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{field_name} must name an environment variable")
    value = env.get(name)
    if value is None or not value.strip():
        raise ConfigError(f"environment variable {name!r} is required")
    return value


def _safe_base_url(raw: Any, *, node_id: str) -> str:
    if not isinstance(raw, str):
        raise ConfigError(f"nodes.{node_id}.url must be a string")
    value = raw.rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"nodes.{node_id}.url must be an http(s) URL without credentials, query, or fragment"
        )
    return value


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str
    port: int
    api_key: str = field(repr=False)
    admin_api_key: str = field(repr=False)
    database_path: Path
    request_timeout_seconds: float
    max_body_bytes: int
    route_history_limit: int
    poll_interval_seconds: float
    snapshot_ttl_seconds: float


@dataclass(frozen=True, slots=True)
class NodeConfig:
    node_id: str
    url: str
    fleet_token: str = field(repr=False)
    inference_token: str = field(repr=False)
    routing_weight: float | None = None


@dataclass(frozen=True, slots=True)
class ModelConfig:
    name: str
    deployment_id: str
    capabilities: frozenset[str]
    queue_depth: int
    queue_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class LedgerConfig:
    dsn: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class FleetConfig:
    server: ServerConfig
    nodes: tuple[NodeConfig, ...]
    models: tuple[ModelConfig, ...]
    ledger: LedgerConfig


def _expect_list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ConfigError(f"{key} must be an array of tables")
    return value


def load_config(
    path: str | os.PathLike[str],
    *,
    environ: dict[str, str] | None = None,
) -> FleetConfig:
    env = dict(os.environ if environ is None else environ)
    config_path = Path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    server_raw = raw.get("server")
    if not isinstance(server_raw, dict):
        raise ConfigError("[server] is required")

    database_path = Path(
        str(server_raw.get("database_path", "/var/lib/mnemosyne-fleet/fleet.db"))
    ).expanduser()
    server = ServerConfig(
        host=str(server_raw.get("host", "127.0.0.1")),
        port=_bounded_int(
            server_raw.get("port", 17400),
            name="server.port",
            minimum=1,
            maximum=65535,
        ),
        api_key=_secret(
            env,
            server_raw.get("api_key_env"),
            field_name="server.api_key_env",
        ),
        admin_api_key=_secret(
            env,
            server_raw.get("admin_api_key_env"),
            field_name="server.admin_api_key_env",
        ),
        database_path=database_path,
        request_timeout_seconds=_positive_float(
            server_raw.get("request_timeout_seconds", 300),
            name="server.request_timeout_seconds",
            maximum=86_400,
        ),
        max_body_bytes=_bounded_int(
            server_raw.get("max_body_bytes", 16 * 1024 * 1024),
            name="server.max_body_bytes",
            minimum=1024,
            maximum=256 * 1024 * 1024,
        ),
        route_history_limit=_bounded_int(
            server_raw.get("route_history_limit", 10_000),
            name="server.route_history_limit",
            minimum=100,
            maximum=1_000_000,
        ),
        poll_interval_seconds=_positive_float(
            server_raw.get("poll_interval_seconds", 2),
            name="server.poll_interval_seconds",
            maximum=300,
        ),
        snapshot_ttl_seconds=_positive_float(
            server_raw.get("snapshot_ttl_seconds", 10),
            name="server.snapshot_ttl_seconds",
            maximum=3600,
        ),
    )
    if server.snapshot_ttl_seconds <= server.poll_interval_seconds:
        raise ConfigError(
            "server.snapshot_ttl_seconds must exceed server.poll_interval_seconds"
        )

    nodes: list[NodeConfig] = []
    seen_nodes: set[str] = set()
    for index, node_raw in enumerate(_expect_list(raw, "nodes")):
        node_id = node_raw.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip() or len(node_id) > 128:
            raise ConfigError(f"nodes[{index}].node_id is invalid")
        if node_id in seen_nodes:
            raise ConfigError(f"duplicate node_id {node_id!r}")
        seen_nodes.add(node_id)
        weight_raw = node_raw.get("routing_weight")
        weight = (
            None
            if weight_raw is None
            else _positive_float(
                weight_raw,
                name=f"nodes.{node_id}.routing_weight",
                maximum=100_000,
            )
        )
        fleet_token = _secret(
            env,
            node_raw.get("fleet_token_env"),
            field_name=f"nodes.{node_id}.fleet_token_env",
        )
        inference_token = _secret(
            env,
            node_raw.get("inference_token_env"),
            field_name=f"nodes.{node_id}.inference_token_env",
        )
        nodes.append(
            NodeConfig(
                node_id=node_id,
                url=_safe_base_url(node_raw.get("url"), node_id=node_id),
                fleet_token=fleet_token,
                inference_token=inference_token,
                routing_weight=weight,
            )
        )
    if not nodes:
        raise ConfigError("at least one [[nodes]] enrollment is required")
    credential_values = [server.api_key, server.admin_api_key]
    credential_values.extend(
        credential
        for node in nodes
        for credential in (node.fleet_token, node.inference_token)
    )
    for index, credential in enumerate(credential_values):
        if any(
            hmac_compare(credential, prior)
            for prior in credential_values[:index]
        ):
            # Do not identify either role (or its environment variable) in
            # the error: configuration diagnostics must not turn credential
            # equality into an oracle.
            raise ConfigError("all configured credentials must be distinct")

    models: list[ModelConfig] = []
    seen_models: set[str] = set()
    for index, model_raw in enumerate(_expect_list(raw, "models")):
        name = model_raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise ConfigError(f"models[{index}].name is invalid")
        if name in seen_models:
            raise ConfigError(f"duplicate model name {name!r}")
        seen_models.add(name)
        deployment_id = model_raw.get("deployment_id")
        if (
            not isinstance(deployment_id, str)
            or not deployment_id.startswith("sha256:")
            or len(deployment_id) != 71
        ):
            raise ConfigError(
                f"models.{name}.deployment_id must be sha256:<64 lowercase hex>"
            )
        try:
            int(deployment_id[7:], 16)
        except ValueError as exc:
            raise ConfigError(f"models.{name}.deployment_id is invalid") from exc
        if deployment_id != deployment_id.lower():
            raise ConfigError(f"models.{name}.deployment_id must be lowercase")
        capabilities_raw = model_raw.get("capabilities")
        if (
            not isinstance(capabilities_raw, list)
            or not capabilities_raw
            or any(not isinstance(value, str) or not value for value in capabilities_raw)
        ):
            raise ConfigError(f"models.{name}.capabilities must be a non-empty string array")
        capabilities = frozenset(capabilities_raw)
        if len(capabilities) != len(capabilities_raw):
            raise ConfigError(f"models.{name}.capabilities must be unique")
        if not capabilities.issubset(CAPABILITIES):
            raise ConfigError(f"models.{name}.capabilities contains an unsupported route")
        models.append(
            ModelConfig(
                name=name,
                deployment_id=deployment_id,
                capabilities=capabilities,
                queue_depth=_bounded_int(
                    model_raw.get("queue_depth", 128),
                    name=f"models.{name}.queue_depth",
                    minimum=0,
                    maximum=100_000,
                ),
                queue_timeout_seconds=_positive_float(
                    model_raw.get("queue_timeout_seconds", 120),
                    name=f"models.{name}.queue_timeout_seconds",
                    maximum=86_400,
                ),
            )
        )
    if not models:
        raise ConfigError("at least one [[models]] mapping is required")

    ledger_raw = raw.get("ledger", {})
    if not isinstance(ledger_raw, dict):
        raise ConfigError("[ledger] must be a table")
    dsn_env = ledger_raw.get("dsn_env")
    dsn = None if dsn_env is None else _secret(env, dsn_env, field_name="ledger.dsn_env")

    return FleetConfig(
        server=server,
        nodes=tuple(nodes),
        models=tuple(models),
        ledger=LedgerConfig(dsn=dsn),
    )


def hmac_compare(left: str, right: str) -> bool:
    # Kept local so validation does not accidentally include either value in
    # an error or diagnostic.
    import hmac

    return hmac.compare_digest(left, right)
