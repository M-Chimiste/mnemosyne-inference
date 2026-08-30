from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_V1 = REPOSITORY_ROOT / "mac_pool_protocol" / "v1"
FLEET_PROTOCOL_V1 = REPOSITORY_ROOT / "fleet_protocol" / "v1"

FROZEN_FLEET_V1_SHA256 = {
    "snapshot.schema.json": (
        "dae686391be61c8f0c85888a8d158a570f00c1f60c7c72359ad0b2d73b3aaa70"
    ),
    "snapshot.example.json": (
        "4d5cb3aa633b49334f2cb269b30cf1f19dce77a5334d00bd268202ccdb7d25b9"
    ),
    "identity_vectors.json": (
        "cc3c5e626e2a72541d70c01bd10efb49b323b651b7491d247983ebbb149084cc"
    ),
}


def _load(stem: str) -> tuple[dict[str, Any], dict[str, Any]]:
    schema = json.loads(
        (PROTOCOL_V1 / f"{stem}.schema.json").read_text(encoding="utf-8")
    )
    example = json.loads(
        (PROTOCOL_V1 / f"{stem}.example.json").read_text(encoding="utf-8")
    )
    return schema, example


def _walk_schema(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_schema(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_schema(child)


def _resolve_ref(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        reference = node["$ref"]
        assert reference.startswith("#/")
        resolved: Any = schema
        for component in reference[2:].split("/"):
            resolved = resolved[component.replace("~1", "/").replace("~0", "~")]
        assert isinstance(resolved, dict)
        node = resolved
    return node


def _assert_example_populates_every_property(
    schema: dict[str, Any],
    node: dict[str, Any],
    value: Any,
) -> None:
    node = _resolve_ref(schema, node)
    if isinstance(value, dict) and node.get("type") == "object":
        properties = node.get("properties", {})
        assert set(value) == set(properties)
        for key, child in value.items():
            _assert_example_populates_every_property(schema, properties[key], child)
    elif isinstance(value, list) and node.get("type") == "array":
        for child in value:
            _assert_example_populates_every_property(schema, node["items"], child)


@pytest.mark.parametrize(
    "stem", ["mac_inventory", "placement_recommendation", "desired_install"]
)
def test_v1_schemas_and_complete_golden_examples_agree(stem: str) -> None:
    schema, example = _load(stem)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)
    assert schema["properties"]["schema_version"]["const"] == 1
    assert example["schema_version"] == 1
    _assert_example_populates_every_property(schema, schema, example)


@pytest.mark.parametrize(
    "stem", ["mac_inventory", "placement_recommendation", "desired_install"]
)
def test_every_wire_object_is_closed_and_every_collection_is_bounded(
    stem: str,
) -> None:
    schema, _example = _load(stem)
    for node in _walk_schema(schema):
        node_type = node.get("type")
        if node_type == "object":
            assert node.get("additionalProperties") is False
        if node_type == "array":
            assert isinstance(node.get("maxItems"), int)
            assert node["maxItems"] > 0
        if node_type == "string" or (
            isinstance(node_type, list) and "string" in node_type
        ):
            assert isinstance(node.get("maxLength"), int)
            assert node["maxLength"] > 0
        if (
            isinstance(node_type, str)
            and node_type in {"integer", "number"}
        ) or (
            isinstance(node_type, list)
            and ({"integer", "number"} & set(node_type))
        ):
            assert "minimum" in node
            assert "maximum" in node


def test_inventory_golden_arrays_are_canonical_and_within_contract_limits() -> None:
    schema, inventory = _load("mac_inventory")
    assert schema["properties"]["storage_locations"]["maxItems"] == 128
    assert schema["properties"]["runtimes"]["maxItems"] == 16
    assert schema["properties"]["installations"]["maxItems"] == 10_000
    assert schema["properties"]["job_acknowledgements"]["maxItems"] == 256

    assert inventory["storage_locations"] == sorted(
        inventory["storage_locations"], key=lambda row: row["storage_location_id"]
    )
    assert inventory["runtimes"] == sorted(
        inventory["runtimes"], key=lambda row: row["engine"]
    )
    assert inventory["installations"] == sorted(
        inventory["installations"], key=lambda row: row["installation_id"]
    )
    assert inventory["job_acknowledgements"] == sorted(
        inventory["job_acknowledgements"], key=lambda row: row["job_id"]
    )
    assert inventory["service"]["supported_inventory_versions"] == sorted(
        inventory["service"]["supported_inventory_versions"]
    )
    assert inventory["service"]["supported_job_versions"] == sorted(
        inventory["service"]["supported_job_versions"]
    )
    assert inventory["service"]["supported_job_versions"] == []
    assert (
        schema["$defs"]["service"]["properties"]
        ["supported_job_versions"]["minItems"]
        == 0
    )
    for installation in inventory["installations"]:
        assert installation["aliases"] == sorted(installation["aliases"])
        assert installation["capabilities"] == sorted(
            installation["capabilities"]
        )


def test_desired_install_matches_exact_inventory_and_storage_basis() -> None:
    _inventory_schema, inventory = _load("mac_inventory")
    _job_schema, job = _load("desired_install")
    assert job["pairing_id"] == inventory["pairing_id"]
    assert job["credential_generation"] == inventory["credential_generation"]
    assert job["recommendation_basis"] == {
        "inventory_instance_id": inventory["inventory_instance_id"],
        "inventory_sequence": inventory["inventory_sequence"],
    }
    selected_storage = next(
        row
        for row in inventory["storage_locations"]
        if row["storage_location_id"] == job["storage_location_id"]
    )
    assert job["storage_binding_generation"] == selected_storage["binding_generation"]
    assert job["catalog_version"] == inventory["service"]["catalog_version"]
    assert job["catalog_digest"] == inventory["service"]["catalog_digest"]
    assert job["capabilities"] == sorted(job["capabilities"])


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "repository_url",
        "path",
        "destination",
        "volume_uuid",
        "scope_id",
        "bookmark",
        "engine_args",
        "credential",
        "delete",
        "prompt",
        "response",
    ],
)
def test_desired_install_rejects_every_location_or_authority_override(
    forbidden_field: str,
) -> None:
    schema, example = _load("desired_install")
    example[forbidden_field] = "canary"
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(example)


@pytest.mark.parametrize(
    ("container", "forbidden_field"),
    [
        (None, "hostname"),
        ("hardware", "serial_number"),
        ("service", "locator"),
        ("storage_locations", "path"),
        ("storage_locations", "volume_uuid"),
        ("storage_locations", "scope_id"),
        ("storage_locations", "bookmark"),
        ("installations", "destination"),
        ("installations", "free_form_error"),
    ],
)
def test_inventory_rejects_private_local_source_fields(
    container: str | None,
    forbidden_field: str,
) -> None:
    schema, example = _load("mac_inventory")
    if container is None:
        target = example
    elif isinstance(example[container], list):
        target = example[container][0]
    else:
        target = example[container]
    target[forbidden_field] = "canary"
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(example)


@pytest.mark.parametrize("stem", ["mac_inventory", "desired_install"])
def test_unknown_major_versions_fail_closed(stem: str) -> None:
    schema, example = _load(stem)
    example["schema_version"] = 2
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(example)


def test_examples_reject_absolute_or_symlink_spelling_as_wire_aliases() -> None:
    inventory_schema, inventory = _load("mac_inventory")
    inventory["installations"][0]["aliases"] = [
        "/Volumes/Athena/models/deepseek"
    ]
    with pytest.raises(ValidationError):
        Draft202012Validator(inventory_schema).validate(inventory)

    desired_schema, desired = _load("desired_install")
    desired["alias"] = "../models/glm"
    with pytest.raises(ValidationError):
        Draft202012Validator(desired_schema).validate(desired)


def test_inventory_storage_identity_and_binding_must_change_together() -> None:
    schema, inventory = _load("mac_inventory")
    installation = inventory["installations"][0]
    installation["storage_location_id"] = None
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(inventory)

    installation["storage_binding_generation"] = None
    Draft202012Validator(schema).validate(inventory)


def test_duplicate_rows_and_oversized_strings_are_rejected() -> None:
    schema, inventory = _load("mac_inventory")
    inventory["storage_locations"].append(
        copy.deepcopy(inventory["storage_locations"][0])
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(inventory)

    desired_schema, desired = _load("desired_install")
    desired["alias"] = "a" * 129
    with pytest.raises(ValidationError):
        Draft202012Validator(desired_schema).validate(desired)


def test_frozen_fleet_snapshot_v1_bytes_remain_unchanged_and_independent() -> None:
    for filename, expected_digest in FROZEN_FLEET_V1_SHA256.items():
        assert (
            hashlib.sha256((FLEET_PROTOCOL_V1 / filename).read_bytes()).hexdigest()
            == expected_digest
        )

    snapshot_schema = json.loads(
        (FLEET_PROTOCOL_V1 / "snapshot.schema.json").read_text(encoding="utf-8")
    )
    snapshot_fields = set(snapshot_schema["properties"])
    assert "inventory_instance_id" not in snapshot_fields
    assert "storage_locations" not in snapshot_fields
    assert "installations" not in snapshot_fields
    assert "job_acknowledgements" not in snapshot_fields
