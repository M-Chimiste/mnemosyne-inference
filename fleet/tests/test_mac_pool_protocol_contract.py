from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from mnemosyne_fleet.inventory_protocol import _SCHEMA as FLEET_INVENTORY_SCHEMA
from mnemosyne_fleet.desired_install_protocol import _SCHEMA as FLEET_DESIRED_SCHEMA


PROTOCOL_V1 = (
    Path(__file__).resolve().parents[2] / "mac_pool_protocol" / "v1"
)


@pytest.mark.parametrize(
    "stem", ["mac_inventory", "placement_recommendation", "desired_install"]
)
def test_nyx_can_validate_shared_mac_pool_v1_golden_contract(stem: str) -> None:
    schema = json.loads(
        (PROTOCOL_V1 / f"{stem}.schema.json").read_text(encoding="utf-8")
    )
    example = json.loads(
        (PROTOCOL_V1 / f"{stem}.example.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)


@pytest.mark.parametrize(
    "stem", ["mac_inventory", "placement_recommendation", "desired_install"]
)
def test_nyx_fails_closed_on_unknown_mac_pool_v1_fields(stem: str) -> None:
    schema = json.loads(
        (PROTOCOL_V1 / f"{stem}.schema.json").read_text(encoding="utf-8")
    )
    example = json.loads(
        (PROTOCOL_V1 / f"{stem}.example.json").read_text(encoding="utf-8")
    )
    example["future_authority"] = "must-not-pass"
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(example)


def test_fleet_runtime_validator_uses_exact_canonical_inventory_schema() -> None:
    canonical = json.loads(
        (PROTOCOL_V1 / "mac_inventory.schema.json").read_text(
            encoding="utf-8"
        )
    )
    packaged = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "src"
            / "mnemosyne_fleet"
            / "schemas"
            / "mac_inventory.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert packaged == canonical
    assert FLEET_INVENTORY_SCHEMA == canonical


def test_fleet_runtime_validator_uses_exact_canonical_desired_install_schema() -> None:
    canonical = json.loads(
        (PROTOCOL_V1 / "desired_install.schema.json").read_text(
            encoding="utf-8"
        )
    )
    packaged = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "src"
            / "mnemosyne_fleet"
            / "schemas"
            / "desired_install.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert packaged == canonical
    assert FLEET_DESIRED_SCHEMA == canonical
