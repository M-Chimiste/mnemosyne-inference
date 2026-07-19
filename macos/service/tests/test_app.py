from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from mnemosyne_macos.app import create_control_app, create_inference_app
from mnemosyne_macos.config import MacConfig
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


class FakeAdapter(EngineAdapter):
    ownership = "fake"

    def __init__(self, engine: EngineName) -> None:
        self.engine = engine
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


def _config(tmp_path, *, endpoint: Endpoint | None = None) -> MacConfig:
    model: dict = {
        "alias": "frontier",
        "engine": "lmstudio",
        "model": "publisher/upstream-model",
    }
    if endpoint is not None:
        model["capabilities"] = [endpoint.value]
    return MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
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
        assert rows[0]["backend"] == "lmstudio"
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
            "server": ("127.0.0.1", 17320),
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
async def test_control_plane_validates_unsaved_configuration(tmp_path) -> None:
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
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        valid = await client.post(
            "/manager/config/validate",
            json={"config_yaml": "models: []\n"},
        )
        assert valid.status_code == 200
        assert valid.json() == {"valid": True, "model_count": 0}

        invalid = await client.post(
            "/manager/config/validate",
            json={"config_yaml": "models: [\n"},
        )
        assert invalid.status_code == 400
        assert "configuration editor" in invalid.json()["detail"]
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
