from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest

from mnemosyne_fleet.inventory_protocol import validate_inventory
from mnemosyne_fleet.inventory_store import (
    InventoryStore,
    InventoryStoreConflictError,
)


PROTOCOL_V1 = Path(__file__).resolve().parents[2] / "mac_pool_protocol" / "v1"


def _inventory(
    *,
    pairing_id: str,
    generation: int = 1,
    instance_id: str | None = None,
    sequence: int = 1,
) -> dict[str, object]:
    value = json.loads(
        (PROTOCOL_V1 / "mac_inventory.example.json").read_text(
            encoding="utf-8"
        )
    )
    value["pairing_id"] = pairing_id
    value["credential_generation"] = generation
    value["inventory_instance_id"] = instance_id or str(uuid.uuid4())
    value["inventory_sequence"] = sequence
    return value


async def test_inventory_store_fences_sequences_instances_and_generations(
    tmp_path,
) -> None:
    pairing_id = str(uuid.uuid4())
    first_instance = str(uuid.uuid4())
    second_instance = str(uuid.uuid4())
    store = InventoryStore(
        tmp_path / "inventory.db",
        process_instance_id=str(uuid.uuid4()),
    )
    await store.initialize()

    first = _inventory(
        pairing_id=pairing_id,
        instance_id=first_instance,
        sequence=3,
    )
    accepted = await store.accept(validate_inventory(first))
    assert accepted.replayed is False

    replay = await store.accept(validate_inventory(copy.deepcopy(first)))
    assert replay.replayed is True
    assert replay.record.inventory_sequence == 3

    conflicting = copy.deepcopy(first)
    conflicting["participation"]["state"] = "paused"  # type: ignore[index]
    with pytest.raises(
        InventoryStoreConflictError,
        match="inventory_sequence_conflict",
    ):
        await store.accept(validate_inventory(conflicting))

    late = copy.deepcopy(first)
    late["inventory_sequence"] = 2
    with pytest.raises(
        InventoryStoreConflictError,
        match="inventory_sequence_stale",
    ):
        await store.accept(validate_inventory(late))

    restarted_service = _inventory(
        pairing_id=pairing_id,
        instance_id=second_instance,
        sequence=0,
    )
    await store.accept(validate_inventory(restarted_service))

    old_service_late = copy.deepcopy(first)
    old_service_late["inventory_sequence"] = 4
    with pytest.raises(
        InventoryStoreConflictError,
        match="inventory_instance_retired",
    ):
        await store.accept(validate_inventory(old_service_late))

    rotated = _inventory(
        pairing_id=pairing_id,
        generation=2,
        instance_id=str(uuid.uuid4()),
        sequence=0,
    )
    await store.accept(validate_inventory(rotated))
    record = await store.record(pairing_id)
    assert record is not None
    assert record.credential_generation == 2


async def test_persisted_inventory_is_stale_until_increasing_post_restart_sync(
    tmp_path,
) -> None:
    pairing_id = str(uuid.uuid4())
    process_one = str(uuid.uuid4())
    process_two = str(uuid.uuid4())
    instance_id = str(uuid.uuid4())
    monotonic = [100.0]
    database = tmp_path / "inventory.db"
    payload = _inventory(
        pairing_id=pairing_id,
        instance_id=instance_id,
        sequence=8,
    )

    first = InventoryStore(
        database,
        process_instance_id=process_one,
        wall_clock=lambda: 1_000.0,
        monotonic_clock=lambda: monotonic[0],
    )
    await first.initialize()
    await first.accept(validate_inventory(payload))
    record = await first.record(pairing_id)
    assert record is not None
    assert first.freshness(
        record,
        enrollment_active=True,
        active_credential_generation=1,
    )["state"] == "fresh"

    restarted = InventoryStore(
        database,
        process_instance_id=process_two,
        wall_clock=lambda: 1_001.0,
        monotonic_clock=lambda: monotonic[0],
    )
    await restarted.initialize()
    persisted = await restarted.record(pairing_id)
    assert persisted is not None
    assert restarted.freshness(
        persisted,
        enrollment_active=True,
        active_credential_generation=1,
    ) == {
        "state": "stale",
        "reason": "hub_restarted",
        "receipt_age_seconds": None,
        "authoritative_for_placement": False,
        "authoritative_for_inference": False,
    }

    replay = await restarted.accept(validate_inventory(copy.deepcopy(payload)))
    assert replay.replayed is True
    still_stale = await restarted.record(pairing_id)
    assert still_stale is not None
    assert restarted.freshness(
        still_stale,
        enrollment_active=True,
        active_credential_generation=1,
    )["reason"] == "hub_restarted"

    payload["inventory_sequence"] = 9
    await restarted.accept(validate_inventory(payload))
    refreshed = await restarted.record(pairing_id)
    assert refreshed is not None
    assert restarted.freshness(
        refreshed,
        enrollment_active=True,
        active_credential_generation=1,
    )["state"] == "fresh"

    monotonic[0] = 161.0
    assert restarted.freshness(
        refreshed,
        enrollment_active=True,
        active_credential_generation=1,
    )["reason"] == "expired"


async def test_generation_and_revocation_are_explicit_freshness_fences(
    tmp_path,
) -> None:
    pairing_id = str(uuid.uuid4())
    store = InventoryStore(tmp_path / "inventory.db")
    await store.initialize()
    await store.accept(validate_inventory(_inventory(pairing_id=pairing_id)))
    record = await store.record(pairing_id)
    assert record is not None

    generation_changed = store.freshness(
        record,
        enrollment_active=True,
        active_credential_generation=2,
    )
    revoked = store.freshness(
        record,
        enrollment_active=False,
        active_credential_generation=1,
    )
    assert generation_changed["reason"] == "credential_generation_changed"
    assert revoked["reason"] == "enrollment_inactive"
    assert generation_changed["authoritative_for_inference"] is False
    assert revoked["authoritative_for_inference"] is False
