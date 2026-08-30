from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


PROTOCOL_V1 = (
    Path(__file__).resolve().parents[3] / "mac_pool_protocol" / "v1"
)


@pytest.mark.parametrize("stem", ["mac_inventory", "desired_install"])
def test_native_service_can_validate_shared_mac_pool_v1_golden_contract(
    stem: str,
) -> None:
    schema = json.loads(
        (PROTOCOL_V1 / f"{stem}.schema.json").read_text(encoding="utf-8")
    )
    example = json.loads(
        (PROTOCOL_V1 / f"{stem}.example.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)


@pytest.mark.parametrize("stem", ["mac_inventory", "desired_install"])
def test_native_service_fails_closed_on_unknown_mac_pool_v1_fields(
    stem: str,
) -> None:
    schema = json.loads(
        (PROTOCOL_V1 / f"{stem}.schema.json").read_text(encoding="utf-8")
    )
    example = json.loads(
        (PROTOCOL_V1 / f"{stem}.example.json").read_text(encoding="utf-8")
    )
    example["future_authority"] = "must-not-pass"
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(example)

