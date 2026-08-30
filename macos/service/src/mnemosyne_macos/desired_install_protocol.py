"""Strict, path-free DesiredInstall v1 wire validation.

This module is deliberately pure protocol code.  It does not know how to
download a model, resolve a local storage binding, mutate configuration, or
contact the Hub.  A validated document is still only an unactioned intent.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.resources
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
import uuid

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError


DESIRED_INSTALL_SCHEMA_VERSION: Final[int] = 1
MAX_DESIRED_INSTALL_BYTES: Final[int] = 32 * 1024
MAX_JOB_ACKNOWLEDGEMENTS: Final[int] = 256
JOB_STATES: Final[tuple[str, ...]] = (
    "received",
    "awaiting_local_approval",
    "accepted",
    "downloading",
    "verifying",
    "downloaded_unregistered",
    "registered",
    "completed",
    "refused",
    "cancelled",
    "failed",
)
TERMINAL_JOB_STATES: Final[frozenset[str]] = frozenset(
    {"completed", "refused", "cancelled", "failed"}
)
JOB_RESULT_CODES: Final[frozenset[str | None]] = frozenset(
    {
        None,
        "local_approval_required",
        "local_policy_refused",
        "pairing_generation_changed",
        "inventory_basis_stale",
        "catalog_changed",
        "recipe_unknown",
        "artifact_mismatch",
        "storage_location_unknown",
        "storage_binding_changed",
        "storage_unavailable",
        "storage_read_only",
        "insufficient_storage",
        "runtime_unavailable",
        "verification_failed",
        "registration_failed",
        "cancelled_by_hub",
        "cancelled_locally",
        "idempotency_conflict",
        "internal_error",
    }
)
_MAX_TIMESTAMP: Final[float] = 4_102_444_800.0
_MAX_BYTES: Final[int] = 1_152_921_504_606_846_976


class DesiredInstallProtocolError(RuntimeError):
    """A fixed-code failure that never includes untrusted document content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _load_schema() -> dict[str, Any]:
    try:
        resource = importlib.resources.files("mnemosyne_macos").joinpath(
            "schemas", "desired_install.schema.json"
        )
        if resource.is_file():
            value = json.loads(resource.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(value)
            return value
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent
            / "mac_pool_protocol"
            / "v1"
            / "desired_install.schema.json"
        )
        if candidate.is_file():
            value = json.loads(candidate.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(value)
            return value
    raise DesiredInstallProtocolError("desired_install_schema_unavailable")


DESIRED_INSTALL_SCHEMA: Final[dict[str, Any]] = _load_schema()
_VALIDATOR = Draft202012Validator(DESIRED_INSTALL_SCHEMA)


@dataclass(frozen=True, slots=True)
class DesiredInstallDocument:
    """Canonical validated DesiredInstall data with indexed authority fields."""

    value: dict[str, Any] = field(repr=False)
    canonical_json: bytes = field(repr=False)
    payload_digest: str
    identity_digest: str
    job_id: str
    job_revision: int
    idempotency_key: str
    desired_state: str
    created_at: float
    expires_at: float
    valid_for_seconds: int
    pairing_id: str
    credential_generation: int
    inventory_instance_id: str
    inventory_sequence: int
    catalog_version: str
    catalog_digest: str
    logical_model_id: str
    recipe_id: str
    artifact_id: str
    engine: str
    capabilities: tuple[str, ...]
    guaranteed_context_tokens: int | None
    alias: str | None
    storage_location_id: str
    storage_binding_generation: int


@dataclass(frozen=True, slots=True)
class JobAcknowledgement:
    """One canonical acknowledgement conforming to MacInventory v1."""

    value: dict[str, Any] = field(repr=False)
    canonical_json: bytes = field(repr=False)
    payload_digest: str
    job_id: str
    job_revision: int
    installation_id: str | None
    state: str
    bytes_downloaded: int
    total_bytes: int | None
    updated_at: float
    result_code: str | None


def canonical_json(value: object, *, error_code: str) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise DesiredInstallProtocolError(error_code) from None


def parse_desired_install(raw: bytes | str) -> DesiredInstallDocument:
    """Parse bounded JSON while rejecting duplicate members and extensions."""

    if isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8")
        except UnicodeError:
            raise DesiredInstallProtocolError("desired_install_invalid") from None
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise DesiredInstallProtocolError("desired_install_invalid")
    if len(encoded) > MAX_DESIRED_INSTALL_BYTES:
        raise DesiredInstallProtocolError("desired_install_too_large")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise DesiredInstallProtocolError("desired_install_invalid") from None
    return validate_desired_install(value)


def validate_desired_install(value: object) -> DesiredInstallDocument:
    """Validate and canonicalize a DesiredInstall v1 mapping."""

    if (
        isinstance(value, dict)
        and "schema_version" in value
        and value.get("schema_version") != 1
    ):
        raise DesiredInstallProtocolError("desired_install_unsupported_version")
    try:
        _VALIDATOR.validate(value)
    except SchemaValidationError:
        raise DesiredInstallProtocolError("desired_install_invalid") from None
    if not isinstance(value, dict):  # pragma: no cover - proven by the schema
        raise DesiredInstallProtocolError("desired_install_invalid")
    try:
        job_id = _canonical_uuid(value["job_id"])
        idempotency_key = _canonical_uuid(value["idempotency_key"])
        pairing_id = _canonical_uuid(value["pairing_id"])
        basis = value["recommendation_basis"]
        inventory_instance_id = _canonical_uuid(basis["inventory_instance_id"])
        storage_location_id = _canonical_uuid(value["storage_location_id"])
        created_at = _timestamp(value["created_at"])
        expires_at = _timestamp(value["expires_at"])
        valid_for_seconds = int(value["valid_for_seconds"])
        capabilities = tuple(value["capabilities"])
        if (
            not math.isclose(
                expires_at,
                created_at + valid_for_seconds,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
            or capabilities != tuple(sorted(set(capabilities)))
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DesiredInstallProtocolError("desired_install_invalid") from None

    canonical = canonical_json(value, error_code="desired_install_invalid")
    if len(canonical) > MAX_DESIRED_INSTALL_BYTES:
        raise DesiredInstallProtocolError("desired_install_too_large")
    canonical_value = json.loads(canonical)
    identity = _identity_value(canonical_value)
    identity_json = canonical_json(
        identity, error_code="desired_install_invalid"
    )
    return DesiredInstallDocument(
        value=canonical_value,
        canonical_json=canonical,
        payload_digest=_digest(canonical),
        identity_digest=_digest(identity_json),
        job_id=job_id,
        job_revision=int(value["job_revision"]),
        idempotency_key=idempotency_key,
        desired_state=str(value["desired_state"]),
        created_at=created_at,
        expires_at=expires_at,
        valid_for_seconds=valid_for_seconds,
        pairing_id=pairing_id,
        credential_generation=int(value["credential_generation"]),
        inventory_instance_id=inventory_instance_id,
        inventory_sequence=int(basis["inventory_sequence"]),
        catalog_version=str(value["catalog_version"]),
        catalog_digest=str(value["catalog_digest"]),
        logical_model_id=str(value["logical_model_id"]),
        recipe_id=str(value["recipe_id"]),
        artifact_id=str(value["artifact_id"]),
        engine=str(value["engine"]),
        capabilities=capabilities,
        guaranteed_context_tokens=value["guaranteed_context_tokens"],
        alias=value.get("alias"),
        storage_location_id=storage_location_id,
        storage_binding_generation=int(value["storage_binding_generation"]),
    )


def validate_job_acknowledgement(value: object) -> JobAcknowledgement:
    """Validate the closed acknowledgement object embedded by MacInventory v1."""

    required = {
        "schema_version",
        "job_id",
        "job_revision",
        "state",
        "bytes_downloaded",
        "total_bytes",
        "updated_at",
        "result_code",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise DesiredInstallProtocolError("desired_install_ack_invalid")
    if set(value) - (required | {"installation_id"}):
        raise DesiredInstallProtocolError("desired_install_ack_invalid")
    try:
        if value["schema_version"] != 1:
            raise ValueError
        job_id = _canonical_uuid(value["job_id"])
        revision = _bounded_integer(value["job_revision"], 1, 2_147_483_647)
        installation_id = value.get("installation_id")
        if installation_id is not None:
            installation_id = _canonical_uuid(installation_id)
        state = value["state"]
        downloaded = _bounded_integer(value["bytes_downloaded"], 0, _MAX_BYTES)
        total = value["total_bytes"]
        if total is not None:
            total = _bounded_integer(total, 0, _MAX_BYTES)
        updated_at = _timestamp(value["updated_at"])
        result_code = value["result_code"]
        if (
            state not in JOB_STATES
            or result_code not in JOB_RESULT_CODES
            or (total is not None and downloaded > total)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DesiredInstallProtocolError("desired_install_ack_invalid") from None
    canonical = canonical_json(value, error_code="desired_install_ack_invalid")
    if len(canonical) > MAX_DESIRED_INSTALL_BYTES:
        raise DesiredInstallProtocolError("desired_install_ack_invalid")
    return JobAcknowledgement(
        value=json.loads(canonical),
        canonical_json=canonical,
        payload_digest=_digest(canonical),
        job_id=job_id,
        job_revision=revision,
        installation_id=installation_id,
        state=str(state),
        bytes_downloaded=downloaded,
        total_bytes=total,
        updated_at=updated_at,
        result_code=result_code,
    )


def build_job_acknowledgement(
    *,
    job_id: str,
    job_revision: int,
    state: str,
    bytes_downloaded: int,
    total_bytes: int | None,
    updated_at: float,
    result_code: str | None,
    installation_id: str | None = None,
) -> JobAcknowledgement:
    value: dict[str, Any] = {
        "schema_version": 1,
        "job_id": job_id,
        "job_revision": job_revision,
        "state": state,
        "bytes_downloaded": bytes_downloaded,
        "total_bytes": total_bytes,
        "updated_at": updated_at,
        "result_code": result_code,
    }
    if installation_id is not None:
        value["installation_id"] = installation_id
    return validate_job_acknowledgement(value)


def desired_install_digest_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _identity_value(value: dict[str, Any]) -> dict[str, Any]:
    """Return fields that must be immutable across run/cancel revisions."""

    return {
        key: value[key]
        for key in (
            "schema_version",
            "job_id",
            "idempotency_key",
            "pairing_id",
            "credential_generation",
            "recommendation_basis",
            "catalog_version",
            "catalog_digest",
            "logical_model_id",
            "recipe_id",
            "artifact_id",
            "engine",
            "capabilities",
            "guaranteed_context_tokens",
            "storage_location_id",
            "storage_binding_generation",
        )
    } | ({"alias": value["alias"]} if "alias" in value else {})


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError
    return value


def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    if not minimum <= value <= maximum:
        raise ValueError
    return value


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= _MAX_TIMESTAMP:
        raise ValueError
    return result


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


__all__ = [
    "DESIRED_INSTALL_SCHEMA",
    "DESIRED_INSTALL_SCHEMA_VERSION",
    "JOB_RESULT_CODES",
    "JOB_STATES",
    "MAX_DESIRED_INSTALL_BYTES",
    "MAX_JOB_ACKNOWLEDGEMENTS",
    "TERMINAL_JOB_STATES",
    "DesiredInstallDocument",
    "DesiredInstallProtocolError",
    "JobAcknowledgement",
    "build_job_acknowledgement",
    "canonical_json",
    "desired_install_digest_equal",
    "parse_desired_install",
    "validate_desired_install",
    "validate_job_acknowledgement",
]
