from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import sqlite3
import time
from uuid import uuid4

import pytest

from mnemosyne_macos.install_provenance import (
    ArtifactAuthority,
    CleanupInstallation,
    CleanupReason,
    DestinationStateBefore,
    ExclusiveManagedProof,
    ManagedCreationClaim,
    MAX_OWNED_FILES,
    OwnershipClass,
    OwnedFile,
    ProvenanceConflictError,
    ProvenanceDataError,
    ProvenanceProofRejected,
    SourceKind,
    canonical_owned_files_json,
    decide_cleanup_eligibility,
    default_provenance,
    destination_binding_digest,
    exclusive_proof_from_claim,
    local_pinned_source_digest,
    owned_manifest_digest,
    provenance_from_exclusive_proof,
)
from mnemosyne_macos.install_store import InstallRecord, InstallStore


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _create_installed(
    store: InstallStore,
    *,
    alias: str = "model",
    destination: str = "/Volumes/Athena/models/llama.cpp/owner/model",
) -> InstallRecord:
    record = store.create(
        repo_id="owner/model",
        engine="llama.cpp",
        storage="athena-models",
        alias=alias,
        destination=destination,
        revision="a" * 40,
        filename="weights/model.gguf",
        download_files=("weights/model.gguf", "metadata/marker.json"),
        family="test",
        total_bytes=4096,
    )
    return store.update(
        record.id,
        status="installed",
        bytes_downloaded=4096,
    )


def _proof(record: InstallRecord) -> ExclusiveManagedProof:
    files = (
        OwnedFile(
            path="weights/model.gguf",
            size_bytes=4096,
            sha256=_digest("model"),
        ),
        OwnedFile(
            path="metadata/marker.json",
            size_bytes=0,
            sha256=_digest("empty"),
        ),
    )
    storage_root = record.destination.split("/llama.cpp/", 1)[0]
    binding = {
        "storage_location_id": "3c7ba6ca-95a0-4ac0-a73a-fda3bcd03002",
        "storage_binding_generation": 7,
        "storage_lexical_root": storage_root,
        "lexical_destination": record.destination,
        "storage_volume_uuid": "ATHENA-UUID",
        "storage_scope_id": "b" * 64,
    }
    return ExclusiveManagedProof(
        installation_id=record.id,
        **binding,
        destination_binding_digest=destination_binding_digest(**binding),
        catalog_id="mnemosyne-apple-silicon",
        logical_model_id="model.logical",
        artifact_id="model.gguf.q4",
        recipe_id="model.llama-cpp.q4",
        resolved_revision=record.revision or "",
        catalog_digest=_digest("catalog"),
        manifest_digest=owned_manifest_digest(files),
        owned_files=files,
        destination_state_before=DestinationStateBefore.ABSENT,
        destination_created_by_transaction=True,
        preexisting_entries=(),
        extra_entries=(),
        creation_transaction_id=str(uuid4()),
    )


def _creation_claim(record: InstallRecord) -> ManagedCreationClaim:
    storage_root = record.destination.split("/llama.cpp/", 1)[0]
    binding = {
        "storage_location_id": "3c7ba6ca-95a0-4ac0-a73a-fda3bcd03002",
        "storage_binding_generation": 7,
        "storage_lexical_root": storage_root,
        "lexical_destination": record.destination,
        "storage_volume_uuid": "ATHENA-UUID",
        "storage_scope_id": "b" * 64,
    }
    return ManagedCreationClaim(
        installation_id=record.id,
        **binding,
        destination_binding_digest=destination_binding_digest(**binding),
        resolved_revision=record.revision or "",
        artifact_authority=ArtifactAuthority.LOCAL_PINNED_DISCOVERY,
        source_identity_digest=local_pinned_source_digest(
            repo_id=record.repo_id,
            engine=record.engine,
            resolved_revision=record.revision or "",
            download_files=("metadata/marker.json", "weights/model.gguf"),
        ),
        creation_transaction_id=str(uuid4()),
        directory_device=42,
        directory_inode=84,
    )


def _decision(record: InstallRecord, proof: ExclusiveManagedProof):
    return decide_cleanup_eligibility(
        record.id,
        CleanupInstallation(
            installation_id=record.id,
            status=record.status,
            destination=record.destination,
            resolved_revision=record.revision,
            total_bytes=record.total_bytes,
        ),
        provenance_from_exclusive_proof(proof),
    )


def _create_legacy_database(path, rows: list[tuple[str, str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE native_model_installs (
            id TEXT PRIMARY KEY,
            repo_id TEXT NOT NULL,
            engine TEXT NOT NULL,
            storage TEXT NOT NULL,
            alias TEXT NOT NULL,
            destination TEXT NOT NULL,
            status TEXT NOT NULL,
            revision TEXT,
            filename TEXT,
            family TEXT,
            bytes_downloaded INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER,
            hidden INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            pid INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    now = time.time()
    connection.executemany(
        """
        INSERT INTO native_model_installs (
            id, repo_id, engine, storage, alias, destination, status,
            revision, filename, family, bytes_downloaded, total_bytes,
            hidden, error, pid, created_at, updated_at
        ) VALUES (?, 'owner/model', 'llama.cpp', 'internal', ?, ?, ?,
                  'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'model.gguf',
                  NULL, 1, 1, ?, NULL, NULL, ?, ?)
        """,
        [
            (
                installation_id,
                "shared-alias",
                f"/models/{installation_id}",
                status,
                hidden,
                now + index,
                now + index,
            )
            for index, (installation_id, status, hidden) in enumerate(rows)
        ],
    )
    connection.commit()
    connection.close()


def test_owned_manifest_is_canonical_and_allows_zero_byte_files() -> None:
    files = (
        OwnedFile(path="z/model.gguf", size_bytes=10, sha256=_digest("z")),
        OwnedFile(path="a/marker", size_bytes=0, sha256=_digest("empty")),
    )

    encoded = canonical_owned_files_json(files)

    assert json.loads(encoded) == [
        {"path": "a/marker", "sha256": _digest("empty"), "size_bytes": 0},
        {"path": "z/model.gguf", "sha256": _digest("z"), "size_bytes": 10},
    ]
    assert encoded == canonical_owned_files_json(tuple(reversed(files)))
    assert owned_manifest_digest(files) == owned_manifest_digest(tuple(reversed(files)))


def test_owned_manifest_is_bounded_and_rejects_unsafe_paths() -> None:
    with pytest.raises(ProvenanceDataError, match="owned_manifest_invalid"):
        canonical_owned_files_json(
            tuple(
                OwnedFile(path=f"files/{index}", size_bytes=0, sha256=_digest("x"))
                for index in range(MAX_OWNED_FILES + 1)
            )
        )
    with pytest.raises(ProvenanceDataError, match="owned_manifest_invalid"):
        canonical_owned_files_json(
            (OwnedFile(path="../outside", size_bytes=1, sha256=_digest("x")),)
        )
    with pytest.raises(ProvenanceDataError, match="owned_manifest_invalid"):
        canonical_owned_files_json(
            (
                OwnedFile(path="weights", size_bytes=1, sha256=_digest("parent")),
                OwnedFile(
                    path="weights/model.gguf",
                    size_bytes=1,
                    sha256=_digest("child"),
                ),
            )
        )


def test_owned_manifest_preserves_case_distinct_paths_for_case_sensitive_volumes() -> None:
    files = (
        OwnedFile(path="weights/A.bin", size_bytes=1, sha256=_digest("upper")),
        OwnedFile(path="weights/a.bin", size_bytes=2, sha256=_digest("lower")),
    )

    assert [
        item["path"] for item in json.loads(canonical_owned_files_json(files))
    ] == ["weights/A.bin", "weights/a.bin"]


def test_local_pinned_source_digest_is_canonical_but_case_exact() -> None:
    common = {
        "repo_id": "owner/model",
        "engine": "llama.cpp",
        "resolved_revision": "a" * 40,
    }
    first = local_pinned_source_digest(
        **common,
        download_files=("weights/model.gguf", "metadata/config.json"),
    )
    reordered = local_pinned_source_digest(
        **common,
        download_files=("metadata/config.json", "weights/model.gguf"),
    )
    different_case = local_pinned_source_digest(
        **common,
        download_files=("metadata/config.json", "weights/Model.gguf"),
    )

    assert first == reordered
    assert first != different_case


def test_revision_two_local_claim_is_durable_and_completes_exact_proof(
    tmp_path,
) -> None:
    store = InstallStore(tmp_path / "state.db")
    installation_id = str(uuid4())
    template = InstallRecord(
        id=installation_id,
        repo_id="owner/model",
        engine="llama.cpp",
        storage="athena-models",
        alias="model",
        destination="/Volumes/Athena/models/llama.cpp/owner/model",
        status="queued",
        revision="a" * 40,
        filename="weights/model.gguf",
        total_bytes=4096,
    )
    claim = _creation_claim(template)
    try:
        record = store.create(
            installation_id=installation_id,
            creation_claim=claim,
            repo_id=template.repo_id,
            engine=template.engine,
            storage=template.storage,
            alias=template.alias,
            destination=template.destination,
            revision=template.revision,
            filename=template.filename,
            download_files=("weights/model.gguf", "metadata/marker.json"),
            family=None,
            # Planned transfer size excludes local download metadata. Revision
            # two therefore binds cleanup to the captured tree, not this UI
            # estimate.
            total_bytes=4096,
        )
        assert store.get_creation_claim(record.id) == claim
        installed = store.update(
            record.id,
            status="installed",
            bytes_downloaded=4113,
        )
        files = (
            OwnedFile(
                path="weights/model.gguf",
                size_bytes=4096,
                sha256=_digest("model"),
            ),
            OwnedFile(
                path=".cache/huggingface/download/metadata",
                size_bytes=17,
                sha256=_digest("metadata"),
            ),
        )
        proof = exclusive_proof_from_claim(claim, files)
        persisted = store.record_exclusive_managed_proof(record.id, proof)

        assert persisted.artifact_authority is (
            ArtifactAuthority.LOCAL_PINNED_DISCOVERY
        )
        assert persisted.catalog_id is None
        assert persisted.directory_device == 42
        assert persisted.directory_inode == 84
        assert persisted.provenance_revision == 2
        assert sum(item.size_bytes for item in files) != installed.total_bytes
        assert store.cleanup_eligibility(record.id).reason is CleanupReason.ELIGIBLE
    finally:
        store.close()

    reopened = InstallStore(tmp_path / "state.db")
    try:
        assert reopened.get_creation_claim(installation_id) == claim
        assert reopened.cleanup_eligibility(installation_id).eligible
    finally:
        reopened.close()


def test_legacy_rows_in_every_state_migrate_to_unknown_without_cleanup_authority(
    tmp_path,
) -> None:
    path = tmp_path / "legacy.db"
    rows = [
        (f"legacy-{status}", status, index % 2)
        for index, status in enumerate(
            (
                "queued",
                "downloading",
                "partial",
                "downloaded",
                "registering",
                "installed",
                "failed",
                "cancelled",
                "deleted",
            )
        )
    ]
    _create_legacy_database(path, rows)

    store = InstallStore(path)
    try:
        for installation_id, _status, hidden in rows:
            record = store.get_by_id(installation_id)
            provenance = store.get_provenance(installation_id)
            assert record.hidden == hidden
            assert provenance.installation_id == installation_id
            assert provenance.source_kind is SourceKind.MANAGED_DOWNLOAD
            assert provenance.ownership_class is OwnershipClass.UNKNOWN
            assert provenance.storage_location_id is None
            assert provenance.owned_files is None
            assert provenance.provenance_revision == 0
            assert store.cleanup_eligibility(installation_id).reason is (
                CleanupReason.OWNERSHIP_NOT_EXCLUSIVE_MANAGED
            )
        assert {record.id for record in store.list(limit=100)} == {
            installation_id
            for installation_id, _status, hidden in rows
            if not hidden
        }
    finally:
        store.close()

    reopened = InstallStore(path)
    try:
        count = reopened._connection.execute(  # type: ignore[attr-defined]
            "SELECT COUNT(*) FROM native_model_install_provenance"
        ).fetchone()[0]
        assert count == len(rows)
    finally:
        reopened.close()


def test_new_installs_and_status_transitions_remain_unknown(tmp_path) -> None:
    store = InstallStore(tmp_path / "state.db")
    try:
        record = store.create(
            repo_id="owner/model",
            engine="llama.cpp",
            storage="internal",
            alias="model",
            destination="/models/owner/model",
            revision="a" * 40,
            filename="model.gguf",
            family=None,
            total_bytes=1,
        )
        for status in (
            "downloading",
            "partial",
            "queued",
            "registering",
            "downloaded",
            "installed",
        ):
            store.update(record.id, status=status)
            provenance = store.get_provenance(record.id)
            assert provenance == default_provenance(record.id)
            assert provenance.provenance_revision == 0
            assert not store.cleanup_eligibility(record.id).eligible
    finally:
        store.close()


def test_alias_reuse_never_substitutes_for_exact_installation_identity(
    tmp_path,
) -> None:
    store = InstallStore(tmp_path / "state.db")
    try:
        first = _create_installed(store, alias="same")
        proved = store.record_exclusive_managed_proof(first.id, _proof(first))
        second = _create_installed(
            store,
            alias="same",
            destination="/Volumes/Athena/models/llama.cpp/owner/other",
        )

        assert store.latest_for_alias("same").id == second.id
        assert store.get_by_id(first.id).id == first.id
        assert proved.installation_id == first.id
        assert store.cleanup_eligibility(first.id).eligible
        assert store.cleanup_eligibility(second.id).reason is (
            CleanupReason.OWNERSHIP_NOT_EXCLUSIVE_MANAGED
        )
        with pytest.raises(KeyError):
            store.get_by_id("same")
        with pytest.raises(ValueError, match="install_update_field_not_allowed"):
            store.update(first.id, id=second.id)
    finally:
        store.close()


def test_complete_proof_is_idempotent_conflict_safe_and_persistent(tmp_path) -> None:
    path = tmp_path / "state.db"
    store = InstallStore(path)
    record = _create_installed(store)
    proof = _proof(record)

    first = store.record_exclusive_managed_proof(record.id, proof)
    second = store.record_exclusive_managed_proof(record.id, proof)

    assert first == second
    assert first.ownership_class is OwnershipClass.EXCLUSIVE_MANAGED
    assert store.cleanup_eligibility(record.id).reason is CleanupReason.ELIGIBLE
    assert [event.event for event in store.events(record.id)].count(
        "provenance_proved"
    ) == 1
    with pytest.raises(ProvenanceConflictError, match="provenance_conflict"):
        store.record_exclusive_managed_proof(
            record.id,
            replace(proof, creation_transaction_id=str(uuid4())),
        )
    with pytest.raises(ValueError, match="install_update_field_not_allowed"):
        store.update(record.id, ownership_class="exclusive_managed")

    store.update(record.id, status="partial")
    assert store.get_provenance(record.id) == first
    assert store.cleanup_eligibility(record.id).reason is (
        CleanupReason.INSTALL_NOT_COMPLETE
    )
    store.update(record.id, status="installed")
    assert store.cleanup_eligibility(record.id).eligible
    store.close()

    reopened = InstallStore(path)
    try:
        assert reopened.get_provenance(record.id) == first
        assert reopened.cleanup_eligibility(record.id).eligible
        encoded = reopened._connection.execute(  # type: ignore[attr-defined]
            """
            SELECT owned_files_json, preexisting_entries_json, extra_entries_json
              FROM native_model_install_provenance
             WHERE installation_id = ?
            """,
            (record.id,),
        ).fetchone()
        assert encoded[0] == canonical_owned_files_json(proof.owned_files)
        assert encoded[1] == "[]"
        assert encoded[2] == "[]"
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            {"storage_binding_generation": 0},
            CleanupReason.STORAGE_BINDING_MISSING,
        ),
        (
            {"destination_binding_digest": _digest("wrong-binding")},
            CleanupReason.DESTINATION_BINDING_DIGEST_MISMATCH,
        ),
        ({"catalog_digest": None}, CleanupReason.DIGEST_EVIDENCE_MISSING),
        (
            {"manifest_digest": _digest("wrong-manifest")},
            CleanupReason.MANIFEST_DIGEST_MISMATCH,
        ),
        (
            {"destination_state_before": DestinationStateBefore.EMPTY},
            CleanupReason.DESTINATION_PREEXISTED,
        ),
        (
            {"destination_created_by_transaction": False},
            CleanupReason.DESTINATION_CREATION_UNPROVEN,
        ),
        (
            {"preexisting_entries": ("unrelated/user.txt",)},
            CleanupReason.PREEXISTING_ENTRIES_PRESENT,
        ),
        (
            {"extra_entries": ("unrelated/user.txt",)},
            CleanupReason.EXTRA_ENTRIES_PRESENT,
        ),
        (
            {"extra_entries": ("weights/model.gguf",)},
            CleanupReason.EVIDENCE_OVERLAP,
        ),
    ],
)
def test_cleanup_decision_denies_incomplete_or_ambiguous_evidence(
    tmp_path,
    change,
    reason,
) -> None:
    store = InstallStore(tmp_path / "state.db")
    try:
        record = _create_installed(store)
        proof = replace(_proof(record), **change)
        assert _decision(record, proof).reason is reason
        with pytest.raises(ProvenanceProofRejected) as rejected:
            store.record_exclusive_managed_proof(record.id, proof)
        assert rejected.value.reason is reason
        assert store.get_provenance(record.id).ownership_class is OwnershipClass.UNKNOWN
    finally:
        store.close()


def test_lexical_destination_and_storage_binding_are_exact(tmp_path) -> None:
    store = InstallStore(tmp_path / "state.db")
    try:
        record = _create_installed(
            store,
            destination="/Volumes/Athena/models-link/llama.cpp/owner/model",
        )
        proof = _proof(record)
        persisted = store.record_exclusive_managed_proof(record.id, proof)

        assert persisted.storage_lexical_root == "/Volumes/Athena/models-link"
        assert persisted.lexical_destination == record.destination
        assert "models-link" in persisted.lexical_destination

        changed_destination = "/Volumes/Athena/resolved/llama.cpp/owner/model"
        changed_binding = destination_binding_digest(
            storage_location_id=proof.storage_location_id,
            storage_binding_generation=proof.storage_binding_generation,
            storage_lexical_root="/Volumes/Athena",
            lexical_destination=changed_destination,
            storage_volume_uuid=proof.storage_volume_uuid,
            storage_scope_id=proof.storage_scope_id,
        )
        mismatched = replace(
            proof,
            storage_lexical_root="/Volumes/Athena",
            lexical_destination=changed_destination,
            destination_binding_digest=changed_binding,
        )
        assert _decision(record, mismatched).reason is (
            CleanupReason.DESTINATION_BINDING_MISMATCH
        )
    finally:
        store.close()


def test_malformed_persisted_provenance_fails_closed(tmp_path) -> None:
    store = InstallStore(tmp_path / "state.db")
    try:
        record = _create_installed(store)
        store.record_exclusive_managed_proof(record.id, _proof(record))
        store._connection.execute(  # type: ignore[attr-defined]
            """
            UPDATE native_model_install_provenance
               SET owned_files_json = '{malformed'
             WHERE installation_id = ?
            """,
            (record.id,),
        )
        store._connection.commit()  # type: ignore[attr-defined]

        with pytest.raises(ProvenanceDataError, match="provenance_malformed"):
            store.get_provenance(record.id)
        decision = store.cleanup_eligibility(record.id)
        assert not decision.eligible
        assert decision.reason is CleanupReason.PROVENANCE_MALFORMED
    finally:
        store.close()


def test_provenance_schema_contains_no_bookmark_bytes_or_secret_fields(
    tmp_path,
) -> None:
    store = InstallStore(tmp_path / "state.db")
    try:
        columns = {
            str(row[1])
            for row in store._connection.execute(  # type: ignore[attr-defined]
                "PRAGMA table_info(native_model_install_provenance)"
            )
        }
        assert "storage_scope_id" in columns
        assert all("bookmark" not in column for column in columns)
        assert all("secret" not in column for column in columns)
        assert all("credential" not in column for column in columns)
    finally:
        store.close()
    ManagedCreationClaim,
