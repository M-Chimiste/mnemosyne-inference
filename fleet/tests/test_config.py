from __future__ import annotations

import pytest

from mnemosyne_fleet.config import ConfigError, NodeConfig, load_config


PAIRING_MASTER_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
CATALOG_PUBLIC_KEY = "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA"


def test_pairing_is_disabled_by_default_and_static_config_stays_compatible(
    tmp_path,
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

    config = load_config(
        path,
        environ={
            "CLIENT": "client",
            "ADMIN": "admin",
            "FLEET": "fleet",
            "INFER": "infer",
        },
    )

    assert config.pairing.enabled is False
    assert config.pairing.master_key is None
    assert config.catalog.enabled is False
    assert config.placement.remote_installs_enabled is False
    assert config.placement.desired_install_database_path is None
    assert config.placement.desired_install_valid_seconds == 900
    assert config.placement.maximum_active_desired_installs == 1_000
    assert config.placement.desired_install_history_limit == 10_000


def test_signed_catalog_configuration_is_explicit_strict_and_public_key_only(
    tmp_path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
database_path = "{tmp_path / 'fleet.db'}"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[catalog]
enabled = true
state_directory = "{tmp_path / 'private' / 'catalog'}"
update_origin = "https://catalog.example.test"
update_path = "/v1/apple-silicon/catalog.json"
update_interval_seconds = 600
total_timeout_seconds = 10
connect_timeout_seconds = 2
max_attempts = 1
retry_delay_seconds = 0

[[catalog.trusted_keys]]
key_id = "mnemosyne-catalog-2026-a"
public_key = "{CATALOG_PUBLIC_KEY}"
valid_from = 1
valid_until = 4102444800
minimum_catalog_sequence = 2
maximum_catalog_sequence = 100

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
    config = load_config(
        path,
        environ={
            "CLIENT": "client",
            "ADMIN": "admin",
            "FLEET": "fleet",
            "INFER": "infer",
        },
    )

    assert config.catalog.enabled is True
    assert config.catalog.update_origin == "https://catalog.example.test"
    assert config.catalog.update_path == "/v1/apple-silicon/catalog.json"
    assert config.catalog.state_directory == tmp_path / "private" / "catalog"
    assert config.catalog.update_interval_seconds == 600
    assert config.catalog.trusted_keys[0].key_id == "mnemosyne-catalog-2026-a"
    assert config.catalog.trusted_keys[0].minimum_catalog_sequence == 2
    assert CATALOG_PUBLIC_KEY not in repr(config)


@pytest.mark.parametrize(
    ("catalog_body", "message"),
    [
        (
            'update_origin = "http://catalog.example.test"\n'
            'update_path = "/v1/catalog.json"',
            "canonical HTTPS origin",
        ),
        (
            'update_origin = "https://Catalog.example.test"\n'
            'update_path = "/v1/catalog.json"',
            "canonical HTTPS origin",
        ),
        (
            'update_origin = "https://catalog.example.test"\n'
            'update_path = "/v1/../catalog.json"',
            "canonical absolute path",
        ),
        (
            'update_origin = "https://catalog.example.test"\n'
            'update_path = "/v1/catalog.json"\n'
            "update_interval_seconds = 59",
            "between 60 and 86400",
        ),
        (
            'update_origin = "https://catalog.example.test"\n'
            'update_path = "/v1/catalog.json"\n'
            "max_attempts = 1.5",
            "must be an integer",
        ),
        (
            'update_origin = "https://catalog.example.test"\n'
            'update_path = "/v1/catalog.json"\n'
            "private_key = \"must-never-be-configured\"",
            "unsupported fields",
        ),
    ],
)
def test_invalid_catalog_configuration_fails_closed(
    tmp_path,
    catalog_body: str,
    message: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[catalog]
enabled = true
{catalog_body}

[[catalog.trusted_keys]]
key_id = "mnemosyne-catalog-2026-a"
public_key = "{CATALOG_PUBLIC_KEY}"

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
    with pytest.raises(ConfigError, match=message):
        load_config(
            path,
            environ={
                "CLIENT": "client",
                "ADMIN": "admin",
                "FLEET": "fleet",
                "INFER": "infer",
            },
        )


def test_remote_install_placement_is_off_by_default_and_requires_both_services(
    tmp_path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[placement]
remote_installs_enabled = true

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
    with pytest.raises(ConfigError, match="requires catalog.enabled"):
        load_config(
            path,
            environ={
                "CLIENT": "client",
                "ADMIN": "admin",
                "FLEET": "fleet",
                "INFER": "infer",
            },
        )


@pytest.mark.parametrize(
    ("placement_body", "message"),
    [
        (
            'remote_installs_enabled = false\n'
            'desired_install_database_path = "/tmp/latent.db"',
            "requires placement.remote_installs_enabled",
        ),
        (
            'remote_installs_enabled = true\n'
            'desired_install_database_path = "relative.db"',
            "must be absolute",
        ),
        (
            "remote_installs_enabled = false\n"
            "maximum_active_desired_installs = 0",
            "must be between 1 and 10000",
        ),
        (
            "remote_installs_enabled = false\n"
            "desired_install_history_limit = 100001",
            "must be between 1 and 100000",
        ),
    ],
)
def test_desired_install_configuration_is_strict_and_bounded(
    tmp_path,
    placement_body: str,
    message: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
database_path = "{tmp_path / 'fleet.db'}"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[placement]
{placement_body}

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
    with pytest.raises(ConfigError, match=message):
        load_config(
            path,
            environ={
                "CLIENT": "client",
                "ADMIN": "admin",
                "FLEET": "fleet",
                "INFER": "infer",
            },
        )


def test_disabled_catalog_rejects_latent_key_or_endpoint_configuration(
    tmp_path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[catalog]
enabled = false
update_origin = "https://catalog.example.test"

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
    with pytest.raises(ConfigError, match="settings require catalog.enabled"):
        load_config(
            path,
            environ={
                "CLIENT": "client",
                "ADMIN": "admin",
                "FLEET": "fleet",
                "INFER": "infer",
            },
        )


def test_pairing_only_config_uses_private_databases_and_redacts_master_key(
    tmp_path,
) -> None:
    fleet_database = tmp_path / "fleet.db"
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
database_path = "{fleet_database}"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[pairing]
enabled = true
public_origin = "https://nyx.example.test/"
master_key_env = "PAIRING_MASTER"
https_cidr_allowlist = ["10.0.0.0/8", "fd12:3456:789a::/48"]
tailscale_cidr_allowlist = ["100.64.0.0/10"]
trusted_lan_http_cidr_allowlist = ["192.168.50.0/24"]
allowed_node_ports = [1240, 443]
dns_resolution_timeout_seconds = 4

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
            "CLIENT": "client",
            "ADMIN": "admin",
            "PAIRING_MASTER": PAIRING_MASTER_KEY,
        },
    )

    assert config.nodes == ()
    assert config.pairing.enabled is True
    assert config.pairing.public_origin == "https://nyx.example.test"
    assert config.pairing.metadata_database_path == (
        tmp_path / "private" / "pairing-metadata.db"
    )
    assert config.pairing.secret_database_path == (
        tmp_path / "private" / "pairing-secrets.db"
    )
    assert config.pairing.inventory_database_path == tmp_path / "mac-inventory.db"
    assert config.pairing.https_cidr_allowlist == (
        "10.0.0.0/8",
        "fd12:3456:789a::/48",
    )
    assert config.pairing.tailscale_cidr_allowlist == ("100.64.0.0/10",)
    assert config.pairing.trusted_lan_http_cidr_allowlist == (
        "192.168.50.0/24",
    )
    assert config.pairing.allowed_node_ports == (1240, 443)
    assert config.pairing.dns_resolution_timeout_seconds == 4
    assert config.pairing.inventory_ttl_seconds == 60
    assert PAIRING_MASTER_KEY not in repr(config)


@pytest.mark.parametrize(
    ("pairing_toml", "environment", "message"),
    [
        (
            'enabled = "yes"',
            {},
            "pairing.enabled must be a boolean",
        ),
        (
            'enabled = true\npublic_origin = "http://nyx.example.test"\nmaster_key_env = "PAIRING_MASTER"',
            {"PAIRING_MASTER": PAIRING_MASTER_KEY},
            "must be an HTTPS origin",
        ),
        (
            'enabled = true\npublic_origin = "https://nyx.example.test/path"\nmaster_key_env = "PAIRING_MASTER"',
            {"PAIRING_MASTER": PAIRING_MASTER_KEY},
            "without credentials, path, query, or fragment",
        ),
        (
            'enabled = true\npublic_origin = "https://nyx.example.test"\nmaster_key_env = "PAIRING_MASTER"',
            {"PAIRING_MASTER": "not-a-key"},
            "canonical 32-byte base64url",
        ),
    ],
)
def test_invalid_pairing_configuration_fails_closed(
    tmp_path,
    pairing_toml: str,
    environment: dict[str, str],
    message: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[pairing]
{pairing_toml}

[[models]]
name = "model"
deployment_id = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
capabilities = ["responses"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(
            path,
            environ={"CLIENT": "client", "ADMIN": "admin", **environment},
        )


@pytest.mark.parametrize(
    ("locator_policy", "message"),
    [
        (
            'https_cidr_allowlist = ["10.1.2.3/8"]',
            "canonical CIDR strings",
        ),
        (
            'tailscale_cidr_allowlist = ["100.64.0.0/10", "100.64.0.0/10"]',
            "unique CIDRs",
        ),
        (
            "allowed_node_ports = []",
            "non-empty array",
        ),
        (
            "allowed_node_ports = [1240, 1240]",
            "unique ports",
        ),
        (
            "allowed_node_ports = [0]",
            "between 1 and 65535",
        ),
        (
            "dns_resolution_timeout_seconds = 31",
            "at most 30",
        ),
        (
            "inventory_ttl_seconds = 14",
            "must be at least 15",
        ),
        (
            "inventory_ttl_seconds = 301",
            "at most 300",
        ),
    ],
)
def test_invalid_pairing_locator_policy_configuration_fails_closed(
    tmp_path,
    locator_policy: str,
    message: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[pairing]
enabled = true
public_origin = "https://nyx.example.test"
master_key_env = "PAIRING_MASTER"
{locator_policy}

[[models]]
name = "model"
deployment_id = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
capabilities = ["responses"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(
            path,
            environ={
                "CLIENT": "client",
                "ADMIN": "admin",
                "PAIRING_MASTER": PAIRING_MASTER_KEY,
            },
        )


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
    assert config.nodes[0].service_class == "primary"
    assert config.nodes[0].source == "static"
    assert config.nodes[0].enrollment_id == "node-a"
    assert config.nodes[0].reporting_node_id == "node-a"


def test_paired_node_requires_an_explicit_opaque_enrollment_identity() -> None:
    with pytest.raises(ValueError, match="enrollment identity"):
        NodeConfig(
            node_id="reporting-node",
            url="https://redacted.invalid",
            fleet_token="snapshot-secret",
            inference_token="dispatch-secret",
            source="paired",
            locator_transport="https",
        )

    paired = NodeConfig(
        node_id="reporting-node",
        url="https://redacted.invalid",
        fleet_token="snapshot-secret",
        inference_token="dispatch-secret",
        source="paired",
        enrollment_id="11111111-1111-4111-8111-111111111111",
        locator_transport="https",
    )
    assert paired.reporting_node_id == "reporting-node"
    assert paired.enrollment_id == "11111111-1111-4111-8111-111111111111"


def test_node_service_class_is_loaded_and_invalid_values_are_rejected(tmp_path) -> None:
    path = tmp_path / "config.toml"
    template = """
[server]
api_key_env = "CLIENT"
admin_api_key_env = "ADMIN"
poll_interval_seconds = 1
snapshot_ttl_seconds = 2

[[nodes]]
node_id = "node-a"
url = "http://node-a:8000"
fleet_token_env = "FLEET"
inference_token_env = "INFER"
service_class = "SERVICE_CLASS"

[[models]]
name = "model"
deployment_id = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
capabilities = ["responses"]
"""
    environment = {
        "CLIENT": "client-secret",
        "ADMIN": "admin-secret",
        "FLEET": "fleet-secret",
        "INFER": "inference-secret",
    }

    path.write_text(
        template.replace("SERVICE_CLASS", "overflow"),
        encoding="utf-8",
    )
    assert load_config(path, environ=environment).nodes[0].service_class == "overflow"

    path.write_text(
        template.replace("SERVICE_CLASS", "limited"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="primary, opportunistic, or overflow"):
        load_config(path, environ=environment)


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
