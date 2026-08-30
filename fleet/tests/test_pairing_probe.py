from __future__ import annotations

import httpx
import pytest

from mnemosyne_fleet.locator_policy import ResolvedLocator
from mnemosyne_fleet.pairing_coordinator import (
    ActivationCandidate,
    PairingCoordinatorError,
)
from mnemosyne_fleet.pairing_probe import probe_activation_candidate
from mnemosyne_fleet.secret_store import CredentialBundle, CredentialSecret

from .helpers import snapshot_payload


def _candidate() -> ActivationCandidate:
    return ActivationCandidate(
        locator=ResolvedLocator(
            origin="https://worker.example.internal:1240",
            transport="https",
            host="worker.example.internal",
            port=1240,
            addresses=("10.20.30.40",),
        ),
        credentials=CredentialBundle(
            pairing_id="11111111-1111-4111-8111-111111111111",
            generation=1,
            snapshot=CredentialSecret("snapshot-ref", "snapshot-secret"),
            dispatch=CredentialSecret("dispatch-ref", "dispatch-secret"),
            management=CredentialSecret("management-ref", "management-secret"),
        ),
        pairing_id="11111111-1111-4111-8111-111111111111",
        reporting_node_id="studio-mac",
        credential_generation=1,
        service_instance_id="service-instance-1",
    )


def _snapshot(sequence: int, *, warm: bool = False) -> dict[str, object]:
    payload = snapshot_payload(
        "studio-mac",
        sequence=sequence,
        instance_id="service-instance-1",
        warm=warm,
    )
    payload["node"]["platform"] = "macos"
    return payload


async def test_activation_probe_is_non_loading_and_uses_distinct_credentials() -> None:
    sequence = 0
    seen_models_marker: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sequence, seen_models_marker
        if request.url.path == "/fleet/v1/snapshot":
            assert request.headers["authorization"] == "Bearer snapshot-secret"
            sequence += 1
            return httpx.Response(200, json=_snapshot(sequence))
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer dispatch-secret"
        seen_models_marker = request.headers["x-mnemosyne-fleet-route"]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "qwen-coder",
                        "object": "model",
                        "owned_by": "mnemosyne",
                    }
                ],
            },
        )

    def client_factory(locator, **_kwargs):
        return httpx.AsyncClient(
            base_url=locator.origin,
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )

    await probe_activation_candidate(
        _candidate(),
        timeout_seconds=5,
        client_factory=client_factory,
    )
    assert sequence == 2
    assert seen_models_marker is not None


@pytest.mark.parametrize(
    "model_row",
    [
        {
            "id": "/Volumes/Athena/models/private.gguf",
            "object": "model",
            "owned_by": "mnemosyne",
        },
        {
            "id": "safe-alias",
            "object": "model",
            "owned_by": "mnemosyne",
            "upstream_model": "/private/model.gguf",
        },
    ],
)
async def test_activation_probe_rejects_path_bearing_or_rich_catalogs(
    model_row,
) -> None:
    sequence = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sequence
        if request.url.path == "/fleet/v1/snapshot":
            sequence += 1
            return httpx.Response(200, json=_snapshot(sequence))
        return httpx.Response(200, json={"object": "list", "data": [model_row]})

    def client_factory(locator, **_kwargs):
        return httpx.AsyncClient(
            base_url=locator.origin,
            transport=httpx.MockTransport(handler),
        )

    with pytest.raises(PairingCoordinatorError) as rejected:
        await probe_activation_candidate(
            _candidate(),
            timeout_seconds=5,
            client_factory=client_factory,
        )
    assert rejected.value.code == "pairing_activation_models_invalid"


async def test_activation_probe_rejects_residency_change() -> None:
    sequence = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sequence
        if request.url.path == "/fleet/v1/snapshot":
            sequence += 1
            return httpx.Response(200, json=_snapshot(sequence, warm=sequence > 1))
        return httpx.Response(200, json={"object": "list", "data": []})

    def client_factory(locator, **_kwargs):
        return httpx.AsyncClient(
            base_url=locator.origin,
            transport=httpx.MockTransport(handler),
        )

    with pytest.raises(PairingCoordinatorError) as changed:
        await probe_activation_candidate(
            _candidate(),
            timeout_seconds=5,
            client_factory=client_factory,
        )
    assert changed.value.code == "pairing_activation_probe_changed_state"

