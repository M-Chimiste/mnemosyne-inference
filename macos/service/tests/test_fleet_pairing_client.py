from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

import httpx
import pytest

from mnemosyne_macos.fleet_pairing import (
    FleetPairingError,
    FleetPairingStore,
    PairingCredentials,
    PairingState,
)
from mnemosyne_macos.fleet_pairing_client import (
    MAX_PAIRING_RESPONSE_BYTES,
    FleetPairingClient,
    PairingClientError,
    PairingClientErrorCode,
    PairingClientPhase,
    PairingInvitation,
    PairingManagementState,
    _ClaimResponse,
    _ProvisionResponse,
    _presence_pin,
)


INVITATION_ID = "11111111-1111-4111-8111-111111111111"
CLAIM_ID = "22222222-2222-4222-8222-222222222222"
PAIRING_ID = "33333333-3333-4333-8333-333333333333"
PAIRING_SECRET = "invitation-secret-that-is-long-enough-1234567890"
HUB_ORIGIN = "https://nyx.example.test"
LOCATOR = "https://studio.example.test:1240"
REPORTING_NODE_ID = "studio-mac"
SNAPSHOT_BEARER = "snapshot-generation-one"
DISPATCH_BEARER = "dispatch-generation-one"
MANAGEMENT_BEARER = "management-generation-one"
SECOND_INVITATION_ID = "99999999-1111-4111-8111-111111111111"
SECOND_CLAIM_ID = "99999999-2222-4222-8222-222222222222"
SECOND_PAIRING_ID = "99999999-3333-4333-8333-333333333333"
SECOND_SNAPSHOT_BEARER = "snapshot-generation-two"
SECOND_DISPATCH_BEARER = "dispatch-generation-two"
SECOND_MANAGEMENT_BEARER = "management-generation-two"


def _invitation(**changes: str) -> PairingInvitation:
    values = {
        "invitation_id": INVITATION_ID,
        "pairing_secret": PAIRING_SECRET,
        "hub_origin": HUB_ORIGIN,
        "locator": LOCATOR,
    }
    values.update(changes)
    return PairingInvitation(**values)


def _second_invitation() -> PairingInvitation:
    return PairingInvitation(
        invitation_id=SECOND_INVITATION_ID,
        pairing_secret="second-invitation-secret-that-is-long-enough-123456",
        hub_origin=HUB_ORIGIN,
        locator="https://second-studio.example.test:1240",
    )


def _claim(request_id: str) -> dict[str, Any]:
    del request_id
    return {
        "schema_version": 1,
        "claim_id": CLAIM_ID,
        "invitation_id": INVITATION_ID,
        "pairing_id": PAIRING_ID,
        "display_name": REPORTING_NODE_ID,
        "reporting_node_id": REPORTING_NODE_ID,
        "service_version": "0.9.0",
        "platform": "macos",
        "protocol_version": 1,
        "state": "claimed",
        "claimed_at": 100.0,
        "expires_at": 4_000_000_000.0,
        "locator_accepted": True,
    }


def _provision() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "claim_id": CLAIM_ID,
        "pairing_id": PAIRING_ID,
        "reporting_node_id": REPORTING_NODE_ID,
        "credential_generation": 1,
        "credentials": {
            "snapshot_bearer": SNAPSHOT_BEARER,
            "dispatch_bearer": DISPATCH_BEARER,
            "management_bearer": MANAGEMENT_BEARER,
        },
        "state": "provisioning",
    }


def _activation() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "pairing_id": PAIRING_ID,
        "reporting_node_id": REPORTING_NODE_ID,
        "display_name": REPORTING_NODE_ID,
        "platform": "macos",
        "service_version": "0.9.0",
        "protocol_version": 1,
        "service_class": "primary",
        "state": "disabled",
        "hub_enabled": False,
        "credential_generation": 1,
        "created_at": 100.0,
        "updated_at": 101.0,
        "revoked_at": None,
        "failure_code": None,
        "activation_complete": True,
    }


def _management(
    state: str,
    *,
    pairing_id: str = PAIRING_ID,
    reporting_node_id: str = REPORTING_NODE_ID,
    credential_generation: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "pairing_id": pairing_id,
        "reporting_node_id": reporting_node_id,
        "display_name": REPORTING_NODE_ID,
        "platform": "macos",
        "service_version": "0.9.0",
        "protocol_version": 1,
        "service_class": "primary",
        "state": state,
        "hub_enabled": False,
        "credential_generation": credential_generation,
        "created_at": 100.0,
        "updated_at": 101.0,
        "revoked_at": 101.0 if state == "revoked" else None,
        "failure_code": None,
    }


def _complete_ceremony_response(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    if request.url.path.endswith("/self-revoke"):
        return httpx.Response(
            200,
            json=_management(
                "revoked",
                pairing_id=payload["pairing_id"],
                reporting_node_id=payload["reporting_node_id"],
                credential_generation=payload["credential_generation"],
            ),
        )
    if request.url.path == "/fleet/pairing/v1/claims":
        if payload["invitation_id"] == INVITATION_ID:
            return httpx.Response(200, json=_claim(payload["request_id"]))
        response = _claim(payload["request_id"])
        response.update(
            invitation_id=SECOND_INVITATION_ID,
            claim_id=SECOND_CLAIM_ID,
            pairing_id=SECOND_PAIRING_ID,
        )
        return httpx.Response(200, json=response)
    if request.url.path.endswith("/provision"):
        if SECOND_CLAIM_ID not in request.url.path:
            return httpx.Response(200, json=_provision())
        response = _provision()
        response.update(
            claim_id=SECOND_CLAIM_ID,
            pairing_id=SECOND_PAIRING_ID,
            credential_generation=2,
            credentials={
                "snapshot_bearer": SECOND_SNAPSHOT_BEARER,
                "dispatch_bearer": SECOND_DISPATCH_BEARER,
                "management_bearer": SECOND_MANAGEMENT_BEARER,
            },
        )
        return httpx.Response(200, json=response)
    if SECOND_PAIRING_ID not in request.url.path:
        return httpx.Response(200, json=_activation())
    response = _activation()
    response.update(
        pairing_id=SECOND_PAIRING_ID,
        credential_generation=2,
    )
    return httpx.Response(200, json=response)


async def _activate_local_pairing(
    pairing: FleetPairingStore,
    client: FleetPairingClient,
) -> None:
    workflow, created = await asyncio.to_thread(
        client._journal.prepare,
        _invitation(),
        reporting_node_id=REPORTING_NODE_ID,
        allow_create=True,
    )
    assert created is True
    await pairing.begin_attempt(
        hub_origin=HUB_ORIGIN,
        node_url=LOCATOR,
        attempt_id=workflow.attempt_id,
    )
    await asyncio.to_thread(
        client._journal.record_claim,
        _ClaimResponse.model_validate(_claim(workflow.claim_request_id)),
    )
    await asyncio.to_thread(
        client._journal.record_staging,
        _ProvisionResponse.model_validate(_provision()),
    )
    await pairing.record_assignment(
        pairing_id=PAIRING_ID,
        node_id=REPORTING_NODE_ID,
        credential_epoch=1,
    )
    await pairing.activate_credentials(
        PairingCredentials(
            snapshot_key=SNAPSHOT_BEARER,
            dispatch_key=DISPATCH_BEARER,
            management_key=MANAGEMENT_BEARER,
        )
    )
    await asyncio.to_thread(client._journal.record_activation_pending)
    await pairing.mark_paired()
    await asyncio.to_thread(client._journal.record_complete)


async def _stores(
    tmp_path: Path,
    handler,
    *,
    on_pairing_authority_changed=None,
    on_self_revoke_pending=None,
    on_self_revoke_aborted=None,
    on_completed_revoke_reset=None,
) -> tuple[FleetPairingStore, FleetPairingClient, Path]:
    database = tmp_path / "state" / "mnemosyne.db"
    environment = tmp_path / "private" / ".env"
    pairing = FleetPairingStore(database, environment, process_environment={})
    await pairing.initialize()
    client = FleetPairingClient(
        database,
        pairing_store=pairing,
        reporting_node_id=REPORTING_NODE_ID,
        service_version="0.9.0",
        service_instance_id="service-instance-one",
        transport=httpx.MockTransport(handler),
        on_pairing_authority_changed=on_pairing_authority_changed,
        on_self_revoke_pending=on_self_revoke_pending,
        on_self_revoke_aborted=on_self_revoke_aborted,
        on_completed_revoke_reset=on_completed_revoke_reset,
    )
    await client.initialize()
    return pairing, client, environment


@pytest.mark.asyncio
async def test_presence_request_returns_hidden_invitation_and_display_pin(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/fleet/pairing/v1/requests":
            return httpx.Response(
                201,
                json={
                    "schema_version": 1,
                    "invitation_id": INVITATION_ID,
                    "pairing_secret": PAIRING_SECRET,
                    "presence_pin": _presence_pin(PAIRING_SECRET),
                    "hub_origin": HUB_ORIGIN,
                    "expires_at": 4_000_000_000.0,
                    "state": "issued",
                },
            )
        if request.url.path == "/fleet/pairing/v1/claims":
            return httpx.Response(200, json=_claim(payload["request_id"]))
        return httpx.Response(
            410,
            json={"detail": {"code": "pairing_transaction_terminal"}},
        )

    pairing, client, _ = await _stores(tmp_path, handler)
    try:
        issued = await client.request_presence_invitation(
            request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            hub_origin=HUB_ORIGIN,
            locator=LOCATOR,
            transport="https",
        )
        assert issued.presence_pin == _presence_pin(PAIRING_SECRET)
        assert issued.invitation == _invitation()
        assert PAIRING_SECRET not in repr(issued)
        assert issued.presence_pin not in repr(issued)
        assert await client.status() is None
        assert (await pairing.status()).state == PairingState.UNPAIRED

        with pytest.raises(PairingClientError) as pending:
            await client.begin(issued.invitation)
        assert pending.value.code == PairingClientErrorCode.APPROVAL_PENDING
        assert requests[0] == (
            "/fleet/pairing/v1/requests",
            {
                "schema_version": 1,
                "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "mac": {
                    "platform": "macos",
                    "service_version": "0.9.0",
                    "display_name": REPORTING_NODE_ID,
                    "reporting_node_id": REPORTING_NODE_ID,
                },
                "locator": LOCATOR,
                "transport": "https",
                "supported_protocol": {"minimum": 1, "maximum": 1},
            },
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_claim_provision_and_activation_are_durable_and_secret_free(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, dict[str, Any], str | None]] = []
    provision_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provision_calls
        payload = json.loads(request.content)
        requests.append(
            (request.url.path, payload, request.headers.get("authorization"))
        )
        if request.url.path == "/fleet/pairing/v1/claims":
            return httpx.Response(200, json=_claim(payload["request_id"]))
        if request.url.path.endswith("/provision"):
            provision_calls += 1
            if provision_calls == 1:
                return httpx.Response(
                    410,
                    json={"detail": {"code": "pairing_transaction_terminal"}},
                )
            return httpx.Response(200, json=_provision())
        assert request.url.path.endswith("/activation-ack")
        return httpx.Response(200, json=_activation())

    pairing, client, environment = await _stores(tmp_path, handler)
    try:
        with pytest.raises(PairingClientError) as pending:
            await client.begin(_invitation())
        assert pending.value.code == PairingClientErrorCode.APPROVAL_PENDING
        awaiting = await client.status()
        assert awaiting is not None
        assert awaiting.phase == PairingClientPhase.AWAITING_APPROVAL

        complete = await client.resume(_invitation())
        assert complete.phase == PairingClientPhase.COMPLETE
        assert (await pairing.status()).state == PairingState.PAIRED
        assert environment.read_text(encoding="utf-8").splitlines() == [
            f"FLEET_API_KEY={SNAPSHOT_BEARER}",
            f"FLEET_INFERENCE_API_KEY={DISPATCH_BEARER}",
            f"FLEET_MANAGEMENT_API_KEY={MANAGEMENT_BEARER}",
        ]

        claim_request = requests[0][1]
        provision_requests = [
            request for path, request, _auth in requests if path.endswith("/provision")
        ]
        activation_request = requests[-1]
        assert claim_request["request_id"] == complete.claim_request_id
        assert {
            request["request_id"] for request in provision_requests
        } == {complete.provision_request_id}
        assert activation_request[1]["request_id"] == complete.activation_request_id
        assert activation_request[2] == f"Bearer {MANAGEMENT_BEARER}"

        serialized = json.dumps(complete.public_payload(), sort_keys=True)
        assert PAIRING_SECRET not in serialized
        assert LOCATOR not in serialized
        assert HUB_ORIGIN not in serialized
        assert SNAPSHOT_BEARER not in serialized
        assert DISPATCH_BEARER not in serialized
        assert MANAGEMENT_BEARER not in serialized

        database_bytes = (tmp_path / "state" / "mnemosyne.db").read_bytes()
        wal_path = tmp_path / "state" / "mnemosyne.db-wal"
        if wal_path.exists():
            database_bytes += wal_path.read_bytes()
        assert PAIRING_SECRET.encode() not in database_bytes
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_conclusively_rejected_unclaimed_attempt_can_be_discarded(
    tmp_path: Path,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(request.url.path)
        if (
            request.url.path == "/fleet/pairing/v1/claims"
            and payload["invitation_id"] == INVITATION_ID
        ):
            return httpx.Response(
                401,
                json={"detail": {"code": "pairing_claim_rejected"}},
            )
        return _complete_ceremony_response(request)

    pairing, client, environment = await _stores(tmp_path, handler)
    try:
        with pytest.raises(PairingClientError) as rejected:
            await client.begin(_invitation())
        assert rejected.value.code == PairingClientErrorCode.CLAIM_REJECTED
        failed = await client.status()
        assert failed is not None
        assert failed.phase == PairingClientPhase.CLAIMING
        assert failed.claim_id is None
        assert failed.pairing_id is None
        assert failed.last_error_code == PairingClientErrorCode.CLAIM_REJECTED
        assert (await pairing.status()).state == PairingState.PENDING

        calls_before_mismatch = len(requests)
        with pytest.raises(PairingClientError) as mismatch:
            await client.begin(_second_invitation())
        assert mismatch.value.code == PairingClientErrorCode.PAYLOAD_MISMATCH
        assert len(requests) == calls_before_mismatch

        await client.discard_rejected_unclaimed_attempt()
        assert await client.status() is None
        reset = await pairing.status()
        assert reset.state == PairingState.UNPAIRED
        assert reset.attempt_id is None
        assert reset.pairing_id is None
        assert not environment.exists()

        completed = await client.begin(_second_invitation())
        assert completed.phase == PairingClientPhase.COMPLETE
        assert (await pairing.status()).state == PairingState.PAIRED
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_ambiguous_unclaimed_attempt_cannot_be_discarded(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ambiguous", request=request)

    pairing, client, _environment = await _stores(tmp_path, handler)
    try:
        with pytest.raises(PairingClientError) as unavailable:
            await client.begin(_invitation())
        assert unavailable.value.code == PairingClientErrorCode.HUB_UNAVAILABLE

        with pytest.raises(PairingClientError) as refused:
            await client.discard_rejected_unclaimed_attempt()
        assert refused.value.code == PairingClientErrorCode.STATE_CONFLICT
        assert await client.status() is not None
        assert (await pairing.status()).state == PairingState.PENDING
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_ambiguous_transports_replay_exact_request_ids_and_staged_bundle(
    tmp_path: Path,
) -> None:
    request_ids: dict[str, list[str]] = {
        "claim": [],
        "provision": [],
        "activation": [],
    }
    calls = {"claim": 0, "provision": 0, "activation": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/fleet/pairing/v1/claims":
            operation = "claim"
        elif request.url.path.endswith("/provision"):
            operation = "provision"
        else:
            operation = "activation"
        calls[operation] += 1
        request_ids[operation].append(payload["request_id"])
        if calls[operation] == 1:
            raise httpx.ReadTimeout("ambiguous", request=request)
        if operation == "claim":
            return httpx.Response(200, json=_claim(payload["request_id"]))
        if operation == "provision":
            return httpx.Response(200, json=_provision())
        return httpx.Response(200, json=_activation())

    pairing, first, _environment = await _stores(tmp_path, handler)
    try:
        with pytest.raises(PairingClientError) as claim_timeout:
            await first.begin(_invitation())
        assert claim_timeout.value.code == PairingClientErrorCode.HUB_UNAVAILABLE

        with pytest.raises(PairingClientError) as provision_timeout:
            await first.resume(_invitation())
        assert provision_timeout.value.code == PairingClientErrorCode.HUB_UNAVAILABLE

        with pytest.raises(PairingClientError) as activation_timeout:
            await first.resume(_invitation())
        assert activation_timeout.value.code == PairingClientErrorCode.HUB_UNAVAILABLE
        staged = await first.status()
        assert staged is not None
        assert staged.phase == PairingClientPhase.ACTIVATION_PENDING
        await first.close()

        # A process restart reloads only the secret-free request journal and
        # manager-owned private credentials, then retries activation directly.
        restarted = FleetPairingClient(
            tmp_path / "state" / "mnemosyne.db",
            pairing_store=pairing,
            reporting_node_id=REPORTING_NODE_ID,
            service_version="0.9.0",
            service_instance_id="service-instance-one",
            transport=httpx.MockTransport(handler),
        )
        await restarted.initialize()
        try:
            complete = await restarted.resume(_invitation())
            assert complete.phase == PairingClientPhase.COMPLETE
        finally:
            await restarted.close()

        assert all(len(set(values)) == 1 for values in request_ids.values())
        assert calls == {"claim": 2, "provision": 2, "activation": 2}
    finally:
        await first.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_management_disable_revoke_and_local_replay_are_closed(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, dict[str, Any], str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(
            (request.url.path, payload, request.headers.get("authorization"))
        )
        state = "revoked" if request.url.path.endswith("/self-revoke") else "disabled"
        return httpx.Response(200, json=_management(state))

    pairing, client, environment = await _stores(tmp_path, handler)
    await _activate_local_pairing(pairing, client)
    try:
        first_disable_id = "55555555-5555-4555-8555-555555555555"
        disabled = await client.self_disable_enrollment(
            request_id=first_disable_id
        )
        assert disabled.state == PairingManagementState.DISABLED
        assert (await pairing.status()).state == PairingState.PAIRED

        # Local state deliberately remains paired, so a later app-only
        # uninstall attempt can safely disable an already-disabled Hub record.
        disabled_again = await client.self_disable_enrollment(
            request_id="66666666-6666-4666-8666-666666666666"
        )
        assert disabled_again.state == PairingManagementState.DISABLED

        revoke_request_id = "77777777-7777-4777-8777-777777777777"
        revoked = await client.self_revoke_enrollment(
            request_id=revoke_request_id
        )
        assert revoked.state == PairingManagementState.REVOKED
        tombstone = await pairing.status()
        assert tombstone.state == PairingState.REVOKED
        assert tombstone.pairing_id == PAIRING_ID
        assert tombstone.node_id == REPORTING_NODE_ID
        assert tombstone.credential_epoch == 1
        assert tombstone.credentials_owned is False
        assert all(
            f"{key}=" not in environment.read_text(encoding="utf-8")
            for key in (
                "FLEET_API_KEY",
                "FLEET_INFERENCE_API_KEY",
                "FLEET_MANAGEMENT_API_KEY",
            )
        )
        call_count = len(requests)
        already_revoked = await client.self_revoke_enrollment(
            request_id=revoke_request_id
        )
        assert already_revoked.state == PairingManagementState.REVOKED
        assert len(requests) == call_count
        with pytest.raises(PairingClientError) as mismatched:
            await client.self_revoke_enrollment(
                request_id="88888888-8888-4888-8888-888888888888"
            )
        assert mismatched.value.code == PairingClientErrorCode.PAYLOAD_MISMATCH
        assert len(requests) == call_count

        for path, payload, authorization in requests:
            assert path.startswith(
                f"/fleet/management/v1/pairings/{PAIRING_ID}/self-"
            )
            assert payload["pairing_id"] == PAIRING_ID
            assert payload["reporting_node_id"] == REPORTING_NODE_ID
            assert payload["credential_generation"] == 1
            assert authorization == f"Bearer {MANAGEMENT_BEARER}"
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_hub_success_local_revoke_failure_requires_exact_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        request_ids.append(payload["request_id"])
        return httpx.Response(200, json=_management("revoked"))

    denied: list[bool] = []
    pairing, client, environment = await _stores(
        tmp_path,
        handler,
        on_self_revoke_pending=lambda: denied.append(True),
    )
    await _activate_local_pairing(pairing, client)
    original_mark_revoked = pairing.mark_revoked
    local_attempts = 0

    async def fail_once():
        nonlocal local_attempts
        local_attempts += 1
        if local_attempts == 1:
            raise FleetPairingError("injected local persistence failure")
        return await original_mark_revoked()

    monkeypatch.setattr(pairing, "mark_revoked", fail_once)
    request_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    try:
        with pytest.raises(PairingClientError) as uncertain:
            await client.self_revoke_enrollment(request_id=request_id)
        assert uncertain.value.code == (
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN
        )
        assert uncertain.value.retryable is True
        assert (await pairing.status()).state == PairingState.PAIRED
        assert denied == [True]
        assert await client.self_revoke_authority_denied() is True

        await client.close()
        monkeypatch.setattr(pairing, "mark_revoked", original_mark_revoked)
        restarted = FleetPairingClient(
            tmp_path / "state" / "mnemosyne.db",
            pairing_store=pairing,
            reporting_node_id=REPORTING_NODE_ID,
            service_version="0.9.0",
            service_instance_id="service-instance-two",
            transport=httpx.MockTransport(handler),
        )
        await restarted.initialize()
        try:
            assert await restarted.self_revoke_authority_denied() is True
            recovered = await restarted.self_revoke_enrollment(
                request_id=request_id
            )
            assert recovered.state == PairingManagementState.REVOKED
            tombstone = await pairing.status()
            assert tombstone.state == PairingState.REVOKED
            assert tombstone.pairing_id == PAIRING_ID
            assert tombstone.credentials_owned is False
            assert MANAGEMENT_BEARER not in environment.read_text(
                encoding="utf-8"
            )
            assert request_ids == [request_id]
        finally:
            await restarted.close()
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_revoked_credential_retirement_failure_retries_without_hub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        request_ids.append(payload["request_id"])
        return httpx.Response(200, json=_management("revoked"))

    pairing, client, environment = await _stores(tmp_path, handler)
    await _activate_local_pairing(pairing, client)
    original_retire = pairing.retire_revoked_credentials
    attempts = 0

    async def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FleetPairingError("injected credential retirement failure")
        return await original_retire()

    monkeypatch.setattr(pairing, "retire_revoked_credentials", fail_once)
    request_id = "abababab-abab-4bab-8bab-abababababab"
    try:
        with pytest.raises(PairingClientError) as uncertain:
            await client.self_revoke_enrollment(request_id=request_id)
        assert uncertain.value.code == (
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN
        )
        pending_cleanup = await pairing.status()
        assert pending_cleanup.state == PairingState.REVOKED
        assert pending_cleanup.credentials_owned is True
        assert pending_cleanup.pairing_id == PAIRING_ID

        recovered = await client.self_revoke_enrollment(request_id=request_id)
        assert recovered.state == PairingManagementState.REVOKED
        retired = await pairing.status()
        assert retired.state == PairingState.REVOKED
        assert retired.pairing_id == PAIRING_ID
        assert retired.credentials_owned is False
        assert MANAGEMENT_BEARER not in environment.read_text(encoding="utf-8")
        assert request_ids == [request_id]
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_incomplete_revoke_cannot_be_replaced_after_local_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return _complete_ceremony_response(request)

    pairing, client, _environment = await _stores(tmp_path, handler)
    await client.begin(_invitation())
    original_retire = pairing.retire_revoked_credentials

    async def fail_retirement():
        raise FleetPairingError("injected incomplete credential retirement")

    monkeypatch.setattr(
        pairing,
        "retire_revoked_credentials",
        fail_retirement,
    )
    try:
        with pytest.raises(PairingClientError) as uncertain:
            await client.self_revoke_enrollment(
                request_id="12121212-1212-4212-8212-121212121212"
            )
        assert uncertain.value.code == (
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN
        )
        assert (await pairing.status()).state == PairingState.REVOKED
        monkeypatch.setattr(
            pairing,
            "retire_revoked_credentials",
            original_retire,
        )
        assert (await pairing.clear_pairing()).state == PairingState.UNPAIRED
        calls_before_new_begin = len(requests)

        with pytest.raises(PairingClientError) as refused:
            await client.begin(_second_invitation())
        assert refused.value.code == PairingClientErrorCode.STATE_CONFLICT
        assert await client.self_revoke_authority_denied() is True
        assert len(requests) == calls_before_new_begin
        assert (await pairing.status()).state == PairingState.UNPAIRED
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_completed_revoke_directly_allows_new_pairing_and_restart(
    tmp_path: Path,
) -> None:
    requests: list[str] = []
    denied: list[bool] = []
    reset: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return _complete_ceremony_response(request)

    pairing, client, environment = await _stores(
        tmp_path,
        handler,
        on_self_revoke_pending=lambda: denied.append(True),
        on_completed_revoke_reset=lambda: reset.append(True),
    )
    await client.begin(_invitation())
    old_revoke_request_id = "13131313-1313-4313-8313-131313131313"
    try:
        revoked = await client.self_revoke_enrollment(
            request_id=old_revoke_request_id
        )
        assert revoked.state == PairingManagementState.REVOKED
        assert denied == [True]
        assert await client.self_revoke_authority_denied() is True
        tombstone = await pairing.status()
        assert tombstone.state == PairingState.REVOKED
        assert tombstone.pairing_id == PAIRING_ID
        assert tombstone.credentials_owned is False

        completed = await client.begin(_second_invitation())
        assert completed.phase == PairingClientPhase.COMPLETE
        assert completed.invitation_id == SECOND_INVITATION_ID
        assert completed.pairing_id == SECOND_PAIRING_ID
        assert reset == [True]
        assert await client.self_revoke_authority_denied() is False
        new_pairing = await pairing.status()
        assert new_pairing.state == PairingState.PAIRED
        assert new_pairing.pairing_id == SECOND_PAIRING_ID
        assert new_pairing.credential_epoch == 2
        contents = environment.read_text(encoding="utf-8")
        assert SNAPSHOT_BEARER not in contents
        assert DISPATCH_BEARER not in contents
        assert MANAGEMENT_BEARER not in contents
        assert SECOND_SNAPSHOT_BEARER in contents
        assert SECOND_DISPATCH_BEARER in contents
        assert SECOND_MANAGEMENT_BEARER in contents

        await client.close()
        await pairing.close()
        restarted_pairing = FleetPairingStore(
            tmp_path / "state" / "mnemosyne.db",
            environment,
            process_environment={},
        )
        restored = await restarted_pairing.initialize()
        restarted_denied: list[bool] = []
        restarted = FleetPairingClient(
            tmp_path / "state" / "mnemosyne.db",
            pairing_store=restarted_pairing,
            reporting_node_id=REPORTING_NODE_ID,
            service_version="0.9.0",
            service_instance_id="service-instance-restarted",
            transport=httpx.MockTransport(handler),
            on_self_revoke_pending=lambda: restarted_denied.append(True),
        )
        await restarted.initialize()
        try:
            assert restored.state == PairingState.PAIRED
            assert restored.pairing_id == SECOND_PAIRING_ID
            assert await restarted.self_revoke_authority_denied() is False
            workflow = await restarted.status()
            assert workflow is not None
            assert workflow.phase == PairingClientPhase.COMPLETE
            assert workflow.invitation_id == SECOND_INVITATION_ID
            assert SNAPSHOT_BEARER not in environment.read_text(encoding="utf-8")

            calls_before_stale_replay = len(requests)
            with pytest.raises(PairingClientError) as stale:
                await restarted.self_revoke_enrollment(
                    request_id=old_revoke_request_id
                )
            assert stale.value.code == PairingClientErrorCode.PAYLOAD_MISMATCH
            assert restarted_denied == []
            assert await restarted.self_revoke_authority_denied() is False
            assert len(requests) == calls_before_stale_replay
            still_paired = await restarted_pairing.status()
            assert still_paired.state == PairingState.PAIRED
            assert still_paired.pairing_id == SECOND_PAIRING_ID
            usable = await restarted_pairing.staged_credentials()
            assert usable.snapshot_key == SECOND_SNAPSHOT_BEARER
            assert usable.dispatch_key == SECOND_DISPATCH_BEARER

            fresh = await restarted.self_revoke_enrollment(
                request_id="15151515-1515-4515-8515-151515151515"
            )
            assert fresh.state == PairingManagementState.REVOKED
            assert restarted_denied == [True]
            assert (await restarted_pairing.status()).state == PairingState.REVOKED
        finally:
            await restarted.close()
            await restarted_pairing.close()
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "corrupted_value"),
    [
        ("attempt_id", "44444444-4444-4444-8444-444444444444"),
        ("hub_origin", "https://changed-hub.example.test"),
        ("node_url", "https://changed-node.example.test:1240"),
        ("paired_at", None),
        ("last_error_code", None),
    ],
)
async def test_completed_revoke_reset_rejects_corrupted_tombstone_fields(
    tmp_path: Path,
    column: str,
    corrupted_value: object,
) -> None:
    requests: list[str] = []
    reset: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return _complete_ceremony_response(request)

    pairing, client, environment = await _stores(
        tmp_path,
        handler,
        on_completed_revoke_reset=lambda: reset.append(True),
    )
    await client.begin(_invitation())
    await client.self_revoke_enrollment(
        request_id="21212121-2121-4121-8121-212121212121"
    )
    calls_before_reset = len(requests)
    database = tmp_path / "state" / "mnemosyne.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"UPDATE native_fleet_pairing SET {column}=? WHERE singleton=1",
            (corrupted_value,),
        )

    try:
        with pytest.raises(PairingClientError) as refused:
            await client.begin(_second_invitation())
        assert refused.value.code == PairingClientErrorCode.STATE_CONFLICT
        assert requests == requests[:calls_before_reset]
        assert len(requests) == calls_before_reset
        assert reset == []
        assert await client.self_revoke_authority_denied() is True
        assert (await pairing.status()).state == PairingState.REVOKED
        workflow = await client.status()
        assert workflow is not None
        assert workflow.phase == PairingClientPhase.COMPLETE
        assert workflow.pairing_id == PAIRING_ID
        assert MANAGEMENT_BEARER not in environment.read_text(encoding="utf-8")
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_completed_revoke_reset_rejects_fully_cleared_tombstone(
    tmp_path: Path,
) -> None:
    requests: list[str] = []
    reset: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return _complete_ceremony_response(request)

    pairing, client, _environment = await _stores(
        tmp_path,
        handler,
        on_completed_revoke_reset=lambda: reset.append(True),
    )
    await client.begin(_invitation())
    await client.self_revoke_enrollment(
        request_id="24242424-2424-4424-8424-242424242424"
    )
    calls_before_reset = len(requests)
    assert (await pairing.clear_pairing()).state == PairingState.UNPAIRED

    try:
        with pytest.raises(PairingClientError) as refused:
            await client.begin(_second_invitation())
        assert refused.value.code == PairingClientErrorCode.STATE_CONFLICT
        assert len(requests) == calls_before_reset
        assert reset == []
        assert await client.self_revoke_authority_denied() is True
        assert (await pairing.status()).state == PairingState.UNPAIRED
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "corrupted_value"),
    [
        ("attempt_id", "44444444-4444-4444-8444-444444444444"),
        ("invitation_id", "55555555-5555-4555-8555-555555555555"),
        ("pairing_id", "66666666-6666-4666-8666-666666666666"),
        ("reporting_node_id", "different-mac"),
        ("credential_generation", 99),
        ("hub_origin_fingerprint", "0" * 64),
        ("locator_fingerprint", "f" * 64),
    ],
)
async def test_completed_revoke_reset_is_bound_to_prior_client_identity(
    tmp_path: Path,
    column: str,
    corrupted_value: object,
) -> None:
    requests: list[str] = []
    reset: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return _complete_ceremony_response(request)

    pairing, client, _environment = await _stores(
        tmp_path,
        handler,
        on_completed_revoke_reset=lambda: reset.append(True),
    )
    await client.begin(_invitation())
    await client.self_revoke_enrollment(
        request_id="25252525-2525-4525-8525-252525252525"
    )
    calls_before_reset = len(requests)
    with sqlite3.connect(tmp_path / "state" / "mnemosyne.db") as connection:
        connection.execute(
            f"UPDATE native_fleet_pairing_client_v1 SET {column}=? "
            "WHERE singleton=1",
            (corrupted_value,),
        )

    try:
        with pytest.raises(PairingClientError) as refused:
            await client.begin(_second_invitation())
        assert refused.value.code == PairingClientErrorCode.STATE_CONFLICT
        assert len(requests) == calls_before_reset
        assert reset == []
        assert await client.self_revoke_authority_denied() is True
        assert (await pairing.status()).state == PairingState.REVOKED
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reused_authority", ["pairing_id", "credentials"])
async def test_repair_never_restores_retired_identity_or_credential_bundle(
    tmp_path: Path,
    reused_authority: str,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        payload = json.loads(request.content)
        if request.url.path.endswith("/self-revoke"):
            return httpx.Response(200, json=_management("revoked"))
        if request.url.path == "/fleet/pairing/v1/claims":
            if payload["invitation_id"] == INVITATION_ID:
                return httpx.Response(200, json=_claim(payload["request_id"]))
            response = _claim(payload["request_id"])
            response.update(
                invitation_id=SECOND_INVITATION_ID,
                claim_id=SECOND_CLAIM_ID,
                pairing_id=(
                    PAIRING_ID
                    if reused_authority == "pairing_id"
                    else SECOND_PAIRING_ID
                ),
            )
            return httpx.Response(200, json=response)
        if request.url.path.endswith("/provision"):
            if SECOND_CLAIM_ID not in request.url.path:
                return httpx.Response(200, json=_provision())
            response = _provision()
            response.update(
                claim_id=SECOND_CLAIM_ID,
                pairing_id=SECOND_PAIRING_ID,
                credential_generation=2,
                credentials={
                    "snapshot_bearer": SNAPSHOT_BEARER,
                    "dispatch_bearer": DISPATCH_BEARER,
                    "management_bearer": MANAGEMENT_BEARER,
                },
            )
            return httpx.Response(200, json=response)
        if SECOND_PAIRING_ID not in request.url.path:
            return httpx.Response(200, json=_activation())
        response = _activation()
        response.update(
            pairing_id=SECOND_PAIRING_ID,
            credential_generation=2,
        )
        return httpx.Response(200, json=response)

    pairing, client, environment = await _stores(tmp_path, handler)
    await client.begin(_invitation())
    await client.self_revoke_enrollment(
        request_id="23232323-2323-4323-8323-232323232323"
    )
    calls_after_revoke = len(requests)
    try:
        with pytest.raises(PairingClientError) as old_invitation:
            await client.begin(_invitation())
        assert old_invitation.value.code == PairingClientErrorCode.PAYLOAD_MISMATCH
        assert len(requests) == calls_after_revoke
        assert (await pairing.status()).state == PairingState.REVOKED

        with pytest.raises(PairingClientError) as restored:
            await client.begin(_second_invitation())
        assert restored.value.code == PairingClientErrorCode.HUB_RESPONSE_INVALID
        assert all(
            value not in environment.read_text(encoding="utf-8")
            for value in (
                SNAPSHOT_BEARER,
                DISPATCH_BEARER,
                MANAGEMENT_BEARER,
            )
        )
        current = await pairing.status()
        assert current.state == PairingState.PENDING
        assert current.pairing_id is None
        assert current.credentials_owned is False

        with sqlite3.connect(tmp_path / "state" / "mnemosyne.db") as connection:
            retired = connection.execute(
                """
                SELECT invitation_id, pairing_id, credential_fingerprint
                  FROM native_fleet_retired_self_revoke_v1
                """
            ).fetchall()
        assert retired == [
            (
                INVITATION_ID,
                PAIRING_ID,
                PairingCredentials(
                    snapshot_key=SNAPSHOT_BEARER,
                    dispatch_key=DISPATCH_BEARER,
                    management_key=MANAGEMENT_BEARER,
                ).fingerprint(),
            )
        ]
        database_bytes = (tmp_path / "state" / "mnemosyne.db").read_bytes()
        assert SNAPSHOT_BEARER.encode() not in database_bytes
        assert DISPATCH_BEARER.encode() not in database_bytes
        assert MANAGEMENT_BEARER.encode() not in database_bytes
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "corrupted_value"),
    [
        ("request_id", "not-a-uuid"),
        ("attempt_id", "not-a-uuid"),
        ("invitation_id", "not-a-uuid"),
        ("pairing_id", "not-a-uuid"),
        ("reporting_node_id", ""),
        ("credential_generation", "not-an-int"),
        ("credential_fingerprint", "0" * 63),
        ("retired_at", float("inf")),
    ],
)
async def test_corrupted_retired_authority_fails_closed_on_restart(
    tmp_path: Path,
    column: str,
    corrupted_value: object,
) -> None:
    pairing, client, environment = await _stores(
        tmp_path,
        _complete_ceremony_response,
    )
    await client.begin(_invitation())
    await client.self_revoke_enrollment(
        request_id="26262626-2626-4626-8626-262626262626"
    )
    await client.begin(_second_invitation())
    await client.close()

    with sqlite3.connect(tmp_path / "state" / "mnemosyne.db") as connection:
        connection.execute(
            f"UPDATE native_fleet_retired_self_revoke_v1 SET {column}=?",
            (corrupted_value,),
        )

    restarted = FleetPairingClient(
        tmp_path / "state" / "mnemosyne.db",
        pairing_store=pairing,
        reporting_node_id=REPORTING_NODE_ID,
        service_version="0.9.0",
        service_instance_id="service-instance-restarted",
        transport=httpx.MockTransport(_complete_ceremony_response),
    )
    try:
        with pytest.raises(PairingClientError) as corrupted:
            await restarted.initialize()
        assert corrupted.value.code == PairingClientErrorCode.STATE_CONFLICT
        current = await pairing.status()
        assert current.state == PairingState.PAIRED
        assert current.pairing_id == SECOND_PAIRING_ID
        assert SECOND_MANAGEMENT_BEARER in environment.read_text(encoding="utf-8")
    finally:
        await restarted.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_terminal_revoke_rejection_restores_authority_and_retires_id(
    tmp_path: Path,
) -> None:
    rejected_request_id = "16161616-1616-4616-8616-161616161616"
    accepted_request_id = "17171717-1717-4717-8717-171717171717"
    requests: list[tuple[str, str | None]] = []
    denied: list[bool] = []
    aborted: list[bool] = []
    reset: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        request_id = payload.get("request_id")
        requests.append((request.url.path, request_id))
        if (
            request.url.path.endswith("/self-revoke")
            and request_id == rejected_request_id
        ):
            return httpx.Response(409)
        return _complete_ceremony_response(request)

    pairing, client, _environment = await _stores(
        tmp_path,
        handler,
        on_self_revoke_pending=lambda: denied.append(True),
        on_self_revoke_aborted=lambda: aborted.append(True),
        on_completed_revoke_reset=lambda: reset.append(True),
    )
    await client.begin(_invitation())
    try:
        with pytest.raises(PairingClientError) as terminal:
            await client.self_revoke_enrollment(
                request_id=rejected_request_id
            )
        assert terminal.value.code == PairingClientErrorCode.MANAGEMENT_REJECTED
        assert terminal.value.retryable is False
        assert denied == [True]
        assert aborted == [True]
        assert await client.self_revoke_authority_denied() is False
        assert await client.self_revoke_status() is None
        still_paired = await pairing.status()
        assert still_paired.state == PairingState.PAIRED
        usable = await pairing.staged_credentials()
        assert usable.management_key == MANAGEMENT_BEARER

        calls_after_rejection = len(requests)
        with pytest.raises(PairingClientError) as stale_same_generation:
            await client.self_revoke_enrollment(
                request_id=rejected_request_id
            )
        assert stale_same_generation.value.code == (
            PairingClientErrorCode.PAYLOAD_MISMATCH
        )
        assert len(requests) == calls_after_rejection
        assert denied == [True]
        assert await client.self_revoke_authority_denied() is False

        accepted = await client.self_revoke_enrollment(
            request_id=accepted_request_id
        )
        assert accepted.state == PairingManagementState.REVOKED
        assert denied == [True, True]
        assert await client.self_revoke_authority_denied() is True

        new_pairing = await client.begin(_second_invitation())
        assert new_pairing.phase == PairingClientPhase.COMPLETE
        assert new_pairing.pairing_id == SECOND_PAIRING_ID
        assert reset == [True]
        assert await client.self_revoke_authority_denied() is False

        calls_before_old_generation_replay = len(requests)
        with pytest.raises(PairingClientError) as stale_later_generation:
            await client.self_revoke_enrollment(
                request_id=rejected_request_id
            )
        assert stale_later_generation.value.code == (
            PairingClientErrorCode.PAYLOAD_MISMATCH
        )
        assert len(requests) == calls_before_old_generation_replay
        assert denied == [True, True]
        assert await client.self_revoke_authority_denied() is False
        current = await pairing.status()
        assert current.state == PairingState.PAIRED
        assert current.pairing_id == SECOND_PAIRING_ID
        current_credentials = await pairing.staged_credentials()
        assert current_credentials.management_key == SECOND_MANAGEMENT_BEARER
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_retired_request_capacity_is_checked_before_latch_or_hub_call(
    tmp_path: Path,
) -> None:
    requests: list[str] = []
    denied: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(409)

    pairing, client, _environment = await _stores(
        tmp_path,
        handler,
        on_self_revoke_pending=lambda: denied.append(True),
    )
    await _activate_local_pairing(pairing, client)
    authority = await pairing.status()
    workflow = await client.status()
    assert authority.credential_fingerprint is not None
    assert workflow is not None
    rows = [
        (
            f"00000000-0000-4000-8000-{index:012x}",
            workflow.attempt_id,
            workflow.invitation_id,
            PAIRING_ID,
            REPORTING_NODE_ID,
            1,
            authority.credential_fingerprint,
            float(index + 1),
        )
        for index in range(128)
    ]
    with sqlite3.connect(tmp_path / "state" / "mnemosyne.db") as connection:
        connection.executemany(
            """
            INSERT INTO native_fleet_retired_self_revoke_v1 (
                request_id, attempt_id, invitation_id, pairing_id,
                reporting_node_id, credential_generation,
                credential_fingerprint, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    try:
        with pytest.raises(PairingClientError) as full:
            await client.self_revoke_enrollment(
                request_id="22222222-aaaa-4aaa-8aaa-222222222222"
            )
        assert full.value.code == PairingClientErrorCode.STATE_CONFLICT
        assert requests == []
        assert denied == []
        assert await client.self_revoke_authority_denied() is False
        assert await client.self_revoke_status() is None
        paired = await pairing.status()
        assert paired.state == PairingState.PAIRED
        assert paired.credentials_owned is True
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_response", "expected_code"),
    [
        (
            httpx.Response(429),
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN,
        ),
        (
            httpx.Response(503),
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN,
        ),
        (
            httpx.Response(200, json={"schema_version": 1}),
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN,
        ),
        (
            httpx.Response(302, headers={"location": "https://other.test"}),
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN,
        ),
        (
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b"x" * (MAX_PAIRING_RESPONSE_BYTES + 1),
            ),
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN,
        ),
        (
            httpx.Response(201, json=_management("revoked")),
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN,
        ),
    ],
)
async def test_ambiguous_revoke_responses_keep_exact_request_fenced(
    tmp_path: Path,
    first_response: httpx.Response,
    expected_code: PairingClientErrorCode,
) -> None:
    request_id = "18181818-1818-4818-8818-181818181818"
    request_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        request_ids.append(payload["request_id"])
        if len(request_ids) == 1:
            return first_response
        return httpx.Response(200, json=_management("revoked"))

    pairing, client, _environment = await _stores(tmp_path, handler)
    await _activate_local_pairing(pairing, client)
    try:
        with pytest.raises(PairingClientError) as ambiguous:
            await client.self_revoke_enrollment(request_id=request_id)
        assert ambiguous.value.code == expected_code
        assert await client.self_revoke_authority_denied() is True
        assert await client.self_revoke_status() == {
            "schema_version": 1,
            "request_id": request_id,
            "phase": "pending",
        }
        paired = await pairing.status()
        assert paired.state == PairingState.PAIRED
        assert paired.credentials_owned is True

        recovered = await client.self_revoke_enrollment(request_id=request_id)
        assert recovered.state == PairingManagementState.REVOKED
        assert request_ids == [request_id, request_id]
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_pending_revoke_and_local_revoked_state_never_fabricate_hub_commit(
    tmp_path: Path,
) -> None:
    request_id = "20202020-2020-4020-8020-202020202020"
    request_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        request_ids.append(payload["request_id"])
        raise httpx.ReadTimeout("ambiguous", request=request)

    pairing, client, _environment = await _stores(tmp_path, handler)
    await _activate_local_pairing(pairing, client)
    try:
        with pytest.raises(PairingClientError) as ambiguous:
            await client.self_revoke_enrollment(request_id=request_id)
        assert ambiguous.value.code == (
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN
        )
        assert (await client.self_revoke_status())["phase"] == "pending"

        # HUB_COMMITTED is always durable before the ordinary local REVOKED
        # transition. This impossible ordering simulates a corrupted/manual
        # tombstone and must not be upgraded into remote proof.
        await pairing.mark_revoked()
        with pytest.raises(PairingClientError) as corrupted:
            await client.self_revoke_enrollment(request_id=request_id)
        assert corrupted.value.code == PairingClientErrorCode.STATE_CONFLICT
        assert request_ids == [request_id]
        assert (await client.self_revoke_status()) == {
            "schema_version": 1,
            "request_id": request_id,
            "phase": "pending",
        }
        assert await client.self_revoke_authority_denied() is True
        assert (await pairing.status()).state == PairingState.REVOKED

        with pytest.raises(PairingClientError) as repair:
            await client.begin(_second_invitation())
        assert repair.value.code == PairingClientErrorCode.STATE_CONFLICT
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_cancellation_during_terminal_abort_finishes_before_reopening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = "19191919-1919-4919-8919-191919191919"
    entered = threading.Event()
    release = threading.Event()
    denied: list[bool] = []
    aborted: list[bool] = []
    pairing, client, _environment = await _stores(
        tmp_path,
        lambda _request: httpx.Response(409),
        on_self_revoke_pending=lambda: denied.append(True),
        on_self_revoke_aborted=lambda: aborted.append(True),
    )
    await _activate_local_pairing(pairing, client)
    original_abort = client._journal.abort_pending_self_revoke

    def blocked_abort(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_abort(*args, **kwargs)

    monkeypatch.setattr(
        client._journal,
        "abort_pending_self_revoke",
        blocked_abort,
    )
    task = asyncio.create_task(
        client.self_revoke_enrollment(request_id=request_id)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert denied == [True]
        assert aborted == [True]
        assert await client.self_revoke_authority_denied() is False
        assert await client.self_revoke_status() is None
        assert (await pairing.status()).state == PairingState.PAIRED

        with pytest.raises(PairingClientError) as stale:
            await client.self_revoke_enrollment(request_id=request_id)
        assert stale.value.code == PairingClientErrorCode.PAYLOAD_MISMATCH
        assert denied == [True]
        assert await client.self_revoke_authority_denied() is False
    finally:
        release.set()
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_static_credentials_block_completed_revoke_reset(
    tmp_path: Path,
) -> None:
    reset: list[bool] = []
    pairing, client, environment = await _stores(
        tmp_path,
        _complete_ceremony_response,
        on_completed_revoke_reset=lambda: reset.append(True),
    )
    await client.begin(_invitation())
    try:
        await client.self_revoke_enrollment(
            request_id="14141414-1414-4414-8414-141414141414"
        )
        static = (
            "FLEET_API_KEY=static-snapshot\n"
            "FLEET_INFERENCE_API_KEY=static-dispatch\n"
            "FLEET_MANAGEMENT_API_KEY=static-management\n"
        )
        environment.write_text(static, encoding="utf-8")

        with pytest.raises(PairingClientError) as refused:
            await client.begin(_second_invitation())
        assert refused.value.code == (
            PairingClientErrorCode.STATIC_CREDENTIALS_PRESENT
        )
        assert reset == []
        assert await client.self_revoke_authority_denied() is True
        assert environment.read_text(encoding="utf-8") == static
        assert (await pairing.status()).state == PairingState.REVOKED
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_management_offline_and_timeout_outcomes_retry_exact_request(
    tmp_path: Path,
) -> None:
    request_ids: list[str] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        request_ids.append(payload["request_id"])
        if calls == 1:
            raise httpx.ConnectError("offline", request=request)
        if calls == 2:
            return httpx.Response(200, json=_management("disabled"))
        if calls == 3:
            raise httpx.ReadTimeout("ambiguous", request=request)
        return httpx.Response(200, json=_management("revoked"))

    pairing, client, environment = await _stores(tmp_path, handler)
    await _activate_local_pairing(pairing, client)
    disable_request = "99999999-9999-4999-8999-999999999999"
    revoke_request = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    try:
        with pytest.raises(PairingClientError) as offline:
            await client.self_disable_enrollment(request_id=disable_request)
        assert offline.value.code == (
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN
        )
        assert offline.value.retryable
        assert (await pairing.status()).state == PairingState.PAIRED
        assert (
            await client.self_disable_enrollment(request_id=disable_request)
        ).state == PairingManagementState.DISABLED

        with pytest.raises(PairingClientError) as ambiguous:
            await client.self_revoke_enrollment(request_id=revoke_request)
        assert ambiguous.value.code == (
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN
        )
        assert (await pairing.status()).state == PairingState.PAIRED
        assert MANAGEMENT_BEARER in environment.read_text(encoding="utf-8")
        assert await client.self_revoke_authority_denied() is True
        assert await client.self_revoke_status() == {
            "schema_version": 1,
            "request_id": revoke_request,
            "phase": "pending",
        }
        assert (
            await client.self_revoke_enrollment(request_id=revoke_request)
        ).state == PairingManagementState.REVOKED
        assert request_ids == [
            disable_request,
            disable_request,
            revoke_request,
            revoke_request,
        ]
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _management("disabled", reporting_node_id="wrong-node"),
        _management("disabled", credential_generation=2),
    ],
)
async def test_management_response_wrong_node_or_generation_fails_closed(
    tmp_path: Path,
    response: dict[str, Any],
) -> None:
    pairing, client, _environment = await _stores(
        tmp_path,
        lambda _request: httpx.Response(200, json=response),
    )
    await _activate_local_pairing(pairing, client)
    try:
        with pytest.raises(PairingClientError) as rejected:
            await client.self_disable_enrollment(
                request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            )
        assert rejected.value.code == PairingClientErrorCode.HUB_RESPONSE_INVALID
        assert (await pairing.status()).state == PairingState.PAIRED
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(307, headers={"Location": "https://other.test"}),
            PairingClientErrorCode.HUB_REDIRECT_REFUSED,
        ),
        (
            httpx.Response(200, content=b"x" * (MAX_PAIRING_RESPONSE_BYTES + 1)),
            PairingClientErrorCode.HUB_RESPONSE_TOO_LARGE,
        ),
        (
            httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=json.dumps(
                    {"pairing_secret": PAIRING_SECRET, "locator": LOCATOR}
                ),
            ),
            PairingClientErrorCode.HUB_RESPONSE_INVALID,
        ),
    ],
)
async def test_redirect_oversize_and_invalid_responses_fail_closed_without_echo(
    tmp_path: Path,
    response: httpx.Response,
    expected: PairingClientErrorCode,
) -> None:
    pairing, client, _environment = await _stores(
        tmp_path,
        lambda _request: response,
    )
    try:
        # These are intentional implementation-bound assertions: the outbound
        # security properties must not regress during future httpx refactors.
        assert client._client._trust_env is False
        assert client._client.follow_redirects is False
        with pytest.raises(PairingClientError) as failed:
            await client.begin(_invitation())
        assert failed.value.code == expected
        assert PAIRING_SECRET not in str(failed.value)
        assert LOCATOR not in str(failed.value)
        status = await client.status()
        assert status is not None
        assert status.last_error_code == expected
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_only_verified_https_hub_origins_are_accepted(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    pairing, client, _environment = await _stores(tmp_path, handler)
    try:
        with pytest.raises(PairingClientError) as rejected:
            await client.begin(_invitation(hub_origin="http://nyx.example.test"))
        assert rejected.value.code == PairingClientErrorCode.PAYLOAD_MISMATCH
        assert calls == 0
        assert await client.status() is None
    finally:
        await client.close()
        await pairing.close()


@pytest.mark.asyncio
async def test_existing_static_credentials_require_explicit_migration(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    database = tmp_path / "state" / "mnemosyne.db"
    pairing = FleetPairingStore(
        database,
        tmp_path / "private" / ".env",
        process_environment={
            "FLEET_API_KEY": "legacy-snapshot",
            "FLEET_INFERENCE_API_KEY": "legacy-dispatch",
        },
    )
    await pairing.initialize()
    client = FleetPairingClient(
        database,
        pairing_store=pairing,
        reporting_node_id=REPORTING_NODE_ID,
        service_version="0.9.0",
        service_instance_id="service-instance-one",
        transport=httpx.MockTransport(handler),
    )
    await client.initialize()
    try:
        disabled = await client.self_disable_enrollment(
            request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        )
        revoked = await client.self_revoke_enrollment(
            request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        )
        assert disabled.state == PairingManagementState.ADMIN_ACTION_REQUIRED
        assert revoked.state == PairingManagementState.ADMIN_ACTION_REQUIRED
        assert disabled.public_payload() == {
            "schema_version": 1,
            "state": "admin_action_required",
            "pairing_id": None,
            "reporting_node_id": None,
            "credential_generation": None,
        }
        with pytest.raises(PairingClientError) as rejected:
            await client.begin(_invitation())
        assert rejected.value.code == (
            PairingClientErrorCode.STATIC_CREDENTIALS_PRESENT
        )
        assert calls == 0
        assert (await pairing.status()).state == PairingState.UNPAIRED
        assert await client.status() is None
    finally:
        await client.close()
        await pairing.close()
