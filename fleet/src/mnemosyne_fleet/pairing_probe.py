from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable

import httpx

from .paired_transport import create_pinned_node_client
from .pairing_coordinator import ActivationCandidate, PairingCoordinatorError
from .protocol import Snapshot, validate_snapshot
from .registry import MAX_SNAPSHOT_BYTES


MAX_ACTIVATION_MODELS_BYTES = 2 * 1024 * 1024
MAX_ACTIVATION_MODELS = 10_000
_PATH_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")

PinnedClientFactory = Callable[..., httpx.AsyncClient]


async def probe_activation_candidate(
    candidate: ActivationCandidate,
    *,
    timeout_seconds: float,
    client_factory: PinnedClientFactory = create_pinned_node_client,
) -> None:
    """Prove candidate snapshot and dispatch credentials without loading work."""

    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
    try:
        client = client_factory(candidate.locator, timeout=timeout)
        async with client:
            before = await _snapshot_probe(client, candidate)
            await _models_probe(client, candidate)
            after = await _snapshot_probe(client, candidate)
    except PairingCoordinatorError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError):
        raise PairingCoordinatorError("pairing_activation_probe_failed") from None

    if (
        after.node.instance_id != before.node.instance_id
        or after.snapshot_sequence <= before.snapshot_sequence
        or after.residency != before.residency
    ):
        raise PairingCoordinatorError("pairing_activation_probe_changed_state")


async def _snapshot_probe(
    client: httpx.AsyncClient,
    candidate: ActivationCandidate,
) -> Snapshot:
    payload = await _bounded_json(
        client,
        "/fleet/v1/snapshot",
        headers={
            "Authorization": (
                f"Bearer {candidate.credentials.snapshot.secret}"
            )
        },
        maximum_bytes=MAX_SNAPSHOT_BYTES,
    )
    try:
        snapshot = validate_snapshot(
            payload,
            expected_node_id=candidate.reporting_node_id,
            ttl_seconds=1,
        )
    except (ValueError, TypeError):
        raise PairingCoordinatorError("pairing_activation_snapshot_invalid") from None
    if (
        snapshot.node.platform != "macos"
        or snapshot.node.instance_id != candidate.service_instance_id
    ):
        raise PairingCoordinatorError("pairing_activation_identity_mismatch")
    return snapshot


async def _models_probe(
    client: httpx.AsyncClient,
    candidate: ActivationCandidate,
) -> None:
    payload = await _bounded_json(
        client,
        "/v1/models",
        headers={
            "Authorization": (
                f"Bearer {candidate.credentials.dispatch.secret}"
            ),
            "X-Mnemosyne-Fleet-Route": str(uuid.uuid4()),
        },
        maximum_bytes=MAX_ACTIVATION_MODELS_BYTES,
    )
    if not isinstance(payload, dict) or set(payload) != {"object", "data"}:
        raise PairingCoordinatorError("pairing_activation_models_invalid")
    rows = payload.get("data")
    if payload.get("object") != "list" or not isinstance(rows, list):
        raise PairingCoordinatorError("pairing_activation_models_invalid")
    if len(rows) > MAX_ACTIVATION_MODELS:
        raise PairingCoordinatorError("pairing_activation_models_invalid")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "object", "owned_by"}:
            raise PairingCoordinatorError("pairing_activation_models_invalid")
        identifier = row.get("id")
        owner = row.get("owned_by")
        if (
            row.get("object") != "model"
            or not isinstance(identifier, str)
            or not 1 <= len(identifier.encode("utf-8")) <= 256
            or identifier.startswith("/")
            or _PATH_DRIVE.match(identifier)
            or "://" in identifier
            or "\0" in identifier
            or not isinstance(owner, str)
            or not 1 <= len(owner.encode("utf-8")) <= 64
        ):
            raise PairingCoordinatorError("pairing_activation_models_invalid")


async def _bounded_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    headers: dict[str, str],
    maximum_bytes: int,
) -> object:
    async with client.stream("GET", path, headers=headers) as response:
        if response.status_code != 200:
            raise PairingCoordinatorError("pairing_activation_probe_failed")
        encoding = response.headers.get("content-encoding", "identity").lower()
        if encoding != "identity":
            raise PairingCoordinatorError("pairing_activation_probe_failed")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                raise PairingCoordinatorError(
                    "pairing_activation_probe_failed"
                ) from None
            if declared_size < 0 or declared_size > maximum_bytes:
                raise PairingCoordinatorError("pairing_activation_probe_failed")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > maximum_bytes:
                raise PairingCoordinatorError("pairing_activation_probe_failed")
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PairingCoordinatorError("pairing_activation_probe_failed") from None

