from __future__ import annotations

import base64
import copy
import json
import time
import uuid
from dataclasses import replace
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mnemosyne_fleet.app import create_app
from mnemosyne_fleet.compatibility_catalog import (
    canonical_json,
    catalog_digest,
    signing_message,
)
from mnemosyne_fleet.config import PlacementConfig

from .test_catalog_placement_app import _catalog_config, _golden
from .test_inventory_app import _complete_pairing, _inventory
from .test_pairing_app import _locator_policy, _pairing_config
from .helpers import snapshot_payload


ADMIN = {"Authorization": "Bearer admin-key"}
TEST_SEED = bytes(range(1, 33))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _eligible_catalog() -> dict[str, Any]:
    envelope = _golden()
    recipe = envelope["catalog"]["recipes"][0]
    recipe["compatibility_tier"] = "verified"
    recipe["hardware"]["soc_families"] = ["apple-m3-max"]
    recipe["hardware"]["required_features"] = []
    recipe["runtime"]["release_tier"] = "stable"
    recipe["runtime"]["known_bad_versions"] = []
    recipe["runtime"]["allowed_runtime_fingerprints"] = []
    recipe["runtime"]["known_bad_runtime_fingerprints"] = []
    recipe["runtime"]["required_features"] = []
    envelope["catalog_digest"] = catalog_digest(envelope["catalog"])
    signature = Ed25519PrivateKey.from_private_bytes(TEST_SEED).sign(
        signing_message(envelope["catalog"])
    )
    envelope["signatures"] = [
        {
            "key_id": "test-catalog-2026-a",
            "algorithm": "Ed25519",
            "signature": _encode(signature),
        }
    ]
    return envelope


def _transport(envelope: dict[str, Any]) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            stream=httpx.ByteStream(canonical_json(envelope)),
            headers={"Content-Type": "application/json"},
        )
    )


def _placement_intent() -> dict[str, object]:
    return {
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


def _create_intent(
    basis: dict[str, object],
    *,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    return {
        **_placement_intent(),
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "candidate_basis": basis,
        "alias": "example-flash",
    }


def _ack(job: dict[str, object], *, state: str = "received") -> dict[str, object]:
    return {
        "schema_version": 1,
        "job_id": job["job_id"],
        "job_revision": job["job_revision"],
        "state": state,
        "bytes_downloaded": 0,
        "total_bytes": 1000,
        "updated_at": time.time(),
        "result_code": "cancelled_by_hub" if state == "cancelled" else None,
    }


async def _enabled_pairing_and_inventory(
    client: httpx.AsyncClient,
    envelope: dict[str, Any],
) -> tuple[dict[str, object], dict[str, object]]:
    paired = await _complete_pairing(client)
    pairing_id = str(paired["pairing_id"])
    enabled = await client.put(
        f"/fleet/api/v1/pairing/enrollments/{pairing_id}/enabled",
        headers=ADMIN,
        json={
            "schema_version": 1,
            "request_id": str(uuid.uuid4()),
            "enabled": True,
        },
    )
    assert enabled.status_code == 200, enabled.text
    inventory = _inventory(
        pairing_id=pairing_id,
        generation=int(paired["credential_generation"]),
        instance_id=str(uuid.uuid4()),
        sequence=1,
    )
    inventory["service"]["supported_job_versions"] = [1]  # type: ignore[index]
    inventory["service"]["catalog_version"] = envelope["catalog"][
        "catalog_version"
    ]  # type: ignore[index]
    inventory["service"]["catalog_digest"] = envelope["catalog_digest"]  # type: ignore[index]
    for runtime in inventory["runtimes"]:  # type: ignore[index]
        runtime["catalog_digest"] = envelope["catalog_digest"]
    synced = await client.post(
        f"/fleet/management/v1/pairings/{pairing_id}/inventory-sync",
        headers={"Authorization": f"Bearer {paired['management_bearer']}"},
        json=inventory,
    )
    assert synced.status_code == 200, synced.text
    return paired, inventory


async def _recommend(
    client: httpx.AsyncClient,
) -> dict[str, object]:
    response = await client.post(
        "/fleet/api/v1/placement/recommendations",
        headers=ADMIN,
        json=_placement_intent(),
    )
    assert response.status_code == 200, response.text
    eligible = [
        row for row in response.json()["candidates"] if row["eligible"]
    ]
    assert eligible
    return eligible[0]["basis"]


async def test_desired_install_is_explicit_bounded_idempotent_and_path_free(
    tmp_path,
) -> None:
    envelope = _eligible_catalog()
    config = replace(
        _pairing_config(tmp_path),
        catalog=_catalog_config(tmp_path),
        placement=PlacementConfig(
            remote_installs_enabled=True,
            recommendation_valid_seconds=45,
            desired_install_database_path=(
                tmp_path / "private" / "desired-installs.db"
            ),
            desired_install_valid_seconds=900,
        ),
    )

    async def activation_probe(_candidate) -> None:
        return None

    app = create_app(
        config,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=activation_probe,
        catalog_update_transport=_transport(envelope),
        start_polling=False,
        start_catalog_updates=False,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://nyx.example.internal",
        ) as client:
            assert (
                await client.post("/fleet/api/v1/catalog/check", headers=ADMIN)
            ).status_code == 200
            paired, inventory = await _enabled_pairing_and_inventory(
                client, envelope
            )
            feature_status = (
                await client.get("/fleet/api/status", headers=ADMIN)
            ).json()["mac_pool"]
            assert feature_status["pairing"]["available"] is True
            assert feature_status["inventory"]["available"] is True
            assert feature_status["catalog"]["available"] is True
            assert feature_status["remote_installs"]["available"] is True
            basis = await _recommend(client)
            intent = _create_intent(basis)
            assert (
                await client.post("/fleet/api/v1/desired-installs", json=intent)
            ).status_code == 401
            assert (
                await client.get("/fleet/api/v1/desired-installs")
            ).status_code == 401
            for forbidden, value in (
                ("job_id", str(uuid.uuid4())),
                ("selected_target", basis["pairing_id"]),
                ("artifact_id", "caller-chosen"),
                ("engine", "llama.cpp"),
                ("path", "/Volumes/private/model"),
                ("created_at", time.time()),
                ("runtime", {"install": True}),
            ):
                rejected = await client.post(
                    "/fleet/api/v1/desired-installs",
                    headers=ADMIN,
                    json={**intent, forbidden: value},
                )
                assert rejected.status_code == 400
                assert "/Volumes/private/model" not in rejected.text
            changed_basis = copy.deepcopy(intent)
            changed_basis["candidate_basis"]["storage_binding_generation"] += 1
            basis_rejected = await client.post(
                "/fleet/api/v1/desired-installs",
                headers=ADMIN,
                json=changed_basis,
            )
            assert basis_rejected.status_code == 409
            assert basis_rejected.json()["detail"]["code"] == (
                "desired_install_basis_changed"
            )
            duplicate = await client.post(
                "/fleet/api/v1/desired-installs",
                headers={**ADMIN, "Content-Type": "application/json"},
                content=b'{"schema_version":1,"schema_version":1}',
            )
            encoded = await client.post(
                "/fleet/api/v1/desired-installs",
                headers={
                    **ADMIN,
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                content=b"not-inspected",
            )
            oversized = await client.post(
                "/fleet/api/v1/desired-installs",
                headers={**ADMIN, "Content-Type": "application/json"},
                content=b" " * (16 * 1024 + 1),
            )
            assert duplicate.status_code == 400
            assert encoded.status_code == 415
            assert oversized.status_code == 413

            scheduler_before = app.state.scheduler.model_matrix()
            enrollments_before = app.state.registry.enrollments()
            created = await client.post(
                "/fleet/api/v1/desired-installs",
                headers=ADMIN,
                json=intent,
            )
            assert created.status_code == 201, created.text
            payload = created.json()
            job = payload["job"]
            assert payload["idempotent_replay"] is False
            assert job["job_revision"] == 1
            assert job["desired_state"] == "run"
            assert job["pairing_id"] == basis["pairing_id"]
            assert job["storage_location_id"] == basis["storage_location_id"]
            assert job["artifact_id"] == "example-flash-vnext-gguf-q4"
            assert "repository" not in json.dumps(job)
            assert "/Volumes/" not in json.dumps(job)
            assert not {
                "memory",
                "hardware",
                "runtime",
                "launch",
                "selected",
                "chosen",
                "cleanup",
                "delete",
            }.intersection(job)

            replay = await client.post(
                "/fleet/api/v1/desired-installs",
                headers=ADMIN,
                json=copy.deepcopy(intent),
            )
            assert replay.status_code == 200
            assert replay.json()["job"]["job_id"] == job["job_id"]
            assert replay.json()["idempotent_replay"] is True
            conflict = await client.post(
                "/fleet/api/v1/desired-installs",
                headers=ADMIN,
                json={**intent, "alias": "different"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == (
                "desired_install_idempotency_conflict"
            )

            listed = await client.get(
                "/fleet/api/v1/desired-installs", headers=ADMIN
            )
            read = await client.get(
                f"/fleet/api/v1/desired-installs/{job['job_id']}",
                headers=ADMIN,
            )
            assert listed.status_code == read.status_code == 200
            assert listed.headers["cache-control"] == "no-store"
            assert read.json()["job"] == job
            assert (
                await client.get(
                    "/fleet/api/v1/desired-installs/not-a-uuid",
                    headers=ADMIN,
                )
            ).status_code == 400
            assert (
                await client.get(
                    f"/fleet/api/v1/desired-installs/{uuid.uuid4()}",
                    headers=ADMIN,
                )
            ).status_code == 404

            inventory["inventory_sequence"] = 2
            delivered = await client.post(
                f"/fleet/management/v1/pairings/{paired['pairing_id']}/inventory-sync",
                headers={
                    "Authorization": f"Bearer {paired['management_bearer']}"
                },
                json=inventory,
            )
            assert delivered.status_code == 200, delivered.text
            assert delivered.json()["desired_jobs"] == [job]

            inventory["inventory_sequence"] = 3
            inventory["job_acknowledgements"] = [_ack(job)]
            acknowledged = await client.post(
                f"/fleet/management/v1/pairings/{paired['pairing_id']}/inventory-sync",
                headers={
                    "Authorization": f"Bearer {paired['management_bearer']}"
                },
                json=inventory,
            )
            assert acknowledged.status_code == 200, acknowledged.text
            assert acknowledged.json()["desired_jobs"] == []

            assert app.state.scheduler.model_matrix() == scheduler_before
            assert app.state.registry.enrollments() == enrollments_before

            # A valid pending job cannot be delivered after revocation; the
            # management credential is rejected before job lookup or output.
            post_ack_basis = await _recommend(client)
            pending = await client.post(
                "/fleet/api/v1/desired-installs",
                headers=ADMIN,
                json=_create_intent(post_ack_basis),
            )
            assert pending.status_code == 201, pending.text
            revoked = await client.post(
                f"/fleet/api/v1/pairing/enrollments/{paired['pairing_id']}/revoke",
                headers=ADMIN,
                json={
                    "schema_version": 1,
                    "request_id": str(uuid.uuid4()),
                },
            )
            assert revoked.status_code == 200
            inventory["inventory_sequence"] = 4
            after_revoke = await client.post(
                f"/fleet/management/v1/pairings/{paired['pairing_id']}/inventory-sync",
                headers={
                    "Authorization": f"Bearer {paired['management_bearer']}"
                },
                json=inventory,
            )
            assert after_revoke.status_code == 401
            assert "desired_jobs" not in after_revoke.json()


async def test_cancel_generation_storage_expiry_and_restart_fence_delivery(
    tmp_path,
) -> None:
    envelope = _eligible_catalog()
    now = [time.time()]
    config = replace(
        _pairing_config(tmp_path),
        catalog=_catalog_config(tmp_path),
        placement=PlacementConfig(
            remote_installs_enabled=True,
            desired_install_database_path=(
                tmp_path / "private" / "desired-installs.db"
            ),
            desired_install_valid_seconds=120,
        ),
    )

    async def activation_probe(_candidate) -> None:
        return None

    def make_app():
        return create_app(
            config,
            pairing_locator_policy=_locator_policy(),
            pairing_activation_probe=activation_probe,
            catalog_update_transport=_transport(envelope),
            catalog_clock=lambda: now[0],
            start_polling=False,
            start_catalog_updates=False,
        )

    first = make_app()
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first),
            base_url="https://nyx.example.internal",
        ) as client:
            assert (
                await client.post("/fleet/api/v1/catalog/check", headers=ADMIN)
            ).status_code == 200
            paired, inventory = await _enabled_pairing_and_inventory(
                client, envelope
            )
            basis = await _recommend(client)
            created = await client.post(
                "/fleet/api/v1/desired-installs",
                headers=ADMIN,
                json=_create_intent(basis),
            )
            assert created.status_code == 201, created.text
            job = created.json()["job"]
            revision_required = await client.post(
                f"/fleet/api/v1/desired-installs/{job['job_id']}/cancel",
                headers=ADMIN,
            )
            assert revision_required.status_code == 428
            assert revision_required.json()["detail"]["code"] == (
                "desired_install_revision_required"
            )
            invalid_revision = await client.post(
                f"/fleet/api/v1/desired-installs/{job['job_id']}/cancel",
                headers={**ADMIN, "If-Match": "1"},
            )
            assert invalid_revision.status_code == 400
            assert invalid_revision.json()["detail"]["code"] == (
                "desired_install_revision_invalid"
            )
            wrong_revision = await client.post(
                f"/fleet/api/v1/desired-installs/{job['job_id']}/cancel",
                headers={**ADMIN, "If-Match": '"2"'},
            )
            assert wrong_revision.status_code == 409
            assert wrong_revision.json()["detail"]["code"] == (
                "desired_install_revision_conflict"
            )
            cancelled = await client.post(
                f"/fleet/api/v1/desired-installs/{job['job_id']}/cancel",
                headers={**ADMIN, "If-Match": '"1"'},
            )
            assert cancelled.status_code == 200, cancelled.text
            cancel_job = cancelled.json()["job"]
            assert cancel_job["job_revision"] == 2
            assert cancel_job["desired_state"] == "cancel"
            retry = await client.post(
                f"/fleet/api/v1/desired-installs/{job['job_id']}/cancel",
                headers={**ADMIN, "If-Match": '"1"'},
            )
            assert retry.status_code == 200
            assert retry.json()["job"]["job_revision"] == 2
            replay = await client.post(
                f"/fleet/api/v1/desired-installs/{job['job_id']}/cancel",
                headers={**ADMIN, "If-Match": '"2"'},
            )
            assert replay.json()["job"]["job_revision"] == 2
            assert (
                await client.post(
                    f"/fleet/api/v1/desired-installs/{job['job_id']}/cancel",
                    headers={**ADMIN, "If-Match": '"2"'},
                    json={"delete": True},
                )
            ).status_code == 400

            inventory["inventory_sequence"] = 2
            delivered_cancel = await client.post(
                f"/fleet/management/v1/pairings/{paired['pairing_id']}/inventory-sync",
                headers={
                    "Authorization": f"Bearer {paired['management_bearer']}"
                },
                json=inventory,
            )
            assert delivered_cancel.status_code == 200
            assert delivered_cancel.json()["desired_jobs"] == [cancel_job]

            # A second job survives restart; persisted inventory alone cannot
            # restore authority, but an increasing sequence on the same exact
            # service instance can after every fence is recomputed.
            basis = await _recommend(client)
            pending = await client.post(
                "/fleet/api/v1/desired-installs",
                headers=ADMIN,
                json=_create_intent(basis),
            )
            assert pending.status_code == 201, pending.text
            pending_job = pending.json()["job"]

    restarted = make_app()
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted),
            base_url="https://nyx.example.internal",
        ) as client:
            inventory["inventory_sequence"] = 3
            inventory["job_acknowledgements"] = [
                _ack(cancel_job, state="cancelled")
            ]
            recovered = await client.post(
                f"/fleet/management/v1/pairings/{paired['pairing_id']}/inventory-sync",
                headers={
                    "Authorization": f"Bearer {paired['management_bearer']}"
                },
                json=inventory,
            )
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["desired_jobs"] == [pending_job]

            # Storage binding changes and credential changes never retarget.
            basis = await _recommend(client)
            fenced = await client.post(
                "/fleet/api/v1/desired-installs",
                headers=ADMIN,
                json=_create_intent(basis),
            )
            assert fenced.status_code == 201, fenced.text
            inventory["inventory_sequence"] = 4
            storage_id = fenced.json()["job"]["storage_location_id"]
            for storage in inventory["storage_locations"]:
                if storage["storage_location_id"] == storage_id:
                    storage["binding_generation"] += 1
            storage_changed = await client.post(
                f"/fleet/management/v1/pairings/{paired['pairing_id']}/inventory-sync",
                headers={
                    "Authorization": f"Bearer {paired['management_bearer']}"
                },
                json=inventory,
            )
            assert storage_changed.status_code == 200, storage_changed.text
            assert storage_changed.json()["desired_jobs"] == []

            wrong_generation = copy.deepcopy(inventory)
            wrong_generation["inventory_sequence"] = 5
            wrong_generation["credential_generation"] += 1
            rejected = await client.post(
                f"/fleet/management/v1/pairings/{paired['pairing_id']}/inventory-sync",
                headers={
                    "Authorization": f"Bearer {paired['management_bearer']}"
                },
                json=wrong_generation,
            )
            assert rejected.status_code == 401

            now[0] += 121
            inventory["inventory_sequence"] = 5
            expired = await client.post(
                f"/fleet/management/v1/pairings/{paired['pairing_id']}/inventory-sync",
                headers={
                    "Authorization": f"Bearer {paired['management_bearer']}"
                },
                json=inventory,
            )
            assert expired.status_code == 200
            assert expired.json()["desired_jobs"] == []


async def test_desired_install_routes_are_absent_when_global_switch_is_off(
    tmp_path,
) -> None:
    app = create_app(_pairing_config(tmp_path), start_polling=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            assert (
                await client.post(
                    "/fleet/api/v1/desired-installs",
                    headers=ADMIN,
                    json={},
                )
            ).status_code == 404
            assert (
                await client.get(
                    f"/fleet/api/v1/desired-installs/{uuid.uuid4()}",
                    headers=ADMIN,
                )
            ).status_code == 404


async def test_desired_install_store_failure_does_not_degrade_inference(
    tmp_path,
) -> None:
    envelope = _eligible_catalog()
    insecure = tmp_path / "insecure-journal"
    insecure.mkdir(mode=0o755)
    config = replace(
        _pairing_config(tmp_path),
        catalog=_catalog_config(tmp_path),
        placement=PlacementConfig(
            remote_installs_enabled=True,
            desired_install_database_path=insecure / "desired.db",
        ),
    )

    async def activation_probe(_candidate) -> None:
        return None

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=snapshot_payload("node-a"))
        )
    ) as registry_client, httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"id": "still-routed"})
        )
    ) as proxy_client:
        app = create_app(
            config,
            registry_client=registry_client,
            proxy_client=proxy_client,
            pairing_locator_policy=_locator_policy(),
            pairing_activation_probe=activation_probe,
            catalog_update_transport=_transport(envelope),
            start_polling=False,
            start_catalog_updates=False,
        )
        async with app.router.lifespan_context(app):
            assert app.state.desired_install_runtime == {
                "enabled": True,
                "available": False,
                "error_code": "desired_install_unavailable",
            }
            await app.state.registry.poll_all_once()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fleet",
            ) as client:
                unavailable = await client.get(
                    "/fleet/api/v1/desired-installs", headers=ADMIN
                )
                routed = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer client-key"},
                    json={"model": "qwen-coder", "input": "do-not-store"},
                )
            assert unavailable.status_code == 503
            assert routed.status_code == 200
            assert routed.json() == {"id": "still-routed"}
