from __future__ import annotations

import asyncio
import base64
import sqlite3
import stat
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

import mnemosyne_fleet.secret_store as secret_store_module
from mnemosyne_fleet.pairing_store import (
    ApprovalRequest,
    ClaimRequest,
    InvitationRequest,
    PairingStore,
    PairingStoreConflictError,
    PairingStoreIntegrityError,
    PairingStoreTerminalError,
    PairingStoreValidationError,
    PresenceApprovalRequest,
    ProvisionRequest,
    pairing_presence_pin,
)
from mnemosyne_fleet.secret_store import (
    SecretStore,
    SecretStoreIntegrityError,
)


class MutableClock:
    def __init__(self, value: float = 1_788_019_200.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _uuid() -> str:
    return str(uuid.uuid4())


def _master_key() -> str:
    return base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


async def _stores(
    tmp_path: Path,
    *,
    clock: MutableClock | None = None,
) -> tuple[SecretStore, PairingStore, MutableClock, Path, Path]:
    active_clock = MutableClock() if clock is None else clock
    private = tmp_path / "private"
    secret_path = private / "pairing-secrets.db"
    metadata_path = private / "pairing-metadata.db"
    secret_store = SecretStore(
        secret_path,
        store_id="nyx-pairing-secrets",
        master_key=_master_key(),
    )
    await secret_store.initialize()
    pairing_store = PairingStore(
        metadata_path,
        store_id="nyx-pairing-metadata",
        secret_store=secret_store,
        clock=active_clock,
    )
    await pairing_store.initialize()
    return secret_store, pairing_store, active_clock, secret_path, metadata_path


def _invitation_request(
    *,
    request_id: str | None = None,
    locator: str = "https://mac-a.example.internal:1240",
    expires: float = 300.0,
) -> InvitationRequest:
    return InvitationRequest(
        request_id=_uuid() if request_id is None else request_id,
        locator=locator,
        expected_reporting_node_id="metis",
        expires_in_seconds=expires,
    )


async def _issue_and_claim(
    pairing_store: PairingStore,
    *,
    locator: str = "https://mac-a.example.internal:1240",
) -> tuple[object, object, ClaimRequest]:
    invitation = await pairing_store.issue_invitation(
        _invitation_request(locator=locator)
    )
    claim_request = ClaimRequest(
        request_id=_uuid(),
        invitation_id=invitation.invitation_id,
        pairing_secret=invitation.pairing_secret,
        locator=locator,
        display_name="Studio Mac",
        reporting_node_id="metis",
        service_version="0.9.0",
    )
    claim = await pairing_store.claim_invitation(claim_request)
    return invitation, claim, claim_request


async def _approve(
    pairing_store: PairingStore,
    claim: object,
    *,
    locator: str = "https://mac-a.example.internal:1240",
    hub_enabled: bool = False,
):
    return await pairing_store.approve_claim(
        ApprovalRequest(
            request_id=_uuid(),
            claim_id=claim.claim_id,
            locator=locator,
            hub_enabled=hub_enabled,
        )
    )


@pytest.mark.asyncio
async def test_invitation_exact_replay_is_private_and_secret_free(
    tmp_path: Path,
) -> None:
    secret_store, store, _, secret_path, metadata_path = await _stores(tmp_path)
    request = _invitation_request()

    first = await store.issue_invitation(request)
    replay = await store.issue_invitation(request)
    assert replay == first
    assert first.state == "issued"
    assert first.pairing_secret not in repr(first)
    assert stat.S_IMODE(metadata_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600
    assert metadata_path != secret_path

    with pytest.raises(PairingStoreConflictError) as conflict:
        await store.issue_invitation(
            replace(request, locator="https://mac-b.example.internal:1240")
        )
    assert conflict.value.code == "idempotency_conflict"

    metadata_bytes = metadata_path.read_bytes()
    secret_bytes = secret_path.read_bytes()
    for private_value in (first.pairing_secret, request.locator):
        assert private_value.encode() not in metadata_bytes
        assert private_value.encode() not in secret_bytes
        assert private_value not in repr(request)
    with sqlite3.connect(metadata_path) as conn:
        schema = " ".join(
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchall()
        ).lower()
        assert "ciphertext" not in schema
        assert "nonce blob" not in schema
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not list(metadata_path.parent.glob("pairing-metadata.db-*"))
    assert await secret_store.load_pairing_material(
        _pairing_id(metadata_path, first.invitation_id),
        first.invitation_id,
    ) is not None


@pytest.mark.asyncio
async def test_expiry_and_failed_attempt_budget_are_durable(tmp_path: Path) -> None:
    _, store, clock, _, _ = await _stores(tmp_path)
    expiring = await store.issue_invitation(_invitation_request(expires=10.0))
    clock.advance(11.0)
    assert await store.expire_invitations() == 1
    expired = await store.invitation(expiring.invitation_id)
    assert expired is not None and expired.state == "expired"
    with pytest.raises(PairingStoreTerminalError) as expired_error:
        await store.claim_invitation(
            ClaimRequest(
                request_id=_uuid(),
                invitation_id=expiring.invitation_id,
                pairing_secret=expiring.pairing_secret,
                locator="https://mac-a.example.internal:1240",
                reporting_node_id="metis",
            )
        )
    assert expired_error.value.code == "pairing_invitation_expired"

    issued = await store.issue_invitation(_invitation_request())
    for attempt in range(5):
        with pytest.raises(PairingStoreTerminalError) as rejected:
            await store.claim_invitation(
                ClaimRequest(
                    request_id=_uuid(),
                    invitation_id=issued.invitation_id,
                    pairing_secret="wrong-high-entropy-secret",
                    locator="https://mac-a.example.internal:1240",
                    reporting_node_id="metis",
                )
            )
        expected = (
            "pairing_attempt_budget_exhausted"
            if attempt == 4
            else "pairing_claim_rejected"
        )
        assert rejected.value.code == expected
    exhausted = await store.invitation(issued.invitation_id)
    assert exhausted is not None
    assert exhausted.state == "failed"
    assert exhausted.attempts_remaining == 0
    assert exhausted.failure_code == "attempt_budget_exhausted"


@pytest.mark.asyncio
async def test_presence_pin_approves_exact_claim_and_uses_attempt_budget(
    tmp_path: Path,
) -> None:
    _, store, _, _, _ = await _stores(tmp_path)
    invitation, claim, _ = await _issue_and_claim(store)
    presence_pin = pairing_presence_pin(invitation.pairing_secret)
    assert len(presence_pin) == 6
    assert presence_pin.isascii() and presence_pin.isdigit()

    with pytest.raises(PairingStoreTerminalError) as rejected:
        await store.approve_claim_with_presence(
            PresenceApprovalRequest(
                request_id=_uuid(),
                claim_id=claim.claim_id,
                presence_pin=("000000" if presence_pin != "000000" else "999999"),
            )
        )
    assert rejected.value.code == "pairing_presence_pin_rejected"
    pending = await store.invitation(invitation.invitation_id)
    assert pending is not None and pending.attempts_remaining == 4

    request = PresenceApprovalRequest(
        request_id=_uuid(),
        claim_id=claim.claim_id,
        presence_pin=presence_pin,
    )
    approved = await store.approve_claim_with_presence(request)
    replay = await store.approve_claim_with_presence(request)
    assert replay == approved
    assert approved.lifecycle_state == "pending"
    assert approved.hub_enabled is False
    assert invitation.pairing_secret not in repr(request)
    assert presence_pin not in repr(request)


@pytest.mark.asyncio
async def test_claim_verification_is_constant_time_and_concurrent_single_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, _, _, _ = await _stores(tmp_path)
    invitation = await store.issue_invitation(_invitation_request())
    comparisons: list[tuple[bytes, bytes]] = []
    original_compare = secret_store_module.hmac.compare_digest

    def traced_compare(left: bytes, right: bytes) -> bool:
        comparisons.append((left, right))
        return original_compare(left, right)

    monkeypatch.setattr(secret_store_module.hmac, "compare_digest", traced_compare)
    with pytest.raises(PairingStoreTerminalError):
        await store.claim_invitation(
            ClaimRequest(
                request_id=_uuid(),
                invitation_id=invitation.invitation_id,
                pairing_secret="wrong-secret-value",
                locator="https://mac-a.example.internal:1240",
                reporting_node_id="metis",
            )
        )
    assert any(right == b"wrong-secret-value" for _, right in comparisons)

    def claim_request() -> ClaimRequest:
        return ClaimRequest(
            request_id=_uuid(),
            invitation_id=invitation.invitation_id,
            pairing_secret=invitation.pairing_secret,
            locator="https://mac-a.example.internal:1240",
            reporting_node_id="metis",
        )

    outcomes = await asyncio.gather(
        store.claim_invitation(claim_request()),
        store.claim_invitation(claim_request()),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    assert sum(isinstance(item, PairingStoreTerminalError) for item in outcomes) == 1


@pytest.mark.asyncio
async def test_claim_approval_and_provision_are_exactly_idempotent(
    tmp_path: Path,
) -> None:
    _, store, _, _, metadata_path = await _stores(tmp_path)
    invitation, claim, claim_request = await _issue_and_claim(store)
    assert await store.claim_invitation(claim_request) == claim
    assert await store.pending_claims() == (claim,)
    assert "https://" not in repr((await store.pending_claims())[0])
    with pytest.raises(PairingStoreValidationError):
        await store.pending_claims(limit=1001)
    with pytest.raises(PairingStoreConflictError) as claim_conflict:
        await store.claim_invitation(replace(claim_request, display_name="Other Mac"))
    assert claim_conflict.value.code == "idempotency_conflict"

    approval = ApprovalRequest(
        request_id=_uuid(),
        claim_id=claim.claim_id,
        locator="https://mac-a.example.internal:1240",
        hub_enabled=False,
    )
    pending = await store.approve_claim(approval)
    assert pending.state == "pending"
    assert await store.pending_claims() == ()
    assert await store.approve_claim(approval) == pending
    with pytest.raises(PairingStoreConflictError) as approval_conflict:
        await store.approve_claim(replace(approval, hub_enabled=True))
    assert approval_conflict.value.code == "idempotency_conflict"

    provision = ProvisionRequest(
        request_id=_uuid(),
        claim_id=claim.claim_id,
        pairing_secret=invitation.pairing_secret,
    )
    result = await store.provision_claim(provision)
    replay = await store.provision_claim(provision)
    assert replay == result
    assert await store.claim_invitation(claim_request) == claim
    assert await store.approve_claim(approval) == pending
    assert result.credentials == replay.credentials
    assert result.credentials.snapshot.secret != result.credentials.dispatch.secret
    assert result.credentials.dispatch.secret != result.credentials.management.secret
    assert result.credentials.snapshot.secret not in repr(result)

    with sqlite3.connect(metadata_path) as conn:
        row = conn.execute(
            """
            SELECT snapshot_ref, dispatch_ref, management_ref
            FROM credential_generations
            """
        ).fetchone()
        assert len(set(row)) == 3
    metadata = metadata_path.read_bytes()
    for secret in (
        invitation.pairing_secret,
        result.credentials.snapshot.secret,
        result.credentials.dispatch.secret,
        result.credentials.management.secret,
    ):
        assert secret.encode() not in metadata


@pytest.mark.asyncio
async def test_provision_requires_bound_secret_on_every_replay(tmp_path: Path) -> None:
    _, store, _, _, metadata_path = await _stores(tmp_path)
    invitation, claim, _ = await _issue_and_claim(store)
    await _approve(store, claim)
    request_id = _uuid()
    wrong = ProvisionRequest(
        request_id=request_id,
        claim_id=claim.claim_id,
        pairing_secret="wrong-provision-secret",
    )
    with pytest.raises(PairingStoreTerminalError) as rejected:
        await store.provision_claim(wrong)
    assert rejected.value.code == "pairing_claim_rejected"
    with sqlite3.connect(metadata_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM credential_generations"
        ).fetchone()[0] == 0

    correct = replace(wrong, pairing_secret=invitation.pairing_secret)
    provisioned = await store.provision_claim(correct)
    with pytest.raises(PairingStoreTerminalError) as replay_rejected:
        await store.provision_claim(wrong)
    assert replay_rejected.value.code == "pairing_claim_rejected"
    assert await store.provision_claim(correct) == provisioned
    assert "pairing_secret" not in repr(correct)


@pytest.mark.asyncio
async def test_reject_is_terminal_idempotent_and_destroys_private_material(
    tmp_path: Path,
) -> None:
    secret_store, store, _, _, _ = await _stores(tmp_path)
    _, claim, _ = await _issue_and_claim(store)
    request_id = _uuid()
    assert await store.reject_claim(request_id=request_id, claim_id=claim.claim_id)
    assert await store.reject_claim(request_id=request_id, claim_id=claim.claim_id)
    invitation = await store.invitation(claim.invitation_id)
    assert invitation is not None and invitation.state == "rejected"
    assert await store.pending_claims() == ()
    assert await secret_store.load_pairing_material(
        claim.pairing_id,
        claim.invitation_id,
    ) is None
    with pytest.raises(PairingStoreTerminalError) as terminal:
        await store.approve_claim(
            ApprovalRequest(
                request_id=_uuid(),
                claim_id=claim.claim_id,
                locator="https://mac-a.example.internal:1240",
            )
        )
    assert terminal.value.code == "pairing_claim_terminal"


@pytest.mark.asyncio
async def test_enrollment_pending_disabled_active_and_revoked_lifecycle(
    tmp_path: Path,
) -> None:
    secret_store, store, _, _, _ = await _stores(tmp_path)
    invitation, claim, _ = await _issue_and_claim(store)
    pending = await _approve(store, claim, hub_enabled=False)
    provisioned = await store.provision_claim(
        ProvisionRequest(_uuid(), claim.claim_id, invitation.pairing_secret)
    )
    generation = provisioned.credential_generation
    assert pending.state == "pending"
    await store.mark_activating(
        request_id=_uuid(),
        pairing_id=claim.pairing_id,
        generation=generation,
    )
    disabled = await store.activate_enrollment(
        request_id=_uuid(),
        pairing_id=claim.pairing_id,
        generation=generation,
    )
    assert disabled.state == "disabled"
    assert not disabled.routable
    assert await store.enrollment_binding(claim.pairing_id) is None
    active_binding = await store.active_binding(claim.pairing_id, generation)
    assert active_binding is not None
    assert active_binding.credential_generation == generation
    assert provisioned.credentials.management.secret_ref not in repr(active_binding)

    active = await store.set_hub_enabled(
        request_id=_uuid(),
        pairing_id=claim.pairing_id,
        enabled=True,
    )
    assert active.state == "active" and active.routable
    binding = await store.enrollment_binding(claim.pairing_id)
    assert binding is not None
    assert provisioned.credentials.snapshot.secret_ref not in repr(binding)

    disabled_again = await store.set_hub_enabled(
        request_id=_uuid(),
        pairing_id=claim.pairing_id,
        enabled=False,
    )
    assert disabled_again.state == "disabled"
    assert await store.active_binding(claim.pairing_id, generation) is not None
    revoked_request = _uuid()
    revoked = await store.revoke_enrollment(
        request_id=revoked_request,
        pairing_id=claim.pairing_id,
    )
    assert revoked.state == "revoked"
    assert not revoked.routable
    assert await store.enrollment_binding(claim.pairing_id) is None
    assert await store.active_binding(claim.pairing_id, generation) is None
    assert await secret_store.load_bundle(claim.pairing_id, generation) is None
    with pytest.raises(SecretStoreIntegrityError):
        await secret_store.load_locator(
            claim.pairing_id,
            _locator_ref(store._path, claim.pairing_id),
        )
    assert (
        await store.revoke_enrollment(
            request_id=revoked_request,
            pairing_id=claim.pairing_id,
        )
    ).state == "revoked"


@pytest.mark.asyncio
async def test_self_management_is_exact_and_revoke_replay_is_tombstoned(
    tmp_path: Path,
) -> None:
    secret_store, store, _, secret_path, metadata_path = await _stores(tmp_path)
    invitation, claim, _ = await _issue_and_claim(store)
    await _approve(store, claim, hub_enabled=True)
    provisioned = await store.provision_claim(
        ProvisionRequest(_uuid(), claim.claim_id, invitation.pairing_secret)
    )
    generation = provisioned.credential_generation
    await store.mark_activating(
        request_id=_uuid(),
        pairing_id=claim.pairing_id,
        generation=generation,
    )
    await store.activate_enrollment(
        request_id=_uuid(),
        pairing_id=claim.pairing_id,
        generation=generation,
    )

    disable_request = _uuid()
    disabled = await store.self_disable_enrollment(
        request_id=disable_request,
        pairing_id=claim.pairing_id,
        reporting_node_id="metis",
        credential_generation=generation,
    )
    assert disabled.state == "disabled"
    assert await store.self_disable_enrollment(
        request_id=disable_request,
        pairing_id=claim.pairing_id,
        reporting_node_id="metis",
        credential_generation=generation,
    ) == disabled
    # A separate lifecycle attempt can safely observe an already-disabled
    # active pairing and record its own idempotent result.
    assert (
        await store.self_disable_enrollment(
            request_id=_uuid(),
            pairing_id=claim.pairing_id,
            reporting_node_id="metis",
            credential_generation=generation,
        )
    ).state == "disabled"

    for node_id, stale_generation in (
        ("other-node", generation),
        ("metis", generation + 1),
    ):
        with pytest.raises(PairingStoreTerminalError) as rejected:
            await store.self_disable_enrollment(
                request_id=_uuid(),
                pairing_id=claim.pairing_id,
                reporting_node_id=node_id,
                credential_generation=stale_generation,
            )
        assert rejected.value.code == (
            "pairing_management_authentication_rejected"
        )

    revoke_request = _uuid()
    verifier = "a" * 64
    revoked = await store.self_revoke_enrollment(
        request_id=revoke_request,
        pairing_id=claim.pairing_id,
        reporting_node_id="metis",
        credential_generation=generation,
        management_bearer_verifier=verifier,
    )
    assert revoked.state == "revoked"
    assert await secret_store.load_bundle(claim.pairing_id, generation) is None
    assert await store.self_revoke_replay(
        request_id=revoke_request,
        pairing_id=claim.pairing_id,
        reporting_node_id="metis",
        credential_generation=generation,
        management_bearer_verifier=verifier,
    ) == revoked
    assert await store.self_revoke_replay(
        request_id=revoke_request,
        pairing_id=claim.pairing_id,
        reporting_node_id="metis",
        credential_generation=generation,
        management_bearer_verifier="b" * 64,
    ) is None
    with pytest.raises(PairingStoreTerminalError):
        await store.self_revoke_enrollment(
            request_id=revoke_request,
            pairing_id=claim.pairing_id,
            reporting_node_id="metis",
            credential_generation=generation,
            management_bearer_verifier="b" * 64,
        )
    assert await store.self_revoke_replay(
        request_id=revoke_request,
        pairing_id=claim.pairing_id,
        reporting_node_id="other-node",
        credential_generation=generation,
        management_bearer_verifier=verifier,
    ) is None
    with pytest.raises(PairingStoreTerminalError):
        await store.self_revoke_enrollment(
            request_id=_uuid(),
            pairing_id=claim.pairing_id,
            reporting_node_id="metis",
            credential_generation=generation,
            management_bearer_verifier=verifier,
        )

    metadata = metadata_path.read_bytes()
    for secret in (
        provisioned.credentials.snapshot.secret,
        provisioned.credentials.dispatch.secret,
        provisioned.credentials.management.secret,
    ):
        assert secret.encode() not in metadata
    with sqlite3.connect(metadata_path) as connection:
        assert connection.execute(
            "SELECT lifecycle_state, hub_enabled FROM enrollments"
        ).fetchone() == ("revoked", 0)
        assert {
            row[0]
            for row in connection.execute(
                "SELECT state FROM credential_generations"
            ).fetchall()
        } == {"revoked"}

    restarted_secrets = SecretStore(
        secret_path,
        store_id="nyx-pairing-secrets",
        master_key=_master_key(),
    )
    await restarted_secrets.initialize()
    restarted = PairingStore(
        metadata_path,
        store_id="nyx-pairing-metadata",
        secret_store=restarted_secrets,
    )
    await restarted.initialize()
    assert await restarted.self_revoke_replay(
        request_id=revoke_request,
        pairing_id=claim.pairing_id,
        reporting_node_id="metis",
        credential_generation=generation,
        management_bearer_verifier=verifier,
    ) == revoked


@pytest.mark.asyncio
async def test_candidate_binding_resumes_exact_pending_generation_after_restart(
    tmp_path: Path,
) -> None:
    secret_store, store, clock, _, metadata_path = await _stores(tmp_path)
    invitation, claim, _ = await _issue_and_claim(store)
    await _approve(store, claim, hub_enabled=True)
    provisioned = await store.provision_claim(
        ProvisionRequest(_uuid(), claim.claim_id, invitation.pairing_secret)
    )
    generation = provisioned.credential_generation

    candidate = await store.candidate_binding(claim.pairing_id, generation)
    assert candidate is not None
    assert candidate.credential_generation == generation
    assert provisioned.credentials.management.secret_ref not in repr(candidate)
    assert await store.candidate_binding(claim.pairing_id, generation + 1) is None
    assert await store.enrollment_binding(claim.pairing_id) is None

    await store.mark_activating(
        request_id=_uuid(),
        pairing_id=claim.pairing_id,
        generation=generation,
    )
    restarted = PairingStore(
        metadata_path,
        store_id="nyx-pairing-metadata",
        secret_store=secret_store,
        clock=clock,
    )
    await restarted.initialize()
    assert await restarted.candidate_binding(claim.pairing_id, generation) == candidate

    active = await restarted.activate_enrollment(
        request_id=_uuid(),
        pairing_id=claim.pairing_id,
        generation=generation,
    )
    assert active.routable
    assert await restarted.candidate_binding(claim.pairing_id, generation) is None
    assert await restarted.enrollment_binding(claim.pairing_id) == candidate


@pytest.mark.asyncio
async def test_restart_reconciles_committed_private_and_generation_records(
    tmp_path: Path,
) -> None:
    secret_store, store, clock, _, metadata_path = await _stores(tmp_path)
    interrupted_issue_request = _invitation_request(
        locator="https://mac-recovery.example.internal:1240"
    )
    interrupted_issue = await store.issue_invitation(interrupted_issue_request)
    with sqlite3.connect(metadata_path) as conn:
        conn.execute(
            "UPDATE invitations SET state='preparing' WHERE invitation_id=?",
            (interrupted_issue.invitation_id,),
        )
    issue_restart = PairingStore(
        metadata_path,
        store_id="nyx-pairing-metadata",
        secret_store=secret_store,
        clock=clock,
    )
    issue_report = await issue_restart.initialize()
    assert issue_report.finalized_invitations == 1
    assert await issue_restart.issue_invitation(interrupted_issue_request) == interrupted_issue

    invitation, claim, _ = await _issue_and_claim(store)
    await _approve(store, claim)
    provision_request = ProvisionRequest(
        _uuid(), claim.claim_id, invitation.pairing_secret
    )
    provisioned = await store.provision_claim(provision_request)
    generation = provisioned.credential_generation

    with sqlite3.connect(metadata_path) as conn:
        conn.execute(
            """
            UPDATE credential_generations SET state='allocating'
            WHERE pairing_id=? AND generation=?
            """,
            (claim.pairing_id, generation),
        )
        conn.execute(
            """
            UPDATE enrollments SET credential_generation=NULL
            WHERE pairing_id=?
            """,
            (claim.pairing_id,),
        )
        conn.execute(
            "UPDATE invitations SET state='approved' WHERE invitation_id=?",
            (invitation.invitation_id,),
        )

    restarted = PairingStore(
        metadata_path,
        store_id="nyx-pairing-metadata",
        secret_store=secret_store,
        clock=clock,
    )
    report = await restarted.initialize()
    assert report.finalized_generations == 1
    recovered = await restarted.enrollment(claim.pairing_id)
    assert recovered is not None
    assert recovered.credential_generation == generation
    assert recovered.failure_code is None

    await secret_store.delete_bundle(claim.pairing_id, generation)
    failed_restart = PairingStore(
        metadata_path,
        store_id="nyx-pairing-metadata",
        secret_store=secret_store,
        clock=clock,
    )
    failed_report = await failed_restart.initialize()
    assert failed_report.failed_closed >= 1
    failed = await failed_restart.enrollment(claim.pairing_id)
    assert failed is not None
    assert failed.state == "pending"
    assert failed.failure_code == "secret_reconciliation_failed"
    assert not failed.routable
    with pytest.raises(PairingStoreIntegrityError) as no_regeneration:
        await failed_restart.provision_claim(provision_request)
    assert no_regeneration.value.code == "pairing_reconciliation_failed"
    assert await secret_store.load_bundle(claim.pairing_id, generation) is None


@pytest.mark.asyncio
async def test_strict_bounds_and_sanitized_records(tmp_path: Path) -> None:
    _, store, _, _, _ = await _stores(tmp_path)
    with pytest.raises(PairingStoreValidationError):
        await store.issue_invitation(
            _invitation_request(request_id=_uuid().upper())
        )
    with pytest.raises(PairingStoreValidationError):
        await store.issue_invitation(
            _invitation_request(expires=float("nan"))
        )
    invitation = await store.issue_invitation(_invitation_request())
    with pytest.raises(PairingStoreValidationError):
        await store.claim_invitation(
            ClaimRequest(
                request_id=_uuid(),
                invitation_id=invitation.invitation_id,
                pairing_secret=invitation.pairing_secret,
                locator="https://mac-a.example.internal:1240",
                display_name="unsafe\nname",
                reporting_node_id="metis",
            )
        )
    record = await store.invitation(invitation.invitation_id)
    assert record is not None
    assert record.pairing_id is None
    rendered = repr(record)
    assert "https://" not in rendered
    assert invitation.pairing_secret not in rendered
    assert "ref" not in rendered


@pytest.mark.asyncio
async def test_pairing_metadata_refuses_an_existing_fleet_database(
    tmp_path: Path,
) -> None:
    secret_store, _, clock, _, _ = await _stores(tmp_path)
    fleet_path = tmp_path / "other-private" / "fleet.db"
    fleet_path.parent.mkdir(mode=0o700)
    fleet_path.touch(mode=0o600)
    with sqlite3.connect(fleet_path) as conn:
        conn.execute("CREATE TABLE routes(route_id TEXT PRIMARY KEY)")
    unrelated = PairingStore(
        fleet_path,
        store_id="nyx-pairing-metadata",
        secret_store=secret_store,
        clock=clock,
    )
    with pytest.raises(PairingStoreIntegrityError) as rejected:
        await unrelated.initialize()
    assert rejected.value.code == "pairing_store_identity_mismatch"


def _pairing_id(path: Path, invitation_id: str) -> str:
    with sqlite3.connect(path) as conn:
        return str(
            conn.execute(
                "SELECT pairing_id FROM invitations WHERE invitation_id=?",
                (invitation_id,),
            ).fetchone()[0]
        )


def _locator_ref(path: Path, pairing_id: str) -> str:
    with sqlite3.connect(path) as conn:
        return str(
            conn.execute(
                "SELECT locator_ref FROM enrollments WHERE pairing_id=?",
                (pairing_id,),
            ).fetchone()[0]
        )
