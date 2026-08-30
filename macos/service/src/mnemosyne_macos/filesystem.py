"""Async client for killable protected-filesystem helper processes."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any, Sequence

from .install_provenance import (
    MAX_OWNED_FILES,
    MAX_PROVENANCE_JSON_BYTES,
    DestinationStateBefore,
    OwnedFile,
    ProvenanceDataError,
    canonical_owned_files_json,
    owned_manifest_digest,
)
from .local_models import LocalModel, LocalProjector
from .scoped_process import wrap_scoped_argv
from .storage import StorageStatus


class FilesystemProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedManagedDestination:
    """Immutable creation facts returned by the isolated filesystem helper."""

    path: str
    state_before: str
    created: bool
    directory_device: int
    directory_inode: int
    recovered_from_marker: bool = False
    unowned_preexisting: bool = False


@dataclass(frozen=True, slots=True)
class CapturedManagedManifest:
    """A bounded exact-file snapshot tied to one directory identity."""

    path: str
    directory_device: int
    directory_inode: int
    files: tuple[OwnedFile, ...]
    total_bytes: int


class FilesystemProbe:
    def __init__(
        self,
        *,
        scope_root: str | Path,
        timeout_seconds: float = 30.0,
        trash_helper: str | Path | None = None,
    ) -> None:
        self.scope_root = Path(scope_root).expanduser()
        self.timeout_seconds = timeout_seconds
        configured_helper = trash_helper or os.environ.get(
            "MNEMOSYNE_FILE_TRASH_HELPER"
        )
        self.trash_helper = (
            Path(configured_helper).expanduser()
            if configured_helper is not None
            else None
        )

    async def _run(
        self,
        *arguments: str,
        scope_id: str | None = None,
        scope_path: str | None = None,
        timeout_seconds: float | None = None,
        stdin_payload: bytes | None = None,
    ) -> dict[str, Any]:
        argv = [sys.executable, "-m", "mnemosyne_macos.fs_worker", *arguments]
        return await self._run_argv(
            argv,
            scope_id=scope_id,
            scope_path=scope_path,
            timeout_seconds=timeout_seconds,
            stdin_payload=stdin_payload,
        )

    async def _run_argv(
        self,
        argv: list[str],
        *,
        scope_id: str | None = None,
        scope_path: str | None = None,
        timeout_seconds: float | None = None,
        stdin_payload: bytes | None = None,
    ) -> dict[str, Any]:
        argv = wrap_scoped_argv(
            argv,
            scope_root=self.scope_root,
            scope_id=scope_id,
            scope_path=scope_path,
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=(
                asyncio.subprocess.PIPE
                if stdin_payload is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timeout = timeout_seconds or self.timeout_seconds
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin_payload),
                timeout=timeout,
            )
        except TimeoutError as exc:
            await _terminate(process)
            raise FilesystemProbeError(
                "model storage operation timed out; verify the selected-folder "
                "permission and that its volume is mounted"
            ) from exc
        except asyncio.CancelledError:
            # Client disconnects and service shutdown must not strand a helper
            # that is blocked in an open/stat on an unavailable volume.
            await asyncio.shield(_terminate(process))
            raise
        if len(stdout) > 8 * 1024 * 1024 or len(stderr) > 512 * 1024:
            raise FilesystemProbeError("model storage helper returned too much data")
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
            raise FilesystemProbeError(
                detail or f"model storage helper exited with status {process.returncode}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            message = payload.get("error") if isinstance(payload, dict) else None
            raise FilesystemProbeError(
                str(message or f"model storage helper exited with status {process.returncode}")
            )
        if process.returncode != 0:
            raise FilesystemProbeError(
                f"model storage helper exited with status {process.returncode}"
            )
        return payload

    async def inspect(
        self,
        path: str,
        *,
        name: str | None = None,
        expected_volume_uuid: str | None = None,
        scope_id: str | None = None,
        scope_path: str | None = None,
    ) -> StorageStatus:
        arguments = ["inspect", "--path", path]
        if name is not None:
            arguments.extend(["--name", name])
        if expected_volume_uuid is not None:
            arguments.extend(["--expected-volume-uuid", expected_volume_uuid])
        payload = await self._run(
            *arguments,
            scope_id=scope_id,
            scope_path=scope_path or path,
        )
        return StorageStatus(**payload["status"])

    async def scan(
        self,
        path: str,
        *,
        scope_id: str | None = None,
        scope_path: str | None = None,
        max_files: int = 100_000,
        max_models: int = 2_000,
    ) -> tuple[StorageStatus, list[LocalModel]]:
        payload = await self._run(
            "scan",
            "--path",
            path,
            "--max-files",
            str(max_files),
            "--max-models",
            str(max_models),
            scope_id=scope_id,
            scope_path=scope_path or path,
        )
        models: list[LocalModel] = []
        for value in payload["models"]:
            projectors = tuple(
                LocalProjector(**projector)
                for projector in value.pop("projector_options", [])
            )
            value["all_paths"] = tuple(value["all_paths"])
            value["capabilities"] = tuple(value["capabilities"])
            models.append(LocalModel(**value, projector_options=projectors))
        return StorageStatus(**payload["status"]), models

    async def validate_llama(
        self,
        *,
        root: str,
        model: str,
        projector: str | None,
        expected_volume_uuid: str | None,
        scope_id: str | None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        arguments = [
            "validate-llama",
            "--root",
            root,
            "--model",
            model,
        ]
        if projector is not None:
            arguments.extend(["--projector", projector])
        if expected_volume_uuid is not None:
            arguments.extend(["--expected-volume-uuid", expected_volume_uuid])
        return await self._run(
            *arguments,
            scope_id=scope_id,
            scope_path=root,
            timeout_seconds=timeout_seconds,
        )

    async def ensure_directory(
        self,
        *,
        root: str,
        path: str,
        expected_volume_uuid: str | None,
        scope_id: str | None,
    ) -> str:
        arguments = ["ensure-directory", "--root", root, "--path", path]
        if expected_volume_uuid is not None:
            arguments.extend(["--expected-volume-uuid", expected_volume_uuid])
        payload = await self._run(
            *arguments,
            scope_id=scope_id,
            scope_path=root,
        )
        return str(payload["path"])

    async def prepare_managed_destination(
        self,
        *,
        root: str,
        path: str,
        expected_volume_uuid: str | None,
        scope_id: str | None,
        installation_id: str | None = None,
        creation_transaction_id: str | None = None,
        destination_binding_digest: str | None = None,
        source_identity_digest: str | None = None,
        allow_preexisting_unowned: bool = False,
        timeout_seconds: float | None = None,
    ) -> PreparedManagedDestination:
        """Create one absent managed destination and retain its exact identity.

        This is intentionally stricter than :meth:`ensure_directory`.  It is
        evidence collection for a new exclusive-managed transaction, not a
        general-purpose directory creation API.
        """

        arguments = ["prepare-managed-destination", "--root", root, "--path", path]
        marker_values = (
            installation_id,
            creation_transaction_id,
            destination_binding_digest,
            source_identity_digest,
        )
        if any(value is not None for value in marker_values):
            if not all(value is not None for value in marker_values):
                raise FilesystemProbeError(
                    "managed destination recovery marker authority is incomplete"
                )
            arguments.extend(
                [
                    "--installation-id",
                    str(installation_id),
                    "--creation-transaction-id",
                    str(creation_transaction_id),
                    "--destination-binding-digest",
                    str(destination_binding_digest),
                    "--source-identity-digest",
                    str(source_identity_digest),
                ]
            )
        if allow_preexisting_unowned:
            arguments.append("--allow-preexisting-unowned")
        if expected_volume_uuid is not None:
            arguments.extend(["--expected-volume-uuid", expected_volume_uuid])
        payload = await self._run(
            *arguments,
            scope_id=scope_id,
            scope_path=root,
            timeout_seconds=timeout_seconds,
        )
        lexical_path = os.path.abspath(os.path.expanduser(path))
        if payload.get("path") != lexical_path:
            raise FilesystemProbeError(
                "managed destination helper returned an invalid path"
            )
        unowned_preexisting = payload.get("unowned_preexisting") is True
        if unowned_preexisting:
            if (
                not allow_preexisting_unowned
                or payload.get("destination_created_by_transaction") is not False
            ):
                raise FilesystemProbeError(
                    "managed destination helper returned invalid preexisting facts"
                )
        elif (
            payload.get("destination_state_before")
            != DestinationStateBefore.ABSENT.value
            or payload.get("destination_created_by_transaction") is not True
        ):
            raise FilesystemProbeError(
                "managed destination helper returned invalid creation facts"
            )
        directory_device = _required_nonnegative_int(
            payload.get("directory_device"),
            label="directory device",
        )
        directory_inode = _required_positive_int(
            payload.get("directory_inode"),
            label="directory inode",
        )
        return PreparedManagedDestination(
            path=lexical_path,
            state_before=(
                DestinationStateBefore.UNKNOWN.value
                if unowned_preexisting
                else DestinationStateBefore.ABSENT.value
            ),
            created=not unowned_preexisting,
            directory_device=directory_device,
            directory_inode=directory_inode,
            recovered_from_marker=payload.get("recovered_from_marker") is True,
            unowned_preexisting=unowned_preexisting,
        )

    async def capture_managed_manifest(
        self,
        *,
        root: str,
        path: str,
        expected_volume_uuid: str | None,
        scope_id: str | None,
        expected_directory_device: int | None = None,
        expected_directory_inode: int | None = None,
        max_files: int = MAX_OWNED_FILES,
        max_entries: int = MAX_OWNED_FILES * 2 + 1,
        max_manifest_bytes: int = MAX_PROVENANCE_JSON_BYTES,
        timeout_seconds: float | None = None,
    ) -> CapturedManagedManifest:
        """Hash an exact, bounded regular-file tree in a killable helper."""

        if (expected_directory_device is None) != (
            expected_directory_inode is None
        ):
            raise FilesystemProbeError(
                "expected directory device and inode must be provided together"
            )
        arguments = [
            "capture-managed-manifest",
            "--root",
            root,
            "--path",
            path,
            "--max-files",
            str(max_files),
            "--max-entries",
            str(max_entries),
            "--max-manifest-bytes",
            str(max_manifest_bytes),
        ]
        if expected_volume_uuid is not None:
            arguments.extend(["--expected-volume-uuid", expected_volume_uuid])
        if expected_directory_device is not None:
            arguments.extend(
                ["--expected-directory-device", str(expected_directory_device)]
            )
            arguments.extend(
                ["--expected-directory-inode", str(expected_directory_inode)]
            )
        payload = await self._run(
            *arguments,
            scope_id=scope_id,
            scope_path=root,
            timeout_seconds=timeout_seconds,
        )
        lexical_path = os.path.abspath(os.path.expanduser(path))
        if payload.get("path") != lexical_path:
            raise FilesystemProbeError(
                "managed manifest helper returned an invalid path"
            )
        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            raise FilesystemProbeError(
                "managed manifest helper returned invalid files"
            )
        try:
            files = tuple(
                OwnedFile(
                    path=item["path"],
                    size_bytes=item["size_bytes"],
                    sha256=item["sha256"],
                )
                for item in raw_files
                if isinstance(item, dict)
                and set(item) == {"path", "size_bytes", "sha256"}
            )
            if len(files) != len(raw_files):
                raise ProvenanceDataError("owned_manifest_invalid")
            canonical = json.loads(canonical_owned_files_json(files))
        except (KeyError, TypeError, ProvenanceDataError) as exc:
            raise FilesystemProbeError(
                "managed manifest helper returned invalid files"
            ) from exc
        if canonical != raw_files:
            raise FilesystemProbeError(
                "managed manifest helper returned noncanonical files"
            )
        directory_device = _required_nonnegative_int(
            payload.get("directory_device"),
            label="directory device",
        )
        directory_inode = _required_positive_int(
            payload.get("directory_inode"),
            label="directory inode",
        )
        if (
            expected_directory_device is not None
            and (
                directory_device != expected_directory_device
                or directory_inode != expected_directory_inode
            )
        ):
            raise FilesystemProbeError(
                "managed destination directory identity changed"
            )
        file_count = _required_positive_int(
            payload.get("file_count"),
            label="file count",
        )
        total_bytes = _required_nonnegative_int(
            payload.get("total_bytes"),
            label="total bytes",
        )
        if file_count != len(files) or total_bytes != sum(
            item.size_bytes for item in files
        ):
            raise FilesystemProbeError(
                "managed manifest helper returned inconsistent totals"
            )
        digest = owned_manifest_digest(files)
        if payload.get("manifest_digest") != digest:
            raise FilesystemProbeError(
                "managed manifest helper returned an invalid digest"
            )
        return CapturedManagedManifest(
            path=lexical_path,
            directory_device=directory_device,
            directory_inode=directory_inode,
            files=files,
            total_bytes=total_bytes,
        )

    async def validate_directory(
        self,
        *,
        root: str,
        path: str,
        expected_volume_uuid: str | None,
        scope_id: str | None,
        timeout_seconds: float | None = None,
    ) -> str:
        arguments = ["validate-directory", "--root", root, "--path", path]
        if expected_volume_uuid is not None:
            arguments.extend(["--expected-volume-uuid", expected_volume_uuid])
        payload = await self._run(
            *arguments,
            scope_id=scope_id,
            scope_path=root,
            timeout_seconds=timeout_seconds,
        )
        return str(payload["path"])

    async def directory_size(
        self,
        *,
        root: str,
        path: str,
        scope_id: str | None,
    ) -> int:
        payload = await self._run(
            "directory-size",
            "--root",
            root,
            "--path",
            path,
            scope_id=scope_id,
            scope_path=root,
        )
        return int(payload["bytes"])

    async def delete_directory(
        self,
        *,
        root: str,
        path: str,
        expected_volume_uuid: str | None,
        scope_id: str | None,
    ) -> bool:
        arguments = ["delete-directory", "--root", root, "--path", path]
        if expected_volume_uuid is not None:
            arguments.extend(["--expected-volume-uuid", expected_volume_uuid])
        payload = await self._run(
            *arguments,
            scope_id=scope_id,
            scope_path=root,
        )
        return bool(payload["deleted"])

    async def verify_exact_manifest(
        self,
        *,
        root: str,
        path: str,
        files: Sequence[OwnedFile],
        expected_volume_uuid: str | None,
        scope_id: str | None,
        expected_directory_device: int | None = None,
        expected_directory_inode: int | None = None,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Freshly prove an exact regular-file tree in a killable helper."""

        if (expected_directory_device is None) != (
            expected_directory_inode is None
        ):
            raise FilesystemProbeError(
                "expected directory device and inode must be provided together"
            )
        try:
            manifest = canonical_owned_files_json(files).encode("utf-8")
        except ProvenanceDataError as exc:
            raise FilesystemProbeError("managed model manifest is invalid") from exc
        arguments = ["verify-manifest", "--root", root, "--path", path]
        if expected_volume_uuid is not None:
            arguments.extend(["--expected-volume-uuid", expected_volume_uuid])
        if expected_directory_device is not None:
            arguments.extend(
                ["--expected-directory-device", str(expected_directory_device)]
            )
            arguments.extend(
                ["--expected-directory-inode", str(expected_directory_inode)]
            )
        payload = await self._run(
            *arguments,
            scope_id=scope_id,
            scope_path=root,
            timeout_seconds=timeout_seconds,
            stdin_payload=manifest,
        )
        lexical_path = os.path.abspath(os.path.expanduser(path))
        if payload.get("verified") is not True or payload.get("path") != lexical_path:
            raise FilesystemProbeError(
                "managed model manifest helper returned an invalid result"
            )
        if expected_directory_device is not None and (
            payload.get("directory_device") != expected_directory_device
            or payload.get("directory_inode") != expected_directory_inode
        ):
            raise FilesystemProbeError(
                "managed destination directory identity changed"
            )
        return True

    async def trash_paths(
        self,
        *,
        root: str,
        paths: list[str] | tuple[str, ...],
        expected_volume_uuid: str | None,
        scope_id: str | None,
        exact_manifest: Sequence[OwnedFile] | None = None,
        expected_directory_device: int | None = None,
        expected_directory_inode: int | None = None,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Move exact, pre-discovered model paths to the macOS Trash."""

        if not paths:
            raise FilesystemProbeError("model cleanup did not identify any files")
        if (expected_directory_device is None) != (
            expected_directory_inode is None
        ):
            raise FilesystemProbeError(
                "expected directory device and inode must be provided together"
            )
        if expected_directory_device is not None:
            if exact_manifest is None:
                raise FilesystemProbeError(
                    "expected directory identity requires an exact managed manifest"
                )
            _required_nonnegative_int(
                expected_directory_device,
                label="directory device",
            )
            _required_positive_int(
                expected_directory_inode,
                label="directory inode",
            )
        helper = self.trash_helper
        if (
            helper is None
            or not helper.is_file()
            or not os.access(helper, os.X_OK)
        ):
            raise FilesystemProbeError(
                "the bundled model Trash helper is unavailable; reinstall "
                "Unified Inference before deleting imported model files"
            )
        status = await self.inspect(
            root,
            expected_volume_uuid=expected_volume_uuid,
            scope_id=scope_id,
            scope_path=root,
        )
        if not status.writable or not status.volume_matches:
            raise FilesystemProbeError(
                status.diagnostic or "selected model storage is not writable"
            )
        arguments = [str(helper), "--root", root]
        if expected_volume_uuid is not None:
            arguments.extend(
                ["--expected-volume-uuid", expected_volume_uuid]
            )
        for path in paths:
            arguments.extend(["--path", path])
        stdin_payload: bytes | None = None
        if exact_manifest is not None:
            if len(paths) != 1:
                raise FilesystemProbeError(
                    "an exact managed manifest requires one destination"
                )
            try:
                stdin_payload = canonical_owned_files_json(exact_manifest).encode(
                    "utf-8"
                )
            except ProvenanceDataError as exc:
                raise FilesystemProbeError(
                    "managed model manifest is invalid"
                ) from exc
            arguments.append("--verify-manifest-stdin")
            if expected_directory_device is not None:
                arguments.extend(
                    [
                        "--expected-directory-device",
                        str(expected_directory_device),
                        "--expected-directory-inode",
                        str(expected_directory_inode),
                    ]
                )
        run_options: dict[str, Any] = {
            "scope_id": scope_id,
            "scope_path": root,
            "timeout_seconds": max(
                120.0,
                self.timeout_seconds,
                timeout_seconds or 0.0,
            ),
        }
        if stdin_payload is not None:
            run_options["stdin_payload"] = stdin_payload
        payload = await self._run_argv(arguments, **run_options)
        trashed = payload.get("trashed")
        if not isinstance(trashed, list):
            raise FilesystemProbeError(
                "model Trash helper returned an invalid result"
            )
        return bool(trashed)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await process.wait()


def _required_nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FilesystemProbeError(
            f"managed filesystem helper returned an invalid {label}"
        )
    return value


def _required_positive_int(value: object, *, label: str) -> int:
    parsed = _required_nonnegative_int(value, label=label)
    if parsed == 0:
        raise FilesystemProbeError(
            f"managed filesystem helper returned an invalid {label}"
        )
    return parsed


__all__ = [
    "CapturedManagedManifest",
    "FilesystemProbe",
    "FilesystemProbeError",
    "PreparedManagedDestination",
]
