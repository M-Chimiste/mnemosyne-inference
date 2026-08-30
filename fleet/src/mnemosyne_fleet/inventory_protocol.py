from __future__ import annotations

import hashlib
import importlib.resources
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from fastapi import Request
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError


INVENTORY_SCHEMA_VERSION: Final[int] = 1
MAX_INVENTORY_REQUEST_BYTES: Final[int] = 2 * 1024 * 1024


class InventoryProtocolError(RuntimeError):
    """A fixed public failure that never contains caller-controlled input."""

    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


def _load_schema() -> dict[str, Any]:
    packaged = importlib.resources.files("mnemosyne_fleet").joinpath(
        "schemas", "mac_inventory.schema.json"
    )
    if packaged.is_file():
        raw = packaged.read_text(encoding="utf-8")
    else:
        # Source checkouts keep the normative protocol outside the independent
        # Fleet package. The wheel build force-includes that exact file at the
        # packaged resource path above.
        canonical = (
            Path(__file__).resolve().parents[3]
            / "mac_pool_protocol"
            / "v1"
            / "mac_inventory.schema.json"
        )
        raw = canonical.read_text(encoding="utf-8")
    value = json.loads(raw)
    Draft202012Validator.check_schema(value)
    return value


_SCHEMA = _load_schema()
_SCHEMA_VALIDATOR = Draft202012Validator(_SCHEMA)


@dataclass(frozen=True, slots=True)
class MacInventoryDocument:
    """A schema-approved, canonical inventory document.

    The representation contains no transport, locator, credential, or local
    path field because the strict version-1 schema has no such extension
    points. ``canonical_json`` is the only payload representation persisted by
    Fleet.
    """

    value: dict[str, Any] = field(repr=False)
    canonical_json: bytes = field(repr=False)
    payload_digest: str
    pairing_id: str
    credential_generation: int
    inventory_instance_id: str
    inventory_sequence: int
    observed_at: float


def validate_inventory(value: object) -> MacInventoryDocument:
    try:
        _SCHEMA_VALIDATOR.validate(value)
    except SchemaValidationError:
        raise InventoryProtocolError(400, "inventory_invalid_request") from None
    if not isinstance(value, dict):  # schema proves this; keeps typing honest
        raise InventoryProtocolError(400, "inventory_invalid_request")
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(canonical) > MAX_INVENTORY_REQUEST_BYTES:
        # Whitespace can only make the received document larger, but retaining
        # the defensive canonical bound protects non-HTTP callers too.
        raise InventoryProtocolError(413, "inventory_request_too_large")
    return MacInventoryDocument(
        value=value,
        canonical_json=canonical,
        payload_digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        pairing_id=str(value["pairing_id"]),
        credential_generation=int(value["credential_generation"]),
        inventory_instance_id=str(value["inventory_instance_id"]),
        inventory_sequence=int(value["inventory_sequence"]),
        observed_at=float(value["observed_at"]),
    )


async def parse_inventory_payload(request: Request) -> MacInventoryDocument:
    """Read and validate bounded identity-encoded UTF-8 JSON.

    Parsing is deliberately manual. Framework validation errors can include
    fragments of the submitted document, which is inappropriate for a payload
    assembled from local machine state. All syntax and schema failures collapse
    to fixed codes without reflecting input.
    """

    encoding = request.headers.get("content-encoding", "identity").lower()
    if encoding != "identity":
        raise InventoryProtocolError(
            415,
            "inventory_content_encoding_unsupported",
        )
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise InventoryProtocolError(415, "inventory_json_required")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise InventoryProtocolError(
                400,
                "inventory_invalid_request",
            ) from None
        if declared_size < 0:
            raise InventoryProtocolError(400, "inventory_invalid_request")
        if declared_size > MAX_INVENTORY_REQUEST_BYTES:
            raise InventoryProtocolError(413, "inventory_request_too_large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_INVENTORY_REQUEST_BYTES:
            raise InventoryProtocolError(413, "inventory_request_too_large")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        decoded = bytes(body).decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise InventoryProtocolError(400, "inventory_invalid_request") from None
    return validate_inventory(value)
