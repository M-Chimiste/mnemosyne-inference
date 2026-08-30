from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import pytest

from mnemosyne_macos.catalog_runtime import (
    NONE_CATALOG_DIGEST,
    NativeCatalogRuntime,
)
from mnemosyne_macos.compatibility_catalog import CatalogStoreError, canonical_json
from mnemosyne_macos.config import (
    CompatibilityCatalogConfig,
    MacConfig,
    save_config,
)
from mnemosyne_macos.app import create_control_app, create_inference_app
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


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_V1 = REPOSITORY_ROOT / "compatibility_catalog" / "v1"
TEST_NOW = 1_790_000_000
TEST_SEED = bytes(range(1, 33))
ORIGIN = "https://catalog.mnemosyne.test"
UPDATE_PATH = "/v1/apple-silicon/catalog.json"
KEY_ENV = "MNEMOSYNE_CATALOG_PUBLIC_KEY_TEST"


class _InferenceAdapter(EngineAdapter):
    engine = EngineName.OMLX
    ownership = "test"

    def __init__(self) -> None:
        self.residents: list[ResidentInstance] = []
        self.loads = 0

    async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
        del deadline
        return EngineSnapshot(
            engine=self.engine,
            residents=tuple(self.residents),
            authoritative=True,
            service_state=ServiceState.READY,
        )

    async def validate_control(self, *, deadline: Deadline) -> EngineSnapshot:
        return await self.inspect(deadline=deadline)

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
            instance_id=f"catalog-isolation-{self.loads}",
        )
        self.residents = [resident]
        return LoadedHandle(
            target=target,
            instance=resident,
            base_url="http://omlx.test",
            wire_model=target.wire_model,
        )

    async def unload(
        self,
        instance: ResidentInstance,
        *,
        deadline: Deadline,
    ) -> None:
        del deadline
        self.residents = [row for row in self.residents if row != instance]

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        return ProxyRoute(
            base_url=handle.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model=handle.wire_model,
        )

    async def aclose(self) -> None:
        return None


def _golden_bytes() -> bytes:
    return canonical_json(
        json.loads(
            (PROTOCOL_V1 / "catalog.golden.json").read_text(encoding="utf-8")
        )
    )


def _public_key() -> str:
    raw = (
        Ed25519PrivateKey.from_private_bytes(TEST_SEED)
        .public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _enabled_config() -> CompatibilityCatalogConfig:
    return CompatibilityCatalogConfig.model_validate(
        {
            "enabled": True,
            "update_origin": ORIGIN,
            "update_path": UPDATE_PATH,
            "update_interval_seconds": 300,
            "total_timeout_seconds": 1,
            "connect_timeout_seconds": 0.5,
            "max_attempts": 1,
            "trusted_keys": [
                {
                    "key_id": "test-catalog-2026-a",
                    "public_key_env": KEY_ENV,
                }
            ],
        }
    )


def _json_response(
    status: int,
    body: bytes = b"",
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    values = dict(headers or {})
    if body:
        values.setdefault("Content-Type", "application/json")
    return httpx.Response(
        status,
        stream=httpx.ByteStream(body),
        headers=values,
    )


@pytest.mark.asyncio
async def test_disabled_catalog_is_offline_empty_and_performs_no_network_or_state_io(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("disabled catalog attempted network access")

    config_path = tmp_path / "configuration" / "config.yaml"
    runtime = NativeCatalogRuntime(
        CompatibilityCatalogConfig(),
        config_path=config_path,
        transport=httpx.MockTransport(handler),
        clock=lambda: TEST_NOW,
        update_interval_seconds=0.01,
    )
    await runtime.start()
    try:
        await asyncio.sleep(0.03)
        status = await runtime.status()
        assert status["state"] == "disabled"
        assert status["active"]["catalog_version"] == "none"
        assert status["active"]["catalog_digest"] == NONE_CATALOG_DIGEST
        assert (await runtime.metadata())["models"] == []
        assert (await runtime.check_now())["outcome"] == "disabled"
        assert calls == 0
        assert not (
            config_path.parent / "state" / "compatibility-catalog"
        ).exists()
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_manual_activation_restarts_from_config_adjacent_lkg_and_is_failure_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "configuration" / "config.yaml"
    unrelated_database = tmp_path / "elsewhere" / "custom.db"
    unrelated_models = tmp_path / "external-models"
    unrelated_database.parent.mkdir()
    unrelated_models.mkdir()

    first = NativeCatalogRuntime(
        _enabled_config(),
        config_path=config_path,
        environment={KEY_ENV: _public_key()},
        transport=httpx.MockTransport(
            lambda _request: _json_response(200, _golden_bytes())
        ),
        clock=lambda: TEST_NOW,
    )
    activated = await first.check_now()
    assert activated["outcome"] == "updated"
    assert activated["changed"] is True
    first_snapshot = await first.snapshot()
    assert first_snapshot.catalog_version == "test-2026.08.1"
    assert (
        config_path.parent / "state" / "compatibility-catalog" / "active.catalog.json"
    ).is_file()
    assert not (unrelated_database.parent / "compatibility-catalog").exists()
    assert not (unrelated_models / "compatibility-catalog").exists()

    # A transient store lock/read failure must retain the already verified
    # immutable in-memory LKG rather than falling back to empty metadata.
    assert first._store is not None
    original_load = first._store.load

    def contended_load(*, now=None):
        del now
        raise CatalogStoreError("catalog_store_unavailable")

    monkeypatch.setattr(first._store, "load", contended_load)
    failed = await first.check_now()
    assert failed["outcome"] == "failed"
    assert failed["error_code"] == "catalog_update_store_error"
    assert (await first.snapshot()).catalog_digest == first_snapshot.catalog_digest
    monkeypatch.setattr(first._store, "load", original_load)
    await first.stop()

    calls = 0

    def unavailable(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(503)

    restarted = NativeCatalogRuntime(
        _enabled_config(),
        config_path=config_path,
        environment={KEY_ENV: _public_key()},
        transport=httpx.MockTransport(unavailable),
        clock=lambda: TEST_NOW,
        update_interval_seconds=300,
    )
    await restarted.start()
    try:
        # LKG loading precedes the asynchronous network check.
        restored = await restarted.snapshot()
        assert restored.catalog_digest == first_snapshot.catalog_digest
        failed_update = await restarted.check_now()
        assert failed_update["outcome"] == "failed"
        assert (await restarted.snapshot()).catalog_digest == first_snapshot.catalog_digest
        assert calls >= 1
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_missing_local_trust_fails_closed_without_network_or_startup_exception(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(200, _golden_bytes())

    runtime = NativeCatalogRuntime(
        _enabled_config(),
        config_path=tmp_path / "config.yaml",
        environment={},
        transport=httpx.MockTransport(handler),
        clock=lambda: TEST_NOW,
    )
    await runtime.start()
    try:
        status = await runtime.status()
        assert status["state"] == "unavailable"
        assert status["last_error_code"] == "catalog_trust_unavailable"
        assert (await runtime.check_now())["error_code"] == "catalog_trust_unavailable"
        assert calls == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_background_and_manual_checks_share_lifecycle_and_trigger_only_activation(
    tmp_path: Path,
) -> None:
    calls = 0
    activated: list[str] = []
    first_request = asyncio.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        first_request.set()
        if calls == 1:
            return _json_response(
                200,
                _golden_bytes(),
                headers={"ETag": '"catalog-42"'},
            )
        return _json_response(304, headers={"ETag": '"catalog-42"'})

    async def on_activation(snapshot) -> None:
        activated.append(snapshot.catalog_digest)

    runtime = NativeCatalogRuntime(
        _enabled_config(),
        config_path=tmp_path / "config.yaml",
        environment={KEY_ENV: _public_key()},
        transport=httpx.MockTransport(handler),
        clock=lambda: TEST_NOW,
        update_interval_seconds=0.02,
        on_activation=on_activation,
    )
    await runtime.start()
    try:
        await asyncio.wait_for(first_request.wait(), 1)
        for _ in range(100):
            if activated:
                break
            await asyncio.sleep(0.005)
        assert len(activated) == 1
        manual = await runtime.check_now()
        assert manual["outcome"] == "not_modified"
        assert manual["changed"] is False
        assert len(activated) == 1
    finally:
        await runtime.stop()
    calls_after_close = calls
    await asyncio.sleep(0.04)
    assert calls == calls_after_close
    status = await runtime.status()
    assert status["running"] is False
    assert status["state"] == "closed"


@pytest.mark.asyncio
async def test_shutdown_waits_for_bounded_inflight_update_and_closes_cleanly(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return _json_response(503)

    runtime = NativeCatalogRuntime(
        _enabled_config(),
        config_path=tmp_path / "config.yaml",
        environment={KEY_ENV: _public_key()},
        transport=httpx.MockTransport(handler),
        clock=lambda: TEST_NOW,
        update_interval_seconds=300,
    )
    await runtime.start()
    await asyncio.wait_for(entered.wait(), 1)
    stop_task = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)
    assert not stop_task.done()
    release.set()
    await asyncio.wait_for(stop_task, 1)
    assert (await runtime.status())["state"] == "closed"


@pytest.mark.asyncio
async def test_catalog_control_routes_require_auth_are_no_store_and_hide_private_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private-configuration" / "config.yaml"
    public_key = _public_key()
    catalog = NativeCatalogRuntime(
        _enabled_config(),
        config_path=config_path,
        environment={KEY_ENV: public_key},
        transport=httpx.MockTransport(
            lambda _request: _json_response(200, _golden_bytes())
        ),
        clock=lambda: TEST_NOW,
    )
    assert (await catalog.check_now())["outcome"] == "updated"

    class Facade:
        config = MacConfig()

        async def compatibility_catalog_status(self):
            return await catalog.status()

        async def compatibility_catalog_metadata(self):
            return await catalog.metadata()

        async def check_compatibility_catalog(self):
            return await catalog.check_now()

    monkeypatch.setenv("ADMIN_PASSWORD", "catalog-admin-password")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(Facade())),
        base_url="http://mnemosyne-control.test",
    )
    try:
        assert (await client.get("/manager/catalog")).status_code == 401
        for method, path in (
            ("GET", "/manager/catalog"),
            ("GET", "/manager/catalog/models"),
            ("POST", "/manager/catalog/check"),
        ):
            response = await client.request(
                method,
                path,
                auth=("admin", "catalog-admin-password"),
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            encoded = response.text
            for private in (
                ORIGIN,
                UPDATE_PATH,
                KEY_ENV,
                public_key,
                str(config_path.parent),
            ):
                assert private not in encoded
        metadata = await client.get(
            "/manager/catalog/models",
            auth=("admin", "catalog-admin-password"),
        )
        assert metadata.json()["models"][0]["logical_model_id"] == (
            "example-flash-vnext"
        )
        assert metadata.json()["recipes"][0]["engine"] == "llama.cpp"
    finally:
        await client.aclose()
        await catalog.stop()


@pytest.mark.asyncio
async def test_catalog_failure_does_not_regress_jit_inference_storage_downloads_or_accounting(
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "exact-selected-model-root"
    model_root.mkdir()
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"omlx": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "local-state.db")},
            "catalog": _enabled_config().model_dump(mode="json"),
            "storage": {
                "default": "selected",
                "locations": [
                    {"name": "selected", "path": str(model_root)}
                ],
            },
            "models": [
                {
                    "alias": "frontier",
                    "engine": "omlx",
                    "model": "publisher/exact-model",
                }
            ],
        }
    )
    seen: list[dict[str, object]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
        )

    adapter = _InferenceAdapter()
    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    runtime = NativeRuntime(
        config,
        config_path=tmp_path / "configuration" / "config.yaml",
        adapters={EngineName.OMLX: adapter},
        proxy_client=proxy_client,
        # The configured trust reference is missing. Catalog startup must fail
        # closed without becoming an inference dependency or attempting HTTP.
        catalog_environment={},
        catalog_transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(
                AssertionError("missing trust must prevent catalog network")
            )
        ),
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        catalog_status = await runtime.compatibility_catalog_status()
        assert catalog_status["last_error_code"] == "catalog_trust_unavailable"
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "frontier", "messages": []},
        )
        assert response.status_code == 200
        assert adapter.loads == 1
        assert seen == [{"model": "publisher/exact-model", "messages": []}]
        rows = await runtime.usage.list_usage()
        assert len(rows) == 1
        assert rows[0]["total_tokens"] == 4
        assert await runtime.installer.list() == []
        assert runtime.config.storage.locations[0].path == str(model_root)
        assert runtime.startup_error is None
    finally:
        await client.aclose()
        await runtime.stop()


@pytest.mark.asyncio
async def test_older_settings_payload_cannot_silently_remove_catalog_policy(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "configuration" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {"llama_cpp": {"enabled": False}},
            "paths": {"state_database": str(tmp_path / "settings.db")},
            "catalog": _enabled_config().model_dump(mode="json"),
        }
    )
    save_config(config, config_path)
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters={},
        catalog_environment={},
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_control_app(runtime)),
        base_url="http://mnemosyne-control.test",
    )
    try:
        snapshot = (await client.get("/manager/config")).json()
        older_payload = dict(snapshot["config"])
        older_payload.pop("catalog")
        older_payload["server"]["max_queue_depth"] = 64
        saved = await client.put(
            "/manager/config",
            json={
                "config": older_payload,
                "revision": snapshot["revision"],
            },
        )
        assert saved.status_code == 200
        persisted = (await runtime.configuration_snapshot())[0]
        assert persisted.catalog == config.catalog
        assert persisted.server.max_queue_depth == 64
    finally:
        await client.aclose()
        await runtime.stop()
