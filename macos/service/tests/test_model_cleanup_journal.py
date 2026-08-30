from __future__ import annotations

import ast
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from mnemosyne_macos.model_cleanup_journal import (
    CleanupConfigState,
    CleanupKind,
    CleanupPhase,
    CleanupRecoveryAction,
    DestinationState,
    InstallLedgerState,
    ModelCleanupJournal,
    ModelCleanupJournalCapacityError,
    ModelCleanupJournalConflictError,
    ModelCleanupJournalIntegrityError,
    decide_cleanup_recovery,
)


SOURCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "mnemosyne_macos"
    / "model_cleanup_journal.py"
)


class Clock:
    def __init__(self) -> None:
        self.value = 1_788_000_000.0

    def __call__(self) -> float:
        return self.value


def _uuid(index: int) -> str:
    return f"{index:08x}-0000-4000-8000-{index:012x}"


def _digest(index: int) -> str:
    return f"{index:064x}"


def _journal(
    tmp_path: Path,
    *,
    maximum_transactions: int = 1024,
    clock: Clock | None = None,
) -> ModelCleanupJournal:
    return ModelCleanupJournal(
        tmp_path / "private" / "config.yaml",
        maximum_transactions=maximum_transactions,
        clock=clock or Clock(),
    )


def _prepare(
    journal: ModelCleanupJournal,
    index: int = 1,
    *,
    kind: CleanupKind = CleanupKind.MANAGED,
):
    return journal.prepare(
        transaction_id=_uuid(index),
        installation_id=_uuid(index + 10_000)
        if kind is CleanupKind.MANAGED
        else None,
        alias_profile_fingerprint=_digest(index),
        original_config_revision=_digest(index + 1_000),
        result_config_revision=_digest(index + 2_000),
        cleanup_kind=kind,
    )


def _complete(journal: ModelCleanupJournal, transaction_id: str) -> None:
    for phase in (
        CleanupPhase.TRASH_CONFIRMED,
        CleanupPhase.LEDGER_MARKED,
        CleanupPhase.CONFIG_SAVED,
        CleanupPhase.COMPLETED,
    ):
        journal.advance(transaction_id, phase)


def test_private_config_adjacent_wal_database_is_created_with_strict_modes(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    assert journal.path == (
        tmp_path
        / "private"
        / "state"
        / "model-cleanup"
        / "journal.sqlite3"
    )
    assert stat.S_IMODE(journal.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        metadata = connection.execute(
            "SELECT store_id, schema_version FROM native_model_cleanup_metadata_v1"
        ).fetchone()
        assert metadata == (
            "mnemosyne-native-model-cleanup-journal-v1",
            1,
        )


def test_existing_private_path_permissions_are_narrowed(tmp_path: Path) -> None:
    database = (
        tmp_path
        / "private"
        / "state"
        / "model-cleanup"
        / "journal.sqlite3"
    )
    database.parent.mkdir(parents=True, mode=0o777)
    database.touch(mode=0o666)
    os.chmod(database.parent.parent, 0o777)
    os.chmod(database.parent, 0o777)
    os.chmod(database, 0o666)
    journal = _journal(tmp_path)
    journal.initialize()
    assert stat.S_IMODE(database.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


@pytest.mark.parametrize("target", ["state", "database"])
def test_symlinked_private_state_or_database_fails_closed(
    tmp_path: Path,
    target: str,
) -> None:
    private = tmp_path / "private"
    private.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if target == "state":
        (private / "state").symlink_to(outside, target_is_directory=True)
    else:
        directory = private / "state" / "model-cleanup"
        directory.mkdir(parents=True)
        (directory / "journal.sqlite3").symlink_to(outside / "journal.sqlite3")
    journal = _journal(tmp_path)
    with pytest.raises(ModelCleanupJournalIntegrityError) as error:
        journal.initialize()
    assert error.value.code == "model_cleanup_journal_insecure_path"


def test_prepare_persists_only_fixed_identifiers_and_fingerprints(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    mutation = _prepare(journal)
    record = mutation.transaction
    assert mutation.replayed is False
    assert record.transaction_id == _uuid(1)
    assert record.installation_id == _uuid(10_001)
    assert record.alias_profile_fingerprint == _digest(1)
    assert record.original_config_revision == _digest(1_001)
    assert record.result_config_revision == _digest(2_001)
    assert record.cleanup_kind is CleanupKind.MANAGED
    assert record.phase is CleanupPhase.PREPARED
    assert record.error_code is None
    assert record.needs_recovery is True
    with sqlite3.connect(journal.path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(native_model_cleanup_transactions_v1)"
            )
        }
        assert columns == {
            "transaction_id",
            "installation_id",
            "alias_profile_fingerprint",
            "original_config_revision",
            "result_config_revision",
            "cleanup_kind",
            "current_phase",
            "created_at",
            "updated_at",
            "error_code",
        }
        event = connection.execute(
            """
            SELECT sequence, phase, error_code
              FROM native_model_cleanup_events_v1
            """
        ).fetchone()
        assert event == (1, "prepared", None)


def test_imported_binding_has_no_installation_id_and_kind_binding_is_strict(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    imported = _prepare(journal, kind=CleanupKind.IMPORTED).transaction
    assert imported.installation_id is None
    assert imported.cleanup_kind is CleanupKind.IMPORTED
    with pytest.raises(ModelCleanupJournalConflictError) as managed:
        journal.prepare(
            transaction_id=_uuid(2),
            installation_id=None,
            alias_profile_fingerprint=_digest(2),
            original_config_revision=_digest(3),
            result_config_revision=_digest(4),
            cleanup_kind="managed",
        )
    assert managed.value.code == "model_cleanup_installation_binding_conflict"
    with pytest.raises(ModelCleanupJournalConflictError) as imported_with_id:
        journal.prepare(
            transaction_id=_uuid(3),
            installation_id=_uuid(30_000),
            alias_profile_fingerprint=_digest(3),
            original_config_revision=_digest(4),
            result_config_revision=_digest(5),
            cleanup_kind="imported",
        )
    assert (
        imported_with_id.value.code
        == "model_cleanup_installation_binding_conflict"
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("transaction_id", "not-a-uuid", "model_cleanup_identifier_invalid"),
        (
            "transaction_id",
            _uuid(10).upper(),
            "model_cleanup_identifier_invalid",
        ),
        ("installation_id", "not-a-uuid", "model_cleanup_identifier_invalid"),
        ("alias_profile_fingerprint", "A" * 64, "model_cleanup_fingerprint_invalid"),
        ("original_config_revision", "0" * 63, "model_cleanup_fingerprint_invalid"),
        ("result_config_revision", "g" * 64, "model_cleanup_fingerprint_invalid"),
    ],
)
def test_prepare_rejects_noncanonical_identifiers_and_digests(
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    values = {
        "transaction_id": _uuid(1),
        "installation_id": _uuid(10_001),
        "alias_profile_fingerprint": _digest(1),
        "original_config_revision": _digest(2),
        "result_config_revision": _digest(3),
        "cleanup_kind": "managed",
    }
    values[field] = value
    with pytest.raises(ModelCleanupJournalConflictError) as error:
        journal.prepare(**values)
    assert error.value.code == code


def test_equal_config_revisions_are_refused(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    with pytest.raises(ModelCleanupJournalConflictError) as error:
        journal.prepare(
            transaction_id=_uuid(1),
            installation_id=_uuid(2),
            alias_profile_fingerprint=_digest(1),
            original_config_revision=_digest(2),
            result_config_revision=_digest(2),
            cleanup_kind="managed",
        )
    assert error.value.code == "model_cleanup_config_revision_conflict"


def test_prepare_replay_survives_restart_and_conflicting_reuse_fails_closed(
    tmp_path: Path,
) -> None:
    clock = Clock()
    journal = _journal(tmp_path, clock=clock)
    journal.initialize()
    first = _prepare(journal).transaction
    journal.close()
    clock.value += 100
    restarted = _journal(tmp_path, clock=clock)
    restarted.initialize()
    replay = _prepare(restarted)
    assert replay.replayed is True
    assert replay.transaction == first
    with pytest.raises(ModelCleanupJournalConflictError) as error:
        restarted.prepare(
            transaction_id=_uuid(1),
            installation_id=_uuid(10_001),
            alias_profile_fingerprint=_digest(999),
            original_config_revision=_digest(1_001),
            result_config_revision=_digest(2_001),
            cleanup_kind="managed",
        )
    assert error.value.code == "model_cleanup_transaction_conflict"
    assert restarted.get(_uuid(1)) == first


def test_phases_append_only_in_exact_order_with_idempotent_replays(
    tmp_path: Path,
) -> None:
    clock = Clock()
    journal = _journal(tmp_path, clock=clock)
    journal.initialize()
    transaction_id = _prepare(journal).transaction.transaction_id
    with pytest.raises(ModelCleanupJournalConflictError) as skipped:
        journal.advance(transaction_id, CleanupPhase.LEDGER_MARKED)
    assert skipped.value.code == "model_cleanup_phase_out_of_order"
    clock.value += 1
    trash = journal.advance(transaction_id, "trash_confirmed")
    assert trash.replayed is False
    assert trash.transaction.phase is CleanupPhase.TRASH_CONFIRMED
    replay = journal.advance(transaction_id, "trash_confirmed")
    assert replay.replayed is True
    assert replay.transaction == trash.transaction
    for phase in (
        CleanupPhase.LEDGER_MARKED,
        CleanupPhase.CONFIG_SAVED,
        CleanupPhase.COMPLETED,
    ):
        clock.value += 1
        final = journal.advance(transaction_id, phase)
        assert final.transaction.phase is phase
    assert final.transaction.completed is True
    assert final.transaction.needs_recovery is False
    assert journal.advance(transaction_id, "config_saved").replayed is True
    with pytest.raises(ModelCleanupJournalConflictError) as terminal:
        journal.mark_manual_recovery(transaction_id, "journal_conflict")
    assert terminal.value.code == "model_cleanup_transaction_terminal"
    with sqlite3.connect(journal.path) as connection:
        phases = [
            row[0]
            for row in connection.execute(
                """
                SELECT phase FROM native_model_cleanup_events_v1
                 WHERE transaction_id = ? ORDER BY sequence
                """,
                (transaction_id,),
            )
        ]
    assert phases == [phase.value for phase in (
        CleanupPhase.PREPARED,
        CleanupPhase.TRASH_CONFIRMED,
        CleanupPhase.LEDGER_MARKED,
        CleanupPhase.CONFIG_SAVED,
        CleanupPhase.COMPLETED,
    )]


def test_pretrash_abort_is_durable_terminal_and_prunable(tmp_path: Path) -> None:
    journal = _journal(tmp_path, maximum_transactions=1)
    journal.initialize()
    prepared = _prepare(journal).transaction

    aborted = journal.abort_without_mutation(prepared.transaction_id)

    assert aborted.replayed is False
    assert aborted.transaction.phase is CleanupPhase.ABORTED
    assert aborted.transaction.completed is False
    assert aborted.transaction.needs_recovery is False
    assert journal.abort_without_mutation(prepared.transaction_id).replayed is True
    assert journal.list_incomplete() == ()
    with pytest.raises(ModelCleanupJournalConflictError) as advance:
        journal.advance(prepared.transaction_id, CleanupPhase.TRASH_CONFIRMED)
    assert advance.value.code == "model_cleanup_transaction_terminal"
    with pytest.raises(ModelCleanupJournalConflictError) as manual:
        journal.mark_manual_recovery(prepared.transaction_id, "journal_conflict")
    assert manual.value.code == "model_cleanup_transaction_terminal"

    journal.close()
    restarted = _journal(tmp_path, maximum_transactions=1)
    restarted.initialize()
    assert restarted.get(prepared.transaction_id).phase is CleanupPhase.ABORTED
    replacement = _prepare(restarted, index=2).transaction
    assert restarted.get(prepared.transaction_id) is None
    assert replacement.phase is CleanupPhase.PREPARED


def test_abort_is_refused_after_trash_may_have_succeeded(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = _prepare(journal).transaction
    journal.advance(prepared.transaction_id, CleanupPhase.TRASH_CONFIRMED)

    with pytest.raises(ModelCleanupJournalConflictError) as error:
        journal.abort_without_mutation(prepared.transaction_id)

    assert error.value.code == "model_cleanup_phase_out_of_order"
    assert journal.get(prepared.transaction_id).phase is CleanupPhase.TRASH_CONFIRMED


@pytest.mark.parametrize(
    "phase",
    [
        CleanupPhase.PREPARED,
        CleanupPhase.TRASH_CONFIRMED,
        CleanupPhase.LEDGER_MARKED,
        CleanupPhase.CONFIG_SAVED,
    ],
)
def test_manual_recovery_is_durable_terminal_from_every_incomplete_phase(
    tmp_path: Path,
    phase: CleanupPhase,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    transaction_id = _prepare(journal).transaction.transaction_id
    for candidate in _NORMAL_ADVANCES_BEFORE[phase]:
        journal.advance(transaction_id, candidate)
    manual = journal.mark_manual_recovery(transaction_id, "destination_mismatch")
    assert manual.replayed is False
    assert manual.transaction.phase is CleanupPhase.MANUAL_RECOVERY
    assert manual.transaction.error_code == "destination_mismatch"
    assert journal.mark_manual_recovery(
        transaction_id, "destination_mismatch"
    ).replayed is True
    with pytest.raises(ModelCleanupJournalConflictError) as changed_reason:
        journal.mark_manual_recovery(transaction_id, "config_conflict")
    assert changed_reason.value.code == "model_cleanup_transaction_conflict"
    with pytest.raises(ModelCleanupJournalConflictError) as normal_advance:
        journal.advance(transaction_id, CleanupPhase.COMPLETED)
    assert normal_advance.value.code == "model_cleanup_transaction_terminal"


_NORMAL_ADVANCES_BEFORE = {
    CleanupPhase.PREPARED: (),
    CleanupPhase.TRASH_CONFIRMED: (CleanupPhase.TRASH_CONFIRMED,),
    CleanupPhase.LEDGER_MARKED: (
        CleanupPhase.TRASH_CONFIRMED,
        CleanupPhase.LEDGER_MARKED,
    ),
    CleanupPhase.CONFIG_SAVED: (
        CleanupPhase.TRASH_CONFIRMED,
        CleanupPhase.LEDGER_MARKED,
        CleanupPhase.CONFIG_SAVED,
    ),
}


def test_manual_recovery_accepts_only_closed_error_codes(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    transaction_id = _prepare(journal).transaction.transaction_id
    with pytest.raises(ModelCleanupJournalConflictError) as error:
        journal.mark_manual_recovery(transaction_id, "/secret/model/path")
    assert error.value.code == "model_cleanup_error_code_invalid"
    assert journal.get(transaction_id).phase is CleanupPhase.PREPARED


def test_startup_listing_includes_midflight_and_manual_but_not_completed(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    prepared = _prepare(journal, 1).transaction
    midflight = _prepare(journal, 2).transaction
    journal.advance(midflight.transaction_id, "trash_confirmed")
    manual = _prepare(journal, 3).transaction
    journal.mark_manual_recovery(manual.transaction_id, "config_unavailable")
    completed = _prepare(journal, 4).transaction
    _complete(journal, completed.transaction_id)
    journal.close()
    restarted = _journal(tmp_path)
    restarted.initialize()
    assert [row.transaction_id for row in restarted.list_incomplete()] == [
        prepared.transaction_id,
        midflight.transaction_id,
        manual.transaction_id,
    ]
    assert len(restarted.list_all()) == 4


def test_completed_rows_prune_oldest_but_incomplete_and_manual_never_prune(
    tmp_path: Path,
) -> None:
    clock = Clock()
    journal = _journal(tmp_path, maximum_transactions=3, clock=clock)
    journal.initialize()
    oldest = _prepare(journal, 1).transaction
    _complete(journal, oldest.transaction_id)
    clock.value += 10
    prepared = _prepare(journal, 2).transaction
    clock.value += 10
    manual = _prepare(journal, 3).transaction
    journal.mark_manual_recovery(manual.transaction_id, "journal_conflict")
    clock.value += 10
    newest = _prepare(journal, 4).transaction
    assert journal.get(oldest.transaction_id) is None
    assert {row.transaction_id for row in journal.list_all()} == {
        prepared.transaction_id,
        manual.transaction_id,
        newest.transaction_id,
    }
    with pytest.raises(ModelCleanupJournalCapacityError) as capacity:
        _prepare(journal, 5)
    assert capacity.value.code == "model_cleanup_journal_capacity_exhausted"
    assert {row.transaction_id for row in journal.list_all()} == {
        prepared.transaction_id,
        manual.transaction_id,
        newest.transaction_id,
    }


def test_restart_with_lower_limit_keeps_and_surfaces_protected_rows(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path, maximum_transactions=3)
    journal.initialize()
    _prepare(journal, 1)
    second = _prepare(journal, 2).transaction
    journal.mark_manual_recovery(second.transaction_id, "journal_conflict")
    _prepare(journal, 3)
    journal.close()
    reduced = _journal(tmp_path, maximum_transactions=1)
    reduced.initialize()
    assert len(reduced.list_incomplete()) == 3
    with pytest.raises(ModelCleanupJournalCapacityError):
        _prepare(reduced, 4)


def test_event_timestamps_never_move_backwards_with_wall_clock(tmp_path: Path) -> None:
    clock = Clock()
    journal = _journal(tmp_path, clock=clock)
    journal.initialize()
    prepared = _prepare(journal).transaction
    clock.value -= 10_000
    advanced = journal.advance(prepared.transaction_id, "trash_confirmed")
    assert advanced.transaction.updated_at == prepared.updated_at


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), -1, True, RuntimeError("clock failed")],
)
def test_invalid_clock_values_fail_before_any_record(tmp_path: Path, value) -> None:
    def clock():
        if isinstance(value, Exception):
            raise value
        return value

    journal = ModelCleanupJournal(
        tmp_path / "config.yaml",
        clock=clock,
    )
    journal.initialize()
    with pytest.raises(ModelCleanupJournalIntegrityError) as error:
        _prepare(journal)
    assert error.value.code == "model_cleanup_journal_clock_invalid"
    assert journal.list_all() == ()


def test_sql_guards_reject_authority_changes_and_protected_deletion(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    record = _prepare(journal).transaction
    with sqlite3.connect(journal.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE native_model_cleanup_transactions_v1
                   SET alias_profile_fingerprint = ?
                 WHERE transaction_id = ?
                """,
                (_digest(999), record.transaction_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                DELETE FROM native_model_cleanup_transactions_v1
                 WHERE transaction_id = ?
                """,
                (record.transaction_id,),
            )
    assert journal.get(record.transaction_id) == record


def test_corrupt_bytes_fail_closed_without_recreating_database(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.path.parent.mkdir(parents=True)
    journal.path.write_bytes(b"not sqlite and not recoverable")
    before = journal.path.read_bytes()
    with pytest.raises(ModelCleanupJournalIntegrityError) as error:
        journal.initialize()
    assert error.value.code == "model_cleanup_journal_corrupt"
    assert journal.path.read_bytes() == before


def test_metadata_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    journal.close()
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "UPDATE native_model_cleanup_metadata_v1 SET store_id = 'other'"
        )
    restarted = _journal(tmp_path)
    with pytest.raises(ModelCleanupJournalIntegrityError) as error:
        restarted.initialize()
    assert error.value.code == "model_cleanup_journal_identity_mismatch"


def test_phase_event_corruption_is_detected_on_restart(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    record = _prepare(journal).transaction
    journal.close()
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            """
            UPDATE native_model_cleanup_transactions_v1
               SET current_phase = 'ledger_marked'
             WHERE transaction_id = ?
            """,
            (record.transaction_id,),
        )
    restarted = _journal(tmp_path)
    with pytest.raises(ModelCleanupJournalIntegrityError) as error:
        restarted.initialize()
    assert error.value.code == "model_cleanup_journal_corrupt"


@pytest.mark.parametrize(
    ("kind", "ledger", "expected"),
    [
        (
            CleanupKind.MANAGED,
            InstallLedgerState.INSTALLED,
            CleanupRecoveryAction.FINISH_LEDGER_AND_CONFIG,
        ),
        (
            CleanupKind.IMPORTED,
            InstallLedgerState.NOT_APPLICABLE,
            CleanupRecoveryAction.FINISH_CONFIG,
        ),
    ],
)
def test_prepared_recovery_aborts_if_tree_exists_and_finishes_if_missing(
    tmp_path: Path,
    kind: CleanupKind,
    ledger: InstallLedgerState,
    expected: CleanupRecoveryAction,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    transaction = _prepare(journal, kind=kind).transaction
    present = decide_cleanup_recovery(
        transaction,
        destination_state=DestinationState.EXACT_PRESENT,
        profile_fingerprint_present=True,
        install_ledger_state=ledger,
        config_state=CleanupConfigState.ORIGINAL,
    )
    assert present.action is CleanupRecoveryAction.ABORT_WITHOUT_MUTATION
    assert present.reason_code is None
    missing = decide_cleanup_recovery(
        transaction,
        destination_state=DestinationState.MISSING,
        profile_fingerprint_present=True,
        install_ledger_state=ledger,
        config_state=CleanupConfigState.ORIGINAL,
    )
    assert missing.action is expected
    assert missing.reason_code is None


def test_recovery_decision_tracks_consistent_cross_resource_crash_points(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    transaction_id = _prepare(journal).transaction.transaction_id
    trash = journal.advance(transaction_id, "trash_confirmed").transaction
    assert decide_cleanup_recovery(
        trash,
        destination_state="missing",
        profile_fingerprint_present=True,
        install_ledger_state="installed",
        config_state="original",
    ).action is CleanupRecoveryAction.FINISH_LEDGER_AND_CONFIG
    assert decide_cleanup_recovery(
        trash,
        destination_state="missing",
        profile_fingerprint_present=True,
        install_ledger_state="trashed",
        config_state="original",
    ).action is CleanupRecoveryAction.FINISH_CONFIG
    ledger = journal.advance(transaction_id, "ledger_marked").transaction
    assert decide_cleanup_recovery(
        ledger,
        destination_state="missing",
        profile_fingerprint_present=True,
        install_ledger_state="trashed",
        config_state="original",
    ).action is CleanupRecoveryAction.FINISH_CONFIG
    # The YAML save can commit before its journal append.
    assert decide_cleanup_recovery(
        ledger,
        destination_state="missing",
        profile_fingerprint_present=False,
        install_ledger_state="trashed",
        config_state="result",
    ).action is CleanupRecoveryAction.FINALIZE_JOURNAL
    config = journal.advance(transaction_id, "config_saved").transaction
    assert decide_cleanup_recovery(
        config,
        destination_state="missing",
        profile_fingerprint_present=False,
        install_ledger_state="trashed",
        config_state="result",
    ).action is CleanupRecoveryAction.FINALIZE_JOURNAL
    completed = journal.advance(transaction_id, "completed").transaction
    assert decide_cleanup_recovery(
        completed,
        destination_state="missing",
        profile_fingerprint_present=False,
        install_ledger_state="trashed",
        config_state="result",
    ).action is CleanupRecoveryAction.NO_ACTION


@pytest.mark.parametrize(
    ("destination", "profile_present", "ledger", "config", "reason"),
    [
        ("mismatch", True, "installed", "original", "destination_mismatch"),
        ("unavailable", True, "installed", "original", "destination_unavailable"),
        ("missing", True, "unavailable", "original", "install_store_unavailable"),
        ("missing", True, "missing", "original", "install_status_conflict"),
        ("missing", True, "installed", "conflict", "config_conflict"),
        ("missing", True, "installed", "unavailable", "config_unavailable"),
        ("missing", False, "installed", "original", "profile_conflict"),
        ("missing", True, "installed", "result", "profile_conflict"),
    ],
)
def test_recovery_refuses_mismatch_unavailable_and_conflicting_observations(
    tmp_path: Path,
    destination: str,
    profile_present: bool,
    ledger: str,
    config: str,
    reason: str,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    transaction = _prepare(journal).transaction
    decision = decide_cleanup_recovery(
        transaction,
        destination_state=destination,
        profile_fingerprint_present=profile_present,
        install_ledger_state=ledger,
        config_state=config,
    )
    assert decision.action is CleanupRecoveryAction.MANUAL_RECOVERY
    assert decision.reason_code == reason


def test_exact_tree_reappearing_after_trash_confirmation_requires_manual_recovery(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    transaction = _prepare(journal).transaction
    transaction = journal.advance(
        transaction.transaction_id, "trash_confirmed"
    ).transaction
    decision = decide_cleanup_recovery(
        transaction,
        destination_state="exact_present",
        profile_fingerprint_present=True,
        install_ledger_state="installed",
        config_state="original",
    )
    assert decision.action is CleanupRecoveryAction.MANUAL_RECOVERY
    assert decision.reason_code == "recovery_observation_conflict"


def test_manual_transaction_decision_never_infers_further_work(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.initialize()
    transaction = _prepare(journal).transaction
    transaction = journal.mark_manual_recovery(
        transaction.transaction_id, "destination_mismatch"
    ).transaction
    decision = decide_cleanup_recovery(
        transaction,
        destination_state="missing",
        profile_fingerprint_present=True,
        install_ledger_state="installed",
        config_state="original",
    )
    assert decision.action is CleanupRecoveryAction.MANUAL_RECOVERY
    assert decision.reason_code == "destination_mismatch"


def test_recovery_function_and_journal_have_no_mutating_layer_imports() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        module.endswith(
            (
                "config",
                "filesystem",
                "install_store",
                "installer",
                "runtime",
                "storage",
            )
        )
        for module in imported_modules
    )
    assert "trash_paths(" not in source
    assert "save_config(" not in source
    assert "mark_trashed(" not in source
