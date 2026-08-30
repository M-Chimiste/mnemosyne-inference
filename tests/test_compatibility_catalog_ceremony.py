from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from compatibility_catalog.catalog import canonical_json
from compatibility_catalog.ceremony import (
    CeremonyError,
    assemble_envelope,
    create_detached_signature,
    generate_key,
    load_trust_key,
    verify_publication,
)


NOW = 2_000_000_000
PASSPHRASE = b"correct horse catalog battery staple"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _catalog(*, sequence: int, version: str | None = None) -> dict:
    return {
        "catalog_id": "mnemosyne-apple-silicon",
        "catalog_version": version or f"2033.05.{sequence}",
        "catalog_sequence": sequence,
        "issued_at": NOW,
        "expires_at": NOW + 86_400,
        "publisher": "mnemosyne",
        "build_revision": f"{sequence:040x}",
        "logical_models": [],
        "artifacts": [],
        "recipes": [],
    }


def _write(path: Path, value: dict, *, mode: int = 0o644) -> Path:
    path.write_bytes(canonical_json(value))
    path.chmod(mode)
    return path


def _vault(tmp_path: Path) -> Path:
    path = tmp_path / "offline-vault"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _key(
    tmp_path: Path,
    *,
    key_id: str,
    minimum_sequence: int = 1,
    maximum_sequence: int = 100,
) -> tuple[Path, Path]:
    vault = _vault(tmp_path)
    private = vault / f"{key_id}.pem"
    trust = tmp_path / f"{key_id}.trust.json"
    generate_key(
        private_key_path=private,
        trust_key_path=trust,
        key_id=key_id,
        passphrase=PASSPHRASE,
        valid_from=NOW - 100,
        valid_until=NOW + 100_000,
        minimum_catalog_sequence=minimum_sequence,
        maximum_catalog_sequence=maximum_sequence,
    )
    return private, trust


def _sign_and_assemble(
    tmp_path: Path,
    *,
    catalog: dict,
    private: Path,
    trust: Path,
    stem: str,
) -> Path:
    catalog_path = _write(tmp_path / f"{stem}.catalog.json", catalog)
    signature = tmp_path / f"{stem}.signature.json"
    envelope = tmp_path / f"{stem}.envelope.json"
    create_detached_signature(
        catalog_path=catalog_path,
        private_key_path=private,
        trust_key_path=trust,
        signature_path=signature,
        passphrase=PASSPHRASE,
    )
    assemble_envelope(
        catalog_path=catalog_path,
        signature_paths=[signature],
        trust_key_paths=[trust],
        output_path=envelope,
    )
    return envelope


def test_offline_key_sign_assemble_and_exact_publication_verification(
    tmp_path: Path,
) -> None:
    private, trust = _key(tmp_path, key_id="catalog-production-2033-a")
    catalog_path = _write(tmp_path / "catalog.json", _catalog(sequence=12))
    signature = tmp_path / "signature.json"
    envelope = tmp_path / "catalog.envelope.json"

    detached = create_detached_signature(
        catalog_path=catalog_path,
        private_key_path=private,
        trust_key_path=trust,
        signature_path=signature,
        passphrase=PASSPHRASE,
    )
    assembled = assemble_envelope(
        catalog_path=catalog_path,
        signature_paths=[signature],
        trust_key_paths=[trust],
        output_path=envelope,
    )
    verified = verify_publication(
        envelope_path=envelope,
        trust_key_paths=[trust],
        now=NOW,
        required_key_ids=["catalog-production-2033-a"],
        expected_envelope_sha256=assembled["envelope_sha256"],
    )

    assert private.stat().st_mode & 0o777 == 0o600
    assert trust.stat().st_mode & 0o777 == 0o644
    assert b"ENCRYPTED PRIVATE KEY" in private.read_bytes()
    assert detached["catalog_digest"] == assembled["catalog_digest"]
    assert verified == assembled
    assert verified["signing_key_ids"] == ["catalog-production-2033-a"]
    assert json.loads(envelope.read_text())["catalog"]["recipes"] == []


def test_rotation_envelope_requires_every_submitted_signature_to_verify(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_private, first_trust = _key(
        first_root,
        key_id="catalog-production-2033-a",
    )
    second_private, second_trust = _key(
        second_root,
        key_id="catalog-production-2033-b",
    )
    catalog_path = _write(tmp_path / "catalog.json", _catalog(sequence=20))
    first_signature = tmp_path / "a.signature.json"
    second_signature = tmp_path / "b.signature.json"
    create_detached_signature(
        catalog_path=catalog_path,
        private_key_path=first_private,
        trust_key_path=first_trust,
        signature_path=first_signature,
        passphrase=PASSPHRASE,
    )
    create_detached_signature(
        catalog_path=catalog_path,
        private_key_path=second_private,
        trust_key_path=second_trust,
        signature_path=second_signature,
        passphrase=PASSPHRASE,
    )
    envelope = tmp_path / "rotation.envelope.json"
    assemble_envelope(
        catalog_path=catalog_path,
        signature_paths=[second_signature, first_signature],
        trust_key_paths=[second_trust, first_trust],
        output_path=envelope,
    )

    receipt = verify_publication(
        envelope_path=envelope,
        trust_key_paths=[first_trust, second_trust],
        now=NOW,
        required_key_ids=[
            "catalog-production-2033-a",
            "catalog-production-2033-b",
        ],
    )
    assert receipt["signing_key_ids"] == [
        "catalog-production-2033-a",
        "catalog-production-2033-b",
    ]

    with pytest.raises(CeremonyError, match="ceremony_signature_untrusted"):
        verify_publication(
            envelope_path=envelope,
            trust_key_paths=[first_trust],
            now=NOW,
        )


def test_publication_verification_rejects_tamper_digest_and_rollback(
    tmp_path: Path,
) -> None:
    private, trust = _key(tmp_path, key_id="catalog-production-2033-a")
    current = _sign_and_assemble(
        tmp_path,
        catalog=_catalog(sequence=11),
        private=private,
        trust=trust,
        stem="current",
    )
    older = _sign_and_assemble(
        tmp_path,
        catalog=_catalog(sequence=10),
        private=private,
        trust=trust,
        stem="older",
    )
    conflict = _sign_and_assemble(
        tmp_path,
        catalog=_catalog(sequence=11, version="2033.05.11-conflict"),
        private=private,
        trust=trust,
        stem="conflict",
    )

    with pytest.raises(CeremonyError, match="ceremony_publication_downgrade"):
        verify_publication(
            envelope_path=older,
            trust_key_paths=[trust],
            now=NOW,
            previous_envelope_path=current,
        )
    with pytest.raises(CeremonyError, match="ceremony_publication_conflict"):
        verify_publication(
            envelope_path=conflict,
            trust_key_paths=[trust],
            now=NOW,
            previous_envelope_path=current,
        )
    with pytest.raises(
        CeremonyError,
        match="ceremony_publication_digest_mismatch",
    ):
        verify_publication(
            envelope_path=current,
            trust_key_paths=[trust],
            now=NOW,
            expected_envelope_sha256="sha256:" + "0" * 64,
        )

    tampered_value = json.loads(current.read_text())
    tampered_value["catalog"]["catalog_version"] = "tampered"
    tampered = _write(tmp_path / "tampered.envelope.json", tampered_value)
    with pytest.raises(CeremonyError, match="ceremony_publication_invalid"):
        verify_publication(
            envelope_path=tampered,
            trust_key_paths=[trust],
            now=NOW,
        )


def test_private_keys_cannot_be_written_or_read_from_repository(
    tmp_path: Path,
) -> None:
    repository_private = REPOSITORY_ROOT / "compatibility_catalog" / "do-not-create.pem"
    assert not repository_private.exists()
    with pytest.raises(CeremonyError, match="ceremony_private_key_in_repository"):
        generate_key(
            private_key_path=repository_private,
            trust_key_path=tmp_path / "trust.json",
            key_id="catalog-production-2033-a",
            passphrase=PASSPHRASE,
        )
    assert not repository_private.exists()


def test_private_inputs_require_owner_only_permissions(tmp_path: Path) -> None:
    private, trust = _key(tmp_path, key_id="catalog-production-2033-a")
    private.chmod(0o640)
    catalog = _write(tmp_path / "catalog.json", _catalog(sequence=1))
    with pytest.raises(
        CeremonyError,
        match="ceremony_private_permissions_invalid",
    ):
        create_detached_signature(
            catalog_path=catalog,
            private_key_path=private,
            trust_key_path=trust,
            signature_path=tmp_path / "signature.json",
            passphrase=PASSPHRASE,
        )


def test_repository_golden_key_is_forbidden_as_publication_authority(
    tmp_path: Path,
) -> None:
    public_key = "ebVWLo_mVPlAeLES6KmLp5AfhTrmlb7X4OORC60ElmQ"
    decoded = base64.urlsafe_b64decode(public_key + "=")
    trust = _write(
        tmp_path / "test-key.trust.json",
        {
            "schema_version": 1,
            "catalog_id": "mnemosyne-apple-silicon",
            "publisher": "mnemosyne",
            "key_id": "test-catalog-2026-a",
            "algorithm": "Ed25519",
            "public_key": public_key,
            "public_key_digest": "sha256:"
            + __import__("hashlib").sha256(decoded).hexdigest(),
            "valid_from": 0,
            "valid_until": 4_102_444_800,
            "minimum_catalog_sequence": 1,
            "maximum_catalog_sequence": 9_007_199_254_740_991,
        },
    )
    with pytest.raises(CeremonyError, match="ceremony_test_key_forbidden"):
        load_trust_key(trust)


def test_detached_signature_is_bound_to_exact_catalog(tmp_path: Path) -> None:
    private, trust = _key(tmp_path, key_id="catalog-production-2033-a")
    catalog_one = _write(tmp_path / "one.json", _catalog(sequence=1))
    catalog_two = _write(tmp_path / "two.json", _catalog(sequence=2))
    signature = tmp_path / "one.signature.json"
    create_detached_signature(
        catalog_path=catalog_one,
        private_key_path=private,
        trust_key_path=trust,
        signature_path=signature,
        passphrase=PASSPHRASE,
    )
    with pytest.raises(
        CeremonyError,
        match="ceremony_signature_catalog_mismatch",
    ):
        assemble_envelope(
            catalog_path=catalog_two,
            signature_paths=[signature],
            trust_key_paths=[trust],
            output_path=tmp_path / "bad.envelope.json",
        )
