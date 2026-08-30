from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from mnemosyne_macos.desired_install_protocol import (
    DESIRED_INSTALL_SCHEMA,
    DesiredInstallProtocolError,
    build_job_acknowledgement,
    parse_desired_install,
    validate_desired_install,
    validate_job_acknowledgement,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = REPOSITORY_ROOT / "mac_pool_protocol" / "v1"
PACKAGED_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "mnemosyne_macos"
    / "schemas"
    / "desired_install.schema.json"
)


def _job() -> dict:
    return json.loads(
        (PROTOCOL / "desired_install.example.json").read_text(encoding="utf-8")
    )


def _protocol_error(call, code: str) -> None:
    with pytest.raises(DesiredInstallProtocolError) as caught:
        call()
    assert caught.value.code == code
    assert str(caught.value) == code


def test_vendored_schema_is_byte_identical_and_runtime_uses_it() -> None:
    canonical = (PROTOCOL / "desired_install.schema.json").read_bytes()
    assert PACKAGED_SCHEMA.read_bytes() == canonical
    assert DESIRED_INSTALL_SCHEMA == json.loads(canonical)
    Draft202012Validator.check_schema(DESIRED_INSTALL_SCHEMA)


def test_golden_document_is_canonical_path_free_and_indexes_every_authority() -> None:
    job = _job()
    document = validate_desired_install(job)
    reparsed = parse_desired_install(document.canonical_json)

    assert reparsed == document
    assert document.job_id == job["job_id"]
    assert document.job_revision == 1
    assert document.idempotency_key == job["idempotency_key"]
    assert document.pairing_id == job["pairing_id"]
    assert document.credential_generation == job["credential_generation"]
    assert document.inventory_instance_id == (
        job["recommendation_basis"]["inventory_instance_id"]
    )
    assert document.inventory_sequence == 42
    assert document.catalog_version == job["catalog_version"]
    assert document.catalog_digest == job["catalog_digest"]
    assert document.logical_model_id == job["logical_model_id"]
    assert document.recipe_id == job["recipe_id"]
    assert document.artifact_id == job["artifact_id"]
    assert document.engine == "omlx"
    assert document.capabilities == tuple(job["capabilities"])
    assert document.guaranteed_context_tokens == 131_072
    assert document.storage_location_id == job["storage_location_id"]
    assert document.storage_binding_generation == 2
    assert document.payload_digest.startswith("sha256:")
    assert document.identity_digest.startswith("sha256:")
    assert "/Volumes/" not in document.canonical_json.decode()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":NaN}',
        b"\xff",
        b"[]",
        b"{}",
    ],
)
def test_malformed_json_is_rejected_with_one_fixed_non_echoing_code(raw: bytes) -> None:
    _protocol_error(lambda: parse_desired_install(raw), "desired_install_invalid")


def test_unsupported_versions_have_a_fixed_non_echoing_code() -> None:
    job = _job()
    job["schema_version"] = 2
    _protocol_error(
        lambda: validate_desired_install(job),
        "desired_install_unsupported_version",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("alias", "a" * 129),
        ("catalog_version", "a" * 129),
        ("capabilities", ["chat/completions"] * 8),
        ("valid_for_seconds", 604_801),
        ("job_revision", 2_147_483_648),
        ("guaranteed_context_tokens", 100_000_001),
    ],
)
def test_string_array_and_numeric_bounds_are_closed(field: str, value: object) -> None:
    job = _job()
    job[field] = value
    _protocol_error(lambda: validate_desired_install(job), "desired_install_invalid")


def test_capability_order_and_expiry_relation_are_canonical() -> None:
    unordered = _job()
    unordered["capabilities"] = list(reversed(unordered["capabilities"]))
    _protocol_error(
        lambda: validate_desired_install(unordered), "desired_install_invalid"
    )

    inconsistent = _job()
    inconsistent["expires_at"] += 1
    _protocol_error(
        lambda: validate_desired_install(inconsistent),
        "desired_install_invalid",
    )


@pytest.mark.parametrize(
    "forbidden",
    [
        "path",
        "destination",
        "volume_uuid",
        "scope_id",
        "bookmark",
        "repository_url",
        "credential",
        "engine_args",
        "delete",
        "prompt",
        "response",
    ],
)
def test_local_paths_secrets_and_new_authority_are_not_in_the_protocol(
    forbidden: str,
) -> None:
    job = _job()
    canary = "/Volumes/Private/models/secret-token"
    job[forbidden] = canary
    with pytest.raises(DesiredInstallProtocolError) as caught:
        validate_desired_install(job)
    assert caught.value.code == "desired_install_invalid"
    assert canary not in str(caught.value)


def test_identity_is_immutable_except_for_revision_state_and_ttl_envelope() -> None:
    run = validate_desired_install(_job())
    cancel_value = copy.deepcopy(run.value)
    cancel_value.update(
        {
            "job_revision": 2,
            "desired_state": "cancel",
            "created_at": run.created_at + 10,
            "expires_at": run.created_at + 70,
            "valid_for_seconds": 60,
        }
    )
    cancel = validate_desired_install(cancel_value)
    assert cancel.payload_digest != run.payload_digest
    assert cancel.identity_digest == run.identity_digest

    changed = copy.deepcopy(cancel.value)
    changed["storage_binding_generation"] += 1
    assert validate_desired_install(changed).identity_digest != run.identity_digest


def test_acknowledgement_builder_matches_inventory_v1_definition() -> None:
    acknowledgement = build_job_acknowledgement(
        job_id=_job()["job_id"],
        job_revision=1,
        state="awaiting_local_approval",
        bytes_downloaded=0,
        total_bytes=None,
        updated_at=1_785_528_051.0,
        result_code="local_approval_required",
    )
    assert validate_job_acknowledgement(acknowledgement.value) == acknowledgement
    inventory_schema = json.loads(
        (PROTOCOL / "mac_inventory.schema.json").read_text(encoding="utf-8")
    )
    ack_schema = {
        "$schema": inventory_schema["$schema"],
        "$defs": inventory_schema["$defs"],
        "$ref": "#/$defs/job_acknowledgement",
    }
    Draft202012Validator(ack_schema).validate(acknowledgement.value)
    assert acknowledgement.canonical_json == json.dumps(
        acknowledgement.value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_acknowledgement_rejects_unknown_fields_and_progress_overflow() -> None:
    acknowledgement = build_job_acknowledgement(
        job_id=_job()["job_id"],
        job_revision=1,
        state="downloading",
        bytes_downloaded=4,
        total_bytes=8,
        updated_at=1_785_528_051.0,
        result_code=None,
    ).value
    unknown = dict(acknowledgement, diagnostics="do-not-echo")
    _protocol_error(
        lambda: validate_job_acknowledgement(unknown),
        "desired_install_ack_invalid",
    )
    overflow = dict(acknowledgement, bytes_downloaded=9)
    _protocol_error(
        lambda: validate_job_acknowledgement(overflow),
        "desired_install_ack_invalid",
    )
