from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from fastapi import Request

from .desired_install_protocol import DesiredInstallDocument, validate_desired_install
from .desired_install_store import DesiredInstallRecord
from .placement import (
    MAX_PLACEMENT_REQUEST_BYTES,
    PlacementInputError,
    PlacementRecommendationDocument,
    PlacementRequest,
    RecipeRequirements,
)


_CREATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "idempotency_key",
        "logical_model_id",
        "recipe_id",
        "required_capabilities",
        "required_context_tokens",
        "required_concurrency",
        "allowed_service_classes",
        "candidate_basis",
    }
)
_OPTIONAL_CREATE_KEYS: Final[frozenset[str]] = frozenset({"alias"})
_BASIS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "pairing_id",
        "credential_generation",
        "inventory_instance_id",
        "inventory_sequence",
        "inventory_received_at",
        "basis_expires_at",
        "storage_location_id",
        "storage_binding_generation",
        "catalog_digest",
    }
)
_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "chat/completions",
        "completions",
        "embeddings",
        "messages",
        "rerank",
        "responses",
    }
)
_SERVICE_CLASSES: Final[tuple[str, ...]] = (
    "primary",
    "opportunistic",
    "overflow",
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_SAFE_ALIAS = re.compile(r"^[^/\\\x00-\x1f]{1,128}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MODEL_INSTALL_READY_RUNTIME_STATES: Final[frozenset[str]] = frozenset(
    {"compatible_verified", "compatible_unverified"}
)


class DesiredInstallAPIError(RuntimeError):
    """A fixed public DesiredInstall error without reflected input."""

    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DesiredInstallIntent:
    value: dict[str, Any] = field(repr=False)
    canonical_json: bytes = field(repr=False)
    intent_digest: str
    idempotency_key: str
    logical_model_id: str
    recipe_id: str
    required_capabilities: tuple[str, ...]
    required_context_tokens: int
    required_concurrency: int
    allowed_service_classes: tuple[str, ...]
    candidate_basis: dict[str, Any] = field(repr=False)
    alias: str | None

    def placement_request(
        self,
        *,
        recommendation_id: str,
        created_at: float,
        valid_for_seconds: int,
    ) -> PlacementRequest:
        try:
            return PlacementRequest(
                recommendation_id=recommendation_id,
                created_at=created_at,
                valid_for_seconds=valid_for_seconds,
                logical_model_id=self.logical_model_id,
                recipe_id=self.recipe_id,
                required_capabilities=self.required_capabilities,
                required_context_tokens=self.required_context_tokens,
                required_concurrency=self.required_concurrency,
                allowed_service_classes=self.allowed_service_classes,
            )
        except PlacementInputError:
            raise DesiredInstallAPIError(
                400, "desired_install_request_invalid"
            ) from None


async def parse_desired_install_intent(request: Request) -> DesiredInstallIntent:
    encoding = request.headers.get("content-encoding", "identity").lower()
    if encoding != "identity":
        raise DesiredInstallAPIError(
            415, "desired_install_content_encoding_unsupported"
        )
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise DesiredInstallAPIError(415, "desired_install_json_required")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise DesiredInstallAPIError(
                400, "desired_install_request_invalid"
            ) from None
        if size < 0:
            raise DesiredInstallAPIError(400, "desired_install_request_invalid")
        if size > MAX_PLACEMENT_REQUEST_BYTES:
            raise DesiredInstallAPIError(413, "desired_install_request_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_PLACEMENT_REQUEST_BYTES:
            raise DesiredInstallAPIError(413, "desired_install_request_too_large")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    try:
        value = json.loads(
            bytes(body).decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise DesiredInstallAPIError(400, "desired_install_request_invalid") from None
    return validate_desired_install_intent(value)


def validate_desired_install_intent(value: object) -> DesiredInstallIntent:
    if (
        not isinstance(value, dict)
        or not _CREATE_KEYS.issubset(value)
        or set(value) - (_CREATE_KEYS | _OPTIONAL_CREATE_KEYS)
        or value.get("schema_version") != 1
    ):
        raise DesiredInstallAPIError(400, "desired_install_request_invalid")
    try:
        idempotency_key = value["idempotency_key"]
        if str(uuid.UUID(idempotency_key)) != idempotency_key:
            raise ValueError
        logical_model_id = value["logical_model_id"]
        recipe_id = value["recipe_id"]
        if (
            not isinstance(logical_model_id, str)
            or _SAFE_IDENTIFIER.fullmatch(logical_model_id) is None
            or not isinstance(recipe_id, str)
            or _SAFE_IDENTIFIER.fullmatch(recipe_id) is None
        ):
            raise ValueError
        capabilities = value["required_capabilities"]
        service_classes = value["allowed_service_classes"]
        if (
            not isinstance(capabilities, list)
            or not 1 <= len(capabilities) <= len(_CAPABILITIES)
            or capabilities != sorted(set(capabilities))
            or not set(capabilities).issubset(_CAPABILITIES)
            or not isinstance(service_classes, list)
            or not service_classes
            or service_classes
            != [item for item in _SERVICE_CLASSES if item in service_classes]
            or len(service_classes) != len(set(service_classes))
        ):
            raise ValueError
        context = value["required_context_tokens"]
        concurrency = value["required_concurrency"]
        if (
            isinstance(context, bool)
            or not isinstance(context, int)
            or not 1 <= context <= 100_000_000
            or isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 1 <= concurrency <= 1024
        ):
            raise ValueError
        basis = value["candidate_basis"]
        if not isinstance(basis, dict) or set(basis) != _BASIS_KEYS:
            raise ValueError
        pairing_id = basis["pairing_id"]
        instance_id = basis["inventory_instance_id"]
        storage_id = basis["storage_location_id"]
        for identifier in (pairing_id, instance_id, storage_id):
            if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
                raise ValueError
        for key, minimum, maximum in (
            ("credential_generation", 1, 2_147_483_647),
            ("inventory_sequence", 0, 9_007_199_254_740_991),
            ("storage_binding_generation", 1, 2_147_483_647),
        ):
            item = basis[key]
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or not minimum <= item <= maximum
            ):
                raise ValueError
        received = basis["inventory_received_at"]
        expires = basis["basis_expires_at"]
        if (
            isinstance(received, bool)
            or not isinstance(received, (int, float))
            or isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(float(received))
            or not math.isfinite(float(expires))
            or not 0 <= float(received) <= float(expires) <= 4_102_444_800
            or not isinstance(basis["catalog_digest"], str)
            or _SHA256.fullmatch(basis["catalog_digest"]) is None
        ):
            raise ValueError
        alias = value.get("alias")
        if alias is not None and (
            not isinstance(alias, str) or _SAFE_ALIAS.fullmatch(alias) is None
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
        raise DesiredInstallAPIError(400, "desired_install_request_invalid") from None
    if len(canonical) > MAX_PLACEMENT_REQUEST_BYTES:
        raise DesiredInstallAPIError(413, "desired_install_request_too_large")
    normalized = json.loads(canonical)
    return DesiredInstallIntent(
        value=normalized,
        canonical_json=canonical,
        intent_digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        idempotency_key=idempotency_key,
        logical_model_id=logical_model_id,
        recipe_id=recipe_id,
        required_capabilities=tuple(capabilities),
        required_context_tokens=context,
        required_concurrency=concurrency,
        allowed_service_classes=tuple(service_classes),
        candidate_basis=normalized["candidate_basis"],
        alias=alias,
    )


def select_exact_candidate(
    recommendation: PlacementRecommendationDocument,
    *,
    basis: dict[str, Any],
) -> dict[str, Any]:
    matches = [
        candidate
        for candidate in recommendation.value["candidates"]
        if candidate["basis"] == basis
    ]
    if len(matches) != 1:
        raise DesiredInstallAPIError(409, "desired_install_basis_changed")
    selected = matches[0]
    if (
        selected["eligible"] is not True
        or selected.get("runtime_state")
        not in _MODEL_INSTALL_READY_RUNTIME_STATES
    ):
        raise DesiredInstallAPIError(409, "desired_install_candidate_ineligible")
    return selected


def current_candidate_for_job(
    recommendation: PlacementRecommendationDocument,
    record: DesiredInstallRecord,
) -> dict[str, Any]:
    document = record.document
    matches = []
    for candidate in recommendation.value["candidates"]:
        basis = candidate["basis"]
        if (
            basis["pairing_id"] == document.pairing_id
            and basis["credential_generation"] == document.credential_generation
            and basis["inventory_instance_id"] == document.inventory_instance_id
            and basis["inventory_sequence"] >= document.inventory_sequence
            and basis["storage_location_id"] == document.storage_location_id
            and basis["storage_binding_generation"]
            == document.storage_binding_generation
            and basis["catalog_digest"] == document.value["catalog_digest"]
        ):
            matches.append(candidate)
    if (
        len(matches) != 1
        or matches[0]["eligible"] is not True
        or matches[0].get("runtime_state")
        not in _MODEL_INSTALL_READY_RUNTIME_STATES
    ):
        raise DesiredInstallAPIError(409, "desired_install_delivery_fenced")
    return matches[0]


def build_run_document(
    intent: DesiredInstallIntent,
    requirements: RecipeRequirements,
    *,
    job_id: str,
    created_at: float,
    valid_for_seconds: int,
) -> DesiredInstallDocument:
    basis = intent.candidate_basis
    value: dict[str, Any] = {
        "schema_version": 1,
        "job_id": job_id,
        "job_revision": 1,
        "idempotency_key": intent.idempotency_key,
        "desired_state": "run",
        "created_at": created_at,
        "expires_at": created_at + valid_for_seconds,
        "valid_for_seconds": valid_for_seconds,
        "pairing_id": basis["pairing_id"],
        "credential_generation": basis["credential_generation"],
        "recommendation_basis": {
            "inventory_instance_id": basis["inventory_instance_id"],
            "inventory_sequence": basis["inventory_sequence"],
        },
        "catalog_version": requirements.catalog_version,
        "catalog_digest": requirements.catalog_digest,
        "logical_model_id": requirements.logical_model_id,
        "recipe_id": requirements.recipe_id,
        "artifact_id": requirements.artifact_id,
        "engine": requirements.engine,
        "capabilities": list(requirements.capabilities),
        "guaranteed_context_tokens": requirements.guaranteed_context_tokens,
        "storage_location_id": basis["storage_location_id"],
        "storage_binding_generation": basis["storage_binding_generation"],
    }
    if intent.alias is not None:
        value["alias"] = intent.alias
    try:
        return validate_desired_install(value)
    except Exception:
        raise DesiredInstallAPIError(503, "desired_install_unavailable") from None


def job_record_payload(
    record: DesiredInstallRecord,
    *,
    now: float,
) -> dict[str, Any]:
    acknowledgement = (
        None if record.acknowledgement is None else record.acknowledgement.value
    )
    return {
        "schema_version": 1,
        "job": record.document.value,
        "status": {
            "expired": record.document.expires_at <= now,
            "terminal": record.terminal,
            "delivery_count": record.delivery_count,
            "last_delivered_revision": record.last_delivered_revision,
            "last_delivered_at": record.last_delivered_at,
            "acknowledgement": acknowledgement,
        },
    }


def new_job_id() -> str:
    return str(uuid.uuid4())


__all__ = [
    "DesiredInstallAPIError",
    "DesiredInstallIntent",
    "build_run_document",
    "current_candidate_for_job",
    "job_record_payload",
    "new_job_id",
    "parse_desired_install_intent",
    "select_exact_candidate",
    "validate_desired_install_intent",
]
