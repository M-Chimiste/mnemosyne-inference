"""Closed path-free protocol for the native lifecycle recovery runner.

Version 2 deliberately carries only opaque identities and fixed lifecycle
verbs.  It is not a command-execution protocol: paths, process identifiers,
ports, launch labels, arguments, and credentials have no fields in any
message.  Every message is an exact-key canonical JSON object transported in
one bounded four-byte-length-prefixed frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
import re
import struct
from typing import Final, TypeAlias
from uuid import UUID


LIFECYCLE_EXECUTION_PROTOCOL_VERSION: Final[int] = 2
LIFECYCLE_RUNNER_IDENTIFIER: Final[str] = (
    "com.mnemosyne.inference.lifecycle-runner"
)
MAXIMUM_EXECUTION_JSON_BYTES: Final[int] = 32 * 1024
MAXIMUM_EXECUTION_FRAME_BYTES: Final[int] = MAXIMUM_EXECUTION_JSON_BYTES + 4
MAXIMUM_RUNNER_LEASE_SECONDS: Final[int] = 60

_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"sha256:[0-9a-f]{64}")
_TEAM_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z0-9]{10}")


class LifecycleExecutionProtocolError(ValueError):
    """A fixed-code protocol failure safe to expose to the local peer."""

    def __init__(self, code: str = "lifecycle_execution_protocol_invalid") -> None:
        self.code = code
        super().__init__(code)


class LifecycleExecutionDirection(str, Enum):
    FORWARD = "forward"
    ROLLBACK = "rollback"
    MANUAL_RECOVERY = "manual_recovery"


class LifecycleExecutionMessageType(str, Enum):
    REGISTER = "register"
    REGISTERED = "registered"
    START = "start"
    OBSERVE = "observe"
    APPLY = "apply"
    FINALIZE = "finalize"
    REFUSED = "refused"


class LifecycleEffectKind(str, Enum):
    PREFLIGHT_CANDIDATE = "preflight_candidate"
    DRAIN_INFERENCE = "drain_inference"
    CAPTURE_ROLLBACK = "capture_rollback"
    STOP_PREDECESSOR = "stop_predecessor"
    INSTALL_CANDIDATE = "install_candidate"
    START_CANDIDATE = "start_candidate"
    VALIDATE_CANDIDATE = "validate_candidate"
    COMMIT_CANDIDATE = "commit_candidate"
    RESTORE_PREDECESSOR = "restore_predecessor"
    QUIESCE_SERVICE = "quiesce_service"
    RESOLVE_OUTBOX = "resolve_outbox"
    RESOLVE_PAIRING = "resolve_pairing"
    RESOLVE_EXCLUSIVE_WEIGHT = "resolve_exclusive_weight"
    RESOLVE_RUNTIME_MEMBER = "resolve_runtime_member"
    UNREGISTER_AGENT = "unregister_agent"
    UNREGISTER_LOGIN_ITEM = "unregister_login_item"
    RESOLVE_STATE_MEMBER = "resolve_state_member"
    QUARANTINE_APPLICATION = "quarantine_application"
    REMOVE_APPLICATION = "remove_application"
    FINALIZE_UNINSTALL = "finalize_uninstall"
    CLEANUP_RECOVERY_CLONE = "cleanup_recovery_clone"


class LifecycleEffectObservation(str, Enum):
    NEEDS_ACTION = "needs_action"
    EFFECT_SATISFIED = "effect_satisfied"
    RETRYABLE_NOT_READY = "retryable_not_ready"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class LifecycleEffectReceiptStatus(str, Enum):
    OBSERVED = "observed"
    APPLY_STARTED = "apply_started"
    APPLIED = "applied"
    FINALIZED = "finalized"
    REFUSED = "refused"


_REFUSAL_CODES: Final[frozenset[str]] = frozenset(
    {
        "runner_adapter_unavailable",
        "execution_disabled",
        "execution_grant_invalid",
        "execution_lease_conflict",
        "execution_not_ready",
        "execution_protocol_invalid",
    }
)


@dataclass(frozen=True, slots=True)
class LifecycleRunnerRegistrationV2:
    protocol_version: int
    message_type: LifecycleExecutionMessageType
    transaction_id: str
    grant_id: str
    grant_digest: str
    runner_session_id: str
    sequence: int
    nonce: str
    runner_identifier: str
    runner_build_digest: str
    runner_identity_digest: str
    team_identifier: str
    code_requirement_digest: str
    requested_lease_seconds: int

    def to_wire_dict(self) -> dict[str, object]:
        return _wire_dict(self)


@dataclass(frozen=True, slots=True)
class LifecycleRunnerRegisteredV2:
    protocol_version: int
    message_type: LifecycleExecutionMessageType
    transaction_id: str
    grant_id: str
    grant_digest: str
    runner_session_id: str
    sequence: int
    nonce: str
    request_nonce: str
    lease_id: str
    lease_epoch: int
    lease_expires_at: int

    def to_wire_dict(self) -> dict[str, object]:
        return _wire_dict(self)


@dataclass(frozen=True, slots=True)
class LifecycleExecutionStartV2:
    protocol_version: int
    message_type: LifecycleExecutionMessageType
    transaction_id: str
    grant_id: str
    grant_digest: str
    runner_session_id: str
    lease_id: str
    lease_epoch: int
    sequence: int
    nonce: str
    direction: LifecycleExecutionDirection
    execution_manifest_digest: str
    recovery_clone_identity_digest: str
    authorization_digest: str
    authorization_session_id: str

    def to_wire_dict(self) -> dict[str, object]:
        return _wire_dict(self)


@dataclass(frozen=True, slots=True)
class LifecycleExecutionObserveV2:
    protocol_version: int
    message_type: LifecycleExecutionMessageType
    transaction_id: str
    grant_id: str
    grant_digest: str
    runner_session_id: str
    lease_id: str
    lease_epoch: int
    sequence: int
    nonce: str
    effect_id: str
    effect_kind: LifecycleEffectKind
    target_digest: str
    attempt: int
    prior_receipt_digest: str | None

    def to_wire_dict(self) -> dict[str, object]:
        return _wire_dict(self)


@dataclass(frozen=True, slots=True)
class LifecycleExecutionApplyV2:
    protocol_version: int
    message_type: LifecycleExecutionMessageType
    transaction_id: str
    grant_id: str
    grant_digest: str
    runner_session_id: str
    lease_id: str
    lease_epoch: int
    sequence: int
    nonce: str
    effect_id: str
    effect_kind: LifecycleEffectKind
    target_digest: str
    attempt: int
    observation_receipt_digest: str

    def to_wire_dict(self) -> dict[str, object]:
        return _wire_dict(self)


@dataclass(frozen=True, slots=True)
class LifecycleExecutionFinalizeV2:
    protocol_version: int
    message_type: LifecycleExecutionMessageType
    transaction_id: str
    grant_id: str
    grant_digest: str
    runner_session_id: str
    lease_id: str
    lease_epoch: int
    sequence: int
    nonce: str
    direction: LifecycleExecutionDirection
    final_receipt_digest: str

    def to_wire_dict(self) -> dict[str, object]:
        return _wire_dict(self)


@dataclass(frozen=True, slots=True)
class LifecycleExecutionRefusedV2:
    protocol_version: int
    message_type: LifecycleExecutionMessageType
    transaction_id: str
    grant_id: str
    runner_session_id: str
    sequence: int
    nonce: str
    request_nonce: str
    error_code: str

    def to_wire_dict(self) -> dict[str, object]:
        return _wire_dict(self)


LifecycleExecutionMessageV2: TypeAlias = (
    LifecycleRunnerRegistrationV2
    | LifecycleRunnerRegisteredV2
    | LifecycleExecutionStartV2
    | LifecycleExecutionObserveV2
    | LifecycleExecutionApplyV2
    | LifecycleExecutionFinalizeV2
    | LifecycleExecutionRefusedV2
)


_MESSAGE_CLASS: Final[dict[LifecycleExecutionMessageType, type[object]]] = {
    LifecycleExecutionMessageType.REGISTER: LifecycleRunnerRegistrationV2,
    LifecycleExecutionMessageType.REGISTERED: LifecycleRunnerRegisteredV2,
    LifecycleExecutionMessageType.START: LifecycleExecutionStartV2,
    LifecycleExecutionMessageType.OBSERVE: LifecycleExecutionObserveV2,
    LifecycleExecutionMessageType.APPLY: LifecycleExecutionApplyV2,
    LifecycleExecutionMessageType.FINALIZE: LifecycleExecutionFinalizeV2,
    LifecycleExecutionMessageType.REFUSED: LifecycleExecutionRefusedV2,
}


def encode_lifecycle_execution_message(
    message: LifecycleExecutionMessageV2,
) -> bytes:
    validated = validate_lifecycle_execution_message(message)
    payload = _canonical_json(validated.to_wire_dict()).encode("utf-8")
    if not payload or len(payload) > MAXIMUM_EXECUTION_JSON_BYTES:
        raise LifecycleExecutionProtocolError("execution_protocol_oversized")
    return struct.pack(">I", len(payload)) + payload


def decode_lifecycle_execution_frame(frame: bytes) -> LifecycleExecutionMessageV2:
    if not isinstance(frame, bytes) or len(frame) < 4:
        raise LifecycleExecutionProtocolError()
    size = struct.unpack(">I", frame[:4])[0]
    if (
        size == 0
        or size > MAXIMUM_EXECUTION_JSON_BYTES
        or len(frame) != size + 4
    ):
        raise LifecycleExecutionProtocolError("execution_protocol_oversized")
    try:
        document = json.loads(
            frame[4:].decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise LifecycleExecutionProtocolError() from None
    if not isinstance(document, dict):
        raise LifecycleExecutionProtocolError()
    try:
        message_type = LifecycleExecutionMessageType(document.get("message_type"))
        message_class = _MESSAGE_CLASS[message_type]
        expected = set(message_class.__dataclass_fields__)
        if set(document) != expected:
            raise ValueError
        values = dict(document)
        values["message_type"] = message_type
        if "direction" in values:
            values["direction"] = LifecycleExecutionDirection(values["direction"])
        if "effect_kind" in values:
            values["effect_kind"] = LifecycleEffectKind(values["effect_kind"])
        message = message_class(**values)
    except (KeyError, TypeError, ValueError):
        raise LifecycleExecutionProtocolError() from None
    return validate_lifecycle_execution_message(message)  # type: ignore[arg-type,return-value]


def validate_lifecycle_execution_message(
    message: LifecycleExecutionMessageV2,
) -> LifecycleExecutionMessageV2:
    if message.protocol_version != LIFECYCLE_EXECUTION_PROTOCOL_VERSION:
        raise LifecycleExecutionProtocolError()
    expected_class = _MESSAGE_CLASS.get(message.message_type)
    if expected_class is None or not isinstance(message, expected_class):
        raise LifecycleExecutionProtocolError()
    _uuid(message.transaction_id)
    _uuid(message.grant_id)
    _uuid(message.runner_session_id)
    _sequence(message.sequence)
    _uuid(message.nonce)

    if isinstance(message, LifecycleRunnerRegistrationV2):
        _digest(message.grant_digest)
        if message.runner_identifier != LIFECYCLE_RUNNER_IDENTIFIER:
            raise LifecycleExecutionProtocolError()
        _digest(message.runner_build_digest)
        _digest(message.runner_identity_digest)
        _team(message.team_identifier)
        _digest(message.code_requirement_digest)
        if (
            isinstance(message.requested_lease_seconds, bool)
            or not isinstance(message.requested_lease_seconds, int)
            or not 5
            <= message.requested_lease_seconds
            <= MAXIMUM_RUNNER_LEASE_SECONDS
        ):
            raise LifecycleExecutionProtocolError()
    elif isinstance(message, LifecycleRunnerRegisteredV2):
        _digest(message.grant_digest)
        _uuid(message.request_nonce)
        _uuid(message.lease_id)
        _epoch(message.lease_epoch)
        _integer(message.lease_expires_at, minimum=1)
    elif isinstance(message, LifecycleExecutionStartV2):
        _lease_fields(message)
        if not isinstance(message.direction, LifecycleExecutionDirection):
            raise LifecycleExecutionProtocolError()
        _digest(message.execution_manifest_digest)
        _digest(message.recovery_clone_identity_digest)
        _digest(message.authorization_digest)
        _uuid(message.authorization_session_id)
    elif isinstance(message, LifecycleExecutionObserveV2):
        _lease_fields(message)
        _effect_fields(message)
        if message.prior_receipt_digest is not None:
            _digest(message.prior_receipt_digest)
    elif isinstance(message, LifecycleExecutionApplyV2):
        _lease_fields(message)
        _effect_fields(message)
        _digest(message.observation_receipt_digest)
    elif isinstance(message, LifecycleExecutionFinalizeV2):
        _lease_fields(message)
        if not isinstance(message.direction, LifecycleExecutionDirection):
            raise LifecycleExecutionProtocolError()
        _digest(message.final_receipt_digest)
    elif isinstance(message, LifecycleExecutionRefusedV2):
        _uuid(message.request_nonce)
        if message.error_code not in _REFUSAL_CODES:
            raise LifecycleExecutionProtocolError()
    else:  # pragma: no cover - exhaustive class map above
        raise LifecycleExecutionProtocolError()
    return message


def _wire_dict(message: object) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in message.__dataclass_fields__:  # type: ignore[attr-defined]
        value = getattr(message, name)
        result[name] = value.value if isinstance(value, Enum) else value
    return result


def _lease_fields(message: object) -> None:
    _digest(getattr(message, "grant_digest"))
    _uuid(getattr(message, "lease_id"))
    _epoch(getattr(message, "lease_epoch"))


def _effect_fields(message: object) -> None:
    _uuid(getattr(message, "effect_id"))
    if not isinstance(getattr(message, "effect_kind"), LifecycleEffectKind):
        raise LifecycleExecutionProtocolError()
    _digest(getattr(message, "target_digest"))
    _integer(getattr(message, "attempt"), minimum=1, maximum=1_024)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise LifecycleExecutionProtocolError() from None


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _uuid(value: object) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise LifecycleExecutionProtocolError() from None
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise LifecycleExecutionProtocolError()
    return value


def _team(value: object) -> str:
    if not isinstance(value, str) or _TEAM_RE.fullmatch(value) is None:
        raise LifecycleExecutionProtocolError()
    if value in {"ADHOC00000", "UNSIGNED00", "NOTSET0000"}:
        raise LifecycleExecutionProtocolError()
    return value


def _sequence(value: object) -> int:
    return _integer(value, minimum=1, maximum=1_000_000)


def _epoch(value: object) -> int:
    return _integer(value, minimum=1, maximum=1_000_000)


def _integer(
    value: object, *, minimum: int, maximum: int = 4_102_444_800
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise LifecycleExecutionProtocolError()
    return value


def validate_execution_timestamp(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 4_102_444_800.0
    ):
        raise LifecycleExecutionProtocolError()
    return float(value)


class LifecycleExecutionProtocolV2:
    """Namespaced façade shared with the Swift protocol surface."""

    protocol_version: Final[int] = LIFECYCLE_EXECUTION_PROTOCOL_VERSION
    runner_identifier: Final[str] = LIFECYCLE_RUNNER_IDENTIFIER
    maximum_json_bytes: Final[int] = MAXIMUM_EXECUTION_JSON_BYTES
    maximum_frame_bytes: Final[int] = MAXIMUM_EXECUTION_FRAME_BYTES
    maximum_lease_seconds: Final[int] = MAXIMUM_RUNNER_LEASE_SECONDS

    encode_frame = staticmethod(encode_lifecycle_execution_message)
    decode_frame = staticmethod(decode_lifecycle_execution_frame)
    validate = staticmethod(validate_lifecycle_execution_message)


__all__ = [
    "LIFECYCLE_EXECUTION_PROTOCOL_VERSION",
    "LIFECYCLE_RUNNER_IDENTIFIER",
    "LifecycleEffectKind",
    "LifecycleEffectObservation",
    "LifecycleEffectReceiptStatus",
    "LifecycleExecutionApplyV2",
    "LifecycleExecutionDirection",
    "LifecycleExecutionFinalizeV2",
    "LifecycleExecutionMessageType",
    "LifecycleExecutionMessageV2",
    "LifecycleExecutionObserveV2",
    "LifecycleExecutionProtocolError",
    "LifecycleExecutionProtocolV2",
    "LifecycleExecutionRefusedV2",
    "LifecycleExecutionStartV2",
    "LifecycleRunnerRegisteredV2",
    "LifecycleRunnerRegistrationV2",
    "MAXIMUM_EXECUTION_FRAME_BYTES",
    "MAXIMUM_EXECUTION_JSON_BYTES",
    "MAXIMUM_RUNNER_LEASE_SECONDS",
    "decode_lifecycle_execution_frame",
    "encode_lifecycle_execution_message",
    "validate_execution_timestamp",
    "validate_lifecycle_execution_message",
]
