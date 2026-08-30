from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_V1 = REPOSITORY_ROOT / "mac_pool_protocol" / "v1"
FLEET_PACKAGE = REPOSITORY_ROOT / "fleet" / "src" / "mnemosyne_fleet"
MAC_PACKAGE = (
    REPOSITORY_ROOT / "macos" / "service" / "src" / "mnemosyne_macos"
)


def _load_isolated_protocol(
    *,
    package_name: str,
    package_path: Path,
    module_alias: str,
):
    """Load one protocol module without executing either package __init__."""

    previous_package = sys.modules.get(package_name)
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        package_path / "__init__.py",
        submodule_search_locations=[str(package_path)],
    )
    if package_spec is None:
        raise AssertionError("package spec unavailable")
    package = types.ModuleType(package_name)
    package.__file__ = str(package_path / "__init__.py")
    package.__path__ = [str(package_path)]
    package.__package__ = package_name
    package.__spec__ = package_spec
    sys.modules[package_name] = package
    qualified_name = f"{package_name}.{module_alias}"
    module_spec = importlib.util.spec_from_file_location(
        qualified_name,
        package_path / "desired_install_protocol.py",
    )
    if module_spec is None or module_spec.loader is None:
        raise AssertionError("protocol module spec unavailable")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[qualified_name] = module
    try:
        module_spec.loader.exec_module(module)
    finally:
        sys.modules.pop(qualified_name, None)
        if previous_package is None:
            sys.modules.pop(package_name, None)
        else:
            sys.modules[package_name] = previous_package
    return module


FLEET = _load_isolated_protocol(
    package_name="mnemosyne_fleet",
    package_path=FLEET_PACKAGE,
    module_alias="desired_install_protocol_contract_fleet",
)
MAC = _load_isolated_protocol(
    package_name="mnemosyne_macos",
    package_path=MAC_PACKAGE,
    module_alias="desired_install_protocol_contract_mac",
)


def _golden() -> dict[str, Any]:
    return json.loads(
        (PROTOCOL_V1 / "desired_install.example.json").read_text(
            encoding="utf-8"
        )
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _authority_index(document: object) -> dict[str, Any]:
    value = document.value
    basis = value["recommendation_basis"]
    return {
        "job_id": document.job_id,
        "job_revision": document.job_revision,
        "idempotency_key": document.idempotency_key,
        "desired_state": document.desired_state,
        "created_at": document.created_at,
        "expires_at": document.expires_at,
        "valid_for_seconds": value["valid_for_seconds"],
        "pairing_id": document.pairing_id,
        "credential_generation": document.credential_generation,
        "inventory_instance_id": document.inventory_instance_id,
        "inventory_sequence": document.inventory_sequence,
        "catalog_version": value["catalog_version"],
        "catalog_digest": value["catalog_digest"],
        "logical_model_id": value["logical_model_id"],
        "recipe_id": value["recipe_id"],
        "artifact_id": value["artifact_id"],
        "engine": value["engine"],
        "capabilities": tuple(value["capabilities"]),
        "guaranteed_context_tokens": value["guaranteed_context_tokens"],
        "alias": value.get("alias"),
        "storage_location_id": document.storage_location_id,
        "storage_binding_generation": document.storage_binding_generation,
        "basis_value": {
            "inventory_instance_id": basis["inventory_instance_id"],
            "inventory_sequence": basis["inventory_sequence"],
        },
    }


def _assert_both_reject(value: object) -> None:
    errors = []
    for implementation in (FLEET, MAC):
        with pytest.raises(implementation.DesiredInstallProtocolError) as caught:
            implementation.validate_desired_install(copy.deepcopy(value))
        errors.append(caught.value.code)
        assert str(caught.value) == caught.value.code
    assert all(code.startswith("desired_install_") for code in errors)


def _mutate(field: str, value: object) -> Callable[[dict[str, Any]], None]:
    def apply(document: dict[str, Any]) -> None:
        document[field] = value

    return apply


def _mutate_basis(field: str, value: object) -> Callable[[dict[str, Any]], None]:
    def apply(document: dict[str, Any]) -> None:
        document["recommendation_basis"][field] = value

    return apply


def _delete(field: str) -> Callable[[dict[str, Any]], None]:
    def apply(document: dict[str, Any]) -> None:
        del document[field]

    return apply


def test_canonical_fleet_and_mac_desired_schemas_are_byte_identical() -> None:
    canonical = (PROTOCOL_V1 / "desired_install.schema.json").read_bytes()
    fleet = (FLEET_PACKAGE / "schemas" / "desired_install.schema.json").read_bytes()
    mac = (MAC_PACKAGE / "schemas" / "desired_install.schema.json").read_bytes()
    assert canonical == fleet == mac
    expected = json.loads(canonical)
    assert FLEET._SCHEMA == expected
    assert MAC.DESIRED_INSTALL_SCHEMA == expected
    assert FLEET.MAX_DESIRED_INSTALL_BYTES == MAC.MAX_DESIRED_INSTALL_BYTES


def test_golden_has_identical_canonical_digest_and_indexed_authority() -> None:
    golden = _golden()
    fleet = FLEET.validate_desired_install(copy.deepcopy(golden))
    mac = MAC.validate_desired_install(copy.deepcopy(golden))
    expected_canonical = _canonical(golden)
    assert fleet.value == mac.value == golden
    assert fleet.canonical_json == mac.canonical_json == expected_canonical
    assert fleet.payload_digest == mac.payload_digest == _digest(expected_canonical)
    assert _authority_index(fleet) == _authority_index(mac)
    assert FLEET.parse_desired_install(expected_canonical) == fleet
    assert MAC.parse_desired_install(expected_canonical) == mac


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        ("unknown field", _mutate("future_authority", "forbidden")),
        (
            "catalog file layout",
            _mutate(
                "gguf_layout",
                {
                    "primary_file": "weights/model.gguf",
                    "required_shards": [],
                    "selected_projector_file": None,
                },
            ),
        ),
        ("missing job", _delete("job_id")),
        ("missing artifact", _delete("artifact_id")),
        ("bool revision", _mutate("job_revision", True)),
        ("bool generation", _mutate("credential_generation", True)),
        ("bool sequence", _mutate_basis("inventory_sequence", False)),
        (
            "unsorted capabilities",
            _mutate("capabilities", ["responses", "chat/completions"]),
        ),
        ("malformed uuid", _mutate("job_id", "not-a-uuid")),
        (
            "uppercase uuid",
            _mutate("job_id", "77777777-7777-4777-8777-77777777777A"),
        ),
        ("malformed digest", _mutate("catalog_digest", "sha256:1234")),
        (
            "uppercase digest",
            _mutate("catalog_digest", "sha256:" + "A" * 64),
        ),
        ("path alias", _mutate("alias", "/Volumes/private/model")),
        ("control alias", _mutate("alias", "private\nmodel")),
        ("long alias", _mutate("alias", "a" * 129)),
        ("zero ttl", _mutate("valid_for_seconds", 0)),
        ("oversize ttl", _mutate("valid_for_seconds", 604_801)),
        ("ttl mismatch", _mutate("expires_at", 1_785_528_951.0)),
        (
            "context bound",
            _mutate("guaranteed_context_tokens", 100_000_001),
        ),
        ("revision bound", _mutate("job_revision", 2_147_483_648)),
        (
            "capability size",
            _mutate(
                "capabilities",
                [
                    "chat/completions",
                    "completions",
                    "embeddings",
                    "images/generations",
                    "messages",
                    "rerank",
                    "responses",
                    "responses",
                ],
            ),
        ),
    ],
)
def test_bounded_mutation_corpus_is_rejected_consistently(
    name: str,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    document = _golden()
    mutation(document)
    _assert_both_reject(document)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":NaN}',
        b"\xff",
        b"[]",
    ],
)
def test_malformed_or_duplicate_json_is_rejected_consistently(raw: bytes) -> None:
    for implementation in (FLEET, MAC):
        with pytest.raises(implementation.DesiredInstallProtocolError) as caught:
            implementation.parse_desired_install(raw)
        assert caught.value.code == "desired_install_invalid"


def test_duplicate_member_in_an_otherwise_complete_job_is_rejected() -> None:
    canonical = _canonical(_golden())
    duplicate = canonical[:-1] + b',"job_id":"77777777-7777-4777-8777-777777777777"}'
    for implementation in (FLEET, MAC):
        with pytest.raises(implementation.DesiredInstallProtocolError) as caught:
            implementation.parse_desired_install(duplicate)
        assert caught.value.code == "desired_install_invalid"


def test_encoded_size_limit_is_identical() -> None:
    oversized = b" " * (FLEET.MAX_DESIRED_INSTALL_BYTES + 1)
    for implementation in (FLEET, MAC):
        with pytest.raises(implementation.DesiredInstallProtocolError) as caught:
            implementation.parse_desired_install(oversized)
        assert caught.value.code == "desired_install_too_large"


@pytest.mark.parametrize(
    ("state", "downloaded", "total", "result_code", "installation_id"),
    [
        ("received", 0, None, None, None),
        (
            "awaiting_local_approval",
            0,
            1_000,
            "local_approval_required",
            None,
        ),
        (
            "downloading",
            400,
            1_000,
            None,
            "99999999-9999-4999-8999-999999999999",
        ),
        (
            "verifying",
            1_000,
            1_000,
            None,
            "99999999-9999-4999-8999-999999999999",
        ),
        (
            "completed",
            1_000,
            1_000,
            None,
            "99999999-9999-4999-8999-999999999999",
        ),
    ],
)
def test_mac_acknowledgements_are_canonical_for_fleet_with_bound_installation_id(
    state: str,
    downloaded: int,
    total: int | None,
    result_code: str | None,
    installation_id: str | None,
) -> None:
    mac = MAC.build_job_acknowledgement(
        job_id=_golden()["job_id"],
        job_revision=1,
        state=state,
        bytes_downloaded=downloaded,
        total_bytes=total,
        updated_at=1_785_528_051.0,
        result_code=result_code,
        installation_id=installation_id,
    )
    fleet = FLEET.validate_job_acknowledgement(copy.deepcopy(mac.value))
    assert fleet.value == mac.value
    assert fleet.canonical_json == mac.canonical_json
    assert fleet.payload_digest == mac.payload_digest
    assert fleet.job_id == mac.job_id
    assert fleet.job_revision == mac.job_revision
    assert fleet.state == mac.state
    assert fleet.bytes_downloaded == mac.bytes_downloaded
    assert fleet.total_bytes == mac.total_bytes
    assert fleet.updated_at == mac.updated_at
    assert fleet.result_code == mac.result_code
    assert fleet.value.get("installation_id") == mac.installation_id


def test_installation_id_is_absent_until_bound_then_stays_identical() -> None:
    installation_id = "99999999-9999-4999-8999-999999999999"
    cases = (
        ("received", 0, None),
        ("downloading", 400, installation_id),
        ("verifying", 1_000, installation_id),
    )
    observed: list[str | None] = []
    for state, downloaded, bound_id in cases:
        mac = MAC.build_job_acknowledgement(
            job_id=_golden()["job_id"],
            job_revision=1,
            state=state,
            bytes_downloaded=downloaded,
            total_bytes=1_000,
            updated_at=1_785_528_051.0 + downloaded,
            result_code=None,
            installation_id=bound_id,
        )
        fleet = FLEET.validate_job_acknowledgement(mac.value)
        assert fleet.canonical_json == mac.canonical_json
        assert fleet.payload_digest == mac.payload_digest
        observed.append(fleet.value.get("installation_id"))
    assert observed == [None, installation_id, installation_id]


@pytest.mark.parametrize(
    "forbidden",
    [
        "path",
        "destination",
        "volume_uuid",
        "scope_id",
        "bookmark",
        "repository_id",
        "repository_url",
        "credential",
        "bearer",
        "engine_args",
        "runtime_args",
        "cleanup",
        "delete",
    ],
)
def test_job_and_ack_wires_reject_paths_credentials_and_repository_authority(
    forbidden: str,
) -> None:
    canary = "https://token.invalid/Volumes/private/model"
    job = _golden()
    job[forbidden] = canary
    _assert_both_reject(job)

    ack = MAC.build_job_acknowledgement(
        job_id=_golden()["job_id"],
        job_revision=1,
        state="received",
        bytes_downloaded=0,
        total_bytes=None,
        updated_at=1_785_528_051.0,
        result_code=None,
    ).value
    ack[forbidden] = canary
    for implementation in (FLEET, MAC):
        with pytest.raises(implementation.DesiredInstallProtocolError) as caught:
            implementation.validate_job_acknowledgement(copy.deepcopy(ack))
        assert caught.value.code == "desired_install_ack_invalid"
        assert canary not in str(caught.value)


def test_valid_wire_shapes_contain_no_path_credential_or_repository_fields() -> None:
    job = _golden()
    ack = MAC.build_job_acknowledgement(
        job_id=job["job_id"],
        job_revision=1,
        state="received",
        bytes_downloaded=0,
        total_bytes=None,
        updated_at=1_785_528_051.0,
        result_code=None,
    ).value
    forbidden_fields = {
        "path",
        "destination",
        "volume_uuid",
        "scope_id",
        "bookmark",
        "repository_id",
        "repository_url",
        "credential",
        "bearer",
        "engine_args",
        "runtime_args",
        "cleanup",
        "delete",
    }
    assert not forbidden_fields.intersection(job)
    assert not forbidden_fields.intersection(ack)
    wire = (_canonical(job) + _canonical(ack)).decode("utf-8")
    assert "/Volumes/" not in wire
    assert "https://" not in wire
    assert "huggingface.co" not in wire
    assert "Bearer " not in wire


def test_ack_contract_enums_and_bounds_match_across_consumers() -> None:
    assert tuple(FLEET.ACK_STATES) == tuple(MAC.JOB_STATES)
    assert frozenset(FLEET.ACK_RESULT_CODES) == frozenset(MAC.JOB_RESULT_CODES)
    assert MAC.MAX_JOB_ACKNOWLEDGEMENTS == 256
