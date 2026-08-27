"""Persistent macOS file bookmarks shared by the menu app and LaunchAgent.

The menu app receives an ephemeral user grant from ``NSOpenPanel``. Foundation
embeds that grant when it creates ordinary bookmark data from the selected URL,
and a receiving launch agent can resolve the bookmark to extend its own access.
Only an opaque digest is stored in YAML; bookmark bytes stay in the private
state directory with mode 0600.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from typing import Protocol


_SCOPE_ID_RE = re.compile(r"[0-9a-f]{64}")
_MAX_BOOKMARK_BYTES = 1024 * 1024
_MAX_PATH_BYTES = 32 * 1024


class SecurityScopeError(RuntimeError):
    pass


class ScopeHandle(Protocol):
    path: str
    stale: bool

    def close(self) -> None: ...


class BookmarkResolver(Protocol):
    def receive(self, bookmark: bytes) -> tuple[ScopeHandle, bytes]: ...

    def activate(self, bookmark: bytes) -> ScopeHandle: ...


def _normalized_path(value: str) -> str:
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.expanduser(value)))
    )


def _paths_equivalent(left: str, right: str) -> bool:
    normalized_left = _normalized_path(left)
    normalized_right = _normalized_path(right)
    if normalized_left == normalized_right:
        return True
    # Finder bookmarks commonly resolve a selected symlink to its target.
    # Scope receipt/reactivation runs in a bounded helper process, so resolving
    # both sides here cannot wedge either HTTP plane.
    return _normalized_path(os.path.realpath(normalized_left)) == _normalized_path(
        os.path.realpath(normalized_right)
    )


class _CoreFoundationScope:
    def __init__(
        self,
        *,
        core_foundation: ctypes.CDLL,
        url_ref: int,
        path: str,
        stale: bool,
        access_started: bool,
    ) -> None:
        self._core_foundation = core_foundation
        self._url_ref = url_ref
        self.path = path
        self.stale = stale
        self._access_started = access_started
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._access_started:
            self._core_foundation.CFURLStopAccessingSecurityScopedResource(
                self._url_ref
            )
        self._core_foundation.CFRelease(self._url_ref)


class DarwinBookmarkResolver:
    """Receive and reactivate Foundation bookmarks without PyObjC.

    The bookmark sent by the menu app carries an implicit, single-transfer
    grant.  Once that grant has been explicitly started in this process, it is
    converted to a receiver-owned security-scoped bookmark suitable for later
    LaunchAgent restarts.
    """

    _WITHOUT_UI = 1 << 8
    _WITHOUT_MOUNTING = 1 << 9
    _WITH_SECURITY_SCOPE = 1 << 10
    _CREATE_WITH_SECURITY_SCOPE = 1 << 11
    _WITHOUT_IMPLICIT_START = 1 << 15

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise SecurityScopeError("macOS file bookmarks are only available on macOS")
        self._cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        byte_pointer = ctypes.POINTER(ctypes.c_ubyte)
        self._cf.CFDataCreate.argtypes = [ctypes.c_void_p, byte_pointer, ctypes.c_long]
        self._cf.CFDataCreate.restype = ctypes.c_void_p
        self._cf.CFURLCreateByResolvingBookmarkData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._cf.CFURLCreateByResolvingBookmarkData.restype = ctypes.c_void_p
        self._cf.CFURLCreateBookmarkData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._cf.CFURLCreateBookmarkData.restype = ctypes.c_void_p
        self._cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFDataGetLength.restype = ctypes.c_long
        self._cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        self._cf.CFDataGetBytePtr.restype = byte_pointer
        self._cf.CFURLGetFileSystemRepresentation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            byte_pointer,
            ctypes.c_long,
        ]
        self._cf.CFURLGetFileSystemRepresentation.restype = ctypes.c_ubyte
        self._cf.CFURLStartAccessingSecurityScopedResource.argtypes = [ctypes.c_void_p]
        self._cf.CFURLStartAccessingSecurityScopedResource.restype = ctypes.c_ubyte
        self._cf.CFURLStopAccessingSecurityScopedResource.argtypes = [ctypes.c_void_p]
        self._cf.CFURLStopAccessingSecurityScopedResource.restype = None
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None

    def receive(self, bookmark: bytes) -> tuple[ScopeHandle, bytes]:
        handle = self._resolve(
            bookmark,
            options=(
                self._WITHOUT_UI
                | self._WITHOUT_MOUNTING
                | self._WITHOUT_IMPLICIT_START
            ),
        )
        try:
            owned_bookmark = self._create_persistent_bookmark(handle)
        except BaseException:
            handle.close()
            raise
        return handle, owned_bookmark

    def activate(self, bookmark: bytes) -> ScopeHandle:
        return self._resolve(
            bookmark,
            options=(
                self._WITH_SECURITY_SCOPE
                | self._WITHOUT_UI
                | self._WITHOUT_MOUNTING
                | self._WITHOUT_IMPLICIT_START
            ),
        )

    def _resolve(self, bookmark: bytes, *, options: int) -> _CoreFoundationScope:
        if not bookmark:
            raise SecurityScopeError("file bookmark is empty")
        raw = (ctypes.c_ubyte * len(bookmark)).from_buffer_copy(bookmark)
        data_ref = self._cf.CFDataCreate(None, raw, len(bookmark))
        if not data_ref:
            raise SecurityScopeError("could not allocate file bookmark data")
        url_ref = 0
        error_ref = ctypes.c_void_p()
        stale = ctypes.c_ubyte(0)
        try:
            url_ref = self._cf.CFURLCreateByResolvingBookmarkData(
                None,
                data_ref,
                options,
                None,
                None,
                ctypes.byref(stale),
                ctypes.byref(error_ref),
            )
        finally:
            self._cf.CFRelease(data_ref)
        if error_ref.value:
            self._cf.CFRelease(error_ref.value)
        if not url_ref:
            raise SecurityScopeError(
                "macOS could not resolve the selected-folder bookmark; choose it again"
            )

        access_started = bool(
            self._cf.CFURLStartAccessingSecurityScopedResource(url_ref)
        )
        if not access_started:
            self._cf.CFRelease(url_ref)
            raise SecurityScopeError(
                "macOS did not grant access to the selected folder; "
                "choose it again in Unified Inference"
            )

        buffer = (ctypes.c_ubyte * _MAX_PATH_BYTES)()
        represented = self._cf.CFURLGetFileSystemRepresentation(
            url_ref,
            1,
            buffer,
            len(buffer),
        )
        if not represented:
            self._cf.CFURLStopAccessingSecurityScopedResource(url_ref)
            self._cf.CFRelease(url_ref)
            raise SecurityScopeError("selected-folder bookmark did not contain a file path")
        raw_path = bytes(buffer).split(b"\0", 1)[0]
        try:
            path = os.fsdecode(raw_path)
        except UnicodeError as exc:
            self._cf.CFURLStopAccessingSecurityScopedResource(url_ref)
            self._cf.CFRelease(url_ref)
            raise SecurityScopeError("selected-folder bookmark path was invalid") from exc

        return _CoreFoundationScope(
            core_foundation=self._cf,
            url_ref=url_ref,
            path=path,
            stale=bool(stale.value),
            access_started=access_started,
        )

    def _create_persistent_bookmark(
        self,
        handle: _CoreFoundationScope,
    ) -> bytes:
        error_ref = ctypes.c_void_p()
        data_ref = self._cf.CFURLCreateBookmarkData(
            None,
            handle._url_ref,
            self._CREATE_WITH_SECURITY_SCOPE,
            None,
            None,
            ctypes.byref(error_ref),
        )
        if error_ref.value:
            self._cf.CFRelease(error_ref.value)
        if not data_ref:
            raise SecurityScopeError(
                "macOS could not retain permission for the selected folder; "
                "choose it again"
            )
        try:
            length = self._cf.CFDataGetLength(data_ref)
            if length <= 0 or length > _MAX_BOOKMARK_BYTES:
                raise SecurityScopeError(
                    "macOS returned an invalid persistent selected-folder bookmark"
                )
            byte_pointer = self._cf.CFDataGetBytePtr(data_ref)
            if not byte_pointer:
                raise SecurityScopeError(
                    "macOS returned an invalid persistent selected-folder bookmark"
                )
            return ctypes.string_at(byte_pointer, length)
        finally:
            self._cf.CFRelease(data_ref)


@dataclass(frozen=True, slots=True)
class RegisteredScope:
    id: str
    path: str


class SecurityScopeRegistry:
    """Persist, resolve, and retain user-selected folder bookmarks."""

    def __init__(
        self,
        root: str | Path,
        *,
        resolver: BookmarkResolver | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self._resolver = resolver
        self._active: dict[str, ScopeHandle] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _effective_resolver(self) -> BookmarkResolver:
        with self._lock:
            self._ensure_open()
            if self._resolver is None:
                self._resolver = DarwinBookmarkResolver()
            return self._resolver

    def _ensure_open(self) -> None:
        if self._closed:
            raise SecurityScopeError("selected-folder permission registry is closed")

    @staticmethod
    def decode(value: str) -> bytes:
        if not value or len(value) > (_MAX_BOOKMARK_BYTES * 2):
            raise SecurityScopeError("selected-folder bookmark is missing or too large")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SecurityScopeError("selected-folder bookmark is not valid base64") from exc
        if not decoded or len(decoded) > _MAX_BOOKMARK_BYTES:
            raise SecurityScopeError("selected-folder bookmark is empty or too large")
        return decoded

    @staticmethod
    def validate_id(scope_id: str) -> str:
        normalized = scope_id.casefold()
        if not _SCOPE_ID_RE.fullmatch(normalized):
            raise SecurityScopeError("invalid selected-folder scope id")
        return normalized

    def register(self, path: str, encoded_bookmark: str) -> RegisteredScope:
        transfer_bookmark = self.decode(encoded_bookmark)
        resolver = self._effective_resolver()
        transfer_handle, bookmark = resolver.receive(transfer_bookmark)
        retained_handle: ScopeHandle | None = None
        try:
            if not bookmark or len(bookmark) > _MAX_BOOKMARK_BYTES:
                raise SecurityScopeError(
                    "macOS returned an invalid persistent selected-folder bookmark"
                )
            if transfer_handle.stale:
                raise SecurityScopeError(
                    "selected-folder bookmark is stale; choose the folder again"
                )
            expected = _normalized_path(path)
            actual = _normalized_path(transfer_handle.path)
            if not _paths_equivalent(actual, expected):
                raise SecurityScopeError(
                    "selected-folder bookmark resolved to a different path"
                )
            scope_id = hashlib.sha256(bookmark).hexdigest()
            self._persist(scope_id, bookmark)

            # The ordinary bookmark's implicit extension is a one-process
            # transfer grant.  Do not retain that handle as evidence that the
            # receiver-owned bookmark will survive a restart: explicitly
            # reactivate the persisted bytes now and retain only that handle.
            retained_handle = resolver.activate(bookmark)
            if retained_handle.stale:
                raise SecurityScopeError(
                    "persistent selected-folder bookmark is stale; choose the folder again"
                )
            retained_path = _normalized_path(retained_handle.path)
            if not _paths_equivalent(retained_path, expected):
                raise SecurityScopeError(
                    "persistent selected-folder bookmark resolved to a different path"
                )

            with self._lock:
                self._ensure_open()
                existing = self._active.get(scope_id)
                if existing is None:
                    self._active[scope_id] = retained_handle
                    retained_handle = None
                else:
                    retained_path = _normalized_path(existing.path)
                    if not _paths_equivalent(retained_path, expected):
                        raise SecurityScopeError(
                            "selected-folder scope is registered for a different path"
                        )
            return RegisteredScope(id=scope_id, path=expected)
        finally:
            transfer_handle.close()
            if retained_handle is not None:
                retained_handle.close()

    def activate(self, scope_id: str, path: str) -> RegisteredScope:
        normalized_id = self.validate_id(scope_id)
        expected_path = _normalized_path(path)
        # Even an already-active extension is useful only for this process.
        # Re-read and authenticate its persisted bookmark so configuration
        # preflight cannot bless a grant that would disappear on restart.
        bookmark_path = self.root / f"{normalized_id}.bookmark"
        try:
            bookmark = bookmark_path.read_bytes()
            os.chmod(bookmark_path, 0o600)
        except OSError as exc:
            raise SecurityScopeError(
                "selected-folder permission is missing; choose the folder again"
            ) from exc
        if not bookmark or len(bookmark) > _MAX_BOOKMARK_BYTES:
            raise SecurityScopeError("stored selected-folder bookmark is invalid")
        if hashlib.sha256(bookmark).hexdigest() != normalized_id:
            raise SecurityScopeError("stored selected-folder bookmark failed validation")
        with self._lock:
            self._ensure_open()
            existing = self._active.get(normalized_id)
            if existing is not None:
                active_path = _normalized_path(existing.path)
                if not _paths_equivalent(active_path, expected_path):
                    raise SecurityScopeError(
                        "selected-folder scope is registered for a different path"
                    )
                return RegisteredScope(id=normalized_id, path=expected_path)
        resolver = self._effective_resolver()
        handle = resolver.activate(bookmark)
        try:
            if handle.stale:
                raise SecurityScopeError(
                    "selected-folder bookmark is stale; choose the folder again"
                )
            actual_path = _normalized_path(handle.path)
            if not _paths_equivalent(actual_path, expected_path):
                raise SecurityScopeError(
                    "selected-folder bookmark resolved to a different path"
                )
            with self._lock:
                self._ensure_open()
                existing = self._active.get(normalized_id)
                if existing is None:
                    self._active[normalized_id] = handle
                    handle = None  # type: ignore[assignment]
                    retained_path = actual_path
                else:
                    retained_path = _normalized_path(existing.path)
                    if not _paths_equivalent(retained_path, expected_path):
                        raise SecurityScopeError(
                            "selected-folder scope is registered for a different path"
                        )
            return RegisteredScope(id=normalized_id, path=expected_path)
        finally:
            if handle is not None:
                handle.close()

    def require(self, scope_id: str | None, path: str) -> None:
        if scope_id is not None:
            self.activate(scope_id, path)

    def prune(self, referenced_ids: set[str]) -> None:
        """Release and remove grants no longer referenced by persisted config."""

        normalized = {self.validate_id(value) for value in referenced_ids}
        with self._lock:
            self._ensure_open()
            removed_handles = [
                handle
                for scope_id, handle in self._active.items()
                if scope_id not in normalized
            ]
            self._active = {
                scope_id: handle
                for scope_id, handle in self._active.items()
                if scope_id in normalized
            }
        for handle in removed_handles:
            handle.close()
        if not self.root.is_dir():
            return
        for bookmark_path in self.root.glob("*.bookmark"):
            scope_id = bookmark_path.stem.casefold()
            if _SCOPE_ID_RE.fullmatch(scope_id) and scope_id not in normalized:
                with contextlib.suppress(OSError):
                    bookmark_path.unlink()

    def discard(self, scope_id: str) -> None:
        """Remove one failed, not-yet-configured grant without broad pruning."""

        normalized_id = self.validate_id(scope_id)
        with self._lock:
            self._ensure_open()
            handle = self._active.pop(normalized_id, None)
        if handle is not None:
            handle.close()
        bookmark_path = self.root / f"{normalized_id}.bookmark"
        with contextlib.suppress(OSError):
            bookmark_path.unlink()

    def _persist(self, scope_id: str, bookmark: bytes) -> None:
        with self._lock:
            self._ensure_open()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        destination = self.root / f"{scope_id}.bookmark"
        if destination.exists():
            try:
                if destination.read_bytes() == bookmark:
                    os.chmod(destination, 0o600)
                    return
            except OSError:
                pass
        file_descriptor, temporary = tempfile.mkstemp(
            prefix=f".{scope_id}.", suffix=".tmp", dir=self.root
        )
        temporary_path = Path(temporary)
        try:
            os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "wb") as stream:
                file_descriptor = -1
                stream.write(bookmark)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, destination)
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            with contextlib.suppress(OSError):
                temporary_path.unlink()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = list(self._active.values())
            self._active.clear()
        for handle in reversed(active):
            handle.close()

__all__ = [
    "BookmarkResolver",
    "DarwinBookmarkResolver",
    "RegisteredScope",
    "ScopeHandle",
    "SecurityScopeError",
    "SecurityScopeRegistry",
]
