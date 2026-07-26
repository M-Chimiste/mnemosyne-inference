from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from mnemosyne_macos.app import (
    _validated_install_capabilities,
    create_control_app,
    create_inference_app,
)
from mnemosyne_macos.config import MacConfig, load_config, save_config
from mnemosyne_macos.engines.base import Deadline, EngineAdapter
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
    monkeypatch.setenv("CUSTOM_SECRET_KEY", "custom-secret-value")
    monkeypatch.setenv("SHORT_SECRET_KEY", "xy")

    redacted = _redact_diagnostic(
        "Authorization: Bearer secret-session-value "
        "postgresql://writer:password123@nyx/token_sidecar "
        "https://reader:webpass@example.test/metrics "
        "api_key=visible custom-secret-value xy",
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
        "api_key=<redacted> <redacted> <redacted>"
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
async def test_runtime_update_control_routes_use_coordinator_barrier(tmp_path) -> None:
    class FakeUpdateManager:
        def __init__(self) -> None:
            self.events: list[str] = []

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
            return SimpleNamespace(release=SimpleNamespace(engine=engine))

        def activate(self, prepared):
            self.events.append(f"activate:{prepared.release.engine}")
            return SimpleNamespace(
                engine=prepared.release.engine,
                version="1.0.0",
                source_revision="abc",
                root=tmp_path / "runtime",
            )

        def rollback(self, engine: str):
            self.events.append(f"rollback:{engine}")
            return SimpleNamespace(
                engine=engine,
                version="0.9.0",
                source_revision="old",
                root=tmp_path / "runtime-old",
            )

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
        installed = await client.post(
            "/manager/runtime-updates/mflux/install",
            json={"version": "1.0.0"},
        )
        assert installed.status_code == 200
        assert installed.json()["activated"]["version"] == "1.0.0"
        rolled_back = await client.post("/manager/runtime-updates/mflux/rollback")
        assert rolled_back.status_code == 200
        assert rolled_back.json()["activated"]["rollback"] is True
        assert updates.events == [
            "check:True",
            "prepare:mflux:1.0.0",
            "activate:mflux",
            "check:False",
            "rollback:mflux",
            "check:False",
        ]
        status = await runtime.coordinator.status()
        assert status.state.value == "idle"
        assert status.resident_alias is None
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
