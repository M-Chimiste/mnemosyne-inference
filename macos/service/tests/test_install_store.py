from __future__ import annotations

import sqlite3
import time

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
    assert recovered.to_dict()["capabilities"] == ["embeddings"]
    assert reopened.list()[0].id == record.id
    reopened.close()


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
        assert {"projector_filename", "files_json", "capabilities_json"} <= columns
        migrated = store.get("legacy-install")
        assert migrated.projector_filename is None
        assert migrated.files_json is None
        assert migrated.capabilities_json is None
        assert migrated.capabilities is None
        assert migrated.to_dict()["download_files"] == []
        assert migrated.to_dict()["capabilities"] is None
    finally:
        store.close()


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

        invalid = store.update(record.id, files_json="{invalid json")
        assert invalid.to_dict()["download_files"] == []
    finally:
        store.close()
