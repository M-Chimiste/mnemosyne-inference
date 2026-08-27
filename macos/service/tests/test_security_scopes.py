from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mnemosyne_macos.security_scopes import (
    DarwinBookmarkResolver,
    SecurityScopeError,
    SecurityScopeRegistry,
)


@dataclass
class _Handle:
    path: str
    stale: bool = False
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class _Resolver:
    def __init__(
        self,
        path: Path,
        *,
        stale: bool = False,
        owned_bookmark: bytes = b"receiver-owned bookmark fixture",
        receive_barrier: threading.Barrier | None = None,
        activate_barrier: threading.Barrier | None = None,
    ) -> None:
        self.path = path
        self.stale = stale
        self.owned_bookmark = owned_bookmark
        self.receive_barrier = receive_barrier
        self.activate_barrier = activate_barrier
        self.handles: list[_Handle] = []
        self.received: list[bytes] = []
        self.activated: list[bytes] = []
        self._lock = threading.Lock()

    def _handle(self) -> _Handle:
        handle = _Handle(str(self.path), stale=self.stale)
        with self._lock:
            self.handles.append(handle)
        return handle

    def receive(self, bookmark: bytes) -> tuple[_Handle, bytes]:
        with self._lock:
            self.received.append(bookmark)
        handle = self._handle()
        if self.receive_barrier is not None:
            self.receive_barrier.wait(timeout=5)
        return handle, self.owned_bookmark

    def activate(self, bookmark: bytes) -> _Handle:
        with self._lock:
            self.activated.append(bookmark)
        handle = self._handle()
        if self.activate_barrier is not None:
            self.activate_barrier.wait(timeout=5)
        return handle


def _encoded(value: bytes = b"finder bookmark fixture") -> str:
    return base64.b64encode(value).decode("ascii")


def test_scope_registry_persists_private_bookmark_and_reactivates(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "Volumes" / "Athena" / "models"
    selected.mkdir(parents=True)
    first_resolver = _Resolver(selected)
    registry = SecurityScopeRegistry(
        tmp_path / "state" / "security-scopes",
        resolver=first_resolver,
    )

    registered = registry.register(str(selected), _encoded())
    expected_persistent = b"receiver-owned bookmark fixture"
    expected_id = hashlib.sha256(expected_persistent).hexdigest()
    assert registered.id == expected_id
    bookmark_path = registry.root / f"{expected_id}.bookmark"
    assert bookmark_path.read_bytes() == expected_persistent
    assert first_resolver.received == [b"finder bookmark fixture"]
    assert first_resolver.activated == [expected_persistent]
    assert stat.S_IMODE(registry.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(bookmark_path.stat().st_mode) == 0o600
    assert len(first_resolver.handles) == 2
    assert first_resolver.handles[0].closed is True
    assert first_resolver.handles[1].closed is False

    registry.close()
    assert all(handle.closed for handle in first_resolver.handles)

    second_resolver = _Resolver(selected)
    restored = SecurityScopeRegistry(registry.root, resolver=second_resolver)
    restored.activate(expected_id, str(selected))
    assert second_resolver.activated == [expected_persistent]
    assert second_resolver.handles[0].closed is False
    restored.close()
    assert second_resolver.handles[0].closed is True


def test_scope_registry_accepts_bookmark_target_for_selected_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Volumes" / "Athena" / "nested" / "models"
    target.mkdir(parents=True)
    selected = tmp_path / "home" / ".lmstudio" / "models"
    selected.parent.mkdir(parents=True)
    selected.symlink_to(target, target_is_directory=True)
    persistent = b"receiver-owned symlink bookmark"
    registry = SecurityScopeRegistry(
        tmp_path / "state" / "security-scopes",
        resolver=_Resolver(target, owned_bookmark=persistent),
    )

    registered = registry.register(str(selected), _encoded())

    assert registered.path == str(selected)
    registry.close()
    restored = SecurityScopeRegistry(
        registry.root,
        resolver=_Resolver(target, owned_bookmark=persistent),
    )
    activated = restored.activate(registered.id, str(selected))
    assert activated.path == str(selected)
    restored.close()


def test_scope_registry_rejects_owned_bookmark_that_cannot_reactivate(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "models"
    selected.mkdir()

    class _RejectingActivationResolver(_Resolver):
        def activate(self, bookmark: bytes) -> _Handle:
            with self._lock:
                self.activated.append(bookmark)
            raise SecurityScopeError("receiver-owned bookmark could not reactivate")

    resolver = _RejectingActivationResolver(selected)
    registry = SecurityScopeRegistry(
        tmp_path / "state" / "security-scopes",
        resolver=resolver,
    )

    with pytest.raises(SecurityScopeError, match="could not reactivate"):
        registry.register(str(selected), _encoded())

    assert resolver.received == [b"finder bookmark fixture"]
    assert resolver.activated == [b"receiver-owned bookmark fixture"]
    assert len(resolver.handles) == 1
    assert resolver.handles[0].closed is True
    registry.close()
    assert resolver.handles[0].closed is True


def test_active_scope_still_requires_a_durable_authenticated_bookmark(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "models"
    selected.mkdir()
    registry = SecurityScopeRegistry(
        tmp_path / "state" / "security-scopes",
        resolver=_Resolver(selected),
    )
    registered = registry.register(str(selected), _encoded())
    bookmark_path = registry.root / f"{registered.id}.bookmark"

    bookmark_path.unlink()
    with pytest.raises(SecurityScopeError, match="permission is missing"):
        registry.activate(registered.id, str(selected))

    registry.close()


def test_scope_registry_discards_only_the_requested_failed_grant(
    tmp_path: Path,
) -> None:
    first_bookmark = b"first receiver-owned bookmark"
    second_bookmark = b"second receiver-owned bookmark"
    first = SecurityScopeRegistry(
        tmp_path / "state" / "security-scopes",
        resolver=_Resolver(tmp_path, owned_bookmark=first_bookmark),
    )
    first_scope = first.register(str(tmp_path), _encoded(b"first transfer"))
    first.close()
    second = SecurityScopeRegistry(
        first.root,
        resolver=_Resolver(tmp_path, owned_bookmark=second_bookmark),
    )
    second_scope = second.register(str(tmp_path), _encoded(b"second transfer"))

    second.discard(second_scope.id)

    assert not (second.root / f"{second_scope.id}.bookmark").exists()
    assert (second.root / f"{first_scope.id}.bookmark").exists()
    second.close()


def test_scope_registry_rejects_mismatch_stale_and_tampering(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "models"
    selected.mkdir()

    mismatch = SecurityScopeRegistry(
        tmp_path / "mismatch",
        resolver=_Resolver(tmp_path / "other"),
    )
    with pytest.raises(SecurityScopeError, match="different path"):
        mismatch.register(str(selected), _encoded())

    stale = SecurityScopeRegistry(
        tmp_path / "stale",
        resolver=_Resolver(selected, stale=True),
    )
    with pytest.raises(SecurityScopeError, match="stale"):
        stale.register(str(selected), _encoded())

    resolver = _Resolver(selected)
    registry = SecurityScopeRegistry(tmp_path / "tampered", resolver=resolver)
    scope = registry.register(str(selected), _encoded())
    registry.close()
    (registry.root / f"{scope.id}.bookmark").write_bytes(b"changed")
    restored = SecurityScopeRegistry(registry.root, resolver=_Resolver(selected))
    with pytest.raises(SecurityScopeError, match="failed validation"):
        restored.activate(scope.id, str(selected))


@pytest.mark.parametrize("value", ["", "not base64!", _encoded(b"x" * (1024 * 1024 + 1))])
def test_scope_registry_rejects_invalid_payloads(tmp_path: Path, value: str) -> None:
    registry = SecurityScopeRegistry(tmp_path, resolver=_Resolver(tmp_path))
    with pytest.raises(SecurityScopeError):
        registry.register(str(tmp_path), value)


def test_scope_registry_concurrent_activation_retains_one_handle(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "models"
    selected.mkdir()
    persistent = b"receiver-owned concurrent fixture"
    scope_id = hashlib.sha256(persistent).hexdigest()
    root = tmp_path / "state" / "security-scopes"
    root.mkdir(parents=True)
    bookmark_path = root / f"{scope_id}.bookmark"
    bookmark_path.write_bytes(persistent)
    barrier = threading.Barrier(2)
    resolver = _Resolver(selected, activate_barrier=barrier)
    registry = SecurityScopeRegistry(root, resolver=resolver)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(registry.activate, scope_id, str(selected))
            for _ in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]

    assert {result.id for result in results} == {scope_id}
    assert len(resolver.handles) == 2
    assert sum(handle.closed for handle in resolver.handles) == 1
    assert stat.S_IMODE(bookmark_path.stat().st_mode) == 0o600
    registry.close()
    assert all(handle.closed for handle in resolver.handles)


def test_scope_registry_closes_late_handle_when_shutdown_wins_race(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "models"
    selected.mkdir()
    persistent = b"receiver-owned shutdown fixture"
    scope_id = hashlib.sha256(persistent).hexdigest()
    root = tmp_path / "state" / "security-scopes"
    root.mkdir(parents=True)
    (root / f"{scope_id}.bookmark").write_bytes(persistent)

    entered = threading.Event()
    release = threading.Event()

    class _BlockingResolver(_Resolver):
        def activate(self, bookmark: bytes) -> _Handle:
            handle = super().activate(bookmark)
            entered.set()
            assert release.wait(timeout=5)
            return handle

    resolver = _BlockingResolver(selected)
    registry = SecurityScopeRegistry(root, resolver=resolver)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(registry.activate, scope_id, str(selected))
        assert entered.wait(timeout=5)
        registry.close()
        release.set()
        with pytest.raises(SecurityScopeError, match="registry is closed"):
            future.result(timeout=10)

    assert len(resolver.handles) == 1
    assert resolver.handles[0].closed is True


def test_scope_registry_prunes_only_unreferenced_grants(tmp_path: Path) -> None:
    selected = tmp_path / "models"
    selected.mkdir()
    resolver = _Resolver(selected)
    registry = SecurityScopeRegistry(
        tmp_path / "state" / "security-scopes",
        resolver=resolver,
    )
    registered = registry.register(str(selected), _encoded())
    retained_path = registry.root / f"{registered.id}.bookmark"
    orphan = b"orphan receiver bookmark"
    orphan_id = hashlib.sha256(orphan).hexdigest()
    orphan_path = registry.root / f"{orphan_id}.bookmark"
    orphan_path.write_bytes(orphan)

    registry.prune({registered.id})

    assert retained_path.exists()
    assert not orphan_path.exists()
    assert resolver.handles[0].closed is True
    assert resolver.handles[1].closed is False

    registry.prune(set())

    assert not retained_path.exists()
    assert all(handle.closed for handle in resolver.handles)
    registry.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreFoundation is macOS-only")
def test_darwin_registry_persists_and_reactivates_receiver_owned_bookmark(
    tmp_path: Path,
) -> None:
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

    path_bytes = os.fsencode(tmp_path)
    raw_path = (ctypes.c_ubyte * len(path_bytes)).from_buffer_copy(path_bytes)
    url_ref = core.CFURLCreateFromFileSystemRepresentation(
        None, raw_path, len(path_bytes), 1
    )
    assert url_ref
    error_ref = ctypes.c_void_p()
    data_ref = core.CFURLCreateBookmarkData(
        None, url_ref, 0, None, None, ctypes.byref(error_ref)
    )
    core.CFRelease(url_ref)
    if error_ref.value:
        core.CFRelease(error_ref.value)
    assert data_ref
    try:
        length = core.CFDataGetLength(data_ref)
        bookmark = ctypes.string_at(core.CFDataGetBytePtr(data_ref), length)
    finally:
        core.CFRelease(data_ref)

    registry = SecurityScopeRegistry(
        tmp_path / "state" / "security-scopes",
        resolver=DarwinBookmarkResolver(),
    )
    try:
        try:
            registered = registry.register(str(tmp_path), _encoded(bookmark))
        except SecurityScopeError as exc:
            pytest.skip(
                "the test runner URL has no transferable NSOpenPanel grant: "
                f"{exc}"
            )
        persistent_path = registry.root / f"{registered.id}.bookmark"
        persistent = persistent_path.read_bytes()
        assert persistent
        assert persistent != bookmark
        assert registered.id == hashlib.sha256(persistent).hexdigest()
        assert stat.S_IMODE(persistent_path.stat().st_mode) == 0o600
    finally:
        registry.close()

    restored = SecurityScopeRegistry(
        registry.root,
        resolver=DarwinBookmarkResolver(),
    )
    try:
        activated = restored.activate(registered.id, str(tmp_path))
        assert activated == registered
    finally:
        restored.close()
