from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import signal
import stat
import struct
import sys

import httpx
import pytest

from mnemosyne_macos.config import DS4Config, MacConfig
from mnemosyne_macos.engines.base import AdapterError, Deadline
from mnemosyne_macos.engines.ds4 import (
    DS4Adapter,
    ProcessIdentity,
    _parse_kern_procargs,
    build_ds4_argv,
)


def _target():
    return MacConfig.model_validate(
        {
            "engines": {"ds4": {"enabled": True}},
            "models": [
                {
                    "alias": "deepseek-v4-flash",
                    "engine": "ds4",
                    "model": "/models/ds4.gguf",
                    "load": {
                        "context_length": 100000,
                        "kv_disk_directory": "/cache/ds4",
                        "kv_disk_space_mb": 8192,
                        "extra_args": ["--power", "70"],
                    },
                }
            ]
        }
    ).profiles()["deepseek-v4-flash"]


@pytest.mark.asyncio
async def test_managed_runtime_overrides_external_ds4_paths(tmp_path: Path) -> None:
    runtime = tmp_path / "runtimes" / "ds4" / "1.0.0"
    work = runtime / "checkout"
    work.mkdir(parents=True)
    binary = work / "ds4-server"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    (runtime / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "ds4",
                "version": "1.0.0",
                "source_revision": "abc",
                "core_protocol": 1,
                "entrypoint": {
                    "binary": "checkout/ds4-server",
                    "working_directory": "checkout",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "runtimes" / "ds4" / "current.json").write_text(
        json.dumps({"schema_version": 1, "version": "1.0.0"}),
        encoding="utf-8",
    )

    adapter = DS4Adapter(
        DS4Config(binary="/external/ds4", working_directory="/external"),
        runtime_root=tmp_path / "runtimes",
    )
    try:
        effective = adapter._effective_config()
        assert effective.binary == str(binary)
        assert effective.working_directory == str(work)
    finally:
        await adapter.aclose()


def test_build_ds4_argv_pins_managed_options() -> None:
    argv = build_ds4_argv(
        DS4Config(binary="/opt/ds4-server", working_directory="/opt"), _target()
    )
    assert argv[:7] == [
        "/opt/ds4-server",
        "-m",
        "/models/ds4.gguf",
        "--host",
        "127.0.0.1",
        "--port",
        "17323",
    ]
    assert argv[-2:] == ["--power", "70"]


def test_kern_procargs_parser_preserves_argument_boundaries_and_spaces() -> None:
    executable = "/Applications/DwarfStar/ds4-server"
    argv = [executable, "-m", "/Volumes/Model Files/ds4.gguf", "--ctx", "100000"]
    payload = (
        struct.pack("i", len(argv))
        + executable.encode()
        + b"\0\0"
        + b"".join(argument.encode() + b"\0" for argument in argv)
        + b"ENV=value\0"
    )
    assert _parse_kern_procargs(payload) == (executable, tuple(argv))


class FakeProcess:
    pid = 4242
    stdout = None

    def __init__(self) -> None:
        self.returncode: int | None = None
        self._exited = asyncio.Event()

    def exit(self, returncode: int) -> None:
        self.returncode = returncode
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


class FakeProcessControl:
    """Injected process identity and group signaling; never touches the OS."""

    def __init__(self, process: FakeProcess, *, term_exits: bool = True) -> None:
        self.process = process
        self.term_exits = term_exits
        self.started = False
        self.api_ready = True
        self.identity: ProcessIdentity | None = None
        self.signals: list[tuple[int, int]] = []

    def observe_spawn(self, binary: Path, argv: tuple[str, ...]) -> None:
        self.started = True
        self.identity = ProcessIdentity(
            pid=self.process.pid,
            process_group_id=self.process.pid,
            start_identity="Sat Jul 19 09:00:00 2026",
            executable=str(binary.resolve()),
            argv=argv,
        )

    async def probe(self, pid: int) -> ProcessIdentity | None:
        if pid != self.process.pid or self.process.returncode is not None:
            return None
        return self.identity

    def signal_group(self, process_group_id: int, signal_number: int) -> None:
        self.signals.append((process_group_id, signal_number))
        if signal_number == signal.SIGTERM and self.term_exits:
            self.process.exit(0)
            self.identity = None
        elif signal_number == signal.SIGKILL:
            self.process.exit(-signal.SIGKILL)
            self.identity = None


def _config(tmp_path: Path, *, shutdown_grace_seconds: float = 1) -> DS4Config:
    binary = tmp_path / "ds4-server"
    binary.write_text("fake")
    binary.chmod(0o755)
    return DS4Config(
        binary=str(binary),
        working_directory=str(tmp_path),
        process_state_path=str(tmp_path / "state" / "ds4-process.json"),
        port=17323,
        shutdown_grace_seconds=shutdown_grace_seconds,
    )


def _isolated_runtime_root(config: DS4Config) -> Path:
    """Keep adapter fixtures independent of a developer's activated runtime."""

    return Path(config.working_directory) / "runtimes"


def _model_handler(process: FakeProcess, control: FakeProcessControl):
    def handler(_request: httpx.Request) -> httpx.Response:
        if control.started and control.api_ready and process.returncode is None:
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "deepseek-v4-flash"}]},
            )
        return httpx.Response(503)

    return handler


def _adapter(
    config: DS4Config,
    process: FakeProcess,
    control: FakeProcessControl,
    client: httpx.AsyncClient,
) -> tuple[DS4Adapter, dict[str, object]]:
    spawn_kwargs: dict[str, object] = {}

    async def spawn(*argv, **kwargs):
        spawn_kwargs.update(kwargs)
        control.observe_spawn(Path(config.binary), tuple(argv))
        return process

    adapter = DS4Adapter(
        config,
        client=client,
        spawn_process=spawn,
        identity_probe=control.probe,
        signal_process_group=control.signal_group,
        poll_interval_seconds=0,
        runtime_root=_isolated_runtime_root(config),
    )
    # Keep these unit tests independent of listeners on the developer host.
    adapter._port_accepting_connections = lambda: False  # type: ignore[method-assign]
    return adapter, spawn_kwargs


@pytest.mark.asyncio
async def test_ds4_persists_identity_and_terminates_process_group(tmp_path) -> None:
    config = _config(tmp_path)
    process = FakeProcess()
    control = FakeProcessControl(process)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    adapter, spawn_kwargs = _adapter(config, process, control, client)

    handle = await adapter.load(_target(), deadline=Deadline.after(5))
    assert handle.instance.managed is True
    assert handle.instance.instance_id == "4242"
    assert spawn_kwargs["start_new_session"] is True

    state_path = Path(config.process_state_path)
    persisted = json.loads(state_path.read_text())
    assert persisted["pid"] == process.pid
    assert persisted["process_group_id"] == process.pid
    assert persisted["start_identity"] == "Sat Jul 19 09:00:00 2026"
    assert persisted["argv"] == build_ds4_argv(config, _target())
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert not list(state_path.parent.glob("*.tmp"))

    await adapter.unload(handle.instance, deadline=Deadline.after(5))
    assert control.signals == [(process.pid, signal.SIGTERM)]
    assert process.returncode == 0
    assert not state_path.exists()
    await client.aclose()


@pytest.mark.asyncio
async def test_ds4_revalidates_in_memory_identity_when_state_persist_fails(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    process = FakeProcess()
    control = FakeProcessControl(process)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    adapter, _spawn_kwargs = _adapter(config, process, control, client)

    def fail_persist(_metadata) -> None:
        raise OSError("state volume unavailable")

    adapter._persist_metadata = fail_persist  # type: ignore[method-assign]
    with pytest.raises(OSError, match="state volume unavailable"):
        await adapter.load(_target(), deadline=Deadline.after(5))

    assert control.signals == [(process.pid, signal.SIGTERM)]
    assert process.returncode == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_ds4_never_signals_spawn_without_a_proven_identity(tmp_path) -> None:
    config = _config(tmp_path)
    process = FakeProcess()
    control = FakeProcessControl(process)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    adapter, _spawn_kwargs = _adapter(config, process, control, client)

    async def unknown_identity(_pid: int) -> None:
        return None

    adapter._identity_probe = unknown_identity  # type: ignore[assignment]
    with pytest.raises(AdapterError, match="refusing to signal"):
        await adapter.load(_target(), deadline=Deadline.after(0.01))

    assert control.signals == []
    process.exit(1)
    await adapter.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_ds4_terminates_scoped_wrapper_that_hangs_before_exec(tmp_path) -> None:
    config = _config(tmp_path, shutdown_grace_seconds=0.01)
    process = FakeProcess()
    control = FakeProcessControl(process)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    spawned_argv: tuple[str, ...] | None = None

    async def spawn(*argv, **_kwargs):
        nonlocal spawned_argv
        spawned_argv = tuple(argv)
        control.observe_spawn(Path(sys.executable), spawned_argv)
        return process

    adapter = DS4Adapter(
        config,
        client=client,
        spawn_process=spawn,
        identity_probe=control.probe,
        signal_process_group=control.signal_group,
        poll_interval_seconds=0,
        security_scope_root=tmp_path / "security-scopes",
        runtime_root=_isolated_runtime_root(config),
    )
    adapter._port_accepting_connections = lambda: False  # type: ignore[method-assign]
    target = replace(
        _target(),
        scope_id="a" * 64,
        storage_path="/Volumes/Athena/models",
    )

    with pytest.raises(AdapterError, match="could not establish"):
        await adapter.load(target, deadline=Deadline.after(0.03))

    assert spawned_argv is not None
    assert spawned_argv[:3] == (
        sys.executable,
        "-m",
        "mnemosyne_macos.scope_exec",
    )
    assert control.signals == [(process.pid, signal.SIGTERM)]
    assert process.returncode == 0
    await adapter.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_ds4_never_signals_reused_pid_after_scoped_wrapper_observation(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    process = FakeProcess()
    control = FakeProcessControl(process)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    probe_count = 0

    async def spawn(*argv, **_kwargs):
        control.observe_spawn(Path(sys.executable), tuple(argv))
        return process

    async def reused_after_first_observation(pid: int) -> ProcessIdentity | None:
        nonlocal probe_count
        identity = await control.probe(pid)
        if identity is None:
            return None
        probe_count += 1
        if probe_count == 1:
            return identity
        return replace(identity, start_identity="pid-was-reused")

    adapter = DS4Adapter(
        config,
        client=client,
        spawn_process=spawn,
        identity_probe=reused_after_first_observation,
        signal_process_group=control.signal_group,
        poll_interval_seconds=0,
        security_scope_root=tmp_path / "security-scopes",
        runtime_root=_isolated_runtime_root(config),
    )
    adapter._port_accepting_connections = lambda: False  # type: ignore[method-assign]
    target = replace(
        _target(),
        scope_id="b" * 64,
        storage_path="/Volumes/Athena/models",
    )

    with pytest.raises(AdapterError, match="refusing to signal"):
        await adapter.load(target, deadline=Deadline.after(0.1))

    assert probe_count >= 3
    assert control.signals == []
    process.exit(1)
    control.identity = None
    await adapter.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_ds4_escalates_process_group_from_term_to_kill(tmp_path) -> None:
    config = _config(tmp_path, shutdown_grace_seconds=0.01)
    process = FakeProcess()
    control = FakeProcessControl(process, term_exits=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    adapter, _spawn_kwargs = _adapter(config, process, control, client)

    handle = await adapter.load(_target(), deadline=Deadline.after(5))
    await adapter.unload(handle.instance, deadline=Deadline.after(5))

    assert control.signals == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]
    assert process.returncode == -signal.SIGKILL
    await client.aclose()


@pytest.mark.asyncio
async def test_ds4_adopts_valid_survivor_after_service_restart(tmp_path) -> None:
    config = _config(tmp_path)
    process = FakeProcess()
    control = FakeProcessControl(process)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    first, _spawn_kwargs = _adapter(config, process, control, client)
    loaded = await first.load(_target(), deadline=Deadline.after(5))

    async def must_not_spawn(*_argv, **_kwargs):
        raise AssertionError("recovery must not spawn a duplicate DS4 process")

    recovered = DS4Adapter(
        config,
        client=client,
        spawn_process=must_not_spawn,
        identity_probe=control.probe,
        signal_process_group=control.signal_group,
        poll_interval_seconds=0,
        runtime_root=_isolated_runtime_root(config),
    )
    recovered._port_accepting_connections = lambda: False  # type: ignore[method-assign]

    snapshot = await recovered.inspect(deadline=Deadline.after(5))
    assert len(snapshot.residents) == 1
    assert snapshot.residents[0].managed is True
    assert snapshot.residents[0].instance_id == str(process.pid)

    await recovered.unload(snapshot.residents[0], deadline=Deadline.after(5))
    assert control.signals == [(process.pid, signal.SIGTERM)]
    assert not Path(config.process_state_path).exists()
    # The original adapter's stale in-memory handle must not signal again.
    await first.aclose()
    assert control.signals == [(process.pid, signal.SIGTERM)]
    assert loaded.instance.instance_id == str(process.pid)
    await client.aclose()


@pytest.mark.asyncio
async def test_ds4_can_stop_valid_survivor_when_model_api_is_unready(tmp_path) -> None:
    config = _config(tmp_path)
    process = FakeProcess()
    control = FakeProcessControl(process)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    first, _spawn_kwargs = _adapter(config, process, control, client)
    await first.load(_target(), deadline=Deadline.after(5))
    control.api_ready = False

    recovered = DS4Adapter(
        config,
        client=client,
        identity_probe=control.probe,
        signal_process_group=control.signal_group,
        poll_interval_seconds=0,
        runtime_root=_isolated_runtime_root(config),
    )
    recovered._port_accepting_connections = (  # type: ignore[method-assign]
        lambda: process.returncode is None
    )
    snapshot = await recovered.inspect(deadline=Deadline.after(5))

    assert snapshot.authoritative is True
    assert len(snapshot.residents) == 1
    assert snapshot.residents[0].managed is True
    assert snapshot.residents[0].ready is False
    await recovered.unload(snapshot.residents[0], deadline=Deadline.after(5))
    assert control.signals == [(process.pid, signal.SIGTERM)]
    assert not Path(config.process_state_path).exists()
    await first.aclose()
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["start", "executable", "argv", "process_group"])
async def test_ds4_never_adopts_or_signals_mismatched_persisted_identity(
    tmp_path,
    mismatch: str,
) -> None:
    config = _config(tmp_path)
    process = FakeProcess()
    control = FakeProcessControl(process)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    first, _spawn_kwargs = _adapter(config, process, control, client)
    await first.load(_target(), deadline=Deadline.after(5))
    assert control.identity is not None

    changed = control.identity
    if mismatch == "start":
        changed = replace(changed, start_identity="different-start")
    elif mismatch == "executable":
        changed = replace(changed, executable="/tmp/not-ds4")
    elif mismatch == "argv":
        changed = replace(changed, argv=changed.argv + ("--surprise",))
    else:
        changed = replace(changed, process_group_id=9999)
    control.identity = changed

    recovered = DS4Adapter(
        config,
        client=client,
        identity_probe=control.probe,
        signal_process_group=control.signal_group,
        poll_interval_seconds=0,
        runtime_root=_isolated_runtime_root(config),
    )
    recovered._port_accepting_connections = lambda: False  # type: ignore[method-assign]
    snapshot = await recovered.inspect(deadline=Deadline.after(5))

    assert len(snapshot.residents) == 1
    assert snapshot.residents[0].managed is False
    with pytest.raises(AdapterError, match="not owned"):
        await recovered.unload(snapshot.residents[0], deadline=Deadline.after(5))
    assert control.signals == []

    process.exit(0)
    control.identity = None
    await client.aclose()


@pytest.mark.asyncio
async def test_ds4_revalidates_identity_immediately_before_signal(tmp_path) -> None:
    config = _config(tmp_path)
    process = FakeProcess()
    control = FakeProcessControl(process)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    first, _spawn_kwargs = _adapter(config, process, control, client)
    await first.load(_target(), deadline=Deadline.after(5))

    recovered = DS4Adapter(
        config,
        client=client,
        identity_probe=control.probe,
        signal_process_group=control.signal_group,
        poll_interval_seconds=0,
        runtime_root=_isolated_runtime_root(config),
    )
    recovered._port_accepting_connections = lambda: False  # type: ignore[method-assign]
    snapshot = await recovered.inspect(deadline=Deadline.after(5))
    resident = snapshot.residents[0]
    assert resident.managed is True

    assert control.identity is not None
    control.identity = replace(control.identity, start_identity="pid-was-reused")
    with pytest.raises(AdapterError, match="refusing to signal"):
        await recovered.unload(resident, deadline=Deadline.after(5))
    assert control.signals == []

    process.exit(0)
    control.identity = None
    await client.aclose()


@pytest.mark.asyncio
async def test_ds4_unknown_listener_is_never_signaled(tmp_path) -> None:
    config = _config(tmp_path)
    process = FakeProcess()
    control = FakeProcessControl(process)
    control.started = True
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    adapter = DS4Adapter(
        config,
        client=client,
        identity_probe=control.probe,
        signal_process_group=control.signal_group,
        runtime_root=_isolated_runtime_root(config),
    )
    adapter._port_accepting_connections = lambda: True  # type: ignore[method-assign]

    snapshot = await adapter.inspect(deadline=Deadline.after(5))
    assert len(snapshot.residents) == 1
    assert snapshot.residents[0].managed is False
    with pytest.raises(AdapterError, match="not owned"):
        await adapter.unload(snapshot.residents[0], deadline=Deadline.after(5))
    assert control.signals == []
    await client.aclose()


@pytest.mark.asyncio
async def test_ds4_removes_dead_survivor_metadata_without_signaling(tmp_path) -> None:
    config = _config(tmp_path)
    process = FakeProcess()
    control = FakeProcessControl(process)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_model_handler(process, control)))
    first, _spawn_kwargs = _adapter(config, process, control, client)
    await first.load(_target(), deadline=Deadline.after(5))
    process.exit(1)
    control.identity = None

    recovered = DS4Adapter(
        config,
        client=client,
        identity_probe=control.probe,
        signal_process_group=control.signal_group,
        runtime_root=_isolated_runtime_root(config),
    )
    recovered._port_accepting_connections = lambda: False  # type: ignore[method-assign]
    snapshot = await recovered.inspect(deadline=Deadline.after(5))

    assert snapshot.empty
    assert not Path(config.process_state_path).exists()
    assert control.signals == []
    await client.aclose()
