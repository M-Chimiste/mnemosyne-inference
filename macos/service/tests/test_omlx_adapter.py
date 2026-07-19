from __future__ import annotations

import httpx
import pytest

from mnemosyne_macos.config import MacConfig, OMLXConfig
from mnemosyne_macos.engines.base import AdapterError, Deadline
from mnemosyne_macos.engines.omlx import OMLXAdapter
from mnemosyne_macos.models import ServiceState


def _target():
    return MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "glm",
                    "engine": "omlx",
                    "model": "mlx-community/GLM",
                }
            ]
        }
    ).profiles()["glm"]


@pytest.mark.asyncio
async def test_omlx_load_unload_and_encoded_model_id() -> None:
    loaded = False
    mutation_paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal loaded
        if request.method == "GET" and request.url.path == "/admin/api/models":
            return httpx.Response(
                200,
                json={"models": [{"id": "mlx-community/GLM", "loaded": loaded}]},
            )
        mutation_paths.append(request.url.raw_path)
        if request.method == "POST" and request.url.path.endswith("/load"):
            loaded = True
            return httpx.Response(
                200,
                json={"status": "ok", "model_id": "mlx-community/GLM"},
            )
        if request.method == "POST" and request.url.path.endswith("/unload"):
            loaded = False
            return httpx.Response(
                200,
                json={"status": "ok", "model_id": "mlx-community/GLM"},
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    target = _target()

    handle = await adapter.load(target, deadline=Deadline.after(5))
    assert handle.instance.canonical_model_id == "mlx-community/GLM"
    await adapter.unload(handle.instance, deadline=Deadline.after(5))
    assert b"mlx-community%2FGLM" in mutation_paths[0]
    assert b"mlx-community%2FGLM" in mutation_paths[1]
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_polls_loading_and_delayed_unload() -> None:
    phase = "idle"
    checks = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal phase, checks
        if request.method == "GET":
            if phase in {"loading", "unloading"}:
                checks += 1
            if phase == "loading" and checks >= 3:
                phase = "loaded"
            elif phase == "unloading" and checks >= 3:
                phase = "idle"
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "mlx-community/GLM",
                            "loaded": phase in {"loaded", "unloading"},
                            "is_loading": phase == "loading",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/load"):
            phase = "loading"
            checks = 0
            return httpx.Response(
                202,
                json={"status": "loading", "model_id": "mlx-community/GLM"},
            )
        if request.url.path.endswith("/unload"):
            phase = "unloading"
            checks = 0
            return httpx.Response(
                202,
                json={"status": "unloading", "model_id": "mlx-community/GLM"},
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client, poll_interval_seconds=0)

    handle = await adapter.load(_target(), deadline=Deadline.after(1))
    assert phase == "loaded"
    assert checks == 3

    await adapter.unload(handle.instance, deadline=Deadline.after(1))
    assert phase == "idle"
    assert checks == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_loopback_refusal_is_authoritative_stopped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    snapshot = await adapter.inspect(deadline=Deadline.after(1))

    assert snapshot.authoritative is True
    assert snapshot.empty is True
    assert snapshot.service_state == ServiceState.STOPPED
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_timeout_is_uncertain_not_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("inspection timed out", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    snapshot = await adapter.inspect(deadline=Deadline.after(1))

    assert snapshot.authoritative is False
    assert snapshot.empty is False
    assert snapshot.service_state == ServiceState.UNREACHABLE
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model, diagnostic",
    [
        ({"id": "mlx-community/GLM"}, "boolean loaded"),
        (
            {"id": "mlx-community/GLM", "loaded": "false"},
            "boolean loaded",
        ),
        (
            {
                "id": "mlx-community/GLM",
                "loaded": False,
                "is_loading": "yes",
            },
            "boolean is_loading",
        ),
    ],
)
async def test_omlx_malformed_inventory_is_never_authoritative(
    model, diagnostic
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": [model]})
        )
    )
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    snapshot = await adapter.validate_control(deadline=Deadline.after(1))

    assert snapshot.authoritative is False
    assert snapshot.service_state == ServiceState.INCOMPATIBLE
    assert diagnostic in (snapshot.diagnostic or "")
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_loading_inventory_is_transitional_not_empty() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "mlx-community/GLM",
                            "loaded": False,
                            "is_loading": True,
                        }
                    ]
                },
            )
        )
    )
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    snapshot = await adapter.inspect(deadline=Deadline.after(1))

    assert snapshot.authoritative is False
    assert snapshot.empty is False
    assert snapshot.service_state == ServiceState.READY
    assert "transitioning" in (snapshot.diagnostic or "")
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_virtual_builtin_does_not_count_as_resident() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "builtin/markitdown",
                            "loaded": True,
                            "is_loading": False,
                            "virtual": True,
                        }
                    ]
                },
            )
        )
    )
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    snapshot = await adapter.inspect(deadline=Deadline.after(1))

    assert snapshot.authoritative is True
    assert snapshot.empty is True
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_bearer_without_admin_session_has_actionable_auth_state(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMLX_API_KEY", "secret")
    monkeypatch.delenv("OMLX_ADMIN_SESSION", raising=False)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="no"))
    )
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    snapshot = await adapter.validate_control(deadline=Deadline.after(1))

    assert snapshot.authoritative is False
    assert snapshot.service_state == ServiceState.UNAUTHORIZED
    assert "bearer API key alone" in (snapshot.diagnostic or "")
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_unload_rejected_without_valid_admin_session() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"id": "mlx-community/GLM", "loaded": True}
                    ]
                },
            )
        return httpx.Response(401, text="admin required")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    instance = (await adapter.inspect(deadline=Deadline.after(1))).residents[0]

    with pytest.raises(AdapterError, match="admin session is required"):
        await adapter.unload(instance, deadline=Deadline.after(1))

    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_ambiguous_unload_timeout_never_implies_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"id": "mlx-community/GLM", "loaded": True}
                    ]
                },
            )
        raise httpx.ReadTimeout("write outcome unknown", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(
        OMLXConfig(), client=client, poll_interval_seconds=0.001
    )
    instance = (await adapter.inspect(deadline=Deadline.after(1))).residents[0]

    with pytest.raises(AdapterError, match="did not confirm unload") as exc_info:
        await adapter.unload(instance, deadline=Deadline.after(0.03))

    assert exc_info.value.retryable is True
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_rejects_wrong_model_in_mutation_response() -> None:
    loaded = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal loaded
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"id": "mlx-community/GLM", "loaded": loaded}
                    ]
                },
            )
        loaded = True
        return httpx.Response(
            200,
            json={"status": "ok", "model_id": "some-other-model"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)

    with pytest.raises(AdapterError, match="different model"):
        await adapter.load(_target(), deadline=Deadline.after(1))

    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_rejects_incomplete_success_schema() -> None:
    loaded = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal loaded
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"id": "mlx-community/GLM", "loaded": loaded}
                    ]
                },
            )
        loaded = True
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)

    with pytest.raises(AdapterError, match="did not contain model_id"):
        await adapter.load(_target(), deadline=Deadline.after(1))

    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_unload_404_is_safe_only_after_confirmed_absence() -> None:
    loaded = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal loaded
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"id": "mlx-community/GLM", "loaded": loaded}
                    ]
                },
            )
        loaded = False
        return httpx.Response(404, json={"detail": "already gone"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    instance = (await adapter.inspect(deadline=Deadline.after(1))).residents[0]

    await adapter.unload(instance, deadline=Deadline.after(1))
    assert loaded is False
    await client.aclose()
