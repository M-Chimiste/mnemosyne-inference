from __future__ import annotations

import plistlib
from pathlib import Path
import stat

import pytest

from mnemosyne_macos.sidecar_discovery import (
    REPORTING_NODE_ID_ENV,
    ReportingMigrationError,
    persist_legacy_reporting_environment,
    resolve_reporting_dsn,
    resolve_reporting_identity,
)


def test_reporting_identity_comes_from_active_sidecar_config(tmp_path: Path) -> None:
    config = tmp_path / "sidecar.yaml"
    config.write_text('node:\n  id: "theseus"\n', encoding="utf-8")
    launch_agent = tmp_path / "sidecar.plist"
    launch_agent.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "TOKEN_SIDECAR_CONFIG": str(config),
                }
            }
        )
    )

    identity = resolve_reporting_identity(
        "mnemosyne-mac",
        environment={},
        launch_agent_path=launch_agent,
        hostname="wrong-host",
    )

    assert identity.node_id == "theseus"
    assert identity.source == "token_sidecar"
    assert identity.sidecar_config_path == config


def test_explicit_reporting_identity_remains_an_override(tmp_path: Path) -> None:
    identity = resolve_reporting_identity(
        "lab-node",
        environment={},
        launch_agent_path=tmp_path / "missing.plist",
        hostname="Theseus.local",
    )
    assert identity.node_id == "lab-node"
    assert identity.source == "configured"


def test_hostname_is_normalized_when_no_sidecar_is_installed(tmp_path: Path) -> None:
    identity = resolve_reporting_identity(
        "",
        environment={},
        launch_agent_path=tmp_path / "missing.plist",
        hostname="Metis Studio.local",
    )
    assert identity.node_id == "metis-studio"
    assert identity.source == "computer_name"


def test_reporting_dsn_migrates_from_previous_sidecar_launch_agent(
    tmp_path: Path,
) -> None:
    launch_agent = tmp_path / "sidecar.plist"
    launch_agent.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "TOKEN_SIDECAR_POSTGRES_DSN": "postgresql://legacy.example/usage",
                }
            }
        )
    )

    assert resolve_reporting_dsn(
        environment={},
        launch_agent_path=launch_agent,
    ) == "postgresql://legacy.example/usage"


def test_unified_inference_reporting_dsn_overrides_migration_value(
    tmp_path: Path,
) -> None:
    launch_agent = tmp_path / "sidecar.plist"
    launch_agent.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "TOKEN_SIDECAR_POSTGRES_DSN": "postgresql://legacy.example/usage",
                }
            }
        )
    )

    assert resolve_reporting_dsn(
        environment={
            "TOKEN_SIDECAR_POSTGRES_DSN": "postgresql://unified.example/usage"
        },
        launch_agent_path=launch_agent,
    ) == "postgresql://unified.example/usage"


def test_legacy_reporting_values_are_persisted_before_sidecar_retirement(
    tmp_path: Path,
) -> None:
    legacy_config = tmp_path / "legacy.yaml"
    legacy_config.write_text('node:\n  id: "theseus"\n', encoding="utf-8")
    launch_agent = tmp_path / "legacy.plist"
    launch_agent.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "TOKEN_SIDECAR_CONFIG": str(legacy_config),
                    "TOKEN_SIDECAR_POSTGRES_DSN": (
                        "postgresql://legacy.example/usage"
                    ),
                }
            }
        )
    )
    env_path = tmp_path / "Unified Inference" / ".env"

    assert persist_legacy_reporting_environment(
        env_path,
        environment={},
        launch_agent_path=launch_agent,
    )
    assert stat.S_IMODE(env_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    persisted = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    launch_agent.unlink()
    legacy_config.unlink()
    identity = resolve_reporting_identity(
        "",
        environment=persisted,
        launch_agent_path=launch_agent,
        hostname="wrong-host",
    )
    assert identity.node_id == "theseus"
    assert identity.source == "token_sidecar"
    assert (
        resolve_reporting_dsn(
            environment=persisted,
            launch_agent_path=launch_agent,
        )
        == "postgresql://legacy.example/usage"
    )


def test_existing_unified_reporting_values_are_never_overwritten(
    tmp_path: Path,
) -> None:
    legacy_config = tmp_path / "legacy.yaml"
    legacy_config.write_text('node:\n  id: "legacy-node"\n', encoding="utf-8")
    launch_agent = tmp_path / "legacy.plist"
    launch_agent.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "TOKEN_SIDECAR_CONFIG": str(legacy_config),
                    "TOKEN_SIDECAR_POSTGRES_DSN": "postgresql://legacy/usage",
                }
            }
        )
    )
    env_path = tmp_path / ".env"
    original = (
        "TOKEN_SIDECAR_POSTGRES_DSN=postgresql://unified/usage\n"
        f"{REPORTING_NODE_ID_ENV}=athena\n"
    )
    env_path.write_text(original, encoding="utf-8")

    assert not persist_legacy_reporting_environment(
        env_path,
        environment={},
        launch_agent_path=launch_agent,
    )
    assert env_path.read_text(encoding="utf-8") == original


def test_reporting_migration_does_not_chmod_an_existing_custom_parent(
    tmp_path: Path,
) -> None:
    launch_agent = tmp_path / "legacy.plist"
    launch_agent.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "TOKEN_SIDECAR_POSTGRES_DSN": "postgresql://legacy/usage",
                }
            }
        )
    )
    custom_parent = tmp_path / "shared-config"
    custom_parent.mkdir(mode=0o755)
    custom_parent.chmod(0o755)

    assert persist_legacy_reporting_environment(
        custom_parent / ".env",
        environment={},
        launch_agent_path=launch_agent,
    )
    assert stat.S_IMODE(custom_parent.stat().st_mode) == 0o755


def test_reporting_migration_refuses_to_replace_a_symlinked_env(
    tmp_path: Path,
) -> None:
    launch_agent = tmp_path / "legacy.plist"
    launch_agent.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "TOKEN_SIDECAR_POSTGRES_DSN": "postgresql://legacy/usage",
                }
            }
        )
    )
    actual_env = tmp_path / "actual.env"
    actual_env.write_text("# existing\n", encoding="utf-8")
    linked_env = tmp_path / ".env"
    linked_env.symlink_to(actual_env)

    with pytest.raises(ReportingMigrationError, match="symlinked"):
        persist_legacy_reporting_environment(
            linked_env,
            environment={},
            launch_agent_path=launch_agent,
        )
    assert actual_env.read_text(encoding="utf-8") == "# existing\n"
