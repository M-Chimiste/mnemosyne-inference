from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid5

from .config import NodeConfig, ServiceClass
from .locator_policy import LocatorPolicy, ResolvedLocator
from .pairing_api import (
    ActivationAcknowledgement,
    ClaimApproval,
    ClaimProvision,
    ClaimStatusRequest,
    InvitationClaim,
    InvitationCreate,
    EnrollmentSelfManagement,
    PresenceClaimApproval,
    PresencePairingRequest,
)
from .pairing_store import (
    ApprovalRequest,
    ClaimRecord,
    ClaimStatusRecord,
    ClaimRequest,
    EnrollmentBinding,
    EnrollmentRecord,
    InvitationIssue,
    InvitationRequest,
    PairingStore,
    PairingStoreIntegrityError,
    PairingStoreTerminalError,
    PairingStoreValidationError,
    PresenceApprovalRequest,
    ProvisionRequest,
    ProvisioningRecord,
)
from .registry import NodeRegistry
from .secret_store import CredentialBundle, SecretStore, SecretStoreError


_INTERNAL_REQUEST_NAMESPACE: Final[UUID] = UUID(
    "35c3da14-5542-5df2-af6d-c37fc259b163"
)


class PairingCoordinatorError(RuntimeError):
    """A fixed-code orchestration failure with no topology or secret detail."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ActivationCandidate:
    locator: ResolvedLocator = field(repr=False)
    credentials: CredentialBundle = field(repr=False)
    pairing_id: str
    reporting_node_id: str
    credential_generation: int
    service_instance_id: str


ActivationProbe = Callable[[ActivationCandidate], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    published: int
    failed_closed: int


class PairingCoordinator:
    """Join durable pairing metadata, encrypted values, and live membership.

    The coordinator never makes a pending or Hub-disabled enrollment visible to
    the scheduler. All locator and credential material stays in private objects
    and every public exception is a fixed code.
    """

    def __init__(
        self,
        *,
        pairing_store: PairingStore,
        secret_store: SecretStore,
        locator_policy: LocatorPolicy,
        registry: NodeRegistry,
        activation_probe: ActivationProbe,
        forbidden_credentials: Iterable[str] = (),
    ) -> None:
        self.pairing_store = pairing_store
        self.secret_store = secret_store
        self.locator_policy = locator_policy
        self.registry = registry
        self._activation_probe = activation_probe
        self._forbidden_credentials = tuple(
            value for value in forbidden_credentials if value
        )

    async def initialize(self) -> ReconciliationResult:
        await self.secret_store.initialize()
        await self.pairing_store.initialize()
        published = 0
        failed_closed = 0
        for enrollment in await self.pairing_store.enrollments():
            if not enrollment.routable:
                continue
            try:
                await self._publish(enrollment)
            except Exception:
                # Persisted pairing state is diagnostic only until private
                # material and locator policy can be proven again. Local Fleet
                # startup continues, but this enrollment receives no poller.
                failed_closed += 1
            else:
                published += 1
        return ReconciliationResult(
            published=published,
            failed_closed=failed_closed,
        )

    async def issue_invitation(
        self,
        payload: InvitationCreate,
    ) -> InvitationIssue:
        expected = payload.expected
        resolved = await self.locator_policy.resolve(
            expected.locator.get_secret_value(),
            transport=expected.transport,
        )
        return await self.pairing_store.issue_invitation(
            InvitationRequest(
                request_id=payload.request_id,
                locator=resolved.origin,
                intent=payload.intent,
                expected_platform=expected.platform,
                expected_reporting_node_id=expected.reporting_node_id,
                transport=expected.transport,
                service_class=expected.service_class,
                expires_in_seconds=payload.expires_in_seconds,
            )
        )

    async def request_presence_pairing(
        self,
        payload: PresencePairingRequest,
    ) -> InvitationIssue:
        """Issue a hidden invitation requested by one exact Mac locator."""

        resolved = await self.locator_policy.resolve(
            payload.locator.get_secret_value(),
            transport=payload.transport,
        )
        return await self.pairing_store.issue_invitation(
            InvitationRequest(
                request_id=payload.request_id,
                locator=resolved.origin,
                intent="new",
                expected_platform=payload.mac.platform,
                expected_reporting_node_id=payload.mac.reporting_node_id,
                transport=payload.transport,
                service_class="primary",
                expires_in_seconds=300.0,
            )
        )

    async def claim_invitation(self, payload: InvitationClaim) -> ClaimRecord:
        invitation = await self.pairing_store.invitation(payload.invitation_id)
        if invitation is None:
            raise PairingStoreTerminalError("pairing_invitation_unknown")
        resolved = await self.locator_policy.resolve(
            payload.locator.get_secret_value(),
            transport=invitation.transport,
        )
        return await self.pairing_store.claim_invitation(
            ClaimRequest(
                request_id=payload.request_id,
                invitation_id=payload.invitation_id,
                pairing_secret=payload.pairing_secret.get_secret_value(),
                locator=resolved.origin,
                display_name=payload.mac.display_name,
                reporting_node_id=payload.mac.reporting_node_id,
                service_version=payload.mac.service_version,
                platform=payload.mac.platform,
                protocol_minimum=payload.supported_protocol.minimum,
                protocol_maximum=payload.supported_protocol.maximum,
            )
        )

    async def approve_claim(
        self,
        claim_id: str,
        payload: ClaimApproval,
    ) -> EnrollmentRecord:
        claim = await self.pairing_store.claim(claim_id)
        if claim is None:
            raise PairingStoreTerminalError("pairing_claim_unknown")
        invitation = await self.pairing_store.invitation(claim.invitation_id)
        if invitation is None:
            raise PairingStoreIntegrityError("pairing_reconciliation_failed")
        resolved = await self.locator_policy.resolve(
            payload.locator.get_secret_value(),
            transport=invitation.transport,
        )
        return await self.pairing_store.approve_claim(
            ApprovalRequest(
                request_id=payload.request_id,
                claim_id=claim_id,
                locator=resolved.origin,
                service_class=payload.service_class,
                hub_enabled=payload.hub_enabled,
            )
        )

    async def approve_claim_with_presence(
        self,
        claim_id: str,
        payload: PresenceClaimApproval,
    ) -> EnrollmentRecord:
        return await self.pairing_store.approve_claim_with_presence(
            PresenceApprovalRequest(
                request_id=payload.request_id,
                claim_id=claim_id,
                presence_pin=payload.presence_pin,
                service_class=payload.service_class,
                hub_enabled=payload.hub_enabled,
            )
        )

    async def provision_claim(
        self,
        claim_id: str,
        payload: ClaimProvision,
    ) -> ProvisioningRecord:
        result = await self.pairing_store.provision_claim(
            ProvisionRequest(
                request_id=payload.request_id,
                claim_id=claim_id,
                pairing_secret=payload.pairing_secret.get_secret_value(),
            )
        )
        self._validate_bundle(
            result.credentials,
            additional_forbidden=(payload.pairing_secret.get_secret_value(),),
        )
        return result

    async def reject_claim(self, *, claim_id: str, request_id: str) -> bool:
        return await self.pairing_store.reject_claim(
            request_id=request_id,
            claim_id=claim_id,
        )

    async def pending_claims(self, *, limit: int = 100) -> tuple[ClaimRecord, ...]:
        return await self.pairing_store.pending_claims(limit=limit)

    async def claim_status(
        self,
        *,
        claim_id: str,
        payload: ClaimStatusRequest,
    ) -> ClaimStatusRecord:
        return await self.pairing_store.claim_status(
            claim_id=claim_id,
            claim_request_id=payload.claim_request_id,
        )

    async def enrollments(self) -> tuple[EnrollmentRecord, ...]:
        return await self.pairing_store.enrollments()

    async def authenticate_active_management(
        self,
        *,
        pairing_id: str,
        credential_generation: int,
        management_bearer: str | None,
    ) -> EnrollmentRecord | None:
        """Authenticate one exact active management generation.

        Hub enablement is deliberately not required: a paired Mac may continue
        reporting path-free inventory while disabled for inference routing.
        Unknown, pending, retired, superseded, and revoked generations collapse
        to the same negative result.
        """

        enrollment = await self.authenticate_active_management_bearer(
            pairing_id=pairing_id,
            management_bearer=management_bearer,
        )
        if (
            enrollment is None
            or enrollment.credential_generation != credential_generation
        ):
            return None
        return enrollment

    async def authenticate_active_management_bearer(
        self,
        *,
        pairing_id: str,
        management_bearer: str | None,
    ) -> EnrollmentRecord | None:
        """Authenticate before reading a potentially large management body."""

        if management_bearer is None or len(management_bearer) > 4096:
            return None
        try:
            enrollment = await self.pairing_store.enrollment(pairing_id)
            if (
                enrollment is None
                or enrollment.lifecycle_state != "active"
                or enrollment.credential_generation is None
            ):
                return None
            binding = await self.pairing_store.active_binding(
                pairing_id,
                enrollment.credential_generation,
            )
        except PairingStoreTerminalError:
            return None
        except PairingStoreValidationError:
            return None
        if binding is None:
            return None
        bundle = await self._load_exact_bundle(binding)
        if not hmac.compare_digest(
            management_bearer,
            bundle.management.secret,
        ):
            return None
        return enrollment

    async def activate(
        self,
        *,
        pairing_id: str,
        management_bearer: str | None,
        payload: ActivationAcknowledgement,
    ) -> EnrollmentRecord:
        generation = payload.credential_generation
        binding = await self.pairing_store.candidate_binding(
            pairing_id,
            generation,
        )
        already_active = False
        if binding is None:
            binding = await self.pairing_store.active_binding(
                pairing_id,
                generation,
            )
            already_active = binding is not None
        if binding is None:
            raise PairingStoreTerminalError("pairing_generation_terminal")
        if payload.reporting_node_id != binding.reporting_node_id:
            raise PairingStoreTerminalError("pairing_activation_rejected")

        bundle = await self._load_exact_bundle(binding)
        if management_bearer is None or not hmac.compare_digest(
            management_bearer,
            bundle.management.secret,
        ):
            raise PairingStoreTerminalError("pairing_activation_rejected")

        activate_request_id = _internal_request_id(
            payload.request_id,
            "activate",
        )
        if already_active:
            record = await self.pairing_store.activate_enrollment(
                request_id=activate_request_id,
                pairing_id=pairing_id,
                generation=generation,
            )
            if record.routable:
                await self._publish(record)
            return record

        invitation = await self._invitation_for(binding)
        locator = await self._resolve_binding(binding, invitation.transport)
        await self.pairing_store.mark_activating(
            request_id=_internal_request_id(payload.request_id, "activating"),
            pairing_id=pairing_id,
            generation=generation,
        )
        await self._activation_probe(
            ActivationCandidate(
                locator=locator,
                credentials=bundle,
                pairing_id=pairing_id,
                reporting_node_id=binding.reporting_node_id,
                credential_generation=generation,
                service_instance_id=payload.service_instance_id,
            )
        )
        record = await self.pairing_store.activate_enrollment(
            request_id=activate_request_id,
            pairing_id=pairing_id,
            generation=generation,
        )
        if record.routable:
            await self._publish(record)
        return record

    async def set_hub_enabled(
        self,
        *,
        pairing_id: str,
        request_id: str,
        enabled: bool,
    ) -> EnrollmentRecord:
        record = await self.pairing_store.set_hub_enabled(
            request_id=request_id,
            pairing_id=pairing_id,
            enabled=enabled,
        )
        if record.routable:
            await self._publish(record)
        else:
            await self.registry.deactivate_enrollment(pairing_id)
        return record

    async def self_disable(
        self,
        *,
        pairing_id: str,
        management_bearer: str | None,
        payload: EnrollmentSelfManagement,
    ) -> EnrollmentRecord:
        """Honor one exact Mac-authorized Hub-disable request.

        Management authentication is intentionally repeated around the body
        binding.  The store then repeats the same identity/generation checks in
        its write transaction, closing both cross-enrollment substitution and
        check-to-write races.
        """

        first = await self._authenticate_self_management(
            pairing_id=pairing_id,
            management_bearer=management_bearer,
            payload=payload,
        )
        second = await self._authenticate_self_management(
            pairing_id=pairing_id,
            management_bearer=management_bearer,
            payload=payload,
        )
        if first != second:
            raise PairingStoreTerminalError(
                "pairing_management_authentication_rejected"
            )
        record = await self._mutate_after_routing_removal(
            pairing_id,
            lambda: self.pairing_store.self_disable_enrollment(
                request_id=payload.request_id,
                pairing_id=payload.pairing_id,
                reporting_node_id=payload.reporting_node_id,
                credential_generation=payload.credential_generation,
            ),
        )
        return record

    async def self_revoke(
        self,
        *,
        pairing_id: str,
        management_bearer: str | None,
        payload: EnrollmentSelfManagement,
    ) -> EnrollmentRecord:
        """Revoke exact dynamic credentials or replay only that same revoke."""

        verifier = None
        if payload.pairing_id == pairing_id:
            verifier = _management_replay_verifier(
                pairing_id=pairing_id,
                credential_generation=payload.credential_generation,
                management_bearer=management_bearer,
            )
        if verifier is not None:
            replay = await self.pairing_store.self_revoke_replay(
                request_id=payload.request_id,
                pairing_id=payload.pairing_id,
                reporting_node_id=payload.reporting_node_id,
                credential_generation=payload.credential_generation,
                management_bearer_verifier=verifier,
            )
            if replay is not None:
                await self.registry.deactivate_enrollment(pairing_id)
                return replay

        first = await self._authenticate_self_management(
            pairing_id=pairing_id,
            management_bearer=management_bearer,
            payload=payload,
        )
        second = await self._authenticate_self_management(
            pairing_id=pairing_id,
            management_bearer=management_bearer,
            payload=payload,
        )
        if first != second or verifier is None:
            raise PairingStoreTerminalError(
                "pairing_management_authentication_rejected"
            )
        record = await self._mutate_after_routing_removal(
            pairing_id,
            lambda: self.pairing_store.self_revoke_enrollment(
                request_id=payload.request_id,
                pairing_id=payload.pairing_id,
                reporting_node_id=payload.reporting_node_id,
                credential_generation=payload.credential_generation,
                management_bearer_verifier=verifier,
            ),
        )
        return record

    async def _authenticate_self_management(
        self,
        *,
        pairing_id: str,
        management_bearer: str | None,
        payload: EnrollmentSelfManagement,
    ) -> EnrollmentRecord:
        if payload.pairing_id != pairing_id:
            raise PairingStoreTerminalError(
                "pairing_management_authentication_rejected"
            )
        enrollment = await self.authenticate_active_management(
            pairing_id=pairing_id,
            credential_generation=payload.credential_generation,
            management_bearer=management_bearer,
        )
        if (
            enrollment is None
            or enrollment.pairing_id != payload.pairing_id
            or enrollment.reporting_node_id != payload.reporting_node_id
            or enrollment.credential_generation
            != payload.credential_generation
            or enrollment.lifecycle_state != "active"
        ):
            raise PairingStoreTerminalError(
                "pairing_management_authentication_rejected"
            )
        return enrollment

    async def _mutate_after_routing_removal(
        self,
        pairing_id: str,
        mutation: Callable[[], Awaitable[EnrollmentRecord]],
    ) -> EnrollmentRecord:
        """Remove live authority first and restore only a still-routable row."""

        await self.registry.deactivate_enrollment(pairing_id)
        try:
            return await mutation()
        except BaseException:
            # If the durable mutation did not commit, restore the exact current
            # enrollment. A failed restore stays safely absent and the original
            # fixed-code mutation failure remains the caller-visible result.
            try:
                enrollment = await self.pairing_store.enrollment(pairing_id)
                if enrollment is not None and enrollment.routable:
                    await self._publish(enrollment)
            except BaseException:
                pass
            raise

    async def revoke(
        self,
        *,
        pairing_id: str,
        request_id: str,
    ) -> EnrollmentRecord:
        record = await self.pairing_store.revoke_enrollment(
            request_id=request_id,
            pairing_id=pairing_id,
        )
        await self.registry.deactivate_enrollment(pairing_id)
        return record

    async def _publish(self, enrollment: EnrollmentRecord) -> None:
        binding = await self.pairing_store.enrollment_binding(
            enrollment.pairing_id
        )
        if binding is None:
            raise PairingCoordinatorError("pairing_binding_unavailable")
        node = await self._node_from_binding(binding, enrollment.service_class)
        await self.registry.activate_enrollment(node)

    async def _node_from_binding(
        self,
        binding: EnrollmentBinding,
        service_class: ServiceClass,
    ) -> NodeConfig:
        invitation = await self._invitation_for(binding)
        locator = await self._resolve_binding(binding, invitation.transport)
        bundle = await self._load_exact_bundle(binding)
        return NodeConfig(
            node_id=binding.reporting_node_id,
            url=locator.origin,
            fleet_token=bundle.snapshot.secret,
            inference_token=bundle.dispatch.secret,
            service_class=service_class,
            source="paired",
            enrollment_id=binding.pairing_id,
            locator_transport=invitation.transport,
        )

    async def _invitation_for(self, binding: EnrollmentBinding):
        invitation = await self.pairing_store.invitation(binding.invitation_id)
        if invitation is None:
            raise PairingCoordinatorError("pairing_binding_unavailable")
        return invitation

    async def _resolve_binding(
        self,
        binding: EnrollmentBinding,
        transport: str,
    ) -> ResolvedLocator:
        try:
            private_locator = await self.secret_store.load_locator(
                binding.pairing_id,
                binding.locator_ref,
            )
            return await self.locator_policy.resolve(
                private_locator,
                transport=transport,  # type: ignore[arg-type]
            )
        except SecretStoreError:
            raise PairingCoordinatorError("pairing_binding_unavailable") from None

    async def _load_exact_bundle(
        self,
        binding: EnrollmentBinding,
    ) -> CredentialBundle:
        try:
            bundle = await self.secret_store.load_bundle(
                binding.pairing_id,
                binding.credential_generation,
            )
        except SecretStoreError:
            raise PairingCoordinatorError("pairing_binding_unavailable") from None
        if bundle is None or (
            bundle.snapshot.secret_ref,
            bundle.dispatch.secret_ref,
            bundle.management.secret_ref,
        ) != (
            binding.snapshot_ref,
            binding.dispatch_ref,
            binding.management_ref,
        ):
            raise PairingCoordinatorError("pairing_binding_unavailable")
        self._validate_bundle(bundle)
        return bundle

    def _validate_bundle(
        self,
        bundle: CredentialBundle,
        *,
        additional_forbidden: Iterable[str] = (),
    ) -> None:
        values = (
            bundle.snapshot.secret,
            bundle.dispatch.secret,
            bundle.management.secret,
        )
        if len(set(values)) != len(values) or any(
            hmac.compare_digest(value, forbidden)
            for value in values
            for forbidden in (
                *self._forbidden_credentials,
                *tuple(additional_forbidden),
            )
        ):
            raise PairingCoordinatorError("pairing_credential_collision")


def _internal_request_id(external_request_id: str, operation: str) -> str:
    return str(
        uuid5(
            _INTERNAL_REQUEST_NAMESPACE,
            f"{operation}:{external_request_id}",
        )
    )


def _management_replay_verifier(
    *,
    pairing_id: str,
    credential_generation: int,
    management_bearer: str | None,
) -> str | None:
    """Return a domain-separated, secret-free exact-replay verifier."""

    if (
        management_bearer is None
        or not isinstance(management_bearer, str)
        or not 1 <= len(management_bearer) <= 4096
    ):
        return None
    digest = hashlib.sha256()
    digest.update(b"mnemosyne-fleet-self-revoke-replay-v1\0")
    digest.update(pairing_id.encode("ascii", errors="strict"))
    digest.update(b"\0")
    digest.update(str(credential_generation).encode("ascii"))
    digest.update(b"\0")
    digest.update(management_bearer.encode("utf-8"))
    return digest.hexdigest()
