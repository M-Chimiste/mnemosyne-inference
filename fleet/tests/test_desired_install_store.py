from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

from mnemosyne_fleet.desired_install_protocol import validate_desired_install
from mnemosyne_fleet.desired_install_store import (
    DesiredInstallConflictError,
    DesiredInstallIntegrityError,
    DesiredInstallStore,
)


PROTOCOL_V1 = Path(__file__).resolve().parents[2] / "mac_pool_protocol" / "v1"


def _job(
    *,
    job_id: str | None = None,
    idempotency_key: str | None = None,
    created_at: float = 1000.0,
    valid_for_seconds: int = 900,
) -> dict[str, object]:
    value = json.loads(
        (PROTOCOL_V1 / "desired_install.example.json").read_text(
            encoding="utf-8"
        )
    )
    value["job_id"] = job_id or str(uuid.uuid4())
    value["idempotency_key"] = idempotency_key or str(uuid.uuid4())
    value["created_at"] = created_at
    value["valid_for_seconds"] = valid_for_seconds
    value["expires_at"] = created_at + valid_for_seconds
    return value


def _intent(value: str = "one") -> tuple[dict[str, object], str]:
    intent: dict[str, object] = {"schema_version": 1, "intent": value}
    canonical = json.dumps(
        intent, sort_keys=True, separators=(",", ":")
    ).encode()
    return intent, "sha256:" + hashlib.sha256(canonical).hexdigest()


def _ack(
    job: dict[str, object],
    *,
    revision: int = 1,
    state: str = "received",
    updated_at: float = 1001.0,
    downloaded: int = 0,
    installation_id: str | None = None,
) -> dict[str, object]:
    result_code = None
    if state == "cancelled":
        result_code = "cancelled_by_hub"
    value: dict[str, object] = {
        "schema_version": 1,
        "job_id": job["job_id"],
        "job_revision": revision,
        "state": state,
        "bytes_downloaded": downloaded,
        "total_bytes": 1000,
        "updated_at": updated_at,
        "result_code": result_code,
    }
    if installation_id is not None:
        value["installation_id"] = installation_id
    return value


async def test_journal_restarts_replays_and_conflicts_without_losing_delivery(
    tmp_path,
) -> None:
    now = [1000.0]
    database = tmp_path / "private" / "desired.db"
    intent, digest = _intent()
    value = _job()
    document = validate_desired_install(value)
    first = DesiredInstallStore(database, wall_clock=lambda: now[0])
    await first.initialize()
    created = await first.create(document, intent_digest=digest, intent=intent)
    assert created.replayed is False
    replay = await first.create(document, intent_digest=digest, intent=intent)
    assert replay.replayed is True
    await first.mark_delivered(job_id=document.job_id, job_revision=1)

    conflicting_intent, conflicting_digest = _intent("different")
    with pytest.raises(
        DesiredInstallConflictError,
        match="desired_install_idempotency_conflict",
    ):
        await first.create(
            document,
            intent_digest=conflicting_digest,
            intent=conflicting_intent,
        )

    restarted = DesiredInstallStore(database, wall_clock=lambda: now[0])
    await restarted.initialize()
    recovered = await restarted.find_idempotent(
        idempotency_key=document.idempotency_key,
        intent_digest=digest,
    )
    assert recovered is not None
    assert recovered.delivery_count == 1
    pending = await restarted.pending_for_delivery(
        pairing_id=document.pairing_id,
        credential_generation=document.credential_generation,
        inventory_instance_id=document.inventory_instance_id,
        inventory_sequence=document.inventory_sequence + 1,
    )
    assert [row.document.job_id for row in pending] == [document.job_id]
    assert stat_mode(database) == 0o600
    assert stat_mode(database.parent) == 0o700


async def test_cancel_revision_and_ack_fences_are_idempotent(tmp_path) -> None:
    now = [1000.0]
    intent, digest = _intent()
    value = _job()
    document = validate_desired_install(value)
    store = DesiredInstallStore(
        tmp_path / "private" / "desired.db",
        wall_clock=lambda: now[0],
    )
    await store.initialize()
    await store.create(document, intent_digest=digest, intent=intent)
    installation_id = str(uuid.uuid4())
    accepted = await store.accept_acknowledgements(
        pairing_id=document.pairing_id,
        credential_generation=document.credential_generation,
        acknowledgements=[
            _ack(value, installation_id=installation_id),
        ],
    )
    assert accepted[0].replayed is False
    assert not await store.pending_for_delivery(
        pairing_id=document.pairing_id,
        credential_generation=document.credential_generation,
        inventory_instance_id=document.inventory_instance_id,
        inventory_sequence=document.inventory_sequence,
    )

    now[0] = 1010.0
    with pytest.raises(
        DesiredInstallConflictError,
        match="desired_install_revision_conflict",
    ):
        await store.cancel(
            document.job_id,
            expected_revision=2,
            issued_at=now[0],
            valid_for_seconds=60,
        )
    cancelled = await store.cancel(
        document.job_id,
        expected_revision=1,
        issued_at=now[0],
        valid_for_seconds=60,
    )
    assert cancelled.document.job_revision == 2
    assert cancelled.document.desired_state == "cancel"
    assert (
        await store.cancel(
            document.job_id,
            expected_revision=2,
            issued_at=now[0],
            valid_for_seconds=60,
        )
    ).document.job_revision == 2
    assert (
        await store.cancel(
            document.job_id,
            expected_revision=1,
            issued_at=now[0],
            valid_for_seconds=60,
        )
    ).document.job_revision == 2

    stale = await store.accept_acknowledgements(
        pairing_id=document.pairing_id,
        credential_generation=document.credential_generation,
        acknowledgements=[
            _ack(
                value,
                state="downloading",
                updated_at=1011.0,
                downloaded=50,
                installation_id=installation_id,
            )
        ],
    )
    assert stale[0].stale_revision is True
    current = await store.get(document.job_id)
    assert current is not None
    assert current.acknowledgement is None

    cancel_ack = _ack(
        value,
        revision=2,
        state="cancelled",
        updated_at=1012.0,
        downloaded=50,
        installation_id=installation_id,
    )
    terminal = await store.accept_acknowledgements(
        pairing_id=document.pairing_id,
        credential_generation=document.credential_generation,
        acknowledgements=[cancel_ack],
    )
    assert terminal[0].record is not None
    assert terminal[0].record.terminal is True
    replay = await store.accept_acknowledgements(
        pairing_id=document.pairing_id,
        credential_generation=document.credential_generation,
        acknowledgements=[copy.deepcopy(cancel_ack)],
    )
    assert replay[0].replayed is True


async def test_ack_progress_binds_installation_and_pruned_terminal_ack_is_safe(
    tmp_path,
) -> None:
    now = [1000.0]
    store = DesiredInstallStore(
        tmp_path / "private" / "desired.db",
        history_limit=1,
        wall_clock=lambda: now[0],
    )
    await store.initialize()
    intent, digest = _intent()
    first_value = _job(created_at=900, valid_for_seconds=50)
    first = validate_desired_install(first_value)
    await store.create(first, intent_digest=digest, intent=intent)

    unknown_terminal = _ack(
        {"job_id": str(uuid.uuid4())},
        state="cancelled",
    )
    retired = await store.accept_acknowledgements(
        pairing_id=first.pairing_id,
        credential_generation=first.credential_generation,
        acknowledgements=[unknown_terminal],
    )
    assert retired[0].retired_unknown is True
    with pytest.raises(
        DesiredInstallConflictError,
        match="desired_install_ack_unknown",
    ):
        await store.accept_acknowledgements(
            pairing_id=first.pairing_id,
            credential_generation=first.credential_generation,
            acknowledgements=[_ack({"job_id": str(uuid.uuid4())})],
        )

    second_intent, second_digest = _intent("second")
    second_value = _job(created_at=1000)
    second = validate_desired_install(second_value)
    await store.create(second, intent_digest=second_digest, intent=second_intent)
    installation_id = str(uuid.uuid4())
    await store.accept_acknowledgements(
        pairing_id=second.pairing_id,
        credential_generation=second.credential_generation,
        acknowledgements=[
            _ack(second_value, installation_id=installation_id),
        ],
    )
    changed = _ack(
        second_value,
        state="downloading",
        updated_at=1002.0,
        downloaded=1,
        installation_id=str(uuid.uuid4()),
    )
    with pytest.raises(
        DesiredInstallConflictError,
        match="desired_install_ack_conflict",
    ):
        await store.accept_acknowledgements(
            pairing_id=second.pairing_id,
            credential_generation=second.credential_generation,
            acknowledgements=[changed],
        )

    # Creating another record prunes expired history to the configured bound;
    # a later terminal acknowledgement for the retired job is still harmless.
    now[0] = 2000.0
    third_intent, third_digest = _intent("third")
    third = validate_desired_install(_job(created_at=2000))
    await store.create(third, intent_digest=third_digest, intent=third_intent)
    records, total = await store.list(limit=100)
    assert total == 2
    assert first.job_id not in {row.document.job_id for row in records}
    retired_after_prune = await store.accept_acknowledgements(
        pairing_id=first.pairing_id,
        credential_generation=first.credential_generation,
        acknowledgements=[
            _ack(first_value, state="cancelled", updated_at=1003.0)
        ],
    )
    assert retired_after_prune[0].retired_unknown is True


async def test_active_limit_never_prunes_or_overwrites_live_authority(tmp_path) -> None:
    store = DesiredInstallStore(
        tmp_path / "private" / "desired.db",
        maximum_active_jobs=1,
        wall_clock=lambda: 1000.0,
    )
    await store.initialize()
    first_intent, first_digest = _intent("first")
    first = validate_desired_install(_job())
    await store.create(first, intent_digest=first_digest, intent=first_intent)
    second_intent, second_digest = _intent("second")
    second = validate_desired_install(_job())
    with pytest.raises(
        DesiredInstallConflictError,
        match="desired_install_active_limit_reached",
    ):
        await store.create(
            second,
            intent_digest=second_digest,
            intent=second_intent,
        )
    recovered = await store.get(first.job_id)
    assert recovered is not None
    assert recovered.document.value == first.value


async def test_journal_rejects_a_shared_or_non_private_parent(tmp_path) -> None:
    parent = tmp_path / "not-private"
    parent.mkdir(mode=0o755)
    store = DesiredInstallStore(parent / "desired.db")
    with pytest.raises(
        DesiredInstallIntegrityError,
        match="desired_install_store_insecure_path",
    ):
        await store.initialize()
    assert stat_mode(parent) == 0o755


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
