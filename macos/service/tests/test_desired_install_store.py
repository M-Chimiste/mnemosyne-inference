from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import sqlite3

import pytest

from mnemosyne_macos.desired_install_protocol import (
    DesiredInstallProtocolError,
)
from mnemosyne_macos.desired_install_store import (
    DesiredInstallConflictError,
    DesiredInstallIntegrityError,
    DesiredInstallStore,
    DesiredInstallStoreError,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_PATH = (
    REPOSITORY_ROOT / "mac_pool_protocol" / "v1" / "desired_install.example.json"
)


class Clock:
    def __init__(self) -> None:
        self.wall = 1_785_528_051.0
        self.monotonic = 100.0
        self.boot = "boot-a"


def _job(index: int = 1) -> dict:
    value = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    value["job_id"] = f"{index:08x}-0000-4000-8000-{index:012x}"
    key_index = index + 100_000
    value["idempotency_key"] = (
        f"{key_index:08x}-0000-4000-8000-{key_index:012x}"
    )
    return value


def _cancel(run: dict, *, created_at: float | None = None) -> dict:
    value = copy.deepcopy(run)
    created = run["created_at"] + 100 if created_at is None else created_at
    value.update(
        {
            "job_revision": run["job_revision"] + 1,
            "desired_state": "cancel",
            "created_at": created,
            "expires_at": created + 300,
            "valid_for_seconds": 300,
        }
    )
    return value


def _store(tmp_path: Path, clock: Clock, **limits) -> DesiredInstallStore:
    return DesiredInstallStore(
        tmp_path / "state" / "desired-jobs.sqlite3",
        wall_clock=lambda: clock.wall,
        monotonic_clock=lambda: clock.monotonic,
        boot_identity=lambda: clock.boot,
        **limits,
    )


@pytest.mark.asyncio
async def test_first_receipt_waits_for_local_approval_and_replay_survives_restart(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()
    first = await store.receive(_job())
    assert first.replayed is False
    assert first.cancelled is False
    assert first.record.state == "awaiting_local_approval"
    assert first.record.result_code == "local_approval_required"
    assert first.record.bytes_downloaded == 0
    assert first.record.total_bytes is None
    # One second elapsed between Hub creation and receipt shortens the TTL.
    assert first.record.current_valid_until_monotonic == 999.0
    database = store.path
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.parent.stat().st_mode & 0o777 == 0o700
    await store.close()

    clock.monotonic = 150.0
    restarted = _store(tmp_path, clock)
    await restarted.initialize()
    restored = await restarted.get(first.record.document.job_id)
    assert restored is not None
    assert restored.acknowledgement() == first.record.acknowledgement()
    replay = await restarted.receive(_job())
    assert replay.replayed is True
    assert replay.record.current_received_monotonic == 100.0
    assert replay.record.current_valid_until_monotonic == 999.0


@pytest.mark.asyncio
async def test_delayed_and_future_dated_receipts_cannot_gain_fresh_authority(
    tmp_path: Path,
) -> None:
    delayed_clock = Clock()
    delayed_clock.wall = _job()["created_at"] + 100
    delayed_clock.monotonic = 500
    delayed = _store(tmp_path / "delayed", delayed_clock)
    await delayed.initialize()
    receipt = await delayed.receive(_job())
    assert receipt.record.current_valid_until_monotonic == 1_300

    future_clock = Clock()
    future_clock.wall = _job()["created_at"] - 10_000
    future_clock.monotonic = 700
    future = _store(tmp_path / "future", future_clock)
    await future.initialize()
    future_receipt = await future.receive(_job())
    assert future_receipt.record.current_valid_until_monotonic == 1_600


@pytest.mark.asyncio
async def test_already_expired_run_and_cancel_documents_cause_no_action(
    tmp_path: Path,
) -> None:
    clock = Clock()
    clock.wall = _job()["expires_at"]
    store = _store(tmp_path / "run", clock)
    await store.initialize()
    with pytest.raises(DesiredInstallConflictError) as expired:
        await store.receive(_job())
    assert expired.value.code == "desired_install_expired"
    assert (await store.list())[1] == 0

    clock = Clock()
    cancel_store = _store(tmp_path / "cancel", clock)
    await cancel_store.initialize()
    await cancel_store.receive(_job())
    expired_cancel = _cancel(_job(), created_at=clock.wall - 301)
    with pytest.raises(DesiredInstallConflictError) as cancel_error:
        await cancel_store.receive(expired_cancel)
    assert cancel_error.value.code == "desired_install_expired"
    current = await cancel_store.get(_job()["job_id"])
    assert current is not None
    assert current.document.job_revision == 1
    assert current.state == "awaiting_local_approval"


@pytest.mark.asyncio
async def test_delayed_cancel_revision_uses_only_its_remaining_lifetime(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()
    run = _job()
    await store.receive(run)
    delayed_cancel = _cancel(run, created_at=clock.wall - 290)
    cancelled = await store.receive(delayed_cancel)
    assert cancelled.record.current_valid_until_monotonic == 110.0
    assert cancelled.record.state == "cancelled"


@pytest.mark.asyncio
async def test_monotonic_expiry_and_boot_change_fail_closed_after_restart(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path / "ttl", clock)
    await store.initialize()
    receipt = await store.receive(_job())
    clock.monotonic = receipt.record.current_valid_until_monotonic
    expired = await store.get(_job()["job_id"])
    assert expired is not None
    assert expired.state == "refused"
    assert expired.result_code == "inventory_basis_stale"
    assert expired.terminal is True

    other_clock = Clock()
    boot_store = _store(tmp_path / "boot", other_clock)
    await boot_store.initialize()
    await boot_store.receive(_job(2))
    await boot_store.close()
    other_clock.boot = "boot-b"
    restarted = _store(tmp_path / "boot", other_clock)
    await restarted.initialize()
    fenced = await restarted.get(_job(2)["job_id"])
    assert fenced is not None
    assert fenced.state == "refused"
    assert fenced.result_code == "inventory_basis_stale"


@pytest.mark.asyncio
async def test_authenticated_recipient_and_n_to_n_plus_one_inventory_basis(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()
    job = _job()

    with pytest.raises(DesiredInstallConflictError) as future_basis:
        await store.receive(
            job,
            expected_inventory_instance_id=(
                job["recommendation_basis"]["inventory_instance_id"]
            ),
            current_inventory_sequence=41,
        )
    assert future_basis.value.code == "desired_install_inventory_basis_mismatch"
    assert (await store.list())[1] == 0

    receipt = await store.receive(
        job,
        expected_pairing_id=job["pairing_id"],
        expected_credential_generation=job["credential_generation"],
        expected_inventory_instance_id=(
            job["recommendation_basis"]["inventory_instance_id"]
        ),
        current_inventory_sequence=43,
    )
    assert receipt.record.document.inventory_sequence == 42

    changed_instance = _job(2)
    with pytest.raises(DesiredInstallConflictError) as instance_error:
        await store.receive(
            changed_instance,
            expected_inventory_instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            current_inventory_sequence=43,
        )
    assert instance_error.value.code == "desired_install_inventory_basis_mismatch"
    with pytest.raises(DesiredInstallConflictError) as pairing_error:
        await store.receive(
            changed_instance,
            expected_pairing_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
    assert pairing_error.value.code == "desired_install_recipient_mismatch"
    assert (await store.list())[1] == 1


@pytest.mark.asyncio
async def test_malformed_unsupported_and_private_payloads_leave_no_row_or_echo(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()
    canary = "/Volumes/Private/models/secret-token"
    bad = _job()
    bad["destination"] = canary
    with pytest.raises(DesiredInstallProtocolError) as malformed:
        await store.receive(bad)
    assert malformed.value.code == "desired_install_invalid"
    assert canary not in str(malformed.value)

    unsupported = _job()
    unsupported["schema_version"] = 2
    with pytest.raises(DesiredInstallProtocolError) as version:
        await store.receive(unsupported)
    assert version.value.code == "desired_install_unsupported_version"
    assert (await store.list())[1] == 0
    assert canary.encode() not in store.path.read_bytes()


@pytest.mark.asyncio
async def test_revision_idempotency_and_immutable_identity_fail_closed(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()

    revision_two = _job()
    revision_two["job_revision"] = 2
    with pytest.raises(DesiredInstallConflictError) as first_revision:
        await store.receive(revision_two)
    assert first_revision.value.code == "desired_install_revision_conflict"

    first_cancel = _cancel(_job())
    with pytest.raises(DesiredInstallConflictError) as unknown_cancel:
        await store.receive(first_cancel)
    assert unknown_cancel.value.code == "desired_install_cancel_unknown"

    run = _job()
    await store.receive(run)
    assert (await store.receive(run)).replayed is True

    same_revision_changed = copy.deepcopy(run)
    same_revision_changed["created_at"] += 1
    same_revision_changed["expires_at"] += 1
    with pytest.raises(DesiredInstallConflictError) as revision_conflict:
        await store.receive(same_revision_changed)
    assert revision_conflict.value.code == "desired_install_revision_conflict"

    higher_run = copy.deepcopy(run)
    higher_run["job_revision"] = 2
    with pytest.raises(DesiredInstallConflictError) as run_conflict:
        await store.receive(higher_run)
    assert run_conflict.value.code == "desired_install_revision_conflict"

    changed_identity = _cancel(run)
    changed_identity["storage_binding_generation"] += 1
    with pytest.raises(DesiredInstallConflictError) as identity_conflict:
        await store.receive(changed_identity)
    assert identity_conflict.value.code == "desired_install_idempotency_conflict"

    reused_key = _job(2)
    reused_key["idempotency_key"] = run["idempotency_key"]
    with pytest.raises(DesiredInstallConflictError) as key_conflict:
        await store.receive(reused_key)
    assert key_conflict.value.code == "desired_install_idempotency_conflict"


@pytest.mark.asyncio
async def test_cancel_revision_preserves_progress_and_cannot_cancel_terminal_work(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()
    run = _job()
    await store.receive(run)
    installation_id = "99999999-9999-4999-8999-999999999999"
    await store.transition(
        job_id=run["job_id"],
        job_revision=1,
        installation_id=installation_id,
        state="accepted",
        bytes_downloaded=0,
        total_bytes=100,
        result_code=None,
    )
    await store.transition(
        job_id=run["job_id"],
        job_revision=1,
        installation_id=installation_id,
        state="downloading",
        bytes_downloaded=25,
        total_bytes=100,
        result_code=None,
    )
    clock.wall += 10
    clock.monotonic += 10
    cancel = _cancel(run, created_at=clock.wall)
    cancelled = await store.receive(cancel)
    assert cancelled.cancelled is True
    assert cancelled.record.state == "cancelled"
    assert cancelled.record.result_code == "cancelled_by_hub"
    assert cancelled.record.installation_id == installation_id
    assert cancelled.record.bytes_downloaded == 25
    assert cancelled.record.total_bytes == 100
    assert (await store.receive(cancel)).replayed is True

    stale = run
    with pytest.raises(DesiredInstallConflictError) as stale_error:
        await store.receive(stale)
    assert stale_error.value.code == "desired_install_revision_stale"

    terminal_run = _job(2)
    await store.receive(terminal_run)
    await store.transition(
        job_id=terminal_run["job_id"],
        job_revision=1,
        state="refused",
        bytes_downloaded=0,
        total_bytes=None,
        result_code="local_policy_refused",
    )
    with pytest.raises(DesiredInstallConflictError) as terminal:
        await store.receive(_cancel(terminal_run, created_at=clock.wall))
    assert terminal.value.code == "desired_install_job_terminal"


@pytest.mark.asyncio
async def test_reconciliation_page_prioritizes_active_and_excludes_history(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()

    active = _job(1)
    refused = _job(2)
    cancelled_run = _job(3)
    await store.receive(active)
    await store.receive(refused)
    await store.transition(
        job_id=refused["job_id"],
        job_revision=1,
        state="refused",
        bytes_downloaded=0,
        total_bytes=None,
        result_code="local_policy_refused",
    )
    await store.receive(cancelled_run)
    installation_id = "99999999-9999-4999-8999-999999999999"
    await store.transition(
        job_id=cancelled_run["job_id"],
        job_revision=1,
        installation_id=installation_id,
        state="accepted",
        bytes_downloaded=0,
        total_bytes=100,
        result_code=None,
    )
    await store.receive(_cancel(cancelled_run, created_at=clock.wall))

    first, total = await store.list_reconcilable(offset=0, limit=1)
    second, repeated_total = await store.list_reconcilable(offset=1, limit=1)

    assert total == repeated_total == 2
    assert [item.document.job_id for item in first] == [active["job_id"]]
    assert [item.document.job_id for item in second] == [
        cancelled_run["job_id"]
    ]
    assert refused["job_id"] not in {
        item.document.job_id for item in first + second
    }

    visible, visible_total = await store.list(offset=0, limit=1)
    assert visible_total == 3
    assert [item.document.job_id for item in visible] == [active["job_id"]]


@pytest.mark.asyncio
async def test_closed_state_machine_progress_and_terminal_replay(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()
    run = _job()
    await store.receive(run)

    with pytest.raises(DesiredInstallConflictError) as skipped:
        await store.transition(
            job_id=run["job_id"],
            job_revision=1,
            state="downloading",
            bytes_downloaded=0,
            total_bytes=10,
            result_code=None,
        )
    assert skipped.value.code == "desired_install_state_conflict"

    states = (
        "accepted",
        "downloading",
        "verifying",
        "downloaded_unregistered",
        "registered",
        "completed",
    )
    last = None
    for index, state in enumerate(states):
        clock.wall += 1
        clock.monotonic += 1
        last = await store.transition(
            job_id=run["job_id"],
            job_revision=1,
            state=state,
            bytes_downloaded=min(index * 2, 10),
            total_bytes=10,
            result_code=None,
        )
        assert last.replayed is False
    assert last is not None
    assert last.record.terminal is True
    replay = await store.transition(
        job_id=run["job_id"],
        job_revision=1,
        state="completed",
        bytes_downloaded=10,
        total_bytes=10,
        result_code=None,
    )
    assert replay.replayed is True

    with pytest.raises(DesiredInstallConflictError) as terminal_progress:
        await store.transition(
            job_id=run["job_id"],
            job_revision=1,
            state="completed",
            bytes_downloaded=11,
            total_bytes=11,
            result_code=None,
        )
    assert terminal_progress.value.code == "desired_install_job_terminal"


@pytest.mark.asyncio
async def test_terminal_history_survives_restart_and_oldest_retires_only_at_capacity(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock, maximum_jobs=2, maximum_active_jobs=2)
    await store.initialize()
    for index in (1, 2):
        run = _job(index)
        await store.receive(run)
        await store.transition(
            job_id=run["job_id"],
            job_revision=1,
            state="refused",
            bytes_downloaded=0,
            total_bytes=None,
            result_code="local_policy_refused",
        )
        clock.wall += 1
        clock.monotonic += 1
    await store.close()

    restarted = _store(tmp_path, clock, maximum_jobs=2, maximum_active_jobs=2)
    await restarted.initialize()
    assert (await restarted.list())[1] == 2
    await restarted.receive(_job(3))
    assert (await restarted.list())[1] == 2
    assert await restarted.get(_job(1)["job_id"]) is None
    assert await restarted.get(_job(2)["job_id"]) is not None
    assert await restarted.get(_job(3)["job_id"]) is not None
    with pytest.raises(DesiredInstallConflictError) as retired_replay:
        await restarted.receive(_job(1))
    assert retired_replay.value.code == "desired_install_history_retired"
    assert (await restarted.list())[1] == 2


@pytest.mark.asyncio
async def test_active_capacity_never_prunes_unfinished_jobs(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock, maximum_jobs=2, maximum_active_jobs=2)
    await store.initialize()
    await store.receive(_job(1))
    await store.receive(_job(2))
    with pytest.raises(DesiredInstallConflictError) as full:
        await store.receive(_job(3))
    assert full.value.code == "desired_install_store_full"
    assert (await store.list())[1] == 2


@pytest.mark.asyncio
async def test_acknowledgement_cursor_rotates_more_than_inventory_array_limit(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock, maximum_jobs=300, maximum_active_jobs=256)
    await store.initialize()
    for index in range(1, 258):
        run = _job(index)
        await store.receive(run)
        await store.transition(
            job_id=run["job_id"],
            job_revision=1,
            state="refused",
            bytes_downloaded=0,
            total_bytes=None,
            result_code="local_policy_refused",
        )

    first = await store.acknowledgement_page(limit=256)
    assert first.total == 257
    assert len(first.acknowledgements) == 256
    assert first.next_cursor is not None
    second = await store.acknowledgement_page(
        after_job_id=first.next_cursor,
        limit=256,
    )
    assert len(second.acknowledgements) == 1
    assert second.next_cursor is None
    ids = [item.job_id for item in first.acknowledgements + second.acknowledgements]
    assert ids == sorted(ids)
    assert len(set(ids)) == 257


@pytest.mark.asyncio
async def test_user_version_zero_migrates_without_losing_history(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()
    await store.receive(_job())
    path = store.path
    await store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=0")

    restarted = _store(tmp_path, clock)
    await restarted.initialize()
    assert await restarted.get(_job()["job_id"]) is not None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_identity_and_payload_corruption_fail_closed_on_restart(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()
    await store.receive(_job())
    path = store.path
    await store.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE native_desired_install_jobs_v1 "
            "SET document_digest = ?",
            ("sha256:" + "0" * 64,),
        )

    corrupted = _store(tmp_path, clock)
    with pytest.raises(DesiredInstallIntegrityError) as failure:
        await corrupted.initialize()
    assert failure.value.code == "desired_install_store_corrupt"


@pytest.mark.asyncio
async def test_unknown_database_and_metadata_version_are_not_adopted(
    tmp_path: Path,
) -> None:
    unknown_path = tmp_path / "unknown" / "jobs.sqlite3"
    unknown_path.parent.mkdir()
    with sqlite3.connect(unknown_path) as connection:
        connection.execute("CREATE TABLE unrelated(secret TEXT)")
    unknown = DesiredInstallStore(unknown_path, boot_identity=lambda: "boot")
    with pytest.raises(DesiredInstallIntegrityError) as identity:
        await unknown.initialize()
    assert identity.value.code == "desired_install_store_identity_mismatch"

    clock = Clock()
    store = _store(tmp_path / "version", clock)
    await store.initialize()
    version_path = store.path
    await store.close()
    with sqlite3.connect(version_path) as connection:
        connection.execute(
            "UPDATE native_desired_install_metadata_v1 SET schema_version=2"
        )
    incompatible = _store(tmp_path / "version", clock)
    with pytest.raises(DesiredInstallIntegrityError) as version:
        await incompatible.initialize()
    assert version.value.code == "desired_install_store_identity_mismatch"


def test_store_module_has_no_installer_network_or_runtime_imports() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "mnemosyne_macos"
        / "desired_install_store.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(
        name.endswith(
            (
                "installer",
                "runtime",
                "config",
                "storage",
                "httpx",
            )
        )
        for name in imported
    )


def test_store_constructor_bounds_are_closed(tmp_path: Path) -> None:
    for maximum_jobs, maximum_active in ((0, 1), (10_001, 1), (1, 2)):
        with pytest.raises(ValueError):
            DesiredInstallStore(
                tmp_path / f"{maximum_jobs}-{maximum_active}.db",
                maximum_jobs=maximum_jobs,
                maximum_active_jobs=maximum_active,
            )


@pytest.mark.asyncio
async def test_result_and_progress_bounds_fail_before_mutation(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    await store.initialize()
    run = _job()
    await store.receive(run)
    with pytest.raises(DesiredInstallStoreError) as result:
        await store.transition(
            job_id=run["job_id"],
            job_revision=1,
            state="accepted",
            bytes_downloaded=0,
            total_bytes=None,
            result_code="arbitrary_remote_diagnostic",
        )
    assert result.value.code == "desired_install_result_invalid"
    with pytest.raises(DesiredInstallStoreError) as progress:
        await store.transition(
            job_id=run["job_id"],
            job_revision=1,
            state="accepted",
            bytes_downloaded=2,
            total_bytes=1,
            result_code=None,
        )
    assert progress.value.code == "desired_install_progress_invalid"
    current = await store.get(run["job_id"])
    assert current is not None
    assert current.state == "awaiting_local_approval"
