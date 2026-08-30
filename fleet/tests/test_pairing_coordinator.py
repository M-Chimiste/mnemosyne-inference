from __future__ import annotations

import base64
import uuid

import httpx
import pytest

from mnemosyne_fleet.locator_policy import LocatorPolicy
from mnemosyne_fleet.pairing_api import (
    ActivationAcknowledgement,
    ClaimApproval,
    ClaimProvision,
    ClaimingMac,
    EnrollmentSelfManagement,
    InvitationClaim,
    InvitationCreate,
    InvitationExpected,
    SupportedPairingProtocol,
)
from mnemosyne_fleet.pairing_coordinator import PairingCoordinator
from mnemosyne_fleet.pairing_store import (
    PairingStore,
    PairingStoreTerminalError,
)
from mnemosyne_fleet.registry import NodeRegistry
from mnemosyne_fleet.secret_store import SecretStore


def _uuid() -> str:
    return str(uuid.uuid4())


def _master_key() -> str:
    return base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


async def _coordinator(tmp_path):
    private = tmp_path / "private"
    secret_store = SecretStore(
        private / "pairing-secrets.db",
        store_id="test-secret-store",
        master_key=_master_key(),
    )
    pairing_store = PairingStore(
        private / "pairing-metadata.db",
        store_id="test-pairing-store",
        secret_store=secret_store,
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500)
        )
    )
    policy = LocatorPolicy(
        cidr_allowlists={
            "https": ("10.20.30.0/24",),
            "tailscale": (),
            "trusted_lan_http": (),
        },
        allowed_ports=(1240,),
        resolver=lambda _host, _port: ("10.20.30.40",),
    )
    registry = NodeRegistry(
        nodes=(),
        client=client,
        poll_interval_seconds=1,
        ttl_seconds=10,
        paired_locator_policy=policy,
    )
    probes = []

    async def probe(candidate) -> None:
        probes.append(candidate)

    coordinator = PairingCoordinator(
        pairing_store=pairing_store,
        secret_store=secret_store,
        locator_policy=policy,
        registry=registry,
        activation_probe=probe,
        forbidden_credentials=("client-key", "admin-key"),
    )
    await coordinator.initialize()
    return coordinator, pairing_store, registry, probes, client


async def test_disabled_activation_replay_enable_and_revoke_are_fail_closed(
    tmp_path,
) -> None:
    coordinator, store, registry, probes, client = await _coordinator(tmp_path)
    try:
        invitation = await coordinator.issue_invitation(
            InvitationCreate(
                schema_version=1,
                request_id=_uuid(),
                intent="new",
                expected=InvitationExpected(
                    platform="macos",
                    reporting_node_id="studio-mac",
                    locator="https://studio.example.internal:1240",
                    transport="https",
                    service_class="primary",
                ),
                expires_in_seconds=300,
            )
        )
        claim = await coordinator.claim_invitation(
            InvitationClaim(
                schema_version=1,
                request_id=_uuid(),
                invitation_id=invitation.invitation_id,
                pairing_secret=invitation.pairing_secret,
                mac=ClaimingMac(
                    platform="macos",
                    service_version="0.9.0",
                    display_name="Studio Mac",
                    reporting_node_id="studio-mac",
                ),
                locator="https://studio.example.internal:1240",
                supported_protocol=SupportedPairingProtocol(
                    minimum=1,
                    maximum=1,
                ),
            )
        )
        approved = await coordinator.approve_claim(
            claim.claim_id,
            ClaimApproval(
                schema_version=1,
                request_id=_uuid(),
                locator="https://studio.example.internal:1240",
                service_class="primary",
                hub_enabled=False,
            ),
        )
        assert approved.state == "pending"
        provisioned = await coordinator.provision_claim(
            claim.claim_id,
            ClaimProvision(
                schema_version=1,
                request_id=_uuid(),
                pairing_secret=invitation.pairing_secret,
            ),
        )
        acknowledgement = ActivationAcknowledgement(
            schema_version=1,
            request_id=_uuid(),
            credential_generation=provisioned.credential_generation,
            reporting_node_id="studio-mac",
            service_instance_id="service-instance-1",
        )

        with pytest.raises(PairingStoreTerminalError):
            await coordinator.activate(
                pairing_id=claim.pairing_id,
                management_bearer="wrong-management-secret",
                payload=acknowledgement,
            )
        assert probes == []
        assert registry.node_count == 0

        disabled = await coordinator.activate(
            pairing_id=claim.pairing_id,
            management_bearer=provisioned.credentials.management.secret,
            payload=acknowledgement,
        )
        assert disabled.state == "disabled"
        assert registry.node_count == 0
        assert len(probes) == 1
        assert probes[0].locator.origin == (
            "https://studio.example.internal:1240"
        )
        assert provisioned.credentials.management.secret not in repr(probes[0])

        replay = await coordinator.activate(
            pairing_id=claim.pairing_id,
            management_bearer=provisioned.credentials.management.secret,
            payload=acknowledgement,
        )
        assert replay == disabled
        assert len(probes) == 1

        active = await coordinator.set_hub_enabled(
            pairing_id=claim.pairing_id,
            request_id=_uuid(),
            enabled=True,
        )
        assert active.routable
        assert registry.node_count == 1
        enrolled = registry.enrollment(claim.pairing_id)
        assert enrolled is not None
        assert enrolled.source == "paired"
        assert enrolled.enrollment_id == claim.pairing_id
        assert enrolled.reporting_node_id == "studio-mac"

        disabled_again = await coordinator.set_hub_enabled(
            pairing_id=claim.pairing_id,
            request_id=_uuid(),
            enabled=False,
        )
        assert disabled_again.state == "disabled"
        assert registry.node_count == 0

        revoked = await coordinator.revoke(
            pairing_id=claim.pairing_id,
            request_id=_uuid(),
        )
        assert revoked.state == "revoked"
        assert registry.node_count == 0
        assert await store.active_binding(
            claim.pairing_id,
            provisioned.credential_generation,
        ) is None
    finally:
        await client.aclose()


async def test_self_disable_and_self_revoke_require_the_exact_active_binding(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _store, registry, _probes, client = await _coordinator(tmp_path)
    try:
        invitation = await coordinator.issue_invitation(
            InvitationCreate(
                schema_version=1,
                request_id=_uuid(),
                intent="new",
                expected=InvitationExpected(
                    platform="macos",
                    reporting_node_id="studio-mac",
                    locator="https://studio.example.internal:1240",
                    transport="https",
                    service_class="primary",
                ),
                expires_in_seconds=300,
            )
        )
        claim = await coordinator.claim_invitation(
            InvitationClaim(
                schema_version=1,
                request_id=_uuid(),
                invitation_id=invitation.invitation_id,
                pairing_secret=invitation.pairing_secret,
                mac=ClaimingMac(
                    platform="macos",
                    service_version="0.9.0",
                    display_name="Studio Mac",
                    reporting_node_id="studio-mac",
                ),
                locator="https://studio.example.internal:1240",
                supported_protocol=SupportedPairingProtocol(minimum=1, maximum=1),
            )
        )
        await coordinator.approve_claim(
            claim.claim_id,
            ClaimApproval(
                schema_version=1,
                request_id=_uuid(),
                locator="https://studio.example.internal:1240",
                service_class="primary",
                hub_enabled=True,
            ),
        )
        provisioned = await coordinator.provision_claim(
            claim.claim_id,
            ClaimProvision(
                schema_version=1,
                request_id=_uuid(),
                pairing_secret=invitation.pairing_secret,
            ),
        )
        await coordinator.activate(
            pairing_id=claim.pairing_id,
            management_bearer=provisioned.credentials.management.secret,
            payload=ActivationAcknowledgement(
                schema_version=1,
                request_id=_uuid(),
                credential_generation=provisioned.credential_generation,
                reporting_node_id="studio-mac",
                service_instance_id="service-instance-1",
            ),
        )
        assert registry.node_count == 1

        disable = EnrollmentSelfManagement(
            schema_version=1,
            request_id=_uuid(),
            pairing_id=claim.pairing_id,
            reporting_node_id="studio-mac",
            credential_generation=provisioned.credential_generation,
        )
        for bearer, payload in (
            ("wrong-management-secret", disable),
            (
                provisioned.credentials.management.secret,
                disable.model_copy(update={"reporting_node_id": "wrong-node"}),
            ),
            (
                provisioned.credentials.management.secret,
                disable.model_copy(
                    update={
                        "credential_generation": (
                            provisioned.credential_generation + 1
                        )
                    }
                ),
            ),
        ):
            with pytest.raises(PairingStoreTerminalError):
                await coordinator.self_disable(
                    pairing_id=claim.pairing_id,
                    management_bearer=bearer,
                    payload=payload,
                )
        original_disable = coordinator.pairing_store.self_disable_enrollment

        async def observe_disable(**kwargs):
            assert registry.node_count == 0
            return await original_disable(**kwargs)

        monkeypatch.setattr(
            coordinator.pairing_store,
            "self_disable_enrollment",
            observe_disable,
        )
        disabled = await coordinator.self_disable(
            pairing_id=claim.pairing_id,
            management_bearer=provisioned.credentials.management.secret,
            payload=disable,
        )
        assert disabled.state == "disabled"
        assert registry.node_count == 0
        assert await coordinator.self_disable(
            pairing_id=claim.pairing_id,
            management_bearer=provisioned.credentials.management.secret,
            payload=disable,
        ) == disabled

        await coordinator.set_hub_enabled(
            pairing_id=claim.pairing_id,
            request_id=_uuid(),
            enabled=True,
        )
        assert registry.node_count == 1
        revoke = disable.model_copy(update={"request_id": _uuid()})
        original_revoke = coordinator.pairing_store.self_revoke_enrollment

        async def observe_revoke(**kwargs):
            assert registry.node_count == 0
            return await original_revoke(**kwargs)

        monkeypatch.setattr(
            coordinator.pairing_store,
            "self_revoke_enrollment",
            observe_revoke,
        )
        revoked = await coordinator.self_revoke(
            pairing_id=claim.pairing_id,
            management_bearer=provisioned.credentials.management.secret,
            payload=revoke,
        )
        assert revoked.state == "revoked"
        assert registry.node_count == 0
        # The live bundle is gone, but only the same body and bearer can recover
        # an ambiguously lost response.
        assert await coordinator.self_revoke(
            pairing_id=claim.pairing_id,
            management_bearer=provisioned.credentials.management.secret,
            payload=revoke,
        ) == revoked
        with pytest.raises(PairingStoreTerminalError):
            await coordinator.self_revoke(
                pairing_id=claim.pairing_id,
                management_bearer="wrong-management-secret",
                payload=revoke,
            )
        with pytest.raises(PairingStoreTerminalError):
            await coordinator.self_revoke(
                pairing_id=claim.pairing_id,
                management_bearer=provisioned.credentials.management.secret,
                payload=revoke.model_copy(update={"request_id": _uuid()}),
            )
    finally:
        await client.aclose()
