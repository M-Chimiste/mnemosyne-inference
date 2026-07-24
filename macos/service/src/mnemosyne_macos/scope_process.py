"""Bounded client for macOS selected-folder bookmark operations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any

from .security_scopes import (
    RegisteredScope,
    SecurityScopeError,
    SecurityScopeRegistry,
)


class SecurityScopeProcess:
    """Receive and preflight bookmarks in killable subprocess groups.

    CoreFoundation bookmark calls can synchronously wait on macOS services.
    Running them in an executor thread would keep that thread alive after an
    HTTP cancellation or timeout.  This client gives every operation its own
    process group so the complete call can be terminated.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        timeout_seconds: float = 30.0,
        python_executable: str | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.timeout_seconds = timeout_seconds
        self.python_executable = python_executable or sys.executable

    async def register(self, path: str, encoded_bookmark: str) -> RegisteredScope:
        payload = await self._run(
            "register",
            "--path",
            path,
            input_payload={"bookmark_data": encoded_bookmark},
        )
        return _registered_scope(payload)

    async def activate(self, scope_id: str, path: str) -> RegisteredScope:
        payload = await self._run(
            "activate",
            "--scope-id",
            scope_id,
            "--path",
            path,
        )
        return _registered_scope(payload)

    async def _run(
        self,
        *arguments: str,
        input_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        argv = [
            self.python_executable,
            "-m",
            "mnemosyne_macos.scope_worker",
            "--scope-root",
            str(self.root),
            *arguments,
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=(
                asyncio.subprocess.PIPE
                if input_payload is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        input_bytes = (
            json.dumps(input_payload, separators=(",", ":")).encode("utf-8")
            if input_payload is not None
            else None
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input_bytes),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            await _terminate(process)
            raise SecurityScopeError(
                "selected-folder permission operation timed out; unlock the Mac, "
                "verify the volume is mounted, and choose the folder again"
            ) from exc
        except asyncio.CancelledError:
            await asyncio.shield(_terminate(process))
            raise

        if len(stdout) > 256 * 1024 or len(stderr) > 256 * 1024:
            raise SecurityScopeError(
                "selected-folder permission helper returned too much data"
            )
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
            raise SecurityScopeError(
                detail
                or (
                    "selected-folder permission helper exited with status "
                    f"{process.returncode}"
                )
            ) from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            message = payload.get("error") if isinstance(payload, dict) else None
            raise SecurityScopeError(
                str(
                    message
                    or (
                        "selected-folder permission helper exited with status "
                        f"{process.returncode}"
                    )
                )
            )
        if process.returncode != 0:
            raise SecurityScopeError(
                "selected-folder permission helper exited with status "
                f"{process.returncode}"
            )
        return payload


def _registered_scope(payload: dict[str, Any]) -> RegisteredScope:
    value = payload.get("scope")
    if not isinstance(value, dict):
        raise SecurityScopeError(
            "selected-folder permission helper returned an invalid result"
        )
    scope_id = value.get("id")
    path = value.get("path")
    if not isinstance(scope_id, str) or not isinstance(path, str):
        raise SecurityScopeError(
            "selected-folder permission helper returned an invalid result"
        )
    normalized_id = SecurityScopeRegistry.validate_id(scope_id)
    if not path:
        raise SecurityScopeError(
            "selected-folder permission helper returned an invalid path"
        )
    return RegisteredScope(id=normalized_id, path=path)


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


__all__ = ["SecurityScopeProcess"]
