"""Async client for killable protected-filesystem helper processes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any

from .local_models import LocalModel, LocalProjector
from .scoped_process import wrap_scoped_argv
from .storage import StorageStatus


class FilesystemProbeError(RuntimeError):
    pass


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
    ) -> dict[str, Any]:
        argv = [sys.executable, "-m", "mnemosyne_macos.fs_worker", *arguments]
        return await self._run_argv(
            argv,
            scope_id=scope_id,
            scope_path=scope_path,
            timeout_seconds=timeout_seconds,
        )

    async def _run_argv(
        self,
        argv: list[str],
        *,
        scope_id: str | None = None,
        scope_path: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        argv = wrap_scoped_argv(
            argv,
            scope_root=self.scope_root,
            scope_id=scope_id,
            scope_path=scope_path,
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timeout = timeout_seconds or self.timeout_seconds
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
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

    async def trash_paths(
        self,
        *,
        root: str,
        paths: list[str] | tuple[str, ...],
        expected_volume_uuid: str | None,
        scope_id: str | None,
    ) -> bool:
        """Move exact, pre-discovered model paths to the macOS Trash."""

        if not paths:
            raise FilesystemProbeError("model cleanup did not identify any files")
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
        payload = await self._run_argv(
            arguments,
            scope_id=scope_id,
            scope_path=root,
            timeout_seconds=max(120.0, self.timeout_seconds),
        )
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


__all__ = ["FilesystemProbe", "FilesystemProbeError"]
