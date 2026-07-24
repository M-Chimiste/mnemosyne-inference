from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemosyne_macos.config import MacConfig, load_config, save_config
from mnemosyne_macos.install_store import InstallRecord
from mnemosyne_macos.local_models import LocalModelError, scan_local_models
from mnemosyne_macos.models import Endpoint, EngineName
from mnemosyne_macos.runtime import NativeRuntime, RuntimeConfigurationError
from mnemosyne_macos.storage import inspect_path


class _DirectFilesystem:
    async def scan(self, path: str, **_kwargs):
        return inspect_path(path), scan_local_models(path)

    async def inspect(self, path: str, **_kwargs):
        return inspect_path(path)


def _gguf(path: Path, payload: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + payload)
    return path


def _mlx_model(
    directory: Path,
    *,
    architecture: str = "ExampleForCausalLM",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(
        f'{{"architectures":["{architecture}"]}}', encoding="utf-8"
    )
    (directory / "model.safetensors").write_bytes(b"weights")
    return directory


def _runtime_for_adoption(config: MacConfig, config_path: Path) -> NativeRuntime:
    """Construct only the state used by the atomic, residency-neutral importer."""

    runtime = object.__new__(NativeRuntime)
    runtime.config = config
    runtime.config_path = config_path
    runtime.env_path = None
    runtime.profiles = config.profiles()
    runtime.adapters = {engine: object() for engine in EngineName}  # type: ignore[assignment]
    runtime.filesystem = _DirectFilesystem()  # type: ignore[assignment]
    runtime.installer = SimpleNamespace(storage=config.storage)
    runtime._reload_lock = asyncio.Lock()
    return runtime


class _ScopeRegistry:
    def __init__(self) -> None:
        self.required: list[tuple[str | None, str]] = []

    async def activate(self, scope_id: str, path: str) -> None:
        self.required.append((scope_id, path))


def _migration_config(
    tmp_path: Path,
    *,
    root: Path,
    model: dict,
    volume_uuid: str | None = None,
) -> tuple[MacConfig, Path]:
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {
                "lmstudio": {"enabled": True},
                "llama_cpp": {"enabled": True},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "existing-models",
                "locations": [
                    {
                        "name": "existing-models",
                        "path": str(root),
                        "volume_uuid": volume_uuid,
                    }
                ],
            },
            "models": [model],
        }
    )
    save_config(config, config_path)
    return config, config_path


@pytest.mark.asyncio
async def test_lmstudio_to_llama_cpp_adoption_preserves_compatible_load_settings(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    directory = root / "publisher" / "vision"
    model = _gguf(directory / "vision-Q4_K_M.gguf")
    first_projector = _gguf(directory / "mmproj-vision-f16.gguf", b"first")
    selected_projector = _gguf(directory / "mmproj-vision-Q8_0.gguf", b"selected")
    candidate = scan_local_models(root)[0]
    projector = next(
        item
        for item in candidate.projector_options
        if item.path == str(selected_projector.resolve())
    )
    config, config_path = _migration_config(
        tmp_path,
        root=root,
        model={
            "alias": "vision",
            "engine": "lmstudio",
            "model": "publisher/vision",
            "served_model_name": "publisher/vision",
            "load": {
                "context_length": 32768,
                "eval_batch_size": 256,
                "flash_attention": False,
                "num_experts": 8,
                "offload_kv_cache_to_gpu": False,
            },
        },
    )
    runtime = _runtime_for_adoption(config, config_path)

    result = await runtime.adopt_local_models(
        str(root),
        [{"candidate_id": candidate.id, "projector_id": projector.id}],
    )

    migrated = load_config(config_path).models[0]
    assert result["restart_required"] is False
    assert result["imported"] == [
        {
            "candidate_id": candidate.id,
            "alias": "vision",
            "engine": "llama.cpp",
            "model_path": str(model.resolve()),
            "projector_path": str(selected_projector.resolve()),
            "migrated": True,
        }
    ]
    assert migrated.alias == "vision"
    assert migrated.engine == EngineName.LLAMA_CPP
    assert migrated.model == str(model.resolve())
    assert migrated.storage == "existing-models"
    assert migrated.served_model_name == "vision"
    assert migrated.load.context_length == 32768
    assert migrated.load.eval_batch_size == 256
    assert migrated.load.flash_attention is False
    assert migrated.load.offload_kv_cache_to_gpu is False
    assert migrated.load.projector_path == str(selected_projector.resolve())
    assert migrated.load.num_experts is None
    assert migrated.load.projector_path != str(first_projector.resolve())
    assert runtime.installer.storage == load_config(config_path).storage


@pytest.mark.asyncio
async def test_lmstudio_to_omlx_adoption_clears_the_old_engine_wire_name(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    directory = _mlx_model(root / "publisher" / "frontier-mlx")
    candidate = scan_local_models(root)[0]
    config, config_path = _migration_config(
        tmp_path,
        root=root,
        model={
            "alias": "frontier",
            "engine": "lmstudio",
            "model": "publisher/frontier-mlx",
            "served_model_name": "lmstudio-only-wire-name",
        },
    )
    runtime = _runtime_for_adoption(config, config_path)

    result = await runtime.adopt_local_models(
        str(root),
        [{"candidate_id": candidate.id}],
    )

    migrated = load_config(config_path).models[0]
    assert result["restart_required"] is False
    assert migrated.alias == "frontier"
    assert migrated.engine == EngineName.OMLX
    assert migrated.model == directory.name
    assert migrated.served_model_name is None
    assert migrated.resolve().wire_model == directory.name


@pytest.mark.asyncio
async def test_local_adoption_does_not_apply_a_pending_restart_configuration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    model = _gguf(root / "publisher" / "chat" / "chat-Q4_K_M.gguf")
    candidate = scan_local_models(root)[0]
    config, config_path = _migration_config(
        tmp_path,
        root=root,
        model={
            "alias": "chat",
            "engine": "lmstudio",
            "model": "publisher/chat",
        },
    )
    runtime = _runtime_for_adoption(config, config_path)
    pending = config.model_copy(
        update={
            "server": config.server.model_copy(
                update={"idle_unload_seconds": 1200}
            )
        }
    )
    save_config(pending, config_path)

    result = await runtime.adopt_local_models(
        str(root),
        [{"candidate_id": candidate.id}],
    )

    persisted = load_config(config_path)
    assert result["restart_required"] is True
    assert persisted.server.idle_unload_seconds == 1200
    assert persisted.models[0].engine == EngineName.LLAMA_CPP
    assert persisted.models[0].model == str(model.resolve())
    assert runtime.config == config
    assert runtime.profiles["chat"].key.engine == EngineName.LMSTUDIO
    assert runtime.installer.storage == config.storage


@pytest.mark.asyncio
async def test_install_completion_does_not_apply_a_pending_restart_configuration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    destination = root / "llama.cpp" / "publisher" / "downloaded"
    model = _gguf(destination / "downloaded-Q4_K_M.gguf")
    config, config_path = _migration_config(
        tmp_path,
        root=root,
        model={
            "alias": "existing",
            "engine": "llama.cpp",
            "model": str(_gguf(root / "existing-Q4_K_M.gguf")),
            "storage": "existing-models",
        },
    )
    runtime = _runtime_for_adoption(config, config_path)
    pending = config.model_copy(
        update={
            "engines": config.engines.model_copy(
                update={
                    "llama_cpp": config.engines.llama_cpp.model_copy(
                        update={"port": 17326}
                    )
                }
            )
        }
    )
    save_config(pending, config_path)
    install = InstallRecord(
        id="install-1",
        repo_id="publisher/downloaded",
        engine=EngineName.LLAMA_CPP.value,
        storage="existing-models",
        alias="downloaded",
        destination=str(destination),
        status="installed",
        filename=model.name,
        capabilities_json='["embeddings"]',
    )

    await runtime._register_installed_model(install)  # noqa: SLF001

    persisted = load_config(config_path)
    assert persisted.engines.llama_cpp.port == 17326
    assert [profile.alias for profile in persisted.models] == ["existing", "downloaded"]
    assert persisted.models[1].capabilities == {Endpoint.EMBEDDINGS}
    assert runtime.config == config
    assert set(runtime.profiles) == {"existing"}
    assert runtime.installer.storage == config.storage


@pytest.mark.parametrize(
    ("alias", "architecture", "expected"),
    [
        (
            "generation",
            "ExampleForCausalLM",
            {
                Endpoint.CHAT_COMPLETIONS,
                Endpoint.COMPLETIONS,
                Endpoint.RESPONSES,
                Endpoint.MESSAGES,
            },
        ),
        ("embeddings", "XLMRobertaModel", {Endpoint.EMBEDDINGS}),
        (
            "reranker",
            "XLMRobertaForSequenceClassification",
            {Endpoint.RERANK},
        ),
    ],
)
@pytest.mark.asyncio
async def test_omlx_install_registration_uses_metadata_derived_capabilities(
    tmp_path: Path,
    alias: str,
    architecture: str,
    expected: set[Endpoint],
) -> None:
    root = tmp_path / "Models"
    destination = _mlx_model(
        root / EngineName.OMLX.value / "publisher" / alias,
        architecture=architecture,
    )
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {
                "llama_cpp": {"enabled": False},
                "omlx": {"enabled": True},
                "ds4": {"enabled": False},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "models",
                "locations": [{"name": "models", "path": str(root)}],
            },
        }
    )
    save_config(config, config_path)
    runtime = _runtime_for_adoption(config, config_path)
    install = InstallRecord(
        id=f"install-{alias}",
        repo_id=f"publisher/{alias}",
        engine=EngineName.OMLX.value,
        storage="models",
        alias=alias,
        destination=str(destination),
        status="installed",
    )

    await runtime._register_installed_model(install)  # noqa: SLF001

    profile = load_config(config_path).models[0]
    assert profile.model == alias
    assert profile.capabilities == expected
    assert profile.resolve().capabilities == frozenset(expected)


@pytest.mark.asyncio
async def test_omlx_install_registration_rejects_a_role_metadata_mismatch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    destination = _mlx_model(
        root / EngineName.OMLX.value / "publisher" / "generation-model",
        architecture="ExampleForCausalLM",
    )
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {
                "llama_cpp": {"enabled": False},
                "omlx": {"enabled": True},
                "ds4": {"enabled": False},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "models",
                "locations": [{"name": "models", "path": str(root)}],
            },
        }
    )
    save_config(config, config_path)
    before = config_path.read_bytes()
    runtime = _runtime_for_adoption(config, config_path)
    install = InstallRecord(
        id="install-role-mismatch",
        repo_id="publisher/generation-model",
        engine=EngineName.OMLX.value,
        storage="models",
        alias="generation-model",
        destination=str(destination),
        status="installed",
        capabilities_json='["embeddings"]',
    )

    with pytest.raises(RuntimeConfigurationError, match="selected model role"):
        await runtime._register_installed_model(install)  # noqa: SLF001

    assert config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_omlx_install_registration_rejects_ambiguous_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    destination = _mlx_model(
        root / EngineName.OMLX.value / "publisher" / "ambiguous",
        architecture="UnknownModel",
    )
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {
                "llama_cpp": {"enabled": False},
                "omlx": {"enabled": True},
                "ds4": {"enabled": False},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "models",
                "locations": [{"name": "models", "path": str(root)}],
            },
        }
    )
    save_config(config, config_path)
    before = config_path.read_bytes()
    runtime = _runtime_for_adoption(config, config_path)
    install = InstallRecord(
        id="install-ambiguous",
        repo_id="publisher/ambiguous",
        engine=EngineName.OMLX.value,
        storage="models",
        alias="ambiguous",
        destination=str(destination),
        status="installed",
    )

    with pytest.raises(
        RuntimeConfigurationError,
        match="does not unambiguously identify",
    ):
        await runtime._register_installed_model(install)  # noqa: SLF001

    assert config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_local_adoption_persists_selected_folder_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    model = _gguf(root / "publisher" / "chat" / "chat-Q4_K_M.gguf")
    candidate = scan_local_models(root)[0]
    config, config_path = _migration_config(
        tmp_path,
        root=root,
        model={
            "alias": "chat",
            "engine": "llama.cpp",
            "model": str(model),
            "storage": "existing-models",
        },
    )
    runtime = _runtime_for_adoption(config, config_path)
    scopes = _ScopeRegistry()
    runtime.security_scope_process = scopes  # type: ignore[assignment]
    scope_id = "a" * 64

    await runtime.adopt_local_models(
        str(root),
        [{"candidate_id": candidate.id}],
        scope_id=scope_id,
    )

    saved = load_config(config_path)
    assert saved.storage.locations[0].scope_id == scope_id
    assert scopes.required
    assert all(item == (scope_id, str(root)) for item in scopes.required)


@pytest.mark.asyncio
async def test_lmstudio_symlink_root_stays_exact_through_scan_and_import(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Volumes" / "Athena" / "nested" / "models"
    model = _gguf(target / "publisher" / "chat" / "chat-Q4_K_M.gguf")
    selected = tmp_path / "home" / ".lmstudio" / "models"
    selected.parent.mkdir(parents=True)
    selected.symlink_to(target, target_is_directory=True)
    candidate = scan_local_models(selected)[0]
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {
                "lmstudio": {"enabled": True},
                "llama_cpp": {"enabled": True},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
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
    runtime = _runtime_for_adoption(config, config_path)
    scopes = _ScopeRegistry()
    runtime.security_scope_process = scopes  # type: ignore[assignment]
    scope_id = "b" * 64

    result = await runtime.adopt_local_models(
        str(selected),
        [{"candidate_id": candidate.id}],
        scope_id=scope_id,
    )

    saved = load_config(config_path)
    adopted = saved.models[0]
    storage = next(
        location for location in saved.storage.locations
        if location.name == adopted.storage
    )
    assert result["imported"][0]["migrated"] is True
    assert storage.path == str(selected)
    assert storage.scope_id == scope_id
    assert adopted.engine == EngineName.LLAMA_CPP
    assert adopted.model == str(model.resolve())
    assert scopes.required
    assert all(item == (scope_id, str(selected)) for item in scopes.required)


@pytest.mark.asyncio
async def test_llama_cpp_readoption_preserves_every_compatible_load_setting(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    directory = root / "publisher" / "chat"
    model = _gguf(directory / "chat-Q5_K_M.gguf")
    previous_projector = _gguf(directory / "mmproj-chat-f16.gguf", b"old")
    selected_projector = _gguf(directory / "mmproj-chat-Q8_0.gguf", b"new")
    candidate = scan_local_models(root)[0]
    projector = next(
        item
        for item in candidate.projector_options
        if item.path == str(selected_projector.resolve())
    )
    config, config_path = _migration_config(
        tmp_path,
        root=root,
        model={
            "alias": "chat",
            "engine": "llama.cpp",
            "model": str(model),
            "storage": "existing-models",
            "load": {
                "context_length": 65536,
                "eval_batch_size": 512,
                "flash_attention": True,
                "offload_kv_cache_to_gpu": False,
                "projector_path": str(previous_projector),
                "gpu_layers": 88,
                "ubatch_size": 128,
                "threads": 12,
                "parallel": 3,
                "extra_args": ["--metrics", "--cont-batching"],
            },
        },
    )
    runtime = _runtime_for_adoption(config, config_path)

    await runtime.adopt_local_models(
        str(root),
        [{"candidate_id": candidate.id, "projector_id": projector.id}],
    )

    load = load_config(config_path).models[0].load
    assert load.model_dump(exclude_none=True, exclude_defaults=True) == {
        "context_length": 65536,
        "eval_batch_size": 512,
        "flash_attention": True,
        "offload_kv_cache_to_gpu": False,
        "projector_path": str(selected_projector.resolve()),
        "gpu_layers": 88,
        "ubatch_size": 128,
        "threads": 12,
        "parallel": 3,
        "extra_args": ["--metrics", "--cont-batching"],
    }


@pytest.mark.asyncio
async def test_local_adoption_rejects_a_stale_projector_selection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    model = _gguf(root / "publisher" / "chat" / "chat-Q4_K_M.gguf")
    candidate = scan_local_models(root)[0]
    config, config_path = _migration_config(
        tmp_path,
        root=root,
        model={
            "alias": "chat",
            "engine": "llama.cpp",
            "model": str(model),
            "storage": "existing-models",
        },
    )
    runtime = _runtime_for_adoption(config, config_path)
    before = config_path.read_bytes()

    with pytest.raises(LocalModelError, match="projector changed"):
        await runtime.adopt_local_models(
            str(root),
            [{"candidate_id": candidate.id, "projector_id": "stale-projector-id"}],
        )

    assert config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_local_adoption_revalidates_existing_storage_volume_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Models"
    model = _gguf(root / "publisher" / "chat" / "chat-Q4_K_M.gguf")
    candidate = scan_local_models(root)[0]
    config, config_path = _migration_config(
        tmp_path,
        root=root,
        volume_uuid="EXPECTED-VOLUME",
        model={
            "alias": "chat",
            "engine": "llama.cpp",
            "model": str(model),
            "storage": "existing-models",
        },
    )
    runtime = _runtime_for_adoption(config, config_path)
    actual = inspect_path(str(root))
    async def mismatched_volume(_path: str, **_kwargs):
        return replace(
                actual,
                volume_uuid="OTHER-VOLUME",
                volume_matches=True,
            )

    monkeypatch.setattr(runtime.filesystem, "inspect", mismatched_volume)
    before = config_path.read_bytes()

    with pytest.raises(LocalModelError, match="volume originally selected"):
        await runtime.adopt_local_models(
            str(root),
            [{"candidate_id": candidate.id}],
        )

    assert config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_omlx_adoption_persists_only_metadata_derived_capabilities(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    directory = _mlx_model(
        root / "mlx-community" / "bge-m3",
        architecture="XLMRobertaModel",
    )
    candidate = scan_local_models(root)[0]
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {
                "llama_cpp": {"enabled": False},
                "omlx": {"enabled": True},
                "ds4": {"enabled": False},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "existing-models",
                "locations": [
                    {"name": "existing-models", "path": str(root)}
                ],
            },
        }
    )
    save_config(config, config_path)
    runtime = _runtime_for_adoption(config, config_path)

    result = await runtime.adopt_local_models(
        str(root),
        [{"candidate_id": candidate.id, "alias": "embeddings"}],
    )

    profile = load_config(config_path).models[0]
    assert result["restart_required"] is False
    assert profile.model == directory.name
    assert profile.capabilities == {Endpoint.EMBEDDINGS}
    assert profile.resolve().capabilities == frozenset({Endpoint.EMBEDDINGS})


@pytest.mark.asyncio
async def test_omlx_discovery_marks_duplicate_id_from_another_registered_root(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "selected"
    registered_root = tmp_path / "registered"
    _mlx_model(selected_root / "owner-two" / "same-model")
    _mlx_model(registered_root / "owner-one" / "same-model")
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {
                "llama_cpp": {"enabled": False},
                "omlx": {
                    "enabled": True,
                    "model_directories": [str(registered_root)],
                },
                "ds4": {"enabled": False},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "selected",
                "locations": [
                    {"name": "selected", "path": str(selected_root)}
                ],
            },
        }
    )
    save_config(config, config_path)
    runtime = _runtime_for_adoption(config, config_path)

    candidates = await runtime.discover_local_models(str(selected_root))

    assert len(candidates) == 1
    assert candidates[0].compatibility == "unavailable"
    assert "first configured root" in candidates[0].compatibility_reason
    with pytest.raises(LocalModelError, match="Duplicate oMLX model ID"):
        await runtime.adopt_local_models(
            str(selected_root),
            [{"candidate_id": candidates[0].id}],
        )


@pytest.mark.asyncio
async def test_omlx_adoption_rejects_duplicate_existing_profile_id(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selected"
    _mlx_model(root / "owner-two" / "same-model")
    candidate = scan_local_models(root)[0]
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {
                "llama_cpp": {"enabled": False},
                "omlx": {"enabled": True},
                "ds4": {"enabled": False},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "selected",
                "locations": [{"name": "selected", "path": str(root)}],
            },
            "models": [
                {
                    "alias": "already-there",
                    "engine": "omlx",
                    "model": "some-owner/same-model",
                }
            ],
        }
    )
    save_config(config, config_path)
    runtime = _runtime_for_adoption(config, config_path)

    with pytest.raises(LocalModelError, match="already used by profile"):
        await runtime.adopt_local_models(
            str(root),
            [{"candidate_id": candidate.id, "alias": "new-copy"}],
        )
