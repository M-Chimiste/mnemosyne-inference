from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import sqlite3
import stat
from threading import Event
from uuid import uuid4

import pytest

from mnemosyne_macos.native_lifecycle import (
    ComponentDisposition,
    ComponentKind,
    ExecutionMemberDisposition,
    ExecutionMemberDomain,
    ExecutionMemberType,
    ExactExecutionMember,
    ExclusiveProofState,
    ExclusiveWeightExecutionIdentity,
    LifecyclePhase,
    MigrationEvidence,
    NativeLifecycleJournal,
    OutboxDecision,
    PRODUCT_IDENTITY,
    PriorEffectsState,
    RecoveryObservation,
    RetentionMode,
    RetentionWeight,
    WeightOwnership,
    build_migration_plan,
    build_private_execution_manifest,
    build_uninstall_plan,
    execution_member_inventory_digest,
    RecoveryCloneIdentity,
    SignedCodeIdentity,
)
from mnemosyne_macos.native_lifecycle_executor import (
    ExactTrashProof,
    ExactTrashProofState,
    HelperAuthorizationObservation,
    LifecycleExecutionState,
    LifecycleHelperAuthority,
    NativeLifecycleExecutor,
)
from test_native_lifecycle import (
    _helper_proof_authority,
    _helper_submission,
)


SOURCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "mnemosyne_macos"
    / "native_lifecycle_executor.py"
)
NOW = 1_788_100_000.0
UNINSTALL_PHASES = (
    LifecyclePhase.SERVICE_QUIESCED,
    LifecyclePhase.OUTBOX_RESOLVED,
    LifecyclePhase.HUB_RESOLVED,
    LifecyclePhase.WEIGHTS_RESOLVED,
    LifecyclePhase.RUNTIMES_RESOLVED,
    LifecyclePhase.AGENT_UNREGISTERED,
    LifecyclePhase.MENU_LOGIN_UNREGISTERED,
    LifecyclePhase.STATE_RESOLVED,
    LifecyclePhase.APPLICATION_QUARANTINED,
    LifecyclePhase.APPLICATION_REMOVED,
    LifecyclePhase.COMPLETED,
)
MIGRATION_PHASES = (
    LifecyclePhase.PREFLIGHTED,
    LifecyclePhase.DRAINED,
    LifecyclePhase.SNAPSHOTTED,
    LifecyclePhase.PREDECESSOR_STOPPED,
    LifecyclePhase.CANDIDATE_INSTALLED,
    LifecyclePhase.CANDIDATE_STARTED,
    LifecyclePhase.VALIDATED,
    LifecyclePhase.COMMITTED,
)
ALL_NORMAL_PHASE_CASES = tuple(
    ("uninstall", phase, previous)
    for phase, previous in zip(
        UNINSTALL_PHASES,
        (LifecyclePhase.AUTHORIZED, *UNINSTALL_PHASES[:-1]),
        strict=True,
    )
) + tuple(
    ("migration", phase, previous)
    for phase, previous in zip(
        MIGRATION_PHASES,
        (LifecyclePhase.AUTHORIZED, *MIGRATION_PHASES[:-1]),
        strict=True,
    )
)


def _digest(index: int) -> str:
    return f"{index:064x}"


def _member(index, domain, path, disposition):
    return ExactExecutionMember(
        domain=domain,
        exact_lexical_path=path,
        member_type=ExecutionMemberType.REGULAR_FILE,
        disposition=disposition,
        device=10,
        inode=10_000 + index,
        mode=stat.S_IFREG | 0o600,
        byte_count=1_024 + index,
        mtime_ns=1_700_000_000_000_000_000 + index,
        content_digest=_digest(900 + index),
    )


def _component_members(mode):
    runtime_disposition = (
        ExecutionMemberDisposition.RETAIN
        if mode is RetentionMode.APP_ONLY
        else ExecutionMemberDisposition.REMOVE_EXACT_ENTRY
    )
    return {
        ExecutionMemberDomain.PRIVATE_STATE: (
            _member(1, ExecutionMemberDomain.PRIVATE_STATE, "/private/config", ExecutionMemberDisposition.RETAIN),
        ),
        ExecutionMemberDomain.MANAGED_RUNTIME: (
            _member(2, ExecutionMemberDomain.MANAGED_RUNTIME, "/private/runtime", runtime_disposition),
        ),
        ExecutionMemberDomain.SECURITY_SCOPE: (
            _member(3, ExecutionMemberDomain.SECURITY_SCOPE, "/private/scope", ExecutionMemberDisposition.RETAIN),
        ),
    }


def _weight(
    index: int,
    ownership: WeightOwnership,
    *,
    fresh: bool = False,
) -> RetentionWeight:
    return RetentionWeight(
        asset_fingerprint=_digest(100 + index),
        payload_fingerprint=_digest(200 + index),
        exact_lexical_path=f"/Volumes/Athena/models/model-{index}",
        storage_location_id=str(uuid4()),
        storage_binding_generation=index,
        volume_uuid=f"VOLUME-{index}",
        scope_id=_digest(300 + index),
        ownership=ownership,
        installation_id=(
            str(uuid4())
            if ownership is WeightOwnership.EXCLUSIVE_MANAGED
            else None
        ),
        exclusive_proof_state=(
            ExclusiveProofState.FRESH_EXACT
            if fresh
            else ExclusiveProofState.NOT_PROVEN
        ),
        exclusive_proof_digest=_digest(400 + index) if fresh else None,
        byte_count=index * 1024,
    )


def _uninstall(
    *,
    mode: RetentionMode = RetentionMode.FULL_EXCLUSIVE_MANAGED,
    transaction_id: str | None = None,
):
    members = _component_members(mode)
    return build_uninstall_plan(
        transaction_id=transaction_id or str(uuid4()),
        retention_mode=mode,
        current_build_digest=_digest(1),
        private_state_fingerprint=execution_member_inventory_digest(
            members[ExecutionMemberDomain.PRIVATE_STATE]
        ),
        runtime_root_fingerprint=execution_member_inventory_digest(
            members[ExecutionMemberDomain.MANAGED_RUNTIME]
        ),
        security_scope_store_fingerprint=execution_member_inventory_digest(
            members[ExecutionMemberDomain.SECURITY_SCOPE]
        ),
        pairing_state_fingerprint=_digest(4),
        token_outbox_count=0,
        outbox_decision=OutboxDecision.PRESERVE_WITH_STATE,
        outbox_evidence_digest=_digest(6),
        weights=(
            _weight(1, WeightOwnership.EXCLUSIVE_MANAGED, fresh=True),
            _weight(2, WeightOwnership.IMPORTED),
        ),
    )


def _migration(*, transaction_id: str | None = None):
    return build_migration_plan(
        transaction_id=transaction_id or str(uuid4()),
        predecessor_build_digest=_digest(10),
        candidate_build_digest=_digest(11),
        evidence=MigrationEvidence(
            raw_config_digest=_digest(20),
            semantic_config_digest=_digest(21),
            sqlite_backup_digest=_digest(22),
            usage_outbox_identity_digest=_digest(23),
            private_environment_references_digest=_digest(24),
            runtime_ownership_digest=_digest(25),
            model_provenance_digest=_digest(26),
            storage_authority_digest=_digest(27),
            registration_state_digest=_digest(28),
            legacy_sidecar_evidence_digest=_digest(29),
            pairing_state_digest=_digest(30),
            participation_state_digest=_digest(31),
            rollback_snapshot_digest=_digest(32),
        ),
    )


def _journal(tmp_path: Path) -> NativeLifecycleJournal:
    journal = NativeLifecycleJournal(
        tmp_path / "private" / "config.yaml",
        clock=lambda: NOW,
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    journal.initialize()
    return journal


def _stage_authority(journal, transaction, *, expires_at=NOW + 600):
    plan = transaction.plan
    if hasattr(plan, "manifest_receipt"):
        private = journal.manifest_store.require_receipt(plan.manifest_receipt)
        plan = replace(
            plan,
            manifest_items=private.items,
            private_manifest=private,
        )
    source_root = "/Applications/Unified Inference.app"
    clone_root = str(
        journal.execution_manifest_store.expected_clone_path(
            transaction.transaction_id
        )
    )
    source_members = (
        _member(20, ExecutionMemberDomain.APPLICATION, f"{source_root}/Contents/MacOS/Unified Inference", ExecutionMemberDisposition.RETAIN),
        _member(21, ExecutionMemberDomain.APPLICATION, f"{source_root}/Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper", ExecutionMemberDisposition.RETAIN),
        _member(22, ExecutionMemberDomain.APPLICATION, f"{source_root}/Contents/MacOS/mnemosyne-lifecycle-runner", ExecutionMemberDisposition.RETAIN),
    )
    clone_members = (
        _member(30, ExecutionMemberDomain.RECOVERY_CLONE, f"{clone_root}/Contents/MacOS/Unified Inference", ExecutionMemberDisposition.RETAIN),
        _member(31, ExecutionMemberDomain.RECOVERY_CLONE, f"{clone_root}/Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper", ExecutionMemberDisposition.RETAIN),
        _member(32, ExecutionMemberDomain.RECOVERY_CLONE, f"{clone_root}/Contents/MacOS/mnemosyne-lifecycle-runner", ExecutionMemberDisposition.RETAIN),
    )
    source = SignedCodeIdentity(
        identifier=PRODUCT_IDENTITY.application_bundle_id,
        version="0.9.0",
        exact_path=source_root,
        build_digest=(
            plan.current_build_digest
            if hasattr(plan, "current_build_digest")
            else plan.predecessor_build_digest
        ),
        team_identifier="TEAM123456",
        code_requirement="identifier com.mnemosyne.inference.menu and anchor apple generic",
        code_directory_digest=_digest(70),
        sealed_resources_digest=_digest(71),
        executable_relative_path="Contents/MacOS/Unified Inference",
        member_inventory_digest=execution_member_inventory_digest(source_members),
    )
    clone = replace(
        source,
        exact_path=clone_root,
        member_inventory_digest=execution_member_inventory_digest(clone_members),
    )
    helper = SignedCodeIdentity(
        identifier="com.mnemosyne.inference.lifecycle-helper",
        version="0.9.0",
        exact_path=f"{clone_root}/Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper",
        build_digest=_digest(50),
        team_identifier="TEAM123456",
        code_requirement="identifier com.mnemosyne.inference.lifecycle-helper and anchor apple generic",
        code_directory_digest=_digest(72),
        sealed_resources_digest=_digest(73),
        executable_relative_path="Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper",
        member_inventory_digest=execution_member_inventory_digest(
            (clone_members[1],)
        ),
    )
    runner = SignedCodeIdentity(
        identifier="com.mnemosyne.inference.lifecycle-runner",
        version="0.9.0",
        exact_path=f"{clone_root}/Contents/MacOS/mnemosyne-lifecycle-runner",
        build_digest=_digest(51),
        team_identifier="TEAM123456",
        code_requirement="identifier com.mnemosyne.inference.lifecycle-runner and anchor apple generic",
        code_directory_digest=_digest(74),
        sealed_resources_digest=_digest(75),
        executable_relative_path="Contents/MacOS/mnemosyne-lifecycle-runner",
        member_inventory_digest=execution_member_inventory_digest(
            (clone_members[2],)
        ),
    )
    recovery = RecoveryCloneIdentity(
        transaction_id=transaction.transaction_id,
        exact_bundle_path=clone_root,
        source_application_identity_digest=source.identity_digest,
        cloned_application_identity_digest=clone.identity_digest,
        cloned_member_inventory_digest=clone.member_inventory_digest,
    )
    exact_members = [*source_members, *clone_members]
    weights = []
    predecessor = None
    candidate = None
    if hasattr(plan, "retention_mode"):
        exact_members.extend(
            member
            for values in _component_members(plan.retention_mode).values()
            for member in values
        )
        for index, item in enumerate(plan.manifest_items):
            if item.disposition.value != "move_to_trash":
                continue
            files = (
                _member(
                    100 + index,
                    ExecutionMemberDomain.EXCLUSIVE_WEIGHT,
                    item.exact_lexical_path,
                    ExecutionMemberDisposition.REMOVE_EXACT_ENTRY,
                ),
            )
            weights.append(
                ExclusiveWeightExecutionIdentity(
                    asset_fingerprint=item.asset_fingerprint,
                    payload_fingerprint=item.payload_fingerprint,
                    exact_lexical_path=item.exact_lexical_path,
                    storage_location_id=item.storage_location_id,
                    storage_binding_generation=item.storage_binding_generation,
                    volume_uuid=item.volume_uuid,
                    scope_id=item.scope_id,
                    installation_id=item.installation_id,
                    provenance_digest=item.exclusive_proof_digest,
                    file_inventory_digest=execution_member_inventory_digest(files),
                    files=files,
                )
            )
        pairing_digest = next(
            item.authority
            for item in plan.components
            if item.kind is ComponentKind.PAIRING_STATE
        )
    else:
        exact_members.extend(
            member
            for values in _component_members(RetentionMode.APP_ONLY).values()
            for member in values
        )
        pairing_digest = plan.evidence.pairing_state_digest
        predecessor = source
        candidate_root = "/Applications/.Unified Inference.candidate.app"
        candidate_members = (
            _member(
                40,
                ExecutionMemberDomain.CANDIDATE_APPLICATION,
                f"{candidate_root}/Contents/MacOS/Unified Inference",
                ExecutionMemberDisposition.RETAIN,
            ),
        )
        exact_members.extend(candidate_members)
        candidate = replace(
            source,
            exact_path=candidate_root,
            build_digest=plan.candidate_build_digest,
            member_inventory_digest=execution_member_inventory_digest(
                candidate_members
            ),
        )
    manifest = build_private_execution_manifest(
        plan=plan,
        transaction_authority_digest=transaction.authority_digest,
        application=source,
        recovery_application=clone,
        helper=helper,
        runner=runner,
        recovery_clone=recovery,
        exact_members=exact_members,
        exclusive_weights=weights,
        pairing_state_digest=pairing_digest,
        predecessor=predecessor,
        candidate=candidate,
    )
    journal.record_helper_staged(manifest)
    lifetime_seconds = min(120, int(expires_at - NOW))
    challenge = journal.issue_helper_authorization_challenge(
        transaction.transaction_id,
        lifetime_seconds=lifetime_seconds,
    ).challenge
    accepted = journal.accept_helper_authorization_receipt(
        _helper_submission(challenge)
    )
    authorization_digest = challenge.authorization_digest.removeprefix(
        "sha256:"
    )
    session_id = challenge.session_id
    authorized = accepted.transaction
    return authorized, manifest, authorization_digest, session_id


def _authority(transaction, manifest, authorization_digest, session_id, *, expires_at: float = NOW + 600):
    return LifecycleHelperAuthority.authenticated_verified(
        transaction_id=transaction.transaction_id,
        transaction_authority_digest=transaction.authority_digest,
        execution_manifest_digest=manifest.receipt.manifest_digest,
        recovery_clone_identity_digest=(
            manifest.receipt.recovery_clone_identity_digest
        ),
        helper_build_digest=_digest(50),
        authorization_digest=authorization_digest,
        session_id=session_id,
        expires_at=expires_at,
    )


class FakeEffects:
    def __init__(self) -> None:
        self.authorization = HelperAuthorizationObservation.AUTHORIZED
        self.prior = PriorEffectsState.INTACT
        self.satisfied: set[LifecyclePhase] = set()
        self.observations: dict[LifecyclePhase, RecoveryObservation] = {}
        self.post_observations: dict[LifecyclePhase, RecoveryObservation] = {}
        self.fail_before: set[LifecyclePhase] = set()
        self.fail_after: set[LifecyclePhase] = set()
        self.calls: list[tuple[LifecyclePhase, tuple[object, ...]]] = []
        self.authorize_calls = 0
        self.trash_proof_state = ExactTrashProofState.FRESH_EXACT
        self.trash_proof_mismatch = False
        self.trash_proof_calls = 0
        self.trash_requests = ()

    def authorize(self, _authority, _transaction):
        self.authorize_calls += 1
        return self.authorization

    def observe_prior_effects(self, _authority, _transaction):
        return self.prior

    def observe_phase(self, _authority, _transaction, phase):
        if phase in self.post_observations and any(
            called is phase for called, _values in self.calls
        ):
            return self.post_observations[phase]
        if phase in self.observations:
            return self.observations[phase]
        return (
            RecoveryObservation.EFFECT_SATISFIED
            if phase in self.satisfied
            else RecoveryObservation.NEEDS_ACTION
        )

    def _apply(self, phase: LifecyclePhase, *values: object) -> None:
        self.calls.append((phase, values))
        if phase in self.fail_before:
            raise RuntimeError("fixed fake before-effect failure")
        self.satisfied.add(phase)
        if phase in self.fail_after:
            raise RuntimeError("fixed fake after-effect failure")

    def quiesce_exact_service(self, _authority, transaction_id):
        self._apply(LifecyclePhase.SERVICE_QUIESCED, transaction_id)

    def resolve_token_outbox(self, _authority, request):
        self._apply(LifecyclePhase.OUTBOX_RESOLVED, request)

    def resolve_hub_pairing(self, _authority, transaction_id, plan):
        self._apply(LifecyclePhase.HUB_RESOLVED, transaction_id, plan)

    def unregister_exact_launch_agent(
        self, _authority, transaction_id, label
    ):
        self._apply(LifecyclePhase.AGENT_UNREGISTERED, transaction_id, label)

    def unregister_menu_login_item(
        self, _authority, transaction_id, bundle_id
    ):
        self._apply(
            LifecyclePhase.MENU_LOGIN_UNREGISTERED,
            transaction_id,
            bundle_id,
        )

    def quarantine_exact_application(
        self, _authority, transaction_id, bundle_id, manifest_digest
    ):
        self._apply(
            LifecyclePhase.APPLICATION_QUARANTINED,
            transaction_id,
            bundle_id,
            manifest_digest,
        )

    def remove_exact_application(
        self, _authority, transaction_id, bundle_id
    ):
        self._apply(
            LifecyclePhase.APPLICATION_REMOVED, transaction_id, bundle_id
        )

    def prove_exact_trash_authority(
        self, _authority, _transaction_id, requests
    ):
        self.trash_proof_calls += 1
        digests = tuple(request.request_digest for request in requests)
        if self.trash_proof_mismatch:
            digests = (_digest(999),)
        return ExactTrashProof(self.trash_proof_state, digests)

    def move_exact_exclusive_weights_to_trash(
        self,
        _authority,
        transaction_id,
        manifest_digest,
        requests,
        retained_count,
    ):
        self.trash_requests = requests
        self._apply(
            LifecyclePhase.WEIGHTS_RESOLVED,
            transaction_id,
            manifest_digest,
            requests,
            retained_count,
        )

    def resolve_managed_runtimes(
        self, _authority, transaction_id, request
    ):
        self._apply(
            LifecyclePhase.RUNTIMES_RESOLVED, transaction_id, request
        )

    def resolve_private_state(self, _authority, transaction_id, requests):
        self._apply(
            LifecyclePhase.STATE_RESOLVED, transaction_id, requests
        )

    def finalize_uninstall(self, _authority, transaction_id):
        self._apply(LifecyclePhase.COMPLETED, transaction_id)

    def preflight_exact_candidate(self, _authority, transaction_id, plan):
        self._apply(LifecyclePhase.PREFLIGHTED, transaction_id, plan)

    def drain_inference(self, _authority, transaction_id):
        self._apply(LifecyclePhase.DRAINED, transaction_id)

    def capture_exact_rollback_snapshot(
        self, _authority, transaction_id, plan
    ):
        self._apply(LifecyclePhase.SNAPSHOTTED, transaction_id, plan)

    def stop_exact_predecessor(
        self, _authority, transaction_id, predecessor_digest
    ):
        self._apply(
            LifecyclePhase.PREDECESSOR_STOPPED,
            transaction_id,
            predecessor_digest,
        )

    def start_exact_candidate(
        self, _authority, transaction_id, candidate_digest
    ):
        self._apply(
            LifecyclePhase.CANDIDATE_STARTED,
            transaction_id,
            candidate_digest,
        )

    def install_exact_candidate(
        self, _authority, transaction_id, candidate_digest
    ):
        self._apply(
            LifecyclePhase.CANDIDATE_INSTALLED,
            transaction_id,
            candidate_digest,
        )

    def validate_candidate_preservation(
        self, _authority, transaction_id, plan
    ):
        self._apply(LifecyclePhase.VALIDATED, transaction_id, plan)

    def commit_exact_candidate(
        self, _authority, transaction_id, candidate_digest
    ):
        self._apply(
            LifecyclePhase.COMMITTED, transaction_id, candidate_digest
        )

    def restore_exact_predecessor(
        self, _authority, transaction_id, plan
    ):
        self._apply(LifecyclePhase.RESTORED, transaction_id, plan)


def _executor(journal, transaction, effects, *, clock=lambda: NOW):
    transaction, manifest, authorization, session = _stage_authority(
        journal, transaction
    )
    return NativeLifecycleExecutor(
        journal,
        effects=effects,
        authority=_authority(
            transaction, manifest, authorization, session
        ),
        clock=clock,
    )


def _prepared_for_kind(tmp_path: Path, kind: str):
    journal = _journal(tmp_path)
    plan = _uninstall() if kind == "uninstall" else _migration()
    return journal, journal.prepare(plan).transaction


def test_execution_is_disabled_without_separate_verified_helper_authority(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_uninstall()).transaction
    report = NativeLifecycleExecutor(journal).run(transaction.transaction_id)
    assert report.state is LifecycleExecutionState.DISABLED
    assert report.error_code == "native_lifecycle_helper_staging_required"
    assert journal.get(transaction.transaction_id).phase is LifecyclePhase.PREPARED


def test_v2_journal_additively_recreates_execution_control_tables(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_migration()).transaction
    path = journal.path
    journal.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE native_lifecycle_execution_claims_v2")
        connection.execute("DROP TABLE native_lifecycle_control_v2")
        connection.commit()
    finally:
        connection.close()

    reopened = NativeLifecycleJournal(
        tmp_path / "private" / "config.yaml",
        clock=lambda: NOW,
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    reopened.initialize()
    restored = reopened.get(transaction.transaction_id)
    assert restored is not None
    assert restored.rollback_requested is False


@pytest.mark.parametrize(
    ("configure", "code"),
    [
        (
            lambda effects, _transaction: setattr(
                effects,
                "authorization",
                HelperAuthorizationObservation.REJECTED,
            ),
            "native_lifecycle_helper_authority_rejected",
        ),
        (
            lambda effects, _transaction: setattr(
                effects,
                "authorization",
                HelperAuthorizationObservation.UNAVAILABLE,
            ),
            "native_lifecycle_helper_authority_unavailable",
        ),
    ],
)
def test_helper_adapter_must_independently_authorize(
    tmp_path: Path, configure, code: str
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_uninstall()).transaction
    effects = FakeEffects()
    configure(effects, transaction)
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id
    )
    assert report.state is LifecycleExecutionState.DISABLED
    assert report.error_code == code
    assert effects.calls == []


def test_expired_or_wrong_transaction_authority_never_invokes_an_effect(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_uninstall()).transaction
    effects = FakeEffects()
    transaction, manifest, authorization, session = _stage_authority(
        journal, transaction
    )
    expired = NativeLifecycleExecutor(
        journal,
        effects=effects,
        authority=_authority(
            transaction,
            manifest,
            authorization,
            session,
            expires_at=NOW,
        ),
        clock=lambda: NOW,
    ).run(transaction.transaction_id)
    assert expired.error_code == "native_lifecycle_helper_authority_expired"
    wrong = LifecycleHelperAuthority.authenticated_verified(
        transaction_id=str(uuid4()),
        transaction_authority_digest=transaction.authority_digest,
        execution_manifest_digest=manifest.receipt.manifest_digest,
        recovery_clone_identity_digest=manifest.receipt.recovery_clone_identity_digest,
        helper_build_digest=_digest(50),
        authorization_digest=_digest(51),
        session_id=str(uuid4()),
        expires_at=NOW + 100,
    )
    mismatch = NativeLifecycleExecutor(
        journal, effects=effects, authority=wrong, clock=lambda: NOW
    ).run(transaction.transaction_id)
    assert mismatch.error_code == "native_lifecycle_helper_authority_mismatch"
    assert effects.calls == []


def test_uninstall_executes_every_closed_phase_and_only_exact_fresh_weights(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    plan = _uninstall()
    transaction = journal.prepare(plan).transaction
    effects = FakeEffects()
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id
    )
    assert report.state is LifecycleExecutionState.TERMINAL
    assert report.transaction.phase is LifecyclePhase.COMPLETED
    assert report.phases_recorded == (
        LifecyclePhase.SERVICE_QUIESCED,
        LifecyclePhase.OUTBOX_RESOLVED,
        LifecyclePhase.HUB_RESOLVED,
        LifecyclePhase.WEIGHTS_RESOLVED,
        LifecyclePhase.RUNTIMES_RESOLVED,
        LifecyclePhase.AGENT_UNREGISTERED,
        LifecyclePhase.MENU_LOGIN_UNREGISTERED,
        LifecyclePhase.STATE_RESOLVED,
        LifecyclePhase.APPLICATION_QUARANTINED,
        LifecyclePhase.APPLICATION_REMOVED,
        LifecyclePhase.COMPLETED,
    )
    assert effects.trash_proof_calls == 1
    assert len(effects.trash_requests) == 1
    request = effects.trash_requests[0]
    assert request.exact_lexical_path.endswith("/model-1")
    assert request.installation_id is not None
    assert all(
        not item.exact_lexical_path.endswith("/model-2")
        for item in effects.trash_requests
    )
    call_values = [values for _phase, values in effects.calls]
    assert any(PRODUCT_IDENTITY.launch_agent_label in row for row in call_values)
    assert any(PRODUCT_IDENTITY.application_bundle_id in row for row in call_values)
    state_request = next(
        values[1]
        for phase, values in effects.calls
        if phase is LifecyclePhase.STATE_RESOLVED
    )
    assert {item.kind for item in state_request} == {
        ComponentKind.PRIVATE_STATE,
        ComponentKind.SECURITY_SCOPES,
    }
    assert all(
        item.disposition is ComponentDisposition.RETAIN
        for item in state_request
    )


def test_app_only_passes_no_trash_target_and_retains_state_authorities(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    plan = _uninstall(mode=RetentionMode.APP_ONLY)
    transaction = journal.prepare(plan).transaction
    effects = FakeEffects()
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id
    )
    assert report.state is LifecycleExecutionState.TERMINAL
    assert effects.trash_proof_calls == 0
    assert effects.trash_requests == ()
    state_request = next(
        values[1]
        for phase, values in effects.calls
        if phase is LifecyclePhase.STATE_RESOLVED
    )
    assert all(
        item.disposition is ComponentDisposition.RETAIN
        for item in state_request
    )
    outbox = next(
        values[0]
        for phase, values in effects.calls
        if phase is LifecyclePhase.OUTBOX_RESOLVED
    )
    assert outbox.decision is OutboxDecision.PRESERVE_WITH_STATE


def test_effect_failure_before_mutation_is_retryable_and_restart_resumes(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_uninstall()).transaction
    failing = FakeEffects()
    failing.fail_before.add(LifecyclePhase.AGENT_UNREGISTERED)
    first = _executor(journal, transaction, failing).run(
        transaction.transaction_id
    )
    assert first.state is LifecycleExecutionState.RETRYABLE
    assert first.transaction.phase is LifecyclePhase.RUNTIMES_RESOLVED

    resumed_effects = FakeEffects()
    resumed_effects.satisfied.update(first.phases_recorded)
    resumed = _executor(
        journal,
        journal.get(transaction.transaction_id),
        resumed_effects,
    ).run(transaction.transaction_id)
    assert resumed.state is LifecycleExecutionState.TERMINAL
    assert resumed.transaction.phase is LifecyclePhase.COMPLETED


@pytest.mark.parametrize(
    ("kind", "failed_phase", "expected_journal_phase"),
    ALL_NORMAL_PHASE_CASES,
)
def test_every_phase_has_a_no_effect_retry_boundary(
    tmp_path: Path,
    kind: str,
    failed_phase: LifecyclePhase,
    expected_journal_phase: LifecyclePhase,
) -> None:
    journal, transaction = _prepared_for_kind(tmp_path, kind)
    effects = FakeEffects()
    effects.fail_before.add(failed_phase)
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id
    )
    assert report.state is LifecycleExecutionState.RETRYABLE
    assert report.transaction.phase is expected_journal_phase
    assert [phase for phase, _values in effects.calls].count(failed_phase) == 1


@pytest.mark.parametrize(
    ("kind", "ambiguous_phase", "_previous"), ALL_NORMAL_PHASE_CASES
)
def test_every_phase_recovers_when_effect_completed_before_exception(
    tmp_path: Path,
    kind: str,
    ambiguous_phase: LifecyclePhase,
    _previous: LifecyclePhase,
) -> None:
    journal, transaction = _prepared_for_kind(tmp_path, kind)
    effects = FakeEffects()
    effects.fail_after.add(ambiguous_phase)
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id
    )
    assert report.state is LifecycleExecutionState.TERMINAL
    expected_terminal = (
        LifecyclePhase.COMPLETED
        if kind == "uninstall"
        else LifecyclePhase.COMMITTED
    )
    assert report.transaction.phase is expected_terminal
    assert [phase for phase, _values in effects.calls].count(
        ambiguous_phase
    ) == 1


def test_restart_records_an_effect_satisfied_before_its_journal_receipt(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_migration()).transaction
    effects = FakeEffects()
    effects.satisfied.add(LifecyclePhase.PREFLIGHTED)
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id, maximum_steps=1
    )
    assert report.state is LifecycleExecutionState.PROGRESSED
    assert report.transaction.phase is LifecyclePhase.PREFLIGHTED
    assert effects.calls == []


def test_authority_expiry_between_phases_stops_before_another_effect(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_migration()).transaction
    clock = [NOW]

    class ExpiringEffects(FakeEffects):
        def preflight_exact_candidate(self, authority, transaction_id, plan):
            super().preflight_exact_candidate(
                authority, transaction_id, plan
            )
            clock[0] = NOW + 1_000

    effects = ExpiringEffects()
    report = _executor(
        journal,
        transaction,
        effects,
        clock=lambda: clock[0],
    ).run(transaction.transaction_id)
    assert report.state is LifecycleExecutionState.DISABLED
    assert report.error_code == "native_lifecycle_helper_authority_expired"
    assert report.transaction.phase is LifecyclePhase.PREFLIGHTED
    assert [phase for phase, _values in effects.calls] == [
        LifecyclePhase.PREFLIGHTED
    ]


def test_authority_is_rechecked_after_slow_trash_proof_before_effect(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_uninstall()).transaction
    clock = [NOW]

    class ExpiringProofEffects(FakeEffects):
        def prove_exact_trash_authority(
            self, authority, transaction_id, requests
        ):
            proof = super().prove_exact_trash_authority(
                authority, transaction_id, requests
            )
            clock[0] = NOW + 1_000
            return proof

    effects = ExpiringProofEffects()
    report = _executor(
        journal,
        transaction,
        effects,
        clock=lambda: clock[0],
    ).run(transaction.transaction_id)
    assert report.state is LifecycleExecutionState.DISABLED
    assert report.error_code == "native_lifecycle_helper_authority_expired"
    assert report.transaction.phase is LifecyclePhase.HUB_RESOLVED
    assert effects.trash_proof_calls == 1
    assert effects.trash_requests == ()


@pytest.mark.parametrize(
    ("proof_state", "mismatch", "reason"),
    [
        (ExactTrashProofState.CONFLICT, False, "exact_identity_mismatch"),
        (
            ExactTrashProofState.UNAVAILABLE,
            False,
            "recovery_observation_unavailable",
        ),
        (ExactTrashProofState.FRESH_EXACT, True, "exact_identity_mismatch"),
    ],
)
def test_exact_trash_requires_a_fresh_matching_same_run_proof(
    tmp_path: Path,
    proof_state: ExactTrashProofState,
    mismatch: bool,
    reason: str,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_uninstall()).transaction
    effects = FakeEffects()
    effects.trash_proof_state = proof_state
    effects.trash_proof_mismatch = mismatch
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id
    )
    assert report.state is LifecycleExecutionState.MANUAL_RECOVERY
    assert report.error_code == reason
    assert effects.trash_requests == ()
    assert not any(
        phase is LifecyclePhase.WEIGHTS_RESOLVED
        for phase, _values in effects.calls
    )


@pytest.mark.parametrize(
    ("prior", "reason"),
    [
        (PriorEffectsState.CONFLICT, "prior_effect_conflict"),
        (PriorEffectsState.UNAVAILABLE, "prior_effect_unavailable"),
    ],
)
def test_uncertain_prior_effects_enter_durable_manual_recovery(
    tmp_path: Path, prior: PriorEffectsState, reason: str
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_migration()).transaction
    effects = FakeEffects()
    effects.prior = prior
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id
    )
    assert report.state is LifecycleExecutionState.MANUAL_RECOVERY
    assert report.error_code == reason
    assert journal.get(transaction.transaction_id).error_code == reason
    assert effects.calls == []


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (
            RecoveryObservation.CONFLICT,
            "recovery_observation_conflict",
        ),
        (
            RecoveryObservation.UNAVAILABLE,
            "recovery_observation_unavailable",
        ),
    ],
)
def test_ambiguous_post_effect_observation_never_replays_automatically(
    tmp_path: Path,
    observation: RecoveryObservation,
    reason: str,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_migration()).transaction
    effects = FakeEffects()
    effects.fail_before.add(LifecyclePhase.PREFLIGHTED)
    effects.post_observations[LifecyclePhase.PREFLIGHTED] = observation
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id
    )
    assert report.state is LifecycleExecutionState.MANUAL_RECOVERY
    assert report.error_code == reason
    assert report.transaction.phase is LifecyclePhase.MANUAL_RECOVERY


def test_migration_commits_only_after_preservation_validation(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    plan = _migration()
    transaction = journal.prepare(plan).transaction
    effects = FakeEffects()
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id
    )
    assert report.state is LifecycleExecutionState.TERMINAL
    assert report.transaction.phase is LifecyclePhase.COMMITTED
    phases = [phase for phase, _values in effects.calls]
    assert phases == [
        LifecyclePhase.PREFLIGHTED,
        LifecyclePhase.DRAINED,
        LifecyclePhase.SNAPSHOTTED,
        LifecyclePhase.PREDECESSOR_STOPPED,
        LifecyclePhase.CANDIDATE_INSTALLED,
        LifecyclePhase.CANDIDATE_STARTED,
        LifecyclePhase.VALIDATED,
        LifecyclePhase.COMMITTED,
    ]
    validation_plan = next(
        values[1]
        for phase, values in effects.calls
        if phase is LifecyclePhase.VALIDATED
    )
    assert validation_plan.evidence.usage_outbox_identity_digest == _digest(23)
    assert validation_plan.evidence.storage_authority_digest == _digest(27)
    assert validation_plan.evidence.pairing_state_digest == _digest(30)


def test_migration_can_restore_exact_predecessor_after_cutover_restart(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    plan = _migration()
    transaction = journal.prepare(plan).transaction
    first_effects = FakeEffects()
    first = _executor(journal, transaction, first_effects).run(
        transaction.transaction_id, maximum_steps=4
    )
    assert first.transaction.phase is LifecyclePhase.PREDECESSOR_STOPPED

    restart_effects = FakeEffects()
    restart_effects.satisfied.update(first.phases_recorded)
    rollback = _executor(
        journal,
        journal.get(transaction.transaction_id),
        restart_effects,
    ).run(transaction.transaction_id, rollback_requested=True)
    assert rollback.state is LifecycleExecutionState.TERMINAL
    assert rollback.transaction.phase is LifecyclePhase.RESTORED
    assert [phase for phase, _values in restart_effects.calls] == [
        LifecyclePhase.RESTORED
    ]


def test_retry_after_failed_restore_keeps_durable_rollback_direction(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_migration()).transaction
    first_effects = FakeEffects()
    first = _executor(journal, transaction, first_effects).run(
        transaction.transaction_id, maximum_steps=4
    )
    assert first.transaction.phase is LifecyclePhase.PREDECESSOR_STOPPED

    failing_restore = FakeEffects()
    failing_restore.satisfied.update(first.phases_recorded)
    failing_restore.fail_before.add(LifecyclePhase.RESTORED)
    requested = _executor(
        journal,
        journal.get(transaction.transaction_id),
        failing_restore,
    ).run(transaction.transaction_id, rollback_requested=True)
    assert requested.state is LifecycleExecutionState.RETRYABLE
    assert requested.transaction.rollback_requested is True
    assert requested.transaction.phase is LifecyclePhase.PREDECESSOR_STOPPED

    restart_effects = FakeEffects()
    restart_effects.satisfied.update(first.phases_recorded)
    restarted = _executor(
        journal,
        journal.get(transaction.transaction_id),
        restart_effects,
    ).run(transaction.transaction_id)
    assert restarted.state is LifecycleExecutionState.TERMINAL
    assert restarted.transaction.phase is LifecyclePhase.RESTORED
    assert [phase for phase, _values in restart_effects.calls] == [
        LifecyclePhase.RESTORED
    ]


def test_rollback_before_predecessor_stop_fails_closed_without_effect(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_migration()).transaction
    effects = FakeEffects()
    report = _executor(journal, transaction, effects).run(
        transaction.transaction_id, rollback_requested=True
    )
    assert report.state is LifecycleExecutionState.MANUAL_RECOVERY
    assert report.error_code == "rollback_not_available"
    assert effects.calls == []


def test_another_incomplete_transaction_blocks_all_effects(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    first = journal.prepare(_uninstall()).transaction
    journal.prepare(_migration())
    effects = FakeEffects()
    report = _executor(journal, first, effects).run(first.transaction_id)
    assert report.state is LifecycleExecutionState.BLOCKED
    assert report.error_code == "native_lifecycle_execution_conflict"
    assert effects.calls == []


def test_unresolved_manual_recovery_blocks_every_other_transaction(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    unresolved = journal.prepare(_uninstall()).transaction
    journal.mark_manual_recovery(
        unresolved.transaction_id, "recovery_observation_unavailable"
    )
    candidate = journal.prepare(_migration()).transaction
    effects = FakeEffects()
    report = _executor(journal, candidate, effects).run(
        candidate.transaction_id
    )
    assert report.state is LifecycleExecutionState.BLOCKED
    assert report.error_code == "native_lifecycle_execution_conflict"
    assert effects.calls == []


def test_product_wide_claim_blocks_same_transaction_across_executors(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "private" / "config.yaml"
    first_journal = NativeLifecycleJournal(
        config_path,
        clock=lambda: NOW,
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    first_journal.initialize()
    transaction = first_journal.prepare(_migration()).transaction
    second_journal = NativeLifecycleJournal(
        config_path,
        clock=lambda: NOW,
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    second_journal.initialize()
    entered = Event()
    release = Event()

    class BlockingEffects(FakeEffects):
        def observe_prior_effects(self, authority, current):
            entered.set()
            assert release.wait(timeout=5)
            return super().observe_prior_effects(authority, current)

    first_effects = BlockingEffects()
    second_effects = FakeEffects()
    first_executor = _executor(first_journal, transaction, first_effects)
    second_executor = _executor(second_journal, transaction, second_effects)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(
            first_executor.run,
            transaction.transaction_id,
            maximum_steps=1,
        )
        assert entered.wait(timeout=5)
        blocked = second_executor.run(transaction.transaction_id)
        release.set()
        progressed = running.result(timeout=5)
    assert blocked.state is LifecycleExecutionState.BLOCKED
    assert blocked.error_code == "native_lifecycle_execution_claim_conflict"
    assert second_effects.calls == []
    assert progressed.state is LifecycleExecutionState.PROGRESSED
    assert [phase for phase, _values in first_effects.calls] == [
        LifecyclePhase.PREFLIGHTED
    ]


def test_expired_claim_enters_manual_recovery_and_cannot_be_stolen(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_migration()).transaction
    transaction, manifest, authorization, session = _stage_authority(
        journal, transaction, expires_at=NOW + 10
    )
    stale_authority = _authority(
        transaction,
        manifest,
        authorization,
        session,
        expires_at=NOW + 10,
    )
    assert stale_authority.session_id is not None
    acquired = journal.acquire_execution_claim(
        transaction_id=transaction.transaction_id,
        authority_digest=transaction.authority_digest,
        helper_session_id=stale_authority.session_id,
        claim_id=str(uuid4()),
        expires_at=NOW + 10,
        now=NOW,
    )
    assert acquired.state.value == "acquired"

    effects = FakeEffects()
    report = _executor(
        journal,
        transaction,
        effects,
        clock=lambda: NOW + 20,
    ).run(transaction.transaction_id)
    assert report.state is LifecycleExecutionState.MANUAL_RECOVERY
    assert report.error_code == "recovery_observation_unavailable"
    assert effects.calls == []

    other = journal.prepare(_uninstall()).transaction
    other_effects = FakeEffects()
    blocked = _executor(
        journal, other, other_effects, clock=lambda: NOW + 20
    ).run(other.transaction_id)
    assert blocked.state is LifecycleExecutionState.BLOCKED
    assert blocked.error_code == "native_lifecycle_execution_conflict"
    assert other_effects.calls == []


def test_abnormal_exit_after_effect_keeps_claim_until_expiry(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    transaction = journal.prepare(_migration()).transaction
    transaction, manifest, authorization, session = _stage_authority(
        journal, transaction, expires_at=NOW + 10
    )
    effects = FakeEffects()
    original_advance = journal.advance

    def fail_receipt(_transaction_id, _phase):
        raise RuntimeError("fixed journal receipt failure")

    journal.advance = fail_receipt  # type: ignore[method-assign]
    executor = NativeLifecycleExecutor(
        journal,
        effects=effects,
        authority=_authority(
            transaction,
            manifest,
            authorization,
            session,
            expires_at=NOW + 10,
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="fixed journal receipt failure"):
        executor.run(transaction.transaction_id)
    assert [phase for phase, _values in effects.calls] == [
        LifecyclePhase.PREFLIGHTED
    ]

    journal.advance = original_advance  # type: ignore[method-assign]
    recovery_effects = FakeEffects()
    recovery = _executor(
        journal,
        transaction,
        recovery_effects,
        clock=lambda: NOW + 20,
    ).run(transaction.transaction_id)
    assert recovery.state is LifecycleExecutionState.MANUAL_RECOVERY
    assert recovery.error_code == "recovery_observation_unavailable"
    assert recovery_effects.calls == []


def test_executor_contains_no_process_or_product_mutation_primitive() -> None:
    tree = ast.parse(SOURCE_PATH.read_text())
    forbidden_attributes = {
        "kill",
        "killpg",
        "rmdir",
        "rmtree",
        "run",
        "spawn",
        "system",
        "unlink",
    }
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert forbidden_attributes.isdisjoint(calls)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not {"subprocess", "shutil"} & imports
