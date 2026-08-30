from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from mnemosyne_macos.config import StorageConfig, StorageLocationConfig
from mnemosyne_macos.filesystem import FilesystemProbe
import mnemosyne_macos.fs_worker as fs_worker_module
import mnemosyne_macos.installer as installer_module
from mnemosyne_macos.install_provenance import (
    ArtifactAuthority,
    OwnedFile,
    ProvenanceDataError,
    owned_manifest_digest,
)
from mnemosyne_macos.installer import NativeInstaller
from mnemosyne_macos.mac_inventory_store import MacInventoryIndex


class FakeDownloadProcess:
    pid = 4321
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(0)
        return b'{"status":"complete"}', b""


@pytest.mark.asyncio
async def test_create_accepts_exact_caller_owned_installation_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = NativeInstaller(tmp_path / "state.db")
    installation_id = str(uuid4())
    scheduled: list[str] = []
    monkeypatch.setattr(installer, "_schedule", scheduled.append)

    created = await installer.create(
        installation_id=installation_id,
        repo_id="owner/model",
        engine="omlx",
        storage="internal",
        alias="model",
        destination=str(tmp_path / "weights"),
        revision="resolved-commit",
        filename=None,
        family="model-family",
        total_bytes=123,
    )

    assert created.id == installation_id
    assert created.launch_contract is None
    assert scheduled == [installation_id]
    assert (await installer.get_by_id(installation_id)).id == installation_id
    await installer.stop()


@pytest.mark.asyncio
async def test_create_rejects_untrusted_or_invalid_launch_before_scheduling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = NativeInstaller(tmp_path / "state.db")
    scheduled: list[str] = []
    monkeypatch.setattr(installer, "_schedule", scheduled.append)
    common = {
        "repo_id": "owner/model",
        "engine": "llama.cpp",
        "storage": "internal",
        "alias": "model",
        "destination": str(tmp_path / "weights"),
        "revision": "a" * 40,
        "filename": "model.gguf",
        "family": "model",
        "total_bytes": 123,
    }

    with pytest.raises(ValueError, match="requires_signed_catalog"):
        await installer.create(
            **common,
            launch_contract={
                "engine": "llama.cpp",
                "parallel_slots": 1,
                "gpu_offload": "all",
                "flash_attention": "enabled",
            },
        )
    with pytest.raises(ValueError, match="signed_catalog_install_launch_required"):
        await installer.create(
            **common,
            artifact_authority=ArtifactAuthority.SIGNED_CATALOG,
            require_exclusive_proof=True,
        )
    with pytest.raises(ValueError, match="install_launch_shape_invalid"):
        await installer.create(
            **common,
            launch_contract={
                "engine": "llama.cpp",
                "parallel_slots": 1,
                "gpu_offload": "all",
            },
            artifact_authority=ArtifactAuthority.SIGNED_CATALOG,
            require_exclusive_proof=True,
        )

    assert scheduled == []
    assert await installer.list() == []
    assert not (tmp_path / "weights").exists()
    await installer.stop()


@pytest.mark.asyncio
async def test_signed_launch_reaches_immutable_install_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    storage_root = tmp_path / "exact"
    storage_root.mkdir()
    index = MacInventoryIndex(database)
    await index.initialize()
    installer = NativeInstaller(
        database,
        storage=StorageConfig(
            default="exact-storage",
            locations=[
                StorageLocationConfig(
                    name="exact-storage",
                    path=str(storage_root),
                )
            ],
        ),
        filesystem_probe=FilesystemProbe(
            scope_root=tmp_path / "security-scopes",
            timeout_seconds=5,
        ),
        inventory_index=index,
    )
    scheduled: list[str] = []
    monkeypatch.setattr(installer, "_schedule", scheduled.append)
    expected_files = (
        OwnedFile(
            path="model.gguf",
            size_bytes=123,
            sha256="sha256:" + "c" * 64,
        ),
    )
    with pytest.raises(ProvenanceDataError, match="expected_artifact_manifest_required"):
        await installer.create(
            repo_id="owner/missing-manifest",
            engine="ds4",
            storage="exact-storage",
            alias="missing-manifest",
            destination=str(storage_root / "missing-manifest"),
            revision="b" * 40,
            filename="model.gguf",
            download_files=("model.gguf",),
            family="model",
            total_bytes=123,
            launch_contract={
                "engine": "ds4",
                "batched_sessions": 2,
                "execution_mode": "single-node",
            },
            artifact_authority=ArtifactAuthority.SIGNED_CATALOG,
            source_identity_digest="sha256:" + "d" * 64,
            catalog_id="catalog:test",
            logical_model_id="model:test",
            artifact_id="artifact:test",
            recipe_id="recipe:test",
            catalog_digest="sha256:" + "e" * 64,
            require_exclusive_proof=True,
        )
    assert not (storage_root / "missing-manifest").exists()
    record = await installer.create(
        repo_id="owner/model",
        engine="ds4",
        storage="exact-storage",
        alias="model",
        destination=str(storage_root / "weights"),
        revision="a" * 40,
        filename="model.gguf",
        download_files=("model.gguf",),
        expected_files=expected_files,
        expected_manifest_digest=owned_manifest_digest(expected_files),
        family="model",
        total_bytes=123,
        launch_contract={
            "engine": "ds4",
            "batched_sessions": 2,
            "execution_mode": "single-node",
        },
        artifact_authority=ArtifactAuthority.SIGNED_CATALOG,
        source_identity_digest="sha256:" + "d" * 64,
        catalog_id="catalog:test",
        logical_model_id="model:test",
        artifact_id="artifact:test",
        recipe_id="recipe:test",
        catalog_digest="sha256:" + "e" * 64,
        require_exclusive_proof=True,
    )

    assert record.launch_json == (
        '{"batched_sessions":2,"engine":"ds4",'
        '"execution_mode":"single-node"}'
    )
    assert record.storage == "exact-storage"
    assert record.destination == str(tmp_path / "exact" / "weights")
    assert record.expected_files == expected_files
    assert record.expected_manifest_digest == owned_manifest_digest(expected_files)
    assert scheduled == [record.id]
    await installer.stop()
    await index.close()


@pytest.mark.asyncio
async def test_create_rejects_noncanonical_caller_installation_id(
    tmp_path: Path,
) -> None:
    installer = NativeInstaller(tmp_path / "state.db")

    with pytest.raises(ValueError, match="installation_id_invalid"):
        await installer.create(
            installation_id=str(uuid4()).upper(),
            repo_id="owner/model",
            engine="omlx",
            storage="internal",
            alias="model",
            destination=str(tmp_path / "weights"),
            revision="resolved-commit",
            filename=None,
            family="model-family",
            total_bytes=123,
        )

    assert await installer.list() == []
    await installer.stop()


@pytest.mark.asyncio
async def test_managed_install_preserves_symlink_selected_storage_and_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    external_target = (
        tmp_path / "Volumes" / "Athena" / "nested" / "user-selected-weights"
    )
    external_target.mkdir(parents=True)
    selected_root = tmp_path / "Library" / "Model Locations" / "athena-link"
    selected_root.parent.mkdir(parents=True)
    selected_root.symlink_to(external_target, target_is_directory=True)
    destination = selected_root / "llama.cpp" / "owner" / "model-GGUF"
    default_root = tmp_path / "Application Support" / "Mnemosyne" / "models"
    scope_root = tmp_path / "state" / "security-scopes"
    scope_id = "a" * 64
    probe = FilesystemProbe(scope_root=scope_root, timeout_seconds=5)
    filesystem_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    async def run_filesystem_worker(
        *arguments: str,
        **kwargs: object,
    ) -> dict[str, object]:
        # Exercise the worker's real containment/create/measurement semantics
        # without trying to consume a macOS bookmark on the test host.
        filesystem_calls.append((arguments, kwargs))
        parsed = fs_worker_module._parser().parse_args(arguments)
        return {"ok": True, **fs_worker_module._run(parsed)}

    monkeypatch.setattr(probe, "_run", run_filesystem_worker)
    download_argv: list[tuple[str, ...]] = []

    async def create_process(*argv: str, **_kwargs: object) -> FakeDownloadProcess:
        download_argv.append(argv)
        return FakeDownloadProcess()

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    storage = StorageConfig(
        default="internal",
        locations=[
            StorageLocationConfig(name="internal", path=str(default_root)),
            StorageLocationConfig(
                name="athena",
                path=str(selected_root),
                scope_id=scope_id,
            ),
        ],
    )
    installer = NativeInstaller(
        tmp_path / "state" / "mnemosyne.db",
        storage=storage,
        filesystem_probe=probe,
    )

    created = await installer.create(
        repo_id="owner/model-GGUF",
        engine="llama.cpp",
        storage="athena",
        alias="model",
        destination=str(destination),
        revision="resolved-commit",
        filename="model-Q4_K_M.gguf",
        family=None,
        total_bytes=None,
    )
    await installer._tasks[created.id]

    completed = await installer.get(created.id)
    assert completed.status == "installed"
    assert completed.error is None
    assert created.destination == str(destination)
    assert completed.destination == str(destination)
    assert destination.is_dir()
    assert destination.resolve() == (
        external_target / "llama.cpp" / "owner" / "model-GGUF"
    ).resolve()
    assert not default_root.exists()
    assert filesystem_calls[0] == (
        (
            "ensure-directory",
            "--root",
            str(selected_root),
            "--path",
            str(destination),
        ),
        {"scope_id": scope_id, "scope_path": str(selected_root)},
    )
    assert download_argv == [
        (
            sys.executable,
            "-m",
            "mnemosyne_macos.scope_exec",
            "--scope-root",
            str(scope_root),
            "--scope-id",
            scope_id,
            "--scope-path",
            str(selected_root),
            "--",
            sys.executable,
            "-m",
            "mnemosyne_macos.download_worker",
            "--repo-id",
            "owner/model-GGUF",
            "--destination",
            str(destination),
            "--revision",
            "resolved-commit",
            "--filename",
            "model-Q4_K_M.gguf",
        )
    ]
    await installer.stop()


@pytest.mark.asyncio
async def test_profile_registration_retry_does_not_redownload_after_restart(
    monkeypatch,
    tmp_path,
) -> None:
    destination = tmp_path / "models" / "owner" / "model"
    destination.mkdir(parents=True)
    (destination / "model.gguf").write_bytes(b"downloaded weights")
    database = tmp_path / "state" / "mnemosyne.db"
    spawn_calls: list[tuple[str, ...]] = []

    async def create_process(*argv: str, **_kwargs: object) -> FakeDownloadProcess:
        spawn_calls.append(argv)
        return FakeDownloadProcess()

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )

    registration_attempts = 0

    async def register_profile(_record) -> None:
        nonlocal registration_attempts
        registration_attempts += 1
        if registration_attempts == 1:
            raise RuntimeError("config temporarily unavailable")

    first = NativeInstaller(database, on_installed=register_profile)
    install = await first.create(
        repo_id="owner/model",
        engine="llama.cpp",
        storage="internal",
        alias="model",
        destination=str(destination),
        revision="resolved-commit",
        filename="model.gguf",
        family=None,
        total_bytes=18,
    )
    await first._tasks[install.id]

    failed_registration = await first.get(install.id)
    assert failed_registration.status == "downloaded"
    assert failed_registration.bytes_downloaded == 18
    assert failed_registration.error == (
        "download completed but profile registration failed: "
        "config temporarily unavailable"
    )
    assert len(spawn_calls) == 1
    await first.stop()

    second = NativeInstaller(database, on_installed=register_profile)
    await second.start()
    retrying = await second.retry(install.id)
    assert retrying.status == "registering"
    await second._tasks[install.id]

    completed = await second.get(install.id)
    assert completed.status == "installed"
    assert completed.error is None
    assert registration_attempts == 2
    assert len(spawn_calls) == 1
    assert [
        (event.event, event.status)
        for event in second.store.events(install.id)
    ] == [
        ("created", "queued"),
        ("status", "downloading"),
        ("status", "registering"),
        ("status", "downloaded"),
        ("status", "registering"),
        ("status", "installed"),
    ]
    await second.stop()


@pytest.mark.asyncio
async def test_install_history_dismissal_refuses_active_downloads(tmp_path) -> None:
    installer = NativeInstaller(tmp_path / "state.db")
    record = installer.store.create(
        repo_id="owner/model",
        engine="llama.cpp",
        storage="internal",
        alias="model",
        destination=str(tmp_path / "models" / "owner" / "model"),
        revision="abc123",
        filename="model.gguf",
        family=None,
        total_bytes=4096,
    )

    with pytest.raises(ValueError, match="active install"):
        await installer.dismiss(record.id)

    installer.store.update(record.id, status="installed", bytes_downloaded=4096)
    dismissed = await installer.dismiss(record.id)

    assert dismissed.status == "installed"
    assert await installer.list() == []
    installer.store.close()
