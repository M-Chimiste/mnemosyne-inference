from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
import pytest

from mnemosyne_macos.fleet_pairing import PairingCredentials, PairingState
from mnemosyne_macos.desired_install_store import DesiredInstallStore
from mnemosyne_macos.mac_inventory_sync import (
    InventorySyncError,
    InventorySyncErrorCode,
    MacInventorySyncClient,
)


PAIRING_ID = "11111111-1111-4111-8111-111111111111"
INSTANCE_ID = "22222222-2222-4222-8222-222222222222"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DESIRED_INSTALL_EXAMPLE = (
    REPOSITORY_ROOT
    / "mac_pool_protocol"
    / "v1"
    / "desired_install.example.json"
)


class FakeProducer:
    def __init__(self) -> None:
        self.instance_id = INSTANCE_ID
        self.sequence = 0
        self.last_document = None
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def next_document(
        self,
        *,
        pairing_id: str,
        credential_generation: int,
    ) -> dict[str, object]:
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        self.sequence += 1
        self.last_document = {
            "schema_version": 1,
            "inventory_instance_id": self.instance_id,
            "inventory_sequence": self.sequence,
            "pairing_id": pairing_id,
            "credential_generation": credential_generation,
            "service": {"supported_job_versions": []},
            "job_acknowledgements": [],
        }
        return dict(self.last_document)


class FakePairingStore:
    def __init__(self) -> None:
        self.record = SimpleNamespace(
            state=PairingState.PAIRED,
            pairing_id=PAIRING_ID,
            credential_epoch=3,
            hub_origin="https://nyx.example.test:8443",
        )
        self.credentials = PairingCredentials(
            snapshot_key="snapshot-role-secret",
            dispatch_key="dispatch-role-secret",
            management_key="management-role-secret",
        )

    async def status(self):
        return self.record

    async def staged_credentials(self) -> PairingCredentials:
        return self.credentials


def _ack(document: dict[str, object], *, desired_jobs=None) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json", "Content-Encoding": "identity"},
        json={
            "schema_version": 1,
            "ack": {
                "pairing_id": document["pairing_id"],
                "credential_generation": document["credential_generation"],
                "inventory_instance_id": document["inventory_instance_id"],
                "inventory_sequence": document["inventory_sequence"],
            },
            "desired_jobs": desired_jobs or [],
        },
    )


def _desired_job(
    document: dict[str, object],
    *,
    index: int = 1,
) -> dict[str, object]:
    value = json.loads(DESIRED_INSTALL_EXAMPLE.read_text(encoding="utf-8"))
    value["job_id"] = f"{index:08x}-0000-4000-8000-{index:012x}"
    key_index = 100_000 + index
    value["idempotency_key"] = (
        f"{key_index:08x}-0000-4000-8000-{key_index:012x}"
    )
    now = time.time()
    value["created_at"] = now
    value["expires_at"] = now + 900
    value["pairing_id"] = document["pairing_id"]
    value["credential_generation"] = document["credential_generation"]
    value["recommendation_basis"] = {
        "inventory_instance_id": document["inventory_instance_id"],
        "inventory_sequence": document["inventory_sequence"],
    }
    return value


@pytest.mark.asyncio
async def test_exact_failed_observation_retries_then_sequence_advances() -> None:
    attempts: list[dict[str, object]] = []
    authorization: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        attempts.append(document)
        authorization.append(request.headers["Authorization"])
        assert request.url == httpx.URL(
            f"https://nyx.example.test:8443/fleet/management/v1/pairings/"
            f"{PAIRING_ID}/inventory-sync"
        )
        assert request.headers["Content-Encoding"] == "identity"
        if len(attempts) == 1:
            raise httpx.ConnectError("secret-local-network-detail", request=request)
        return _ack(document)

    producer = FakeProducer()
    client = MacInventorySyncClient(
        producer,  # type: ignore[arg-type]
        FakePairingStore(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(InventorySyncError) as first:
        await client.sync_once()
    assert first.value.code == InventorySyncErrorCode.HUB_UNAVAILABLE
    pending = await client.inspection()
    assert pending["sync"]["pending_sequence"] == 1
    assert "secret-local-network-detail" not in json.dumps(pending)

    acknowledged = await client.sync_once()
    assert acknowledged.last_acknowledged_sequence == 1
    await client.sync_once()
    assert attempts[0] == attempts[1]
    assert attempts[2]["inventory_sequence"] == 2
    assert authorization == ["Bearer management-role-secret"] * 3
    await client.stop()


@pytest.mark.asyncio
async def test_inspection_does_not_wait_for_slow_collection_or_network() -> None:
    producer = FakeProducer()
    producer.started = asyncio.Event()
    producer.release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        return _ack(json.loads(request.content))

    client = MacInventorySyncClient(
        producer,  # type: ignore[arg-type]
        FakePairingStore(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    syncing = asyncio.create_task(client.sync_once())
    await producer.started.wait()
    inspection = await asyncio.wait_for(client.inspection(), timeout=0.2)
    assert inspection["sync"]["state"] == "syncing"
    assert inspection["inventory"] is None
    producer.release.set()
    await syncing
    await client.stop()


@pytest.mark.asyncio
async def test_generation_change_discards_old_pending_and_uses_new_management_role() -> None:
    store = FakePairingStore()
    attempts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(json.loads(request.content))
        if len(attempts) == 1:
            raise httpx.ConnectError("offline", request=request)
        assert request.headers["Authorization"] == "Bearer management-generation-four"
        return _ack(attempts[-1])

    client = MacInventorySyncClient(
        FakeProducer(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(InventorySyncError):
        await client.sync_once()
    store.record.credential_epoch = 4
    store.credentials = PairingCredentials(
        snapshot_key="snapshot-four",
        dispatch_key="dispatch-four",
        management_key="management-generation-four",
    )
    await client.sync_once()
    assert attempts[0]["credential_generation"] == 3
    assert attempts[1]["credential_generation"] == 4
    assert attempts[1]["inventory_sequence"] == 2
    await client.stop()


@pytest.mark.asyncio
async def test_invalid_tls_origin_and_nonempty_jobs_fail_closed_without_execution() -> None:
    store = FakePairingStore()
    store.record.hub_origin = "https://nyx.example.test:not-a-port"
    client = MacInventorySyncClient(
        FakeProducer(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    with pytest.raises(InventorySyncError) as invalid:
        await client.sync_once()
    assert invalid.value.code == InventorySyncErrorCode.HUB_ORIGIN_INVALID
    await client.stop()

    executions: list[object] = []
    producer = FakeProducer()

    async def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        # No executor callback exists on the initial client; this canary would
        # catch any accidental future side effect added to the response path.
        assert executions == []
        return _ack(
            document,
            desired_jobs=[{"schema_version": 1, "job_id": "untrusted"}],
        )

    client = MacInventorySyncClient(
        producer,  # type: ignore[arg-type]
        FakePairingStore(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(InventorySyncError) as jobs:
        await client.sync_once()
    assert jobs.value.code == InventorySyncErrorCode.HUB_RESPONSE_INVALID
    assert executions == []
    status = await client.inspection()
    assert status["sync"]["pending_sequence"] == 1
    await client.stop()


@pytest.mark.asyncio
async def test_valid_job_is_journaled_then_acknowledged_without_execution(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, object]] = []
    executions: list[object] = []
    desired_store = DesiredInstallStore(tmp_path / "desired-installs.sqlite3")
    await desired_store.initialize()

    async def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        requests.append(document)
        assert executions == []
        jobs = [_desired_job(document)] if len(requests) == 1 else []
        return _ack(document, desired_jobs=jobs)

    client = MacInventorySyncClient(
        FakeProducer(),  # type: ignore[arg-type]
        FakePairingStore(),  # type: ignore[arg-type]
        desired_install_store=desired_store,
        transport=httpx.MockTransport(handler),
    )
    try:
        first = await client.sync_once()
        assert first.last_acknowledged_sequence == 1
        record = await desired_store.get(_desired_job(requests[0])["job_id"])
        assert record is not None
        assert record.state == "awaiting_local_approval"
        assert record.result_code == "local_approval_required"
        assert record.installation_id is None
        assert requests[0]["service"]["supported_job_versions"] == []
        assert requests[0]["job_acknowledgements"] == []

        await client.sync_once()
        assert len(requests[1]["job_acknowledgements"]) == 1
        acknowledgement = requests[1]["job_acknowledgements"][0]
        assert acknowledgement == record.acknowledgement().value
        assert "path" not in json.dumps(acknowledgement).casefold()
        assert executions == []
    finally:
        await client.stop()
        await desired_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ("pairing", "generation", "instance", "future_sequence"),
)
async def test_job_recipient_and_acknowledged_basis_are_fenced_before_persistence(
    tmp_path: Path,
    mutation: str,
) -> None:
    desired_store = DesiredInstallStore(
        tmp_path / mutation / "desired-installs.sqlite3"
    )
    await desired_store.initialize()

    async def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        job = _desired_job(document)
        if mutation == "pairing":
            job["pairing_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        elif mutation == "generation":
            job["credential_generation"] = int(
                document["credential_generation"]
            ) + 1
        elif mutation == "instance":
            job["recommendation_basis"]["inventory_instance_id"] = (
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            )
        else:
            job["recommendation_basis"]["inventory_sequence"] = (
                int(document["inventory_sequence"]) + 1
            )
        return _ack(document, desired_jobs=[job])

    client = MacInventorySyncClient(
        FakeProducer(),  # type: ignore[arg-type]
        FakePairingStore(),  # type: ignore[arg-type]
        desired_install_store=desired_store,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(InventorySyncError) as rejected:
            await client.sync_once()
        assert rejected.value.code == InventorySyncErrorCode.DESIRED_JOBS_REJECTED
        assert (await desired_store.list())[1] == 0
        assert (await client.status()).last_acknowledged_sequence is None
    finally:
        await client.stop()
        await desired_store.close()


@pytest.mark.asyncio
async def test_pairing_rotation_during_response_rejects_old_generation_job(
    tmp_path: Path,
) -> None:
    desired_store = DesiredInstallStore(tmp_path / "desired-installs.sqlite3")
    await desired_store.initialize()
    pairing_store = FakePairingStore()

    async def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        job = _desired_job(document)
        pairing_store.record.credential_epoch = 4
        return _ack(document, desired_jobs=[job])

    client = MacInventorySyncClient(
        FakeProducer(),  # type: ignore[arg-type]
        pairing_store,  # type: ignore[arg-type]
        desired_install_store=desired_store,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(InventorySyncError) as rejected:
            await client.sync_once()
        assert rejected.value.code == InventorySyncErrorCode.DESIRED_JOBS_REJECTED
        assert (await desired_store.list())[1] == 0
        assert (await client.status()).last_acknowledged_sequence is None
    finally:
        await client.stop()
        await desired_store.close()


@pytest.mark.asyncio
async def test_durable_acknowledgement_is_replayed_after_client_restart(
    tmp_path: Path,
) -> None:
    desired_store = DesiredInstallStore(tmp_path / "desired-installs.sqlite3")
    await desired_store.initialize()
    seed_document = {
        "pairing_id": PAIRING_ID,
        "credential_generation": 3,
        "inventory_instance_id": INSTANCE_ID,
        "inventory_sequence": 1,
    }
    job = _desired_job(seed_document)
    await desired_store.receive(job)
    attempts: list[dict[str, object]] = []

    async def offline(request: httpx.Request) -> httpx.Response:
        attempts.append(json.loads(request.content))
        raise httpx.ConnectError("offline", request=request)

    first = MacInventorySyncClient(
        FakeProducer(),  # type: ignore[arg-type]
        FakePairingStore(),  # type: ignore[arg-type]
        desired_install_store=desired_store,
        transport=httpx.MockTransport(offline),
    )
    with pytest.raises(InventorySyncError):
        await first.sync_once()
    await first.stop()

    async def online(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        attempts.append(document)
        return _ack(document)

    restarted = MacInventorySyncClient(
        FakeProducer(),  # type: ignore[arg-type]
        FakePairingStore(),  # type: ignore[arg-type]
        desired_install_store=desired_store,
        transport=httpx.MockTransport(online),
    )
    try:
        await restarted.sync_once()
        expected = (await desired_store.get(str(job["job_id"]))).acknowledgement()
        assert attempts[0]["job_acknowledgements"] == [expected.value]
        assert attempts[1]["job_acknowledgements"] == [expected.value]
    finally:
        await restarted.stop()
        await desired_store.close()


@pytest.mark.asyncio
async def test_acknowledgements_from_an_old_generation_are_not_rebound(
    tmp_path: Path,
) -> None:
    desired_store = DesiredInstallStore(tmp_path / "desired-installs.sqlite3")
    await desired_store.initialize()
    seed_document = {
        "pairing_id": PAIRING_ID,
        "credential_generation": 2,
        "inventory_instance_id": INSTANCE_ID,
        "inventory_sequence": 1,
    }
    old_job = _desired_job(seed_document, index=1)
    await desired_store.receive(old_job)
    seed_document["credential_generation"] = 3
    current_job = _desired_job(seed_document, index=2)
    await desired_store.receive(current_job)
    attempts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        attempts.append(document)
        return _ack(document)

    client = MacInventorySyncClient(
        FakeProducer(),  # type: ignore[arg-type]
        FakePairingStore(),  # type: ignore[arg-type]
        desired_install_store=desired_store,
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.sync_once()
        assert [
            item["job_id"] for item in attempts[0]["job_acknowledgements"]
        ] == [current_job["job_id"]]
    finally:
        await client.stop()
        await desired_store.close()


@pytest.mark.asyncio
async def test_ack_page_cursor_advances_only_after_exact_request_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mnemosyne_macos.mac_inventory_sync.MAX_JOB_ACKNOWLEDGEMENTS",
        1,
    )
    desired_store = DesiredInstallStore(tmp_path / "desired-installs.sqlite3")
    await desired_store.initialize()
    seed_document = {
        "pairing_id": PAIRING_ID,
        "credential_generation": 3,
        "inventory_instance_id": INSTANCE_ID,
        "inventory_sequence": 1,
    }
    for index in (1, 2):
        job = _desired_job(seed_document, index=index)
        await desired_store.receive(job)
        await desired_store.transition(
            job_id=str(job["job_id"]),
            job_revision=1,
            state="refused",
            bytes_downloaded=0,
            total_bytes=None,
            result_code="local_policy_refused",
        )

    attempts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        attempts.append(copy.deepcopy(document))
        if len(attempts) == 1:
            raise httpx.ConnectError("offline", request=request)
        return _ack(document)

    client = MacInventorySyncClient(
        FakeProducer(),  # type: ignore[arg-type]
        FakePairingStore(),  # type: ignore[arg-type]
        desired_install_store=desired_store,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(InventorySyncError):
            await client.sync_once()
        await client.sync_once()
        await client.sync_once()
        await client.sync_once()
        assert attempts[0] == attempts[1]
        ack_ids = [
            attempt["job_acknowledgements"][0]["job_id"]
            for attempt in attempts[1:]
        ]
        assert ack_ids == [
            "00000001-0000-4000-8000-000000000001",
            "00000002-0000-4000-8000-000000000002",
            "00000001-0000-4000-8000-000000000001",
        ]
    finally:
        await client.stop()
        await desired_store.close()
