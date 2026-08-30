from __future__ import annotations

import ast
from dataclasses import replace
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from mnemosyne_macos.native_lifecycle import (
    ComponentDisposition,
    ComponentKind,
    ExecutionMemberDisposition,
    ExecutionMemberDomain,
    ExecutionMemberType,
    ExactExecutionMember,
    ExclusiveWeightExecutionIdentity,
    ExclusiveProofState,
    HubRevocationState,
    HelperAuthorizationProofAuthority,
    LEGACY_SIDECAR_LABEL,
    LegacySidecarMigrationState,
    LifecycleKind,
    LifecyclePhase,
    LifecycleRecoveryAction,
    MigrationEvidence,
    NativeLifecycleCapacityError,
    NativeLifecycleConflictError,
    NativeLifecycleIntegrityError,
    NativeLifecycleJournal,
    OutboxDecision,
    PriorEffectsState,
    RecoveryObservation,
    RetentionManifestStore,
    RetentionMode,
    RetentionWeight,
    WeightDisposition,
    WeightOwnership,
    build_migration_plan,
    build_private_execution_manifest,
    build_uninstall_plan,
    decide_lifecycle_recovery,
    decide_manual_recovery_action,
    execution_member_inventory_digest,
    helper_authorization_submission_from_mapping,
    RecoveryCloneIdentity,
    SignedCodeIdentity,
)


SOURCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "mnemosyne_macos"
    / "native_lifecycle.py"
)
_HELPER_PROOF_KEY = b"mnemosyne-explicit-test-helper-proof-key-v1"


def _helper_proof_authority() -> HelperAuthorizationProofAuthority:
    key_id = "sha256:" + sha256(_HELPER_PROOF_KEY).hexdigest()

    def verify(payload: bytes, proof: str) -> bool:
        expected = hmac.new(_HELPER_PROOF_KEY, payload, sha256).hexdigest()
        return hmac.compare_digest(expected, proof)

    return HelperAuthorizationProofAuthority(
        algorithm="test-hmac-sha256-v1",
        key_id=key_id,
        verifier=verify,
    )


def _digest(index: int) -> str:
    return f"{index:064x}"


def _uuid(index: int) -> str:
    return f"{index:08x}-0000-4000-8000-{index:012x}"


class Clock:
    def __init__(self) -> None:
        self.value = 1_788_100_000.0

    def __call__(self) -> float:
        value = self.value
        self.value += 1
        return value


def _weight(
    index: int,
    ownership: WeightOwnership,
    *,
    path: str | None = None,
    fresh: bool = False,
) -> RetentionWeight:
    managed = ownership is WeightOwnership.EXCLUSIVE_MANAGED
    return RetentionWeight(
        asset_fingerprint=_digest(100 + index),
        payload_fingerprint=_digest(200 + index),
        exact_lexical_path=path or f"/Volumes/Athena/models/model-{index}",
        storage_location_id=_uuid(300 + index),
        storage_binding_generation=index + 1,
        volume_uuid=f"VOLUME-{index}",
        scope_id=_digest(400 + index),
        ownership=ownership,
        installation_id=_uuid(500 + index) if managed else None,
        exclusive_proof_state=(
            ExclusiveProofState.FRESH_EXACT
            if fresh
            else ExclusiveProofState.NOT_PROVEN
        ),
        exclusive_proof_digest=_digest(600 + index) if fresh else None,
        byte_count=index * 1024,
    )


def _plan(
    *,
    transaction_index: int = 1,
    mode: RetentionMode = RetentionMode.KEEP_WEIGHTS,
    outbox_count: int = 0,
    outbox_decision: OutboxDecision | None = None,
    weights: tuple[RetentionWeight, ...] | None = None,
):
    if outbox_decision is None:
        outbox_decision = OutboxDecision.PRESERVE_WITH_STATE
    component_members = _component_members(mode)
    return build_uninstall_plan(
        transaction_id=_uuid(transaction_index),
        retention_mode=mode,
        current_build_digest=_digest(1),
        private_state_fingerprint=execution_member_inventory_digest(
            component_members[ExecutionMemberDomain.PRIVATE_STATE]
        ),
        runtime_root_fingerprint=execution_member_inventory_digest(
            component_members[ExecutionMemberDomain.MANAGED_RUNTIME]
        ),
        security_scope_store_fingerprint=execution_member_inventory_digest(
            component_members[ExecutionMemberDomain.SECURITY_SCOPE]
        ),
        pairing_state_fingerprint=_digest(4),
        token_outbox_count=outbox_count,
        outbox_decision=outbox_decision,
        outbox_evidence_digest=_digest(6),
        weights=weights
        if weights is not None
        else (_weight(1, WeightOwnership.IMPORTED),),
    )


def _member(
    index: int,
    domain: ExecutionMemberDomain,
    path: str,
    disposition: ExecutionMemberDisposition,
) -> ExactExecutionMember:
    return ExactExecutionMember(
        domain=domain,
        exact_lexical_path=path,
        member_type=ExecutionMemberType.REGULAR_FILE,
        disposition=disposition,
        device=10,
        inode=1_000 + index,
        mode=stat.S_IFREG | 0o600,
        byte_count=1_024 + index,
        mtime_ns=1_700_000_000_000_000_000 + index,
        content_digest=_digest(800 + index),
    )


def _component_members(mode: RetentionMode):
    runtime_disposition = (
        ExecutionMemberDisposition.RETAIN
        if mode is RetentionMode.APP_ONLY
        else ExecutionMemberDisposition.REMOVE_EXACT_ENTRY
    )
    return {
        ExecutionMemberDomain.PRIVATE_STATE: (
            _member(1, ExecutionMemberDomain.PRIVATE_STATE, "/private/config.yaml", ExecutionMemberDisposition.RETAIN),
        ),
        ExecutionMemberDomain.MANAGED_RUNTIME: (
            _member(2, ExecutionMemberDomain.MANAGED_RUNTIME, "/private/runtimes/llama", runtime_disposition),
        ),
        ExecutionMemberDomain.SECURITY_SCOPE: (
            _member(3, ExecutionMemberDomain.SECURITY_SCOPE, "/private/scopes/one", ExecutionMemberDisposition.RETAIN),
        ),
    }


def _migration(
    transaction_index: int = 50,
    *,
    legacy_sidecar_state: LegacySidecarMigrationState = LegacySidecarMigrationState.ABSENT,
):
    return build_migration_plan(
        transaction_id=_uuid(transaction_index),
        predecessor_build_digest=_digest(700),
        candidate_build_digest=_digest(701),
        evidence=MigrationEvidence(
            raw_config_digest=_digest(710),
            semantic_config_digest=_digest(711),
            sqlite_backup_digest=_digest(712),
            usage_outbox_identity_digest=_digest(713),
            private_environment_references_digest=_digest(714),
            runtime_ownership_digest=_digest(715),
            model_provenance_digest=_digest(716),
            storage_authority_digest=_digest(717),
            registration_state_digest=_digest(718),
            legacy_sidecar_evidence_digest=_digest(719),
            pairing_state_digest=_digest(720),
            participation_state_digest=_digest(721),
            rollback_snapshot_digest=_digest(722),
        ),
        legacy_sidecar_state=legacy_sidecar_state,
    )


def _journal(tmp_path: Path, **kwargs) -> NativeLifecycleJournal:
    return NativeLifecycleJournal(
        tmp_path / "private" / "config.yaml",
        clock=Clock(),
        helper_authorization_proof_authority=_helper_proof_authority(),
        **kwargs,
    )


def _execution_manifest_for(
    journal: NativeLifecycleJournal,
    transaction,
    *,
    team_identifier: str = "TEAM123456",
):
    plan = transaction.plan
    source_root = "/Applications/Unified Inference.app"
    clone_root = str(
        journal.execution_manifest_store.expected_clone_path(
            transaction.transaction_id
        )
    )
    source_members = (
        _member(
            20,
            ExecutionMemberDomain.APPLICATION,
            f"{source_root}/Contents/MacOS/Unified Inference",
            ExecutionMemberDisposition.RETAIN,
        ),
        _member(
            21,
            ExecutionMemberDomain.APPLICATION,
            f"{source_root}/Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper",
            ExecutionMemberDisposition.RETAIN,
        ),
        _member(
            22,
            ExecutionMemberDomain.APPLICATION,
            f"{source_root}/Contents/MacOS/mnemosyne-lifecycle-runner",
            ExecutionMemberDisposition.RETAIN,
        ),
    )
    clone_members = (
        _member(
            30,
            ExecutionMemberDomain.RECOVERY_CLONE,
            f"{clone_root}/Contents/MacOS/Unified Inference",
            ExecutionMemberDisposition.RETAIN,
        ),
        _member(
            31,
            ExecutionMemberDomain.RECOVERY_CLONE,
            f"{clone_root}/Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper",
            ExecutionMemberDisposition.RETAIN,
        ),
        _member(
            32,
            ExecutionMemberDomain.RECOVERY_CLONE,
            f"{clone_root}/Contents/MacOS/mnemosyne-lifecycle-runner",
            ExecutionMemberDisposition.RETAIN,
        ),
    )
    build_digest = (
        plan.current_build_digest
        if plan.kind is LifecycleKind.UNINSTALL
        else plan.predecessor_build_digest
    )
    source_identity = SignedCodeIdentity(
        identifier="com.mnemosyne.inference.menu",
        version="0.9.0",
        exact_path=source_root,
        build_digest=build_digest,
        team_identifier=team_identifier,
        code_requirement="identifier com.mnemosyne.inference.menu and anchor apple generic",
        code_directory_digest=_digest(850),
        sealed_resources_digest=_digest(851),
        executable_relative_path="Contents/MacOS/Unified Inference",
        member_inventory_digest=execution_member_inventory_digest(source_members),
    )
    clone_identity = replace(
        source_identity,
        exact_path=clone_root,
        member_inventory_digest=execution_member_inventory_digest(clone_members),
    )
    helper = SignedCodeIdentity(
        identifier="com.mnemosyne.inference.lifecycle-helper",
        version="0.9.0",
        exact_path=f"{clone_root}/Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper",
        build_digest=_digest(852),
        team_identifier=team_identifier,
        code_requirement="identifier com.mnemosyne.inference.lifecycle-helper and anchor apple generic",
        code_directory_digest=_digest(853),
        sealed_resources_digest=_digest(854),
        executable_relative_path="Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper",
        member_inventory_digest=execution_member_inventory_digest(
            (clone_members[1],)
        ),
    )
    runner = SignedCodeIdentity(
        identifier="com.mnemosyne.inference.lifecycle-runner",
        version="0.9.0",
        exact_path=f"{clone_root}/Contents/MacOS/mnemosyne-lifecycle-runner",
        build_digest=_digest(855),
        team_identifier=team_identifier,
        code_requirement="identifier com.mnemosyne.inference.lifecycle-runner and anchor apple generic",
        code_directory_digest=_digest(857),
        sealed_resources_digest=_digest(858),
        executable_relative_path="Contents/MacOS/mnemosyne-lifecycle-runner",
        member_inventory_digest=execution_member_inventory_digest(
            (clone_members[2],)
        ),
    )
    recovery = RecoveryCloneIdentity(
        transaction_id=transaction.transaction_id,
        exact_bundle_path=clone_root,
        source_application_identity_digest=source_identity.identity_digest,
        cloned_application_identity_digest=clone_identity.identity_digest,
        cloned_member_inventory_digest=clone_identity.member_inventory_digest,
    )
    exact_members = [*source_members, *clone_members]
    exclusive_weights = []
    predecessor = None
    candidate = None
    if plan.kind is LifecycleKind.UNINSTALL:
        exact_members.extend(
            member
            for values in _component_members(plan.retention_mode).values()
            for member in values
        )
        for index, item in enumerate(plan.manifest_items, start=1):
            if item.disposition is not WeightDisposition.MOVE_TO_TRASH:
                continue
            files = (
                _member(
                    100 + index,
                    ExecutionMemberDomain.EXCLUSIVE_WEIGHT,
                    item.exact_lexical_path,
                    ExecutionMemberDisposition.REMOVE_EXACT_ENTRY,
                ),
            )
            exclusive_weights.append(
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
        for values in _component_members(RetentionMode.APP_ONLY).values():
            exact_members.extend(values)
        pairing_digest = plan.evidence.pairing_state_digest
        predecessor = source_identity
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
            source_identity,
            exact_path=candidate_root,
            build_digest=plan.candidate_build_digest,
            member_inventory_digest=execution_member_inventory_digest(
                candidate_members
            ),
        )
    manifest = build_private_execution_manifest(
        plan=plan,
        transaction_authority_digest=transaction.authority_digest,
        application=source_identity,
        recovery_application=clone_identity,
        helper=helper,
        runner=runner,
        recovery_clone=recovery,
        exact_members=exact_members,
        exclusive_weights=exclusive_weights,
        pairing_state_digest=pairing_digest,
        predecessor=predecessor,
        candidate=candidate,
    )
    return manifest


def _stage_authorize(journal: NativeLifecycleJournal, transaction):
    manifest = _execution_manifest_for(journal, transaction)
    staged = journal.record_helper_staged(manifest).transaction
    challenge = journal.issue_helper_authorization_challenge(
        transaction.transaction_id
    ).challenge
    acceptance = journal.accept_helper_authorization_receipt(
        _helper_submission(challenge)
    )
    authorization_digest = challenge.authorization_digest.removeprefix(
        "sha256:"
    )
    session_id = challenge.session_id
    authorized = acceptance.transaction
    assert staged.phase is LifecyclePhase.HELPER_STAGED
    return authorized, manifest, authorization_digest, session_id


def _write_legacy_v1_journal(
    path: Path,
    plan,
    phases: tuple[LifecyclePhase, ...],
) -> tuple[str, str]:
    path.parent.mkdir(parents=True, mode=0o700)
    document = plan._journal_dict()
    document["schema_version"] = 1
    document["product"] = {
        "application_name": "Unified Inference.app",
        "application_bundle_id": "com.mnemosyne.inference.menu",
        "launch_agent_label": "com.mnemosyne.inference.agent",
        "service_code_requirement_id": "com.mnemosyne.inference.service",
    }
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    authority = sha256(canonical.encode()).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE native_lifecycle_metadata_v1 (
                singleton INTEGER PRIMARY KEY,
                store_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            );
            CREATE TABLE native_lifecycle_transactions_v1 (
                transaction_id TEXT PRIMARY KEY,
                lifecycle_kind TEXT NOT NULL,
                authority_json TEXT NOT NULL,
                authority_digest TEXT NOT NULL,
                current_phase TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error_code TEXT
            );
            CREATE TABLE native_lifecycle_events_v1 (
                transaction_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                phase TEXT NOT NULL,
                recorded_at REAL NOT NULL,
                error_code TEXT,
                PRIMARY KEY (transaction_id, sequence)
            );
            """
        )
        connection.execute(
            "INSERT INTO native_lifecycle_metadata_v1 VALUES (1, ?, 1)",
            ("mnemosyne-native-lifecycle-journal-v1",),
        )
        connection.execute(
            """
            INSERT INTO native_lifecycle_transactions_v1 VALUES (
                ?, ?, ?, ?, ?, ?, ?, NULL
            )
            """,
            (
                plan.transaction_id,
                plan.kind.value,
                canonical,
                authority,
                phases[-1].value,
                1_788_000_000.0,
                1_788_000_000.0 + len(phases) - 1,
            ),
        )
        for index, phase in enumerate(phases, start=1):
            connection.execute(
                "INSERT INTO native_lifecycle_events_v1 VALUES (?, ?, ?, ?, NULL)",
                (
                    plan.transaction_id,
                    index,
                    phase.value,
                    1_788_000_000.0 + index - 1,
                ),
            )
    os.chmod(path, 0o600)
    return canonical, authority


def _components(plan) -> dict[ComponentKind, ComponentDisposition]:
    return {item.kind: item.disposition for item in plan.components}


def test_app_only_removes_only_exact_product_and_retains_everything_else() -> None:
    weights = (
        _weight(1, WeightOwnership.EXCLUSIVE_MANAGED, fresh=True),
        _weight(2, WeightOwnership.IMPORTED),
    )
    plan = _plan(mode=RetentionMode.APP_ONLY, weights=weights)
    components = _components(plan)
    assert components[ComponentKind.APPLICATION] is ComponentDisposition.REMOVE_EXACT
    assert components[ComponentKind.LAUNCH_AGENT] is ComponentDisposition.REMOVE_EXACT
    for kind in (
        ComponentKind.PRIVATE_STATE,
        ComponentKind.MANAGED_RUNTIMES,
        ComponentKind.SECURITY_SCOPES,
        ComponentKind.PAIRING_STATE,
    ):
        assert components[kind] is ComponentDisposition.RETAIN
    assert all(
        item.disposition is WeightDisposition.RETAIN
        for item in plan.manifest_items
    )
    assert plan.outbox_decision is OutboxDecision.PRESERVE_WITH_STATE


def test_keep_weights_removes_runtimes_but_retains_recovery_state_and_weights() -> None:
    plan = _plan(
        weights=(
            _weight(1, WeightOwnership.EXCLUSIVE_MANAGED, fresh=True),
            _weight(2, WeightOwnership.UNKNOWN),
        )
    )
    components = _components(plan)
    assert components[ComponentKind.MANAGED_RUNTIMES] is ComponentDisposition.REMOVE_PROVEN_MEMBERS
    for kind in (
        ComponentKind.PRIVATE_STATE,
        ComponentKind.SECURITY_SCOPES,
        ComponentKind.PAIRING_STATE,
    ):
        assert components[kind] is ComponentDisposition.RETAIN
    assert components[ComponentKind.APPLICATION] is ComponentDisposition.REMOVE_EXACT
    assert components[ComponentKind.LAUNCH_AGENT] is ComponentDisposition.REMOVE_EXACT
    assert plan.manifest_receipt.retained_count == 2
    assert plan.manifest_receipt.trash_count == 0


@pytest.mark.parametrize(
    "ownership",
    [
        WeightOwnership.IMPORTED,
        WeightOwnership.LM_STUDIO,
        WeightOwnership.EXTERNAL,
        WeightOwnership.SHARED,
        WeightOwnership.AMBIGUOUS,
        WeightOwnership.UNKNOWN,
    ],
)
def test_full_mode_retains_every_nonexclusive_ownership(
    ownership: WeightOwnership,
) -> None:
    plan = _plan(
        mode=RetentionMode.FULL_EXCLUSIVE_MANAGED,
        weights=(
            _weight(1, WeightOwnership.EXCLUSIVE_MANAGED, fresh=True),
            _weight(2, ownership),
        ),
    )
    assert plan.manifest_items[0].disposition is WeightDisposition.MOVE_TO_TRASH
    assert plan.manifest_items[1].disposition is WeightDisposition.RETAIN


def test_full_mode_requires_fresh_exact_proof_before_exclusive_weight_is_eligible() -> None:
    plan = _plan(
        mode=RetentionMode.FULL_EXCLUSIVE_MANAGED,
        weights=(_weight(1, WeightOwnership.EXCLUSIVE_MANAGED),),
    )
    assert plan.manifest_items[0].disposition is WeightDisposition.RETAIN
    assert plan.manifest_items[0].reason_code == "retain_exclusive_managed"


def test_full_mode_downgrades_overlapping_recursive_targets_to_retention() -> None:
    parent = _weight(
        1,
        WeightOwnership.EXCLUSIVE_MANAGED,
        fresh=True,
        path="/Volumes/Athena/models/managed-tree",
    )
    child = _weight(
        2,
        WeightOwnership.EXCLUSIVE_MANAGED,
        fresh=True,
        path="/Volumes/Athena/models/managed-tree/nested-copy",
    )
    plan = _plan(
        mode=RetentionMode.FULL_EXCLUSIVE_MANAGED,
        weights=(parent, child),
    )
    assert all(
        item.disposition is WeightDisposition.RETAIN
        and item.reason_code == "retain_overlapping_authority"
        for item in plan.manifest_items
    )


@pytest.mark.parametrize(
    ("count", "decision"),
    [
        (1, OutboxDecision.EMPTY_CONFIRMED),
        (0, OutboxDecision.RECOVERY_CAPSULE),
        (0, OutboxDecision.EXPLICIT_ABANDONMENT),
    ],
)
def test_reinstall_safe_uninstall_rejects_any_destructive_outbox_decision(
    count: int, decision: OutboxDecision
) -> None:
    with pytest.raises(NativeLifecycleConflictError) as error:
        _plan(outbox_count=count, outbox_decision=decision)
    assert error.value.code == "native_lifecycle_outbox_decision_conflict"


def test_nonzero_outbox_is_retained_with_accounting_state() -> None:
    plan = _plan(
        outbox_count=9,
        outbox_decision=OutboxDecision.PRESERVE_WITH_STATE,
    )
    assert plan.token_outbox_count == 9
    assert plan.outbox_decision is OutboxDecision.PRESERVE_WITH_STATE
    assert plan.outbox_evidence_digest == _digest(6)


def test_legacy_empty_outbox_decision_remains_readable_for_in_place_upgrade() -> None:
    plan = _plan(
        outbox_count=0,
        outbox_decision=OutboxDecision.EMPTY_CONFIRMED,
    )
    assert plan.outbox_decision is OutboxDecision.EMPTY_CONFIRMED
    # New plans do not emit the legacy decision; this compatibility exists so
    # an incomplete v2 recovery journal cannot prevent a newer service start.
    assert _plan().outbox_decision is OutboxDecision.PRESERVE_WITH_STATE


def test_manifest_preserves_exact_lexical_symlink_spelling_and_storage_authority(
    tmp_path: Path,
) -> None:
    exact = "/Volumes/Athena/models/by-symlink/GLM 5.3 Flash"
    plan = _plan(weights=(_weight(1, WeightOwnership.IMPORTED, path=exact),))
    store = RetentionManifestStore(tmp_path / "private" / "config.yaml")
    receipt = store.write(plan.private_manifest)
    assert receipt == plan.manifest_receipt
    manifest = store.read(plan.transaction_id)
    assert manifest.items[0].exact_lexical_path == exact
    assert manifest.items[0].storage_location_id == _uuid(301)
    assert manifest.items[0].storage_binding_generation == 2
    assert manifest.items[0].volume_uuid == "VOLUME-1"
    assert manifest.items[0].scope_id == _digest(401)
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path_for(plan.transaction_id).stat().st_mode) == 0o600


def test_manifest_write_is_idempotent_and_conflicting_transaction_is_refused(
    tmp_path: Path,
) -> None:
    store = RetentionManifestStore(tmp_path / "private" / "config.yaml")
    first = _plan(weights=(_weight(1, WeightOwnership.IMPORTED),))
    assert store.write(first.private_manifest) == store.write(first.private_manifest)
    second = _plan(weights=(_weight(2, WeightOwnership.EXTERNAL),))
    with pytest.raises(NativeLifecycleConflictError) as error:
        store.write(second.private_manifest)
    assert error.value.code == "native_lifecycle_retention_manifest_conflict"


@pytest.mark.parametrize(
    "path",
    ["relative/model", "/", "/Volumes/../private", "/Volumes//model", "/tmp/model\nname"],
)
def test_retention_manifest_rejects_unsafe_or_ambiguous_paths(path: str) -> None:
    with pytest.raises(NativeLifecycleConflictError) as error:
        _plan(weights=(_weight(1, WeightOwnership.IMPORTED, path=path),))
    assert error.value.code == "native_lifecycle_weight_path_invalid"


def test_public_preview_exposes_manifest_digest_and_counts_but_no_private_authority() -> None:
    exact = "/Volumes/Athena/models/private-name"
    plan = _plan(weights=(_weight(1, WeightOwnership.IMPORTED, path=exact),))
    payload = json.dumps(plan.to_public_dict(), sort_keys=True)
    assert plan.manifest_receipt.digest in payload
    assert exact not in payload
    assert _uuid(301) not in payload
    assert _digest(401) not in payload
    assert "bookmark" not in payload.casefold()
    assert "secret" not in payload.casefold()


def test_private_config_adjacent_wal_journal_and_manifest_have_strict_modes(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    mutation = journal.prepare(_plan())
    assert mutation.transaction.phase is LifecyclePhase.PREPARED
    assert journal.path == (
        tmp_path / "private" / "state" / "native-lifecycle" / "journal.sqlite3"
    )
    assert stat.S_IMODE(journal.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    manifest_path = journal.manifest_store.path_for(_uuid(1))
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute(
            "SELECT store_id, schema_version FROM native_lifecycle_metadata_v2"
        ).fetchone() == ("mnemosyne-native-lifecycle-journal-v2", 2)


def test_journal_is_path_free_while_private_manifest_contains_the_exact_path(
    tmp_path: Path,
) -> None:
    exact = "/Volumes/Athena/models/nested/symlink-name"
    journal = _journal(tmp_path)
    journal.initialize()
    journal.prepare(
        _plan(weights=(_weight(1, WeightOwnership.LM_STUDIO, path=exact),))
    )
    journal_bytes = journal.path.read_bytes()
    manifest_bytes = journal.manifest_store.path_for(_uuid(1)).read_bytes()
    assert exact.encode() not in journal_bytes
    assert exact.encode() in manifest_bytes
    with sqlite3.connect(journal.path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(native_lifecycle_transactions_v2)"
            )
        }
    assert not any(
        forbidden in column.casefold()
        for column in columns
        for forbidden in ("path", "bookmark", "secret", "credential")
    )


def test_helper_stage_and_authorization_receipts_survive_restart_without_paths(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_plan()).transaction
    transaction, manifest, authorization, session = _stage_authorize(
        journal, prepared
    )
    clone_path = manifest.recovery_clone.exact_bundle_path
    application_path = manifest.application.exact_path
    manifest_path = journal.execution_manifest_store.path_for(
        transaction.transaction_id
    )

    assert clone_path.encode() in manifest_path.read_bytes()
    assert application_path.encode() in manifest_path.read_bytes()
    assert clone_path.encode() not in journal.path.read_bytes()
    assert application_path.encode() not in journal.path.read_bytes()
    assert clone_path not in json.dumps(transaction.plan.to_public_dict())
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM native_lifecycle_helper_stage_receipts_v2"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM native_lifecycle_authorization_receipts_v2"
        ).fetchone()[0] == 1

    restarted = _journal(tmp_path)
    restarted.initialize()
    current = restarted.get(transaction.transaction_id)
    assert current is not None
    assert current.phase is LifecyclePhase.AUTHORIZED
    stage, receipt = restarted.require_helper_authority(
        transaction_id=current.transaction_id,
        authority_digest=current.authority_digest,
        execution_manifest_digest=manifest.receipt.manifest_digest,
        helper_build_digest=manifest.helper.build_digest,
        authorization_digest=authorization,
        helper_session_id=session,
        now=1_788_100_050.0,
    )
    assert stage.recovery_clone_identity_digest == (
        manifest.recovery_clone.identity_digest
    )
    assert receipt.authorization_digest == authorization


def _helper_submission(challenge, **changes):
    payload = {
        **challenge.to_public_dict(),
        "authorization_digest": challenge.authorization_digest,
        "authenticated_at": float(challenge.issued_at),
        "authorization_proof": "_" * 64,
    }
    payload.update(changes)
    submission = helper_authorization_submission_from_mapping(payload)
    if "authorization_proof" not in changes:
        payload["authorization_proof"] = hmac.new(
            _HELPER_PROOF_KEY, submission.proof_bytes(), sha256
        ).hexdigest()
        submission = helper_authorization_submission_from_mapping(payload)
    return submission


def test_one_time_helper_challenge_binds_and_authorizes_exact_staged_authority(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_plan()).transaction
    manifest = _execution_manifest_for(journal, prepared)
    staged = journal.record_helper_staged(manifest).transaction

    issued = journal.issue_helper_authorization_challenge(
        staged.transaction_id
    )
    replay = journal.issue_helper_authorization_challenge(
        staged.transaction_id
    )
    challenge = issued.challenge
    assert issued.replayed is False
    assert replay.replayed is True
    assert replay.challenge == challenge
    serialized = json.dumps(challenge.to_public_dict(), sort_keys=True)
    assert manifest.application.exact_path not in serialized
    assert manifest.recovery_clone.exact_bundle_path not in serialized
    assert challenge.transaction_authority_digest == (
        "sha256:" + staged.authority_digest
    )
    assert challenge.execution_manifest_digest == (
        "sha256:" + manifest.receipt.manifest_digest
    )
    assert challenge.recovery_clone_identity_digest == (
        "sha256:" + manifest.recovery_clone.identity_digest
    )
    assert journal.helper_authorization_pending_count() == 1

    accepted = journal.accept_helper_authorization_receipt(
        _helper_submission(challenge)
    )
    assert accepted.transaction.phase is LifecyclePhase.AUTHORIZED
    assert accepted.authorization_digest == challenge.authorization_digest
    assert journal.helper_authorization_pending_count() == 0
    assert journal.helper_authorization_status(staged.transaction_id)["state"] == (
        "authorized"
    )
    with pytest.raises(NativeLifecycleConflictError) as replayed:
        journal.accept_helper_authorization_receipt(
            _helper_submission(challenge)
        )
    assert replayed.value.code == "native_lifecycle_helper_authority_replayed"


def test_helper_receipt_tampering_does_not_consume_the_live_challenge(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_plan()).transaction
    manifest = _execution_manifest_for(journal, prepared)
    journal.record_helper_staged(manifest)
    challenge = journal.issue_helper_authorization_challenge(
        prepared.transaction_id
    ).challenge

    for key in (
        "transaction_authority_digest",
        "execution_manifest_digest",
        "recovery_clone_identity_digest",
        "expected_helper_build_digest",
        "expected_code_requirement_digest",
        "expected_app_build_digest",
    ):
        with pytest.raises(NativeLifecycleConflictError) as tampered:
            journal.accept_helper_authorization_receipt(
                _helper_submission(challenge, **{key: "sha256:" + _digest(999)})
            )
        assert tampered.value.code == "native_lifecycle_helper_authority_mismatch"
        assert journal.helper_authorization_pending_count() == 1

    with pytest.raises(NativeLifecycleConflictError) as wrong_digest:
        journal.accept_helper_authorization_receipt(
            _helper_submission(
                challenge,
                authorization_digest="sha256:" + _digest(998),
            )
        )
    assert wrong_digest.value.code == "native_lifecycle_helper_authority_mismatch"
    assert journal.accept_helper_authorization_receipt(
        _helper_submission(challenge)
    ).transaction.phase is LifecyclePhase.AUTHORIZED


class ManualClock:
    def __init__(self, value: float = 1_788_100_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_helper_challenge_expiry_cancellation_and_restart_are_durable(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    config_path = tmp_path / "private" / "config.yaml"
    journal = NativeLifecycleJournal(
        config_path,
        clock=clock,
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    journal.initialize()
    prepared = journal.prepare(_plan()).transaction
    journal.record_helper_staged(_execution_manifest_for(journal, prepared))
    challenge = journal.issue_helper_authorization_challenge(
        prepared.transaction_id,
        lifetime_seconds=5,
    ).challenge
    journal.close()

    restarted = NativeLifecycleJournal(
        config_path,
        clock=clock,
        helper_authorization_proof_authority=_helper_proof_authority(),
    )
    restarted.initialize()
    exact_replay = restarted.issue_helper_authorization_challenge(
        prepared.transaction_id,
        lifetime_seconds=5,
    )
    assert exact_replay.replayed is True
    assert exact_replay.challenge == challenge
    cancelled = restarted.cancel_helper_authorization_challenge(
        transaction_id=prepared.transaction_id,
        nonce=challenge.nonce,
        session_id=challenge.session_id,
    )
    assert cancelled.replayed is False
    assert restarted.cancel_helper_authorization_challenge(
        transaction_id=prepared.transaction_id,
        nonce=challenge.nonce,
        session_id=challenge.session_id,
    ).replayed is True
    with pytest.raises(NativeLifecycleConflictError) as cancelled_receipt:
        restarted.accept_helper_authorization_receipt(
            _helper_submission(challenge)
        )
    assert cancelled_receipt.value.code == (
        "native_lifecycle_helper_authority_cancelled"
    )

    replacement = restarted.issue_helper_authorization_challenge(
        prepared.transaction_id,
        lifetime_seconds=5,
    ).challenge
    assert replacement.nonce != challenge.nonce
    clock.value = replacement.expires_at + 1
    with pytest.raises(NativeLifecycleConflictError) as expired:
        restarted.accept_helper_authorization_receipt(
            _helper_submission(replacement)
        )
    assert expired.value.code == "native_lifecycle_helper_authority_expired"
    next_challenge = restarted.issue_helper_authorization_challenge(
        prepared.transaction_id,
        lifetime_seconds=5,
    ).challenge
    assert next_challenge.nonce not in {challenge.nonce, replacement.nonce}


def test_ad_hoc_helper_identity_cannot_mint_real_authorization(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_plan()).transaction
    manifest = _execution_manifest_for(
        journal,
        prepared,
        team_identifier="ADHOC00000",
    )
    journal.record_helper_staged(manifest)
    with pytest.raises(NativeLifecycleConflictError) as unavailable:
        journal.issue_helper_authorization_challenge(prepared.transaction_id)
    assert unavailable.value.code == (
        "native_lifecycle_helper_authority_unavailable"
    )


@pytest.mark.parametrize(
    "retained_path",
    (
        "/Applications",
        "/Applications/Unified Inference.app",
        "/Applications/Unified Inference.app/models/retained.gguf",
        "/private",
        "/private/runtimes/llama",
        "/private/runtimes/llama/retained.gguf",
    ),
)
def test_retained_weight_cannot_overlap_any_exact_remove_authority(
    tmp_path: Path,
    retained_path: str,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    plan = _plan(
        weights=(
            _weight(
                1,
                WeightOwnership.IMPORTED,
                path=retained_path,
            ),
        )
    )
    prepared = journal.prepare(plan).transaction
    manifest = _execution_manifest_for(journal, prepared)

    with pytest.raises(NativeLifecycleConflictError) as overlap:
        journal.record_helper_staged(manifest)
    assert overlap.value.code == "native_lifecycle_retained_weight_overlap"


def test_retained_weight_overlap_uses_path_components_not_string_prefixes(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    plan = _plan(
        weights=(
            _weight(
                1,
                WeightOwnership.IMPORTED,
                path="/private/config.yaml.backup/model.gguf",
            ),
        )
    )
    prepared = journal.prepare(plan).transaction
    manifest = _execution_manifest_for(journal, prepared)

    assert journal.record_helper_staged(manifest).transaction.phase is (
        LifecyclePhase.HELPER_STAGED
    )


def test_helper_phases_cannot_be_forced_or_authorized_before_exact_staging(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_plan()).transaction
    for phase in (LifecyclePhase.HELPER_STAGED, LifecyclePhase.AUTHORIZED):
        with pytest.raises(NativeLifecycleConflictError) as error:
            journal.advance(prepared.transaction_id, phase)
        assert error.value.code == "native_lifecycle_phase_out_of_order"
    with pytest.raises(NativeLifecycleConflictError) as error:
        journal.issue_helper_authorization_challenge(prepared.transaction_id)
    assert error.value.code == "native_lifecycle_phase_out_of_order"
    assert not hasattr(journal, "record_authorized")


def test_execution_manifest_rejects_a_clone_with_a_different_signing_team(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_plan()).transaction
    manifest = _execution_manifest_for(journal, prepared)
    mismatched = replace(
        manifest,
        helper=replace(manifest.helper, team_identifier="OTHERTEAM"),
    )
    with pytest.raises(NativeLifecycleConflictError) as error:
        journal.record_helper_staged(mismatched)
    assert error.value.code == "native_lifecycle_recovery_clone_identity_invalid"


@pytest.mark.parametrize("target", ["state", "journal", "manifest"])
def test_symlinked_private_artifact_fails_closed(tmp_path: Path, target: str) -> None:
    private = tmp_path / "private"
    private.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if target == "state":
        (private / "state").symlink_to(outside, target_is_directory=True)
    elif target == "journal":
        directory = private / "state" / "native-lifecycle"
        directory.mkdir(parents=True)
        (directory / "journal.sqlite3").symlink_to(outside / "journal.sqlite3")
    else:
        directory = private / "state" / "native-lifecycle"
        directory.mkdir(parents=True)
        (directory / "retention-manifests").symlink_to(
            outside, target_is_directory=True
        )
    journal = _journal(tmp_path)
    if target == "manifest":
        journal.initialize()
        with pytest.raises(NativeLifecycleIntegrityError):
            journal.prepare(_plan())
    else:
        with pytest.raises(NativeLifecycleIntegrityError):
            journal.initialize()


def test_uninstall_phase_order_is_strict_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    transaction_id = _uuid(1)
    assert journal.prepare(_plan()).replayed is False
    assert journal.prepare(_plan()).replayed is True
    with pytest.raises(NativeLifecycleConflictError) as error:
        journal.advance(transaction_id, LifecyclePhase.OUTBOX_RESOLVED)
    assert error.value.code == "native_lifecycle_phase_out_of_order"
    transaction, _manifest, _authorization, _session = _stage_authorize(
        journal, journal.get(transaction_id)
    )
    assert transaction.phase is LifecyclePhase.AUTHORIZED
    phases = (
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
    for phase in phases:
        mutation = journal.advance(transaction_id, phase)
        assert mutation.transaction.phase is phase
        assert mutation.replayed is False
        assert journal.advance(transaction_id, phase).replayed is True
    assert journal.get(transaction_id).terminal is True
    assert journal.list_incomplete() == ()


def test_migration_captures_content_free_snapshot_evidence_and_can_commit(
    tmp_path: Path,
) -> None:
    plan = _migration()
    serialized = json.dumps(plan._journal_dict(), sort_keys=True)
    assert "/" not in serialized
    assert "bookmark" not in serialized.casefold()
    journal = _journal(tmp_path)
    journal.initialize()
    mutation = journal.prepare(plan)
    assert mutation.transaction.kind is LifecycleKind.MIGRATION
    assert mutation.transaction.phase is LifecyclePhase.DISCOVERED
    transaction, _manifest, _authorization, _session = _stage_authorize(
        journal, mutation.transaction
    )
    for phase in (
        LifecyclePhase.PREFLIGHTED,
        LifecyclePhase.DRAINED,
        LifecyclePhase.SNAPSHOTTED,
        LifecyclePhase.PREDECESSOR_STOPPED,
        LifecyclePhase.CANDIDATE_INSTALLED,
        LifecyclePhase.CANDIDATE_STARTED,
        LifecyclePhase.VALIDATED,
        LifecyclePhase.COMMITTED,
    ):
        journal.advance(plan.transaction_id, phase)
    assert journal.get(plan.transaction_id).terminal is True


def test_migration_manifest_requires_exact_installed_predecessor_identity(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_migration()).transaction
    manifest = _execution_manifest_for(journal, prepared)
    assert manifest.predecessor is not None
    mismatched = replace(
        manifest,
        predecessor=replace(
            manifest.predecessor,
            exact_path="/Applications/Other Unified Inference.app",
        ),
    )

    with pytest.raises(NativeLifecycleConflictError) as error:
        journal.record_helper_staged(mismatched)
    assert error.value.code == "native_lifecycle_candidate_identity_conflict"


def test_migration_manifest_requires_candidate_signing_team_continuity(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_migration()).transaction
    manifest = _execution_manifest_for(journal, prepared)
    assert manifest.candidate is not None
    mismatched = replace(
        manifest,
        candidate=replace(
            manifest.candidate,
            team_identifier="OTHERTEAM1",
        ),
    )

    with pytest.raises(NativeLifecycleConflictError) as error:
        journal.record_helper_staged(mismatched)
    assert error.value.code == "native_lifecycle_candidate_identity_conflict"


def test_migration_manifest_requires_exact_candidate_member_inventory(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_migration()).transaction
    manifest = _execution_manifest_for(journal, prepared)
    detached = replace(
        manifest,
        exact_members=tuple(
            member
            for member in manifest.exact_members
            if member.domain is not ExecutionMemberDomain.CANDIDATE_APPLICATION
        ),
    )

    with pytest.raises(NativeLifecycleConflictError) as error:
        journal.record_helper_staged(detached)
    assert error.value.code == "native_lifecycle_candidate_identity_conflict"


def test_legacy_sidecar_migration_names_only_the_exact_job_after_inheritance() -> None:
    plan = _migration(
        legacy_sidecar_state=LegacySidecarMigrationState.INHERITANCE_DURABLY_VALIDATED
    )
    payload = plan.to_public_dict()
    assert payload["legacy_sidecar"] == {
        "label": LEGACY_SIDECAR_LABEL,
        "state": "inheritance_durably_validated",
    }


def test_migration_can_branch_to_exact_restore_only_after_predecessor_stopped(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    plan = _migration()
    prepared = journal.prepare(plan).transaction
    _stage_authorize(journal, prepared)
    for phase in (
        LifecyclePhase.PREFLIGHTED,
        LifecyclePhase.DRAINED,
        LifecyclePhase.SNAPSHOTTED,
    ):
        journal.advance(plan.transaction_id, phase)
    with pytest.raises(NativeLifecycleConflictError):
        journal.advance(plan.transaction_id, LifecyclePhase.RESTORED)
    journal.advance(plan.transaction_id, LifecyclePhase.PREDECESSOR_STOPPED)
    journal.advance(plan.transaction_id, LifecyclePhase.CANDIDATE_INSTALLED)
    restored = journal.advance(plan.transaction_id, LifecyclePhase.RESTORED)
    assert restored.transaction.terminal is True


def test_recovery_decision_distinguishes_replay_from_journal_catch_up(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_plan()).transaction
    transaction, _manifest, _authorization, _session = _stage_authorize(
        journal, prepared
    )
    execute = decide_lifecycle_recovery(
        transaction,
        next_effect=RecoveryObservation.NEEDS_ACTION,
        prior_effects=PriorEffectsState.INTACT,
    )
    assert execute.action is LifecycleRecoveryAction.EXECUTE_PHASE
    assert execute.phase is LifecyclePhase.SERVICE_QUIESCED
    record = decide_lifecycle_recovery(
        transaction,
        next_effect=RecoveryObservation.EFFECT_SATISFIED,
        prior_effects=PriorEffectsState.INTACT,
    )
    assert record.action is LifecycleRecoveryAction.RECORD_PHASE
    assert record.phase is LifecyclePhase.SERVICE_QUIESCED


@pytest.mark.parametrize(
    ("next_effect", "prior_effects", "reason"),
    [
        (
            RecoveryObservation.CONFLICT,
            PriorEffectsState.INTACT,
            "recovery_observation_conflict",
        ),
        (
            RecoveryObservation.UNAVAILABLE,
            PriorEffectsState.INTACT,
            "recovery_observation_unavailable",
        ),
        (
            RecoveryObservation.NEEDS_ACTION,
            PriorEffectsState.CONFLICT,
            "prior_effect_conflict",
        ),
        (
            RecoveryObservation.NEEDS_ACTION,
            PriorEffectsState.UNAVAILABLE,
            "prior_effect_unavailable",
        ),
    ],
)
def test_recovery_observation_uncertainty_always_requires_manual_recovery(
    tmp_path: Path,
    next_effect: RecoveryObservation,
    prior_effects: PriorEffectsState,
    reason: str,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_plan()).transaction
    transaction, _manifest, _authorization, _session = _stage_authorize(
        journal, prepared
    )
    decision = decide_lifecycle_recovery(
        transaction, next_effect=next_effect, prior_effects=prior_effects
    )
    assert decision.action is LifecycleRecoveryAction.MANUAL_RECOVERY
    assert decision.reason_code == reason


def test_migration_recovery_rollback_is_closed_and_phase_fenced(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    plan = _migration()
    prepared = journal.prepare(plan).transaction
    transaction, _manifest, _authorization, _session = _stage_authorize(
        journal, prepared
    )
    too_early = decide_lifecycle_recovery(
        transaction,
        next_effect=RecoveryObservation.NEEDS_ACTION,
        prior_effects=PriorEffectsState.INTACT,
        rollback_requested=True,
    )
    assert too_early.reason_code == "rollback_not_available"
    for phase in (
        LifecyclePhase.PREFLIGHTED,
        LifecyclePhase.DRAINED,
        LifecyclePhase.SNAPSHOTTED,
        LifecyclePhase.PREDECESSOR_STOPPED,
    ):
        transaction = journal.advance(plan.transaction_id, phase).transaction
    rollback = decide_lifecycle_recovery(
        transaction,
        next_effect=RecoveryObservation.EFFECT_SATISFIED,
        prior_effects=PriorEffectsState.INTACT,
        rollback_requested=True,
    )
    assert rollback.action is LifecycleRecoveryAction.RECORD_PHASE
    assert rollback.phase is LifecyclePhase.RESTORED


def test_manual_recovery_is_durable_and_replayed(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    transaction_id = _uuid(1)
    journal.prepare(_plan())
    first = journal.mark_manual_recovery(
        transaction_id, "retention_manifest_unavailable"
    )
    second = journal.mark_manual_recovery(
        transaction_id, "retention_manifest_unavailable"
    )
    assert first.transaction.phase is LifecyclePhase.MANUAL_RECOVERY
    assert first.replayed is False
    assert second.replayed is True
    assert journal.list_incomplete() == (first.transaction,)


def test_manual_recovery_exposes_only_closed_exact_authority_actions(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    initial = journal.prepare(_plan(transaction_index=10)).transaction
    initial = journal.mark_manual_recovery(
        initial.transaction_id, "retention_manifest_unavailable"
    ).transaction
    abort = decide_manual_recovery_action(
        initial,
        requested_action=LifecycleRecoveryAction.ABORT_BEFORE_ANY_EFFECT,
        exact_authority_matches=True,
        next_effect=RecoveryObservation.NEEDS_ACTION,
        any_effect_observed=False,
    )
    assert abort.action is LifecycleRecoveryAction.ABORT_BEFORE_ANY_EFFECT
    assert abort.phase is None
    resume = decide_manual_recovery_action(
        initial,
        requested_action=LifecycleRecoveryAction.RESUME_IDENTICAL,
        exact_authority_matches=True,
        next_effect=RecoveryObservation.NEEDS_ACTION,
        any_effect_observed=False,
    )
    assert resume.action is LifecycleRecoveryAction.RESUME_IDENTICAL
    assert resume.phase is LifecyclePhase.HELPER_STAGED
    wrong_observation = decide_manual_recovery_action(
        initial,
        requested_action=(
            LifecycleRecoveryAction.RECORD_CONCLUSIVELY_SATISFIED
        ),
        exact_authority_matches=True,
        next_effect=RecoveryObservation.NEEDS_ACTION,
        any_effect_observed=False,
    )
    assert wrong_observation.action is LifecycleRecoveryAction.MANUAL_RECOVERY

    progressed = journal.prepare(_plan(transaction_index=11)).transaction
    progressed, _manifest, _authorization, _session = _stage_authorize(
        journal, progressed
    )
    progressed = journal.advance(
        progressed.transaction_id, LifecyclePhase.SERVICE_QUIESCED
    ).transaction
    progressed = journal.mark_manual_recovery(
        progressed.transaction_id, "prior_effect_conflict"
    ).transaction
    denied_abort = decide_manual_recovery_action(
        progressed,
        requested_action=LifecycleRecoveryAction.ABORT_BEFORE_ANY_EFFECT,
        exact_authority_matches=True,
        next_effect=RecoveryObservation.NEEDS_ACTION,
        any_effect_observed=True,
    )
    assert denied_abort.action is LifecycleRecoveryAction.MANUAL_RECOVERY
    assert denied_abort.reason_code == "prior_effect_conflict"

    migration = journal.prepare(_migration(transaction_index=60)).transaction
    migration, _manifest, _authorization, _session = _stage_authorize(
        journal, migration
    )
    for phase in (
        LifecyclePhase.PREFLIGHTED,
        LifecyclePhase.DRAINED,
        LifecyclePhase.SNAPSHOTTED,
        LifecyclePhase.PREDECESSOR_STOPPED,
    ):
        migration = journal.advance(migration.transaction_id, phase).transaction
    migration = journal.mark_manual_recovery(
        migration.transaction_id, "candidate_identity_conflict"
    ).transaction
    restore = decide_manual_recovery_action(
        migration,
        requested_action=LifecycleRecoveryAction.RESTORE_EXACT_PREDECESSOR,
        exact_authority_matches=True,
        next_effect=RecoveryObservation.NEEDS_ACTION,
        any_effect_observed=True,
    )
    assert restore.action is LifecycleRecoveryAction.RESTORE_EXACT_PREDECESSOR
    assert restore.phase is LifecyclePhase.RESTORED


def test_incomplete_v1_journal_is_immutably_quarantined_for_manual_recovery(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    plan = _plan()
    canonical, authority = _write_legacy_v1_journal(
        journal.path,
        plan,
        (LifecyclePhase.PREPARED, LifecyclePhase.SERVICE_QUIESCED),
    )
    journal.initialize()

    recovered = journal.get(plan.transaction_id)
    assert recovered is not None
    assert recovered.contract_version == 1
    assert recovered.phase is LifecyclePhase.MANUAL_RECOVERY
    assert recovered.recovery_from_phase is LifecyclePhase.SERVICE_QUIESCED
    assert recovered.error_code == "legacy_v1_manual_recovery_required"
    assert journal.list_incomplete() == (recovered,)
    with sqlite3.connect(journal.path) as connection:
        source = connection.execute(
            """
            SELECT authority_json, authority_digest, current_phase
              FROM native_lifecycle_transactions_v1
             WHERE transaction_id = ?
            """,
            (plan.transaction_id,),
        ).fetchone()
        receipt = connection.execute(
            """
            SELECT authority_json, authority_digest, original_phase,
                   recovery_error_code
              FROM native_lifecycle_legacy_v1_recovery_v2
             WHERE transaction_id = ?
            """,
            (plan.transaction_id,),
        ).fetchone()
    assert source == (canonical, authority, "service_quiesced")
    assert receipt == (
        canonical,
        authority,
        "service_quiesced",
        "legacy_v1_manual_recovery_required",
    )
    with pytest.raises(NativeLifecycleConflictError) as error:
        journal.prepare(plan)
    assert error.value.code == "native_lifecycle_transaction_conflict"


def test_terminal_v1_journal_remains_terminal_without_v2_reinterpretation(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    plan = _plan()
    _write_legacy_v1_journal(
        journal.path,
        plan,
        (
            LifecyclePhase.PREPARED,
            LifecyclePhase.SERVICE_QUIESCED,
            LifecyclePhase.OUTBOX_RESOLVED,
            LifecyclePhase.AGENT_UNREGISTERED,
            LifecyclePhase.APPLICATION_REMOVED,
            LifecyclePhase.WEIGHTS_RESOLVED,
            LifecyclePhase.RUNTIMES_RESOLVED,
            LifecyclePhase.STATE_RESOLVED,
            LifecyclePhase.COMPLETED,
        ),
    )
    journal.initialize()
    recovered = journal.get(plan.transaction_id)
    assert recovered is not None
    assert recovered.contract_version == 1
    assert recovered.phase is LifecyclePhase.COMPLETED
    assert recovered.recovery_from_phase is None
    assert recovered.error_code is None
    assert recovered.terminal is True
    assert journal.list_incomplete() == ()


def test_corrupt_authority_is_detected_on_restart(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    journal.prepare(_plan())
    with sqlite3.connect(journal.path) as connection:
        connection.execute("DROP TRIGGER native_lifecycle_authority_immutable_v2")
        connection.execute(
            "UPDATE native_lifecycle_transactions_v2 SET authority_json = '{}'"
        )
        connection.commit()
    restarted = _journal(tmp_path)
    with pytest.raises(NativeLifecycleIntegrityError) as error:
        restarted.initialize()
    assert error.value.code == "native_lifecycle_journal_corrupt"


def test_missing_or_changed_private_manifest_is_detected_on_restart(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    journal.prepare(_plan())
    manifest_path = journal.manifest_store.path_for(_uuid(1))
    payload = manifest_path.read_text()
    manifest_path.write_text(payload.replace("VOLUME-1", "VOLUME-X"))
    restarted = _journal(tmp_path)
    with pytest.raises(NativeLifecycleIntegrityError) as error:
        restarted.initialize()
    assert error.value.code == "native_lifecycle_retention_manifest_conflict"


def test_changed_execution_manifest_is_detected_on_restart(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = journal.prepare(_plan()).transaction
    _transaction, manifest, _authorization, _session = _stage_authorize(
        journal, prepared
    )
    manifest_path = journal.execution_manifest_store.path_for(
        prepared.transaction_id
    )
    payload = manifest_path.read_text()
    manifest_path.write_text(payload.replace("TEAM123", "OTHERTEAM"))

    restarted = _journal(tmp_path)
    with pytest.raises(NativeLifecycleIntegrityError) as error:
        restarted.initialize()
    assert error.value.code == "native_lifecycle_execution_manifest_conflict"


def test_capacity_never_prunes_incomplete_recovery_authority(tmp_path: Path) -> None:
    journal = _journal(tmp_path, maximum_transactions=1)
    journal.initialize()
    journal.prepare(_plan(transaction_index=1))
    with pytest.raises(NativeLifecycleCapacityError) as error:
        journal.prepare(_plan(transaction_index=2))
    assert error.value.code == "native_lifecycle_journal_capacity_exhausted"


def test_module_contains_no_product_mutation_or_process_execution_primitives() -> None:
    tree = ast.parse(SOURCE_PATH.read_text())
    forbidden_attributes = {
        "kill",
        "killpg",
        "rmdir",
        "rmtree",
        "run",
        "spawn",
        "system",
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
