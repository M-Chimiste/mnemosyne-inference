from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import httpx

from mnemosyne_fleet.app import create_app

from .test_pairing_app import _locator_policy, _pairing_config


PROTOCOL_V1 = Path(__file__).resolve().parents[2] / "mac_pool_protocol" / "v1"
ADMIN = {"Authorization": "Bearer admin-key"}
LOCATOR = "https://studio.example.internal:1240"


def _uuid() -> str:
    return str(uuid.uuid4())


def _inventory(
    *,
    pairing_id: str,
    generation: int,
    instance_id: str,
    sequence: int,
) -> dict[str, object]:
    value = json.loads(
        (PROTOCOL_V1 / "mac_inventory.example.json").read_text(
            encoding="utf-8"
        )
    )
    value["pairing_id"] = pairing_id
    value["credential_generation"] = generation
    value["inventory_instance_id"] = instance_id
    value["inventory_sequence"] = sequence
    return value


async def _complete_pairing(client: httpx.AsyncClient) -> dict[str, object]:
    invitation = (
        await client.post(
            "/fleet/api/v1/pairing/invitations",
            headers=ADMIN,
            json={
                "schema_version": 1,
                "request_id": _uuid(),
                "intent": "new",
                "expected": {
                    "platform": "macos",
                    "reporting_node_id": "studio-mac",
                    "locator": LOCATOR,
                    "transport": "https",
                    "service_class": "primary",
                },
                "expires_in_seconds": 300,
            },
        )
    ).json()
    claim = (
        await client.post(
            "/fleet/pairing/v1/claims",
            json={
                "schema_version": 1,
                "request_id": _uuid(),
                "invitation_id": invitation["invitation_id"],
                "pairing_secret": invitation["pairing_secret"],
                "mac": {
                    "platform": "macos",
                    "service_version": "0.9.0",
                    "display_name": "Studio Mac",
                    "reporting_node_id": "studio-mac",
                },
                "locator": LOCATOR,
                "supported_protocol": {"minimum": 1, "maximum": 1},
            },
        )
    ).json()
    approval = await client.post(
        f"/fleet/api/v1/pairing/claims/{claim['claim_id']}/approve",
        headers=ADMIN,
        json={
            "schema_version": 1,
            "request_id": _uuid(),
            "locator": LOCATOR,
            "service_class": "primary",
            "hub_enabled": False,
        },
    )
    assert approval.status_code == 200
    provisioned = (
        await client.post(
            f"/fleet/pairing/v1/claims/{claim['claim_id']}/provision",
            json={
                "schema_version": 1,
                "request_id": _uuid(),
                "pairing_secret": invitation["pairing_secret"],
            },
        )
    ).json()
    activation = await client.post(
        f"/fleet/management/v1/pairings/{claim['pairing_id']}/activation-ack",
        headers={
            "Authorization": (
                "Bearer "
                + provisioned["credentials"]["management_bearer"]
            )
        },
        json={
            "schema_version": 1,
            "request_id": _uuid(),
            "credential_generation": provisioned["credential_generation"],
            "reporting_node_id": "studio-mac",
            "service_instance_id": "service-instance-1",
        },
    )
    assert activation.status_code == 200
    return {
        "pairing_id": claim["pairing_id"],
        "credential_generation": provisioned["credential_generation"],
        **provisioned["credentials"],
    }


async def test_inventory_sync_uses_only_active_management_role_and_never_routes(
    tmp_path,
) -> None:
    async def activation_probe(_candidate) -> None:
        return None

    app = create_app(
        _pairing_config(tmp_path),
        start_polling=False,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=activation_probe,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://nyx.example.internal",
        ) as client:
            paired = await _complete_pairing(client)
            pairing_id = str(paired["pairing_id"])
            generation = int(paired["credential_generation"])
            instance_id = _uuid()
            inventory = _inventory(
                pairing_id=pairing_id,
                generation=generation,
                instance_id=instance_id,
                sequence=1,
            )
            endpoint = (
                f"/fleet/management/v1/pairings/{pairing_id}/inventory-sync"
            )
            memberships_before = app.state.registry.enrollments()
            for rejected in (
                paired["snapshot_bearer"],
                paired["dispatch_bearer"],
                "client-key",
                "admin-key",
            ):
                response = await client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {rejected}"},
                    json=inventory,
                )
                assert response.status_code == 401
                assert response.json()["detail"]["code"] == (
                    "inventory_authentication_rejected"
                )

            accepted = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {paired['management_bearer']}"
                },
                json=inventory,
            )
            assert accepted.status_code == 200
            assert accepted.json() == {
                "schema_version": 1,
                "ack": {
                    "pairing_id": pairing_id,
                    "credential_generation": generation,
                    "inventory_instance_id": instance_id,
                    "inventory_sequence": 1,
                },
                "desired_jobs": [],
            }
            assert app.state.registry.enrollments() == memberships_before

            unauthorized_list = await client.get(
                "/fleet/api/v1/inventory",
                headers={"Authorization": "Bearer client-key"},
            )
            listed = await client.get(
                "/fleet/api/v1/inventory",
                headers=ADMIN,
            )
            detail = await client.get(
                f"/fleet/api/v1/inventory/{pairing_id}",
                headers=ADMIN,
            )
            assert unauthorized_list.status_code == 401
            assert listed.status_code == 200
            assert listed.json()["devices"][0]["freshness"] == {
                "state": "fresh",
                "reason": None,
                "receipt_age_seconds": listed.json()["devices"][0][
                    "freshness"
                ]["receipt_age_seconds"],
                "authoritative_for_placement": True,
                "authoritative_for_inference": False,
            }
            assert detail.status_code == 200
            assert detail.json()["device"]["inventory"] == inventory
            rendered = listed.text + detail.text
            assert LOCATOR not in rendered
            for credential_name in (
                "snapshot_bearer",
                "dispatch_bearer",
                "management_bearer",
            ):
                assert str(paired[credential_name]) not in rendered


async def test_inventory_rejects_oversize_invalid_and_late_documents_without_echo(
    tmp_path,
) -> None:
    async def activation_probe(_candidate) -> None:
        return None

    app = create_app(
        _pairing_config(tmp_path),
        start_polling=False,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=activation_probe,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://nyx.example.internal",
        ) as client:
            paired = await _complete_pairing(client)
            pairing_id = str(paired["pairing_id"])
            generation = int(paired["credential_generation"])
            endpoint = (
                f"/fleet/management/v1/pairings/{pairing_id}/inventory-sync"
            )
            headers = {
                "Authorization": f"Bearer {paired['management_bearer']}"
            }
            secret = "private-value-that-must-never-be-reflected-or-stored"
            invalid = _inventory(
                pairing_id=pairing_id,
                generation=generation,
                instance_id=_uuid(),
                sequence=1,
            )
            invalid["unexpected_secret"] = secret
            rejected = await client.post(endpoint, headers=headers, json=invalid)
            assert rejected.status_code == 400
            assert secret not in rejected.text

            oversized = await client.post(
                endpoint,
                headers={**headers, "Content-Type": "application/json"},
                content=(b'{"unexpected":"' + b"x" * (2 * 1024 * 1024) + b'"}'),
            )
            assert oversized.status_code == 413

            unauthenticated_oversize = await client.post(
                endpoint,
                headers={
                    "Authorization": "Bearer wrong-management-value",
                    "Content-Type": "application/json",
                },
                content=(b'{"unexpected":"' + b"x" * (2 * 1024 * 1024) + b'"}'),
            )
            assert unauthenticated_oversize.status_code == 401

            malformed_pairing_id = await client.post(
                "/fleet/management/v1/pairings/not-a-uuid/inventory-sync",
                headers=headers,
                json={},
            )
            assert malformed_pairing_id.status_code == 400
            assert malformed_pairing_id.json()["detail"]["code"] == (
                "inventory_invalid_request"
            )

            current = _inventory(
                pairing_id=pairing_id,
                generation=generation,
                instance_id=_uuid(),
                sequence=5,
            )
            assert (
                await client.post(endpoint, headers=headers, json=current)
            ).status_code == 200
            late = copy.deepcopy(current)
            late["inventory_sequence"] = 4
            late_response = await client.post(endpoint, headers=headers, json=late)
            assert late_response.status_code == 409
            assert late_response.json()["detail"]["code"] == (
                "inventory_sequence_stale"
            )

    database_bytes = b"".join(
        path.read_bytes()
        for path in tmp_path.glob("mac-inventory.db*")
        if path.is_file()
    )
    assert secret.encode() not in database_bytes


async def test_inventory_restart_replay_generation_and_revocation_fences(
    tmp_path,
) -> None:
    async def activation_probe(_candidate) -> None:
        return None

    config = _pairing_config(tmp_path)
    first = create_app(
        config,
        start_polling=False,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=activation_probe,
    )
    instance_id = _uuid()
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first),
            base_url="https://nyx.example.internal",
        ) as client:
            paired = await _complete_pairing(client)
            pairing_id = str(paired["pairing_id"])
            generation = int(paired["credential_generation"])
            endpoint = (
                f"/fleet/management/v1/pairings/{pairing_id}/inventory-sync"
            )
            headers = {
                "Authorization": f"Bearer {paired['management_bearer']}"
            }
            inventory = _inventory(
                pairing_id=pairing_id,
                generation=generation,
                instance_id=instance_id,
                sequence=7,
            )
            assert (
                await client.post(endpoint, headers=headers, json=inventory)
            ).status_code == 200

    restarted = create_app(
        config,
        start_polling=False,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=activation_probe,
    )
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted),
            base_url="https://nyx.example.internal",
        ) as client:
            stale = await client.get(
                f"/fleet/api/v1/inventory/{pairing_id}",
                headers=ADMIN,
            )
            assert stale.status_code == 200
            assert stale.json()["device"]["freshness"]["reason"] == (
                "hub_restarted"
            )

            replay = await client.post(endpoint, headers=headers, json=inventory)
            assert replay.status_code == 200
            still_stale = await client.get(
                f"/fleet/api/v1/inventory/{pairing_id}",
                headers=ADMIN,
            )
            assert still_stale.json()["device"]["freshness"]["reason"] == (
                "hub_restarted"
            )

            inventory["inventory_sequence"] = 8
            assert (
                await client.post(endpoint, headers=headers, json=inventory)
            ).status_code == 200
            fresh = await client.get(
                f"/fleet/api/v1/inventory/{pairing_id}",
                headers=ADMIN,
            )
            assert fresh.json()["device"]["freshness"]["state"] == "fresh"

            wrong_generation = copy.deepcopy(inventory)
            wrong_generation["credential_generation"] = generation + 1
            generation_rejected = await client.post(
                endpoint,
                headers=headers,
                json=wrong_generation,
            )
            assert generation_rejected.status_code == 401

            revoked = await client.post(
                f"/fleet/api/v1/pairing/enrollments/{pairing_id}/revoke",
                headers=ADMIN,
                json={"schema_version": 1, "request_id": _uuid()},
            )
            assert revoked.status_code == 200
            inventory["inventory_sequence"] = 9
            post_revoke = await client.post(
                endpoint,
                headers=headers,
                json=inventory,
            )
            assert post_revoke.status_code == 401
            retained = await client.get(
                f"/fleet/api/v1/inventory/{pairing_id}",
                headers=ADMIN,
            )
            assert retained.status_code == 200
            assert retained.json()["device"]["inventory_sequence"] == 8
            assert retained.json()["device"]["freshness"]["reason"] == (
                "enrollment_inactive"
            )


async def test_inventory_routes_are_absent_when_pairing_is_disabled(
    tmp_path,
) -> None:
    from .helpers import fleet_config

    app = create_app(fleet_config(tmp_path), start_polling=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            response = await client.get(
                "/fleet/api/v1/inventory",
                headers=ADMIN,
            )
    assert response.status_code == 404
