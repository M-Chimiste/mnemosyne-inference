from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import signal

import httpx
import pytest

from mnemosyne_macos.config import LlamaCppConfig, MacConfig
from mnemosyne_macos.engines.base import AdapterError, Deadline
from mnemosyne_macos.engines.ds4 import ProcessIdentity, _OwnedProcessMetadata
from mnemosyne_macos.engines.llamacpp import (
    LlamaCppAdapter,
    build_llama_cpp_argv,
)
from mnemosyne_macos.models import Endpoint, EngineName
from mnemosyne_macos.filesystem import FilesystemProbeError


def _gguf(path: Path, payload: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + payload)
    return path


def _target(
    model: Path,
    *,
    alias: str = "local-model",
    capabilities: list[str] | None = None,
    load: dict | None = None,
):
    profile: dict[str, object] = {
        "alias": alias,
        "engine": "llama.cpp",
        "model": str(model),
    }
    if capabilities is not None:
        profile["capabilities"] = capabilities
    if load is not None:
        profile["load"] = load
    return MacConfig.model_validate({"models": [profile]}).profiles()[alias]


def test_build_llama_cpp_argv_pins_generation_options_and_projector(
    tmp_path: Path,
) -> None:
    model = _gguf(tmp_path / "models" / "vision-Q4_K_M.gguf")
    projector = _gguf(tmp_path / "models" / "mmproj-vision-f16.gguf")
    target = _target(
        model,
        alias="vision-model",
        load={
            "gpu_layers": 99,
            "context_length": 8192,
            "eval_batch_size": 512,
            "ubatch_size": 128,
            "threads": 8,
            "parallel": 2,
            "flash_attention": True,
            "offload_kv_cache_to_gpu": False,
            "projector_path": str(projector),
            "extra_args": ["--metrics"],
        },
    )

    argv = build_llama_cpp_argv(
        LlamaCppConfig(binary="/opt/llama-server", port=17325),
        target,
    )

    assert argv == [
        "/opt/llama-server",
        "--model",
        str(model.resolve()),
        "--alias",
        "vision-model",
        "--host",
        "127.0.0.1",
        "--port",
        "17325",
        "--jinja",
        "--no-webui",
        "--n-gpu-layers",
        "99",
        "--ctx-size",
        "8192",
        "--batch-size",
        "512",
        "--ubatch-size",
        "128",
        "--threads",
        "8",
        "--parallel",
        "2",
        "--flash-attn",
        "on",
        "--no-kv-offload",
        "--mmproj",
        str(projector.resolve()),
        "--metrics",
    ]


def test_build_llama_cpp_argv_selects_embedding_and_rerank_server_modes(
    tmp_path: Path,
) -> None:
    model = _gguf(tmp_path / "embed-Q8_0.gguf")
    config = LlamaCppConfig(binary="/opt/llama-server")

    embeddings = build_llama_cpp_argv(
        config,
        _target(
            model,
            alias="embeddings",
            capabilities=["embeddings"],
            load={"pooling": "mean"},
        ),
    )
    assert embeddings[-3:] == ["--embedding", "--pooling", "mean"]
    assert "--reranking" not in embeddings

    rerank = build_llama_cpp_argv(
        config,
        _target(model, alias="reranker", capabilities=["rerank"]),
    )
    assert rerank[-4:] == ["--embedding", "--reranking", "--pooling", "rank"]


@pytest.mark.parametrize(
    "extra_arg",
    [
        "--model=/tmp/other.gguf",
        "-m",
        "--alias=other",
        "--host",
        "--port=9999",
        "--ctx-size",
        "--batch-size=1",
        "--ubatch-size",
        "--threads",
        "--parallel",
        "--n-gpu-layers=0",
        "--flash-attn",
        "--no-kv-offload",
        "--mmproj=/tmp/other.gguf",
        "--embedding",
        "--reranking",
        "--pooling=none",
    ],
)
def test_build_llama_cpp_argv_rejects_reserved_extra_args(
    tmp_path: Path,
    extra_arg: str,
) -> None:
    model = _gguf(tmp_path / "model.gguf")
    target = _target(model, load={"extra_args": [extra_arg]})

    with pytest.raises(AdapterError, match="may not override managed option"):
        build_llama_cpp_argv(LlamaCppConfig(), target)


def test_build_llama_cpp_argv_validates_model_and_projector_headers(
    tmp_path: Path,
) -> None:
    invalid_model = tmp_path / "invalid.gguf"
    invalid_model.write_bytes(b"NOPE")
    with pytest.raises(AdapterError, match="does not have a GGUF header"):
        build_llama_cpp_argv(LlamaCppConfig(), _target(invalid_model))

    model = _gguf(tmp_path / "model.gguf")
    invalid_projector = tmp_path / "mmproj.gguf"
    invalid_projector.write_bytes(b"not a gguf")
    with pytest.raises(AdapterError, match="multimodal projector.*GGUF header"):
        build_llama_cpp_argv(
            LlamaCppConfig(),
            _target(model, load={"projector_path": str(invalid_projector)}),
        )

    with pytest.raises(AdapterError, match="must be different"):
        build_llama_cpp_argv(
            LlamaCppConfig(),
            _target(model, load={"projector_path": str(model)}),
        )

    wrong_extension = tmp_path / "model.bin"
    wrong_extension.write_bytes(b"GGUFfixture")
    with pytest.raises(AdapterError, match=r"must select a \.gguf file"):
        build_llama_cpp_argv(LlamaCppConfig(), _target(wrong_extension))


@pytest.mark.asyncio
async def test_llama_cpp_file_validation_does_not_block_event_loop(
    tmp_path: Path,
) -> None:
    model = _gguf(tmp_path / "model.gguf")
    target = _target(model)
    entered = asyncio.Event()
    release = asyncio.Event()
    class Probe:
        async def validate_llama(self, **_kwargs):
            entered.set()
            await release.wait()
            return {"model": str(model), "projector": None}

    adapter = LlamaCppAdapter(
        LlamaCppConfig(request_timeout_seconds=1),
        runtime_root=tmp_path / "managed-runtimes",
        filesystem_probe=Probe(),  # type: ignore[arg-type]
    )
    try:
        preparation = asyncio.create_task(
            adapter._build_argv_async(  # noqa: SLF001 - regression boundary
                adapter.config,
                target,
                deadline=Deadline.after(2),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=0.5)

        # A heartbeat scheduled on the main loop must run while the simulated
        # protected-folder open remains blocked in its worker thread.
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=0.1)

        release.set()
        argv = await preparation
        assert argv[0].endswith("llama-server")
    finally:
        release.set()
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llama_cpp_file_validation_timeout_is_actionable(
    tmp_path: Path,
) -> None:
    model = _gguf(tmp_path / "model.gguf")

    class Probe:
        async def validate_llama(self, **_kwargs):
            raise FilesystemProbeError(
                "model storage operation timed out; verify the selected-folder permission"
            )

    adapter = LlamaCppAdapter(
        LlamaCppConfig(request_timeout_seconds=0.05),
        runtime_root=tmp_path / "managed-runtimes",
        filesystem_probe=Probe(),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(AdapterError, match="selected-folder permission"):
            await adapter._build_argv_async(  # noqa: SLF001 - regression boundary
                adapter.config,
                _target(model),
                deadline=Deadline.after(1),
            )
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("capabilities", "load", "message"),
    [
        (
            ["chat/completions", "embeddings"],
            {},
            "generation profiles cannot also advertise embeddings or rerank",
        ),
        (
            ["rerank", "embeddings"],
            {},
            "rerank profiles must use only the rerank capability",
        ),
        (
            ["embeddings"],
            {"projector_path": "/models/mmproj.gguf"},
            "projectors require a generation-capable profile",
        ),
    ],
)
def test_llama_cpp_profile_rejects_incompatible_capability_modes(
    capabilities: list[str],
    load: dict,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        MacConfig.model_validate(
            {
                "models": [
                    {
                        "alias": "invalid",
                        "engine": "llama.cpp",
                        "model": "/models/model.gguf",
                        "capabilities": capabilities,
                        "load": load,
                    }
                ]
            }
        )


def test_llama_cpp_profile_resolves_specialized_capabilities_and_wire_name() -> None:
    target = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "embed-local",
                    "engine": "llama.cpp",
                    "model": "~/Models/embed.gguf",
                    "capabilities": ["embeddings"],
                    "load": {"pooling": "cls"},
                }
            ]
        }
    ).profiles()["embed-local"]

    assert target.key.engine == EngineName.LLAMA_CPP
    assert target.wire_model == "embed-local"
    assert target.capabilities == frozenset({Endpoint.EMBEDDINGS})
    assert target.load_options == {"pooling": "cls"}
    assert Path(target.key.canonical_model_id).is_absolute()


@pytest.mark.asyncio
async def test_llama_cpp_owned_metadata_retains_scope_for_restart_validation(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "runtime" / "llama-server"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    model = _gguf(tmp_path / "protected" / "model-Q4_K_M.gguf")
    target = replace(
        _target(model, alias="protected-model"),
        storage_path=str(model.parent),
        scope_id="a" * 64,
        storage_volume_uuid="PROTECTED-VOLUME",
    )
    config = LlamaCppConfig(
        binary=str(binary),
        working_directory=str(binary.parent),
    )
    argv = build_llama_cpp_argv(config, target)
    identity = ProcessIdentity(
        pid=42426,
        process_group_id=42426,
        start_identity="Wed Jul 22 12:00:00 2026",
        executable=str(binary.resolve()),
        argv=tuple(argv),
    )
    restored = _OwnedProcessMetadata.from_dict(
        _OwnedProcessMetadata.for_spawn(
            identity=identity,
            executable=binary,
            argv=argv,
            working_directory=binary.parent,
            target=target,
        ).to_dict()
    )
    calls: list[dict] = []

    class Probe:
        async def validate_llama(self, **kwargs):
            calls.append(kwargs)
            return {"model": str(model), "projector": None}

    adapter = LlamaCppAdapter(
        config,
        runtime_root=tmp_path / "managed-runtimes",
        filesystem_probe=Probe(),  # type: ignore[arg-type]
    )
    try:
        assert await adapter._metadata_matches_config(  # noqa: SLF001
            restored,
            deadline=Deadline.after(2),
        )
        recovered = restored.target(EngineName.LLAMA_CPP)
        assert restored.schema_version == 2
        assert recovered.storage_path == str(model.parent)
        assert recovered.scope_id == "a" * 64
        assert recovered.storage_volume_uuid == "PROTECTED-VOLUME"
        assert calls[0]["root"] == str(model.parent)
        assert calls[0]["scope_id"] == "a" * 64
        assert calls[0]["expected_volume_uuid"] == "PROTECTED-VOLUME"
    finally:
        await adapter.aclose()


class _FakeProcess:
    pid = 42425
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


@pytest.mark.asyncio
async def test_llama_cpp_inherits_owned_process_lifecycle_and_routing(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "runtime" / "llama-server"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    model = _gguf(tmp_path / "models" / "chat-Q4_K_M.gguf")
    state_path = tmp_path / "state" / "llama-cpp.json"
    config = LlamaCppConfig(
        binary=str(binary),
        working_directory=str(binary.parent),
        process_state_path=str(state_path),
        shutdown_grace_seconds=1,
    )
    target = _target(model, alias="chat-local")
    process = _FakeProcess()
    identity: ProcessIdentity | None = None
    spawn_kwargs: dict[str, object] = {}
    signals: list[tuple[int, int]] = []

    async def spawn(*argv: str, **kwargs):
        nonlocal identity
        spawn_kwargs.update(kwargs)
        identity = ProcessIdentity(
            pid=process.pid,
            process_group_id=process.pid,
            start_identity="Wed Jul 22 12:00:00 2026",
            executable=str(binary.resolve()),
            argv=tuple(argv),
        )
        return process

    async def probe(pid: int) -> ProcessIdentity | None:
        if pid == process.pid and process.returncode is None:
            return identity
        return None

    def signal_group(process_group_id: int, signal_number: int) -> None:
        nonlocal identity
        signals.append((process_group_id, signal_number))
        process.exit(0)
        identity = None

    def handler(_request: httpx.Request) -> httpx.Response:
        if identity is not None and process.returncode is None:
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": target.wire_model}]},
            )
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LlamaCppAdapter(
        config,
        runtime_root=tmp_path / "managed-runtimes",
        client=client,
        spawn_process=spawn,
        identity_probe=probe,
        signal_process_group=signal_group,
        poll_interval_seconds=0,
    )
    adapter._port_accepting_connections = lambda: False  # type: ignore[method-assign]
    try:
        handle = await adapter.load(target, deadline=Deadline.after(5))

        assert handle.instance.engine == EngineName.LLAMA_CPP
        assert handle.instance.managed is True
        assert spawn_kwargs["cwd"] == str(binary.parent)
        assert spawn_kwargs["start_new_session"] is True
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["canonical_model_id"] == str(model.resolve())
        assert persisted["argv"] == build_llama_cpp_argv(config, target)

        route = adapter.route(handle, Endpoint.MESSAGES)
        assert route.path == "/v1/messages"
        assert route.usage_dialect == "anthropic"
        with pytest.raises(AdapterError, match="unsupported"):
            adapter.route(handle, Endpoint.EMBEDDINGS)

        await adapter.unload(handle.instance, deadline=Deadline.after(5))
        assert signals == [(process.pid, signal.SIGTERM)]
        assert not state_path.exists()
    finally:
        await adapter.aclose()
        await client.aclose()
