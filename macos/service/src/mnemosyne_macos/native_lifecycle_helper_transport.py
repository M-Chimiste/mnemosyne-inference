"""Bounded service-to-helper transport for native lifecycle authorization.

The bundled lifecycle helper authenticates its connected peer as the exact
sealed service Python.  Consequently the menu process must never create the
helper socketpair itself.  This module is the narrow service-owned transport:
it launches only the bootstrap-pinned helper, sends one closed challenge over
an unnamed socketpair, and returns one bounded receipt document.

Transport success is *not* authorization.  The lifecycle journal still
requires its separately provisioned OS-backed proof authority to validate the
receipt.  The normal production construction currently has no such authority,
so this transport cannot make authorization or lifecycle effects available.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import time
from typing import Final, Protocol

from .native_lifecycle import (
    HelperAuthorizationChallenge,
    NativeLifecycleError,
)


_MAXIMUM_JSON_BYTES: Final[int] = 16 * 1024
_MAXIMUM_HELPER_SECONDS: Final[float] = 122.0
_HELPER_NAME: Final[str] = "mnemosyne-lifecycle-helper"
_HELPER_WRAPPER_NAME: Final[str] = "MnemosyneLifecycleAuthorization.app"
_OUTER_APP_NAME: Final[str] = "Unified Inference.app"


class NativeLifecycleHelperTransportError(NativeLifecycleError):
    """Fixed-code transport failure safe for the loopback control plane."""


class LifecycleHelperTransport(Protocol):
    async def authorize(
        self, challenge: HelperAuthorizationChallenge
    ) -> Mapping[str, object]: ...


class BundledLifecycleHelperTransport:
    """Run one bundled helper session with the service as its direct peer."""

    def __init__(
        self,
        executable: str | Path,
        *,
        timeout_seconds: float = _MAXIMUM_HELPER_SECONDS,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= float(timeout_seconds) <= _MAXIMUM_HELPER_SECONDS
        ):
            raise ValueError("invalid lifecycle helper timeout")
        self.executable = Path(executable)
        self.timeout_seconds = float(timeout_seconds)

    async def authorize(
        self, challenge: HelperAuthorizationChallenge
    ) -> Mapping[str, object]:
        if not isinstance(challenge, HelperAuthorizationChallenge):
            raise TypeError("challenge must be a HelperAuthorizationChallenge")
        executable = _validated_helper_path(self.executable)
        remaining = min(
            self.timeout_seconds,
            float(challenge.expires_at) - time.time() + 1.0,
        )
        if remaining <= 0:
            raise NativeLifecycleHelperTransportError(
                "native_lifecycle_helper_authority_expired"
            )

        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        parent.setblocking(False)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable),
                "--session-fd",
                str(child.fileno()),
                pass_fds=(child.fileno(),),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                start_new_session=True,
            )
            child.close()
            payload = challenge.canonical_bytes()
            if not payload or len(payload) > _MAXIMUM_JSON_BYTES:
                raise NativeLifecycleHelperTransportError(
                    "native_lifecycle_helper_authority_invalid"
                )
            frame = struct.pack(">I", len(payload)) + payload
            response = await asyncio.wait_for(
                _exchange_one_frame(parent, process, frame),
                timeout=remaining,
            )
        except asyncio.CancelledError:
            if process is not None:
                await asyncio.shield(_terminate(process))
            raise
        except TimeoutError as exc:
            if process is not None:
                await _terminate(process)
            raise NativeLifecycleHelperTransportError(
                "native_lifecycle_helper_authority_expired"
            ) from exc
        except NativeLifecycleError:
            if process is not None:
                await _terminate(process)
            raise
        except (OSError, ValueError) as exc:
            if process is not None:
                await _terminate(process)
            raise NativeLifecycleHelperTransportError(
                "native_lifecycle_helper_authority_unavailable"
            ) from exc
        finally:
            parent.close()
            child.close()

        try:
            document = json.loads(
                response.decode("utf-8"),
                object_pairs_hook=_closed_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError()
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise NativeLifecycleHelperTransportError(
                "native_lifecycle_helper_authority_invalid"
            ) from None
        if not isinstance(document, dict):
            raise NativeLifecycleHelperTransportError(
                "native_lifecycle_helper_authority_invalid"
            )
        return document


async def _exchange_one_frame(
    peer: socket.socket,
    process: asyncio.subprocess.Process,
    request: bytes,
) -> bytes:
    loop = asyncio.get_running_loop()
    await loop.sock_sendall(peer, request)
    peer.shutdown(socket.SHUT_WR)
    header = await _read_exact(peer, 4)
    size = struct.unpack(">I", header)[0]
    if size == 0 or size > _MAXIMUM_JSON_BYTES:
        raise NativeLifecycleHelperTransportError(
            "native_lifecycle_helper_authority_invalid"
        )
    payload = await _read_exact(peer, size)
    # Wait for EOF so a helper cannot smuggle a second response frame after a
    # valid-looking first receipt.
    if await loop.sock_recv(peer, 1):
        raise NativeLifecycleHelperTransportError(
            "native_lifecycle_helper_authority_conflict"
        )
    return_code = await process.wait()
    if return_code != 0:
        raise NativeLifecycleHelperTransportError(
            "native_lifecycle_helper_authority_unavailable"
        )
    return payload


async def _read_exact(peer: socket.socket, size: int) -> bytes:
    loop = asyncio.get_running_loop()
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = await loop.sock_recv(peer, remaining)
        if not chunk:
            raise NativeLifecycleHelperTransportError(
                "native_lifecycle_helper_authority_unavailable"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await process.wait()


def _validated_helper_path(value: Path) -> Path:
    if not value.is_absolute() or "\0" in str(value):
        raise NativeLifecycleHelperTransportError(
            "native_lifecycle_helper_authority_unavailable"
        )
    path = Path(os.path.abspath(value))
    try:
        metadata = path.lstat()
        macos_directory = path.parent
        helper_contents = macos_directory.parent
        helper_wrapper = helper_contents.parent
        helpers_directory = helper_wrapper.parent
        outer_contents = helpers_directory.parent
        outer_app = outer_contents.parent
        ancestors = (
            macos_directory,
            helper_contents,
            helper_wrapper,
            helpers_directory,
            outer_contents,
            outer_app,
        )
        ancestor_metadata = tuple(item.lstat() for item in ancestors)
    except OSError:
        raise NativeLifecycleHelperTransportError(
            "native_lifecycle_helper_authority_unavailable"
        ) from None
    if (
        path.name != _HELPER_NAME
        or macos_directory.name != "MacOS"
        or helper_contents.name != "Contents"
        or helper_wrapper.name != _HELPER_WRAPPER_NAME
        or helpers_directory.name != "Helpers"
        or outer_contents.name != "Contents"
        or outer_app.name != _OUTER_APP_NAME
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not os.access(path, os.X_OK)
        or any(
            stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode)
            for item in ancestor_metadata
        )
    ):
        raise NativeLifecycleHelperTransportError(
            "native_lifecycle_helper_authority_unavailable"
        )
    return path


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


__all__ = [
    "BundledLifecycleHelperTransport",
    "LifecycleHelperTransport",
    "NativeLifecycleHelperTransportError",
]
