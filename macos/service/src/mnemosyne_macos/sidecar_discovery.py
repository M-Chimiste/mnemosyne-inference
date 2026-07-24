"""Migrate reporting settings from the previous local token sidecar.

The standalone sidecar's health response intentionally contains only liveness.
Its user LaunchAgent does, however, record the exact active config path in
``TOKEN_SIDECAR_CONFIG``.  Reading that path keeps Unified Inference aligned
with the same stable node ID while avoiding a machine name in this repo. During
migration, the LaunchAgent can also supply the secret Postgres DSN without
copying it into YAML or returning it through the control API.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import re
import socket
import tempfile
from typing import Mapping

import yaml


DEFAULT_SIDECAR_LAUNCH_AGENT = (
    Path.home() / "Library" / "LaunchAgents" / "com.athena.token-sidecar.plist"
)
LEGACY_AUTOMATIC_NODE_IDS = frozenset({"auto", "mnemosyne-mac"})
REPORTING_NODE_ID_ENV = "MNEMOSYNE_REPORTING_NODE_ID"


class ReportingMigrationError(RuntimeError):
    """Legacy values exist but could not be persisted safely."""


@dataclass(frozen=True, slots=True)
class ReportingIdentity:
    node_id: str
    source: str
    sidecar_config_path: Path | None = None


def _launch_agent_environment(launch_agent_path: Path) -> dict[str, str]:
    try:
        with launch_agent_path.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}
    if not isinstance(payload, dict):
        return {}

    launch_environment = payload.get("EnvironmentVariables")
    if not isinstance(launch_environment, dict):
        return {}
    return {
        key: value
        for key, value in launch_environment.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _effective_launch_agent_path(
    environment: Mapping[str, str],
    launch_agent_path: str | Path | None,
) -> Path:
    plist_override = environment.get(
        "MNEMOSYNE_TOKEN_SIDECAR_LAUNCH_AGENT", ""
    ).strip()
    return Path(
        launch_agent_path or plist_override or DEFAULT_SIDECAR_LAUNCH_AGENT
    ).expanduser()


def _sidecar_config_path(
    *,
    environment: Mapping[str, str],
    launch_agent_path: Path,
) -> Path | None:
    configured = environment.get("TOKEN_SIDECAR_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser()

    launch_environment = _launch_agent_environment(launch_agent_path)
    configured = launch_environment.get("TOKEN_SIDECAR_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser()

    try:
        with launch_agent_path.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    if not isinstance(payload, dict):
        return None

    working_directory = payload.get("WorkingDirectory")
    if isinstance(working_directory, str) and working_directory.strip():
        return Path(working_directory).expanduser() / "config.yaml"
    return None


def _node_id_from_sidecar_config(path: Path) -> str | None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(payload, dict):
        return None
    node = payload.get("node")
    if not isinstance(node, dict):
        return None
    value = node.get("id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _computer_node_id(hostname: str) -> str:
    value = hostname.strip().removesuffix(".local").casefold()
    value = re.sub(r"[^a-z0-9._-]+", "-", value).strip("-._")
    return value or "local"


def resolve_reporting_identity(
    configured_node_id: str | None,
    *,
    environment: Mapping[str, str] | None = None,
    launch_agent_path: str | Path | None = None,
    hostname: str | None = None,
) -> ReportingIdentity:
    """Resolve an explicit override, existing sidecar ID, or hostname fallback."""

    configured = (configured_node_id or "").strip()
    if configured and configured.casefold() not in LEGACY_AUTOMATIC_NODE_IDS:
        return ReportingIdentity(node_id=configured, source="configured")

    effective_environment = os.environ if environment is None else environment
    persisted = effective_environment.get(REPORTING_NODE_ID_ENV, "").strip()
    if persisted:
        return ReportingIdentity(node_id=persisted, source="token_sidecar")
    effective_launch_agent = _effective_launch_agent_path(
        effective_environment,
        launch_agent_path,
    )
    sidecar_config = _sidecar_config_path(
        environment=effective_environment,
        launch_agent_path=effective_launch_agent,
    )
    if sidecar_config is not None:
        sidecar_node_id = _node_id_from_sidecar_config(sidecar_config)
        if sidecar_node_id:
            return ReportingIdentity(
                node_id=sidecar_node_id,
                source="token_sidecar",
                sidecar_config_path=sidecar_config,
            )

    return ReportingIdentity(
        node_id=_computer_node_id(hostname or socket.gethostname()),
        source="computer_name",
    )


def resolve_reporting_dsn(
    *,
    environment: Mapping[str, str] | None = None,
    launch_agent_path: str | Path | None = None,
) -> str | None:
    """Resolve the ledger DSN without exposing it through service status.

    Unified Inference's own environment always wins. The previous sidecar's
    LaunchAgent is a migration fallback so an existing workstation can switch
    reporters without copying a secret into config.yaml.
    """

    effective_environment = os.environ if environment is None else environment
    configured = effective_environment.get("TOKEN_SIDECAR_POSTGRES_DSN", "").strip()
    if configured:
        return configured

    effective_launch_agent = _effective_launch_agent_path(
        effective_environment,
        launch_agent_path,
    )
    inherited = _launch_agent_environment(effective_launch_agent).get(
        "TOKEN_SIDECAR_POSTGRES_DSN", ""
    ).strip()
    return inherited or None


def persist_legacy_reporting_environment(
    env_path: str | Path | None,
    *,
    environment: Mapping[str, str] | None = None,
    launch_agent_path: str | Path | None = None,
) -> bool:
    """Copy one-time legacy reporting values into Unified Inference's private env.

    The migration fallback cannot remain a permanent dependency: once the old
    LaunchAgent is retired, future Unified Inference starts still need the
    same ledger DSN and node identity. Existing keys (including intentionally
    blank ones) always win. The secret value is never returned or logged.
    """

    if env_path is None:
        return False
    destination = Path(env_path).expanduser()
    try:
        destination_is_symlink = destination.is_symlink()
        existing = (
            destination.read_text(encoding="utf-8") if destination.exists() else ""
        )
    except OSError as exc:
        raise ReportingMigrationError(
            f"could not inspect the Unified Inference environment file: {exc}"
        ) from exc
    existing_keys = {
        line.split("=", 1)[0].strip()
        for raw_line in existing.splitlines()
        if (line := raw_line.strip())
        and not line.startswith("#")
        and "=" in line
    }

    effective_environment = os.environ if environment is None else environment
    legacy_agent = _effective_launch_agent_path(
        effective_environment,
        launch_agent_path,
    )
    legacy_environment = _launch_agent_environment(legacy_agent)
    additions: dict[str, str] = {}

    if (
        "TOKEN_SIDECAR_POSTGRES_DSN" not in existing_keys
        and not effective_environment.get("TOKEN_SIDECAR_POSTGRES_DSN", "").strip()
    ):
        legacy_dsn = legacy_environment.get(
            "TOKEN_SIDECAR_POSTGRES_DSN", ""
        ).strip()
        if _safe_env_value(legacy_dsn):
            additions["TOKEN_SIDECAR_POSTGRES_DSN"] = legacy_dsn

    if (
        REPORTING_NODE_ID_ENV not in existing_keys
        and not effective_environment.get(REPORTING_NODE_ID_ENV, "").strip()
    ):
        sidecar_config = _sidecar_config_path(
            environment=effective_environment,
            launch_agent_path=legacy_agent,
        )
        legacy_node_id = (
            _node_id_from_sidecar_config(sidecar_config)
            if sidecar_config is not None
            else None
        )
        if (
            legacy_node_id
            and len(legacy_node_id) <= 128
            and _safe_env_value(legacy_node_id)
        ):
            additions[REPORTING_NODE_ID_ENV] = legacy_node_id

    if not additions:
        return False
    if destination_is_symlink:
        raise ReportingMigrationError(
            "refusing to persist legacy reporting settings through a "
            "symlinked environment file"
        )

    try:
        parent_existed = destination.parent.exists()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            os.chmod(destination.parent, 0o700)
        prefix = existing
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        payload = (
            prefix
            + "# Migrated from the previous token sidecar.\n"
            + "".join(f"{key}={value}\n" for key, value in additions.items())
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        descriptor_open = True
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor_open = False
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            parent_descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        finally:
            if descriptor_open:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as exc:
        raise ReportingMigrationError(
            f"could not persist legacy reporting settings: {exc}"
        ) from exc

    # Make the migration effective for this process as well as future starts.
    if environment is None:
        for key, value in additions.items():
            os.environ.setdefault(key, value)
    return True


def _safe_env_value(value: str) -> bool:
    return bool(value) and not any(character in value for character in "\r\n\0")


__all__ = [
    "DEFAULT_SIDECAR_LAUNCH_AGENT",
    "REPORTING_NODE_ID_ENV",
    "ReportingMigrationError",
    "ReportingIdentity",
    "persist_legacy_reporting_environment",
    "resolve_reporting_dsn",
    "resolve_reporting_identity",
]
