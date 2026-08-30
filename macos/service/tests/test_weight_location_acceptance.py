from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from mnemosyne_macos.app import create_control_app, create_inference_app
from mnemosyne_macos.config import (
    MacConfig,
    StorageConfig,
    StorageLocationConfig,
    load_config,
    save_config,
)
from mnemosyne_macos.desired_install_runtime import (
    NativeDesiredInstallAuthorities,
)
from mnemosyne_macos.engines.base import Deadline, EngineAdapter
import mnemosyne_macos.installer as installer_module
from mnemosyne_macos.installer import NativeInstaller
from mnemosyne_macos.local_models import scan_local_models
from mnemosyne_macos.mac_inventory_store import MacInventoryIndex
from mnemosyne_macos.models import (
    DEFAULT_CAPABILITIES,
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
    ServiceState,
)
from mnemosyne_macos.runtime import NativeRuntime
from mnemosyne_macos.storage import StorageStatus, inspect_path, install_destination
from mnemosyne_macos.usage_delivery import PgUsageWriter


class _ColdAdapter(EngineAdapter):
    engine = EngineName.OMLX
    ownership = "weight-location-acceptance"

    def __init__(self) -> None:
        self.residents: list[ResidentInstance] = []
        self.loads = 0

    async def validate_control(self, *, deadline: Deadline) -> EngineSnapshot:
        return await self.inspect(deadline=deadline)

    async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
        del deadline
        return EngineSnapshot(
            engine=self.engine,
            residents=tuple(self.residents),
            authoritative=True,
            service_state=ServiceState.READY,
        )

    async def load(
        self,
        target: ResolvedTarget,
        *,
        deadline: Deadline,
    ) -> LoadedHandle:
        del deadline
        self.loads += 1
        resident = ResidentInstance(
            engine=self.engine,
            canonical_model_id=target.key.canonical_model_id,
            instance_id=f"weight-location-{self.loads}",
        )
        self.residents = [resident]
        return LoadedHandle(
            target=target,
            instance=resident,
            base_url="http://serving-mac.test",
            wire_model=target.wire_model,
        )

    async def unload(
        self,
        instance: ResidentInstance,
        *,
        deadline: Deadline,
    ) -> None:
        del deadline
        self.residents = [item for item in self.residents if item != instance]

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        return ProxyRoute(
            base_url=handle.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model=handle.wire_model,
        )

    async def aclose(self) -> None:
        return None


class _ObservedFilesystem:
    def __init__(self, *, volume_uuid: str) -> None:
        self.volume_uuid = volume_uuid
        self.calls: list[dict[str, Any]] = []

    async def inspect(self, path: str, **kwargs: Any) -> StorageStatus:
        self.calls.append({"path": path, **kwargs})
        return StorageStatus(
            name=kwargs.get("name"),
            path=path,
            exists=True,
            is_directory=True,
            writable=True,
            mount_path="/Volumes/Athena",
            volume_uuid=self.volume_uuid,
            expected_volume_uuid=kwargs.get("expected_volume_uuid"),
            volume_matches=True,
            total_bytes=128 * 1024**3,
            free_bytes=96 * 1024**3,
            diagnostic=None,
        )


def test_complete_mac_inference_surface_and_capability_floor_remain_intact() -> None:
    app = create_inference_app(object())  # type: ignore[arg-type]
    registered = {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
    }
    assert registered == {
        ("/health", "GET"),
        ("/v1/models", "GET"),
        ("/fleet/v1/snapshot", "GET"),
        ("/v1/chat/completions", "POST"),
        ("/v1/completions", "POST"),
        ("/v1/responses", "POST"),
        ("/v1/messages", "POST"),
        ("/v1/embeddings", "POST"),
        ("/v1/rerank", "POST"),
        ("/v1/images/generations", "POST"),
    }
    generation = {
        Endpoint.CHAT_COMPLETIONS,
        Endpoint.COMPLETIONS,
        Endpoint.RESPONSES,
    }
    assert DEFAULT_CAPABILITIES == {
        EngineName.LLAMA_CPP: frozenset(generation),
        EngineName.OMLX: frozenset(
            generation
            | {Endpoint.MESSAGES, Endpoint.EMBEDDINGS, Endpoint.RERANK}
        ),
        EngineName.DS4: frozenset(generation | {Endpoint.MESSAGES}),
        EngineName.MFLUX: frozenset({Endpoint.IMAGES_GENERATIONS}),
        EngineName.MLXCEL: frozenset(generation),
        EngineName.MISTRAL_RS: frozenset(generation | {Endpoint.MESSAGES}),
    }


@pytest.mark.asyncio
async def test_desired_install_storage_is_local_opaque_and_exactly_bound(
    tmp_path: Path,
) -> None:
    physical_root = (
        tmp_path / "Volumes" / "Athena" / "nested" / "user-chosen-weights"
    )
    physical_root.mkdir(parents=True)
    selected_root = tmp_path / "Library" / "Model Locations" / "athena-link"
    selected_root.parent.mkdir(parents=True)
    selected_root.symlink_to(physical_root, target_is_directory=True)
    lexical_root = str(selected_root)
    volume_uuid = "ATHENA-EXTERNAL-VOLUME"
    scope_id = "a" * 64

    selected_index = MacInventoryIndex(tmp_path / "selected-mac.sqlite3")
    other_index = MacInventoryIndex(tmp_path / "other-mac.sqlite3")
    await selected_index.initialize()
    await other_index.initialize()
    try:
        binding = (
            await selected_index.reconcile_storage(
                [("athena", lexical_root, volume_uuid, scope_id)]
            )
        )["athena"]
        await other_index.reconcile_storage(
            [
                (
                    "other",
                    str(tmp_path / "other-mac-models"),
                    "OTHER-VOLUME",
                    "b" * 64,
                )
            ]
        )
        selected_filesystem = _ObservedFilesystem(volume_uuid=volume_uuid)
        selected_runtime = SimpleNamespace(
            mac_inventory_index=selected_index,
            config=SimpleNamespace(
                storage=SimpleNamespace(
                    locations=[
                        SimpleNamespace(
                            name="athena",
                            path=lexical_root,
                            volume_uuid=volume_uuid,
                            scope_id=scope_id,
                        )
                    ]
                )
            ),
            filesystem=selected_filesystem,
        )
        authority = await NativeDesiredInstallAuthorities(
            selected_runtime
        ).resolve_storage(
            binding.storage_location_id,
            binding.binding_generation,
        )

        assert authority is not None
        assert authority.indexed_lexical_root == lexical_root
        assert authority.configured_lexical_root == lexical_root
        assert authority.observed_lexical_root == lexical_root
        assert authority.indexed_volume_uuid == volume_uuid
        assert authority.configured_volume_uuid == volume_uuid
        assert authority.observed_volume_uuid == volume_uuid
        assert authority.indexed_scope_id == scope_id
        assert authority.configured_scope_id == scope_id
        assert selected_filesystem.calls == [
            {
                "path": lexical_root,
                "name": "athena",
                "expected_volume_uuid": volume_uuid,
                "scope_id": scope_id,
                "scope_path": lexical_root,
            }
        ]

        destination = install_destination(
            Path(authority.configured_lexical_root),
            EngineName.LLAMA_CPP,
            "publisher/model-GGUF",
        )
        assert destination == (
            selected_root / "llama.cpp" / "publisher" / "model-GGUF"
        )
        assert str(destination).startswith(f"{lexical_root}{os.sep}")
        assert not str(destination).startswith(f"{physical_root}{os.sep}")

        other_runtime = SimpleNamespace(
            mac_inventory_index=other_index,
            config=SimpleNamespace(storage=SimpleNamespace(locations=[])),
            filesystem=_ObservedFilesystem(volume_uuid="OTHER-VOLUME"),
        )
        assert (
            await NativeDesiredInstallAuthorities(other_runtime).resolve_storage(
                binding.storage_location_id,
                binding.binding_generation,
            )
            is None
        )

        changed_filesystem = _ObservedFilesystem(volume_uuid=volume_uuid)
        changed_runtime = SimpleNamespace(
            mac_inventory_index=selected_index,
            config=SimpleNamespace(
                storage=SimpleNamespace(
                    locations=[
                        SimpleNamespace(
                            name="athena",
                            path=str(tmp_path / "fallback-must-not-be-used"),
                            volume_uuid=volume_uuid,
                            scope_id=scope_id,
                        )
                    ]
                )
            ),
            filesystem=changed_filesystem,
        )
        stale = await NativeDesiredInstallAuthorities(
            changed_runtime
        ).resolve_storage(
            binding.storage_location_id,
            binding.binding_generation,
        )
        assert stale is not None
        assert stale.availability == "unavailable"
        assert stale.observed_lexical_root == ""
        assert changed_filesystem.calls == []
    finally:
        await selected_index.close()
        await other_index.close()


class _CompletedDownload:
    pid = 43210
    returncode = 0

    def __init__(self, destination: Path) -> None:
        self.destination = destination

    async def communicate(self) -> tuple[bytes, bytes]:
        self.destination.mkdir(parents=True, exist_ok=True)
        (self.destination / "config.json").write_text(
            '{"architectures":["ExampleForCausalLM"]}',
            encoding="utf-8",
        )
        (self.destination / "model.safetensors").write_bytes(b"weights")
        await asyncio.sleep(0)
        return b'{"status":"complete"}', b""


@pytest.mark.asyncio
async def test_selected_location_install_stays_cold_then_jit_and_accounts_on_serving_mac(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKEN_SIDECAR_POSTGRES_DSN", raising=False)
    monkeypatch.setenv(
        "MNEMOSYNE_TOKEN_SIDECAR_LAUNCH_AGENT",
        str(tmp_path / "missing-legacy-sidecar.plist"),
    )
    physical_root = tmp_path / "external-volume" / "nested" / "models"
    physical_root.mkdir(parents=True)
    selected_root = tmp_path / "selected" / "models-link"
    selected_root.parent.mkdir(parents=True)
    selected_root.symlink_to(physical_root, target_is_directory=True)
    destination = install_destination(
        selected_root,
        EngineName.OMLX,
        "publisher/model",
    )
    default_root = tmp_path / "Application Support" / "Mnemosyne" / "models"
    database = tmp_path / "state" / "mnemosyne.db"
    adapter = _ColdAdapter()
    download_argv: list[tuple[str, ...]] = []
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def create_process(*argv: str, **_kwargs: Any) -> _CompletedDownload:
        download_argv.append(argv)
        destination_index = argv.index("--destination") + 1
        return _CompletedDownload(Path(argv[destination_index]))

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    registered_while_cold: list[str] = []

    async def observe_registration(record: Any) -> None:
        assert adapter.loads == 0
        registered_while_cold.append(record.destination)

    storage = StorageConfig(
        default="external",
        locations=[
            StorageLocationConfig(name="external", path=str(selected_root))
        ],
    )
    installer = NativeInstaller(
        database,
        on_installed=observe_registration,
        storage=storage,
    )
    install = await installer.create(
        repo_id="publisher/model",
        engine="omlx",
        storage="external",
        alias="pooled-model",
        destination=str(destination),
        revision="a" * 40,
        filename=None,
        family="example",
        total_bytes=None,
    )
    install_task = installer._tasks[install.id]  # noqa: SLF001
    await asyncio.wait_for(install_task, timeout=10)
    installed = await installer.get(install.id)
    assert installed.status == "installed"
    assert installed.destination == str(destination)
    assert registered_while_cold == [str(destination)]
    assert adapter.loads == 0
    assert download_argv
    assert download_argv[0][download_argv[0].index("--destination") + 1] == str(
        destination
    )
    weight_via_selection = destination / "model.safetensors"
    weight_on_volume = (
        physical_root / "omlx" / "publisher" / "model" / "model.safetensors"
    )
    assert weight_via_selection.read_bytes() == b"weights"
    assert weight_via_selection.stat().st_ino == weight_on_volume.stat().st_ino
    assert not default_root.exists()
    await installer.stop()
    # ``installer_module.asyncio`` is the shared asyncio module. Restore the
    # real process launcher before NativeRuntime performs its independent
    # startup probes.
    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        real_create_subprocess_exec,
    )

    forwarded: dict[str, Any] = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        forwarded["path"] = request.url.path
        forwarded["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-weight-location",
                "choices": [
                    {"message": {"role": "assistant", "content": "ready"}}
                ],
                "usage": {
                    "prompt_tokens": 13,
                    "completion_tokens": 5,
                    "total_tokens": 18,
                },
            },
        )

    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"omlx": {"enabled": True}},
            "paths": {"state_database": str(database)},
            "token_sidecar": {
                "enabled": True,
                "node_id": "mac-studio-serving-device",
                "flush_interval_seconds": 3600,
            },
            "storage": storage.model_dump(mode="json"),
            "models": [
                {
                    "alias": "pooled-model",
                    "engine": "omlx",
                    "model": "model",
                    "storage": "external",
                    "capabilities": ["chat/completions"],
                }
            ],
        }
    )
    runtime = NativeRuntime(
        config,
        adapters={EngineName.OMLX: adapter},
        proxy_client=proxy_client,
        filesystem_probe=_DirectFilesystem(),
    )
    await asyncio.wait_for(runtime.start(raise_on_degraded=True), timeout=15)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        before = await runtime.coordinator.status()
        assert before.resident_alias is None
        assert adapter.loads == 0

        response = await asyncio.wait_for(
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "pooled-model",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            ),
            timeout=15,
        )

        assert response.status_code == 200, response.text
        assert adapter.loads == 1
        assert (await runtime.coordinator.status()).resident_alias == "pooled-model"
        assert forwarded == {
            "path": "/v1/chat/completions",
            "body": {
                "model": "model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        }
        usage_rows = await runtime.usage.list_usage()
        assert len(usage_rows) == 1
        assert (
            usage_rows[0]["prompt_tokens"],
            usage_rows[0]["completion_tokens"],
            usage_rows[0]["total_tokens"],
        ) == (13, 5, 18)
        outbox_rows = runtime.usage.store.peek_outbox(limit=10)
        assert len(outbox_rows) == 1
        usage_status = await runtime.usage.status()
        assert usage_status["node_id"] == "mac-studio-serving-device"
        writer = PgUsageWriter(
            dsn="postgresql://unused.invalid/ledger",
            node_id=usage_status["node_id"],
            connect_timeout=1,
        )
        assert writer._params(outbox_rows[0])[2] == "mac-studio-serving-device"  # noqa: SLF001
        assert weight_via_selection.read_bytes() == b"weights"
        assert not default_root.exists()
    finally:
        await client.aclose()
        await runtime.stop()
        await proxy_client.aclose()


class _DirectFilesystem:
    def __init__(self, *, volume_uuid: str | None = None) -> None:
        self.volume_uuid = volume_uuid

    async def scan(self, path: str, **_kwargs: Any):
        return await self.inspect(path), scan_local_models(path)

    async def inspect(self, path: str, **_kwargs: Any) -> StorageStatus:
        status = inspect_path(path)
        if self.volume_uuid is None:
            return status
        return replace(
            status,
            volume_uuid=self.volume_uuid,
            expected_volume_uuid=self.volume_uuid,
            volume_matches=True,
        )


class _ScopeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def activate(self, scope_id: str, path: str) -> None:
        self.calls.append((scope_id, path))


@pytest.mark.asyncio
async def test_lmstudio_migration_adopts_external_weights_without_copy_or_move(
    tmp_path: Path,
) -> None:
    physical_root = tmp_path / "external" / "nested" / "models"
    model_path = physical_root / "publisher" / "chat" / "chat-Q4_K_M.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"GGUFexternal-import")
    selected_root = tmp_path / "home" / ".lmstudio" / "models"
    selected_root.parent.mkdir(parents=True)
    selected_root.symlink_to(physical_root, target_is_directory=True)
    candidate = scan_local_models(selected_root)[0]
    before_stat = model_path.stat()
    before_bytes = model_path.read_bytes()
    volume_uuid = "ATHENA-MIGRATION-VOLUME"
    scope_id = "c" * 64
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {
                "lmstudio": {"enabled": True},
                "llama_cpp": {"enabled": True},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "lmstudio-models",
                "locations": [
                    {
                        "name": "lmstudio-models",
                        "path": str(selected_root),
                        "volume_uuid": volume_uuid,
                        "scope_id": scope_id,
                    }
                ],
            },
            "models": [
                {
                    "alias": "chat",
                    "engine": "lmstudio",
                    "model": "publisher/chat",
                }
            ],
        }
    )
    save_config(config, config_path)
    runtime = object.__new__(NativeRuntime)
    runtime.config = config
    runtime.config_path = config_path
    runtime.env_path = None
    runtime.profiles = config.profiles()
    runtime.adapters = {engine: object() for engine in EngineName}
    runtime.filesystem = _DirectFilesystem(volume_uuid=volume_uuid)
    runtime.installer = SimpleNamespace(storage=config.storage)
    runtime._reload_lock = asyncio.Lock()
    scopes = _ScopeRecorder()
    runtime.security_scope_process = scopes

    result = await runtime.adopt_local_models(
        str(selected_root),
        [{"candidate_id": candidate.id}],
        scope_id=scope_id,
    )

    saved = load_config(config_path)
    adopted = saved.models[0]
    location = next(item for item in saved.storage.locations if item.name == adopted.storage)
    assert result["imported"][0]["migrated"] is True
    assert location.path == str(selected_root)
    assert location.volume_uuid == volume_uuid
    assert location.scope_id == scope_id
    assert scopes.calls
    assert all(call == (scope_id, str(selected_root)) for call in scopes.calls)
    assert adopted.engine == EngineName.LLAMA_CPP
    assert adopted.model == str(selected_root / model_path.relative_to(physical_root))
    assert runtime.storage_scope_for_path(adopted.model) == (
        scope_id,
        str(selected_root),
    )
    assert model_path.read_bytes() == before_bytes
    after_stat = model_path.stat()
    assert (after_stat.st_dev, after_stat.st_ino) == (
        before_stat.st_dev,
        before_stat.st_ino,
    )
    assert selected_root.is_symlink()
    assert not (tmp_path / "Application Support" / "Mnemosyne" / "models").exists()


def _cleanup_config(
    tmp_path: Path,
    *,
    selected_root: Path,
    models: list[dict[str, Any]],
    omlx: bool = False,
) -> tuple[MacConfig, Path]:
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {
                "llama_cpp": {"enabled": not omlx},
                "omlx": {"enabled": omlx},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "token_sidecar": {"enabled": False},
            "storage": {
                "default": "external",
                "locations": [
                    {"name": "external", "path": str(selected_root)}
                ],
            },
            "models": models,
        }
    )
    save_config(config, config_path)
    return config, config_path


@pytest.mark.asyncio
async def test_cleanup_refuses_shared_imported_weights_on_external_symlink(
    tmp_path: Path,
) -> None:
    physical_root = tmp_path / "external-volume" / "nested" / "models"
    model_path = physical_root / "publisher" / "shared-Q4_K_M.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"GGUFshared-external")
    selected_root = tmp_path / "selected" / "external-link"
    selected_root.parent.mkdir(parents=True)
    selected_root.symlink_to(physical_root, target_is_directory=True)
    lexical_model_path = selected_root / model_path.relative_to(physical_root)
    config, config_path = _cleanup_config(
        tmp_path,
        selected_root=selected_root,
        models=[
            {
                "alias": "first",
                "engine": "llama.cpp",
                    "model": str(lexical_model_path),
                "storage": "external",
            },
            {
                "alias": "second",
                "engine": "llama.cpp",
                    "model": str(lexical_model_path),
                "storage": "external",
            },
        ],
    )
    adapter = _ColdAdapter()
    adapter.engine = EngineName.LLAMA_CPP
    proxy_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters={EngineName.LLAMA_CPP: adapter},
        proxy_client=proxy_client,
        filesystem_probe=_DirectFilesystem(),
    )
    await asyncio.wait_for(runtime.start(raise_on_degraded=True), timeout=15)
    filesystem_mutations: list[str] = []

    async def unexpected_mutation(**_kwargs: Any) -> bool:
        filesystem_mutations.append("called")
        raise AssertionError("shared imported weights must not reach Trash")

    runtime.filesystem.trash_paths = unexpected_mutation  # type: ignore[method-assign]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        config_before = config_path.read_bytes()
        stat_before = model_path.stat()
        revision = (await client.get("/manager/config")).json()["revision"]
        response = await asyncio.wait_for(
            client.request(
                "DELETE",
                "/manager/models/first",
                json={"revision": revision},
            ),
            timeout=15,
        )

        assert response.status_code == 400
        assert "second" in response.json()["detail"]
        assert filesystem_mutations == []
        assert config_path.read_bytes() == config_before
        assert model_path.read_bytes() == b"GGUFshared-external"
        stat_after = model_path.stat()
        assert (stat_after.st_dev, stat_after.st_ino) == (
            stat_before.st_dev,
            stat_before.st_ino,
        )
        assert selected_root.is_symlink()
    finally:
        await client.aclose()
        await runtime.stop()
        await proxy_client.aclose()


@pytest.mark.asyncio
async def test_cleanup_refuses_ambiguous_external_omlx_weights(
    tmp_path: Path,
) -> None:
    physical_root = tmp_path / "external-volume" / "nested" / "models"
    weight_paths: list[Path] = []
    for owner in ("publisher-a", "publisher-b"):
        model_root = physical_root / owner / "duplicate-model"
        model_root.mkdir(parents=True)
        (model_root / "config.json").write_text(
            '{"architectures":["ExampleForCausalLM"]}',
            encoding="utf-8",
        )
        weight = model_root / "model.safetensors"
        weight.write_bytes(f"weights-{owner}".encode())
        weight_paths.append(weight)
    selected_root = tmp_path / "selected" / "external-link"
    selected_root.parent.mkdir(parents=True)
    selected_root.symlink_to(physical_root, target_is_directory=True)
    config, config_path = _cleanup_config(
        tmp_path,
        selected_root=selected_root,
        omlx=True,
        models=[
            {
                "alias": "ambiguous",
                "engine": "omlx",
                "model": "duplicate-model",
                "storage": "external",
            }
        ],
    )
    adapter = _ColdAdapter()
    proxy_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters={EngineName.OMLX: adapter},
        proxy_client=proxy_client,
        filesystem_probe=_DirectFilesystem(),
    )
    await asyncio.wait_for(runtime.start(raise_on_degraded=True), timeout=15)
    filesystem_mutations: list[str] = []

    async def unexpected_mutation(**_kwargs: Any) -> bool:
        filesystem_mutations.append("called")
        raise AssertionError("ambiguous imported weights must not reach Trash")

    runtime.filesystem.trash_paths = unexpected_mutation  # type: ignore[method-assign]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        config_before = config_path.read_bytes()
        before = {
            path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino)
            for path in weight_paths
        }
        revision = (await client.get("/manager/config")).json()["revision"]
        response = await asyncio.wait_for(
            client.request(
                "DELETE",
                "/manager/models/ambiguous",
                json={"revision": revision},
            ),
            timeout=15,
        )

        assert response.status_code == 400
        assert "exactly one item" in response.json()["detail"]
        assert filesystem_mutations == []
        assert config_path.read_bytes() == config_before
        for path, (contents, device, inode) in before.items():
            assert path.read_bytes() == contents
            current = path.stat()
            assert (current.st_dev, current.st_ino) == (device, inode)
        assert selected_root.is_symlink()
    finally:
        await client.aclose()
        await runtime.stop()
        await proxy_client.aclose()
