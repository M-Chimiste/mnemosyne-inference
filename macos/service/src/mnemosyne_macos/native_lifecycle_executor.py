"""Closed, restart-safe execution orchestration for native lifecycle plans.

This module is deliberately not a macOS implementation.  It contains no
process, launchctl, filesystem-removal, Trash, or code-signing primitive.  A
future bundled and signed helper must implement :class:`NativeLifecycleEffects`
and independently authenticate every short-lived authority before any named
effect can run.  Without both that adapter and an exact verified authority the
executor is preview-only.

The boundary is intentionally narrow:

* mutation methods are named for one fixed lifecycle effect; there is no
  generic command, path, PID, port, or argv entry point;
* product identity comes only from ``PRODUCT_IDENTITY``;
* state/runtime operations receive only immutable member-inventory digests;
* weights can be passed only to the exact-exclusive Trash method after a new
  same-run proof over the private manifest; retained weights are never targets;
* every effect is observed before and after invocation, so a crash between an
  effect and its journal receipt resumes by recording proven state rather than
  repeating work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import math
import threading
import time
from typing import Final, Protocol
from uuid import UUID, uuid4

from .native_lifecycle import (
    ComponentDisposition,
    ComponentKind,
    LifecycleKind,
    LifecycleExecutionClaimState,
    LifecyclePhase,
    LifecycleRecoveryAction,
    LifecycleTransaction,
    MigrationPlan,
    NativeLifecycleConflictError,
    NativeLifecycleJournal,
    NativeLifecycleNotFoundError,
    OutboxDecision,
    PriorEffectsState,
    PRODUCT_IDENTITY,
    RecoveryObservation,
    RetentionManifestItem,
    UninstallPlan,
    WeightDisposition,
    WeightOwnership,
    decide_lifecycle_recovery,
)


LIFECYCLE_HELPER_PROTOCOL_VERSION: Final[int] = 2
LIFECYCLE_HELPER_IDENTIFIER: Final[str] = (
    "com.mnemosyne.inference.lifecycle-helper"
)
_MAX_TIMESTAMP: Final[float] = 4_102_444_800.0
_MAX_STEPS: Final[int] = 16


class HelperAuthorityState(str, Enum):
    DISABLED = "disabled"
    AUTHENTICATED_VERIFIED = "authenticated_verified"


class HelperAuthorizationObservation(str, Enum):
    AUTHORIZED = "authorized"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class ExactTrashProofState(str, Enum):
    FRESH_EXACT = "fresh_exact"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class LifecycleExecutionState(str, Enum):
    DISABLED = "disabled"
    BLOCKED = "blocked"
    PROGRESSED = "progressed"
    RETRYABLE = "retryable"
    MANUAL_RECOVERY = "manual_recovery"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class LifecycleHelperAuthority:
    """Memory-only authority produced by a separately verified helper flow."""

    state: HelperAuthorityState
    helper_protocol_version: int | None = None
    helper_identifier: str | None = None
    product_bundle_id: str | None = None
    transaction_id: str | None = None
    transaction_authority_digest: str | None = None
    execution_manifest_digest: str | None = None
    recovery_clone_identity_digest: str | None = None
    helper_build_digest: str | None = None
    authorization_digest: str | None = None
    session_id: str | None = None
    expires_at: float | None = None

    @classmethod
    def disabled(cls) -> "LifecycleHelperAuthority":
        return cls(state=HelperAuthorityState.DISABLED)

    @classmethod
    def authenticated_verified(
        cls,
        *,
        transaction_id: str,
        transaction_authority_digest: str,
        execution_manifest_digest: str,
        recovery_clone_identity_digest: str,
        helper_build_digest: str,
        authorization_digest: str,
        session_id: str,
        expires_at: float,
    ) -> "LifecycleHelperAuthority":
        authority = cls(
            state=HelperAuthorityState.AUTHENTICATED_VERIFIED,
            helper_protocol_version=LIFECYCLE_HELPER_PROTOCOL_VERSION,
            helper_identifier=LIFECYCLE_HELPER_IDENTIFIER,
            product_bundle_id=PRODUCT_IDENTITY.application_bundle_id,
            transaction_id=_uuid(transaction_id),
            transaction_authority_digest=_digest(
                transaction_authority_digest
            ),
            execution_manifest_digest=_digest(execution_manifest_digest),
            recovery_clone_identity_digest=_digest(
                recovery_clone_identity_digest
            ),
            helper_build_digest=_digest(helper_build_digest),
            authorization_digest=_digest(authorization_digest),
            session_id=_uuid(session_id),
            expires_at=_timestamp(expires_at),
        )
        authority.validate()
        return authority

    def validate(self) -> None:
        if self.state is HelperAuthorityState.DISABLED:
            if any(
                value is not None
                for value in (
                    self.helper_protocol_version,
                    self.helper_identifier,
                    self.product_bundle_id,
                    self.transaction_id,
                    self.transaction_authority_digest,
                    self.execution_manifest_digest,
                    self.recovery_clone_identity_digest,
                    self.helper_build_digest,
                    self.authorization_digest,
                    self.session_id,
                    self.expires_at,
                )
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_invalid"
                )
            return
        if (
            self.state is not HelperAuthorityState.AUTHENTICATED_VERIFIED
            or self.helper_protocol_version
            != LIFECYCLE_HELPER_PROTOCOL_VERSION
            or self.helper_identifier != LIFECYCLE_HELPER_IDENTIFIER
            or self.product_bundle_id
            != PRODUCT_IDENTITY.application_bundle_id
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_helper_authority_invalid"
            )
        _uuid(self.transaction_id)
        _digest(self.transaction_authority_digest)
        _digest(self.execution_manifest_digest)
        _digest(self.recovery_clone_identity_digest)
        _digest(self.helper_build_digest)
        _digest(self.authorization_digest)
        _uuid(self.session_id)
        _timestamp(self.expires_at)


@dataclass(frozen=True, slots=True)
class OutboxResolutionRequest:
    transaction_id: str
    token_outbox_count: int
    decision: OutboxDecision
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class ComponentResolutionRequest:
    kind: ComponentKind
    disposition: ComponentDisposition
    member_inventory_digest: str


@dataclass(frozen=True, slots=True)
class ExactTrashRequest:
    """One private exact target; only the Trash adapter may receive it."""

    asset_fingerprint: str
    payload_fingerprint: str
    exact_lexical_path: str
    storage_location_id: str
    storage_binding_generation: int
    volume_uuid: str | None
    scope_id: str | None
    installation_id: str
    original_exclusive_proof_digest: str
    byte_count: int | None
    request_digest: str


@dataclass(frozen=True, slots=True)
class ExactTrashProof:
    state: ExactTrashProofState
    request_digests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LifecycleExecutionReport:
    state: LifecycleExecutionState
    transaction: LifecycleTransaction
    phases_recorded: tuple[LifecyclePhase, ...] = ()
    error_code: str | None = None


class NativeLifecycleEffects(Protocol):
    """Implemented only by the authenticated, signed helper boundary."""

    def authorize(
        self,
        authority: LifecycleHelperAuthority,
        transaction: LifecycleTransaction,
    ) -> HelperAuthorizationObservation: ...

    def observe_prior_effects(
        self,
        authority: LifecycleHelperAuthority,
        transaction: LifecycleTransaction,
    ) -> PriorEffectsState: ...

    def observe_phase(
        self,
        authority: LifecycleHelperAuthority,
        transaction: LifecycleTransaction,
        phase: LifecyclePhase,
    ) -> RecoveryObservation: ...

    # Uninstall effects.  Every target is fixed identity or digest authority.
    def quiesce_exact_service(
        self, authority: LifecycleHelperAuthority, transaction_id: str
    ) -> None: ...

    def resolve_token_outbox(
        self,
        authority: LifecycleHelperAuthority,
        request: OutboxResolutionRequest,
    ) -> None: ...

    def resolve_hub_pairing(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        plan: UninstallPlan,
    ) -> None: ...

    def unregister_menu_login_item(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        application_bundle_id: str,
    ) -> None: ...

    def unregister_exact_launch_agent(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        launch_agent_label: str,
    ) -> None: ...

    def remove_exact_application(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        application_bundle_id: str,
    ) -> None: ...

    def quarantine_exact_application(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        application_bundle_id: str,
        execution_manifest_digest: str,
    ) -> None: ...

    def prove_exact_trash_authority(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        requests: tuple[ExactTrashRequest, ...],
    ) -> ExactTrashProof: ...

    def move_exact_exclusive_weights_to_trash(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        manifest_digest: str,
        requests: tuple[ExactTrashRequest, ...],
        retained_count: int,
    ) -> None: ...

    def resolve_managed_runtimes(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        request: ComponentResolutionRequest,
    ) -> None: ...

    def resolve_private_state(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        requests: tuple[ComponentResolutionRequest, ...],
    ) -> None: ...

    def finalize_uninstall(
        self, authority: LifecycleHelperAuthority, transaction_id: str
    ) -> None: ...

    # Migration effects retain weights, storage, scopes, pairing and outbox.
    def preflight_exact_candidate(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        plan: MigrationPlan,
    ) -> None: ...

    def drain_inference(
        self, authority: LifecycleHelperAuthority, transaction_id: str
    ) -> None: ...

    def capture_exact_rollback_snapshot(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        plan: MigrationPlan,
    ) -> None: ...

    def stop_exact_predecessor(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        predecessor_build_digest: str,
    ) -> None: ...

    def start_exact_candidate(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        candidate_build_digest: str,
    ) -> None: ...

    def install_exact_candidate(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        candidate_build_digest: str,
    ) -> None: ...

    def validate_candidate_preservation(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        plan: MigrationPlan,
    ) -> None: ...

    def commit_exact_candidate(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        candidate_build_digest: str,
    ) -> None: ...

    def restore_exact_predecessor(
        self,
        authority: LifecycleHelperAuthority,
        transaction_id: str,
        plan: MigrationPlan,
    ) -> None: ...


class _DisabledEffects:
    """Default adapter: it can authorize and execute nothing."""

    def authorize(self, _authority, _transaction):
        return HelperAuthorizationObservation.REJECTED


class NativeLifecycleExecutor:
    """Drive one journaled plan through observed, named helper effects."""

    def __init__(
        self,
        journal: NativeLifecycleJournal,
        *,
        effects: NativeLifecycleEffects | None = None,
        authority: LifecycleHelperAuthority | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(journal, NativeLifecycleJournal):
            raise TypeError("journal must be a NativeLifecycleJournal")
        self._journal = journal
        self._effects = effects if effects is not None else _DisabledEffects()
        self._authority = authority or LifecycleHelperAuthority.disabled()
        self._clock = clock
        self._lock = threading.RLock()
        self._claim_id = str(uuid4())

    @property
    def execution_enabled(self) -> bool:
        return (
            self._authority.state
            is HelperAuthorityState.AUTHENTICATED_VERIFIED
            and not isinstance(self._effects, _DisabledEffects)
        )

    def run(
        self,
        transaction_id: str,
        *,
        rollback_requested: bool = False,
        maximum_steps: int = _MAX_STEPS,
    ) -> LifecycleExecutionReport:
        transaction_id = _uuid(transaction_id)
        if not isinstance(rollback_requested, bool) or (
            isinstance(maximum_steps, bool)
            or not isinstance(maximum_steps, int)
            or not 1 <= maximum_steps <= _MAX_STEPS
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_request_invalid"
            )
        with self._lock:
            transaction = self._journal.get(transaction_id)
            if transaction is None:
                raise NativeLifecycleNotFoundError(
                    "native_lifecycle_transaction_not_found"
                )
            if transaction.terminal:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.TERMINAL, transaction
                )
            if transaction.phase is LifecyclePhase.MANUAL_RECOVERY:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.MANUAL_RECOVERY,
                    transaction,
                    error_code=transaction.error_code,
                )
            if transaction.phase in {
                LifecyclePhase.PREPARED,
                LifecyclePhase.DISCOVERED,
            }:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.DISABLED,
                    transaction,
                    error_code="native_lifecycle_helper_staging_required",
                )
            if transaction.phase is LifecyclePhase.HELPER_STAGED:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.DISABLED,
                    transaction,
                    error_code="native_lifecycle_helper_authorization_required",
                )
            authority_error = self._authorize(transaction)
            if authority_error is not None:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.DISABLED,
                    transaction,
                    error_code=authority_error,
                )
            if (
                self._authority.session_id is None
                or self._authority.expires_at is None
            ):  # pragma: no cover - validated authority invariant
                return LifecycleExecutionReport(
                    LifecycleExecutionState.DISABLED,
                    transaction,
                    error_code="native_lifecycle_helper_authority_invalid",
                )
            try:
                now = _timestamp(self._clock())
            except Exception:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.DISABLED,
                    transaction,
                    error_code="native_lifecycle_helper_authority_unavailable",
                )
            if now >= self._authority.expires_at:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.DISABLED,
                    transaction,
                    error_code="native_lifecycle_helper_authority_expired",
                )
            claim = self._journal.acquire_execution_claim(
                transaction_id=transaction_id,
                authority_digest=transaction.authority_digest,
                helper_session_id=self._authority.session_id,
                claim_id=self._claim_id,
                expires_at=self._authority.expires_at,
                now=now,
            )
            if claim.state is LifecycleExecutionClaimState.EXPIRED:
                current = self._journal.get(transaction_id)
                if (
                    current is not None
                    and current.phase is LifecyclePhase.MANUAL_RECOVERY
                ):
                    return LifecycleExecutionReport(
                        LifecycleExecutionState.MANUAL_RECOVERY,
                        current,
                        error_code=current.error_code,
                    )
                return LifecycleExecutionReport(
                    LifecycleExecutionState.BLOCKED,
                    transaction,
                    error_code="native_lifecycle_execution_claim_expired",
                )
            if claim.state is not LifecycleExecutionClaimState.ACQUIRED:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.BLOCKED,
                    transaction,
                    error_code="native_lifecycle_execution_claim_conflict",
                )

            report = self._run_acquired_claim(
                transaction,
                rollback_requested=rollback_requested,
                maximum_steps=maximum_steps,
            )
            self._journal.release_execution_claim(
                transaction_id=transaction_id,
                authority_digest=transaction.authority_digest,
                helper_session_id=self._authority.session_id,
                claim_id=self._claim_id,
            )
            return report

    def _run_acquired_claim(
        self,
        transaction: LifecycleTransaction,
        *,
        rollback_requested: bool,
        maximum_steps: int,
    ) -> LifecycleExecutionReport:
        transaction_id = transaction.transaction_id
        other = tuple(
            row
            for row in self._journal.list_incomplete()
            if row.transaction_id != transaction_id
        )
        if other:
            return LifecycleExecutionReport(
                LifecycleExecutionState.BLOCKED,
                transaction,
                error_code="native_lifecycle_execution_conflict",
            )
        if rollback_requested:
            try:
                transaction = self._journal.request_rollback(
                    transaction_id=transaction_id,
                    authority_digest=transaction.authority_digest,
                    helper_session_id=self._authority.session_id,
                    claim_id=self._claim_id,
                ).transaction
            except NativeLifecycleConflictError as error:
                if error.code != "rollback_not_available":
                    raise
                return self._manual(
                    transaction, "rollback_not_available", []
                )
        else:
            current = self._journal.get(transaction_id)
            if current is None:  # pragma: no cover - journal invariant
                raise NativeLifecycleNotFoundError(
                    "native_lifecycle_transaction_not_found"
                )
            transaction = current
        return self._run_claimed(
            transaction,
            maximum_steps=maximum_steps,
        )

    def _run_claimed(
        self,
        transaction: LifecycleTransaction,
        *,
        maximum_steps: int,
    ) -> LifecycleExecutionReport:
        transaction_id = transaction.transaction_id
        recorded: list[LifecyclePhase] = []
        while len(recorded) < maximum_steps:
            if transaction.terminal:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.TERMINAL,
                    transaction,
                    tuple(recorded),
                )
            if transaction.phase is LifecyclePhase.MANUAL_RECOVERY:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.MANUAL_RECOVERY,
                    transaction,
                    tuple(recorded),
                    transaction.error_code,
                )
            authority_error = self._authorize(transaction)
            if authority_error is not None:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.DISABLED,
                    transaction,
                    tuple(recorded),
                    authority_error,
                )
            prior = self._observe_prior(transaction)
            preliminary = decide_lifecycle_recovery(
                transaction,
                next_effect=RecoveryObservation.NEEDS_ACTION,
                prior_effects=prior,
                rollback_requested=transaction.rollback_requested,
            )
            if preliminary.action is LifecycleRecoveryAction.MANUAL_RECOVERY:
                return self._manual(
                    transaction,
                    preliminary.reason_code or "journal_conflict",
                    recorded,
                )
            if preliminary.phase is None:
                return self._manual(
                    transaction, "journal_conflict", recorded
                )
            target = preliminary.phase
            observation = self._observe_phase(transaction, target)
            decision = decide_lifecycle_recovery(
                transaction,
                next_effect=observation,
                prior_effects=prior,
                rollback_requested=transaction.rollback_requested,
            )
            if decision.action is LifecycleRecoveryAction.MANUAL_RECOVERY:
                return self._manual(
                    transaction,
                    decision.reason_code or "journal_conflict",
                    recorded,
                )
            if decision.phase is None:
                return self._manual(
                    transaction, "journal_conflict", recorded
                )
            if decision.action is LifecycleRecoveryAction.RETRY_WHEN_READY:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.RETRYABLE,
                    transaction,
                    tuple(recorded),
                    decision.reason_code or "recovery_observation_not_ready",
                )
            if (
                decision.action
                is LifecycleRecoveryAction.RECORD_CONCLUSIVELY_SATISFIED
            ):
                transaction = self._journal.advance(
                    transaction_id, decision.phase
                ).transaction
                recorded.append(decision.phase)
                continue
            if decision.action not in {
                LifecycleRecoveryAction.RESUME_IDENTICAL,
                LifecycleRecoveryAction.RESTORE_EXACT_PREDECESSOR,
            }:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.TERMINAL,
                    transaction,
                    tuple(recorded),
                )

            proof_error = self._prepare_phase(transaction, decision.phase)
            if proof_error is not None:
                return self._manual(transaction, proof_error, recorded)
            if not self._claim_is_current(transaction):
                current = self._journal.get(transaction_id)
                if (
                    current is not None
                    and current.phase is LifecyclePhase.MANUAL_RECOVERY
                ):
                    return LifecycleExecutionReport(
                        LifecycleExecutionState.MANUAL_RECOVERY,
                        current,
                        tuple(recorded),
                        current.error_code,
                    )
                return LifecycleExecutionReport(
                    LifecycleExecutionState.BLOCKED,
                    transaction,
                    tuple(recorded),
                    "native_lifecycle_execution_claim_conflict",
                )
            authority_error = self._authorize(transaction)
            if authority_error is not None:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.DISABLED,
                    transaction,
                    tuple(recorded),
                    authority_error,
                )
            try:
                self._apply_phase(transaction, decision.phase)
            except Exception:
                # Never decide replay safety from the exception.  Only a
                # fresh exact observation after it may prove success or prove
                # that no effect remains.
                pass
            after = self._observe_phase(transaction, decision.phase)
            if after is RecoveryObservation.EFFECT_SATISFIED:
                transaction = self._journal.advance(
                    transaction_id, decision.phase
                ).transaction
                recorded.append(decision.phase)
                continue
            if after is RecoveryObservation.NEEDS_ACTION:
                return LifecycleExecutionReport(
                    LifecycleExecutionState.RETRYABLE,
                    transaction,
                    tuple(recorded),
                    "native_lifecycle_effect_not_satisfied",
                )
            reason = (
                "recovery_observation_conflict"
                if after is RecoveryObservation.CONFLICT
                else "recovery_observation_unavailable"
            )
            return self._manual(transaction, reason, recorded)

        transaction = self._journal.get(transaction_id)
        if transaction is None:  # pragma: no cover - journal invariant
            raise NativeLifecycleNotFoundError(
                "native_lifecycle_transaction_not_found"
            )
        return LifecycleExecutionReport(
            LifecycleExecutionState.PROGRESSED,
            transaction,
            tuple(recorded),
        )

    def _authorize(self, transaction: LifecycleTransaction) -> str | None:
        try:
            self._authority.validate()
        except NativeLifecycleConflictError:
            return "native_lifecycle_helper_authority_invalid"
        if not self.execution_enabled:
            return "native_lifecycle_execution_disabled"
        if (
            self._authority.transaction_id != transaction.transaction_id
            or self._authority.transaction_authority_digest
            != transaction.authority_digest
        ):
            return "native_lifecycle_helper_authority_mismatch"
        try:
            now = _timestamp(self._clock())
        except Exception:
            return "native_lifecycle_helper_authority_unavailable"
        if self._authority.expires_at is None or now >= self._authority.expires_at:
            return "native_lifecycle_helper_authority_expired"
        if (
            self._authority.execution_manifest_digest is None
            or self._authority.recovery_clone_identity_digest is None
            or self._authority.helper_build_digest is None
            or self._authority.authorization_digest is None
            or self._authority.session_id is None
        ):
            return "native_lifecycle_helper_authority_invalid"
        try:
            stage, _authorization = self._journal.require_helper_authority(
                transaction_id=transaction.transaction_id,
                authority_digest=transaction.authority_digest,
                execution_manifest_digest=(
                    self._authority.execution_manifest_digest
                ),
                helper_build_digest=self._authority.helper_build_digest,
                authorization_digest=self._authority.authorization_digest,
                helper_session_id=self._authority.session_id,
                now=now,
            )
            if (
                stage.recovery_clone_identity_digest
                != self._authority.recovery_clone_identity_digest
            ):
                return "native_lifecycle_helper_authority_mismatch"
        except NativeLifecycleConflictError as error:
            return error.code
        except Exception:
            return "native_lifecycle_helper_authority_unavailable"
        try:
            observed = HelperAuthorizationObservation(
                self._effects.authorize(self._authority, transaction)
            )
        except Exception:
            observed = HelperAuthorizationObservation.UNAVAILABLE
        if observed is HelperAuthorizationObservation.AUTHORIZED:
            try:
                now = _timestamp(self._clock())
            except Exception:
                return "native_lifecycle_helper_authority_unavailable"
            return (
                None
                if self._authority.expires_at is not None
                and now < self._authority.expires_at
                else "native_lifecycle_helper_authority_expired"
            )
        return (
            "native_lifecycle_helper_authority_rejected"
            if observed is HelperAuthorizationObservation.REJECTED
            else "native_lifecycle_helper_authority_unavailable"
        )

    def _claim_is_current(self, transaction: LifecycleTransaction) -> bool:
        if self._authority.session_id is None:
            return False
        try:
            return self._journal.execution_claim_is_current(
                transaction_id=transaction.transaction_id,
                authority_digest=transaction.authority_digest,
                helper_session_id=self._authority.session_id,
                claim_id=self._claim_id,
            )
        except Exception:
            return False

    def _observe_prior(
        self, transaction: LifecycleTransaction
    ) -> PriorEffectsState:
        try:
            return PriorEffectsState(
                self._effects.observe_prior_effects(
                    self._authority, transaction
                )
            )
        except Exception:
            return PriorEffectsState.UNAVAILABLE

    def _observe_phase(
        self, transaction: LifecycleTransaction, phase: LifecyclePhase
    ) -> RecoveryObservation:
        try:
            return RecoveryObservation(
                self._effects.observe_phase(
                    self._authority, transaction, phase
                )
            )
        except Exception:
            return RecoveryObservation.UNAVAILABLE

    def _prepare_phase(
        self, transaction: LifecycleTransaction, phase: LifecyclePhase
    ) -> str | None:
        if (
            transaction.kind is not LifecycleKind.UNINSTALL
            or phase is not LifecyclePhase.WEIGHTS_RESOLVED
        ):
            return None
        requests = self._exact_trash_requests(transaction)
        if not requests:
            return None
        try:
            proof = self._effects.prove_exact_trash_authority(
                self._authority, transaction.transaction_id, requests
            )
            state = ExactTrashProofState(proof.state)
        except Exception:
            return "recovery_observation_unavailable"
        if state is ExactTrashProofState.CONFLICT:
            return "exact_identity_mismatch"
        if state is ExactTrashProofState.UNAVAILABLE:
            return "recovery_observation_unavailable"
        expected = tuple(item.request_digest for item in requests)
        if proof.request_digests != expected:
            return "exact_identity_mismatch"
        return None

    def _apply_phase(
        self, transaction: LifecycleTransaction, phase: LifecyclePhase
    ) -> None:
        if transaction.kind is LifecycleKind.UNINSTALL:
            self._apply_uninstall(transaction, phase)
        else:
            self._apply_migration(transaction, phase)

    def _apply_uninstall(
        self, transaction: LifecycleTransaction, phase: LifecyclePhase
    ) -> None:
        plan = transaction.plan
        if not isinstance(plan, UninstallPlan):
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_plan_invalid"
            )
        transaction_id = transaction.transaction_id
        if phase is LifecyclePhase.SERVICE_QUIESCED:
            self._effects.quiesce_exact_service(
                self._authority, transaction_id
            )
        elif phase is LifecyclePhase.OUTBOX_RESOLVED:
            self._effects.resolve_token_outbox(
                self._authority,
                OutboxResolutionRequest(
                    transaction_id=transaction_id,
                    token_outbox_count=plan.token_outbox_count,
                    decision=plan.outbox_decision,
                    evidence_digest=plan.outbox_evidence_digest,
                ),
            )
        elif phase is LifecyclePhase.HUB_RESOLVED:
            self._effects.resolve_hub_pairing(
                self._authority, transaction_id, plan
            )
        elif phase is LifecyclePhase.WEIGHTS_RESOLVED:
            self._effects.move_exact_exclusive_weights_to_trash(
                self._authority,
                transaction_id,
                plan.manifest_receipt.digest,
                self._exact_trash_requests(transaction),
                plan.manifest_receipt.retained_count,
            )
        elif phase is LifecyclePhase.RUNTIMES_RESOLVED:
            self._effects.resolve_managed_runtimes(
                self._authority,
                transaction_id,
                self._component_request(
                    plan, ComponentKind.MANAGED_RUNTIMES
                ),
            )
        elif phase is LifecyclePhase.AGENT_UNREGISTERED:
            self._effects.unregister_exact_launch_agent(
                self._authority,
                transaction_id,
                PRODUCT_IDENTITY.launch_agent_label,
            )
        elif phase is LifecyclePhase.MENU_LOGIN_UNREGISTERED:
            self._effects.unregister_menu_login_item(
                self._authority,
                transaction_id,
                PRODUCT_IDENTITY.application_bundle_id,
            )
        elif phase is LifecyclePhase.STATE_RESOLVED:
            self._effects.resolve_private_state(
                self._authority,
                transaction_id,
                tuple(
                    self._component_request(plan, kind)
                    for kind in (
                        ComponentKind.PRIVATE_STATE,
                        ComponentKind.SECURITY_SCOPES,
                    )
                ),
            )
        elif phase is LifecyclePhase.APPLICATION_QUARANTINED:
            if self._authority.execution_manifest_digest is None:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_invalid"
                )
            self._effects.quarantine_exact_application(
                self._authority,
                transaction_id,
                PRODUCT_IDENTITY.application_bundle_id,
                self._authority.execution_manifest_digest,
            )
        elif phase is LifecyclePhase.APPLICATION_REMOVED:
            self._effects.remove_exact_application(
                self._authority,
                transaction_id,
                PRODUCT_IDENTITY.application_bundle_id,
            )
        elif phase is LifecyclePhase.COMPLETED:
            self._effects.finalize_uninstall(
                self._authority, transaction_id
            )
        else:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_phase_invalid"
            )

    def _apply_migration(
        self, transaction: LifecycleTransaction, phase: LifecyclePhase
    ) -> None:
        plan = transaction.plan
        if not isinstance(plan, MigrationPlan):
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_plan_invalid"
            )
        transaction_id = transaction.transaction_id
        if phase is LifecyclePhase.PREFLIGHTED:
            self._effects.preflight_exact_candidate(
                self._authority, transaction_id, plan
            )
        elif phase is LifecyclePhase.DRAINED:
            self._effects.drain_inference(self._authority, transaction_id)
        elif phase is LifecyclePhase.SNAPSHOTTED:
            self._effects.capture_exact_rollback_snapshot(
                self._authority, transaction_id, plan
            )
        elif phase is LifecyclePhase.PREDECESSOR_STOPPED:
            self._effects.stop_exact_predecessor(
                self._authority,
                transaction_id,
                plan.predecessor_build_digest,
            )
        elif phase is LifecyclePhase.CANDIDATE_INSTALLED:
            self._effects.install_exact_candidate(
                self._authority,
                transaction_id,
                plan.candidate_build_digest,
            )
        elif phase is LifecyclePhase.CANDIDATE_STARTED:
            self._effects.start_exact_candidate(
                self._authority,
                transaction_id,
                plan.candidate_build_digest,
            )
        elif phase is LifecyclePhase.VALIDATED:
            self._effects.validate_candidate_preservation(
                self._authority, transaction_id, plan
            )
        elif phase is LifecyclePhase.COMMITTED:
            self._effects.commit_exact_candidate(
                self._authority,
                transaction_id,
                plan.candidate_build_digest,
            )
        elif phase is LifecyclePhase.RESTORED:
            self._effects.restore_exact_predecessor(
                self._authority, transaction_id, plan
            )
        else:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_phase_invalid"
            )

    def _exact_trash_requests(
        self, transaction: LifecycleTransaction
    ) -> tuple[ExactTrashRequest, ...]:
        plan = transaction.plan
        if not isinstance(plan, UninstallPlan):
            return ()
        manifest = self._journal.manifest_store.require_receipt(
            plan.manifest_receipt
        )
        return tuple(
            _trash_request(item)
            for item in manifest.items
            if item.disposition is WeightDisposition.MOVE_TO_TRASH
        )

    @staticmethod
    def _component_request(
        plan: UninstallPlan, kind: ComponentKind
    ) -> ComponentResolutionRequest:
        matches = tuple(item for item in plan.components if item.kind is kind)
        if len(matches) != 1:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_plan_invalid"
            )
        item = matches[0]
        return ComponentResolutionRequest(
            kind=item.kind,
            disposition=item.disposition,
            member_inventory_digest=_digest(item.authority),
        )

    def _manual(
        self,
        transaction: LifecycleTransaction,
        reason: str,
        recorded: list[LifecyclePhase],
    ) -> LifecycleExecutionReport:
        mutation = self._journal.mark_manual_recovery(
            transaction.transaction_id, reason
        )
        return LifecycleExecutionReport(
            LifecycleExecutionState.MANUAL_RECOVERY,
            mutation.transaction,
            tuple(recorded),
            reason,
        )


def _trash_request(item: RetentionManifestItem) -> ExactTrashRequest:
    if (
        item.disposition is not WeightDisposition.MOVE_TO_TRASH
        or item.ownership is not WeightOwnership.EXCLUSIVE_MANAGED
        or item.installation_id is None
        or item.exclusive_proof_digest is None
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_exact_trash_authority_invalid"
        )
    value = {
        "asset_fingerprint": item.asset_fingerprint,
        "payload_fingerprint": item.payload_fingerprint,
        "exact_lexical_path": item.exact_lexical_path,
        "storage_location_id": item.storage_location_id,
        "storage_binding_generation": item.storage_binding_generation,
        "volume_uuid": item.volume_uuid,
        "scope_id": item.scope_id,
        "installation_id": item.installation_id,
        "original_exclusive_proof_digest": item.exclusive_proof_digest,
        "byte_count": item.byte_count,
    }
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return ExactTrashRequest(
        **value,
        request_digest=sha256(canonical).hexdigest(),
    )


def _uuid(value: object) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        ) from None
    return value


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    return value


def _timestamp(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= _MAX_TIMESTAMP
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    return float(value)


__all__ = [
    "ComponentResolutionRequest",
    "ExactTrashProof",
    "ExactTrashProofState",
    "ExactTrashRequest",
    "HelperAuthorizationObservation",
    "HelperAuthorityState",
    "LIFECYCLE_HELPER_IDENTIFIER",
    "LIFECYCLE_HELPER_PROTOCOL_VERSION",
    "LifecycleExecutionReport",
    "LifecycleExecutionState",
    "LifecycleHelperAuthority",
    "NativeLifecycleEffects",
    "NativeLifecycleExecutor",
    "OutboxResolutionRequest",
]
