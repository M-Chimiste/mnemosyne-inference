from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any, Final

from fastapi import Request

from .inventory_store import InventoryStore
from .pairing_coordinator import PairingCoordinator
from .placement import (
    MAX_PLACEMENT_REQUEST_BYTES,
    PlacementCandidateInput,
    PlacementInputError,
    PlacementRequest,
)


_INTENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "logical_model_id",
        "recipe_id",
        "required_capabilities",
        "required_context_tokens",
        "required_concurrency",
        "allowed_service_classes",
    }
)
_SERVICE_CLASSES: Final[tuple[str, ...]] = (
    "primary",
    "opportunistic",
    "overflow",
)
_SAFE_DISPLAY_NAME = re.compile(r"^[^/\\\x00-\x1f]{1,64}$")


class PlacementAPIError(RuntimeError):
    """A fixed public placement error that never reflects caller input."""

    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


async def parse_placement_intent(
    request: Request,
    *,
    recommendation_id: str,
    created_at: int | float,
    valid_for_seconds: int,
) -> PlacementRequest:
    """Accept only user intent; Hub authority stamps identity and time."""

    try:
        if str(uuid.UUID(recommendation_id)) != recommendation_id:
            raise ValueError
        stamped_at = float(created_at)
        if (
            not math.isfinite(stamped_at)
            or stamped_at < 0
            or stamped_at > 4_102_444_800
            or isinstance(valid_for_seconds, bool)
            or not isinstance(valid_for_seconds, int)
            or not 1 <= valid_for_seconds <= 300
        ):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise PlacementAPIError(503, "placement_clock_unavailable") from None

    encoding = request.headers.get("content-encoding", "identity").lower()
    if encoding != "identity":
        raise PlacementAPIError(415, "placement_content_encoding_unsupported")
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise PlacementAPIError(415, "placement_json_required")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise PlacementAPIError(400, "placement_request_invalid") from None
        if declared_size < 0:
            raise PlacementAPIError(400, "placement_request_invalid")
        if declared_size > MAX_PLACEMENT_REQUEST_BYTES:
            raise PlacementAPIError(413, "placement_request_too_large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_PLACEMENT_REQUEST_BYTES:
            raise PlacementAPIError(413, "placement_request_too_large")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate member")
            value[key] = item
        return value

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        value = json.loads(
            bytes(body).decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PlacementAPIError(400, "placement_request_invalid") from None
    if not isinstance(value, dict) or set(value) != _INTENT_KEYS:
        raise PlacementAPIError(400, "placement_request_invalid")
    if value.get("schema_version") != 1:
        raise PlacementAPIError(400, "placement_request_invalid")
    capabilities = value.get("required_capabilities")
    service_classes = value.get("allowed_service_classes")
    if not isinstance(capabilities, list) or not isinstance(service_classes, list):
        raise PlacementAPIError(400, "placement_request_invalid")
    try:
        if capabilities != sorted(set(capabilities)):
            raise PlacementAPIError(400, "placement_request_invalid")
        if service_classes != [
            item for item in _SERVICE_CLASSES if item in service_classes
        ] or len(service_classes) != len(set(service_classes)):
            raise PlacementAPIError(400, "placement_request_invalid")
        placement_request = PlacementRequest(
            recommendation_id=recommendation_id,
            created_at=stamped_at,
            valid_for_seconds=valid_for_seconds,
            logical_model_id=value["logical_model_id"],
            recipe_id=value["recipe_id"],
            required_capabilities=tuple(capabilities),
            required_context_tokens=value["required_context_tokens"],
            required_concurrency=value["required_concurrency"],
            allowed_service_classes=tuple(service_classes),
        )
    except PlacementAPIError:
        raise
    except (KeyError, TypeError, ValueError, PlacementInputError):
        raise PlacementAPIError(400, "placement_request_invalid") from None
    return placement_request


async def inventory_backed_candidates(
    *,
    coordinator: PairingCoordinator,
    inventory_store: InventoryStore,
) -> tuple[PlacementCandidateInput, ...]:
    """Adapt enrollment and inventory authority without touching routing."""

    enrollments = {
        enrollment.pairing_id: enrollment
        for enrollment in await coordinator.enrollments()
    }
    records = await inventory_store.records(limit=1000)
    if len(records) >= 1000:
        # Never truncate a recommendation and imply the omitted Macs were
        # considered. A larger protocol revision can add bounded pagination.
        raise PlacementAPIError(503, "placement_candidate_limit_reached")
    candidates: list[PlacementCandidateInput] = []
    for record in records:
        enrollment = enrollments.get(record.pairing_id)
        if enrollment is None:
            # Inventory is accepted only for a durable active enrollment. An
            # orphaned row is a store-integrity problem, not an invented Mac.
            raise PlacementAPIError(503, "placement_enrollment_unavailable")
        if enrollment.lifecycle_state == "revoked":
            enrollment_state = "revoked"
        elif enrollment.lifecycle_state == "active" and enrollment.hub_enabled:
            enrollment_state = "active"
        else:
            enrollment_state = "disabled"
        freshness = inventory_store.freshness(
            record,
            enrollment_active=enrollment.lifecycle_state == "active",
            active_credential_generation=enrollment.credential_generation,
        )
        if freshness["state"] == "fresh":
            freshness_state = "fresh"
        elif freshness["reason"] == "hub_restarted":
            freshness_state = "hub_restarted"
        else:
            freshness_state = "expired"
        display_name = enrollment.display_name
        if _SAFE_DISPLAY_NAME.fullmatch(display_name) is None:
            display_name = f"Paired Mac {record.pairing_id[:8]}"
        candidates.append(
            PlacementCandidateInput(
                pairing_id=record.pairing_id,
                pairing_display_name=display_name,
                service_class=enrollment.service_class,
                enrollment_state=enrollment_state,
                active_credential_generation=enrollment.credential_generation,
                freshness_state=freshness_state,
                inventory_received_at=record.received_at,
                basis_expires_at=(
                    record.received_at
                    + inventory_store.freshness_ttl_seconds
                ),
                inventory=record.inventory,
                hub_remote_installs_enabled=True,
            )
        )
    return tuple(candidates)


def new_recommendation_id() -> str:
    return str(uuid.uuid4())


__all__ = [
    "PlacementAPIError",
    "inventory_backed_candidates",
    "new_recommendation_id",
    "parse_placement_intent",
]
