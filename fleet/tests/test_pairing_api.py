from __future__ import annotations

import json
import uuid

import pytest
from starlette.requests import Request

from mnemosyne_fleet.pairing_api import (
    InvitationClaim,
    InvitationCreate,
    MAX_PAIRING_REQUEST_BYTES,
    PairingAPIError,
    parse_pairing_payload,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _request(body: bytes, *, content_type: str = "application/json") -> Request:
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    headers = [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/fleet/pairing/v1/claims",
            "raw_path": b"/fleet/pairing/v1/claims",
            "query_string": b"",
            "headers": headers,
            "client": ("192.0.2.2", 1234),
            "server": ("nyx.example.internal", 443),
        },
        receive,
    )


def _claim_payload(secret: str = "s" * 43) -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": _uuid(),
        "invitation_id": _uuid(),
        "pairing_secret": secret,
        "mac": {
            "platform": "macos",
            "service_version": "0.9.0",
            "display_name": "Studio Mac",
            "reporting_node_id": "metis",
        },
        "locator": "https://mac-a.example.internal:1240",
        "supported_protocol": {"minimum": 1, "maximum": 1},
    }


async def test_pairing_claim_parses_strict_nested_contract_and_masks_secrets() -> None:
    payload = _claim_payload()
    parsed = await parse_pairing_payload(
        _request(json.dumps(payload).encode()),
        InvitationClaim,
    )

    assert parsed.schema_version == 1
    assert parsed.mac.reporting_node_id == "metis"
    assert parsed.pairing_secret.get_secret_value() == "s" * 43
    assert "s" * 43 not in repr(parsed)
    assert "mac-a.example.internal" not in repr(parsed)


async def test_pairing_parser_collapses_secret_bearing_validation_failures() -> None:
    secret = "private-pairing-secret-that-must-never-be-reflected"
    payload = _claim_payload(secret)
    payload["unexpected"] = secret

    with pytest.raises(PairingAPIError) as rejected:
        await parse_pairing_payload(
            _request(json.dumps(payload).encode()),
            InvitationClaim,
        )

    assert rejected.value.status_code == 400
    assert rejected.value.code == "pairing_invalid_request"
    assert secret not in repr(rejected.value)
    assert secret not in str(rejected.value)


async def test_pairing_parser_rejects_oversize_or_encoded_bodies_before_parse() -> None:
    oversized = b"{" + (b" " * MAX_PAIRING_REQUEST_BYTES) + b"}"
    with pytest.raises(PairingAPIError) as too_large:
        await parse_pairing_payload(_request(oversized), InvitationClaim)
    assert too_large.value.status_code == 413

    encoded = _request(json.dumps(_claim_payload()).encode())
    encoded.scope["headers"].append((b"content-encoding", b"gzip"))
    with pytest.raises(PairingAPIError) as unsupported:
        await parse_pairing_payload(encoded, InvitationClaim)
    assert unsupported.value.status_code == 415


async def test_invitation_requires_canonical_ids_and_explicit_static_adoption() -> None:
    payload = {
        "schema_version": 1,
        "request_id": _uuid(),
        "intent": "adopt-static",
        "expected": {
            "platform": "macos",
            "reporting_node_id": None,
            "locator": "https://mac-a.example.internal:1240",
            "transport": "https",
            "service_class": "primary",
        },
        "expires_in_seconds": 300,
    }
    with pytest.raises(PairingAPIError) as missing_identity:
        await parse_pairing_payload(
            _request(json.dumps(payload).encode()),
            InvitationCreate,
        )
    assert missing_identity.value.code == "pairing_invalid_request"

    payload["intent"] = "new"
    payload["request_id"] = str(payload["request_id"]).upper()
    with pytest.raises(PairingAPIError) as noncanonical:
        await parse_pairing_payload(
            _request(json.dumps(payload).encode()),
            InvitationCreate,
        )
    assert noncanonical.value.code == "pairing_invalid_request"

