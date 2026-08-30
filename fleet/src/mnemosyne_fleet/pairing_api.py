from __future__ import annotations

import hmac
from typing import Annotated, Literal, TypeVar

from fastapi import Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)


PAIRING_SCHEMA_VERSION = 1
MAX_PAIRING_REQUEST_BYTES = 16 * 1024

CanonicalUUID = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    ),
]
BoundedIdentifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[!-~]+$"),
]
BoundedVersion = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[!-~]+$"),
]


class PairingAPIError(RuntimeError):
    """A fixed public failure that never contains caller-controlled input."""

    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


class StrictPairingModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        str_strip_whitespace=False,
    )


class PairingPayload(StrictPairingModel):
    schema_version: Literal[PAIRING_SCHEMA_VERSION]


class InvitationExpected(StrictPairingModel):
    platform: Literal["macos"]
    reporting_node_id: BoundedIdentifier | None = None
    locator: SecretStr = Field(min_length=1, max_length=2048, repr=False)
    transport: Literal["https", "tailscale", "trusted_lan_http"]
    service_class: Literal["primary", "opportunistic", "overflow"]


class InvitationCreate(PairingPayload):
    request_id: CanonicalUUID
    intent: Literal["new", "adopt-static"]
    expected: InvitationExpected
    expires_in_seconds: float = Field(gt=0, le=300)

    @model_validator(mode="after")
    def adoption_names_reporting_identity(self) -> "InvitationCreate":
        if (
            self.intent == "adopt-static"
            and self.expected.reporting_node_id is None
        ):
            raise ValueError("adoption requires an expected reporting identity")
        return self


class ClaimingMac(StrictPairingModel):
    platform: Literal["macos"]
    service_version: BoundedVersion
    display_name: str = Field(min_length=1, max_length=128)
    reporting_node_id: BoundedIdentifier

    @field_validator("display_name")
    @classmethod
    def display_name_is_bounded_text(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("display_name must be bounded printable text")
        return value


class SupportedPairingProtocol(StrictPairingModel):
    minimum: int = Field(strict=True, ge=1, le=1)
    maximum: int = Field(strict=True, ge=1, le=1)


class InvitationClaim(PairingPayload):
    request_id: CanonicalUUID
    invitation_id: CanonicalUUID
    pairing_secret: SecretStr = Field(min_length=32, max_length=4096, repr=False)
    mac: ClaimingMac
    locator: SecretStr = Field(min_length=1, max_length=2048, repr=False)
    supported_protocol: SupportedPairingProtocol


class ClaimApproval(PairingPayload):
    request_id: CanonicalUUID
    locator: SecretStr = Field(min_length=1, max_length=2048, repr=False)
    service_class: Literal["primary", "opportunistic", "overflow"]
    hub_enabled: bool


class ClaimRejection(PairingPayload):
    request_id: CanonicalUUID


class ClaimProvision(PairingPayload):
    request_id: CanonicalUUID
    pairing_secret: SecretStr = Field(min_length=32, max_length=4096, repr=False)


class ActivationAcknowledgement(PairingPayload):
    request_id: CanonicalUUID
    credential_generation: int = Field(strict=True, ge=1, le=(1 << 63) - 1)
    reporting_node_id: BoundedIdentifier
    service_instance_id: BoundedIdentifier


class EnrollmentPolicyUpdate(PairingPayload):
    request_id: CanonicalUUID
    enabled: bool


class EnrollmentRevocation(PairingPayload):
    request_id: CanonicalUUID


class EnrollmentSelfManagement(PairingPayload):
    """One exact Mac-authorized lifecycle mutation.

    The pairing identifier deliberately appears in both the request target and
    the bounded body.  Together with the reporting identity and credential
    generation this prevents a valid management bearer from being replayed
    against a different enrollment or generation.
    """

    request_id: CanonicalUUID
    pairing_id: CanonicalUUID
    reporting_node_id: BoundedIdentifier
    credential_generation: int = Field(strict=True, ge=1, le=(1 << 63) - 1)


_PayloadT = TypeVar("_PayloadT", bound=PairingPayload)


async def parse_pairing_payload(
    request: Request,
    payload_type: type[_PayloadT],
) -> _PayloadT:
    """Parse a strict, bounded JSON body without reflecting invalid input.

    FastAPI's ordinary validation response includes fragments of invalid input.
    Pairing bodies contain invitation and management material, so these routes
    parse manually and collapse every syntax/schema failure to one fixed code.
    """

    encoding = request.headers.get("content-encoding", "identity").lower()
    if encoding != "identity":
        raise PairingAPIError(415, "pairing_content_encoding_unsupported")
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise PairingAPIError(415, "pairing_json_required")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise PairingAPIError(400, "pairing_invalid_request") from None
        if declared_size < 0:
            raise PairingAPIError(400, "pairing_invalid_request")
        if declared_size > MAX_PAIRING_REQUEST_BYTES:
            raise PairingAPIError(413, "pairing_request_too_large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_PAIRING_REQUEST_BYTES:
            raise PairingAPIError(413, "pairing_request_too_large")
    try:
        return payload_type.model_validate_json(bytes(body))
    except (ValidationError, ValueError, TypeError):
        raise PairingAPIError(400, "pairing_invalid_request") from None


def bearer_token(request: Request) -> str | None:
    value = request.headers.get("authorization")
    if not value or not value.startswith("Bearer "):
        return None
    token = value[7:]
    return token if token else None


def bearer_matches(request: Request, expected: str) -> bool:
    candidate = bearer_token(request)
    return candidate is not None and hmac.compare_digest(candidate, expected)
