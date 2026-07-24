"""Killable, durable native model-install orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
from typing import Awaitable, Callable

from .config import StorageConfig, StorageLocationConfig
from .filesystem import FilesystemProbe
from .install_store import InstallRecord, InstallStore
from .scoped_process import wrap_scoped_argv


InstallCallback = Callable[[InstallRecord], Awaitable[None]]


class NativeInstaller:
    def __init__(
        self,
        database_path: str | Path,
        *,
        on_installed: InstallCallback | None = None,
        storage: StorageConfig | None = None,
        filesystem_probe: FilesystemProbe | None = None,
    ) -> None:
        self.store = InstallStore(database_path)
        self.on_installed = on_installed
        self.storage = storage
        self.filesystem = filesystem_probe
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._semaphore = asyncio.Semaphore(1)

    async def start(self) -> None:
        await asyncio.to_thread(self.store.recover_interrupted)

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await asyncio.to_thread(self.store.close)

    async def create(
        self,
        *,
        repo_id: str,
        engine: str,
        storage: str,
        alias: str,
        destination: str,
        revision: str | None,
        filename: str | None,
        projector_filename: str | None = None,
        download_files: list[str] | tuple[str, ...] = (),
        capabilities: list[str] | tuple[str, ...] | None = None,
        family: str | None,
        total_bytes: int | None,
    ) -> InstallRecord:
        record = await asyncio.to_thread(
            self.store.create,
            repo_id=repo_id,
            engine=engine,
            storage=storage,
            alias=alias,
            destination=destination,
            revision=revision,
            filename=filename,
            projector_filename=projector_filename,
            download_files=download_files,
            capabilities=capabilities,
            family=family,
            total_bytes=total_bytes,
        )
        self._schedule(record.id)
        return record

    async def retry(self, install_id: str) -> InstallRecord:
        record = await asyncio.to_thread(self.store.get, install_id)
        if record.status not in {"failed", "cancelled", "partial", "downloaded"}:
            raise ValueError(
                "only failed, cancelled, partial, or downloaded installs can be retried"
            )
        active = self._tasks.get(install_id)
        if active is not None and not active.done():
            # A terminal state is persisted just before the original task
            # returns. Let its cleanup finish so a fast UI retry cannot leave
            # the record in "registering" without a replacement task.
            await asyncio.gather(active, return_exceptions=True)
        next_status = "registering" if record.status == "downloaded" else "queued"
        record = await asyncio.to_thread(
            self.store.update,
            install_id,
            status=next_status,
            error=None,
            pid=None,
        )
        self._schedule(install_id)
        return record

    async def cancel(self, install_id: str) -> InstallRecord:
        record = await asyncio.to_thread(self.store.get, install_id)
        if record.status not in {"queued", "downloading", "registering"}:
            raise ValueError("install is not active")
        task = self._tasks.get(install_id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return await asyncio.to_thread(self.store.get, install_id)

    async def get(self, install_id: str) -> InstallRecord:
        return await asyncio.to_thread(self.store.get, install_id)

    async def list(self, *, limit: int = 100) -> list[InstallRecord]:
        return await asyncio.to_thread(self.store.list, limit=limit)

    def _schedule(self, install_id: str) -> None:
        if install_id in self._tasks and not self._tasks[install_id].done():
            raise ValueError("install is already active")
        task = asyncio.create_task(self._run(install_id), name=f"model-install-{install_id}")
        self._tasks[install_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(install_id, None))

    async def _run(self, install_id: str) -> None:
        process: asyncio.subprocess.Process | None = None
        try:
            async with self._semaphore:
                record = await asyncio.to_thread(self.store.get, install_id)
                if record.status == "registering":
                    await self._register_downloaded(record)
                    return
                location = self._storage_location(record)
                if location is not None and self.filesystem is not None:
                    destination = await self.filesystem.ensure_directory(
                        root=location.path,
                        path=record.destination,
                        expected_volume_uuid=location.volume_uuid,
                        scope_id=location.scope_id,
                    )
                    if _lexical_path(destination) != _lexical_path(record.destination):
                        raise RuntimeError("model destination changed during validation")
                argv = [
                    sys.executable,
                    "-m",
                    "mnemosyne_macos.download_worker",
                    "--repo-id",
                    record.repo_id,
                    "--destination",
                    record.destination,
                ]
                if record.revision:
                    argv.extend(["--revision", record.revision])
                try:
                    download_files = (
                        json.loads(record.files_json) if record.files_json else []
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    download_files = []
                if isinstance(download_files, list) and download_files:
                    for filename in download_files:
                        if isinstance(filename, str) and filename:
                            argv.extend(["--include-file", filename])
                elif record.filename:
                    argv.extend(["--filename", record.filename])
                if location is not None:
                    argv = wrap_scoped_argv(
                        argv,
                        scope_root=(
                            self.filesystem.scope_root
                            if self.filesystem is not None
                            else None
                        ),
                        scope_id=location.scope_id,
                        scope_path=location.path,
                    )
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                self._processes[install_id] = process
                await asyncio.to_thread(
                    self.store.update,
                    install_id,
                    status="downloading",
                    pid=process.pid,
                    error=None,
                )
                communicate = asyncio.create_task(process.communicate())
                while not communicate.done():
                    await asyncio.sleep(1)
                    downloaded = await self._downloaded_size(record, location)
                    await asyncio.to_thread(
                        self.store.update,
                        install_id,
                        bytes_downloaded=downloaded,
                    )
                stdout, stderr = await communicate
                downloaded = await self._downloaded_size(record, location)
                if process.returncode != 0:
                    message = stderr.decode("utf-8", errors="replace").strip()[-2000:]
                    raise RuntimeError(message or f"download worker exited with {process.returncode}")
                record = await asyncio.to_thread(
                    self.store.update,
                    install_id,
                    status="registering",
                    bytes_downloaded=downloaded,
                    pid=None,
                    error=None,
                )
                await self._register_downloaded(record)
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                await _terminate(process)
            current = await asyncio.to_thread(self.store.get, install_id)
            if current.status in {"queued", "downloading"}:
                status = "cancelled"
                error = "download cancelled"
            elif current.status == "registering":
                status = "downloaded"
                error = "download completed but profile registration was cancelled"
            else:
                status = current.status
                error = current.error
            location = self._storage_location(current)
            downloaded = await self._downloaded_size(current, location)
            await asyncio.to_thread(
                self.store.update,
                install_id,
                status=status,
                bytes_downloaded=downloaded,
                pid=None,
                error=error,
            )
            raise
        except Exception as exc:
            current = await asyncio.to_thread(self.store.get, install_id)
            location = self._storage_location(current)
            try:
                downloaded = await self._downloaded_size(current, location)
            except Exception:
                downloaded = current.bytes_downloaded
            await asyncio.to_thread(
                self.store.update,
                install_id,
                status="failed",
                bytes_downloaded=downloaded,
                pid=None,
                error=str(exc),
            )
        finally:
            self._processes.pop(install_id, None)

    async def _register_downloaded(self, record: InstallRecord) -> None:
        """Finish profile creation without invoking the download worker again."""

        if self.on_installed is not None:
            try:
                await self.on_installed(record)
            except Exception as exc:
                await asyncio.to_thread(
                    self.store.update,
                    record.id,
                    status="downloaded",
                    pid=None,
                    error=f"download completed but profile registration failed: {exc}",
                )
                return
        await asyncio.to_thread(
            self.store.update,
            record.id,
            status="installed",
            pid=None,
            error=None,
        )

    def _storage_location(self, record: InstallRecord) -> StorageLocationConfig | None:
        if self.storage is None:
            return None
        location = next(
            (item for item in self.storage.locations if item.name == record.storage),
            None,
        )
        if location is None:
            raise RuntimeError(f"unknown storage location '{record.storage}'")
        return location

    async def _downloaded_size(
        self,
        record: InstallRecord,
        location: StorageLocationConfig | None,
    ) -> int:
        if location is not None and self.filesystem is not None:
            return await self.filesystem.directory_size(
                root=location.path,
                path=record.destination,
                scope_id=location.scope_id,
            )
        return await asyncio.to_thread(_directory_size, Path(record.destination))


async def _terminate(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
        return
    except asyncio.TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await process.wait()


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _directories, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(root) / filename).stat().st_size
            except OSError:
                continue
    return total


def _lexical_path(value: str | Path) -> str:
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.expanduser(str(value))))
    )


__all__ = ["NativeInstaller"]
