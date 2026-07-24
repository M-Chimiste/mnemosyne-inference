from __future__ import annotations

import json

import httpx
import pytest

from mnemosyne_macos.config import LMStudioConfig, MacConfig
from mnemosyne_macos.engines.base import AdapterError, Deadline
from mnemosyne_macos.engines.lmstudio import LMStudioAdapter
from mnemosyne_macos.models import ServiceState


def _target():
    return MacConfig.model_validate(
        {
            "engines": {"lmstudio": {"enabled": True}},
            "models": [
                {
                    "alias": "studio-model",
                    "engine": "lmstudio",
                    "model": "org/model",
                    "load": {"context_length": 8192},
                }
            ]
        }
    ).profiles()["studio-model"]


@pytest.mark.asyncio
async def test_lmstudio_inventory_profiles_downloaded_models_without_loading() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "type": "llm",
                        "publisher": "mlx-community",
                        "key": "gemma-4-31b-it",
                        "display_name": "Gemma 4 31B Instruct",
                        "architecture": "gemma4",
                        "quantization": {"name": "4bit", "bits_per_weight": 4},
                        "size_bytes": 18_444_413_040,
                        "params_string": "31B",
                        "loaded_instances": [],
                        "max_context_length": 262_144,
                        "format": "mlx",
                        "capabilities": {
                            "vision": True,
                            "trained_for_tool_use": True,
                        },
                    },
                    {
                        "type": "embedding",
                        "key": "nomic-embed",
                        "display_name": "Nomic Embed",
                        "loaded_instances": [{"id": "embedding-instance"}],
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)

    inventory = await adapter.inventory(deadline=Deadline.after(1))

    assert requests == [("GET", "/api/v1/models")]
    assert inventory[0] == {
        "key": "gemma-4-31b-it",
        "display_name": "Gemma 4 31B Instruct",
        "type": "llm",
        "publisher": "mlx-community",
        "architecture": "gemma4",
        "quantization_name": "4bit",
        "bits_per_weight": 4,
        "size_bytes": 18_444_413_040,
        "params_string": "31B",
        "max_context_length": 262_144,
        "format": "mlx",
        "vision": True,
        "trained_for_tool_use": True,
        "loaded": False,
    }
    assert inventory[1]["type"] == "embedding"
    assert inventory[1]["loaded"] is True
    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_load_and_unload_uses_native_instance_api() -> None:
    loaded: list[dict] = []
    requests: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/api/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "key": "org/model",
                            "loaded_instances": list(loaded),
                        }
                    ]
                },
            )
        if request.url.path == "/api/v1/models/load":
            loaded.append({"id": "instance-1", "config": {}})
            return httpx.Response(
                200,
                json={
                    "type": "llm",
                    "instance_id": "instance-1",
                    "load_time_seconds": 1.5,
                    "status": "loaded",
                },
            )
        if request.url.path == "/api/v1/models/unload":
            loaded.clear()
            return httpx.Response(200, json={"instance_id": "instance-1"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)
    target = _target()

    handle = await adapter.load(target, deadline=Deadline.after(5))
    assert handle.instance.instance_id == "instance-1"
    assert requests[1] == (
        "POST",
        "/api/v1/models/load",
        {"model": "org/model", "context_length": 8192},
    )

    await adapter.unload(handle.instance, deadline=Deadline.after(5))
    unload_requests = [entry for entry in requests if entry[1].endswith("/unload")]
    assert unload_requests == [
        ("POST", "/api/v1/models/unload", {"instance_id": "instance-1"})
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_unauthorized_state_is_not_authoritative() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="no"))
    )
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)
    snapshot = await adapter.inspect(deadline=Deadline.after(5))
    assert snapshot.authoritative is False
    assert snapshot.service_state == ServiceState.UNAUTHORIZED
    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_polls_delayed_load_and_unload_convergence() -> None:
    phase = "idle"
    checks = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal phase, checks
        if request.method == "GET":
            if phase in {"loading", "unloading"}:
                checks += 1
            loaded = phase in {"loaded", "unloading"}
            if phase == "loading" and checks >= 3:
                phase = "loaded"
                loaded = True
            elif phase == "unloading" and checks >= 3:
                phase = "idle"
                loaded = False
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "key": "org/model",
                            "loaded_instances": (
                                [{"id": "instance-delayed"}] if loaded else []
                            ),
                        }
                    ]
                },
            )
        if request.url.path == "/api/v1/models/load":
            phase = "loading"
            checks = 0
            return httpx.Response(202, json={"instance_id": "instance-delayed"})
        if request.url.path == "/api/v1/models/unload":
            phase = "unloading"
            checks = 0
            return httpx.Response(202, json={"instance_id": "instance-delayed"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(
        LMStudioConfig(), client=client, poll_interval_seconds=0
    )

    handle = await adapter.load(_target(), deadline=Deadline.after(1))
    assert handle.instance.instance_id == "instance-delayed"
    assert checks == 3

    await adapter.unload(handle.instance, deadline=Deadline.after(1))
    assert phase == "idle"
    assert checks == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_exact_instance_unload_preserves_other_instance() -> None:
    loaded = [{"id": "instance-a"}, {"id": "instance-b"}]
    unload_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"key": "org/model", "loaded_instances": list(loaded)}
                    ]
                },
            )
        body = json.loads(request.content)
        unload_bodies.append(body)
        loaded[:] = [entry for entry in loaded if entry["id"] != body["instance_id"]]
        return httpx.Response(200, json={"instance_id": body["instance_id"]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)
    before = await adapter.inspect(deadline=Deadline.after(1))

    await adapter.unload(before.residents[0], deadline=Deadline.after(1))

    assert unload_bodies == [{"instance_id": "instance-a"}]
    after = await adapter.inspect(deadline=Deadline.after(1))
    assert [resident.instance_id for resident in after.residents] == ["instance-b"]
    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_loopback_refusal_is_authoritative_stopped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 61] Connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)
    snapshot = await adapter.validate_control(deadline=Deadline.after(1))

    assert snapshot.authoritative is True
    assert snapshot.empty is True
    assert snapshot.service_state == ServiceState.STOPPED
    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_timeout_is_uncertain_not_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("inspection timed out", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)
    snapshot = await adapter.inspect(deadline=Deadline.after(1))

    assert snapshot.authoritative is False
    assert snapshot.empty is False
    assert snapshot.service_state == ServiceState.UNREACHABLE
    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_protocol_failure_is_uncertain_not_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("invalid response framing", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)
    snapshot = await adapter.inspect(deadline=Deadline.after(1))

    assert snapshot.authoritative is False
    assert snapshot.service_state == ServiceState.UNREACHABLE
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, diagnostic",
    [
        ({"models": [{"key": "org/model"}]}, "loaded_instances"),
        (
            {
                "models": [
                    {
                        "key": "org/model",
                        "loaded_instances": [{"id": "same"}, {"id": "same"}],
                    }
                ]
            },
            "duplicate loaded instance",
        ),
    ],
)
async def test_lmstudio_malformed_inventory_is_never_authoritative(
    payload, diagnostic
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)
    snapshot = await adapter.inspect(deadline=Deadline.after(1))

    assert snapshot.authoritative is False
    assert snapshot.service_state == ServiceState.INCOMPATIBLE
    assert diagnostic in (snapshot.diagnostic or "")
    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_ambiguous_unload_timeout_never_implies_empty() -> None:
    loaded = [{"id": "instance-1"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"key": "org/model", "loaded_instances": list(loaded)}
                    ]
                },
            )
        raise httpx.ReadTimeout("write outcome unknown", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(
        LMStudioConfig(), client=client, poll_interval_seconds=0.001
    )
    instance = (await adapter.inspect(deadline=Deadline.after(1))).residents[0]

    with pytest.raises(AdapterError, match="did not confirm unload") as exc_info:
        await adapter.unload(instance, deadline=Deadline.after(0.03))

    assert exc_info.value.retryable is True
    assert loaded == [{"id": "instance-1"}]
    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_rejects_mismatched_load_instance_response() -> None:
    loaded: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"key": "org/model", "loaded_instances": list(loaded)}
                    ]
                },
            )
        loaded.append({"id": "actual-instance"})
        return httpx.Response(200, json={"instance_id": "different-instance"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)

    with pytest.raises(AdapterError, match="different-instance"):
        await adapter.load(_target(), deadline=Deadline.after(1))

    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_rejects_invalid_success_schema_after_load() -> None:
    loaded: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"key": "org/model", "loaded_instances": list(loaded)}
                    ]
                },
            )
        loaded.append({"id": "actual-instance"})
        return httpx.Response(200, json={"status": "error"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)

    with pytest.raises(AdapterError, match="did not contain instance_id"):
        await adapter.load(_target(), deadline=Deadline.after(1))

    await client.aclose()


@pytest.mark.asyncio
async def test_lmstudio_unload_404_is_safe_only_after_confirmed_absence() -> None:
    loaded = [{"id": "instance-1"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"key": "org/model", "loaded_instances": list(loaded)}
                    ]
                },
            )
        loaded.clear()
        return httpx.Response(404, json={"error": "already gone"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(LMStudioConfig(), client=client)
    instance = (await adapter.inspect(deadline=Deadline.after(1))).residents[0]

    await adapter.unload(instance, deadline=Deadline.after(1))
    assert loaded == []
    await client.aclose()
