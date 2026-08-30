from __future__ import annotations

import sqlite3
import time

import pytest

from mnemosyne_macos.install_launch import LlamaCppInstallLaunch
from mnemosyne_macos.install_provenance import (
    OwnedFile,
    ProvenanceDataError,
    owned_manifest_digest,
)
from mnemosyne_macos.install_store import InstallStore


def test_install_store_persists_and_recovers_interrupted_downloads(tmp_path) -> None:
    path = tmp_path / "state" / "mnemosyne.db"
    store = InstallStore(path)
    record = store.create(
        repo_id="mlx-community/GLM",
        engine="omlx",
        storage="athena-models",
        alias="glm",
        destination="/Volumes/Athena/models/omlx/mlx-community/GLM",
        revision=None,
        filename=None,
        capabilities=["embeddings"],
        family=None,
        total_bytes=2048,
    )
    store.update(record.id, status="downloading", pid=123, bytes_downloaded=1024)
    store.close()

    reopened = InstallStore(path)
    reopened.recover_interrupted()
    recovered = reopened.get(record.id)
    assert recovered.status == "partial"
    assert recovered.pid is None
    assert recovered.bytes_downloaded == 1024
    assert recovered.total_bytes == 2048
    assert recovered.capabilities == ("embeddings",)
    assert recovered.launch_contract is None
    assert recovered.to_dict()["capabilities"] == ["embeddings"]
    assert reopened.list()[0].id == record.id
    reopened.close()


def test_ordinary_install_cannot_persist_a_signed_artifact_expectation(tmp_path) -> None:
    store = InstallStore(tmp_path / "state.db")
    expected_files = (
        OwnedFile(
            path="model.safetensors",
            size_bytes=8,
            sha256="sha256:" + "1" * 64,
        ),
    )
    try:
        with pytest.raises(
            ProvenanceDataError,
            match="expected_artifact_manifest_not_allowed",
        ):
            store.create(
                repo_id="owner/model",
                engine="omlx",
                storage="internal",
                alias="model",
                destination="/models/owner/model",
                revision="a" * 40,
                filename=None,
                download_files=("model.safetensors",),
                expected_files=expected_files,
                expected_manifest_digest=owned_manifest_digest(expected_files),
                family=None,
                total_bytes=8,
            )
        assert store.list() == []
    finally:
        store.close()


def test_install_store_recovers_interrupted_and_legacy_profile_registration(
    tmp_path,
) -> None:
    path = tmp_path / "state" / "mnemosyne.db"
    store = InstallStore(path)
    registering = store.create(
        repo_id="owner/model",
        engine="omlx",
        storage="internal",
        alias="registering-model",
        destination="/models/owner/model",
        revision="resolved-commit",
        filename=None,
        family=None,
        total_bytes=2048,
    )
    legacy = store.create(
        repo_id="owner/legacy",
        engine="omlx",
        storage="internal",
        alias="legacy-model",
        destination="/models/owner/legacy",
        revision="resolved-commit",
        filename=None,
        family=None,
        total_bytes=4096,
    )
    store.update(
        registering.id,
        status="registering",
        pid=123,
        bytes_downloaded=2048,
    )
    legacy_error = (
        "download completed but profile registration failed: config unavailable"
    )
    store.update(
        legacy.id,
        status="installed",
        pid=456,
        bytes_downloaded=4096,
        error=legacy_error,
    )
    store.close()

    reopened = InstallStore(path)
    reopened.recover_interrupted()

    recovered = reopened.get(registering.id)
    assert recovered.status == "downloaded"
    assert recovered.pid is None
    assert recovered.bytes_downloaded == 2048
    assert recovered.error == (
        "download completed but profile registration was interrupted"
    )

    migrated = reopened.get(legacy.id)
    assert migrated.status == "downloaded"
    assert migrated.pid is None
    assert migrated.bytes_downloaded == 4096
    assert migrated.error == legacy_error
    reopened.close()


def test_install_store_migrates_projector_and_download_file_columns(
    tmp_path,
) -> None:
    path = tmp_path / "state" / "legacy.db"
    path.parent.mkdir()
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
            error TEXT,
            pid INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    now = time.time()
    connection.execute(
        """
        INSERT INTO native_model_installs (
            id, repo_id, engine, storage, alias, destination, status,
            revision, filename, family, bytes_downloaded, total_bytes,
            error, pid, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-install",
            "owner/model-GGUF",
            "llama.cpp",
            "internal",
            "legacy",
            "/models/owner/model-GGUF",
            "installed",
            "abc123",
            "model-Q4_K_M.gguf",
            None,
            100,
            100,
            None,
            None,
            now,
            now,
        ),
    )
    connection.commit()
    connection.close()

    store = InstallStore(path)
    try:
        columns = {
            row[1]
            for row in store._connection.execute(  # type: ignore[attr-defined]
                "PRAGMA table_info(native_model_installs)"
            )
        }
        assert {
            "projector_filename",
            "context_length",
            "files_json",
            "expected_files_json",
            "expected_manifest_digest",
            "capabilities_json",
            "download_speed_bps",
            "hidden",
            "launch_json",
        } <= columns
        migrated = store.get("legacy-install")
        assert migrated.projector_filename is None
        assert migrated.files_json is None
        assert migrated.expected_files_json is None
        assert migrated.expected_manifest_digest is None
        assert migrated.expected_files is None
        assert migrated.capabilities_json is None
        assert migrated.capabilities is None
        assert migrated.launch_contract is None
        assert migrated.to_dict()["download_files"] == []
        assert migrated.to_dict()["capabilities"] is None
    finally:
        store.close()


def test_install_store_persists_launch_contract_immutably(tmp_path) -> None:
    path = tmp_path / "state.db"
    store = InstallStore(path)
    try:
        record = store.create(
            repo_id="owner/model-GGUF",
            engine="llama.cpp",
            storage="external-models",
            alias="model",
            destination="/Volumes/Athena/nested/models/owner/model-GGUF",
            revision="a" * 40,
            filename="model.gguf",
            family="model",
            launch_contract={
                "engine": "llama.cpp",
                "parallel_slots": 2,
                "gpu_offload": "all",
                "flash_attention": "disabled",
            },
            total_bytes=123,
        )
        assert record.destination == (
            "/Volumes/Athena/nested/models/owner/model-GGUF"
        )
        assert record.launch_contract == LlamaCppInstallLaunch(
            engine="llama.cpp",
            parallel_slots=2,
            gpu_offload="all",
            flash_attention="disabled",
        )
        assert record.to_dict()["launch_contract"] == {
            "engine": "llama.cpp",
            "parallel_slots": 2,
            "gpu_offload": "all",
            "flash_attention": "disabled",
        }
        with pytest.raises(ValueError, match="install_update_field_not_allowed"):
            store.update(record.id, launch_json=None)
    finally:
        store.close()

    reopened = InstallStore(path)
    try:
        assert reopened.get(record.id).launch_contract == record.launch_contract
    finally:
        reopened.close()


def test_install_history_can_be_hidden_without_losing_managed_download_identity(
    tmp_path,
) -> None:
    store = InstallStore(tmp_path / "state.db")
    try:
        record = store.create(
            repo_id="owner/model",
            engine="llama.cpp",
            storage="internal",
            alias="model",
            destination="/models/owner/model",
            revision="abc123",
            filename="model.gguf",
            family=None,
            total_bytes=4096,
        )
        store.update(
            record.id,
            status="installed",
            bytes_downloaded=4096,
            download_speed_bps=None,
        )

        dismissed = store.dismiss(record.id)

        assert dismissed.status == "installed"
        assert store.list() == []
        assert store.get(record.id).hidden == 1
        assert store.latest_for_alias("model").id == record.id
        assert "hidden" not in store.get(record.id).to_dict()
        evidence = store.evidence()
        assert len(evidence) == 1
        assert evidence[0]["dismissed"] is True
        assert [
            (event["event"], event["status"])
            for event in evidence[0]["events"]
        ] == [
            ("created", "queued"),
            ("status", "installed"),
            ("history_dismissed", "installed"),
        ]
    finally:
        store.close()


def test_existing_install_rows_gain_only_an_honest_migration_snapshot(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-events.db"
    first = InstallStore(path)
    record = first.create(
        repo_id="owner/model",
        engine="llama.cpp",
        storage="internal",
        alias="model",
        destination="/models/owner/model",
        revision="abc123",
        filename="model.gguf",
        family=None,
        total_bytes=4096,
    )
    first.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "DELETE FROM native_model_install_events WHERE install_id = ?",
        (record.id,),
    )
    connection.commit()
    connection.close()

    migrated = InstallStore(path)
    try:
        events = migrated.events(record.id)
        assert [(event.event, event.status) for event in events] == [
            ("snapshot", "queued")
        ]
    finally:
        migrated.close()


def test_install_store_round_trips_exact_shards_and_projector(tmp_path) -> None:
    store = InstallStore(tmp_path / "state.db")
    try:
        record = store.create(
            repo_id="owner/vision-GGUF",
            engine="llama.cpp",
            storage="athena-models",
            alias="vision",
            destination="/Volumes/Athena/models/llama.cpp/owner/vision-GGUF",
            revision="resolved-commit",
            filename="vision-Q4_K_M-00001-of-00002.gguf",
            projector_filename="mmproj-vision-Q8_0.gguf",
            download_files=(
                "vision-Q4_K_M-00001-of-00002.gguf",
                "vision-Q4_K_M-00002-of-00002.gguf",
                "mmproj-vision-Q8_0.gguf",
            ),
            family=None,
            total_bytes=230,
        )

        reopened = store.get(record.id)
        assert reopened.projector_filename == "mmproj-vision-Q8_0.gguf"
        assert reopened.to_dict()["download_files"] == [
            "vision-Q4_K_M-00001-of-00002.gguf",
            "vision-Q4_K_M-00002-of-00002.gguf",
            "mmproj-vision-Q8_0.gguf",
        ]
        assert reopened.total_bytes == 230

        with pytest.raises(ValueError, match="install_update_field_not_allowed"):
            store.update(record.id, files_json="{invalid json")
        store._connection.execute(  # type: ignore[attr-defined]
            "UPDATE native_model_installs SET files_json = ? WHERE id = ?",
            ("{invalid json", record.id),
        )
        store._connection.commit()  # type: ignore[attr-defined]
        invalid = store.get_by_id(record.id)
        assert invalid.to_dict()["download_files"] == []
    finally:
        store.close()


def test_install_store_generic_updates_cannot_rewrite_install_identity(tmp_path) -> None:
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
            projector_filename=None,
            download_files=("model.gguf",),
            capabilities=("chat/completions",),
            context_length=32_768,
            family="test",
            total_bytes=4096,
        )
        identity_fields = {
            "repo_id": "other/model",
            "engine": "omlx",
            "storage": "external",
            "alias": "other",
            "destination": "/models/other",
            "revision": "b" * 40,
            "filename": "other.gguf",
            "projector_filename": "mmproj.gguf",
            "context_length": 65_536,
            "files_json": "[]",
            "capabilities_json": "[]",
            "family": "other",
            "total_bytes": 8192,
        }
        for field, value in identity_fields.items():
            with pytest.raises(
                ValueError,
                match="install_update_field_not_allowed",
            ):
                store.update(record.id, **{field: value})
        assert store.get_by_id(record.id) == record
    finally:
        store.close()
