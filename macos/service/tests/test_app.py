from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
from jsonschema import Draft202012Validator
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

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
from mnemosyne_macos.config import MacConfig, load_config, save_config
from mnemosyne_macos.coordinator import CoordinatorError, CoordinatorState
from mnemosyne_macos.engines.base import AdapterError, Deadline, EngineAdapter
from mnemosyne_macos.install_store import InstallRecord
from mnemosyne_macos.model_library import LibraryModel
from mnemosyne_macos.models import (
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
from mnemosyne_macos.runtime import _redact_diagnostic
from mnemosyne_macos.runtime_updates import RuntimeUpdateError


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


def _adapters() -> dict[EngineName, FakeAdapter]:
    return {engine: FakeAdapter(engine) for engine in EngineName}


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
        EngineName.MLXCEL: 25,
        EngineName.MISTRAL_RS: 15,
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
        assert set(seen) == set(EngineName)
        assert [item["engine"] for item in unified.json()["models"]] == [
            "omlx",
            "ds4",
            "mlxcel",
            "mflux",
            "mistral.rs",
            "llama.cpp",
        ]

        seen.clear()
        filtered = await client.get(
            "/manager/model-library/search",
            params={"q": "qwen", "limit": 7, "engine": "llama.cpp"},
        )
        assert filtered.status_code == 200
        assert seen == [EngineName.LLAMA_CPP]
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


@pytest.mark.asyncio
async def test_failed_benchmark_winner_falls_back_before_upstream_work(
    tmp_path,
) -> None:
    class FailingLoadAdapter(FakeAdapter):
        async def runtime_fingerprint(self, *, deadline: Deadline) -> str:
            del deadline
            return "mlxcel-v1"

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
    failing = FailingLoadAdapter(EngineName.MLXCEL)
    adapters[EngineName.MLXCEL] = failing
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
        EngineName.MLXCEL: "mlxcel-v1",
    }
    for target, runtime_id, ttft in (
        (primary, "omlx-v1", 200.0),
        (alternative, "mlxcel-v1", 50.0),
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
            "host": "mlxcel.test",
            "body": {
                "model": "frontier",
                "messages": [{"role": "user", "content": "hi"}],
            },
        }
        assert adapters[EngineName.MLXCEL].loads == 1
        assert adapters[EngineName.OMLX].loads == 0
        rows = await runtime.usage.list_usage()
        assert rows[0]["backend"] == "mlxcel"
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
        for _ in range(50):
            if (await runtime.coordinator.status()).queued == 1:
                break
            await asyncio.sleep(0)

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

        async def check(self, *, refresh: bool = True) -> dict:
            self.events.append(f"check:{refresh}")
            return {
                "channel": "stable",
                "manifest_url": None,
                "checked_at": 1,
                "core_protocol": 1,
                "engines": [],
            }

        async def prepare(self, engine: str, version: str | None = None):
            self.events.append(f"prepare:{engine}:{version}")
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
                ),
            )

        def activate(self, prepared):
            self.events.append(f"activate:{prepared.release.engine}")
            self.current = "1.0.0"
            return SimpleNamespace(
                engine=prepared.release.engine,
                version="1.0.0",
                source_revision="abc",
                root=tmp_path / "runtime",
            )

        def rollback(self, engine: str):
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
        checked = await client.post("/manager/runtime-updates/check")
        assert checked.status_code == 200
        evidence = await client.get("/manager/runtime-updates/evidence")
        assert evidence.status_code == 200
        assert evidence.json()["journal"]["valid"] is True
        installed = await client.post(
            "/manager/runtime-updates/mflux/install",
            json={"version": "1.0.0"},
        )
        assert installed.status_code == 200
        assert installed.json()["activated"]["version"] == "1.0.0"
        rolled_back = await client.post("/manager/runtime-updates/mflux/rollback")
        assert rolled_back.status_code == 200
        assert rolled_back.json()["activated"]["rollback"] is True
        rejected = await client.post(
            "/manager/runtime-updates/mflux/install",
            json={"version": "corrupt"},
        )
        assert rejected.status_code == 400
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
async def test_control_plane_lists_loads_and_unloads_models(tmp_path) -> None:
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    runtime = NativeRuntime(
        _config(tmp_path),
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
        assert evidence_before_delete["installs"][0]["dismissed"] is True
        assert evidence_before_delete["installs"][0]["events"][-1]["event"] == (
            "history_dismissed"
        )

        revision = (await client.get("/manager/config")).json()["revision"]
        deleted = await client.request(
            "DELETE",
            "/manager/models/managed-model",
            json={"revision": revision},
        )

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_files"] is True
        assert deleted.json()["model_count"] == 0
        assert not destination.exists()
        assert model_root.is_dir()
        assert load_config(config_path).models == []
        assert runtime.model_list() == []
        assert await runtime.installer.list() == []
        assert runtime.installer.store.latest_for_alias("managed-model").status == "deleted"
        evidence_after_delete = (
            await client.get("/manager/model-library/install-evidence")
        ).json()
        assert [
            (event["event"], event["status"])
            for event in evidence_after_delete["installs"][0]["events"][-2:]
        ] == [
            ("history_dismissed", "installed"),
            ("status", "deleted"),
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
        )
        try:
            await runtime.start(raise_on_degraded=True)
        finally:
            await runtime.stop()
            await upstream_client.aclose()

    assert activations == [(scope_id, str(selected)), (scope_id, str(selected))]


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
async def test_inference_and_control_auth_are_independent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INFERENCE_API_KEY", "inference-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-secret")
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
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
async def test_fleet_snapshot_uses_dedicated_auth_and_path_free_v1_shape(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("FLEET_API_KEY", raising=False)
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
        assert snapshot["residency"]["engine"] == "mlxcel"
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
