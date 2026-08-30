from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from mnemosyne_macos.config import StorageConfig, StorageLocationConfig
from mnemosyne_macos.filesystem import FilesystemProbe, FilesystemProbeError
from mnemosyne_macos.install_provenance import (
    ArtifactAuthority,
    CleanupReason,
    MANAGED_CREATION_MARKER_PATH,
    OwnedFile,
    owned_manifest_digest,
)
import mnemosyne_macos.installer as installer_module
from mnemosyne_macos.installer import NativeInstaller
from mnemosyne_macos.mac_inventory_store import MacInventoryIndex


class WritingDownloadProcess:
    pid = 8712
    returncode = 0

    def __init__(self, destination: Path, *, add_foreign_content: bool = False) -> None:
        self.destination = destination
        self.add_foreign_content = add_foreign_content

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        weights = self.destination / "weights" / "Model.gguf"
        weights.parent.mkdir(parents=True, exist_ok=True)
        weights.write_bytes(b"managed weights")
        metadata = (
            self.destination
            / ".cache"
            / "huggingface"
            / "download"
            / "weights"
            / "Model.gguf.metadata"
        )
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_bytes(b"local metadata")
        if self.add_foreign_content:
            (self.destination / "owner-note.txt").write_text(
                "preserve me",
                encoding="utf-8",
            )
        await asyncio.sleep(0)
        return b'{"status":"complete"}', b""


@pytest.mark.asyncio
async def test_install_journal_precedes_destination_filesystem_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    root.mkdir()
    destination = root / "llama.cpp" / "owner" / "model"
    database = tmp_path / "state" / "mnemosyne.db"
    index = MacInventoryIndex(database)
    await index.initialize()
    installer = NativeInstaller(
        database,
        storage=StorageConfig(
            default="internal",
            locations=[StorageLocationConfig(name="internal", path=str(root))],
        ),
        filesystem_probe=FilesystemProbe(
            scope_root=tmp_path / "state" / "security-scopes",
            timeout_seconds=10,
        ),
        inventory_index=index,
    )

    def reject_create(**_kwargs: object) -> None:
        raise RuntimeError("simulated durable-journal failure")

    monkeypatch.setattr(installer.store, "create", reject_create)

    with pytest.raises(RuntimeError, match="durable-journal failure"):
        await installer.create(
            repo_id="owner/model",
            engine="llama.cpp",
            storage="internal",
            alias="model",
            destination=str(destination),
            revision="a" * 40,
            filename="weights/Model.gguf",
            download_files=("weights/Model.gguf",),
            family=None,
            total_bytes=10,
        )

    assert not destination.exists()
    await installer.stop()
    await index.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("add_foreign_content", [False, True])
async def test_marker_recovers_exact_creation_but_preserves_ambiguous_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    add_foreign_content: bool,
) -> None:
    root = tmp_path / "models"
    root.mkdir()
    destination = root / "llama.cpp" / "owner" / "model"
    marker = destination / MANAGED_CREATION_MARKER_PATH
    database = tmp_path / "state" / "mnemosyne.db"
    index = MacInventoryIndex(database)
    await index.initialize()
    installer = NativeInstaller(
        database,
        storage=StorageConfig(
            default="internal",
            locations=[StorageLocationConfig(name="internal", path=str(root))],
        ),
        filesystem_probe=FilesystemProbe(
            scope_root=tmp_path / "state" / "security-scopes",
            timeout_seconds=10,
        ),
        inventory_index=index,
    )
    installation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    scheduled: list[str] = []
    real_finalize = installer.store.finalize_creation

    def reject_finalize(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated post-filesystem journal failure")

    monkeypatch.setattr(installer, "_schedule", scheduled.append)
    monkeypatch.setattr(installer.store, "finalize_creation", reject_finalize)
    with pytest.raises(RuntimeError, match="post-filesystem journal failure"):
        await installer.create(
            installation_id=installation_id,
            repo_id="owner/model",
            engine="llama.cpp",
            storage="internal",
            alias="model",
            destination=str(destination),
            revision="a" * 40,
            filename="weights/Model.gguf",
            download_files=("weights/Model.gguf",),
            family=None,
            total_bytes=10,
        )

    preparing = await installer.get(installation_id)
    intent = installer.store.get_creation_intent(installation_id)
    assert preparing.status == "preparing"
    assert intent is not None and intent.state == "recovery_required"
    assert installer.store.get_creation_claim(installation_id) is None
    assert marker.is_file()
    marker_text = marker.read_text(encoding="utf-8")
    marker_payload = json.loads(marker_text)
    assert set(marker_payload) == {
        "creation_transaction_id",
        "destination_binding_digest",
        "installation_id",
        "schema_version",
        "source_identity_digest",
    }
    assert marker_payload["installation_id"] == installation_id
    assert marker_payload["schema_version"] == 1
    assert str(root) not in marker_text
    assert str(destination) not in marker_text
    assert scheduled == []

    if add_foreign_content:
        foreign = destination / "owner-note.txt"
        foreign.write_text("preserve me", encoding="utf-8")
    monkeypatch.setattr(installer.store, "finalize_creation", real_finalize)

    if add_foreign_content:
        with pytest.raises(FilesystemProbeError, match="exact recovery marker"):
            await installer.retry(installation_id)
        assert (destination / "owner-note.txt").read_text(encoding="utf-8") == (
            "preserve me"
        )
        assert marker.is_file()
        assert (await installer.get(installation_id)).status == "preparing"
        assert scheduled == []
    else:
        resumed = await installer.retry(installation_id)
        claim = installer.store.get_creation_claim(installation_id)
        intent = installer.store.get_creation_intent(installation_id)
        assert resumed.status == "queued"
        assert claim is not None
        assert claim.directory_device == destination.stat().st_dev
        assert claim.directory_inode == destination.stat().st_ino
        assert intent is not None and intent.state == "claimed"
        assert scheduled == [installation_id]

    await installer.stop()
    await index.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("add_foreign_content", [False, True])
async def test_new_absent_destination_records_only_unambiguous_local_ownership_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    add_foreign_content: bool,
) -> None:
    root = tmp_path / "Volumes" / "Athena" / "user-selected-weights"
    root.mkdir(parents=True)
    destination = root / "llama.cpp" / "owner" / "model"
    database = tmp_path / "state" / "mnemosyne.db"
    index = MacInventoryIndex(database)
    await index.initialize()
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=10,
    )
    real_create_process = asyncio.create_subprocess_exec

    async def create_process(*argv: str, **kwargs: object):
        if "mnemosyne_macos.fs_worker" in argv:
            return await real_create_process(*argv, **kwargs)
        return WritingDownloadProcess(
            destination,
            add_foreign_content=add_foreign_content,
        )

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    installer = NativeInstaller(
        database,
        storage=StorageConfig(
            default="athena",
            locations=[StorageLocationConfig(name="athena", path=str(root))],
        ),
        filesystem_probe=probe,
        inventory_index=index,
    )

    created = await installer.create(
        repo_id="owner/model",
        engine="llama.cpp",
        storage="athena",
        alias="model",
        destination=str(destination),
        revision="a" * 40,
        filename="weights/Model.gguf",
        download_files=("weights/Model.gguf",),
        family=None,
        # The downloaded tree also contains local Hub metadata, so cleanup
        # authority must bind to the captured exact tree rather than this
        # transfer estimate.
        total_bytes=len(b"managed weights"),
    )
    await installer._tasks[created.id]

    completed = await installer.get(created.id)
    claim = installer.store.get_creation_claim(created.id)
    provenance = installer.store.get_provenance(created.id)
    assert completed.status == "installed"
    assert claim is not None
    assert claim.lexical_destination == str(destination)
    if add_foreign_content:
        assert provenance.ownership_class.value == "unknown"
        assert provenance.owned_files is None
        assert installer.store.cleanup_eligibility(created.id).reason is (
            CleanupReason.OWNERSHIP_NOT_EXCLUSIVE_MANAGED
        )
        assert (destination / "owner-note.txt").read_text(encoding="utf-8") == (
            "preserve me"
        )
    else:
        assert provenance.artifact_authority is (
            ArtifactAuthority.LOCAL_PINNED_DISCOVERY
        )
        assert provenance.catalog_id is None
        assert provenance.owned_files is not None
        assert [item.path for item in provenance.owned_files] == [
            ".cache/huggingface/.mnemosyne-managed-creation-v1.json",
            ".cache/huggingface/download/weights/Model.gguf.metadata",
            "weights/Model.gguf",
        ]
        assert provenance.directory_device == destination.stat().st_dev
        assert provenance.directory_inode == destination.stat().st_ino
        assert installer.store.cleanup_eligibility(created.id).reason is (
            CleanupReason.ELIGIBLE
        )

    await installer.stop()
    await index.close()


@pytest.mark.asyncio
async def test_preexisting_destination_installs_but_never_gains_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    destination = root / "llama.cpp" / "owner" / "model"
    destination.mkdir(parents=True)
    (destination / "user-note.txt").write_text("keep me", encoding="utf-8")
    database = tmp_path / "state" / "mnemosyne.db"
    index = MacInventoryIndex(database)
    await index.initialize()
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=10,
    )
    real_create_process = asyncio.create_subprocess_exec

    async def create_process(*argv: str, **kwargs: object):
        if "mnemosyne_macos.fs_worker" in argv:
            return await real_create_process(*argv, **kwargs)
        return WritingDownloadProcess(destination)

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    installer = NativeInstaller(
        database,
        storage=StorageConfig(
            default="internal",
            locations=[StorageLocationConfig(name="internal", path=str(root))],
        ),
        filesystem_probe=probe,
        inventory_index=index,
    )

    created = await installer.create(
        repo_id="owner/model",
        engine="llama.cpp",
        storage="internal",
        alias="model",
        destination=str(destination),
        revision="b" * 40,
        filename="weights/Model.gguf",
        family=None,
        total_bytes=len(b"managed weights"),
    )
    await installer._tasks[created.id]

    assert (await installer.get(created.id)).status == "installed"
    assert installer.store.get_creation_claim(created.id) is None
    assert installer.store.cleanup_eligibility(created.id).reason is (
        CleanupReason.OWNERSHIP_NOT_EXCLUSIVE_MANAGED
    )
    assert (destination / "user-note.txt").read_text(encoding="utf-8") == "keep me"

    await installer.stop()
    await index.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", (None, "digest_mismatch", "capture_error"))
async def test_signed_artifact_is_verified_before_profile_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str | None,
) -> None:
    root = tmp_path / "models"
    root.mkdir()
    destination = root / "llama.cpp" / "owner" / "signed-model"
    database = tmp_path / "state" / "mnemosyne.db"
    index = MacInventoryIndex(database)
    await index.initialize()
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=10,
    )
    real_create_process = asyncio.create_subprocess_exec

    async def create_process(*argv: str, **kwargs: object):
        if "mnemosyne_macos.fs_worker" in argv:
            return await real_create_process(*argv, **kwargs)
        return WritingDownloadProcess(destination)

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    if failure == "capture_error":
        async def reject_capture(**_kwargs: object):
            raise FilesystemProbeError("simulated capture failure")

        monkeypatch.setattr(probe, "capture_managed_manifest", reject_capture)

    callbacks: list[tuple[str, str, str]] = []
    installer: NativeInstaller

    async def on_installed(record):
        current = installer.store.get(record.id)
        provenance = installer.store.get_provenance(record.id)
        callbacks.append(
            (current.status, provenance.artifact_authority.value, record.id)
        )

    installer = NativeInstaller(
        database,
        on_installed=on_installed,
        storage=StorageConfig(
            default="internal",
            locations=[StorageLocationConfig(name="internal", path=str(root))],
        ),
        filesystem_probe=probe,
        inventory_index=index,
    )
    expected_sha = "sha256:" + hashlib.sha256(b"managed weights").hexdigest()
    if failure == "digest_mismatch":
        expected_sha = "sha256:" + "f" * 64
    expected_files = (
        OwnedFile(
            path="weights/Model.gguf",
            size_bytes=len(b"managed weights"),
            sha256=expected_sha,
        ),
    )

    created = await installer.create(
        repo_id="owner/signed-model",
        engine="llama.cpp",
        storage="internal",
        alias="signed-model",
        destination=str(destination),
        revision="c" * 40,
        filename="weights/Model.gguf",
        download_files=("weights/Model.gguf",),
        expected_files=expected_files,
        expected_manifest_digest=owned_manifest_digest(expected_files),
        family="signed-family",
        total_bytes=len(b"managed weights"),
        launch_contract={
            "engine": "llama.cpp",
            "parallel_slots": 1,
            "gpu_offload": "all",
            "flash_attention": "automatic",
        },
        artifact_authority=ArtifactAuthority.SIGNED_CATALOG,
        source_identity_digest="sha256:" + "1" * 64,
        catalog_id="catalog:test",
        logical_model_id="model:test",
        artifact_id="artifact:test",
        recipe_id="recipe:test",
        catalog_digest="sha256:" + "2" * 64,
        require_exclusive_proof=True,
    )
    await installer._tasks[created.id]

    completed = await installer.get(created.id)
    assert completed.expected_files == expected_files
    assert completed.expected_manifest_digest == owned_manifest_digest(expected_files)
    assert "expected_files_json" not in completed.to_dict()
    assert "expected_manifest_digest" not in completed.to_dict()
    assert installer.store.get_creation_claim(created.id) is not None
    if failure is None:
        assert completed.status == "installed"
        assert callbacks == [
            ("registering", ArtifactAuthority.SIGNED_CATALOG.value, created.id)
        ]
        assert installer.store.cleanup_eligibility(created.id).reason is (
            CleanupReason.ELIGIBLE
        )
        assert "signed_artifact_verified" in {
            event.event for event in installer.store.events(created.id)
        }
    else:
        assert completed.status == "failed"
        assert completed.error == "signed_artifact_verification_failed"
        assert callbacks == []
        assert installer.store.get_provenance(created.id).owned_files is None
        assert installer.store.cleanup_eligibility(created.id).reason is (
            CleanupReason.OWNERSHIP_NOT_EXCLUSIVE_MANAGED
        )

    await installer.stop()
    await index.close()


@pytest.mark.asyncio
async def test_signed_verification_survives_callback_failure_and_restart_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    root.mkdir()
    destination = root / "llama.cpp" / "owner" / "signed-retry"
    database = tmp_path / "state" / "mnemosyne.db"
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=10,
    )
    real_create_process = asyncio.create_subprocess_exec

    async def create_process(*argv: str, **kwargs: object):
        if "mnemosyne_macos.fs_worker" in argv:
            return await real_create_process(*argv, **kwargs)
        return WritingDownloadProcess(destination)

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    attempts: list[str] = []

    async def reject_registration(record):
        attempts.append(record.id)
        raise RuntimeError("simulated registration failure")

    first_index = MacInventoryIndex(database)
    await first_index.initialize()
    first = NativeInstaller(
        database,
        on_installed=reject_registration,
        storage=StorageConfig(
            default="internal",
            locations=[StorageLocationConfig(name="internal", path=str(root))],
        ),
        filesystem_probe=probe,
        inventory_index=first_index,
    )
    expected_files = (
        OwnedFile(
            path="weights/Model.gguf",
            size_bytes=len(b"managed weights"),
            sha256="sha256:" + hashlib.sha256(b"managed weights").hexdigest(),
        ),
    )
    created = await first.create(
        repo_id="owner/signed-retry",
        engine="llama.cpp",
        storage="internal",
        alias="signed-retry",
        destination=str(destination),
        revision="d" * 40,
        filename="weights/Model.gguf",
        download_files=("weights/Model.gguf",),
        expected_files=expected_files,
        expected_manifest_digest=owned_manifest_digest(expected_files),
        family="signed-family",
        total_bytes=len(b"managed weights"),
        launch_contract={
            "engine": "llama.cpp",
            "parallel_slots": 1,
            "gpu_offload": "all",
            "flash_attention": "automatic",
        },
        artifact_authority=ArtifactAuthority.SIGNED_CATALOG,
        source_identity_digest="sha256:" + "3" * 64,
        catalog_id="catalog:test",
        logical_model_id="model:test",
        artifact_id="artifact:test",
        recipe_id="recipe:test",
        catalog_digest="sha256:" + "4" * 64,
        require_exclusive_proof=True,
    )
    await first._tasks[created.id]
    interrupted = await first.get(created.id)
    assert interrupted.status == "downloaded"
    assert interrupted.expected_files == expected_files
    assert first.store.get_provenance(created.id).owned_files is not None
    assert first.store.cleanup_eligibility(created.id).reason is (
        CleanupReason.INSTALL_NOT_COMPLETE
    )
    await first.stop()
    await first_index.close()

    completed_callbacks: list[str] = []

    async def accept_registration(record):
        completed_callbacks.append(record.id)

    second_index = MacInventoryIndex(database)
    await second_index.initialize()
    second = NativeInstaller(
        database,
        on_installed=accept_registration,
        storage=StorageConfig(
            default="internal",
            locations=[StorageLocationConfig(name="internal", path=str(root))],
        ),
        filesystem_probe=probe,
        inventory_index=second_index,
    )
    await second.start()
    persisted = await second.get(created.id)
    assert persisted.status == "downloaded"
    assert persisted.expected_files == expected_files
    await second.retry(created.id)
    await second._tasks[created.id]

    assert (await second.get(created.id)).status == "installed"
    assert attempts == [created.id]
    assert completed_callbacks == [created.id]
    assert second.store.cleanup_eligibility(created.id).reason is CleanupReason.ELIGIBLE
    await second.stop()
    await second_index.close()
