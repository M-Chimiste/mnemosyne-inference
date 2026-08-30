from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import struct

import pytest

from mnemosyne_macos.lifecycle_execution_protocol import (
    LifecycleEffectKind,
    LifecycleEffectObservation,
    LifecycleEffectReceiptStatus,
    LifecycleExecutionApplyV2,
    LifecycleExecutionDirection,
    LifecycleExecutionFinalizeV2,
    LifecycleExecutionMessageType,
    LifecycleExecutionObserveV2,
    LifecycleExecutionProtocolError,
    LifecycleExecutionRefusedV2,
    LifecycleExecutionStartV2,
    LifecycleRunnerRegistrationV2,
    decode_lifecycle_execution_frame,
    encode_lifecycle_execution_message,
)
from mnemosyne_macos.native_lifecycle import (
    NativeLifecycleConflictError,
    NativeLifecycleIntegrityError,
    NativeLifecycleJournal,
    RecoveryObservation,
    PriorEffectsState,
    LifecycleRecoveryAction,
    decide_lifecycle_recovery,
)
from mnemosyne_macos.native_lifecycle_recovery_worker import (
    _journal_from_state_anchor_descriptor,
    process_one_frame,
)

from test_native_lifecycle import (
    _digest,
    _execution_manifest_for,
    _helper_proof_authority,
    _helper_submission,
    _migration,
    _plan,
    _uuid,
)


class MutableClock:
    def __init__(self, value: float = 1_788_100_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _authorized(tmp_path: Path, clock: MutableClock, *, plan=None):
    config = tmp_path / "private" / "config.yaml"
    journal = NativeLifecycleJournal(
        config,
        clock=clock,
        monotonic_clock=clock,
        boot_identity=lambda: "test-boot-session",
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    journal.initialize()
    transaction = journal.prepare(plan or _plan()).transaction
    manifest = _execution_manifest_for(journal, transaction)
    journal.record_helper_staged(manifest)
    challenge = journal.issue_helper_authorization_challenge(
        transaction.transaction_id,
        lifetime_seconds=20,
    ).challenge
    transaction = journal.accept_helper_authorization_receipt(
        _helper_submission(challenge)
    ).transaction
    authorization_digest = challenge.authorization_digest.removeprefix(
        "sha256:"
    )
    session_id = challenge.session_id
    return config, journal, transaction, manifest, authorization_digest, session_id


def _grant(tmp_path: Path, clock: MutableClock, *, plan=None):
    values = _authorized(tmp_path, clock, plan=plan)
    config, journal, transaction, manifest, authorization, session = values
    mutation = journal.create_execution_start_grant(
        transaction_id=transaction.transaction_id,
        direction=LifecycleExecutionDirection.FORWARD,
        authorization_digest=authorization,
        authorization_session_id=session,
    )
    return (*values, mutation.grant)


def _registration(grant, *, sequence: int = 1, nonce: str | None = None, session: str | None = None):
    return LifecycleRunnerRegistrationV2(
        protocol_version=2,
        message_type=LifecycleExecutionMessageType.REGISTER,
        transaction_id=grant.transaction_id,
        grant_id=grant.grant_id,
        grant_digest=f"sha256:{grant.grant_digest}",
        runner_session_id=session or _uuid(1_600),
        sequence=sequence,
        nonce=nonce or _uuid(1_601 + sequence),
        runner_identifier=grant.runner_identifier,
        runner_build_digest=f"sha256:{grant.runner_build_digest}",
        runner_identity_digest=f"sha256:{grant.runner_identity_digest}",
        team_identifier=grant.runner_team_identifier,
        code_requirement_digest=f"sha256:{grant.runner_code_requirement_digest}",
        requested_lease_seconds=60,
    )


def test_protocol_v2_round_trips_every_closed_message_without_path_fields():
    common = {
        "protocol_version": 2,
        "transaction_id": _uuid(1),
        "grant_id": _uuid(2),
        "grant_digest": f"sha256:{_digest(3)}",
        "runner_session_id": _uuid(4),
        "lease_id": _uuid(5),
        "lease_epoch": 1,
        "sequence": 2,
        "nonce": _uuid(6),
    }
    messages = (
        LifecycleExecutionStartV2(
            message_type=LifecycleExecutionMessageType.START,
            direction=LifecycleExecutionDirection.FORWARD,
            execution_manifest_digest=f"sha256:{_digest(7)}",
            recovery_clone_identity_digest=f"sha256:{_digest(8)}",
            authorization_digest=f"sha256:{_digest(9)}",
            authorization_session_id=_uuid(10),
            **common,
        ),
        LifecycleExecutionObserveV2(
            message_type=LifecycleExecutionMessageType.OBSERVE,
            effect_id=_uuid(11),
            effect_kind=LifecycleEffectKind.RESOLVE_EXCLUSIVE_WEIGHT,
            target_digest=f"sha256:{_digest(12)}",
            attempt=1,
            prior_receipt_digest=None,
            **common,
        ),
        LifecycleExecutionApplyV2(
            message_type=LifecycleExecutionMessageType.APPLY,
            effect_id=_uuid(11),
            effect_kind=LifecycleEffectKind.RESOLVE_EXCLUSIVE_WEIGHT,
            target_digest=f"sha256:{_digest(12)}",
            attempt=1,
            observation_receipt_digest=f"sha256:{_digest(13)}",
            **common,
        ),
        LifecycleExecutionFinalizeV2(
            message_type=LifecycleExecutionMessageType.FINALIZE,
            direction=LifecycleExecutionDirection.FORWARD,
            final_receipt_digest=f"sha256:{_digest(14)}",
            **common,
        ),
        LifecycleExecutionRefusedV2(
            protocol_version=2,
            message_type=LifecycleExecutionMessageType.REFUSED,
            transaction_id=_uuid(1),
            grant_id=_uuid(2),
            runner_session_id=_uuid(4),
            sequence=2,
            nonce=_uuid(6),
            request_nonce=_uuid(15),
            error_code="runner_adapter_unavailable",
        ),
    )
    forbidden = (b'"path"', b'"pid"', b'"port"', b'"label"', b'"argv"', b'"credential"')
    for message in messages:
        frame = encode_lifecycle_execution_message(message)
        assert decode_lifecycle_execution_frame(frame) == message
        assert all(item not in frame for item in forbidden)


def test_protocol_v2_rejects_unknown_duplicate_and_path_bearing_keys(tmp_path):
    clock = MutableClock()
    # A locally built grant supplies a completely valid registration shape.
    _, _, _, _, _, _, grant = _grant(tmp_path, clock)
    registration = _registration(grant)
    frame = encode_lifecycle_execution_message(registration)
    document = json.loads(frame[4:])
    document["path"] = "/tmp/not-authority"
    payload = json.dumps(document, separators=(",", ":")).encode()
    with pytest.raises(LifecycleExecutionProtocolError):
        decode_lifecycle_execution_frame(struct.pack(">I", len(payload)) + payload)

    raw = frame[4:].decode()
    duplicate = raw.replace(
        '"grant_id":', f'"grant_id":"{grant.grant_id}","grant_id":', 1
    ).encode()
    with pytest.raises(LifecycleExecutionProtocolError):
        decode_lifecycle_execution_frame(struct.pack(">I", len(duplicate)) + duplicate)


def test_start_grant_is_immutable_single_use_and_direction_bound(tmp_path):
    clock = MutableClock()
    _, journal, transaction, _, authorization, session, grant = _grant(
        tmp_path, clock
    )
    replay = journal.create_execution_start_grant(
        transaction_id=transaction.transaction_id,
        direction=LifecycleExecutionDirection.FORWARD,
        authorization_digest=authorization,
        authorization_session_id=session,
    )
    assert replay.replayed is True
    assert replay.grant == grant
    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_execution_fresh_authorization_required",
    ):
        journal.create_execution_start_grant(
            transaction_id=transaction.transaction_id,
            direction=LifecycleExecutionDirection.MANUAL_RECOVERY,
            authorization_digest=authorization,
            authorization_session_id=session,
        )


def test_start_grant_rejects_receipt_not_linked_to_consumed_challenge(
    tmp_path,
):
    clock = MutableClock()
    config, journal, transaction, manifest, _, _ = _authorized(
        tmp_path, clock
    )
    forged_digest = _digest(1_880)
    forged_session = _uuid(1_881)
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            """
            INSERT INTO native_lifecycle_authorization_receipts_v2 (
                transaction_id, authority_digest,
                execution_manifest_digest, helper_build_digest,
                authorization_digest, helper_session_id, expires_at,
                recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transaction.transaction_id,
                transaction.authority_digest,
                manifest.receipt.manifest_digest,
                manifest.helper.build_digest,
                forged_digest,
                forged_session,
                clock.value + 60,
                clock.value,
            ),
        )

    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_execution_fresh_authorization_required",
    ):
        journal.create_execution_start_grant(
            transaction_id=transaction.transaction_id,
            direction=LifecycleExecutionDirection.FORWARD,
            authorization_digest=forged_digest,
            authorization_session_id=forged_session,
        )
    journal.close()
    restarted = NativeLifecycleJournal(
        config,
        clock=clock,
        monotonic_clock=clock,
        boot_identity=lambda: "test-boot-session",
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    with pytest.raises(NativeLifecycleIntegrityError):
        restarted.initialize()


def test_exact_runner_lease_resumes_after_authorization_expiry_and_restart(tmp_path):
    clock = MutableClock()
    config, journal, _, _, _, _, grant = _grant(tmp_path, clock)
    registration = _registration(grant)
    fence = journal.acquire_runner_process_fence(grant.transaction_id)
    first = journal.register_lifecycle_runner(
        registration, process_fence=fence
    )
    assert first.replayed is False
    assert journal.register_lifecycle_runner(
        registration, process_fence=fence
    ).lease == first.lease

    clock.value += 61  # both helper authorization and the first lease expired
    fence.close()
    journal.close()
    restarted = NativeLifecycleJournal(
        config,
        clock=clock,
        monotonic_clock=clock,
        boot_identity=lambda: "test-boot-session",
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    restarted.initialize()
    resumed_registration = _registration(
        grant,
        session=_uuid(1_700),
        nonce=_uuid(1_701),
    )
    resumed_fence = restarted.acquire_runner_process_fence(
        grant.transaction_id
    )
    resumed = restarted.register_lifecycle_runner(
        resumed_registration, process_fence=resumed_fence
    )
    assert resumed.lease.lease_epoch == first.lease.lease_epoch + 1
    assert resumed.lease.prior_lease_id == first.lease.lease_id


def test_exact_runner_lease_renews_with_monotonic_epoch_before_expiry(tmp_path):
    clock = MutableClock()
    _, journal, _, _, _, _, grant = _grant(tmp_path, clock)
    first_registration = _registration(grant)
    fence = journal.acquire_runner_process_fence(grant.transaction_id)
    first = journal.register_lifecycle_runner(
        first_registration, process_fence=fence
    ).lease
    renewed = journal.register_lifecycle_runner(
        _registration(
            grant,
            sequence=2,
            nonce=_uuid(1_750),
            session=first_registration.runner_session_id,
        ),
        process_fence=fence,
    ).lease
    assert renewed.lease_epoch == first.lease_epoch + 1
    assert renewed.prior_lease_id == first.lease_id


def test_process_fence_and_monotonic_lease_survive_wall_clock_jump(tmp_path):
    wall = MutableClock()
    monotonic = MutableClock(10_000.0)
    config = tmp_path / "private" / "config.yaml"
    journal = NativeLifecycleJournal(
        config,
        clock=wall,
        monotonic_clock=monotonic,
        boot_identity=lambda: "test-boot-session",
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    journal.initialize()
    transaction = journal.prepare(_plan()).transaction
    manifest = _execution_manifest_for(journal, transaction)
    journal.record_helper_staged(manifest)
    challenge = journal.issue_helper_authorization_challenge(
        transaction.transaction_id,
        lifetime_seconds=20,
    ).challenge
    journal.accept_helper_authorization_receipt(_helper_submission(challenge))
    grant = journal.create_execution_start_grant(
        transaction_id=transaction.transaction_id,
        direction=LifecycleExecutionDirection.FORWARD,
        authorization_digest=challenge.authorization_digest.removeprefix(
            "sha256:"
        ),
        authorization_session_id=challenge.session_id,
    ).grant
    first_fence = journal.acquire_runner_process_fence(
        transaction.transaction_id
    )
    journal.register_lifecycle_runner(
        _registration(grant), process_fence=first_fence
    )

    wall.value += 1_000_000
    competing = NativeLifecycleJournal(
        config,
        clock=wall,
        monotonic_clock=monotonic,
        boot_identity=lambda: "test-boot-session",
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    competing.initialize()
    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_execution_process_fence_unavailable",
    ):
        competing.acquire_runner_process_fence(transaction.transaction_id)

    first_fence.close()
    replacement_fence = competing.acquire_runner_process_fence(
        transaction.transaction_id
    )
    replacement_registration = _registration(
        grant,
        session=_uuid(1_760),
        nonce=_uuid(1_761),
    )
    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_execution_lease_conflict",
    ):
        competing.register_lifecycle_runner(
            replacement_registration,
            process_fence=replacement_fence,
        )

    monotonic.value += 61
    resumed = competing.register_lifecycle_runner(
        replacement_registration,
        process_fence=replacement_fence,
    )
    assert resumed.lease.lease_epoch == 2


def test_expired_helper_receipt_cannot_mint_a_new_start_grant(tmp_path):
    clock = MutableClock()
    _, journal, transaction, _, authorization, session = _authorized(
        tmp_path, clock
    )
    clock.value += 21
    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_helper_authority_expired",
    ):
        journal.create_execution_start_grant(
            transaction_id=transaction.transaction_id,
            direction=LifecycleExecutionDirection.FORWARD,
            authorization_digest=authorization,
            authorization_session_id=session,
        )


def test_registration_replay_and_runner_identity_tampering_fail_closed(tmp_path):
    clock = MutableClock()
    _, journal, _, _, _, _, grant = _grant(tmp_path, clock)
    registration = _registration(grant)
    fence = journal.acquire_runner_process_fence(grant.transaction_id)
    journal.register_lifecycle_runner(
        registration, process_fence=fence
    )
    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_execution_registration_replayed",
    ):
        journal.register_lifecycle_runner(
            replace(registration, grant_digest=f"sha256:{_digest(9_999)}"),
            process_fence=fence,
        )
    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_execution_runner_identity_mismatch",
    ):
        journal.register_lifecycle_runner(
            replace(
                registration,
                nonce=_uuid(1_999),
                runner_build_digest=f"sha256:{_digest(9_998)}",
            ),
            process_fence=fence,
        )


def test_effect_receipt_rejects_target_not_in_immutable_effect_graph(tmp_path):
    clock = MutableClock()
    _, journal, transaction, _, _, _, grant = _grant(tmp_path, clock)
    fence = journal.acquire_runner_process_fence(grant.transaction_id)
    lease = journal.register_lifecycle_runner(
        _registration(grant), process_fence=fence
    ).lease
    target = journal.authorized_lifecycle_effects(
        transaction.transaction_id,
        direction=grant.direction,
    )[0]

    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_effect_authority_mismatch",
    ):
        journal.record_lifecycle_effect_receipt(
            process_fence=fence,
            transaction_id=transaction.transaction_id,
            grant_id=grant.grant_id,
            lease_id=lease.lease_id,
            lease_epoch=lease.lease_epoch,
            sequence=1,
            request_nonce=_uuid(1_910),
            effect_id=target.effect_id,
            effect_kind=target.effect_kind,
            target_digest=_digest(1_911),
            attempt=1,
            status=LifecycleEffectReceiptStatus.OBSERVED,
            observation=LifecycleEffectObservation.NEEDS_ACTION,
            after_observation_digest=_digest(1_912),
        )


def test_effect_graph_rejects_later_target_before_prior_is_finalized(tmp_path):
    clock = MutableClock()
    _, journal, transaction, _, _, _, grant = _grant(tmp_path, clock)
    fence = journal.acquire_runner_process_fence(grant.transaction_id)
    lease = journal.register_lifecycle_runner(
        _registration(grant), process_fence=fence
    ).lease
    targets = journal.authorized_lifecycle_effects(
        transaction.transaction_id,
        direction=grant.direction,
    )

    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_effect_receipt_out_of_order",
    ):
        journal.record_lifecycle_effect_receipt(
            process_fence=fence,
            transaction_id=transaction.transaction_id,
            grant_id=grant.grant_id,
            lease_id=lease.lease_id,
            lease_epoch=lease.lease_epoch,
            sequence=1,
            request_nonce=_uuid(1_913),
            effect_id=targets[1].effect_id,
            effect_kind=targets[1].effect_kind,
            target_digest=targets[1].target_digest,
            attempt=1,
            status=LifecycleEffectReceiptStatus.OBSERVED,
            observation=LifecycleEffectObservation.NEEDS_ACTION,
            after_observation_digest=_digest(1_914),
        )
    assert journal.list_lifecycle_effect_receipts(
        transaction.transaction_id
    ) == ()


def test_restart_rejects_valid_target_receipt_that_skips_graph_order(tmp_path):
    clock = MutableClock()
    config, journal, transaction, _, _, _, grant = _grant(tmp_path, clock)
    fence = journal.acquire_runner_process_fence(grant.transaction_id)
    lease = journal.register_lifecycle_runner(
        _registration(grant), process_fence=fence
    ).lease
    targets = journal.authorized_lifecycle_effects(
        transaction.transaction_id,
        direction=grant.direction,
    )
    receipt = journal.record_lifecycle_effect_receipt(
        process_fence=fence,
        transaction_id=transaction.transaction_id,
        grant_id=grant.grant_id,
        lease_id=lease.lease_id,
        lease_epoch=lease.lease_epoch,
        sequence=1,
        request_nonce=_uuid(1_915),
        effect_id=targets[0].effect_id,
        effect_kind=targets[0].effect_kind,
        target_digest=targets[0].target_digest,
        attempt=1,
        status=LifecycleEffectReceiptStatus.OBSERVED,
        observation=LifecycleEffectObservation.NEEDS_ACTION,
        after_observation_digest=_digest(1_916),
    ).receipt
    corrupted = replace(
        receipt,
        effect_id=targets[1].effect_id,
        effect_kind=targets[1].effect_kind,
        target_digest=targets[1].target_digest,
        receipt_digest="",
    )
    binding = {
        "schema_version": 2,
        "receipt_id": corrupted.receipt_id,
        "transaction_id": corrupted.transaction_id,
        "grant_id": corrupted.grant_id,
        "lease_id": corrupted.lease_id,
        "lease_epoch": corrupted.lease_epoch,
        "sequence": corrupted.sequence,
        "request_nonce": corrupted.request_nonce,
        "effect_id": corrupted.effect_id,
        "effect_kind": corrupted.effect_kind.value,
        "target_digest": corrupted.target_digest,
        "attempt": corrupted.attempt,
        "status": corrupted.status.value,
        "observation": corrupted.observation.value,
        "before_observation_digest": corrupted.before_observation_digest,
        "after_observation_digest": corrupted.after_observation_digest,
        "prior_receipt_digest": corrupted.prior_receipt_digest,
        "fixed_error_code": corrupted.fixed_error_code,
        "recorded_at": corrupted.recorded_at,
    }
    corrupted_digest = sha256(
        json.dumps(
            binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    fence.close()
    journal.close()
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "DROP TRIGGER native_lifecycle_effect_receipts_v2_immutable"
        )
        connection.execute(
            """
            UPDATE native_lifecycle_effect_receipts_v2
               SET effect_id = ?, effect_kind = ?, target_digest = ?,
                   receipt_digest = ?
             WHERE receipt_id = ?
            """,
            (
                corrupted.effect_id,
                corrupted.effect_kind.value,
                corrupted.target_digest,
                corrupted_digest,
                corrupted.receipt_id,
            ),
        )

    restarted = NativeLifecycleJournal(
        config,
        clock=clock,
        monotonic_clock=clock,
        boot_identity=lambda: "test-boot-session",
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    with pytest.raises(NativeLifecycleIntegrityError):
        restarted.initialize()


def test_effect_chain_cannot_reset_across_fresh_grants(tmp_path):
    clock = MutableClock()
    _, journal, transaction, _, _, _, first_grant = _grant(tmp_path, clock)
    first_fence = journal.acquire_runner_process_fence(
        first_grant.transaction_id
    )
    first_lease = journal.register_lifecycle_runner(
        _registration(first_grant), process_fence=first_fence
    ).lease
    target = journal.authorized_lifecycle_effects(
        transaction.transaction_id,
        direction=first_grant.direction,
    )[0]
    first = journal.record_lifecycle_effect_receipt(
        process_fence=first_fence,
        transaction_id=transaction.transaction_id,
        grant_id=first_grant.grant_id,
        lease_id=first_lease.lease_id,
        lease_epoch=first_lease.lease_epoch,
        sequence=1,
        request_nonce=_uuid(1_920),
        effect_id=target.effect_id,
        effect_kind=target.effect_kind,
        target_digest=target.target_digest,
        attempt=1,
        status=LifecycleEffectReceiptStatus.OBSERVED,
        observation=LifecycleEffectObservation.NEEDS_ACTION,
        after_observation_digest=_digest(1_921),
    ).receipt

    clock.value += 61
    first_fence.close()
    challenge = journal.issue_helper_authorization_challenge(
        transaction.transaction_id,
        lifetime_seconds=20,
    ).challenge
    journal.accept_helper_authorization_receipt(_helper_submission(challenge))
    second_grant = journal.create_execution_start_grant(
        transaction_id=transaction.transaction_id,
        direction=LifecycleExecutionDirection.FORWARD,
        authorization_digest=challenge.authorization_digest.removeprefix(
            "sha256:"
        ),
        authorization_session_id=challenge.session_id,
    ).grant
    second_fence = journal.acquire_runner_process_fence(
        second_grant.transaction_id
    )
    second_lease = journal.register_lifecycle_runner(
        _registration(
            second_grant,
            session=_uuid(1_922),
            nonce=_uuid(1_923),
        ),
        process_fence=second_fence,
    ).lease

    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_effect_receipt_out_of_order",
    ):
        journal.record_lifecycle_effect_receipt(
            process_fence=second_fence,
            transaction_id=transaction.transaction_id,
            grant_id=second_grant.grant_id,
            lease_id=second_lease.lease_id,
            lease_epoch=second_lease.lease_epoch,
            sequence=1,
            request_nonce=_uuid(1_924),
            effect_id=target.effect_id,
            effect_kind=target.effect_kind,
            target_digest=target.target_digest,
            attempt=1,
            status=LifecycleEffectReceiptStatus.APPLY_STARTED,
            observation=LifecycleEffectObservation.NEEDS_ACTION,
            before_observation_digest=first.after_observation_digest,
            prior_receipt_digest=first.receipt_digest,
        )

    continued = journal.record_lifecycle_effect_receipt(
        process_fence=second_fence,
        transaction_id=transaction.transaction_id,
        grant_id=second_grant.grant_id,
        lease_id=second_lease.lease_id,
        lease_epoch=second_lease.lease_epoch,
        sequence=2,
        request_nonce=_uuid(1_925),
        effect_id=target.effect_id,
        effect_kind=target.effect_kind,
        target_digest=target.target_digest,
        attempt=1,
        status=LifecycleEffectReceiptStatus.APPLY_STARTED,
        observation=LifecycleEffectObservation.NEEDS_ACTION,
        before_observation_digest=first.after_observation_digest,
        prior_receipt_digest=first.receipt_digest,
    ).receipt
    assert continued.grant_id == second_grant.grant_id
    assert continued.prior_receipt_digest == first.receipt_digest


def test_effect_authority_is_strictly_grant_direction_specific(tmp_path):
    clock = MutableClock()
    _, journal, transaction, _, authorization, session, forward_grant = (
        _grant(tmp_path, clock, plan=_migration())
    )
    forward_targets = journal.authorized_lifecycle_effects(
        transaction.transaction_id,
        direction=LifecycleExecutionDirection.FORWARD,
    )
    rollback_targets = journal.authorized_lifecycle_effects(
        transaction.transaction_id,
        direction=LifecycleExecutionDirection.ROLLBACK,
    )
    assert all(
        item.effect_kind is not LifecycleEffectKind.RESTORE_PREDECESSOR
        for item in forward_targets
    )
    assert all(
        item.effect_kind
        not in {
            LifecycleEffectKind.PREFLIGHT_CANDIDATE,
            LifecycleEffectKind.INSTALL_CANDIDATE,
            LifecycleEffectKind.COMMIT_CANDIDATE,
        }
        for item in rollback_targets
    )
    rollback_target = next(
        item
        for item in rollback_targets
        if item.effect_kind is LifecycleEffectKind.RESTORE_PREDECESSOR
    )

    forward_fence = journal.acquire_runner_process_fence(
        forward_grant.transaction_id
    )
    forward_lease = journal.register_lifecycle_runner(
        _registration(forward_grant), process_fence=forward_fence
    ).lease
    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_effect_authority_mismatch",
    ):
        journal.record_lifecycle_effect_receipt(
            process_fence=forward_fence,
            transaction_id=transaction.transaction_id,
            grant_id=forward_grant.grant_id,
            lease_id=forward_lease.lease_id,
            lease_epoch=forward_lease.lease_epoch,
            sequence=1,
            request_nonce=_uuid(1_930),
            effect_id=rollback_target.effect_id,
            effect_kind=rollback_target.effect_kind,
            target_digest=rollback_target.target_digest,
            attempt=1,
            status=LifecycleEffectReceiptStatus.OBSERVED,
            observation=LifecycleEffectObservation.NEEDS_ACTION,
            after_observation_digest=_digest(1_931),
        )

    forward_fence.close()
    clock.value += 61
    for phase in (
        "preflighted",
        "drained",
        "snapshotted",
        "predecessor_stopped",
    ):
        transaction = journal.advance(
            transaction.transaction_id, phase
        ).transaction
    claim_id = _uuid(1_932)
    journal.acquire_execution_claim(
        transaction_id=transaction.transaction_id,
        authority_digest=transaction.authority_digest,
        helper_session_id=session,
        claim_id=claim_id,
        expires_at=clock.value + 120,
        now=clock.value,
    )
    journal.request_rollback(
        transaction_id=transaction.transaction_id,
        authority_digest=transaction.authority_digest,
        helper_session_id=session,
        claim_id=claim_id,
    )
    assert journal.release_execution_claim(
        transaction_id=transaction.transaction_id,
        authority_digest=transaction.authority_digest,
        helper_session_id=session,
        claim_id=claim_id,
    )
    rollback_challenge = journal.issue_helper_authorization_challenge(
        transaction.transaction_id,
        lifetime_seconds=20,
    ).challenge
    journal.accept_helper_authorization_receipt(
        _helper_submission(rollback_challenge)
    )
    rollback_grant = journal.create_execution_start_grant(
        transaction_id=transaction.transaction_id,
        direction=LifecycleExecutionDirection.ROLLBACK,
        authorization_digest=(
            rollback_challenge.authorization_digest.removeprefix("sha256:")
        ),
        authorization_session_id=rollback_challenge.session_id,
    ).grant
    rollback_fence = journal.acquire_runner_process_fence(
        rollback_grant.transaction_id
    )
    rollback_lease = journal.register_lifecycle_runner(
        _registration(
            rollback_grant,
            session=_uuid(1_933),
            nonce=_uuid(1_934),
        ),
        process_fence=rollback_fence,
    ).lease
    forward_target = next(
        item
        for item in forward_targets
        if item.effect_kind is LifecycleEffectKind.PREFLIGHT_CANDIDATE
    )
    with pytest.raises(
        NativeLifecycleConflictError,
        match="native_lifecycle_effect_authority_mismatch",
    ):
        journal.record_lifecycle_effect_receipt(
            process_fence=rollback_fence,
            transaction_id=transaction.transaction_id,
            grant_id=rollback_grant.grant_id,
            lease_id=rollback_lease.lease_id,
            lease_epoch=rollback_lease.lease_epoch,
            sequence=1,
            request_nonce=_uuid(1_935),
            effect_id=forward_target.effect_id,
            effect_kind=forward_target.effect_kind,
            target_digest=forward_target.target_digest,
            attempt=1,
            status=LifecycleEffectReceiptStatus.OBSERVED,
            observation=LifecycleEffectObservation.NEEDS_ACTION,
            after_observation_digest=_digest(1_936),
        )
def test_partial_target_receipts_survive_restart_and_not_ready_is_retryable(tmp_path):
    clock = MutableClock()
    config, journal, transaction, _, _, _, grant = _grant(tmp_path, clock)
    fence = journal.acquire_runner_process_fence(grant.transaction_id)
    lease = journal.register_lifecycle_runner(
        _registration(grant), process_fence=fence
    ).lease
    targets = journal.authorized_lifecycle_effects(
        transaction.transaction_id,
        direction=grant.direction,
    )
    first_target, second_target = targets[:2]
    first = journal.record_lifecycle_effect_receipt(
        process_fence=fence,
        transaction_id=transaction.transaction_id,
        grant_id=grant.grant_id,
        lease_id=lease.lease_id,
        lease_epoch=lease.lease_epoch,
        sequence=1,
        request_nonce=_uuid(2_001),
        effect_id=first_target.effect_id,
        effect_kind=first_target.effect_kind,
        target_digest=first_target.target_digest,
        attempt=1,
        status=LifecycleEffectReceiptStatus.OBSERVED,
        observation=LifecycleEffectObservation.NEEDS_ACTION,
        after_observation_digest=_digest(2_300),
    ).receipt
    started = journal.record_lifecycle_effect_receipt(
        process_fence=fence,
        transaction_id=transaction.transaction_id,
        grant_id=grant.grant_id,
        lease_id=lease.lease_id,
        lease_epoch=lease.lease_epoch,
        sequence=2,
        request_nonce=_uuid(2_003),
        effect_id=first.effect_id,
        effect_kind=first.effect_kind,
        target_digest=first.target_digest,
        attempt=1,
        status=LifecycleEffectReceiptStatus.APPLY_STARTED,
        observation=LifecycleEffectObservation.NEEDS_ACTION,
        before_observation_digest=first.after_observation_digest,
        prior_receipt_digest=first.receipt_digest,
    ).receipt
    satisfied = journal.record_lifecycle_effect_receipt(
        process_fence=fence,
        transaction_id=transaction.transaction_id,
        grant_id=grant.grant_id,
        lease_id=lease.lease_id,
        lease_epoch=lease.lease_epoch,
        sequence=3,
        request_nonce=_uuid(2_004),
        effect_id=first.effect_id,
        effect_kind=first.effect_kind,
        target_digest=first.target_digest,
        attempt=1,
        status=LifecycleEffectReceiptStatus.APPLIED,
        observation=LifecycleEffectObservation.EFFECT_SATISFIED,
        before_observation_digest=first.after_observation_digest,
        after_observation_digest=_digest(2_302),
        prior_receipt_digest=started.receipt_digest,
    ).receipt
    finalized = journal.record_lifecycle_effect_receipt(
        process_fence=fence,
        transaction_id=transaction.transaction_id,
        grant_id=grant.grant_id,
        lease_id=lease.lease_id,
        lease_epoch=lease.lease_epoch,
        sequence=4,
        request_nonce=_uuid(2_005),
        effect_id=first.effect_id,
        effect_kind=first.effect_kind,
        target_digest=first.target_digest,
        attempt=1,
        status=LifecycleEffectReceiptStatus.FINALIZED,
        observation=LifecycleEffectObservation.EFFECT_SATISFIED,
        before_observation_digest=satisfied.after_observation_digest,
        after_observation_digest=_digest(2_303),
        prior_receipt_digest=satisfied.receipt_digest,
    ).receipt
    second = journal.record_lifecycle_effect_receipt(
        process_fence=fence,
        transaction_id=transaction.transaction_id,
        grant_id=grant.grant_id,
        lease_id=lease.lease_id,
        lease_epoch=lease.lease_epoch,
        sequence=5,
        request_nonce=_uuid(2_002),
        effect_id=second_target.effect_id,
        effect_kind=second_target.effect_kind,
        target_digest=second_target.target_digest,
        attempt=1,
        status=LifecycleEffectReceiptStatus.OBSERVED,
        observation=LifecycleEffectObservation.RETRYABLE_NOT_READY,
        after_observation_digest=_digest(2_301),
        fixed_error_code="observation_not_ready",
    ).receipt
    assert first.target_digest != second.target_digest
    fence.close()
    journal.close()
    restarted = NativeLifecycleJournal(
        config,
        clock=clock,
        monotonic_clock=clock,
        boot_identity=lambda: "test-boot-session",
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    restarted.initialize()
    assert restarted.list_lifecycle_effect_receipts(transaction.transaction_id) == (
        first,
        started,
        satisfied,
        finalized,
        second,
    )
    decision = decide_lifecycle_recovery(
        transaction,
        next_effect=RecoveryObservation.RETRYABLE_NOT_READY,
        prior_effects=PriorEffectsState.INTACT,
    )
    assert decision.action is LifecycleRecoveryAction.RETRY_WHEN_READY
    assert decision.reason_code == "recovery_observation_not_ready"


def test_restart_rejects_tampered_grant_runner_identity(tmp_path):
    clock = MutableClock()
    config, journal, _, _, _, _, grant = _grant(tmp_path, clock)
    journal.close()
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "DROP TRIGGER native_lifecycle_execution_start_grants_v2_immutable"
        )
        connection.execute(
            """
            UPDATE native_lifecycle_execution_start_grants_v2
               SET runner_build_digest = ? WHERE grant_id = ?
            """,
            (_digest(9_900), grant.grant_id),
        )
    restarted = NativeLifecycleJournal(
        config,
        clock=clock,
        monotonic_clock=clock,
        boot_identity=lambda: "test-boot-session",
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    with pytest.raises(NativeLifecycleIntegrityError):
        restarted.initialize()


def test_inert_worker_opens_only_active_grant_and_refuses_effects(tmp_path):
    clock = MutableClock()
    _, journal, _, _, _, _, grant = _grant(tmp_path, clock)
    registration = _registration(grant)
    response = decode_lifecycle_execution_frame(
        process_one_frame(journal, encode_lifecycle_execution_message(registration))
    )
    assert isinstance(response, LifecycleExecutionRefusedV2)
    assert response.error_code == "execution_disabled"
    assert journal.list_lifecycle_effect_receipts(grant.transaction_id) == ()


def test_recovery_worker_uses_only_inherited_custom_state_anchor(tmp_path):
    anchor = tmp_path / "custom-config-anchor"
    anchor.mkdir()
    descriptor = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        journal = _journal_from_state_anchor_descriptor(descriptor)
        assert journal.path.parent == anchor / "state" / "native-lifecycle"
        assert str(Path.home()) not in str(journal.path)
        journal.initialize()
        assert (anchor / "state" / "native-lifecycle" / journal.path.name).is_file()
    finally:
        os.close(descriptor)
