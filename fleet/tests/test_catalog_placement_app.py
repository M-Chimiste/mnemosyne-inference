from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from mnemosyne_fleet.app import create_app
from mnemosyne_fleet.compatibility_catalog import (
    CatalogStoreError,
    canonical_json,
)
from mnemosyne_fleet.config import (
    CatalogConfig,
    CatalogTrustKeyConfig,
    PlacementConfig,
)

from .helpers import fleet_config, snapshot_payload
from .test_inventory_app import _complete_pairing, _inventory
from .test_pairing_app import _locator_policy, _pairing_config


ROOT = Path(__file__).resolve().parents[2]
CATALOG_V1 = ROOT / "compatibility_catalog" / "v1"
ADMIN = {"Authorization": "Bearer admin-key"}


def _golden() -> dict[str, Any]:
    return json.loads(
        (CATALOG_V1 / "catalog.golden.json").read_text(encoding="utf-8")
    )


def _catalog_config(tmp_path: Path) -> CatalogConfig:
    public = json.loads(
        (CATALOG_V1 / "test_keys.json").read_text(encoding="utf-8")
    )["keys"][0]
    return CatalogConfig(
        enabled=True,
        state_directory=tmp_path / "private" / "catalog",
        update_origin="https://catalog.mnemosyne.test",
        update_path="/v1/apple-silicon/catalog.json",
        update_interval_seconds=3600,
        total_timeout_seconds=2,
        connect_timeout_seconds=1,
        max_attempts=1,
        trusted_keys=(
            CatalogTrustKeyConfig(
                key_id=public["key_id"],
                public_key=public["public_key"],
            ),
        ),
    )


def _catalog_transport(seen: list[httpx.Request] | None = None):
    envelope = _golden()

    async def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = canonical_json(envelope)
        return httpx.Response(
            200,
            stream=httpx.ByteStream(body),
            headers={"Content-Type": "application/json"},
        )

    return httpx.MockTransport(handler)


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value))
    return set()


async def test_catalog_and_placement_routes_are_absent_by_default(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires catalog and pairing"):
        create_app(
            replace(
                fleet_config(tmp_path / "invalid-programmatic"),
                catalog=_catalog_config(tmp_path / "invalid-programmatic"),
                placement=PlacementConfig(remote_installs_enabled=True),
            ),
            start_polling=False,
        )
    disabled = create_app(fleet_config(tmp_path), start_polling=False)
    catalog_only = create_app(
        replace(
            fleet_config(tmp_path / "catalog-only"),
            catalog=_catalog_config(tmp_path / "catalog-only"),
        ),
        catalog_update_transport=_catalog_transport(),
        start_polling=False,
        start_catalog_updates=False,
    )
    async with disabled.router.lifespan_context(disabled):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=disabled),
            base_url="http://fleet",
        ) as client:
            assert (await client.get("/fleet/api/v1/catalog/status", headers=ADMIN)).status_code == 404
            assert (
                await client.post(
                    "/fleet/api/v1/placement/recommendations",
                    headers=ADMIN,
                    json={},
                )
            ).status_code == 404
    async with catalog_only.router.lifespan_context(catalog_only):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=catalog_only),
            base_url="http://fleet",
        ) as client:
            assert (await client.get("/fleet/api/v1/catalog/status", headers=ADMIN)).status_code == 200
            assert (
                await client.post(
                    "/fleet/api/v1/placement/recommendations",
                    headers=ADMIN,
                    json={},
                )
            ).status_code == 404


async def test_catalog_admin_surface_is_bounded_authenticated_and_path_free(
    tmp_path,
) -> None:
    seen: list[httpx.Request] = []
    config = replace(
        fleet_config(tmp_path),
        catalog=_catalog_config(tmp_path),
    )
    app = create_app(
        config,
        catalog_update_transport=_catalog_transport(seen),
        start_polling=False,
        start_catalog_updates=False,
    )
    async with app.router.lifespan_context(app):
        scheduler_before = app.state.scheduler.model_matrix()
        enrollments_before = app.state.registry.enrollments()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            assert (await client.get("/fleet/api/v1/catalog/status")).status_code == 401
            before = await client.get("/fleet/api/v1/catalog/status", headers=ADMIN)
            assert before.status_code == 200
            assert before.json()["available"] is False
            checked = await client.post("/fleet/api/v1/catalog/check", headers=ADMIN)
            assert checked.status_code == 200, checked.text
            assert checked.json()["result"]["outcome"] == "updated"
            models = await client.get(
                "/fleet/api/v1/catalog/models",
                params={"limit": 1},
                headers=ADMIN,
            )
            recipes = await client.get(
                "/fleet/api/v1/catalog/recipes",
                params={"limit": 1, "logical_model_id": "example-flash-vnext"},
                headers=ADMIN,
            )
            assert models.status_code == 200
            assert recipes.status_code == 200
            assert models.json()["models"][0]["logical_model_id"] == (
                "example-flash-vnext"
            )
            assert recipes.json()["recipes"][0]["recipe_id"] == (
                "example-flash-vnext-llamacpp-q4"
            )
            assert models.headers["cache-control"] == "no-store"
            assert recipes.headers["cache-control"] == "no-store"
        assert app.state.scheduler.model_matrix() == scheduler_before
        assert app.state.registry.enrollments() == enrollments_before

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == (
        "https://catalog.mnemosyne.test/v1/apple-silicon/catalog.json"
    )
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers
    rendered = models.text + recipes.text + checked.text
    assert "/Volumes/" not in rendered
    assert "active.catalog.json" not in rendered
    assert str(config.catalog.state_directory) not in rendered
    assert "catalog.mnemosyne.test" not in rendered
    assert config.catalog.trusted_keys[0].public_key not in rendered


async def test_catalog_background_refresh_is_bounded_and_lifecycle_owned(
    tmp_path,
) -> None:
    refreshed = asyncio.Event()
    calls = 0
    envelope = _golden()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        refreshed.set()
        return httpx.Response(
            200,
            stream=httpx.ByteStream(canonical_json(envelope)),
            headers={"Content-Type": "application/json"},
        )

    config = replace(
        fleet_config(tmp_path),
        catalog=_catalog_config(tmp_path),
    )
    app = create_app(
        config,
        catalog_update_transport=httpx.MockTransport(handler),
        start_polling=False,
        start_catalog_updates=True,
    )
    # The service constructor enforces the production 60-second minimum. A
    # short post-construction interval keeps this lifecycle test bounded.
    app.state.catalog._update_interval_seconds = 0.01

    async def wait_until_active() -> None:
        await refreshed.wait()
        while app.state.catalog.active().source != "signed":
            await asyncio.sleep(0.001)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(wait_until_active(), timeout=1)
        assert calls >= 1
        assert app.state.catalog.active().source == "signed"
    completed_calls = calls
    await asyncio.sleep(0.03)
    assert calls == completed_calls


async def test_hub_stamps_closed_placement_and_returns_every_inventory_storage(
    tmp_path,
) -> None:
    config = replace(
        _pairing_config(tmp_path),
        catalog=_catalog_config(tmp_path),
        placement=PlacementConfig(
            remote_installs_enabled=True,
            recommendation_valid_seconds=45,
        ),
    )

    async def activation_probe(_candidate) -> None:
        return None

    app = create_app(
        config,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=activation_probe,
        catalog_update_transport=_catalog_transport(),
        start_polling=False,
        start_catalog_updates=False,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://nyx.example.internal",
        ) as client:
            initial_check = await client.post(
                "/fleet/api/v1/catalog/check", headers=ADMIN
            )
            assert initial_check.status_code == 200, initial_check.text
            paired = await _complete_pairing(client)
            pairing_id = str(paired["pairing_id"])
            inventory = _inventory(
                pairing_id=pairing_id,
                generation=int(paired["credential_generation"]),
                instance_id=str(uuid.uuid4()),
                sequence=1,
            )
            envelope = _golden()
            inventory["service"]["catalog_version"] = envelope["catalog"][
                "catalog_version"
            ]
            inventory["service"]["catalog_digest"] = envelope["catalog_digest"]
            for runtime in inventory["runtimes"]:
                runtime["catalog_digest"] = envelope["catalog_digest"]
            synced = await client.post(
                f"/fleet/management/v1/pairings/{pairing_id}/inventory-sync",
                headers={
                    "Authorization": f"Bearer {paired['management_bearer']}"
                },
                json=inventory,
            )
            assert synced.status_code == 200

            intent = {
                "schema_version": 1,
                "logical_model_id": "example-flash-vnext",
                "recipe_id": "example-flash-vnext-llamacpp-q4",
                "required_capabilities": ["chat/completions", "responses"],
                "required_context_tokens": 8192,
                "required_concurrency": 2,
                "allowed_service_classes": [
                    "primary",
                    "opportunistic",
                    "overflow",
                ],
            }
            assert (
                await client.post(
                    "/fleet/api/v1/placement/recommendations",
                    headers={"Authorization": "Bearer client-key"},
                    json=intent,
                )
            ).status_code == 401
            for forbidden in (
                "recommendation_id",
                "created_at",
                "selected_pairing_id",
                "path",
                "memory",
                "runtime",
            ):
                rejected = await client.post(
                    "/fleet/api/v1/placement/recommendations",
                    headers=ADMIN,
                    json={**intent, forbidden: "/Volumes/private/model"},
                )
                assert rejected.status_code == 400
                assert rejected.json()["detail"]["code"] == (
                    "placement_request_invalid"
                )
                assert "/Volumes/private/model" not in rejected.text
            duplicate = await client.post(
                "/fleet/api/v1/placement/recommendations",
                headers={**ADMIN, "Content-Type": "application/json"},
                content=b'{"schema_version":1,"schema_version":1}',
            )
            assert duplicate.status_code == 400
            encoded = await client.post(
                "/fleet/api/v1/placement/recommendations",
                headers={
                    **ADMIN,
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                content=b"not-inspected",
            )
            assert encoded.status_code == 415
            oversized = await client.post(
                "/fleet/api/v1/placement/recommendations",
                headers={**ADMIN, "Content-Type": "application/json"},
                content=b" " * (16 * 1024 + 1),
            )
            assert oversized.status_code == 413

            response = await client.post(
                "/fleet/api/v1/placement/recommendations",
                headers=ADMIN,
                json=intent,
            )
            assert response.status_code == 200
            result = response.json()
            uuid.UUID(result["recommendation_id"])
            assert result["created_at"] > 0
            assert result["expires_at"] == result["created_at"]
            assert result["request"]["logical_model_id"] == (
                "example-flash-vnext"
            )
            assert len(result["candidates"]) == len(
                inventory["storage_locations"]
            )
            assert {
                row["basis"]["storage_location_id"]
                for row in result["candidates"]
            } == {
                row["storage_location_id"]
                for row in inventory["storage_locations"]
            }
            assert not any(
                "selected" in key or "chosen" in key
                for key in _all_keys(result)
            )
            serialized = json.dumps(result)
            assert "/Volumes/" not in serialized
            assert "studio.example.internal" not in serialized
            for role in (
                "snapshot_bearer",
                "dispatch_bearer",
                "management_bearer",
            ):
                assert str(paired[role]) not in serialized
            revoked = await client.post(
                f"/fleet/api/v1/pairing/enrollments/{pairing_id}/revoke",
                headers=ADMIN,
                json={
                    "schema_version": 1,
                    "request_id": str(uuid.uuid4()),
                },
            )
            assert revoked.status_code == 200
            after_revoke = await client.post(
                "/fleet/api/v1/placement/recommendations",
                headers=ADMIN,
                json=intent,
            )
            assert after_revoke.status_code == 200
            assert all(
                "pairing_revoked" in row["hard_gate_codes"]
                for row in after_revoke.json()["candidates"]
            )


async def test_catalog_failure_and_store_contention_preserve_valid_lkg_and_inference(
    tmp_path,
    monkeypatch,
) -> None:
    snapshot = snapshot_payload("node-a")

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot)

    def proxy_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "still-routed"})

    config = replace(
        fleet_config(tmp_path),
        catalog=_catalog_config(tmp_path),
    )
    app = create_app(
        config,
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        catalog_update_transport=_catalog_transport(),
        start_polling=False,
        start_catalog_updates=False,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            initial_check = await client.post(
                "/fleet/api/v1/catalog/check", headers=ADMIN
            )
            assert initial_check.status_code == 200, initial_check.text
            catalog = app.state.catalog
            signed_digest = catalog.active().catalog_digest

            def contended_load(*, now=None):
                raise CatalogStoreError("catalog_store_busy")

            monkeypatch.setattr(catalog._store, "load", contended_load)
            status = await client.get("/fleet/api/v1/catalog/status", headers=ADMIN)
            assert status.status_code == 200
            assert status.json()["available"] is True
            assert status.json()["active"]["catalog_digest"] == signed_digest
            assert status.json()["load_error_code"] == "catalog_store_unavailable"

            await app.state.registry.poll_all_once()
            routed = await client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer client-key"},
                json={"model": "qwen-coder", "input": "must-not-persist"},
            )
            assert routed.status_code == 200
            assert routed.json() == {"id": "still-routed"}
            expires_at = catalog.active().expires_at
            assert expires_at is not None
            catalog._clock = lambda: expires_at
            expired = await client.get(
                "/fleet/api/v1/catalog/status",
                headers=ADMIN,
            )
            assert expired.status_code == 200
            assert expired.json()["available"] is False
            assert expired.json()["active"]["source"] == "built_in"


async def test_hub_restart_keeps_inventory_visible_but_fences_placement(
    tmp_path,
) -> None:
    config = replace(
        _pairing_config(tmp_path),
        catalog=_catalog_config(tmp_path),
        placement=PlacementConfig(remote_installs_enabled=True),
    )

    async def activation_probe(_candidate) -> None:
        return None

    first = create_app(
        config,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=activation_probe,
        catalog_update_transport=_catalog_transport(),
        start_polling=False,
        start_catalog_updates=False,
    )
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first),
            base_url="https://nyx.example.internal",
        ) as client:
            assert (
                await client.post("/fleet/api/v1/catalog/check", headers=ADMIN)
            ).status_code == 200
            paired = await _complete_pairing(client)
            pairing_id = str(paired["pairing_id"])
            observation = _inventory(
                pairing_id=pairing_id,
                generation=int(paired["credential_generation"]),
                instance_id=str(uuid.uuid4()),
                sequence=1,
            )
            envelope = _golden()
            observation["service"]["catalog_version"] = envelope["catalog"][
                "catalog_version"
            ]
            observation["service"]["catalog_digest"] = envelope[
                "catalog_digest"
            ]
            for runtime in observation["runtimes"]:
                runtime["catalog_digest"] = envelope["catalog_digest"]
            assert (
                await client.post(
                    f"/fleet/management/v1/pairings/{pairing_id}/inventory-sync",
                    headers={
                        "Authorization": (
                            f"Bearer {paired['management_bearer']}"
                        )
                    },
                    json=observation,
                )
            ).status_code == 200

    restarted = create_app(
        config,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=activation_probe,
        catalog_update_transport=_catalog_transport(),
        start_polling=False,
        start_catalog_updates=False,
    )
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted),
            base_url="https://nyx.example.internal",
        ) as client:
            response = await client.post(
                "/fleet/api/v1/placement/recommendations",
                headers=ADMIN,
                json={
                    "schema_version": 1,
                    "logical_model_id": "example-flash-vnext",
                    "recipe_id": "example-flash-vnext-llamacpp-q4",
                    "required_capabilities": [
                        "chat/completions",
                        "responses",
                    ],
                    "required_context_tokens": 8192,
                    "required_concurrency": 2,
                    "allowed_service_classes": [
                        "primary",
                        "opportunistic",
                        "overflow",
                    ],
                },
            )
            assert response.status_code == 200
            assert response.json()["candidates"]
            assert all(
                "hub_restarted" in row["hard_gate_codes"]
                for row in response.json()["candidates"]
            )


async def test_failed_catalog_check_does_not_degrade_existing_inference(tmp_path) -> None:
    snapshot = snapshot_payload("node-a")

    def unavailable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    config = replace(
        fleet_config(tmp_path),
        catalog=_catalog_config(tmp_path),
    )
    app = create_app(
        config,
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=snapshot)
            )
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"ok": True})
            )
        ),
        catalog_update_transport=httpx.MockTransport(unavailable),
        start_polling=False,
        start_catalog_updates=False,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            failed = await client.post("/fleet/api/v1/catalog/check", headers=ADMIN)
            assert failed.status_code == 503
            assert failed.json()["result"]["error_code"] == (
                "catalog_update_network_error"
            )
            await app.state.registry.poll_all_once()
            routed = await client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer client-key"},
                json={"model": "qwen-coder", "input": "hello"},
            )
            assert routed.status_code == 200
            assert routed.json() == {"ok": True}
