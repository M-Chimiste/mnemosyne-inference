"""Model-library storage inspection and path-safe destination resolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

from .config import StorageConfig, StorageLocationConfig
from .models import EngineName


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StorageStatus:
    name: str | None
    path: str
    exists: bool
    is_directory: bool
    writable: bool
    mount_path: str | None
    volume_uuid: str | None
    expected_volume_uuid: str | None
    volume_matches: bool
    total_bytes: int | None
    free_bytes: int | None
    diagnostic: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def inspect_path(
    path: str,
    *,
    name: str | None = None,
    expected_volume_uuid: str | None = None,
) -> StorageStatus:
    """Inspect an existing folder without creating it or following a fallback path."""

    expanded = Path(path).expanduser()
    exists = expanded.exists()
    is_directory = expanded.is_dir() if exists else False
    writable = is_directory and os.access(expanded, os.W_OK)
    selected_path = expanded.absolute()
    resolved = expanded.resolve() if exists else expanded.absolute()
    mount_path: str | None = None
    volume_uuid: str | None = None
    total_bytes: int | None = None
    free_bytes: int | None = None
    diagnostic: str | None = None

    if is_directory:
        mount_path, volume_uuid = _volume_identity(resolved)
        try:
            usage = shutil.disk_usage(resolved)
            total_bytes = usage.total
            free_bytes = usage.free
        except OSError as exc:
            diagnostic = f"could not read disk usage: {exc}"
    elif exists:
        diagnostic = "selected model storage path is not a directory"
    else:
        diagnostic = "selected model storage folder is unavailable"

    volume_matches = (
        expected_volume_uuid is None
        or (
            volume_uuid is not None
            and volume_uuid.casefold() == expected_volume_uuid.casefold()
        )
    )
    if is_directory and not writable:
        diagnostic = "selected model storage folder is not writable"
    if not volume_matches:
        diagnostic = "the folder is not on the volume originally selected"

    return StorageStatus(
        name=name,
        # Preserve the exact nested/symlink path selected by the user. The
        # resolved target is used only for volume and disk probes.
        path=str(selected_path),
        exists=exists,
        is_directory=is_directory,
        writable=writable,
        mount_path=mount_path,
        volume_uuid=volume_uuid,
        expected_volume_uuid=expected_volume_uuid,
        volume_matches=volume_matches,
        total_bytes=total_bytes,
        free_bytes=free_bytes,
        diagnostic=diagnostic,
    )


def inspect_location(location: StorageLocationConfig) -> StorageStatus:
    return inspect_path(
        location.path,
        name=location.name,
        expected_volume_uuid=location.volume_uuid,
    )


def resolve_location(storage: StorageConfig, name: str | None = None) -> Path:
    selected = name or storage.default
    location = next(
        (candidate for candidate in storage.locations if candidate.name == selected),
        None,
    )
    if location is None:
        raise StorageError(f"unknown storage location '{selected}'")
    status = inspect_location(location)
    if not status.exists or not status.is_directory:
        raise StorageError(status.diagnostic or f"storage '{selected}' is unavailable")
    if not status.writable or not status.volume_matches:
        raise StorageError(status.diagnostic or f"storage '{selected}' is not writable")
    return Path(status.path)


def install_destination(root: Path, engine: EngineName, repo_id: str) -> Path:
    pieces = repo_id.split("/")
    if len(pieces) != 2 or any(not _safe_component(piece) for piece in pieces):
        raise StorageError("Hugging Face repository must use the form owner/model")
    normalized_root = Path(os.path.abspath(os.path.expanduser(str(root))))
    destination = Path(
        os.path.abspath(
            os.path.join(normalized_root, engine.value, pieces[0], pieces[1])
        )
    )
    try:
        contained = os.path.commonpath([str(normalized_root), str(destination)]) == str(
            normalized_root
        )
    except ValueError:
        contained = False
    if destination == normalized_root or not contained:
        raise StorageError("resolved model destination escapes the configured storage root")
    return destination


def _safe_component(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "/" not in value and "\\" not in value


def _volume_identity(path: Path) -> tuple[str, str | None]:
    mount = path
    while mount.parent != mount and not os.path.ismount(mount):
        mount = mount.parent

    volume_uuid: str | None = None
    diskutil = Path("/usr/sbin/diskutil")
    if diskutil.exists():
        try:
            result = subprocess.run(
                [str(diskutil), "info", "-plist", str(path)],
                check=True,
                capture_output=True,
                timeout=5,
            )
            payload = plistlib.loads(result.stdout)
            reported_mount = payload.get("MountPoint")
            if isinstance(reported_mount, str) and reported_mount:
                mount = Path(reported_mount)
            reported_uuid = payload.get("VolumeUUID")
            if isinstance(reported_uuid, str) and reported_uuid:
                volume_uuid = reported_uuid
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
            pass
    return str(mount), volume_uuid


__all__ = [
    "StorageError",
    "StorageStatus",
    "inspect_location",
    "inspect_path",
    "install_destination",
    "resolve_location",
]
