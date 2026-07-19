"""Managed-process adapter for DwarfStar's model-specific HTTP server.

DS4 is launched in its own process group.  A small, atomically-written state
record lets a replacement Mnemosyne service identify a surviving child after
a restart.  Persisted ownership is never trusted by itself: PID, process
group, process start identity, executable, and complete argv must all match
the live process before the adapter adopts or signals it.
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Awaitable, Callable, Mapping, Protocol

import httpx

from .base import AdapterError, Deadline, EngineAdapter
from ..config import DS4Config
from ..models import (
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
    ServiceState,
    TargetKey,
)


class ProcessLike(Protocol):
    pid: int
    returncode: int | None
    stdout: asyncio.StreamReader | None

    async def wait(self) -> int: ...


SpawnProcess = Callable[..., Awaitable[ProcessLike]]


@dataclass(frozen=True)
class ProcessIdentity:
    """Kernel-observed identity used to make PID-reuse checks fail closed."""

    pid: int
    process_group_id: int
    start_identity: str
    executable: str
    argv: tuple[str, ...]


IdentityProbe = Callable[[int], Awaitable[ProcessIdentity | None]]
SignalProcessGroup = Callable[[int, int], None]


@dataclass(frozen=True)
class _OwnedProcessMetadata:
    schema_version: int
    pid: int
    process_group_id: int
    start_identity: str
    executable: str
    argv: tuple[str, ...]
    working_directory: str
    target_alias: str
    canonical_model_id: str
    load_config_digest: str
    wire_model: str
    capabilities: tuple[str, ...]
    load_options: Mapping[str, Any]

    @classmethod
    def for_spawn(
        cls,
        *,
        identity: ProcessIdentity,
        executable: Path,
        argv: list[str],
        working_directory: Path,
        target: ResolvedTarget,
    ) -> "_OwnedProcessMetadata":
        return cls(
            schema_version=1,
            pid=identity.pid,
            process_group_id=identity.process_group_id,
            start_identity=identity.start_identity,
            executable=_normalized_path(executable),
            argv=tuple(argv),
            working_directory=_normalized_path(working_directory),
            target_alias=target.alias,
            canonical_model_id=target.key.canonical_model_id,
            load_config_digest=target.key.load_config_digest,
            wire_model=target.wire_model,
            capabilities=tuple(sorted(endpoint.value for endpoint in target.capabilities)),
            load_options=dict(target.load_options),
        )

    @classmethod
    def from_dict(cls, value: object) -> "_OwnedProcessMetadata":
        if not isinstance(value, dict):
            raise ValueError("state record must be a JSON object")
        if value.get("schema_version") != 1:
            raise ValueError("unsupported state-record schema version")
        pid = value.get("pid")
        process_group_id = value.get("process_group_id")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 1
            or not isinstance(process_group_id, int)
            or isinstance(process_group_id, bool)
            or process_group_id <= 1
        ):
            raise ValueError("state record contains an invalid PID or process group")

        required_strings = (
            "start_identity",
            "executable",
            "working_directory",
            "target_alias",
            "canonical_model_id",
            "load_config_digest",
            "wire_model",
        )
        strings: dict[str, str] = {}
        for key in required_strings:
            candidate = value.get(key)
            if not isinstance(candidate, str) or not candidate:
                raise ValueError(f"state record contains invalid {key}")
            strings[key] = candidate

        argv_raw = value.get("argv")
        if (
            not isinstance(argv_raw, list)
            or not argv_raw
            or not all(isinstance(arg, str) for arg in argv_raw)
        ):
            raise ValueError("state record contains invalid argv")
        capabilities_raw = value.get("capabilities")
        if not isinstance(capabilities_raw, list) or not all(
            isinstance(item, str) for item in capabilities_raw
        ):
            raise ValueError("state record contains invalid capabilities")
        # Validate endpoint names while parsing rather than when routing.
        for item in capabilities_raw:
            Endpoint(item)
        load_options = value.get("load_options")
        if not isinstance(load_options, dict):
            raise ValueError("state record contains invalid load_options")

        return cls(
            schema_version=1,
            pid=pid,
            process_group_id=process_group_id,
            start_identity=strings["start_identity"],
            executable=strings["executable"],
            argv=tuple(argv_raw),
            working_directory=strings["working_directory"],
            target_alias=strings["target_alias"],
            canonical_model_id=strings["canonical_model_id"],
            load_config_digest=strings["load_config_digest"],
            wire_model=strings["wire_model"],
            capabilities=tuple(capabilities_raw),
            load_options=dict(load_options),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "start_identity": self.start_identity,
            "executable": self.executable,
            "argv": list(self.argv),
            "working_directory": self.working_directory,
            "target_alias": self.target_alias,
            "canonical_model_id": self.canonical_model_id,
            "load_config_digest": self.load_config_digest,
            "wire_model": self.wire_model,
            "capabilities": list(self.capabilities),
            "load_options": dict(self.load_options),
        }

    def target(self) -> ResolvedTarget:
        return ResolvedTarget(
            alias=self.target_alias,
            key=TargetKey(
                engine=EngineName.DS4,
                canonical_model_id=self.canonical_model_id,
                load_config_digest=self.load_config_digest,
            ),
            wire_model=self.wire_model,
            capabilities=frozenset(Endpoint(item) for item in self.capabilities),
            load_options=dict(self.load_options),
        )


_RESERVED_EXTRA_ARGS = {
    "-m",
    "--model",
    "--host",
    "--port",
    "--chdir",
    "--ctx",
    "--kv-disk-dir",
    "--kv-disk-space-mb",
}


def _normalized_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def build_ds4_argv(config: DS4Config, target: ResolvedTarget) -> list[str]:
    options = target.load_options
    extra_args = list(options.get("extra_args", []))
    for arg in extra_args:
        name = str(arg).split("=", 1)[0]
        if name in _RESERVED_EXTRA_ARGS:
            raise AdapterError(
                EngineName.DS4,
                "build_argv",
                f"extra_args may not override managed option {name}",
            )
    argv = [
        str(Path(config.binary).expanduser()),
        "-m",
        target.key.canonical_model_id,
        "--host",
        config.host,
        "--port",
        str(config.port),
    ]
    if options.get("context_length") is not None:
        argv.extend(["--ctx", str(options["context_length"])])
    if options.get("kv_disk_directory") is not None:
        argv.extend(
            ["--kv-disk-dir", str(Path(str(options["kv_disk_directory"])).expanduser())]
        )
    if options.get("kv_disk_space_mb") is not None:
        argv.extend(["--kv-disk-space-mb", str(options["kv_disk_space_mb"])])
    argv.extend(str(arg) for arg in extra_args)
    return argv


def _parse_kern_procargs(data: bytes) -> tuple[str, tuple[str, ...]] | None:
    """Parse macOS KERN_PROCARGS2's argc/executable/NUL-delimited argv layout."""

    if len(data) < struct.calcsize("i"):
        return None
    argc = struct.unpack_from("i", data)[0]
    if argc <= 0 or argc > 100_000:
        return None
    cursor = struct.calcsize("i")
    try:
        executable_end = data.index(b"\0", cursor)
    except ValueError:
        return None
    executable = os.fsdecode(data[cursor:executable_end])
    if not executable:
        return None
    cursor = executable_end
    while cursor < len(data) and data[cursor] == 0:
        cursor += 1

    argv: list[str] = []
    try:
        for _ in range(argc):
            argument_end = data.index(b"\0", cursor)
            argv.append(os.fsdecode(data[cursor:argument_end]))
            cursor = argument_end + 1
    except ValueError:
        return None
    return executable, tuple(argv)


def _macos_process_args(pid: int) -> tuple[str, tuple[str, ...]] | None:
    if sys.platform != "darwin":
        return None
    # Darwin sys/sysctl.h: CTL_KERN=1, KERN_PROCARGS2=49.
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)
    size = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        return None
    if size.value <= 0 or size.value > 16 * 1024 * 1024:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    return _parse_kern_procargs(buffer.raw[: size.value])


def _probe_process_identity_sync(pid: int) -> ProcessIdentity | None:
    """Best-effort macOS process identity probe using `/bin/ps`.

    The result is deliberately strict.  If the full argv cannot be parsed,
    recovery refuses ownership instead of weakening the comparison.
    """

    def field(name: str) -> str | None:
        try:
            result = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-ww", "-o", f"{name}="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    start_identity = field("lstart")
    pgid_text = field("pgid")
    process_args = _macos_process_args(pid)
    if process_args is not None:
        executable, argv = process_args
    else:
        executable = field("comm")
        command = field("command")
        if executable is None or command is None:
            return None
        try:
            argv = tuple(shlex.split(command))
        except ValueError:
            return None
    if start_identity is None or pgid_text is None:
        return None
    try:
        process_group_id = int(pgid_text)
    except ValueError:
        return None
    if not argv:
        return None
    return ProcessIdentity(
        pid=pid,
        process_group_id=process_group_id,
        start_identity=start_identity,
        executable=_normalized_path(executable),
        argv=argv,
    )


class DS4Adapter(EngineAdapter):
    engine = EngineName.DS4
    ownership = "managed_process"

    def __init__(
        self,
        config: DS4Config,
        *,
        client: httpx.AsyncClient | None = None,
        spawn_process: SpawnProcess | None = None,
        identity_probe: IdentityProbe | None = None,
        signal_process_group: SignalProcessGroup | None = None,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self.config = config
        self.base_url = f"http://{config.host}:{config.port}"
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._spawn_process = spawn_process or self._default_spawn
        self._identity_probe = identity_probe or self._default_identity_probe
        self._signal_process_group = signal_process_group or os.killpg
        self._poll_interval_seconds = poll_interval_seconds
        self._state_path = Path(config.process_state_path).expanduser()
        self._metadata_loaded = False
        self._owned_metadata: _OwnedProcessMetadata | None = None
        self._ownership_diagnostic: str | None = None
        self._process: ProcessLike | None = None
        self._target: ResolvedTarget | None = None
        self._log_task: asyncio.Task[None] | None = None
        self._log_tail: deque[str] = deque(maxlen=80)

    async def _default_spawn(self, *argv: str, **kwargs) -> ProcessLike:
        return await asyncio.create_subprocess_exec(*argv, **kwargs)

    async def _default_identity_probe(self, pid: int) -> ProcessIdentity | None:
        return await asyncio.to_thread(_probe_process_identity_sync, pid)

    async def _capture_logs(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            self._log_tail.append(line.decode("utf-8", errors="replace").rstrip())

    def _port_accepting_connections(self) -> bool:
        try:
            with socket.create_connection(
                (self.config.host, self.config.port), timeout=0.2
            ):
                return True
        except OSError:
            return False

    async def _models_payload(self, deadline: Deadline) -> dict | None:
        remaining = min(deadline.remaining(), self.config.request_timeout_seconds)
        if remaining <= 0:
            return None
        try:
            response = await self._client.get(
                f"{self.base_url}/v1/models", timeout=remaining
            )
            if response.status_code != 200:
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    def _persist_metadata(self, metadata: _OwnedProcessMetadata) -> None:
        parent = self._state_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self._state_path.name}.", suffix=".tmp", dir=parent
        )
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                json.dump(metadata.to_dict(), stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._state_path)
            with contextlib.suppress(OSError):
                directory_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()

    def _remove_metadata(self, expected: _OwnedProcessMetadata) -> None:
        """Remove only the exact record we observed, never a newer owner's file."""

        try:
            current = _OwnedProcessMetadata.from_dict(
                json.loads(self._state_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if (
            current.pid == expected.pid
            and current.start_identity == expected.start_identity
            and current.argv == expected.argv
        ):
            with contextlib.suppress(FileNotFoundError):
                self._state_path.unlink()

    def _metadata_matches_config(self, metadata: _OwnedProcessMetadata) -> bool:
        try:
            target = metadata.target()
            expected_argv = tuple(build_ds4_argv(self.config, target))
        except (AdapterError, ValueError):
            return False
        return (
            metadata.pid == metadata.process_group_id
            and metadata.executable == _normalized_path(self.config.binary)
            and metadata.working_directory
            == _normalized_path(self.config.working_directory)
            and metadata.argv == expected_argv
            and _normalized_path(metadata.argv[0]) == metadata.executable
        )

    @staticmethod
    def _metadata_matches_identity(
        metadata: _OwnedProcessMetadata,
        identity: ProcessIdentity,
    ) -> bool:
        return (
            identity.pid == metadata.pid
            and identity.process_group_id == metadata.process_group_id
            and identity.start_identity == metadata.start_identity
            and _normalized_path(identity.executable) == metadata.executable
            and identity.argv == metadata.argv
        )

    async def _load_persisted_ownership(self) -> None:
        if self._metadata_loaded or self._process is not None:
            return
        self._metadata_loaded = True
        if not self._state_path.exists():
            return
        try:
            metadata = _OwnedProcessMetadata.from_dict(
                json.loads(self._state_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._ownership_diagnostic = f"invalid DS4 ownership state: {exc}"
            return

        identity = await self._identity_probe(metadata.pid)
        if identity is None:
            self._remove_metadata(metadata)
            self._ownership_diagnostic = "removed stale DS4 ownership state for an exited process"
            return
        if not self._metadata_matches_config(metadata):
            self._remove_metadata(metadata)
            self._ownership_diagnostic = (
                "persisted DS4 ownership does not match current executable, argv, or configuration"
            )
            return
        if not self._metadata_matches_identity(metadata, identity):
            self._remove_metadata(metadata)
            self._ownership_diagnostic = (
                "persisted DS4 PID was reused or its executable/start identity/argv changed"
            )
            return

        self._owned_metadata = metadata
        self._target = metadata.target()
        self._ownership_diagnostic = None

    async def _refresh_owned_identity(self) -> bool:
        metadata = self._owned_metadata
        if metadata is None:
            return False
        process = self._process
        if process is not None and process.returncode is not None:
            self._remove_metadata(metadata)
            self._owned_metadata = None
            self._process = None
            self._target = None
            return False
        identity = await self._identity_probe(metadata.pid)
        if identity is not None and self._metadata_matches_identity(metadata, identity):
            return True
        self._remove_metadata(metadata)
        self._owned_metadata = None
        self._process = None
        self._target = None
        self._ownership_diagnostic = (
            "owned DS4 process exited"
            if identity is None
            else "DS4 PID identity changed; refusing to adopt or signal it"
        )
        return False

    async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
        await self._load_persisted_ownership()
        owned = await self._refresh_owned_identity()
        payload = await self._models_payload(deadline)
        if payload is None:
            if owned and self._owned_metadata is not None and self._target is not None:
                metadata = self._owned_metadata
                return EngineSnapshot(
                    engine=self.engine,
                    residents=(
                        ResidentInstance(
                            engine=self.engine,
                            canonical_model_id=self._target.key.canonical_model_id,
                            instance_id=str(metadata.pid),
                            ready=False,
                            managed=True,
                            raw={"reported_model": None},
                        ),
                    ),
                    # Process identity is authoritative even when readiness is
                    # not. Reporting the resident lets startup cleanup stop a
                    # surviving, half-ready DS4 process safely.
                    authoritative=True,
                    service_state=ServiceState.UNREACHABLE,
                    diagnostic="managed DS4 process is running but its model API is unavailable",
                )
            if not owned and not self._port_accepting_connections():
                diagnostic = self._ownership_diagnostic
                self._ownership_diagnostic = None
                return EngineSnapshot(
                    engine=self.engine,
                    residents=(),
                    authoritative=True,
                    service_state=ServiceState.STOPPED,
                    diagnostic=diagnostic,
                )
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=False,
                service_state=ServiceState.UNREACHABLE,
                diagnostic=(
                    self._ownership_diagnostic
                    or (
                        "managed DS4 process is running but its model API is unavailable"
                        if owned
                        else "configured DS4 port is occupied by an unknown or unready process"
                    )
                ),
            )

        reported_model: str | None = None
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            candidate = data[0].get("id")
            if isinstance(candidate, str):
                reported_model = candidate
        if reported_model is None:
            if owned and self._owned_metadata is not None and self._target is not None:
                return EngineSnapshot(
                    engine=self.engine,
                    residents=(
                        ResidentInstance(
                            engine=self.engine,
                            canonical_model_id=self._target.key.canonical_model_id,
                            instance_id=str(self._owned_metadata.pid),
                            ready=False,
                            managed=True,
                            raw={"reported_model": None},
                        ),
                    ),
                    authoritative=True,
                    service_state=ServiceState.INCOMPATIBLE,
                    diagnostic="managed DS4 process returned no model id",
                )
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=False,
                service_state=ServiceState.INCOMPATIBLE,
                diagnostic="DS4 /v1/models response contained no model id",
            )

        metadata = self._owned_metadata if owned else None
        managed = metadata is not None and self._target is not None
        canonical = (
            self._target.key.canonical_model_id if managed and self._target else reported_model
        )
        resident = ResidentInstance(
            engine=self.engine,
            canonical_model_id=canonical,
            instance_id=str(metadata.pid) if metadata is not None else None,
            managed=managed,
            raw={"reported_model": reported_model},
        )
        return EngineSnapshot(
            engine=self.engine,
            residents=(resident,),
            authoritative=True,
            service_state=ServiceState.READY,
            diagnostic=(
                None
                if managed
                else self._ownership_diagnostic
                or "DS4 server is not owned by Mnemosyne"
            ),
        )

    async def validate_control(self, *, deadline: Deadline) -> EngineSnapshot:
        return await self.inspect(deadline=deadline)

    async def _observe_spawn_identity(
        self,
        process: ProcessLike,
        argv: list[str],
        deadline: Deadline,
    ) -> ProcessIdentity:
        identity_deadline = time.monotonic() + min(2.0, deadline.remaining())
        while time.monotonic() <= identity_deadline:
            if process.returncode is not None:
                raise AdapterError(
                    self.engine,
                    "load",
                    f"DS4 exited before process identity could be recorded: {process.returncode}",
                )
            identity = await self._identity_probe(process.pid)
            if identity is not None:
                if (
                    identity.pid != process.pid
                    or identity.process_group_id != process.pid
                    or _normalized_path(identity.executable)
                    != _normalized_path(self.config.binary)
                    or identity.argv != tuple(argv)
                ):
                    raise AdapterError(
                        self.engine,
                        "load",
                        "spawned DS4 process identity did not match its executable, "
                        "process group, or argv",
                    )
                return identity
            await asyncio.sleep(min(0.02, max(0.0, identity_deadline - time.monotonic())))
        raise AdapterError(
            self.engine,
            "load",
            "could not establish DS4 process identity after spawn",
        )

    async def load(self, target: ResolvedTarget, *, deadline: Deadline) -> LoadedHandle:
        if target.key.engine != self.engine:
            raise AdapterError(self.engine, "load", "target belongs to another engine")
        before = await self.inspect(deadline=deadline)
        if not before.authoritative:
            raise AdapterError(self.engine, "load", before.diagnostic or "unknown state")
        if before.residents:
            if (
                len(before.residents) == 1
                and before.residents[0].managed
                and before.residents[0].ready
                and self._target is not None
                and self._target.key == target.key
            ):
                return self._handle(target, before.residents[0])
            raise AdapterError(
                self.engine,
                "load",
                before.diagnostic or "DS4 is already resident",
            )
        if self._port_accepting_connections():
            raise AdapterError(
                self.engine,
                "load",
                f"port {self.config.port} is occupied; refusing to signal an unknown process",
            )

        binary = Path(self.config.binary).expanduser()
        working_directory = Path(self.config.working_directory).expanduser()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise AdapterError(self.engine, "load", f"DS4 binary is not executable: {binary}")
        if not working_directory.is_dir():
            raise AdapterError(
                self.engine,
                "load",
                f"DS4 working directory does not exist: {working_directory}",
            )

        argv = build_ds4_argv(self.config, target)
        try:
            process = await self._spawn_process(
                *argv,
                cwd=str(working_directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise AdapterError(self.engine, "load", f"failed to start DS4: {exc}") from exc
        self._process = process
        self._target = target
        self._metadata_loaded = True
        self._log_task = asyncio.create_task(self._capture_logs(process.stdout))

        try:
            identity = await self._observe_spawn_identity(process, argv, deadline)
            metadata = _OwnedProcessMetadata.for_spawn(
                identity=identity,
                executable=binary,
                argv=argv,
                working_directory=working_directory,
                target=target,
            )
            # Keep the fully validated identity in memory before touching
            # disk. If the atomic state write fails, cleanup can still
            # revalidate this exact process identity before signaling it.
            self._owned_metadata = metadata
            self._persist_metadata(metadata)
            self._ownership_diagnostic = None

            while deadline.remaining() > 0:
                if process.returncode is not None:
                    detail = "\n".join(self._log_tail)[-4000:]
                    raise AdapterError(
                        self.engine,
                        "load",
                        f"DS4 exited with status {process.returncode}: {detail}",
                    )
                snapshot = await self.inspect(deadline=deadline)
                if snapshot.authoritative and len(snapshot.residents) == 1:
                    resident = snapshot.residents[0]
                    if (
                        resident.managed
                        and resident.ready
                        and resident.canonical_model_id
                        == target.key.canonical_model_id
                    ):
                        return self._handle(target, resident)
                await asyncio.sleep(min(self._poll_interval_seconds, deadline.remaining()))
            raise AdapterError(
                self.engine,
                "load",
                "DS4 readiness deadline expired",
                retryable=True,
            )
        except BaseException:
            await self._stop_managed_process()
            raise

    async def _wait_for_owned_exit(
        self,
        metadata: _OwnedProcessMetadata,
        timeout: float,
    ) -> bool:
        process = self._process
        if process is not None and process.pid == metadata.pid:
            try:
                await asyncio.wait_for(process.wait(), timeout=max(0.0, timeout))
                return True
            except asyncio.TimeoutError:
                return False

        end = time.monotonic() + max(0.0, timeout)
        while True:
            identity = await self._identity_probe(metadata.pid)
            if identity is None:
                return True
            if not self._metadata_matches_identity(metadata, identity):
                self._ownership_diagnostic = (
                    "DS4 PID identity changed while waiting for exit; refusing further signals"
                )
                return True
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self._poll_interval_seconds, remaining))

    async def _signal_owned(
        self,
        metadata: _OwnedProcessMetadata,
        signal_number: int,
    ) -> bool:
        identity = await self._identity_probe(metadata.pid)
        if identity is None:
            return False
        if not self._metadata_matches_identity(metadata, identity):
            self._remove_metadata(metadata)
            self._owned_metadata = None
            self._process = None
            self._target = None
            self._ownership_diagnostic = (
                "DS4 PID identity changed; refusing to signal an unknown process group"
            )
            raise AdapterError(self.engine, "unload", self._ownership_diagnostic)
        try:
            self._signal_process_group(metadata.process_group_id, signal_number)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise AdapterError(
                self.engine,
                "unload",
                f"failed to signal DS4 process group {metadata.process_group_id}: {exc}",
            ) from exc
        return True

    async def _finish_log_task(self) -> None:
        if self._log_task is None:
            return
        try:
            await asyncio.wait_for(self._log_task, timeout=1)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._log_task.cancel()
        except Exception:
            pass
        self._log_task = None

    async def _stop_managed_process(self) -> None:
        metadata = self._owned_metadata
        process = self._process
        if metadata is None:
            if process is None:
                return
            if process.returncode is not None:
                self._process = None
                self._target = None
                await self._finish_log_task()
                return
            # A spawn that fails before its first complete identity probe has
            # no durable start identity. Refuse to signal in that case: a
            # leaked child is recoverable, while signaling a reused PID is not.
            self._ownership_diagnostic = (
                "could not prove ownership of the newly spawned DS4 process; "
                "refusing to signal its process group"
            )
            raise AdapterError(self.engine, "unload", self._ownership_diagnostic)

        term_sent = await self._signal_owned(metadata, signal.SIGTERM)
        exited = not term_sent or await self._wait_for_owned_exit(
            metadata, self.config.shutdown_grace_seconds
        )
        if not exited:
            kill_sent = await self._signal_owned(metadata, signal.SIGKILL)
            if kill_sent and not await self._wait_for_owned_exit(metadata, 5.0):
                raise AdapterError(
                    self.engine,
                    "unload",
                    "DS4 process group remained alive after SIGKILL",
                )

        self._remove_metadata(metadata)
        self._owned_metadata = None
        self._process = None
        self._target = None
        await self._finish_log_task()

    async def unload(
        self,
        instance: ResidentInstance,
        *,
        deadline: Deadline,
    ) -> None:
        del deadline
        if not instance.managed:
            raise AdapterError(
                self.engine,
                "unload",
                "refusing to terminate a DS4 server not owned by Mnemosyne",
            )
        await self._load_persisted_ownership()
        metadata = self._owned_metadata
        if metadata is None or instance.instance_id != str(metadata.pid):
            raise AdapterError(
                self.engine,
                "unload",
                "resident instance does not match validated Mnemosyne ownership",
            )
        await self._stop_managed_process()
        if self._port_accepting_connections():
            raise AdapterError(
                self.engine,
                "unload",
                f"port {self.config.port} remains occupied after managed process exit",
            )

    def _handle(self, target: ResolvedTarget, instance: ResidentInstance) -> LoadedHandle:
        return LoadedHandle(
            target=target,
            instance=instance,
            base_url=self.base_url,
            wire_model=target.wire_model,
        )

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        if endpoint not in handle.target.capabilities:
            raise AdapterError(self.engine, "route", f"endpoint {endpoint} is unsupported")
        return ProxyRoute(
            base_url=self.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model=handle.wire_model,
            usage_dialect="anthropic" if endpoint == Endpoint.MESSAGES else "openai",
        )

    async def aclose(self) -> None:
        try:
            await self._load_persisted_ownership()
            await self._stop_managed_process()
        finally:
            if self._owns_client:
                await self._client.aclose()
