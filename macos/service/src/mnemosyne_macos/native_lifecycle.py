"""Pure planning and durable receipts for native migration and uninstall.

This module deliberately has no executor.  It never unregisters a job, stops a
process, removes an application, moves a model, or deletes state.  It defines
the closed authority that a future signed lifecycle helper may consume and a
restart-safe journal for recording which exact milestone was proved.

The SQLite journal is path-free.  Exact lexical model locations live only in a
private retention manifest, while the v2 helper's complete exact-entry and
signed-code authority lives only in a separate private execution manifest.
Public callers receive only path-free plans, fixed counts, and versioned phase
status.  Bookmark bytes, credentials, request content, and arbitrary
diagnostics are never accepted by this module.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import ctypes
import ctypes.util
from dataclasses import dataclass, replace
from enum import Enum
from hashlib import sha256
import fcntl
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import threading
import time
import sys
from typing import Any, Final, TypeVar
from uuid import UUID, uuid4, uuid5

from .lifecycle_execution_protocol import (
    LIFECYCLE_RUNNER_IDENTIFIER,
    LifecycleEffectKind,
    LifecycleEffectObservation,
    LifecycleEffectReceiptStatus,
    LifecycleExecutionDirection,
    LifecycleExecutionMessageType,
    LifecycleRunnerRegisteredV2,
    LifecycleRunnerRegistrationV2,
    validate_lifecycle_execution_message,
)


_STORE_ID: Final[str] = "mnemosyne-native-lifecycle-journal-v2"
_STORE_SCHEMA_VERSION: Final[int] = 2
_LIFECYCLE_CONTRACT_VERSION: Final[int] = 2
_LEGACY_STORE_ID: Final[str] = "mnemosyne-native-lifecycle-journal-v1"
_LEGACY_STORE_SCHEMA_VERSION: Final[int] = 1
_MANIFEST_SCHEMA_VERSION: Final[int] = 1
_EXECUTION_MANIFEST_SCHEMA_VERSION: Final[int] = 2
_DATABASE_NAME: Final[str] = "journal.sqlite3"
_MAX_TRANSACTIONS: Final[int] = 1_024
_HARD_MAX_TRANSACTIONS: Final[int] = 10_000
_MAX_TIMESTAMP: Final[float] = 4_102_444_800.0
_MAX_WEIGHT_ITEMS: Final[int] = 100_000
_MAX_EXECUTION_MEMBERS: Final[int] = 250_000
_MAX_PATH_BYTES: Final[int] = 16_384
_MAX_OPAQUE_TEXT: Final[int] = 255
_MAX_PRIVATE_TEXT_BYTES: Final[int] = 8_192
_MAX_HELPER_AUTHORIZATION_LIFETIME_SECONDS: Final[int] = 120
_DEFAULT_HELPER_AUTHORIZATION_LIFETIME_SECONDS: Final[int] = 90
_MAX_HELPER_AUTHORIZATION_CHALLENGES_PER_TRANSACTION: Final[int] = 32
_MAX_EXECUTION_START_GRANTS_PER_TRANSACTION: Final[int] = 32
_MAX_RUNNER_LEASE_EPOCHS_PER_TRANSACTION: Final[int] = 100_000
_MAX_EFFECT_RECEIPTS_PER_TRANSACTION: Final[int] = 250_000
_EFFECT_ID_NAMESPACE: Final[UUID] = UUID("1d306030-1041-4d69-88f4-6545c26ef2bd")
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_PREFIXED_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"sha256:[0-9a-f]{64}")
_TEAM_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z0-9]{10}")
_AUTHORIZATION_PROOF_ALGORITHM_RE: Final[re.Pattern[str]] = re.compile(
    r"[a-z][a-z0-9-]{0,63}"
)
_AUTHORIZATION_PROOF_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_-]{32,4096}"
)
_OPAQUE_TEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:+@-]{0,254}"
)
_EFFECT_RECEIPT_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "effect_not_satisfied",
        "effect_refused",
        "observation_conflict",
        "observation_not_ready",
        "observation_unavailable",
    }
)


@dataclass(frozen=True, slots=True)
class NativeProductIdentity:
    """The only current product identities a lifecycle plan may target."""

    application_name: str = "Unified Inference.app"
    application_bundle_id: str = "com.mnemosyne.inference.menu"
    launch_agent_label: str = "com.mnemosyne.inference.agent"
    service_code_requirement_id: str = "com.mnemosyne.inference.service"
    lifecycle_helper_identifier: str = (
        "com.mnemosyne.inference.lifecycle-helper"
    )
    lifecycle_helper_relative_path: str = (
        "Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/"
        "MacOS/mnemosyne-lifecycle-helper"
    )
    lifecycle_runner_identifier: str = LIFECYCLE_RUNNER_IDENTIFIER
    lifecycle_runner_relative_path: str = (
        "Contents/MacOS/mnemosyne-lifecycle-runner"
    )


PRODUCT_IDENTITY: Final[NativeProductIdentity] = NativeProductIdentity()
LEGACY_SIDECAR_LABEL: Final[str] = "com.athena.token-sidecar"


class LifecycleKind(str, Enum):
    MIGRATION = "migration"
    UNINSTALL = "uninstall"


class RetentionMode(str, Enum):
    """The three closed, reinstall-safe choices shown to a Mac owner.

    Private configuration, the private environment, the token ledger/outbox,
    storage grants, and Fleet identity are recovery data.  They are retained
    by every uninstall mode so removing the application cannot silently reset
    accounting identity or discard usage that has not reached the central
    ledger.  The modes differ only in whether managed engine runtimes and
    freshly proved exclusive-managed weights are removed.
    """

    APP_ONLY = "app_only"
    KEEP_WEIGHTS = "remove_state_runtimes_keep_weights"
    FULL_EXCLUSIVE_MANAGED = "remove_exclusive_managed"


class WeightOwnership(str, Enum):
    EXCLUSIVE_MANAGED = "exclusive_managed"
    IMPORTED = "imported"
    LM_STUDIO = "lm_studio"
    EXTERNAL = "external"
    SHARED = "shared"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class ExclusiveProofState(str, Enum):
    FRESH_EXACT = "fresh_exact"
    NOT_PROVEN = "not_proven"


class WeightDisposition(str, Enum):
    RETAIN = "retain"
    MOVE_TO_TRASH = "move_to_trash"


class ComponentKind(str, Enum):
    APPLICATION = "application"
    LAUNCH_AGENT = "launch_agent"
    PRIVATE_STATE = "private_state"
    MANAGED_RUNTIMES = "managed_runtimes"
    SECURITY_SCOPES = "security_scopes"
    PAIRING_STATE = "pairing_state"


class ComponentDisposition(str, Enum):
    RETAIN = "retain"
    REMOVE_EXACT = "remove_exact"
    # State-like roots may contain the lifecycle receipts required to resume.
    # Their authority digest must bind an enumerated member set; a future
    # executor must never translate this into recursive root removal.
    REMOVE_PROVEN_MEMBERS = "remove_proven_members"
    REPLACE_EXACT = "replace_exact"


class ExecutionMemberDomain(str, Enum):
    """Closed private member inventories understood by the future helper."""

    APPLICATION = "application"
    # Exact, read-only inventory of the signed update candidate before any
    # lifecycle effect. Migration must never authorize a candidate from a
    # SignedCodeIdentity alone: its sealed bundle inventory is immutable
    # preparation evidence just like the installed app and recovery clone.
    CANDIDATE_APPLICATION = "candidate_application"
    PRIVATE_STATE = "private_state"
    MANAGED_RUNTIME = "managed_runtime"
    SECURITY_SCOPE = "security_scope"
    # Kept only so a pre-release manifest carrying the old value can be
    # parsed and rejected deterministically. Pairing is logical Hub state,
    # never filesystem-member authority.
    PAIRING_STATE = "pairing_state"
    RECOVERY_CLONE = "recovery_clone"
    EXCLUSIVE_WEIGHT = "exclusive_weight"


class ExecutionMemberType(str, Enum):
    REGULAR_FILE = "regular_file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


class ExecutionMemberDisposition(str, Enum):
    RETAIN = "retain"
    REMOVE_EXACT_ENTRY = "remove_exact_entry"
    REPLACE_EXACT_ENTRY = "replace_exact_entry"


class PairingDecision(str, Enum):
    RETAIN = "retain"
    REVOKE_CONFIRMED = "revoke_confirmed"
    REVOKE_PENDING_OFFLINE = "revoke_pending_offline"


class OutboxDecision(str, Enum):
    """Durable treatment of local token events before private-state removal."""

    PRESERVE_WITH_STATE = "preserve_with_state"
    EMPTY_CONFIRMED = "empty_confirmed"
    RECOVERY_CAPSULE = "recovery_capsule"
    EXPLICIT_ABANDONMENT = "explicit_abandonment"


class HubRevocationState(str, Enum):
    """Pairing revocation is explicit and never implied by local removal."""

    NOT_REQUESTED = "not_requested"
    CONFIRMED = "confirmed"
    PENDING_OFFLINE = "pending_offline"


class LegacySidecarMigrationState(str, Enum):
    """Closed predecessor handling for the former token-sidecar job."""

    ABSENT = "absent"
    INHERITANCE_DURABLY_VALIDATED = "inheritance_durably_validated"


class LifecyclePhase(str, Enum):
    # Version-2 migration phases mirror the helper-surviving transactional
    # cutover.  ``candidate_installed`` is deliberately distinct from start:
    # a candidate may be present on disk without ever becoming authoritative.
    DISCOVERED = "discovered"
    HELPER_STAGED = "helper_staged"
    AUTHORIZED = "authorized"
    PREFLIGHTED = "preflighted"
    DRAINED = "drained"
    SNAPSHOTTED = "snapshotted"
    PREDECESSOR_STOPPED = "predecessor_stopped"
    CANDIDATE_INSTALLED = "candidate_installed"
    CANDIDATE_STARTED = "candidate_started"
    VALIDATED = "validated"
    COMMITTED = "committed"
    RESTORED = "restored"

    # Uninstall phases are deliberately granular around durable resources.
    PREPARED = "prepared"
    SERVICE_QUIESCED = "service_quiesced"
    OUTBOX_RESOLVED = "outbox_resolved"
    HUB_RESOLVED = "hub_resolved"
    WEIGHTS_RESOLVED = "weights_resolved"
    RUNTIMES_RESOLVED = "runtimes_resolved"
    AGENT_UNREGISTERED = "agent_unregistered"
    MENU_LOGIN_UNREGISTERED = "menu_login_unregistered"
    STATE_RESOLVED = "state_resolved"
    APPLICATION_QUARANTINED = "application_quarantined"
    APPLICATION_REMOVED = "application_removed"
    COMPLETED = "completed"

    MANUAL_RECOVERY = "manual_recovery"


class RecoveryObservation(str, Enum):
    NEEDS_ACTION = "needs_action"
    EFFECT_SATISFIED = "effect_satisfied"
    RETRYABLE_NOT_READY = "retryable_not_ready"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class PriorEffectsState(str, Enum):
    INTACT = "intact"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class LifecycleRecoveryAction(str, Enum):
    # These are closed recovery ceremonies, not generic phase-forcing verbs.
    RESUME_IDENTICAL = "resume_identical"
    RECORD_CONCLUSIVELY_SATISFIED = "record_conclusively_satisfied"
    RESTORE_EXACT_PREDECESSOR = "restore_exact_predecessor"
    ABORT_BEFORE_ANY_EFFECT = "abort_before_any_effect"
    NO_ACTION = "no_action"
    RETRY_WHEN_READY = "retry_when_ready"
    MANUAL_RECOVERY = "manual_recovery"

    # Source-compatibility aliases.  Their wire values are the closed v2
    # actions above, so no caller can persist the old generic instructions.
    EXECUTE_PHASE = "resume_identical"
    RECORD_PHASE = "record_conclusively_satisfied"


class LifecycleExecutionClaimState(str, Enum):
    """Result of atomically fencing one product-wide lifecycle executor."""

    ACQUIRED = "acquired"
    BLOCKED = "blocked"
    EXPIRED = "expired"


MANUAL_RECOVERY_CODES: Final[frozenset[str]] = frozenset(
    {
        "candidate_identity_conflict",
        "exact_identity_mismatch",
        "journal_conflict",
        "legacy_v1_manual_recovery_required",
        "outbox_unresolved",
        "prior_effect_conflict",
        "prior_effect_unavailable",
        "recovery_observation_conflict",
        "recovery_observation_unavailable",
        "retention_manifest_conflict",
        "retention_manifest_unavailable",
        "rollback_not_available",
        "snapshot_conflict",
    }
)


_MIGRATION_SEQUENCE: Final[tuple[LifecyclePhase, ...]] = (
    LifecyclePhase.DISCOVERED,
    LifecyclePhase.HELPER_STAGED,
    LifecyclePhase.AUTHORIZED,
    LifecyclePhase.PREFLIGHTED,
    LifecyclePhase.DRAINED,
    LifecyclePhase.SNAPSHOTTED,
    LifecyclePhase.PREDECESSOR_STOPPED,
    LifecyclePhase.CANDIDATE_INSTALLED,
    LifecyclePhase.CANDIDATE_STARTED,
    LifecyclePhase.VALIDATED,
    LifecyclePhase.COMMITTED,
)
_UNINSTALL_SEQUENCE: Final[tuple[LifecyclePhase, ...]] = (
    LifecyclePhase.PREPARED,
    LifecyclePhase.HELPER_STAGED,
    LifecyclePhase.AUTHORIZED,
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
_LEGACY_MIGRATION_SEQUENCE_V1: Final[tuple[LifecyclePhase, ...]] = (
    LifecyclePhase.DISCOVERED,
    LifecyclePhase.PREFLIGHTED,
    LifecyclePhase.DRAINED,
    LifecyclePhase.SNAPSHOTTED,
    LifecyclePhase.PREDECESSOR_STOPPED,
    LifecyclePhase.CANDIDATE_STARTED,
    LifecyclePhase.VALIDATED,
    LifecyclePhase.COMMITTED,
)
_LEGACY_UNINSTALL_SEQUENCE_V1: Final[tuple[LifecyclePhase, ...]] = (
    LifecyclePhase.PREPARED,
    LifecyclePhase.SERVICE_QUIESCED,
    LifecyclePhase.OUTBOX_RESOLVED,
    LifecyclePhase.AGENT_UNREGISTERED,
    LifecyclePhase.APPLICATION_REMOVED,
    LifecyclePhase.WEIGHTS_RESOLVED,
    LifecyclePhase.RUNTIMES_RESOLVED,
    LifecyclePhase.STATE_RESOLVED,
    LifecyclePhase.COMPLETED,
)
_TERMINAL_PHASES: Final[frozenset[LifecyclePhase]] = frozenset(
    {
        LifecyclePhase.COMMITTED,
        LifecyclePhase.RESTORED,
        LifecyclePhase.COMPLETED,
    }
)
_MIGRATION_ROLLBACK_FROM: Final[frozenset[LifecyclePhase]] = frozenset(
    {
        LifecyclePhase.PREDECESSOR_STOPPED,
        LifecyclePhase.CANDIDATE_INSTALLED,
        LifecyclePhase.CANDIDATE_STARTED,
        LifecyclePhase.VALIDATED,
    }
)


class NativeLifecycleError(RuntimeError):
    """A fixed-code failure safe for a local status surface."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class NativeLifecycleConflictError(NativeLifecycleError):
    pass


class NativeLifecycleIntegrityError(NativeLifecycleError):
    pass


class NativeLifecycleCapacityError(NativeLifecycleError):
    pass


class NativeLifecycleNotFoundError(NativeLifecycleError):
    pass


@dataclass(frozen=True, slots=True)
class RetentionWeight:
    """Private exact weight authority used to build one retention manifest."""

    asset_fingerprint: str
    payload_fingerprint: str
    exact_lexical_path: str
    storage_location_id: str
    storage_binding_generation: int
    volume_uuid: str | None
    scope_id: str | None
    ownership: WeightOwnership
    installation_id: str | None = None
    exclusive_proof_state: ExclusiveProofState = ExclusiveProofState.NOT_PROVEN
    exclusive_proof_digest: str | None = None
    byte_count: int | None = None


@dataclass(frozen=True, slots=True)
class RetentionManifestItem:
    asset_fingerprint: str
    payload_fingerprint: str
    exact_lexical_path: str
    storage_location_id: str
    storage_binding_generation: int
    volume_uuid: str | None
    scope_id: str | None
    ownership: WeightOwnership
    installation_id: str | None
    exclusive_proof_state: ExclusiveProofState
    exclusive_proof_digest: str | None
    byte_count: int | None
    disposition: WeightDisposition
    reason_code: str

    def _private_dict(self) -> dict[str, object]:
        return {
            "asset_fingerprint": self.asset_fingerprint,
            "payload_fingerprint": self.payload_fingerprint,
            "exact_lexical_path": self.exact_lexical_path,
            "storage_location_id": self.storage_location_id,
            "storage_binding_generation": self.storage_binding_generation,
            "volume_uuid": self.volume_uuid,
            "scope_id": self.scope_id,
            "ownership": self.ownership.value,
            "installation_id": self.installation_id,
            "exclusive_proof_state": self.exclusive_proof_state.value,
            "exclusive_proof_digest": self.exclusive_proof_digest,
            "byte_count": self.byte_count,
            "disposition": self.disposition.value,
            "reason_code": self.reason_code,
        }

    def _journal_dict(self) -> dict[str, object]:
        value = self._private_dict()
        value.pop("exact_lexical_path")
        return value


@dataclass(frozen=True, slots=True)
class RetentionManifestReceipt:
    transaction_id: str
    digest: str
    item_count: int
    retained_count: int
    trash_count: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "digest": self.digest,
            "item_count": self.item_count,
            "retained_count": self.retained_count,
            "trash_count": self.trash_count,
        }


@dataclass(frozen=True, slots=True)
class PrivateRetentionManifest:
    """Path-bearing private artifact; never serialize this to an HTTP reply."""

    transaction_id: str
    retention_mode: RetentionMode
    items: tuple[RetentionManifestItem, ...]

    def private_dict(self) -> dict[str, object]:
        return {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "retention_mode": self.retention_mode.value,
            "product": {
                "application_name": PRODUCT_IDENTITY.application_name,
                "application_bundle_id": PRODUCT_IDENTITY.application_bundle_id,
                "launch_agent_label": PRODUCT_IDENTITY.launch_agent_label,
            },
            "weights": [item._private_dict() for item in self.items],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.private_dict()).encode("utf-8") + b"\n"

    @property
    def receipt(self) -> RetentionManifestReceipt:
        payload = self.canonical_bytes()
        retained = sum(
            item.disposition is WeightDisposition.RETAIN for item in self.items
        )
        trash = sum(
            item.disposition is WeightDisposition.MOVE_TO_TRASH
            for item in self.items
        )
        return RetentionManifestReceipt(
            transaction_id=self.transaction_id,
            digest=sha256(payload).hexdigest(),
            item_count=len(self.items),
            retained_count=retained,
            trash_count=trash,
        )


@dataclass(frozen=True, slots=True)
class ComponentPlan:
    kind: ComponentKind
    disposition: ComponentDisposition
    authority: str

    def _journal_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "disposition": self.disposition.value,
            "authority": self.authority,
        }


@dataclass(frozen=True, slots=True)
class UninstallPlan:
    transaction_id: str
    retention_mode: RetentionMode
    current_build_digest: str
    token_outbox_count: int
    outbox_decision: OutboxDecision
    outbox_evidence_digest: str
    hub_revocation_state: HubRevocationState
    components: tuple[ComponentPlan, ...]
    manifest_receipt: RetentionManifestReceipt
    manifest_items: tuple[RetentionManifestItem, ...]
    private_manifest: PrivateRetentionManifest | None

    @property
    def kind(self) -> LifecycleKind:
        return LifecycleKind.UNINSTALL

    def _journal_dict(self) -> dict[str, object]:
        return {
            "schema_version": _LIFECYCLE_CONTRACT_VERSION,
            "kind": self.kind.value,
            "transaction_id": self.transaction_id,
            "retention_mode": self.retention_mode.value,
            "product": _product_dict(),
            "current_build_digest": self.current_build_digest,
            "token_outbox_count": self.token_outbox_count,
            "outbox_decision": self.outbox_decision.value,
            "outbox_evidence_digest": self.outbox_evidence_digest,
            "hub_revocation_state": self.hub_revocation_state.value,
            "components": [item._journal_dict() for item in self.components],
            "retention_manifest": self.manifest_receipt.to_public_dict(),
            "weights": [item._journal_dict() for item in self.manifest_items],
        }

    def to_public_dict(self) -> dict[str, object]:
        """Return a deliberately path/authority-redacted preview."""

        return {
            "schema_version": _LIFECYCLE_CONTRACT_VERSION,
            "kind": self.kind.value,
            "transaction_id": self.transaction_id,
            "retention_mode": self.retention_mode.value,
            "product": _public_product_dict(),
            "token_outbox_count": self.token_outbox_count,
            "outbox_decision": self.outbox_decision.value,
            "hub_revocation_state": self.hub_revocation_state.value,
            "components": [
                {
                    "kind": item.kind.value,
                    "disposition": item.disposition.value,
                }
                for item in self.components
            ],
            "retention_manifest": self.manifest_receipt.to_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class MigrationEvidence:
    """Content-free fingerprints for one private rollback snapshot."""

    raw_config_digest: str
    semantic_config_digest: str
    sqlite_backup_digest: str
    usage_outbox_identity_digest: str
    private_environment_references_digest: str
    runtime_ownership_digest: str
    model_provenance_digest: str
    storage_authority_digest: str
    registration_state_digest: str
    legacy_sidecar_evidence_digest: str
    pairing_state_digest: str
    participation_state_digest: str
    rollback_snapshot_digest: str

    def _journal_dict(self) -> dict[str, str]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    transaction_id: str
    predecessor_build_digest: str
    candidate_build_digest: str
    legacy_sidecar_state: LegacySidecarMigrationState
    evidence: MigrationEvidence

    @property
    def kind(self) -> LifecycleKind:
        return LifecycleKind.MIGRATION

    def _journal_dict(self) -> dict[str, object]:
        return {
            "schema_version": _LIFECYCLE_CONTRACT_VERSION,
            "kind": self.kind.value,
            "transaction_id": self.transaction_id,
            "product": _product_dict(),
            "predecessor_build_digest": self.predecessor_build_digest,
            "candidate_build_digest": self.candidate_build_digest,
            "legacy_sidecar": {
                "label": LEGACY_SIDECAR_LABEL,
                "state": self.legacy_sidecar_state.value,
            },
            "retention_contract": {
                "private_state": "retain",
                "managed_runtimes": "retain",
                "security_scopes": "retain",
                "pairing_state": "retain",
                "weights": "retain",
            },
            "evidence": self.evidence._journal_dict(),
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": _LIFECYCLE_CONTRACT_VERSION,
            "kind": self.kind.value,
            "transaction_id": self.transaction_id,
            "product": _public_product_dict(),
            "predecessor_build_digest": self.predecessor_build_digest,
            "candidate_build_digest": self.candidate_build_digest,
            "legacy_sidecar": {
                "label": LEGACY_SIDECAR_LABEL,
                "state": self.legacy_sidecar_state.value,
            },
            "retention_contract": {
                "private_state": "retain",
                "managed_runtimes": "retain",
                "security_scopes": "retain",
                "pairing_state": "retain",
                "weights": "retain",
            },
        }


LifecyclePlan = UninstallPlan | MigrationPlan


@dataclass(frozen=True, slots=True)
class ExactExecutionMember:
    """One exact private filesystem entry, never a recursive root grant."""

    domain: ExecutionMemberDomain
    exact_lexical_path: str
    member_type: ExecutionMemberType
    disposition: ExecutionMemberDisposition
    device: int
    inode: int
    mode: int
    byte_count: int
    mtime_ns: int
    content_digest: str | None = None
    symlink_target_digest: str | None = None

    def _private_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain.value,
            "exact_lexical_path": self.exact_lexical_path,
            "member_type": self.member_type.value,
            "disposition": self.disposition.value,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "byte_count": self.byte_count,
            "mtime_ns": self.mtime_ns,
            "content_digest": self.content_digest,
            "symlink_target_digest": self.symlink_target_digest,
        }


@dataclass(frozen=True, slots=True)
class SignedCodeIdentity:
    """Exact signed-code evidence captured by the future bundled helper."""

    identifier: str
    version: str
    exact_path: str
    build_digest: str
    team_identifier: str
    code_requirement: str
    code_directory_digest: str
    sealed_resources_digest: str
    executable_relative_path: str
    member_inventory_digest: str

    def _private_dict(self) -> dict[str, str]:
        return {
            field: getattr(self, field) for field in self.__dataclass_fields__
        }

    @property
    def identity_digest(self) -> str:
        return sha256(
            _canonical_json(self._private_dict()).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class RecoveryCloneIdentity:
    transaction_id: str
    exact_bundle_path: str
    source_application_identity_digest: str
    cloned_application_identity_digest: str
    cloned_member_inventory_digest: str

    def _private_dict(self) -> dict[str, str]:
        return {
            field: getattr(self, field) for field in self.__dataclass_fields__
        }

    @property
    def identity_digest(self) -> str:
        return sha256(
            _canonical_json(self._private_dict()).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ExclusiveWeightExecutionIdentity:
    """Fresh exact files for one retention-manifest Trash candidate."""

    asset_fingerprint: str
    payload_fingerprint: str
    exact_lexical_path: str
    storage_location_id: str
    storage_binding_generation: int
    volume_uuid: str | None
    scope_id: str | None
    installation_id: str
    provenance_digest: str
    file_inventory_digest: str
    files: tuple[ExactExecutionMember, ...]

    def _private_dict(self) -> dict[str, object]:
        return {
            "asset_fingerprint": self.asset_fingerprint,
            "payload_fingerprint": self.payload_fingerprint,
            "exact_lexical_path": self.exact_lexical_path,
            "storage_location_id": self.storage_location_id,
            "storage_binding_generation": self.storage_binding_generation,
            "volume_uuid": self.volume_uuid,
            "scope_id": self.scope_id,
            "installation_id": self.installation_id,
            "provenance_digest": self.provenance_digest,
            "file_inventory_digest": self.file_inventory_digest,
            "files": [item._private_dict() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class ExecutionManifestReceipt:
    transaction_id: str
    transaction_authority_digest: str
    manifest_digest: str
    application_identity_digest: str
    helper_identity_digest: str
    runner_identity_digest: str
    recovery_clone_identity_digest: str
    exact_member_count: int
    exclusive_weight_count: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": _EXECUTION_MANIFEST_SCHEMA_VERSION,
            "manifest_digest": self.manifest_digest,
            "application_identity_digest": self.application_identity_digest,
            "helper_identity_digest": self.helper_identity_digest,
            "runner_identity_digest": self.runner_identity_digest,
            "recovery_clone_identity_digest": (
                self.recovery_clone_identity_digest
            ),
            "exact_member_count": self.exact_member_count,
            "exclusive_weight_count": self.exclusive_weight_count,
        }


@dataclass(frozen=True, slots=True)
class PrivateExecutionManifest:
    """Path-bearing v2 helper authority; never serialize to an API response."""

    transaction_id: str
    kind: LifecycleKind
    transaction_authority_digest: str
    application: SignedCodeIdentity
    recovery_application: SignedCodeIdentity
    helper: SignedCodeIdentity
    runner: SignedCodeIdentity
    recovery_clone: RecoveryCloneIdentity
    exact_members: tuple[ExactExecutionMember, ...]
    exclusive_weights: tuple[ExclusiveWeightExecutionIdentity, ...]
    token_outbox_count: int
    outbox_decision: OutboxDecision
    outbox_evidence_digest: str
    pairing_decision: PairingDecision
    pairing_state_digest: str
    predecessor: SignedCodeIdentity | None
    candidate: SignedCodeIdentity | None

    def private_dict(self) -> dict[str, object]:
        return {
            "schema_version": _EXECUTION_MANIFEST_SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "kind": self.kind.value,
            "transaction_authority_digest": self.transaction_authority_digest,
            "application": self.application._private_dict(),
            "recovery_application": self.recovery_application._private_dict(),
            "helper": self.helper._private_dict(),
            "runner": self.runner._private_dict(),
            "recovery_clone": self.recovery_clone._private_dict(),
            "exact_members": [item._private_dict() for item in self.exact_members],
            "exclusive_weights": [
                item._private_dict() for item in self.exclusive_weights
            ],
            "outbox": {
                "count": self.token_outbox_count,
                "decision": self.outbox_decision.value,
                "evidence_digest": self.outbox_evidence_digest,
            },
            "pairing": {
                "decision": self.pairing_decision.value,
                "state_digest": self.pairing_state_digest,
            },
            "predecessor": (
                self.predecessor._private_dict()
                if self.predecessor is not None
                else None
            ),
            "candidate": (
                self.candidate._private_dict()
                if self.candidate is not None
                else None
            ),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.private_dict()).encode("utf-8") + b"\n"

    @property
    def receipt(self) -> ExecutionManifestReceipt:
        nested_count = sum(len(weight.files) for weight in self.exclusive_weights)
        return ExecutionManifestReceipt(
            transaction_id=self.transaction_id,
            transaction_authority_digest=self.transaction_authority_digest,
            manifest_digest=sha256(self.canonical_bytes()).hexdigest(),
            application_identity_digest=self.application.identity_digest,
            helper_identity_digest=self.helper.identity_digest,
            runner_identity_digest=self.runner.identity_digest,
            recovery_clone_identity_digest=self.recovery_clone.identity_digest,
            exact_member_count=len(self.exact_members) + nested_count,
            exclusive_weight_count=len(self.exclusive_weights),
        )


@dataclass(frozen=True, slots=True)
class HelperStageReceipt:
    transaction_id: str
    transaction_authority_digest: str
    execution_manifest_digest: str
    recovery_clone_identity_digest: str
    helper_build_digest: str
    recorded_at: float


@dataclass(frozen=True, slots=True)
class HelperAuthorizationReceipt:
    transaction_id: str
    transaction_authority_digest: str
    execution_manifest_digest: str
    helper_build_digest: str
    authorization_digest: str
    helper_session_id: str
    expires_at: float
    recorded_at: float


@dataclass(frozen=True, slots=True)
class HelperAuthorizationProofAuthority:
    """Explicit verifier seam for one OS-bound helper proof authority.

    Production construction deliberately supplies no authority until the
    signed helper and service have a direct peer-attested provisioning path.
    Tests may inject a keyed verifier, but an unkeyed challenge digest can
    never satisfy this interface by itself.
    """

    algorithm: str
    key_id: str
    verifier: Callable[[bytes, str], bool]

    def validate(self) -> None:
        if (
            not isinstance(self.algorithm, str)
            or _AUTHORIZATION_PROOF_ALGORITHM_RE.fullmatch(self.algorithm)
            is None
            or not isinstance(self.key_id, str)
            or _PREFIXED_DIGEST_RE.fullmatch(self.key_id) is None
            or not callable(self.verifier)
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_helper_authority_unavailable"
            )


@dataclass(frozen=True, slots=True)
class HelperAuthorizationChallenge:
    """Closed helper-v2 challenge derived only from durable private authority."""

    schema_version: int
    helper_protocol_version: int
    nonce: str
    transaction_id: str
    transaction_authority_digest: str
    execution_manifest_digest: str
    recovery_clone_identity_digest: str
    expected_helper_identifier: str
    expected_helper_build_digest: str
    expected_team_identifier: str
    expected_code_requirement_digest: str
    expected_app_build_digest: str
    expected_authorization_proof_algorithm: str
    expected_authorization_key_id: str
    session_id: str
    issued_at: int
    expires_at: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "helper_protocol_version": self.helper_protocol_version,
            "nonce": self.nonce,
            "transaction_id": self.transaction_id,
            "transaction_authority_digest": self.transaction_authority_digest,
            "execution_manifest_digest": self.execution_manifest_digest,
            "recovery_clone_identity_digest": self.recovery_clone_identity_digest,
            "expected_helper_identifier": self.expected_helper_identifier,
            "expected_helper_build_digest": self.expected_helper_build_digest,
            "expected_team_identifier": self.expected_team_identifier,
            "expected_code_requirement_digest": (
                self.expected_code_requirement_digest
            ),
            "expected_app_build_digest": self.expected_app_build_digest,
            "expected_authorization_proof_algorithm": (
                self.expected_authorization_proof_algorithm
            ),
            "expected_authorization_key_id": (
                self.expected_authorization_key_id
            ),
            "session_id": self.session_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_public_dict()).encode("utf-8")

    @property
    def authorization_digest(self) -> str:
        return _prefixed_digest(sha256(self.canonical_bytes()).hexdigest())


@dataclass(frozen=True, slots=True)
class HelperAuthorizationSubmission:
    """Exact receipt returned by the signed bundled lifecycle helper."""

    schema_version: int
    helper_protocol_version: int
    nonce: str
    transaction_id: str
    transaction_authority_digest: str
    execution_manifest_digest: str
    recovery_clone_identity_digest: str
    expected_helper_identifier: str
    expected_helper_build_digest: str
    expected_team_identifier: str
    expected_code_requirement_digest: str
    expected_app_build_digest: str
    expected_authorization_proof_algorithm: str
    expected_authorization_key_id: str
    session_id: str
    issued_at: int
    expires_at: int
    authorization_digest: str
    authenticated_at: float
    authorization_proof: str

    def challenge_dict(self) -> dict[str, object]:
        value = {
            field: getattr(self, field)
            for field in HelperAuthorizationChallenge.__dataclass_fields__
        }
        return {
            key: value[key]
            for key in HelperAuthorizationChallenge(
                **value
            ).to_public_dict()
        }

    def proof_bytes(self) -> bytes:
        """Canonical statement the configured proof authority must verify."""

        timestamp_bits = struct.pack(">d", self.authenticated_at).hex()
        fields = (
            "mnemosyne-lifecycle-helper-proof-v1",
            self.authorization_digest,
            timestamp_bits,
            self.expected_authorization_proof_algorithm,
            self.expected_authorization_key_id,
        )
        return "\x00".join(fields).encode("ascii")


@dataclass(frozen=True, slots=True)
class HelperAuthorizationChallengeMutation:
    challenge: HelperAuthorizationChallenge
    replayed: bool


@dataclass(frozen=True, slots=True)
class HelperAuthorizationAcceptance:
    transaction: "LifecycleTransaction"
    authorization_digest: str
    helper_session_id: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class HelperAuthorizationCancellation:
    transaction_id: str
    nonce: str
    session_id: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class LifecycleExecutionStartGrant:
    """Immutable bridge from short owner authorization to durable execution."""

    grant_id: str
    transaction_id: str
    direction: LifecycleExecutionDirection
    transaction_authority_digest: str
    execution_manifest_digest: str
    recovery_clone_identity_digest: str
    runner_identifier: str
    runner_build_digest: str
    runner_identity_digest: str
    runner_team_identifier: str
    runner_code_requirement_digest: str
    authorization_digest: str
    authorization_session_id: str
    authorization_expires_at: float
    grant_sequence: int
    created_at: float
    grant_digest: str

    def binding_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "grant_id": self.grant_id,
            "transaction_id": self.transaction_id,
            "direction": self.direction.value,
            "transaction_authority_digest": self.transaction_authority_digest,
            "execution_manifest_digest": self.execution_manifest_digest,
            "recovery_clone_identity_digest": self.recovery_clone_identity_digest,
            "runner_identifier": self.runner_identifier,
            "runner_build_digest": self.runner_build_digest,
            "runner_identity_digest": self.runner_identity_digest,
            "runner_team_identifier": self.runner_team_identifier,
            "runner_code_requirement_digest": self.runner_code_requirement_digest,
            "authorization_digest": self.authorization_digest,
            "authorization_session_id": self.authorization_session_id,
            "authorization_expires_at": self.authorization_expires_at,
            "grant_sequence": self.grant_sequence,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class LifecycleExecutionGrantMutation:
    grant: LifecycleExecutionStartGrant
    replayed: bool


@dataclass(frozen=True, slots=True)
class LifecycleRunnerLeaseEpoch:
    lease_id: str
    transaction_id: str
    grant_id: str
    runner_session_id: str
    process_fence_id: str
    boot_id: str
    registration_nonce: str
    lease_epoch: int
    prior_lease_id: str | None
    issued_at: int
    expires_at: int
    issued_monotonic: float
    expires_monotonic: float


@dataclass(frozen=True, slots=True)
class LifecycleRunnerLeaseMutation:
    lease: LifecycleRunnerLeaseEpoch
    replayed: bool


class LifecycleRunnerProcessFence:
    """Process-local capability backed by one exclusive advisory file lock."""

    __slots__ = (
        "_boot_id",
        "_descriptor",
        "_fence_id",
        "_owner_pid",
        "_path",
        "_token",
        "_transaction_id",
    )

    def __init__(
        self,
        *,
        token: object,
        descriptor: int,
        path: Path,
        transaction_id: str,
        fence_id: str,
        boot_id: str,
    ) -> None:
        self._token = token
        self._descriptor = descriptor
        self._path = path
        self._transaction_id = transaction_id
        self._fence_id = fence_id
        self._boot_id = boot_id
        self._owner_pid = os.getpid()

    @property
    def fence_id(self) -> str:
        return self._fence_id

    @property
    def boot_id(self) -> str:
        return self._boot_id

    def close(self) -> None:
        descriptor = self._descriptor
        self._descriptor = -1
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self) -> "LifecycleRunnerProcessFence":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - defensive leak guard
        try:
            self.close()
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class LifecycleEffectReceipt:
    """One immutable target-level observation/application receipt."""

    receipt_id: str
    transaction_id: str
    grant_id: str
    lease_id: str
    lease_epoch: int
    sequence: int
    request_nonce: str
    effect_id: str
    effect_kind: LifecycleEffectKind
    target_digest: str
    attempt: int
    status: LifecycleEffectReceiptStatus
    observation: LifecycleEffectObservation
    before_observation_digest: str | None
    after_observation_digest: str | None
    prior_receipt_digest: str | None
    fixed_error_code: str | None
    recorded_at: float
    receipt_digest: str


@dataclass(frozen=True, slots=True)
class LifecycleEffectReceiptMutation:
    receipt: LifecycleEffectReceipt
    replayed: bool


@dataclass(frozen=True, slots=True)
class AuthorizedLifecycleEffect:
    """One path-free effect target derived from immutable plan/manifest data."""

    effect_id: str
    effect_kind: LifecycleEffectKind
    target_digest: str


@dataclass(frozen=True, slots=True)
class LifecycleTransaction:
    transaction_id: str
    kind: LifecycleKind
    plan: LifecyclePlan
    authority_digest: str
    phase: LifecyclePhase
    created_at: float
    updated_at: float
    error_code: str | None
    rollback_requested: bool = False
    contract_version: int = _LIFECYCLE_CONTRACT_VERSION
    recovery_from_phase: LifecyclePhase | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in _TERMINAL_PHASES

    @property
    def needs_recovery(self) -> bool:
        return not self.terminal


@dataclass(frozen=True, slots=True)
class LifecycleJournalMutation:
    transaction: LifecycleTransaction
    replayed: bool


@dataclass(frozen=True, slots=True)
class LifecycleExecutionClaimResult:
    state: LifecycleExecutionClaimState
    blocking_transaction_id: str | None = None


@dataclass(frozen=True, slots=True)
class LifecycleRecoveryDecision:
    action: LifecycleRecoveryAction
    phase: LifecyclePhase | None = None
    reason_code: str | None = None


def build_uninstall_plan(
    *,
    transaction_id: str,
    retention_mode: RetentionMode | str,
    current_build_digest: str,
    private_state_fingerprint: str,
    runtime_root_fingerprint: str,
    security_scope_store_fingerprint: str,
    pairing_state_fingerprint: str,
    token_outbox_count: int,
    outbox_decision: OutboxDecision | str,
    outbox_evidence_digest: str,
    weights: Iterable[RetentionWeight],
    hub_revocation_state: HubRevocationState | str = HubRevocationState.NOT_REQUESTED,
) -> UninstallPlan:
    """Build a closed uninstall plan without performing any planned action.

    State-like fingerprints bind exact member inventories, not directory
    roots.  The private-state inventory must exclude ``state/native-lifecycle``
    so the journal and retention manifests survive until a later, separately
    authorized receipt cleanup.  This core never turns an inventory digest
    into a recursive filesystem target.
    """

    transaction_id = _canonical_uuid(transaction_id)
    retention_mode = _enum(
        RetentionMode, retention_mode, "native_lifecycle_retention_mode_invalid"
    )
    outbox_decision = _enum(
        OutboxDecision, outbox_decision, "native_lifecycle_outbox_decision_invalid"
    )
    hub_revocation_state = _enum(
        HubRevocationState,
        hub_revocation_state,
        "native_lifecycle_revocation_state_invalid",
    )
    for digest in (
        current_build_digest,
        private_state_fingerprint,
        runtime_root_fingerprint,
        security_scope_store_fingerprint,
        pairing_state_fingerprint,
        outbox_evidence_digest,
    ):
        _validate_digest(digest)
    _validate_outbox(
        retention_mode=retention_mode,
        count=token_outbox_count,
        decision=outbox_decision,
    )

    copied = tuple(weights)
    if len(copied) > _MAX_WEIGHT_ITEMS:
        raise NativeLifecycleConflictError("native_lifecycle_weight_limit_exceeded")
    manifest_items = tuple(
        _manifest_item(weight, retention_mode=retention_mode) for weight in copied
    )
    asset_ids = [item.asset_fingerprint for item in manifest_items]
    if len(asset_ids) != len(set(asset_ids)):
        raise NativeLifecycleConflictError("native_lifecycle_weight_duplicate")
    installation_ids = [
        item.installation_id
        for item in manifest_items
        if item.installation_id is not None
    ]
    if len(installation_ids) != len(set(installation_ids)):
        raise NativeLifecycleConflictError("native_lifecycle_weight_duplicate")
    lexical_paths = [item.exact_lexical_path for item in manifest_items]
    if len(lexical_paths) != len(set(lexical_paths)):
        raise NativeLifecycleConflictError("native_lifecycle_weight_duplicate")
    # Even fresh per-row ownership cannot authorize a recursive Trash target
    # that lexically contains another known payload.  Retain both sides until
    # an integrating layer can produce one non-overlapping exact manifest.
    overlapping: set[int] = set()
    for left_index, left_path in enumerate(lexical_paths):
        for right_index in range(left_index + 1, len(lexical_paths)):
            right_path = lexical_paths[right_index]
            if _paths_overlap(left_path, right_path):
                overlapping.update((left_index, right_index))
    manifest_items = tuple(
        replace(
            item,
            disposition=WeightDisposition.RETAIN,
            reason_code="retain_overlapping_authority",
        )
        if index in overlapping
        and item.disposition is WeightDisposition.MOVE_TO_TRASH
        else item
        for index, item in enumerate(manifest_items)
    )
    manifest = PrivateRetentionManifest(
        transaction_id=transaction_id,
        retention_mode=retention_mode,
        items=manifest_items,
    )

    remove_runtimes = retention_mode is not RetentionMode.APP_ONLY
    components = (
        ComponentPlan(
            ComponentKind.APPLICATION,
            ComponentDisposition.REMOVE_EXACT,
            PRODUCT_IDENTITY.application_bundle_id,
        ),
        ComponentPlan(
            ComponentKind.LAUNCH_AGENT,
            ComponentDisposition.REMOVE_EXACT,
            PRODUCT_IDENTITY.launch_agent_label,
        ),
        ComponentPlan(
            ComponentKind.PRIVATE_STATE,
            ComponentDisposition.RETAIN,
            private_state_fingerprint,
        ),
        ComponentPlan(
            ComponentKind.MANAGED_RUNTIMES,
            ComponentDisposition.REMOVE_PROVEN_MEMBERS
            if remove_runtimes
            else ComponentDisposition.RETAIN,
            runtime_root_fingerprint,
        ),
        ComponentPlan(
            ComponentKind.SECURITY_SCOPES,
            ComponentDisposition.RETAIN,
            security_scope_store_fingerprint,
        ),
        ComponentPlan(
            ComponentKind.PAIRING_STATE,
            ComponentDisposition.RETAIN,
            pairing_state_fingerprint,
        ),
    )
    return UninstallPlan(
        transaction_id=transaction_id,
        retention_mode=retention_mode,
        current_build_digest=current_build_digest,
        token_outbox_count=token_outbox_count,
        outbox_decision=outbox_decision,
        outbox_evidence_digest=outbox_evidence_digest,
        hub_revocation_state=hub_revocation_state,
        components=components,
        manifest_receipt=manifest.receipt,
        manifest_items=manifest_items,
        private_manifest=manifest,
    )


def build_migration_plan(
    *,
    transaction_id: str,
    predecessor_build_digest: str,
    candidate_build_digest: str,
    evidence: MigrationEvidence,
    legacy_sidecar_state: LegacySidecarMigrationState | str = LegacySidecarMigrationState.ABSENT,
) -> MigrationPlan:
    """Bind one exact candidate to content-free private rollback evidence."""

    transaction_id = _canonical_uuid(transaction_id)
    _validate_digest(predecessor_build_digest)
    _validate_digest(candidate_build_digest)
    if predecessor_build_digest == candidate_build_digest:
        raise NativeLifecycleConflictError(
            "native_lifecycle_candidate_identity_conflict"
        )
    if not isinstance(evidence, MigrationEvidence):
        raise NativeLifecycleConflictError("native_lifecycle_evidence_invalid")
    for value in evidence._journal_dict().values():
        _validate_digest(value)
    legacy_sidecar_state = _enum(
        LegacySidecarMigrationState,
        legacy_sidecar_state,
        "native_lifecycle_legacy_sidecar_state_invalid",
    )
    return MigrationPlan(
        transaction_id=transaction_id,
        predecessor_build_digest=predecessor_build_digest,
        candidate_build_digest=candidate_build_digest,
        legacy_sidecar_state=legacy_sidecar_state,
        evidence=evidence,
    )


def execution_member_inventory_digest(
    members: Iterable[ExactExecutionMember],
) -> str:
    """Digest an ordered, exact-entry inventory for plan/manifest binding."""

    copied = tuple(_validate_execution_member(item) for item in members)
    if len(copied) > _MAX_EXECUTION_MEMBERS:
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_limit_exceeded"
        )
    ordered = tuple(
        sorted(copied, key=lambda item: (item.exact_lexical_path, item.domain.value))
    )
    if len({item.exact_lexical_path for item in ordered}) != len(ordered):
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_duplicate"
        )
    return sha256(
        _canonical_json([item._private_dict() for item in ordered]).encode("utf-8")
    ).hexdigest()


def build_private_execution_manifest(
    *,
    plan: LifecyclePlan,
    transaction_authority_digest: str,
    application: SignedCodeIdentity,
    recovery_application: SignedCodeIdentity,
    helper: SignedCodeIdentity,
    runner: SignedCodeIdentity,
    recovery_clone: RecoveryCloneIdentity,
    exact_members: Iterable[ExactExecutionMember],
    exclusive_weights: Iterable[ExclusiveWeightExecutionIdentity] = (),
    pairing_decision: PairingDecision | str = PairingDecision.RETAIN,
    pairing_state_digest: str,
    predecessor: SignedCodeIdentity | None = None,
    candidate: SignedCodeIdentity | None = None,
) -> PrivateExecutionManifest:
    """Build the closed path-bearing authority a signed helper may consume.

    The manifest grants exact-entry authority only.  Directory members do not
    imply recursive authority; a helper must process the complete enumerated
    child set and remove a directory only after proving it empty.
    """

    if not isinstance(plan, (UninstallPlan, MigrationPlan)):
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_manifest_invalid"
        )
    authority = _validate_digest(transaction_authority_digest)
    expected_authority = sha256(
        _canonical_json(plan._journal_dict()).encode("utf-8")
    ).hexdigest()
    if authority != expected_authority:
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_manifest_authority_mismatch"
        )
    application = _validate_signed_code_identity(
        application,
        expected_identifier=PRODUCT_IDENTITY.application_bundle_id,
    )
    recovery_application = _validate_signed_code_identity(
        recovery_application,
        expected_identifier=PRODUCT_IDENTITY.application_bundle_id,
    )
    helper = _validate_signed_code_identity(
        helper,
        expected_identifier=PRODUCT_IDENTITY.lifecycle_helper_identifier,
    )
    runner = _validate_signed_code_identity(
        runner,
        expected_identifier=PRODUCT_IDENTITY.lifecycle_runner_identifier,
    )
    recovery_clone = _validate_recovery_clone_identity(
        recovery_clone, transaction_id=plan.transaction_id
    )
    members = tuple(_validate_execution_member(item) for item in exact_members)
    weights = tuple(
        _validate_exclusive_weight_execution_identity(item)
        for item in exclusive_weights
    )
    if len(members) + sum(len(item.files) for item in weights) > _MAX_EXECUTION_MEMBERS:
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_limit_exceeded"
        )
    if len(weights) > _MAX_WEIGHT_ITEMS:
        raise NativeLifecycleConflictError("native_lifecycle_weight_limit_exceeded")
    all_paths = [item.exact_lexical_path for item in members]
    all_paths.extend(
        member.exact_lexical_path
        for weight in weights
        for member in weight.files
    )
    if len(all_paths) != len(set(all_paths)):
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_duplicate"
        )
    by_domain = {
        domain: tuple(item for item in members if item.domain is domain)
        for domain in ExecutionMemberDomain
    }
    if by_domain[ExecutionMemberDomain.PAIRING_STATE]:
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_authority_mismatch"
        )
    if not by_domain[ExecutionMemberDomain.APPLICATION] or not by_domain[
        ExecutionMemberDomain.RECOVERY_CLONE
    ]:
        raise NativeLifecycleConflictError(
            "native_lifecycle_signed_application_identity_invalid"
        )
    if not _signed_application_members_match(
        application,
        by_domain[ExecutionMemberDomain.APPLICATION],
        domain=ExecutionMemberDomain.APPLICATION,
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_signed_application_identity_invalid"
        )
    if not _signed_application_members_match(
        recovery_application,
        by_domain[ExecutionMemberDomain.RECOVERY_CLONE],
        domain=ExecutionMemberDomain.RECOVERY_CLONE,
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_recovery_clone_identity_invalid"
        )
    if (
        recovery_clone.source_application_identity_digest
        != application.identity_digest
        or recovery_clone.cloned_application_identity_digest
        != recovery_application.identity_digest
        or recovery_clone.cloned_member_inventory_digest
        != recovery_application.member_inventory_digest
        or recovery_application.exact_path != recovery_clone.exact_bundle_path
        or helper.exact_path
        != os.path.join(
            recovery_clone.exact_bundle_path,
            PRODUCT_IDENTITY.lifecycle_helper_relative_path,
        )
        or runner.exact_path
        != os.path.join(
            recovery_clone.exact_bundle_path,
            PRODUCT_IDENTITY.lifecycle_runner_relative_path,
        )
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_recovery_clone_identity_invalid"
        )
    helper_members = tuple(
        member
        for member in by_domain[ExecutionMemberDomain.RECOVERY_CLONE]
        if member.exact_lexical_path == helper.exact_path
    )
    runner_members = tuple(
        member
        for member in by_domain[ExecutionMemberDomain.RECOVERY_CLONE]
        if member.exact_lexical_path == runner.exact_path
    )
    if (
        len(helper_members) != 1
        or helper_members[0].member_type is not ExecutionMemberType.REGULAR_FILE
        or helper_members[0].disposition is not ExecutionMemberDisposition.RETAIN
        or helper.member_inventory_digest
        != execution_member_inventory_digest(helper_members)
        or len(runner_members) != 1
        or runner_members[0].member_type is not ExecutionMemberType.REGULAR_FILE
        or runner_members[0].disposition is not ExecutionMemberDisposition.RETAIN
        or runner.member_inventory_digest
        != execution_member_inventory_digest(runner_members)
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_recovery_clone_identity_invalid"
        )
    if not (
        application.team_identifier
        == recovery_application.team_identifier
        == helper.team_identifier
        == runner.team_identifier
        and application.build_digest == recovery_application.build_digest
        and application.code_requirement == recovery_application.code_requirement
        and application.code_directory_digest
        == recovery_application.code_directory_digest
        and application.sealed_resources_digest
        == recovery_application.sealed_resources_digest
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_signed_application_identity_invalid"
        )

    pairing_decision = _enum(
        PairingDecision,
        pairing_decision,
        "native_lifecycle_pairing_decision_invalid",
    )
    _validate_digest(pairing_state_digest)
    if isinstance(plan, UninstallPlan):
        if by_domain[ExecutionMemberDomain.CANDIDATE_APPLICATION]:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_manifest_invalid"
            )
        component_domain = {
            ComponentKind.PRIVATE_STATE: ExecutionMemberDomain.PRIVATE_STATE,
            ComponentKind.MANAGED_RUNTIMES: ExecutionMemberDomain.MANAGED_RUNTIME,
            ComponentKind.SECURITY_SCOPES: ExecutionMemberDomain.SECURITY_SCOPE,
        }
        for component in plan.components:
            domain = component_domain.get(component.kind)
            if domain is None:
                continue
            if component.authority != execution_member_inventory_digest(
                by_domain[domain]
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_member_authority_mismatch"
                )
        expected_pairing = {
            HubRevocationState.NOT_REQUESTED: PairingDecision.RETAIN,
            HubRevocationState.CONFIRMED: PairingDecision.REVOKE_CONFIRMED,
            HubRevocationState.PENDING_OFFLINE: (
                PairingDecision.REVOKE_PENDING_OFFLINE
            ),
        }[plan.hub_revocation_state]
        if pairing_decision is not expected_pairing:
            raise NativeLifecycleConflictError(
                "native_lifecycle_pairing_decision_invalid"
            )
        if pairing_state_digest != next(
            item.authority
            for item in plan.components
            if item.kind is ComponentKind.PAIRING_STATE
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_pairing_decision_invalid"
            )
        _validate_execution_weights(plan, weights)
        token_count = plan.token_outbox_count
        outbox_decision = plan.outbox_decision
        outbox_evidence = plan.outbox_evidence_digest
        if predecessor is not None or candidate is not None:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_manifest_invalid"
            )
    else:
        if weights:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_manifest_invalid"
            )
        if pairing_decision is not PairingDecision.RETAIN:
            raise NativeLifecycleConflictError(
                "native_lifecycle_pairing_decision_invalid"
            )
        if pairing_state_digest != plan.evidence.pairing_state_digest:
            raise NativeLifecycleConflictError(
                "native_lifecycle_pairing_decision_invalid"
            )
        predecessor = _validate_signed_code_identity(
            predecessor,
            expected_identifier=PRODUCT_IDENTITY.application_bundle_id,
        )
        candidate = _validate_signed_code_identity(
            candidate,
            expected_identifier=PRODUCT_IDENTITY.application_bundle_id,
        )
        if (
            predecessor.build_digest != plan.predecessor_build_digest
            or candidate.build_digest != plan.candidate_build_digest
            or predecessor != application
            or candidate.team_identifier != application.team_identifier
            or not _signed_application_members_match(
                candidate,
                by_domain[ExecutionMemberDomain.CANDIDATE_APPLICATION],
                domain=ExecutionMemberDomain.CANDIDATE_APPLICATION,
            )
            or _paths_overlap(candidate.exact_path, application.exact_path)
            or _paths_overlap(
                candidate.exact_path, recovery_clone.exact_bundle_path
            )
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_candidate_identity_conflict"
            )
        token_count = 0
        outbox_decision = OutboxDecision.PRESERVE_WITH_STATE
        outbox_evidence = plan.evidence.usage_outbox_identity_digest

    manifest = PrivateExecutionManifest(
        transaction_id=plan.transaction_id,
        kind=plan.kind,
        transaction_authority_digest=authority,
        application=application,
        recovery_application=recovery_application,
        helper=helper,
        runner=runner,
        recovery_clone=recovery_clone,
        exact_members=tuple(
            sorted(members, key=lambda item: (item.exact_lexical_path, item.domain.value))
        ),
        exclusive_weights=tuple(
            sorted(weights, key=lambda item: item.exact_lexical_path)
        ),
        token_outbox_count=token_count,
        outbox_decision=outbox_decision,
        outbox_evidence_digest=outbox_evidence,
        pairing_decision=pairing_decision,
        pairing_state_digest=pairing_state_digest,
        predecessor=predecessor,
        candidate=candidate,
    )
    # Exercise the strict parser now so write-time behavior cannot diverge.
    return _execution_manifest_from_dict(manifest.private_dict(), plan=plan)


class RetentionManifestStore:
    """Atomic private store for exact path-bearing retention manifests."""

    def __init__(self, config_path: str | Path) -> None:
        anchor = Path(config_path).expanduser()
        self._directory = (
            anchor.parent / "state" / "native-lifecycle" / "retention-manifests"
        )
        self._lock = threading.RLock()

    @property
    def directory(self) -> Path:
        return self._directory

    def path_for(self, transaction_id: str) -> Path:
        return self._directory / f"{_canonical_uuid(transaction_id)}.json"

    def write(self, manifest: PrivateRetentionManifest) -> RetentionManifestReceipt:
        if not isinstance(manifest, PrivateRetentionManifest):
            raise TypeError("manifest must be a PrivateRetentionManifest")
        validated = _private_manifest_from_dict(manifest.private_dict())
        payload = validated.canonical_bytes()
        receipt = validated.receipt
        destination = self.path_for(validated.transaction_id)
        with self._lock:
            self._prepare_directory()
            if destination.exists():
                existing = self._read_bytes(destination)
                if existing != payload:
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_retention_manifest_conflict"
                    )
                _secure_regular_file(destination, destination.lstat())
                return receipt

            temporary = self._directory / (
                f".{validated.transaction_id}.{uuid4()}.tmp"
            )
            descriptor: int | None = None
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(temporary, flags, 0o600)
                os.fchmod(descriptor, 0o600)
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short retention manifest write")
                    view = view[written:]
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                try:
                    # A same-directory hard-link publishes the fully fsynced
                    # inode atomically without ever replacing an existing
                    # receipt.  A competing writer must prove byte identity.
                    os.link(
                        temporary,
                        destination,
                        src_dir_fd=None,
                        dst_dir_fd=None,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    existing = self._read_bytes(destination)
                    if existing != payload:
                        raise NativeLifecycleConflictError(
                            "native_lifecycle_retention_manifest_conflict"
                        )
                temporary.unlink()
                _secure_regular_file(destination, destination.lstat())
                _fsync_directory(self._directory)
                return receipt
            except NativeLifecycleError:
                raise
            except OSError:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_retention_manifest_unavailable"
                ) from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def read(self, transaction_id: str) -> PrivateRetentionManifest:
        path = self.path_for(transaction_id)
        with self._lock:
            self._prepare_directory()
            try:
                payload = self._read_bytes(path)
                document = json.loads(payload)
                return _private_manifest_from_dict(document)
            except NativeLifecycleError:
                raise
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_retention_manifest_unavailable"
                ) from None

    def require_receipt(
        self, receipt: RetentionManifestReceipt
    ) -> PrivateRetentionManifest:
        manifest = self.read(receipt.transaction_id)
        if manifest.receipt != receipt:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_retention_manifest_conflict"
            )
        return manifest

    def _prepare_directory(self) -> None:
        try:
            state_directory = self._directory.parents[1]
            lifecycle_directory = self._directory.parent
            state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _secure_directory(state_directory)
            lifecycle_directory.mkdir(exist_ok=True, mode=0o700)
            _secure_directory(lifecycle_directory)
            self._directory.mkdir(exist_ok=True, mode=0o700)
            _secure_directory(self._directory)
        except NativeLifecycleError:
            raise
        except OSError:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_retention_manifest_unavailable"
            ) from None

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        status = path.lstat()
        _secure_regular_file(path, status)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                total += len(chunk)
                if total > 64 * 1024 * 1024:
                    raise NativeLifecycleIntegrityError(
                        "native_lifecycle_retention_manifest_unavailable"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)


class ExecutionManifestStore:
    """Atomic private store for v2 helper/recovery-clone authority."""

    def __init__(self, config_path: str | Path) -> None:
        anchor = Path(config_path).expanduser()
        lifecycle = anchor.parent / "state" / "native-lifecycle"
        self._directory = lifecycle / "execution-manifests"
        self._recovery_directory = lifecycle / "recovery"
        self._lock = threading.RLock()

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def recovery_directory(self) -> Path:
        return self._recovery_directory

    def path_for(self, transaction_id: str) -> Path:
        return self._directory / f"{_canonical_uuid(transaction_id)}.json"

    def expected_clone_path(self, transaction_id: str) -> Path:
        return (
            self._recovery_directory
            / _canonical_uuid(transaction_id)
            / PRODUCT_IDENTITY.application_name
        )

    def write(self, manifest: PrivateExecutionManifest) -> ExecutionManifestReceipt:
        if not isinstance(manifest, PrivateExecutionManifest):
            raise TypeError("manifest must be a PrivateExecutionManifest")
        validated = _execution_manifest_from_dict(manifest.private_dict())
        if validated.recovery_clone.exact_bundle_path != str(
            self.expected_clone_path(validated.transaction_id)
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_recovery_clone_identity_invalid"
            )
        payload = validated.canonical_bytes()
        receipt = validated.receipt
        destination = self.path_for(validated.transaction_id)
        with self._lock:
            self._prepare_directory()
            if destination.exists():
                existing = RetentionManifestStore._read_bytes(destination)
                if existing != payload:
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_execution_manifest_conflict"
                    )
                return receipt
            temporary = self._directory / (
                f".{validated.transaction_id}.{uuid4()}.tmp"
            )
            descriptor: int | None = None
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(temporary, flags, 0o600)
                os.fchmod(descriptor, 0o600)
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short execution manifest write")
                    view = view[written:]
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                try:
                    os.link(
                        temporary,
                        destination,
                        src_dir_fd=None,
                        dst_dir_fd=None,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    if RetentionManifestStore._read_bytes(destination) != payload:
                        raise NativeLifecycleConflictError(
                            "native_lifecycle_execution_manifest_conflict"
                        )
                temporary.unlink()
                _secure_regular_file(destination, destination.lstat())
                _fsync_directory(self._directory)
                return receipt
            except NativeLifecycleError:
                raise
            except OSError:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_execution_manifest_unavailable"
                ) from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temporary.unlink()
                except (FileNotFoundError, OSError):
                    pass

    def read(self, transaction_id: str) -> PrivateExecutionManifest:
        path = self.path_for(transaction_id)
        with self._lock:
            self._prepare_directory()
            try:
                payload = RetentionManifestStore._read_bytes(path)
                return _execution_manifest_from_dict(json.loads(payload))
            except NativeLifecycleConflictError:
                # A strict-contract failure while reading an already durable
                # manifest is corruption, not a caller-resolvable conflict.
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_execution_manifest_conflict"
                ) from None
            except NativeLifecycleError:
                raise
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_execution_manifest_unavailable"
                ) from None

    def require_receipt(
        self, receipt: ExecutionManifestReceipt
    ) -> PrivateExecutionManifest:
        manifest = self.read(receipt.transaction_id)
        if manifest.receipt != receipt:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_execution_manifest_conflict"
            )
        return manifest

    def _prepare_directory(self) -> None:
        try:
            state = self._directory.parents[1]
            lifecycle = self._directory.parent
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            _secure_directory(state)
            lifecycle.mkdir(exist_ok=True, mode=0o700)
            _secure_directory(lifecycle)
            self._directory.mkdir(exist_ok=True, mode=0o700)
            _secure_directory(self._directory)
            self._recovery_directory.mkdir(exist_ok=True, mode=0o700)
            _secure_directory(self._recovery_directory)
        except NativeLifecycleError:
            raise
        except OSError:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_execution_manifest_unavailable"
            ) from None


_T = TypeVar("_T")


class NativeLifecycleJournal:
    """Private bounded path-free lifecycle transaction journal."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        maximum_transactions: int = _MAX_TRANSACTIONS,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        boot_identity: Callable[[], str] | None = None,
        helper_authorization_proof_authority: (
            HelperAuthorizationProofAuthority | None
        ) = None,
    ) -> None:
        if (
            isinstance(maximum_transactions, bool)
            or not isinstance(maximum_transactions, int)
            or not 1 <= maximum_transactions <= _HARD_MAX_TRANSACTIONS
        ):
            raise ValueError("invalid native lifecycle journal limit")
        anchor = Path(config_path).expanduser()
        self._directory = anchor.parent / "state" / "native-lifecycle"
        self._path = self._directory / _DATABASE_NAME
        self._manifest_store = RetentionManifestStore(anchor)
        self._execution_manifest_store = ExecutionManifestStore(anchor)
        self._maximum_transactions = maximum_transactions
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._boot_identity_provider = boot_identity or _default_boot_identity
        self._helper_authorization_proof_authority = (
            helper_authorization_proof_authority
        )
        if helper_authorization_proof_authority is not None:
            helper_authorization_proof_authority.validate()
        self._runner_fence_token = object()
        self._lock = threading.RLock()
        self._initialized = False
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def manifest_store(self) -> RetentionManifestStore:
        return self._manifest_store

    @property
    def execution_manifest_store(self) -> ExecutionManifestStore:
        return self._execution_manifest_store

    @property
    def helper_authorization_available(self) -> bool:
        """Whether a real, explicitly configured proof verifier is present."""

        return self._helper_authorization_proof_authority is not None

    def acquire_runner_process_fence(
        self, transaction_id: str
    ) -> LifecycleRunnerProcessFence:
        """Acquire the sole same-host runner lock for one transaction.

        The returned capability must remain alive through every observation
        and effect. It cannot be reused after fork, reboot, close, or by a
        different journal instance.
        """

        transaction_id = _canonical_uuid(transaction_id)
        self._require_ready()
        self._prepare_path()
        lock_directory = self._directory / "runner-fences"
        try:
            lock_directory.mkdir(exist_ok=True, mode=0o700)
            _secure_directory(lock_directory)
            path = lock_directory / f"{transaction_id}.lock"
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                status = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(status.st_mode)
                    or status.st_uid != os.geteuid()
                    or stat.S_IMODE(status.st_mode) != 0o600
                    or status.st_nlink != 1
                ):
                    raise OSError
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                boot_id = self._boot_id()
                return LifecycleRunnerProcessFence(
                    token=self._runner_fence_token,
                    descriptor=descriptor,
                    path=path,
                    transaction_id=transaction_id,
                    fence_id=str(uuid4()),
                    boot_id=boot_id,
                )
            except Exception:
                os.close(descriptor)
                raise
        except (OSError, NativeLifecycleError):
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_process_fence_unavailable"
            ) from None

    def initialize(self) -> None:
        with self._lock:
            if self._closed:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_unavailable"
                )
            self._prepare_path()
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                self._create_schema(connection)
                self._validate_metadata(connection)
                self._validate_all(connection)
                self._prune(connection, reserve=0, fail_if_protected=False)
                connection.execute("COMMIT")
                self._initialized = True
            except NativeLifecycleError:
                _rollback(connection)
                raise
            except (OSError, sqlite3.Error, TypeError, ValueError):
                _rollback(connection)
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                ) from None
            finally:
                if connection is not None:
                    connection.close()
                self._secure_database_artifacts()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._initialized = False

    def prepare(self, plan: LifecyclePlan) -> LifecycleJournalMutation:
        if not isinstance(plan, (UninstallPlan, MigrationPlan)):
            raise TypeError("plan must be an UninstallPlan or MigrationPlan")
        # Round-trip through the strict parser before persisting authority.
        payload = plan._journal_dict()
        validated = _plan_from_journal_dict(payload)
        canonical = _canonical_json(validated._journal_dict())
        authority_digest = sha256(canonical.encode("utf-8")).hexdigest()
        initial_phase = (
            LifecyclePhase.PREPARED
            if validated.kind is LifecycleKind.UNINSTALL
            else LifecyclePhase.DISCOVERED
        )
        if isinstance(plan, UninstallPlan):
            if plan.private_manifest is not None:
                receipt = self._manifest_store.write(plan.private_manifest)
                if receipt != plan.manifest_receipt:
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_retention_manifest_conflict"
                    )
            else:
                self._manifest_store.require_receipt(plan.manifest_receipt)
            self._validate_manifest_binding(validated)
        now = self._timestamp()

        def operation(connection: sqlite3.Connection) -> LifecycleJournalMutation:
            existing = self._select(connection, validated.transaction_id)
            if existing is not None:
                if (
                    existing.kind is not validated.kind
                    or existing.authority_digest != authority_digest
                ):
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_transaction_conflict"
                    )
                return LifecycleJournalMutation(existing, replayed=True)
            if self._select_legacy(connection, validated.transaction_id) is not None:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_transaction_conflict"
                )
            self._prune(connection, reserve=1, fail_if_protected=True)
            connection.execute(
                """
                INSERT INTO native_lifecycle_transactions_v2 (
                    transaction_id, contract_version, lifecycle_kind, authority_json,
                    authority_digest, current_phase, created_at, updated_at,
                    error_code
                ) VALUES (?, 2, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    validated.transaction_id,
                    validated.kind.value,
                    canonical,
                    authority_digest,
                    initial_phase.value,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO native_lifecycle_events_v2 (
                    transaction_id, sequence, phase, recorded_at, error_code
                ) VALUES (?, 1, ?, ?, NULL)
                """,
                (validated.transaction_id, initial_phase.value, now),
            )
            connection.execute(
                """
                INSERT INTO native_lifecycle_control_v2 (
                    transaction_id, rollback_requested, updated_at
                ) VALUES (?, 0, ?)
                """,
                (validated.transaction_id, now),
            )
            return LifecycleJournalMutation(
                self._require(connection, validated.transaction_id),
                replayed=False,
            )

        return self._write(operation)

    def record_helper_staged(
        self, manifest: PrivateExecutionManifest
    ) -> LifecycleJournalMutation:
        """Bind a complete verified recovery clone before authorization.

        Writing the private manifest may precede the SQLite receipt after a
        crash.  An identical replay is safe; a different manifest for the
        same transaction is a permanent conflict.
        """

        if not isinstance(manifest, PrivateExecutionManifest):
            raise TypeError("manifest must be a PrivateExecutionManifest")
        receipt = self._execution_manifest_store.write(manifest)
        now = self._timestamp()

        def operation(connection: sqlite3.Connection) -> LifecycleJournalMutation:
            current = self._require(connection, manifest.transaction_id)
            _validate_execution_manifest_binding(
                manifest, self._execution_binding_plan(current.plan)
            )
            if current.authority_digest != manifest.transaction_authority_digest:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_manifest_authority_mismatch"
                )
            existing = self._helper_stage_receipt(
                connection, current.transaction_id
            )
            expected = HelperStageReceipt(
                transaction_id=current.transaction_id,
                transaction_authority_digest=current.authority_digest,
                execution_manifest_digest=receipt.manifest_digest,
                recovery_clone_identity_digest=(
                    receipt.recovery_clone_identity_digest
                ),
                helper_build_digest=manifest.helper.build_digest,
                recorded_at=now,
            )
            if existing is not None:
                if replace(existing, recorded_at=now) != expected:
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_helper_stage_conflict"
                    )
                return LifecycleJournalMutation(current, replayed=True)
            initial = (
                LifecyclePhase.PREPARED
                if current.kind is LifecycleKind.UNINSTALL
                else LifecyclePhase.DISCOVERED
            )
            if current.phase is not initial:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_phase_out_of_order"
                )
            connection.execute(
                """
                INSERT INTO native_lifecycle_helper_stage_receipts_v2 (
                    transaction_id, authority_digest,
                    execution_manifest_digest,
                    recovery_clone_identity_digest, helper_build_digest,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    expected.transaction_id,
                    expected.transaction_authority_digest,
                    expected.execution_manifest_digest,
                    expected.recovery_clone_identity_digest,
                    expected.helper_build_digest,
                    expected.recorded_at,
                ),
            )
            self._append_phase_in_connection(
                connection, current, LifecyclePhase.HELPER_STAGED, now
            )
            return LifecycleJournalMutation(
                self._require(connection, current.transaction_id), replayed=False
            )

        return self._write(operation)

    def issue_helper_authorization_challenge(
        self,
        transaction_id: str,
        *,
        lifetime_seconds: int = _DEFAULT_HELPER_AUTHORIZATION_LIFETIME_SECONDS,
    ) -> HelperAuthorizationChallengeMutation:
        """Issue or replay one live challenge for exact staged authority.

        The caller supplies only the transaction UUID. Every authority field
        is derived from the immutable transaction, helper-stage receipt, and
        private execution manifest. A live challenge is replayed byte-for-byte
        so a service restart or an ambiguous HTTP response cannot multiply
        valid owner-authentication ceremonies.
        """

        transaction_id = _canonical_uuid(transaction_id)
        proof_authority = self._helper_authorization_proof_authority
        if proof_authority is None:
            # The old receipt was only SHA-256 over public challenge fields and
            # was therefore forgeable by any authenticated control caller.
            # Keep authorization unavailable until a signed-helper/OS-peer
            # verifier is deliberately provisioned.
            raise NativeLifecycleConflictError(
                "native_lifecycle_helper_authority_unavailable"
            )
        proof_authority.validate()
        if (
            isinstance(lifetime_seconds, bool)
            or not isinstance(lifetime_seconds, int)
            or not 5
            <= lifetime_seconds
            <= _MAX_HELPER_AUTHORIZATION_LIFETIME_SECONDS
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_helper_authority_invalid"
            )
        observed_now = self._timestamp()
        issued_at = max(1, int(math.floor(observed_now)))
        expires_at = issued_at + lifetime_seconds

        def operation(
            connection: sqlite3.Connection,
        ) -> HelperAuthorizationChallengeMutation:
            current = self._require(connection, transaction_id)
            basis = self._helper_authorization_basis(connection, current)
            connection.execute(
                """
                UPDATE native_lifecycle_authorization_challenges_v2
                   SET state = 'cancelled', completed_at = ?
                 WHERE transaction_id = ?
                   AND state = 'pending'
                   AND expires_at <= ?
                """,
                (observed_now, transaction_id, observed_now),
            )
            rows = connection.execute(
                """
                SELECT *
                  FROM native_lifecycle_authorization_challenges_v2
                 WHERE transaction_id = ?
                   AND state = 'pending'
                   AND expires_at > ?
                 ORDER BY issued_at DESC, nonce DESC
                """,
                (transaction_id, observed_now),
            ).fetchall()
            if len(rows) > 1:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            if rows:
                existing = self._authorization_challenge_from_row(rows[0])
                self._validate_challenge_basis(existing, basis)
                return HelperAuthorizationChallengeMutation(
                    existing, replayed=True
                )
            count_row = connection.execute(
                """
                SELECT COUNT(*) AS total
                  FROM native_lifecycle_authorization_challenges_v2
                 WHERE transaction_id = ?
                """,
                (transaction_id,),
            ).fetchone()
            if count_row is None or int(count_row["total"]) >= (
                _MAX_HELPER_AUTHORIZATION_CHALLENGES_PER_TRANSACTION
            ):
                raise NativeLifecycleCapacityError(
                    "native_lifecycle_helper_authority_capacity_exhausted"
                )
            challenge = HelperAuthorizationChallenge(
                schema_version=2,
                helper_protocol_version=2,
                nonce=str(uuid4()),
                transaction_id=current.transaction_id,
                transaction_authority_digest=_prefixed_digest(
                    current.authority_digest
                ),
                execution_manifest_digest=_prefixed_digest(
                    basis[0].execution_manifest_digest
                ),
                recovery_clone_identity_digest=_prefixed_digest(
                    basis[0].recovery_clone_identity_digest
                ),
                expected_helper_identifier=(
                    PRODUCT_IDENTITY.lifecycle_helper_identifier
                ),
                expected_helper_build_digest=_prefixed_digest(
                    basis[1].helper.build_digest
                ),
                expected_team_identifier=basis[1].helper.team_identifier,
                expected_code_requirement_digest=_prefixed_digest(
                    sha256(
                        basis[1].helper.code_requirement.encode("utf-8")
                    ).hexdigest()
                ),
                expected_app_build_digest=_prefixed_digest(
                    basis[1].application.build_digest
                ),
                expected_authorization_proof_algorithm=(
                    proof_authority.algorithm
                ),
                expected_authorization_key_id=proof_authority.key_id,
                session_id=str(uuid4()),
                issued_at=issued_at,
                expires_at=expires_at,
            )
            challenge = _validate_helper_authorization_challenge(challenge)
            connection.execute(
                """
                INSERT INTO native_lifecycle_authorization_challenges_v2 (
                    nonce, transaction_id, session_id, challenge_json,
                    challenge_digest, issued_at, expires_at, state,
                    authorization_digest, authenticated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL)
                """,
                (
                    challenge.nonce,
                    challenge.transaction_id,
                    challenge.session_id,
                    _canonical_json(challenge.to_public_dict()),
                    _unprefixed_digest(challenge.authorization_digest),
                    challenge.issued_at,
                    challenge.expires_at,
                ),
            )
            return HelperAuthorizationChallengeMutation(
                challenge, replayed=False
            )

        return self._write(operation)

    def accept_helper_authorization_receipt(
        self,
        submission: HelperAuthorizationSubmission,
    ) -> HelperAuthorizationAcceptance:
        """Consume exactly one helper receipt and durably authorize v2.

        Invalid or tampered receipts do not burn the challenge. A successful
        receipt does, in the same SQLite transaction that appends the existing
        helper authorization receipt and ``authorized`` phase.
        """

        submission = _validate_helper_authorization_submission(submission)
        observed_now = self._timestamp()

        def operation(connection: sqlite3.Connection) -> HelperAuthorizationAcceptance:
            row = connection.execute(
                """
                SELECT *
                  FROM native_lifecycle_authorization_challenges_v2
                 WHERE nonce = ? AND transaction_id = ?
                """,
                (submission.nonce, submission.transaction_id),
            ).fetchone()
            if row is None:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_mismatch"
                )
            state = str(row["state"])
            if state == "consumed":
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_replayed"
                )
            if state == "cancelled":
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_cancelled"
                )
            if state != "pending":
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            challenge = self._authorization_challenge_from_row(row)
            if submission.challenge_dict() != challenge.to_public_dict():
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_mismatch"
                )
            if observed_now >= challenge.expires_at:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_expired"
                )
            if not (
                challenge.issued_at
                <= submission.authenticated_at
                < challenge.expires_at
                and submission.authenticated_at <= observed_now
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_invalid"
                )
            if not hmac.compare_digest(
                submission.authorization_digest,
                challenge.authorization_digest,
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_mismatch"
                )
            current = self._require(connection, submission.transaction_id)
            basis = self._helper_authorization_basis(
                connection, current, allow_authorized=True
            )
            self._validate_challenge_basis(challenge, basis)
            proof_authority = self._helper_authorization_proof_authority
            if proof_authority is None:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_unavailable"
                )
            proof_authority.validate()
            if (
                challenge.expected_authorization_proof_algorithm
                != proof_authority.algorithm
                or challenge.expected_authorization_key_id
                != proof_authority.key_id
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_mismatch"
                )
            try:
                proof_valid = proof_authority.verifier(
                    submission.proof_bytes(), submission.authorization_proof
                )
            except Exception:
                proof_valid = False
            if proof_valid is not True:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_invalid"
                )
            mutation = self._record_authorized_in_connection(
                connection,
                current=current,
                execution_manifest_digest=_unprefixed_digest(
                    challenge.execution_manifest_digest
                ),
                helper_build_digest=_unprefixed_digest(
                    challenge.expected_helper_build_digest
                ),
                authorization_digest=_unprefixed_digest(
                    challenge.authorization_digest
                ),
                helper_session_id=challenge.session_id,
                expires_at=float(challenge.expires_at),
                now=observed_now,
            )
            updated = connection.execute(
                """
                UPDATE native_lifecycle_authorization_challenges_v2
                   SET state = 'consumed',
                       authorization_digest = ?,
                       authenticated_at = ?,
                       completed_at = ?
                 WHERE nonce = ? AND state = 'pending'
                """,
                (
                    _unprefixed_digest(challenge.authorization_digest),
                    submission.authenticated_at,
                    observed_now,
                    challenge.nonce,
                ),
            )
            if updated.rowcount != 1:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_replayed"
                )
            return HelperAuthorizationAcceptance(
                transaction=mutation.transaction,
                authorization_digest=challenge.authorization_digest,
                helper_session_id=challenge.session_id,
                replayed=mutation.replayed,
            )

        return self._write(operation)

    def cancel_helper_authorization_challenge(
        self,
        *,
        transaction_id: str,
        nonce: str,
        session_id: str,
    ) -> HelperAuthorizationCancellation:
        """Durably invalidate a challenge after owner cancellation/failure."""

        transaction_id = _canonical_uuid(transaction_id)
        nonce = _canonical_uuid(nonce)
        session_id = _canonical_uuid(session_id)
        observed_now = self._timestamp()

        def operation(connection: sqlite3.Connection) -> HelperAuthorizationCancellation:
            self._require(connection, transaction_id)
            row = connection.execute(
                """
                SELECT *
                  FROM native_lifecycle_authorization_challenges_v2
                 WHERE nonce = ? AND transaction_id = ?
                """,
                (nonce, transaction_id),
            ).fetchone()
            if row is None:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_mismatch"
                )
            challenge = self._authorization_challenge_from_row(row)
            if challenge.session_id != session_id:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_mismatch"
                )
            state = str(row["state"])
            if state == "consumed":
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_replayed"
                )
            if state == "cancelled":
                return HelperAuthorizationCancellation(
                    transaction_id, nonce, session_id, replayed=True
                )
            if state != "pending":
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            updated = connection.execute(
                """
                UPDATE native_lifecycle_authorization_challenges_v2
                   SET state = 'cancelled', completed_at = ?
                 WHERE nonce = ? AND state = 'pending'
                """,
                (observed_now, nonce),
            )
            if updated.rowcount != 1:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_conflict"
                )
            return HelperAuthorizationCancellation(
                transaction_id, nonce, session_id, replayed=False
            )

        return self._write(operation)

    def helper_authorization_status(
        self, transaction_id: str
    ) -> dict[str, object]:
        """Return a path-free authorization status for one transaction."""

        transaction_id = _canonical_uuid(transaction_id)
        observed_now = self._timestamp()

        def operation(connection: sqlite3.Connection) -> dict[str, object]:
            current = self._require(connection, transaction_id)
            row = connection.execute(
                """
                SELECT state, issued_at, expires_at
                  FROM native_lifecycle_authorization_challenges_v2
                 WHERE transaction_id = ?
                 ORDER BY issued_at DESC, nonce DESC LIMIT 1
                """,
                (transaction_id,),
            ).fetchone()
            state = "unavailable"
            can_request = False
            if (
                current.phase is LifecyclePhase.HELPER_STAGED
                and self.helper_authorization_available
            ):
                state = "ready"
                can_request = True
            elif LifecyclePhase.AUTHORIZED in {
                event[1] for event in self._events(connection, transaction_id)
            }:
                state = "authorized"
            if row is not None and state != "authorized":
                stored = str(row["state"])
                if stored == "pending" and float(row["expires_at"]) > observed_now:
                    state = "challenge_pending"
                    can_request = False
                elif stored == "pending":
                    state = "challenge_expired"
                    can_request = current.phase is LifecyclePhase.HELPER_STAGED
                elif stored == "cancelled":
                    state = "challenge_cancelled"
                    can_request = current.phase is LifecyclePhase.HELPER_STAGED
                elif stored == "consumed":
                    state = "authorized"
                    can_request = False
                else:
                    raise NativeLifecycleIntegrityError(
                        "native_lifecycle_journal_corrupt"
                    )
            return {
                "schema_version": 2,
                "transaction_id": transaction_id,
                "phase": current.phase.value,
                "state": state,
                "can_request": can_request,
                "execution_available": False,
            }

        return self._read(operation)

    def helper_authorization_pending_count(self) -> int:
        observed_now = self._timestamp()

        def operation(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                  FROM native_lifecycle_authorization_challenges_v2
                 WHERE state = 'pending' AND expires_at > ?
                """,
                (observed_now,),
            ).fetchone()
            if row is None:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            total = int(row["total"])
            if not 0 <= total <= self._maximum_transactions:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            return total

        return self._read(operation)

    def require_helper_authority(
        self,
        *,
        transaction_id: str,
        authority_digest: str,
        execution_manifest_digest: str,
        helper_build_digest: str,
        authorization_digest: str,
        helper_session_id: str,
        now: float,
    ) -> tuple[HelperStageReceipt, HelperAuthorizationReceipt]:
        """Prove that memory-only authority matches durable v2 receipts."""

        transaction_id = _canonical_uuid(transaction_id)
        authority_digest = _validate_digest(authority_digest)
        execution_manifest_digest = _validate_digest(execution_manifest_digest)
        helper_build_digest = _validate_digest(helper_build_digest)
        authorization_digest = _validate_digest(authorization_digest)
        helper_session_id = _canonical_uuid(helper_session_id)
        now = _validate_timestamp(now)

        def operation(connection: sqlite3.Connection):
            transaction = self._require(connection, transaction_id)
            stage = self._helper_stage_receipt(connection, transaction_id)
            authorization = self._authorization_receipt(
                connection,
                transaction_id=transaction_id,
                authorization_digest=authorization_digest,
            )
            if (
                transaction.contract_version != _LIFECYCLE_CONTRACT_VERSION
                or transaction.authority_digest != authority_digest
                or stage is None
                or authorization is None
                or stage.execution_manifest_digest != execution_manifest_digest
                or stage.helper_build_digest != helper_build_digest
                or authorization.transaction_authority_digest != authority_digest
                or authorization.execution_manifest_digest
                != execution_manifest_digest
                or authorization.helper_build_digest != helper_build_digest
                or authorization.helper_session_id != helper_session_id
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_mismatch"
                )
            if now >= authorization.expires_at:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_expired"
                )
            manifest = self._execution_manifest_store.read(transaction_id)
            if manifest.receipt.manifest_digest != execution_manifest_digest:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_execution_manifest_conflict"
                )
            _validate_execution_manifest_binding(
                manifest, self._execution_binding_plan(transaction.plan)
            )
            return stage, authorization

        return self._read(operation)

    def create_execution_start_grant(
        self,
        *,
        transaction_id: str,
        direction: LifecycleExecutionDirection | str,
        authorization_digest: str,
        authorization_session_id: str,
    ) -> LifecycleExecutionGrantMutation:
        """Convert one live owner receipt into one immutable runner grant.

        The short-lived receipt must still be live at this boundary. Once the
        grant is committed, runner lease renewal is fenced by the immutable
        grant rather than by the expired biometric ceremony. A receipt may
        mint exactly one direction; changing direction requires a new receipt.
        """

        transaction_id = _canonical_uuid(transaction_id)
        direction = _enum(
            LifecycleExecutionDirection,
            direction,
            "native_lifecycle_execution_direction_invalid",
        )
        authorization_digest = _validate_digest(authorization_digest)
        authorization_session_id = _canonical_uuid(authorization_session_id)
        now = self._timestamp()

        def operation(
            connection: sqlite3.Connection,
        ) -> LifecycleExecutionGrantMutation:
            transaction = self._require(connection, transaction_id)
            self._require_execution_direction(transaction, direction)
            stage = self._helper_stage_receipt(connection, transaction_id)
            authorization = self._authorization_receipt(
                connection,
                transaction_id=transaction_id,
                authorization_digest=authorization_digest,
            )
            challenge_row = connection.execute(
                """
                SELECT *
                  FROM native_lifecycle_authorization_challenges_v2
                 WHERE transaction_id = ?
                   AND session_id = ?
                   AND authorization_digest = ?
                   AND state = 'consumed'
                """,
                (
                    transaction_id,
                    authorization_session_id,
                    authorization_digest,
                ),
            ).fetchone()
            challenge = (
                self._authorization_challenge_from_row(challenge_row)
                if challenge_row is not None
                else None
            )
            if (
                transaction.contract_version != _LIFECYCLE_CONTRACT_VERSION
                or stage is None
                or authorization is None
                or challenge is None
                or authorization.helper_session_id != authorization_session_id
                or challenge.session_id != authorization_session_id
                or _unprefixed_digest(challenge.authorization_digest)
                != authorization_digest
                or authorization.transaction_authority_digest
                != transaction.authority_digest
                or authorization.execution_manifest_digest
                != stage.execution_manifest_digest
                or authorization.helper_build_digest != stage.helper_build_digest
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_fresh_authorization_required"
                )
            if now >= authorization.expires_at:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_expired"
                )
            manifest = self._execution_manifest_store.read(transaction_id)
            _validate_execution_manifest_binding(
                manifest, self._execution_binding_plan(transaction.plan)
            )
            receipt = manifest.receipt
            runner = manifest.runner
            runner_requirement_digest = sha256(
                runner.code_requirement.encode("utf-8")
            ).hexdigest()
            if (
                receipt.manifest_digest != stage.execution_manifest_digest
                or receipt.recovery_clone_identity_digest
                != stage.recovery_clone_identity_digest
                or runner.identifier != PRODUCT_IDENTITY.lifecycle_runner_identifier
                or runner.team_identifier != manifest.application.team_identifier
                or _TEAM_IDENTIFIER_RE.fullmatch(runner.team_identifier) is None
                or runner.team_identifier
                in {"ADHOC00000", "UNSIGNED00", "NOTSET0000"}
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_runner_identity_unavailable"
                )
            existing_row = connection.execute(
                """
                SELECT *
                  FROM native_lifecycle_execution_start_grants_v2
                 WHERE transaction_id = ? AND authorization_digest = ?
                """,
                (transaction_id, authorization_digest),
            ).fetchone()
            if existing_row is not None:
                existing = self._execution_start_grant_from_row(existing_row)
                if (
                    existing.direction is not direction
                    or existing.transaction_authority_digest
                    != transaction.authority_digest
                    or existing.execution_manifest_digest
                    != receipt.manifest_digest
                    or existing.recovery_clone_identity_digest
                    != receipt.recovery_clone_identity_digest
                    or existing.runner_identifier != runner.identifier
                    or existing.runner_build_digest != runner.build_digest
                    or existing.runner_identity_digest != runner.identity_digest
                    or existing.runner_team_identifier != runner.team_identifier
                    or existing.runner_code_requirement_digest
                    != runner_requirement_digest
                    or existing.authorization_session_id
                    != authorization_session_id
                    or existing.authorization_expires_at
                    != authorization.expires_at
                ):
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_execution_fresh_authorization_required"
                    )
                return LifecycleExecutionGrantMutation(existing, replayed=True)
            count_row = connection.execute(
                """
                SELECT COUNT(*) AS total, COALESCE(MAX(grant_sequence), 0) AS maximum
                  FROM native_lifecycle_execution_start_grants_v2
                 WHERE transaction_id = ?
                """,
                (transaction_id,),
            ).fetchone()
            if count_row is None:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            count = int(count_row["total"])
            if count >= _MAX_EXECUTION_START_GRANTS_PER_TRANSACTION:
                raise NativeLifecycleCapacityError(
                    "native_lifecycle_execution_grant_capacity_exhausted"
                )
            grant = LifecycleExecutionStartGrant(
                grant_id=str(uuid4()),
                transaction_id=transaction_id,
                direction=direction,
                transaction_authority_digest=transaction.authority_digest,
                execution_manifest_digest=receipt.manifest_digest,
                recovery_clone_identity_digest=(
                    receipt.recovery_clone_identity_digest
                ),
                runner_identifier=runner.identifier,
                runner_build_digest=runner.build_digest,
                runner_identity_digest=runner.identity_digest,
                runner_team_identifier=runner.team_identifier,
                runner_code_requirement_digest=runner_requirement_digest,
                authorization_digest=authorization.authorization_digest,
                authorization_session_id=authorization.helper_session_id,
                authorization_expires_at=authorization.expires_at,
                grant_sequence=int(count_row["maximum"]) + 1,
                created_at=now,
                grant_digest="",
            )
            grant = replace(
                grant,
                grant_digest=sha256(
                    _canonical_json(grant.binding_dict()).encode("utf-8")
                ).hexdigest(),
            )
            self._validate_execution_start_grant(grant)
            connection.execute(
                """
                INSERT INTO native_lifecycle_execution_start_grants_v2 (
                    grant_id, transaction_id, direction, authority_digest,
                    execution_manifest_digest, recovery_clone_identity_digest,
                    runner_identifier, runner_build_digest,
                    runner_identity_digest, runner_team_identifier,
                    runner_code_requirement_digest, authorization_digest,
                    authorization_session_id, authorization_expires_at,
                    grant_sequence, created_at, grant_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant.grant_id,
                    grant.transaction_id,
                    grant.direction.value,
                    grant.transaction_authority_digest,
                    grant.execution_manifest_digest,
                    grant.recovery_clone_identity_digest,
                    grant.runner_identifier,
                    grant.runner_build_digest,
                    grant.runner_identity_digest,
                    grant.runner_team_identifier,
                    grant.runner_code_requirement_digest,
                    grant.authorization_digest,
                    grant.authorization_session_id,
                    grant.authorization_expires_at,
                    grant.grant_sequence,
                    grant.created_at,
                    grant.grant_digest,
                ),
            )
            return LifecycleExecutionGrantMutation(grant, replayed=False)

        return self._write(operation)

    def require_execution_start_grant(
        self, *, transaction_id: str, grant_id: str
    ) -> LifecycleExecutionStartGrant:
        transaction_id = _canonical_uuid(transaction_id)
        grant_id = _canonical_uuid(grant_id)

        def operation(connection: sqlite3.Connection) -> LifecycleExecutionStartGrant:
            row = connection.execute(
                """
                SELECT * FROM native_lifecycle_execution_start_grants_v2
                 WHERE transaction_id = ? AND grant_id = ?
                """,
                (transaction_id, grant_id),
            ).fetchone()
            if row is None:
                raise NativeLifecycleNotFoundError(
                    "native_lifecycle_execution_grant_not_found"
                )
            grant = self._execution_start_grant_from_row(row)
            transaction = self._require(connection, transaction_id)
            self._validate_execution_grant_binding(
                connection, transaction, grant
            )
            self._require_execution_direction(transaction, grant.direction)
            return grant

        return self._read(operation)

    def register_lifecycle_runner(
        self,
        registration: LifecycleRunnerRegistrationV2,
        *,
        process_fence: LifecycleRunnerProcessFence | None = None,
    ) -> LifecycleRunnerLeaseMutation:
        """Acquire or renew an epoch while holding the exact process fence."""

        try:
            registration = validate_lifecycle_execution_message(registration)
        except Exception:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_registration_invalid"
            ) from None
        if not isinstance(registration, LifecycleRunnerRegistrationV2):
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_registration_invalid"
            )
        now = max(1, int(math.floor(self._timestamp())))
        boot_id, monotonic_now = self._validate_runner_process_fence(
            process_fence, transaction_id=registration.transaction_id
        )
        assert process_fence is not None
        registration_digest = sha256(
            _canonical_json(registration.to_wire_dict()).encode("utf-8")
        ).hexdigest()

        def operation(
            connection: sqlite3.Connection,
        ) -> LifecycleRunnerLeaseMutation:
            transaction = self._require(connection, registration.transaction_id)
            row = connection.execute(
                """
                SELECT * FROM native_lifecycle_execution_start_grants_v2
                 WHERE grant_id = ? AND transaction_id = ?
                """,
                (registration.grant_id, registration.transaction_id),
            ).fetchone()
            if row is None:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_grant_invalid"
                )
            grant = self._execution_start_grant_from_row(row)
            self._validate_execution_grant_binding(
                connection, transaction, grant
            )
            self._require_execution_direction(transaction, grant.direction)
            replay_row = connection.execute(
                """
                SELECT * FROM native_lifecycle_runner_lease_epochs_v2
                 WHERE registration_nonce = ?
                """,
                (registration.nonce,),
            ).fetchone()
            if replay_row is not None:
                replay_lease = self._runner_lease_from_row(replay_row)
                if (
                    str(replay_row["registration_digest"])
                    != registration_digest
                    or replay_lease.process_fence_id
                    != process_fence.fence_id
                    or replay_lease.boot_id != boot_id
                ):
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_execution_registration_replayed"
                    )
                return LifecycleRunnerLeaseMutation(
                    replay_lease, replayed=True
                )
            if (
                registration.grant_digest != _prefixed_digest(grant.grant_digest)
                or registration.runner_identifier != grant.runner_identifier
                or registration.runner_build_digest
                != _prefixed_digest(grant.runner_build_digest)
                or registration.runner_identity_digest
                != _prefixed_digest(grant.runner_identity_digest)
                or registration.team_identifier != grant.runner_team_identifier
                or registration.code_requirement_digest
                != _prefixed_digest(grant.runner_code_requirement_digest)
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_runner_identity_mismatch"
                )
            active_rows = connection.execute(
                """
                SELECT lease.*
                  FROM native_lifecycle_runner_lease_epochs_v2 AS lease
                 WHERE lease.boot_id = ?
                   AND lease.expires_monotonic > ?
                   AND lease.lease_epoch = (
                       SELECT MAX(candidate.lease_epoch)
                         FROM native_lifecycle_runner_lease_epochs_v2 AS candidate
                        WHERE candidate.transaction_id = lease.transaction_id
                   )
                 ORDER BY issued_at DESC, transaction_id, lease_epoch DESC
                """,
                (boot_id, monotonic_now),
            ).fetchall()
            if len(active_rows) > 1:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            previous: LifecycleRunnerLeaseEpoch | None = None
            if active_rows:
                previous = self._runner_lease_from_row(active_rows[0])
                if (
                    previous.transaction_id != registration.transaction_id
                    or previous.grant_id != registration.grant_id
                    or previous.runner_session_id
                    != registration.runner_session_id
                    or previous.process_fence_id != process_fence.fence_id
                    or previous.boot_id != boot_id
                ):
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_execution_lease_conflict"
                    )
                previous_sequence = int(active_rows[0]["registration_sequence"])
                if registration.sequence != previous_sequence + 1:
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_execution_registration_replayed"
                    )
            else:
                last_session = connection.execute(
                    """
                    SELECT * FROM native_lifecycle_runner_lease_epochs_v2
                     WHERE grant_id = ? AND runner_session_id = ?
                     ORDER BY lease_epoch DESC LIMIT 1
                    """,
                    (registration.grant_id, registration.runner_session_id),
                ).fetchone()
                expected_sequence = (
                    int(last_session["registration_sequence"]) + 1
                    if last_session is not None
                    else 1
                )
                if registration.sequence != expected_sequence:
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_execution_registration_replayed"
                    )
                latest = connection.execute(
                    """
                    SELECT * FROM native_lifecycle_runner_lease_epochs_v2
                     WHERE transaction_id = ?
                     ORDER BY lease_epoch DESC LIMIT 1
                    """,
                    (registration.transaction_id,),
                ).fetchone()
                previous = (
                    self._runner_lease_from_row(latest)
                    if latest is not None
                    else None
                )
            maximum = connection.execute(
                """
                SELECT COALESCE(MAX(lease_epoch), 0) AS maximum,
                       COUNT(*) AS total
                  FROM native_lifecycle_runner_lease_epochs_v2
                 WHERE transaction_id = ?
                """,
                (registration.transaction_id,),
            ).fetchone()
            if maximum is None or int(maximum["total"]) >= (
                _MAX_RUNNER_LEASE_EPOCHS_PER_TRANSACTION
            ):
                raise NativeLifecycleCapacityError(
                    "native_lifecycle_execution_lease_capacity_exhausted"
                )
            lease = LifecycleRunnerLeaseEpoch(
                lease_id=str(uuid4()),
                transaction_id=registration.transaction_id,
                grant_id=registration.grant_id,
                runner_session_id=registration.runner_session_id,
                process_fence_id=process_fence.fence_id,
                boot_id=boot_id,
                registration_nonce=registration.nonce,
                lease_epoch=int(maximum["maximum"]) + 1,
                prior_lease_id=(previous.lease_id if previous is not None else None),
                issued_at=now,
                expires_at=now + registration.requested_lease_seconds,
                issued_monotonic=monotonic_now,
                expires_monotonic=(
                    monotonic_now + registration.requested_lease_seconds
                ),
            )
            connection.execute(
                """
                INSERT INTO native_lifecycle_runner_lease_epochs_v2 (
                    lease_id, transaction_id, grant_id, runner_session_id,
                    process_fence_id, boot_id, registration_nonce,
                    registration_digest,
                    registration_sequence, lease_epoch, prior_lease_id,
                    issued_at, expires_at, issued_monotonic,
                    expires_monotonic
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease.lease_id,
                    lease.transaction_id,
                    lease.grant_id,
                    lease.runner_session_id,
                    lease.process_fence_id,
                    lease.boot_id,
                    lease.registration_nonce,
                    registration_digest,
                    registration.sequence,
                    lease.lease_epoch,
                    lease.prior_lease_id,
                    lease.issued_at,
                    lease.expires_at,
                    lease.issued_monotonic,
                    lease.expires_monotonic,
                ),
            )
            return LifecycleRunnerLeaseMutation(lease, replayed=False)

        return self._write(operation)

    def runner_registration_response(
        self,
        registration: LifecycleRunnerRegistrationV2,
        mutation: LifecycleRunnerLeaseMutation,
    ) -> LifecycleRunnerRegisteredV2:
        """Build the closed response for one accepted registration."""

        lease = mutation.lease
        return LifecycleRunnerRegisteredV2(
            protocol_version=2,
            message_type=LifecycleExecutionMessageType.REGISTERED,
            transaction_id=lease.transaction_id,
            grant_id=lease.grant_id,
            grant_digest=registration.grant_digest,
            runner_session_id=lease.runner_session_id,
            sequence=registration.sequence,
            nonce=str(uuid4()),
            request_nonce=registration.nonce,
            lease_id=lease.lease_id,
            lease_epoch=lease.lease_epoch,
            lease_expires_at=lease.expires_at,
        )

    def record_lifecycle_effect_receipt(
        self,
        *,
        process_fence: LifecycleRunnerProcessFence | None = None,
        transaction_id: str,
        grant_id: str,
        lease_id: str,
        lease_epoch: int,
        sequence: int,
        request_nonce: str,
        effect_id: str,
        effect_kind: LifecycleEffectKind | str,
        target_digest: str,
        attempt: int,
        status: LifecycleEffectReceiptStatus | str,
        observation: LifecycleEffectObservation | str,
        before_observation_digest: str | None = None,
        after_observation_digest: str | None = None,
        prior_receipt_digest: str | None = None,
        fixed_error_code: str | None = None,
    ) -> LifecycleEffectReceiptMutation:
        """Append one exact target-level receipt to a monotonic effect chain."""

        transaction_id = _canonical_uuid(transaction_id)
        grant_id = _canonical_uuid(grant_id)
        lease_id = _canonical_uuid(lease_id)
        lease_epoch = _bounded_positive_integer(lease_epoch, maximum=1_000_000)
        sequence = _bounded_positive_integer(sequence, maximum=1_000_000)
        request_nonce = _canonical_uuid(request_nonce)
        effect_id = _canonical_uuid(effect_id)
        effect_kind = _enum(
            LifecycleEffectKind,
            effect_kind,
            "native_lifecycle_effect_receipt_invalid",
        )
        target_digest = _validate_digest(target_digest)
        attempt = _bounded_positive_integer(attempt, maximum=1_024)
        status = _enum(
            LifecycleEffectReceiptStatus,
            status,
            "native_lifecycle_effect_receipt_invalid",
        )
        observation = _enum(
            LifecycleEffectObservation,
            observation,
            "native_lifecycle_effect_receipt_invalid",
        )
        before_observation_digest = _optional_digest(before_observation_digest)
        after_observation_digest = _optional_digest(after_observation_digest)
        prior_receipt_digest = _optional_digest(prior_receipt_digest)
        if fixed_error_code is not None and fixed_error_code not in (
            _EFFECT_RECEIPT_ERROR_CODES
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_effect_receipt_invalid"
            )
        self._validate_effect_receipt_shape(
            status=status,
            observation=observation,
            before_digest=before_observation_digest,
            after_digest=after_observation_digest,
            fixed_error_code=fixed_error_code,
        )
        _ = self._timestamp()  # diagnostics only; authority uses monotonic time
        boot_id, monotonic_now = self._validate_runner_process_fence(
            process_fence, transaction_id=transaction_id
        )

        def operation(
            connection: sqlite3.Connection,
        ) -> LifecycleEffectReceiptMutation:
            replay_row = connection.execute(
                """
                SELECT * FROM native_lifecycle_effect_receipts_v2
                 WHERE request_nonce = ?
                """,
                (request_nonce,),
            ).fetchone()
            if replay_row is not None:
                replay = self._effect_receipt_from_row(replay_row)
                requested = replace(
                    replay,
                    receipt_id=replay.receipt_id,
                    recorded_at=replay.recorded_at,
                    receipt_digest=replay.receipt_digest,
                )
                if (
                    requested.transaction_id != transaction_id
                    or requested.grant_id != grant_id
                    or requested.lease_id != lease_id
                    or requested.lease_epoch != lease_epoch
                    or requested.sequence != sequence
                    or requested.effect_id != effect_id
                    or requested.effect_kind is not effect_kind
                    or requested.target_digest != target_digest
                    or requested.attempt != attempt
                    or requested.status is not status
                    or requested.observation is not observation
                    or requested.before_observation_digest
                    != before_observation_digest
                    or requested.after_observation_digest
                    != after_observation_digest
                    or requested.prior_receipt_digest != prior_receipt_digest
                    or requested.fixed_error_code != fixed_error_code
                ):
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_effect_receipt_replayed"
                    )
                return LifecycleEffectReceiptMutation(replay, replayed=True)
            lease_row = connection.execute(
                """
                SELECT * FROM native_lifecycle_runner_lease_epochs_v2
                 WHERE lease_id = ? AND transaction_id = ? AND grant_id = ?
                """,
                (lease_id, transaction_id, grant_id),
            ).fetchone()
            if lease_row is None:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_lease_conflict"
                )
            lease = self._runner_lease_from_row(lease_row)
            self._validate_runner_process_fence(
                process_fence,
                transaction_id=transaction_id,
                expected_fence_id=lease.process_fence_id,
            )
            if (
                lease.lease_epoch != lease_epoch
                or lease.boot_id != boot_id
                or monotonic_now >= lease.expires_monotonic
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_lease_expired"
                )
            logical_recorded_at = lease.issued_at + (
                monotonic_now - lease.issued_monotonic
            )
            latest_lease = connection.execute(
                """
                SELECT lease_id FROM native_lifecycle_runner_lease_epochs_v2
                 WHERE transaction_id = ? ORDER BY lease_epoch DESC LIMIT 1
                """,
                (transaction_id,),
            ).fetchone()
            if latest_lease is None or str(latest_lease["lease_id"]) != lease_id:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_lease_conflict"
                )
            transaction = self._require(connection, transaction_id)
            grant_row = connection.execute(
                "SELECT * FROM native_lifecycle_execution_start_grants_v2 WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            if grant_row is None:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            grant = self._execution_start_grant_from_row(grant_row)
            self._validate_execution_grant_binding(
                connection,
                transaction,
                grant,
            )
            self._require_execution_direction(
                transaction,
                grant.direction,
            )
            manifest = self._execution_manifest_store.read(transaction_id)
            _validate_execution_manifest_binding(
                manifest, self._execution_binding_plan(transaction.plan)
            )
            direction_graph = _authorized_lifecycle_effects(
                transaction, manifest, grant.direction
            )
            authorized = {
                (item.effect_id, item.effect_kind, item.target_digest)
                for item in direction_graph
            }
            if (effect_id, effect_kind, target_digest) not in authorized:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_effect_authority_mismatch"
                )
            totals = connection.execute(
                """
                SELECT COUNT(*) AS total, COALESCE(MAX(sequence), 0) AS maximum
                  FROM native_lifecycle_effect_receipts_v2
                 WHERE transaction_id = ?
                """,
                (transaction_id,),
            ).fetchone()
            if totals is None or int(totals["total"]) >= (
                _MAX_EFFECT_RECEIPTS_PER_TRANSACTION
            ):
                raise NativeLifecycleCapacityError(
                    "native_lifecycle_effect_receipt_capacity_exhausted"
                )
            if sequence != int(totals["maximum"]) + 1:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_effect_receipt_out_of_order"
                )
            prior_row = connection.execute(
                """
                SELECT * FROM native_lifecycle_effect_receipts_v2
                 WHERE transaction_id = ?
                   AND effect_id = ? AND target_digest = ?
                 ORDER BY sequence DESC LIMIT 1
                """,
                (transaction_id, effect_id, target_digest),
            ).fetchone()
            prior = (
                self._effect_receipt_from_row(prior_row)
                if prior_row is not None
                else None
            )
            if (prior.receipt_digest if prior is not None else None) != (
                prior_receipt_digest
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_effect_receipt_out_of_order"
                )
            if prior is not None and (
                prior.effect_kind is not effect_kind or attempt < prior.attempt
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_effect_receipt_out_of_order"
                )
            receipt = LifecycleEffectReceipt(
                receipt_id=str(uuid4()),
                transaction_id=transaction_id,
                grant_id=grant_id,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
                sequence=sequence,
                request_nonce=request_nonce,
                effect_id=effect_id,
                effect_kind=effect_kind,
                target_digest=target_digest,
                attempt=attempt,
                status=status,
                observation=observation,
                before_observation_digest=before_observation_digest,
                after_observation_digest=after_observation_digest,
                prior_receipt_digest=prior_receipt_digest,
                fixed_error_code=fixed_error_code,
                recorded_at=logical_recorded_at,
                receipt_digest="",
            )
            self._validate_effect_receipt_transition(prior, receipt)
            target_index = next(
                index
                for index, item in enumerate(direction_graph)
                if (
                    item.effect_id,
                    item.effect_kind,
                    item.target_digest,
                ) == (effect_id, effect_kind, target_digest)
            )
            if prior is None and target_index > 0:
                previous_target = direction_graph[target_index - 1]
                previous_row = connection.execute(
                    """
                    SELECT * FROM native_lifecycle_effect_receipts_v2
                     WHERE transaction_id = ?
                       AND effect_id = ? AND target_digest = ?
                     ORDER BY sequence DESC LIMIT 1
                    """,
                    (
                        transaction_id,
                        previous_target.effect_id,
                        previous_target.target_digest,
                    ),
                ).fetchone()
                previous = (
                    self._effect_receipt_from_row(previous_row)
                    if previous_row is not None
                    else None
                )
                if (
                    previous is None
                    or previous.effect_kind is not previous_target.effect_kind
                    or previous.status
                    is not LifecycleEffectReceiptStatus.FINALIZED
                    or previous.observation
                    is not LifecycleEffectObservation.EFFECT_SATISFIED
                ):
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_effect_receipt_out_of_order"
                    )
            receipt = replace(
                receipt,
                receipt_digest=sha256(
                    _canonical_json(
                        self._effect_receipt_binding_dict(receipt)
                    ).encode("utf-8")
                ).hexdigest(),
            )
            connection.execute(
                """
                INSERT INTO native_lifecycle_effect_receipts_v2 (
                    receipt_id, transaction_id, grant_id, lease_id,
                    lease_epoch, sequence, request_nonce, effect_id,
                    effect_kind, target_digest, attempt, status, observation,
                    before_observation_digest, after_observation_digest,
                    prior_receipt_digest, fixed_error_code, recorded_at,
                    receipt_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    receipt.transaction_id,
                    receipt.grant_id,
                    receipt.lease_id,
                    receipt.lease_epoch,
                    receipt.sequence,
                    receipt.request_nonce,
                    receipt.effect_id,
                    receipt.effect_kind.value,
                    receipt.target_digest,
                    receipt.attempt,
                    receipt.status.value,
                    receipt.observation.value,
                    receipt.before_observation_digest,
                    receipt.after_observation_digest,
                    receipt.prior_receipt_digest,
                    receipt.fixed_error_code,
                    receipt.recorded_at,
                    receipt.receipt_digest,
                ),
            )
            return LifecycleEffectReceiptMutation(receipt, replayed=False)

        return self._write(operation)

    def list_lifecycle_effect_receipts(
        self, transaction_id: str
    ) -> tuple[LifecycleEffectReceipt, ...]:
        transaction_id = _canonical_uuid(transaction_id)

        def operation(connection: sqlite3.Connection):
            self._require(connection, transaction_id)
            return tuple(
                self._effect_receipt_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM native_lifecycle_effect_receipts_v2
                     WHERE transaction_id = ? ORDER BY sequence
                    """,
                    (transaction_id,),
                ).fetchall()
            )

        return self._read(operation)

    def authorized_lifecycle_effects(
        self,
        transaction_id: str,
        *,
        direction: LifecycleExecutionDirection | str,
    ) -> tuple[AuthorizedLifecycleEffect, ...]:
        """Return only opaque targets derived from immutable private authority."""

        transaction_id = _canonical_uuid(transaction_id)
        direction = _enum(
            LifecycleExecutionDirection,
            direction,
            "native_lifecycle_execution_direction_invalid",
        )

        def operation(connection: sqlite3.Connection):
            transaction = self._require(connection, transaction_id)
            manifest = self._execution_manifest_store.read(transaction_id)
            _validate_execution_manifest_binding(
                manifest, self._execution_binding_plan(transaction.plan)
            )
            return _authorized_lifecycle_effects(
                transaction, manifest, direction
            )

        return self._read(operation)

    def advance(
        self, transaction_id: str, phase: LifecyclePhase | str
    ) -> LifecycleJournalMutation:
        transaction_id = _canonical_uuid(transaction_id)
        phase = _enum(
            LifecyclePhase, phase, "native_lifecycle_phase_invalid"
        )
        if phase in {
            LifecyclePhase.DISCOVERED,
            LifecyclePhase.PREPARED,
            LifecyclePhase.HELPER_STAGED,
            LifecyclePhase.AUTHORIZED,
            LifecyclePhase.MANUAL_RECOVERY,
        }:
            raise NativeLifecycleConflictError(
                "native_lifecycle_phase_out_of_order"
            )

        def operation(connection: sqlite3.Connection) -> LifecycleJournalMutation:
            current = self._require(connection, transaction_id)
            events = self._events(connection, transaction_id)
            if any(event[1] is phase for event in events):
                return LifecycleJournalMutation(current, replayed=True)
            if current.phase in _TERMINAL_PHASES or current.phase is LifecyclePhase.MANUAL_RECOVERY:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_transaction_terminal"
                )
            if phase not in _allowed_next_phases(
                current.kind, current.phase, current.contract_version
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_phase_out_of_order"
                )
            now = max(self._timestamp(), current.updated_at)
            self._append_phase_in_connection(connection, current, phase, now)
            return LifecycleJournalMutation(
                self._require(connection, transaction_id), replayed=False
            )

        return self._write(operation)

    def mark_manual_recovery(
        self, transaction_id: str, error_code: str
    ) -> LifecycleJournalMutation:
        transaction_id = _canonical_uuid(transaction_id)
        if error_code not in MANUAL_RECOVERY_CODES:
            raise NativeLifecycleConflictError(
                "native_lifecycle_error_code_invalid"
            )

        def operation(connection: sqlite3.Connection) -> LifecycleJournalMutation:
            current = self._require(connection, transaction_id)
            if current.phase is LifecyclePhase.MANUAL_RECOVERY:
                if current.error_code != error_code:
                    raise NativeLifecycleConflictError(
                        "native_lifecycle_transaction_conflict"
                    )
                return LifecycleJournalMutation(current, replayed=True)
            if current.phase in _TERMINAL_PHASES:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_transaction_terminal"
                )
            self._mark_manual_in_connection(
                connection, current, error_code, self._timestamp()
            )
            return LifecycleJournalMutation(
                self._require(connection, transaction_id), replayed=False
            )

        return self._write(operation)

    def get(self, transaction_id: str) -> LifecycleTransaction | None:
        transaction_id = _canonical_uuid(transaction_id)
        return self._read(
            lambda connection: self._select(connection, transaction_id)
            or self._select_legacy(connection, transaction_id)
        )

    def list_all(self) -> tuple[LifecycleTransaction, ...]:
        def operation(connection: sqlite3.Connection):
            rows = (*self._select_many(connection, ""), *self._select_legacy_many(connection))
            return tuple(sorted(rows, key=lambda item: (item.created_at, item.transaction_id)))

        return self._read(operation)

    def list_incomplete(self) -> tuple[LifecycleTransaction, ...]:
        terminal = ",".join(f"'{phase.value}'" for phase in _TERMINAL_PHASES)
        def operation(connection: sqlite3.Connection):
            rows = (
                *self._select_many(
                    connection, f"WHERE current_phase NOT IN ({terminal})"
                ),
                *(
                    item
                    for item in self._select_legacy_many(connection)
                    if not item.terminal
                ),
            )
            return tuple(sorted(rows, key=lambda item: (item.created_at, item.transaction_id)))

        return self._read(operation)

    def acquire_execution_claim(
        self,
        *,
        transaction_id: str,
        authority_digest: str,
        helper_session_id: str,
        claim_id: str,
        expires_at: float,
        now: float,
    ) -> LifecycleExecutionClaimResult:
        """Atomically fence all product lifecycle effects to one executor.

        An expired foreign claim is never stolen.  Its transaction is moved
        to durable manual recovery first, which keeps every other lifecycle
        transaction blocked until a future authenticated recovery ceremony.
        """

        transaction_id = _canonical_uuid(transaction_id)
        _validate_digest(authority_digest)
        helper_session_id = _canonical_uuid(helper_session_id)
        claim_id = _canonical_uuid(claim_id)
        expires_at = _validate_timestamp(expires_at)
        now = _validate_timestamp(now)
        if expires_at <= now:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_claim_invalid"
            )

        def operation(
            connection: sqlite3.Connection,
        ) -> LifecycleExecutionClaimResult:
            current = self._require(connection, transaction_id)
            if current.authority_digest != authority_digest:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_claim_invalid"
                )
            existing = self._execution_claim(connection)
            if existing is not None:
                (
                    bound_transaction_id,
                    bound_authority_digest,
                    bound_session_id,
                    bound_claim_id,
                    bound_expires_at,
                    _created_at,
                ) = existing
                bound = self._require(connection, bound_transaction_id)
                if bound.terminal:
                    connection.execute(
                        "DELETE FROM native_lifecycle_execution_claims_v2 "
                        "WHERE singleton = 1"
                    )
                elif bound_expires_at <= now:
                    if bound.phase is not LifecyclePhase.MANUAL_RECOVERY:
                        self._mark_manual_in_connection(
                            connection,
                            bound,
                            "recovery_observation_unavailable",
                            now,
                        )
                    connection.execute(
                        "DELETE FROM native_lifecycle_execution_claims_v2 "
                        "WHERE singleton = 1"
                    )
                    return LifecycleExecutionClaimResult(
                        LifecycleExecutionClaimState.EXPIRED,
                        bound_transaction_id,
                    )
                elif (
                    bound_transaction_id == transaction_id
                    and bound_authority_digest == authority_digest
                    and bound_session_id == helper_session_id
                    and bound_claim_id == claim_id
                ):
                    return LifecycleExecutionClaimResult(
                        LifecycleExecutionClaimState.ACQUIRED
                    )
                else:
                    return LifecycleExecutionClaimResult(
                        LifecycleExecutionClaimState.BLOCKED,
                        bound_transaction_id,
                    )
            connection.execute(
                """
                INSERT INTO native_lifecycle_execution_claims_v2 (
                    singleton, transaction_id, authority_digest,
                    helper_session_id, claim_id, expires_at, created_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    authority_digest,
                    helper_session_id,
                    claim_id,
                    expires_at,
                    now,
                ),
            )
            return LifecycleExecutionClaimResult(
                LifecycleExecutionClaimState.ACQUIRED
            )

        return self._write(operation)

    def execution_claim_is_current(
        self,
        *,
        transaction_id: str,
        authority_digest: str,
        helper_session_id: str,
        claim_id: str,
    ) -> bool:
        transaction_id = _canonical_uuid(transaction_id)
        _validate_digest(authority_digest)
        helper_session_id = _canonical_uuid(helper_session_id)
        claim_id = _canonical_uuid(claim_id)

        def operation(connection: sqlite3.Connection) -> bool:
            existing = self._execution_claim(connection)
            return existing is not None and existing[:4] == (
                transaction_id,
                authority_digest,
                helper_session_id,
                claim_id,
            )

        return self._read(operation)

    def release_execution_claim(
        self,
        *,
        transaction_id: str,
        authority_digest: str,
        helper_session_id: str,
        claim_id: str,
    ) -> bool:
        transaction_id = _canonical_uuid(transaction_id)
        _validate_digest(authority_digest)
        helper_session_id = _canonical_uuid(helper_session_id)
        claim_id = _canonical_uuid(claim_id)

        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                DELETE FROM native_lifecycle_execution_claims_v2
                 WHERE singleton = 1
                   AND transaction_id = ?
                   AND authority_digest = ?
                   AND helper_session_id = ?
                   AND claim_id = ?
                """,
                (
                    transaction_id,
                    authority_digest,
                    helper_session_id,
                    claim_id,
                ),
            )
            return cursor.rowcount == 1

        return self._write(operation)

    def request_rollback(
        self,
        *,
        transaction_id: str,
        authority_digest: str,
        helper_session_id: str,
        claim_id: str,
    ) -> LifecycleJournalMutation:
        """Durably make an eligible migration rollback-only."""

        transaction_id = _canonical_uuid(transaction_id)
        _validate_digest(authority_digest)
        helper_session_id = _canonical_uuid(helper_session_id)
        claim_id = _canonical_uuid(claim_id)

        def operation(connection: sqlite3.Connection) -> LifecycleJournalMutation:
            current = self._require(connection, transaction_id)
            existing = self._execution_claim(connection)
            if existing is None or existing[:4] != (
                transaction_id,
                authority_digest,
                helper_session_id,
                claim_id,
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_claim_invalid"
                )
            if (
                current.kind is not LifecycleKind.MIGRATION
                or current.phase not in _MIGRATION_ROLLBACK_FROM
            ):
                raise NativeLifecycleConflictError("rollback_not_available")
            if current.rollback_requested:
                return LifecycleJournalMutation(current, replayed=True)
            now = max(self._timestamp(), current.updated_at)
            connection.execute(
                """
                UPDATE native_lifecycle_control_v2
                   SET rollback_requested = 1, updated_at = ?
                 WHERE transaction_id = ?
                """,
                (now, transaction_id),
            )
            return LifecycleJournalMutation(
                self._require(connection, transaction_id), replayed=False
            )

        return self._write(operation)

    @staticmethod
    def _require_execution_direction(
        transaction: LifecycleTransaction,
        direction: LifecycleExecutionDirection,
    ) -> None:
        if transaction.terminal:
            raise NativeLifecycleConflictError(
                "native_lifecycle_transaction_terminal"
            )
        if direction is LifecycleExecutionDirection.FORWARD:
            valid = (
                transaction.phase is not LifecyclePhase.MANUAL_RECOVERY
                and not transaction.rollback_requested
            )
        elif direction is LifecycleExecutionDirection.ROLLBACK:
            valid = (
                transaction.kind is LifecycleKind.MIGRATION
                and transaction.rollback_requested
                and transaction.phase in _MIGRATION_ROLLBACK_FROM
            )
        else:
            valid = transaction.phase is LifecyclePhase.MANUAL_RECOVERY
        if not valid:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_fresh_authorization_required"
            )

    @staticmethod
    def _validate_execution_start_grant(
        grant: LifecycleExecutionStartGrant,
    ) -> None:
        try:
            _canonical_uuid(grant.grant_id)
            _canonical_uuid(grant.transaction_id)
            if not isinstance(grant.direction, LifecycleExecutionDirection):
                raise ValueError
            for digest in (
                grant.transaction_authority_digest,
                grant.execution_manifest_digest,
                grant.recovery_clone_identity_digest,
                grant.runner_build_digest,
                grant.runner_identity_digest,
                grant.runner_code_requirement_digest,
                grant.authorization_digest,
                grant.grant_digest,
            ):
                _validate_digest(digest)
            if grant.runner_identifier != PRODUCT_IDENTITY.lifecycle_runner_identifier:
                raise ValueError
            if (
                _TEAM_IDENTIFIER_RE.fullmatch(grant.runner_team_identifier) is None
                or grant.runner_team_identifier
                in {"ADHOC00000", "UNSIGNED00", "NOTSET0000"}
            ):
                raise ValueError
            _canonical_uuid(grant.authorization_session_id)
            _validate_timestamp(grant.authorization_expires_at)
            _bounded_positive_integer(grant.grant_sequence, maximum=32)
            _validate_timestamp(grant.created_at)
            expected = sha256(
                _canonical_json(grant.binding_dict()).encode("utf-8")
            ).hexdigest()
            if not hmac.compare_digest(expected, grant.grant_digest):
                raise ValueError
        except (NativeLifecycleError, TypeError, ValueError):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            ) from None

    def _execution_start_grant_from_row(
        self, row: sqlite3.Row
    ) -> LifecycleExecutionStartGrant:
        try:
            grant = LifecycleExecutionStartGrant(
                grant_id=str(row["grant_id"]),
                transaction_id=str(row["transaction_id"]),
                direction=LifecycleExecutionDirection(str(row["direction"])),
                transaction_authority_digest=str(row["authority_digest"]),
                execution_manifest_digest=str(row["execution_manifest_digest"]),
                recovery_clone_identity_digest=str(
                    row["recovery_clone_identity_digest"]
                ),
                runner_identifier=str(row["runner_identifier"]),
                runner_build_digest=str(row["runner_build_digest"]),
                runner_identity_digest=str(row["runner_identity_digest"]),
                runner_team_identifier=str(row["runner_team_identifier"]),
                runner_code_requirement_digest=str(
                    row["runner_code_requirement_digest"]
                ),
                authorization_digest=str(row["authorization_digest"]),
                authorization_session_id=str(row["authorization_session_id"]),
                authorization_expires_at=float(row["authorization_expires_at"]),
                grant_sequence=int(row["grant_sequence"]),
                created_at=float(row["created_at"]),
                grant_digest=str(row["grant_digest"]),
            )
            self._validate_execution_start_grant(grant)
            return grant
        except (KeyError, TypeError, ValueError):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            ) from None

    def _validate_execution_grant_binding(
        self,
        connection: sqlite3.Connection,
        transaction: LifecycleTransaction,
        grant: LifecycleExecutionStartGrant,
    ) -> None:
        self._validate_execution_start_grant(grant)
        if (
            grant.transaction_id != transaction.transaction_id
            or grant.transaction_authority_digest != transaction.authority_digest
        ):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            )
        stage = self._helper_stage_receipt(
            connection, transaction.transaction_id
        )
        authorization = self._authorization_receipt(
            connection,
            transaction_id=transaction.transaction_id,
            authorization_digest=grant.authorization_digest,
        )
        manifest = self._execution_manifest_store.read(
            transaction.transaction_id
        )
        _validate_execution_manifest_binding(
            manifest, self._execution_binding_plan(transaction.plan)
        )
        receipt = manifest.receipt
        requirement_digest = sha256(
            manifest.runner.code_requirement.encode("utf-8")
        ).hexdigest()
        if (
            stage is None
            or authorization is None
            or stage.execution_manifest_digest != grant.execution_manifest_digest
            or stage.recovery_clone_identity_digest
            != grant.recovery_clone_identity_digest
            or receipt.manifest_digest != grant.execution_manifest_digest
            or receipt.recovery_clone_identity_digest
            != grant.recovery_clone_identity_digest
            or manifest.runner.identifier != grant.runner_identifier
            or manifest.runner.build_digest != grant.runner_build_digest
            or manifest.runner.identity_digest != grant.runner_identity_digest
            or manifest.runner.team_identifier != grant.runner_team_identifier
            or requirement_digest != grant.runner_code_requirement_digest
            or authorization.helper_session_id
            != grant.authorization_session_id
            or authorization.expires_at != grant.authorization_expires_at
        ):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            )

    @staticmethod
    def _runner_lease_from_row(row: sqlite3.Row) -> LifecycleRunnerLeaseEpoch:
        try:
            lease = LifecycleRunnerLeaseEpoch(
                lease_id=_canonical_uuid(str(row["lease_id"])),
                transaction_id=_canonical_uuid(str(row["transaction_id"])),
                grant_id=_canonical_uuid(str(row["grant_id"])),
                runner_session_id=_canonical_uuid(str(row["runner_session_id"])),
                process_fence_id=_canonical_uuid(
                    str(row["process_fence_id"])
                ),
                boot_id=_validate_prefixed_digest(str(row["boot_id"])),
                registration_nonce=_canonical_uuid(
                    str(row["registration_nonce"])
                ),
                lease_epoch=_bounded_positive_integer(
                    int(row["lease_epoch"]), maximum=1_000_000
                ),
                prior_lease_id=(
                    _canonical_uuid(str(row["prior_lease_id"]))
                    if row["prior_lease_id"] is not None
                    else None
                ),
                issued_at=_bounded_positive_integer(
                    int(row["issued_at"]), maximum=int(_MAX_TIMESTAMP)
                ),
                expires_at=_bounded_positive_integer(
                    int(row["expires_at"]), maximum=int(_MAX_TIMESTAMP)
                ),
                issued_monotonic=_validate_monotonic(
                    row["issued_monotonic"]
                ),
                expires_monotonic=_validate_monotonic(
                    row["expires_monotonic"]
                ),
            )
            if (
                lease.expires_at <= lease.issued_at
                or lease.expires_monotonic <= lease.issued_monotonic
            ):
                raise ValueError
            _validate_digest(str(row["registration_digest"]))
            _bounded_positive_integer(
                int(row["registration_sequence"]), maximum=1_000_000
            )
            return lease
        except (NativeLifecycleError, KeyError, TypeError, ValueError):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            ) from None

    @staticmethod
    def _validate_effect_receipt_shape(
        *,
        status: LifecycleEffectReceiptStatus,
        observation: LifecycleEffectObservation,
        before_digest: str | None,
        after_digest: str | None,
        fixed_error_code: str | None,
    ) -> None:
        valid = False
        if status is LifecycleEffectReceiptStatus.OBSERVED:
            valid = before_digest is None and after_digest is not None
        elif status is LifecycleEffectReceiptStatus.APPLY_STARTED:
            valid = (
                before_digest is not None
                and after_digest is None
                and observation is LifecycleEffectObservation.NEEDS_ACTION
                and fixed_error_code is None
            )
        elif status is LifecycleEffectReceiptStatus.APPLIED:
            valid = before_digest is not None and after_digest is not None
        elif status is LifecycleEffectReceiptStatus.FINALIZED:
            valid = (
                before_digest is not None
                and after_digest is not None
                and observation
                is LifecycleEffectObservation.EFFECT_SATISFIED
                and fixed_error_code is None
            )
        elif status is LifecycleEffectReceiptStatus.REFUSED:
            valid = (
                before_digest is not None
                and after_digest is None
                and fixed_error_code is not None
                and observation
                is not LifecycleEffectObservation.EFFECT_SATISFIED
            )
        if observation is LifecycleEffectObservation.RETRYABLE_NOT_READY:
            valid = valid and fixed_error_code == "observation_not_ready"
        if observation is LifecycleEffectObservation.CONFLICT:
            valid = valid and fixed_error_code == "observation_conflict"
        if observation is LifecycleEffectObservation.UNAVAILABLE:
            valid = valid and fixed_error_code == "observation_unavailable"
        if not valid:
            raise NativeLifecycleConflictError(
                "native_lifecycle_effect_receipt_invalid"
            )

    @staticmethod
    def _effect_receipt_binding_dict(
        receipt: LifecycleEffectReceipt,
    ) -> dict[str, object]:
        return {
            "schema_version": 2,
            "receipt_id": receipt.receipt_id,
            "transaction_id": receipt.transaction_id,
            "grant_id": receipt.grant_id,
            "lease_id": receipt.lease_id,
            "lease_epoch": receipt.lease_epoch,
            "sequence": receipt.sequence,
            "request_nonce": receipt.request_nonce,
            "effect_id": receipt.effect_id,
            "effect_kind": receipt.effect_kind.value,
            "target_digest": receipt.target_digest,
            "attempt": receipt.attempt,
            "status": receipt.status.value,
            "observation": receipt.observation.value,
            "before_observation_digest": receipt.before_observation_digest,
            "after_observation_digest": receipt.after_observation_digest,
            "prior_receipt_digest": receipt.prior_receipt_digest,
            "fixed_error_code": receipt.fixed_error_code,
            "recorded_at": receipt.recorded_at,
        }

    @staticmethod
    def _validate_effect_receipt_transition(
        prior: LifecycleEffectReceipt | None,
        current: LifecycleEffectReceipt,
    ) -> None:
        if prior is None:
            valid = (
                current.status is LifecycleEffectReceiptStatus.OBSERVED
                and current.prior_receipt_digest is None
            )
        elif (
            prior.effect_kind is not current.effect_kind
            or prior.effect_id != current.effect_id
            or prior.target_digest != current.target_digest
            or current.prior_receipt_digest != prior.receipt_digest
            or current.attempt < prior.attempt
            or prior.status is LifecycleEffectReceiptStatus.FINALIZED
            or (
                prior.observation
                is LifecycleEffectObservation.EFFECT_SATISFIED
                and current.status is not LifecycleEffectReceiptStatus.FINALIZED
            )
        ):
            valid = False
        elif current.status is LifecycleEffectReceiptStatus.OBSERVED:
            valid = current.attempt >= prior.attempt
        elif current.status is LifecycleEffectReceiptStatus.APPLY_STARTED:
            valid = (
                prior.status is LifecycleEffectReceiptStatus.OBSERVED
                and prior.observation is LifecycleEffectObservation.NEEDS_ACTION
                and current.attempt == prior.attempt
                and current.before_observation_digest
                == prior.after_observation_digest
            )
        elif current.status is LifecycleEffectReceiptStatus.APPLIED:
            valid = (
                prior.status is LifecycleEffectReceiptStatus.APPLY_STARTED
                and current.attempt == prior.attempt
                and current.before_observation_digest
                == prior.before_observation_digest
            )
        elif current.status is LifecycleEffectReceiptStatus.FINALIZED:
            valid = (
                prior.observation is LifecycleEffectObservation.EFFECT_SATISFIED
            )
        else:
            valid = current.status is LifecycleEffectReceiptStatus.REFUSED
        if not valid:
            raise NativeLifecycleConflictError(
                "native_lifecycle_effect_receipt_out_of_order"
            )

    def _effect_receipt_from_row(
        self, row: sqlite3.Row
    ) -> LifecycleEffectReceipt:
        try:
            receipt = LifecycleEffectReceipt(
                receipt_id=_canonical_uuid(str(row["receipt_id"])),
                transaction_id=_canonical_uuid(str(row["transaction_id"])),
                grant_id=_canonical_uuid(str(row["grant_id"])),
                lease_id=_canonical_uuid(str(row["lease_id"])),
                lease_epoch=_bounded_positive_integer(
                    int(row["lease_epoch"]), maximum=1_000_000
                ),
                sequence=_bounded_positive_integer(
                    int(row["sequence"]), maximum=1_000_000
                ),
                request_nonce=_canonical_uuid(str(row["request_nonce"])),
                effect_id=_canonical_uuid(str(row["effect_id"])),
                effect_kind=LifecycleEffectKind(str(row["effect_kind"])),
                target_digest=_validate_digest(str(row["target_digest"])),
                attempt=_bounded_positive_integer(
                    int(row["attempt"]), maximum=1_024
                ),
                status=LifecycleEffectReceiptStatus(str(row["status"])),
                observation=LifecycleEffectObservation(str(row["observation"])),
                before_observation_digest=_optional_digest(
                    row["before_observation_digest"]
                ),
                after_observation_digest=_optional_digest(
                    row["after_observation_digest"]
                ),
                prior_receipt_digest=_optional_digest(
                    row["prior_receipt_digest"]
                ),
                fixed_error_code=(
                    str(row["fixed_error_code"])
                    if row["fixed_error_code"] is not None
                    else None
                ),
                recorded_at=_validate_timestamp(row["recorded_at"]),
                receipt_digest=_validate_digest(str(row["receipt_digest"])),
            )
            self._validate_effect_receipt_shape(
                status=receipt.status,
                observation=receipt.observation,
                before_digest=receipt.before_observation_digest,
                after_digest=receipt.after_observation_digest,
                fixed_error_code=receipt.fixed_error_code,
            )
            expected = sha256(
                _canonical_json(
                    self._effect_receipt_binding_dict(receipt)
                ).encode("utf-8")
            ).hexdigest()
            if not hmac.compare_digest(expected, receipt.receipt_digest):
                raise ValueError
            return receipt
        except (NativeLifecycleError, KeyError, TypeError, ValueError):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            ) from None

    def _read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return self._operate(operation, write=False)

    def _write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return self._operate(operation, write=True)

    def _operate(
        self, operation: Callable[[sqlite3.Connection], _T], *, write: bool
    ) -> _T:
        with self._lock:
            self._require_ready()
            self._prepare_path()
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                self._validate_metadata(connection)
                result = operation(connection)
                connection.execute("COMMIT")
                return result
            except NativeLifecycleError:
                _rollback(connection)
                raise
            except (OSError, sqlite3.Error, TypeError, ValueError):
                _rollback(connection)
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                ) from None
            finally:
                if connection is not None:
                    connection.close()
                self._secure_database_artifacts()

    def _require_ready(self) -> None:
        if self._closed or not self._initialized:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_unavailable"
            )

    def _timestamp(self) -> float:
        try:
            return _validate_timestamp(self._clock())
        except NativeLifecycleError:
            raise
        except Exception:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_clock_invalid"
            ) from None

    def _monotonic(self) -> float:
        try:
            value = float(self._monotonic_clock())
            if not math.isfinite(value) or value < 0:
                raise ValueError
            return value
        except Exception:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_clock_invalid"
            ) from None

    def _boot_id(self) -> str:
        try:
            return _boot_digest(self._boot_identity_provider())
        except Exception:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_clock_invalid"
            ) from None

    def _validate_runner_process_fence(
        self,
        fence: LifecycleRunnerProcessFence | None,
        *,
        transaction_id: str,
        expected_fence_id: str | None = None,
    ) -> tuple[str, float]:
        if (
            not isinstance(fence, LifecycleRunnerProcessFence)
            or fence._token is not self._runner_fence_token
            or fence._transaction_id != transaction_id
            or fence._owner_pid != os.getpid()
            or fence._descriptor < 0
            or (expected_fence_id is not None and fence.fence_id != expected_fence_id)
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_process_fence_unavailable"
            )
        boot_id = self._boot_id()
        monotonic = self._monotonic()
        try:
            descriptor_status = os.fstat(fence._descriptor)
            path_status = fence._path.lstat()
        except OSError:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_process_fence_unavailable"
            ) from None
        if (
            fence.boot_id != boot_id
            or not stat.S_ISREG(descriptor_status.st_mode)
            or not stat.S_ISREG(path_status.st_mode)
            or descriptor_status.st_dev != path_status.st_dev
            or descriptor_status.st_ino != path_status.st_ino
            or descriptor_status.st_uid != os.geteuid()
            or stat.S_IMODE(descriptor_status.st_mode) != 0o600
            or descriptor_status.st_nlink != 1
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_process_fence_unavailable"
            )
        return boot_id, monotonic

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        phase_sql = ",".join(f"'{phase.value}'" for phase in LifecyclePhase)
        kind_sql = ",".join(f"'{kind.value}'" for kind in LifecycleKind)
        error_sql = ",".join(f"'{code}'" for code in sorted(MANUAL_RECOVERY_CODES))
        direction_sql = ",".join(
            f"'{direction.value}'" for direction in LifecycleExecutionDirection
        )
        effect_kind_sql = ",".join(
            f"'{kind.value}'" for kind in LifecycleEffectKind
        )
        receipt_status_sql = ",".join(
            f"'{status.value}'" for status in LifecycleEffectReceiptStatus
        )
        effect_observation_sql = ",".join(
            f"'{state.value}'" for state in LifecycleEffectObservation
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_lifecycle_metadata_v2 (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                store_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS native_lifecycle_transactions_v2 (
                transaction_id TEXT PRIMARY KEY,
                contract_version INTEGER NOT NULL CHECK (contract_version = 2),
                lifecycle_kind TEXT NOT NULL CHECK (lifecycle_kind IN ({kind_sql})),
                authority_json TEXT NOT NULL,
                authority_digest TEXT NOT NULL,
                current_phase TEXT NOT NULL CHECK (current_phase IN ({phase_sql})),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error_code TEXT CHECK (
                    error_code IS NULL OR error_code IN ({error_sql})
                )
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS native_lifecycle_events_v2 (
                transaction_id TEXT NOT NULL REFERENCES
                    native_lifecycle_transactions_v2(transaction_id)
                    ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence >= 1 AND sequence <= 32),
                phase TEXT NOT NULL CHECK (phase IN ({phase_sql})),
                recorded_at REAL NOT NULL,
                error_code TEXT CHECK (
                    error_code IS NULL OR error_code IN ({error_sql})
                ),
                PRIMARY KEY (transaction_id, sequence),
                UNIQUE (transaction_id, phase)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_lifecycle_control_v2 (
                transaction_id TEXT PRIMARY KEY REFERENCES
                    native_lifecycle_transactions_v2(transaction_id)
                    ON DELETE CASCADE,
                rollback_requested INTEGER NOT NULL DEFAULT 0 CHECK (
                    rollback_requested IN (0, 1)
                ),
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO native_lifecycle_control_v2 (
                transaction_id, rollback_requested, updated_at
            )
            SELECT transaction_id, 0, updated_at
              FROM native_lifecycle_transactions_v2
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_lifecycle_execution_claims_v2 (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                transaction_id TEXT NOT NULL UNIQUE REFERENCES
                    native_lifecycle_transactions_v2(transaction_id)
                    ON DELETE CASCADE,
                authority_digest TEXT NOT NULL,
                helper_session_id TEXT NOT NULL,
                claim_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_lifecycle_helper_stage_receipts_v2 (
                transaction_id TEXT PRIMARY KEY REFERENCES
                    native_lifecycle_transactions_v2(transaction_id)
                    ON DELETE CASCADE,
                authority_digest TEXT NOT NULL,
                execution_manifest_digest TEXT NOT NULL,
                recovery_clone_identity_digest TEXT NOT NULL,
                helper_build_digest TEXT NOT NULL,
                recorded_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_lifecycle_authorization_receipts_v2 (
                transaction_id TEXT NOT NULL REFERENCES
                    native_lifecycle_transactions_v2(transaction_id)
                    ON DELETE CASCADE,
                authority_digest TEXT NOT NULL,
                execution_manifest_digest TEXT NOT NULL,
                helper_build_digest TEXT NOT NULL,
                authorization_digest TEXT NOT NULL,
                helper_session_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                recorded_at REAL NOT NULL,
                PRIMARY KEY (transaction_id, authorization_digest),
                UNIQUE (transaction_id, helper_session_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_lifecycle_authorization_challenges_v2 (
                nonce TEXT PRIMARY KEY,
                transaction_id TEXT NOT NULL REFERENCES
                    native_lifecycle_transactions_v2(transaction_id)
                    ON DELETE CASCADE,
                session_id TEXT NOT NULL UNIQUE,
                challenge_json TEXT NOT NULL,
                challenge_digest TEXT NOT NULL,
                issued_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL CHECK (expires_at > issued_at),
                state TEXT NOT NULL CHECK (
                    state IN ('pending', 'cancelled', 'consumed')
                ),
                authorization_digest TEXT,
                authenticated_at REAL,
                completed_at REAL,
                UNIQUE (transaction_id, nonce),
                CHECK (
                    (state = 'pending' AND authorization_digest IS NULL
                        AND authenticated_at IS NULL AND completed_at IS NULL)
                    OR
                    (state = 'cancelled' AND authorization_digest IS NULL
                        AND authenticated_at IS NULL AND completed_at IS NOT NULL)
                    OR
                    (state = 'consumed' AND authorization_digest IS NOT NULL
                        AND authenticated_at IS NOT NULL AND completed_at IS NOT NULL)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                native_lifecycle_one_pending_authorization_v2
                ON native_lifecycle_authorization_challenges_v2(transaction_id)
                WHERE state = 'pending'
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS native_lifecycle_execution_start_grants_v2 (
                grant_id TEXT PRIMARY KEY,
                transaction_id TEXT NOT NULL REFERENCES
                    native_lifecycle_transactions_v2(transaction_id)
                    ON DELETE CASCADE,
                direction TEXT NOT NULL CHECK (direction IN ({direction_sql})),
                authority_digest TEXT NOT NULL,
                execution_manifest_digest TEXT NOT NULL,
                recovery_clone_identity_digest TEXT NOT NULL,
                runner_identifier TEXT NOT NULL,
                runner_build_digest TEXT NOT NULL,
                runner_identity_digest TEXT NOT NULL,
                runner_team_identifier TEXT NOT NULL,
                runner_code_requirement_digest TEXT NOT NULL,
                authorization_digest TEXT NOT NULL,
                authorization_session_id TEXT NOT NULL,
                authorization_expires_at REAL NOT NULL,
                grant_sequence INTEGER NOT NULL CHECK (
                    grant_sequence >= 1 AND grant_sequence <= 32
                ),
                created_at REAL NOT NULL,
                grant_digest TEXT NOT NULL,
                UNIQUE (transaction_id, authorization_digest),
                UNIQUE (transaction_id, grant_sequence),
                UNIQUE (transaction_id, grant_digest)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_lifecycle_runner_lease_epochs_v2 (
                lease_id TEXT PRIMARY KEY,
                transaction_id TEXT NOT NULL REFERENCES
                    native_lifecycle_transactions_v2(transaction_id)
                    ON DELETE CASCADE,
                grant_id TEXT NOT NULL REFERENCES
                    native_lifecycle_execution_start_grants_v2(grant_id)
                    ON DELETE CASCADE,
                runner_session_id TEXT NOT NULL,
                process_fence_id TEXT NOT NULL,
                boot_id TEXT NOT NULL,
                registration_nonce TEXT NOT NULL UNIQUE,
                registration_digest TEXT NOT NULL,
                registration_sequence INTEGER NOT NULL CHECK (
                    registration_sequence >= 1
                    AND registration_sequence <= 1000000
                ),
                lease_epoch INTEGER NOT NULL CHECK (
                    lease_epoch >= 1 AND lease_epoch <= 1000000
                ),
                prior_lease_id TEXT,
                issued_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL CHECK (expires_at > issued_at),
                issued_monotonic REAL NOT NULL CHECK (issued_monotonic >= 0),
                expires_monotonic REAL NOT NULL CHECK (
                    expires_monotonic > issued_monotonic
                ),
                UNIQUE (transaction_id, lease_epoch),
                UNIQUE (grant_id, runner_session_id, registration_sequence)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS native_lifecycle_effect_receipts_v2 (
                receipt_id TEXT PRIMARY KEY,
                transaction_id TEXT NOT NULL REFERENCES
                    native_lifecycle_transactions_v2(transaction_id)
                    ON DELETE CASCADE,
                grant_id TEXT NOT NULL REFERENCES
                    native_lifecycle_execution_start_grants_v2(grant_id)
                    ON DELETE CASCADE,
                lease_id TEXT NOT NULL REFERENCES
                    native_lifecycle_runner_lease_epochs_v2(lease_id)
                    ON DELETE CASCADE,
                lease_epoch INTEGER NOT NULL CHECK (lease_epoch >= 1),
                sequence INTEGER NOT NULL CHECK (
                    sequence >= 1 AND sequence <= 1000000
                ),
                request_nonce TEXT NOT NULL UNIQUE,
                effect_id TEXT NOT NULL,
                effect_kind TEXT NOT NULL CHECK (
                    effect_kind IN ({effect_kind_sql})
                ),
                target_digest TEXT NOT NULL,
                attempt INTEGER NOT NULL CHECK (attempt >= 1 AND attempt <= 1024),
                status TEXT NOT NULL CHECK (status IN ({receipt_status_sql})),
                observation TEXT NOT NULL CHECK (
                    observation IN ({effect_observation_sql})
                ),
                before_observation_digest TEXT,
                after_observation_digest TEXT,
                prior_receipt_digest TEXT,
                fixed_error_code TEXT,
                recorded_at REAL NOT NULL,
                receipt_digest TEXT NOT NULL,
                UNIQUE (grant_id, sequence),
                UNIQUE (transaction_id, receipt_digest)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS native_lifecycle_effect_target_v2
                ON native_lifecycle_effect_receipts_v2(
                    transaction_id, effect_id, target_digest, sequence
                )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                native_lifecycle_effect_sequence_v2
                ON native_lifecycle_effect_receipts_v2(
                    transaction_id, sequence
                )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_lifecycle_legacy_v1_recovery_v2 (
                transaction_id TEXT PRIMARY KEY,
                lifecycle_kind TEXT NOT NULL CHECK (lifecycle_kind IN ('migration', 'uninstall')),
                authority_digest TEXT NOT NULL,
                authority_json TEXT NOT NULL,
                original_phase TEXT NOT NULL,
                source_events_digest TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                terminal INTEGER NOT NULL CHECK (terminal IN (0, 1)),
                recovery_error_code TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS native_lifecycle_phase_v2
                ON native_lifecycle_transactions_v2(
                    current_phase, updated_at, transaction_id
                )
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS native_lifecycle_authority_immutable_v2
            BEFORE UPDATE OF transaction_id, lifecycle_kind, authority_json,
                authority_digest, created_at
            ON native_lifecycle_transactions_v2
            BEGIN
                SELECT RAISE(ABORT, 'native_lifecycle_authority_immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS native_lifecycle_event_immutable_v2
            BEFORE UPDATE ON native_lifecycle_events_v2
            BEGIN
                SELECT RAISE(ABORT, 'native_lifecycle_event_immutable');
            END
            """
        )
        for table in (
            "native_lifecycle_helper_stage_receipts_v2",
            "native_lifecycle_authorization_receipts_v2",
            "native_lifecycle_execution_start_grants_v2",
            "native_lifecycle_runner_lease_epochs_v2",
            "native_lifecycle_effect_receipts_v2",
            "native_lifecycle_legacy_v1_recovery_v2",
        ):
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_immutable
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'native_lifecycle_receipt_immutable');
                END
                """
            )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS
                native_lifecycle_authorization_challenge_identity_immutable_v2
            BEFORE UPDATE OF nonce, transaction_id, session_id,
                challenge_json, challenge_digest, issued_at, expires_at
            ON native_lifecycle_authorization_challenges_v2
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'native_lifecycle_authorization_challenge_immutable'
                );
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS
                native_lifecycle_authorization_challenge_monotonic_v2
            BEFORE UPDATE OF state, authorization_digest,
                authenticated_at, completed_at
            ON native_lifecycle_authorization_challenges_v2
            WHEN OLD.state != 'pending'
              OR NEW.state NOT IN ('cancelled', 'consumed')
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'native_lifecycle_authorization_challenge_not_monotonic'
                );
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS native_lifecycle_rollback_monotonic_v2
            BEFORE UPDATE OF transaction_id, rollback_requested
            ON native_lifecycle_control_v2
            WHEN NEW.transaction_id != OLD.transaction_id
              OR NEW.rollback_requested < OLD.rollback_requested
            BEGIN
                SELECT RAISE(ABORT, 'native_lifecycle_rollback_not_monotonic');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS native_lifecycle_recovery_protected_v2
            BEFORE DELETE ON native_lifecycle_transactions_v2
            WHEN OLD.current_phase NOT IN ('committed', 'restored', 'completed')
            BEGIN
                SELECT RAISE(ABORT, 'native_lifecycle_recovery_protected');
            END
            """
        )
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM native_lifecycle_metadata_v2"
        ).fetchone()
        if row is None:
            raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
        if int(row["total"]) == 0:
            transactions = connection.execute(
                "SELECT COUNT(*) AS total FROM native_lifecycle_transactions_v2"
            ).fetchone()
            if transactions is None or int(transactions["total"]) != 0:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_identity_mismatch"
                )
            connection.execute(
                """
                INSERT INTO native_lifecycle_metadata_v2 (
                    singleton, store_id, schema_version
                ) VALUES (1, ?, ?)
                """,
                (_STORE_ID, _STORE_SCHEMA_VERSION),
            )
        self._quarantine_legacy_v1(connection)

    def _quarantine_legacy_v1(self, connection: sqlite3.Connection) -> None:
        """Preserve v1 byte authority and quarantine incomplete work.

        Version-1 ordering is never replayed under version 2.  The original
        tables remain untouched.  A path-free immutable v2 receipt records
        their exact authority and event history; incomplete rows are exposed
        only as authenticated manual recovery.
        """

        names = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required = {
            "native_lifecycle_metadata_v1",
            "native_lifecycle_transactions_v1",
            "native_lifecycle_events_v1",
        }
        if not (required & names):
            return
        if not required <= names:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_legacy_v1_journal_corrupt"
            )
        metadata = connection.execute(
            """
            SELECT singleton, store_id, schema_version
              FROM native_lifecycle_metadata_v1
            """
        ).fetchall()
        if (
            len(metadata) != 1
            or int(metadata[0]["singleton"]) != 1
            or str(metadata[0]["store_id"]) != _LEGACY_STORE_ID
            or int(metadata[0]["schema_version"])
            != _LEGACY_STORE_SCHEMA_VERSION
        ):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_legacy_v1_journal_corrupt"
            )
        rows = connection.execute(
            """
            SELECT transaction_id, lifecycle_kind, authority_json,
                   authority_digest, current_phase, created_at, updated_at,
                   error_code
              FROM native_lifecycle_transactions_v1
             ORDER BY created_at, transaction_id
            """
        ).fetchall()
        for row in rows:
            try:
                transaction_id = _canonical_uuid(str(row["transaction_id"]))
                kind = LifecycleKind(str(row["lifecycle_kind"]))
                authority_json = str(row["authority_json"])
                authority_digest = _validate_digest(
                    str(row["authority_digest"])
                )
                document = json.loads(authority_json)
                if _canonical_json(document) != authority_json:
                    raise ValueError
                if sha256(authority_json.encode("utf-8")).hexdigest() != authority_digest:
                    raise ValueError
                plan = _plan_from_legacy_v1_dict(document)
                if plan.transaction_id != transaction_id or plan.kind is not kind:
                    raise ValueError
                original_phase = LifecyclePhase(str(row["current_phase"]))
                created_at = _validate_timestamp(row["created_at"])
                updated_at = _validate_timestamp(row["updated_at"])
                raw_events = connection.execute(
                    """
                    SELECT sequence, phase, recorded_at, error_code
                      FROM native_lifecycle_events_v1
                     WHERE transaction_id = ? ORDER BY sequence
                    """,
                    (transaction_id,),
                ).fetchall()
                _validate_legacy_v1_events(
                    kind=kind,
                    current_phase=original_phase,
                    created_at=created_at,
                    updated_at=updated_at,
                    rows=raw_events,
                )
                source_events = [
                    {
                        "sequence": int(event["sequence"]),
                        "phase": str(event["phase"]),
                        "recorded_at": float(event["recorded_at"]),
                        "error_code": (
                            str(event["error_code"])
                            if event["error_code"] is not None
                            else None
                        ),
                    }
                    for event in raw_events
                ]
                source_events_digest = sha256(
                    _canonical_json(source_events).encode("utf-8")
                ).hexdigest()
                terminal = original_phase in _TERMINAL_PHASES
                recovery_error = (
                    None
                    if terminal
                    else "legacy_v1_manual_recovery_required"
                )
                v2 = connection.execute(
                    """
                    SELECT authority_digest
                      FROM native_lifecycle_transactions_v2
                     WHERE transaction_id = ?
                    """,
                    (transaction_id,),
                ).fetchone()
                if v2 is not None:
                    raise ValueError
                connection.execute(
                    """
                    INSERT OR IGNORE INTO native_lifecycle_legacy_v1_recovery_v2 (
                        transaction_id, lifecycle_kind, authority_digest,
                        authority_json, original_phase, source_events_digest,
                        created_at, updated_at, terminal, recovery_error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transaction_id,
                        kind.value,
                        authority_digest,
                        authority_json,
                        original_phase.value,
                        source_events_digest,
                        created_at,
                        updated_at,
                        int(terminal),
                        recovery_error,
                    ),
                )
                receipt = connection.execute(
                    """
                    SELECT lifecycle_kind, authority_digest, authority_json,
                           original_phase, source_events_digest, created_at,
                           updated_at, terminal, recovery_error_code
                      FROM native_lifecycle_legacy_v1_recovery_v2
                     WHERE transaction_id = ?
                    """,
                    (transaction_id,),
                ).fetchone()
                if receipt is None or tuple(receipt) != (
                    kind.value,
                    authority_digest,
                    authority_json,
                    original_phase.value,
                    source_events_digest,
                    created_at,
                    updated_at,
                    int(terminal),
                    recovery_error,
                ):
                    raise ValueError
            except (
                NativeLifecycleError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_legacy_v1_journal_corrupt"
                ) from None

    def _validate_metadata(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT singleton, store_id, schema_version FROM native_lifecycle_metadata_v2"
        ).fetchall()
        if (
            len(rows) != 1
            or int(rows[0]["singleton"]) != 1
            or str(rows[0]["store_id"]) != _STORE_ID
            or int(rows[0]["schema_version"]) != _STORE_SCHEMA_VERSION
        ):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_identity_mismatch"
            )

    def _validate_all(self, connection: sqlite3.Connection) -> None:
        check = connection.execute("PRAGMA integrity_check(1)").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
        rows = connection.execute(
            "SELECT * FROM native_lifecycle_transactions_v2 ORDER BY created_at, transaction_id"
        ).fetchall()
        for row in rows:
            record = self._record_from_row(connection, row)
            self._validate_helper_receipts(connection, record)
        self._validate_authorization_challenges(connection)
        self._validate_execution_foundation(connection)
        controls = connection.execute(
            "SELECT COUNT(*) AS total FROM native_lifecycle_control_v2"
        ).fetchone()
        if controls is None or int(controls["total"]) != len(rows):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            )
        self._execution_claim(connection)

    def _validate_execution_foundation(
        self, connection: sqlite3.Connection
    ) -> None:
        grants = connection.execute(
            """
            SELECT * FROM native_lifecycle_execution_start_grants_v2
             ORDER BY transaction_id, grant_sequence
            """
        ).fetchall()
        grant_by_id: dict[str, LifecycleExecutionStartGrant] = {}
        grant_sequences: dict[str, int] = {}
        for row in grants:
            grant = self._execution_start_grant_from_row(row)
            expected_sequence = grant_sequences.get(grant.transaction_id, 0) + 1
            if grant.grant_sequence != expected_sequence:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            grant_sequences[grant.transaction_id] = expected_sequence
            transaction = self._require(connection, grant.transaction_id)
            self._validate_execution_grant_binding(
                connection, transaction, grant
            )
            grant_by_id[grant.grant_id] = grant

        lease_rows = connection.execute(
            """
            SELECT * FROM native_lifecycle_runner_lease_epochs_v2
             ORDER BY transaction_id, lease_epoch
            """
        ).fetchall()
        lease_by_id: dict[str, LifecycleRunnerLeaseEpoch] = {}
        lease_sequences: dict[str, int] = {}
        prior_by_transaction: dict[str, str | None] = {}
        session_sequences: dict[tuple[str, str], int] = {}
        for row in lease_rows:
            lease = self._runner_lease_from_row(row)
            grant = grant_by_id.get(lease.grant_id)
            if grant is None or grant.transaction_id != lease.transaction_id:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            expected_epoch = lease_sequences.get(lease.transaction_id, 0) + 1
            if (
                lease.lease_epoch != expected_epoch
                or lease.prior_lease_id
                != prior_by_transaction.get(lease.transaction_id)
            ):
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            lease_sequences[lease.transaction_id] = expected_epoch
            prior_by_transaction[lease.transaction_id] = lease.lease_id
            session_key = (lease.grant_id, lease.runner_session_id)
            expected_registration_sequence = session_sequences.get(session_key, 0) + 1
            registration_sequence = int(row["registration_sequence"])
            if registration_sequence != expected_registration_sequence:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            session_sequences[session_key] = registration_sequence
            registration = LifecycleRunnerRegistrationV2(
                protocol_version=2,
                message_type=LifecycleExecutionMessageType.REGISTER,
                transaction_id=lease.transaction_id,
                grant_id=lease.grant_id,
                grant_digest=_prefixed_digest(grant.grant_digest),
                runner_session_id=lease.runner_session_id,
                sequence=registration_sequence,
                nonce=lease.registration_nonce,
                runner_identifier=grant.runner_identifier,
                runner_build_digest=_prefixed_digest(grant.runner_build_digest),
                runner_identity_digest=_prefixed_digest(
                    grant.runner_identity_digest
                ),
                team_identifier=grant.runner_team_identifier,
                code_requirement_digest=_prefixed_digest(
                    grant.runner_code_requirement_digest
                ),
                requested_lease_seconds=lease.expires_at - lease.issued_at,
            )
            validate_lifecycle_execution_message(registration)
            expected_registration_digest = sha256(
                _canonical_json(registration.to_wire_dict()).encode("utf-8")
            ).hexdigest()
            if not hmac.compare_digest(
                expected_registration_digest,
                _validate_digest(str(row["registration_digest"])),
            ):
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            lease_by_id[lease.lease_id] = lease

        active_transaction: str | None = None
        active_boot: str | None = None
        active_until = -1.0
        for lease in sorted(
            lease_by_id.values(),
            key=lambda item: (
                item.boot_id,
                item.issued_monotonic,
                item.transaction_id,
                item.lease_epoch,
            ),
        ):
            if (
                lease.boot_id == active_boot
                and lease.issued_monotonic < active_until
                and active_transaction != lease.transaction_id
            ):
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            if (
                lease.boot_id != active_boot
                or active_transaction != lease.transaction_id
                or lease.issued_monotonic >= active_until
            ):
                active_boot = lease.boot_id
                active_transaction = lease.transaction_id
                active_until = lease.expires_monotonic
            else:
                active_until = max(active_until, lease.expires_monotonic)

        receipt_rows = connection.execute(
            """
            SELECT * FROM native_lifecycle_effect_receipts_v2
             ORDER BY transaction_id, sequence
            """
        ).fetchall()
        transaction_receipt_sequences: dict[str, int] = {}
        target_receipts: dict[tuple[str, str, str], LifecycleEffectReceipt] = {}
        direction_receipts: dict[
            tuple[str, LifecycleExecutionDirection],
            list[LifecycleEffectReceipt],
        ] = {}
        direction_graphs: dict[
            tuple[str, LifecycleExecutionDirection],
            tuple[AuthorizedLifecycleEffect, ...],
        ] = {}
        for row in receipt_rows:
            receipt = self._effect_receipt_from_row(row)
            grant = grant_by_id.get(receipt.grant_id)
            lease = lease_by_id.get(receipt.lease_id)
            expected_sequence = (
                transaction_receipt_sequences.get(receipt.transaction_id, 0) + 1
            )
            if (
                grant is None
                or lease is None
                or receipt.transaction_id != grant.transaction_id
                or lease.transaction_id != receipt.transaction_id
                or lease.grant_id != receipt.grant_id
                or lease.lease_epoch != receipt.lease_epoch
                or receipt.sequence != expected_sequence
                or not lease.issued_at <= receipt.recorded_at <= lease.expires_at
            ):
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            transaction = self._require(connection, receipt.transaction_id)
            manifest = self._execution_manifest_store.read(
                receipt.transaction_id
            )
            _validate_execution_manifest_binding(
                manifest, self._execution_binding_plan(transaction.plan)
            )
            direction_key = (receipt.transaction_id, grant.direction)
            direction_graph = direction_graphs.get(direction_key)
            if direction_graph is None:
                direction_graph = _authorized_lifecycle_effects(
                    transaction, manifest, grant.direction
                )
                direction_graphs[direction_key] = direction_graph
            if (
                receipt.effect_id,
                receipt.effect_kind,
                receipt.target_digest,
            ) not in {
                (item.effect_id, item.effect_kind, item.target_digest)
                for item in direction_graph
            }:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            transaction_receipt_sequences[receipt.transaction_id] = (
                expected_sequence
            )
            target_key = (
                receipt.transaction_id,
                receipt.effect_id,
                receipt.target_digest,
            )
            prior = target_receipts.get(target_key)
            if (
                receipt.prior_receipt_digest
                != (prior.receipt_digest if prior is not None else None)
                or (prior is not None and prior.effect_kind is not receipt.effect_kind)
                or (prior is not None and receipt.attempt < prior.attempt)
            ):
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            try:
                self._validate_effect_receipt_transition(prior, receipt)
                observed_direction_receipts = direction_receipts.setdefault(
                    direction_key, []
                )
                observed_direction_receipts.append(receipt)
            except NativeLifecycleError:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                ) from None
            target_receipts[target_key] = receipt

        for direction_key, receipts in direction_receipts.items():
            try:
                _validate_ordered_effect_graph_receipts(
                    direction_graphs[direction_key], tuple(receipts)
                )
            except NativeLifecycleError:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                ) from None

    def _validate_authorization_challenges(
        self, connection: sqlite3.Connection
    ) -> None:
        rows = connection.execute(
            """
            SELECT *
              FROM native_lifecycle_authorization_challenges_v2
             ORDER BY transaction_id, issued_at, nonce
            """
        ).fetchall()
        per_transaction: dict[str, int] = {}
        pending: set[str] = set()
        for row in rows:
            challenge = self._authorization_challenge_from_row(row)
            transaction = self._require(connection, challenge.transaction_id)
            count = per_transaction.get(challenge.transaction_id, 0) + 1
            per_transaction[challenge.transaction_id] = count
            if count > _MAX_HELPER_AUTHORIZATION_CHALLENGES_PER_TRANSACTION:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            stage = self._helper_stage_receipt(
                connection, challenge.transaction_id
            )
            if stage is None:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            manifest = self._execution_manifest_store.read(
                challenge.transaction_id
            )
            _validate_execution_manifest_binding(
                manifest, self._execution_binding_plan(transaction.plan)
            )
            self._validate_challenge_basis(challenge, (stage, manifest))
            state = str(row["state"])
            authorization_digest = row["authorization_digest"]
            authenticated_at = row["authenticated_at"]
            completed_at = row["completed_at"]
            if state == "pending":
                if challenge.transaction_id in pending or any(
                    item is not None
                    for item in (
                        authorization_digest,
                        authenticated_at,
                        completed_at,
                    )
                ):
                    raise NativeLifecycleIntegrityError(
                        "native_lifecycle_journal_corrupt"
                    )
                pending.add(challenge.transaction_id)
            elif state == "cancelled":
                if (
                    authorization_digest is not None
                    or authenticated_at is not None
                    or completed_at is None
                ):
                    raise NativeLifecycleIntegrityError(
                        "native_lifecycle_journal_corrupt"
                    )
                _validate_timestamp(completed_at)
            elif state == "consumed":
                try:
                    digest = _validate_digest(str(authorization_digest))
                    authenticated = _validate_timestamp(authenticated_at)
                    completed = _validate_timestamp(completed_at)
                except NativeLifecycleError:
                    raise NativeLifecycleIntegrityError(
                        "native_lifecycle_journal_corrupt"
                    ) from None
                if (
                    digest != _unprefixed_digest(challenge.authorization_digest)
                    or not challenge.issued_at
                    <= authenticated
                    < challenge.expires_at
                    or completed < authenticated
                ):
                    raise NativeLifecycleIntegrityError(
                        "native_lifecycle_journal_corrupt"
                    )
                receipt = self._authorization_receipt(
                    connection,
                    transaction_id=challenge.transaction_id,
                    authorization_digest=digest,
                )
                if (
                    receipt is None
                    or receipt.helper_session_id != challenge.session_id
                    or receipt.expires_at != challenge.expires_at
                ):
                    raise NativeLifecycleIntegrityError(
                        "native_lifecycle_journal_corrupt"
                    )
            else:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )

    def _validate_helper_receipts(
        self,
        connection: sqlite3.Connection,
        transaction: LifecycleTransaction,
    ) -> None:
        events = self._events(connection, transaction.transaction_id)
        phases = {event[1] for event in events}
        stage = self._helper_stage_receipt(
            connection, transaction.transaction_id
        )
        has_stage_phase = LifecyclePhase.HELPER_STAGED in phases
        if (stage is None) != (not has_stage_phase):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            )
        rows = connection.execute(
            """
            SELECT authorization_digest
              FROM native_lifecycle_authorization_receipts_v2
             WHERE transaction_id = ?
             ORDER BY recorded_at, authorization_digest
            """,
            (transaction.transaction_id,),
        ).fetchall()
        authorizations = tuple(
            self._authorization_receipt(
                connection,
                transaction_id=transaction.transaction_id,
                authorization_digest=str(row["authorization_digest"]),
            )
            for row in rows
        )
        has_authorized_phase = LifecyclePhase.AUTHORIZED in phases
        if has_authorized_phase != bool(authorizations):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            )
        if stage is None:
            return
        manifest = self._execution_manifest_store.read(
            transaction.transaction_id
        )
        receipt = manifest.receipt
        if (
            stage.transaction_authority_digest != transaction.authority_digest
            or stage.execution_manifest_digest != receipt.manifest_digest
            or stage.recovery_clone_identity_digest
            != receipt.recovery_clone_identity_digest
            or stage.helper_build_digest != manifest.helper.build_digest
        ):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_execution_manifest_conflict"
            )
        _validate_execution_manifest_binding(
            manifest, self._execution_binding_plan(transaction.plan)
        )
        for authorization in authorizations:
            if authorization is None:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
            challenge_row = connection.execute(
                """
                SELECT nonce
                  FROM native_lifecycle_authorization_challenges_v2
                 WHERE transaction_id = ?
                   AND session_id = ?
                   AND authorization_digest = ?
                   AND state = 'consumed'
                """,
                (
                    transaction.transaction_id,
                    authorization.helper_session_id,
                    authorization.authorization_digest,
                ),
            ).fetchone()
            if challenge_row is None or (
                authorization.transaction_authority_digest
                != transaction.authority_digest
                or authorization.execution_manifest_digest
                != stage.execution_manifest_digest
                or authorization.helper_build_digest
                != stage.helper_build_digest
            ):
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )

    def _execution_binding_plan(self, plan: LifecyclePlan) -> LifecyclePlan:
        if not isinstance(plan, UninstallPlan):
            return plan
        private = self._manifest_store.require_receipt(plan.manifest_receipt)
        return replace(
            plan,
            manifest_items=private.items,
            private_manifest=private,
        )

    def _helper_authorization_basis(
        self,
        connection: sqlite3.Connection,
        current: LifecycleTransaction,
        *,
        allow_authorized: bool = False,
    ) -> tuple[HelperStageReceipt, PrivateExecutionManifest]:
        events = {event[1] for event in self._events(connection, current.transaction_id)}
        if current.contract_version != _LIFECYCLE_CONTRACT_VERSION:
            raise NativeLifecycleConflictError(
                "native_lifecycle_helper_authority_mismatch"
            )
        if LifecyclePhase.HELPER_STAGED not in events:
            if current.phase in _TERMINAL_PHASES:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_transaction_terminal"
                )
            raise NativeLifecycleConflictError(
                "native_lifecycle_phase_out_of_order"
            )
        if current.phase in _TERMINAL_PHASES:
            raise NativeLifecycleConflictError(
                "native_lifecycle_transaction_terminal"
            )
        if (
            not allow_authorized
            and current.phase is not LifecyclePhase.HELPER_STAGED
            and LifecyclePhase.AUTHORIZED not in events
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_phase_out_of_order"
            )
        stage = self._helper_stage_receipt(
            connection, current.transaction_id
        )
        if stage is None or stage.transaction_authority_digest != current.authority_digest:
            raise NativeLifecycleConflictError(
                "native_lifecycle_helper_stage_conflict"
            )
        manifest = self._execution_manifest_store.read(current.transaction_id)
        _validate_execution_manifest_binding(
            manifest, self._execution_binding_plan(current.plan)
        )
        receipt = manifest.receipt
        if (
            receipt.manifest_digest != stage.execution_manifest_digest
            or receipt.recovery_clone_identity_digest
            != stage.recovery_clone_identity_digest
            or manifest.helper.build_digest != stage.helper_build_digest
            or manifest.helper.identifier
            != PRODUCT_IDENTITY.lifecycle_helper_identifier
            or manifest.helper.team_identifier
            != manifest.application.team_identifier
            or _TEAM_IDENTIFIER_RE.fullmatch(
                manifest.helper.team_identifier
            )
            is None
            or manifest.helper.team_identifier
            in {"ADHOC00000", "UNSIGNED00", "NOTSET0000"}
        ):
            # An ad-hoc/unsealed development identity can be staged for
            # verification tests, but it can never mint real authority.
            raise NativeLifecycleConflictError(
                "native_lifecycle_helper_authority_unavailable"
            )
        return stage, manifest

    def _validate_challenge_basis(
        self,
        challenge: HelperAuthorizationChallenge,
        basis: tuple[HelperStageReceipt, PrivateExecutionManifest],
    ) -> None:
        stage, manifest = basis
        proof_authority = self._helper_authorization_proof_authority
        if proof_authority is None:
            raise NativeLifecycleConflictError(
                "native_lifecycle_helper_authority_unavailable"
            )
        proof_authority.validate()
        expected = {
            "transaction_id": stage.transaction_id,
            "transaction_authority_digest": _prefixed_digest(
                stage.transaction_authority_digest
            ),
            "execution_manifest_digest": _prefixed_digest(
                stage.execution_manifest_digest
            ),
            "recovery_clone_identity_digest": _prefixed_digest(
                stage.recovery_clone_identity_digest
            ),
            "expected_helper_identifier": (
                PRODUCT_IDENTITY.lifecycle_helper_identifier
            ),
            "expected_helper_build_digest": _prefixed_digest(
                manifest.helper.build_digest
            ),
            "expected_team_identifier": manifest.helper.team_identifier,
            "expected_code_requirement_digest": _prefixed_digest(
                sha256(
                    manifest.helper.code_requirement.encode("utf-8")
                ).hexdigest()
            ),
            "expected_app_build_digest": _prefixed_digest(
                manifest.application.build_digest
            ),
            "expected_authorization_proof_algorithm": (
                proof_authority.algorithm
            ),
            "expected_authorization_key_id": proof_authority.key_id,
        }
        if any(getattr(challenge, key) != value for key, value in expected.items()):
            raise NativeLifecycleConflictError(
                "native_lifecycle_helper_authority_mismatch"
            )

    @staticmethod
    def _authorization_challenge_from_row(
        row: sqlite3.Row,
    ) -> HelperAuthorizationChallenge:
        try:
            document = json.loads(str(row["challenge_json"]))
            challenge = helper_authorization_challenge_from_mapping(document)
            if (
                challenge.nonce != str(row["nonce"])
                or challenge.transaction_id != str(row["transaction_id"])
                or challenge.session_id != str(row["session_id"])
                or challenge.issued_at != int(row["issued_at"])
                or challenge.expires_at != int(row["expires_at"])
                or _canonical_json(challenge.to_public_dict())
                != str(row["challenge_json"])
                or _unprefixed_digest(challenge.authorization_digest)
                != _validate_digest(str(row["challenge_digest"]))
            ):
                raise ValueError
        except (
            NativeLifecycleError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            ) from None
        return challenge

    def _record_authorized_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        current: LifecycleTransaction,
        execution_manifest_digest: str,
        helper_build_digest: str,
        authorization_digest: str,
        helper_session_id: str,
        expires_at: float,
        now: float,
    ) -> LifecycleJournalMutation:
        if current.phase in _TERMINAL_PHASES:
            raise NativeLifecycleConflictError(
                "native_lifecycle_transaction_terminal"
            )
        stage = self._helper_stage_receipt(
            connection, current.transaction_id
        )
        if (
            stage is None
            or stage.transaction_authority_digest != current.authority_digest
            or stage.execution_manifest_digest != execution_manifest_digest
            or stage.helper_build_digest != helper_build_digest
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_helper_stage_conflict"
            )
        existing = self._authorization_receipt(
            connection,
            transaction_id=current.transaction_id,
            authorization_digest=authorization_digest,
        )
        expected = HelperAuthorizationReceipt(
            transaction_id=current.transaction_id,
            transaction_authority_digest=current.authority_digest,
            execution_manifest_digest=execution_manifest_digest,
            helper_build_digest=helper_build_digest,
            authorization_digest=authorization_digest,
            helper_session_id=helper_session_id,
            expires_at=expires_at,
            recorded_at=now,
        )
        if existing is not None:
            if replace(existing, recorded_at=now) != expected:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_helper_authority_conflict"
                )
            return LifecycleJournalMutation(current, replayed=True)
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
                current.transaction_id,
                current.authority_digest,
                execution_manifest_digest,
                helper_build_digest,
                authorization_digest,
                helper_session_id,
                expires_at,
                now,
            ),
        )
        if current.phase is LifecyclePhase.HELPER_STAGED:
            self._append_phase_in_connection(
                connection, current, LifecyclePhase.AUTHORIZED, now
            )
        elif LifecyclePhase.AUTHORIZED not in {
            event[1]
            for event in self._events(connection, current.transaction_id)
        }:
            raise NativeLifecycleConflictError(
                "native_lifecycle_phase_out_of_order"
            )
        return LifecycleJournalMutation(
            self._require(connection, current.transaction_id), replayed=False
        )

    def _append_phase_in_connection(
        self,
        connection: sqlite3.Connection,
        current: LifecycleTransaction,
        phase: LifecyclePhase,
        observed_at: float,
    ) -> None:
        events = self._events(connection, current.transaction_id)
        now = max(_validate_timestamp(observed_at), current.updated_at)
        connection.execute(
            """
            INSERT INTO native_lifecycle_events_v2 (
                transaction_id, sequence, phase, recorded_at, error_code
            ) VALUES (?, ?, ?, ?, NULL)
            """,
            (current.transaction_id, len(events) + 1, phase.value, now),
        )
        connection.execute(
            """
            UPDATE native_lifecycle_transactions_v2
               SET current_phase = ?, updated_at = ?, error_code = NULL
             WHERE transaction_id = ?
            """,
            (phase.value, now, current.transaction_id),
        )

    def _helper_stage_receipt(
        self, connection: sqlite3.Connection, transaction_id: str
    ) -> HelperStageReceipt | None:
        row = connection.execute(
            """
            SELECT transaction_id, authority_digest,
                   execution_manifest_digest,
                   recovery_clone_identity_digest, helper_build_digest,
                   recorded_at
              FROM native_lifecycle_helper_stage_receipts_v2
             WHERE transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            receipt = HelperStageReceipt(
                transaction_id=_canonical_uuid(str(row["transaction_id"])),
                transaction_authority_digest=_validate_digest(
                    str(row["authority_digest"])
                ),
                execution_manifest_digest=_validate_digest(
                    str(row["execution_manifest_digest"])
                ),
                recovery_clone_identity_digest=_validate_digest(
                    str(row["recovery_clone_identity_digest"])
                ),
                helper_build_digest=_validate_digest(
                    str(row["helper_build_digest"])
                ),
                recorded_at=_validate_timestamp(row["recorded_at"]),
            )
        except (NativeLifecycleError, TypeError, ValueError):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            ) from None
        return receipt

    def _authorization_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_id: str,
        authorization_digest: str,
    ) -> HelperAuthorizationReceipt | None:
        row = connection.execute(
            """
            SELECT transaction_id, authority_digest,
                   execution_manifest_digest, helper_build_digest,
                   authorization_digest, helper_session_id, expires_at,
                   recorded_at
              FROM native_lifecycle_authorization_receipts_v2
             WHERE transaction_id = ? AND authorization_digest = ?
            """,
            (transaction_id, authorization_digest),
        ).fetchone()
        if row is None:
            return None
        try:
            receipt = HelperAuthorizationReceipt(
                transaction_id=_canonical_uuid(str(row["transaction_id"])),
                transaction_authority_digest=_validate_digest(
                    str(row["authority_digest"])
                ),
                execution_manifest_digest=_validate_digest(
                    str(row["execution_manifest_digest"])
                ),
                helper_build_digest=_validate_digest(
                    str(row["helper_build_digest"])
                ),
                authorization_digest=_validate_digest(
                    str(row["authorization_digest"])
                ),
                helper_session_id=_canonical_uuid(
                    str(row["helper_session_id"])
                ),
                expires_at=_validate_timestamp(row["expires_at"]),
                recorded_at=_validate_timestamp(row["recorded_at"]),
            )
            if receipt.expires_at <= receipt.recorded_at:
                raise ValueError
        except (NativeLifecycleError, TypeError, ValueError):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            ) from None
        return receipt

    def _execution_claim(
        self, connection: sqlite3.Connection
    ) -> tuple[str, str, str, str, float, float] | None:
        rows = connection.execute(
            """
            SELECT transaction_id, authority_digest, helper_session_id,
                   claim_id, expires_at, created_at
              FROM native_lifecycle_execution_claims_v2
            """
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            )
        try:
            row = rows[0]
            transaction_id = _canonical_uuid(str(row["transaction_id"]))
            authority_digest = _validate_digest(str(row["authority_digest"]))
            helper_session_id = _canonical_uuid(str(row["helper_session_id"]))
            claim_id = _canonical_uuid(str(row["claim_id"]))
            expires_at = _validate_timestamp(row["expires_at"])
            created_at = _validate_timestamp(row["created_at"])
            if expires_at <= created_at:
                raise ValueError
            transaction = self._require(connection, transaction_id)
            if transaction.authority_digest != authority_digest:
                raise ValueError
        except (NativeLifecycleError, TypeError, ValueError):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            ) from None
        return (
            transaction_id,
            authority_digest,
            helper_session_id,
            claim_id,
            expires_at,
            created_at,
        )

    def _mark_manual_in_connection(
        self,
        connection: sqlite3.Connection,
        current: LifecycleTransaction,
        error_code: str,
        observed_at: float,
    ) -> None:
        if error_code not in MANUAL_RECOVERY_CODES:
            raise NativeLifecycleConflictError(
                "native_lifecycle_error_code_invalid"
            )
        if current.terminal:
            raise NativeLifecycleConflictError(
                "native_lifecycle_transaction_terminal"
            )
        events = self._events(connection, current.transaction_id)
        now = max(_validate_timestamp(observed_at), current.updated_at)
        connection.execute(
            """
            INSERT INTO native_lifecycle_events_v2 (
                transaction_id, sequence, phase, recorded_at, error_code
            ) VALUES (?, ?, 'manual_recovery', ?, ?)
            """,
            (current.transaction_id, len(events) + 1, now, error_code),
        )
        connection.execute(
            """
            UPDATE native_lifecycle_transactions_v2
               SET current_phase = 'manual_recovery', updated_at = ?,
                   error_code = ?
             WHERE transaction_id = ?
            """,
            (now, error_code, current.transaction_id),
        )

    def _record_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> LifecycleTransaction:
        try:
            transaction_id = _canonical_uuid(str(row["transaction_id"]))
            contract_version = int(row["contract_version"])
            if contract_version != _LIFECYCLE_CONTRACT_VERSION:
                raise ValueError
            kind = LifecycleKind(str(row["lifecycle_kind"]))
            authority_json = str(row["authority_json"])
            authority_digest = str(row["authority_digest"])
            _validate_digest(authority_digest)
            raw = json.loads(authority_json)
            plan = _plan_from_journal_dict(raw)
            canonical = _canonical_json(plan._journal_dict())
            if canonical != authority_json:
                raise ValueError
            if sha256(canonical.encode("utf-8")).hexdigest() != authority_digest:
                raise ValueError
            if plan.transaction_id != transaction_id or plan.kind is not kind:
                raise ValueError
            phase = LifecyclePhase(str(row["current_phase"]))
            created_at = _validate_timestamp(row["created_at"])
            updated_at = _validate_timestamp(row["updated_at"])
            error_code = str(row["error_code"]) if row["error_code"] is not None else None
            if error_code is not None and error_code not in MANUAL_RECOVERY_CODES:
                raise ValueError
            control = connection.execute(
                """
                SELECT rollback_requested, updated_at
                  FROM native_lifecycle_control_v2
                 WHERE transaction_id = ?
                """,
                (transaction_id,),
            ).fetchone()
            if control is None or int(control["rollback_requested"]) not in (0, 1):
                raise ValueError
            rollback_requested = bool(int(control["rollback_requested"]))
            control_updated_at = _validate_timestamp(control["updated_at"])
            if control_updated_at < created_at:
                raise ValueError
            if rollback_requested and (
                kind is not LifecycleKind.MIGRATION
                or phase
                not in {
                    *_MIGRATION_ROLLBACK_FROM,
                    LifecyclePhase.RESTORED,
                    LifecyclePhase.MANUAL_RECOVERY,
                }
            ):
                raise ValueError
        except (NativeLifecycleError, json.JSONDecodeError, TypeError, ValueError):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_corrupt"
            ) from None
        record = LifecycleTransaction(
            transaction_id=transaction_id,
            kind=kind,
            plan=plan,
            authority_digest=authority_digest,
            phase=phase,
            created_at=created_at,
            updated_at=updated_at,
            error_code=error_code,
            rollback_requested=rollback_requested,
            contract_version=contract_version,
            recovery_from_phase=(
                self._events(connection, transaction_id)[-2][1]
                if phase is LifecyclePhase.MANUAL_RECOVERY
                and len(self._events(connection, transaction_id)) >= 2
                else None
            ),
        )
        self._validate_events(connection, record)
        if isinstance(plan, UninstallPlan):
            self._validate_manifest_binding(plan)
        return record

    def _validate_manifest_binding(self, plan: UninstallPlan) -> None:
        private_manifest = self._manifest_store.require_receipt(
            plan.manifest_receipt
        )
        journal_items = tuple(
            item._journal_dict() for item in plan.manifest_items
        )
        private_items = tuple(
            item._journal_dict() for item in private_manifest.items
        )
        if journal_items != private_items:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_retention_manifest_conflict"
            )

    def _validate_events(
        self, connection: sqlite3.Connection, record: LifecycleTransaction
    ) -> None:
        events = self._events(connection, record.transaction_id)
        if not events:
            raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
        initial = (
            LifecyclePhase.PREPARED
            if record.kind is LifecycleKind.UNINSTALL
            else LifecyclePhase.DISCOVERED
        )
        if events[0][1] is not initial:
            raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
        previous = initial
        previous_at = -1.0
        for expected_sequence, (sequence, phase, recorded_at, error_code) in enumerate(
            events, start=1
        ):
            if sequence != expected_sequence or recorded_at < previous_at:
                raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
            if expected_sequence > 1:
                if phase is LifecyclePhase.MANUAL_RECOVERY:
                    if error_code is None or expected_sequence != len(events):
                        raise NativeLifecycleIntegrityError(
                            "native_lifecycle_journal_corrupt"
                        )
                elif phase not in _allowed_next_phases(
                    record.kind, previous, record.contract_version
                ):
                    raise NativeLifecycleIntegrityError(
                        "native_lifecycle_journal_corrupt"
                    )
            elif error_code is not None:
                raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
            if phase is not LifecyclePhase.MANUAL_RECOVERY and error_code is not None:
                raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
            previous = phase
            previous_at = recorded_at
        if record.phase is not events[-1][1]:
            raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
        if record.created_at != events[0][2] or record.updated_at != events[-1][2]:
            raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
        if record.phase is LifecyclePhase.MANUAL_RECOVERY:
            if record.error_code != events[-1][3]:
                raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
        elif record.error_code is not None:
            raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")

    def _events(
        self, connection: sqlite3.Connection, transaction_id: str
    ) -> tuple[tuple[int, LifecyclePhase, float, str | None], ...]:
        rows = connection.execute(
            """
            SELECT sequence, phase, recorded_at, error_code
              FROM native_lifecycle_events_v2
             WHERE transaction_id = ? ORDER BY sequence
            """,
            (transaction_id,),
        ).fetchall()
        result: list[tuple[int, LifecyclePhase, float, str | None]] = []
        for row in rows:
            try:
                sequence = int(row["sequence"])
                phase = LifecyclePhase(str(row["phase"]))
                recorded_at = _validate_timestamp(row["recorded_at"])
                error_code = str(row["error_code"]) if row["error_code"] is not None else None
                if error_code is not None and error_code not in MANUAL_RECOVERY_CODES:
                    raise ValueError
            except (TypeError, ValueError):
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                ) from None
            result.append((sequence, phase, recorded_at, error_code))
        return tuple(result)

    def _select(
        self, connection: sqlite3.Connection, transaction_id: str
    ) -> LifecycleTransaction | None:
        row = connection.execute(
            "SELECT * FROM native_lifecycle_transactions_v2 WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        return self._record_from_row(connection, row) if row is not None else None

    def _select_legacy(
        self, connection: sqlite3.Connection, transaction_id: str
    ) -> LifecycleTransaction | None:
        row = connection.execute(
            """
            SELECT transaction_id, lifecycle_kind, authority_digest,
                   authority_json, original_phase, created_at, updated_at,
                   terminal, recovery_error_code
              FROM native_lifecycle_legacy_v1_recovery_v2
             WHERE transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()
        return self._legacy_record_from_row(row) if row is not None else None

    def _select_legacy_many(
        self, connection: sqlite3.Connection
    ) -> tuple[LifecycleTransaction, ...]:
        rows = connection.execute(
            """
            SELECT transaction_id, lifecycle_kind, authority_digest,
                   authority_json, original_phase, created_at, updated_at,
                   terminal, recovery_error_code
              FROM native_lifecycle_legacy_v1_recovery_v2
             ORDER BY created_at, transaction_id
            """
        ).fetchall()
        return tuple(self._legacy_record_from_row(row) for row in rows)

    @staticmethod
    def _legacy_record_from_row(row: sqlite3.Row) -> LifecycleTransaction:
        try:
            transaction_id = _canonical_uuid(str(row["transaction_id"]))
            kind = LifecycleKind(str(row["lifecycle_kind"]))
            authority_digest = _validate_digest(str(row["authority_digest"]))
            authority_json = str(row["authority_json"])
            document = json.loads(authority_json)
            if (
                _canonical_json(document) != authority_json
                or sha256(authority_json.encode("utf-8")).hexdigest()
                != authority_digest
            ):
                raise ValueError
            plan = _plan_from_legacy_v1_dict(document)
            original_phase = LifecyclePhase(str(row["original_phase"]))
            terminal = bool(int(row["terminal"]))
            error_code = (
                str(row["recovery_error_code"])
                if row["recovery_error_code"] is not None
                else None
            )
            if terminal != (original_phase in _TERMINAL_PHASES):
                raise ValueError
            if terminal:
                phase = original_phase
                if error_code is not None:
                    raise ValueError
            else:
                phase = LifecyclePhase.MANUAL_RECOVERY
                if error_code != "legacy_v1_manual_recovery_required":
                    raise ValueError
            created_at = _validate_timestamp(row["created_at"])
            updated_at = _validate_timestamp(row["updated_at"])
        except (
            NativeLifecycleError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_legacy_v1_journal_corrupt"
            ) from None
        return LifecycleTransaction(
            transaction_id=transaction_id,
            kind=kind,
            plan=plan,
            authority_digest=authority_digest,
            phase=phase,
            created_at=created_at,
            updated_at=updated_at,
            error_code=error_code,
            rollback_requested=False,
            contract_version=1,
            recovery_from_phase=(None if terminal else original_phase),
        )

    def _require(
        self, connection: sqlite3.Connection, transaction_id: str
    ) -> LifecycleTransaction:
        record = self._select(connection, transaction_id)
        if record is None:
            raise NativeLifecycleNotFoundError(
                "native_lifecycle_transaction_not_found"
            )
        return record

    def _select_many(
        self, connection: sqlite3.Connection, where: str
    ) -> tuple[LifecycleTransaction, ...]:
        rows = connection.execute(
            f"SELECT * FROM native_lifecycle_transactions_v2 {where} "
            "ORDER BY created_at, transaction_id"
        ).fetchall()
        return tuple(self._record_from_row(connection, row) for row in rows)

    def _prune(
        self,
        connection: sqlite3.Connection,
        *,
        reserve: int,
        fail_if_protected: bool,
    ) -> None:
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM native_lifecycle_transactions_v2)
              + (SELECT COUNT(*) FROM native_lifecycle_legacy_v1_recovery_v2)
                AS total
            """
        ).fetchone()
        if row is None:
            raise NativeLifecycleIntegrityError("native_lifecycle_journal_corrupt")
        excess = int(row["total"]) + reserve - self._maximum_transactions
        if excess > 0:
            rows = connection.execute(
                """
                SELECT transaction_id FROM native_lifecycle_transactions_v2
                 WHERE current_phase IN ('committed', 'restored', 'completed')
                 ORDER BY updated_at, transaction_id LIMIT ?
                """,
                (excess,),
            ).fetchall()
            for candidate in rows:
                connection.execute(
                    "DELETE FROM native_lifecycle_transactions_v2 WHERE transaction_id = ?",
                    (str(candidate["transaction_id"]),),
                )
            excess -= len(rows)
        if excess > 0 and fail_if_protected:
            raise NativeLifecycleCapacityError(
                "native_lifecycle_journal_capacity_exhausted"
            )

    def _prepare_path(self) -> None:
        try:
            state_directory = self._directory.parent
            state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _secure_directory(state_directory)
            self._directory.mkdir(exist_ok=True, mode=0o700)
            _secure_directory(self._directory)
            try:
                status = self._path.lstat()
            except FileNotFoundError:
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self._path, flags, 0o600)
                try:
                    os.fchmod(descriptor, 0o600)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                _fsync_directory(self._directory)
                status = self._path.lstat()
            _secure_regular_file(self._path, status)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self._path}{suffix}")
                try:
                    sidecar_status = sidecar.lstat()
                except FileNotFoundError:
                    continue
                _secure_regular_file(sidecar, sidecar_status)
        except NativeLifecycleError:
            raise
        except OSError:
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_insecure_path"
            ) from None

    def _secure_database_artifacts(self) -> None:
        try:
            for path in (
                self._path,
                Path(f"{self._path}-wal"),
                Path(f"{self._path}-shm"),
            ):
                try:
                    status = path.lstat()
                except FileNotFoundError:
                    continue
                _secure_regular_file(path, status)
        except (OSError, NativeLifecycleError):
            return

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._path),
            timeout=10,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA trusted_schema=OFF")
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            connection.close()
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_journal_durability_unavailable"
            )
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA wal_autocheckpoint=1")
        return connection


def decide_lifecycle_recovery(
    transaction: LifecycleTransaction,
    *,
    next_effect: RecoveryObservation | str,
    prior_effects: PriorEffectsState | str,
    rollback_requested: bool = False,
) -> LifecycleRecoveryDecision:
    """Choose replay vs. journal catch-up from fresh exact observations.

    ``next_effect`` describes only the next authorized milestone.  The caller
    must separately prove that every journaled effect remains intact.  This
    prevents a crash recovery from widening targets or inferring success from
    a missing path/listener.
    """

    if not isinstance(transaction, LifecycleTransaction):
        raise TypeError("transaction must be a LifecycleTransaction")
    next_effect = _enum(
        RecoveryObservation,
        next_effect,
        "native_lifecycle_recovery_observation_invalid",
    )
    prior_effects = _enum(
        PriorEffectsState,
        prior_effects,
        "native_lifecycle_recovery_observation_invalid",
    )
    if not isinstance(rollback_requested, bool):
        raise NativeLifecycleConflictError(
            "native_lifecycle_recovery_observation_invalid"
        )
    if transaction.phase is LifecyclePhase.MANUAL_RECOVERY:
        return _manual(transaction.error_code or "journal_conflict")
    if transaction.terminal:
        return LifecycleRecoveryDecision(LifecycleRecoveryAction.NO_ACTION)
    if prior_effects is PriorEffectsState.CONFLICT:
        return _manual("prior_effect_conflict")
    if prior_effects is PriorEffectsState.UNAVAILABLE:
        return _manual("prior_effect_unavailable")

    if rollback_requested:
        if (
            transaction.kind is not LifecycleKind.MIGRATION
            or transaction.phase not in _MIGRATION_ROLLBACK_FROM
        ):
            return _manual("rollback_not_available")
        phase = LifecyclePhase.RESTORED
    else:
        allowed = _allowed_next_phases(
            transaction.kind,
            transaction.phase,
            transaction.contract_version,
        )
        normal = tuple(
            phase for phase in allowed if phase is not LifecyclePhase.RESTORED
        )
        if len(normal) != 1:
            return _manual("journal_conflict")
        phase = normal[0]

    if next_effect is RecoveryObservation.CONFLICT:
        return _manual("recovery_observation_conflict")
    if next_effect is RecoveryObservation.UNAVAILABLE:
        return _manual("recovery_observation_unavailable")
    if next_effect is RecoveryObservation.RETRYABLE_NOT_READY:
        return LifecycleRecoveryDecision(
            LifecycleRecoveryAction.RETRY_WHEN_READY,
            phase=phase,
            reason_code="recovery_observation_not_ready",
        )
    if next_effect is RecoveryObservation.EFFECT_SATISFIED:
        return LifecycleRecoveryDecision(
            LifecycleRecoveryAction.RECORD_CONCLUSIVELY_SATISFIED,
            phase=phase,
        )
    if rollback_requested:
        return LifecycleRecoveryDecision(
            LifecycleRecoveryAction.RESTORE_EXACT_PREDECESSOR,
            phase=LifecyclePhase.RESTORED,
        )
    return LifecycleRecoveryDecision(
        LifecycleRecoveryAction.RESUME_IDENTICAL, phase=phase
    )


def decide_manual_recovery_action(
    transaction: LifecycleTransaction,
    *,
    requested_action: LifecycleRecoveryAction | str,
    exact_authority_matches: bool,
    next_effect: RecoveryObservation | str,
    any_effect_observed: bool,
) -> LifecycleRecoveryDecision:
    """Validate one closed, helper-authenticated manual recovery request.

    This pure policy function never mutates a journal.  A future signed helper
    must first prove the same transaction/manifest authorization receipts and
    then carry out the returned closed ceremony.  There is intentionally no
    caller-selected phase argument.
    """

    if (
        not isinstance(transaction, LifecycleTransaction)
        or transaction.phase is not LifecyclePhase.MANUAL_RECOVERY
        or not isinstance(exact_authority_matches, bool)
        or not isinstance(any_effect_observed, bool)
    ):
        return _manual("journal_conflict")
    action = _enum(
        LifecycleRecoveryAction,
        requested_action,
        "native_lifecycle_recovery_observation_invalid",
    )
    observation = _enum(
        RecoveryObservation,
        next_effect,
        "native_lifecycle_recovery_observation_invalid",
    )
    origin = transaction.recovery_from_phase
    if not exact_authority_matches or origin is None:
        return _manual("exact_identity_mismatch")
    if observation in {
        RecoveryObservation.CONFLICT,
        RecoveryObservation.UNAVAILABLE,
    }:
        return _manual(
            "recovery_observation_conflict"
            if observation is RecoveryObservation.CONFLICT
            else "recovery_observation_unavailable"
        )
    if action is LifecycleRecoveryAction.ABORT_BEFORE_ANY_EFFECT:
        initial = (
            LifecyclePhase.PREPARED
            if transaction.kind is LifecycleKind.UNINSTALL
            else LifecyclePhase.DISCOVERED
        )
        if origin is initial and not any_effect_observed:
            return LifecycleRecoveryDecision(action)
        return _manual("prior_effect_conflict")
    if action is LifecycleRecoveryAction.RESTORE_EXACT_PREDECESSOR:
        if (
            transaction.kind is LifecycleKind.MIGRATION
            and origin in _MIGRATION_ROLLBACK_FROM
            and any_effect_observed
        ):
            return LifecycleRecoveryDecision(
                action, phase=LifecyclePhase.RESTORED
            )
        return _manual("rollback_not_available")
    allowed = _allowed_next_phases(
        transaction.kind, origin, transaction.contract_version
    )
    normal = tuple(
        phase for phase in allowed if phase is not LifecyclePhase.RESTORED
    )
    if len(normal) != 1:
        return _manual("journal_conflict")
    target = normal[0]
    if action is LifecycleRecoveryAction.RESUME_IDENTICAL:
        return (
            LifecycleRecoveryDecision(action, phase=target)
            if observation is RecoveryObservation.NEEDS_ACTION
            else _manual("recovery_observation_conflict")
        )
    if action is LifecycleRecoveryAction.RECORD_CONCLUSIVELY_SATISFIED:
        return (
            LifecycleRecoveryDecision(action, phase=target)
            if observation is RecoveryObservation.EFFECT_SATISFIED
            else _manual("recovery_observation_conflict")
        )
    return _manual("journal_conflict")


def _manifest_item(
    weight: RetentionWeight, *, retention_mode: RetentionMode
) -> RetentionManifestItem:
    if not isinstance(weight, RetentionWeight):
        raise NativeLifecycleConflictError("native_lifecycle_weight_invalid")
    _validate_digest(weight.asset_fingerprint)
    _validate_digest(weight.payload_fingerprint)
    exact_path = _validate_exact_lexical_path(weight.exact_lexical_path)
    storage_id = _canonical_uuid(weight.storage_location_id)
    if (
        isinstance(weight.storage_binding_generation, bool)
        or not isinstance(weight.storage_binding_generation, int)
        or not 1 <= weight.storage_binding_generation <= 2_147_483_647
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_storage_generation_invalid"
        )
    volume_uuid = _optional_opaque_text(weight.volume_uuid)
    if weight.scope_id is not None:
        _validate_digest(weight.scope_id)
    ownership = _enum(
        WeightOwnership, weight.ownership, "native_lifecycle_weight_invalid"
    )
    proof_state = _enum(
        ExclusiveProofState,
        weight.exclusive_proof_state,
        "native_lifecycle_weight_invalid",
    )
    installation_id = (
        _canonical_uuid(weight.installation_id)
        if weight.installation_id is not None
        else None
    )
    if weight.byte_count is not None and (
        isinstance(weight.byte_count, bool)
        or not isinstance(weight.byte_count, int)
        or not 0 <= weight.byte_count <= 2**63 - 1
    ):
        raise NativeLifecycleConflictError("native_lifecycle_weight_invalid")
    if proof_state is ExclusiveProofState.FRESH_EXACT:
        if (
            ownership is not WeightOwnership.EXCLUSIVE_MANAGED
            or installation_id is None
            or weight.exclusive_proof_digest is None
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_exclusive_proof_invalid"
            )
        _validate_digest(weight.exclusive_proof_digest)
    elif weight.exclusive_proof_digest is not None:
        raise NativeLifecycleConflictError(
            "native_lifecycle_exclusive_proof_invalid"
        )
    if ownership is WeightOwnership.EXCLUSIVE_MANAGED and installation_id is None:
        raise NativeLifecycleConflictError(
            "native_lifecycle_exclusive_proof_invalid"
        )

    eligible = (
        retention_mode is RetentionMode.FULL_EXCLUSIVE_MANAGED
        and ownership is WeightOwnership.EXCLUSIVE_MANAGED
        and proof_state is ExclusiveProofState.FRESH_EXACT
    )
    disposition = (
        WeightDisposition.MOVE_TO_TRASH if eligible else WeightDisposition.RETAIN
    )
    if eligible:
        reason = "fresh_exclusive_managed_proof"
    elif retention_mode is not RetentionMode.FULL_EXCLUSIVE_MANAGED:
        reason = "retention_mode_keeps_weights"
    else:
        reason = f"retain_{ownership.value}"
    return RetentionManifestItem(
        asset_fingerprint=weight.asset_fingerprint,
        payload_fingerprint=weight.payload_fingerprint,
        exact_lexical_path=exact_path,
        storage_location_id=storage_id,
        storage_binding_generation=weight.storage_binding_generation,
        volume_uuid=volume_uuid,
        scope_id=weight.scope_id,
        ownership=ownership,
        installation_id=installation_id,
        exclusive_proof_state=proof_state,
        exclusive_proof_digest=weight.exclusive_proof_digest,
        byte_count=weight.byte_count,
        disposition=disposition,
        reason_code=reason,
    )


def _validate_outbox(
    *, retention_mode: RetentionMode, count: int, decision: OutboxDecision
) -> None:
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 0 <= count <= 2**63 - 1
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_outbox_count_invalid"
        )
    # New plans always preserve accounting state.  Continue parsing the
    # stricter destructive decisions emitted by earlier v2 builds so their
    # durable recovery journals cannot make an in-place upgrade fail to
    # start.  The current planner never emits these legacy combinations.
    if decision is OutboxDecision.PRESERVE_WITH_STATE:
        return
    if retention_mode is RetentionMode.APP_ONLY:
        raise NativeLifecycleConflictError(
            "native_lifecycle_outbox_decision_conflict"
        )
    if count == 0 and decision is OutboxDecision.EMPTY_CONFIRMED:
        return
    if count > 0 and decision in {
        OutboxDecision.RECOVERY_CAPSULE,
        OutboxDecision.EXPLICIT_ABANDONMENT,
    }:
        return
    raise NativeLifecycleConflictError(
        "native_lifecycle_outbox_decision_conflict"
    )


def _plan_from_journal_dict(value: object) -> LifecyclePlan:
    document = _strict_mapping(value)
    _require_keys(document, {"schema_version", "kind", "transaction_id", "product"}, minimum=True)
    if document.get("schema_version") != _LIFECYCLE_CONTRACT_VERSION:
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    if document.get("product") != _product_dict():
        raise NativeLifecycleConflictError("native_lifecycle_identity_invalid")
    try:
        kind = LifecycleKind(document["kind"])
    except (KeyError, TypeError, ValueError):
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid") from None
    if kind is LifecycleKind.UNINSTALL:
        return _uninstall_from_journal_dict(document)
    return _migration_from_journal_dict(document)


def _plan_from_legacy_v1_dict(value: object) -> LifecyclePlan:
    document = _strict_mapping(value)
    _require_keys(
        document,
        {"schema_version", "kind", "transaction_id", "product"},
        minimum=True,
    )
    legacy_product = {
        "application_name": PRODUCT_IDENTITY.application_name,
        "application_bundle_id": PRODUCT_IDENTITY.application_bundle_id,
        "launch_agent_label": PRODUCT_IDENTITY.launch_agent_label,
        "service_code_requirement_id": (
            PRODUCT_IDENTITY.service_code_requirement_id
        ),
    }
    if document.get("schema_version") != 1 or document.get("product") != legacy_product:
        raise NativeLifecycleConflictError(
            "native_lifecycle_legacy_v1_journal_corrupt"
        )
    upgraded = dict(document)
    upgraded["schema_version"] = _LIFECYCLE_CONTRACT_VERSION
    upgraded["product"] = _product_dict()
    try:
        kind = LifecycleKind(upgraded["kind"])
    except (KeyError, TypeError, ValueError):
        raise NativeLifecycleConflictError(
            "native_lifecycle_legacy_v1_journal_corrupt"
        ) from None
    return (
        _uninstall_from_journal_dict(upgraded)
        if kind is LifecycleKind.UNINSTALL
        else _migration_from_journal_dict(upgraded)
    )


def _uninstall_from_journal_dict(document: Mapping[str, object]) -> UninstallPlan:
    expected = {
        "schema_version",
        "kind",
        "transaction_id",
        "retention_mode",
        "product",
        "current_build_digest",
        "token_outbox_count",
        "outbox_decision",
        "outbox_evidence_digest",
        "hub_revocation_state",
        "components",
        "retention_manifest",
        "weights",
    }
    _require_keys(document, expected)
    transaction_id = _canonical_uuid(_string(document["transaction_id"]))
    mode = _enum(
        RetentionMode,
        document["retention_mode"],
        "native_lifecycle_plan_invalid",
    )
    current_build = _string(document["current_build_digest"])
    _validate_digest(current_build)
    count = document["token_outbox_count"]
    decision = _enum(
        OutboxDecision, document["outbox_decision"], "native_lifecycle_plan_invalid"
    )
    evidence_digest = _string(document["outbox_evidence_digest"])
    _validate_digest(evidence_digest)
    revocation = _enum(
        HubRevocationState,
        document["hub_revocation_state"],
        "native_lifecycle_plan_invalid",
    )
    if isinstance(count, bool) or not isinstance(count, int):
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    _validate_outbox(retention_mode=mode, count=count, decision=decision)

    components_raw = _list(document["components"])
    components: list[ComponentPlan] = []
    for raw in components_raw:
        item = _strict_mapping(raw)
        _require_keys(item, {"kind", "disposition", "authority"})
        component = ComponentPlan(
            kind=_enum(ComponentKind, item["kind"], "native_lifecycle_plan_invalid"),
            disposition=_enum(
                ComponentDisposition,
                item["disposition"],
                "native_lifecycle_plan_invalid",
            ),
            authority=_string(item["authority"]),
        )
        components.append(component)
    if tuple(component.kind for component in components) != tuple(ComponentKind):
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    _validate_component_contract(tuple(components), mode)

    weights_raw = _list(document["weights"])
    manifest_items = tuple(
        _journal_manifest_item(item, retention_mode=mode)
        for item in weights_raw
    )
    receipt_raw = _strict_mapping(document["retention_manifest"])
    _require_keys(
        receipt_raw,
        {"schema_version", "digest", "item_count", "retained_count", "trash_count"},
    )
    receipt = RetentionManifestReceipt(
        transaction_id=transaction_id,
        digest=_string(receipt_raw["digest"]),
        item_count=_integer(receipt_raw["item_count"]),
        retained_count=_integer(receipt_raw["retained_count"]),
        trash_count=_integer(receipt_raw["trash_count"]),
    )
    _validate_digest(receipt.digest)
    if receipt_raw["schema_version"] != _MANIFEST_SCHEMA_VERSION:
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    if (
        receipt.item_count != len(manifest_items)
        or receipt.retained_count
        != sum(item.disposition is WeightDisposition.RETAIN for item in manifest_items)
        or receipt.trash_count
        != sum(
            item.disposition is WeightDisposition.MOVE_TO_TRASH
            for item in manifest_items
        )
    ):
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    return UninstallPlan(
        transaction_id=transaction_id,
        retention_mode=mode,
        current_build_digest=current_build,
        token_outbox_count=count,
        outbox_decision=decision,
        outbox_evidence_digest=evidence_digest,
        hub_revocation_state=revocation,
        components=tuple(components),
        manifest_receipt=receipt,
        manifest_items=manifest_items,
        private_manifest=None,
    )


def _migration_from_journal_dict(document: Mapping[str, object]) -> MigrationPlan:
    expected = {
        "schema_version",
        "kind",
        "transaction_id",
        "product",
        "predecessor_build_digest",
        "candidate_build_digest",
        "legacy_sidecar",
        "retention_contract",
        "evidence",
    }
    _require_keys(document, expected)
    if document["retention_contract"] != {
        "private_state": "retain",
        "managed_runtimes": "retain",
        "security_scopes": "retain",
        "pairing_state": "retain",
        "weights": "retain",
    }:
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    evidence_raw = _strict_mapping(document["evidence"])
    fields = set(MigrationEvidence.__dataclass_fields__)
    _require_keys(evidence_raw, fields)
    evidence = MigrationEvidence(
        **{name: _string(evidence_raw[name]) for name in fields}
    )
    legacy_raw = _strict_mapping(document["legacy_sidecar"])
    _require_keys(legacy_raw, {"label", "state"})
    if legacy_raw["label"] != LEGACY_SIDECAR_LABEL:
        raise NativeLifecycleConflictError("native_lifecycle_identity_invalid")
    return build_migration_plan(
        transaction_id=_string(document["transaction_id"]),
        predecessor_build_digest=_string(document["predecessor_build_digest"]),
        candidate_build_digest=_string(document["candidate_build_digest"]),
        evidence=evidence,
        legacy_sidecar_state=_enum(
            LegacySidecarMigrationState,
            legacy_raw["state"],
            "native_lifecycle_plan_invalid",
        ),
    )


def _journal_manifest_item(
    value: object, *, retention_mode: RetentionMode
) -> RetentionManifestItem:
    item = _strict_mapping(value)
    expected = {
        "asset_fingerprint",
        "payload_fingerprint",
        "storage_location_id",
        "storage_binding_generation",
        "volume_uuid",
        "scope_id",
        "ownership",
        "installation_id",
        "exclusive_proof_state",
        "exclusive_proof_digest",
        "byte_count",
        "disposition",
        "reason_code",
    }
    _require_keys(item, expected)
    # Validate all path-free authority by using a synthetic safe path, then
    # assert that the journaled disposition matches the closed planner.
    weight = RetentionWeight(
        asset_fingerprint=_string(item["asset_fingerprint"]),
        payload_fingerprint=_string(item["payload_fingerprint"]),
        exact_lexical_path="/private/journal-path-redacted",
        storage_location_id=_string(item["storage_location_id"]),
        storage_binding_generation=_integer(item["storage_binding_generation"]),
        volume_uuid=_optional_string(item["volume_uuid"]),
        scope_id=_optional_string(item["scope_id"]),
        ownership=_enum(
            WeightOwnership, item["ownership"], "native_lifecycle_plan_invalid"
        ),
        installation_id=_optional_string(item["installation_id"]),
        exclusive_proof_state=_enum(
            ExclusiveProofState,
            item["exclusive_proof_state"],
            "native_lifecycle_plan_invalid",
        ),
        exclusive_proof_digest=_optional_string(item["exclusive_proof_digest"]),
        byte_count=(
            _integer(item["byte_count"]) if item["byte_count"] is not None else None
        ),
    )
    validated = _manifest_item(weight, retention_mode=retention_mode)
    disposition = _enum(
        WeightDisposition, item["disposition"], "native_lifecycle_plan_invalid"
    )
    reason = _string(item["reason_code"])
    overlap_downgrade = (
        validated.disposition is WeightDisposition.MOVE_TO_TRASH
        and disposition is WeightDisposition.RETAIN
        and reason == "retain_overlapping_authority"
    )
    if not overlap_downgrade and (
        disposition is not validated.disposition
        or reason != validated.reason_code
    ):
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    return RetentionManifestItem(
        asset_fingerprint=validated.asset_fingerprint,
        payload_fingerprint=validated.payload_fingerprint,
        exact_lexical_path="",
        storage_location_id=validated.storage_location_id,
        storage_binding_generation=validated.storage_binding_generation,
        volume_uuid=validated.volume_uuid,
        scope_id=validated.scope_id,
        ownership=validated.ownership,
        installation_id=validated.installation_id,
        exclusive_proof_state=validated.exclusive_proof_state,
        exclusive_proof_digest=validated.exclusive_proof_digest,
        byte_count=validated.byte_count,
        disposition=disposition,
        reason_code=reason,
    )


def _private_manifest_from_dict(value: object) -> PrivateRetentionManifest:
    document = _strict_mapping(value)
    _require_keys(
        document,
        {"schema_version", "transaction_id", "retention_mode", "product", "weights"},
    )
    if (
        document["schema_version"] != _MANIFEST_SCHEMA_VERSION
        or document["product"]
        != {
            "application_name": PRODUCT_IDENTITY.application_name,
            "application_bundle_id": PRODUCT_IDENTITY.application_bundle_id,
            "launch_agent_label": PRODUCT_IDENTITY.launch_agent_label,
        }
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_retention_manifest_conflict"
        )
    transaction_id = _canonical_uuid(_string(document["transaction_id"]))
    mode = _enum(
        RetentionMode,
        document["retention_mode"],
        "native_lifecycle_retention_manifest_conflict",
    )
    items_raw = _list(document["weights"])
    if len(items_raw) > _MAX_WEIGHT_ITEMS:
        raise NativeLifecycleConflictError("native_lifecycle_weight_limit_exceeded")
    items: list[RetentionManifestItem] = []
    for raw in items_raw:
        item = _strict_mapping(raw)
        expected = {
            "asset_fingerprint",
            "payload_fingerprint",
            "exact_lexical_path",
            "storage_location_id",
            "storage_binding_generation",
            "volume_uuid",
            "scope_id",
            "ownership",
            "installation_id",
            "exclusive_proof_state",
            "exclusive_proof_digest",
            "byte_count",
            "disposition",
            "reason_code",
        }
        _require_keys(item, expected)
        weight = RetentionWeight(
            asset_fingerprint=_string(item["asset_fingerprint"]),
            payload_fingerprint=_string(item["payload_fingerprint"]),
            exact_lexical_path=_string(item["exact_lexical_path"]),
            storage_location_id=_string(item["storage_location_id"]),
            storage_binding_generation=_integer(item["storage_binding_generation"]),
            volume_uuid=_optional_string(item["volume_uuid"]),
            scope_id=_optional_string(item["scope_id"]),
            ownership=_enum(
                WeightOwnership,
                item["ownership"],
                "native_lifecycle_retention_manifest_conflict",
            ),
            installation_id=_optional_string(item["installation_id"]),
            exclusive_proof_state=_enum(
                ExclusiveProofState,
                item["exclusive_proof_state"],
                "native_lifecycle_retention_manifest_conflict",
            ),
            exclusive_proof_digest=_optional_string(item["exclusive_proof_digest"]),
            byte_count=(
                _integer(item["byte_count"])
                if item["byte_count"] is not None
                else None
            ),
        )
        planned = _manifest_item(weight, retention_mode=mode)
        items.append(
            replace(
                planned,
                disposition=_enum(
                    WeightDisposition,
                    item["disposition"],
                    "native_lifecycle_retention_manifest_conflict",
                ),
                reason_code=_string(item["reason_code"]),
            )
        )
    if len({item.asset_fingerprint for item in items}) != len(items):
        raise NativeLifecycleConflictError("native_lifecycle_weight_duplicate")
    # Rebuild the complete plan to validate overlap downgrades and every
    # derived disposition rather than trusting path-bearing JSON fields.
    rebuilt = build_uninstall_plan(
        transaction_id=transaction_id,
        retention_mode=mode,
        current_build_digest="0" * 64,
        private_state_fingerprint="1" * 64,
        runtime_root_fingerprint="2" * 64,
        security_scope_store_fingerprint="3" * 64,
        pairing_state_fingerprint="4" * 64,
        token_outbox_count=0,
        outbox_decision=OutboxDecision.PRESERVE_WITH_STATE,
        outbox_evidence_digest="5" * 64,
        weights=tuple(
            RetentionWeight(
                asset_fingerprint=item.asset_fingerprint,
                payload_fingerprint=item.payload_fingerprint,
                exact_lexical_path=item.exact_lexical_path,
                storage_location_id=item.storage_location_id,
                storage_binding_generation=item.storage_binding_generation,
                volume_uuid=item.volume_uuid,
                scope_id=item.scope_id,
                ownership=item.ownership,
                installation_id=item.installation_id,
                exclusive_proof_state=item.exclusive_proof_state,
                exclusive_proof_digest=item.exclusive_proof_digest,
                byte_count=item.byte_count,
            )
            for item in items
        ),
    )
    expected_items = rebuilt.manifest_items
    if tuple(items) != expected_items:
        raise NativeLifecycleConflictError(
            "native_lifecycle_retention_manifest_conflict"
        )
    return PrivateRetentionManifest(transaction_id, mode, expected_items)


def _execution_manifest_from_dict(
    value: object, *, plan: LifecyclePlan | None = None
) -> PrivateExecutionManifest:
    document = _strict_mapping(value)
    _require_keys(
        document,
        {
            "schema_version",
            "transaction_id",
            "kind",
            "transaction_authority_digest",
            "application",
            "recovery_application",
            "helper",
            "runner",
            "recovery_clone",
            "exact_members",
            "exclusive_weights",
            "outbox",
            "pairing",
            "predecessor",
            "candidate",
        },
    )
    if document["schema_version"] != _EXECUTION_MANIFEST_SCHEMA_VERSION:
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_manifest_invalid"
        )
    transaction_id = _canonical_uuid(_string(document["transaction_id"]))
    kind = _enum(
        LifecycleKind,
        document["kind"],
        "native_lifecycle_execution_manifest_invalid",
    )
    authority = _validate_digest(
        _string(document["transaction_authority_digest"])
    )
    application = _signed_code_identity_from_dict(
        document["application"],
        expected_identifier=PRODUCT_IDENTITY.application_bundle_id,
    )
    recovery_application = _signed_code_identity_from_dict(
        document["recovery_application"],
        expected_identifier=PRODUCT_IDENTITY.application_bundle_id,
    )
    helper = _signed_code_identity_from_dict(
        document["helper"],
        expected_identifier=PRODUCT_IDENTITY.lifecycle_helper_identifier,
    )
    runner = _signed_code_identity_from_dict(
        document["runner"],
        expected_identifier=PRODUCT_IDENTITY.lifecycle_runner_identifier,
    )
    recovery_clone = _recovery_clone_from_dict(
        document["recovery_clone"], transaction_id=transaction_id
    )
    members = tuple(
        _execution_member_from_dict(item)
        for item in _list(document["exact_members"])
    )
    weights = tuple(
        _exclusive_weight_from_dict(item)
        for item in _list(document["exclusive_weights"])
    )
    outbox = _strict_mapping(document["outbox"])
    _require_keys(outbox, {"count", "decision", "evidence_digest"})
    count = _integer(outbox["count"])
    if count < 0 or count > 2**63 - 1:
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_manifest_invalid"
        )
    outbox_decision = _enum(
        OutboxDecision,
        outbox["decision"],
        "native_lifecycle_execution_manifest_invalid",
    )
    outbox_evidence = _validate_digest(_string(outbox["evidence_digest"]))
    pairing = _strict_mapping(document["pairing"])
    _require_keys(pairing, {"decision", "state_digest"})
    pairing_decision = _enum(
        PairingDecision,
        pairing["decision"],
        "native_lifecycle_execution_manifest_invalid",
    )
    pairing_digest = _validate_digest(_string(pairing["state_digest"]))
    predecessor = (
        _signed_code_identity_from_dict(
            document["predecessor"],
            expected_identifier=PRODUCT_IDENTITY.application_bundle_id,
        )
        if document["predecessor"] is not None
        else None
    )
    candidate = (
        _signed_code_identity_from_dict(
            document["candidate"],
            expected_identifier=PRODUCT_IDENTITY.application_bundle_id,
        )
        if document["candidate"] is not None
        else None
    )
    if len(members) + sum(len(item.files) for item in weights) > _MAX_EXECUTION_MEMBERS:
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_limit_exceeded"
        )
    paths = [item.exact_lexical_path for item in members]
    paths.extend(
        child.exact_lexical_path for item in weights for child in item.files
    )
    if len(paths) != len(set(paths)):
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_duplicate"
        )
    by_domain = {
        domain: tuple(item for item in members if item.domain is domain)
        for domain in ExecutionMemberDomain
    }
    helper_members = tuple(
        member
        for member in by_domain[ExecutionMemberDomain.RECOVERY_CLONE]
        if member.exact_lexical_path == helper.exact_path
    )
    runner_members = tuple(
        member
        for member in by_domain[ExecutionMemberDomain.RECOVERY_CLONE]
        if member.exact_lexical_path == runner.exact_path
    )
    if (
        not _signed_application_members_match(
            application,
            by_domain[ExecutionMemberDomain.APPLICATION],
            domain=ExecutionMemberDomain.APPLICATION,
        )
        or not _signed_application_members_match(
            recovery_application,
            by_domain[ExecutionMemberDomain.RECOVERY_CLONE],
            domain=ExecutionMemberDomain.RECOVERY_CLONE,
        )
        or recovery_clone.source_application_identity_digest
        != application.identity_digest
        or recovery_clone.cloned_application_identity_digest
        != recovery_application.identity_digest
        or recovery_clone.cloned_member_inventory_digest
        != recovery_application.member_inventory_digest
        or recovery_application.exact_path != recovery_clone.exact_bundle_path
        or helper.exact_path
        != os.path.join(
            recovery_clone.exact_bundle_path,
            PRODUCT_IDENTITY.lifecycle_helper_relative_path,
        )
        or runner.exact_path
        != os.path.join(
            recovery_clone.exact_bundle_path,
            PRODUCT_IDENTITY.lifecycle_runner_relative_path,
        )
        or len(helper_members) != 1
        or helper_members[0].member_type is not ExecutionMemberType.REGULAR_FILE
        or helper_members[0].disposition is not ExecutionMemberDisposition.RETAIN
        or helper.member_inventory_digest
        != execution_member_inventory_digest(helper_members)
        or len(runner_members) != 1
        or runner_members[0].member_type is not ExecutionMemberType.REGULAR_FILE
        or runner_members[0].disposition is not ExecutionMemberDisposition.RETAIN
        or runner.member_inventory_digest
        != execution_member_inventory_digest(runner_members)
        or application.team_identifier != recovery_application.team_identifier
        or application.team_identifier != helper.team_identifier
        or application.team_identifier != runner.team_identifier
        or application.build_digest != recovery_application.build_digest
        or application.code_requirement != recovery_application.code_requirement
        or application.code_directory_digest
        != recovery_application.code_directory_digest
        or application.sealed_resources_digest
        != recovery_application.sealed_resources_digest
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_recovery_clone_identity_invalid"
        )
    if predecessor is not None and predecessor != application:
        raise NativeLifecycleConflictError(
            "native_lifecycle_candidate_identity_conflict"
        )
    if candidate is None:
        if by_domain[ExecutionMemberDomain.CANDIDATE_APPLICATION]:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_manifest_invalid"
            )
    elif (
        candidate.team_identifier != application.team_identifier
        or not _signed_application_members_match(
            candidate,
            by_domain[ExecutionMemberDomain.CANDIDATE_APPLICATION],
            domain=ExecutionMemberDomain.CANDIDATE_APPLICATION,
        )
        or _paths_overlap(candidate.exact_path, application.exact_path)
        or _paths_overlap(candidate.exact_path, recovery_clone.exact_bundle_path)
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_candidate_identity_conflict"
        )
    manifest = PrivateExecutionManifest(
        transaction_id=transaction_id,
        kind=kind,
        transaction_authority_digest=authority,
        application=application,
        recovery_application=recovery_application,
        helper=helper,
        runner=runner,
        recovery_clone=recovery_clone,
        exact_members=members,
        exclusive_weights=weights,
        token_outbox_count=count,
        outbox_decision=outbox_decision,
        outbox_evidence_digest=outbox_evidence,
        pairing_decision=pairing_decision,
        pairing_state_digest=pairing_digest,
        predecessor=predecessor,
        candidate=candidate,
    )
    if plan is not None:
        _validate_execution_manifest_binding(manifest, plan)
    return manifest


def _signed_code_identity_from_dict(
    value: object, *, expected_identifier: str
) -> SignedCodeIdentity:
    document = _strict_mapping(value)
    fields = set(SignedCodeIdentity.__dataclass_fields__)
    _require_keys(document, fields)
    return _validate_signed_code_identity(
        SignedCodeIdentity(**{field: _string(document[field]) for field in fields}),
        expected_identifier=expected_identifier,
    )


def _validate_signed_code_identity(
    value: object, *, expected_identifier: str
) -> SignedCodeIdentity:
    if not isinstance(value, SignedCodeIdentity):
        raise NativeLifecycleConflictError(
            "native_lifecycle_signed_code_identity_invalid"
        )
    if value.identifier != expected_identifier:
        raise NativeLifecycleConflictError(
            "native_lifecycle_signed_code_identity_invalid"
        )
    _validate_private_text(value.version)
    _validate_exact_lexical_path(value.exact_path)
    _validate_digest(value.build_digest)
    _optional_opaque_text(value.team_identifier)
    if not value.team_identifier:
        raise NativeLifecycleConflictError(
            "native_lifecycle_signed_code_identity_invalid"
        )
    _validate_private_text(value.code_requirement)
    _validate_digest(value.code_directory_digest)
    _validate_digest(value.sealed_resources_digest)
    _validate_digest(value.member_inventory_digest)
    relative = value.executable_relative_path
    if (
        not isinstance(relative, str)
        or not relative
        or os.path.isabs(relative)
        or "\x00" in relative
        or "\n" in relative
        or "\r" in relative
        or os.path.normpath(relative) != relative
        or relative == "."
        or relative.startswith("../")
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_signed_code_identity_invalid"
        )
    return value


def _signed_application_members_match(
    identity: SignedCodeIdentity,
    members: tuple[ExactExecutionMember, ...],
    *,
    domain: ExecutionMemberDomain,
) -> bool:
    """Bind one signed app identity to exact, retained preparation evidence.

    This grants no copy, replacement, launch, or removal authority. In
    particular, a migration candidate cannot be represented only by a signed
    identity detached from the complete sealed member inventory that produced
    its digest.
    """

    if not members or any(
        item.domain is not domain
        or item.disposition is not ExecutionMemberDisposition.RETAIN
        or not _path_is_within_or_equal(
            identity.exact_path, item.exact_lexical_path
        )
        for item in members
    ):
        return False
    executable_path = os.path.join(
        identity.exact_path, identity.executable_relative_path
    )
    executable_members = tuple(
        item for item in members if item.exact_lexical_path == executable_path
    )
    return bool(
        len(executable_members) == 1
        and executable_members[0].member_type
        is ExecutionMemberType.REGULAR_FILE
        and identity.member_inventory_digest
        == execution_member_inventory_digest(members)
    )


def _recovery_clone_from_dict(
    value: object, *, transaction_id: str
) -> RecoveryCloneIdentity:
    document = _strict_mapping(value)
    fields = set(RecoveryCloneIdentity.__dataclass_fields__)
    _require_keys(document, fields)
    return _validate_recovery_clone_identity(
        RecoveryCloneIdentity(
            **{field: _string(document[field]) for field in fields}
        ),
        transaction_id=transaction_id,
    )


def _validate_recovery_clone_identity(
    value: object, *, transaction_id: str
) -> RecoveryCloneIdentity:
    if not isinstance(value, RecoveryCloneIdentity):
        raise NativeLifecycleConflictError(
            "native_lifecycle_recovery_clone_identity_invalid"
        )
    if value.transaction_id != _canonical_uuid(transaction_id):
        raise NativeLifecycleConflictError(
            "native_lifecycle_recovery_clone_identity_invalid"
        )
    _validate_exact_lexical_path(value.exact_bundle_path)
    if Path(value.exact_bundle_path).name != PRODUCT_IDENTITY.application_name:
        raise NativeLifecycleConflictError(
            "native_lifecycle_recovery_clone_identity_invalid"
        )
    for digest in (
        value.source_application_identity_digest,
        value.cloned_application_identity_digest,
        value.cloned_member_inventory_digest,
    ):
        _validate_digest(digest)
    return value


def _execution_member_from_dict(value: object) -> ExactExecutionMember:
    document = _strict_mapping(value)
    fields = set(ExactExecutionMember.__dataclass_fields__)
    _require_keys(document, fields)
    return _validate_execution_member(
        ExactExecutionMember(
            domain=_enum(
                ExecutionMemberDomain,
                document["domain"],
                "native_lifecycle_execution_member_invalid",
            ),
            exact_lexical_path=_string(document["exact_lexical_path"]),
            member_type=_enum(
                ExecutionMemberType,
                document["member_type"],
                "native_lifecycle_execution_member_invalid",
            ),
            disposition=_enum(
                ExecutionMemberDisposition,
                document["disposition"],
                "native_lifecycle_execution_member_invalid",
            ),
            device=_integer(document["device"]),
            inode=_integer(document["inode"]),
            mode=_integer(document["mode"]),
            byte_count=_integer(document["byte_count"]),
            mtime_ns=_integer(document["mtime_ns"]),
            content_digest=_optional_string(document["content_digest"]),
            symlink_target_digest=_optional_string(
                document["symlink_target_digest"]
            ),
        )
    )


def _validate_execution_member(value: object) -> ExactExecutionMember:
    if not isinstance(value, ExactExecutionMember):
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_invalid"
        )
    _enum(
        ExecutionMemberDomain,
        value.domain,
        "native_lifecycle_execution_member_invalid",
    )
    _enum(
        ExecutionMemberDisposition,
        value.disposition,
        "native_lifecycle_execution_member_invalid",
    )
    path = _validate_exact_lexical_path(value.exact_lexical_path)
    for number, maximum in (
        (value.device, 2**63 - 1),
        (value.inode, 2**63 - 1),
        (value.mode, 0o177777),
        (value.byte_count, 2**63 - 1),
        (value.mtime_ns, 2**63 - 1),
    ):
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= maximum:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_member_invalid"
            )
    expected_mode = {
        ExecutionMemberType.REGULAR_FILE: stat.S_IFREG,
        ExecutionMemberType.DIRECTORY: stat.S_IFDIR,
        ExecutionMemberType.SYMLINK: stat.S_IFLNK,
    }.get(value.member_type)
    if expected_mode is None or stat.S_IFMT(value.mode) != expected_mode:
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_invalid"
        )
    if value.member_type is ExecutionMemberType.REGULAR_FILE:
        if value.content_digest is None or value.symlink_target_digest is not None:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_member_invalid"
            )
        _validate_digest(value.content_digest)
    elif value.member_type is ExecutionMemberType.SYMLINK:
        if value.symlink_target_digest is None or value.content_digest is not None:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_member_invalid"
            )
        _validate_digest(value.symlink_target_digest)
    elif value.content_digest is not None or value.symlink_target_digest is not None:
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_invalid"
        )
    return replace(value, exact_lexical_path=path)


def _exclusive_weight_from_dict(value: object) -> ExclusiveWeightExecutionIdentity:
    document = _strict_mapping(value)
    fields = set(ExclusiveWeightExecutionIdentity.__dataclass_fields__)
    _require_keys(document, fields)
    return _validate_exclusive_weight_execution_identity(
        ExclusiveWeightExecutionIdentity(
            asset_fingerprint=_string(document["asset_fingerprint"]),
            payload_fingerprint=_string(document["payload_fingerprint"]),
            exact_lexical_path=_string(document["exact_lexical_path"]),
            storage_location_id=_string(document["storage_location_id"]),
            storage_binding_generation=_integer(
                document["storage_binding_generation"]
            ),
            volume_uuid=_optional_string(document["volume_uuid"]),
            scope_id=_optional_string(document["scope_id"]),
            installation_id=_string(document["installation_id"]),
            provenance_digest=_string(document["provenance_digest"]),
            file_inventory_digest=_string(document["file_inventory_digest"]),
            files=tuple(
                _execution_member_from_dict(item)
                for item in _list(document["files"])
            ),
        )
    )


def _validate_exclusive_weight_execution_identity(
    value: object,
) -> ExclusiveWeightExecutionIdentity:
    if not isinstance(value, ExclusiveWeightExecutionIdentity):
        raise NativeLifecycleConflictError(
            "native_lifecycle_exact_weight_identity_invalid"
        )
    for digest in (
        value.asset_fingerprint,
        value.payload_fingerprint,
        value.provenance_digest,
        value.file_inventory_digest,
    ):
        _validate_digest(digest)
    root = _validate_exact_lexical_path(value.exact_lexical_path)
    _canonical_uuid(value.storage_location_id)
    _canonical_uuid(value.installation_id)
    if (
        isinstance(value.storage_binding_generation, bool)
        or not isinstance(value.storage_binding_generation, int)
        or not 1 <= value.storage_binding_generation <= 2_147_483_647
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_exact_weight_identity_invalid"
        )
    _optional_opaque_text(value.volume_uuid)
    if value.scope_id is not None:
        _validate_digest(value.scope_id)
    files = tuple(_validate_execution_member(item) for item in value.files)
    if not files or any(
        item.domain is not ExecutionMemberDomain.EXCLUSIVE_WEIGHT
        or item.disposition is not ExecutionMemberDisposition.REMOVE_EXACT_ENTRY
        or not _path_is_within_or_equal(root, item.exact_lexical_path)
        for item in files
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_exact_weight_identity_invalid"
        )
    if value.file_inventory_digest != execution_member_inventory_digest(files):
        raise NativeLifecycleConflictError(
            "native_lifecycle_exact_weight_identity_invalid"
        )
    return replace(value, exact_lexical_path=root, files=files)


def _validate_execution_weights(
    plan: UninstallPlan,
    weights: tuple[ExclusiveWeightExecutionIdentity, ...],
) -> None:
    expected = tuple(
        item
        for item in plan.manifest_items
        if item.disposition is WeightDisposition.MOVE_TO_TRASH
    )
    if len(expected) != len(weights):
        raise NativeLifecycleConflictError(
            "native_lifecycle_exact_weight_identity_invalid"
        )
    by_asset = {item.asset_fingerprint: item for item in weights}
    if len(by_asset) != len(weights):
        raise NativeLifecycleConflictError(
            "native_lifecycle_exact_weight_identity_invalid"
        )
    for item in expected:
        execution = by_asset.get(item.asset_fingerprint)
        if execution is None or (
            execution.payload_fingerprint != item.payload_fingerprint
            or execution.exact_lexical_path != item.exact_lexical_path
            or execution.storage_location_id != item.storage_location_id
            or execution.storage_binding_generation
            != item.storage_binding_generation
            or execution.volume_uuid != item.volume_uuid
            or execution.scope_id != item.scope_id
            or execution.installation_id != item.installation_id
            or execution.provenance_digest != item.exclusive_proof_digest
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_exact_weight_identity_invalid"
            )


def _validate_execution_manifest_binding(
    manifest: PrivateExecutionManifest, plan: LifecyclePlan
) -> None:
    expected_authority = sha256(
        _canonical_json(plan._journal_dict()).encode("utf-8")
    ).hexdigest()
    if (
        manifest.transaction_id != plan.transaction_id
        or manifest.kind is not plan.kind
        or manifest.transaction_authority_digest != expected_authority
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_manifest_authority_mismatch"
        )
    by_domain = {
        domain: tuple(
            item for item in manifest.exact_members if item.domain is domain
        )
        for domain in ExecutionMemberDomain
    }
    if by_domain[ExecutionMemberDomain.PAIRING_STATE]:
        raise NativeLifecycleConflictError(
            "native_lifecycle_execution_member_authority_mismatch"
        )
    if any(
        item.disposition is not ExecutionMemberDisposition.RETAIN
        for item in by_domain[ExecutionMemberDomain.RECOVERY_CLONE]
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_recovery_clone_identity_invalid"
        )
    if isinstance(plan, UninstallPlan):
        _validate_retained_weight_separation(plan, manifest)
        if (
            manifest.token_outbox_count != plan.token_outbox_count
            or manifest.outbox_decision is not plan.outbox_decision
            or manifest.outbox_evidence_digest != plan.outbox_evidence_digest
            or manifest.predecessor is not None
            or manifest.candidate is not None
            or by_domain[ExecutionMemberDomain.CANDIDATE_APPLICATION]
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_manifest_invalid"
            )
        component_domains = {
            ComponentKind.PRIVATE_STATE: ExecutionMemberDomain.PRIVATE_STATE,
            ComponentKind.MANAGED_RUNTIMES: ExecutionMemberDomain.MANAGED_RUNTIME,
            ComponentKind.SECURITY_SCOPES: ExecutionMemberDomain.SECURITY_SCOPE,
        }
        for component in plan.components:
            domain = component_domains.get(component.kind)
            if domain is None:
                continue
            members = by_domain[domain]
            if execution_member_inventory_digest(members) != component.authority:
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_member_authority_mismatch"
                )
            expected_disposition = (
                ExecutionMemberDisposition.RETAIN
                if component.disposition is ComponentDisposition.RETAIN
                else ExecutionMemberDisposition.REMOVE_EXACT_ENTRY
            )
            if any(
                member.disposition is not expected_disposition
                for member in members
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_member_authority_mismatch"
                )
        expected_pairing = {
            HubRevocationState.NOT_REQUESTED: PairingDecision.RETAIN,
            HubRevocationState.CONFIRMED: PairingDecision.REVOKE_CONFIRMED,
            HubRevocationState.PENDING_OFFLINE: (
                PairingDecision.REVOKE_PENDING_OFFLINE
            ),
        }[plan.hub_revocation_state]
        pairing_component = next(
            item
            for item in plan.components
            if item.kind is ComponentKind.PAIRING_STATE
        )
        if (
            manifest.pairing_decision is not expected_pairing
            or manifest.pairing_state_digest != pairing_component.authority
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_pairing_decision_invalid"
            )
        _validate_execution_weights(plan, manifest.exclusive_weights)
    else:
        if (
            manifest.token_outbox_count != 0
            or manifest.outbox_decision is not OutboxDecision.PRESERVE_WITH_STATE
            or manifest.outbox_evidence_digest
            != plan.evidence.usage_outbox_identity_digest
            or manifest.predecessor is None
            or manifest.candidate is None
            or manifest.predecessor.build_digest
            != plan.predecessor_build_digest
            or manifest.candidate.build_digest != plan.candidate_build_digest
            or manifest.predecessor != manifest.application
            or manifest.candidate.team_identifier
            != manifest.application.team_identifier
            or not _signed_application_members_match(
                manifest.candidate,
                by_domain[ExecutionMemberDomain.CANDIDATE_APPLICATION],
                domain=ExecutionMemberDomain.CANDIDATE_APPLICATION,
            )
            or _paths_overlap(
                manifest.candidate.exact_path, manifest.application.exact_path
            )
            or _paths_overlap(
                manifest.candidate.exact_path,
                manifest.recovery_clone.exact_bundle_path,
            )
        ):
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_manifest_invalid"
            )
        if (
            manifest.pairing_decision is not PairingDecision.RETAIN
            or manifest.pairing_state_digest
            != plan.evidence.pairing_state_digest
            or any(
                member.disposition is not ExecutionMemberDisposition.RETAIN
                for domain in (
                    ExecutionMemberDomain.PRIVATE_STATE,
                    ExecutionMemberDomain.MANAGED_RUNTIME,
                    ExecutionMemberDomain.SECURITY_SCOPE,
                )
                for member in by_domain[domain]
            )
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_execution_manifest_invalid"
                )


def _authorized_lifecycle_effects(
    transaction: LifecycleTransaction,
    manifest: PrivateExecutionManifest,
    direction: LifecycleExecutionDirection,
) -> tuple[AuthorizedLifecycleEffect, ...]:
    """Derive one direction's opaque graph from immutable authority."""

    receipt = manifest.receipt
    authorities: list[tuple[LifecycleEffectKind, object]] = []
    if (
        transaction.kind is LifecycleKind.UNINSTALL
        and direction is LifecycleExecutionDirection.FORWARD
    ):
        plan = transaction.plan
        if not isinstance(plan, UninstallPlan):
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_plan_invalid"
            )
        authorities.extend(
            (
                (LifecycleEffectKind.QUIESCE_SERVICE, manifest.application.identity_digest),
                (LifecycleEffectKind.RESOLVE_OUTBOX, {
                    "count": plan.token_outbox_count,
                    "decision": plan.outbox_decision.value,
                    "evidence_digest": plan.outbox_evidence_digest,
                }),
                (LifecycleEffectKind.RESOLVE_PAIRING, {
                    "decision": manifest.pairing_decision.value,
                    "state_digest": manifest.pairing_state_digest,
                }),
            )
        )
        authorities.extend(
            (LifecycleEffectKind.RESOLVE_EXCLUSIVE_WEIGHT, item._private_dict())
            for item in manifest.exclusive_weights
        )
        authorities.extend(
            (LifecycleEffectKind.RESOLVE_RUNTIME_MEMBER, item._private_dict())
            for item in manifest.exact_members
            if item.domain is ExecutionMemberDomain.MANAGED_RUNTIME
        )
        authorities.extend(
            (LifecycleEffectKind.RESOLVE_STATE_MEMBER, item._private_dict())
            for item in manifest.exact_members
            if item.domain
            in {
                ExecutionMemberDomain.PRIVATE_STATE,
                ExecutionMemberDomain.SECURITY_SCOPE,
            }
        )
        authorities.extend(
            (
                (LifecycleEffectKind.UNREGISTER_AGENT, {
                    "label": PRODUCT_IDENTITY.launch_agent_label,
                }),
                (LifecycleEffectKind.UNREGISTER_LOGIN_ITEM, {
                    "bundle_id": PRODUCT_IDENTITY.application_bundle_id,
                }),
                (
                    LifecycleEffectKind.QUARANTINE_APPLICATION,
                    manifest.application.identity_digest,
                ),
                (
                    LifecycleEffectKind.REMOVE_APPLICATION,
                    manifest.application.identity_digest,
                ),
                (LifecycleEffectKind.FINALIZE_UNINSTALL, transaction.authority_digest),
            )
        )
    elif transaction.kind is LifecycleKind.MIGRATION:
        plan = transaction.plan
        if not isinstance(plan, MigrationPlan) or manifest.candidate is None or manifest.predecessor is None:
            raise NativeLifecycleConflictError(
                "native_lifecycle_execution_plan_invalid"
            )
        if direction is LifecycleExecutionDirection.FORWARD:
            authorities.extend((
                (LifecycleEffectKind.PREFLIGHT_CANDIDATE, manifest.candidate.identity_digest),
                (LifecycleEffectKind.DRAIN_INFERENCE, manifest.application.identity_digest),
                (LifecycleEffectKind.CAPTURE_ROLLBACK, {
                    "rollback_snapshot_digest": plan.evidence.rollback_snapshot_digest,
                    "predecessor_identity_digest": manifest.predecessor.identity_digest,
                }),
                (LifecycleEffectKind.STOP_PREDECESSOR, manifest.predecessor.identity_digest),
                (LifecycleEffectKind.INSTALL_CANDIDATE, manifest.candidate.identity_digest),
                (LifecycleEffectKind.START_CANDIDATE, manifest.candidate.identity_digest),
                (LifecycleEffectKind.VALIDATE_CANDIDATE, {
                    "candidate_identity_digest": manifest.candidate.identity_digest,
                    "manifest_digest": receipt.manifest_digest,
                }),
                (LifecycleEffectKind.COMMIT_CANDIDATE, manifest.candidate.identity_digest),
            ))
        elif direction is LifecycleExecutionDirection.ROLLBACK:
            authorities.append(
                (
                    LifecycleEffectKind.RESTORE_PREDECESSOR,
                    manifest.predecessor.identity_digest,
                )
            )
    if authorities and direction in {
        LifecycleExecutionDirection.FORWARD,
        LifecycleExecutionDirection.ROLLBACK,
    }:
        authorities.append(
            (
                LifecycleEffectKind.CLEANUP_RECOVERY_CLONE,
                manifest.recovery_clone.identity_digest,
            )
        )

    result: list[AuthorizedLifecycleEffect] = []
    for effect_kind, target_authority in authorities:
        target_digest = sha256(
            _canonical_json(
                {
                    "effect_kind": effect_kind.value,
                    "execution_direction": direction.value,
                    "execution_manifest_digest": receipt.manifest_digest,
                    "target_authority": target_authority,
                    "transaction_authority_digest": transaction.authority_digest,
                    "transaction_id": transaction.transaction_id,
                    "version": 3,
                }
            ).encode("utf-8")
        ).hexdigest()
        effect_id = str(
            uuid5(
                _EFFECT_ID_NAMESPACE,
                f"{transaction.transaction_id}:{effect_kind.value}:{target_digest}",
            )
        )
        result.append(
            AuthorizedLifecycleEffect(effect_id, effect_kind, target_digest)
        )
    if len({(item.effect_id, item.target_digest) for item in result}) != len(result):
        raise NativeLifecycleConflictError(
            "native_lifecycle_effect_authority_conflict"
        )
    return tuple(result)


def _validate_ordered_effect_graph_receipts(
    authorized: tuple[AuthorizedLifecycleEffect, ...],
    receipts: tuple[LifecycleEffectReceipt, ...],
) -> None:
    """Require conclusive completion before the next opaque target starts.

    Per-target receipt chaining alone cannot express the graph's destructive
    ordering boundary.  This additional invariant keeps a future runner from
    observing or applying any later target until every earlier target has a
    conclusive ``FINALIZED`` receipt.  It grants no effect authority and is
    also applied while validating the journal after restart.
    """

    positions = {
        (item.effect_id, item.effect_kind, item.target_digest): index
        for index, item in enumerate(authorized)
    }
    if len(positions) != len(authorized):
        raise NativeLifecycleConflictError(
            "native_lifecycle_effect_authority_conflict"
        )
    current_index = 0
    for receipt in receipts:
        index = positions.get(
            (receipt.effect_id, receipt.effect_kind, receipt.target_digest)
        )
        if index is None:
            raise NativeLifecycleConflictError(
                "native_lifecycle_effect_authority_mismatch"
            )
        if index != current_index:
            raise NativeLifecycleConflictError(
                "native_lifecycle_effect_receipt_out_of_order"
            )
        if receipt.status is LifecycleEffectReceiptStatus.FINALIZED:
            if (
                receipt.observation
                is not LifecycleEffectObservation.EFFECT_SATISFIED
            ):
                raise NativeLifecycleConflictError(
                    "native_lifecycle_effect_receipt_out_of_order"
                )
            current_index += 1


def _validate_retained_weight_separation(
    plan: UninstallPlan,
    manifest: PrivateExecutionManifest,
) -> None:
    """Keep every retained lexical weight outside every removal authority."""

    retained = tuple(
        item
        for item in plan.manifest_items
        if item.disposition is WeightDisposition.RETAIN
        and item.exact_lexical_path
    )
    removal_paths = [
        member.exact_lexical_path
        for member in manifest.exact_members
        if member.disposition is ExecutionMemberDisposition.REMOVE_EXACT_ENTRY
    ]
    removal_paths.extend(
        member.exact_lexical_path
        for weight in manifest.exclusive_weights
        for member in weight.files
    )
    application_component = next(
        item
        for item in plan.components
        if item.kind is ComponentKind.APPLICATION
    )
    if application_component.disposition is ComponentDisposition.REMOVE_EXACT:
        removal_paths.append(manifest.application.exact_path)

    for item in retained:
        retained_path = _validate_exact_lexical_path(item.exact_lexical_path)
        _canonical_uuid(item.storage_location_id)
        if item.scope_id is not None:
            _validate_digest(item.scope_id)
        _optional_opaque_text(item.volume_uuid)
        if any(_paths_overlap(retained_path, target) for target in removal_paths):
            # No volume, scope, provenance, or symlink inference can widen a
            # removal grant past this exact lexical denial boundary.
            raise NativeLifecycleConflictError(
                "native_lifecycle_retained_weight_overlap"
            )


def _path_is_within_or_equal(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath((root, candidate)) == root
    except ValueError:
        return False


def _validate_component_contract(
    components: tuple[ComponentPlan, ...], mode: RetentionMode
) -> None:
    by_kind = {item.kind: item for item in components}
    if by_kind[ComponentKind.APPLICATION] != ComponentPlan(
        ComponentKind.APPLICATION,
        ComponentDisposition.REMOVE_EXACT,
        PRODUCT_IDENTITY.application_bundle_id,
    ) or by_kind[ComponentKind.LAUNCH_AGENT] != ComponentPlan(
        ComponentKind.LAUNCH_AGENT,
        ComponentDisposition.REMOVE_EXACT,
        PRODUCT_IDENTITY.launch_agent_label,
    ):
        raise NativeLifecycleConflictError("native_lifecycle_identity_invalid")
    state_kinds = (
        ComponentKind.PRIVATE_STATE,
        ComponentKind.SECURITY_SCOPES,
        ComponentKind.PAIRING_STATE,
    )
    runtime_disposition = (
        ComponentDisposition.RETAIN
        if mode is RetentionMode.APP_ONLY
        else ComponentDisposition.REMOVE_PROVEN_MEMBERS
    )
    reinstall_safe = all(
        by_kind[kind].disposition is ComponentDisposition.RETAIN
        for kind in state_kinds
    ) and (
        by_kind[ComponentKind.MANAGED_RUNTIMES].disposition
        is runtime_disposition
    )
    legacy_disposition = (
        ComponentDisposition.RETAIN
        if mode is RetentionMode.APP_ONLY
        else ComponentDisposition.REMOVE_PROVEN_MEMBERS
    )
    legacy_v2 = all(
        by_kind[kind].disposition is legacy_disposition
        for kind in (
            ComponentKind.PRIVATE_STATE,
            ComponentKind.MANAGED_RUNTIMES,
            ComponentKind.SECURITY_SCOPES,
            ComponentKind.PAIRING_STATE,
        )
    )
    if not (reinstall_safe or legacy_v2):
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    for kind in (
        ComponentKind.PRIVATE_STATE,
        ComponentKind.MANAGED_RUNTIMES,
        ComponentKind.SECURITY_SCOPES,
        ComponentKind.PAIRING_STATE,
    ):
        _validate_digest(by_kind[kind].authority)


def _allowed_next_phases(
    kind: LifecycleKind,
    phase: LifecyclePhase,
    contract_version: int = _LIFECYCLE_CONTRACT_VERSION,
) -> tuple[LifecyclePhase, ...]:
    if contract_version != _LIFECYCLE_CONTRACT_VERSION:
        return ()
    sequence = (
        _UNINSTALL_SEQUENCE
        if kind is LifecycleKind.UNINSTALL
        else _MIGRATION_SEQUENCE
    )
    try:
        index = sequence.index(phase)
    except ValueError:
        return ()
    if index + 1 >= len(sequence):
        return ()
    result = [sequence[index + 1]]
    if kind is LifecycleKind.MIGRATION and phase in _MIGRATION_ROLLBACK_FROM:
        result.append(LifecyclePhase.RESTORED)
    return tuple(result)


def _validate_legacy_v1_events(
    *,
    kind: LifecycleKind,
    current_phase: LifecyclePhase,
    created_at: float,
    updated_at: float,
    rows: Iterable[sqlite3.Row],
) -> None:
    events = tuple(rows)
    sequence = (
        _LEGACY_UNINSTALL_SEQUENCE_V1
        if kind is LifecycleKind.UNINSTALL
        else _LEGACY_MIGRATION_SEQUENCE_V1
    )
    if not events:
        raise NativeLifecycleIntegrityError(
            "native_lifecycle_legacy_v1_journal_corrupt"
        )
    previous: LifecyclePhase | None = None
    previous_at = -1.0
    for expected, row in enumerate(events, start=1):
        try:
            if int(row["sequence"]) != expected:
                raise ValueError
            phase = LifecyclePhase(str(row["phase"]))
            recorded_at = _validate_timestamp(row["recorded_at"])
            error = (
                str(row["error_code"])
                if row["error_code"] is not None
                else None
            )
            if recorded_at < previous_at:
                raise ValueError
            if expected == 1:
                if phase is not sequence[0] or error is not None:
                    raise ValueError
            elif phase is LifecyclePhase.MANUAL_RECOVERY:
                if expected != len(events) or error not in MANUAL_RECOVERY_CODES:
                    raise ValueError
            else:
                if previous is None:
                    raise ValueError
                try:
                    index = sequence.index(previous)
                except ValueError:
                    raise ValueError from None
                allowed = (
                    (sequence[index + 1],)
                    if index + 1 < len(sequence)
                    else ()
                )
                if (
                    kind is LifecycleKind.MIGRATION
                    and previous in _MIGRATION_ROLLBACK_FROM
                ):
                    allowed = (*allowed, LifecyclePhase.RESTORED)
                if phase not in allowed or error is not None:
                    raise ValueError
            previous = phase
            previous_at = recorded_at
        except (NativeLifecycleError, TypeError, ValueError):
            raise NativeLifecycleIntegrityError(
                "native_lifecycle_legacy_v1_journal_corrupt"
            ) from None
    if (
        previous is not current_phase
        or float(events[0]["recorded_at"]) != created_at
        or float(events[-1]["recorded_at"]) != updated_at
    ):
        raise NativeLifecycleIntegrityError(
            "native_lifecycle_legacy_v1_journal_corrupt"
        )


def _manual(code: str) -> LifecycleRecoveryDecision:
    if code not in MANUAL_RECOVERY_CODES:
        code = "journal_conflict"
    return LifecycleRecoveryDecision(
        LifecycleRecoveryAction.MANUAL_RECOVERY, reason_code=code
    )


def _product_dict() -> dict[str, str]:
    return {
        "application_name": PRODUCT_IDENTITY.application_name,
        "application_bundle_id": PRODUCT_IDENTITY.application_bundle_id,
        "launch_agent_label": PRODUCT_IDENTITY.launch_agent_label,
        "service_code_requirement_id": PRODUCT_IDENTITY.service_code_requirement_id,
        "lifecycle_helper_identifier": (
            PRODUCT_IDENTITY.lifecycle_helper_identifier
        ),
    }


def _public_product_dict() -> dict[str, str]:
    return {
        "application_name": PRODUCT_IDENTITY.application_name,
        "application_bundle_id": PRODUCT_IDENTITY.application_bundle_id,
        "launch_agent_label": PRODUCT_IDENTITY.launch_agent_label,
        "service_code_requirement_id": (
            PRODUCT_IDENTITY.service_code_requirement_id
        ),
    }


def helper_authorization_challenge_from_mapping(
    value: object,
) -> HelperAuthorizationChallenge:
    """Parse a closed helper-v2 challenge mapping with no coercion."""

    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    expected = {
        "schema_version",
        "helper_protocol_version",
        "nonce",
        "transaction_id",
        "transaction_authority_digest",
        "execution_manifest_digest",
        "recovery_clone_identity_digest",
        "expected_helper_identifier",
        "expected_helper_build_digest",
        "expected_team_identifier",
        "expected_code_requirement_digest",
        "expected_app_build_digest",
        "expected_authorization_proof_algorithm",
        "expected_authorization_key_id",
        "session_id",
        "issued_at",
        "expires_at",
    }
    if set(value) != expected:
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    challenge = HelperAuthorizationChallenge(
        schema_version=_helper_integer(value["schema_version"]),
        helper_protocol_version=_helper_integer(
            value["helper_protocol_version"]
        ),
        nonce=_helper_string(value["nonce"]),
        transaction_id=_helper_string(value["transaction_id"]),
        transaction_authority_digest=_helper_string(
            value["transaction_authority_digest"]
        ),
        execution_manifest_digest=_helper_string(
            value["execution_manifest_digest"]
        ),
        recovery_clone_identity_digest=_helper_string(
            value["recovery_clone_identity_digest"]
        ),
        expected_helper_identifier=_helper_string(
            value["expected_helper_identifier"]
        ),
        expected_helper_build_digest=_helper_string(
            value["expected_helper_build_digest"]
        ),
        expected_team_identifier=_helper_string(
            value["expected_team_identifier"]
        ),
        expected_code_requirement_digest=_helper_string(
            value["expected_code_requirement_digest"]
        ),
        expected_app_build_digest=_helper_string(
            value["expected_app_build_digest"]
        ),
        expected_authorization_proof_algorithm=_helper_string(
            value["expected_authorization_proof_algorithm"]
        ),
        expected_authorization_key_id=_helper_string(
            value["expected_authorization_key_id"]
        ),
        session_id=_helper_string(value["session_id"]),
        issued_at=_helper_integer(value["issued_at"]),
        expires_at=_helper_integer(value["expires_at"]),
    )
    return _validate_helper_authorization_challenge(challenge)


def helper_authorization_submission_from_mapping(
    value: object,
) -> HelperAuthorizationSubmission:
    """Parse the helper receipt's closed proof-bearing contract."""

    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    challenge_keys = set(
        HelperAuthorizationChallenge(
            schema_version=2,
            helper_protocol_version=2,
            nonce="00000000-0000-0000-0000-000000000000",
            transaction_id="00000000-0000-0000-0000-000000000000",
            transaction_authority_digest="sha256:" + "0" * 64,
            execution_manifest_digest="sha256:" + "0" * 64,
            recovery_clone_identity_digest="sha256:" + "0" * 64,
            expected_helper_identifier=(
                PRODUCT_IDENTITY.lifecycle_helper_identifier
            ),
            expected_helper_build_digest="sha256:" + "0" * 64,
            expected_team_identifier="AAAAAAAAAA",
            expected_code_requirement_digest="sha256:" + "0" * 64,
            expected_app_build_digest="sha256:" + "0" * 64,
            expected_authorization_proof_algorithm="os-peer-attested-v1",
            expected_authorization_key_id="sha256:" + "0" * 64,
            session_id="00000000-0000-0000-0000-000000000000",
            issued_at=1,
            expires_at=2,
        ).to_public_dict()
    )
    if set(value) != challenge_keys | {
        "authorization_digest",
        "authenticated_at",
        "authorization_proof",
    }:
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    challenge = helper_authorization_challenge_from_mapping(
        {key: value[key] for key in challenge_keys}
    )
    authenticated_at = value["authenticated_at"]
    if (
        isinstance(authenticated_at, bool)
        or not isinstance(authenticated_at, (int, float))
        or not math.isfinite(float(authenticated_at))
        or not 0 < float(authenticated_at) <= _MAX_TIMESTAMP
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    return _validate_helper_authorization_submission(
        HelperAuthorizationSubmission(
            **{
                field: getattr(challenge, field)
                for field in HelperAuthorizationChallenge.__dataclass_fields__
            },
            authorization_digest=_helper_string(
                value["authorization_digest"]
            ),
            authenticated_at=float(authenticated_at),
            authorization_proof=_helper_string(
                value["authorization_proof"]
            ),
        )
    )


def _validate_helper_authorization_challenge(
    value: object,
) -> HelperAuthorizationChallenge:
    if not isinstance(value, HelperAuthorizationChallenge):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    try:
        if (
            value.schema_version != 2
            or value.helper_protocol_version != 2
            or value.expected_helper_identifier
            != PRODUCT_IDENTITY.lifecycle_helper_identifier
            or _AUTHORIZATION_PROOF_ALGORITHM_RE.fullmatch(
                value.expected_authorization_proof_algorithm
            )
            is None
            or _TEAM_IDENTIFIER_RE.fullmatch(value.expected_team_identifier)
            is None
            or value.issued_at <= 0
            or value.expires_at <= value.issued_at
            or value.expires_at - value.issued_at
            > _MAX_HELPER_AUTHORIZATION_LIFETIME_SECONDS
        ):
            raise ValueError
        _canonical_uuid(value.nonce)
        _canonical_uuid(value.transaction_id)
        _canonical_uuid(value.session_id)
        for digest in (
            value.transaction_authority_digest,
            value.execution_manifest_digest,
            value.recovery_clone_identity_digest,
            value.expected_helper_build_digest,
            value.expected_code_requirement_digest,
            value.expected_app_build_digest,
            value.expected_authorization_key_id,
        ):
            _validate_prefixed_digest(digest)
    except (NativeLifecycleError, TypeError, ValueError):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        ) from None
    return value


def _validate_helper_authorization_submission(
    value: object,
) -> HelperAuthorizationSubmission:
    if not isinstance(value, HelperAuthorizationSubmission):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    challenge = helper_authorization_challenge_from_mapping(
        value.challenge_dict()
    )
    _validate_prefixed_digest(value.authorization_digest)
    if _AUTHORIZATION_PROOF_RE.fullmatch(value.authorization_proof) is None:
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    if not (
        challenge.issued_at <= value.authenticated_at < challenge.expires_at
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    return value


def _helper_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    return value


def _helper_string(value: object) -> str:
    if not isinstance(value, str):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    return value


def _validate_prefixed_digest(value: object) -> str:
    if not isinstance(value, str) or _PREFIXED_DIGEST_RE.fullmatch(value) is None:
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    return value


def _prefixed_digest(value: str) -> str:
    return "sha256:" + _validate_digest(value)


def _unprefixed_digest(value: str) -> str:
    _validate_prefixed_digest(value)
    return value[7:]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise NativeLifecycleConflictError("native_lifecycle_identifier_invalid")
    try:
        canonical = str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise NativeLifecycleConflictError(
            "native_lifecycle_identifier_invalid"
        ) from None
    if canonical != value:
        raise NativeLifecycleConflictError("native_lifecycle_identifier_invalid")
    return canonical


def _validate_digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise NativeLifecycleConflictError("native_lifecycle_fingerprint_invalid")
    return value


def _optional_digest(value: object) -> str | None:
    if value is None:
        return None
    return _validate_digest(value)


def _bounded_positive_integer(value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_effect_receipt_invalid"
        )
    return value


def _validate_exact_lexical_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value == "/"
        or "\x00" in value
        or len(value.encode("utf-8")) > _MAX_PATH_BYTES
        or any(ord(character) < 32 for character in value)
    ):
        raise NativeLifecycleConflictError("native_lifecycle_weight_path_invalid")
    components = value.split("/")[1:]
    if not components or any(part in {"", ".", ".."} for part in components):
        raise NativeLifecycleConflictError("native_lifecycle_weight_path_invalid")
    # Preserve the exact spelling.  In particular, never resolve symlinks,
    # expand variables, case-fold, or replace the selected folder by a mount.
    return value


def _paths_overlap(left: str, right: str) -> bool:
    """Compare already-validated lexical paths without resolving symlinks."""

    return (
        left == right
        or left.startswith(f"{right}/")
        or right.startswith(f"{left}/")
    )


def _optional_opaque_text(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > _MAX_OPAQUE_TEXT
        or _OPAQUE_TEXT_RE.fullmatch(value) is None
    ):
        raise NativeLifecycleConflictError("native_lifecycle_opaque_value_invalid")
    return value


def _validate_private_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or len(value.encode("utf-8")) > _MAX_PRIVATE_TEXT_BYTES
    ):
        raise NativeLifecycleConflictError(
            "native_lifecycle_signed_code_identity_invalid"
        )
    return value


def _validate_timestamp(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= _MAX_TIMESTAMP
    ):
        raise NativeLifecycleIntegrityError(
            "native_lifecycle_journal_clock_invalid"
        )
    return float(value)


def _validate_monotonic(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise NativeLifecycleIntegrityError(
            "native_lifecycle_journal_clock_invalid"
        )
    return float(value)


def _enum(enum_type: type[_T], value: object, code: str) -> _T:
    try:
        return enum_type(value)  # type: ignore[call-arg,return-value]
    except (TypeError, ValueError):
        raise NativeLifecycleConflictError(code) from None


def _strict_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    return value


def _require_keys(
    value: Mapping[str, object], expected: set[str], *, minimum: bool = False
) -> None:
    keys = set(value)
    if (minimum and not expected <= keys) or (not minimum and keys != expected):
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NativeLifecycleConflictError("native_lifecycle_plan_invalid")
    return value


def _boot_digest(value: object) -> str:
    if (
        isinstance(value, str)
        and value.startswith("sha256:")
        and _PREFIXED_DIGEST_RE.fullmatch(value) is not None
    ):
        return value
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise NativeLifecycleIntegrityError(
            "native_lifecycle_journal_clock_invalid"
        )
    return _prefixed_digest(sha256(value.encode("utf-8")).hexdigest())


def _default_boot_identity() -> str:
    linux_boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        if linux_boot_id.is_file():
            value = linux_boot_id.read_text(encoding="ascii").strip()
            if value:
                return value
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            library = ctypes.util.find_library("c")
            libc = ctypes.CDLL(library or None, use_errno=True)
            sysctlbyname = libc.sysctlbyname
            sysctlbyname.argtypes = (
                ctypes.c_char_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.c_void_p,
                ctypes.c_size_t,
            )
            sysctlbyname.restype = ctypes.c_int
            size = ctypes.c_size_t()
            name = b"kern.bootsessionuuid"
            if sysctlbyname(name, None, ctypes.byref(size), None, 0) != 0:
                raise OSError
            if not 1 <= size.value <= 4096:
                raise OSError
            buffer = ctypes.create_string_buffer(size.value)
            if sysctlbyname(name, buffer, ctypes.byref(size), None, 0) != 0:
                raise OSError
            value = buffer.value.decode("ascii").strip()
            if value:
                return value
        except (OSError, AttributeError, UnicodeError, ValueError):
            pass
    # This fallback intentionally fails closed across a wall-clock change;
    # authoritative Darwin builds normally use ``kern.bootsessionuuid``.
    return f"boot-epoch:{int(round(time.time() - time.monotonic()))}"


def _secure_directory(path: Path) -> None:
    status = path.lstat()
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise NativeLifecycleIntegrityError(
            "native_lifecycle_journal_insecure_path"
        )
    os.chmod(path, 0o700, follow_symlinks=False)


def _secure_regular_file(path: Path, status: os.stat_result) -> None:
    if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise NativeLifecycleIntegrityError(
            "native_lifecycle_journal_insecure_path"
        )
    os.chmod(path, 0o600, follow_symlinks=False)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback(connection: sqlite3.Connection | None) -> None:
    if connection is None:
        return
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        return


__all__ = [
    "AuthorizedLifecycleEffect",
    "ComponentDisposition",
    "ComponentKind",
    "ComponentPlan",
    "ExecutionManifestReceipt",
    "ExecutionManifestStore",
    "ExecutionMemberDisposition",
    "ExecutionMemberDomain",
    "ExecutionMemberType",
    "ExactExecutionMember",
    "ExclusiveWeightExecutionIdentity",
    "ExclusiveProofState",
    "HubRevocationState",
    "LifecycleJournalMutation",
    "LifecycleKind",
    "LifecycleExecutionClaimResult",
    "LifecycleExecutionClaimState",
    "LifecycleExecutionDirection",
    "LifecycleExecutionGrantMutation",
    "LifecycleExecutionStartGrant",
    "LifecycleEffectKind",
    "LifecycleEffectObservation",
    "LifecycleEffectReceipt",
    "LifecycleEffectReceiptMutation",
    "LifecycleEffectReceiptStatus",
    "LifecyclePhase",
    "LifecycleRecoveryAction",
    "LifecycleRecoveryDecision",
    "LifecycleTransaction",
    "LifecycleRunnerLeaseEpoch",
    "LifecycleRunnerLeaseMutation",
    "LifecycleRunnerProcessFence",
    "HelperAuthorizationProofAuthority",
    "HelperAuthorizationReceipt",
    "HelperStageReceipt",
    "MANUAL_RECOVERY_CODES",
    "LEGACY_SIDECAR_LABEL",
    "LegacySidecarMigrationState",
    "MigrationEvidence",
    "MigrationPlan",
    "NativeLifecycleCapacityError",
    "NativeLifecycleConflictError",
    "NativeLifecycleError",
    "NativeLifecycleIntegrityError",
    "NativeLifecycleJournal",
    "NativeLifecycleNotFoundError",
    "NativeProductIdentity",
    "OutboxDecision",
    "PRODUCT_IDENTITY",
    "PriorEffectsState",
    "PrivateExecutionManifest",
    "PrivateRetentionManifest",
    "RecoveryObservation",
    "RetentionManifestItem",
    "RetentionManifestReceipt",
    "RetentionManifestStore",
    "RetentionMode",
    "RetentionWeight",
    "RecoveryCloneIdentity",
    "SignedCodeIdentity",
    "UninstallPlan",
    "WeightDisposition",
    "WeightOwnership",
    "build_migration_plan",
    "build_private_execution_manifest",
    "build_uninstall_plan",
    "decide_lifecycle_recovery",
    "decide_manual_recovery_action",
    "execution_member_inventory_digest",
]
