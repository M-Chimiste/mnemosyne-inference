"""Hermetic Phase-1 acceptance for the real Mac-to-Fleet request path.

This harness composes two real ``NativeRuntime`` instances, their real
``ResidencyCoordinator`` objects and per-node SQLite usage/outbox stores, the
real native ASGI inference apps, and the real Fleet registry, scheduler,
route store, proxy, and ASGI app.  Only the external engine boundary is
deterministic: a small ``EngineAdapter`` and its HTTP response stand in for an
oMLX process so the test does not require weights or Metal hardware.

This is deliberately not evidence for pairing, signed catalogs, inventory or
placement, selected-storage downloads, protected-folder grants, restart
recovery, Postgres delivery, real model compatibility, or signed multi-Mac
artifacts.  Those remain separate release gates.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FLEET_SOURCE = REPOSITORY_ROOT / "fleet" / "src"
MAC_SERVICE_SOURCE = REPOSITORY_ROOT / "macos" / "service" / "src"
if str(FLEET_SOURCE) not in sys.path:
    sys.path.insert(0, str(FLEET_SOURCE))
if str(MAC_SERVICE_SOURCE) not in sys.path:
    sys.path.insert(0, str(MAC_SERVICE_SOURCE))

from mnemosyne_fleet.app import create_app as create_fleet_app  # noqa: E402
from mnemosyne_fleet.config import (  # noqa: E402
    FleetConfig,
    LedgerConfig,
    ModelConfig,
    NodeConfig,
    ServerConfig,
)
from mnemosyne_macos.app import create_inference_app  # noqa: E402
from mnemosyne_macos.config import MacConfig  # noqa: E402
from mnemosyne_macos.engines.base import (  # noqa: E402
    CapacityHint,
    Deadline,
    EngineAdapter,
)
from mnemosyne_macos.install_store import InstallStore  # noqa: E402
from mnemosyne_macos.models import (  # noqa: E402
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
    ServiceState,
)
from mnemosyne_macos.runtime import NativeRuntime  # noqa: E402
from mnemosyne_macos.storage import install_destination  # noqa: E402
from mnemosyne_macos.usage_store import (  # noqa: E402
    UsageEventDuplicate,
    UsageStore,
)


LOCAL_ALIAS = "phase-one-model"
PUBLIC_MODEL = "phase-one-public"
REPOSITORY_ID = "mnemosyne/phase-one-model"
RESOLVED_REVISION = "a" * 40
PROMPT_CANARY = "phase-one-prompt-must-not-persist"


class _DeterministicEngineAdapter(EngineAdapter):
    """One-slot fake at the only boundary not owned by Mnemosyne."""

    engine = EngineName.OMLX
    ownership = "phase-one-test-engine"

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        self.residents: list[ResidentInstance] = []
        self.loads = 0
        self.unloads = 0

    def capacity_hint(self, target: ResolvedTarget) -> CapacityHint:
        del target
        return CapacityHint(
            limit=1,
            source="phase-one-engine-slot",
            confidence="authoritative",
        )

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
            instance_id=f"{self.node_id}-resident-{self.loads}",
        )
        self.residents = [resident]
        return LoadedHandle(
            target=target,
            instance=resident,
            base_url=f"http://engine-{self.node_id}",
            wire_model=target.wire_model,
        )

    async def unload(
        self,
        instance: ResidentInstance,
        *,
        deadline: Deadline,
    ) -> None:
        del deadline
        self.unloads += 1
        self.residents = [row for row in self.residents if row != instance]

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        return ProxyRoute(
            base_url=handle.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model=handle.wire_model,
        )

    async def aclose(self) -> None:
        return None


class _HeldDeterministicUpstream:
    """Hold engine responses so Fleet must reserve both one-slot Macs."""

    def __init__(self, node_id: str, release: asyncio.Event) -> None:
        self.node_id = node_id
        self.release = release
        self.entered = asyncio.Event()
        self.requests: list[dict[str, object]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        self.requests.append(payload)
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), timeout=5)
        return httpx.Response(
            200,
            json={
                "id": f"response-{self.node_id}",
                "object": "response",
                "output": [],
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )


class _HostASGITransport(httpx.AsyncBaseTransport):
    """Route fixed test hostnames into real node ASGI applications."""

    def __init__(self, applications: dict[str, object]) -> None:
        self._transports = {
            host: httpx.ASGITransport(app=application)
            for host, application in applications.items()
        }

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        transport = self._transports.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(
                "Phase-1 transport received an unknown host",
                request=request,
            )
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        await asyncio.gather(
            *(transport.aclose() for transport in self._transports.values())
        )


class _NativeNodeHarness:
    def __init__(
        self,
        *,
        node_id: str,
        runtime: NativeRuntime,
        adapter: _DeterministicEngineAdapter,
        upstream: _HeldDeterministicUpstream,
        upstream_client: httpx.AsyncClient,
        snapshot_key: str,
        dispatch_key: str,
    ) -> None:
        self.node_id = node_id
        self.runtime = runtime
        self.adapter = adapter
        self.upstream = upstream
        self.upstream_client = upstream_client
        self.snapshot_key = snapshot_key
        self.dispatch_key = dispatch_key
        self.app = create_inference_app(runtime)


def _build_native_node(
    root: Path,
    *,
    node_id: str,
    environment_prefix: str,
    release: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> _NativeNodeHarness:
    storage_root = root / "selected-model-storage"
    storage_root.mkdir(parents=True)
    database_path = root / "state" / "mnemosyne.sqlite3"
    snapshot_env = f"{environment_prefix}_SNAPSHOT_KEY"
    dispatch_env = f"{environment_prefix}_DISPATCH_KEY"
    local_env = f"{environment_prefix}_LOCAL_KEY"
    snapshot_key = f"snapshot-{node_id}"
    dispatch_key = f"dispatch-{node_id}"
    monkeypatch.setenv(snapshot_env, snapshot_key)
    monkeypatch.setenv(dispatch_env, dispatch_key)
    monkeypatch.setenv(local_env, f"local-{node_id}")

    destination = install_destination(
        storage_root,
        EngineName.OMLX,
        REPOSITORY_ID,
    )
    destination.mkdir(parents=True)
    config = MacConfig.model_validate(
        {
            "server": {
                "idle_unload_seconds": None,
                "max_concurrency": 1,
                # One unaccounted Fleet reservation must exhaust this cold
                # node so the next request reaches the overflow Mac.
                "max_queue_depth": 1,
                "inference_api_key_env": local_env,
                "fleet_api_key_env": snapshot_env,
                "fleet_inference_api_key_env": dispatch_env,
            },
            "engines": {
                "llama_cpp": {"enabled": False},
                "omlx": {"enabled": True},
            },
            "paths": {"state_database": str(database_path)},
            "storage": {
                "default": "selected",
                "locations": [
                    {"name": "selected", "path": str(storage_root)}
                ],
            },
            "models": [
                {
                    "alias": LOCAL_ALIAS,
                    "engine": "omlx",
                    "model": destination.name,
                    "storage": "selected",
                    "capabilities": ["responses"],
                }
            ],
            "token_sidecar": {
                "enabled": True,
                "node_id": node_id,
                "flush_interval_seconds": 30,
                "max_outbox_rows": 32,
            },
        }
    )

    install_store = InstallStore(database_path)
    install = install_store.create(
        repo_id=REPOSITORY_ID,
        engine=EngineName.OMLX.value,
        storage="selected",
        alias=LOCAL_ALIAS,
        destination=str(destination),
        revision=RESOLVED_REVISION,
        filename=None,
        download_files=("model.safetensors",),
        capabilities=(Endpoint.RESPONSES.value,),
        family=None,
    )
    install_store.update(install.id, status="installed")
    install_store.close()

    adapter = _DeterministicEngineAdapter(node_id)
    upstream = _HeldDeterministicUpstream(node_id, release)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        trust_env=False,
        follow_redirects=False,
    )
    runtime = NativeRuntime(
        config,
        adapters={EngineName.OMLX: adapter},
        proxy_client=upstream_client,
    )
    return _NativeNodeHarness(
        node_id=node_id,
        runtime=runtime,
        adapter=adapter,
        upstream=upstream,
        upstream_client=upstream_client,
        snapshot_key=snapshot_key,
        dispatch_key=dispatch_key,
    )


async def _snapshot(node: _NativeNodeHarness) -> dict[str, object]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=node.app),
        base_url=f"http://{node.node_id}",
    ) as client:
        response = await client.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": f"Bearer {node.snapshot_key}"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["node"]["platform"] == "macos"
    return payload


@pytest.mark.asyncio
async def test_phase1_two_cold_macs_jit_fan_out_and_account_by_serving_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route overlapping work primary-first, then to one limited overflow Mac."""

    monkeypatch.setenv(
        "MNEMOSYNE_TOKEN_SIDECAR_LAUNCH_AGENT",
        str(tmp_path / "no-legacy-token-sidecar.plist"),
    )
    monkeypatch.delenv("TOKEN_SIDECAR_POSTGRES_DSN", raising=False)
    release = asyncio.Event()
    primary = _build_native_node(
        tmp_path / "primary",
        node_id="mac-primary",
        environment_prefix="PHASE1_PRIMARY",
        release=release,
        monkeypatch=monkeypatch,
    )
    overflow = _build_native_node(
        tmp_path / "overflow",
        node_id="mac-overflow",
        environment_prefix="PHASE1_OVERFLOW",
        release=release,
        monkeypatch=monkeypatch,
    )
    nodes = (primary, overflow)
    registry_client: httpx.AsyncClient | None = None
    proxy_client: httpx.AsyncClient | None = None
    request_tasks: list[asyncio.Task[httpx.Response]] = []
    try:
        await asyncio.gather(
            *(node.runtime.start(raise_on_degraded=True) for node in nodes)
        )
        assert primary.runtime is not overflow.runtime
        assert isinstance(primary.runtime.usage.store, UsageStore)
        assert isinstance(overflow.runtime.usage.store, UsageStore)

        cold_snapshots = await asyncio.gather(*(_snapshot(node) for node in nodes))
        deployments = [snapshot["deployments"][0] for snapshot in cold_snapshots]
        deployment_id = deployments[0]["deployment_id"]
        assert {snapshot["node"]["platform"] for snapshot in cold_snapshots} == {
            "macos"
        }
        assert all(
            snapshot["residency"]["deployment_id"] is None
            and deployment["deployment_id"] == deployment_id
            and deployment["identity_confidence"] == "authoritative"
            and deployment["fleet_eligible"] is True
            and deployment["loadable"] is True
            and deployment["warm"] is False
            and deployment["capacity"]["effective_limit"] == 1
            for snapshot, deployment in zip(
                cold_snapshots,
                deployments,
                strict=True,
            )
        )
        assert all(node.adapter.loads == 0 for node in nodes)

        native_apps = {node.node_id: node.app for node in nodes}
        registry_client = httpx.AsyncClient(
            transport=_HostASGITransport(native_apps),
            trust_env=False,
            follow_redirects=False,
        )
        proxy_client = httpx.AsyncClient(
            transport=_HostASGITransport(native_apps),
            trust_env=False,
            follow_redirects=False,
        )
        enrollments = (
            NodeConfig(
                node_id=primary.node_id,
                url=f"http://{primary.node_id}",
                fleet_token=primary.snapshot_key,
                inference_token=primary.dispatch_key,
                service_class="primary",
            ),
            NodeConfig(
                node_id=overflow.node_id,
                url=f"http://{overflow.node_id}",
                fleet_token=overflow.snapshot_key,
                inference_token=overflow.dispatch_key,
                service_class="overflow",
            ),
        )
        fleet_config = FleetConfig(
            server=ServerConfig(
                host="127.0.0.1",
                port=17400,
                api_key="phase-one-client-key",
                admin_api_key="phase-one-admin-key",
                database_path=tmp_path / "fleet" / "fleet.sqlite3",
                request_timeout_seconds=10,
                max_body_bytes=1024 * 1024,
                route_history_limit=100,
                poll_interval_seconds=0.05,
                snapshot_ttl_seconds=2,
            ),
            nodes=enrollments,
            models=(
                ModelConfig(
                    name=PUBLIC_MODEL,
                    deployment_id=str(deployment_id),
                    capabilities=frozenset(
                        deployments[0]["identity"]["capabilities"]
                    ),
                    queue_depth=2,
                    queue_timeout_seconds=2,
                ),
            ),
            ledger=LedgerConfig(),
        )
        gateway = create_fleet_app(
            fleet_config,
            registry_client=registry_client,
            proxy_client=proxy_client,
            start_polling=False,
        )

        async with gateway.router.lifespan_context(gateway):
            await gateway.state.registry.poll_all_once()
            matrix = gateway.state.scheduler.model_matrix()
            eligibility = {
                str(row["service_class"]): bool(row["eligible"])
                for row in matrix[0]["nodes"]
            }
            assert eligibility == {"primary": True, "overflow": True}

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway),
                base_url="http://nyx",
            ) as fleet:
                request = {
                    "model": PUBLIC_MODEL,
                    "input": PROMPT_CANARY,
                }
                request_tasks.append(
                    asyncio.create_task(
                        fleet.post(
                            "/v1/responses",
                            headers={
                                "Authorization": "Bearer phase-one-client-key"
                            },
                            json=request,
                        )
                    )
                )
                await asyncio.wait_for(primary.upstream.entered.wait(), timeout=3)
                assert not overflow.upstream.entered.is_set()

                request_tasks.append(
                    asyncio.create_task(
                        fleet.post(
                            "/v1/responses",
                            headers={
                                "Authorization": "Bearer phase-one-client-key"
                            },
                            json=request,
                        )
                    )
                )
                await asyncio.wait_for(overflow.upstream.entered.wait(), timeout=3)

                assert gateway.state.scheduler.status()["active_total"] == 2
                assert all(node.adapter.loads == 1 for node in nodes)
                coordinator_statuses = await asyncio.gather(
                    *(node.runtime.coordinator.status() for node in nodes)
                )
                assert all(
                    status.resident_alias == LOCAL_ALIAS
                    for status in coordinator_statuses
                )
                release.set()
                responses = await asyncio.gather(*request_tasks)
                request_tasks.clear()

            assert all(response.status_code == 200 for response in responses)
            assert {response.json()["id"] for response in responses} == {
                "response-mac-primary",
                "response-mac-overflow",
            }
            assert all(node.adapter.loads == 1 for node in nodes)
            assert all(len(node.upstream.requests) == 1 for node in nodes)
            assert all(
                node.upstream.requests[0]["model"]
                == REPOSITORY_ID.split("/", 1)[1]
                for node in nodes
            )
            assert gateway.state.scheduler.status()["active_total"] == 0

            routes = await gateway.state.store.recent_routes(limit=10)
            assert len(routes) == 2
            assert {route["node_id"] for route in routes} == {
                primary.node_id,
                overflow.node_id,
            }
            assert all(
                route["status_code"] == 200
                and route["failure_code"] is None
                for route in routes
            )
            route_by_node = {str(route["node_id"]): route for route in routes}

            for node in nodes:
                usage_rows = await node.runtime.usage.list_usage(limit=10)
                outbox_rows = [
                    dict(row)
                    for row in await asyncio.to_thread(
                        node.runtime.usage.store.peek_outbox,
                        limit=10,
                    )
                ]
                route_id = str(route_by_node[node.node_id]["route_id"])
                assert len(usage_rows) == 1
                assert len(outbox_rows) == 1
                assert usage_rows[0]["event_id"] == route_id
                assert outbox_rows[0]["event_id"] == route_id
                assert usage_rows[0]["alias"] == LOCAL_ALIAS
                assert usage_rows[0]["backend"] == EngineName.OMLX.value
                assert usage_rows[0]["endpoint"] == "/v1/responses"
                assert usage_rows[0]["prompt_tokens"] == 3
                assert usage_rows[0]["completion_tokens"] == 2
                assert usage_rows[0]["total_tokens"] == 5
                # A successfully accounted route is fenced by request_usage;
                # the transient reservation is deliberately removed instead
                # of retaining a duplicate tombstone.  A replay must therefore
                # fail closed as durably completed without mutating either
                # per-node ledger.
                assert node.runtime.usage.store.reservation_state(route_id) is None
                assert node.runtime.usage.store.count_active_reservations() == 0
                with pytest.raises(UsageEventDuplicate) as replay:
                    node.runtime.usage.store.reserve_event(
                        route_id,
                        fleet_route=True,
                        reserve_outbox=True,
                        max_outbox_rows=100,
                    )
                assert replay.value.state == "completed"
                assert node.runtime.usage.store.count_request_usage() == 1
                assert node.runtime.usage.store.count_outbox() == 1
                persisted = json.dumps(
                    {
                        "usage": usage_rows,
                        "outbox": outbox_rows,
                        "route": route_by_node[node.node_id],
                    },
                    sort_keys=True,
                )
                assert PROMPT_CANARY not in persisted
    finally:
        release.set()
        for task in request_tasks:
            task.cancel()
        if request_tasks:
            await asyncio.gather(*request_tasks, return_exceptions=True)
        if registry_client is not None:
            await registry_client.aclose()
        if proxy_client is not None:
            await proxy_client.aclose()
        await asyncio.gather(
            *(node.runtime.stop() for node in nodes),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(node.upstream_client.aclose() for node in nodes),
            return_exceptions=True,
        )
