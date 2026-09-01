from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import HTTPException
from jsonschema import Draft202012Validator
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

import mnemosyne_macos.runtime as runtime_module
from mnemosyne_macos.app import (
    _validated_install_capabilities,
    create_control_app,
    create_inference_app,
)
from mnemosyne_macos.benchmarking import (
    BENCHMARK_SUITE_VERSION,
    BenchmarkRecord,
    candidate_set_fingerprint,
    system_fingerprint,
    target_fingerprint,
)
from mnemosyne_macos.config import (
    MacConfig,
    ModelContextConfig,
    StorageLocationConfig,
    load_config,
    save_config,
)
from mnemosyne_macos.coordinator import CoordinatorError, CoordinatorState
from mnemosyne_macos.engines.base import AdapterError, Deadline, EngineAdapter
from mnemosyne_macos.filesystem import FilesystemProbeError
from mnemosyne_macos.fleet_pairing import FleetPairingError, PairingCredentials
from mnemosyne_macos.fleet_pairing_client import (
    PairingClientErrorCode,
    PairingInvitation,
    _ClaimResponse,
    _ProvisionResponse,
    _presence_pin,
)
from mnemosyne_macos.install_provenance import (
    DestinationStateBefore,
    ExclusiveManagedProof,
    InstallationProvenance,
    OwnedFile,
    destination_binding_digest,
    owned_manifest_digest,
)
from mnemosyne_macos.install_launch import (
    OMLX_TARGET_LAUNCH_KEY,
    OMLXInstallLaunch,
    omlx_target_launch,
)
from mnemosyne_macos.install_store import InstallRecord, InstallStore
from mnemosyne_macos.mac_inventory_store import StorageBinding
from mnemosyne_macos.model_library import LibraryModel
from mnemosyne_macos.model_cleanup_journal import CleanupPhase
from mnemosyne_macos.models import (
    ACTIVE_ENGINE_NAMES,
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
    ServiceState,
)
from mnemosyne_macos.runtime import (
    ModelCleanupRejected,
    NativeRuntime,
    RuntimeConfigurationError,
    _current_storage_binding_matches,
    _redact_diagnostic,
    configuration_revision,
    validate_exposure,
)
from mnemosyne_macos.runtime_updates import RuntimeUpdateError
from mnemosyne_macos.storage import StorageStatus
from mnemosyne_macos.usage import NormalizedUsage, UsageEvent


FLEET_SNAPSHOT_SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "fleet_protocol"
    / "v1"
    / "snapshot.schema.json"
)


class FakeAdapter(EngineAdapter):
    ownership = "fake"

    def __init__(self, engine: EngineName) -> None:
        self.engine = engine
        self.residents: list[ResidentInstance] = []
        self.loads = 0
        self.service_state = ServiceState.READY

    async def validate_control(self, *, deadline: Deadline) -> EngineSnapshot:
        return await self.inspect(deadline=deadline)

    async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
        del deadline
        return EngineSnapshot(
            engine=self.engine,
            residents=tuple(self.residents),
            authoritative=True,
            service_state=self.service_state,
        )

    async def load(self, target: ResolvedTarget, *, deadline: Deadline) -> LoadedHandle:
        del deadline
        self.loads += 1
        resident = ResidentInstance(
            engine=self.engine,
            canonical_model_id=target.key.canonical_model_id,
            instance_id=f"fake-{self.loads}",
        )
        self.residents = [resident]
        return LoadedHandle(
            target=target,
            instance=resident,
            base_url=f"http://{self.engine}.test",
            wire_model=target.wire_model,
        )

    async def unload(
        self, instance: ResidentInstance, *, deadline: Deadline
    ) -> None:
        del deadline
        self.residents = [current for current in self.residents if current != instance]

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        return ProxyRoute(
            base_url=handle.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model=handle.wire_model,
        )

    async def aclose(self) -> None:
        return None


class ContractOMLXAdapter(FakeAdapter):
    """Content-free oMLX fake that can drift its two service globals."""

    def __init__(self, *, slots: int = 2, memory_guard: bool = True) -> None:
        super().__init__(EngineName.OMLX)
        self.slots = slots
        self.memory_guard = memory_guard
        self.contract_checks = 0

    async def require_launch_contract(
        self,
        contract: OMLXInstallLaunch,
        *,
        deadline: Deadline,
    ) -> SimpleNamespace:
        del deadline
        self.contract_checks += 1
        if contract.scheduler_slots != self.slots:
            raise AdapterError(
                self.engine,
                "verify signed launch contract",
                "scheduler drift",
            )
        if contract.memory_guard == "required" and not self.memory_guard:
            raise AdapterError(
                self.engine,
                "verify signed launch contract",
                "memory guard drift",
            )
        return SimpleNamespace(
            scheduler_slots=self.slots,
            memory_guard_enabled=self.memory_guard,
        )


def test_readiness_diagnostics_redact_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OMLX_ADMIN_SESSION", "secret-session-value")
    monkeypatch.setenv("FLEET_API_KEY", "fleet-secret-value")
    monkeypatch.setenv("CUSTOM_SECRET_KEY", "custom-secret-value")
    monkeypatch.setenv("SHORT_SECRET_KEY", "xy")

    redacted = _redact_diagnostic(
        "Authorization: Bearer secret-session-value "
        "postgresql://writer:password123@nyx/token_sidecar "
        "https://reader:webpass@example.test/metrics "
        "api_key=visible custom-secret-value xy fleet-secret-value",
        secret_env_keys=("CUSTOM_SECRET_KEY", "SHORT_SECRET_KEY"),
    )

    assert "secret-session-value" not in redacted
    assert "password123" not in redacted
    assert "webpass" not in redacted
    assert "visible" not in redacted
    assert redacted == (
        "Authorization: Bearer <redacted> "
        "postgresql://writer:<redacted>@nyx/token_sidecar "
        "https://reader:<redacted>@example.test/metrics "
        "api_key=<redacted> <redacted> <redacted> <redacted>"
    )


def _config(tmp_path, *, endpoint: Endpoint | None = None) -> MacConfig:
    model: dict = {
        "alias": "frontier",
        "engine": "omlx",
        "model": "publisher/upstream-model",
    }
    if endpoint is not None:
        model["capabilities"] = [endpoint.value]
    return MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"omlx": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [model],
        }
    )


def test_lan_inference_allows_optional_authentication(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("INFERENCE_API_KEY", raising=False)
    config = _config(tmp_path)
    config.server.inference_bind = "0.0.0.0"

    validate_exposure(config)


def test_lan_control_still_requires_authentication(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    config = _config(tmp_path)
    config.server.control_bind = "0.0.0.0"

    with pytest.raises(
        RuntimeConfigurationError,
        match="non-loopback control bind requires an admin password",
    ):
        validate_exposure(config)


def _adapters() -> dict[EngineName, FakeAdapter]:
    return {engine: FakeAdapter(engine) for engine in EngineName}


async def _record_exclusive_cleanup_proof(
    runtime: NativeRuntime,
    install: InstallRecord,
    *,
    location_name: str,
    location_path: Path,
    files: tuple[Path, ...],
) -> None:
    binding = (
        await runtime.mac_inventory_index.reconcile_storage(
            [(location_name, str(location_path), None, None)]
        )
    )[location_name]
    owned_files = tuple(
        OwnedFile(
            path=path.relative_to(Path(install.destination)).as_posix(),
            size_bytes=path.stat().st_size,
            sha256="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in files
    )
    binding_fields = {
        "storage_location_id": binding.storage_location_id,
        "storage_binding_generation": binding.binding_generation,
        "storage_lexical_root": binding.exact_path,
        "lexical_destination": install.destination,
        "storage_volume_uuid": binding.volume_uuid,
        "storage_scope_id": binding.scope_id,
    }
    runtime.installer.store.record_exclusive_managed_proof(
        install.id,
        ExclusiveManagedProof(
            installation_id=install.id,
            **binding_fields,
            destination_binding_digest=destination_binding_digest(
                **binding_fields
            ),
            catalog_id="test-catalog",
            logical_model_id="test.logical-model",
            artifact_id="test.artifact",
            recipe_id="test.recipe",
            resolved_revision=install.revision or "",
            catalog_digest="sha256:" + "a" * 64,
            manifest_digest=owned_manifest_digest(owned_files),
            owned_files=owned_files,
            destination_state_before=DestinationStateBefore.ABSENT,
            destination_created_by_transaction=True,
            preexisting_entries=(),
            extra_entries=(),
            creation_transaction_id=str(uuid4()),
        ),
    )


@pytest.mark.parametrize(
    ("binding_change", "location_change"),
    [
        ({"binding_generation": 8}, {}),
        ({"exact_path": "/Volumes/Other/models"}, {}),
        ({"volume_uuid": "VOL-TWO"}, {}),
        ({"scope_id": "b" * 64}, {}),
        ({}, {"path": "/Volumes/Other/models"}),
        ({}, {"volume_uuid": "VOL-TWO"}),
        ({}, {"scope_id": "b" * 64}),
    ],
)
def test_cleanup_storage_binding_requires_exact_generation_path_volume_and_scope(
    binding_change: dict[str, object],
    location_change: dict[str, object],
) -> None:
    provenance = InstallationProvenance(
        installation_id="1084c4de-d0c2-49b0-a317-c08237ad6c69",
        storage_location_id="3c7ba6ca-95a0-4ac0-a73a-fda3bcd03002",
        storage_binding_generation=7,
        storage_lexical_root="/Volumes/Athena/models",
        storage_volume_uuid="VOL-ONE",
        storage_scope_id="a" * 64,
    )
    binding = StorageBinding(
        local_key="athena-models",
        storage_location_id=provenance.storage_location_id or "",
        binding_generation=7,
        exact_path="/Volumes/Athena/models",
        volume_uuid="VOL-ONE",
        scope_id="a" * 64,
    )
    location = StorageLocationConfig(
        name="athena-models",
        path="/Volumes/Athena/models",
        volume_uuid="VOL-ONE",
        scope_id="a" * 64,
    )

    assert _current_storage_binding_matches(
        provenance=provenance,
        binding=binding,
        location=location,
    )
    assert not _current_storage_binding_matches(
        provenance=provenance,
        binding=replace(binding, **binding_change),
        location=location.model_copy(update=location_change),
    )


def _image_config(tmp_path, *, timeout: float = 1800) -> MacConfig:
    return MacConfig.model_validate(
        {
            "server": {
                "idle_unload_seconds": None,
                "image_request_timeout_seconds": timeout,
            },
            "engines": {"mflux": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [
                {
                    "alias": "qwen-image",
                    "engine": "mflux",
                    "model": "Qwen/Qwen-Image",
                    "kind": "image",
                    "image": {
                        "family": "qwen-image",
                        "num_inference_steps": 12,
                        "guidance_scale": 2.5,
                    },
                }
            ],
        }
    )


def test_managed_install_roles_are_canonical_and_engine_scoped() -> None:
    assert _validated_install_capabilities(
        engine=EngineName.LLAMA_CPP,
        requested=None,
        suggested_role="generation",
        has_projector=True,
    ) == frozenset(
        {
            Endpoint.CHAT_COMPLETIONS,
            Endpoint.COMPLETIONS,
            Endpoint.RESPONSES,
        }
    )
    generation_with_messages = frozenset(
        {
            Endpoint.CHAT_COMPLETIONS,
            Endpoint.COMPLETIONS,
            Endpoint.RESPONSES,
            Endpoint.MESSAGES,
        }
    )
    assert _validated_install_capabilities(
        engine=EngineName.LLAMA_CPP,
        requested=set(generation_with_messages),
        suggested_role=None,
        has_projector=False,
    ) == generation_with_messages
    assert _validated_install_capabilities(
        engine=EngineName.OMLX,
        requested=None,
        suggested_role="generation",
        has_projector=False,
    ) == generation_with_messages
    assert _validated_install_capabilities(
        engine=EngineName.DS4,
        requested=None,
        suggested_role=None,
        has_projector=False,
    ) == generation_with_messages
    assert _validated_install_capabilities(
        engine=EngineName.LLAMA_CPP,
        requested=None,
        suggested_role="embeddings",
        has_projector=False,
    ) == frozenset({Endpoint.EMBEDDINGS})
    assert _validated_install_capabilities(
        engine=EngineName.MFLUX,
        requested=None,
        suggested_role=None,
        has_projector=False,
    ) == frozenset({Endpoint.IMAGES_GENERATIONS})

    with pytest.raises(ValueError, match="Generation role"):
        _validated_install_capabilities(
            engine=EngineName.LLAMA_CPP,
            requested={Endpoint.EMBEDDINGS},
            suggested_role=None,
            has_projector=True,
        )
    with pytest.raises(ValueError, match="require one supported model role"):
        _validated_install_capabilities(
            engine=EngineName.DS4,
            requested={Endpoint.EMBEDDINGS},
            suggested_role=None,
            has_projector=False,
        )
    with pytest.raises(ValueError, match="require one supported model role"):
        _validated_install_capabilities(
            engine=EngineName.LLAMA_CPP,
            requested={Endpoint.CHAT_COMPLETIONS, Endpoint.EMBEDDINGS},
            suggested_role=None,
            has_projector=False,
        )


@pytest.mark.asyncio
async def test_model_library_search_unifies_all_engine_catalogs(
    tmp_path,
    monkeypatch,
) -> None:
    seen: list[EngineName] = []
    downloads = {
        EngineName.LLAMA_CPP: 10,
        EngineName.OMLX: 40,
        EngineName.DS4: 30,
        EngineName.MFLUX: 20,
    }

    def fake_search(query, *, engine, limit):
        assert query == "qwen"
        assert limit == 7
        seen.append(engine)
        return [
            LibraryModel(
                repo_id=f"owner/{engine.value}",
                engine=engine.value,
                display_name=engine.value,
                model_kind="image" if engine == EngineName.MFLUX else "language",
                compatibility="verified",
                compatibility_reason="Supported by the selected engine.",
                downloads=downloads[engine],
            )
        ]

    monkeypatch.setattr("mnemosyne_macos.app.search_models", fake_search)
    runtime = SimpleNamespace(config=_config(tmp_path))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        unified = await client.get(
            "/manager/model-library/search",
            params={"q": "qwen", "limit": 7},
        )
        assert unified.status_code == 200
        assert set(seen) == set(ACTIVE_ENGINE_NAMES)
        assert [item["engine"] for item in unified.json()["models"]] == [
            "omlx",
            "ds4",
            "mflux",
            "llama.cpp",
        ]

        seen.clear()
        filtered = await client.get(
            "/manager/model-library/search",
            params={"q": "qwen", "limit": 7, "engine": "llama.cpp"},
        )
        assert filtered.status_code == 200
        assert seen == [EngineName.LLAMA_CPP]

        retired = await client.get(
            "/manager/model-library/search",
            params={"q": "qwen", "engine": "mlxcel"},
        )
        assert retired.status_code == 410
        assert seen == [EngineName.LLAMA_CPP]

        retired_install = await client.post(
            "/manager/model-library/installs",
            json={"repo_id": "owner/model", "engine": "mistral.rs"},
        )
        assert retired_install.status_code == 400
        assert "retired on macOS" in retired_install.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_non_streaming_proxy_rewrites_model_strips_credentials_and_records_usage(
    tmp_path,
) -> None:
    seen: dict = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        seen["authorization"] = request.headers.get("authorization")
        seen["api-key"] = request.headers.get("api-key")
        seen["x-api-key"] = request.headers.get("x-api-key")
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(
            200,
            headers={"X-Mnemosyne-Error": "node_busy"},
            json={
                "id": "chatcmpl-1",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        )

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer outer-secret",
                "Api-Key": "azure-outer-secret",
                "X-Api-Key": "anthropic-outer-secret",
                "Cookie": "outer=secret",
            },
            json={
                "model": "frontier",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                "temperature": 0.25,
            },
        )
        assert response.status_code == 200
        assert "x-mnemosyne-error" not in response.headers
        assert seen["json"]["model"] == "publisher/upstream-model"
        assert seen["json"]["messages"][0]["content"][0]["text"] == "hi"
        assert seen["json"]["temperature"] == 0.25
        assert seen["authorization"] is None
        assert seen["api-key"] is None
        assert seen["x-api-key"] is None
        assert seen["cookie"] is None
        assert (await runtime.coordinator.status()).inflight == 0

        rows = await runtime.usage.list_usage()
        assert len(rows) == 1
        assert rows[0]["alias"] == "frontier"
        assert rows[0]["backend"] == "omlx"
        assert rows[0]["total_tokens"] == 6
        performance = runtime.performance.snapshot()
        assert performance["sample_count"] == 1
        assert performance["by_model"][0]["alias"] == "frontier"
        assert performance["by_model"][0]["cold_starts"] == 1
        assert performance["recent"][0]["status_code"] == 200
        assert performance["recent"][0]["admission_ms"] is not None
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()

@pytest.mark.parametrize(
    ("engine", "budget_field"),
    [
        (EngineName.OMLX, "thinking_budget"),
        (EngineName.LLAMA_CPP, "reasoning_budget_tokens"),
    ],
)
@pytest.mark.asyncio
async def test_proxy_translates_qwen_controls_for_the_resolved_engine(
    tmp_path,
    engine: EngineName,
    budget_field: str,
) -> None:
    seen: dict = {}
    engine_config_key = (
        "llama_cpp" if engine == EngineName.LLAMA_CPP else engine.value
    )

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )

    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {engine_config_key: {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [
                {
                    "alias": "qwen38",
                    "engine": engine.value,
                    "model": "publisher/qwen38",
                }
            ],
        }
    )
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        config,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen38",
                "messages": [{"role": "user", "content": "Solve this."}],
                "reasoning_effort": "medium",
                "thinking_budget": 4096,
                "enable_thinking": True,
                "preserve_thinking": False,
            },
        )

        assert response.status_code == 200, response.text
        assert seen["model"] == (
            "qwen38" if engine == EngineName.LLAMA_CPP else "publisher/qwen38"
        )
        assert seen[budget_field] == 4096
        assert seen["chat_template_kwargs"] == {
            "enable_thinking": True,
            "preserve_thinking": False,
            "reasoning_effort": "medium",
        }
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_failed_benchmark_winner_falls_back_before_upstream_work(
    tmp_path,
) -> None:
    class FailingLoadAdapter(FakeAdapter):
        async def runtime_fingerprint(self, *, deadline: Deadline) -> str:
            del deadline
            return "ds4-v1"

        async def load(
            self,
            target: ResolvedTarget,
            *,
            deadline: Deadline,
        ) -> LoadedHandle:
            del target, deadline
            self.loads += 1
            raise AdapterError(self.engine, "load", "candidate rejected the model")

    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {
                "omlx": {"enabled": True},
                "ds4": {"enabled": True},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [
                {
                    "alias": "frontier",
                    "engine": "omlx",
                    "model": "publisher/upstream-model",
                    "alternatives": [
                        {
                            "engine": "ds4",
                            "model": str(tmp_path / "deepseek-v4.gguf"),
                        }
                    ],
                    "selection": {
                        "mode": "benchmark",
                        "objective": "latency",
                        "minimum_samples": 1,
                        "allow_preview": True,
                    },
                }
            ],
        }
    )
    adapters = _adapters()
    failing = FailingLoadAdapter(EngineName.DS4)
    adapters[EngineName.DS4] = failing
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "ok"}}
                    ],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                },
            )
        )
    )
    runtime = NativeRuntime(
        config,
        adapters=adapters,
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    primary, alternative = runtime.profile_candidates["frontier"]
    revision = candidate_set_fingerprint((primary, alternative))
    runtime._runtime_fingerprints = {  # noqa: SLF001
        EngineName.OMLX: "omlx-v1",
        EngineName.DS4: "ds4-v1",
    }
    for target, runtime_id, ttft in (
        (primary, "omlx-v1", 200.0),
        (alternative, "ds4-v1", 50.0),
    ):
        runtime.benchmark_store.record(
            BenchmarkRecord(
                created_at=time.time(),
                alias="frontier",
                endpoint="chat/completions",
                engine=target.key.engine.value,
                target_fingerprint=target_fingerprint(target),
                runtime_fingerprint=runtime_id,
                system_fingerprint=system_fingerprint(),
                config_revision=revision,
                suite_version=BENCHMARK_SUITE_VERSION,
                successful_samples=1,
                failed_samples=0,
                p50_ttft_ms=ttft,
                p50_total_ms=500,
                p50_output_tps=20,
            )
        )
    runtime._reload_benchmark_records("frontier")  # noqa: SLF001
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    request = {
        "model": "frontier",
        "messages": [{"role": "user", "content": "hi"}],
    }
    try:
        first = await client.post("/v1/chat/completions", json=request)
        second = await client.post("/v1/chat/completions", json=request)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert failing.loads == 1
        assert adapters[EngineName.OMLX].loads == 1
        assert runtime.benchmark_store.list(alias="frontier") == []
        status = await runtime.coordinator.status()
        assert status.state == CoordinatorState.READY
        assert status.resident_engine == EngineName.OMLX
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_user_pin_routes_to_the_selected_engine_without_benchmark_evidence(
    tmp_path,
) -> None:
    seen: dict = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )

    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {
                "omlx": {"enabled": True},
                "ds4": {"enabled": True},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [
                {
                    "alias": "frontier",
                    "engine": "omlx",
                    "model": "publisher/upstream-model",
                    "alternatives": [
                        {
                            "engine": "ds4",
                            "model": str(tmp_path / "deepseek-v4.gguf"),
                        }
                    ],
                    "selection": {
                        "mode": "pinned",
                        "pinned_engine": "ds4",
                    },
                }
            ],
        }
    )
    adapters = _adapters()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        config,
        adapters=adapters,
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "frontier",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200, response.text
        assert seen == {
            "host": "ds4.test",
            "body": {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        }
        assert adapters[EngineName.DS4].loads == 1
        assert adapters[EngineName.OMLX].loads == 0
        rows = await runtime.usage.list_usage()
        assert rows[0]["backend"] == "ds4"
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_image_proxy_normalizes_request_and_does_not_record_usage(tmp_path) -> None:
    seen: dict = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "created": 1,
                "data": [{"b64_json": "cG5n"}],
                "usage": {"prompt_tokens": 999, "total_tokens": 999},
            },
        )

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
    runtime = NativeRuntime(
        _image_config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/v1/images/generations",
            json={"model": "qwen-image", "prompt": "a local image", "seed": 5},
        )
        assert response.status_code == 200
        assert seen["model"] == "qwen-image"
        assert seen["width"] == 1024
        assert seen["height"] == 1024
        assert seen["num_inference_steps"] == 12
        assert seen["guidance_scale"] == 2.5
        assert await runtime.usage.list_usage() == []
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_image_timeout_unloads_worker_and_releases_lease(tmp_path) -> None:
    class SlowProxyClient:
        def build_request(self, **kwargs) -> httpx.Request:
            return httpx.Request(
                kwargs["method"],
                kwargs["url"],
                headers=kwargs.get("headers"),
                content=kwargs.get("content"),
            )

        async def send(self, _request: httpx.Request, *, stream: bool):
            assert stream is True
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    adapters = _adapters()
    runtime = NativeRuntime(
        _image_config(tmp_path, timeout=0.01),
        adapters=adapters,
        proxy_client=SlowProxyClient(),  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/v1/images/generations",
            json={"model": "qwen-image", "prompt": "timeout"},
        )
        assert response.status_code == 504
        status = await runtime.coordinator.status()
        assert status.inflight == 0
        assert status.resident_alias is None
        assert adapters[EngineName.MFLUX].residents == []
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_cancelled_buffered_image_body_fences_and_unloads_before_successor(
    tmp_path,
) -> None:
    body_started = asyncio.Event()
    body_block = asyncio.Event()
    closed = asyncio.Event()

    class BufferedUpstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "application/json"})

        async def aread(self) -> bytes:
            body_started.set()
            await body_block.wait()
            return b'{"data":[]}'

        async def aclose(self) -> None:
            closed.set()

    class ProxyClient:
        def build_request(self, **kwargs) -> httpx.Request:
            return httpx.Request(
                kwargs["method"],
                kwargs["url"],
                headers=kwargs.get("headers"),
                content=kwargs.get("content"),
            )

        async def send(self, _request: httpx.Request, *, stream: bool):
            assert stream is True
            return BufferedUpstream()

    adapters = _adapters()
    runtime = NativeRuntime(
        _image_config(tmp_path),
        adapters=adapters,
        proxy_client=ProxyClient(),  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    app = create_inference_app(runtime)
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", None) == "/v1/images/generations"
    )
    body = json.dumps({"model": "qwen-image", "prompt": "cancel"}).encode()
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/images/generations",
            "raw_path": b"/v1/images/generations",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 1240),
        },
        receive,
    )
    request_task = asyncio.create_task(route.endpoint(request))
    try:
        await asyncio.wait_for(body_started.wait(), timeout=1)
        successor = asyncio.create_task(
            runtime.coordinator.acquire(runtime.profiles["qwen-image"])
        )
        deadline = asyncio.get_running_loop().time() + 1.0
        while (
            (await runtime.coordinator.status()).queued != 1
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.001)

        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        with pytest.raises(CoordinatorError, match="abort in progress"):
            await successor

        status = await runtime.coordinator.status()
        assert closed.is_set()
        assert status.state == CoordinatorState.IDLE
        assert status.inflight == 0
        assert status.queued == 0
        assert status.resident_alias is None
        assert adapters[EngineName.MFLUX].residents == []
    finally:
        body_block.set()
        if not request_task.done():
            request_task.cancel()
        await runtime.stop()


@pytest.mark.asyncio
async def test_failed_streaming_image_body_fences_and_unloads_before_successor(
    tmp_path,
) -> None:
    body_failed = asyncio.Event()
    closed = asyncio.Event()

    class StreamingUpstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_bytes(self):
            yield b'data: {"data":[{"b64_json":"cG5n"}]}\n\n'
            await body_failed.wait()
            raise httpx.ReadError("synthetic image stream failure")

        async def aclose(self) -> None:
            closed.set()

    class ProxyClient:
        def build_request(self, **kwargs) -> httpx.Request:
            return httpx.Request(
                kwargs["method"],
                kwargs["url"],
                headers=kwargs.get("headers"),
                content=kwargs.get("content"),
            )

        async def send(self, _request: httpx.Request, *, stream: bool):
            assert stream is True
            return StreamingUpstream()

    adapters = _adapters()
    runtime = NativeRuntime(
        _image_config(tmp_path),
        adapters=adapters,
        proxy_client=ProxyClient(),  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    app = create_inference_app(runtime)
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", None) == "/v1/images/generations"
    )
    body = json.dumps({"model": "qwen-image", "prompt": "stream"}).encode()
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/images/generations",
            "raw_path": b"/v1/images/generations",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 1240),
        },
        receive,
    )
    response = await route.endpoint(request)
    assert isinstance(response, StreamingResponse)
    iterator = response.body_iterator.__aiter__()
    assert b'"b64_json":"cG5n"' in await iterator.__anext__()
    successor = asyncio.create_task(
        runtime.coordinator.acquire(runtime.profiles["qwen-image"])
    )
    try:
        for _ in range(50):
            if (await runtime.coordinator.status()).queued == 1:
                break
            await asyncio.sleep(0)
        body_failed.set()

        with pytest.raises(httpx.ReadError, match="synthetic image stream"):
            await iterator.__anext__()
        with pytest.raises(CoordinatorError, match="abort in progress"):
            await successor

        status = await runtime.coordinator.status()
        assert closed.is_set()
        assert status.state == CoordinatorState.IDLE
        assert status.inflight == 0
        assert status.queued == 0
        assert status.resident_alias is None
        assert adapters[EngineName.MFLUX].residents == []
    finally:
        body_failed.set()
        if not successor.done():
            successor.cancel()
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_update_control_routes_use_coordinator_barrier(tmp_path) -> None:
    class FakeUpdateManager:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.current = "0.9.0"
            self.activation_threads: list[int] = []
            self.rollback_threads: list[int] = []
            self.block_next_activation = False
            self.activation_started = threading.Event()
            self.activation_release = threading.Event()
            self.block_next_rollback = False
            self.rollback_started = threading.Event()
            self.rollback_release = threading.Event()

        async def check(self, *, refresh: bool = True) -> dict:
            self.events.append(f"check:{refresh}")
            return {
                "channel": "stable",
                "manifest_url": None,
                "checked_at": 1,
                "core_protocol": 1,
                "engines": [],
            }

        async def prepare(
            self,
            engine: str,
            version: str | None = None,
            *,
            channel: str | None = None,
        ):
            suffix = f":{channel}" if channel is not None else ""
            self.events.append(f"prepare:{engine}:{version}{suffix}")
            if version == "corrupt":
                raise RuntimeUpdateError("SHA-256 verification failed")
            return SimpleNamespace(
                release=SimpleNamespace(
                    engine=engine,
                    version="1.0.0",
                    source_revision="abc",
                ),
                runtime=SimpleNamespace(
                    version="1.0.0",
                    source_revision="abc",
                    channel=channel or "official",
                ),
            )

        def activate(self, prepared):
            self.activation_threads.append(threading.get_ident())
            if self.block_next_activation:
                self.block_next_activation = False
                self.activation_started.set()
                if not self.activation_release.wait(1.0):
                    raise RuntimeError("activation test release timed out")
            self.events.append(f"activate:{prepared.release.engine}")
            self.current = "1.0.0"
            return SimpleNamespace(
                engine=prepared.release.engine,
                version="1.0.0",
                source_revision="abc",
                channel=getattr(prepared.runtime, "channel", "official"),
                root=tmp_path / "runtime",
            )

        def rollback(self, engine: str):
            self.rollback_threads.append(threading.get_ident())
            if self.block_next_rollback:
                self.block_next_rollback = False
                self.rollback_started.set()
                if not self.rollback_release.wait(1.0):
                    raise RuntimeError("rollback test release timed out")
            self.events.append(f"rollback:{engine}")
            self.current = "0.9.0"
            return SimpleNamespace(
                engine=engine,
                version="0.9.0",
                source_revision="old",
                root=tmp_path / "runtime-old",
            )

        def active_version(self, _engine: str) -> str:
            return self.current

        def record_lifecycle(self, **values) -> dict:
            self.events.append(
                f"lifecycle:{values['action']}:{values['outcome']}"
            )
            return dict(values)

        def lifecycle_evidence(self) -> dict:
            return {
                "schema_version": 1,
                "valid": True,
                "dropped_events": 0,
                "events": [],
            }

        async def installed_status(self) -> dict:
            return {
                "mflux": {
                    "installed": True,
                    "version": self.current,
                    "revision": None,
                    "path": str(tmp_path),
                }
            }

        async def aclose(self) -> None:
            return None

    updates = FakeUpdateManager()
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        update_manager=updates,  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        event_loop_thread = threading.get_ident()
        checked = await client.post("/manager/runtime-updates/check")
        assert checked.status_code == 200
        evidence = await client.get("/manager/runtime-updates/evidence")
        assert evidence.status_code == 200
        assert evidence.json()["journal"]["valid"] is True
        updates.block_next_activation = True
        activation_timer = threading.Timer(0.5, updates.activation_release.set)
        activation_timer.start()
        activation_started_at = time.monotonic()
        installed_request = asyncio.create_task(
            client.post(
                "/manager/runtime-updates/mflux/install",
                json={"version": "1.0.0"},
            )
        )
        while not updates.activation_started.is_set():
            await asyncio.sleep(0)
        activation_elapsed = time.monotonic() - activation_started_at
        updates.activation_release.set()
        installed = await installed_request
        activation_timer.cancel()
        activation_timer.join()
        assert activation_elapsed < 0.2
        assert installed.status_code == 200
        assert installed.json()["activated"]["version"] == "1.0.0"
        updates.block_next_rollback = True
        rollback_timer = threading.Timer(0.5, updates.rollback_release.set)
        rollback_timer.start()
        rollback_started_at = time.monotonic()
        rollback_request = asyncio.create_task(
            client.post("/manager/runtime-updates/mflux/rollback")
        )
        while not updates.rollback_started.is_set():
            await asyncio.sleep(0)
        rollback_elapsed = time.monotonic() - rollback_started_at
        updates.rollback_release.set()
        rolled_back = await rollback_request
        rollback_timer.cancel()
        rollback_timer.join()
        assert rollback_elapsed < 0.2
        assert rolled_back.status_code == 200
        assert rolled_back.json()["activated"]["rollback"] is True
        preview = await client.post(
            "/manager/runtime-updates/ds4/install",
            json={"version": "1.0.0", "channel": "glm-5.3-flash"},
        )
        assert preview.status_code == 200
        assert preview.json()["activated"]["channel"] == "glm-5.3-flash"
        rejected = await client.post(
            "/manager/runtime-updates/mflux/install",
            json={"version": "corrupt"},
        )
        assert rejected.status_code == 400
        assert all(
            thread != event_loop_thread for thread in updates.activation_threads
        )
        assert len(updates.rollback_threads) == 1
        assert updates.rollback_threads[0] != event_loop_thread
        assert updates.events == [
            "check:True",
            "lifecycle:install_requested:started",
            "prepare:mflux:1.0.0",
            "lifecycle:prepared:succeeded",
            "activate:mflux",
            "lifecycle:activated:succeeded",
            "check:False",
            "lifecycle:rollback_requested:started",
            "rollback:mflux",
            "lifecycle:rolled_back:succeeded",
            "check:False",
            "lifecycle:install_requested:started",
            "prepare:ds4:1.0.0:glm-5.3-flash",
            "lifecycle:prepared:succeeded",
            "activate:ds4",
            "lifecycle:activated:succeeded",
            "check:False",
            "lifecycle:install_requested:started",
            "prepare:mflux:corrupt",
            "lifecycle:install_rejected:failed",
        ]
        status = await runtime.coordinator.status()
        assert status.state.value == "idle"
        assert status.resident_alias is None
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_supervised_omlx_update_drains_and_validates_before_reopening(
    tmp_path: Path,
) -> None:
    class FakeOMLXUpdateManager:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.current = "0.5.6"

        async def installed_status(self) -> dict:
            return {
                "omlx": {
                    "installed": True,
                    "version": self.current,
                    "revision": None,
                    "path": "/opt/homebrew/bin/omlx",
                }
            }

        async def upgrade_omlx_homebrew(
            self, version: str | None
        ) -> tuple[str, str]:
            self.events.append(f"upgrade:{version}")
            self.current = "0.5.7"
            return self.current, "/opt/homebrew/bin/omlx"

        async def check(self, *, refresh: bool = True) -> dict:
            self.events.append(f"check:{refresh}")
            return {
                "channel": "official",
                "manifest_url": None,
                "checked_at": 1,
                "core_protocol": 1,
                "engines": [],
            }

        def record_lifecycle(self, **values) -> dict:
            self.events.append(
                f"lifecycle:{values['action']}:{values['outcome']}"
            )
            return dict(values)

        async def aclose(self) -> None:
            return None

    updates = FakeOMLXUpdateManager()
    adapters = _adapters()
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=adapters,
        update_manager=updates,  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/manager/runtime-updates/omlx/install",
            json={"version": "0.5.7"},
        )
        assert response.status_code == 200
        assert response.json()["activated"] == {
            "engine": "omlx",
            "version": "0.5.7",
            "source_revision": None,
            "path": "/opt/homebrew/bin/omlx",
            "external_owner": "homebrew",
        }
        assert updates.events == [
            "lifecycle:external_update_requested:started",
            "upgrade:0.5.7",
            "lifecycle:external_updated:succeeded",
            "check:False",
        ]
        status = await runtime.coordinator.status()
        assert status.state == CoordinatorState.IDLE
        assert status.accepting is True
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_upstream_close_failure_cannot_leak_model_lease(tmp_path) -> None:
    class FailingCloseResponse:
        status_code = 200
        headers = httpx.Headers({"content-type": "application/json"})

        async def aread(self) -> bytes:
            return json.dumps(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ).encode()

        async def aclose(self) -> None:
            raise RuntimeError("synthetic close failure")

    class ProxyClient:
        def build_request(self, **kwargs) -> httpx.Request:
            return httpx.Request(
                kwargs["method"],
                kwargs["url"],
                headers=kwargs.get("headers"),
                content=kwargs.get("content"),
                params=kwargs.get("params"),
            )

        async def send(self, _request: httpx.Request, *, stream: bool):
            assert stream is True
            return FailingCloseResponse()

    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=ProxyClient(),  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "frontier", "messages": []},
        )
        assert response.status_code == 200
        assert (await runtime.coordinator.status()).inflight == 0
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_cancelled_stream_with_close_failure_releases_model_lease(tmp_path) -> None:
    stream_block = asyncio.Event()

    class StreamingUpstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            await stream_block.wait()

        async def aclose(self) -> None:
            raise RuntimeError("synthetic close failure")

    class ProxyClient:
        def build_request(self, **kwargs) -> httpx.Request:
            return httpx.Request(
                kwargs["method"],
                kwargs["url"],
                headers=kwargs.get("headers"),
                content=kwargs.get("content"),
                params=kwargs.get("params"),
            )

        async def send(self, _request: httpx.Request, *, stream: bool):
            assert stream is True
            return StreamingUpstream()

    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=ProxyClient(),  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    app = create_inference_app(runtime)
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", None) == "/v1/chat/completions"
    )
    request_body = json.dumps(
        {"model": "frontier", "messages": [], "stream": True}
    ).encode()
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 1240),
        },
        receive,
    )
    response = await route.endpoint(request)
    assert isinstance(response, StreamingResponse)
    iterator = response.body_iterator.__aiter__()
    assert b'"content":"hi"' in await iterator.__anext__()

    blocked_read = asyncio.create_task(iterator.__anext__())
    await asyncio.sleep(0)
    blocked_read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked_read
    await asyncio.sleep(0)

    assert (await runtime.coordinator.status()).inflight == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_stream_response_double_cancel_before_iteration_finishes_cleanup(
    tmp_path,
) -> None:
    body_iterated = asyncio.Event()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    closed = asyncio.Event()

    class StreamingUpstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_bytes(self):
            body_iterated.set()
            yield b"unreachable"

        async def aclose(self) -> None:
            close_started.set()
            await allow_close.wait()
            closed.set()

    class ProxyClient:
        def build_request(self, **kwargs) -> httpx.Request:
            return httpx.Request(
                kwargs["method"],
                kwargs["url"],
                headers=kwargs.get("headers"),
                content=kwargs.get("content"),
                params=kwargs.get("params"),
            )

        async def send(self, _request: httpx.Request, *, stream: bool):
            assert stream is True
            return StreamingUpstream()

    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=ProxyClient(),  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    app = create_inference_app(runtime)
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", None) == "/v1/chat/completions"
    )
    request_body = json.dumps(
        {"model": "frontier", "messages": [], "stream": True}
    ).encode()
    delivered = False

    async def request_receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {
                "type": "http.request",
                "body": request_body,
                "more_body": False,
            }
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 1240),
    }
    response = await route.endpoint(Request(scope, request_receive))
    assert isinstance(response, StreamingResponse)
    assert (await runtime.coordinator.status()).inflight == 1

    response_started = asyncio.Event()

    async def response_receive() -> dict:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            response_started.set()
            await asyncio.Event().wait()

    response_task = asyncio.create_task(
        response(scope, response_receive, send)
    )
    try:
        await response_started.wait()
        assert not body_iterated.is_set()
        response_task.cancel()
        await close_started.wait()
        response_task.cancel()
        await asyncio.sleep(0)
        assert not response_task.done()

        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await response_task

        assert not body_iterated.is_set()
        assert closed.is_set()
        assert (await runtime.coordinator.status()).inflight == 0
    finally:
        allow_close.set()
        if not response_task.done():
            response_task.cancel()
        await runtime.stop()


@pytest.mark.asyncio
async def test_default_native_proxy_client_ignores_ambient_proxies(
    tmp_path,
) -> None:
    runtime = NativeRuntime(_config(tmp_path), adapters=_adapters())
    try:
        assert runtime.proxy_client._trust_env is False
    finally:
        await runtime.start(raise_on_degraded=True)
        await runtime.stop()


@pytest.mark.asyncio
async def test_streaming_proxy_hides_forced_usage_event_but_persists_it(tmp_path) -> None:
    seen: dict = {}
    stream = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":5,'
        b'"completion_tokens":3,"total_tokens":8}}\n\n'
        b"data: [DONE]\n\n"
    )

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=stream,
            headers={"content-type": "text/event-stream"},
        )

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "frontier", "messages": [], "stream": True},
        )
        assert response.status_code == 200
        assert seen["json"]["stream_options"] == {"include_usage": True}
        assert b'"usage"' not in response.content
        assert b"data: [DONE]" in response.content

        rows = await runtime.usage.list_usage()
        assert len(rows) == 1
        assert rows[0]["streamed"] == 1
        assert rows[0]["total_tokens"] == 8
        assert (await runtime.coordinator.status()).inflight == 0
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_full_usage_outbox_rejects_before_load_or_upstream_work(tmp_path) -> None:
    calls = 0

    def upstream_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    config = _config(tmp_path)
    config.token_sidecar.max_outbox_rows = 1
    adapters = _adapters()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        config,
        adapters=adapters,
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    await runtime.record_usage(
        UsageEvent(
            usage=NormalizedUsage(1, 1, 2),
            endpoint="/v1/chat/completions",
            engine="omlx",
            event_id="already-pending",
        )
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "frontier", "messages": []},
        )

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "usage_outbox_full"
        assert calls == 0
        assert all(adapter.loads == 0 for adapter in adapters.values())
        assert runtime.usage.store.count_outbox() == 1
        assert runtime.usage.store.count_request_usage() == 1
        assert runtime.usage.store.count_active_reservations() == 0
        assert (await runtime.coordinator.status()).inflight == 0
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_non_streaming_usage_failure_never_returns_upstream_success(
    tmp_path,
) -> None:
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "total_tokens": 6,
                    },
                },
            )
        )
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)

    async def fail_record(_event: UsageEvent) -> None:
        raise RuntimeError("injected durable usage failure")

    runtime.record_usage = fail_record  # type: ignore[method-assign]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "frontier", "messages": []},
        )

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "usage_persistence_failed"
        assert b'"content":"done"' not in response.content
        assert runtime.usage.store.count_active_reservations() == 0
        assert (await runtime.coordinator.status()).inflight == 0
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_stream_terminal_is_withheld_when_usage_commit_fails(tmp_path) -> None:
    stream = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":5,'
        b'"completion_tokens":3,"total_tokens":8}}\n\n'
        b"data: [DONE]\n\n"
    )
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=stream,
                headers={"content-type": "text/event-stream"},
            )
        )
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)

    async def fail_record(_event: UsageEvent) -> None:
        raise RuntimeError("injected durable usage failure")

    runtime.record_usage = fail_record  # type: ignore[method-assign]
    app = create_inference_app(runtime)
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", None) == "/v1/chat/completions"
    )
    body = json.dumps(
        {"model": "frontier", "messages": [], "stream": True}
    ).encode()
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 1240),
        },
        receive,
    )
    try:
        response = await route.endpoint(request)
        iterator = response.body_iterator.__aiter__()
        first = await iterator.__anext__()
        assert b'"content":"hi"' in first
        assert b"[DONE]" not in first
        with pytest.raises(HTTPException) as failed:
            await iterator.__anext__()
        assert failed.value.status_code == 503
        assert failed.value.detail["code"] == "usage_persistence_failed"
        assert runtime.usage.store.count_active_reservations() == 0
        assert (await runtime.coordinator.status()).inflight == 0
    finally:
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_fleet_route_uuid_is_the_stable_usage_event_id(
    tmp_path,
    monkeypatch,
) -> None:
    route_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("FLEET_INFERENCE_API_KEY", "dispatch-secret")
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "total_tokens": 6,
                    },
                },
            )
        )
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer dispatch-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
            json={"model": "frontier", "messages": []},
        )

        assert response.status_code == 200, response.text
        rows = await runtime.usage.list_usage()
        assert len(rows) == 1
        assert rows[0]["event_id"] == route_id
        assert (await runtime.coordinator.status()).inflight == 0
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_fleet_route_reservation_fences_race_and_replay_before_engine_work(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLEET_INFERENCE_API_KEY", "dispatch-secret")
    route_id = "11111111-1111-4111-8111-111111111111"
    other_route_id = "22222222-2222-4222-8222-222222222222"
    stream_gate = asyncio.Event()
    upstream_calls = 0

    class StreamingUpstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            await stream_gate.wait()
            yield (
                b'data: {"choices":[],"usage":{"prompt_tokens":5,'
                b'"completion_tokens":3,"total_tokens":8}}\n\n'
            )
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            return None

    class ProxyClient:
        def build_request(self, **kwargs) -> httpx.Request:
            return httpx.Request(
                kwargs["method"],
                kwargs["url"],
                headers=kwargs.get("headers"),
                content=kwargs.get("content"),
                params=kwargs.get("params"),
            )

        async def send(self, _request: httpx.Request, *, stream: bool):
            nonlocal upstream_calls
            assert stream is True
            upstream_calls += 1
            return StreamingUpstream()

    config = _config(tmp_path)
    config.token_sidecar.max_outbox_rows = 1
    runtime = NativeRuntime(
        config,
        adapters=_adapters(),
        proxy_client=ProxyClient(),  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    app = create_inference_app(runtime)
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", None) == "/v1/chat/completions"
    )
    request_body = json.dumps(
        {"model": "frontier", "messages": [], "stream": True}
    ).encode()
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-mnemosyne-fleet-route", route_id.encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 1240),
        },
        receive,
    )
    response = await route.endpoint(request)
    iterator = response.body_iterator.__aiter__()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://mnemosyne.test",
    )
    try:
        assert b'"content":"hi"' in await iterator.__anext__()
        assert runtime.usage.store.reservation_state(route_id) == "started"

        duplicate_active = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer dispatch-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
            json={"model": "frontier", "messages": []},
        )
        stale_capacity = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer dispatch-secret",
                "X-Mnemosyne-Fleet-Route": other_route_id,
            },
            json={"model": "frontier", "messages": []},
        )

        assert duplicate_active.status_code == 409
        assert duplicate_active.json()["detail"]["code"] == "duplicate_fleet_route"
        assert stale_capacity.status_code == 429
        assert stale_capacity.headers["retry-after"] == "1"
        assert stale_capacity.headers["x-mnemosyne-error"] == "node_busy"
        assert stale_capacity.json()["detail"]["code"] == "node_busy"
        assert upstream_calls == 1

        stream_gate.set()
        assert [chunk async for chunk in iterator] == [b"data: [DONE]\n\n"]
        assert runtime.usage.store.reservation_state(route_id) is None
        assert runtime.usage.store.count_active_reservations() == 0
        assert runtime.usage.store.count_request_usage() == 1
        assert runtime.usage.store.peek_outbox(limit=1)[0]["event_id"] == route_id

        duplicate_completed = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer dispatch-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
            json={"model": "frontier", "messages": []},
        )
        assert duplicate_completed.status_code == 409
        assert (
            duplicate_completed.json()["detail"]["code"]
            == "duplicate_fleet_route"
        )
        assert upstream_calls == 1
    finally:
        stream_gate.set()
        with contextlib.suppress(Exception):
            await iterator.aclose()
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_fleet_route_releases_prework_error_and_fences_post_dispatch_error(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLEET_INFERENCE_API_KEY", "dispatch-secret")
    route_id = "33333333-3333-4333-8333-333333333333"
    upstream_calls = 0

    def upstream_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(500, json={"error": {"message": "unavailable"}})

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    headers = {
        "Authorization": "Bearer dispatch-secret",
        "X-Mnemosyne-Fleet-Route": route_id,
    }
    try:
        prework = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "missing", "messages": []},
        )
        assert prework.status_code == 404
        assert runtime.usage.store.reservation_state(route_id) is None
        assert runtime.usage.store.count_active_reservations() == 0
        assert upstream_calls == 0

        post_dispatch = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "frontier", "messages": []},
        )
        assert post_dispatch.status_code == 500
        assert runtime.usage.store.reservation_state(route_id) == "completed"
        assert runtime.usage.store.count_active_reservations() == 0
        assert upstream_calls == 1

        replay = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "frontier", "messages": []},
        )
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "duplicate_fleet_route"
        assert upstream_calls == 1
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_fleet_success_without_usage_fails_closed_but_standalone_is_unchanged(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLEET_INFERENCE_API_KEY", "dispatch-secret")
    route_id = "44444444-4444-4444-8444-444444444444"
    upstream_calls = 0

    def upstream_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "done"}}]},
        )

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        standalone = await client.post(
            "/v1/chat/completions",
            json={"model": "frontier", "messages": []},
        )
        assert standalone.status_code == 200
        assert standalone.json()["choices"][0]["message"]["content"] == "done"
        assert runtime.usage.store.count_active_reservations() == 0

        fleet = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer dispatch-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
            json={"model": "frontier", "messages": []},
        )
        assert fleet.status_code == 502
        assert fleet.json()["detail"]["code"] == "usage_missing"
        assert b'"content":"done"' not in fleet.content
        assert runtime.usage.store.reservation_state(route_id) == "completed"
        assert runtime.usage.store.count_active_reservations() == 0
        assert runtime.usage.store.count_request_usage() == 0

        replay = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer dispatch-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
            json={"model": "frontier", "messages": []},
        )
        assert replay.status_code == 409
        assert upstream_calls == 2
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal",
    [
        b"data: [DONE]\n\n",
        b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ],
)
async def test_fleet_stream_without_usage_withholds_terminal_and_fences_route(
    tmp_path,
    monkeypatch,
    terminal: bytes,
) -> None:
    monkeypatch.setenv("FLEET_INFERENCE_API_KEY", "dispatch-secret")
    route_id = "55555555-5555-4555-8555-555555555555"
    upstream_calls = 0
    stream = (
        b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":" second"}}]}\n\n'
        + terminal
    )

    def upstream_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(
            200,
            content=stream,
            headers={"content-type": "text/event-stream"},
        )

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    app = create_inference_app(runtime)
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", None) == "/v1/chat/completions"
    )
    body = json.dumps(
        {"model": "frontier", "messages": [], "stream": True}
    ).encode()
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer dispatch-secret"),
                (b"x-mnemosyne-fleet-route", route_id.encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 1240),
        },
        receive,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await route.endpoint(request)
        assert response.status_code == 200
        iterator = response.body_iterator.__aiter__()
        first = await iterator.__anext__()
        second = await iterator.__anext__()
        assert b'"content":"first"' in first
        assert b'"content":" second"' in second
        assert terminal not in first + second

        with pytest.raises(HTTPException) as failed:
            await iterator.__anext__()
        assert failed.value.status_code == 502
        assert failed.value.detail["code"] == "usage_missing"
        assert runtime.usage.store.reservation_state(route_id) == "completed"
        assert runtime.usage.store.count_active_reservations() == 0
        assert runtime.usage.store.count_request_usage() == 0
        assert runtime.usage.store.count_outbox() == 0
        assert (await runtime.coordinator.status()).inflight == 0

        replay = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer dispatch-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
            json={"model": "frontier", "messages": [], "stream": True},
        )
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "duplicate_fleet_route"
        assert upstream_calls == 1
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_control_plane_lists_loads_and_unloads_models(tmp_path) -> None:
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    config = _config(tmp_path)
    config.models[0].context = ModelContextConfig(
        mode="fixed",
        native_tokens=131_072,
        fixed_tokens=65_536,
    )
    runtime = NativeRuntime(
        config,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        listed = await client.get("/manager/models")
        assert listed.status_code == 200
        assert listed.json()["models"][0]["id"] == "frontier"
        assert listed.json()["models"][0]["max_model_len"] == 65_536
        assert listed.json()["models"][0]["context_window"] == {
            "mode": "fixed",
            "native_tokens": 131_072,
            "configured_tokens": 65_536,
            "effective_tokens": 65_536,
            "verified_tokens": None,
            "guaranteed_tokens": 65_536,
            "source": "configured-load",
            "confidence": "authoritative",
        }

        contexts = await client.get("/manager/contexts", params={"alias": "frontier"})
        assert contexts.status_code == 200
        assert contexts.json()["models"][0]["candidates"][0][
            "guaranteed_tokens"
        ] == 65_536

        loaded = await client.post("/manager/load", json={"model": "frontier"})
        assert loaded.status_code == 200
        assert loaded.json()["resident_alias"] == "frontier"

        unloaded = await client.post("/manager/unload")
        assert unloaded.status_code == 200
        assert unloaded.json()["resident_alias"] is None
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_readiness_exposes_bounded_actionable_health_and_release_tiers(
    tmp_path,
    monkeypatch,
) -> None:
    class InstalledUpdateManager:
        async def installed_status(self) -> dict:
            return {
                engine.value: {
                    "installed": True,
                    "version": "test-version",
                    "revision": None,
                    "path": f"/runtimes/{engine.value}",
                }
                for engine in EngineName
            }

        async def aclose(self) -> None:
            return None

    storage = tmp_path / "Models"
    storage.mkdir()
    payload = _config(tmp_path).model_dump(mode="json")
    payload["storage"] = {
        "default": "internal",
        "locations": [{"name": "internal", "path": str(storage)}],
    }
    adapters = _adapters()
    adapters[EngineName.LLAMA_CPP].service_state = ServiceState.STOPPED
    runtime = NativeRuntime(
        MacConfig.model_validate(payload),
        adapters=adapters,
        update_manager=InstalledUpdateManager(),  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        response = await client.get("/manager/readiness")

        assert response.status_code == 200
        readiness = response.json()
        assert readiness["product_version"] == "0.9.0"
        assert readiness["core"]["ready"] is True
        assert readiness["storage"][0]["available"] is True
        assert readiness["models"] == {"configured": 1, "callable": 1}
        assert readiness["ready_for_inference"] is True
        engines = {item["engine"]: item for item in readiness["engines"]}
        assert engines["llama.cpp"]["release_tier"] == "stable"
        assert engines["omlx"]["release_tier"] == "stable"
        assert engines["ds4"]["release_tier"] == "preview"
        assert engines["mflux"]["release_tier"] == "preview"
        assert engines["llama.cpp"]["ready"] is True
        assert engines["llama.cpp"]["service_state"] == "stopped"
        assert engines["omlx"]["ready"] is True

        monkeypatch.setenv("OMLX_ADMIN_SESSION", "menu-secret")
        runtime.startup_error = (
            "oMLX failed with session=menu-secret and "
            "postgresql://writer:db-password@nyx/token_sidecar"
        )
        runtime.usage.last_error = "https://writer:other-password@nyx/metrics"
        status_response = await client.get("/manager/status")
        status_payload = status_response.json()
        rendered = json.dumps(status_payload)
        assert "menu-secret" not in rendered
        assert "db-password" not in rendered
        assert "other-password" not in rendered
        assert "<redacted>" in rendered
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_control_self_test_uses_public_inference_path_and_verifies_usage(
    tmp_path,
) -> None:
    seen: dict = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-self-test",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Alpacas are gentle camelids.",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 6,
                    "total_tokens": 15,
                },
            },
        )

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    await runtime.self_test_client.aclose()
    runtime.self_test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://127.0.0.1:1240",
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        response = await client.post(
            "/manager/self-test",
            json={"model": "frontier"},
        )

        assert response.status_code == 200, response.text
        result = response.json()
        assert result["success"] is True
        assert result["endpoint"] == "/v1/chat/completions"
        assert result["release_tier"] == "stable"
        assert result["response_preview"] == "Alpacas are gentle camelids."
        assert result["usage"] == {
            "prompt_tokens": 9,
            "completion_tokens": 6,
            "total_tokens": 15,
        }
        assert result["usage_recorded"] is True
        assert result["cold_start"] is True
        assert result["unloaded_after"] is None
        assert "alpacas" in seen["request"]["messages"][0]["content"].lower()
        assert seen["request"]["max_tokens"] == 128
        rows = await runtime.usage.list_usage()
        assert rows[0]["alias"] == "frontier"
        assert rows[0]["total_tokens"] == 15
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_control_self_test_uses_configured_llama_projector_by_default(
    tmp_path,
) -> None:
    seen: dict = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "A red square on a light background.",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 24,
                    "completion_tokens": 9,
                    "total_tokens": 33,
                },
            },
        )

    payload = _config(tmp_path).model_dump(mode="json")
    payload["models"][0].update(
        {
            "engine": "llama.cpp",
            "model": "/models/vision.gguf",
            "load": {"projector_path": "/models/mmproj-vision.gguf"},
        }
    )
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        MacConfig.model_validate(payload),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    await runtime.self_test_client.aclose()
    runtime.self_test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://127.0.0.1:1240",
    )
    try:
        result = await runtime.self_test("frontier")

        assert result["vision"] is True
        content = seen["request"]["messages"][0]["content"]
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        image_url = content[1]["image_url"]["url"]
        assert image_url.startswith("data:image/png;base64,")
        assert len(image_url) > 200
        assert result["usage_recorded"] is True
    finally:
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_control_plane_reads_saves_and_applies_structured_configuration(tmp_path) -> None:
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    config = _config(tmp_path)
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        loaded = await client.get("/manager/config")
        assert loaded.status_code == 200
        assert loaded.json()["config"]["models"][0]["alias"] == "frontier"
        assert loaded.json()["restart_required"] is False
        assert loaded.json()["applied_revision"] == loaded.json()["revision"]
        revision = loaded.json()["revision"]

        edited = config.model_dump(mode="json")
        edited["models"].append(
            {"alias": "second-model", "engine": "omlx", "model": "publisher/second"}
        )
        saved = await client.put(
            "/manager/config", json={"config": edited, "revision": revision}
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["saved"] is True
        assert saved.json()["applied"] is True
        assert saved.json()["restart_required"] is False
        assert saved.json()["model_count"] == 2
        assert set(runtime.profiles) == {"frontier", "second-model"}
        assert len(load_config(config_path).models) == 2

        invalid_document = config_path.read_text(encoding="utf-8")
        stale = await client.put(
            "/manager/config", json={"config": edited, "revision": revision}
        )
        assert stale.status_code == 409
        assert "settings changed" in stale.text
        assert config_path.read_text(encoding="utf-8") == invalid_document

        edited["models"][1]["alias"] = "Not Valid"
        invalid = await client.put(
            "/manager/config",
            json={"config": edited, "revision": saved.json()["revision"]},
        )
        assert invalid.status_code == 400
        assert config_path.read_text(encoding="utf-8") == invalid_document
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_storage_health_keeps_other_locations_when_one_grant_is_stale(
    tmp_path,
) -> None:
    healthy = tmp_path / "Healthy"
    broken = tmp_path / "Broken"
    healthy.mkdir()
    broken.mkdir()
    payload = _config(tmp_path).model_dump(mode="json")
    payload["storage"] = {
        "default": "healthy",
        "locations": [
            {"name": "healthy", "path": str(healthy)},
            {"name": "broken", "path": str(broken)},
        ],
    }
    config = MacConfig.model_validate(payload)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)

    class _StorageProbe:
        async def inspect(
            self,
            path: str,
            *,
            name: str | None = None,
            expected_volume_uuid: str | None = None,
            scope_id: str | None = None,
        ) -> StorageStatus:
            del scope_id
            if name == "broken":
                raise FilesystemProbeError(
                    "macOS could not resolve the selected-folder bookmark; choose it again"
                )
            return StorageStatus(
                name=name,
                path=path,
                exists=True,
                is_directory=True,
                writable=True,
                mount_path="/",
                volume_uuid=None,
                expected_volume_uuid=expected_volume_uuid,
                volume_matches=True,
                total_bytes=1_000,
                free_bytes=500,
                diagnostic=None,
            )

    runtime.filesystem = _StorageProbe()  # type: ignore[assignment]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        response = await client.get("/manager/storage")

        assert response.status_code == 200
        locations = {item["name"]: item for item in response.json()["locations"]}
        assert locations["healthy"]["writable"] is True
        assert locations["broken"]["writable"] is False
        assert "choose it again" in locations["broken"]["diagnostic"]
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_selected_accessible_storage_does_not_persist_a_bookmark(
    tmp_path,
    monkeypatch,
) -> None:
    selected = tmp_path / "Selected"
    selected.mkdir()
    config = _config(tmp_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)

    async def unexpected_registration(_path: str, _bookmark: str) -> str:
        raise AssertionError("an already-accessible folder must not retain a bookmark")

    monkeypatch.setattr(runtime, "register_security_scope", unexpected_registration)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        response = await client.post(
            "/manager/storage/inspect",
            json={"path": str(selected), "bookmark_data": "dHJhbnNmZXI="},
        )

        assert response.status_code == 200, response.text
        assert response.json()["path"] == str(selected)
        assert response.json()["scope_id"] is None
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_control_plane_deletes_only_an_exact_managed_model_destination(
    tmp_path,
) -> None:
    model_root = tmp_path / "Models"
    destination = model_root / "llama.cpp" / "owner" / "model-GGUF"
    destination.mkdir(parents=True)
    model_path = destination / "model-Q4_K_M.gguf"
    model_path.write_bytes(b"GGUFmanaged")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"llama_cpp": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "internal",
                "locations": [{"name": "internal", "path": str(model_root)}],
            },
            "models": [
                {
                    "alias": "managed-model",
                    "engine": "llama.cpp",
                    "model": str(model_path),
                    "storage": "internal",
                }
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    install = runtime.installer.store.create(
        repo_id="owner/model-GGUF",
        engine="llama.cpp",
        storage="internal",
        alias="managed-model",
        destination=str(destination),
        revision="abc123",
        filename=model_path.name,
        family=None,
        total_bytes=model_path.stat().st_size,
    )
    runtime.installer.store.update(
        install.id,
        status="installed",
        bytes_downloaded=model_path.stat().st_size,
    )
    installed = runtime.installer.store.get_by_id(install.id)
    await _record_exclusive_cleanup_proof(
        runtime,
        installed,
        location_name="internal",
        location_path=model_root,
        files=(model_path,),
    )
    newer = runtime.installer.store.create(
        repo_id="owner/reused-alias",
        engine="llama.cpp",
        storage="internal",
        alias="managed-model",
        destination=str(model_root / "llama.cpp" / "owner" / "other"),
        revision="def456",
        filename="other.gguf",
        family=None,
        total_bytes=12,
    )
    runtime.installer.store.update(newer.id, status="failed", hidden=1)
    assert runtime.installer.store.latest_for_alias("managed-model").id == newer.id
    trash = tmp_path / "Trash"
    trash.mkdir()
    captured: dict[str, object] = {}

    async def fake_trash_paths(**kwargs: object) -> bool:
        captured.update(kwargs)
        destination.rename(trash / destination.name)
        return True

    async def unexpected_delete_directory(**_kwargs: object) -> bool:
        raise AssertionError("managed cleanup must never permanently delete")

    runtime.filesystem.trash_paths = fake_trash_paths  # type: ignore[method-assign]
    runtime.filesystem.delete_directory = (  # type: ignore[method-assign]
        unexpected_delete_directory
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        dismissed = await client.delete(
            f"/manager/model-library/installs/{install.id}"
        )
        assert dismissed.status_code == 204
        assert (await client.get("/manager/model-library/installs")).json() == {
            "installs": []
        }
        evidence_before_delete = (
            await client.get("/manager/model-library/install-evidence")
        ).json()
        assert evidence_before_delete["schema_version"] == 1
        install_before_delete = next(
            item
            for item in evidence_before_delete["installs"]
            if item["id"] == install.id
        )
        assert install_before_delete["dismissed"] is True
        assert install_before_delete["events"][-1]["event"] == (
            "history_dismissed"
        )

        revision = (await client.get("/manager/config")).json()["revision"]
        deleted = await client.request(
            "DELETE",
            "/manager/models/managed-model",
            json={"revision": revision, "installation_id": install.id},
        )

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_files"] is True
        assert deleted.json()["files_disposition"] == "trashed"
        assert deleted.json()["model_count"] == 0
        assert not destination.exists()
        assert (trash / destination.name / model_path.name).is_file()
        assert model_root.is_dir()
        assert captured["root"] == str(model_root)
        assert captured["paths"] == (str(destination),)
        assert captured["expected_volume_uuid"] is None
        assert captured["scope_id"] is None
        assert tuple(captured["exact_manifest"])[0].path == model_path.name
        assert load_config(config_path).models == []
        assert runtime.model_list() == []
        assert await runtime.installer.list() == []
        assert runtime.installer.store.get_by_id(install.id).status == "trashed"
        assert runtime.installer.store.get_by_id(newer.id).status == "failed"
        evidence_after_delete = (
            await client.get("/manager/model-library/install-evidence")
        ).json()
        install_after_delete = next(
            item
            for item in evidence_after_delete["installs"]
            if item["id"] == install.id
        )
        assert [
            (event["event"], event["status"])
            for event in install_after_delete["events"][-2:]
        ] == [
            ("history_dismissed", "installed"),
            ("status", "trashed"),
        ]
        cleanup_transactions = runtime.model_cleanup_journal.list_all()
        assert len(cleanup_transactions) == 1
        assert cleanup_transactions[0].installation_id == install.id
        assert cleanup_transactions[0].phase is CleanupPhase.COMPLETED
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()

    reopened = InstallStore(tmp_path / "state.db")
    try:
        assert reopened.get_by_id(install.id).status == "trashed"
        assert reopened.get_provenance(install.id).installation_id == install.id
        assert reopened.events(install.id)[-1].status == "trashed"
        assert reopened.get_by_id(newer.id).status == "failed"
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_managed_cleanup_cannot_downgrade_unknown_history_to_import_scan(
    tmp_path,
) -> None:
    model_root = tmp_path / "Models"
    destination = model_root / "llama.cpp" / "owner" / "model-GGUF"
    destination.mkdir(parents=True)
    model_path = destination / "model-Q4_K_M.gguf"
    model_path.write_bytes(b"GGUFmanaged")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"llama_cpp": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "internal",
                "locations": [{"name": "internal", "path": str(model_root)}],
            },
            "models": [
                {
                    "alias": "managed-model",
                    "engine": "llama.cpp",
                    "model": str(model_path),
                    "storage": "internal",
                }
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    install = runtime.installer.store.create(
        repo_id="owner/model-GGUF",
        engine="llama.cpp",
        storage="internal",
        alias="managed-model",
        destination=str(destination),
        revision="abc123",
        filename=model_path.name,
        family=None,
        total_bytes=model_path.stat().st_size,
    )
    runtime.installer.store.update(
        install.id,
        status="installed",
        bytes_downloaded=model_path.stat().st_size,
        hidden=1,
    )
    filesystem_calls: list[str] = []

    async def unexpected_filesystem(*_args: object, **_kwargs: object):
        filesystem_calls.append("called")
        raise AssertionError("ineligible managed cleanup reached the filesystem")

    runtime.filesystem.scan = unexpected_filesystem  # type: ignore[method-assign]
    runtime.filesystem.verify_exact_manifest = (  # type: ignore[method-assign]
        unexpected_filesystem
    )
    runtime.filesystem.trash_paths = unexpected_filesystem  # type: ignore[method-assign]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        revision = (await client.get("/manager/config")).json()["revision"]
        config_before = config_path.read_bytes()
        evidence_before = runtime.installer.store.evidence()

        omitted = await client.request(
            "DELETE",
            "/manager/models/managed-model",
            json={"revision": revision},
        )
        unknown_proof = await client.request(
            "DELETE",
            "/manager/models/managed-model",
            json={"revision": revision, "installation_id": install.id},
        )
        unknown_id = await client.request(
            "DELETE",
            "/manager/models/managed-model",
            json={
                "revision": revision,
                "installation_id": "00000000-0000-0000-0000-000000000001",
            },
        )
        malformed_id = await client.request(
            "DELETE",
            "/manager/models/managed-model",
            json={
                "revision": revision,
                "installation_id": install.id.upper(),
            },
        )

        assert omitted.status_code == 400
        assert "exact installation" in omitted.json()["detail"]
        assert unknown_proof.status_code == 400
        assert "ownership_not_exclusive_managed" in unknown_proof.json()["detail"]
        assert unknown_id.status_code == 400
        assert "installation_missing" in unknown_id.json()["detail"]
        assert malformed_id.status_code == 422
        assert filesystem_calls == []
        assert config_path.read_bytes() == config_before
        assert runtime.installer.store.evidence() == evidence_before
        assert model_path.read_bytes() == b"GGUFmanaged"
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_managed_cleanup_recovers_trash_to_ledger_and_config_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "Models"
    destination = model_root / "llama.cpp" / "owner" / "model-GGUF"
    destination.mkdir(parents=True)
    model_path = destination / "model-Q4_K_M.gguf"
    model_path.write_bytes(b"GGUFmanaged")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"llama_cpp": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "internal",
                "locations": [
                    {"name": "internal", "path": str(model_root)}
                ],
            },
            "models": [
                {
                    "alias": "managed-model",
                    "engine": "llama.cpp",
                    "model": str(model_path),
                    "storage": "internal",
                }
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    first_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    first = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=first_client,
    )
    await first.start(raise_on_degraded=True)
    install = first.installer.store.create(
        repo_id="owner/model-GGUF",
        engine="llama.cpp",
        storage="internal",
        alias="managed-model",
        destination=str(destination),
        revision="abc123",
        filename=model_path.name,
        family=None,
        total_bytes=model_path.stat().st_size,
    )
    first.installer.store.update(
        install.id,
        status="installed",
        bytes_downloaded=model_path.stat().st_size,
    )
    await _record_exclusive_cleanup_proof(
        first,
        first.installer.store.get_by_id(install.id),
        location_name="internal",
        location_path=model_root,
        files=(model_path,),
    )
    trash = tmp_path / "Trash"
    trash.mkdir()

    async def move_then_confirm(**_kwargs: object) -> bool:
        destination.rename(trash / destination.name)
        return True

    first.filesystem.trash_paths = move_then_confirm  # type: ignore[method-assign]

    def fail_ledger(_installation_id: str):
        raise RuntimeError("injected ledger interruption")

    monkeypatch.setattr(first.installer.store, "mark_trashed", fail_ledger)
    revision = configuration_revision(load_config(config_path))
    try:
        with pytest.raises(RuntimeError, match="injected ledger interruption"):
            await first.delete_managed_model(
                "managed-model",
                expected_revision=revision,
                installation_id=install.id,
            )
        pending = first.model_cleanup_journal.list_incomplete()
        assert len(pending) == 1
        assert pending[0].phase is CleanupPhase.TRASH_CONFIRMED
        assert first.installer.store.get_by_id(install.id).status == "installed"
        assert len(load_config(config_path).models) == 1
        assert not destination.exists()
    finally:
        await first.stop()
        await first_client.aclose()

    recovered_config = load_config(config_path)
    second_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    second = NativeRuntime(
        recovered_config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=second_client,
    )
    try:
        await second.start(raise_on_degraded=True)
        assert second.installer.store.get_by_id(install.id).status == "trashed"
        assert load_config(config_path).models == []
        transactions = second.model_cleanup_journal.list_all()
        assert len(transactions) == 1
        assert transactions[0].phase is CleanupPhase.COMPLETED
        assert second.model_list() == []
    finally:
        await second.stop()
        await second_client.aclose()


@pytest.mark.asyncio
async def test_exact_managed_cleanup_failure_and_cancellation_preserve_state(
    tmp_path,
) -> None:
    model_root = tmp_path / "Models"
    destination = model_root / "llama.cpp" / "owner" / "model-GGUF"
    destination.mkdir(parents=True)
    model_path = destination / "model-Q4_K_M.gguf"
    model_path.write_bytes(b"GGUFmanaged")
    other_model = model_root / "imports" / "other-Q4_K_M.gguf"
    other_model.parent.mkdir(parents=True)
    other_model.write_bytes(b"GGUFother")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"llama_cpp": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "internal",
                "locations": [{"name": "internal", "path": str(model_root)}],
            },
            "models": [
                {
                    "alias": "managed-model",
                    "engine": "llama.cpp",
                    "model": str(model_path),
                    "storage": "internal",
                },
                {
                    "alias": "other-model",
                    "engine": "llama.cpp",
                    "model": str(other_model),
                    "storage": "internal",
                },
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    install = runtime.installer.store.create(
        repo_id="owner/model-GGUF",
        engine="llama.cpp",
        storage="internal",
        alias="managed-model",
        destination=str(destination),
        revision="abc123",
        filename=model_path.name,
        family=None,
        total_bytes=model_path.stat().st_size,
    )
    runtime.installer.store.update(
        install.id,
        status="installed",
        bytes_downloaded=model_path.stat().st_size,
    )
    await _record_exclusive_cleanup_proof(
        runtime,
        runtime.installer.store.get_by_id(install.id),
        location_name="internal",
        location_path=model_root,
        files=(model_path,),
    )
    revision = configuration_revision(load_config(config_path))
    config_before = config_path.read_bytes()
    evidence_before = runtime.installer.store.evidence()

    trash_calls: list[str] = []

    async def failed_trash(**_kwargs: object) -> bool:
        trash_calls.append("called")
        raise FilesystemProbeError("trash helper rejected the target")

    runtime.filesystem.trash_paths = failed_trash  # type: ignore[method-assign]
    try:
        with pytest.raises(ModelCleanupRejected, match="no longer matches"):
            await runtime.delete_managed_model(
                "other-model",
                expected_revision=revision,
                installation_id=install.id,
            )
        assert trash_calls == []
        with pytest.raises(FilesystemProbeError, match="trash helper rejected"):
            await runtime.delete_managed_model(
                "managed-model",
                expected_revision=revision,
                installation_id=install.id,
            )
        assert trash_calls == ["called"]
        assert config_path.read_bytes() == config_before
        assert runtime.installer.store.evidence() == evidence_before
        assert model_path.is_file()
        assert (await runtime.coordinator.status()).state == CoordinatorState.IDLE

        trash_started = asyncio.Event()

        async def blocked_trash(**_kwargs: object) -> bool:
            trash_started.set()
            await asyncio.Event().wait()
            return True

        runtime.filesystem.trash_paths = blocked_trash  # type: ignore[method-assign]
        cleanup = asyncio.create_task(
            runtime.delete_managed_model(
                "managed-model",
                expected_revision=revision,
                installation_id=install.id,
            )
        )
        await asyncio.wait_for(trash_started.wait(), timeout=2)
        cleanup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert config_path.read_bytes() == config_before
        assert runtime.installer.store.evidence() == evidence_before
        assert model_path.is_file()
        assert (await runtime.coordinator.status()).state == CoordinatorState.IDLE

        changed_root = tmp_path / "ChangedModels"
        changed_root.mkdir()
        await runtime.mac_inventory_index.reconcile_storage(
            [("internal", str(changed_root), None, None)]
        )
        with pytest.raises(ModelCleanupRejected, match="storage binding has changed"):
            await runtime.delete_managed_model(
                "managed-model",
                expected_revision=revision,
                installation_id=install.id,
            )
        assert config_path.read_bytes() == config_before
        assert runtime.installer.store.evidence() == evidence_before
        assert model_path.is_file()
    finally:
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_imported_gguf_files_move_to_trash_and_profile_is_removed(
    tmp_path,
) -> None:
    model_root = tmp_path / "Models"
    model_path = model_root / "publisher" / "model-Q4_K_M.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"GGUFimported")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"llama_cpp": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "existing-models",
                "locations": [
                    {"name": "existing-models", "path": str(model_root)}
                ],
            },
            "models": [
                {
                    "alias": "imported-model",
                    "engine": "llama.cpp",
                    "model": str(model_path),
                    "storage": "existing-models",
                }
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    trash = tmp_path / "Trash"
    trash.mkdir()
    captured: dict[str, object] = {}

    async def fake_trash_paths(**kwargs: object) -> bool:
        captured.update(kwargs)
        paths = kwargs["paths"]
        assert isinstance(paths, tuple)
        for value in paths:
            Path(value).rename(trash / Path(value).name)
        return True

    runtime.filesystem.trash_paths = fake_trash_paths  # type: ignore[method-assign]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        revision = (await client.get("/manager/config")).json()["revision"]
        deleted = await client.request(
            "DELETE",
            "/manager/models/imported-model",
            json={"revision": revision},
        )

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_files"] is True
        assert deleted.json()["files_disposition"] == "trashed"
        assert deleted.json()["model_count"] == 0
        assert not model_path.exists()
        assert (trash / model_path.name).is_file()
        assert captured == {
            "root": str(model_root),
            "paths": (str(model_path.resolve()),),
            "expected_volume_uuid": None,
            "scope_id": None,
        }
        assert load_config(config_path).models == []
        assert (await runtime.coordinator.status()).state.value == "idle"
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_imported_omlx_directory_moves_to_trash(
    tmp_path,
) -> None:
    model_root = tmp_path / "Models"
    model_directory = model_root / "publisher" / "omlx-model"
    model_directory.mkdir(parents=True)
    (model_directory / "config.json").write_text(
        '{"architectures":["ExampleForCausalLM"]}',
        encoding="utf-8",
    )
    (model_directory / "model.safetensors").write_bytes(b"weights")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {
                "omlx": {
                    "enabled": True,
                    "model_directories": [str(model_root)],
                }
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "existing-models",
                "locations": [
                    {"name": "existing-models", "path": str(model_root)}
                ],
            },
            "models": [
                {
                    "alias": "imported-omlx",
                    "engine": "omlx",
                    "model": "omlx-model",
                    "storage": "existing-models",
                }
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    trash = tmp_path / "Trash"
    trash.mkdir()

    async def fake_trash_paths(**kwargs: object) -> bool:
        paths = kwargs["paths"]
        assert paths == (str(model_directory.resolve()),)
        model_directory.rename(trash / model_directory.name)
        return True

    runtime.filesystem.trash_paths = fake_trash_paths  # type: ignore[method-assign]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        revision = (await client.get("/manager/config")).json()["revision"]
        deleted = await client.request(
            "DELETE",
            "/manager/models/imported-omlx",
            json={"revision": revision},
        )

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["files_disposition"] == "trashed"
        assert not model_directory.exists()
        assert (trash / model_directory.name / "model.safetensors").is_file()
        assert load_config(config_path).models == []
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_imported_cleanup_refuses_files_shared_by_another_profile(
    tmp_path,
) -> None:
    model_root = tmp_path / "Models"
    model_path = model_root / "publisher" / "model-Q4_K_M.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"GGUFshared")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"llama_cpp": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "existing-models",
                "locations": [
                    {"name": "existing-models", "path": str(model_root)}
                ],
            },
            "models": [
                {
                    "alias": "first-profile",
                    "engine": "llama.cpp",
                    "model": str(model_path),
                    "storage": "existing-models",
                },
                {
                    "alias": "second-profile",
                    "engine": "llama.cpp",
                    "model": str(model_path),
                    "storage": "existing-models",
                },
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        revision = (await client.get("/manager/config")).json()["revision"]
        deleted = await client.request(
            "DELETE",
            "/manager/models/first-profile",
            json={"revision": revision},
        )

        assert deleted.status_code == 400
        assert "second-profile" in deleted.json()["detail"]
        assert model_path.is_file()
        assert len(load_config(config_path).models) == 2
        assert (await runtime.coordinator.status()).state.value == "idle"
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_managed_cleanup_refuses_directory_shared_by_another_primary_profile(
    tmp_path,
) -> None:
    model_root = tmp_path / "Models"
    destination = model_root / "llama.cpp" / "owner" / "shared-GGUF"
    destination.mkdir(parents=True)
    managed_model = destination / "managed-Q4_K_M.gguf"
    shared_model = destination / "shared-Q5_K_M.gguf"
    managed_model.write_bytes(b"GGUFmanaged")
    shared_model.write_bytes(b"GGUFshared")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"llama_cpp": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "internal",
                "locations": [{"name": "internal", "path": str(model_root)}],
            },
            "models": [
                {
                    "alias": "managed-model",
                    "engine": "llama.cpp",
                    "model": str(managed_model),
                    "storage": "internal",
                },
                {
                    "alias": "shared-primary",
                    "engine": "llama.cpp",
                    "model": str(shared_model),
                    "storage": "internal",
                },
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    install = runtime.installer.store.create(
        repo_id="owner/shared-GGUF",
        engine="llama.cpp",
        storage="internal",
        alias="managed-model",
        destination=str(destination),
        revision="abc123",
        filename=managed_model.name,
        family=None,
        total_bytes=managed_model.stat().st_size,
    )
    runtime.installer.store.update(
        install.id,
        status="installed",
        bytes_downloaded=managed_model.stat().st_size,
    )
    await _record_exclusive_cleanup_proof(
        runtime,
        runtime.installer.store.get_by_id(install.id),
        location_name="internal",
        location_path=model_root,
        files=(managed_model,),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        config_before = config_path.read_bytes()
        ledger_before = runtime.installer.store.evidence()
        revision = (await client.get("/manager/config")).json()["revision"]

        refused = await client.request(
            "DELETE",
            "/manager/models/managed-model",
            json={"revision": revision, "installation_id": install.id},
        )

        assert refused.status_code == 400
        assert "shared-primary" in refused.json()["detail"]
        assert managed_model.read_bytes() == b"GGUFmanaged"
        assert shared_model.read_bytes() == b"GGUFshared"
        assert destination.is_dir()
        assert config_path.read_bytes() == config_before
        assert runtime.installer.store.evidence() == ledger_before
        assert runtime.installer.store.get(install.id).status == "installed"
        assert [item.alias for item in runtime.config.models] == [
            "managed-model",
            "shared-primary",
        ]
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_managed_cleanup_refuses_directory_used_by_an_engine_alternative(
    tmp_path,
) -> None:
    model_root = tmp_path / "Models"
    destination = model_root / "llama.cpp" / "owner" / "shared-GGUF"
    destination.mkdir(parents=True)
    managed_model = destination / "managed-Q4_K_M.gguf"
    alternative_model = destination / "alternative-Q5_K_M.gguf"
    managed_model.write_bytes(b"GGUFmanaged")
    alternative_model.write_bytes(b"GGUFalternative")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {
                "llama_cpp": {"enabled": True},
                "omlx": {"enabled": True},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "internal",
                "locations": [{"name": "internal", "path": str(model_root)}],
            },
            "models": [
                {
                    "alias": "managed-model",
                    "engine": "llama.cpp",
                    "model": str(managed_model),
                    "storage": "internal",
                },
                {
                    "alias": "alternative-owner",
                    "engine": "omlx",
                    "model": "unrelated-omlx",
                    "storage": "internal",
                    "alternatives": [
                        {
                            "engine": "llama.cpp",
                            "model": str(alternative_model),
                            "storage": "internal",
                        }
                    ],
                },
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    install = runtime.installer.store.create(
        repo_id="owner/shared-GGUF",
        engine="llama.cpp",
        storage="internal",
        alias="managed-model",
        destination=str(destination),
        revision="abc123",
        filename=managed_model.name,
        family=None,
        total_bytes=managed_model.stat().st_size,
    )
    runtime.installer.store.update(
        install.id,
        status="installed",
        bytes_downloaded=managed_model.stat().st_size,
    )
    await _record_exclusive_cleanup_proof(
        runtime,
        runtime.installer.store.get_by_id(install.id),
        location_name="internal",
        location_path=model_root,
        files=(managed_model,),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        config_before = config_path.read_bytes()
        ledger_before = runtime.installer.store.evidence()
        revision = (await client.get("/manager/config")).json()["revision"]

        refused = await client.request(
            "DELETE",
            "/manager/models/managed-model",
            json={"revision": revision, "installation_id": install.id},
        )

        assert refused.status_code == 400
        assert "alternative-owner" in refused.json()["detail"]
        assert managed_model.read_bytes() == b"GGUFmanaged"
        assert alternative_model.read_bytes() == b"GGUFalternative"
        assert destination.is_dir()
        assert config_path.read_bytes() == config_before
        assert runtime.installer.store.evidence() == ledger_before
        assert runtime.installer.store.get(install.id).status == "installed"
        assert [item.alias for item in runtime.config.models] == [
            "managed-model",
            "alternative-owner",
        ]
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.parametrize("shared_reference", ["model", "projector"])
@pytest.mark.asyncio
async def test_imported_gguf_cleanup_refuses_alternative_model_or_projector(
    tmp_path,
    shared_reference: str,
) -> None:
    model_root = tmp_path / "Models"
    imported_directory = model_root / "publisher"
    imported_directory.mkdir(parents=True)
    model_path = imported_directory / "model-Q4_K_M.gguf"
    projector_path = imported_directory / "mmproj-model-F16.gguf"
    unrelated_model = model_root / "other" / "unrelated.gguf"
    model_path.write_bytes(b"GGUFmodel")
    projector_path.write_bytes(b"GGUFprojector")
    alternative = {
        "engine": "llama.cpp",
        "model": str(
            model_path if shared_reference == "model" else unrelated_model
        ),
        "storage": "existing-models",
    }
    if shared_reference == "projector":
        alternative["load"] = {"projector_path": str(projector_path)}
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {
                "llama_cpp": {"enabled": True},
                "omlx": {"enabled": True},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "existing-models",
                "locations": [
                    {"name": "existing-models", "path": str(model_root)}
                ],
            },
            "models": [
                {
                    "alias": "imported-model",
                    "engine": "llama.cpp",
                    "model": str(model_path),
                    "storage": "existing-models",
                    "load": {"projector_path": str(projector_path)},
                },
                {
                    "alias": "alternative-owner",
                    "engine": "omlx",
                    "model": "unrelated-omlx",
                    "storage": "existing-models",
                    "alternatives": [alternative],
                },
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        config_before = config_path.read_bytes()
        ledger_before = runtime.installer.store.evidence()
        revision = (await client.get("/manager/config")).json()["revision"]

        refused = await client.request(
            "DELETE",
            "/manager/models/imported-model",
            json={"revision": revision},
        )

        assert refused.status_code == 400
        assert "alternative-owner" in refused.json()["detail"]
        assert model_path.read_bytes() == b"GGUFmodel"
        assert projector_path.read_bytes() == b"GGUFprojector"
        assert config_path.read_bytes() == config_before
        assert runtime.installer.store.evidence() == ledger_before == []
        assert [item.alias for item in runtime.config.models] == [
            "imported-model",
            "alternative-owner",
        ]
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_imported_omlx_cleanup_refuses_model_used_by_an_engine_alternative(
    tmp_path,
) -> None:
    model_root = tmp_path / "Models"
    model_directory = model_root / "publisher" / "omlx-model"
    model_directory.mkdir(parents=True)
    config_file = model_directory / "config.json"
    weights = model_directory / "model.safetensors"
    config_file.write_text(
        '{"architectures":["ExampleForCausalLM"]}',
        encoding="utf-8",
    )
    weights.write_bytes(b"weights")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {
                "llama_cpp": {"enabled": True},
                "omlx": {
                    "enabled": True,
                    "model_directories": [str(model_root)],
                },
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "existing-models",
                "locations": [
                    {"name": "existing-models", "path": str(model_root)}
                ],
            },
            "models": [
                {
                    "alias": "imported-omlx",
                    "engine": "omlx",
                    "model": "omlx-model",
                    "storage": "existing-models",
                },
                {
                    "alias": "alternative-owner",
                    "engine": "llama.cpp",
                    "model": str(model_root / "unrelated.gguf"),
                    "storage": "existing-models",
                    "alternatives": [
                        {
                            "engine": "omlx",
                            "model": "omlx-model",
                            "storage": "existing-models",
                        }
                    ],
                },
            ],
        }
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        config_before = config_path.read_bytes()
        ledger_before = runtime.installer.store.evidence()
        revision = (await client.get("/manager/config")).json()["revision"]

        refused = await client.request(
            "DELETE",
            "/manager/models/imported-omlx",
            json={"revision": revision},
        )

        assert refused.status_code == 400
        assert "alternative-owner" in refused.json()["detail"]
        assert config_file.is_file()
        assert weights.read_bytes() == b"weights"
        assert model_directory.is_dir()
        assert config_path.read_bytes() == config_before
        assert runtime.installer.store.evidence() == ledger_before == []
        assert [item.alias for item in runtime.config.models] == [
            "imported-omlx",
            "alternative-owner",
        ]
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_structured_configuration_flags_restart_only_settings(tmp_path) -> None:
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    config = _config(tmp_path)
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        edited = config.model_dump(mode="json")
        edited["server"]["idle_unload_seconds"] = 1200
        revision = (await client.get("/manager/config")).json()["revision"]
        saved = await client.put(
            "/manager/config", json={"config": edited, "revision": revision}
        )

        assert saved.status_code == 200
        assert saved.json()["applied"] is False
        assert saved.json()["restart_required"] is True
        assert runtime.config.server.idle_unload_seconds is None
        assert load_config(config_path).server.idle_unload_seconds == 1200
        pending = await client.get("/manager/config")
        assert pending.status_code == 200
        assert pending.json()["restart_required"] is True
        assert pending.json()["revision"] != pending.json()["applied_revision"]
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_security_scope_store_does_not_follow_configurable_state_database(
    tmp_path,
) -> None:
    config_path = tmp_path / "settings" / "config.yaml"
    expected = config_path.parent / "state" / "security-scopes"
    for name in ("first.db", "moved.db"):
        config = _config(tmp_path).model_copy(
            update={
                "paths": _config(tmp_path).paths.model_copy(
                    update={"state_database": str(tmp_path / name)}
                )
            }
        )
        save_config(config, config_path)
        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(500))
        )
        runtime = NativeRuntime(
            config,
            config_path=config_path,
            adapters=_adapters(),
            proxy_client=upstream_client,
        )
        try:
            assert runtime.security_scopes.root == expected
            await runtime.start(raise_on_degraded=True)
        finally:
            await runtime.stop()
            await upstream_client.aclose()


@pytest.mark.asyncio
async def test_each_runtime_start_reactivates_configured_folder_scope(
    tmp_path,
) -> None:
    scope_id = "a" * 64
    selected = tmp_path / "Models"
    selected.mkdir()
    base = _config(tmp_path)
    payload = base.model_dump(mode="json")
    payload["storage"] = {
        "default": "selected",
        "locations": [
            {
                "name": "selected",
                "path": str(selected),
                "scope_id": scope_id,
            }
        ],
    }
    config = MacConfig.model_validate(payload)
    config_path = tmp_path / "settings" / "config.yaml"
    save_config(config, config_path)
    activations: list[tuple[str, str]] = []

    class _RecordingScopeProcess:
        async def activate(self, value: str, path: str) -> None:
            activations.append((value, path))

    class _ProtectedStorageProbe:
        async def inspect(
            self,
            path: str,
            *,
            name: str | None = None,
            expected_volume_uuid: str | None = None,
            scope_id: str | None = None,
        ) -> StorageStatus:
            del scope_id
            return StorageStatus(
                name=name,
                path=path,
                exists=False,
                is_directory=False,
                writable=False,
                mount_path=None,
                volume_uuid=None,
                expected_volume_uuid=expected_volume_uuid,
                volume_matches=False,
                total_bytes=None,
                free_bytes=None,
                diagnostic="permission required",
            )

    for _ in range(2):
        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(500))
        )
        runtime = NativeRuntime(
            config,
            config_path=config_path,
            adapters=_adapters(),
            proxy_client=upstream_client,
            security_scope_process=_RecordingScopeProcess(),  # type: ignore[arg-type]
            filesystem_probe=_ProtectedStorageProbe(),  # type: ignore[arg-type]
        )
        try:
            await runtime.start(raise_on_degraded=True)
        finally:
            await runtime.stop()
            await upstream_client.aclose()

    assert activations == [(scope_id, str(selected)), (scope_id, str(selected))]


@pytest.mark.asyncio
async def test_runtime_removes_unnecessary_scope_without_folder_reselection(
    tmp_path,
) -> None:
    scope_id = "b" * 64
    selected = tmp_path / "Models"
    selected.mkdir()
    payload = _config(tmp_path).model_dump(mode="json")
    payload["storage"] = {
        "default": "selected",
        "locations": [
            {
                "name": "selected",
                "path": str(selected),
                "scope_id": scope_id,
            }
        ],
    }
    payload["models"][0]["storage"] = "selected"
    config = MacConfig.model_validate(payload)
    config_path = tmp_path / "settings" / "config.yaml"
    save_config(config, config_path)
    scope_root = config_path.parent / "state" / "security-scopes"
    scope_root.mkdir(parents=True)
    obsolete = scope_root / f"{scope_id}.bookmark"
    obsolete.write_bytes(b"obsolete")

    class _UnexpectedScopeProcess:
        async def activate(self, _value: str, _path: str) -> None:
            raise AssertionError("ordinary accessible storage must not reactivate a scope")

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
        security_scope_process=_UnexpectedScopeProcess(),  # type: ignore[arg-type]
    )
    try:
        await runtime.start(raise_on_degraded=True)

        assert runtime.config.storage.locations[0].scope_id is None
        assert runtime.resolve("frontier").scope_id is None
        assert load_config(config_path).storage.locations[0].scope_id is None
        assert not obsolete.exists()
    finally:
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_structured_configuration_rejects_unusable_folder_grant_before_write(
    tmp_path, monkeypatch
) -> None:
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    config = _config(tmp_path)
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    original = config_path.read_text(encoding="utf-8")
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    checked: list[MacConfig] = []

    async def reject_scope(candidate: MacConfig) -> None:
        checked.append(candidate)
        raise RuntimeError("selected-folder permission is missing")

    monkeypatch.setattr(runtime, "validate_security_scopes", reject_scope)
    try:
        edited = config.model_dump(mode="json")
        edited["storage"]["locations"][0].update(
            {
                "path": str(tmp_path / "Models"),
                "scope_id": "a" * 64,
            }
        )
        revision = (await client.get("/manager/config")).json()["revision"]
        response = await client.put(
            "/manager/config", json={"config": edited, "revision": revision}
        )

        assert response.status_code == 400
        assert "selected-folder permission is missing" in response.text
        assert len(checked) == 1
        assert checked[0].storage.locations[0].scope_id == "a" * 64
        assert config_path.read_text(encoding="utf-8") == original
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_lan_inference_without_key_accepts_unauthenticated_requests(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("INFERENCE_API_KEY", raising=False)
    config = _config(tmp_path)
    config.server.inference_bind = "0.0.0.0"
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    inference = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    try:
        response = await inference.get("/v1/models")
        assert response.status_code == 200
    finally:
        await inference.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_inference_and_control_auth_are_independent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INFERENCE_API_KEY", "inference-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-secret")
    config = _config(tmp_path)
    config.server.inference_bind = "0.0.0.0"
    config.server.control_bind = "0.0.0.0"
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        config,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    inference = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
    )
    try:
        assert (await inference.get("/v1/models")).status_code == 401
        assert (
            await inference.get(
                "/v1/models", headers={"Authorization": "Bearer inference-secret"}
            )
        ).status_code == 200
        assert (await control.get("/manager/status")).status_code == 401
        assert (
            await control.get("/manager/status", auth=("admin", "admin-secret"))
        ).status_code == 200
    finally:
        await inference.aclose()
        await control.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_fleet_dispatch_credential_is_valid_only_for_marked_requests(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("INFERENCE_API_KEY", "local-inference-secret")
    monkeypatch.setenv("FLEET_INFERENCE_API_KEY", "fleet-dispatch-secret")
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    inference = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    marker = {"X-Mnemosyne-Fleet-Route": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
    try:
        assert (
            await inference.get(
                "/v1/models",
                headers={"Authorization": "Bearer fleet-dispatch-secret"},
            )
        ).status_code == 401
        assert (
            await inference.get(
                "/v1/models",
                headers={
                    **marker,
                    "Authorization": "Bearer local-inference-secret",
                },
            )
        ).status_code == 401
        assert (
            await inference.get(
                "/v1/models",
                headers={
                    **marker,
                    "Authorization": "Bearer fleet-dispatch-secret",
                },
            )
        ).status_code == 200
        malformed = await inference.get(
            "/v1/models",
            headers={
                "X-Mnemosyne-Fleet-Route": "not-a-route-id",
                "Authorization": "Bearer fleet-dispatch-secret",
            },
        )
        assert malformed.status_code == 400
        assert malformed.json()["detail"]["code"] == "invalid_fleet_route"
        assert (
            await inference.get(
                "/v1/models",
                headers={"Authorization": "Bearer local-inference-secret"},
            )
        ).status_code == 200
    finally:
        await inference.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_fleet_marked_model_probe_never_exposes_local_model_paths(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("INFERENCE_API_KEY", "local-inference-secret")
    monkeypatch.setenv("FLEET_INFERENCE_API_KEY", "fleet-dispatch-secret")
    model_path = tmp_path / "nested" / "weights" / "model.gguf"
    config = MacConfig.model_validate(
        {
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [
                {
                    "alias": "local-model",
                    "engine": "llama.cpp",
                    "model": str(model_path),
                }
            ],
        }
    )
    runtime = NativeRuntime(config, adapters=_adapters())
    await runtime.start(raise_on_degraded=True)
    inference = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    try:
        local = await inference.get(
            "/v1/models",
            headers={"Authorization": "Bearer local-inference-secret"},
        )
        fleet = await inference.get(
            "/v1/models",
            headers={
                "Authorization": "Bearer fleet-dispatch-secret",
                "X-Mnemosyne-Fleet-Route": (
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                ),
            },
        )

        assert local.status_code == 200
        assert str(model_path.absolute()) in json.dumps(local.json())
        assert fleet.status_code == 200
        assert fleet.json() == {
            "object": "list",
            "data": [
                {
                    "id": "local-model",
                    "object": "model",
                    "owned_by": "mnemosyne",
                }
            ],
        }
        assert str(tmp_path) not in json.dumps(fleet.json(), sort_keys=True)
    finally:
        await inference.aclose()
        await runtime.stop()


async def _seed_staged_pairing_workflow(
    runtime: NativeRuntime,
    *,
    hub_origin: str,
    locator: str,
    pairing_id: str,
    credentials: PairingCredentials,
) -> None:
    invitation = PairingInvitation(
        invitation_id="11111111-1111-4111-8111-111111111111",
        pairing_secret="fixture-pairing-secret-that-is-long-enough-123456",
        hub_origin=hub_origin,
        locator=locator,
    ).validated()
    workflow, created = await asyncio.to_thread(
        runtime.fleet_pairing_client._journal.prepare,
        invitation,
        reporting_node_id=runtime.usage.identity.node_id,
        allow_create=True,
    )
    assert created is True
    await runtime.fleet_pairing.begin_attempt(
        hub_origin=hub_origin,
        node_url=locator,
        attempt_id=workflow.attempt_id,
    )
    claim_id = "22222222-2222-4222-8222-222222222221"
    await asyncio.to_thread(
        runtime.fleet_pairing_client._journal.record_claim,
        _ClaimResponse.model_validate(
            {
                "schema_version": 1,
                "claim_id": claim_id,
                "invitation_id": invitation.invitation_id,
                "pairing_id": pairing_id,
                "display_name": runtime.usage.identity.node_id,
                "reporting_node_id": runtime.usage.identity.node_id,
                "service_version": "0.9.0",
                "platform": "macos",
                "protocol_version": 1,
                "state": "claimed",
                "claimed_at": 100.0,
                "expires_at": 4_000_000_000.0,
                "locator_accepted": True,
            }
        ),
    )
    await asyncio.to_thread(
        runtime.fleet_pairing_client._journal.record_staging,
        _ProvisionResponse.model_validate(
            {
                "schema_version": 1,
                "claim_id": claim_id,
                "pairing_id": pairing_id,
                "reporting_node_id": runtime.usage.identity.node_id,
                "credential_generation": 1,
                "credentials": {
                    "snapshot_bearer": credentials.snapshot_key,
                    "dispatch_bearer": credentials.dispatch_key,
                    "management_bearer": credentials.management_key,
                },
                "state": "provisioning",
            }
        ),
    )
    await runtime.fleet_pairing.record_assignment(
        pairing_id=pairing_id,
        node_id=runtime.usage.identity.node_id,
        credential_epoch=1,
    )
    await runtime.fleet_pairing.activate_credentials(credentials)
    await asyncio.to_thread(
        runtime.fleet_pairing_client._journal.record_activation_pending
    )


@pytest.mark.asyncio
async def test_staged_pairing_allows_only_non_loading_probes_until_commit(
    tmp_path,
    monkeypatch,
) -> None:
    for key in (
        "FLEET_API_KEY",
        "FLEET_INFERENCE_API_KEY",
        "FLEET_MANAGEMENT_API_KEY",
    ):
        # Register teardown ownership even when the outer environment had no
        # value; FleetPairingStore intentionally updates this same mapping.
        monkeypatch.setenv(key, "")

    upstream_calls = 0

    def pairing_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path.endswith("/self-revoke")
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "pairing_id": payload["pairing_id"],
                "reporting_node_id": payload["reporting_node_id"],
                "display_name": payload["reporting_node_id"],
                "platform": "macos",
                "service_version": "0.9.0",
                "protocol_version": 1,
                "service_class": "primary",
                "state": "revoked",
                "hub_enabled": False,
                "credential_generation": payload["credential_generation"],
                "created_at": 100.0,
                "updated_at": 101.0,
                "revoked_at": 101.0,
                "failure_code": None,
            },
        )

    def upstream_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        env_path=tmp_path / "private" / ".env",
        adapters=_adapters(),
        proxy_client=upstream_client,
        pairing_transport=httpx.MockTransport(pairing_handler),
    )
    await runtime.start(raise_on_degraded=True)
    inference = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
    )
    credentials = PairingCredentials(
        snapshot_key="snapshot-pairing-secret",
        dispatch_key="dispatch-pairing-secret",
        management_key="management-pairing-secret",
    )
    route_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    try:
        await _seed_staged_pairing_workflow(
            runtime,
            hub_origin="https://nyx.example.test",
            locator="https://mac.example.test:1240",
            pairing_id="22222222-2222-4222-8222-222222222222",
            credentials=credentials,
        )
        pending = await runtime.fleet_pairing_status()
        assert pending["state"] == "pending"
        assert pending["credentials_configured"] is True
        assert "hub_origin" not in pending
        serialized = json.dumps(pending, sort_keys=True)
        assert "snapshot-pairing-secret" not in serialized
        assert "dispatch-pairing-secret" not in serialized
        assert "management-pairing-secret" not in serialized

        snapshot = await inference.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer snapshot-pairing-secret"},
        )
        probe = await inference.get(
            "/v1/models",
            headers={
                "Authorization": "Bearer dispatch-pairing-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
        )
        rejected = await inference.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer dispatch-pairing-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
            json={"model": "frontier", "messages": []},
        )
        assert snapshot.status_code == 200
        assert probe.status_code == 200
        assert rejected.status_code == 503
        assert rejected.json()["detail"]["code"] == "fleet_pairing_not_active"
        assert upstream_calls == 0

        await runtime.fleet_pairing.mark_paired()
        await asyncio.to_thread(
            runtime.fleet_pairing_client._journal.record_complete
        )
        paired = await runtime.fleet_pairing_status()
        assert paired["state"] == "paired"
        admitted = await inference.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer dispatch-pairing-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
            json={"model": "frontier", "messages": []},
        )
        assert admitted.status_code == 200
        assert upstream_calls == 1

        removal = await control.post(
            "/manager/fleet/pairing/revoke",
            json={
                "schema_version": 1,
                "request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            },
        )
        assert removal.status_code == 200
        assert removal.json()["result"] == {
            "schema_version": 1,
            "state": "revoked",
            "pairing_id": "22222222-2222-4222-8222-222222222222",
            "reporting_node_id": runtime.usage.identity.node_id,
            "credential_generation": 1,
        }
        assert removal.json()["pairing"]["state"] == "revoked"
        assert removal.json()["pairing"]["self_revoke"] is None
        # Do not poll pairing status here: the successful self-revoke itself
        # must synchronously close the runtime's cached Fleet authority.
        denied_snapshot = await inference.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer snapshot-pairing-secret"},
        )
        denied_probe = await inference.get(
            "/v1/models",
            headers={
                "Authorization": "Bearer dispatch-pairing-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
        )
        assert denied_snapshot.status_code == 503
        assert denied_probe.status_code == 401
        assert upstream_calls == 1
        revoked = await runtime.fleet_pairing_status()
        assert revoked["state"] == "revoked"
        assert revoked["pairing_id"] == "22222222-2222-4222-8222-222222222222"
        assert revoked["reporting_node_id"] == runtime.usage.identity.node_id
        assert revoked["credential_generation"] == 1
        assert revoked["credentials_configured"] is False
        assert all(
            not value
            for value in (
                os.environ.get("FLEET_API_KEY"),
                os.environ.get("FLEET_INFERENCE_API_KEY"),
                os.environ.get("FLEET_MANAGEMENT_API_KEY"),
            )
        )
        assert (await control.get("/manager/fleet/pairing")).json() == revoked
    finally:
        await inference.aclose()
        await control.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_self_revoke_pending_denies_immediately_and_across_restart(
    tmp_path,
    monkeypatch,
) -> None:
    for key in (
        "FLEET_API_KEY",
        "FLEET_INFERENCE_API_KEY",
        "FLEET_MANAGEMENT_API_KEY",
    ):
        monkeypatch.setenv(key, "")

    hub_request_ids: list[str] = []

    def pairing_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        hub_request_ids.append(payload["request_id"])
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "pairing_id": payload["pairing_id"],
                "reporting_node_id": payload["reporting_node_id"],
                "display_name": payload["reporting_node_id"],
                "platform": "macos",
                "service_version": "0.9.0",
                "protocol_version": 1,
                "service_class": "primary",
                "state": "revoked",
                "hub_enabled": False,
                "credential_generation": payload["credential_generation"],
                "created_at": 100.0,
                "updated_at": 101.0,
                "revoked_at": 101.0,
                "failure_code": None,
            },
        )

    config = _config(tmp_path)
    environment = tmp_path / "private" / ".env"
    credentials = PairingCredentials(
        snapshot_key="snapshot-pending-secret",
        dispatch_key="dispatch-pending-secret",
        management_key="management-pending-secret",
    )
    pairing_id = "22222222-2222-4222-8222-222222222222"
    request_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    route_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    runtime = NativeRuntime(
        config,
        env_path=environment,
        adapters=_adapters(),
        pairing_transport=httpx.MockTransport(pairing_handler),
    )
    await runtime.start(raise_on_degraded=True)
    inference = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
    )
    try:
        await _seed_staged_pairing_workflow(
            runtime,
            hub_origin="https://nyx.example.test",
            locator="https://mac.example.test:1240",
            pairing_id=pairing_id,
            credentials=credentials,
        )
        await runtime.fleet_pairing.mark_paired()
        await asyncio.to_thread(
            runtime.fleet_pairing_client._journal.record_complete
        )
        await runtime.fleet_pairing_status()

        async def fail_mark_revoked():
            raise FleetPairingError("injected revoked-tombstone write failure")

        monkeypatch.setattr(
            runtime.fleet_pairing,
            "mark_revoked",
            fail_mark_revoked,
        )
        uncertain = await control.post(
            "/manager/fleet/pairing/revoke",
            json={"schema_version": 1, "request_id": request_id},
        )
        assert uncertain.status_code == 503
        assert uncertain.json()["detail"]["code"] == (
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN.value
        )
        assert uncertain.json()["detail"]["retryable"] is True
        assert uncertain.json()["pairing"]["self_revoke"] == {
            "schema_version": 1,
            "request_id": request_id,
            "phase": "hub_committed",
        }
        assert hub_request_ids == [request_id]
        assert (await runtime.fleet_pairing.status()).state.value == "paired"
        assert runtime.fleet_snapshot_credential_active() is False
        assert runtime.fleet_dispatch_credential_active(
            activation_probe=True
        ) is False

        immediate_snapshot = await inference.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer snapshot-pending-secret"},
        )
        immediate_dispatch = await inference.get(
            "/v1/models",
            headers={
                "Authorization": "Bearer dispatch-pending-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
        )
        assert immediate_snapshot.status_code == 503
        assert immediate_dispatch.status_code == 503
    finally:
        await inference.aclose()
        await control.aclose()
        await runtime.stop()

    restarted = NativeRuntime(
        config,
        env_path=environment,
        adapters=_adapters(),
        pairing_transport=httpx.MockTransport(pairing_handler),
    )
    await restarted.start(raise_on_degraded=True)
    restarted_inference = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(restarted)),
        base_url="http://inference.test",
    )
    restarted_control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(restarted)),
        base_url="http://control.test",
    )
    try:
        assert restarted.fleet_snapshot_credential_active() is False
        assert restarted.fleet_dispatch_credential_active(
            activation_probe=True
        ) is False
        restart_dispatch = await restarted_inference.get(
            "/v1/models",
            headers={
                "Authorization": "Bearer dispatch-pending-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
        )
        assert restart_dispatch.status_code == 503

        recovered = await restarted_control.post(
            "/manager/fleet/pairing/revoke",
            json={"schema_version": 1, "request_id": request_id},
        )
        assert recovered.status_code == 200
        assert recovered.json()["result"]["state"] == "revoked"
        assert recovered.json()["pairing"]["self_revoke"] is None
        assert hub_request_ids == [request_id]
        tombstone = await restarted.fleet_pairing_status()
        assert tombstone["state"] == "revoked"
        assert tombstone["pairing_id"] == pairing_id
        assert tombstone["credential_generation"] == 1
        assert tombstone["credentials_configured"] is False
        assert all(
            not os.environ.get(key)
            for key in (
                "FLEET_API_KEY",
                "FLEET_INFERENCE_API_KEY",
                "FLEET_MANAGEMENT_API_KEY",
            )
        )
        old_dispatch = await restarted_inference.get(
            "/v1/models",
            headers={
                "Authorization": "Bearer dispatch-pending-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
        )
        assert old_dispatch.status_code == 401
    finally:
        await restarted_inference.aclose()
        await restarted_control.aclose()
        await restarted.stop()


@pytest.mark.asyncio
async def test_presence_pairing_request_is_bounded_and_does_not_start_locally(
    tmp_path,
    monkeypatch,
) -> None:
    for key in (
        "FLEET_API_KEY",
        "FLEET_INFERENCE_API_KEY",
        "FLEET_MANAGEMENT_API_KEY",
    ):
        monkeypatch.setenv(key, "")

    pairing_secret = "presence-invitation-secret-that-is-long-enough-1234"
    hub_requests: list[dict[str, object]] = []

    def hub(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        hub_requests.append(payload)
        return httpx.Response(
            201,
            json={
                "schema_version": 1,
                "invitation_id": "11111111-1111-4111-8111-111111111111",
                "pairing_secret": pairing_secret,
                "presence_pin": _presence_pin(pairing_secret),
                "hub_origin": "https://nyx.private.example",
                "expires_at": 4_000_000_000.0,
                "state": "issued",
            },
        )

    runtime = NativeRuntime(
        _config(tmp_path),
        env_path=tmp_path / "private" / ".env",
        adapters=_adapters(),
        pairing_transport=httpx.MockTransport(hub),
    )
    await runtime.start(raise_on_degraded=True)
    control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
    )
    request = {
        "schema_version": 1,
        "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "hub_origin": "https://nyx.private.example",
        "locator": "http://metis.private.example:1240",
        "transport": "tailscale",
    }
    try:
        response = await control.post(
            "/manager/fleet/pairing/request",
            json=request,
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "schema_version": 1,
            "presence_pin": _presence_pin(pairing_secret),
            "expires_at": 4_000_000_000.0,
            "invitation_id": "11111111-1111-4111-8111-111111111111",
            "pairing_secret": pairing_secret,
            "hub_origin": "https://nyx.private.example",
            "locator": "http://metis.private.example:1240",
        }
        assert hub_requests[0]["request_id"] == request["request_id"]
        assert hub_requests[0]["locator"] == request["locator"]
        assert (await runtime.fleet_pairing_status())["state"] == "unpaired"

        private_locator = "http://must-not-reflect.private:1240"
        malformed = await control.post(
            "/manager/fleet/pairing/request",
            json={**request, "locator": private_locator, "unexpected": "value"},
        )
        assert malformed.status_code == 400
        assert private_locator not in malformed.text
    finally:
        await control.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_pairing_hub_failure_is_secret_safe_and_inference_neutral(
    tmp_path,
    monkeypatch,
) -> None:
    for key in (
        "FLEET_API_KEY",
        "FLEET_INFERENCE_API_KEY",
        "FLEET_MANAGEMENT_API_KEY",
    ):
        monkeypatch.setenv(key, "")

    hub_calls = 0

    def unavailable_hub(request: httpx.Request) -> httpx.Response:
        nonlocal hub_calls
        hub_calls += 1
        raise httpx.ConnectError("unavailable", request=request)

    adapters = _adapters()
    environment = tmp_path / "private" / ".env"
    runtime = NativeRuntime(
        _config(tmp_path),
        env_path=environment,
        adapters=adapters,
        pairing_transport=httpx.MockTransport(unavailable_hub),
    )
    await runtime.start(raise_on_degraded=True)
    control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
    )
    inference = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    pairing_secret = "invitation-secret-that-is-long-enough-1234567890"
    locator = "https://mac-a.private.example:1240"
    hub_origin = "https://nyx.private.example"
    request_body = {
        "schema_version": 1,
        "invitation_id": "11111111-1111-4111-8111-111111111111",
        "pairing_secret": pairing_secret,
        "hub_origin": hub_origin,
        "locator": locator,
    }
    try:
        before_participation = await runtime.fleet_participation.status()
        before_installs = await runtime.installer.list()
        before_usage = await runtime.usage.status()
        before_storage = runtime.config.storage.model_dump(mode="json")
        before_profiles = tuple(runtime.profiles)

        failed = await control.post(
            "/manager/fleet/pairing/begin",
            json=request_body,
        )
        assert failed.status_code == 503
        assert failed.json()["detail"] == {
            "code": "pairing_hub_unavailable",
            "message": (
                "The Hub is temporarily unavailable; the exact request can be "
                "resumed."
            ),
            "retryable": True,
        }
        serialized = failed.text
        assert pairing_secret not in serialized
        assert locator not in serialized
        assert hub_origin not in serialized
        assert str(tmp_path) not in serialized

        malformed_secret = "secret-value-that-must-never-be-reflected-123456"
        malformed = await control.post(
            "/manager/fleet/pairing/resume",
            json={
                **request_body,
                "pairing_secret": malformed_secret,
                "unexpected": locator,
            },
        )
        assert malformed.status_code == 400
        assert malformed_secret not in malformed.text
        assert locator not in malformed.text

        local_models = await inference.get("/v1/models")
        assert local_models.status_code == 200
        assert hub_calls == 1
        assert tuple(runtime.profiles) == before_profiles
        assert runtime.config.storage.model_dump(mode="json") == before_storage
        assert await runtime.installer.list() == before_installs
        assert await runtime.fleet_participation.status() == before_participation
        after_usage = await runtime.usage.status()
        assert after_usage["outbox_depth"] == before_usage["outbox_depth"]
        assert all(adapter.loads == 0 for adapter in adapters.values())
        assert not environment.exists()
        assert runtime.startup_error is None
    finally:
        await inference.aclose()
        await control.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_rejected_claim_can_be_discarded_through_local_control(
    tmp_path,
    monkeypatch,
) -> None:
    for key in (
        "FLEET_API_KEY",
        "FLEET_INFERENCE_API_KEY",
        "FLEET_MANAGEMENT_API_KEY",
    ):
        monkeypatch.setenv(key, "")

    hub_invitation_ids: list[str] = []

    def rejecting_hub(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        hub_invitation_ids.append(payload["invitation_id"])
        return httpx.Response(
            401,
            json={"detail": {"code": "pairing_claim_rejected"}},
        )

    runtime = NativeRuntime(
        _config(tmp_path),
        env_path=tmp_path / "private" / ".env",
        adapters=_adapters(),
        pairing_transport=httpx.MockTransport(rejecting_hub),
    )
    await runtime.start(raise_on_degraded=True)
    control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
    )
    first = {
        "schema_version": 1,
        "invitation_id": "11111111-1111-4111-8111-111111111111",
        "pairing_secret": "first-invitation-secret-that-is-long-enough-1234",
        "hub_origin": "https://nyx.private.example",
        "locator": "http://metis.private.example:1240",
    }
    second = {
        **first,
        "invitation_id": "22222222-2222-4222-8222-222222222222",
        "pairing_secret": "second-invitation-secret-that-is-long-enough-123",
    }
    try:
        rejected = await control.post(
            "/manager/fleet/pairing/begin",
            json=first,
        )
        assert rejected.status_code == 401
        pending = await control.get("/manager/fleet/pairing")
        assert pending.json()["state"] == "pending"
        assert pending.json()["workflow"]["phase"] == "claiming"
        assert pending.json()["workflow"]["last_error_code"] == (
            "pairing_claim_rejected"
        )

        discarded = await control.post(
            "/manager/fleet/pairing/discard-rejected"
        )
        assert discarded.status_code == 200
        assert discarded.json()["state"] == "unpaired"
        assert discarded.json()["credentials_configured"] is False
        assert discarded.json()["workflow"]["phase"] is None

        second_rejected = await control.post(
            "/manager/fleet/pairing/begin",
            json=second,
        )
        assert second_rejected.status_code == 401
        assert hub_invitation_ids == [
            first["invitation_id"],
            second["invitation_id"],
        ]
    finally:
        await control.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_refresh_confirms_and_discards_terminal_pairing_only(
    tmp_path,
    monkeypatch,
) -> None:
    for key in (
        "FLEET_API_KEY",
        "FLEET_INFERENCE_API_KEY",
        "FLEET_MANAGEMENT_API_KEY",
    ):
        monkeypatch.setenv(key, "")

    claim_id = "22222222-2222-4222-8222-222222222222"
    pairing_id = "33333333-3333-4333-8333-333333333333"
    invitation_id = "11111111-1111-4111-8111-111111111111"
    expires_at = 4_000_000_000.0
    claim_request_ids: list[str] = []
    runtime: NativeRuntime

    def hub(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/fleet/pairing/v1/claims":
            claim_request_ids.append(payload["request_id"])
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "claim_id": claim_id,
                    "invitation_id": invitation_id,
                    "pairing_id": pairing_id,
                    "display_name": runtime.usage.identity.node_id,
                    "reporting_node_id": runtime.usage.identity.node_id,
                    "service_version": "0.9.0",
                    "platform": "macos",
                    "protocol_version": 1,
                    "state": "claimed",
                    "claimed_at": 100.0,
                    "expires_at": expires_at,
                    "locator_accepted": True,
                },
            )
        if request.url.path.endswith("/provision"):
            return httpx.Response(
                410,
                json={"detail": {"code": "pairing_claim_terminal"}},
            )
        if request.url.path.endswith("/status"):
            assert payload == {
                "schema_version": 1,
                "claim_request_id": claim_request_ids[0],
            }
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "claim_id": claim_id,
                    "invitation_id": invitation_id,
                    "pairing_id": pairing_id,
                    "reporting_node_id": runtime.usage.identity.node_id,
                    "state": "expired",
                    "expires_at": expires_at,
                },
            )
        raise AssertionError(request.url.path)

    runtime = NativeRuntime(
        _config(tmp_path),
        env_path=tmp_path / "private" / ".env",
        adapters=_adapters(),
        pairing_transport=httpx.MockTransport(hub),
    )
    await runtime.start(raise_on_degraded=True)
    control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
    )
    request = {
        "schema_version": 1,
        "invitation_id": invitation_id,
        "pairing_secret": "invitation-secret-that-is-long-enough-1234567890",
        "hub_origin": "https://nyx.private.example",
        "locator": "http://metis.private.example:1240",
    }
    try:
        usage_before = await runtime.usage.status()
        pending = await control.post("/manager/fleet/pairing/begin", json=request)
        assert pending.status_code == 202

        refreshed = await control.post("/manager/fleet/pairing/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["workflow"]["last_error_code"] == (
            "pairing_remote_attempt_terminal"
        )

        discarded = await control.post(
            "/manager/fleet/pairing/discard-terminal"
        )
        assert discarded.status_code == 200
        assert discarded.json()["state"] == "unpaired"
        assert discarded.json()["workflow"]["phase"] is None
        usage_after = await runtime.usage.status()
        assert usage_after["outbox_depth"] == usage_before["outbox_depth"]
        assert all(adapter.loads == 0 for adapter in runtime.adapters.values())
    finally:
        await control.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_pairing_staging_is_probe_visible_before_activation_ack(
    tmp_path,
    monkeypatch,
) -> None:
    for key in (
        "FLEET_API_KEY",
        "FLEET_INFERENCE_API_KEY",
        "FLEET_MANAGEMENT_API_KEY",
    ):
        monkeypatch.setenv(key, "")

    runtime: NativeRuntime
    pairing_id = "33333333-3333-4333-8333-333333333333"
    claim_id = "22222222-2222-4222-8222-222222222222"
    invitation_id = "11111111-1111-4111-8111-111111111111"
    pairing_secret = "invitation-secret-that-is-long-enough-1234567890"
    credentials = {
        "snapshot_bearer": "snapshot-generation-one",
        "dispatch_bearer": "dispatch-generation-one",
        "management_bearer": "management-generation-one",
    }
    observed_probe_authority: list[tuple[bool, bool, bool]] = []

    def hub(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/fleet/pairing/v1/claims":
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "claim_id": claim_id,
                    "invitation_id": invitation_id,
                    "pairing_id": pairing_id,
                    "display_name": runtime.usage.identity.node_id,
                    "reporting_node_id": runtime.usage.identity.node_id,
                    "service_version": "0.9.0",
                    "platform": "macos",
                    "protocol_version": 1,
                    "state": "claimed",
                    "claimed_at": 100.0,
                    "expires_at": 4_000_000_000.0,
                    "locator_accepted": True,
                },
            )
        if request.url.path.endswith("/provision"):
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "claim_id": claim_id,
                    "pairing_id": pairing_id,
                    "reporting_node_id": runtime.usage.identity.node_id,
                    "credential_generation": 1,
                    "credentials": credentials,
                    "state": "provisioning",
                },
            )
        observed_probe_authority.append(
            (
                runtime.fleet_snapshot_credential_active(),
                runtime.fleet_dispatch_credential_active(activation_probe=True),
                runtime.fleet_dispatch_credential_active(activation_probe=False),
            )
        )
        assert request.headers["authorization"] == (
            f"Bearer {credentials['management_bearer']}"
        )
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "pairing_id": pairing_id,
                "reporting_node_id": runtime.usage.identity.node_id,
                "display_name": runtime.usage.identity.node_id,
                "platform": "macos",
                "service_version": "0.9.0",
                "protocol_version": 1,
                "service_class": "primary",
                "state": "disabled",
                "hub_enabled": False,
                "credential_generation": 1,
                "created_at": 100.0,
                "updated_at": 101.0,
                "revoked_at": None,
                "failure_code": None,
                "activation_complete": True,
            },
        )

    adapters = _adapters()
    runtime = NativeRuntime(
        _config(tmp_path),
        env_path=tmp_path / "private" / ".env",
        adapters=adapters,
        pairing_transport=httpx.MockTransport(hub),
    )
    await runtime.start(raise_on_degraded=True)
    control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
    )
    try:
        before_participation = await runtime.fleet_participation.status()
        paired = await control.post(
            "/manager/fleet/pairing/begin",
            json={
                "schema_version": 1,
                "invitation_id": invitation_id,
                "pairing_secret": pairing_secret,
                "hub_origin": "https://nyx.example.test",
                "locator": "https://studio.example.test:1240",
            },
        )
        assert paired.status_code == 200
        assert paired.json()["workflow"]["phase"] == "complete"
        assert observed_probe_authority == [(True, True, False)]
        assert (await runtime.fleet_pairing_status())["state"] == "paired"
        assert await runtime.fleet_participation.status() == before_participation
        assert all(adapter.loads == 0 for adapter in adapters.values())
    finally:
        await control.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_fleet_snapshot_uses_dedicated_auth_and_path_free_v1_shape(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("FLEET_API_KEY", raising=False)
    monkeypatch.delenv("FLEET_INFERENCE_API_KEY", raising=False)
    monkeypatch.setenv("INFERENCE_API_KEY", "inference-secret")
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    try:
        assert (await client.get("/fleet/v1/snapshot")).status_code == 503

        monkeypatch.setenv("FLEET_API_KEY", "fleet-secret")
        inference_token = await client.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer inference-secret"},
        )
        assert inference_token.status_code == 401

        monkeypatch.delenv("INFERENCE_API_KEY")
        missing_inference_auth = await client.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer fleet-secret"},
        )
        assert missing_inference_auth.status_code == 503
        assert (
            missing_inference_auth.json()["detail"]["code"]
            == "fleet_inference_auth_unconfigured"
        )

        monkeypatch.setenv("FLEET_INFERENCE_API_KEY", "dispatch-secret")
        dedicated_dispatch = await client.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer fleet-secret"},
        )
        assert dedicated_dispatch.status_code == 200
        monkeypatch.delenv("FLEET_INFERENCE_API_KEY")
        monkeypatch.setenv("INFERENCE_API_KEY", "inference-secret")

        first = await client.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer fleet-secret"},
        )
        second = await client.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer fleet-secret"},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        snapshot = first.json()
        schema = json.loads(FLEET_SNAPSHOT_SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(snapshot)
        assert second.json()["snapshot_sequence"] == snapshot["snapshot_sequence"] + 1
        assert set(snapshot) == {
            "schema_version",
            "snapshot_sequence",
            "observed_at",
            "node",
            "health",
            "residency",
            "admission",
            "capacity",
            "deployments",
            "usage_delivery",
        }
        assert snapshot["schema_version"] == 1
        assert set(snapshot["node"]) == {
            "node_id",
            "instance_id",
            "platform",
            "version",
        }
        assert snapshot["node"]["platform"] == "macos"
        assert set(snapshot["health"]) == {
            "state",
            "accepting",
            "authoritative",
            "diagnostic_code",
        }
        assert set(snapshot["residency"]) == {
            "alias",
            "deployment_id",
            "engine",
            "epoch",
            "transition_target",
        }
        assert set(snapshot["admission"]) == {
            "queue_depth",
            "queue_limit",
            "queued_by_deployment",
        }
        capacity_fields = {
            "derived_limit",
            "configured_max_concurrency",
            "effective_limit",
            "active",
            "queued",
            "available",
            "source",
            "confidence",
            "saturation",
        }
        assert set(snapshot["capacity"]) == capacity_fields
        assert set(snapshot["usage_delivery"]) == {
            "enabled",
            "writer_ready",
            "outbox_pending",
            "last_flush_at",
            "last_error_code",
        }
        deployment = snapshot["deployments"][0]
        assert set(deployment) == {
            "alias",
            "deployment_id",
            "identity",
            "identity_confidence",
            "fleet_eligible",
            "loadable",
            "warm",
            "capacity",
        }
        assert set(deployment["capacity"]) == capacity_fields
        assert set(deployment["identity"]) == {
            "protocol",
            "engine",
            "upstream_model",
            "resolved_revision",
            "artifact",
            "kind",
            "capabilities",
            "load_config_digest",
        }
        assert set(deployment["identity"]["artifact"]) == {
            "format",
            "selected_files",
            "quantization",
            "content_digest",
        }
        assert deployment["identity_confidence"] == "unverified"
        assert deployment["fleet_eligible"] is False
        serialized = json.dumps(snapshot, sort_keys=True)
        assert str(tmp_path) not in serialized
        assert "inference-secret" not in serialized
        assert "fleet-secret" not in serialized
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_signed_omlx_global_drift_closes_only_its_fleet_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A durable signed binding survives store faults and is rechecked cold."""

    storage_root = tmp_path / "selected-model-folder"
    storage_root.mkdir()
    destination = storage_root / "omlx" / "publisher" / "model"
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"omlx": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "selected",
                "locations": [
                    {"name": "selected", "path": str(storage_root)}
                ],
            },
            "models": [
                {
                    "alias": "signed-omlx",
                    "engine": "omlx",
                    "model": "model",
                    "storage": "selected",
                },
                {
                    "alias": "ordinary-local",
                    "engine": "omlx",
                    "model": "local-only",
                    "storage": "selected",
                },
            ],
        }
    )
    # Seed the durable install journal before constructing the service. The
    # runtime must recover the binding on a cold restart without YAML fields.
    install_store = InstallStore(config.paths.state_database)
    record = install_store.create(
        repo_id="publisher/model",
        engine="omlx",
        storage="selected",
        alias="signed-omlx",
        destination=str(destination),
        revision="a" * 40,
        filename=None,
        download_files=["weights.safetensors"],
        family=None,
        launch_contract={
            "engine": "omlx",
            "scheduler_slots": 2,
            "memory_guard": "required",
        },
    )
    install_store.update(record.id, status="installed")
    install_store.close()

    adapter = ContractOMLXAdapter()
    runtime = NativeRuntime(
        config,
        adapters={EngineName.OMLX: adapter},
    )

    signed = runtime.resolve("signed-omlx")
    ordinary = runtime.resolve("ordinary-local")
    assert omlx_target_launch(signed.load_options) == OMLXInstallLaunch(
        engine="omlx",
        scheduler_slots=2,
        memory_guard="required",
    )
    assert OMLX_TARGET_LAUNCH_KEY not in ordinary.load_options

    def unavailable_index(**_kwargs):
        raise RuntimeError("install journal read fault")

    monkeypatch.setattr(
        runtime.installer.store,
        "signed_launch_records",
        unavailable_index,
    )
    runtime._apply_profiles(config)  # noqa: SLF001 - post-start refresh fault

    # The exact signed marker is retained, while an independently proved
    # ordinary local target is not spuriously converted into a signed one.
    assert omlx_target_launch(
        runtime.resolve("signed-omlx").load_options
    ) is not None
    assert (
        OMLX_TARGET_LAUNCH_KEY
        not in runtime.resolve("ordinary-local").load_options
    )

    await runtime.start(raise_on_degraded=True)
    try:
        before = await runtime.fleet_snapshot()
        signed_before = next(
            item
            for item in before["deployments"]
            if item["alias"] == "signed-omlx"
        )
        assert signed_before["identity_confidence"] == "authoritative"
        assert signed_before["fleet_eligible"] is True
        assert signed_before["loadable"] is True
        assert signed_before["capacity"]["available"] == 1
        assert {row["id"] for row in runtime.model_list()} == {
            "signed-omlx",
            "ordinary-local",
        }
        assert (await runtime.coordinator.status()).resident_alias is None
        assert adapter.loads == 0

        adapter.slots = 1
        after = await runtime.fleet_snapshot()
        signed_after = next(
            item
            for item in after["deployments"]
            if item["alias"] == "signed-omlx"
        )
        assert signed_after["identity_confidence"] == "unverified"
        assert signed_after["fleet_eligible"] is False
        assert signed_after["loadable"] is False
        assert signed_after["capacity"]["available"] == 0
        assert {row["id"] for row in runtime.model_list()} == {
            "signed-omlx",
            "ordinary-local",
        }
        assert (await runtime.coordinator.status()).resident_alias is None
        assert adapter.loads == 0
        with pytest.raises(RuntimeConfigurationError, match="does not prove"):
            await runtime.resolve_target("signed-omlx")
        assert adapter.loads == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_fleet_participation_control_and_request_gating_are_local_only(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("FLEET_INFERENCE_API_KEY", "fleet-dispatch-secret")
    upstream_calls = 0

    def upstream_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={"choices": []})

    adapters = _adapters()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=adapters,
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    inference = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
    )
    request = {"model": "frontier", "messages": []}
    route_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    try:
        assert (
            await control.get("/manager/fleet/participation")
        ).status_code == 401
        initial = await control.get(
            "/manager/fleet/participation",
            auth=("admin", "admin-secret"),
        )
        assert initial.status_code == 200
        assert set(initial.json()) == {
            "enabled",
            "state",
            "active_requests",
            "updated_at",
        }
        assert initial.json()["enabled"] is True
        assert initial.json()["state"] == "joined"

        paused = await control.put(
            "/manager/fleet/participation",
            auth=("admin", "admin-secret"),
            json={"enabled": False},
        )
        assert paused.status_code == 200
        assert paused.json()["enabled"] is False
        assert paused.json()["state"] == "paused"
        assert paused.json()["active_requests"] == 0

        fleet = await inference.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer fleet-dispatch-secret",
                "X-Mnemosyne-Fleet-Route": route_id,
            },
            json=request,
        )
        assert fleet.status_code == 429
        assert fleet.headers["retry-after"] == "1"
        assert fleet.headers["x-mnemosyne-error"] == "node_busy"
        assert fleet.json()["detail"]["code"] == "node_busy"
        assert adapters[EngineName.OMLX].loads == 0
        assert upstream_calls == 0

        # Missing markers are local. Present-but-invalid markers fail closed
        # instead of silently bypassing the Fleet-only participation gate.
        local = await inference.post("/v1/chat/completions", json=request)
        malformed = await inference.post(
            "/v1/chat/completions",
            headers={"X-Mnemosyne-Fleet-Route": "not-a-uuid"},
            json=request,
        )
        duplicate = await inference.post(
            "/v1/chat/completions",
            headers=[
                ("X-Mnemosyne-Fleet-Route", route_id),
                (
                    "X-Mnemosyne-Fleet-Route",
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                ),
            ],
            json=request,
        )
        assert local.status_code == 200
        assert malformed.status_code == 400
        assert malformed.json()["detail"]["code"] == "invalid_fleet_route"
        assert duplicate.status_code == 400
        assert duplicate.json()["detail"]["code"] == "invalid_fleet_route"
        assert adapters[EngineName.OMLX].loads == 1
        assert upstream_calls == 1
    finally:
        await inference.aclose()
        await control.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_paused_participation_closes_fleet_snapshot_capacity(tmp_path) -> None:
    runtime = NativeRuntime(_config(tmp_path), adapters=_adapters())
    await runtime.start(raise_on_degraded=True)
    try:
        await runtime.fleet_participation.set_joined(False)
        snapshot = await runtime.fleet_snapshot()
        schema = json.loads(FLEET_SNAPSHOT_SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(snapshot)

        assert snapshot["health"] == {
            "state": "idle",
            "accepting": False,
            "authoritative": True,
            "diagnostic_code": "fleet_participation_paused",
        }
        assert snapshot["capacity"]["available"] == 0
        assert all(
            deployment["capacity"]["available"] == 0
            and deployment["loadable"] is False
            for deployment in snapshot["deployments"]
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_accounting_reservation_closes_fleet_snapshot_capacity(tmp_path) -> None:
    config = _config(tmp_path)
    config.token_sidecar.max_outbox_rows = 1
    runtime = NativeRuntime(config, adapters=_adapters())
    await runtime.start(raise_on_degraded=True)
    reservation = None
    try:
        baseline = await runtime.fleet_snapshot()
        assert baseline["health"]["accepting"] is True
        assert any(
            deployment["capacity"]["available"] > 0
            for deployment in baseline["deployments"]
        )

        reservation = await runtime.usage.reserve(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            fleet_route=True,
            requires_accounting=True,
        )
        snapshot = await runtime.fleet_snapshot()
        schema = json.loads(FLEET_SNAPSHOT_SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(snapshot)

        assert snapshot["health"]["accepting"] is False
        assert snapshot["health"]["diagnostic_code"] == "usage_outbox_full"
        assert snapshot["capacity"]["available"] == 0
        assert snapshot["usage_delivery"]["last_error_code"] == "outbox_full"
        assert all(
            deployment["capacity"]["available"] == 0
            and deployment["loadable"] is False
            for deployment in snapshot["deployments"]
        )

        await reservation.finish()
        reservation = None
        restored = await runtime.fleet_snapshot()
        assert restored["health"]["accepting"] is True
        assert restored["health"]["diagnostic_code"] is None
        assert any(
            deployment["capacity"]["available"] > 0
            for deployment in restored["deployments"]
        )
    finally:
        if reservation is not None:
            await reservation.finish()
        await runtime.stop()


@pytest.mark.asyncio
async def test_participation_does_not_mask_coordinator_diagnostics(tmp_path) -> None:
    runtime = NativeRuntime(_config(tmp_path), adapters=_adapters())
    await runtime.start(raise_on_degraded=True)
    await runtime.fleet_participation.set_joined(False)
    try:
        runtime.startup_error = "synthetic startup failure"
        assert (
            await runtime.fleet_snapshot()
        )["health"]["diagnostic_code"] == "startup_error"

        runtime.startup_error = None
        runtime.coordinator._state = CoordinatorState.DEGRADED  # noqa: SLF001
        runtime.coordinator._diagnostic = "synthetic drift"  # noqa: SLF001
        assert (
            await runtime.fleet_snapshot()
        )["health"]["diagnostic_code"] == "coordinator_degraded"

        runtime.coordinator._state = CoordinatorState.IDLE  # noqa: SLF001
        runtime.coordinator._diagnostic = None  # noqa: SLF001
        runtime.coordinator._initialized = False  # noqa: SLF001
        assert (
            await runtime.fleet_snapshot()
        )["health"]["diagnostic_code"] == "not_initialized"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_pause_during_fleet_stream_drains_then_pauses(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLEET_INFERENCE_API_KEY", "fleet-dispatch-secret")
    stream_gate = asyncio.Event()
    upstream_closed = asyncio.Event()
    upstream_calls = 0

    class StreamingUpstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            await stream_gate.wait()
            yield (
                b'data: {"choices":[],"usage":{"prompt_tokens":1,'
                b'"completion_tokens":1,"total_tokens":2}}\n\n'
            )
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            upstream_closed.set()

    class ProxyClient:
        def build_request(self, **kwargs) -> httpx.Request:
            return httpx.Request(
                kwargs["method"],
                kwargs["url"],
                headers=kwargs.get("headers"),
                content=kwargs.get("content"),
                params=kwargs.get("params"),
            )

        async def send(self, _request: httpx.Request, *, stream: bool):
            nonlocal upstream_calls
            assert stream is True
            upstream_calls += 1
            return StreamingUpstream()

    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=_adapters(),
        proxy_client=ProxyClient(),  # type: ignore[arg-type]
    )
    await runtime.start(raise_on_degraded=True)
    app = create_inference_app(runtime)
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", None) == "/v1/chat/completions"
    )
    request_body = json.dumps(
        {"model": "frontier", "messages": [], "stream": True}
    ).encode()
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (
                    b"x-mnemosyne-fleet-route",
                    b"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                ),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 1240),
        },
        receive,
    )
    response = await route.endpoint(request)
    assert isinstance(response, StreamingResponse)
    iterator = response.body_iterator.__aiter__()
    try:
        assert b'"content":"hi"' in await iterator.__anext__()
        status = await runtime.fleet_participation.status()
        assert status.state.value == "joined"
        assert status.active_fleet_requests == 1

        draining = await runtime.fleet_participation.set_joined(False)
        assert draining.state.value == "draining"
        assert draining.active_fleet_requests == 1
        snapshot = await runtime.fleet_snapshot()
        assert snapshot["health"]["state"] == "draining"
        assert snapshot["health"]["accepting"] is False
        assert (
            snapshot["health"]["diagnostic_code"]
            == "fleet_participation_draining"
        )
        assert snapshot["capacity"]["available"] == 0
        assert all(
            deployment["capacity"]["available"] == 0
            and deployment["loadable"] is False
            for deployment in snapshot["deployments"]
        )

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://inference.test",
        )
        try:
            rejected = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer fleet-dispatch-secret",
                    "X-Mnemosyne-Fleet-Route": (
                        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                    )
                },
                json={"model": "frontier", "messages": []},
            )
            assert rejected.status_code == 429
            assert upstream_calls == 1
        finally:
            await client.aclose()

        stream_gate.set()
        assert [chunk async for chunk in iterator] == [b"data: [DONE]\n\n"]
        assert upstream_closed.is_set()
        paused = await runtime.fleet_participation.status()
        assert paused.state.value == "paused"
        assert paused.active_fleet_requests == 0

        closed_snapshot = await runtime.fleet_snapshot()
        assert closed_snapshot["health"]["accepting"] is False
        assert (
            closed_snapshot["health"]["diagnostic_code"]
            == "fleet_participation_paused"
        )
    finally:
        stream_gate.set()
        with contextlib.suppress(Exception):
            await iterator.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_fleet_snapshot_maps_internal_verifying_state_and_transition_target(
    tmp_path,
    monkeypatch,
) -> None:
    class VerifyGateAdapter(FakeAdapter):
        def __init__(self, engine: EngineName) -> None:
            super().__init__(engine)
            self.verify_started = asyncio.Event()
            self.verify_gate = asyncio.Event()

        async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
            snapshot = await super().inspect(deadline=deadline)
            if self.residents and not self.verify_gate.is_set():
                self.verify_started.set()
                await self.verify_gate.wait()
            return snapshot

    monkeypatch.setenv("FLEET_API_KEY", "fleet-secret")
    monkeypatch.setenv("INFERENCE_API_KEY", "inference-secret")
    adapters = _adapters()
    gated = VerifyGateAdapter(EngineName.OMLX)
    adapters[EngineName.OMLX] = gated
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    )
    runtime = NativeRuntime(
        _config(tmp_path),
        adapters=adapters,
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    lease_task = asyncio.create_task(
        runtime.coordinator.acquire(runtime.profiles["frontier"])
    )
    try:
        await asyncio.wait_for(gated.verify_started.wait(), timeout=1)
        assert (await runtime.coordinator.status()).state == (
            CoordinatorState.VERIFYING_TARGET
        )
        response = await client.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer fleet-secret"},
        )
        assert response.status_code == 200
        snapshot = response.json()
        deployment = snapshot["deployments"][0]
        assert snapshot["health"]["state"] == "verifying"
        assert snapshot["residency"]["transition_target"] == (
            deployment["deployment_id"]
        )
    finally:
        gated.verify_gate.set()
        lease = await asyncio.wait_for(lease_task, timeout=1)
        await lease.release()
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_fleet_does_not_report_an_automatic_alternative_as_primary_warm(
    tmp_path,
    monkeypatch,
) -> None:
    """Fleet identities remain exact even when local policy uses an alternative."""

    monkeypatch.setenv("FLEET_API_KEY", "fleet-secret")
    monkeypatch.setenv("INFERENCE_API_KEY", "inference-secret")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {
                "omlx": {"enabled": True},
                "ds4": {"enabled": True},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [
                {
                    "alias": "frontier",
                    "engine": "omlx",
                    "model": "publisher/upstream-model",
                    "alternatives": [
                        {
                            "engine": "ds4",
                            "model": str(tmp_path / "deepseek-v4.gguf"),
                        }
                    ],
                }
            ],
        }
    )
    runtime = NativeRuntime(config, adapters=_adapters())
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    lease = await runtime.coordinator.acquire(
        runtime.profile_candidates["frontier"][1]
    )
    try:
        response = await client.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer fleet-secret"},
        )
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["residency"]["alias"] == "frontier"
        assert snapshot["residency"]["engine"] == "ds4"
        assert snapshot["residency"]["deployment_id"] is None
        assert snapshot["deployments"][0]["warm"] is False
    finally:
        await lease.release()
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_fleet_excludes_alias_pinned_to_a_nonprimary_engine(
    tmp_path,
    monkeypatch,
) -> None:
    """Fleet must not route an immutable primary ID through a pinned alternative."""

    monkeypatch.setenv("FLEET_API_KEY", "fleet-secret")
    monkeypatch.setenv("INFERENCE_API_KEY", "inference-secret")
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {
                "omlx": {"enabled": True},
                "mlxcel": {"enabled": True},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [
                {
                    "alias": "frontier",
                    "engine": "omlx",
                    "model": "publisher/upstream-model",
                    "alternatives": [
                        {
                            "engine": "mlxcel",
                            "model": str(tmp_path / "mlx-model"),
                        }
                    ],
                    "selection": {
                        "mode": "pinned",
                        "pinned_engine": "mlxcel",
                    },
                }
            ],
        }
    )
    runtime = NativeRuntime(config, adapters=_adapters())
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    try:
        response = await client.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer fleet-secret"},
        )

        assert response.status_code == 200
        deployment = response.json()["deployments"][0]
        assert deployment["fleet_eligible"] is False
        assert deployment["loadable"] is False
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_fleet_snapshot_warm_capacity_is_zero_while_resident_drains(
    tmp_path,
) -> None:
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"omlx": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [
                {
                    "alias": "warm",
                    "engine": "llama.cpp",
                    "model": "/models/warm.gguf",
                    "load": {"parallel": 4},
                },
                {
                    "alias": "cold",
                    "engine": "omlx",
                    "model": "publisher/cold",
                },
            ],
        }
    )
    runtime = NativeRuntime(config, adapters=_adapters())
    await runtime.start(raise_on_degraded=True)
    warm_lease = await runtime.coordinator.acquire(runtime.profiles["warm"])
    successor = asyncio.create_task(
        runtime.coordinator.acquire(runtime.profiles["cold"])
    )
    try:
        for _ in range(50):
            if (await runtime.coordinator.status()).state == CoordinatorState.DRAINING:
                break
            await asyncio.sleep(0)
        assert (await runtime.coordinator.status()).state == CoordinatorState.DRAINING

        snapshot = await runtime.fleet_snapshot()
        by_alias = {item["alias"]: item for item in snapshot["deployments"]}
        assert by_alias["warm"]["warm"] is True
        assert by_alias["warm"]["capacity"]["effective_limit"] == 4
        assert by_alias["warm"]["capacity"]["available"] == 0
        assert by_alias["cold"]["warm"] is False
        assert by_alias["cold"]["capacity"]["available"] == 1
    finally:
        await warm_lease.release()
        cold_lease = await asyncio.wait_for(successor, timeout=1)
        await cold_lease.release()
        await runtime.stop()


@pytest.mark.asyncio
async def test_fleet_snapshot_aggregates_synonym_queues_by_deployment_id(
    tmp_path,
) -> None:
    destination = tmp_path / "managed"
    filename = "model-Q4_K_M.gguf"
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [
                {
                    "alias": "primary",
                    "engine": "llama.cpp",
                    "model": str(destination / filename),
                    "storage": "internal",
                },
                {
                    "alias": "synonym",
                    "engine": "llama.cpp",
                    "model": str(destination / filename),
                    "storage": "internal",
                },
            ],
        }
    )
    runtime = NativeRuntime(config, adapters=_adapters())

    async def latest_for_alias(alias: str) -> InstallRecord:
        return InstallRecord(
            id=f"install-{alias}",
            repo_id="publisher/shared-GGUF",
            engine="llama.cpp",
            storage="internal",
            alias=alias,
            destination=str(destination),
            status="installed",
            revision="a" * 40,
            filename=filename,
        )

    runtime.installer.latest_for_alias = latest_for_alias  # type: ignore[method-assign]
    await runtime.start(raise_on_degraded=True)
    resident = await runtime.coordinator.acquire(runtime.profiles["primary"])
    first_waiter = asyncio.create_task(
        runtime.coordinator.acquire(runtime.profiles["primary"])
    )
    second_waiter = asyncio.create_task(
        runtime.coordinator.acquire(runtime.profiles["synonym"])
    )
    try:
        for _ in range(50):
            if (await runtime.coordinator.status()).queued == 2:
                break
            await asyncio.sleep(0)
        assert (await runtime.coordinator.status()).queued == 2

        snapshot = await runtime.fleet_snapshot()
        deployments = snapshot["deployments"]
        assert len({item["deployment_id"] for item in deployments}) == 1
        deployment_id = deployments[0]["deployment_id"]
        assert snapshot["admission"]["queued_by_deployment"] == {
            deployment_id: 2
        }
        assert {
            item["capacity"]["queued"] for item in deployments
        } == {2}
    finally:
        first_waiter.cancel()
        second_waiter.cancel()
        await asyncio.gather(
            first_waiter,
            second_waiter,
            return_exceptions=True,
        )
        await resident.release()
        await runtime.stop()


@pytest.mark.asyncio
async def test_full_waiter_queue_returns_stable_429_before_upstream_work(
    tmp_path,
) -> None:
    config_payload = _config(tmp_path).model_dump(mode="json")
    config_payload["server"]["max_queue_depth"] = 1
    config = MacConfig.model_validate(config_payload)
    upstream_calls = 0

    def upstream_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={"choices": []})

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        config,
        adapters=_adapters(),
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://inference.test",
    )
    resident = await runtime.coordinator.acquire(runtime.profiles["frontier"])
    waiting_request = asyncio.create_task(
        client.post(
            "/v1/chat/completions",
            json={"model": "frontier", "messages": []},
        )
    )
    try:
        for _ in range(50):
            if (await runtime.coordinator.status()).queued == 1:
                break
            await asyncio.sleep(0)
        assert (await runtime.coordinator.status()).queued == 1

        rejected = await client.post(
            "/v1/chat/completions",
            json={"model": "frontier", "messages": []},
        )
        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == "1"
        assert rejected.headers["x-mnemosyne-error"] == "node_busy"
        assert rejected.json()["detail"]["code"] == "node_busy"
        assert upstream_calls == 0

        await resident.release()
        admitted = await asyncio.wait_for(waiting_request, timeout=1)
        assert admitted.status_code == 200
        assert upstream_calls == 1
    finally:
        if not waiting_request.done():
            waiting_request.cancel()
        await resident.release()
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_desired_install_control_is_read_refuse_only_and_inference_neutral(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "desired-admin-secret")
    monkeypatch.setattr(
        runtime_module,
        "SIGNED_LAUNCH_MATERIALIZATION_ENABLED",
        False,
    )
    adapters = _adapters()
    runtime = NativeRuntime(_config(tmp_path), adapters=adapters)
    await runtime.start(raise_on_degraded=True)
    control = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
    )
    authenticated = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
        auth=("admin", "desired-admin-secret"),
    )
    try:
        example = (
            Path(__file__).resolve().parents[3]
            / "mac_pool_protocol"
            / "v1"
            / "desired_install.example.json"
        )
        job = json.loads(example.read_text(encoding="utf-8"))
        now = time.time()
        job["created_at"] = now
        job["expires_at"] = now + job["valid_for_seconds"]
        await runtime.desired_install_store.receive(job)

        before_profiles = tuple(runtime.profiles)
        before_storage = runtime.config.storage.model_dump(mode="json")
        before_installs = await runtime.installer.list()

        unauthenticated = await control.get(
            "/manager/fleet/desired-installs"
        )
        assert unauthenticated.status_code == 401

        listed = await authenticated.get(
            "/manager/fleet/desired-installs",
            params={"offset": 0, "limit": 10},
        )
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store"
        payload = listed.json()
        assert payload["executor_available"] is False
        assert payload["approval_available"] is False
        assert payload["total"] == 1
        assert payload["items"][0]["job"] == job
        assert payload["items"][0]["local_actions"] == {
            "refusal_available": True,
            "approval_available": False,
            "cancellation_available": False,
        }
        assert str(tmp_path) not in listed.text

        job_id = job["job_id"]
        read = await authenticated.get(
            f"/manager/fleet/desired-installs/{job_id}"
        )
        assert read.status_code == 200
        assert read.json()["item"] == payload["items"][0]

        unavailable_approval = await authenticated.post(
            f"/manager/fleet/desired-installs/{job_id}/approve",
            json={"schema_version": 1, "job_revision": 1},
        )
        assert unavailable_approval.status_code == 503
        assert unavailable_approval.json()["detail"]["code"] == (
            "desired_install_internal_error"
        )

        refused = await authenticated.post(
            f"/manager/fleet/desired-installs/{job_id}/refuse",
            json={"schema_version": 1, "job_revision": 1},
        )
        assert refused.status_code == 200
        assert refused.headers["cache-control"] == "no-store"
        assert refused.json()["item"]["acknowledgement"]["state"] == "refused"
        assert (
            refused.json()["item"]["acknowledgement"]["result_code"]
            == "local_policy_refused"
        )
        assert refused.json()["item"]["local_actions"] == {
            "refusal_available": False,
            "approval_available": False,
            "cancellation_available": False,
        }

        stale = await authenticated.post(
            f"/manager/fleet/desired-installs/{job_id}/refuse",
            json={"schema_version": 1, "job_revision": 2},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == (
            "desired_install_revision_changed"
        )
        unknown = await authenticated.get(
            "/manager/fleet/desired-installs/"
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == (
            "desired_install_job_unknown"
        )

        assert tuple(runtime.profiles) == before_profiles
        assert runtime.config.storage.model_dump(mode="json") == before_storage
        assert await runtime.installer.list() == before_installs
        assert all(adapter.loads == 0 for adapter in adapters.values())
    finally:
        await control.aclose()
        await authenticated.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_desired_install_local_approval_and_cancel_are_revision_fenced(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "desired-executor-secret")

    class FakeExecutor:
        def __init__(self, runtime: NativeRuntime) -> None:
            self.runtime = runtime
            self.approvals = 0
            self.cancellations = 0

        async def approve(self, job_id: str):
            self.approvals += 1
            record = await self.runtime.desired_install_store.get(job_id)
            assert record is not None
            transition = await self.runtime.desired_install_store.transition(
                job_id=job_id,
                job_revision=record.document.job_revision,
                state="accepted",
                bytes_downloaded=0,
                total_bytes=100,
                result_code=None,
                installation_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
            return transition.record

        async def cancel_locally(self, job_id: str):
            self.cancellations += 1
            record = await self.runtime.desired_install_store.get(job_id)
            assert record is not None
            transition = await self.runtime.desired_install_store.transition(
                job_id=job_id,
                job_revision=record.document.job_revision,
                state="cancelled",
                bytes_downloaded=record.bytes_downloaded,
                total_bytes=record.total_bytes,
                result_code="cancelled_locally",
                installation_id=record.installation_id,
            )
            return transition.record

        async def reconcile(self, job_id: str):
            record = await self.runtime.desired_install_store.get(job_id)
            assert record is not None
            return record

    adapters = _adapters()
    runtime = NativeRuntime(_config(tmp_path), adapters=adapters)
    fake_executor = FakeExecutor(runtime)
    runtime.desired_install_executor = fake_executor
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://control.test",
        auth=("admin", "desired-executor-secret"),
    )
    try:
        example = (
            Path(__file__).resolve().parents[3]
            / "mac_pool_protocol"
            / "v1"
            / "desired_install.example.json"
        )
        job = json.loads(example.read_text(encoding="utf-8"))
        now = time.time()
        job["created_at"] = now
        job["expires_at"] = now + job["valid_for_seconds"]
        await runtime.desired_install_store.receive(job)

        listed = await client.get("/manager/fleet/desired-installs")
        assert listed.status_code == 200
        assert listed.json()["executor_available"] is True
        assert listed.json()["items"][0]["local_actions"] == {
            "refusal_available": True,
            "approval_available": True,
            "cancellation_available": False,
        }

        job_id = job["job_id"]
        stale = await client.post(
            f"/manager/fleet/desired-installs/{job_id}/approve",
            json={"schema_version": 1, "job_revision": 2},
        )
        assert stale.status_code == 409
        assert fake_executor.approvals == 0

        approved = await client.post(
            f"/manager/fleet/desired-installs/{job_id}/approve",
            json={"schema_version": 1, "job_revision": 1},
        )
        assert approved.status_code == 200
        approved_item = approved.json()["item"]
        assert approved_item["acknowledgement"]["state"] == "accepted"
        assert approved_item["acknowledgement"]["installation_id"] == (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        assert approved_item["local_actions"] == {
            "refusal_available": False,
            "approval_available": False,
            "cancellation_available": True,
        }

        too_late_to_refuse = await client.post(
            f"/manager/fleet/desired-installs/{job_id}/refuse",
            json={"schema_version": 1, "job_revision": 1},
        )
        assert too_late_to_refuse.status_code == 409

        cancelled = await client.post(
            f"/manager/fleet/desired-installs/{job_id}/cancel",
            json={"schema_version": 1, "job_revision": 1},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["item"]["acknowledgement"]["state"] == (
            "cancelled"
        )
        assert cancelled.json()["item"]["local_actions"] == {
            "refusal_available": False,
            "approval_available": False,
            "cancellation_available": False,
        }
        assert fake_executor.approvals == 1
        assert fake_executor.cancellations == 1
        assert await runtime.installer.list() == []
        assert all(adapter.loads == 0 for adapter in adapters.values())
    finally:
        await client.aclose()
        await runtime.stop()
