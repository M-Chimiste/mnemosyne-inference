from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mnemosyne_macos.config import MacConfig, OMLXConfig
from mnemosyne_macos.coordinator import CoordinatorState, ResidencyCoordinator
from mnemosyne_macos.engines.base import AdapterError, Deadline
from mnemosyne_macos.engines.omlx import OMLXAdapter
from mnemosyne_macos.install_launch import (
    OMLXInstallLaunch,
    with_omlx_target_launch,
)
from mnemosyne_macos.models import EngineName, EngineSnapshot, ServiceState
from mnemosyne_macos.runtime import NativeRuntime


def _target():
    return MacConfig.model_validate(
        {
            "engines": {"omlx": {"enabled": True}},
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
async def test_signed_omlx_launch_proof_is_exact_authenticated_get_only(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMLX_ADMIN_SESSION", "admin-session")
    requests: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (request.method, request.url.path, request.headers.get("cookie"))
        )
        return httpx.Response(
            200,
            json={
                "scheduler": {"max_concurrent_requests": 3},
                "memory": {"prefill_memory_guard": True},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    evidence = await adapter.require_launch_contract(
        OMLXInstallLaunch(
            engine="omlx",
            scheduler_slots=3,
            memory_guard="required",
        ),
        deadline=Deadline.after(1),
    )

    assert evidence.scheduler_slots == 3
    assert evidence.memory_guard_enabled is True
    assert requests == [
        (
            "GET",
            "/admin/api/global-settings",
            "omlx_admin_session=admin-session",
        )
    ]
    assert adapter.capacity_hint(_target()).limit == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_fresh_signed_launch_proof_replaces_stale_capacity_hint() -> None:
    scheduler_slots = 8

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/api/models":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/admin/api/global-settings":
            return httpx.Response(
                200,
                json={
                    "scheduler": {"max_concurrent_requests": scheduler_slots},
                    "memory": {"prefill_memory_guard": True},
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    await adapter.validate_control(deadline=Deadline.after(1))
    assert adapter.capacity_hint(_target()).limit == 8

    scheduler_slots = 4
    await adapter.require_launch_contract(
        OMLXInstallLaunch(
            engine="omlx",
            scheduler_slots=4,
            memory_guard="required",
        ),
        deadline=Deadline.after(1),
    )

    hint = adapter.capacity_hint(_target())
    assert hint is not None
    assert hint.limit == 4
    coordinator = ResidencyCoordinator({EngineName.OMLX: adapter})
    assert coordinator.capacity_for(_target()).effective_limit == 4
    await client.aclose()


@pytest.mark.asyncio
async def test_failed_fresh_signed_launch_proof_clears_stale_capacity_hint() -> None:
    valid = True

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/api/models":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/admin/api/global-settings":
            return httpx.Response(
                200,
                json=(
                    {
                        "scheduler": {"max_concurrent_requests": 8},
                        "memory": {"prefill_memory_guard": True},
                    }
                    if valid
                    else {"scheduler": {}, "memory": {}}
                ),
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    await adapter.validate_control(deadline=Deadline.after(1))
    assert adapter.capacity_hint(_target()).limit == 8

    valid = False
    with pytest.raises(AdapterError, match="omitted an exact scheduler"):
        await adapter.require_launch_contract(
            OMLXInstallLaunch(
                engine="omlx",
                scheduler_slots=8,
                memory_guard="required",
            ),
            deadline=Deadline.after(1),
        )

    assert adapter.capacity_hint(_target()) is None
    assert "omitted an exact scheduler" in (adapter.capacity_diagnostic or "")
    assert ResidencyCoordinator({EngineName.OMLX: adapter}).capacity_for(
        _target()
    ).effective_limit == 1
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {
            "scheduler": {"max_concurrent_requests": 1},
            "memory": {"prefill_memory_guard": True},
        },
        {
            "scheduler": {"max_concurrent_requests": 2},
            "memory": {"prefill_memory_guard": False},
        },
        {
            "scheduler": {"max_concurrent_requests": 2},
            "memory": {},
        },
    ],
)
async def test_signed_omlx_load_fails_before_inventory_or_mutation_on_drift(
    settings: dict[str, object],
) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/admin/api/global-settings":
            return httpx.Response(200, json=settings)
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    baseline = _target()
    target = replace(
        baseline,
        load_options=with_omlx_target_launch(
            baseline.load_options,
            OMLXInstallLaunch(
                engine="omlx",
                scheduler_slots=2,
                memory_guard="required",
            ),
        ),
    )

    with pytest.raises(AdapterError):
        await adapter.load(target, deadline=Deadline.after(1))

    assert requests == [("GET", "/admin/api/global-settings")]
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_context_contract_reads_native_and_effective_limits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models/status":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "mlx-community/GLM",
                            "max_context_window": 131_072,
                        }
                    ]
                },
            )
        if request.url.path == "/admin/api/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "mlx-community/GLM",
                            "model_context_length": 262_144,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)

    hint = await adapter.context_window(_target(), deadline=Deadline.after(1))

    assert hint.effective_tokens == 131_072
    assert hint.native_tokens == 262_144
    assert hint.confidence == "authoritative"
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_applies_the_profile_context_before_loading() -> None:
    effective = 32_768
    loaded = False
    mutations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal effective, loaded
        if request.method == "GET" and request.url.path == "/v1/models/status":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "mlx-community/GLM",
                            "max_context_window": effective,
                        }
                    ]
                },
            )
        if request.method == "GET" and request.url.path == "/admin/api/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "mlx-community/GLM",
                            "loaded": loaded,
                            "is_loading": False,
                            "model_context_length": 262_144,
                        }
                    ]
                },
            )
        if request.method == "PUT" and request.url.path.endswith("/settings"):
            mutations.append("settings")
            assert request.read() == b'{"max_context_window":131072}'
            effective = 131_072
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and request.url.path.endswith("/load"):
            mutations.append("load")
            loaded = True
            return httpx.Response(
                200,
                json={"status": "ok", "model_id": "mlx-community/GLM"},
            )
        return httpx.Response(404)

    target = MacConfig.model_validate(
        {
            "engines": {"omlx": {"enabled": True}},
            "models": [
                {
                    "alias": "glm",
                    "engine": "omlx",
                    "model": "mlx-community/GLM",
                    "context": {
                        "mode": "fixed",
                        "native_tokens": 262_144,
                        "fixed_tokens": 131_072,
                    },
                }
            ],
        }
    ).profiles()["glm"]
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)

    await adapter.load(target, deadline=Deadline.after(1))

    assert mutations == ["settings", "load"]
    assert effective == 131_072
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_delegates_context_profiling_to_its_memory_guard_benchmark() -> None:
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.method == "POST" and request.url.path.endswith("/context/start"):
            assert request.read() == (
                b'{"model_id":"mlx-community/GLM","target_tokens":262144}'
            )
            return httpx.Response(
                200,
                json={"bench_id": "context-run", "status": "started"},
            )
        if request.method == "GET" and request.url.path.endswith("/results"):
            polls += 1
            if polls == 1:
                return httpx.Response(
                    200,
                    json={"status": "running", "result": None},
                )
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "result": {
                        "applied": True,
                        "applied_tokens": 245_760,
                        "verified_prompt_tokens": 247_808,
                    },
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client, poll_interval_seconds=0)

    result = await adapter.profile_context_window(
        _target(),
        262_144,
        deadline=Deadline.after(1),
    )

    assert result is not None
    assert result.requested_tokens == 262_144
    assert result.verified_tokens == 245_760
    assert result.prompt_tokens == 247_808
    assert result.source == "omlx-native-context-benchmark"
    assert polls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_cache_health_is_sanitized_and_reset_uses_vendor_api() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/admin/api/stats":
            assert request.url.params["scope"] == "alltime"
            return httpx.Response(
                200,
                json={
                    "total_requests": 40,
                    "total_cached_tokens": 0,
                    "cache_efficiency": 0.0,
                    "api_key": "must-not-escape",
                    "runtime_cache": {
                        "ssd_cache_dir": "/Users/private/.omlx/cache",
                        "total_num_files": 72,
                        "total_size_bytes": 12 * 1024**3,
                        "disk_max_bytes": 32 * 1024**3,
                        "hot_cache_size_bytes": 512,
                        "hot_cache_max_bytes": 1024,
                    },
                },
            )
        if (
            request.method == "POST"
            and request.url.path == "/admin/api/ssd-cache/clear"
        ):
            return httpx.Response(
                200,
                json={"status": "ok", "total_deleted": 72},
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)

    health = await adapter.cache_health(deadline=Deadline.after(1))
    assert health == {
        "available": True,
        "total_requests": 40,
        "total_cached_tokens": 0,
        "cache_efficiency": 0.0,
        "ssd_file_count": 72,
        "ssd_size_bytes": 12 * 1024**3,
        "ssd_limit_bytes": 32 * 1024**3,
        "hot_size_bytes": 512,
        "hot_limit_bytes": 1024,
        "reset_recommended": True,
        "diagnostic": (
            "The persistent oMLX cache is large but has recorded no prefix-cache reuse; reset it if warm requests remain slow."
        ),
    }
    assert "api_key" not in health
    assert "ssd_cache_dir" not in health
    assert await adapter.clear_ssd_cache(deadline=Deadline.after(1)) == 72
    assert requests == [
        ("GET", "/admin/api/stats"),
        ("POST", "/admin/api/ssd-cache/clear"),
    ]
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

        async def status(self):
            return SimpleNamespace(initialized=False)

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
    adapter.inspect = AsyncMock(  # type: ignore[method-assign]
        return_value=EngineSnapshot(
            engine=EngineName.OMLX,
            residents=(),
            authoritative=True,
            service_state=ServiceState.READY,
        )
    )
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
async def test_stopped_omlx_directory_sync_stays_pending_without_global_degradation(
    tmp_path: Path,
) -> None:
    class Coordinator:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def run_empty_maintenance(self, operation, *, name: str) -> None:
            self.events.append(f"maintenance:{name}")
            await operation(Deadline.after(1))

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
                "locations": [
                    {"name": "models", "path": str(tmp_path / "models")}
                ],
            },
        }
    )
    Path(config.storage.locations[0].path).mkdir()
    adapter = OMLXAdapter(OMLXConfig())
    adapter.inspect = AsyncMock(  # type: ignore[method-assign]
        return_value=EngineSnapshot(
            engine=EngineName.OMLX,
            residents=(),
            authoritative=True,
            service_state=ServiceState.STOPPED,
        )
    )
    register = AsyncMock()
    adapter.register_model_directories = register  # type: ignore[method-assign]
    runtime = object.__new__(NativeRuntime)
    runtime.config = config
    runtime.adapters = {EngineName.OMLX: adapter}
    runtime.coordinator = Coordinator()
    runtime._omlx_directory_sync_pending = False

    await runtime._sync_omlx_model_directories()

    assert runtime._omlx_directory_sync_pending is True
    assert runtime.coordinator.events == []
    register.assert_not_awaited()
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
async def test_omlx_control_validation_observes_scheduler_capacity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/api/models":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/admin/api/global-settings":
            return httpx.Response(
                200,
                json={"scheduler": {"max_concurrent_requests": 8}},
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    snapshot = await adapter.validate_control(deadline=Deadline.after(1))

    assert snapshot.authoritative is True
    hint = adapter.capacity_hint(_target())
    assert hint is not None
    assert hint.limit == 8
    assert hint.source == "omlx-admin-settings"
    assert hint.confidence == "authoritative"

    coordinator = ResidencyCoordinator(
        {EngineName.OMLX: adapter},
        configured_max_concurrency=6,
    )
    capacity = coordinator.capacity_for(_target())
    assert capacity.derived_limit == 8
    assert capacity.effective_limit == 6
    assert capacity.source == "omlx-admin-settings"
    await client.aclose()


@pytest.mark.asyncio
async def test_omlx_missing_scheduler_capacity_remains_callable_and_conservative() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/api/models":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/admin/api/global-settings":
            return httpx.Response(200, json={"scheduler": {}})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OMLXAdapter(OMLXConfig(), client=client)
    snapshot = await adapter.validate_control(deadline=Deadline.after(1))

    assert snapshot.authoritative is True
    assert adapter.capacity_hint(_target()) is None
    assert "max_concurrent_requests" in (adapter.capacity_diagnostic or "")
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


@pytest.mark.asyncio
async def test_default_omlx_client_ignores_ambient_proxies() -> None:
    adapter = OMLXAdapter(OMLXConfig())
    try:
        assert adapter._client._trust_env is False
    finally:
        await adapter.aclose()
