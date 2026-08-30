"""Killable filesystem operations for protected and removable model folders."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Any
from uuid import UUID

from .install_provenance import (
    MAX_OWNED_FILES,
    MAX_PROVENANCE_JSON_BYTES,
    MANAGED_CREATION_MARKER_PATH,
    OwnedFile,
    ProvenanceDataError,
    canonical_owned_files_json,
    owned_manifest_digest,
)
from .local_models import scan_local_models
from .storage import inspect_path


_MAX_LEXICAL_PATH_BYTES = 4096
_MAX_CAPTURE_ENTRIES = MAX_OWNED_FILES * 2 + 1
_HASH_CHUNK_BYTES = 1024 * 1024
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--path", required=True)
    inspect.add_argument("--name")
    inspect.add_argument("--expected-volume-uuid")

    scan = commands.add_parser("scan")
    scan.add_argument("--path", required=True)
    scan.add_argument("--max-files", type=int, default=100_000)
    scan.add_argument("--max-models", type=int, default=2_000)

    validate = commands.add_parser("validate-llama")
    validate.add_argument("--root", required=True)
    validate.add_argument("--model", required=True)
    validate.add_argument("--projector")
    validate.add_argument("--expected-volume-uuid")

    validate_directory = commands.add_parser("validate-directory")
    validate_directory.add_argument("--root", required=True)
    validate_directory.add_argument("--path", required=True)
    validate_directory.add_argument("--expected-volume-uuid")

    size = commands.add_parser("directory-size")
    size.add_argument("--root", required=True)
    size.add_argument("--path", required=True)

    ensure = commands.add_parser("ensure-directory")
    ensure.add_argument("--root", required=True)
    ensure.add_argument("--path", required=True)
    ensure.add_argument("--expected-volume-uuid")

    prepare = commands.add_parser("prepare-managed-destination")
    prepare.add_argument("--root", required=True)
    prepare.add_argument("--path", required=True)
    prepare.add_argument("--expected-volume-uuid")
    prepare.add_argument("--installation-id")
    prepare.add_argument("--creation-transaction-id")
    prepare.add_argument("--destination-binding-digest")
    prepare.add_argument("--source-identity-digest")
    prepare.add_argument("--allow-preexisting-unowned", action="store_true")

    capture = commands.add_parser("capture-managed-manifest")
    capture.add_argument("--root", required=True)
    capture.add_argument("--path", required=True)
    capture.add_argument("--expected-volume-uuid")
    capture.add_argument("--expected-directory-device", type=int)
    capture.add_argument("--expected-directory-inode", type=int)
    capture.add_argument("--max-files", type=int, default=MAX_OWNED_FILES)
    capture.add_argument("--max-entries", type=int, default=_MAX_CAPTURE_ENTRIES)
    capture.add_argument(
        "--max-manifest-bytes",
        type=int,
        default=MAX_PROVENANCE_JSON_BYTES,
    )

    delete = commands.add_parser("delete-directory")
    delete.add_argument("--root", required=True)
    delete.add_argument("--path", required=True)
    delete.add_argument("--expected-volume-uuid")

    verify_manifest = commands.add_parser("verify-manifest")
    verify_manifest.add_argument("--root", required=True)
    verify_manifest.add_argument("--path", required=True)
    verify_manifest.add_argument("--expected-volume-uuid")
    verify_manifest.add_argument("--expected-directory-device", type=int)
    verify_manifest.add_argument("--expected-directory-inode", type=int)
    return parser


def _contained(root: Path, value: str, *, must_exist: bool) -> Path:
    candidate = Path(value).expanduser().resolve(strict=must_exist)
    if candidate == root or candidate.is_relative_to(root):
        return candidate
    raise ValueError(f"path escapes selected model storage: {candidate}")


def _gguf(root: Path, value: str, *, label: str) -> Path:
    path = _contained(root, value, must_exist=True)
    if not path.is_file():
        raise ValueError(f"{label} GGUF file is unavailable: {path}")
    if path.suffix.casefold() != ".gguf":
        raise ValueError(f"{label} must select a .gguf file: {path}")
    with path.open("rb") as stream:
        if stream.read(4) != b"GGUF":
            raise ValueError(f"{label} does not have a GGUF header: {path}")
    return path


def _validated_root(path: str, expected_volume_uuid: str | None) -> tuple[Path, dict]:
    status = inspect_path(path, expected_volume_uuid=expected_volume_uuid)
    if not status.exists or not status.is_directory or not status.volume_matches:
        raise ValueError(status.diagnostic or "selected model storage is unavailable")
    # StorageStatus intentionally preserves the user's lexical path, including
    # a symlink. Resolve only inside this bounded helper for containment.
    return Path(status.path).resolve(strict=True), status.to_dict()


def _directory_size(root: Path, path: str) -> int:
    selected = _contained(root, path, must_exist=False)
    if not selected.exists():
        return 0
    total = 0
    for current, _directories, files in os.walk(selected, followlinks=False):
        current_path = Path(current)
        for filename in files:
            candidate = _contained(root, str(current_path / filename), must_exist=True)
            if candidate.is_file():
                total += candidate.stat().st_size
    return total


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _lexical_descendant(root_value: str, path: str) -> tuple[Path, tuple[str, ...]]:
    lexical_root = Path(os.path.abspath(os.path.expanduser(root_value)))
    lexical_target = Path(os.path.abspath(os.path.expanduser(path)))
    try:
        if (
            len(os.fsencode(lexical_root)) > _MAX_LEXICAL_PATH_BYTES
            or len(os.fsencode(lexical_target)) > _MAX_LEXICAL_PATH_BYTES
        ):
            raise ValueError("managed model path is too long")
    except UnicodeEncodeError as exc:
        raise ValueError("managed model path is not valid UTF-8") from exc
    try:
        relative = lexical_target.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError(
            f"path escapes selected model storage: {lexical_target}"
        ) from exc
    if not relative.parts:
        raise ValueError("refusing to use the selected model storage root")
    return lexical_target, tuple(relative.parts)


def _open_selected_root(root: Path, root_value: str) -> tuple[int, tuple[int, int]]:
    """Open the selected root while deliberately allowing that boundary symlink."""

    lexical_root = os.path.abspath(os.path.expanduser(root_value))
    try:
        expected = os.stat(root)
        descriptor = os.open(
            lexical_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise ValueError("selected model storage is unavailable") from exc
    observed = os.fstat(descriptor)
    if not stat.S_ISDIR(observed.st_mode) or _identity(observed) != _identity(expected):
        os.close(descriptor)
        raise ValueError("selected model storage changed during inspection")
    return descriptor, _identity(observed)


def _assert_selected_root_identity(
    root_value: str,
    expected_identity: tuple[int, int],
) -> None:
    try:
        descriptor = os.open(
            os.path.abspath(os.path.expanduser(root_value)),
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise ValueError("selected model storage changed during inspection") from exc
    try:
        if _identity(os.fstat(descriptor)) != expected_identity:
            raise ValueError("selected model storage changed during inspection")
    finally:
        os.close(descriptor)


def _child_metadata(
    parent_descriptor: int,
    component: str,
    *,
    display_path: str,
) -> os.stat_result:
    try:
        return os.stat(
            component,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(
            f"managed model path could not be inspected: {display_path}"
        ) from exc


def _open_child_directory(
    parent_descriptor: int,
    component: str,
    *,
    display_path: str,
) -> tuple[int, os.stat_result]:
    metadata = _child_metadata(
        parent_descriptor,
        component,
        display_path=display_path,
    )
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(
            f"managed model path contains a descendant symlink: {display_path}"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(
            f"managed model path contains a non-directory: {display_path}"
        )
    try:
        descriptor = os.open(
            component,
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(
                f"managed model path contains a descendant symlink: {display_path}"
            ) from exc
        raise ValueError(
            f"managed model path changed while opening: {display_path}"
        ) from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or _identity(opened) != _identity(metadata)
    ):
        os.close(descriptor)
        raise ValueError(
            f"managed model path changed while opening: {display_path}"
        )
    return descriptor, opened


def _open_descendant(
    root_descriptor: int,
    parts: tuple[str, ...],
    *,
    lexical_root: Path,
) -> tuple[int, os.stat_result]:
    descriptor = os.dup(root_descriptor)
    metadata = os.fstat(descriptor)
    cursor = lexical_root
    try:
        for component in parts:
            cursor /= component
            next_descriptor, metadata = _open_child_directory(
                descriptor,
                component,
                display_path=str(cursor),
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _prepare_managed_destination(
    root: Path,
    root_value: str,
    path: str,
    *,
    marker_bytes: bytes | None,
    allow_preexisting_unowned: bool,
) -> dict[str, Any]:
    """Create an absent destination without following any path below root."""

    lexical_target, parts = _lexical_descendant(root_value, path)
    lexical_root = Path(os.path.abspath(os.path.expanduser(root_value)))
    root_descriptor, root_identity = _open_selected_root(root, root_value)
    parent_descriptor = os.dup(root_descriptor)
    cursor = lexical_root
    try:
        for component in parts[:-1]:
            cursor /= component
            try:
                next_descriptor, _metadata = _open_child_directory(
                    parent_descriptor,
                    component,
                    display_path=str(cursor),
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, dir_fd=parent_descriptor)
                except FileExistsError:
                    # A concurrent creator is acceptable only when the entry
                    # is now the same kind of no-follow directory we require.
                    pass
                except OSError as exc:
                    raise ValueError(
                        f"managed model parent could not be created: {cursor}"
                    ) from exc
                next_descriptor, _metadata = _open_child_directory(
                    parent_descriptor,
                    component,
                    display_path=str(cursor),
                )
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor

        final_component = parts[-1]
        try:
            existing = _child_metadata(
                parent_descriptor,
                final_component,
                display_path=str(lexical_target),
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            destination_descriptor, destination_metadata = _open_child_directory(
                parent_descriptor,
                final_component,
                display_path=str(lexical_target),
            )
            try:
                if marker_bytes is not None and _has_exact_creation_marker(
                    destination_descriptor,
                    marker_bytes,
                ):
                    recovered = True
                elif allow_preexisting_unowned:
                    directory_device, directory_inode = _identity(
                        destination_metadata
                    )
                    return {
                        "path": str(lexical_target),
                        "destination_state_before": "unknown",
                        "destination_created_by_transaction": False,
                        "directory_device": directory_device,
                        "directory_inode": directory_inode,
                        "recovered_from_marker": False,
                        "unowned_preexisting": True,
                    }
                elif marker_bytes is None:
                    raise ValueError("managed model destination must be absent")
                else:
                    raise ValueError(
                        "managed model destination has no exact recovery marker"
                    )
            finally:
                os.close(destination_descriptor)
        else:
            recovered = False
        if existing is None:
            try:
                os.mkdir(final_component, dir_fd=parent_descriptor)
            except FileExistsError as exc:
                raise ValueError(
                    "managed model destination appeared during preparation"
                ) from exc
            except OSError as exc:
                raise ValueError("managed model destination could not be created") from exc
        destination_descriptor, destination_metadata = _open_child_directory(
            parent_descriptor,
            final_component,
            display_path=str(lexical_target),
        )
        try:
            if not recovered and marker_bytes is not None:
                _write_creation_marker(destination_descriptor, marker_bytes)
            # Reopen the complete route before recording ownership facts. This
            # catches renamed/replaced ancestors and a retargeted selected-root
            # symlink without ever removing the created directory.
            reopened, reopened_metadata = _open_descendant(
                root_descriptor,
                parts,
                lexical_root=lexical_root,
            )
            try:
                if _identity(reopened_metadata) != _identity(destination_metadata):
                    raise ValueError(
                        "managed model destination changed during preparation"
                    )
            finally:
                os.close(reopened)
            _assert_selected_root_identity(root_value, root_identity)
            directory_device, directory_inode = _identity(destination_metadata)
            return {
                "path": str(lexical_target),
                "destination_state_before": "absent",
                "destination_created_by_transaction": True,
                "directory_device": directory_device,
                "directory_inode": directory_inode,
                "recovered_from_marker": recovered,
                "unowned_preexisting": False,
            }
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(parent_descriptor)
        os.close(root_descriptor)


def _creation_marker_bytes(args: argparse.Namespace) -> bytes | None:
    values = (
        args.installation_id,
        args.creation_transaction_id,
        args.destination_binding_digest,
        args.source_identity_digest,
    )
    if not any(value is not None for value in values):
        return None
    if not all(isinstance(value, str) for value in values):
        raise ValueError("managed creation marker authority is incomplete")
    installation_id, transaction_id, binding_digest, source_digest = values
    try:
        if (
            str(UUID(installation_id)) != installation_id
            or str(UUID(transaction_id)) != transaction_id
        ):
            raise ValueError
    except (AttributeError, ValueError):
        raise ValueError("managed creation marker identity is invalid") from None
    for digest in (binding_digest, source_digest):
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in digest[7:])
        ):
            raise ValueError("managed creation marker digest is invalid")
    return json.dumps(
        {
            "creation_transaction_id": transaction_id,
            "destination_binding_digest": binding_digest,
            "installation_id": installation_id,
            "schema_version": 1,
            "source_identity_digest": source_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_creation_marker(destination_descriptor: int, payload: bytes) -> None:
    cache_descriptor: int | None = None
    huggingface_descriptor: int | None = None
    try:
        for parent_descriptor, component, display_path in (
            (destination_descriptor, ".cache", ".cache"),
        ):
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
            cache_descriptor, _metadata = _open_child_directory(
                parent_descriptor,
                component,
                display_path=display_path,
            )
        assert cache_descriptor is not None
        try:
            os.mkdir("huggingface", mode=0o700, dir_fd=cache_descriptor)
        except FileExistsError:
            pass
        huggingface_descriptor, _metadata = _open_child_directory(
            cache_descriptor,
            "huggingface",
            display_path=".cache/huggingface",
        )
        marker_name = MANAGED_CREATION_MARKER_PATH.rsplit("/", 1)[1]
        descriptor = os.open(
            marker_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=huggingface_descriptor,
        )
        try:
            written = 0
            while written < len(payload):
                amount = os.write(descriptor, payload[written:])
                if amount <= 0:
                    raise OSError("managed creation marker write made no progress")
                written += amount
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(huggingface_descriptor)
        os.fsync(cache_descriptor)
        os.fsync(destination_descriptor)
    except OSError as exc:
        raise ValueError("managed creation recovery marker could not be written") from exc
    finally:
        if huggingface_descriptor is not None:
            os.close(huggingface_descriptor)
        if cache_descriptor is not None:
            os.close(cache_descriptor)


def _has_exact_creation_marker(
    destination_descriptor: int,
    expected: bytes,
) -> bool:
    """Recover only a directory containing our exact marker and no other entry."""

    descriptors: list[int] = []
    try:
        if sorted(os.listdir(destination_descriptor)) != [".cache"]:
            return False
        cache_descriptor, _metadata = _open_child_directory(
            destination_descriptor,
            ".cache",
            display_path=".cache",
        )
        descriptors.append(cache_descriptor)
        if sorted(os.listdir(cache_descriptor)) != ["huggingface"]:
            return False
        huggingface_descriptor, _metadata = _open_child_directory(
            cache_descriptor,
            "huggingface",
            display_path=".cache/huggingface",
        )
        descriptors.append(huggingface_descriptor)
        marker_name = MANAGED_CREATION_MARKER_PATH.rsplit("/", 1)[1]
        if sorted(os.listdir(huggingface_descriptor)) != [marker_name]:
            return False
        metadata = os.stat(
            marker_name,
            dir_fd=huggingface_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(expected):
            return False
        descriptor = os.open(
            marker_name,
            _FILE_OPEN_FLAGS,
            dir_fd=huggingface_descriptor,
        )
        try:
            observed = b""
            while len(observed) <= len(expected):
                block = os.read(descriptor, len(expected) + 1 - len(observed))
                if not block:
                    break
                observed += block
            return observed == expected
        finally:
            os.close(descriptor)
    except (FileNotFoundError, OSError, ValueError):
        return False
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and _identity(left) == _identity(right)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _validate_manifest_relative_path(relative_path: str) -> None:
    try:
        canonical_owned_files_json(
            (
                OwnedFile(
                    path=relative_path,
                    size_bytes=0,
                    sha256="sha256:" + "0" * 64,
                ),
            )
        )
    except ProvenanceDataError as exc:
        raise ValueError("managed model manifest contains an invalid path") from exc


def _validate_manifest_directory_path(relative_path: str) -> None:
    """Bound a prefix while leaving room for ``/`` plus a one-byte filename."""

    try:
        encoded = relative_path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("managed model manifest contains an invalid path") from exc
    if (
        not 1 <= len(encoded) <= 510
        or relative_path.startswith("/")
        or "\\" in relative_path
        or "\x00" in relative_path
        or any(
            component in {"", ".", ".."}
            for component in relative_path.split("/")
        )
    ):
        raise ValueError("managed model manifest contains an invalid path")


def _hash_regular_file(
    parent_descriptor: int,
    name: str,
    relative_path: str,
    initial_metadata: os.stat_result,
) -> OwnedFile:
    try:
        descriptor = os.open(name, _FILE_OPEN_FLAGS, dir_fd=parent_descriptor)
    except OSError as exc:
        raise ValueError(
            f"managed model file changed while opening: {relative_path}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file_snapshot(
            initial_metadata,
            opened,
        ):
            raise ValueError(
                f"managed model file changed while opening: {relative_path}"
            )
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, _HASH_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
        completed = os.fstat(descriptor)
        if not _same_file_snapshot(opened, completed):
            raise ValueError(
                f"managed model file changed while hashing: {relative_path}"
            )
    finally:
        os.close(descriptor)
    try:
        current = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ValueError(
            f"managed model file changed after hashing: {relative_path}"
        ) from exc
    if not _same_file_snapshot(completed, current):
        raise ValueError(
            f"managed model file changed after hashing: {relative_path}"
        )
    return OwnedFile(
        path=relative_path,
        size_bytes=int(completed.st_size),
        sha256="sha256:" + digest.hexdigest(),
    )


def _capture_tree(
    descriptor: int,
    *,
    prefix: str,
    files: list[OwnedFile],
    counters: dict[str, int],
    max_files: int,
    max_entries: int,
) -> int:
    try:
        initial_names = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise ValueError("managed model manifest could not be listed") from exc
    counters["entries"] += len(initial_names)
    if counters["entries"] > max_entries:
        raise ValueError("managed model manifest contains too many entries")

    subtree_file_count = 0
    for name in initial_names:
        relative_path = f"{prefix}/{name}" if prefix else name
        display_path = relative_path
        try:
            metadata = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError(
                f"managed model manifest changed during inspection: {display_path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(
                f"managed model manifest contains a symlink: {display_path}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            _validate_manifest_directory_path(relative_path)
            child_descriptor, child_metadata = _open_child_directory(
                descriptor,
                name,
                display_path=display_path,
            )
            try:
                child_file_count = _capture_tree(
                    child_descriptor,
                    prefix=relative_path,
                    files=files,
                    counters=counters,
                    max_files=max_files,
                    max_entries=max_entries,
                )
                try:
                    current_child = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise ValueError(
                        "managed model directory changed during inspection: "
                        f"{display_path}"
                    ) from exc
                if (
                    not stat.S_ISDIR(current_child.st_mode)
                    or _identity(current_child) != _identity(child_metadata)
                ):
                    raise ValueError(
                        "managed model directory changed during inspection: "
                        f"{display_path}"
                    )
                if child_file_count == 0:
                    raise ValueError(
                        f"managed model manifest contains an empty directory: {display_path}"
                    )
                subtree_file_count += child_file_count
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(
                f"managed model manifest contains a special entry: {display_path}"
            )
        _validate_manifest_relative_path(relative_path)
        if len(files) >= max_files:
            raise ValueError("managed model manifest contains too many files")
        files.append(
            _hash_regular_file(
                descriptor,
                name,
                relative_path,
                metadata,
            )
        )
        subtree_file_count += 1

    try:
        completed_names = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise ValueError("managed model manifest changed during inspection") from exc
    if completed_names != initial_names:
        raise ValueError("managed model manifest changed during inspection")
    return subtree_file_count


def _capture_managed_manifest(
    root: Path,
    root_value: str,
    path: str,
    *,
    expected_directory_device: int | None,
    expected_directory_inode: int | None,
    max_files: int,
    max_entries: int,
    max_manifest_bytes: int,
) -> dict[str, Any]:
    if (expected_directory_device is None) != (expected_directory_inode is None):
        raise ValueError(
            "expected directory device and inode must be provided together"
        )
    if (
        expected_directory_device is not None
        and (
            expected_directory_device < 0
            or expected_directory_inode is None
            or expected_directory_inode <= 0
        )
    ):
        raise ValueError("expected directory identity is invalid")
    if not 1 <= max_files <= MAX_OWNED_FILES:
        raise ValueError("managed manifest file limit is invalid")
    if not 1 <= max_entries <= _MAX_CAPTURE_ENTRIES:
        raise ValueError("managed manifest entry limit is invalid")
    if not 1 <= max_manifest_bytes <= MAX_PROVENANCE_JSON_BYTES:
        raise ValueError("managed manifest JSON limit is invalid")

    lexical_target, parts = _lexical_descendant(root_value, path)
    lexical_root = Path(os.path.abspath(os.path.expanduser(root_value)))
    root_descriptor, root_identity = _open_selected_root(root, root_value)
    try:
        destination_descriptor, destination_metadata = _open_descendant(
            root_descriptor,
            parts,
            lexical_root=lexical_root,
        )
        try:
            directory_device, directory_inode = _identity(destination_metadata)
            if expected_directory_device is not None and (
                directory_device != expected_directory_device
                or directory_inode != expected_directory_inode
            ):
                raise ValueError("managed destination directory identity changed")
            files: list[OwnedFile] = []
            _capture_tree(
                destination_descriptor,
                prefix="",
                files=files,
                counters={"entries": 0},
                max_files=max_files,
                max_entries=max_entries,
            )
            if not files:
                raise ValueError("managed model manifest contains no files")
            manifest_json = canonical_owned_files_json(files)
            if len(manifest_json.encode("utf-8")) > max_manifest_bytes:
                raise ValueError("managed model manifest JSON is too large")

            reopened, reopened_metadata = _open_descendant(
                root_descriptor,
                parts,
                lexical_root=lexical_root,
            )
            try:
                if _identity(reopened_metadata) != _identity(destination_metadata):
                    raise ValueError(
                        "managed destination directory identity changed"
                    )
            finally:
                os.close(reopened)
            _assert_selected_root_identity(root_value, root_identity)
            canonical_files = json.loads(manifest_json)
            canonical_owned_files = tuple(OwnedFile(**item) for item in canonical_files)
            return {
                "path": str(lexical_target),
                "directory_device": directory_device,
                "directory_inode": directory_inode,
                "files": canonical_files,
                "file_count": len(canonical_owned_files),
                "total_bytes": sum(item.size_bytes for item in canonical_owned_files),
                "manifest_digest": owned_manifest_digest(canonical_owned_files),
            }
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(root_descriptor)


def _delete_directory(root: Path, root_value: str, path: str) -> bool:
    lexical_root = Path(root_value).expanduser().absolute()
    lexical_target = Path(path).expanduser().absolute()
    try:
        relative = lexical_target.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError(
            f"path escapes selected model storage: {lexical_target}"
        ) from exc
    if not relative.parts:
        raise ValueError("refusing to delete the selected model storage root")

    current = lexical_root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise ValueError(
                f"refusing to delete a model path containing a symlink: {current}"
            )

    selected = _contained(root, str(lexical_target), must_exist=False)
    if selected == root:
        raise ValueError("refusing to delete the selected model storage root")
    if not selected.exists():
        return False
    if not selected.is_dir():
        raise ValueError("managed model deletion requires a directory")
    shutil.rmtree(lexical_target)
    return True


def _manifest_from_stdin() -> tuple[OwnedFile, ...]:
    encoded = sys.stdin.buffer.read(MAX_PROVENANCE_JSON_BYTES + 1)
    if not encoded or len(encoded) > MAX_PROVENANCE_JSON_BYTES:
        raise ValueError("managed model manifest is missing or too large")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("managed model manifest is invalid") from exc
    if not isinstance(payload, list):
        raise ValueError("managed model manifest is invalid")
    try:
        files = tuple(
            OwnedFile(
                path=item["path"],
                size_bytes=item["size_bytes"],
                sha256=item["sha256"],
            )
            for item in payload
            if isinstance(item, dict)
            and set(item) == {"path", "size_bytes", "sha256"}
        )
        if len(files) != len(payload):
            raise ProvenanceDataError("owned_manifest_invalid")
        canonical = json.loads(canonical_owned_files_json(files))
    except (KeyError, TypeError, ProvenanceDataError) as exc:
        raise ValueError("managed model manifest is invalid") from exc
    return tuple(OwnedFile(**item) for item in canonical)


def _verify_manifest(
    root: Path,
    root_value: str,
    path: str,
    files: tuple[OwnedFile, ...],
    *,
    expected_directory_device: int | None = None,
    expected_directory_inode: int | None = None,
) -> dict[str, Any]:
    """Verify exact paths, sizes, and bytes through the hardened capture path."""

    try:
        captured = _capture_managed_manifest(
            root,
            root_value,
            path,
            expected_directory_device=expected_directory_device,
            expected_directory_inode=expected_directory_inode,
            max_files=MAX_OWNED_FILES,
            max_entries=_MAX_CAPTURE_ENTRIES,
            max_manifest_bytes=MAX_PROVENANCE_JSON_BYTES,
        )
    except ValueError as exc:
        empty_prefix = "managed model manifest contains an empty directory: "
        message = str(exc)
        if message.startswith(empty_prefix):
            directory = message.removeprefix(empty_prefix)
            if any(item.path.startswith(directory + "/") for item in files):
                raise ValueError(
                    "managed model manifest is missing a proven file"
                ) from exc
            raise ValueError(
                "managed model manifest contains an extra entry: " + directory
            ) from exc
        raise
    observed = tuple(OwnedFile(**item) for item in captured["files"])
    expected_by_path = {item.path: item for item in files}
    observed_by_path = {item.path: item for item in observed}
    extra_paths = sorted(set(observed_by_path) - set(expected_by_path))
    if extra_paths:
        raise ValueError(
            "managed model manifest contains an extra entry: " + extra_paths[0]
        )
    if set(expected_by_path) - set(observed_by_path):
        raise ValueError("managed model manifest is missing a proven file")
    for relative_path in sorted(expected_by_path):
        expected = expected_by_path[relative_path]
        actual = observed_by_path[relative_path]
        if actual.size_bytes != expected.size_bytes:
            raise ValueError(f"managed model file size changed: {relative_path}")
        if actual.sha256 != expected.sha256:
            raise ValueError(f"managed model file digest changed: {relative_path}")
    return {
        "verified": True,
        "path": captured["path"],
        "directory_device": captured["directory_device"],
        "directory_inode": captured["directory_inode"],
        "file_count": len(observed),
        "total_bytes": sum(item.size_bytes for item in observed),
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "inspect":
        return {
            "status": inspect_path(
                args.path,
                name=args.name,
                expected_volume_uuid=args.expected_volume_uuid,
            ).to_dict()
        }
    if args.command == "scan":
        root, status = _validated_root(args.path, None)
        models = scan_local_models(
            root,
            lexical_root=status["path"],
            max_files=args.max_files,
            max_models=args.max_models,
        )
        return {"status": status, "models": [model.to_dict() for model in models]}
    if args.command == "validate-llama":
        root, status = _validated_root(args.root, args.expected_volume_uuid)
        model = _gguf(root, args.model, label="model")
        projector = (
            _gguf(root, args.projector, label="multimodal projector")
            if args.projector
            else None
        )
        if projector == model:
            raise ValueError("the model and multimodal projector must be different GGUF files")
        return {
            "status": status,
            "model": str(model),
            "projector": str(projector) if projector is not None else None,
        }
    if args.command == "validate-directory":
        root, status = _validated_root(args.root, args.expected_volume_uuid)
        selected = _contained(root, args.path, must_exist=True)
        if not selected.is_dir():
            raise ValueError("model target is not a directory")
        return {"status": status, "path": str(selected)}
    if args.command == "directory-size":
        root, _status = _validated_root(args.root, None)
        return {"bytes": _directory_size(root, args.path)}
    if args.command == "ensure-directory":
        root, status = _validated_root(args.root, args.expected_volume_uuid)
        if not status["writable"]:
            raise ValueError(status["diagnostic"] or "selected model storage is not writable")
        # Keep the exact lexical route through the user-selected storage root
        # as the public result.  ``_contained`` resolves symlinks solely to
        # prove that the target remains under the validated physical root;
        # returning that resolved proof path would silently replace a selected
        # nested/symlink location in installer state and child argv.
        lexical_destination = Path(
            os.path.abspath(os.path.expanduser(args.path))
        )
        destination = _contained(root, args.path, must_exist=False)
        destination.mkdir(parents=True, exist_ok=True)
        destination = _contained(root, str(destination), must_exist=True)
        if not destination.is_dir():
            raise ValueError("model destination is not a directory")
        return {"status": status, "path": str(lexical_destination)}
    if args.command == "prepare-managed-destination":
        root, status = _validated_root(args.root, args.expected_volume_uuid)
        if not status["writable"]:
            raise ValueError(
                status["diagnostic"] or "selected model storage is not writable"
            )
        return {
            "status": status,
            **_prepare_managed_destination(
                root,
                args.root,
                args.path,
                marker_bytes=_creation_marker_bytes(args),
                allow_preexisting_unowned=args.allow_preexisting_unowned,
            ),
        }
    if args.command == "capture-managed-manifest":
        root, status = _validated_root(args.root, args.expected_volume_uuid)
        if not status["writable"]:
            raise ValueError(
                status["diagnostic"] or "selected model storage is not writable"
            )
        return {
            "status": status,
            **_capture_managed_manifest(
                root,
                args.root,
                args.path,
                expected_directory_device=args.expected_directory_device,
                expected_directory_inode=args.expected_directory_inode,
                max_files=args.max_files,
                max_entries=args.max_entries,
                max_manifest_bytes=args.max_manifest_bytes,
            ),
        }
    if args.command == "delete-directory":
        root, status = _validated_root(args.root, args.expected_volume_uuid)
        if not status["writable"]:
            raise ValueError(
                status["diagnostic"] or "selected model storage is not writable"
            )
        return {
            "status": status,
            "deleted": _delete_directory(root, args.root, args.path),
        }
    if args.command == "verify-manifest":
        root, status = _validated_root(args.root, args.expected_volume_uuid)
        if not status["writable"]:
            raise ValueError(
                status["diagnostic"] or "selected model storage is not writable"
            )
        return {
            "status": status,
            **_verify_manifest(
                root,
                args.root,
                args.path,
                _manifest_from_stdin(),
                expected_directory_device=args.expected_directory_device,
                expected_directory_inode=args.expected_directory_inode,
            ),
        }
    raise ValueError("unsupported filesystem operation")


def main() -> None:
    try:
        payload = {"ok": True, **_run(_parser().parse_args())}
        code = 0
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
        code = 2
    print(json.dumps(payload, separators=(",", ":")), flush=True)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
