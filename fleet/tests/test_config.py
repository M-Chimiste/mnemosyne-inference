from __future__ import annotations

import pytest

from mnemosyne_fleet.config import ConfigError, load_config


def test_separate_node_secrets_are_required_and_not_repr_exposed(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
database_path = "/tmp/fleet-test.db"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[[nodes]]
node_id = "node-a"
url = "http://node-a:8000"
fleet_token_env = "FLEET"
inference_token_env = "INFER"

[[models]]
name = "model"
deployment_id = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
capabilities = ["responses"]
""",
        encoding="utf-8",
    )
    config = load_config(
        path,
        environ={
            "CLIENT": "client-secret",
            "ADMIN": "admin-secret",
            "FLEET": "fleet-secret",
            "INFER": "inference-secret",
        },
    )
    rendered = repr(config)
    assert "client-secret" not in rendered
    assert "admin-secret" not in rendered
    assert "fleet-secret" not in rendered
    assert "inference-secret" not in rendered
    assert config.nodes[0].fleet_token == "fleet-secret"
    assert config.nodes[0].inference_token == "inference-secret"


def test_credentials_and_capabilities_are_separated(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2
[[nodes]]
node_id = "node"
url = "http://node"
fleet_token_env = "FLEET"
inference_token_env = "INFER"
[[models]]
name = "model"
deployment_id = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
capabilities = ["responses", "responses"]
""",
        encoding="utf-8",
    )
    base = {
        "CLIENT": "one",
        "ADMIN": "two",
        "FLEET": "three",
        "INFER": "four",
    }
    with pytest.raises(ConfigError, match="unique"):
        load_config(path, environ=base)
    with pytest.raises(ConfigError, match="all configured credentials must be distinct"):
        load_config(path, environ={**base, "ADMIN": "one"})
    with pytest.raises(ConfigError, match="all configured credentials must be distinct"):
        load_config(path, environ={**base, "INFER": "three"})


@pytest.mark.parametrize(
    ("overrides",),
    [
        ({"FLEET_B": "client"},),
        ({"INFER_B": "admin"},),
        ({"FLEET_B": "fleet-a"},),
        ({"INFER_B": "infer-a"},),
        ({"INFER_B": "fleet-b"},),
    ],
)
def test_credentials_cannot_be_reused_across_any_node_or_role(
    tmp_path,
    overrides: dict[str, str],
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[[nodes]]
node_id = "a"
url = "http://a"
fleet_token_env = "FLEET_A"
inference_token_env = "INFER_A"

[[nodes]]
node_id = "b"
url = "http://b"
fleet_token_env = "FLEET_B"
inference_token_env = "INFER_B"

[[models]]
name = "model"
deployment_id = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
capabilities = ["responses"]
""",
        encoding="utf-8",
    )
    environment = {
        "CLIENT": "client",
        "ADMIN": "admin",
        "FLEET_A": "fleet-a",
        "INFER_A": "infer-a",
        "FLEET_B": "fleet-b",
        "INFER_B": "infer-b",
        **overrides,
    }
    with pytest.raises(ConfigError) as captured:
        load_config(path, environ=environment)
    assert str(captured.value) == "all configured credentials must be distinct"


@pytest.mark.parametrize("non_finite", ["nan", "+inf", "-inf"])
def test_non_finite_server_timing_is_rejected(tmp_path, non_finite: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
poll_interval_seconds = {non_finite}
snapshot_ttl_seconds = 10

[[nodes]]
node_id = "node"
url = "http://node"
fleet_token_env = "FLEET"
inference_token_env = "INFER"

[[models]]
name = "model"
deployment_id = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
capabilities = ["responses"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="finite number"):
        load_config(
            path,
            environ={
                "CLIENT": "client",
                "ADMIN": "admin",
                "FLEET": "fleet",
                "INFER": "infer",
            },
        )
