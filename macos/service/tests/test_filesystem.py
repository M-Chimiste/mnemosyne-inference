from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading

import pytest

import mnemosyne_macos.filesystem as filesystem_module
import mnemosyne_macos.fs_worker as fs_worker_module
from mnemosyne_macos.filesystem import FilesystemProbe, FilesystemProbeError
from mnemosyne_macos.install_provenance import (
    OwnedFile,
    canonical_owned_files_json,
)
from mnemosyne_macos.scoped_process import wrap_scoped_argv
from mnemosyne_macos.security_scopes import (
    DarwinBookmarkResolver,
    SecurityScopeError,
    SecurityScopeRegistry,
)
from mnemosyne_macos.storage import inspect_path


def _gguf(path: Path, payload: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + payload)
    return path


@pytest.mark.asyncio
async def test_filesystem_probe_inspects_selected_folder(tmp_path: Path) -> None:
    root = tmp_path / "Models"
    root.mkdir()
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    status = await probe.inspect(str(root), name="external-models")

    assert status.name == "external-models"
    assert status.path == str(root.resolve())
    assert status.exists is True
    assert status.is_directory is True
    assert status.writable is True
    assert status.volume_matches is True
    assert status.total_bytes is not None
    assert status.free_bytes is not None
    assert status.diagnostic is None


@pytest.mark.asyncio
async def test_filesystem_probe_activates_configured_root_for_child_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Models"
    child = root / "publisher" / "model"
    child.mkdir(parents=True)
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )
    captured: dict[str, object] = {}

    async def fake_run(*arguments: str, **kwargs: object) -> dict[str, object]:
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return {"status": inspect_path(str(child)).to_dict()}

    monkeypatch.setattr(probe, "_run", fake_run)

    await probe.inspect(
        str(child),
        scope_id="a" * 64,
        scope_path=str(root),
    )

    assert captured["arguments"] == ("inspect", "--path", str(child))
    assert captured["kwargs"] == {
        "scope_id": "a" * 64,
        "scope_path": str(root),
    }


@pytest.mark.asyncio
async def test_filesystem_probe_scans_gguf_without_loading_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    directory = root / "publisher" / "vision-model"
    model = _gguf(directory / "vision-Q4_K_M.gguf", b"model")
    projector = _gguf(directory / "mmproj-vision-f16.gguf", b"projector")
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    status, models = await probe.scan(str(root))

    assert status.path == str(root.resolve())
    assert len(models) == 1
    discovered = models[0]
    assert discovered.engine == "llama.cpp"
    assert discovered.model_path == str(model.resolve())
    assert discovered.all_paths == (str(model.resolve()),)
    assert discovered.quantization == "Q4_K_M"
    assert discovered.compatibility == "structural"
    assert [candidate.path for candidate in discovered.projector_options] == [
        str(projector.resolve())
    ]


@pytest.mark.asyncio
async def test_filesystem_probe_scan_preserves_selected_symlink_spelling(
    tmp_path: Path,
) -> None:
    physical = tmp_path / "Volumes" / "Athena" / "models"
    model = _gguf(physical / "publisher" / "model-Q4_K_M.gguf")
    selected = tmp_path / "selected-models"
    selected.symlink_to(physical, target_is_directory=True)
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    status, models = await probe.scan(str(selected))

    assert status.path == str(selected)
    assert models[0].model_path == str(selected / model.relative_to(physical))


@pytest.mark.asyncio
async def test_filesystem_probe_validates_llama_model_and_projector(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    model = _gguf(root / "publisher" / "model-Q4_K_M.gguf", b"model")
    projector = _gguf(root / "publisher" / "mmproj-model-f16.gguf", b"projector")
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    result = await probe.validate_llama(
        root=str(root),
        model=str(model),
        projector=str(projector),
        expected_volume_uuid=None,
        scope_id=None,
    )

    assert result["ok"] is True
    assert result["model"] == str(model.resolve())
    assert result["projector"] == str(projector.resolve())
    assert result["status"]["path"] == str(root.resolve())


@pytest.mark.asyncio
async def test_filesystem_probe_ensures_directory_and_measures_size(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    root.mkdir()
    destination = root / "llama.cpp" / "owner" / "model"
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    created = await probe.ensure_directory(
        root=str(root),
        path=str(destination),
        expected_volume_uuid=None,
        scope_id=None,
    )
    validated = await probe.validate_directory(
        root=str(root),
        path=str(destination),
        expected_volume_uuid=None,
        scope_id=None,
    )
    (destination / "weights.bin").write_bytes(b"weights")
    nested = destination / "metadata"
    nested.mkdir()
    (nested / "config.json").write_bytes(b"{}")

    size = await probe.directory_size(
        root=str(root),
        path=str(destination),
        scope_id=None,
    )

    assert created == str(destination.resolve())
    assert validated == str(destination.resolve())
    assert destination.is_dir()
    assert size == len(b"weights") + len(b"{}")

    deleted = await probe.delete_directory(
        root=str(root),
        path=str(destination),
        expected_volume_uuid=None,
        scope_id=None,
    )

    assert deleted is True
    assert not destination.exists()
    assert root.is_dir()


@pytest.mark.asyncio
async def test_filesystem_probe_prepares_absent_managed_destination_and_captures_manifest(
    tmp_path: Path,
) -> None:
    physical_root = tmp_path / "PhysicalModels"
    physical_root.mkdir()
    lexical_root = tmp_path / "Models"
    lexical_root.symlink_to(physical_root, target_is_directory=True)
    lexical_destination = lexical_root / "llama.cpp" / "owner" / "model"
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    prepared = await probe.prepare_managed_destination(
        root=str(lexical_root),
        path=str(lexical_destination),
        expected_volume_uuid=None,
        scope_id=None,
    )

    physical_destination = physical_root / "llama.cpp" / "owner" / "model"
    destination_metadata = physical_destination.stat()
    assert prepared.path == str(lexical_destination)
    assert prepared.state_before == "absent"
    assert prepared.created is True
    assert prepared.directory_device == destination_metadata.st_dev
    assert prepared.directory_inode == destination_metadata.st_ino

    first_payload = b"first-weight"
    second_payload = b"{}"
    (physical_destination / "model.gguf").write_bytes(first_payload)
    metadata = physical_destination / "Metadata"
    metadata.mkdir()
    (metadata / "config.json").write_bytes(second_payload)

    captured = await probe.capture_managed_manifest(
        root=str(lexical_root),
        path=str(lexical_destination),
        expected_volume_uuid=None,
        scope_id=None,
        expected_directory_device=prepared.directory_device,
        expected_directory_inode=prepared.directory_inode,
    )

    assert captured.path == str(lexical_destination)
    assert captured.directory_device == prepared.directory_device
    assert captured.directory_inode == prepared.directory_inode
    assert captured.total_bytes == len(first_payload) + len(second_payload)
    assert captured.files == (
        OwnedFile(
            path="Metadata/config.json",
            size_bytes=len(second_payload),
            sha256="sha256:" + hashlib.sha256(second_payload).hexdigest(),
        ),
        OwnedFile(
            path="model.gguf",
            size_bytes=len(first_payload),
            sha256="sha256:" + hashlib.sha256(first_payload).hexdigest(),
        ),
    )


@pytest.mark.asyncio
async def test_filesystem_probe_prepare_is_absent_only_and_rejects_descendant_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    root.mkdir()
    destination = root / "managed" / "model"
    destination.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = root / "linked"
    linked_parent.symlink_to(outside, target_is_directory=True)
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    with pytest.raises(FilesystemProbeError, match="must be absent"):
        await probe.prepare_managed_destination(
            root=str(root),
            path=str(destination),
            expected_volume_uuid=None,
            scope_id=None,
        )
    with pytest.raises(FilesystemProbeError, match="descendant symlink"):
        await probe.prepare_managed_destination(
            root=str(root),
            path=str(linked_parent / "model"),
            expected_volume_uuid=None,
            scope_id=None,
        )

    assert not (outside / "model").exists()


@pytest.mark.asyncio
async def test_filesystem_probe_capture_rejects_replaced_destination_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    root.mkdir()
    destination = root / "managed" / "model"
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )
    prepared = await probe.prepare_managed_destination(
        root=str(root),
        path=str(destination),
        expected_volume_uuid=None,
        scope_id=None,
    )
    old_destination = root / "managed" / "old-model"
    destination.rename(old_destination)
    destination.mkdir()
    (destination / "model.gguf").write_bytes(b"replacement")

    with pytest.raises(FilesystemProbeError, match="identity changed"):
        await probe.capture_managed_manifest(
            root=str(root),
            path=str(destination),
            expected_volume_uuid=None,
            scope_id=None,
            expected_directory_device=prepared.directory_device,
            expected_directory_inode=prepared.directory_inode,
        )
    replacement = b"replacement"
    with pytest.raises(FilesystemProbeError, match="identity changed"):
        await probe.verify_exact_manifest(
            root=str(root),
            path=str(destination),
            files=(
                OwnedFile(
                    path="model.gguf",
                    size_bytes=len(replacement),
                    sha256=(
                        "sha256:" + hashlib.sha256(replacement).hexdigest()
                    ),
                ),
            ),
            expected_volume_uuid=None,
            scope_id=None,
            expected_directory_device=prepared.directory_device,
            expected_directory_inode=prepared.directory_inode,
        )


def test_filesystem_worker_capture_rejects_file_mutation_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Models"
    destination = root / "managed" / "model"
    destination.mkdir(parents=True)
    model = destination / "model.gguf"
    model.write_bytes(b"a" * (2 * 1024 * 1024))
    real_read = fs_worker_module.os.read
    changed = False

    def read_then_change(descriptor: int, size: int) -> bytes:
        nonlocal changed
        block = real_read(descriptor, size)
        if block and not changed:
            changed = True
            model.write_bytes(b"b" * (2 * 1024 * 1024))
        return block

    monkeypatch.setattr(fs_worker_module.os, "read", read_then_change)

    with pytest.raises(ValueError, match="changed while hashing"):
        fs_worker_module._capture_managed_manifest(
            root.resolve(strict=True),
            str(root),
            str(destination),
            expected_directory_device=None,
            expected_directory_inode=None,
            max_files=10,
            max_entries=20,
            max_manifest_bytes=1024 * 1024,
        )

    assert changed is True


@pytest.mark.asyncio
async def test_filesystem_probe_capture_rejects_symlinks_special_files_and_empty_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    destination = root / "managed" / "model"
    destination.mkdir(parents=True)
    model = destination / "model.gguf"
    model.write_bytes(b"weights")
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    link = destination / "linked.gguf"
    link.symlink_to(model)
    with pytest.raises(FilesystemProbeError, match="contains a symlink"):
        await probe.capture_managed_manifest(
            root=str(root),
            path=str(destination),
            expected_volume_uuid=None,
            scope_id=None,
        )
    link.unlink()

    fifo = destination / "download.pipe"
    os.mkfifo(fifo)
    with pytest.raises(FilesystemProbeError, match="special entry"):
        await probe.capture_managed_manifest(
            root=str(root),
            path=str(destination),
            expected_volume_uuid=None,
            scope_id=None,
        )
    fifo.unlink()

    (destination / "empty").mkdir()
    with pytest.raises(FilesystemProbeError, match="empty directory"):
        await probe.capture_managed_manifest(
            root=str(root),
            path=str(destination),
            expected_volume_uuid=None,
            scope_id=None,
        )


@pytest.mark.asyncio
async def test_filesystem_probe_capture_enforces_file_entry_path_and_json_bounds(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    destination = root / "managed" / "model"
    destination.mkdir(parents=True)
    (destination / "one.bin").write_bytes(b"1")
    (destination / "two.bin").write_bytes(b"2")
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    with pytest.raises(FilesystemProbeError, match="too many files"):
        await probe.capture_managed_manifest(
            root=str(root),
            path=str(destination),
            expected_volume_uuid=None,
            scope_id=None,
            max_files=1,
        )
    with pytest.raises(FilesystemProbeError, match="too many entries"):
        await probe.capture_managed_manifest(
            root=str(root),
            path=str(destination),
            expected_volume_uuid=None,
            scope_id=None,
            max_entries=1,
        )
    with pytest.raises(FilesystemProbeError, match="JSON is too large"):
        await probe.capture_managed_manifest(
            root=str(root),
            path=str(destination),
            expected_volume_uuid=None,
            scope_id=None,
            max_manifest_bytes=32,
        )

    long_parent = destination / ("a" * 200) / ("b" * 200) / ("c" * 110)
    long_parent.mkdir(parents=True)
    (long_parent / "weight.bin").write_bytes(b"long")
    with pytest.raises(FilesystemProbeError, match="invalid path"):
        await probe.capture_managed_manifest(
            root=str(root),
            path=str(destination),
            expected_volume_uuid=None,
            scope_id=None,
        )


@pytest.mark.asyncio
async def test_filesystem_probe_capture_preserves_case_distinct_manifest_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Models"
    destination = root / "managed" / "model"
    destination.mkdir(parents=True)
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )
    metadata = destination.stat()
    upper = b"upper"
    lower = b"lower"
    files = (
        OwnedFile(
            path="Model.bin",
            size_bytes=len(upper),
            sha256="sha256:" + hashlib.sha256(upper).hexdigest(),
        ),
        OwnedFile(
            path="model.bin",
            size_bytes=len(lower),
            sha256="sha256:" + hashlib.sha256(lower).hexdigest(),
        ),
    )

    async def fake_run(*_arguments: str, **_kwargs: object) -> dict[str, object]:
        return {
            "ok": True,
            "path": str(destination),
            "directory_device": metadata.st_dev,
            "directory_inode": metadata.st_ino,
            "files": json.loads(canonical_owned_files_json(files)),
            "file_count": 2,
            "total_bytes": len(upper) + len(lower),
            "manifest_digest": (
                "sha256:"
                + hashlib.sha256(
                    canonical_owned_files_json(files).encode("utf-8")
                ).hexdigest()
            ),
        }

    monkeypatch.setattr(probe, "_run", fake_run)

    captured = await probe.capture_managed_manifest(
        root=str(root),
        path=str(destination),
        expected_volume_uuid=None,
        scope_id=None,
    )

    assert tuple(item.path for item in captured.files) == (
        "Model.bin",
        "model.bin",
    )


@pytest.mark.parametrize(
    "operation",
    [
        "validate-model",
        "validate-projector",
        "ensure-directory",
        "validate-directory",
        "directory-size",
        "delete-directory",
    ],
)
@pytest.mark.asyncio
async def test_filesystem_probe_rejects_paths_outside_selected_root(
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / "Models"
    root.mkdir()
    model = _gguf(root / "model-Q4_K_M.gguf")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_gguf = _gguf(outside / "outside.gguf")
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    if operation == "validate-model":
        request = probe.validate_llama(
            root=str(root),
            model=str(outside_gguf),
            projector=None,
            expected_volume_uuid=None,
            scope_id=None,
        )
    elif operation == "validate-projector":
        request = probe.validate_llama(
            root=str(root),
            model=str(model),
            projector=str(outside_gguf),
            expected_volume_uuid=None,
            scope_id=None,
        )
    elif operation == "ensure-directory":
        request = probe.ensure_directory(
            root=str(root),
            path=str(outside / "new-model"),
            expected_volume_uuid=None,
            scope_id=None,
        )
    elif operation == "validate-directory":
        request = probe.validate_directory(
            root=str(root),
            path=str(outside),
            expected_volume_uuid=None,
            scope_id=None,
        )
    elif operation == "directory-size":
        request = probe.directory_size(
            root=str(root),
            path=str(outside),
            scope_id=None,
        )
    else:
        request = probe.delete_directory(
            root=str(root),
            path=str(outside),
            expected_volume_uuid=None,
            scope_id=None,
        )

    with pytest.raises(
        FilesystemProbeError,
        match="path escapes selected model storage",
    ):
        await request
    assert not (outside / "new-model").exists()


@pytest.mark.asyncio
async def test_filesystem_probe_refuses_storage_root_and_symlink_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    root.mkdir()
    destination = root / "managed"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "linked"
    link.symlink_to(outside, target_is_directory=True)
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    with pytest.raises(FilesystemProbeError, match="storage root"):
        await probe.delete_directory(
            root=str(root),
            path=str(root),
            expected_volume_uuid=None,
            scope_id=None,
        )
    with pytest.raises(FilesystemProbeError, match="symlink"):
        await probe.delete_directory(
            root=str(root),
            path=str(link),
            expected_volume_uuid=None,
            scope_id=None,
        )

    assert root.is_dir()
    assert outside.is_dir()


@pytest.mark.asyncio
async def test_filesystem_probe_verifies_exact_manifest_through_lexical_symlink_root(
    tmp_path: Path,
) -> None:
    physical_root = tmp_path / "PhysicalModels"
    destination = physical_root / "managed" / "model"
    nested = destination / "metadata"
    nested.mkdir(parents=True)
    model = destination / "model.gguf"
    marker = nested / "marker.json"
    model.write_bytes(b"GGUFmanaged")
    marker.write_bytes(b"")
    lexical_root = tmp_path / "Models"
    lexical_root.symlink_to(physical_root, target_is_directory=True)
    lexical_destination = lexical_root / "managed" / "model"
    files = (
        OwnedFile(
            path="metadata/marker.json",
            size_bytes=0,
            sha256="sha256:" + hashlib.sha256(b"").hexdigest(),
        ),
        OwnedFile(
            path="model.gguf",
            size_bytes=model.stat().st_size,
            sha256="sha256:" + hashlib.sha256(b"GGUFmanaged").hexdigest(),
        ),
    )
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    assert await probe.verify_exact_manifest(
        root=str(lexical_root),
        path=str(lexical_destination),
        files=files,
        expected_volume_uuid=None,
        scope_id=None,
        expected_directory_device=destination.stat().st_dev,
        expected_directory_inode=destination.stat().st_ino,
    )

    extra = destination / "unexpected.txt"
    extra.write_text("extra", encoding="utf-8")
    with pytest.raises(FilesystemProbeError, match="extra entry"):
        await probe.verify_exact_manifest(
            root=str(lexical_root),
            path=str(lexical_destination),
            files=files,
            expected_volume_uuid=None,
            scope_id=None,
        )
    extra.unlink()

    linked = destination / "linked.bin"
    linked.symlink_to(model)
    with pytest.raises(FilesystemProbeError, match="symlink"):
        await probe.verify_exact_manifest(
            root=str(lexical_root),
            path=str(lexical_destination),
            files=files,
            expected_volume_uuid=None,
            scope_id=None,
        )
    linked.unlink()

    model.write_bytes(b"GGUFchanged-size")
    with pytest.raises(FilesystemProbeError, match="size changed"):
        await probe.verify_exact_manifest(
            root=str(lexical_root),
            path=str(lexical_destination),
            files=files,
            expected_volume_uuid=None,
            scope_id=None,
        )
    model.write_bytes(b"GGUFmanaged")
    model.write_bytes(b"BAD!managed")
    with pytest.raises(FilesystemProbeError, match="digest changed"):
        await probe.verify_exact_manifest(
            root=str(lexical_root),
            path=str(lexical_destination),
            files=files,
            expected_volume_uuid=None,
            scope_id=None,
        )
    model.write_bytes(b"GGUFmanaged")
    marker.unlink()
    with pytest.raises(FilesystemProbeError, match="missing a proven file"):
        await probe.verify_exact_manifest(
            root=str(lexical_root),
            path=str(lexical_destination),
            files=files,
            expected_volume_uuid=None,
            scope_id=None,
        )


@pytest.mark.asyncio
async def test_filesystem_probe_trashes_only_explicit_paths_with_bundled_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Models"
    root.mkdir()
    first = _gguf(root / "publisher" / "model-00001-of-00002.gguf")
    second = _gguf(root / "publisher" / "model-00002-of-00002.gguf")
    helper = tmp_path / "mnemosyne-file-trash"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
        trash_helper=helper,
    )
    captured: dict[str, object] = {}

    async def fake_inspect(*_args: object, **_kwargs: object):
        return inspect_path(str(root))

    async def fake_run_argv(
        argv: list[str],
        **kwargs: object,
    ) -> dict[str, object]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return {"ok": True, "trashed": [str(first), str(second)], "skipped": []}

    monkeypatch.setattr(probe, "inspect", fake_inspect)
    monkeypatch.setattr(probe, "_run_argv", fake_run_argv)

    removed = await probe.trash_paths(
        root=str(root),
        paths=[str(first), str(second)],
        expected_volume_uuid=None,
        scope_id="a" * 64,
    )

    assert removed is True
    assert captured["argv"] == [
        str(helper),
        "--root",
        str(root),
        "--path",
        str(first),
        "--path",
        str(second),
    ]
    assert captured["kwargs"] == {
        "scope_id": "a" * 64,
        "scope_path": str(root),
        "timeout_seconds": 120.0,
    }


@pytest.mark.asyncio
async def test_filesystem_probe_passes_exact_manifest_to_trash_helper_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Models"
    destination = root / "managed"
    destination.mkdir(parents=True)
    model = destination / "model.gguf"
    model.write_bytes(b"GGUFmanaged")
    helper = tmp_path / "mnemosyne-file-trash"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
        trash_helper=helper,
    )
    files = (
        OwnedFile(
            path="model.gguf",
            size_bytes=model.stat().st_size,
            sha256="sha256:" + "c" * 64,
        ),
    )
    captured: dict[str, object] = {}

    async def fake_inspect(*_args: object, **_kwargs: object):
        return inspect_path(str(root))

    async def fake_run_argv(
        argv: list[str],
        **kwargs: object,
    ) -> dict[str, object]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return {"ok": True, "trashed": [str(destination)], "skipped": []}

    monkeypatch.setattr(probe, "inspect", fake_inspect)
    monkeypatch.setattr(probe, "_run_argv", fake_run_argv)

    assert await probe.trash_paths(
        root=str(root),
        paths=(str(destination),),
        expected_volume_uuid=None,
        scope_id=None,
        exact_manifest=files,
        expected_directory_device=destination.stat().st_dev,
        expected_directory_inode=destination.stat().st_ino,
    )
    assert captured["argv"] == [
        str(helper),
        "--root",
        str(root),
        "--path",
        str(destination),
        "--verify-manifest-stdin",
        "--expected-directory-device",
        str(destination.stat().st_dev),
        "--expected-directory-inode",
        str(destination.stat().st_ino),
    ]
    assert captured["kwargs"] == {
        "scope_id": None,
        "scope_path": str(root),
        "timeout_seconds": 120.0,
        "stdin_payload": canonical_owned_files_json(files).encode("utf-8"),
    }


@pytest.mark.asyncio
async def test_filesystem_probe_requires_paired_trash_directory_identity_and_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    destination = root / "managed"
    destination.mkdir(parents=True)
    helper = tmp_path / "mnemosyne-file-trash"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
        trash_helper=helper,
    )

    with pytest.raises(FilesystemProbeError, match="provided together"):
        await probe.trash_paths(
            root=str(root),
            paths=(str(destination),),
            expected_volume_uuid=None,
            scope_id=None,
            exact_manifest=(),
            expected_directory_device=destination.stat().st_dev,
        )
    with pytest.raises(FilesystemProbeError, match="requires an exact"):
        await probe.trash_paths(
            root=str(root),
            paths=(str(destination),),
            expected_volume_uuid=None,
            scope_id=None,
            expected_directory_device=destination.stat().st_dev,
            expected_directory_inode=destination.stat().st_ino,
        )


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_filesystem_probe_timeout_terminates_process_group_without_executor_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "blocking_helper.py"
    pids_path = tmp_path / "pids.json"
    helper.write_text(
        "\n".join(
            [
                "import json",
                "from pathlib import Path",
                "import subprocess",
                "import sys",
                "import time",
                "child = subprocess.Popen(",
                "    [sys.executable, '-c', 'import time; time.sleep(60)']",
                ")",
                "Path(sys.argv[1]).write_text(",
                "    json.dumps({'parent': __import__('os').getpid(), 'child': child.pid}),",
                "    encoding='utf-8',",
                ")",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def launch_blocking_helper(
        *_arguments: str,
        **kwargs: object,
    ) -> asyncio.subprocess.Process:
        return await real_create_subprocess_exec(
            sys.executable,
            str(helper),
            str(pids_path),
            **kwargs,
        )

    monkeypatch.setattr(
        filesystem_module.asyncio,
        "create_subprocess_exec",
        launch_blocking_helper,
    )
    executor_threads_before = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("asyncio")
    }
    heartbeat = asyncio.Event()

    async def beat() -> None:
        await asyncio.sleep(0.05)
        heartbeat.set()

    heartbeat_task = asyncio.create_task(beat())
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=0.35,
    )
    pids: dict[str, int] = {}
    try:
        with pytest.raises(FilesystemProbeError, match="operation timed out"):
            await probe.inspect(str(tmp_path))
        await heartbeat_task
        assert heartbeat.is_set()
        assert pids_path.exists()
        pids = json.loads(pids_path.read_text(encoding="utf-8"))

        deadline = asyncio.get_running_loop().time() + 3
        while (
            any(_process_is_running(pid) for pid in pids.values())
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.05)
        assert {
            label: pid
            for label, pid in pids.items()
            if _process_is_running(pid)
        } == {}

        executor_threads_after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("asyncio")
        }
        assert executor_threads_after == executor_threads_before
    finally:
        if not heartbeat_task.done():
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
        for pid in pids.values():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_filesystem_probe_cancellation_terminates_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "cancelled_helper.py"
    pids_path = tmp_path / "cancelled-pids.json"
    helper.write_text(
        "\n".join(
            [
                "import json, os, subprocess, sys, time",
                "from pathlib import Path",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                "Path(sys.argv[1]).write_text(json.dumps({'parent': os.getpid(), 'child': child.pid}), encoding='utf-8')",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def launch_blocking_helper(
        *_arguments: str,
        **kwargs: object,
    ) -> asyncio.subprocess.Process:
        return await real_create_subprocess_exec(
            sys.executable,
            str(helper),
            str(pids_path),
            **kwargs,
        )

    monkeypatch.setattr(
        filesystem_module.asyncio,
        "create_subprocess_exec",
        launch_blocking_helper,
    )
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=30,
    )
    task = asyncio.create_task(probe.inspect(str(tmp_path)))
    deadline = asyncio.get_running_loop().time() + 3
    while not pids_path.exists() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert pids_path.exists()
    pids = json.loads(pids_path.read_text(encoding="utf-8"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = asyncio.get_running_loop().time() + 3
    while (
        any(_process_is_running(pid) for pid in pids.values())
        and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.05)
    try:
        assert {
            label: pid
            for label, pid in pids.items()
            if _process_is_running(pid)
        } == {}
    finally:
        for pid in pids.values():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_wrap_scoped_argv_preserves_unscoped_command_and_wraps_scoped_command(
    tmp_path: Path,
) -> None:
    command = ["/usr/bin/env", "printf", "ready"]

    assert wrap_scoped_argv(
        command,
        scope_root=None,
        scope_id=None,
        scope_path=None,
    ) == command

    wrapped = wrap_scoped_argv(
        command,
        scope_root=tmp_path / "scopes",
        scope_id="a" * 64,
        scope_path="/Volumes/Athena/models",
        python_executable="/test/python",
        remove_pythonpath=("/service/source",),
    )
    assert wrapped == [
        "/test/python",
        "-m",
        "mnemosyne_macos.scope_exec",
        "--scope-root",
        str(tmp_path / "scopes"),
        "--scope-id",
        "a" * 64,
        "--scope-path",
        "/Volumes/Athena/models",
        "--remove-pythonpath",
        "/service/source",
        "--",
        *command,
    ]


def test_wrap_scoped_argv_rejects_incomplete_scoped_command() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        wrap_scoped_argv(
            [],
            scope_root=None,
            scope_id=None,
            scope_path=None,
        )
    with pytest.raises(ValueError, match="bookmark root and exact path"):
        wrap_scoped_argv(
            ["/usr/bin/true"],
            scope_root=None,
            scope_id="a" * 64,
            scope_path="/Volumes/Athena/models",
        )


def _ordinary_bookmark(path: Path) -> bytes:
    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    byte_pointer = ctypes.POINTER(ctypes.c_ubyte)
    core.CFURLCreateFromFileSystemRepresentation.argtypes = [
        ctypes.c_void_p,
        byte_pointer,
        ctypes.c_long,
        ctypes.c_ubyte,
    ]
    core.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    core.CFURLCreateBookmarkData.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    core.CFURLCreateBookmarkData.restype = ctypes.c_void_p
    core.CFDataGetLength.argtypes = [ctypes.c_void_p]
    core.CFDataGetLength.restype = ctypes.c_long
    core.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    core.CFDataGetBytePtr.restype = byte_pointer
    core.CFRelease.argtypes = [ctypes.c_void_p]

    path_bytes = os.fsencode(path)
    raw_path = (ctypes.c_ubyte * len(path_bytes)).from_buffer_copy(path_bytes)
    url_ref = core.CFURLCreateFromFileSystemRepresentation(
        None,
        raw_path,
        len(path_bytes),
        1,
    )
    if not url_ref:
        raise RuntimeError("could not create test folder URL")
    error_ref = ctypes.c_void_p()
    data_ref = core.CFURLCreateBookmarkData(
        None,
        url_ref,
        0,
        None,
        None,
        ctypes.byref(error_ref),
    )
    core.CFRelease(url_ref)
    if error_ref.value:
        core.CFRelease(error_ref.value)
    if not data_ref:
        raise RuntimeError("could not create test folder bookmark")
    try:
        length = core.CFDataGetLength(data_ref)
        pointer = core.CFDataGetBytePtr(data_ref)
        if length <= 0 or not pointer:
            raise RuntimeError("test folder bookmark was empty")
        return ctypes.string_at(pointer, length)
    finally:
        core.CFRelease(data_ref)


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreFoundation is macOS-only")
@pytest.mark.asyncio
async def test_scoped_child_opens_file_through_real_persisted_bookmark(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    target = selected / "payload.txt"
    target.write_text("scoped child ready", encoding="utf-8")
    scope_root = tmp_path / "state" / "security-scopes"
    registry = SecurityScopeRegistry(
        scope_root,
        resolver=DarwinBookmarkResolver(),
    )
    try:
        transfer = _ordinary_bookmark(selected)
        try:
            registered = registry.register(
                str(selected),
                base64.b64encode(transfer).decode("ascii"),
            )
        except SecurityScopeError as exc:
            pytest.skip(f"the test runner has no transferable folder grant: {exc}")
    finally:
        registry.close()

    assert (scope_root / f"{registered.id}.bookmark").is_file()
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import sys; "
            "print(Path(sys.argv[1]).read_text(encoding='utf-8'))"
        ),
        str(target),
    ]
    argv = wrap_scoped_argv(
        command,
        scope_root=scope_root,
        scope_id=registered.id,
        scope_path=str(selected),
    )
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    decoded_stderr = stderr.decode("utf-8", errors="replace")
    if process.returncode == 78 and "selected-folder grant" in decoded_stderr:
        pytest.skip(f"the child process could not consume the folder grant: {decoded_stderr}")

    assert process.returncode == 0, decoded_stderr
    assert stdout.decode("utf-8").strip() == "scoped child ready"
