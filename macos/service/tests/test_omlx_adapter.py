from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from mnemosyne_macos.config import MacConfig, OMLXConfig
from mnemosyne_macos.coordinator import CoordinatorState, ResidencyCoordinator
from mnemosyne_macos.engines.base import AdapterError, Deadline
from mnemosyne_macos.engines.omlx import OMLXAdapter
from mnemosyne_macos.models import EngineName, ServiceState
from mnemosyne_macos.runtime import NativeRuntime


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
async def test_omlx_registers_nested_library_root_and_rescans() -> None:
    requests: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        requests.append((request.method, request.url.path, body))
        if (
            request.method == "GET"
            and request.url.path == "/admin/api/global-settings"
        ):
            return httpx.Response(
                200,
                json={
                    "model": {
                        "model_dirs": ["/Users/c/.omlx/models"],
                        "effective_model_dirs": ["/Users/c/.omlx/models"],
                    }
                },
            )
        if request.method == "GET" and request.url.path == "/admin/api/models":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    await adapter.register_model_directories(
        ["/Volumes/Athena/models/omlx"], deadline=Deadline.after(1)
    )

    assert requests[1][1] == "/admin/api/global-settings"
    assert b'"/Volumes/Athena/models/omlx"' in requests[1][2]
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_rescan_unloads_preloaded_pinned_models_before_returning() -> None:
    directory = "/Volumes/Athena/models/omlx"
    loaded: set[str] = set()
    mutation_paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "GET"
            and request.url.path == "/admin/api/global-settings"
        ):
            return httpx.Response(
                200,
                json={
                    "model": {
                        "model_dirs": [directory],
                        "effective_model_dirs": [directory],
                    }
                },
            )
        if request.method == "POST" and request.url.path == "/admin/api/reload":
            # Official oMLX reload semantics synchronously preload pinned
            # models after rediscovery.
            loaded.update({"owner/pinned-a", "owner/pinned-b"})
            mutation_paths.append(request.url.raw_path)
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and request.url.path == "/admin/api/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": model_id,
                            "loaded": model_id in loaded,
                            "is_loading": False,
                        }
                        for model_id in ("owner/pinned-a", "owner/pinned-b")
                    ]
                },
            )
        if request.method == "POST" and request.url.path.endswith("/unload"):
            mutation_paths.append(request.url.raw_path)
            raw_path = request.url.raw_path
            model_id = (
                "owner/pinned-a"
                if b"owner%2Fpinned-a" in raw_path
                else "owner/pinned-b"
            )
            loaded.discard(model_id)
            return httpx.Response(
                200,
                json={"status": "ok", "model_id": model_id},
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    coordinator = ResidencyCoordinator(
        {EngineName.OMLX: adapter},
        queue_timeout_seconds=1,
        transition_timeout_seconds=1,
        cleanup_timeout_seconds=1,
    )
    await coordinator.initialize()

    async def rescan(deadline: Deadline) -> None:
        await adapter.register_model_directories([directory], deadline=deadline)

    await coordinator.run_empty_maintenance(
        rescan,
        name="oMLX model-library rescan",
    )

    assert mutation_paths[0] == b"/admin/api/reload"
    assert set(mutation_paths[1:]) == {
        b"/admin/api/models/owner%2Fpinned-a/unload",
        b"/admin/api/models/owner%2Fpinned-b/unload",
    }
    assert loaded == set()
    assert (await adapter.inspect(deadline=Deadline.after(1))).empty is True
    assert (await coordinator.status()).state == CoordinatorState.IDLE
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_rescan_fails_closed_when_pinned_model_cannot_be_unloaded() -> None:
    directory = "/Volumes/Athena/models/omlx"
    loaded = False
    unload_attempted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal loaded, unload_attempted
        if (
            request.method == "GET"
            and request.url.path == "/admin/api/global-settings"
        ):
            return httpx.Response(
                200,
                json={
                    "model": {
                        "model_dirs": [directory],
                        "effective_model_dirs": [directory],
                    }
                },
            )
        if request.method == "POST" and request.url.path == "/admin/api/reload":
            loaded = True
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and request.url.path == "/admin/api/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "owner/pinned",
                            "loaded": loaded,
                            "is_loading": False,
                        }
                    ]
                },
            )
        if request.method == "POST" and request.url.path.endswith("/unload"):
            unload_attempted = True
            return httpx.Response(401, json={"detail": "admin required"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    coordinator = ResidencyCoordinator(
        {EngineName.OMLX: adapter},
        queue_timeout_seconds=1,
        transition_timeout_seconds=1,
        cleanup_timeout_seconds=1,
    )
    await coordinator.initialize()

    async def rescan(deadline: Deadline) -> None:
        await adapter.register_model_directories([directory], deadline=deadline)

    with pytest.raises(
        AdapterError,
        match="could not authoritatively restore empty residency",
    ):
        await coordinator.run_empty_maintenance(
            rescan,
            name="oMLX model-library rescan",
        )

    assert unload_attempted is True
    assert loaded is True
    status = await coordinator.status()
    assert status.state == CoordinatorState.DEGRADED
    assert "could not authoritatively restore empty residency" in (
        status.diagnostic or ""
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_failed_model_directory_sync_stays_pending_and_retries_after_reconcile(
    tmp_path: Path,
) -> None:
    class Coordinator:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def run_empty_maintenance(self, operation, *, name: str) -> None:
            self.events.append(f"maintenance:{name}")
            await operation(Deadline.after(1))

        async def reconcile(self) -> bool:
            self.events.append("reconcile")
            return True

        async def audit(self) -> bool:
            self.events.append("audit")
            return True

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
                "locations": [{"name": "models", "path": str(tmp_path / "models")}],
            },
        }
    )
    Path(config.storage.locations[0].path).mkdir()
    adapter = OMLXAdapter(OMLXConfig())
    register = AsyncMock(side_effect=[RuntimeError("oMLX is starting"), None])
    adapter.register_model_directories = register  # type: ignore[method-assign]
    runtime = object.__new__(NativeRuntime)
    runtime.config = config
    runtime.adapters = {EngineName.OMLX: adapter}
    runtime.coordinator = Coordinator()
    runtime._omlx_directory_sync_pending = False
    runtime.startup_error = "oMLX is starting"

    with pytest.raises(RuntimeError, match="starting"):
        await runtime._sync_omlx_model_directories()

    assert runtime._omlx_directory_sync_pending is True
    await runtime._reconcile_maintenance()

    assert runtime.coordinator.events == [
        "maintenance:oMLX model-library rescan",
        "reconcile",
        "maintenance:oMLX model-library rescan",
    ]
    assert register.await_count == 2
    assert runtime._omlx_directory_sync_pending is False
    assert runtime.startup_error is None
    await adapter.aclose()


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
