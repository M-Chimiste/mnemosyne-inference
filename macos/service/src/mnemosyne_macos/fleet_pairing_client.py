"""Bounded outbound client for the version-1 Nyx pairing ceremony.

The client is deliberately isolated from inference, residency, downloads,
storage, participation, and usage.  Its only durable effects are a secret-free
request journal plus the identity and credentials committed by
``FleetPairingStore``.  Callers must present the original invitation material
again to resume; the journal retains only domain-separated fingerprints.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import hmac
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Awaitable, Callable, Literal, Mapping, TypeVar
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
)
from .fleet_pairing import (
    FleetPairingError,
    FleetPairingStore,
    LegacyFleetCredentialsPresent,
    PairingCredentials,
    PairingState,
    _safe_node_url,
)


PAIRING_PROTOCOL_VERSION = 1
MAX_PAIRING_REQUEST_BYTES = 16 * 1024
MAX_PAIRING_RESPONSE_BYTES = 32 * 1024
PAIRING_CONNECT_TIMEOUT_SECONDS = 5.0
PAIRING_READ_TIMEOUT_SECONDS = 15.0
PAIRING_WRITE_TIMEOUT_SECONDS = 10.0
PAIRING_POOL_TIMEOUT_SECONDS = 5.0
PAIRING_TOTAL_TIMEOUT_SECONDS = 20.0
PAIRING_MAX_CONNECTIONS = 2
PAIRING_MAX_KEEPALIVE_CONNECTIONS = 1
MAX_RETIRED_SELF_REVOKE_IDS = 128


class PairingClientPhase(StrEnum):
    CLAIMING = "claiming"
    AWAITING_APPROVAL = "awaiting_approval"
    STAGING = "staging"
    ACTIVATION_PENDING = "activation_pending"
    COMPLETE = "complete"


class PairingClientErrorCode(StrEnum):
    NO_ATTEMPT = "pairing_no_attempt"
    PAYLOAD_MISMATCH = "pairing_payload_mismatch"
    STATIC_CREDENTIALS_PRESENT = "pairing_static_credentials_present"
    LOCAL_IDENTITY_INVALID = "pairing_local_identity_invalid"
    STATE_CONFLICT = "pairing_state_conflict"
    HUB_UNAVAILABLE = "pairing_hub_unavailable"
    HUB_REDIRECT_REFUSED = "pairing_hub_redirect_refused"
    HUB_RESPONSE_TOO_LARGE = "pairing_hub_response_too_large"
    HUB_RESPONSE_INVALID = "pairing_hub_response_invalid"
    CLAIM_REJECTED = "pairing_claim_rejected"
    APPROVAL_PENDING = "pairing_approval_pending"
    ACTIVATION_REJECTED = "pairing_activation_rejected"
    MANAGEMENT_REJECTED = "pairing_management_rejected"
    MANAGEMENT_OUTCOME_UNKNOWN = "pairing_management_outcome_unknown"
    EXPIRED = "pairing_expired"


class PairingManagementState(StrEnum):
    DISABLED = "disabled"
    REVOKED = "revoked"
    NOT_PAIRED = "not_paired"
    ADMIN_ACTION_REQUIRED = "admin_action_required"


class _SelfRevokePhase(StrEnum):
    PENDING = "pending"
    HUB_COMMITTED = "hub_committed"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class _SelfRevokeRecord:
    request_id: str
    attempt_id: str
    invitation_id: str
    pairing_id: str
    reporting_node_id: str
    credential_generation: int
    credential_fingerprint: str
    phase: _SelfRevokePhase

    def public_payload(self) -> dict[str, object]:
        """Return the secret-free retry identity for local control clients."""

        return {
            "schema_version": PAIRING_PROTOCOL_VERSION,
            "request_id": self.request_id,
            "phase": self.phase.value,
        }


_ERROR_MESSAGES: dict[PairingClientErrorCode, str] = {
    PairingClientErrorCode.NO_ATTEMPT: "No resumable Fleet pairing attempt exists.",
    PairingClientErrorCode.PAYLOAD_MISMATCH: (
        "The pairing details do not match the durable attempt."
    ),
    PairingClientErrorCode.STATIC_CREDENTIALS_PRESENT: (
        "Existing static Fleet credentials require explicit migration."
    ),
    PairingClientErrorCode.LOCAL_IDENTITY_INVALID: (
        "The local reporting identity cannot be paired."
    ),
    PairingClientErrorCode.STATE_CONFLICT: (
        "The local pairing state conflicts with this attempt."
    ),
    PairingClientErrorCode.HUB_UNAVAILABLE: (
        "The Hub is temporarily unavailable; the exact request can be resumed."
    ),
    PairingClientErrorCode.HUB_REDIRECT_REFUSED: (
        "The Hub returned a redirect, which pairing does not follow."
    ),
    PairingClientErrorCode.HUB_RESPONSE_TOO_LARGE: (
        "The Hub pairing response exceeded its size limit."
    ),
    PairingClientErrorCode.HUB_RESPONSE_INVALID: (
        "The Hub returned an invalid pairing response."
    ),
    PairingClientErrorCode.CLAIM_REJECTED: (
        "The Hub did not accept this pairing claim."
    ),
    PairingClientErrorCode.APPROVAL_PENDING: (
        "The pairing claim is waiting for Hub approval."
    ),
    PairingClientErrorCode.ACTIVATION_REJECTED: (
        "The Hub did not confirm pairing activation."
    ),
    PairingClientErrorCode.MANAGEMENT_REJECTED: (
        "The Hub did not accept this pairing management request."
    ),
    PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN: (
        "The Hub outcome is unknown; retry the exact request ID."
    ),
    PairingClientErrorCode.EXPIRED: "The pairing invitation has expired.",
}


class PairingClientError(RuntimeError):
    """Fixed, secret-free error suitable for the loopback control API."""

    def __init__(
        self,
        code: PairingClientErrorCode,
        *,
        status_code: int,
        retryable: bool,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(_ERROR_MESSAGES[code])

    @property
    def public_message(self) -> str:
        return _ERROR_MESSAGES[self.code]


@dataclass(frozen=True, slots=True)
class PairingInvitation:
    invitation_id: str
    pairing_secret: str = field(repr=False)
    hub_origin: str
    locator: str = field(repr=False)

    def validated(self) -> "PairingInvitation":
        invitation_id = _canonical_uuid(
            self.invitation_id,
            field_name="invitation_id",
        )
        if (
            not isinstance(self.pairing_secret, str)
            or self.pairing_secret != self.pairing_secret.strip()
            or not 32 <= len(self.pairing_secret) <= 4096
            or any(character in self.pairing_secret for character in "\r\n\0")
        ):
            raise ValueError("invalid pairing secret")
        hub_origin = _verified_https_origin(self.hub_origin)
        locator = _safe_node_url(self.locator)
        return PairingInvitation(
            invitation_id=invitation_id,
            pairing_secret=self.pairing_secret,
            hub_origin=hub_origin,
            locator=locator,
        )


@dataclass(frozen=True, slots=True)
class PresencePairingInvitation:
    invitation: PairingInvitation = field(repr=False)
    presence_pin: str = field(repr=False)
    expires_at: float


@dataclass(frozen=True, slots=True)
class PairingClientRecord:
    phase: PairingClientPhase
    attempt_id: str
    invitation_id: str
    claim_request_id: str
    provision_request_id: str
    activation_request_id: str
    claim_id: str | None
    pairing_id: str | None
    reporting_node_id: str
    credential_generation: int | None
    expires_at: float | None
    last_error_code: PairingClientErrorCode | None
    created_at: float
    updated_at: float

    def public_payload(self) -> dict[str, object]:
        return {
            "schema_version": PAIRING_PROTOCOL_VERSION,
            "phase": self.phase.value,
            "attempt_id": self.attempt_id,
            "invitation_id": self.invitation_id,
            "claim_request_id": self.claim_request_id,
            "provision_request_id": self.provision_request_id,
            "activation_request_id": self.activation_request_id,
            "claim_id": self.claim_id,
            "pairing_id": self.pairing_id,
            "reporting_node_id": self.reporting_node_id,
            "credential_generation": self.credential_generation,
            "expires_at": self.expires_at,
            "last_error_code": (
                self.last_error_code.value if self.last_error_code else None
            ),
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class PairingManagementResult:
    state: PairingManagementState
    pairing_id: str | None
    reporting_node_id: str | None
    credential_generation: int | None

    def public_payload(self) -> dict[str, object]:
        return {
            "schema_version": PAIRING_PROTOCOL_VERSION,
            "state": self.state.value,
            "pairing_id": self.pairing_id,
            "reporting_node_id": self.reporting_node_id,
            "credential_generation": self.credential_generation,
        }


class _StrictResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        str_strip_whitespace=False,
    )


class _ClaimResponse(_StrictResponse):
    schema_version: Literal[1]
    claim_id: str = Field(min_length=36, max_length=36)
    invitation_id: str = Field(min_length=36, max_length=36)
    pairing_id: str = Field(min_length=36, max_length=36)
    display_name: str = Field(min_length=1, max_length=128)
    reporting_node_id: str = Field(min_length=1, max_length=128)
    service_version: str = Field(min_length=1, max_length=64)
    platform: Literal["macos"]
    protocol_version: Literal[1]
    state: Literal["claimed"]
    claimed_at: float
    expires_at: float
    locator_accepted: Literal[True]


class _PresenceRequestResponse(_StrictResponse):
    schema_version: Literal[1]
    invitation_id: str = Field(min_length=36, max_length=36)
    pairing_secret: SecretStr = Field(min_length=32, max_length=4096, repr=False)
    presence_pin: str = Field(pattern=r"^[0-9]{6}$", repr=False)
    hub_origin: str = Field(min_length=1, max_length=2048)
    expires_at: float
    state: Literal["issued"]


class _ProvisionCredentials(_StrictResponse):
    snapshot_bearer: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    dispatch_bearer: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    management_bearer: SecretStr = Field(min_length=1, max_length=4096, repr=False)


class _ProvisionResponse(_StrictResponse):
    schema_version: Literal[1]
    claim_id: str = Field(min_length=36, max_length=36)
    pairing_id: str = Field(min_length=36, max_length=36)
    reporting_node_id: str = Field(min_length=1, max_length=128)
    credential_generation: int = Field(strict=True, ge=1, le=(1 << 63) - 1)
    credentials: _ProvisionCredentials = Field(repr=False)
    state: Literal["provisioning"]


class _ActivationResponse(_StrictResponse):
    schema_version: Literal[1]
    pairing_id: str = Field(min_length=36, max_length=36)
    reporting_node_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    platform: Literal["macos"]
    service_version: str = Field(min_length=1, max_length=64)
    protocol_version: Literal[1]
    service_class: Literal["primary", "opportunistic", "overflow"]
    state: Literal["disabled", "active"]
    hub_enabled: bool
    credential_generation: int = Field(strict=True, ge=1, le=(1 << 63) - 1)
    created_at: float
    updated_at: float
    revoked_at: float | None
    failure_code: str | None = Field(default=None, max_length=128)
    activation_complete: Literal[True]


class _ManagementResponse(_StrictResponse):
    schema_version: Literal[1]
    pairing_id: str = Field(min_length=36, max_length=36)
    reporting_node_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    platform: Literal["macos"]
    service_version: str = Field(min_length=1, max_length=64)
    protocol_version: Literal[1]
    service_class: Literal["primary", "opportunistic", "overflow"]
    state: Literal["disabled", "revoked"]
    hub_enabled: Literal[False]
    credential_generation: int = Field(strict=True, ge=1, le=(1 << 63) - 1)
    created_at: float
    updated_at: float
    revoked_at: float | None
    failure_code: str | None = Field(default=None, max_length=128)


_T = TypeVar("_T")


async def _await_blocking(
    function: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Finish a SQLite outcome before replaying caller cancellation."""

    task = asyncio.create_task(
        asyncio.to_thread(function, *args, **kwargs),
        name="mnemosyne-fleet-pairing-client-journal",
    )
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    if cancellation is not None:
        try:
            task.result()
        except BaseException:
            pass
        raise cancellation
    return task.result()


async def _await_async_outcome(task: asyncio.Task[_T]) -> _T:
    """Finish an already-started authority transition before cancellation.

    Once Nyx has confirmed a revocation, cancellation must not be able to
    strand the process-local routing cache in its previous paired state.  This
    mirrors the journal helper above, but owns one bounded async sequence that
    includes both the durable store mutation and its cache notification.
    """

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    if cancellation is not None:
        try:
            task.result()
        except BaseException:
            pass
        raise cancellation
    return task.result()


def _canonical_uuid(value: str, *, field_name: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} is invalid") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError(f"{field_name} is invalid")
    return canonical


def _verified_https_origin(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("hub origin is invalid")
    if value != value.strip() or any(ord(character) < 33 for character in value):
        raise ValueError("hub origin is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or "%" in parsed.netloc
    ):
        raise ValueError("hub origin must be a verified HTTPS origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("hub origin is invalid") from exc
    host = parsed.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"https://{authority}"


def _fingerprint(label: str, value: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"mnemosyne-native-pairing-client-v1\0")
    digest.update(label.encode("ascii"))
    digest.update(b"\0")
    digest.update(value.encode("utf-8"))
    return digest.hexdigest()


def _valid_fingerprint(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


class _PairingClientJournal:
    """Secret-free, single-attempt request-ID journal."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self) -> PairingClientRecord | None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS native_fleet_pairing_client_v1 (
                    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                    phase TEXT NOT NULL CHECK (
                        phase IN (
                            'claiming', 'awaiting_approval', 'staging',
                            'activation_pending', 'complete'
                        )
                    ),
                    attempt_id TEXT NOT NULL UNIQUE,
                    invitation_id TEXT NOT NULL,
                    invitation_secret_fingerprint TEXT NOT NULL,
                    hub_origin_fingerprint TEXT NOT NULL,
                    locator_fingerprint TEXT NOT NULL,
                    claim_request_id TEXT NOT NULL UNIQUE,
                    provision_request_id TEXT NOT NULL UNIQUE,
                    activation_request_id TEXT NOT NULL UNIQUE,
                    claim_id TEXT,
                    pairing_id TEXT,
                    reporting_node_id TEXT NOT NULL,
                    credential_generation INTEGER,
                    expires_at REAL,
                    last_error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS native_fleet_self_revoke_v1 (
                    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                    request_id TEXT NOT NULL UNIQUE,
                    attempt_id TEXT NOT NULL,
                    invitation_id TEXT NOT NULL,
                    pairing_id TEXT NOT NULL,
                    reporting_node_id TEXT NOT NULL,
                    credential_generation INTEGER NOT NULL CHECK (
                        credential_generation > 0
                    ),
                    credential_fingerprint TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (
                        phase IN ('pending', 'hub_committed', 'complete')
                    ),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS native_fleet_retired_self_revoke_v1 (
                    request_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    invitation_id TEXT NOT NULL,
                    pairing_id TEXT NOT NULL,
                    reporting_node_id TEXT NOT NULL,
                    credential_generation INTEGER NOT NULL CHECK (
                        credential_generation > 0
                    ),
                    credential_fingerprint TEXT NOT NULL,
                    retired_at REAL NOT NULL
                )
                """
            )
            self_revoke_columns = {
                str(column["name"])
                for column in connection.execute(
                    "PRAGMA table_info(native_fleet_self_revoke_v1)"
                ).fetchall()
            }
            retired_columns = {
                str(column["name"])
                for column in connection.execute(
                    "PRAGMA table_info(native_fleet_retired_self_revoke_v1)"
                ).fetchall()
            }
            for column in (
                "attempt_id",
                "invitation_id",
                "credential_fingerprint",
            ):
                if column not in self_revoke_columns:
                    connection.execute(
                        "ALTER TABLE native_fleet_self_revoke_v1 "
                        f"ADD COLUMN {column} TEXT"
                    )
                if column not in retired_columns:
                    connection.execute(
                        "ALTER TABLE native_fleet_retired_self_revoke_v1 "
                        f"ADD COLUMN {column} TEXT"
                    )
            retired_rows = connection.execute(
                """
                SELECT * FROM native_fleet_retired_self_revoke_v1
                 LIMIT ?
                """,
                (MAX_RETIRED_SELF_REVOKE_IDS + 1,),
            ).fetchall()
            if len(retired_rows) > MAX_RETIRED_SELF_REVOKE_IDS:
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                )
            for retired_row in retired_rows:
                self._validate_retired_self_revoke_row(retired_row)
            self._self_revoke_record(self._self_revoke_row(connection))
            return self._record(self._row(connection))

    def status(self) -> PairingClientRecord | None:
        with self._connect() as connection:
            return self._record(self._row(connection))

    def self_revoke_status(self) -> _SelfRevokeRecord | None:
        with self._connect() as connection:
            return self._self_revoke_record(
                self._self_revoke_row(connection)
            )

    def prepare_self_revoke(
        self,
        *,
        request_id: str,
        attempt_id: str,
        invitation_id: str,
        pairing_id: str,
        reporting_node_id: str,
        credential_generation: int,
        credential_fingerprint: str,
        hub_origin: str,
        node_url: str,
        on_pending: Callable[[], None] | None = None,
    ) -> _SelfRevokeRecord:
        try:
            request_id = _canonical_uuid(request_id, field_name="request_id")
            attempt_id = _canonical_uuid(attempt_id, field_name="attempt_id")
            invitation_id = _canonical_uuid(
                invitation_id,
                field_name="invitation_id",
            )
            pairing_id = _canonical_uuid(pairing_id, field_name="pairing_id")
            reporting_node_id = _bounded_identifier(reporting_node_id)
            if credential_generation < 1 or not _valid_fingerprint(
                credential_fingerprint
            ):
                raise ValueError("invalid self-revoke authority")
        except (TypeError, ValueError):
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=409,
                retryable=False,
            ) from None
        requested = (
            request_id,
            attempt_id,
            invitation_id,
            pairing_id,
            reporting_node_id,
            credential_generation,
            credential_fingerprint,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._retired_self_revoke_exists(connection, request_id):
                raise PairingClientError(
                    PairingClientErrorCode.PAYLOAD_MISMATCH,
                    status_code=409,
                    retryable=False,
                )
            client_row = self._row(connection)
            if (
                client_row is None
                or client_row["phase"] != PairingClientPhase.COMPLETE.value
                or client_row["attempt_id"] != attempt_id
                or client_row["invitation_id"] != invitation_id
                or client_row["pairing_id"] != pairing_id
                or client_row["reporting_node_id"] != reporting_node_id
                or client_row["credential_generation"] != credential_generation
                or not hmac.compare_digest(
                    str(client_row["hub_origin_fingerprint"]),
                    _fingerprint("hub-origin", hub_origin),
                )
                or not hmac.compare_digest(
                    str(client_row["locator_fingerprint"]),
                    _fingerprint("node-locator", node_url),
                )
            ):
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                )
            row = self._self_revoke_row(connection)
            if row is not None:
                existing = (
                    str(row["request_id"]),
                    str(row["attempt_id"]),
                    str(row["invitation_id"]),
                    str(row["pairing_id"]),
                    str(row["reporting_node_id"]),
                    int(row["credential_generation"]),
                    str(row["credential_fingerprint"]),
                )
                if existing != requested:
                    raise PairingClientError(
                        PairingClientErrorCode.PAYLOAD_MISMATCH,
                        status_code=409,
                        retryable=False,
                    )
                record = self._self_revoke_record(row)
                assert record is not None
                if on_pending is not None:
                    on_pending()
                return record
            if self._retired_self_revoke_count(connection) >= (
                MAX_RETIRED_SELF_REVOKE_IDS
            ):
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                )
            now = time.time()
            connection.execute(
                """
                INSERT INTO native_fleet_self_revoke_v1 (
                    singleton, request_id, attempt_id, invitation_id,
                    pairing_id, reporting_node_id, credential_generation,
                    credential_fingerprint, phase, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *requested,
                    _SelfRevokePhase.PENDING.value,
                    now,
                    now,
                ),
            )
            record = self._self_revoke_record(
                self._self_revoke_row(connection)
            )
            assert record is not None
            if on_pending is not None:
                on_pending()
            return record

    def record_self_revoke_hub_committed(
        self,
        request_id: str,
    ) -> _SelfRevokeRecord:
        return self._set_self_revoke_phase(
            request_id,
            _SelfRevokePhase.HUB_COMMITTED,
        )

    def record_self_revoke_complete(
        self,
        request_id: str,
    ) -> _SelfRevokeRecord:
        return self._set_self_revoke_phase(
            request_id,
            _SelfRevokePhase.COMPLETE,
        )

    def abort_pending_self_revoke(
        self,
        request_id: str,
        *,
        on_aborted: Callable[[], None] | None = None,
    ) -> _SelfRevokeRecord:
        """Retire one proven-rejected intent before reopening local authority.

        The callback deliberately runs only after the SQLite commit.  Callers
        use it to clear the process-local denial latch, so cancellation cannot
        expose the old pairing while the durable pending fence still exists.
        """

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._self_revoke_row(connection)
            if (
                row is None
                or str(row["request_id"]) != request_id
                or row["phase"] != _SelfRevokePhase.PENDING.value
            ):
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                )
            if self._retired_self_revoke_exists(connection, request_id):
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                )
            retired_count = self._retired_self_revoke_count(connection)
            if retired_count >= MAX_RETIRED_SELF_REVOKE_IDS:
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                )
            record = self._self_revoke_record(row)
            assert record is not None
            now = time.time()
            connection.execute(
                """
                INSERT INTO native_fleet_retired_self_revoke_v1 (
                    request_id, attempt_id, invitation_id, pairing_id,
                    reporting_node_id, credential_generation,
                    credential_fingerprint, retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.request_id,
                    record.attempt_id,
                    record.invitation_id,
                    record.pairing_id,
                    record.reporting_node_id,
                    record.credential_generation,
                    record.credential_fingerprint,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM native_fleet_self_revoke_v1 WHERE singleton=1"
            )
            connection.commit()
        if on_aborted is not None:
            on_aborted()
        return record

    def _set_self_revoke_phase(
        self,
        request_id: str,
        phase: _SelfRevokePhase,
    ) -> _SelfRevokeRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._self_revoke_row(connection)
            if row is None or str(row["request_id"]) != request_id:
                raise PairingClientError(
                    PairingClientErrorCode.PAYLOAD_MISMATCH,
                    status_code=409,
                    retryable=False,
                )
            current = _SelfRevokePhase(str(row["phase"]))
            allowed = {
                _SelfRevokePhase.HUB_COMMITTED: {
                    _SelfRevokePhase.PENDING,
                    _SelfRevokePhase.HUB_COMMITTED,
                },
                _SelfRevokePhase.COMPLETE: {
                    _SelfRevokePhase.HUB_COMMITTED,
                    _SelfRevokePhase.COMPLETE,
                },
            }[phase]
            if current not in allowed:
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                )
            connection.execute(
                """
                UPDATE native_fleet_self_revoke_v1
                   SET phase=?, updated_at=? WHERE singleton=1
                """,
                (phase.value, time.time()),
            )
            record = self._self_revoke_record(
                self._self_revoke_row(connection)
            )
            assert record is not None
            return record

    def prepare(
        self,
        invitation: PairingInvitation,
        *,
        reporting_node_id: str,
        allow_create: bool,
    ) -> tuple[PairingClientRecord, bool]:
        fingerprints = (
            _fingerprint("invitation-secret", invitation.pairing_secret),
            _fingerprint("hub-origin", invitation.hub_origin),
            _fingerprint("node-locator", invitation.locator),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            if row is not None:
                matches = (
                    row["invitation_id"] == invitation.invitation_id
                    and hmac.compare_digest(
                        str(row["invitation_secret_fingerprint"]),
                        fingerprints[0],
                    )
                    and hmac.compare_digest(
                        str(row["hub_origin_fingerprint"]), fingerprints[1]
                    )
                    and hmac.compare_digest(
                        str(row["locator_fingerprint"]), fingerprints[2]
                    )
                    and row["reporting_node_id"] == reporting_node_id
                )
                if not matches:
                    raise PairingClientError(
                        PairingClientErrorCode.PAYLOAD_MISMATCH,
                        status_code=409,
                        retryable=False,
                    )
                connection.commit()
                record = self._record(row)
                assert record is not None
                return record, False
            if not allow_create:
                raise PairingClientError(
                    PairingClientErrorCode.NO_ATTEMPT,
                    status_code=409,
                    retryable=False,
                )
            now = time.time()
            connection.execute(
                """
                INSERT INTO native_fleet_pairing_client_v1 (
                    singleton, phase, attempt_id, invitation_id,
                    invitation_secret_fingerprint, hub_origin_fingerprint,
                    locator_fingerprint, claim_request_id,
                    provision_request_id, activation_request_id,
                    reporting_node_id, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    PairingClientPhase.CLAIMING.value,
                    str(uuid4()),
                    invitation.invitation_id,
                    *fingerprints,
                    str(uuid4()),
                    str(uuid4()),
                    str(uuid4()),
                    reporting_node_id,
                    now,
                    now,
                ),
            )
            record = self._record(self._row(connection))
            assert record is not None
            return record, True

    def replace_completed_revoke_and_prepare(
        self,
        connection: sqlite3.Connection,
        pairing_row: sqlite3.Row,
        invitation: PairingInvitation,
        reporting_node_id: str,
        attempt_id: str,
    ) -> PairingClientRecord:
        """Replace both old singletons inside the store's guarded transaction."""

        client_row = self._row(connection)
        revoke_row = self._self_revoke_row(connection)
        if (
            client_row is None
            or revoke_row is None
            or client_row["phase"] != PairingClientPhase.COMPLETE.value
            or revoke_row["phase"] != _SelfRevokePhase.COMPLETE.value
            or client_row["attempt_id"] != revoke_row["attempt_id"]
            or client_row["invitation_id"] != revoke_row["invitation_id"]
            or client_row["pairing_id"] != revoke_row["pairing_id"]
            or client_row["reporting_node_id"]
            != revoke_row["reporting_node_id"]
            or client_row["credential_generation"]
            != revoke_row["credential_generation"]
            or not revoke_row["credential_fingerprint"]
        ):
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=409,
                retryable=False,
            )

        pairing_state = PairingState(str(pairing_row["state"]))
        if pairing_state is not PairingState.REVOKED or (
            pairing_row["attempt_id"] != client_row["attempt_id"]
            or pairing_row["pairing_id"] != client_row["pairing_id"]
            or pairing_row["node_id"] != client_row["reporting_node_id"]
            or pairing_row["credential_epoch"]
            != client_row["credential_generation"]
            or not pairing_row["hub_origin"]
            or not pairing_row["node_url"]
            or not hmac.compare_digest(
                str(client_row["hub_origin_fingerprint"]),
                _fingerprint(
                    "hub-origin",
                    str(pairing_row["hub_origin"]),
                ),
            )
            or not hmac.compare_digest(
                str(client_row["locator_fingerprint"]),
                _fingerprint(
                    "node-locator",
                    str(pairing_row["node_url"]),
                ),
            )
        ):
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=409,
                retryable=False,
            )

        retired_count = self._retired_self_revoke_count(connection)
        retired_request_id = str(revoke_row["request_id"])
        if (
            retired_count >= MAX_RETIRED_SELF_REVOKE_IDS
            or self._retired_self_revoke_exists(
                connection,
                retired_request_id,
            )
        ):
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=409,
                retryable=False,
            )
        if (
            invitation.invitation_id == str(revoke_row["invitation_id"])
            or self._retired_self_revoke_authority_exists(
                connection,
                invitation_id=invitation.invitation_id,
            )
        ):
            raise PairingClientError(
                PairingClientErrorCode.PAYLOAD_MISMATCH,
                status_code=409,
                retryable=False,
            )

        fingerprints = (
            _fingerprint("invitation-secret", invitation.pairing_secret),
            _fingerprint("hub-origin", invitation.hub_origin),
            _fingerprint("node-locator", invitation.locator),
        )
        now = time.time()
        connection.execute(
            """
            INSERT INTO native_fleet_retired_self_revoke_v1 (
                request_id, attempt_id, invitation_id, pairing_id,
                reporting_node_id, credential_generation,
                credential_fingerprint, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                retired_request_id,
                str(revoke_row["attempt_id"]),
                str(revoke_row["invitation_id"]),
                str(revoke_row["pairing_id"]),
                str(revoke_row["reporting_node_id"]),
                int(revoke_row["credential_generation"]),
                str(revoke_row["credential_fingerprint"]),
                now,
            ),
        )
        connection.execute(
            "DELETE FROM native_fleet_pairing_client_v1 WHERE singleton=1"
        )
        connection.execute(
            "DELETE FROM native_fleet_self_revoke_v1 WHERE singleton=1"
        )
        connection.execute(
            """
            INSERT INTO native_fleet_pairing_client_v1 (
                singleton, phase, attempt_id, invitation_id,
                invitation_secret_fingerprint, hub_origin_fingerprint,
                locator_fingerprint, claim_request_id,
                provision_request_id, activation_request_id,
                reporting_node_id, created_at, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                PairingClientPhase.CLAIMING.value,
                attempt_id,
                invitation.invitation_id,
                *fingerprints,
                str(uuid4()),
                str(uuid4()),
                str(uuid4()),
                reporting_node_id,
                now,
                now,
            ),
        )
        return self._required_record(self._require_row(connection))

    def record_claim(self, response: _ClaimResponse) -> PairingClientRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_row(connection)
            if row["phase"] != PairingClientPhase.CLAIMING.value:
                if (
                    row["claim_id"] == response.claim_id
                    and row["pairing_id"] == response.pairing_id
                ):
                    connection.commit()
                    return self._required_record(row)
                raise self._conflict()
            if self._retired_self_revoke_authority_exists(
                connection,
                pairing_id=response.pairing_id,
            ):
                raise self._invalid_response()
            now = time.time()
            connection.execute(
                """
                UPDATE native_fleet_pairing_client_v1
                   SET phase=?, claim_id=?, pairing_id=?, expires_at=?,
                       last_error_code=NULL, updated_at=?
                 WHERE singleton=1
                """,
                (
                    PairingClientPhase.AWAITING_APPROVAL.value,
                    response.claim_id,
                    response.pairing_id,
                    response.expires_at,
                    now,
                ),
            )
            return self._required_record(self._require_row(connection))

    def record_staging(
        self,
        response: _ProvisionResponse,
    ) -> PairingClientRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_row(connection)
            if row["phase"] not in {
                PairingClientPhase.AWAITING_APPROVAL.value,
                PairingClientPhase.STAGING.value,
            }:
                if (
                    row["pairing_id"] == response.pairing_id
                    and row["credential_generation"]
                    == response.credential_generation
                ):
                    connection.commit()
                    return self._required_record(row)
                raise self._conflict()
            if (
                row["claim_id"] != response.claim_id
                or row["pairing_id"] != response.pairing_id
                or row["reporting_node_id"] != response.reporting_node_id
            ):
                raise self._conflict()
            generation = row["credential_generation"]
            if generation is not None and generation != response.credential_generation:
                raise self._conflict()
            if self._retired_self_revoke_authority_exists(
                connection,
                credential_fingerprint=_credentials_from_response(
                    response
                ).fingerprint(),
            ):
                raise self._invalid_response()
            connection.execute(
                """
                UPDATE native_fleet_pairing_client_v1
                   SET phase=?, credential_generation=?, last_error_code=NULL,
                       updated_at=? WHERE singleton=1
                """,
                (
                    PairingClientPhase.STAGING.value,
                    response.credential_generation,
                    time.time(),
                ),
            )
            return self._required_record(self._require_row(connection))

    def record_activation_pending(self) -> PairingClientRecord:
        return self._set_phase(PairingClientPhase.ACTIVATION_PENDING)

    def record_complete(self) -> PairingClientRecord:
        return self._set_phase(PairingClientPhase.COMPLETE)

    def record_error(
        self,
        code: PairingClientErrorCode,
    ) -> PairingClientRecord | None:
        with self._connect() as connection:
            if self._row(connection) is None:
                return None
            connection.execute(
                """
                UPDATE native_fleet_pairing_client_v1
                   SET last_error_code=?, updated_at=? WHERE singleton=1
                """,
                (code.value, time.time()),
            )
            return self._record(self._row(connection))

    def discard_rejected_unclaimed_attempt(
        self,
        connection: sqlite3.Connection,
        pairing_row: sqlite3.Row,
        attempt_id: str,
    ) -> None:
        """Delete only the journal row proven to have no remote claim.

        The caller owns the shared SQLite transaction and independently proves
        that the pairing store names the same credential-free attempt.  A
        transport-ambiguous error is deliberately ineligible because Nyx may
        already have committed its claim.
        """

        row = self._row(connection)
        self_revoke = self._self_revoke_row(connection)
        exact_rejected_claim = (
            row is not None
            and self_revoke is None
            and row["attempt_id"] == attempt_id
            and pairing_row["attempt_id"] == attempt_id
            and row["phase"] == PairingClientPhase.CLAIMING.value
            and row["claim_id"] is None
            and row["pairing_id"] is None
            and row["credential_generation"] is None
            and row["expires_at"] is None
            and row["last_error_code"]
            == PairingClientErrorCode.CLAIM_REJECTED.value
        )
        if not exact_rejected_claim:
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=409,
                retryable=False,
            )
        connection.execute(
            "DELETE FROM native_fleet_pairing_client_v1 WHERE singleton=1"
        )

    def _set_phase(self, phase: PairingClientPhase) -> PairingClientRecord:
        with self._connect() as connection:
            row = self._require_row(connection)
            allowed = {
                PairingClientPhase.ACTIVATION_PENDING: {
                    PairingClientPhase.STAGING.value,
                    PairingClientPhase.ACTIVATION_PENDING.value,
                },
                PairingClientPhase.COMPLETE: {
                    PairingClientPhase.ACTIVATION_PENDING.value,
                    PairingClientPhase.COMPLETE.value,
                },
            }[phase]
            if row["phase"] not in allowed:
                raise self._conflict()
            connection.execute(
                """
                UPDATE native_fleet_pairing_client_v1
                   SET phase=?, last_error_code=NULL, updated_at=?
                 WHERE singleton=1
                """,
                (phase.value, time.time()),
            )
            return self._required_record(self._require_row(connection))

    @staticmethod
    def _conflict() -> PairingClientError:
        return PairingClientError(
            PairingClientErrorCode.STATE_CONFLICT,
            status_code=409,
            retryable=False,
        )

    @staticmethod
    def _invalid_response() -> PairingClientError:
        return PairingClientError(
            PairingClientErrorCode.HUB_RESPONSE_INVALID,
            status_code=502,
            retryable=False,
        )

    @staticmethod
    def _row(connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM native_fleet_pairing_client_v1 WHERE singleton=1"
        ).fetchone()

    @staticmethod
    def _self_revoke_row(
        connection: sqlite3.Connection,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM native_fleet_self_revoke_v1 WHERE singleton=1"
        ).fetchone()

    @staticmethod
    def _retired_self_revoke_exists(
        connection: sqlite3.Connection,
        request_id: str,
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1 FROM native_fleet_retired_self_revoke_v1
                 WHERE request_id=?
                """,
                (request_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _retired_self_revoke_count(connection: sqlite3.Connection) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM native_fleet_retired_self_revoke_v1"
            ).fetchone()[0]
        )

    @staticmethod
    def _retired_self_revoke_authority_exists(
        connection: sqlite3.Connection,
        *,
        invitation_id: str | None = None,
        pairing_id: str | None = None,
        credential_fingerprint: str | None = None,
    ) -> bool:
        requested = {
            "invitation_id": invitation_id,
            "pairing_id": pairing_id,
            "credential_fingerprint": credential_fingerprint,
        }
        selected = [(field, value) for field, value in requested.items() if value]
        if len(selected) != 1:
            raise ValueError("one retired authority field is required")
        field, value = selected[0]
        return (
            connection.execute(
                f"SELECT 1 FROM native_fleet_retired_self_revoke_v1 "
                f"WHERE {field}=? LIMIT 1",
                (value,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _validate_retired_self_revoke_row(row: sqlite3.Row) -> None:
        try:
            _canonical_uuid(str(row["request_id"]), field_name="request_id")
            _canonical_uuid(str(row["attempt_id"]), field_name="attempt_id")
            _canonical_uuid(
                str(row["invitation_id"]),
                field_name="invitation_id",
            )
            _canonical_uuid(str(row["pairing_id"]), field_name="pairing_id")
            _bounded_identifier(str(row["reporting_node_id"]))
            credential_generation = int(row["credential_generation"])
            credential_fingerprint = str(row["credential_fingerprint"])
            retired_at = float(row["retired_at"])
            if (
                credential_generation < 1
                or not _valid_fingerprint(credential_fingerprint)
                or not math.isfinite(retired_at)
                or retired_at <= 0
            ):
                raise ValueError("invalid retired self-revoke authority")
        except (PairingClientError, TypeError, ValueError, OverflowError):
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=409,
                retryable=False,
            ) from None

    @staticmethod
    def _self_revoke_record(
        row: sqlite3.Row | None,
    ) -> _SelfRevokeRecord | None:
        if row is None:
            return None
        try:
            request_id = _canonical_uuid(
                str(row["request_id"]),
                field_name="request_id",
            )
            attempt_id = _canonical_uuid(
                str(row["attempt_id"]),
                field_name="attempt_id",
            )
            invitation_id = _canonical_uuid(
                str(row["invitation_id"]),
                field_name="invitation_id",
            )
            pairing_id = _canonical_uuid(
                str(row["pairing_id"]),
                field_name="pairing_id",
            )
            reporting_node_id = _bounded_identifier(
                str(row["reporting_node_id"])
            )
            credential_generation = int(row["credential_generation"])
            credential_fingerprint = str(row["credential_fingerprint"])
            if (
                credential_generation < 1
                or not _valid_fingerprint(credential_fingerprint)
            ):
                raise ValueError("invalid self-revoke authority")
            phase = _SelfRevokePhase(str(row["phase"]))
        except (PairingClientError, TypeError, ValueError, OverflowError):
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=409,
                retryable=False,
            ) from None
        return _SelfRevokeRecord(
            request_id=request_id,
            attempt_id=attempt_id,
            invitation_id=invitation_id,
            pairing_id=pairing_id,
            reporting_node_id=reporting_node_id,
            credential_generation=credential_generation,
            credential_fingerprint=credential_fingerprint,
            phase=phase,
        )

    def _require_row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = self._row(connection)
        if row is None:
            raise self._conflict()
        return row

    def _record(self, row: sqlite3.Row | None) -> PairingClientRecord | None:
        if row is None:
            return None
        error = row["last_error_code"]
        return PairingClientRecord(
            phase=PairingClientPhase(row["phase"]),
            attempt_id=str(row["attempt_id"]),
            invitation_id=str(row["invitation_id"]),
            claim_request_id=str(row["claim_request_id"]),
            provision_request_id=str(row["provision_request_id"]),
            activation_request_id=str(row["activation_request_id"]),
            claim_id=row["claim_id"],
            pairing_id=row["pairing_id"],
            reporting_node_id=str(row["reporting_node_id"]),
            credential_generation=(
                int(row["credential_generation"])
                if row["credential_generation"] is not None
                else None
            ),
            expires_at=(
                float(row["expires_at"]) if row["expires_at"] is not None else None
            ),
            last_error_code=(
                PairingClientErrorCode(str(error)) if error else None
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def _required_record(self, row: sqlite3.Row) -> PairingClientRecord:
        record = self._record(row)
        assert record is not None
        return record


class FleetPairingClient:
    """Advance one durable Mac-to-Nyx pairing transaction at a time."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        pairing_store: FleetPairingStore,
        reporting_node_id: str,
        service_version: str,
        service_instance_id: str,
        display_name: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        on_pairing_authority_changed: Callable[[], Awaitable[None]] | None = None,
        on_self_revoke_pending: Callable[[], None] | None = None,
        on_self_revoke_aborted: Callable[[], None] | None = None,
        on_completed_revoke_reset: Callable[[], None] | None = None,
    ) -> None:
        self._journal = _PairingClientJournal(database_path)
        self._pairing_store = pairing_store
        # Validate only when pairing is explicitly requested.  A malformed
        # legacy reporting identity must not make the ordinary local service
        # or its inference routes fail to construct or start.
        self._reporting_node_id = reporting_node_id
        self._display_name = display_name or reporting_node_id
        self._service_version = service_version
        self._service_instance_id = service_instance_id
        self._on_pairing_authority_changed = on_pairing_authority_changed
        self._on_self_revoke_pending = on_self_revoke_pending
        self._on_self_revoke_aborted = on_self_revoke_aborted
        self._on_completed_revoke_reset = on_completed_revoke_reset
        self._client = httpx.AsyncClient(
            verify=True,
            timeout=httpx.Timeout(
                connect=PAIRING_CONNECT_TIMEOUT_SECONDS,
                read=PAIRING_READ_TIMEOUT_SECONDS,
                write=PAIRING_WRITE_TIMEOUT_SECONDS,
                pool=PAIRING_POOL_TIMEOUT_SECONDS,
            ),
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=PAIRING_MAX_CONNECTIONS,
                max_keepalive_connections=PAIRING_MAX_KEEPALIVE_CONNECTIONS,
            ),
            transport=transport,
        )
        self._lock = asyncio.Lock()
        self._initialized = False
        self._closed = False

    async def initialize(self) -> PairingClientRecord | None:
        async with self._lock:
            self._ensure_open()
            record = await _await_blocking(self._journal.initialize)
            self._initialized = True
            return record

    async def status(self) -> PairingClientRecord | None:
        async with self._lock:
            self._ensure_ready()
            return await _await_blocking(self._journal.status)

    async def self_revoke_authority_denied(self) -> bool:
        """Return the durable local denial fence for this pairing identity."""

        async with self._lock:
            self._ensure_ready()
            return (
                await _await_blocking(self._journal.self_revoke_status)
                is not None
            )

    async def self_revoke_status(self) -> dict[str, object] | None:
        """Expose only the durable, secret-free local retry identity.

        A loopback UI must replay the exact request ID after an ambiguous Hub
        outcome.  Keeping that ID in this service-owned journal makes recovery
        survive either the menu app or service restarting without exposing any
        Fleet credential or remote locator.
        """

        async with self._lock:
            self._ensure_ready()
            record = await _await_blocking(self._journal.self_revoke_status)
            if record is None or record.phase is _SelfRevokePhase.COMPLETE:
                return None
            return record.public_payload()

    async def begin(self, invitation: PairingInvitation) -> PairingClientRecord:
        return await self._advance(invitation, allow_create=True)

    async def resume(self, invitation: PairingInvitation) -> PairingClientRecord:
        return await self._advance(invitation, allow_create=False)

    async def request_presence_invitation(
        self,
        *,
        request_id: str,
        hub_origin: str,
        locator: str,
        transport: str = "tailscale",
    ) -> PresencePairingInvitation:
        """Ask the Hub for a hidden invitation and short display code."""

        try:
            request_id = _canonical_uuid(request_id, field_name="request_id")
            hub_origin = _verified_https_origin(hub_origin)
            locator = _safe_node_url(locator)
            reporting_node_id = _bounded_identifier(self._reporting_node_id)
            display_name = _bounded_display_name(self._display_name)
            service_version = _bounded_identifier(
                self._service_version,
                maximum=64,
            )
            if transport not in {"https", "tailscale", "trusted_lan_http"}:
                raise ValueError("invalid pairing transport")
        except ValueError:
            raise PairingClientError(
                PairingClientErrorCode.PAYLOAD_MISMATCH,
                status_code=400,
                retryable=False,
            ) from None

        async with self._lock:
            self._ensure_ready()
            if await _await_blocking(self._journal.status) is not None:
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                )
            pairing = await self._pairing_store.status()
            if pairing.state not in {PairingState.UNPAIRED, PairingState.REVOKED}:
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                )
            response = await self._post(
                hub_origin,
                "/fleet/pairing/v1/requests",
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "mac": {
                        "platform": "macos",
                        "service_version": service_version,
                        "display_name": display_name,
                        "reporting_node_id": reporting_node_id,
                    },
                    "locator": locator,
                    "transport": transport,
                    "supported_protocol": {"minimum": 1, "maximum": 1},
                },
            )
            if response.status_code != 201:
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise self._hub_unavailable()
                raise PairingClientError(
                    PairingClientErrorCode.CLAIM_REJECTED,
                    status_code=401,
                    retryable=False,
                )
            issued = self._response_model(response, _PresenceRequestResponse)
            invitation = PairingInvitation(
                invitation_id=issued.invitation_id,
                pairing_secret=issued.pairing_secret.get_secret_value(),
                hub_origin=issued.hub_origin,
                locator=locator,
            ).validated()
            if (
                invitation.hub_origin != hub_origin
                or issued.expires_at <= time.time()
                or issued.presence_pin
                != _presence_pin(invitation.pairing_secret)
            ):
                raise PairingClientError(
                    PairingClientErrorCode.HUB_RESPONSE_INVALID,
                    status_code=502,
                    retryable=False,
                )
            return PresencePairingInvitation(
                invitation=invitation,
                presence_pin=issued.presence_pin,
                expires_at=issued.expires_at,
            )

    async def discard_rejected_unclaimed_attempt(self) -> None:
        """Forget a claim that Nyx conclusively rejected before enrollment.

        This cannot discard an ambiguous request, an approved claim, staged
        credentials, or a completed pairing.  The pairing store and client
        journal are reset in one shared SQLite transaction.
        """

        async with self._lock:
            self._ensure_ready()
            record = await _await_blocking(self._journal.status)
            if record is None:
                raise PairingClientError(
                    PairingClientErrorCode.NO_ATTEMPT,
                    status_code=409,
                    retryable=False,
                )

            def discard(
                connection: sqlite3.Connection,
                pairing_row: sqlite3.Row,
            ) -> None:
                self._journal.discard_rejected_unclaimed_attempt(
                    connection,
                    pairing_row,
                    record.attempt_id,
                )

            try:
                await _await_async_outcome(
                    asyncio.create_task(
                        self._pairing_store._discard_rejected_unclaimed_attempt(
                            record.attempt_id,
                            discard,
                        ),
                        name="mnemosyne-fleet-discard-rejected-claim",
                    )
                )
            except PairingClientError:
                raise
            except (FleetPairingError, ValueError):
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                ) from None
            await self._notify_pairing_authority_changed()

    async def self_disable_enrollment(
        self,
        *,
        request_id: str,
    ) -> PairingManagementResult:
        """Disable Hub routing while retaining the dynamic pairing locally.

        The lifecycle caller owns the durable request ID and must replay that
        exact value after ``MANAGEMENT_OUTCOME_UNKNOWN``.
        """

        return await self._self_manage(
            request_id=request_id,
            operation="self-disable",
        )

    async def self_revoke_enrollment(
        self,
        *,
        request_id: str,
    ) -> PairingManagementResult:
        """Revoke the exact dynamic pairing before local state removal.

        The lifecycle caller owns the durable request ID and must replay that
        exact value after ``MANAGEMENT_OUTCOME_UNKNOWN``.
        """

        return await self._self_manage(
            request_id=request_id,
            operation="self-revoke",
        )

    async def _self_manage(
        self,
        *,
        request_id: str,
        operation: Literal["self-disable", "self-revoke"],
    ) -> PairingManagementResult:
        try:
            request_id = _canonical_uuid(request_id, field_name="request_id")
        except ValueError:
            raise PairingClientError(
                PairingClientErrorCode.PAYLOAD_MISMATCH,
                status_code=400,
                retryable=False,
            ) from None

        async with self._lock:
            self._ensure_ready()
            try:
                pairing = await self._pairing_store.status()
                if pairing.legacy_credentials_present:
                    return PairingManagementResult(
                        state=PairingManagementState.ADMIN_ACTION_REQUIRED,
                        pairing_id=None,
                        reporting_node_id=None,
                        credential_generation=None,
                    )
                if pairing.state == PairingState.UNPAIRED:
                    return PairingManagementResult(
                        state=PairingManagementState.NOT_PAIRED,
                        pairing_id=None,
                        reporting_node_id=None,
                        credential_generation=None,
                    )

                revoke_record: _SelfRevokeRecord | None = None
                if operation == "self-revoke":
                    workflow = await _await_blocking(self._journal.status)
                    existing_revoke = await _await_blocking(
                        self._journal.self_revoke_status
                    )
                    credential_fingerprint = pairing.credential_fingerprint
                    if credential_fingerprint is None and existing_revoke is not None:
                        credential_fingerprint = (
                            existing_revoke.credential_fingerprint
                        )
                    if (
                        pairing.state
                        not in {PairingState.PAIRED, PairingState.REVOKED}
                        or not pairing.pairing_id
                        or not pairing.node_id
                        or pairing.credential_epoch is None
                        or not pairing.attempt_id
                        or not pairing.hub_origin
                        or not pairing.node_url
                        or workflow is None
                        or workflow.phase is not PairingClientPhase.COMPLETE
                        or workflow.attempt_id != pairing.attempt_id
                        or workflow.pairing_id != pairing.pairing_id
                        or workflow.reporting_node_id != pairing.node_id
                        or workflow.credential_generation
                        != pairing.credential_epoch
                        or not _valid_fingerprint(credential_fingerprint)
                    ):
                        raise PairingClientError(
                            PairingClientErrorCode.STATE_CONFLICT,
                            status_code=409,
                            retryable=False,
                        )
                    pairing_id = _canonical_uuid(
                        pairing.pairing_id,
                        field_name="pairing_id",
                    )
                    reporting_node_id = _bounded_identifier(pairing.node_id)
                    hub_origin = _verified_https_origin(pairing.hub_origin)
                    node_url = _safe_node_url(pairing.node_url)

                    # The journal checks retired IDs and records/replays the
                    # current immutable intent before invoking this callback.
                    # Running the synchronous latch inside that cancellation-
                    # safe transaction prevents both stale-ID denial and a
                    # commit-without-live-denial cancellation window.
                    revoke_record = await _await_blocking(
                        self._journal.prepare_self_revoke,
                        request_id=request_id,
                        attempt_id=workflow.attempt_id,
                        invitation_id=workflow.invitation_id,
                        pairing_id=pairing_id,
                        reporting_node_id=reporting_node_id,
                        credential_generation=pairing.credential_epoch,
                        credential_fingerprint=credential_fingerprint,
                        hub_origin=hub_origin,
                        node_url=node_url,
                        on_pending=self._deny_self_revoke_authority,
                    )

                    if (
                        pairing.state == PairingState.REVOKED
                        and revoke_record.phase is _SelfRevokePhase.PENDING
                    ):
                        # HUB_COMMITTED is journaled before the store can enter
                        # REVOKED. This combination is therefore corruption,
                        # not a crash boundary that may manufacture Hub proof.
                        raise PairingClientError(
                            PairingClientErrorCode.STATE_CONFLICT,
                            status_code=409,
                            retryable=False,
                        )

                    if revoke_record.phase != _SelfRevokePhase.PENDING:
                        try:
                            await _await_async_outcome(
                                asyncio.create_task(
                                    self._finish_self_revoke(request_id),
                                    name=(
                                        "mnemosyne-fleet-self-revoke-local-"
                                        "recovery"
                                    ),
                                )
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            raise self._management_outcome_unknown() from None
                        return PairingManagementResult(
                            state=PairingManagementState.REVOKED,
                            pairing_id=pairing_id,
                            reporting_node_id=reporting_node_id,
                            credential_generation=pairing.credential_epoch,
                        )
                elif (
                    await _await_blocking(self._journal.self_revoke_status)
                    is not None
                ):
                    raise PairingClientError(
                        PairingClientErrorCode.STATE_CONFLICT,
                        status_code=409,
                        retryable=False,
                    )

                if pairing.state == PairingState.REVOKED:
                    # Legacy/local revoked tombstones reach this only for
                    # operations other than self-revoke.
                    await self._notify_pairing_authority_changed()
                    return PairingManagementResult(
                        state=PairingManagementState.REVOKED,
                        pairing_id=pairing.pairing_id,
                        reporting_node_id=pairing.node_id,
                        credential_generation=pairing.credential_epoch,
                    )
                if (
                    pairing.state != PairingState.PAIRED
                    or not pairing.pairing_id
                    or not pairing.node_id
                    or pairing.credential_epoch is None
                    or not pairing.hub_origin
                    or not pairing.credentials_owned
                ):
                    raise PairingClientError(
                        PairingClientErrorCode.STATE_CONFLICT,
                        status_code=409,
                        retryable=False,
                    )

                pairing_id = _canonical_uuid(
                    pairing.pairing_id,
                    field_name="pairing_id",
                )
                reporting_node_id = _bounded_identifier(pairing.node_id)
                hub_origin = _verified_https_origin(pairing.hub_origin)
                credentials = await self._pairing_store.staged_credentials()
                payload = {
                    "schema_version": PAIRING_PROTOCOL_VERSION,
                    "request_id": request_id,
                    "pairing_id": pairing_id,
                    "reporting_node_id": reporting_node_id,
                    "credential_generation": pairing.credential_epoch,
                }
                try:
                    response = await self._post(
                        hub_origin,
                        (
                            f"/fleet/management/v1/pairings/{pairing_id}/"
                            f"{operation}"
                        ),
                        payload,
                        bearer=credentials.management_key,
                    )
                except PairingClientError as error:
                    if error.code == PairingClientErrorCode.HUB_UNAVAILABLE or (
                        operation == "self-revoke"
                        and error.code
                        in {
                            PairingClientErrorCode.HUB_REDIRECT_REFUSED,
                            PairingClientErrorCode.HUB_RESPONSE_TOO_LARGE,
                            PairingClientErrorCode.HUB_RESPONSE_INVALID,
                        }
                    ):
                        raise self._management_outcome_unknown() from None
                    raise
                if response.status_code != 200:
                    if response.status_code in {
                        408,
                        429,
                        500,
                        502,
                        503,
                        504,
                    } or (
                        operation == "self-revoke"
                        and 200 <= response.status_code < 300
                    ):
                        raise self._management_outcome_unknown()
                    terminal = PairingClientError(
                        PairingClientErrorCode.MANAGEMENT_REJECTED,
                        status_code=(
                            401 if response.status_code == 401 else 409
                        ),
                        retryable=False,
                    )
                    if operation == "self-revoke":
                        try:
                            await _await_async_outcome(
                                asyncio.create_task(
                                    self._abort_terminal_self_revoke(request_id),
                                    name=(
                                        "mnemosyne-fleet-self-revoke-terminal-"
                                        "abort"
                                    ),
                                )
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # Nyx proved a terminal rejection, but local durable
                            # cleanup did not complete. Keep admission fenced and
                            # preserve the exact request for a later recovery.
                            raise self._management_outcome_unknown() from None
                    raise terminal
                try:
                    managed = self._response_model(response, _ManagementResponse)
                    self._validate_management(
                        managed,
                        pairing_id=pairing_id,
                        reporting_node_id=reporting_node_id,
                        credential_generation=pairing.credential_epoch,
                        operation=operation,
                    )
                except PairingClientError as error:
                    if (
                        operation == "self-revoke"
                        and error.code
                        == PairingClientErrorCode.HUB_RESPONSE_INVALID
                    ):
                        raise self._management_outcome_unknown() from None
                    raise
                if operation == "self-revoke":
                    try:
                        assert revoke_record is not None
                        await _await_blocking(
                            self._journal.record_self_revoke_hub_committed,
                            request_id,
                        )
                        await _await_async_outcome(
                            asyncio.create_task(
                                self._finish_self_revoke(request_id),
                                name="mnemosyne-fleet-self-revoke-local-commit",
                            )
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Nyx may already have committed the exact request. A
                        # local store/cache failure is therefore an ambiguous
                        # outcome and must be retried with the same request ID,
                        # never converted into a terminal state conflict.
                        raise self._management_outcome_unknown() from None
                    state = PairingManagementState.REVOKED
                else:
                    state = PairingManagementState.DISABLED
                return PairingManagementResult(
                    state=state,
                    pairing_id=pairing_id,
                    reporting_node_id=reporting_node_id,
                    credential_generation=pairing.credential_epoch,
                )
            except PairingClientError:
                raise
            except (FleetPairingError, ValueError):
                raise PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                ) from None

    async def _advance(
        self,
        invitation: PairingInvitation,
        *,
        allow_create: bool,
    ) -> PairingClientRecord:
        try:
            validated = invitation.validated()
            reporting_node_id = _bounded_identifier(self._reporting_node_id)
            self._display_name = _bounded_display_name(self._display_name)
            self._service_version = _bounded_identifier(
                self._service_version,
                maximum=64,
            )
            self._service_instance_id = _bounded_identifier(
                self._service_instance_id
            )
        except ValueError:
            raise PairingClientError(
                PairingClientErrorCode.PAYLOAD_MISMATCH,
                status_code=400,
                retryable=False,
            ) from None
        async with self._lock:
            self._ensure_ready()
            record: PairingClientRecord | None = None
            try:
                pairing = await self._pairing_store.status()
                if pairing.legacy_credentials_present:
                    raise PairingClientError(
                        PairingClientErrorCode.STATIC_CREDENTIALS_PRESENT,
                        status_code=409,
                        retryable=False,
                    )
                revoke_record = await _await_blocking(
                    self._journal.self_revoke_status
                )
                if (
                    allow_create
                    and pairing.state
                    in {PairingState.UNPAIRED, PairingState.REVOKED}
                    and revoke_record is not None
                ):
                    if (
                        revoke_record.phase != _SelfRevokePhase.COMPLETE
                        or pairing.credentials_owned
                        or pairing.credential_write_pending
                        or pairing.legacy_credentials_present
                    ):
                        raise PairingClientError(
                            PairingClientErrorCode.STATE_CONFLICT,
                            status_code=409,
                            retryable=False,
                        )
                    record = await _await_async_outcome(
                        asyncio.create_task(
                            self._reset_completed_revoke_and_begin(
                                validated,
                                reporting_node_id,
                                revoke_record,
                            ),
                            name="mnemosyne-fleet-completed-revoke-reset",
                        )
                    )
                    pairing = await self._pairing_store.status()
                else:
                    record, _created = await _await_blocking(
                        self._journal.prepare,
                        validated,
                        reporting_node_id=reporting_node_id,
                        allow_create=allow_create,
                    )
                    if pairing.state == PairingState.UNPAIRED:
                        await self._pairing_store.begin_attempt(
                            hub_origin=validated.hub_origin,
                            node_url=validated.locator,
                            attempt_id=record.attempt_id,
                        )
                if (
                    pairing.state != PairingState.UNPAIRED
                    and pairing.attempt_id != record.attempt_id
                ):
                    if not (
                        pairing.state == PairingState.PAIRED
                        and record.phase == PairingClientPhase.COMPLETE
                        and pairing.pairing_id == record.pairing_id
                    ):
                        raise PairingClientError(
                            PairingClientErrorCode.STATE_CONFLICT,
                            status_code=409,
                            retryable=False,
                        )

                if record.phase == PairingClientPhase.COMPLETE:
                    if pairing.state != PairingState.PAIRED:
                        raise PairingClientError(
                            PairingClientErrorCode.STATE_CONFLICT,
                            status_code=409,
                            retryable=False,
                        )
                    return record

                if record.phase == PairingClientPhase.CLAIMING:
                    claim = await self._claim(validated, record)
                    self._validate_claim(claim, validated, record)
                    record = await _await_blocking(
                        self._journal.record_claim,
                        claim,
                    )

                if record.phase in {
                    PairingClientPhase.AWAITING_APPROVAL,
                    PairingClientPhase.STAGING,
                }:
                    if record.expires_at is not None and time.time() >= record.expires_at:
                        raise PairingClientError(
                            PairingClientErrorCode.EXPIRED,
                            status_code=410,
                            retryable=False,
                        )
                    provisioned = await self._provision(validated, record)
                    self._validate_provision(provisioned, record)
                    record = await _await_blocking(
                        self._journal.record_staging,
                        provisioned,
                    )
                    credentials = _credentials_from_response(provisioned)
                    await self._pairing_store.record_assignment(
                        pairing_id=provisioned.pairing_id,
                        node_id=provisioned.reporting_node_id,
                        credential_epoch=provisioned.credential_generation,
                    )
                    await self._pairing_store.activate_credentials(credentials)
                    record = await _await_blocking(
                        self._journal.record_activation_pending
                    )

                if record.phase == PairingClientPhase.ACTIVATION_PENDING:
                    if self._on_pairing_authority_changed is not None:
                        # Nyx's activation acknowledgement synchronously probes
                        # the candidate snapshot and dispatch credentials. Make
                        # the runtime's read-only authorization cache observe
                        # the durable staged state before every attempt. Doing
                        # this on retries also closes the cancellation window
                        # after the journal commit and before an earlier cache
                        # refresh.
                        await self._notify_pairing_authority_changed()
                    credentials = await self._pairing_store.staged_credentials()
                    activated = await self._activate(validated, record, credentials)
                    self._validate_activation(activated, record)
                    pairing = await self._pairing_store.status()
                    if pairing.state != PairingState.PAIRED:
                        await self._pairing_store.mark_paired()
                        await self._notify_pairing_authority_changed()
                    record = await _await_blocking(self._journal.record_complete)
                return record
            except PairingClientError as exc:
                if record is not None:
                    await _await_blocking(self._journal.record_error, exc.code)
                raise
            except LegacyFleetCredentialsPresent:
                error = PairingClientError(
                    PairingClientErrorCode.STATIC_CREDENTIALS_PRESENT,
                    status_code=409,
                    retryable=False,
                )
                if record is not None:
                    await _await_blocking(self._journal.record_error, error.code)
                raise error from None
            except (FleetPairingError, ValueError):
                error = PairingClientError(
                    PairingClientErrorCode.STATE_CONFLICT,
                    status_code=409,
                    retryable=False,
                )
                if record is not None:
                    await _await_blocking(self._journal.record_error, error.code)
                raise error from None

    async def _finish_self_revoke(self, request_id: str) -> None:
        await self._pairing_store.mark_revoked()
        await self._pairing_store.retire_revoked_credentials()
        await _await_blocking(
            self._journal.record_self_revoke_complete,
            request_id,
        )
        await self._notify_pairing_authority_changed()

    async def _abort_terminal_self_revoke(self, request_id: str) -> None:
        await _await_blocking(
            self._journal.abort_pending_self_revoke,
            request_id,
            on_aborted=self._clear_aborted_self_revoke_denial,
        )

    async def _reset_completed_revoke_and_begin(
        self,
        invitation: PairingInvitation,
        reporting_node_id: str,
        revoke_record: _SelfRevokeRecord,
    ) -> PairingClientRecord:
        attempt_id = str(uuid4())

        def replace_journals(
            connection: sqlite3.Connection,
            pairing_row: sqlite3.Row,
        ) -> PairingClientRecord:
            return self._journal.replace_completed_revoke_and_prepare(
                connection,
                pairing_row,
                invitation,
                reporting_node_id,
                attempt_id,
            )

        record = (
            await self._pairing_store._begin_repairing_after_completed_revoke(
                invitation.hub_origin,
                invitation.locator,
                attempt_id,
                revoke_record.pairing_id,
                revoke_record.reporting_node_id,
                revoke_record.credential_generation,
                revoke_record.attempt_id,
                replace_journals,
            )
        )
        if self._on_completed_revoke_reset is not None:
            self._on_completed_revoke_reset()
        return record

    def _deny_self_revoke_authority(self) -> None:
        if self._on_self_revoke_pending is not None:
            self._on_self_revoke_pending()

    def _clear_aborted_self_revoke_denial(self) -> None:
        if self._on_self_revoke_aborted is not None:
            self._on_self_revoke_aborted()

    async def _notify_pairing_authority_changed(self) -> None:
        if self._on_pairing_authority_changed is not None:
            await self._on_pairing_authority_changed()

    async def _claim(
        self,
        invitation: PairingInvitation,
        record: PairingClientRecord,
    ) -> _ClaimResponse:
        payload = {
            "schema_version": 1,
            "request_id": record.claim_request_id,
            "invitation_id": invitation.invitation_id,
            "pairing_secret": invitation.pairing_secret,
            "mac": {
                "platform": "macos",
                "service_version": self._service_version,
                "display_name": self._display_name,
                "reporting_node_id": self._reporting_node_id,
            },
            "locator": invitation.locator,
            "supported_protocol": {"minimum": 1, "maximum": 1},
        }
        response = await self._post(
            invitation.hub_origin,
            "/fleet/pairing/v1/claims",
            payload,
        )
        if response.status_code != 200:
            if response.status_code in {429, 500, 502, 503, 504}:
                raise self._hub_unavailable()
            raise PairingClientError(
                PairingClientErrorCode.CLAIM_REJECTED,
                status_code=401,
                retryable=False,
            )
        return self._response_model(response, _ClaimResponse)

    async def _provision(
        self,
        invitation: PairingInvitation,
        record: PairingClientRecord,
    ) -> _ProvisionResponse:
        assert record.claim_id is not None
        response = await self._post(
            invitation.hub_origin,
            f"/fleet/pairing/v1/claims/{record.claim_id}/provision",
            {
                "schema_version": 1,
                "request_id": record.provision_request_id,
                "pairing_secret": invitation.pairing_secret,
            },
        )
        if response.status_code == 410:
            raise PairingClientError(
                PairingClientErrorCode.APPROVAL_PENDING,
                status_code=202,
                retryable=True,
            )
        if response.status_code != 200:
            if response.status_code in {429, 500, 502, 503, 504}:
                raise self._hub_unavailable()
            raise PairingClientError(
                PairingClientErrorCode.CLAIM_REJECTED,
                status_code=401,
                retryable=False,
            )
        return self._response_model(response, _ProvisionResponse)

    async def _activate(
        self,
        invitation: PairingInvitation,
        record: PairingClientRecord,
        credentials: PairingCredentials,
    ) -> _ActivationResponse:
        assert record.pairing_id is not None
        assert record.credential_generation is not None
        response = await self._post(
            invitation.hub_origin,
            (
                "/fleet/management/v1/pairings/"
                f"{record.pairing_id}/activation-ack"
            ),
            {
                "schema_version": 1,
                "request_id": record.activation_request_id,
                "credential_generation": record.credential_generation,
                "reporting_node_id": record.reporting_node_id,
                "service_instance_id": self._service_instance_id,
            },
            bearer=credentials.management_key,
        )
        if response.status_code != 200:
            if response.status_code in {429, 500, 502, 503, 504}:
                raise self._hub_unavailable()
            raise PairingClientError(
                PairingClientErrorCode.ACTIVATION_REJECTED,
                status_code=401,
                retryable=False,
            )
        return self._response_model(response, _ActivationResponse)

    async def _post(
        self,
        origin: str,
        path: str,
        payload: Mapping[str, object],
        *,
        bearer: str | None = None,
    ) -> httpx.Response:
        body = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(body) > MAX_PAIRING_REQUEST_BYTES:
            raise PairingClientError(
                PairingClientErrorCode.PAYLOAD_MISMATCH,
                status_code=400,
                retryable=False,
            )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cache-Control": "no-store",
        }
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        self._client.cookies.clear()
        request = self._client.build_request(
            "POST",
            f"{origin}{path}",
            headers=headers,
            content=body,
        )
        deadline = asyncio.get_running_loop().time() + PAIRING_TOTAL_TIMEOUT_SECONDS
        try:
            response = await asyncio.wait_for(
                self._client.send(request, stream=True),
                timeout=PAIRING_TOTAL_TIMEOUT_SECONDS,
            )
        except (
            TimeoutError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
        ):
            raise self._hub_unavailable() from None
        try:
            try:
                if 300 <= response.status_code < 400:
                    raise PairingClientError(
                        PairingClientErrorCode.HUB_REDIRECT_REFUSED,
                        status_code=502,
                        retryable=False,
                    )
                encoding = response.headers.get(
                    "content-encoding", "identity"
                ).lower()
                if encoding != "identity":
                    raise PairingClientError(
                        PairingClientErrorCode.HUB_RESPONSE_INVALID,
                        status_code=502,
                        retryable=False,
                    )
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        raise PairingClientError(
                            PairingClientErrorCode.HUB_RESPONSE_INVALID,
                            status_code=502,
                            retryable=False,
                        ) from None
                    if declared_size < 0:
                        raise PairingClientError(
                            PairingClientErrorCode.HUB_RESPONSE_INVALID,
                            status_code=502,
                            retryable=False,
                        )
                    if declared_size > MAX_PAIRING_RESPONSE_BYTES:
                        raise self._response_too_large()
                content = bytearray()
                if response.is_stream_consumed:
                    # In-memory transports may hand back an already-consumed
                    # bounded response. Real network responses take the
                    # streaming branch below.
                    content.extend(response.content)
                    if len(content) > MAX_PAIRING_RESPONSE_BYTES:
                        raise self._response_too_large()
                else:
                    iterator = response.aiter_raw().__aiter__()
                    while True:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise TimeoutError
                        try:
                            chunk = await asyncio.wait_for(
                                iterator.__anext__(),
                                timeout=remaining,
                            )
                        except StopAsyncIteration:
                            break
                        content.extend(chunk)
                        if len(content) > MAX_PAIRING_RESPONSE_BYTES:
                            raise self._response_too_large()
                # Return a detached bounded response. This prevents later code
                # from reaching a network stream or an unbounded decoder.
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(content),
                    request=request,
                )
            except PairingClientError:
                raise
            except (
                TimeoutError,
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.ProtocolError,
                httpx.StreamError,
            ):
                raise self._hub_unavailable() from None
        finally:
            try:
                await response.aclose()
            finally:
                self._client.cookies.clear()

    @staticmethod
    def _response_model(
        response: httpx.Response,
        model_type: type[_T],
    ) -> _T:
        content_type = response.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise PairingClientError(
                PairingClientErrorCode.HUB_RESPONSE_INVALID,
                status_code=502,
                retryable=False,
            )
        try:
            return model_type.model_validate_json(  # type: ignore[attr-defined, no-any-return]
                response.content
            )
        except (ValidationError, ValueError, TypeError):
            raise PairingClientError(
                PairingClientErrorCode.HUB_RESPONSE_INVALID,
                status_code=502,
                retryable=False,
            ) from None

    def _validate_claim(
        self,
        response: _ClaimResponse,
        invitation: PairingInvitation,
        record: PairingClientRecord,
    ) -> None:
        try:
            claim_id = _canonical_uuid(response.claim_id, field_name="claim_id")
            pairing_id = _canonical_uuid(
                response.pairing_id,
                field_name="pairing_id",
            )
            response_invitation = _canonical_uuid(
                response.invitation_id,
                field_name="invitation_id",
            )
        except ValueError:
            raise self._invalid_response() from None
        if (
            response_invitation != invitation.invitation_id
            or response.reporting_node_id != record.reporting_node_id
            or response.service_version != self._service_version
            or not claim_id
            or not pairing_id
            or response.expires_at <= response.claimed_at
        ):
            raise self._invalid_response()

    def _validate_provision(
        self,
        response: _ProvisionResponse,
        record: PairingClientRecord,
    ) -> None:
        try:
            claim_id = _canonical_uuid(response.claim_id, field_name="claim_id")
            pairing_id = _canonical_uuid(
                response.pairing_id,
                field_name="pairing_id",
            )
            _credentials_from_response(response).validated()
        except ValueError:
            raise self._invalid_response() from None
        if (
            claim_id != record.claim_id
            or pairing_id != record.pairing_id
            or response.reporting_node_id != record.reporting_node_id
        ):
            raise self._invalid_response()

    def _validate_activation(
        self,
        response: _ActivationResponse,
        record: PairingClientRecord,
    ) -> None:
        try:
            pairing_id = _canonical_uuid(
                response.pairing_id,
                field_name="pairing_id",
            )
        except ValueError:
            raise self._invalid_response() from None
        if (
            pairing_id != record.pairing_id
            or response.reporting_node_id != record.reporting_node_id
            or response.credential_generation != record.credential_generation
        ):
            raise self._invalid_response()

    def _validate_management(
        self,
        response: _ManagementResponse,
        *,
        pairing_id: str,
        reporting_node_id: str,
        credential_generation: int,
        operation: Literal["self-disable", "self-revoke"],
    ) -> None:
        try:
            response_pairing_id = _canonical_uuid(
                response.pairing_id,
                field_name="pairing_id",
            )
        except ValueError:
            raise self._invalid_response() from None
        expected_state = "disabled" if operation == "self-disable" else "revoked"
        if (
            response_pairing_id != pairing_id
            or response.reporting_node_id != reporting_node_id
            or response.credential_generation != credential_generation
            or response.state != expected_state
            or (operation == "self-disable" and response.revoked_at is not None)
            or (operation == "self-revoke" and response.revoked_at is None)
        ):
            raise self._invalid_response()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._client.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=503,
                retryable=False,
            )

    def _ensure_ready(self) -> None:
        self._ensure_open()
        if not self._initialized:
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=503,
                retryable=False,
            )

    @staticmethod
    def _hub_unavailable() -> PairingClientError:
        return PairingClientError(
            PairingClientErrorCode.HUB_UNAVAILABLE,
            status_code=503,
            retryable=True,
        )

    @staticmethod
    def _management_outcome_unknown() -> PairingClientError:
        return PairingClientError(
            PairingClientErrorCode.MANAGEMENT_OUTCOME_UNKNOWN,
            status_code=503,
            retryable=True,
        )

    @staticmethod
    def _response_too_large() -> PairingClientError:
        return PairingClientError(
            PairingClientErrorCode.HUB_RESPONSE_TOO_LARGE,
            status_code=502,
            retryable=False,
        )

    @staticmethod
    def _invalid_response() -> PairingClientError:
        return PairingClientError(
            PairingClientErrorCode.HUB_RESPONSE_INVALID,
            status_code=502,
            retryable=False,
        )


def _bounded_identifier(value: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise PairingClientError(
            PairingClientErrorCode.LOCAL_IDENTITY_INVALID,
            status_code=409,
            retryable=False,
        )
    return value


def _bounded_display_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise PairingClientError(
            PairingClientErrorCode.LOCAL_IDENTITY_INVALID,
            status_code=409,
            retryable=False,
        )
    return value


def _credentials_from_response(response: _ProvisionResponse) -> PairingCredentials:
    credentials = response.credentials
    return PairingCredentials(
        snapshot_key=credentials.snapshot_bearer.get_secret_value(),
        dispatch_key=credentials.dispatch_bearer.get_secret_value(),
        management_key=credentials.management_bearer.get_secret_value(),
    )


def _presence_pin(pairing_secret: str) -> str:
    digest = hmac.new(
        pairing_secret.encode("utf-8"),
        b"mnemosyne-fleet-presence-pin-v1",
        hashlib.sha256,
    ).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"


__all__ = [
    "FleetPairingClient",
    "PairingClientError",
    "PairingClientErrorCode",
    "PairingClientPhase",
    "PairingClientRecord",
    "PairingInvitation",
    "PresencePairingInvitation",
    "PairingManagementResult",
    "PairingManagementState",
]
