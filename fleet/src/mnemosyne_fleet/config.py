from __future__ import annotations

import base64
import binascii
import ipaddress
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from .locator_policy import LocatorTransport, TRANSPORTS


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

ServiceClass = Literal["primary", "opportunistic", "overflow"]
EnrollmentSource = Literal["static", "paired"]
SERVICE_CLASSES: tuple[ServiceClass, ...] = (
    "primary",
    "opportunistic",
    "overflow",
)

_CATALOG_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
_CATALOG_HOST_LABEL = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_SAFE_CATALOG_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$"
)
_MAX_CATALOG_SEQUENCE = 9_007_199_254_740_991
_MAX_TIMESTAMP = 4_102_444_800


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


def _strict_bounded_int(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


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


def _bounded_float(
    value: Any,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a finite number") from exc
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise ConfigError(
            f"{name} must be between {minimum:g} and {maximum:g}"
        )
    return parsed


def _cidr_allowlist(value: Any, *, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 64:
        raise ConfigError(f"{name} must be an array of at most 64 CIDRs")
    networks: list[str] = []
    seen: set[tuple[int, int, int]] = set()
    for raw_network in value:
        if not isinstance(raw_network, str) or not raw_network:
            raise ConfigError(f"{name} must contain canonical CIDR strings")
        try:
            network = ipaddress.ip_network(raw_network, strict=True)
        except ValueError as exc:
            raise ConfigError(f"{name} must contain canonical CIDR strings") from exc
        if str(network) != raw_network:
            raise ConfigError(f"{name} must contain canonical CIDR strings")
        key = (network.version, int(network.network_address), network.prefixlen)
        if key in seen:
            raise ConfigError(f"{name} must contain unique CIDRs")
        seen.add(key)
        networks.append(raw_network)
    return tuple(networks)


def _port_allowlist(value: Any, *, name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value or len(value) > 32:
        raise ConfigError(f"{name} must be a non-empty array of at most 32 ports")
    ports: list[int] = []
    seen: set[int] = set()
    for raw_port in value:
        if isinstance(raw_port, bool) or not isinstance(raw_port, int):
            raise ConfigError(f"{name} must contain integer ports")
        if not 1 <= raw_port <= 65535:
            raise ConfigError(f"{name} ports must be between 1 and 65535")
        if raw_port in seen:
            raise ConfigError(f"{name} must contain unique ports")
        seen.add(raw_port)
        ports.append(raw_port)
    return tuple(ports)


def _secret(env: dict[str, str], name: Any, *, field_name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{field_name} must name an environment variable")
    value = env.get(name)
    if value is None or not value.strip():
        raise ConfigError(f"environment variable {name!r} is required")
    return value


def _pairing_master_key(
    env: dict[str, str],
    name: Any,
    *,
    field_name: str,
) -> str:
    value = _secret(env, name, field_name=field_name).strip()
    if len(value) not in {43, 44} or (len(value) == 44 and not value.endswith("=")):
        raise ConfigError("pairing master key must be canonical 32-byte base64url")
    try:
        decoded = base64.b64decode(
            value + ("=" if len(value) == 43 else ""),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        decoded = b""
    if len(decoded) != 32:
        raise ConfigError("pairing master key must be canonical 32-byte base64url")
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


def _safe_https_origin(raw: Any, *, field_name: str) -> str:
    if not isinstance(raw, str) or len(raw) > 2048:
        raise ConfigError(f"{field_name} must be a bounded HTTPS origin")
    value = raw.rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{field_name} must be an HTTPS origin without credentials, path, query, or fragment"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{field_name} contains an invalid port") from exc
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"https://{host}{f':{port}' if port is not None else ''}"


def _catalog_update_origin(raw: Any) -> str:
    if (
        not isinstance(raw, str)
        or not raw.isascii()
        or not 1 <= len(raw) <= 512
    ):
        raise ConfigError("catalog.update_origin must be a canonical HTTPS origin")
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError:
        raise ConfigError(
            "catalog.update_origin must be a canonical HTTPS origin"
        ) from None
    hostname = parsed.hostname
    labels = [] if hostname is None else hostname.split(".")
    if (
        parsed.scheme != "https"
        or hostname is None
        or hostname != hostname.lower()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != ""
        or parsed.query != ""
        or parsed.fragment != ""
        or port == 443
        or not 1 <= len(hostname) <= 253
        or hostname.endswith(".")
        or not labels
        or any(_CATALOG_HOST_LABEL.fullmatch(label) is None for label in labels)
    ):
        raise ConfigError("catalog.update_origin must be a canonical HTTPS origin")
    canonical = f"https://{hostname}"
    if port is not None:
        canonical += f":{port}"
    if raw != canonical:
        raise ConfigError("catalog.update_origin must be a canonical HTTPS origin")
    return canonical


def _catalog_update_path(raw: Any) -> str:
    if (
        not isinstance(raw, str)
        or not raw.isascii()
        or not 1 <= len(raw) <= 512
        or not raw.startswith("/")
        or raw.endswith("/")
        or "//" in raw
        or "\\" in raw
        or "?" in raw
        or "#" in raw
        or "%" in raw
    ):
        raise ConfigError("catalog.update_path must be a canonical absolute path")
    segments = raw[1:].split("/")
    if any(
        segment in {"", ".", ".."}
        or _CATALOG_PATH_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise ConfigError("catalog.update_path must be a canonical absolute path")
    return raw


def _catalog_public_key(raw: Any, *, key_id: str) -> str:
    if not isinstance(raw, str) or len(raw) != 43 or not raw.isascii():
        raise ConfigError(f"catalog trusted key {key_id!r} is invalid")
    try:
        decoded = base64.b64decode(
            raw + "=",
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        decoded = b""
    if (
        len(decoded) != 32
        or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != raw
    ):
        raise ConfigError(f"catalog trusted key {key_id!r} is invalid")
    return raw


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
    service_class: ServiceClass = "primary"
    source: EnrollmentSource = "static"
    enrollment_id: str = ""
    locator_transport: LocatorTransport | None = None

    def __post_init__(self) -> None:
        if self.source not in {"static", "paired"}:
            raise ValueError("node enrollment source must be static or paired")
        enrollment_id = self.enrollment_id
        if self.source == "static":
            if self.locator_transport is not None:
                raise ValueError(
                    "static enrollment must not carry paired locator transport"
                )
            enrollment_id = enrollment_id or self.node_id
            if enrollment_id != self.node_id:
                raise ValueError(
                    "static enrollment identity must equal its reporting node ID"
                )
        else:
            if (
                not enrollment_id
                or enrollment_id != enrollment_id.strip()
                or len(enrollment_id) > 128
                or any(ord(character) < 33 for character in enrollment_id)
            ):
                raise ValueError("node enrollment identity is invalid")
            if self.locator_transport not in TRANSPORTS:
                raise ValueError(
                    "paired enrollment requires a valid locator transport"
                )
        object.__setattr__(self, "enrollment_id", enrollment_id)

    @property
    def reporting_node_id(self) -> str:
        """Snapshot-v1 and token-ledger identity retained across pairing."""

        return self.node_id


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
class PairingConfig:
    enabled: bool = False
    public_origin: str | None = None
    metadata_database_path: Path | None = None
    secret_database_path: Path | None = None
    inventory_database_path: Path | None = None
    secret_store_id: str = "mnemosyne-fleet-pairing-v1"
    master_key: str | None = field(default=None, repr=False)
    activation_timeout_seconds: float = 15.0
    https_cidr_allowlist: tuple[str, ...] = ()
    tailscale_cidr_allowlist: tuple[str, ...] = ()
    trusted_lan_http_cidr_allowlist: tuple[str, ...] = ()
    allowed_node_ports: tuple[int, ...] = (1240,)
    dns_resolution_timeout_seconds: float = 5.0
    inventory_ttl_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class CatalogTrustKeyConfig:
    key_id: str
    public_key: str = field(repr=False)
    valid_from: int = 0
    valid_until: int = _MAX_TIMESTAMP
    minimum_catalog_sequence: int = 1
    maximum_catalog_sequence: int = _MAX_CATALOG_SEQUENCE


@dataclass(frozen=True, slots=True)
class CatalogConfig:
    enabled: bool = False
    state_directory: Path | None = None
    update_origin: str | None = None
    update_path: str | None = None
    update_interval_seconds: float = 3600.0
    total_timeout_seconds: float = 20.0
    connect_timeout_seconds: float = 5.0
    max_attempts: int = 2
    retry_delay_seconds: float = 0.0
    trusted_keys: tuple[CatalogTrustKeyConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class PlacementConfig:
    remote_installs_enabled: bool = False
    recommendation_valid_seconds: int = 60
    desired_install_database_path: Path | None = None
    desired_install_valid_seconds: int = 900
    maximum_active_desired_installs: int = 1_000
    desired_install_history_limit: int = 10_000


@dataclass(frozen=True, slots=True)
class BatchConfig:
    """Bounded, process-local async batch execution.

    Batch inputs and results are intentionally never written to Fleet's
    metadata database.  A Hub restart therefore expires every batch.
    """

    enabled: bool = True
    max_active_jobs: int = 32
    max_requests_per_job: int = 256
    max_concurrency: int = 4
    max_result_bytes_per_item: int = 16 * 1024 * 1024
    max_retained_result_bytes: int = 256 * 1024 * 1024
    retention_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class FleetConfig:
    server: ServerConfig
    nodes: tuple[NodeConfig, ...]
    models: tuple[ModelConfig, ...]
    ledger: LedgerConfig
    pairing: PairingConfig = field(default_factory=PairingConfig)
    catalog: CatalogConfig = field(default_factory=CatalogConfig)
    placement: PlacementConfig = field(default_factory=PlacementConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)


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

    pairing_raw = raw.get("pairing", {})
    if not isinstance(pairing_raw, dict):
        raise ConfigError("[pairing] must be a table")
    pairing_enabled = pairing_raw.get("enabled", False)
    if not isinstance(pairing_enabled, bool):
        raise ConfigError("pairing.enabled must be a boolean")
    if pairing_enabled:
        public_origin = _safe_https_origin(
            pairing_raw.get("public_origin"),
            field_name="pairing.public_origin",
        )
        private_root = server.database_path.parent / "private"
        metadata_database_path = Path(
            str(
                pairing_raw.get(
                    "metadata_database_path",
                    private_root / "pairing-metadata.db",
                )
            )
        ).expanduser()
        secret_database_path = Path(
            str(
                pairing_raw.get(
                    "secret_database_path",
                    private_root / "pairing-secrets.db",
                )
            )
        ).expanduser()
        inventory_database_path = Path(
            str(
                pairing_raw.get(
                    "inventory_database_path",
                    server.database_path.parent / "mac-inventory.db",
                )
            )
        ).expanduser()
        if len(
            {
                server.database_path,
                metadata_database_path,
                secret_database_path,
                inventory_database_path,
            }
        ) != 4:
            raise ConfigError(
                "Fleet, pairing metadata, pairing secret, and inventory "
                "databases must differ"
            )
        secret_store_id = pairing_raw.get(
            "secret_store_id",
            "mnemosyne-fleet-pairing-v1",
        )
        if (
            not isinstance(secret_store_id, str)
            or not secret_store_id.strip()
            or len(secret_store_id.encode("utf-8")) > 128
        ):
            raise ConfigError("pairing.secret_store_id is invalid")
        pairing = PairingConfig(
            enabled=True,
            public_origin=public_origin,
            metadata_database_path=metadata_database_path,
            secret_database_path=secret_database_path,
            inventory_database_path=inventory_database_path,
            secret_store_id=secret_store_id,
            master_key=_pairing_master_key(
                env,
                pairing_raw.get("master_key_env"),
                field_name="pairing.master_key_env",
            ),
            activation_timeout_seconds=_positive_float(
                pairing_raw.get("activation_timeout_seconds", 15),
                name="pairing.activation_timeout_seconds",
                maximum=120,
            ),
            https_cidr_allowlist=_cidr_allowlist(
                pairing_raw.get("https_cidr_allowlist"),
                name="pairing.https_cidr_allowlist",
            ),
            tailscale_cidr_allowlist=_cidr_allowlist(
                pairing_raw.get("tailscale_cidr_allowlist"),
                name="pairing.tailscale_cidr_allowlist",
            ),
            trusted_lan_http_cidr_allowlist=_cidr_allowlist(
                pairing_raw.get("trusted_lan_http_cidr_allowlist"),
                name="pairing.trusted_lan_http_cidr_allowlist",
            ),
            allowed_node_ports=_port_allowlist(
                pairing_raw.get("allowed_node_ports", [1240]),
                name="pairing.allowed_node_ports",
            ),
            dns_resolution_timeout_seconds=_positive_float(
                pairing_raw.get("dns_resolution_timeout_seconds", 5),
                name="pairing.dns_resolution_timeout_seconds",
                maximum=30,
            ),
            inventory_ttl_seconds=_positive_float(
                pairing_raw.get("inventory_ttl_seconds", 60),
                name="pairing.inventory_ttl_seconds",
                maximum=300,
            ),
        )
        if pairing.inventory_ttl_seconds < 15:
            raise ConfigError(
                "pairing.inventory_ttl_seconds must be at least 15"
            )
    else:
        pairing = PairingConfig()

    catalog_raw = raw.get("catalog", {})
    if not isinstance(catalog_raw, dict):
        raise ConfigError("[catalog] must be a table")
    catalog_enabled = catalog_raw.get("enabled", False)
    if not isinstance(catalog_enabled, bool):
        raise ConfigError("catalog.enabled must be a boolean")
    allowed_catalog_fields = {
        "enabled",
        "state_directory",
        "update_origin",
        "update_path",
        "update_interval_seconds",
        "total_timeout_seconds",
        "connect_timeout_seconds",
        "max_attempts",
        "retry_delay_seconds",
        "trusted_keys",
    }
    if set(catalog_raw) - allowed_catalog_fields:
        raise ConfigError("[catalog] contains unsupported fields")
    if not catalog_enabled and set(catalog_raw) - {"enabled"}:
        raise ConfigError("catalog settings require catalog.enabled")
    if catalog_enabled:
        state_directory_raw = catalog_raw.get(
            "state_directory",
            server.database_path.parent / "private" / "compatibility-catalog",
        )
        if not isinstance(state_directory_raw, (str, os.PathLike)):
            raise ConfigError("catalog.state_directory must be a path")
        state_directory = Path(state_directory_raw).expanduser()
        forbidden_state_directories = {
            Path("/"),
            Path.home(),
            server.database_path.parent,
            server.database_path,
            pairing.metadata_database_path,
            pairing.secret_database_path,
            pairing.inventory_database_path,
        }
        forbidden_state_directories.update(
            path.parent
            for path in (
                pairing.metadata_database_path,
                pairing.secret_database_path,
                pairing.inventory_database_path,
            )
            if path is not None
        )
        if (
            not state_directory.is_absolute()
            or state_directory in forbidden_state_directories
        ):
            raise ConfigError("catalog.state_directory must be a dedicated directory")

        trusted_raw = catalog_raw.get("trusted_keys")
        if (
            not isinstance(trusted_raw, list)
            or not trusted_raw
            or len(trusted_raw) > 8
            or any(not isinstance(item, dict) for item in trusted_raw)
        ):
            raise ConfigError(
                "catalog.trusted_keys must contain between 1 and 8 public keys"
            )
        trusted_keys: list[CatalogTrustKeyConfig] = []
        seen_key_ids: set[str] = set()
        allowed_key_fields = {
            "key_id",
            "public_key",
            "valid_from",
            "valid_until",
            "minimum_catalog_sequence",
            "maximum_catalog_sequence",
        }
        for index, key_raw in enumerate(trusted_raw):
            if set(key_raw) - allowed_key_fields:
                raise ConfigError(
                    f"catalog.trusted_keys[{index}] contains unsupported fields"
                )
            key_id = key_raw.get("key_id")
            if (
                not isinstance(key_id, str)
                or _SAFE_CATALOG_IDENTIFIER.fullmatch(key_id) is None
                or key_id in seen_key_ids
            ):
                raise ConfigError(f"catalog.trusted_keys[{index}].key_id is invalid")
            seen_key_ids.add(key_id)
            valid_from = _strict_bounded_int(
                key_raw.get("valid_from", 0),
                name=f"catalog.trusted_keys.{key_id}.valid_from",
                minimum=0,
                maximum=_MAX_TIMESTAMP,
            )
            valid_until = _strict_bounded_int(
                key_raw.get("valid_until", _MAX_TIMESTAMP),
                name=f"catalog.trusted_keys.{key_id}.valid_until",
                minimum=0,
                maximum=_MAX_TIMESTAMP,
            )
            minimum_sequence = _strict_bounded_int(
                key_raw.get("minimum_catalog_sequence", 1),
                name=(
                    f"catalog.trusted_keys.{key_id}.minimum_catalog_sequence"
                ),
                minimum=1,
                maximum=_MAX_CATALOG_SEQUENCE,
            )
            maximum_sequence = _strict_bounded_int(
                key_raw.get(
                    "maximum_catalog_sequence",
                    _MAX_CATALOG_SEQUENCE,
                ),
                name=(
                    f"catalog.trusted_keys.{key_id}.maximum_catalog_sequence"
                ),
                minimum=1,
                maximum=_MAX_CATALOG_SEQUENCE,
            )
            if valid_from >= valid_until or minimum_sequence > maximum_sequence:
                raise ConfigError(f"catalog trusted key {key_id!r} window is invalid")
            trusted_keys.append(
                CatalogTrustKeyConfig(
                    key_id=key_id,
                    public_key=_catalog_public_key(
                        key_raw.get("public_key"),
                        key_id=key_id,
                    ),
                    valid_from=valid_from,
                    valid_until=valid_until,
                    minimum_catalog_sequence=minimum_sequence,
                    maximum_catalog_sequence=maximum_sequence,
                )
            )
        total_timeout = _bounded_float(
            catalog_raw.get("total_timeout_seconds", 20),
            name="catalog.total_timeout_seconds",
            minimum=0.05,
            maximum=300,
        )
        connect_timeout = _bounded_float(
            catalog_raw.get("connect_timeout_seconds", 5),
            name="catalog.connect_timeout_seconds",
            minimum=0.05,
            maximum=300,
        )
        if connect_timeout > total_timeout:
            raise ConfigError(
                "catalog.connect_timeout_seconds must not exceed total timeout"
            )
        catalog = CatalogConfig(
            enabled=True,
            state_directory=state_directory,
            update_origin=_catalog_update_origin(
                catalog_raw.get("update_origin")
            ),
            update_path=_catalog_update_path(catalog_raw.get("update_path")),
            update_interval_seconds=_bounded_float(
                catalog_raw.get("update_interval_seconds", 3600),
                name="catalog.update_interval_seconds",
                minimum=60,
                maximum=86_400,
            ),
            total_timeout_seconds=total_timeout,
            connect_timeout_seconds=connect_timeout,
            max_attempts=_strict_bounded_int(
                catalog_raw.get("max_attempts", 2),
                name="catalog.max_attempts",
                minimum=1,
                maximum=3,
            ),
            retry_delay_seconds=_bounded_float(
                catalog_raw.get("retry_delay_seconds", 0),
                name="catalog.retry_delay_seconds",
                minimum=0,
                maximum=5,
            ),
            trusted_keys=tuple(trusted_keys),
        )
    else:
        catalog = CatalogConfig()

    placement_raw = raw.get("placement", {})
    if not isinstance(placement_raw, dict):
        raise ConfigError("[placement] must be a table")
    allowed_placement_fields = {
        "remote_installs_enabled",
        "recommendation_valid_seconds",
        "desired_install_database_path",
        "desired_install_valid_seconds",
        "maximum_active_desired_installs",
        "desired_install_history_limit",
    }
    if set(placement_raw) - allowed_placement_fields:
        raise ConfigError("[placement] contains unsupported fields")
    remote_installs_enabled = placement_raw.get(
        "remote_installs_enabled",
        False,
    )
    if not isinstance(remote_installs_enabled, bool):
        raise ConfigError("placement.remote_installs_enabled must be a boolean")
    desired_install_database_path: Path | None = None
    if remote_installs_enabled:
        raw_job_database = placement_raw.get(
            "desired_install_database_path",
            server.database_path.parent / "private" / "desired-installs.db",
        )
        if not isinstance(raw_job_database, (str, os.PathLike)):
            raise ConfigError(
                "placement.desired_install_database_path must be a path"
            )
        desired_install_database_path = Path(raw_job_database).expanduser()
        if not desired_install_database_path.is_absolute():
            raise ConfigError(
                "placement.desired_install_database_path must be absolute"
            )
        forbidden_job_paths = {
            server.database_path,
            pairing.metadata_database_path,
            pairing.secret_database_path,
            pairing.inventory_database_path,
        }
        if (
            desired_install_database_path in forbidden_job_paths
            or desired_install_database_path == catalog.state_directory
            or (
                catalog.state_directory is not None
                and catalog.state_directory
                in desired_install_database_path.parents
            )
            or desired_install_database_path.parent
            in {Path("/"), Path.home(), server.database_path.parent}
        ):
            raise ConfigError(
                "placement.desired_install_database_path must be a dedicated "
                "private database"
            )
    elif "desired_install_database_path" in placement_raw:
        raise ConfigError(
            "placement.desired_install_database_path requires "
            "placement.remote_installs_enabled"
        )
    placement = PlacementConfig(
        remote_installs_enabled=remote_installs_enabled,
        recommendation_valid_seconds=_strict_bounded_int(
            placement_raw.get("recommendation_valid_seconds", 60),
            name="placement.recommendation_valid_seconds",
            minimum=1,
            maximum=300,
        ),
        desired_install_database_path=desired_install_database_path,
        desired_install_valid_seconds=_strict_bounded_int(
            placement_raw.get("desired_install_valid_seconds", 900),
            name="placement.desired_install_valid_seconds",
            minimum=1,
            maximum=604_800,
        ),
        maximum_active_desired_installs=_strict_bounded_int(
            placement_raw.get("maximum_active_desired_installs", 1_000),
            name="placement.maximum_active_desired_installs",
            minimum=1,
            maximum=10_000,
        ),
        desired_install_history_limit=_strict_bounded_int(
            placement_raw.get("desired_install_history_limit", 10_000),
            name="placement.desired_install_history_limit",
            minimum=1,
            maximum=100_000,
        ),
    )
    if placement.remote_installs_enabled and not catalog.enabled:
        raise ConfigError(
            "placement.remote_installs_enabled requires catalog.enabled"
        )
    if placement.remote_installs_enabled and not pairing.enabled:
        raise ConfigError(
            "placement.remote_installs_enabled requires pairing.enabled"
        )

    batch_raw = raw.get("batch", {})
    if not isinstance(batch_raw, dict):
        raise ConfigError("[batch] must be a table")
    allowed_batch_fields = {
        "enabled",
        "max_active_jobs",
        "max_requests_per_job",
        "max_concurrency",
        "max_result_bytes_per_item",
        "max_retained_result_bytes",
        "retention_seconds",
    }
    if set(batch_raw) - allowed_batch_fields:
        raise ConfigError("[batch] contains unsupported fields")
    batch_enabled = batch_raw.get("enabled", True)
    if not isinstance(batch_enabled, bool):
        raise ConfigError("batch.enabled must be a boolean")
    batch = BatchConfig(
        enabled=batch_enabled,
        max_active_jobs=_strict_bounded_int(
            batch_raw.get("max_active_jobs", 32),
            name="batch.max_active_jobs",
            minimum=1,
            maximum=1_000,
        ),
        max_requests_per_job=_strict_bounded_int(
            batch_raw.get("max_requests_per_job", 256),
            name="batch.max_requests_per_job",
            minimum=1,
            maximum=10_000,
        ),
        max_concurrency=_strict_bounded_int(
            batch_raw.get("max_concurrency", 4),
            name="batch.max_concurrency",
            minimum=1,
            maximum=128,
        ),
        max_result_bytes_per_item=_strict_bounded_int(
            batch_raw.get("max_result_bytes_per_item", 16 * 1024 * 1024),
            name="batch.max_result_bytes_per_item",
            minimum=1_024,
            maximum=256 * 1024 * 1024,
        ),
        max_retained_result_bytes=_strict_bounded_int(
            batch_raw.get("max_retained_result_bytes", 256 * 1024 * 1024),
            name="batch.max_retained_result_bytes",
            minimum=1_024,
            maximum=1024 * 1024 * 1024,
        ),
        retention_seconds=_strict_bounded_int(
            batch_raw.get("retention_seconds", 3600),
            name="batch.retention_seconds",
            minimum=60,
            maximum=86_400,
        ),
    )
    if batch.max_result_bytes_per_item > batch.max_retained_result_bytes:
        raise ConfigError(
            "batch.max_result_bytes_per_item must not exceed retained capacity"
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
        service_class_raw = node_raw.get("service_class", "primary")
        if (
            not isinstance(service_class_raw, str)
            or service_class_raw not in SERVICE_CLASSES
        ):
            raise ConfigError(
                f"nodes.{node_id}.service_class must be one of "
                "primary, opportunistic, or overflow"
            )
        service_class = cast(ServiceClass, service_class_raw)
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
                service_class=service_class,
            )
        )
    if not nodes and not pairing.enabled:
        raise ConfigError(
            "at least one [[nodes]] enrollment or enabled [pairing] service is required"
        )
    credential_values = [server.api_key, server.admin_api_key]
    credential_values.extend(
        credential
        for node in nodes
        for credential in (node.fleet_token, node.inference_token)
    )
    if pairing.master_key is not None:
        credential_values.append(pairing.master_key)
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
        pairing=pairing,
        catalog=catalog,
        placement=placement,
        batch=batch,
    )


def hmac_compare(left: str, right: str) -> bool:
    # Kept local so validation does not accidentally include either value in
    # an error or diagnostic.
    import hmac

    return hmac.compare_digest(left, right)
