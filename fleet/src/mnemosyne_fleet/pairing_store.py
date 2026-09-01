from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import stat
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, TypeVar

from .secret_store import (
    CredentialBundle,
    CredentialSecret,
    PairingMaterial,
    PrivateValue,
    SecretStore,
    SecretStoreError,
)


InvitationIntent = Literal["new", "adopt-static"]
InvitationState = Literal[
    "preparing",
    "issued",
    "claimed",
    "approved",
    "provisioning",
    "activating",
    "completed",
    "expired",
    "rejected",
    "failed",
]
EnrollmentLifecycle = Literal["pending", "active", "revoked"]
EnrollmentState = Literal["pending", "active", "disabled", "revoked"]
GenerationState = Literal[
    "allocating",
    "candidate",
    "active",
    "retiring",
    "retired",
    "revoked",
]
ServiceClass = Literal["primary", "opportunistic", "overflow"]
Transport = Literal["https", "tailscale", "trusted_lan_http"]

_SCHEMA_VERSION: Final[int] = 1
_PAIRING_PROTOCOL_VERSION: Final[int] = 1
_MAX_INVITATION_SECONDS: Final[float] = 300.0
_INVITATION_ATTEMPT_LIMIT: Final[int] = 5
_MAX_PENDING_INVITATIONS: Final[int] = 1024
_MAX_TEXT_BYTES: Final[int] = 128
_MAX_VERSION_BYTES: Final[int] = 64
_MAX_LOCATOR_BYTES: Final[int] = 2048
_FIXED_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        "attempt_budget_exhausted",
        "credential_material_missing",
        "private_material_missing",
        "secret_cleanup_pending",
        "secret_reconciliation_failed",
        "secret_store_failure",
    }
)
_SERVICE_CLASSES: Final[tuple[ServiceClass, ...]] = (
    "primary",
    "opportunistic",
    "overflow",
)
_TRANSPORTS: Final[tuple[Transport, ...]] = (
    "https",
    "tailscale",
    "trusted_lan_http",
)
_INVITATION_TERMINAL: Final[frozenset[str]] = frozenset(
    {"completed", "expired", "rejected", "failed"}
)

_T = TypeVar("_T")


class PairingStoreError(RuntimeError):
    """A fixed-code pairing failure with no caller-controlled diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PairingStoreValidationError(PairingStoreError):
    pass


class PairingStoreConflictError(PairingStoreError):
    pass


class PairingStoreTerminalError(PairingStoreError):
    pass


class PairingStoreIntegrityError(PairingStoreError):
    pass


@dataclass(frozen=True, slots=True)
class InvitationRequest:
    request_id: str
    locator: str = field(repr=False)
    intent: InvitationIntent = "new"
    expected_platform: str = "macos"
    expected_reporting_node_id: str | None = None
    transport: Transport = "https"
    service_class: ServiceClass = "primary"
    expires_in_seconds: float = _MAX_INVITATION_SECONDS


@dataclass(frozen=True, slots=True)
class InvitationIssue:
    invitation_id: str
    pairing_secret: str = field(repr=False)
    expires_at: float
    state: InvitationState


@dataclass(frozen=True, slots=True)
class InvitationRecord:
    invitation_id: str
    pairing_id: str | None
    intent: InvitationIntent
    expected_platform: str
    expected_reporting_node_id: str | None
    transport: Transport
    service_class: ServiceClass
    state: InvitationState
    created_at: float
    expires_at: float
    attempts_remaining: int
    claim_id: str | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    request_id: str
    invitation_id: str
    pairing_secret: str = field(repr=False)
    locator: str = field(repr=False)
    display_name: str = "Mac"
    reporting_node_id: str = "node"
    service_version: str = "0.0.0"
    platform: str = "macos"
    protocol_minimum: int = 1
    protocol_maximum: int = 1


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    invitation_id: str
    pairing_id: str
    display_name: str
    reporting_node_id: str
    service_version: str
    platform: str
    protocol_version: int
    state: InvitationState
    claimed_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    claim_id: str
    locator: str = field(repr=False)
    service_class: ServiceClass = "primary"
    hub_enabled: bool = False


@dataclass(frozen=True, slots=True)
class PresenceApprovalRequest:
    request_id: str
    claim_id: str
    presence_pin: str = field(repr=False)
    service_class: ServiceClass = "primary"
    hub_enabled: bool = False


@dataclass(frozen=True, slots=True)
class ProvisionRequest:
    request_id: str
    claim_id: str
    pairing_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProvisioningRecord:
    claim_id: str
    pairing_id: str
    reporting_node_id: str
    credential_generation: int
    credentials: CredentialBundle = field(repr=False)
    state: InvitationState = "provisioning"


@dataclass(frozen=True, slots=True)
class EnrollmentRecord:
    pairing_id: str
    reporting_node_id: str
    display_name: str
    platform: str
    service_version: str
    protocol_version: int
    service_class: ServiceClass
    lifecycle_state: EnrollmentLifecycle
    hub_enabled: bool
    credential_generation: int | None
    created_at: float
    updated_at: float
    revoked_at: float | None
    failure_code: str | None

    @property
    def state(self) -> EnrollmentState:
        if self.lifecycle_state == "revoked":
            return "revoked"
        if self.lifecycle_state == "pending":
            return "pending"
        return "active" if self.hub_enabled else "disabled"

    @property
    def routable(self) -> bool:
        return self.state == "active" and self.failure_code is None


@dataclass(frozen=True, slots=True)
class EnrollmentBinding:
    pairing_id: str
    invitation_id: str
    reporting_node_id: str
    locator_ref: str = field(repr=False)
    credential_generation: int
    snapshot_ref: str = field(repr=False)
    dispatch_ref: str = field(repr=False)
    management_ref: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    expired: int = 0
    finalized_invitations: int = 0
    finalized_generations: int = 0
    failed_closed: int = 0
    cleaned: int = 0


class PairingStore:
    """Secret-free durable pairing journal backed by a dedicated SQLite file.

    Raw invitation values, locators, and all credential ciphertext remain in
    ``SecretStore``. This database persists only bounded lifecycle metadata,
    canonical secret-free request digests, and opaque references.
    """

    def __init__(
        self,
        path: Path,
        *,
        store_id: str,
        secret_store: SecretStore,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._store_id = _bounded_text(store_id, maximum=_MAX_TEXT_BYTES)
        self._secret_store = secret_store
        self._clock = clock
        self._io_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> ReconciliationReport:
        await self._run_sync(self._initialize_sync)
        self._initialized = True
        return await self.reconcile()

    async def issue_invitation(self, request: InvitationRequest) -> InvitationIssue:
        """Issue once; exact request replay recovers the same encrypted secret."""

        _validate_invitation_request(request)
        self._require_initialized()
        return await self._mutate(lambda: self._issue_invitation(request))

    async def claim_invitation(self, request: ClaimRequest) -> ClaimRecord:
        _validate_claim_request(request)
        self._require_initialized()
        return await self._mutate(lambda: self._claim_invitation(request))

    async def approve_claim(self, request: ApprovalRequest) -> EnrollmentRecord:
        _validate_approval_request(request)
        self._require_initialized()
        return await self._mutate(lambda: self._approve_claim(request))

    async def approve_claim_with_presence(
        self,
        request: PresenceApprovalRequest,
    ) -> EnrollmentRecord:
        _validate_presence_approval_request(request)
        self._require_initialized()
        return await self._mutate(
            lambda: self._approve_claim_with_presence(request)
        )

    async def reject_claim(self, *, request_id: str, claim_id: str) -> bool:
        request_id = _canonical_uuid(request_id)
        claim_id = _canonical_uuid(claim_id)
        self._require_initialized()
        return await self._mutate(
            lambda: self._reject_claim(request_id=request_id, claim_id=claim_id)
        )

    async def provision_claim(
        self,
        request: ProvisionRequest,
    ) -> ProvisioningRecord:
        _validate_provision_request(request)
        self._require_initialized()
        return await self._mutate(lambda: self._provision_claim(request))

    async def mark_activating(
        self,
        *,
        request_id: str,
        pairing_id: str,
        generation: int,
    ) -> EnrollmentRecord:
        request_id = _canonical_uuid(request_id)
        pairing_id = _canonical_uuid(pairing_id)
        generation = _bounded_generation(generation)
        self._require_initialized()
        return await self._mutate(
            lambda: self._mark_activating(
                request_id=request_id,
                pairing_id=pairing_id,
                generation=generation,
            )
        )

    async def activate_enrollment(
        self,
        *,
        request_id: str,
        pairing_id: str,
        generation: int,
    ) -> EnrollmentRecord:
        request_id = _canonical_uuid(request_id)
        pairing_id = _canonical_uuid(pairing_id)
        generation = _bounded_generation(generation)
        self._require_initialized()
        return await self._mutate(
            lambda: self._activate_enrollment(
                request_id=request_id,
                pairing_id=pairing_id,
                generation=generation,
            )
        )

    async def set_hub_enabled(
        self,
        *,
        request_id: str,
        pairing_id: str,
        enabled: bool,
    ) -> EnrollmentRecord:
        request_id = _canonical_uuid(request_id)
        pairing_id = _canonical_uuid(pairing_id)
        if not isinstance(enabled, bool):
            raise PairingStoreValidationError("pairing_invalid_request")
        self._require_initialized()
        return await self._mutate(
            lambda: self._set_hub_enabled(
                request_id=request_id,
                pairing_id=pairing_id,
                enabled=enabled,
            )
        )

    async def revoke_enrollment(
        self,
        *,
        request_id: str,
        pairing_id: str,
    ) -> EnrollmentRecord:
        request_id = _canonical_uuid(request_id)
        pairing_id = _canonical_uuid(pairing_id)
        self._require_initialized()
        return await self._mutate(
            lambda: self._revoke_enrollment(
                request_id=request_id,
                pairing_id=pairing_id,
            )
        )

    async def self_disable_enrollment(
        self,
        *,
        request_id: str,
        pairing_id: str,
        reporting_node_id: str,
        credential_generation: int,
    ) -> EnrollmentRecord:
        """Disable one exact active dynamic enrollment without revoking it."""

        request_id = _canonical_uuid(request_id)
        pairing_id = _canonical_uuid(pairing_id)
        reporting_node_id = _bounded_text(
            reporting_node_id,
            maximum=_MAX_TEXT_BYTES,
        )
        credential_generation = _bounded_generation(credential_generation)
        self._require_initialized()
        return await self._mutate(
            lambda: self._self_disable_enrollment(
                request_id=request_id,
                pairing_id=pairing_id,
                reporting_node_id=reporting_node_id,
                credential_generation=credential_generation,
            )
        )

    async def self_revoke_enrollment(
        self,
        *,
        request_id: str,
        pairing_id: str,
        reporting_node_id: str,
        credential_generation: int,
        management_bearer_verifier: str,
    ) -> EnrollmentRecord:
        """Revoke one exact active enrollment and retain only a replay proof."""

        request_id = _canonical_uuid(request_id)
        pairing_id = _canonical_uuid(pairing_id)
        reporting_node_id = _bounded_text(
            reporting_node_id,
            maximum=_MAX_TEXT_BYTES,
        )
        credential_generation = _bounded_generation(credential_generation)
        management_bearer_verifier = _bounded_verifier(
            management_bearer_verifier
        )
        self._require_initialized()
        return await self._mutate(
            lambda: self._self_revoke_enrollment(
                request_id=request_id,
                pairing_id=pairing_id,
                reporting_node_id=reporting_node_id,
                credential_generation=credential_generation,
                management_bearer_verifier=management_bearer_verifier,
            )
        )

    async def self_revoke_replay(
        self,
        *,
        request_id: str,
        pairing_id: str,
        reporting_node_id: str,
        credential_generation: int,
        management_bearer_verifier: str,
    ) -> EnrollmentRecord | None:
        """Authenticate only an exact completed self-revoke retry.

        This proof cannot authorize a new mutation.  It exists solely because
        the first successful revoke invalidates and removes the live credential
        bundle before an ambiguously lost HTTP response can be retried.
        """

        request_id = _canonical_uuid(request_id)
        pairing_id = _canonical_uuid(pairing_id)
        reporting_node_id = _bounded_text(
            reporting_node_id,
            maximum=_MAX_TEXT_BYTES,
        )
        credential_generation = _bounded_generation(credential_generation)
        management_bearer_verifier = _bounded_verifier(
            management_bearer_verifier
        )
        self._require_initialized()
        digest = _request_digest(
            "self_revoke_enrollment",
            {
                "request_id": request_id,
                "pairing_id": pairing_id,
                "reporting_node_id": reporting_node_id,
                "credential_generation": credential_generation,
            },
        )
        return await self._run_sync(
            lambda: self._self_revoke_replay_sync(
                request_id,
                pairing_id,
                reporting_node_id,
                credential_generation,
                digest,
                management_bearer_verifier,
            )
        )

    async def expire_invitations(self) -> int:
        self._require_initialized()
        return await self._mutate(self._expire_invitations)

    async def invitation(self, invitation_id: str) -> InvitationRecord | None:
        self._require_initialized()
        invitation_id = _canonical_uuid(invitation_id)
        return await self._run_sync(
            lambda: self._invitation_record_sync(invitation_id)
        )

    async def claim(self, claim_id: str) -> ClaimRecord | None:
        self._require_initialized()
        claim_id = _canonical_uuid(claim_id)
        return await self._run_sync(lambda: self._claim_record_sync(claim_id))

    async def pending_claims(self, *, limit: int = 100) -> tuple[ClaimRecord, ...]:
        """Return a bounded admin-safe view with no locator or secret refs."""

        self._require_initialized()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 1000
        ):
            raise PairingStoreValidationError("pairing_invalid_request")
        return await self._run_sync(lambda: self._pending_claims_sync(limit))

    async def enrollment(self, pairing_id: str) -> EnrollmentRecord | None:
        self._require_initialized()
        pairing_id = _canonical_uuid(pairing_id)
        return await self._run_sync(
            lambda: self._enrollment_record_sync(pairing_id)
        )

    async def enrollments(self) -> tuple[EnrollmentRecord, ...]:
        self._require_initialized()
        return await self._run_sync(self._enrollment_records_sync)

    async def enrollment_binding(
        self,
        pairing_id: str,
    ) -> EnrollmentBinding | None:
        self._require_initialized()
        pairing_id = _canonical_uuid(pairing_id)
        return await self._run_sync(
            lambda: self._enrollment_binding_sync(pairing_id)
        )

    async def candidate_binding(
        self,
        pairing_id: str,
        generation: int,
    ) -> EnrollmentBinding | None:
        """Return opaque refs for the exact pending activation candidate.

        This internal lookup deliberately excludes active, revoked, failed, and
        superseded generations. It lets the Hub resume candidate
        acknowledgement and probes after restart without making a pending
        enrollment routable or exposing any secret value.
        """

        self._require_initialized()
        pairing_id = _canonical_uuid(pairing_id)
        generation = _bounded_generation(generation)
        return await self._run_sync(
            lambda: self._candidate_binding_sync(pairing_id, generation)
        )

    async def active_binding(
        self,
        pairing_id: str,
        generation: int,
    ) -> EnrollmentBinding | None:
        """Return opaque refs for an exact active credential generation.

        Unlike :meth:`enrollment_binding`, this internal recovery lookup does
        not require Hub enablement. It permits an activation acknowledgement
        whose response was lost to authenticate an exact retry without making
        a deliberately disabled enrollment routable.
        """

        self._require_initialized()
        pairing_id = _canonical_uuid(pairing_id)
        generation = _bounded_generation(generation)
        return await self._run_sync(
            lambda: self._active_binding_sync(pairing_id, generation)
        )

    async def reconcile(self) -> ReconciliationReport:
        self._require_initialized()
        return await self._mutate(self._reconcile)

    async def _mutate(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        async with self._mutation_lock:
            worker = asyncio.create_task(operation())
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                try:
                    await worker
                except BaseException:
                    pass
                raise

    async def _run_sync(self, operation: Callable[[], _T]) -> _T:
        async with self._io_lock:
            worker = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                try:
                    await worker
                except BaseException:
                    pass
                raise

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise PairingStoreValidationError("pairing_store_not_initialized")

    async def _issue_invitation(
        self,
        request: InvitationRequest,
    ) -> InvitationIssue:
        digest = _request_digest(
            "issue_invitation",
            {
                "request_id": request.request_id,
                "intent": request.intent,
                "expected_platform": request.expected_platform,
                "expected_reporting_node_id": request.expected_reporting_node_id,
                "transport": request.transport,
                "service_class": request.service_class,
                "expires_in_seconds": float(request.expires_in_seconds),
            },
        )
        existing = await self._run_sync(
            lambda: self._idempotent_invitation_sync(request.request_id, digest)
        )
        if existing is not None:
            return await self._resume_invitation_issue(request, existing)

        now = _finite_timestamp(self._clock())
        invitation_id = str(uuid.uuid4())
        pairing_id = str(uuid.uuid4())
        invitation_ref = str(uuid.uuid4())
        locator_ref = str(uuid.uuid4())
        expires_at = now + request.expires_in_seconds
        await self._run_sync(
            lambda: self._insert_preparing_invitation_sync(
                request,
                digest=digest,
                invitation_id=invitation_id,
                pairing_id=pairing_id,
                invitation_ref=invitation_ref,
                locator_ref=locator_ref,
                now=now,
                expires_at=expires_at,
            )
        )
        pairing_secret = secrets.token_urlsafe(32)
        material = PairingMaterial(
            pairing_id=pairing_id,
            invitation_id=invitation_id,
            invitation=PrivateValue(
                value_ref=invitation_ref,
                value=pairing_secret,
            ),
            locator=PrivateValue(value_ref=locator_ref, value=request.locator),
        )
        try:
            stored = await self._secret_store.create_pairing_material(material)
        except SecretStoreError:
            # A retry first checks for an ambiguously committed private write.
            raise PairingStoreIntegrityError("pairing_secret_store_failure") from None
        if stored != material:
            raise PairingStoreIntegrityError("pairing_secret_store_failure")
        await self._run_sync(
            lambda: self._finalize_invitation_issue_sync(invitation_id)
        )
        return InvitationIssue(
            invitation_id=invitation_id,
            pairing_secret=pairing_secret,
            expires_at=expires_at,
            state="issued",
        )

    async def _resume_invitation_issue(
        self,
        request: InvitationRequest,
        invitation: dict[str, object],
    ) -> InvitationIssue:
        state = str(invitation["state"])
        if state in _INVITATION_TERMINAL:
            raise PairingStoreTerminalError("pairing_invitation_terminal")
        try:
            material = await self._secret_store.load_pairing_material(
                str(invitation["pairing_id"]),
                str(invitation["invitation_id"]),
            )
        except SecretStoreError:
            raise PairingStoreIntegrityError("pairing_secret_store_failure") from None
        if material is None:
            if state != "preparing":
                await self._run_sync(
                    lambda: self._mark_invitation_failed_sync(
                        str(invitation["invitation_id"]),
                        "private_material_missing",
                    )
                )
                raise PairingStoreIntegrityError(
                    "pairing_reconciliation_failed"
                )
            # No secret was ever returned from a preparing operation, so an
            # exact request may safely finish creating its original references.
            material = PairingMaterial(
                pairing_id=str(invitation["pairing_id"]),
                invitation_id=str(invitation["invitation_id"]),
                invitation=PrivateValue(
                    value_ref=str(invitation["invitation_ref"]),
                    value=secrets.token_urlsafe(32),
                ),
                locator=PrivateValue(
                    value_ref=str(invitation["locator_ref"]),
                    value=request.locator,
                ),
            )
            try:
                material = await self._secret_store.create_pairing_material(material)
            except SecretStoreError:
                raise PairingStoreIntegrityError(
                    "pairing_secret_store_failure"
                ) from None
        if (
            material.invitation.value_ref != invitation["invitation_ref"]
            or material.locator.value_ref != invitation["locator_ref"]
        ):
            raise PairingStoreIntegrityError("pairing_reconciliation_failed")
        locator_matches = await self._secret_store.verify_pairing_material(
            material.pairing_id,
            material.invitation_id,
            "locator",
            material.locator.value_ref,
            request.locator,
        )
        if not locator_matches:
            raise PairingStoreConflictError("idempotency_conflict")
        if state == "preparing":
            if float(invitation["expires_at"]) <= _finite_timestamp(self._clock()):
                await self._run_sync(
                    lambda: self._expire_one_sync(material.invitation_id)
                )
                await self._delete_all_pairing_material(material)
                raise PairingStoreTerminalError("pairing_invitation_expired")
            await self._run_sync(
                lambda: self._finalize_invitation_issue_sync(
                    material.invitation_id
                )
            )
        return InvitationIssue(
            invitation_id=material.invitation_id,
            pairing_secret=material.invitation.value,
            expires_at=float(invitation["expires_at"]),
            state="issued" if state == "preparing" else state,  # type: ignore[arg-type]
        )

    async def _claim_invitation(self, request: ClaimRequest) -> ClaimRecord:
        digest = _request_digest(
            "claim_invitation",
            {
                "request_id": request.request_id,
                "invitation_id": request.invitation_id,
                "display_name": request.display_name,
                "reporting_node_id": request.reporting_node_id,
                "service_version": request.service_version,
                "platform": request.platform,
                "protocol_minimum": request.protocol_minimum,
                "protocol_maximum": request.protocol_maximum,
            },
        )
        now = _finite_timestamp(self._clock())
        context = await self._run_sync(
            lambda: self._claim_context_sync(request, digest, now)
        )
        if context["state"] == "expired":
            await self._delete_context_material(context)
            raise PairingStoreTerminalError("pairing_invitation_expired")
        if context["state"] in _INVITATION_TERMINAL:
            raise PairingStoreTerminalError("pairing_invitation_terminal")
        if context.get("claimed_by_other"):
            raise PairingStoreTerminalError("pairing_invitation_claimed")

        try:
            secret_ok = await self._secret_store.verify_pairing_material(
                str(context["pairing_id"]),
                request.invitation_id,
                "invitation",
                str(context["invitation_ref"]),
                request.pairing_secret,
            )
            locator_ok = await self._secret_store.verify_pairing_material(
                str(context["pairing_id"]),
                request.invitation_id,
                "locator",
                str(context["locator_ref"]),
                request.locator,
            )
        except SecretStoreError:
            await self._run_sync(
                lambda: self._mark_invitation_failed_sync(
                    request.invitation_id,
                    "secret_store_failure",
                )
            )
            raise PairingStoreIntegrityError("pairing_secret_store_failure") from None
        if not (secret_ok and locator_ok):
            failed_state = await self._run_sync(
                lambda: self._record_failed_attempt_sync(
                    request.invitation_id,
                    _finite_timestamp(self._clock()),
                )
            )
            if failed_state == "failed":
                await self._delete_context_material(context)
                raise PairingStoreTerminalError(
                    "pairing_attempt_budget_exhausted"
                )
            raise PairingStoreTerminalError("pairing_claim_rejected")

        claim_id = str(context.get("claim_id") or uuid.uuid4())
        record = await self._run_sync(
            lambda: self._commit_claim_sync(
                request,
                digest=digest,
                claim_id=claim_id,
                now=_finite_timestamp(self._clock()),
            )
        )
        return record

    async def _approve_claim(self, request: ApprovalRequest) -> EnrollmentRecord:
        digest = _request_digest(
            "approve_claim",
            {
                "request_id": request.request_id,
                "claim_id": request.claim_id,
                "service_class": request.service_class,
                "hub_enabled": request.hub_enabled,
            },
        )
        context = await self._run_sync(
            lambda: self._approval_context_sync(request, digest)
        )
        try:
            locator_ok = await self._secret_store.verify_pairing_material(
                str(context["pairing_id"]),
                str(context["invitation_id"]),
                "locator",
                str(context["locator_ref"]),
                request.locator,
            )
        except SecretStoreError:
            raise PairingStoreIntegrityError("pairing_secret_store_failure") from None
        if not locator_ok:
            raise PairingStoreConflictError("pairing_locator_mismatch")
        for other in await self._run_sync(
            lambda: self._other_locator_bindings_sync(
                str(context["pairing_id"])
            )
        ):
            try:
                duplicate = await self._secret_store.verify_pairing_material(
                    str(other["pairing_id"]),
                    str(other["invitation_id"]),
                    "locator",
                    str(other["locator_ref"]),
                    request.locator,
                )
            except SecretStoreError:
                await self._run_sync(
                    lambda other_pairing=str(other["pairing_id"]):
                    self._fail_enrollment_closed_sync(
                        other_pairing,
                        "secret_reconciliation_failed",
                    )
                )
                continue
            if duplicate:
                raise PairingStoreConflictError("pairing_duplicate_locator")
        return await self._run_sync(
            lambda: self._commit_approval_sync(request, digest=digest)
        )

    async def _approve_claim_with_presence(
        self,
        request: PresenceApprovalRequest,
    ) -> EnrollmentRecord:
        """Approve using the six-digit code displayed by the requesting Mac.

        The PIN is derived from the existing high-entropy invitation secret;
        it is never a credential and requires the separately authenticated Hub
        administrator. Exact approval replay skips PIN verification after the
        one-time invitation value has been retired.
        """

        digest = _request_digest(
            "approve_claim",
            {
                "request_id": request.request_id,
                "claim_id": request.claim_id,
                "service_class": request.service_class,
                "hub_enabled": request.hub_enabled,
            },
        )
        approval = ApprovalRequest(
            request_id=request.request_id,
            claim_id=request.claim_id,
            locator="presence-locator-loaded-below",
            service_class=request.service_class,
            hub_enabled=request.hub_enabled,
        )
        context = await self._run_sync(
            lambda: self._approval_context_sync(approval, digest)
        )
        try:
            if bool(context.get("idempotent_replay")):
                locator = await self._secret_store.load_locator(
                    str(context["pairing_id"]),
                    str(context["locator_ref"]),
                )
            else:
                material = await self._secret_store.load_pairing_material(
                    str(context["pairing_id"]),
                    str(context["invitation_id"]),
                )
                if material is None or (
                    material.invitation.value_ref != context["invitation_ref"]
                    or material.locator.value_ref != context["locator_ref"]
                ):
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                expected_pin = pairing_presence_pin(material.invitation.value)
                if not hmac.compare_digest(expected_pin, request.presence_pin):
                    failed_state = await self._run_sync(
                        lambda: self._record_failed_attempt_sync(
                            str(context["invitation_id"]),
                            _finite_timestamp(self._clock()),
                        )
                    )
                    if failed_state == "failed":
                        await self._delete_context_material(context)
                        raise PairingStoreTerminalError(
                            "pairing_attempt_budget_exhausted"
                        )
                    raise PairingStoreTerminalError(
                        "pairing_presence_pin_rejected"
                    )
                locator = material.locator.value
        except SecretStoreError:
            raise PairingStoreIntegrityError("pairing_secret_store_failure") from None

        return await self._approve_claim(
            ApprovalRequest(
                request_id=request.request_id,
                claim_id=request.claim_id,
                locator=locator,
                service_class=request.service_class,
                hub_enabled=request.hub_enabled,
            )
        )

    async def _reject_claim(self, *, request_id: str, claim_id: str) -> bool:
        digest = _request_digest(
            "reject_claim",
            {"request_id": request_id, "claim_id": claim_id},
        )
        context = await self._run_sync(
            lambda: self._reject_claim_sync(request_id, claim_id, digest)
        )
        try:
            await self._secret_store.delete_pairing_material(
                str(context["pairing_id"]),
                str(context["invitation_id"]),
            )
        except SecretStoreError:
            await self._run_sync(
                lambda: self._mark_invitation_failed_sync(
                    str(context["invitation_id"]),
                    "secret_cleanup_pending",
                    preserve_state=True,
                )
            )
        return True

    async def _provision_claim(
        self,
        request: ProvisionRequest,
    ) -> ProvisioningRecord:
        digest = _request_digest(
            "provision_claim",
            {"request_id": request.request_id, "claim_id": request.claim_id},
        )
        auth_context = await self._run_sync(
            lambda: self._provision_auth_context_sync(request, digest)
        )
        try:
            secret_ok = await self._secret_store.verify_pairing_material(
                str(auth_context["pairing_id"]),
                str(auth_context["invitation_id"]),
                "invitation",
                str(auth_context["invitation_ref"]),
                request.pairing_secret,
            )
        except SecretStoreError:
            await self._run_sync(
                lambda: self._fail_enrollment_closed_sync(
                    str(auth_context["pairing_id"]),
                    "secret_store_failure",
                )
            )
            raise PairingStoreIntegrityError("pairing_secret_store_failure") from None
        if not secret_ok:
            failed_state = await self._run_sync(
                lambda: self._record_failed_attempt_sync(
                    str(auth_context["invitation_id"]),
                    _finite_timestamp(self._clock()),
                )
            )
            if failed_state == "failed":
                await self._run_sync(
                    lambda: self._fail_enrollment_closed_sync(
                        str(auth_context["pairing_id"]),
                        "attempt_budget_exhausted",
                    )
                )
                await self._delete_context_material(auth_context)
                raise PairingStoreTerminalError(
                    "pairing_attempt_budget_exhausted"
                )
            raise PairingStoreTerminalError("pairing_claim_rejected")
        refs = (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
        context = await self._run_sync(
            lambda: self._prepare_provision_sync(request, digest, refs)
        )
        pairing_id = str(context["pairing_id"])
        generation = int(context["generation"])
        try:
            bundle = await self._secret_store.load_bundle(pairing_id, generation)
        except SecretStoreError:
            await self._run_sync(
                lambda: self._fail_enrollment_closed_sync(
                    pairing_id,
                    "secret_store_failure",
                )
            )
            raise PairingStoreIntegrityError("pairing_secret_store_failure") from None
        expected_refs = (
            str(context["snapshot_ref"]),
            str(context["dispatch_ref"]),
            str(context["management_ref"]),
        )
        if bundle is None:
            if context["generation_state"] != "allocating":
                await self._run_sync(
                    lambda: self._fail_enrollment_closed_sync(
                        pairing_id,
                        "secret_reconciliation_failed",
                    )
                )
                raise PairingStoreIntegrityError(
                    "pairing_reconciliation_failed"
                )
            values: set[str] = set()
            while len(values) < 3:
                values.add(secrets.token_urlsafe(32))
            snapshot_secret, dispatch_secret, management_secret = tuple(values)
            bundle = CredentialBundle(
                pairing_id=pairing_id,
                generation=generation,
                snapshot=CredentialSecret(expected_refs[0], snapshot_secret),
                dispatch=CredentialSecret(expected_refs[1], dispatch_secret),
                management=CredentialSecret(expected_refs[2], management_secret),
            )
            try:
                bundle = await self._secret_store.create_bundle(bundle)
            except SecretStoreError:
                await self._run_sync(
                    lambda: self._fail_enrollment_closed_sync(
                        pairing_id,
                        "secret_store_failure",
                    )
                )
                raise PairingStoreIntegrityError(
                    "pairing_secret_store_failure"
                ) from None
        if (
            bundle.snapshot.secret_ref,
            bundle.dispatch.secret_ref,
            bundle.management.secret_ref,
        ) != expected_refs:
            await self._run_sync(
                lambda: self._fail_enrollment_closed_sync(
                    pairing_id,
                    "secret_reconciliation_failed",
                )
            )
            raise PairingStoreIntegrityError("pairing_reconciliation_failed")
        await self._run_sync(
            lambda: self._finalize_provision_sync(pairing_id, generation)
        )
        return ProvisioningRecord(
            claim_id=request.claim_id,
            pairing_id=pairing_id,
            reporting_node_id=str(context["reporting_node_id"]),
            credential_generation=generation,
            credentials=bundle,
        )

    async def _mark_activating(
        self,
        *,
        request_id: str,
        pairing_id: str,
        generation: int,
    ) -> EnrollmentRecord:
        digest = _request_digest(
            "mark_activating",
            {
                "request_id": request_id,
                "pairing_id": pairing_id,
                "generation": generation,
            },
        )
        return await self._run_sync(
            lambda: self._mark_activating_sync(
                request_id,
                pairing_id,
                generation,
                digest,
            )
        )

    async def _activate_enrollment(
        self,
        *,
        request_id: str,
        pairing_id: str,
        generation: int,
    ) -> EnrollmentRecord:
        digest = _request_digest(
            "activate_enrollment",
            {
                "request_id": request_id,
                "pairing_id": pairing_id,
                "generation": generation,
            },
        )
        record, invitation_id = await self._run_sync(
            lambda: self._activate_enrollment_sync(
                request_id,
                pairing_id,
                generation,
                digest,
            )
        )
        try:
            await self._secret_store.delete_invitation_material(
                pairing_id,
                invitation_id,
            )
        except SecretStoreError:
            await self._run_sync(
                lambda: self._set_enrollment_failure_sync(
                    pairing_id,
                    "secret_cleanup_pending",
                )
            )
            refreshed = await self.enrollment(pairing_id)
            if refreshed is not None:
                return refreshed
        return record

    async def _set_hub_enabled(
        self,
        *,
        request_id: str,
        pairing_id: str,
        enabled: bool,
    ) -> EnrollmentRecord:
        digest = _request_digest(
            "set_hub_enabled",
            {
                "request_id": request_id,
                "pairing_id": pairing_id,
                "enabled": enabled,
            },
        )
        return await self._run_sync(
            lambda: self._set_hub_enabled_sync(
                request_id,
                pairing_id,
                enabled,
                digest,
            )
        )

    async def _self_disable_enrollment(
        self,
        *,
        request_id: str,
        pairing_id: str,
        reporting_node_id: str,
        credential_generation: int,
    ) -> EnrollmentRecord:
        digest = _request_digest(
            "self_disable_enrollment",
            {
                "request_id": request_id,
                "pairing_id": pairing_id,
                "reporting_node_id": reporting_node_id,
                "credential_generation": credential_generation,
            },
        )
        return await self._run_sync(
            lambda: self._self_disable_enrollment_sync(
                request_id,
                pairing_id,
                reporting_node_id,
                credential_generation,
                digest,
            )
        )

    async def _revoke_enrollment(
        self,
        *,
        request_id: str,
        pairing_id: str,
    ) -> EnrollmentRecord:
        digest = _request_digest(
            "revoke_enrollment",
            {"request_id": request_id, "pairing_id": pairing_id},
        )
        record, cleanup = await self._run_sync(
            lambda: self._revoke_enrollment_sync(
                request_id,
                pairing_id,
                digest,
            )
        )
        return await self._finish_revocation_cleanup(record, cleanup)

    async def _self_revoke_enrollment(
        self,
        *,
        request_id: str,
        pairing_id: str,
        reporting_node_id: str,
        credential_generation: int,
        management_bearer_verifier: str,
    ) -> EnrollmentRecord:
        digest = _request_digest(
            "self_revoke_enrollment",
            {
                "request_id": request_id,
                "pairing_id": pairing_id,
                "reporting_node_id": reporting_node_id,
                "credential_generation": credential_generation,
            },
        )
        record, cleanup = await self._run_sync(
            lambda: self._self_revoke_enrollment_sync(
                request_id,
                pairing_id,
                reporting_node_id,
                credential_generation,
                digest,
                management_bearer_verifier,
            )
        )
        return await self._finish_revocation_cleanup(record, cleanup)

    async def _finish_revocation_cleanup(
        self,
        record: EnrollmentRecord,
        cleanup: dict[str, object],
    ) -> EnrollmentRecord:
        cleanup_failed = False
        generations = cleanup["generations"]
        if not isinstance(generations, list):
            raise PairingStoreIntegrityError("pairing_reconciliation_failed")
        for generation in generations:
            if not isinstance(generation, int):
                raise PairingStoreIntegrityError("pairing_reconciliation_failed")
            try:
                await self._secret_store.delete_bundle(
                    record.pairing_id,
                    generation,
                )
            except SecretStoreError:
                cleanup_failed = True
        invitation_id = cleanup["invitation_id"]
        if not isinstance(invitation_id, str):
            raise PairingStoreIntegrityError("pairing_reconciliation_failed")
        try:
            await self._secret_store.delete_pairing_material(
                record.pairing_id,
                invitation_id,
            )
        except SecretStoreError:
            cleanup_failed = True
        await self._run_sync(
            lambda: self._set_enrollment_failure_sync(
                record.pairing_id,
                "secret_cleanup_pending" if cleanup_failed else None,
            )
        )
        refreshed = await self.enrollment(record.pairing_id)
        return record if refreshed is None else refreshed

    async def _expire_invitations(self) -> int:
        expired = await self._run_sync(
            lambda: self._expire_due_sync(_finite_timestamp(self._clock()))
        )
        for context in expired:
            await self._delete_context_material(context)
        return len(expired)

    async def _reconcile(self) -> ReconciliationReport:
        now = _finite_timestamp(self._clock())
        expired_contexts, preparing, allocating, enrollment_contexts, cleanup = (
            await self._run_sync(lambda: self._reconciliation_snapshot_sync(now))
        )
        report = ReconciliationReport(expired=len(expired_contexts))
        cleaned = 0
        finalized_invitations = 0
        finalized_generations = 0
        failed_closed = 0

        for context in expired_contexts:
            if await self._delete_context_material(context):
                cleaned += 1

        for context in preparing:
            try:
                material = await self._secret_store.load_pairing_material(
                    context["pairing_id"],
                    context["invitation_id"],
                )
            except SecretStoreError:
                material = None
            if material is None or (
                material.invitation.value_ref != context["invitation_ref"]
                or material.locator.value_ref != context["locator_ref"]
            ):
                await self._run_sync(
                    lambda invitation_id=context["invitation_id"]:
                    self._mark_invitation_failed_sync(
                        invitation_id,
                        "private_material_missing",
                    )
                )
                failed_closed += 1
            else:
                await self._run_sync(
                    lambda invitation_id=context["invitation_id"]:
                    self._finalize_invitation_issue_sync(invitation_id)
                )
                finalized_invitations += 1

        for context in allocating:
            try:
                bundle = await self._secret_store.load_bundle(
                    context["pairing_id"],
                    context["generation"],
                )
            except SecretStoreError:
                bundle = None
            expected = (
                context["snapshot_ref"],
                context["dispatch_ref"],
                context["management_ref"],
            )
            if bundle is None or (
                bundle.snapshot.secret_ref,
                bundle.dispatch.secret_ref,
                bundle.management.secret_ref,
            ) != expected:
                await self._run_sync(
                    lambda pairing_id=context["pairing_id"]:
                    self._fail_enrollment_closed_sync(
                        pairing_id,
                        "credential_material_missing",
                    )
                )
                failed_closed += 1
            else:
                await self._run_sync(
                    lambda pairing_id=context["pairing_id"], generation=context[
                        "generation"
                    ]: self._finalize_provision_sync(pairing_id, generation)
                )
                finalized_generations += 1

        for context in enrollment_contexts:
            failed = False
            try:
                await self._secret_store.load_locator(
                    context["pairing_id"],
                    context["locator_ref"],
                )
                bundle = await self._secret_store.load_bundle(
                    context["pairing_id"],
                    context["generation"],
                )
                failed = bundle is None or (
                    bundle.snapshot.secret_ref,
                    bundle.dispatch.secret_ref,
                    bundle.management.secret_ref,
                ) != (
                    context["snapshot_ref"],
                    context["dispatch_ref"],
                    context["management_ref"],
                )
            except SecretStoreError:
                failed = True
            if failed:
                await self._run_sync(
                    lambda pairing_id=context["pairing_id"]:
                    self._fail_enrollment_closed_sync(
                        pairing_id,
                        "secret_reconciliation_failed",
                    )
                )
                failed_closed += 1

        for context in cleanup:
            try:
                if context["mode"] == "invitation":
                    removed = await self._secret_store.delete_invitation_material(
                        context["pairing_id"],
                        context["invitation_id"],
                    )
                else:
                    removed = await self._secret_store.delete_pairing_material(
                        context["pairing_id"],
                        context["invitation_id"],
                    )
                    for generation in context["generations"]:
                        removed = (
                            await self._secret_store.delete_bundle(
                                context["pairing_id"],
                                generation,
                            )
                            or removed
                        )
                if removed:
                    cleaned += 1
                await self._run_sync(
                    lambda pairing_id=context.get("enrollment_pairing_id"):
                    self._clear_cleanup_failure_sync(pairing_id)
                )
            except SecretStoreError:
                if context.get("enrollment_pairing_id"):
                    await self._run_sync(
                        lambda pairing_id=context["enrollment_pairing_id"]:
                        self._set_enrollment_failure_sync(
                            pairing_id,
                            "secret_cleanup_pending",
                        )
                    )

        return ReconciliationReport(
            expired=report.expired,
            finalized_invitations=finalized_invitations,
            finalized_generations=finalized_generations,
            failed_closed=failed_closed,
            cleaned=cleaned,
        )

    async def _delete_context_material(self, context: dict[str, object]) -> bool:
        try:
            return await self._secret_store.delete_pairing_material(
                str(context["pairing_id"]),
                str(context["invitation_id"]),
            )
        except SecretStoreError:
            return False

    async def _delete_all_pairing_material(self, material: PairingMaterial) -> bool:
        try:
            return await self._secret_store.delete_pairing_material(
                material.pairing_id,
                material.invitation_id,
            )
        except SecretStoreError:
            return False

    def _initialize_sync(self) -> None:
        self._prepare_private_path()
        with self._connect() as conn:
            existing_tables = {
                str(row["name"])
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
            }
            allowed_tables = {
                "pairing_metadata",
                "invitations",
                "claims",
                "enrollments",
                "credential_generations",
                "idempotency",
                "management_revoke_replays",
            }
            if existing_tables - allowed_tables:
                raise PairingStoreIntegrityError(
                    "pairing_store_identity_mismatch"
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pairing_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    store_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invitations (
                    invitation_id TEXT PRIMARY KEY,
                    pairing_id TEXT NOT NULL UNIQUE,
                    intent TEXT NOT NULL CHECK (
                        intent IN ('new', 'adopt-static')
                    ),
                    expected_platform TEXT NOT NULL,
                    expected_reporting_node_id TEXT,
                    transport TEXT NOT NULL CHECK (
                        transport IN ('https', 'tailscale', 'trusted_lan_http')
                    ),
                    service_class TEXT NOT NULL CHECK (
                        service_class IN (
                            'primary', 'opportunistic', 'overflow'
                        )
                    ),
                    invitation_ref TEXT NOT NULL UNIQUE,
                    locator_ref TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'preparing', 'issued', 'claimed', 'approved',
                            'provisioning', 'activating', 'completed',
                            'expired', 'rejected', 'failed'
                        )
                    ),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    attempt_limit INTEGER NOT NULL,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    claim_id TEXT UNIQUE,
                    failure_code TEXT
                );
                CREATE INDEX IF NOT EXISTS invitations_state_expiry_idx
                    ON invitations(state, expires_at);
                CREATE TABLE IF NOT EXISTS claims (
                    claim_id TEXT PRIMARY KEY,
                    invitation_id TEXT NOT NULL UNIQUE REFERENCES invitations(
                        invitation_id
                    ),
                    platform TEXT NOT NULL,
                    service_version TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    reporting_node_id TEXT NOT NULL,
                    protocol_version INTEGER NOT NULL,
                    claimed_at REAL NOT NULL,
                    approved_at REAL
                );
                CREATE TABLE IF NOT EXISTS enrollments (
                    pairing_id TEXT PRIMARY KEY,
                    invitation_id TEXT NOT NULL UNIQUE REFERENCES invitations(
                        invitation_id
                    ),
                    claim_id TEXT NOT NULL UNIQUE REFERENCES claims(claim_id),
                    reporting_node_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    service_version TEXT NOT NULL,
                    protocol_version INTEGER NOT NULL,
                    service_class TEXT NOT NULL CHECK (
                        service_class IN (
                            'primary', 'opportunistic', 'overflow'
                        )
                    ),
                    lifecycle_state TEXT NOT NULL CHECK (
                        lifecycle_state IN ('pending', 'active', 'revoked')
                    ),
                    hub_enabled INTEGER NOT NULL CHECK (hub_enabled IN (0, 1)),
                    locator_ref TEXT NOT NULL UNIQUE,
                    credential_generation INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    revoked_at REAL,
                    failure_code TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS enrollments_reporting_active_idx
                    ON enrollments(reporting_node_id)
                    WHERE lifecycle_state != 'revoked';
                CREATE TABLE IF NOT EXISTS credential_generations (
                    pairing_id TEXT NOT NULL REFERENCES enrollments(pairing_id),
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'allocating', 'candidate', 'active', 'retiring',
                            'retired', 'revoked'
                        )
                    ),
                    snapshot_ref TEXT NOT NULL UNIQUE,
                    dispatch_ref TEXT NOT NULL UNIQUE,
                    management_ref TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    activated_at REAL,
                    failure_code TEXT,
                    PRIMARY KEY (pairing_id, generation)
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    request_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    result_id TEXT NOT NULL,
                    pairing_id TEXT,
                    generation INTEGER,
                    result_state TEXT NOT NULL,
                    result_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS management_revoke_replays (
                    request_id TEXT PRIMARY KEY REFERENCES idempotency(
                        request_id
                    ),
                    pairing_id TEXT NOT NULL REFERENCES enrollments(pairing_id),
                    reporting_node_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    bearer_verifier TEXT NOT NULL CHECK (
                        length(bearer_verifier) = 64
                    ),
                    created_at REAL NOT NULL
                );
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                metadata = conn.execute(
                    """
                    SELECT schema_version, store_id
                    FROM pairing_metadata
                    WHERE singleton=1
                    """
                ).fetchone()
                if metadata is None:
                    conn.execute(
                        """
                        INSERT INTO pairing_metadata(
                            singleton, schema_version, store_id
                        ) VALUES (1, ?, ?)
                        """,
                        (_SCHEMA_VERSION, self._store_id),
                    )
                elif (
                    metadata["schema_version"] != _SCHEMA_VERSION
                    or metadata["store_id"] != self._store_id
                ):
                    raise PairingStoreIntegrityError(
                        "pairing_store_identity_mismatch"
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _prepare_private_path(self) -> None:
        parent = self._path.parent
        try:
            parent_status = parent.lstat()
        except FileNotFoundError:
            try:
                parent.mkdir(mode=0o700, parents=True)
                os.chmod(parent, 0o700, follow_symlinks=False)
            except OSError as exc:
                raise PairingStoreIntegrityError(
                    "pairing_store_insecure_path"
                ) from exc
        else:
            _assert_private_directory(parent_status)
        _assert_private_directory(_safe_lstat(parent))
        try:
            database_status = self._path.lstat()
        except FileNotFoundError:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self._path, flags, 0o600)
            except OSError as exc:
                raise PairingStoreIntegrityError(
                    "pairing_store_insecure_path"
                ) from exc
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            database_status = _safe_lstat(self._path)
        _assert_private_file(database_status)

    def _connect(self) -> sqlite3.Connection:
        _assert_private_directory(_safe_lstat(self._path.parent))
        before = _safe_lstat(self._path)
        _assert_private_file(before)
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                self._path,
                timeout=10,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            after = _safe_lstat(self._path)
            _assert_private_file(after)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise PairingStoreIntegrityError("pairing_store_insecure_path")
            conn.execute("PRAGMA foreign_keys=ON")
            journal = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            secure_delete = conn.execute("PRAGMA secure_delete=ON").fetchone()[0]
            if str(journal).lower() != "delete" or int(secure_delete) != 1:
                raise PairingStoreIntegrityError("pairing_store_database_failure")
            conn.execute("PRAGMA busy_timeout=10000")
            return conn
        except PairingStoreError:
            if conn is not None:
                conn.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if conn is not None:
                conn.close()
            raise PairingStoreIntegrityError(
                "pairing_store_database_failure"
            ) from exc

    def _idempotent_invitation_sync(
        self,
        request_id: str,
        digest: str,
    ) -> dict[str, object] | None:
        with self._connect() as conn:
            operation = _check_idempotency(
                conn,
                request_id,
                "issue_invitation",
                digest,
            )
            if operation is None:
                return None
            row = conn.execute(
                "SELECT * FROM invitations WHERE invitation_id=?",
                (operation["result_id"],),
            ).fetchone()
        if row is None:
            raise PairingStoreIntegrityError("pairing_reconciliation_failed")
        return dict(row)

    def _insert_preparing_invitation_sync(
        self,
        request: InvitationRequest,
        *,
        digest: str,
        invitation_id: str,
        pairing_id: str,
        invitation_ref: str,
        locator_ref: str,
        now: float,
        expires_at: float,
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    """
                    SELECT count(*) FROM invitations
                    WHERE state NOT IN ('completed', 'expired', 'rejected', 'failed')
                    """
                ).fetchone()[0] >= _MAX_PENDING_INVITATIONS:
                    raise PairingStoreTerminalError(
                        "pairing_pending_limit_reached"
                    )
                existing = _check_idempotency(
                    conn,
                    request.request_id,
                    "issue_invitation",
                    digest,
                )
                if existing is not None:
                    raise PairingStoreConflictError("idempotency_conflict")
                conn.execute(
                    """
                    INSERT INTO invitations(
                        invitation_id, pairing_id, intent, expected_platform,
                        expected_reporting_node_id, transport, service_class,
                        invitation_ref, locator_ref, state, created_at,
                        expires_at, attempt_limit, failed_attempts,
                        claim_id, failure_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?, ?, 0,
                              NULL, NULL)
                    """,
                    (
                        invitation_id,
                        pairing_id,
                        request.intent,
                        request.expected_platform,
                        request.expected_reporting_node_id,
                        request.transport,
                        request.service_class,
                        invitation_ref,
                        locator_ref,
                        now,
                        expires_at,
                        _INVITATION_ATTEMPT_LIMIT,
                    ),
                )
                _insert_idempotency(
                    conn,
                    request_id=request.request_id,
                    operation="issue_invitation",
                    digest=digest,
                    result_id=invitation_id,
                    pairing_id=pairing_id,
                    generation=None,
                    result_state="preparing",
                    now=now,
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _finalize_invitation_issue_sync(self, invitation_id: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT state FROM invitations WHERE invitation_id=?",
                    (invitation_id,),
                ).fetchone()
                if row is None:
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                if row["state"] == "preparing":
                    conn.execute(
                        """
                        UPDATE invitations SET state='issued'
                        WHERE invitation_id=?
                        """,
                        (invitation_id,),
                    )
                    conn.execute(
                        """
                        UPDATE idempotency SET result_state='issued'
                        WHERE operation='issue_invitation' AND result_id=?
                        """,
                        (invitation_id,),
                    )
                elif row["state"] not in {
                    "issued",
                    "claimed",
                    "approved",
                    "provisioning",
                    "activating",
                    "completed",
                }:
                    raise PairingStoreTerminalError("pairing_invitation_terminal")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _mark_invitation_failed_sync(
        self,
        invitation_id: str,
        failure_code: str,
        *,
        preserve_state: bool = False,
    ) -> None:
        _validate_failure_code(failure_code)
        with self._connect() as conn:
            if preserve_state:
                conn.execute(
                    """
                    UPDATE invitations SET failure_code=?
                    WHERE invitation_id=?
                    """,
                    (failure_code, invitation_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE invitations SET state='failed', failure_code=?
                    WHERE invitation_id=? AND state != 'completed'
                    """,
                    (failure_code, invitation_id),
                )

    def _expire_one_sync(self, invitation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE invitations SET state='expired'
                WHERE invitation_id=? AND state IN ('preparing', 'issued', 'claimed')
                """,
                (invitation_id,),
            )

    def _expire_due_sync(self, now: float) -> list[dict[str, object]]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT invitation_id, pairing_id
                    FROM invitations
                    WHERE state IN ('preparing', 'issued', 'claimed')
                      AND expires_at <= ?
                    """,
                    (now,),
                ).fetchall()
                conn.execute(
                    """
                    UPDATE invitations SET state='expired'
                    WHERE state IN ('preparing', 'issued', 'claimed')
                      AND expires_at <= ?
                    """,
                    (now,),
                )
                conn.commit()
                return [dict(row) for row in rows]
            except BaseException:
                conn.rollback()
                raise

    def _claim_context_sync(
        self,
        request: ClaimRequest,
        digest: str,
        now: float,
    ) -> dict[str, object]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                invitation = conn.execute(
                    "SELECT * FROM invitations WHERE invitation_id=?",
                    (request.invitation_id,),
                ).fetchone()
                if invitation is None:
                    raise PairingStoreTerminalError("pairing_invitation_unknown")
                if (
                    invitation["state"] in {"preparing", "issued", "claimed"}
                    and invitation["expires_at"] <= now
                ):
                    conn.execute(
                        """
                        UPDATE invitations SET state='expired'
                        WHERE invitation_id=?
                        """,
                        (request.invitation_id,),
                    )
                    invitation = conn.execute(
                        "SELECT * FROM invitations WHERE invitation_id=?",
                        (request.invitation_id,),
                    ).fetchone()
                operation = _check_idempotency(
                    conn,
                    request.request_id,
                    "claim_invitation",
                    digest,
                )
                context = dict(invitation)
                if operation is not None:
                    claim = conn.execute(
                        "SELECT claim_id FROM claims WHERE claim_id=?",
                        (operation["result_id"],),
                    ).fetchone()
                    if claim is None:
                        raise PairingStoreIntegrityError(
                            "pairing_reconciliation_failed"
                        )
                    context["claim_id"] = claim["claim_id"]
                    context["claimed_by_other"] = False
                else:
                    context["claimed_by_other"] = bool(
                        invitation["claim_id"] is not None
                    )
                conn.commit()
                return context
            except BaseException:
                conn.rollback()
                raise

    def _record_failed_attempt_sync(
        self,
        invitation_id: str,
        now: float,
    ) -> str:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT state, expires_at, failed_attempts, attempt_limit
                    FROM invitations WHERE invitation_id=?
                    """,
                    (invitation_id,),
                ).fetchone()
                if row is None:
                    raise PairingStoreTerminalError("pairing_invitation_unknown")
                state = row["state"]
                if state not in {"issued", "claimed", "approved", "provisioning"}:
                    conn.commit()
                    return str(state)
                if row["expires_at"] <= now:
                    conn.execute(
                        "UPDATE invitations SET state='expired' WHERE invitation_id=?",
                        (invitation_id,),
                    )
                    conn.commit()
                    return "expired"
                attempts = row["failed_attempts"] + 1
                state = "failed" if attempts >= row["attempt_limit"] else state
                failure = (
                    "attempt_budget_exhausted" if state == "failed" else None
                )
                conn.execute(
                    """
                    UPDATE invitations
                    SET failed_attempts=?, state=?, failure_code=?
                    WHERE invitation_id=?
                    """,
                    (attempts, state, failure, invitation_id),
                )
                conn.commit()
                return str(state)
            except BaseException:
                conn.rollback()
                raise

    def _commit_claim_sync(
        self,
        request: ClaimRequest,
        *,
        digest: str,
        claim_id: str,
        now: float,
    ) -> ClaimRecord:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = _check_idempotency(
                    conn,
                    request.request_id,
                    "claim_invitation",
                    digest,
                )
                if operation is not None:
                    conn.commit()
                    record = self._claim_record_with_conn(conn, operation["result_id"])
                    if record is None:
                        raise PairingStoreIntegrityError(
                            "pairing_reconciliation_failed"
                        )
                    return record
                invitation = conn.execute(
                    "SELECT * FROM invitations WHERE invitation_id=?",
                    (request.invitation_id,),
                ).fetchone()
                if invitation is None or invitation["state"] != "issued":
                    raise PairingStoreTerminalError("pairing_invitation_claimed")
                if invitation["expires_at"] <= now:
                    conn.execute(
                        "UPDATE invitations SET state='expired' WHERE invitation_id=?",
                        (request.invitation_id,),
                    )
                    conn.commit()
                    raise PairingStoreTerminalError("pairing_invitation_expired")
                expected_reporting = invitation["expected_reporting_node_id"]
                if invitation["expected_platform"] != request.platform:
                    raise PairingStoreConflictError(
                        "pairing_platform_mismatch"
                    )
                if (
                    expected_reporting is not None
                    and expected_reporting != request.reporting_node_id
                ):
                    raise PairingStoreConflictError(
                        "pairing_reporting_identity_mismatch"
                    )
                conn.execute(
                    """
                    INSERT INTO claims(
                        claim_id, invitation_id, platform, service_version,
                        display_name, reporting_node_id, protocol_version,
                        claimed_at, approved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        claim_id,
                        request.invitation_id,
                        request.platform,
                        request.service_version,
                        request.display_name,
                        request.reporting_node_id,
                        _PAIRING_PROTOCOL_VERSION,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE invitations SET state='claimed', claim_id=?
                    WHERE invitation_id=?
                    """,
                    (claim_id, request.invitation_id),
                )
                _insert_idempotency(
                    conn,
                    request_id=request.request_id,
                    operation="claim_invitation",
                    digest=digest,
                    result_id=claim_id,
                    pairing_id=invitation["pairing_id"],
                    generation=None,
                    result_state="claimed",
                    now=now,
                )
                conn.commit()
                record = self._claim_record_with_conn(conn, claim_id)
                if record is None:
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                return record
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def _approval_context_sync(
        self,
        request: ApprovalRequest,
        digest: str,
    ) -> dict[str, object]:
        with self._connect() as conn:
            operation = _check_idempotency(
                conn,
                request.request_id,
                "approve_claim",
                digest,
            )
            row = conn.execute(
                """
                SELECT i.*, c.reporting_node_id, c.display_name,
                       c.platform, c.service_version, c.protocol_version
                FROM claims c
                JOIN invitations i ON i.invitation_id=c.invitation_id
                WHERE c.claim_id=?
                """,
                (request.claim_id,),
            ).fetchone()
        if row is None:
            raise PairingStoreTerminalError("pairing_claim_unknown")
        if operation is None and row["state"] != "claimed":
            raise PairingStoreTerminalError("pairing_claim_terminal")
        if request.service_class != row["service_class"]:
            raise PairingStoreConflictError("pairing_service_class_mismatch")
        context = dict(row)
        context["idempotent_replay"] = operation is not None
        return context

    def _other_locator_bindings_sync(
        self,
        pairing_id: str,
    ) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT pairing_id, invitation_id, locator_ref
                FROM enrollments
                WHERE lifecycle_state != 'revoked' AND pairing_id != ?
                ORDER BY created_at
                LIMIT ?
                """,
                (pairing_id, _MAX_PENDING_INVITATIONS),
            ).fetchall()
        return [dict(row) for row in rows]

    def _commit_approval_sync(
        self,
        request: ApprovalRequest,
        *,
        digest: str,
    ) -> EnrollmentRecord:
        now = _finite_timestamp(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = _check_idempotency(
                    conn,
                    request.request_id,
                    "approve_claim",
                    digest,
                )
                if operation is not None:
                    conn.commit()
                    record = self._approval_result_with_conn(
                        conn,
                        str(operation["pairing_id"]),
                        hub_enabled=request.hub_enabled,
                        approved_at=float(operation["result_at"]),
                    )
                    if record is None:
                        raise PairingStoreIntegrityError(
                            "pairing_reconciliation_failed"
                        )
                    return record
                row = conn.execute(
                    """
                    SELECT i.*, c.reporting_node_id, c.display_name,
                           c.platform, c.service_version, c.protocol_version
                    FROM claims c
                    JOIN invitations i ON i.invitation_id=c.invitation_id
                    WHERE c.claim_id=?
                    """,
                    (request.claim_id,),
                ).fetchone()
                if row is None or row["state"] != "claimed":
                    raise PairingStoreTerminalError("pairing_claim_terminal")
                if row["expires_at"] <= now:
                    conn.execute(
                        "UPDATE invitations SET state='expired' WHERE invitation_id=?",
                        (row["invitation_id"],),
                    )
                    conn.commit()
                    raise PairingStoreTerminalError("pairing_invitation_expired")
                if row["service_class"] != request.service_class:
                    raise PairingStoreConflictError(
                        "pairing_service_class_mismatch"
                    )
                duplicate = conn.execute(
                    """
                    SELECT 1 FROM enrollments
                    WHERE reporting_node_id=? AND lifecycle_state != 'revoked'
                    LIMIT 1
                    """,
                    (row["reporting_node_id"],),
                ).fetchone()
                if duplicate is not None:
                    raise PairingStoreConflictError(
                        "pairing_duplicate_reporting_identity"
                    )
                conn.execute(
                    """
                    INSERT INTO enrollments(
                        pairing_id, invitation_id, claim_id, reporting_node_id,
                        display_name, platform, service_version,
                        protocol_version, service_class, lifecycle_state,
                        hub_enabled, locator_ref, credential_generation,
                        created_at, updated_at, revoked_at, failure_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL,
                              ?, ?, NULL, NULL)
                    """,
                    (
                        row["pairing_id"],
                        row["invitation_id"],
                        request.claim_id,
                        row["reporting_node_id"],
                        row["display_name"],
                        row["platform"],
                        row["service_version"],
                        row["protocol_version"],
                        request.service_class,
                        int(request.hub_enabled),
                        row["locator_ref"],
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE claims SET approved_at=? WHERE claim_id=?
                    """,
                    (now, request.claim_id),
                )
                conn.execute(
                    """
                    UPDATE invitations SET state='approved'
                    WHERE invitation_id=?
                    """,
                    (row["invitation_id"],),
                )
                _insert_idempotency(
                    conn,
                    request_id=request.request_id,
                    operation="approve_claim",
                    digest=digest,
                    result_id=request.claim_id,
                    pairing_id=row["pairing_id"],
                    generation=None,
                    result_state="approved",
                    now=now,
                )
                conn.commit()
                record = self._approval_result_with_conn(
                    conn,
                    row["pairing_id"],
                    hub_enabled=request.hub_enabled,
                    approved_at=now,
                )
                if record is None:
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                return record
            except sqlite3.IntegrityError as exc:
                if conn.in_transaction:
                    conn.rollback()
                raise PairingStoreConflictError(
                    "pairing_enrollment_conflict"
                ) from exc
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def _reject_claim_sync(
        self,
        request_id: str,
        claim_id: str,
        digest: str,
    ) -> dict[str, object]:
        now = _finite_timestamp(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = _check_idempotency(
                    conn,
                    request_id,
                    "reject_claim",
                    digest,
                )
                row = conn.execute(
                    """
                    SELECT i.invitation_id, i.pairing_id, i.state
                    FROM claims c
                    JOIN invitations i ON i.invitation_id=c.invitation_id
                    WHERE c.claim_id=?
                    """,
                    (claim_id,),
                ).fetchone()
                if row is None:
                    raise PairingStoreTerminalError("pairing_claim_unknown")
                if operation is None:
                    if row["state"] != "claimed":
                        raise PairingStoreTerminalError("pairing_claim_terminal")
                    conn.execute(
                        """
                        UPDATE invitations SET state='rejected', failure_code=NULL
                        WHERE invitation_id=?
                        """,
                        (row["invitation_id"],),
                    )
                    _insert_idempotency(
                        conn,
                        request_id=request_id,
                        operation="reject_claim",
                        digest=digest,
                        result_id=claim_id,
                        pairing_id=row["pairing_id"],
                        generation=None,
                        result_state="rejected",
                        now=now,
                    )
                conn.commit()
                return dict(row)
            except BaseException:
                conn.rollback()
                raise

    def _prepare_provision_sync(
        self,
        request: ProvisionRequest,
        digest: str,
        references: tuple[str, str, str],
    ) -> dict[str, object]:
        now = _finite_timestamp(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = _check_idempotency(
                    conn,
                    request.request_id,
                    "provision_claim",
                    digest,
                )
                row = conn.execute(
                    """
                    SELECT i.invitation_id, i.pairing_id, i.state,
                           c.reporting_node_id
                    FROM claims c
                    JOIN invitations i ON i.invitation_id=c.invitation_id
                    WHERE c.claim_id=?
                    """,
                    (request.claim_id,),
                ).fetchone()
                if row is None:
                    raise PairingStoreTerminalError("pairing_claim_unknown")
                if operation is not None:
                    generation = conn.execute(
                        """
                        SELECT generation, state AS generation_state,
                               snapshot_ref, dispatch_ref, management_ref
                        FROM credential_generations
                        WHERE pairing_id=? AND generation=?
                        """,
                        (row["pairing_id"], operation["generation"]),
                    ).fetchone()
                    if generation is None:
                        raise PairingStoreIntegrityError(
                            "pairing_reconciliation_failed"
                        )
                    context = dict(row)
                    context.update(dict(generation))
                    conn.commit()
                    return context
                if row["state"] != "approved":
                    raise PairingStoreTerminalError("pairing_claim_terminal")
                if conn.execute(
                    """
                    SELECT 1 FROM credential_generations WHERE pairing_id=?
                    """,
                    (row["pairing_id"],),
                ).fetchone() is not None:
                    raise PairingStoreConflictError(
                        "pairing_already_provisioned"
                    )
                generation_number = 1
                conn.execute(
                    """
                    INSERT INTO credential_generations(
                        pairing_id, generation, state, snapshot_ref,
                        dispatch_ref, management_ref, created_at,
                        activated_at, failure_code
                    ) VALUES (?, ?, 'allocating', ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        row["pairing_id"],
                        generation_number,
                        references[0],
                        references[1],
                        references[2],
                        now,
                    ),
                )
                _insert_idempotency(
                    conn,
                    request_id=request.request_id,
                    operation="provision_claim",
                    digest=digest,
                    result_id=request.claim_id,
                    pairing_id=row["pairing_id"],
                    generation=generation_number,
                    result_state="allocating",
                    now=now,
                )
                context = dict(row)
                context.update(
                    {
                        "generation": generation_number,
                        "generation_state": "allocating",
                        "snapshot_ref": references[0],
                        "dispatch_ref": references[1],
                        "management_ref": references[2],
                    }
                )
                conn.commit()
                return context
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise PairingStoreConflictError(
                    "pairing_generation_conflict"
                ) from exc
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def _provision_auth_context_sync(
        self,
        request: ProvisionRequest,
        digest: str,
    ) -> dict[str, object]:
        with self._connect() as conn:
            operation = _check_idempotency(
                conn,
                request.request_id,
                "provision_claim",
                digest,
            )
            row = conn.execute(
                """
                SELECT i.invitation_id, i.pairing_id, i.invitation_ref,
                       i.state, i.failed_attempts, i.attempt_limit
                FROM claims c
                JOIN invitations i ON i.invitation_id=c.invitation_id
                WHERE c.claim_id=?
                """,
                (request.claim_id,),
            ).fetchone()
        if row is None:
            raise PairingStoreTerminalError("pairing_claim_unknown")
        if operation is None and row["state"] != "approved":
            raise PairingStoreTerminalError("pairing_claim_terminal")
        if operation is not None and row["state"] not in {
            "approved",
            "provisioning",
            "activating",
        }:
            raise PairingStoreTerminalError("pairing_claim_terminal")
        return dict(row)

    def _finalize_provision_sync(self, pairing_id: str, generation: int) -> None:
        now = _finite_timestamp(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT e.invitation_id, g.state
                    FROM credential_generations g
                    JOIN enrollments e ON e.pairing_id=g.pairing_id
                    WHERE g.pairing_id=? AND g.generation=?
                    """,
                    (pairing_id, generation),
                ).fetchone()
                if row is None:
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                if row["state"] == "allocating":
                    conn.execute(
                        """
                        UPDATE credential_generations
                        SET state='candidate', failure_code=NULL
                        WHERE pairing_id=? AND generation=?
                        """,
                        (pairing_id, generation),
                    )
                    conn.execute(
                        """
                        UPDATE enrollments
                        SET credential_generation=?, updated_at=?, failure_code=NULL
                        WHERE pairing_id=? AND lifecycle_state='pending'
                        """,
                        (generation, now, pairing_id),
                    )
                    conn.execute(
                        """
                        UPDATE invitations SET state='provisioning', failure_code=NULL
                        WHERE invitation_id=? AND state='approved'
                        """,
                        (row["invitation_id"],),
                    )
                    conn.execute(
                        """
                        UPDATE idempotency SET result_state='provisioning'
                        WHERE operation='provision_claim'
                          AND pairing_id=? AND generation=?
                        """,
                        (pairing_id, generation),
                    )
                elif row["state"] not in {"candidate", "active"}:
                    raise PairingStoreTerminalError("pairing_generation_terminal")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _mark_activating_sync(
        self,
        request_id: str,
        pairing_id: str,
        generation: int,
        digest: str,
    ) -> EnrollmentRecord:
        now = _finite_timestamp(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = _check_idempotency(
                    conn,
                    request_id,
                    "mark_activating",
                    digest,
                )
                enrollment = self._enrollment_record_with_conn(conn, pairing_id)
                if enrollment is None:
                    raise PairingStoreTerminalError("pairing_enrollment_unknown")
                if operation is None:
                    if enrollment.lifecycle_state != "pending":
                        raise PairingStoreTerminalError(
                            "pairing_enrollment_terminal"
                        )
                    row = conn.execute(
                        """
                        SELECT state FROM credential_generations
                        WHERE pairing_id=? AND generation=?
                        """,
                        (pairing_id, generation),
                    ).fetchone()
                    if row is None or row["state"] != "candidate":
                        raise PairingStoreTerminalError(
                            "pairing_generation_terminal"
                        )
                    conn.execute(
                        """
                        UPDATE invitations SET state='activating'
                        WHERE invitation_id=(
                            SELECT invitation_id FROM enrollments
                            WHERE pairing_id=?
                        ) AND state='provisioning'
                        """,
                        (pairing_id,),
                    )
                    conn.execute(
                        """
                        UPDATE enrollments SET updated_at=? WHERE pairing_id=?
                        """,
                        (now, pairing_id),
                    )
                    _insert_idempotency(
                        conn,
                        request_id=request_id,
                        operation="mark_activating",
                        digest=digest,
                        result_id=pairing_id,
                        pairing_id=pairing_id,
                        generation=generation,
                        result_state="activating",
                        now=now,
                    )
                conn.commit()
                record = self._enrollment_record_with_conn(conn, pairing_id)
                if record is None:
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                return record
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def _activate_enrollment_sync(
        self,
        request_id: str,
        pairing_id: str,
        generation: int,
        digest: str,
    ) -> tuple[EnrollmentRecord, str]:
        now = _finite_timestamp(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = _check_idempotency(
                    conn,
                    request_id,
                    "activate_enrollment",
                    digest,
                )
                row = conn.execute(
                    """
                    SELECT e.invitation_id, e.lifecycle_state, e.failure_code,
                           g.state AS generation_state
                    FROM enrollments e
                    JOIN credential_generations g
                      ON g.pairing_id=e.pairing_id
                     AND g.generation=?
                    WHERE e.pairing_id=?
                    """,
                    (generation, pairing_id),
                ).fetchone()
                if row is None:
                    raise PairingStoreTerminalError("pairing_enrollment_unknown")
                if operation is None:
                    if (
                        row["lifecycle_state"] != "pending"
                        or row["generation_state"] != "candidate"
                    ):
                        raise PairingStoreTerminalError(
                            "pairing_enrollment_terminal"
                        )
                    conn.execute(
                        """
                        UPDATE credential_generations
                        SET state='active', activated_at=?, failure_code=NULL
                        WHERE pairing_id=? AND generation=?
                        """,
                        (now, pairing_id, generation),
                    )
                    conn.execute(
                        """
                        UPDATE enrollments
                        SET lifecycle_state='active', credential_generation=?,
                            updated_at=?, failure_code=NULL
                        WHERE pairing_id=?
                        """,
                        (generation, now, pairing_id),
                    )
                    conn.execute(
                        """
                        UPDATE invitations
                        SET state='completed', failure_code=NULL
                        WHERE invitation_id=?
                        """,
                        (row["invitation_id"],),
                    )
                    _insert_idempotency(
                        conn,
                        request_id=request_id,
                        operation="activate_enrollment",
                        digest=digest,
                        result_id=pairing_id,
                        pairing_id=pairing_id,
                        generation=generation,
                        result_state="active",
                        now=now,
                    )
                conn.commit()
                record = self._enrollment_record_with_conn(conn, pairing_id)
                if record is None:
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                return record, str(row["invitation_id"])
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def _set_hub_enabled_sync(
        self,
        request_id: str,
        pairing_id: str,
        enabled: bool,
        digest: str,
    ) -> EnrollmentRecord:
        now = _finite_timestamp(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = _check_idempotency(
                    conn,
                    request_id,
                    "set_hub_enabled",
                    digest,
                )
                record = self._enrollment_record_with_conn(conn, pairing_id)
                if record is None:
                    raise PairingStoreTerminalError("pairing_enrollment_unknown")
                if operation is None:
                    if record.lifecycle_state != "active":
                        raise PairingStoreTerminalError(
                            "pairing_enrollment_terminal"
                        )
                    conn.execute(
                        """
                        UPDATE enrollments
                        SET hub_enabled=?, updated_at=?
                        WHERE pairing_id=?
                        """,
                        (int(enabled), now, pairing_id),
                    )
                    _insert_idempotency(
                        conn,
                        request_id=request_id,
                        operation="set_hub_enabled",
                        digest=digest,
                        result_id=pairing_id,
                        pairing_id=pairing_id,
                        generation=record.credential_generation,
                        result_state="active" if enabled else "disabled",
                        now=now,
                    )
                conn.commit()
                record = self._enrollment_record_with_conn(conn, pairing_id)
                if record is None:
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                return record
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def _self_disable_enrollment_sync(
        self,
        request_id: str,
        pairing_id: str,
        reporting_node_id: str,
        credential_generation: int,
        digest: str,
    ) -> EnrollmentRecord:
        now = _finite_timestamp(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = _check_idempotency(
                    conn,
                    request_id,
                    "self_disable_enrollment",
                    digest,
                )
                row = conn.execute(
                    """
                    SELECT e.lifecycle_state, e.reporting_node_id,
                           e.credential_generation, e.failure_code,
                           g.state AS generation_state,
                           g.failure_code AS generation_failure_code
                    FROM enrollments e
                    LEFT JOIN credential_generations g
                      ON g.pairing_id=e.pairing_id
                     AND g.generation=e.credential_generation
                    WHERE e.pairing_id=?
                    """,
                    (pairing_id,),
                ).fetchone()
                if row is None:
                    raise PairingStoreTerminalError(
                        "pairing_management_authentication_rejected"
                    )
                if not _active_management_row_matches(
                    row,
                    reporting_node_id=reporting_node_id,
                    credential_generation=credential_generation,
                ):
                    raise PairingStoreTerminalError(
                        "pairing_management_authentication_rejected"
                    )
                if operation is None:
                    changed = conn.execute(
                        """
                        UPDATE enrollments
                        SET hub_enabled=0, updated_at=?
                        WHERE pairing_id=?
                          AND reporting_node_id=?
                          AND credential_generation=?
                          AND lifecycle_state='active'
                          AND failure_code IS NULL
                        """,
                        (
                            now,
                            pairing_id,
                            reporting_node_id,
                            credential_generation,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise PairingStoreTerminalError(
                            "pairing_management_authentication_rejected"
                        )
                    _insert_idempotency(
                        conn,
                        request_id=request_id,
                        operation="self_disable_enrollment",
                        digest=digest,
                        result_id=pairing_id,
                        pairing_id=pairing_id,
                        generation=credential_generation,
                        result_state="disabled",
                        now=now,
                    )
                conn.commit()
                record = self._enrollment_record_with_conn(conn, pairing_id)
                if record is None:
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                return record
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def _self_revoke_enrollment_sync(
        self,
        request_id: str,
        pairing_id: str,
        reporting_node_id: str,
        credential_generation: int,
        digest: str,
        management_bearer_verifier: str,
    ) -> tuple[EnrollmentRecord, dict[str, object]]:
        now = _finite_timestamp(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = _check_idempotency(
                    conn,
                    request_id,
                    "self_revoke_enrollment",
                    digest,
                )
                row = conn.execute(
                    """
                    SELECT e.invitation_id, e.lifecycle_state,
                           e.reporting_node_id, e.credential_generation,
                           e.failure_code, g.state AS generation_state,
                           g.failure_code AS generation_failure_code
                    FROM enrollments e
                    LEFT JOIN credential_generations g
                      ON g.pairing_id=e.pairing_id
                     AND g.generation=e.credential_generation
                    WHERE e.pairing_id=?
                    """,
                    (pairing_id,),
                ).fetchone()
                if row is None:
                    raise PairingStoreTerminalError(
                        "pairing_management_authentication_rejected"
                    )
                if operation is None:
                    if not _active_management_row_matches(
                        row,
                        reporting_node_id=reporting_node_id,
                        credential_generation=credential_generation,
                    ):
                        raise PairingStoreTerminalError(
                            "pairing_management_authentication_rejected"
                        )
                    changed = conn.execute(
                        """
                        UPDATE enrollments
                        SET lifecycle_state='revoked', hub_enabled=0,
                            updated_at=?, revoked_at=?,
                            failure_code='secret_cleanup_pending'
                        WHERE pairing_id=?
                          AND reporting_node_id=?
                          AND credential_generation=?
                          AND lifecycle_state='active'
                          AND failure_code IS NULL
                        """,
                        (
                            now,
                            now,
                            pairing_id,
                            reporting_node_id,
                            credential_generation,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise PairingStoreTerminalError(
                            "pairing_management_authentication_rejected"
                        )
                    conn.execute(
                        """
                        UPDATE credential_generations SET state='revoked'
                        WHERE pairing_id=? AND state != 'retired'
                        """,
                        (pairing_id,),
                    )
                    _insert_idempotency(
                        conn,
                        request_id=request_id,
                        operation="self_revoke_enrollment",
                        digest=digest,
                        result_id=pairing_id,
                        pairing_id=pairing_id,
                        generation=credential_generation,
                        result_state="revoked",
                        now=now,
                    )
                    conn.execute(
                        """
                        INSERT INTO management_revoke_replays(
                            request_id, pairing_id, reporting_node_id,
                            generation, bearer_verifier, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            request_id,
                            pairing_id,
                            reporting_node_id,
                            credential_generation,
                            management_bearer_verifier,
                            now,
                        ),
                    )
                else:
                    replay = conn.execute(
                        """
                        SELECT pairing_id, reporting_node_id, generation,
                               bearer_verifier
                        FROM management_revoke_replays
                        WHERE request_id=?
                        """,
                        (request_id,),
                    ).fetchone()
                    if (
                        replay is None
                        or row["lifecycle_state"] != "revoked"
                        or row["reporting_node_id"] != reporting_node_id
                        or row["credential_generation"]
                        != credential_generation
                        or replay["pairing_id"] != pairing_id
                        or replay["reporting_node_id"] != reporting_node_id
                        or replay["generation"] != credential_generation
                        or not secrets.compare_digest(
                            str(replay["bearer_verifier"]),
                            management_bearer_verifier,
                        )
                    ):
                        raise PairingStoreTerminalError(
                            "pairing_management_authentication_rejected"
                        )
                generations = [
                    int(item["generation"])
                    for item in conn.execute(
                        """
                        SELECT generation FROM credential_generations
                        WHERE pairing_id=?
                        """,
                        (pairing_id,),
                    ).fetchall()
                ]
                conn.commit()
                record = self._enrollment_record_with_conn(conn, pairing_id)
                if record is None:
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                return record, {
                    "invitation_id": str(row["invitation_id"]),
                    "generations": generations,
                }
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def _self_revoke_replay_sync(
        self,
        request_id: str,
        pairing_id: str,
        reporting_node_id: str,
        credential_generation: int,
        digest: str,
        management_bearer_verifier: str,
    ) -> EnrollmentRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT i.operation, i.request_digest, i.result_state,
                       i.pairing_id AS result_pairing_id,
                       i.generation AS result_generation,
                       r.pairing_id, r.reporting_node_id, r.generation,
                       r.bearer_verifier,
                       e.lifecycle_state,
                       e.reporting_node_id AS enrollment_node_id,
                       e.credential_generation
                FROM idempotency i
                JOIN management_revoke_replays r
                  ON r.request_id=i.request_id
                JOIN enrollments e ON e.pairing_id=r.pairing_id
                WHERE i.request_id=?
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            exact = (
                row["operation"] == "self_revoke_enrollment"
                and row["result_state"] == "revoked"
                and row["result_pairing_id"] == pairing_id
                and row["result_generation"] == credential_generation
                and row["pairing_id"] == pairing_id
                and row["reporting_node_id"] == reporting_node_id
                and row["generation"] == credential_generation
                and row["lifecycle_state"] == "revoked"
                and row["enrollment_node_id"] == reporting_node_id
                and row["credential_generation"] == credential_generation
                and secrets.compare_digest(str(row["request_digest"]), digest)
                and secrets.compare_digest(
                    str(row["bearer_verifier"]),
                    management_bearer_verifier,
                )
            )
            if not exact:
                return None
            return self._enrollment_record_with_conn(conn, pairing_id)

    def _revoke_enrollment_sync(
        self,
        request_id: str,
        pairing_id: str,
        digest: str,
    ) -> tuple[EnrollmentRecord, dict[str, object]]:
        now = _finite_timestamp(self._clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                operation = _check_idempotency(
                    conn,
                    request_id,
                    "revoke_enrollment",
                    digest,
                )
                row = conn.execute(
                    """
                    SELECT invitation_id, lifecycle_state,
                           credential_generation
                    FROM enrollments WHERE pairing_id=?
                    """,
                    (pairing_id,),
                ).fetchone()
                if row is None:
                    raise PairingStoreTerminalError("pairing_enrollment_unknown")
                if operation is None:
                    conn.execute(
                        """
                        UPDATE enrollments
                        SET lifecycle_state='revoked', hub_enabled=0,
                            updated_at=?, revoked_at=?,
                            failure_code='secret_cleanup_pending'
                        WHERE pairing_id=?
                        """,
                        (now, now, pairing_id),
                    )
                    conn.execute(
                        """
                        UPDATE credential_generations SET state='revoked'
                        WHERE pairing_id=? AND state != 'retired'
                        """,
                        (pairing_id,),
                    )
                    _insert_idempotency(
                        conn,
                        request_id=request_id,
                        operation="revoke_enrollment",
                        digest=digest,
                        result_id=pairing_id,
                        pairing_id=pairing_id,
                        generation=row["credential_generation"],
                        result_state="revoked",
                        now=now,
                    )
                generations = [
                    int(item["generation"])
                    for item in conn.execute(
                        """
                        SELECT generation FROM credential_generations
                        WHERE pairing_id=?
                        """,
                        (pairing_id,),
                    ).fetchall()
                ]
                conn.commit()
                record = self._enrollment_record_with_conn(conn, pairing_id)
                if record is None:
                    raise PairingStoreIntegrityError(
                        "pairing_reconciliation_failed"
                    )
                return record, {
                    "invitation_id": str(row["invitation_id"]),
                    "generations": generations,
                }
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def _set_enrollment_failure_sync(
        self,
        pairing_id: str,
        failure_code: str | None,
    ) -> None:
        if failure_code is not None:
            _validate_failure_code(failure_code)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE enrollments SET failure_code=?, updated_at=?
                WHERE pairing_id=?
                """,
                (failure_code, _finite_timestamp(self._clock()), pairing_id),
            )

    def _fail_enrollment_closed_sync(
        self,
        pairing_id: str,
        failure_code: str,
    ) -> None:
        _validate_failure_code(failure_code)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE enrollments
                SET lifecycle_state=CASE
                        WHEN lifecycle_state='revoked' THEN 'revoked'
                        ELSE 'pending'
                    END,
                    hub_enabled=0, failure_code=?, updated_at=?
                WHERE pairing_id=?
                """,
                (
                    failure_code,
                    _finite_timestamp(self._clock()),
                    pairing_id,
                ),
            )

    def _clear_cleanup_failure_sync(self, pairing_id: object) -> None:
        if not isinstance(pairing_id, str):
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE enrollments SET failure_code=NULL
                WHERE pairing_id=? AND failure_code='secret_cleanup_pending'
                """,
                (pairing_id,),
            )

    def _invitation_record_sync(
        self,
        invitation_id: str,
    ) -> InvitationRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM invitations WHERE invitation_id=?",
                (invitation_id,),
            ).fetchone()
        if row is None:
            return None
        return _invitation_record(row)

    def _claim_record_sync(self, claim_id: str) -> ClaimRecord | None:
        with self._connect() as conn:
            return self._claim_record_with_conn(conn, claim_id)

    def _pending_claims_sync(self, limit: int) -> tuple[ClaimRecord, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.claim_id
                FROM claims c
                JOIN invitations i ON i.invitation_id=c.invitation_id
                WHERE i.state='claimed'
                ORDER BY c.claimed_at, c.claim_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            records = tuple(
                self._claim_record_with_conn(conn, row["claim_id"])
                for row in rows
            )
        if any(record is None for record in records):
            raise PairingStoreIntegrityError("pairing_reconciliation_failed")
        return tuple(record for record in records if record is not None)

    def _claim_record_with_conn(
        self,
        conn: sqlite3.Connection,
        claim_id: object,
    ) -> ClaimRecord | None:
        row = conn.execute(
            """
            SELECT c.claim_id, c.invitation_id, i.pairing_id,
                   c.display_name, c.reporting_node_id, c.service_version,
                   c.platform, c.protocol_version, i.state, c.claimed_at,
                   i.expires_at
            FROM claims c
            JOIN invitations i ON i.invitation_id=c.invitation_id
            WHERE c.claim_id=?
            """,
            (claim_id,),
        ).fetchone()
        if row is None:
            return None
        return ClaimRecord(
            claim_id=str(row["claim_id"]),
            invitation_id=str(row["invitation_id"]),
            pairing_id=str(row["pairing_id"]),
            display_name=str(row["display_name"]),
            reporting_node_id=str(row["reporting_node_id"]),
            service_version=str(row["service_version"]),
            platform=str(row["platform"]),
            protocol_version=int(row["protocol_version"]),
            state="claimed",
            claimed_at=float(row["claimed_at"]),
            expires_at=float(row["expires_at"]),
        )

    def _enrollment_record_sync(
        self,
        pairing_id: str,
    ) -> EnrollmentRecord | None:
        with self._connect() as conn:
            return self._enrollment_record_with_conn(conn, pairing_id)

    def _enrollment_record_with_conn(
        self,
        conn: sqlite3.Connection,
        pairing_id: str,
    ) -> EnrollmentRecord | None:
        row = conn.execute(
            "SELECT * FROM enrollments WHERE pairing_id=?",
            (pairing_id,),
        ).fetchone()
        if row is None:
            return None
        return _enrollment_record(row)

    def _approval_result_with_conn(
        self,
        conn: sqlite3.Connection,
        pairing_id: str,
        *,
        hub_enabled: bool,
        approved_at: float,
    ) -> EnrollmentRecord | None:
        row = conn.execute(
            "SELECT * FROM enrollments WHERE pairing_id=?",
            (pairing_id,),
        ).fetchone()
        if row is None:
            return None
        return EnrollmentRecord(
            pairing_id=str(row["pairing_id"]),
            reporting_node_id=str(row["reporting_node_id"]),
            display_name=str(row["display_name"]),
            platform=str(row["platform"]),
            service_version=str(row["service_version"]),
            protocol_version=int(row["protocol_version"]),
            service_class=str(row["service_class"]),  # type: ignore[arg-type]
            lifecycle_state="pending",
            hub_enabled=hub_enabled,
            credential_generation=None,
            created_at=approved_at,
            updated_at=approved_at,
            revoked_at=None,
            failure_code=None,
        )

    def _enrollment_records_sync(self) -> tuple[EnrollmentRecord, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM enrollments
                ORDER BY created_at, pairing_id
                LIMIT ?
                """,
                (_MAX_PENDING_INVITATIONS,),
            ).fetchall()
        return tuple(_enrollment_record(row) for row in rows)

    def _enrollment_binding_sync(
        self,
        pairing_id: str,
    ) -> EnrollmentBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT e.pairing_id, e.invitation_id,
                       e.reporting_node_id, e.locator_ref,
                       e.credential_generation, g.snapshot_ref,
                       g.dispatch_ref, g.management_ref
                FROM enrollments e
                JOIN credential_generations g
                  ON g.pairing_id=e.pairing_id
                 AND g.generation=e.credential_generation
                WHERE e.pairing_id=?
                  AND e.lifecycle_state='active'
                  AND e.hub_enabled=1
                  AND e.failure_code IS NULL
                  AND g.state='active'
                """,
                (pairing_id,),
            ).fetchone()
        if row is None:
            return None
        return EnrollmentBinding(
            pairing_id=str(row["pairing_id"]),
            invitation_id=str(row["invitation_id"]),
            reporting_node_id=str(row["reporting_node_id"]),
            locator_ref=str(row["locator_ref"]),
            credential_generation=int(row["credential_generation"]),
            snapshot_ref=str(row["snapshot_ref"]),
            dispatch_ref=str(row["dispatch_ref"]),
            management_ref=str(row["management_ref"]),
        )

    def _candidate_binding_sync(
        self,
        pairing_id: str,
        generation: int,
    ) -> EnrollmentBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT e.pairing_id, e.invitation_id,
                       e.reporting_node_id, e.locator_ref,
                       e.credential_generation, g.snapshot_ref,
                       g.dispatch_ref, g.management_ref
                FROM enrollments e
                JOIN invitations i ON i.invitation_id=e.invitation_id
                JOIN credential_generations g
                  ON g.pairing_id=e.pairing_id
                 AND g.generation=e.credential_generation
                WHERE e.pairing_id=?
                  AND e.credential_generation=?
                  AND e.lifecycle_state='pending'
                  AND e.failure_code IS NULL
                  AND i.state IN ('provisioning', 'activating')
                  AND g.state='candidate'
                  AND g.failure_code IS NULL
                """,
                (pairing_id, generation),
            ).fetchone()
        if row is None:
            return None
        return EnrollmentBinding(
            pairing_id=str(row["pairing_id"]),
            invitation_id=str(row["invitation_id"]),
            reporting_node_id=str(row["reporting_node_id"]),
            locator_ref=str(row["locator_ref"]),
            credential_generation=int(row["credential_generation"]),
            snapshot_ref=str(row["snapshot_ref"]),
            dispatch_ref=str(row["dispatch_ref"]),
            management_ref=str(row["management_ref"]),
        )

    def _active_binding_sync(
        self,
        pairing_id: str,
        generation: int,
    ) -> EnrollmentBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT e.pairing_id, e.invitation_id,
                       e.reporting_node_id, e.locator_ref,
                       e.credential_generation, g.snapshot_ref,
                       g.dispatch_ref, g.management_ref
                FROM enrollments e
                JOIN credential_generations g
                  ON g.pairing_id=e.pairing_id
                 AND g.generation=e.credential_generation
                WHERE e.pairing_id=?
                  AND e.credential_generation=?
                  AND e.lifecycle_state='active'
                  AND e.failure_code IS NULL
                  AND g.state='active'
                  AND g.failure_code IS NULL
                """,
                (pairing_id, generation),
            ).fetchone()
        if row is None:
            return None
        return EnrollmentBinding(
            pairing_id=str(row["pairing_id"]),
            invitation_id=str(row["invitation_id"]),
            reporting_node_id=str(row["reporting_node_id"]),
            locator_ref=str(row["locator_ref"]),
            credential_generation=int(row["credential_generation"]),
            snapshot_ref=str(row["snapshot_ref"]),
            dispatch_ref=str(row["dispatch_ref"]),
            management_ref=str(row["management_ref"]),
        )

    def _reconciliation_snapshot_sync(
        self,
        now: float,
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                expired = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT invitation_id, pairing_id
                        FROM invitations
                        WHERE state IN ('preparing', 'issued', 'claimed')
                          AND expires_at <= ?
                        """,
                        (now,),
                    ).fetchall()
                ]
                conn.execute(
                    """
                    UPDATE invitations SET state='expired'
                    WHERE state IN ('preparing', 'issued', 'claimed')
                      AND expires_at <= ?
                    """,
                    (now,),
                )
                preparing = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT invitation_id, pairing_id,
                               invitation_ref, locator_ref
                        FROM invitations
                        WHERE state='preparing' AND expires_at > ?
                        """,
                        (now,),
                    ).fetchall()
                ]
                allocating = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT pairing_id, generation, snapshot_ref,
                               dispatch_ref, management_ref
                        FROM credential_generations
                        WHERE state='allocating'
                        """
                    ).fetchall()
                ]
                enrollment_contexts = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT e.pairing_id, e.locator_ref,
                               e.credential_generation AS generation,
                               g.snapshot_ref, g.dispatch_ref, g.management_ref
                        FROM enrollments e
                        JOIN credential_generations g
                          ON g.pairing_id=e.pairing_id
                         AND g.generation=e.credential_generation
                        WHERE e.lifecycle_state != 'revoked'
                          AND e.credential_generation IS NOT NULL
                          AND g.state IN ('candidate', 'active')
                        """
                    ).fetchall()
                ]
                cleanup: list[dict[str, object]] = []
                for row in conn.execute(
                    """
                    SELECT i.invitation_id, i.pairing_id, i.state,
                           e.lifecycle_state,
                           e.pairing_id AS enrollment_pairing_id
                    FROM invitations i
                    LEFT JOIN enrollments e ON e.pairing_id=i.pairing_id
                    WHERE i.state IN ('completed', 'expired', 'rejected', 'failed')
                    """
                ).fetchall():
                    mode = (
                        "invitation"
                        if row["state"] == "completed"
                        and row["lifecycle_state"] != "revoked"
                        else "all"
                    )
                    generations = [
                        int(item["generation"])
                        for item in conn.execute(
                            """
                            SELECT generation FROM credential_generations
                            WHERE pairing_id=?
                            """,
                            (row["pairing_id"],),
                        ).fetchall()
                    ]
                    cleanup.append(
                        {
                            "invitation_id": str(row["invitation_id"]),
                            "pairing_id": str(row["pairing_id"]),
                            "enrollment_pairing_id": row[
                                "enrollment_pairing_id"
                            ],
                            "mode": mode,
                            "generations": generations,
                        }
                    )
                conn.commit()
                return (
                    expired,
                    preparing,
                    allocating,
                    enrollment_contexts,
                    cleanup,
                )
            except BaseException:
                conn.rollback()
                raise


def _validate_invitation_request(request: object) -> None:
    if not isinstance(request, InvitationRequest):
        raise PairingStoreValidationError("pairing_invalid_request")
    _canonical_uuid(request.request_id)
    _bounded_private_text(request.locator, maximum=_MAX_LOCATOR_BYTES)
    if request.intent not in {"new", "adopt-static"}:
        raise PairingStoreValidationError("pairing_invalid_request")
    if request.expected_platform != "macos":
        raise PairingStoreValidationError("pairing_unsupported_platform")
    if request.expected_reporting_node_id is not None:
        _bounded_text(
            request.expected_reporting_node_id,
            maximum=_MAX_TEXT_BYTES,
        )
    if request.intent == "adopt-static" and request.expected_reporting_node_id is None:
        raise PairingStoreValidationError("pairing_invalid_request")
    if request.transport not in _TRANSPORTS:
        raise PairingStoreValidationError("pairing_invalid_request")
    if request.service_class not in _SERVICE_CLASSES:
        raise PairingStoreValidationError("pairing_invalid_request")
    if (
        isinstance(request.expires_in_seconds, bool)
        or not isinstance(request.expires_in_seconds, (int, float))
        or not math.isfinite(float(request.expires_in_seconds))
        or float(request.expires_in_seconds) <= 0
        or float(request.expires_in_seconds) > _MAX_INVITATION_SECONDS
    ):
        raise PairingStoreValidationError("pairing_invalid_expiry")


def _validate_claim_request(request: object) -> None:
    if not isinstance(request, ClaimRequest):
        raise PairingStoreValidationError("pairing_invalid_request")
    _canonical_uuid(request.request_id)
    _canonical_uuid(request.invitation_id)
    _bounded_private_text(request.pairing_secret, maximum=4096)
    _bounded_private_text(request.locator, maximum=_MAX_LOCATOR_BYTES)
    _bounded_text(request.display_name, maximum=_MAX_TEXT_BYTES)
    _bounded_text(request.reporting_node_id, maximum=_MAX_TEXT_BYTES)
    _bounded_text(request.service_version, maximum=_MAX_VERSION_BYTES)
    if request.platform != "macos":
        raise PairingStoreValidationError("pairing_unsupported_platform")
    if (
        isinstance(request.protocol_minimum, bool)
        or isinstance(request.protocol_maximum, bool)
        or not isinstance(request.protocol_minimum, int)
        or not isinstance(request.protocol_maximum, int)
        or request.protocol_minimum < 1
        or request.protocol_maximum > 255
        or request.protocol_minimum > _PAIRING_PROTOCOL_VERSION
        or request.protocol_maximum < _PAIRING_PROTOCOL_VERSION
    ):
        raise PairingStoreValidationError("pairing_protocol_unsupported")


def _validate_approval_request(request: object) -> None:
    if not isinstance(request, ApprovalRequest):
        raise PairingStoreValidationError("pairing_invalid_request")
    _canonical_uuid(request.request_id)
    _canonical_uuid(request.claim_id)
    _bounded_private_text(request.locator, maximum=_MAX_LOCATOR_BYTES)
    if request.service_class not in _SERVICE_CLASSES:
        raise PairingStoreValidationError("pairing_invalid_request")
    if not isinstance(request.hub_enabled, bool):
        raise PairingStoreValidationError("pairing_invalid_request")


def _validate_presence_approval_request(request: object) -> None:
    if not isinstance(request, PresenceApprovalRequest):
        raise PairingStoreValidationError("pairing_invalid_request")
    _canonical_uuid(request.request_id)
    _canonical_uuid(request.claim_id)
    if (
        not isinstance(request.presence_pin, str)
        or len(request.presence_pin) != 6
        or not request.presence_pin.isascii()
        or not request.presence_pin.isdigit()
    ):
        raise PairingStoreValidationError("pairing_invalid_request")
    if request.service_class not in _SERVICE_CLASSES:
        raise PairingStoreValidationError("pairing_invalid_request")
    if not isinstance(request.hub_enabled, bool):
        raise PairingStoreValidationError("pairing_invalid_request")


def _validate_provision_request(request: object) -> None:
    if not isinstance(request, ProvisionRequest):
        raise PairingStoreValidationError("pairing_invalid_request")
    _canonical_uuid(request.request_id)
    _canonical_uuid(request.claim_id)
    _bounded_private_text(request.pairing_secret, maximum=4096)


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise PairingStoreValidationError("pairing_invalid_id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise PairingStoreValidationError("pairing_invalid_id") from None
    if str(parsed) != value:
        raise PairingStoreValidationError("pairing_invalid_id")
    return value


def pairing_presence_pin(pairing_secret: str) -> str:
    """Derive the short-lived display code from a strong invitation secret."""

    secret = _bounded_private_text(pairing_secret, maximum=4096)
    digest = hmac.new(
        secret.encode("utf-8"),
        b"mnemosyne-fleet-presence-pin-v1",
        hashlib.sha256,
    ).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"


def _bounded_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise PairingStoreValidationError("pairing_invalid_request")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise PairingStoreValidationError("pairing_invalid_request") from None
    if len(encoded) > maximum or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PairingStoreValidationError("pairing_invalid_request")
    return value


def _bounded_private_text(value: object, *, maximum: int) -> str:
    # Identical validation with a fixed error that never includes the value.
    return _bounded_text(value, maximum=maximum)


def _bounded_generation(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > (1 << 63) - 1
    ):
        raise PairingStoreValidationError("pairing_invalid_request")
    return value


def _bounded_verifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PairingStoreValidationError("pairing_invalid_request")
    return value


def _active_management_row_matches(
    row: sqlite3.Row,
    *,
    reporting_node_id: str,
    credential_generation: int,
) -> bool:
    """Repeat the management authorization binding inside one SQL claim."""

    return (
        row["lifecycle_state"] == "active"
        and row["reporting_node_id"] == reporting_node_id
        and row["credential_generation"] == credential_generation
        and row["failure_code"] is None
        and row["generation_state"] == "active"
        and row["generation_failure_code"] is None
    )


def _finite_timestamp(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise PairingStoreIntegrityError("pairing_invalid_timestamp")
    return float(value)


def _request_digest(operation: str, payload: dict[str, object]) -> str:
    canonical = json.dumps(
        {"operation": operation, "payload": payload, "schema_version": 1},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _check_idempotency(
    conn: sqlite3.Connection,
    request_id: str,
    operation: str,
    digest: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT request_id, operation, request_digest, result_id,
               pairing_id, generation, result_state, result_at
        FROM idempotency WHERE request_id=?
        """,
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    if row["operation"] != operation or not secrets.compare_digest(
        str(row["request_digest"]),
        digest,
    ):
        raise PairingStoreConflictError("idempotency_conflict")
    return row


def _insert_idempotency(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    operation: str,
    digest: str,
    result_id: str,
    pairing_id: str | None,
    generation: int | None,
    result_state: str,
    now: float,
) -> None:
    conn.execute(
        """
        INSERT INTO idempotency(
            request_id, operation, request_digest, result_id,
            pairing_id, generation, result_state, result_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            operation,
            digest,
            result_id,
            pairing_id,
            generation,
            result_state,
            now,
        ),
    )


def _validate_failure_code(code: str) -> None:
    if code not in _FIXED_FAILURE_CODES:
        raise PairingStoreIntegrityError("pairing_invalid_failure_code")


def _invitation_record(row: sqlite3.Row) -> InvitationRecord:
    state = str(row["state"])
    pairing_id = None if state in {"preparing", "issued"} else str(row["pairing_id"])
    return InvitationRecord(
        invitation_id=str(row["invitation_id"]),
        pairing_id=pairing_id,
        intent=str(row["intent"]),  # type: ignore[arg-type]
        expected_platform=str(row["expected_platform"]),
        expected_reporting_node_id=(
            None
            if row["expected_reporting_node_id"] is None
            else str(row["expected_reporting_node_id"])
        ),
        transport=str(row["transport"]),  # type: ignore[arg-type]
        service_class=str(row["service_class"]),  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        attempts_remaining=max(
            0,
            int(row["attempt_limit"]) - int(row["failed_attempts"]),
        ),
        claim_id=None if row["claim_id"] is None else str(row["claim_id"]),
        failure_code=(
            None if row["failure_code"] is None else str(row["failure_code"])
        ),
    )


def _enrollment_record(row: sqlite3.Row) -> EnrollmentRecord:
    return EnrollmentRecord(
        pairing_id=str(row["pairing_id"]),
        reporting_node_id=str(row["reporting_node_id"]),
        display_name=str(row["display_name"]),
        platform=str(row["platform"]),
        service_version=str(row["service_version"]),
        protocol_version=int(row["protocol_version"]),
        service_class=str(row["service_class"]),  # type: ignore[arg-type]
        lifecycle_state=str(row["lifecycle_state"]),  # type: ignore[arg-type]
        hub_enabled=bool(row["hub_enabled"]),
        credential_generation=(
            None
            if row["credential_generation"] is None
            else int(row["credential_generation"])
        ),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        revoked_at=(
            None if row["revoked_at"] is None else float(row["revoked_at"])
        ),
        failure_code=(
            None if row["failure_code"] is None else str(row["failure_code"])
        ),
    )


def _current_uid() -> int:
    return os.getuid()


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except (OSError, FileNotFoundError, NotADirectoryError) as exc:
        raise PairingStoreIntegrityError("pairing_store_insecure_path") from exc


def _assert_private_directory(status: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != _current_uid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise PairingStoreIntegrityError("pairing_store_insecure_path")


def _assert_private_file(status: os.stat_result) -> None:
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != _current_uid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise PairingStoreIntegrityError("pairing_store_insecure_path")
