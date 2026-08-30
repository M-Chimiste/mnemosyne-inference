from __future__ import annotations

import hashlib
import hmac
import importlib.resources
import json
import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError


DESIRED_INSTALL_SCHEMA_VERSION: Final[int] = 1
MAX_DESIRED_INSTALL_BYTES: Final[int] = 32 * 1024
ACK_STATES: Final[tuple[str, ...]] = (
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
ACK_RESULT_CODES: Final[frozenset[str | None]] = frozenset(
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


class DesiredInstallProtocolError(RuntimeError):
    """A fixed-code DesiredInstall failure without caller-controlled text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _load_schema() -> dict[str, Any]:
    packaged = importlib.resources.files("mnemosyne_fleet").joinpath(
        "schemas", "desired_install.schema.json"
    )
    if packaged.is_file():
        raw = packaged.read_text(encoding="utf-8")
    else:
        canonical = (
            Path(__file__).resolve().parents[3]
            / "mac_pool_protocol"
            / "v1"
            / "desired_install.schema.json"
        )
        raw = canonical.read_text(encoding="utf-8")
    value = json.loads(raw)
    Draft202012Validator.check_schema(value)
    return value


_SCHEMA = _load_schema()
_VALIDATOR = Draft202012Validator(_SCHEMA)


@dataclass(frozen=True, slots=True)
class DesiredInstallDocument:
    value: dict[str, Any] = field(repr=False)
    canonical_json: bytes = field(repr=False)
    payload_digest: str
    job_id: str
    job_revision: int
    idempotency_key: str
    desired_state: str
    pairing_id: str
    credential_generation: int
    inventory_instance_id: str
    inventory_sequence: int
    storage_location_id: str
    storage_binding_generation: int
    created_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class JobAcknowledgement:
    value: dict[str, Any] = field(repr=False)
    canonical_json: bytes = field(repr=False)
    payload_digest: str
    job_id: str
    job_revision: int
    state: str
    bytes_downloaded: int
    total_bytes: int | None
    updated_at: float
    result_code: str | None


def validate_job_acknowledgement(value: object) -> JobAcknowledgement:
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
        job_id = value["job_id"]
        if value["schema_version"] != 1 or str(uuid.UUID(job_id)) != job_id:
            raise ValueError
        revision = value["job_revision"]
        downloaded = value["bytes_downloaded"]
        total = value["total_bytes"]
        updated_at = value["updated_at"]
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 1 <= revision <= 2_147_483_647
            or value["state"] not in ACK_STATES
            or isinstance(downloaded, bool)
            or not isinstance(downloaded, int)
            or not 0 <= downloaded <= 1_152_921_504_606_846_976
            or (
                total is not None
                and (
                    isinstance(total, bool)
                    or not isinstance(total, int)
                    or not 0 <= total <= 1_152_921_504_606_846_976
                )
            )
            or (total is not None and downloaded > total)
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
            or not math.isfinite(float(updated_at))
            or not 0 <= float(updated_at) <= 4_102_444_800
            or value["result_code"] not in ACK_RESULT_CODES
        ):
            raise ValueError
        installation_id = value.get("installation_id")
        if installation_id is not None and (
            not isinstance(installation_id, str)
            or str(uuid.UUID(installation_id)) != installation_id
        ):
            raise ValueError
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (KeyError, TypeError, ValueError, AttributeError, UnicodeError):
        raise DesiredInstallProtocolError("desired_install_ack_invalid") from None
    if len(canonical) > MAX_DESIRED_INSTALL_BYTES:
        raise DesiredInstallProtocolError("desired_install_ack_invalid")
    return JobAcknowledgement(
        value=json.loads(canonical),
        canonical_json=canonical,
        payload_digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        job_id=job_id,
        job_revision=revision,
        state=str(value["state"]),
        bytes_downloaded=downloaded,
        total_bytes=total,
        updated_at=float(updated_at),
        result_code=value["result_code"],
    )


def validate_desired_install(value: object) -> DesiredInstallDocument:
    try:
        _VALIDATOR.validate(value)
    except SchemaValidationError:
        raise DesiredInstallProtocolError("desired_install_invalid") from None
    if not isinstance(value, dict):  # pragma: no cover - schema proves this
        raise DesiredInstallProtocolError("desired_install_invalid")
    try:
        for key in ("job_id", "idempotency_key", "pairing_id"):
            if str(uuid.UUID(str(value[key]))) != value[key]:
                raise ValueError
        instance_id = value["recommendation_basis"]["inventory_instance_id"]
        if str(uuid.UUID(str(instance_id))) != instance_id:
            raise ValueError
        storage_id = value["storage_location_id"]
        if str(uuid.UUID(str(storage_id))) != storage_id:
            raise ValueError
        created_at = float(value["created_at"])
        expires_at = float(value["expires_at"])
        valid_for = int(value["valid_for_seconds"])
        if (
            not math.isfinite(created_at)
            or not math.isfinite(expires_at)
            or not math.isclose(
                expires_at,
                created_at + valid_for,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
            or value["capabilities"] != sorted(set(value["capabilities"]))
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DesiredInstallProtocolError("desired_install_invalid") from None
    try:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise DesiredInstallProtocolError("desired_install_invalid") from None
    if len(canonical) > MAX_DESIRED_INSTALL_BYTES:
        raise DesiredInstallProtocolError("desired_install_too_large")
    return DesiredInstallDocument(
        value=json.loads(canonical),
        canonical_json=canonical,
        payload_digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        job_id=str(value["job_id"]),
        job_revision=int(value["job_revision"]),
        idempotency_key=str(value["idempotency_key"]),
        desired_state=str(value["desired_state"]),
        pairing_id=str(value["pairing_id"]),
        credential_generation=int(value["credential_generation"]),
        inventory_instance_id=str(instance_id),
        inventory_sequence=int(value["recommendation_basis"]["inventory_sequence"]),
        storage_location_id=str(storage_id),
        storage_binding_generation=int(value["storage_binding_generation"]),
        created_at=created_at,
        expires_at=expires_at,
    )


def parse_desired_install(raw: bytes | str) -> DesiredInstallDocument:
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


def desired_install_digest_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


__all__ = [
    "ACK_RESULT_CODES",
    "ACK_STATES",
    "DESIRED_INSTALL_SCHEMA_VERSION",
    "MAX_DESIRED_INSTALL_BYTES",
    "DesiredInstallDocument",
    "DesiredInstallProtocolError",
    "JobAcknowledgement",
    "desired_install_digest_equal",
    "parse_desired_install",
    "validate_desired_install",
    "validate_job_acknowledgement",
]
