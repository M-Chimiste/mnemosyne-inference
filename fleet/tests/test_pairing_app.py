from __future__ import annotations

import base64
import json
import uuid
from dataclasses import replace

import httpx

from mnemosyne_fleet.app import create_app
from mnemosyne_fleet.config import PairingConfig
from mnemosyne_fleet.locator_policy import LocatorPolicy

from .helpers import fleet_config


def _uuid() -> str:
    return str(uuid.uuid4())


def _master_key() -> str:
    return base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def _pairing_config(tmp_path, *, master_key: str | None = None):
    base = fleet_config(tmp_path)
    return replace(
        base,
        pairing=PairingConfig(
            enabled=True,
            public_origin="https://nyx.example.internal",
            metadata_database_path=tmp_path / "private" / "pairing-metadata.db",
            secret_database_path=tmp_path / "private" / "pairing-secrets.db",
            secret_store_id="test-pairing-secrets",
            master_key=master_key or _master_key(),
            activation_timeout_seconds=5,
            https_cidr_allowlist=("10.40.50.0/24",),
            allowed_node_ports=(1240,),
        ),
    )


def _locator_policy() -> LocatorPolicy:
    return LocatorPolicy(
        cidr_allowlists={
            "https": ("10.40.50.0/24",),
            "tailscale": (),
            "trusted_lan_http": (),
        },
        allowed_ports=(1240,),
        resolver=lambda _host, _port: ("10.40.50.60",),
    )


async def test_pairing_http_ceremony_stays_disabled_until_admin_enable(
    tmp_path,
) -> None:
    probes = []

    async def activation_probe(candidate) -> None:
        probes.append(candidate)

    app = create_app(
        _pairing_config(tmp_path),
        start_polling=False,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=activation_probe,
    )
    admin = {"Authorization": "Bearer admin-key"}
    locator = "https://studio.example.internal:1240"

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://nyx.example.internal",
        ) as client:
            invitation_request = {
                "schema_version": 1,
                "request_id": _uuid(),
                "intent": "new",
                "expected": {
                    "platform": "macos",
                    "reporting_node_id": "studio-mac",
                    "locator": locator,
                    "transport": "https",
                    "service_class": "primary",
                },
                "expires_in_seconds": 300,
            }
            unauthorized = await client.post(
                "/fleet/api/v1/pairing/invitations",
                json=invitation_request,
            )
            assert unauthorized.status_code == 401

            invitation_response = await client.post(
                "/fleet/api/v1/pairing/invitations",
                headers=admin,
                json=invitation_request,
            )
            assert invitation_response.status_code == 201
            assert invitation_response.headers["cache-control"] == "no-store"
            invitation = invitation_response.json()
            pairing_secret = invitation["pairing_secret"]
            assert invitation["hub_origin"] == "https://nyx.example.internal"

            claim_request_id = _uuid()
            claim_response = await client.post(
                "/fleet/pairing/v1/claims",
                json={
                    "schema_version": 1,
                    "request_id": claim_request_id,
                    "invitation_id": invitation["invitation_id"],
                    "pairing_secret": pairing_secret,
                    "mac": {
                        "platform": "macos",
                        "service_version": "0.9.0",
                        "display_name": "Studio Mac",
                        "reporting_node_id": "studio-mac",
                    },
                    "locator": locator,
                    "supported_protocol": {"minimum": 1, "maximum": 1},
                },
            )
            assert claim_response.status_code == 200
            claim = claim_response.json()

            status = await client.post(
                f"/fleet/pairing/v1/claims/{claim['claim_id']}/status",
                json={
                    "schema_version": 1,
                    "claim_request_id": claim_request_id,
                },
            )
            assert status.status_code == 200
            assert status.headers["cache-control"] == "no-store"
            assert status.json() == {
                "schema_version": 1,
                "claim_id": claim["claim_id"],
                "invitation_id": claim["invitation_id"],
                "pairing_id": claim["pairing_id"],
                "reporting_node_id": "studio-mac",
                "state": "claimed",
                "expires_at": claim["expires_at"],
            }
            hidden = await client.post(
                f"/fleet/pairing/v1/claims/{claim['claim_id']}/status",
                json={
                    "schema_version": 1,
                    "claim_request_id": _uuid(),
                },
            )
            assert hidden.status_code == 410
            assert hidden.json()["detail"]["code"] == (
                "pairing_transaction_terminal"
            )
            assert claim["locator_accepted"] is True

            pending_response = await client.get(
                "/fleet/api/v1/pairing/claims",
                headers=admin,
            )
            assert pending_response.status_code == 200
            pending_text = pending_response.text
            assert claim["claim_id"] in pending_text
            assert pairing_secret not in pending_text
            assert locator not in pending_text

            approval_response = await client.post(
                f"/fleet/api/v1/pairing/claims/{claim['claim_id']}/approve",
                headers=admin,
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "locator": locator,
                    "service_class": "primary",
                    "hub_enabled": False,
                },
            )
            assert approval_response.status_code == 200
            assert approval_response.json()["state"] == "pending"

            wrong_provision = await client.post(
                f"/fleet/pairing/v1/claims/{claim['claim_id']}/provision",
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "pairing_secret": "wrong-credential-value-that-is-long-enough",
                },
            )
            assert wrong_provision.status_code == 401
            assert pairing_secret not in wrong_provision.text

            provision_response = await client.post(
                f"/fleet/pairing/v1/claims/{claim['claim_id']}/provision",
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "pairing_secret": pairing_secret,
                },
            )
            assert provision_response.status_code == 200
            provisioned = provision_response.json()
            credentials = provisioned["credentials"]
            assert len(set(credentials.values())) == 3

            activation_payload = {
                "schema_version": 1,
                "request_id": _uuid(),
                "credential_generation": provisioned["credential_generation"],
                "reporting_node_id": "studio-mac",
                "service_instance_id": "service-instance-1",
            }
            wrong_activation = await client.post(
                f"/fleet/management/v1/pairings/{claim['pairing_id']}/activation-ack",
                headers={"Authorization": "Bearer wrong-management-value"},
                json=activation_payload,
            )
            assert wrong_activation.status_code == 401
            assert probes == []

            activation_response = await client.post(
                f"/fleet/management/v1/pairings/{claim['pairing_id']}/activation-ack",
                headers={
                    "Authorization": (
                        f"Bearer {credentials['management_bearer']}"
                    )
                },
                json=activation_payload,
            )
            assert activation_response.status_code == 200
            assert activation_response.json()["activation_complete"] is True
            assert activation_response.json()["state"] == "disabled"
            assert len(probes) == 1
            assert app.state.registry.enrollment(claim["pairing_id"]) is None

            enable_response = await client.put(
                f"/fleet/api/v1/pairing/enrollments/{claim['pairing_id']}/enabled",
                headers=admin,
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "enabled": True,
                },
            )
            assert enable_response.status_code == 200
            assert enable_response.json()["state"] == "active"
            paired_node = app.state.registry.enrollment(claim["pairing_id"])
            assert paired_node is not None
            assert paired_node.reporting_node_id == "studio-mac"

            enrollments = await client.get(
                "/fleet/api/v1/pairing/enrollments",
                headers=admin,
            )
            status = await client.get("/fleet/api/status", headers=admin)
            redacted = enrollments.text + status.text
            assert locator not in redacted
            assert pairing_secret not in redacted
            for credential in credentials.values():
                assert credential not in redacted

            management_payload = {
                "schema_version": 1,
                "request_id": _uuid(),
                "pairing_id": claim["pairing_id"],
                "reporting_node_id": "studio-mac",
                "credential_generation": provisioned["credential_generation"],
            }
            management_headers = {
                "Authorization": f"Bearer {credentials['management_bearer']}"
            }
            for path_pairing_id, headers, payload in (
                (
                    claim["pairing_id"],
                    {"Authorization": "Bearer wrong-management-value"},
                    management_payload,
                ),
                (
                    _uuid(),
                    management_headers,
                    management_payload,
                ),
                (
                    claim["pairing_id"],
                    management_headers,
                    {**management_payload, "reporting_node_id": "wrong-node"},
                ),
                (
                    claim["pairing_id"],
                    management_headers,
                    {
                        **management_payload,
                        "credential_generation": (
                            provisioned["credential_generation"] + 1
                        ),
                    },
                ),
            ):
                rejected = await client.post(
                    (
                        "/fleet/management/v1/pairings/"
                        f"{path_pairing_id}/self-disable"
                    ),
                    headers=headers,
                    json=payload,
                )
                assert rejected.status_code == 401
                assert rejected.json()["detail"]["code"] == (
                    "pairing_management_authentication_rejected"
                )

            self_disabled = await client.post(
                (
                    "/fleet/management/v1/pairings/"
                    f"{claim['pairing_id']}/self-disable"
                ),
                headers=management_headers,
                json=management_payload,
            )
            assert self_disabled.status_code == 200
            assert self_disabled.json()["state"] == "disabled"
            assert app.state.registry.enrollment(claim["pairing_id"]) is None
            replay_disabled = await client.post(
                (
                    "/fleet/management/v1/pairings/"
                    f"{claim['pairing_id']}/self-disable"
                ),
                headers=management_headers,
                json=management_payload,
            )
            assert replay_disabled.json() == self_disabled.json()

            reenable_response = await client.put(
                f"/fleet/api/v1/pairing/enrollments/{claim['pairing_id']}/enabled",
                headers=admin,
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "enabled": True,
                },
            )
            assert reenable_response.status_code == 200
            revoke_payload = {**management_payload, "request_id": _uuid()}
            self_revoked = await client.post(
                (
                    "/fleet/management/v1/pairings/"
                    f"{claim['pairing_id']}/self-revoke"
                ),
                headers=management_headers,
                json=revoke_payload,
            )
            assert self_revoked.status_code == 200
            assert self_revoked.json()["state"] == "revoked"
            assert app.state.registry.enrollment(claim["pairing_id"]) is None
            replay_revoked = await client.post(
                (
                    "/fleet/management/v1/pairings/"
                    f"{claim['pairing_id']}/self-revoke"
                ),
                headers=management_headers,
                json=revoke_payload,
            )
            assert replay_revoked.json() == self_revoked.json()
            already_revoked = await client.post(
                (
                    "/fleet/management/v1/pairings/"
                    f"{claim['pairing_id']}/self-revoke"
                ),
                headers=management_headers,
                json={**revoke_payload, "request_id": _uuid()},
            )
            assert already_revoked.status_code == 401
            wrong_replay = await client.post(
                (
                    "/fleet/management/v1/pairings/"
                    f"{claim['pairing_id']}/self-revoke"
                ),
                headers={"Authorization": "Bearer wrong-management-value"},
                json=revoke_payload,
            )
            assert wrong_replay.status_code == 401
            pairing_metadata = (
                tmp_path / "private" / "pairing-metadata.db"
            ).read_bytes()
            for credential in credentials.values():
                assert credential.encode() not in pairing_metadata

            revoke_response = await client.post(
                f"/fleet/api/v1/pairing/enrollments/{claim['pairing_id']}/revoke",
                headers=admin,
                json={"schema_version": 1, "request_id": _uuid()},
            )
            assert revoke_response.status_code == 200
            assert revoke_response.json()["state"] == "revoked"
            assert app.state.registry.enrollment(claim["pairing_id"]) is None


async def test_presence_pairing_requests_pin_then_explicitly_enables(
    tmp_path,
) -> None:
    probes = []

    async def activation_probe(candidate) -> None:
        probes.append(candidate)

    app = create_app(
        _pairing_config(tmp_path),
        start_polling=False,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=activation_probe,
    )
    admin = {"Authorization": "Bearer admin-key"}
    locator = "https://studio.example.internal:1240"
    mac = {
        "platform": "macos",
        "service_version": "0.9.0",
        "display_name": "Studio Mac",
        "reporting_node_id": "studio-mac",
    }

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://nyx.example.internal",
        ) as client:
            requested = await client.post(
                "/fleet/pairing/v1/requests",
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "mac": mac,
                    "locator": locator,
                    "transport": "https",
                    "supported_protocol": {"minimum": 1, "maximum": 1},
                },
            )
            assert requested.status_code == 201
            assert requested.headers["cache-control"] == "no-store"
            invitation = requested.json()
            assert invitation["hub_origin"] == "https://nyx.example.internal"
            assert len(invitation["presence_pin"]) == 6
            assert invitation["presence_pin"].isdigit()
            assert locator not in requested.text

            claim_response = await client.post(
                "/fleet/pairing/v1/claims",
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "invitation_id": invitation["invitation_id"],
                    "pairing_secret": invitation["pairing_secret"],
                    "mac": mac,
                    "locator": locator,
                    "supported_protocol": {"minimum": 1, "maximum": 1},
                },
            )
            assert claim_response.status_code == 200
            claim = claim_response.json()

            wrong_pin = (
                "000000"
                if invitation["presence_pin"] != "000000"
                else "999999"
            )
            rejected = await client.post(
                (
                    "/fleet/api/v1/pairing/claims/"
                    f"{claim['claim_id']}/approve-presence"
                ),
                headers=admin,
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "presence_pin": wrong_pin,
                    "service_class": "primary",
                    "hub_enabled": False,
                },
            )
            assert rejected.status_code == 401
            assert rejected.json()["detail"]["code"] == (
                "pairing_presence_pin_rejected"
            )

            approved = await client.post(
                (
                    "/fleet/api/v1/pairing/claims/"
                    f"{claim['claim_id']}/approve-presence"
                ),
                headers=admin,
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "presence_pin": invitation["presence_pin"],
                    "service_class": "primary",
                    "hub_enabled": False,
                },
            )
            assert approved.status_code == 200
            assert approved.json()["state"] == "pending"
            assert approved.json()["hub_enabled"] is False

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
            activated = await client.post(
                (
                    "/fleet/management/v1/pairings/"
                    f"{claim['pairing_id']}/activation-ack"
                ),
                headers={
                    "Authorization": (
                        "Bearer " + provisioned["credentials"]["management_bearer"]
                    )
                },
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "credential_generation": provisioned[
                        "credential_generation"
                    ],
                    "reporting_node_id": "studio-mac",
                    "service_instance_id": "service-instance-1",
                },
            )
            assert activated.status_code == 200
            assert activated.json()["state"] == "disabled"
            assert len(probes) == 1
            assert app.state.registry.enrollment(claim["pairing_id"]) is None

            enabled = await client.put(
                (
                    "/fleet/api/v1/pairing/enrollments/"
                    f"{claim['pairing_id']}/enabled"
                ),
                headers=admin,
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "enabled": True,
                },
            )
            assert enabled.status_code == 200
            assert enabled.json()["state"] == "active"
            assert app.state.registry.enrollment(claim["pairing_id"]) is not None


async def test_pairing_routes_are_absent_when_disabled(tmp_path) -> None:
    app = create_app(fleet_config(tmp_path), start_polling=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            response = await client.post(
                "/fleet/pairing/v1/claims",
                json={"schema_version": 1},
            )
    assert response.status_code == 404


async def test_invalid_pairing_body_never_reflects_secret(tmp_path) -> None:
    app = create_app(
        _pairing_config(tmp_path),
        start_polling=False,
        pairing_locator_policy=_locator_policy(),
        pairing_activation_probe=lambda _candidate: None,  # never reached
    )
    secret = "private-value-that-must-not-appear-anywhere"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://nyx.example.internal",
        ) as client:
            response = await client.post(
                "/fleet/pairing/v1/claims",
                json={
                    "schema_version": 1,
                    "pairing_secret": secret,
                    "unexpected": secret,
                },
            )
    assert response.status_code == 400
    assert secret not in response.text
    assert json.loads(response.text)["detail"]["code"] == "pairing_invalid_request"


async def test_pairing_store_key_failure_keeps_static_fleet_available(tmp_path) -> None:
    first = create_app(
        _pairing_config(tmp_path),
        start_polling=False,
        pairing_locator_policy=_locator_policy(),
    )
    async with first.router.lifespan_context(first):
        assert first.state.pairing_runtime["available"] is True

    different_key = base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode().rstrip("=")
    restarted = create_app(
        _pairing_config(tmp_path, master_key=different_key),
        start_polling=False,
        pairing_locator_policy=_locator_policy(),
    )
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted),
            base_url="https://nyx.example.internal",
        ) as client:
            health = await client.get("/health")
            models = await client.get(
                "/v1/models",
                headers={"Authorization": "Bearer client-key"},
            )
            pairing = await client.post(
                "/fleet/api/v1/pairing/invitations",
                headers={"Authorization": "Bearer admin-key"},
                json={
                    "schema_version": 1,
                    "request_id": _uuid(),
                    "intent": "new",
                    "expected": {
                        "platform": "macos",
                        "reporting_node_id": "studio-mac",
                        "locator": "https://studio.example.internal:1240",
                        "transport": "https",
                        "service_class": "primary",
                    },
                    "expires_in_seconds": 300,
                },
            )

    assert health.status_code == 200
    assert models.status_code == 200
    assert pairing.status_code == 503
    assert pairing.json()["detail"]["code"] == "pairing_unavailable"
