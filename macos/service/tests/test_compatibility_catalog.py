from __future__ import annotations

import base64
import copy
import fcntl
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mnemosyne_macos.compatibility_catalog import (
    CatalogRollbackError,
    CatalogStore,
    CatalogStoreError,
    CatalogTrustError,
    CatalogValidationError,
    CatalogVerifier,
    TrustedCatalogKey,
    artifact_manifest_digest,
    built_in_empty_catalog,
    canonical_json,
    catalog_digest,
    parse_catalog_json,
    signing_message,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_V1 = REPOSITORY_ROOT / "compatibility_catalog" / "v1"
TEST_NOW = 1_790_000_000
TEST_SEED_A = bytes(range(1, 33))
TEST_SEED_B = bytes(range(33, 65))


def _golden() -> dict:
    return json.loads(
        (PROTOCOL_V1 / "catalog.golden.json").read_text(encoding="utf-8")
    )


def _gguf_layout_golden() -> dict:
    return json.loads(
        (PROTOCOL_V1 / "catalog.gguf-layout.golden.json").read_text(
            encoding="utf-8"
        )
    )


def _private(seed: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(seed)


def _trusted(
    key_id: str,
    seed: bytes,
    **window,
) -> TrustedCatalogKey:
    public = _private(seed).public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return TrustedCatalogKey(key_id=key_id, public_key=public, **window)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _resign(
    value: dict,
    *signers: tuple[str, bytes],
) -> dict:
    candidate = copy.deepcopy(value)
    candidate["catalog_digest"] = catalog_digest(candidate["catalog"])
    candidate["signatures"] = sorted(
        (
            {
                "key_id": key_id,
                "algorithm": "Ed25519",
                "signature": _encode(
                    _private(seed).sign(signing_message(candidate["catalog"]))
                ),
            }
            for key_id, seed in signers
        ),
        key=lambda row: row["key_id"],
    )
    return candidate


@pytest.fixture
def verifier() -> CatalogVerifier:
    key = _trusted("test-catalog-2026-a", TEST_SEED_A)
    return CatalogVerifier({key.key_id: key})


def test_test_signed_golden_verifies_deterministically(
    verifier: CatalogVerifier,
) -> None:
    golden = _golden()
    verified = verifier.verify(golden, now=TEST_NOW)
    assert verified.catalog_sequence == 42
    assert verified.catalog_version == "test-2026.08.1"
    assert verified.catalog_digest == golden["catalog_digest"]
    assert verified.signing_key_ids == ("test-catalog-2026-a",)
    assert verified.source == "signed"
    assert verified.canonical_catalog == canonical_json(golden["catalog"])
    assert verified.envelope() == golden

    spaced = json.dumps(golden, indent=4, ensure_ascii=False).encode()
    assert verifier.verify(spaced, now=TEST_NOW).canonical_envelope == (
        verified.canonical_envelope
    )


def test_test_signed_gguf_layout_golden_verifies_and_is_signature_bound(
    verifier: CatalogVerifier,
) -> None:
    golden = _gguf_layout_golden()
    verified = verifier.verify(golden, now=TEST_NOW)
    artifact = verified.catalog()["artifacts"][0]
    assert verified.catalog_sequence == 43
    assert artifact["manifest_digest"] == artifact_manifest_digest(
        artifact["files"]
    )
    assert artifact["gguf_layout"] == {
        "kind": "gguf-file-set",
        "primary_file": "example-flash-vnext-Q4_K_M-00001-of-00002.gguf",
        "required_shards": [
            "example-flash-vnext-Q4_K_M-00002-of-00002.gguf"
        ],
        "selected_projector_file": "mmproj-example-flash-vnext-f16.gguf",
    }

    tampered = copy.deepcopy(golden)
    tampered["catalog"]["artifacts"][0]["gguf_layout"][
        "selected_projector_file"
    ] = None
    with pytest.raises(CatalogTrustError) as digest_error:
        verifier.verify(tampered, now=TEST_NOW)
    assert digest_error.value.code == "catalog_digest_mismatch"

    incomplete = copy.deepcopy(golden)
    incomplete["catalog"]["artifacts"][0]["gguf_layout"][
        "required_shards"
    ] = []
    with pytest.raises(CatalogValidationError) as layout_error:
        verifier.verify(
            _resign(incomplete, ("test-catalog-2026-a", TEST_SEED_A)),
            now=TEST_NOW,
        )
    assert layout_error.value.code == "catalog_artifact_layout_invalid"

    ambiguous_legacy = copy.deepcopy(golden)
    ambiguous_legacy["catalog"]["artifacts"][0].pop("gguf_layout")
    with pytest.raises(CatalogValidationError) as ambiguous_error:
        verifier.verify(
            _resign(ambiguous_legacy, ("test-catalog-2026-a", TEST_SEED_A)),
            now=TEST_NOW,
        )
    assert ambiguous_error.value.code == "catalog_artifact_layout_invalid"

    unsupported_projector = copy.deepcopy(golden)
    unsupported_projector["catalog"]["artifacts"][0]["format"] = "ds4-weights"
    with pytest.raises(CatalogValidationError) as projector_error:
        verifier.verify(
            _resign(unsupported_projector, ("test-catalog-2026-a", TEST_SEED_A)),
            now=TEST_NOW,
        )
    assert projector_error.value.code == "catalog_artifact_layout_invalid"


def test_test_key_fixture_matches_the_deterministic_public_key() -> None:
    keys = json.loads((PROTOCOL_V1 / "test_keys.json").read_text())
    expected = _trusted("test-catalog-2026-a", TEST_SEED_A)
    assert keys["keys"] == [
        {
            "key_id": expected.key_id,
            "algorithm": "Ed25519",
            "public_key": _encode(expected.public_key),
        }
    ]


def test_payload_and_signature_tampering_fail_closed(
    verifier: CatalogVerifier,
) -> None:
    tampered = _golden()
    tampered["catalog"]["recipes"][0]["context"]["guaranteed_tokens"] = 4096
    with pytest.raises(CatalogTrustError) as digest_error:
        verifier.verify(tampered, now=TEST_NOW)
    assert digest_error.value.code == "catalog_digest_mismatch"

    tampered["catalog_digest"] = catalog_digest(tampered["catalog"])
    with pytest.raises(CatalogTrustError) as signature_error:
        verifier.verify(tampered, now=TEST_NOW)
    assert signature_error.value.code == "catalog_signature_invalid"


def test_unknown_key_and_key_window_are_rejected() -> None:
    unknown = _resign(
        _golden(),
        ("test-catalog-2026-b", TEST_SEED_B),
    )
    old = _trusted("test-catalog-2026-a", TEST_SEED_A)
    verifier = CatalogVerifier({old.key_id: old})
    with pytest.raises(CatalogTrustError) as error:
        verifier.verify(unknown, now=TEST_NOW)
    assert error.value.code == "catalog_unknown_key"

    bounded = _trusted(
        "test-catalog-2026-a",
        TEST_SEED_A,
        minimum_catalog_sequence=43,
    )
    with pytest.raises(CatalogTrustError) as bounded_error:
        CatalogVerifier({bounded.key_id: bounded}).verify(
            _golden(), now=TEST_NOW
        )
    assert bounded_error.value.code == "catalog_unknown_key"


def test_dual_signature_supports_pinned_key_rotation() -> None:
    dual = _resign(
        _golden(),
        ("test-catalog-2026-a", TEST_SEED_A),
        ("test-catalog-2026-b", TEST_SEED_B),
    )
    new = _trusted("test-catalog-2026-b", TEST_SEED_B)
    verified = CatalogVerifier({new.key_id: new}).verify(dual, now=TEST_NOW)
    assert verified.signing_key_ids == ("test-catalog-2026-b",)

    both = {
        key.key_id: key
        for key in (
            _trusted("test-catalog-2026-a", TEST_SEED_A),
            new,
        )
    }
    assert CatalogVerifier(both).verify(
        dual, now=TEST_NOW
    ).signing_key_ids == (
        "test-catalog-2026-a",
        "test-catalog-2026-b",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows.reverse(),
        lambda rows: rows.append(copy.deepcopy(rows[0])),
    ],
)
def test_signature_set_must_be_sorted_and_unique(
    mutation,
) -> None:
    dual = _resign(
        _golden(),
        ("test-catalog-2026-a", TEST_SEED_A),
        ("test-catalog-2026-b", TEST_SEED_B),
    )
    mutation(dual["signatures"])
    keys = {
        key.key_id: key
        for key in (
            _trusted("test-catalog-2026-a", TEST_SEED_A),
            _trusted("test-catalog-2026-b", TEST_SEED_B),
        )
    }
    with pytest.raises(CatalogValidationError) as error:
        CatalogVerifier(keys).verify(dual, now=TEST_NOW)
    assert error.value.code == "catalog_order_invalid"


def test_expired_and_not_yet_valid_catalogs_are_rejected(
    verifier: CatalogVerifier,
) -> None:
    golden = _golden()
    with pytest.raises(CatalogTrustError) as expired:
        verifier.verify(golden, now=golden["catalog"]["expires_at"])
    assert expired.value.code == "catalog_expired"
    with pytest.raises(CatalogTrustError) as future:
        verifier.verify(
            golden,
            now=golden["catalog"]["issued_at"] - 301,
        )
    assert future.value.code == "catalog_not_yet_valid"


def test_duplicate_json_members_and_unsigned_offline_catalog_are_not_trusted(
    verifier: CatalogVerifier,
) -> None:
    with pytest.raises(Exception) as duplicate:
        parse_catalog_json(b'{"schema_version":1,"schema_version":1}')
    assert getattr(duplicate.value, "code", None) == "catalog_invalid_json"
    with pytest.raises(CatalogValidationError) as unsigned:
        verifier.verify(
            built_in_empty_catalog().canonical_envelope,
            now=TEST_NOW,
        )
    assert unsigned.value.code == "catalog_schema_invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["catalog"]["artifacts"][0]["files"][0].update(
            path="/Volumes/Athena/model.gguf"
        ),
        lambda value: value["catalog"]["artifacts"][0]["source"].update(
            url="https://attacker.invalid/model"
        ),
        lambda value: value["catalog"]["recipes"][0]["launch"].update(
            extra_args=["--override"]
        ),
        lambda value: value["catalog"]["recipes"][0].update(
            credential="secret"
        ),
    ],
)
def test_paths_urls_args_and_credentials_never_enter_signed_catalog(
    verifier: CatalogVerifier,
    mutation,
) -> None:
    candidate = _golden()
    mutation(candidate)
    candidate = _resign(
        candidate,
        ("test-catalog-2026-a", TEST_SEED_A),
    )
    with pytest.raises(CatalogValidationError) as error:
        verifier.verify(candidate, now=TEST_NOW)
    assert error.value.code == "catalog_schema_invalid"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda value: value["catalog"]["artifacts"][0].update(
                manifest_digest="sha256:" + "9" * 64
            ),
            "catalog_manifest_digest_invalid",
        ),
        (
            lambda value: value["catalog"]["artifacts"][0].update(
                total_size_bytes=999
            ),
            "catalog_artifact_size_invalid",
        ),
        (
            lambda value: value["catalog"]["recipes"][0].update(
                artifact_id="missing-artifact"
            ),
            "catalog_reference_invalid",
        ),
        (
            lambda value: value["catalog"]["recipes"][0]["runtime"].update(
                engine="omlx"
            ),
            "catalog_recipe_invalid",
        ),
        (
            lambda value: value["catalog"]["recipes"][0]["context"].update(
                guaranteed_tokens=65536
            ),
            "catalog_context_invalid",
        ),
        (
            lambda value: value["catalog"]["recipes"][0]["memory"].update(
                weights_bytes=999
            ),
            "catalog_memory_invalid",
        ),
    ],
)
def test_signed_content_relationships_are_strict(
    verifier: CatalogVerifier,
    mutation,
    code: str,
) -> None:
    candidate = _golden()
    mutation(candidate)
    candidate = _resign(
        candidate,
        ("test-catalog-2026-a", TEST_SEED_A),
    )
    with pytest.raises(CatalogValidationError) as error:
        verifier.verify(candidate, now=TEST_NOW)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("engine", "artifact_format", "launch"),
    [
        (
            "llama.cpp",
            "gguf",
            {
                "engine": "llama.cpp",
                "parallel_slots": 1,
                "gpu_offload": "all",
                "flash_attention": "automatic",
            },
        ),
        (
            "omlx",
            "mlx",
            {
                "engine": "omlx",
                "scheduler_slots": 1,
                "memory_guard": "required",
            },
        ),
        (
            "ds4",
            "ds4-weights",
            {
                "engine": "ds4",
                "batched_sessions": 1,
                "execution_mode": "single-node",
            },
        ),
    ],
)
def test_verifier_accepts_all_typed_mac_engine_contracts(
    verifier: CatalogVerifier,
    engine: str,
    artifact_format: str,
    launch: dict,
) -> None:
    candidate = _golden()
    artifact = candidate["catalog"]["artifacts"][0]
    artifact["format"] = artifact_format
    recipe = candidate["catalog"]["recipes"][0]
    recipe["engine"] = engine
    recipe["runtime"]["engine"] = engine
    recipe["launch"] = launch
    candidate = _resign(
        candidate,
        ("test-catalog-2026-a", TEST_SEED_A),
    )
    assert verifier.verify(candidate, now=TEST_NOW).source == "signed"


def _at_sequence(value: dict, sequence: int) -> dict:
    candidate = copy.deepcopy(value)
    candidate["catalog"]["catalog_sequence"] = sequence
    candidate["catalog"]["catalog_version"] = f"test-{sequence}"
    return _resign(
        candidate,
        ("test-catalog-2026-a", TEST_SEED_A),
    )


def test_store_activates_atomically_and_rejects_downgrade_or_conflict(
    tmp_path: Path,
    verifier: CatalogVerifier,
) -> None:
    store = CatalogStore(tmp_path / "catalog-state", verifier)
    assert store.load(now=TEST_NOW).source == "built_in"
    first = store.activate(_golden(), now=TEST_NOW)
    assert first.changed is True
    assert store.activate(_golden(), now=TEST_NOW).changed is False
    assert store.load(now=TEST_NOW).catalog_sequence == 42

    upgraded = _at_sequence(_golden(), 43)
    assert store.activate(
        upgraded, now=TEST_NOW
    ).catalog.catalog_sequence == 43
    previous = json.loads(
        (tmp_path / "catalog-state" / "previous.catalog.json").read_text()
    )
    assert previous["catalog"]["catalog_sequence"] == 42

    with pytest.raises(CatalogRollbackError) as downgrade:
        store.activate(_golden(), now=TEST_NOW)
    assert downgrade.value.code == "catalog_downgrade_rejected"

    conflict = copy.deepcopy(upgraded)
    conflict["catalog"]["catalog_version"] = "test-43-conflict"
    conflict = _resign(
        conflict,
        ("test-catalog-2026-a", TEST_SEED_A),
    )
    with pytest.raises(CatalogRollbackError) as conflict_error:
        store.activate(conflict, now=TEST_NOW)
    assert conflict_error.value.code == "catalog_version_conflict"

    directory_mode = stat_mode(tmp_path / "catalog-state")
    active_mode = stat_mode(tmp_path / "catalog-state" / "active.catalog.json")
    assert directory_mode == 0o700
    assert active_mode == 0o600


def test_rejected_update_never_changes_last_known_good(
    tmp_path: Path,
    verifier: CatalogVerifier,
) -> None:
    store = CatalogStore(tmp_path / "catalog-state", verifier)
    store.activate(_golden(), now=TEST_NOW)
    active = tmp_path / "catalog-state" / "active.catalog.json"
    before = active.read_bytes()
    candidate = _at_sequence(_golden(), 43)
    candidate["catalog"]["catalog_version"] = "tampered-after-signing"
    with pytest.raises(CatalogTrustError):
        store.activate(candidate, now=TEST_NOW)
    assert active.read_bytes() == before
    assert store.load(now=TEST_NOW).catalog_sequence == 42


def test_store_lock_contention_fails_closed_without_touching_last_known_good(
    tmp_path: Path,
    verifier: CatalogVerifier,
) -> None:
    directory = tmp_path / "catalog-state"
    store = CatalogStore(directory, verifier)
    store.activate(_golden(), now=TEST_NOW)
    active = directory / "active.catalog.json"
    before = active.read_bytes()
    descriptor = os.open(directory / "catalog.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CatalogStoreError) as error:
            store.activate(_at_sequence(_golden(), 43), now=TEST_NOW)
        assert error.value.code == "catalog_store_unavailable"
        assert active.read_bytes() == before
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert store.load(now=TEST_NOW).catalog_sequence == 42


def test_corrupt_or_expired_active_with_high_watermark_uses_offline_empty(
    tmp_path: Path,
    verifier: CatalogVerifier,
) -> None:
    directory = tmp_path / "catalog-state"
    store = CatalogStore(directory, verifier)
    store.activate(_golden(), now=TEST_NOW)
    store.activate(_at_sequence(_golden(), 43), now=TEST_NOW)
    assert json.loads((directory / "previous.catalog.json").read_text())[
        "catalog"
    ]["catalog_sequence"] == 42

    (directory / "active.catalog.json").write_bytes(b"{")
    loaded = store.load(now=TEST_NOW)
    assert loaded.source == "built_in"
    assert loaded.catalog_sequence == 0

    store.activate(_at_sequence(_golden(), 43), now=TEST_NOW)
    expired = store.load(now=_golden()["catalog"]["expires_at"])
    assert expired.source == "built_in"
    assert expired.catalog_sequence == 0

    with pytest.raises(CatalogRollbackError):
        store.activate(_golden(), now=TEST_NOW)


def test_artifact_manifest_digest_is_order_and_field_exact() -> None:
    files = _golden()["catalog"]["artifacts"][0]["files"]
    expected = artifact_manifest_digest(files)
    changed = copy.deepcopy(files)
    changed[0]["size_bytes"] += 1
    assert artifact_manifest_digest(changed) != expected
    changed = copy.deepcopy(files)
    changed[0]["sha256"] = "sha256:" + "9" * 64
    assert artifact_manifest_digest(changed) != expected


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777
