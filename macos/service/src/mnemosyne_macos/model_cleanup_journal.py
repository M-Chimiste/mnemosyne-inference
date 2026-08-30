"""Durable, mutation-free journal core for native model cleanup.

The cleanup operation crosses three independently durable resources: the
macOS Trash operation, the exact installation ledger, and the YAML
configuration.  This module records the authority and ordering needed to
recover that sequence after a process crash.  It deliberately has no imports
from the filesystem, installer, configuration, residency, or HTTP layers and
never performs any of those mutations itself.

Only content-free identifiers and fingerprints are retained.  In particular,
the journal never stores model paths, aliases, request bodies, credentials,
bookmark bytes, prompts, or arbitrary diagnostics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import math
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from typing import Final, TypeVar
from uuid import UUID


_STORE_ID: Final[str] = "mnemosyne-native-model-cleanup-journal-v1"
_STORE_SCHEMA_VERSION: Final[int] = 1
_DATABASE_NAME: Final[str] = "journal.sqlite3"
_MAX_TRANSACTIONS: Final[int] = 10_000
_MAX_TIMESTAMP: Final[float] = 4_102_444_800.0
_DIGEST_LENGTH: Final[int] = 64
_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")


class CleanupKind(str, Enum):
    """The already-authorized local cleanup class."""

    MANAGED = "managed"
    IMPORTED = "imported"


class CleanupPhase(str, Enum):
    """Closed, append-only cleanup phases."""

    PREPARED = "prepared"
    TRASH_CONFIRMED = "trash_confirmed"
    LEDGER_MARKED = "ledger_marked"
    CONFIG_SAVED = "config_saved"
    COMPLETED = "completed"
    ABORTED = "aborted"
    MANUAL_RECOVERY = "manual_recovery"


class DestinationState(str, Enum):
    """Result of a fresh exact-tree observation made outside this module."""

    EXACT_PRESENT = "exact_present"
    MISSING = "missing"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


class InstallLedgerState(str, Enum):
    """Normalized state of the exact installation row."""

    INSTALLED = "installed"
    TRASHED = "trashed"
    NOT_APPLICABLE = "not_applicable"
    MISSING = "missing"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class CleanupConfigState(str, Enum):
    """Observed configuration revision, compared outside this module."""

    ORIGINAL = "original"
    RESULT = "result"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class CleanupRecoveryAction(str, Enum):
    """Closed safe outcomes from :func:`decide_cleanup_recovery`."""

    ABORT_WITHOUT_MUTATION = "abort_without_mutation"
    FINISH_LEDGER_AND_CONFIG = "finish_ledger_and_config"
    FINISH_CONFIG = "finish_config"
    FINALIZE_JOURNAL = "finalize_journal"
    NO_ACTION = "no_action"
    MANUAL_RECOVERY = "manual_recovery"


MANUAL_RECOVERY_CODES: Final[frozenset[str]] = frozenset(
    {
        "config_conflict",
        "config_save_failed",
        "config_unavailable",
        "destination_mismatch",
        "destination_unavailable",
        "install_status_conflict",
        "install_store_unavailable",
        "journal_conflict",
        "ledger_update_failed",
        "profile_conflict",
        "recovery_observation_conflict",
    }
)


_NORMAL_PHASES: Final[tuple[CleanupPhase, ...]] = (
    CleanupPhase.PREPARED,
    CleanupPhase.TRASH_CONFIRMED,
    CleanupPhase.LEDGER_MARKED,
    CleanupPhase.CONFIG_SAVED,
    CleanupPhase.COMPLETED,
)
_NORMAL_PHASE_INDEX: Final[dict[CleanupPhase, int]] = {
    phase: index for index, phase in enumerate(_NORMAL_PHASES)
}
_NEXT_PHASE: Final[dict[CleanupPhase, CleanupPhase]] = {
    earlier: later for earlier, later in zip(_NORMAL_PHASES, _NORMAL_PHASES[1:])
}
_PHASE_SQL: Final[str] = ",".join(
    f"'{phase.value}'" for phase in CleanupPhase
)
_KIND_SQL: Final[str] = ",".join(f"'{kind.value}'" for kind in CleanupKind)
_ERROR_SQL: Final[str] = ",".join(
    f"'{code}'" for code in sorted(MANUAL_RECOVERY_CODES)
)


class ModelCleanupJournalError(RuntimeError):
    """A fixed-code journal failure safe for a local status surface."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ModelCleanupJournalConflictError(ModelCleanupJournalError):
    pass


class ModelCleanupJournalIntegrityError(ModelCleanupJournalError):
    pass


class ModelCleanupJournalCapacityError(ModelCleanupJournalError):
    pass


class ModelCleanupJournalNotFoundError(ModelCleanupJournalError):
    pass


@dataclass(frozen=True, slots=True)
class CleanupTransaction:
    """One validated cleanup transaction and its current durable phase."""

    transaction_id: str
    installation_id: str | None
    alias_profile_fingerprint: str
    original_config_revision: str
    result_config_revision: str
    cleanup_kind: CleanupKind
    phase: CleanupPhase
    created_at: float
    updated_at: float
    error_code: str | None

    @property
    def completed(self) -> bool:
        return self.phase is CleanupPhase.COMPLETED

    @property
    def needs_recovery(self) -> bool:
        return self.phase not in {
            CleanupPhase.COMPLETED,
            CleanupPhase.ABORTED,
        }


@dataclass(frozen=True, slots=True)
class CleanupJournalMutation:
    transaction: CleanupTransaction
    replayed: bool


@dataclass(frozen=True, slots=True)
class CleanupRecoveryDecision:
    action: CleanupRecoveryAction
    reason_code: str | None = None


_T = TypeVar("_T")


class ModelCleanupJournal:
    """Private, bounded, append-only model-cleanup transaction journal.

    ``config_path`` is used only as a location anchor.  The journal always
    lives in ``state/model-cleanup`` beside that exact active configuration;
    it cannot follow the user-configurable model or catalog database roots.
    """

    def __init__(
        self,
        config_path: str | Path,
        *,
        maximum_transactions: int = 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            isinstance(maximum_transactions, bool)
            or not isinstance(maximum_transactions, int)
            or not 1 <= maximum_transactions <= _MAX_TRANSACTIONS
        ):
            raise ValueError("invalid model-cleanup journal limit")
        anchor = Path(config_path).expanduser()
        self._directory = anchor.parent / "state" / "model-cleanup"
        self._path = self._directory / _DATABASE_NAME
        self._maximum_transactions = maximum_transactions
        self._clock = clock
        self._lock = threading.RLock()
        self._initialized = False
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        """Create or validate the journal and surface durable corruption."""

        with self._lock:
            if self._closed:
                raise ModelCleanupJournalIntegrityError(
                    "model_cleanup_journal_unavailable"
                )
            self._prepare_path()
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                self._create_schema(connection)
                self._validate_metadata(connection)
                self._validate_all(connection)
                # A deployment may lower its configured history limit while
                # protected recovery rows already exceed it.  Startup must
                # still surface those rows; only a later prepare is refused.
                self._prune_to_limit(
                    connection,
                    reserve=0,
                    fail_if_protected=False,
                )
                connection.execute("COMMIT")
                self._initialized = True
            except ModelCleanupJournalError:
                _rollback(connection)
                raise
            except (OSError, sqlite3.Error, TypeError, ValueError):
                _rollback(connection)
                raise ModelCleanupJournalIntegrityError(
                    "model_cleanup_journal_corrupt"
                ) from None
            finally:
                if connection is not None:
                    connection.close()
                self._secure_database_artifacts()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._initialized = False

    def prepare(
        self,
        *,
        transaction_id: str,
        installation_id: str | None,
        alias_profile_fingerprint: str,
        original_config_revision: str,
        result_config_revision: str,
        cleanup_kind: CleanupKind | str,
    ) -> CleanupJournalMutation:
        """Durably authorize one exact cleanup before any Trash operation."""

        transaction_id = _canonical_uuid(transaction_id)
        installation_id = (
            _canonical_uuid(installation_id)
            if installation_id is not None
            else None
        )
        cleanup_kind = _coerce_enum(
            CleanupKind,
            cleanup_kind,
            "model_cleanup_journal_invalid_record",
        )
        _validate_digest(alias_profile_fingerprint)
        _validate_digest(original_config_revision)
        _validate_digest(result_config_revision)
        if original_config_revision == result_config_revision:
            raise ModelCleanupJournalConflictError(
                "model_cleanup_config_revision_conflict"
            )
        if (
            cleanup_kind is CleanupKind.MANAGED
            and installation_id is None
        ) or (
            cleanup_kind is CleanupKind.IMPORTED
            and installation_id is not None
        ):
            raise ModelCleanupJournalConflictError(
                "model_cleanup_installation_binding_conflict"
            )
        now = self._timestamp()

        def operation(connection: sqlite3.Connection) -> CleanupJournalMutation:
            existing = self._select_transaction(connection, transaction_id)
            if existing is not None:
                if (
                    existing.installation_id != installation_id
                    or existing.alias_profile_fingerprint
                    != alias_profile_fingerprint
                    or existing.original_config_revision
                    != original_config_revision
                    or existing.result_config_revision
                    != result_config_revision
                    or existing.cleanup_kind is not cleanup_kind
                ):
                    raise ModelCleanupJournalConflictError(
                        "model_cleanup_transaction_conflict"
                    )
                return CleanupJournalMutation(existing, replayed=True)

            self._prune_to_limit(
                connection,
                reserve=1,
                fail_if_protected=True,
            )
            connection.execute(
                """
                INSERT INTO native_model_cleanup_transactions_v1 (
                    transaction_id, installation_id,
                    alias_profile_fingerprint,
                    original_config_revision, result_config_revision,
                    cleanup_kind, current_phase, created_at, updated_at,
                    error_code
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?, NULL)
                """,
                (
                    transaction_id,
                    installation_id,
                    alias_profile_fingerprint,
                    original_config_revision,
                    result_config_revision,
                    cleanup_kind.value,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO native_model_cleanup_events_v1 (
                    transaction_id, sequence, phase, recorded_at, error_code
                ) VALUES (?, 1, 'prepared', ?, NULL)
                """,
                (transaction_id, now),
            )
            record = self._require_transaction(connection, transaction_id)
            return CleanupJournalMutation(record, replayed=False)

        return self._write(operation)

    def advance(
        self,
        transaction_id: str,
        phase: CleanupPhase | str,
    ) -> CleanupJournalMutation:
        """Append exactly the next normal phase, or replay an existing one."""

        transaction_id = _canonical_uuid(transaction_id)
        phase = _coerce_enum(
            CleanupPhase,
            phase,
            "model_cleanup_phase_invalid",
        )
        if phase in {
            CleanupPhase.PREPARED,
            CleanupPhase.ABORTED,
            CleanupPhase.MANUAL_RECOVERY,
        }:
            raise ModelCleanupJournalConflictError(
                "model_cleanup_phase_out_of_order"
            )

        def operation(connection: sqlite3.Connection) -> CleanupJournalMutation:
            current = self._require_transaction(connection, transaction_id)
            events = self._events(connection, transaction_id)
            if any(event_phase is phase for _, event_phase, _, _ in events):
                return CleanupJournalMutation(current, replayed=True)
            if current.phase in {
                CleanupPhase.COMPLETED,
                CleanupPhase.ABORTED,
                CleanupPhase.MANUAL_RECOVERY,
            }:
                raise ModelCleanupJournalConflictError(
                    "model_cleanup_transaction_terminal"
                )
            if _NEXT_PHASE.get(current.phase) is not phase:
                raise ModelCleanupJournalConflictError(
                    "model_cleanup_phase_out_of_order"
                )
            now = max(self._timestamp(), current.updated_at)
            sequence = len(events) + 1
            connection.execute(
                """
                INSERT INTO native_model_cleanup_events_v1 (
                    transaction_id, sequence, phase, recorded_at, error_code
                ) VALUES (?, ?, ?, ?, NULL)
                """,
                (transaction_id, sequence, phase.value, now),
            )
            connection.execute(
                """
                UPDATE native_model_cleanup_transactions_v1
                   SET current_phase = ?, updated_at = ?, error_code = NULL
                 WHERE transaction_id = ?
                """,
                (phase.value, now, transaction_id),
            )
            updated = self._require_transaction(connection, transaction_id)
            return CleanupJournalMutation(updated, replayed=False)

        return self._write(operation)

    def abort_without_mutation(
        self,
        transaction_id: str,
    ) -> CleanupJournalMutation:
        """Close a prepared row after proving its exact tree was untouched.

        This branch is deliberately available only from ``prepared``. Once
        Trash may have succeeded, a caller must finish the transaction or
        enter manual recovery; it can never relabel ambiguity as an abort.
        """

        transaction_id = _canonical_uuid(transaction_id)

        def operation(connection: sqlite3.Connection) -> CleanupJournalMutation:
            current = self._require_transaction(connection, transaction_id)
            if current.phase is CleanupPhase.ABORTED:
                return CleanupJournalMutation(current, replayed=True)
            if current.phase is not CleanupPhase.PREPARED:
                raise ModelCleanupJournalConflictError(
                    "model_cleanup_transaction_terminal"
                    if current.phase
                    in {
                        CleanupPhase.COMPLETED,
                        CleanupPhase.MANUAL_RECOVERY,
                    }
                    else "model_cleanup_phase_out_of_order"
                )
            events = self._events(connection, transaction_id)
            now = max(self._timestamp(), current.updated_at)
            connection.execute(
                """
                INSERT INTO native_model_cleanup_events_v1 (
                    transaction_id, sequence, phase, recorded_at, error_code
                ) VALUES (?, ?, 'aborted', ?, NULL)
                """,
                (transaction_id, len(events) + 1, now),
            )
            connection.execute(
                """
                UPDATE native_model_cleanup_transactions_v1
                   SET current_phase = 'aborted', updated_at = ?,
                       error_code = NULL
                 WHERE transaction_id = ?
                """,
                (now, transaction_id),
            )
            updated = self._require_transaction(connection, transaction_id)
            return CleanupJournalMutation(updated, replayed=False)

        return self._write(operation)

    def mark_manual_recovery(
        self,
        transaction_id: str,
        error_code: str,
    ) -> CleanupJournalMutation:
        """Append the closed manual-recovery terminal for a fixed reason."""

        transaction_id = _canonical_uuid(transaction_id)
        if error_code not in MANUAL_RECOVERY_CODES:
            raise ModelCleanupJournalConflictError(
                "model_cleanup_error_code_invalid"
            )

        def operation(connection: sqlite3.Connection) -> CleanupJournalMutation:
            current = self._require_transaction(connection, transaction_id)
            if current.phase is CleanupPhase.MANUAL_RECOVERY:
                if current.error_code != error_code:
                    raise ModelCleanupJournalConflictError(
                        "model_cleanup_transaction_conflict"
                    )
                return CleanupJournalMutation(current, replayed=True)
            if current.phase in {
                CleanupPhase.COMPLETED,
                CleanupPhase.ABORTED,
            }:
                raise ModelCleanupJournalConflictError(
                    "model_cleanup_transaction_terminal"
                )
            events = self._events(connection, transaction_id)
            now = max(self._timestamp(), current.updated_at)
            connection.execute(
                """
                INSERT INTO native_model_cleanup_events_v1 (
                    transaction_id, sequence, phase, recorded_at, error_code
                ) VALUES (?, ?, 'manual_recovery', ?, ?)
                """,
                (transaction_id, len(events) + 1, now, error_code),
            )
            connection.execute(
                """
                UPDATE native_model_cleanup_transactions_v1
                   SET current_phase = 'manual_recovery', updated_at = ?,
                       error_code = ?
                 WHERE transaction_id = ?
                """,
                (now, error_code, transaction_id),
            )
            updated = self._require_transaction(connection, transaction_id)
            return CleanupJournalMutation(updated, replayed=False)

        return self._write(operation)

    def get(self, transaction_id: str) -> CleanupTransaction | None:
        transaction_id = _canonical_uuid(transaction_id)
        return self._read(
            lambda connection: self._select_transaction(
                connection, transaction_id
            )
        )

    def list_incomplete(self) -> tuple[CleanupTransaction, ...]:
        """Return every startup recovery item, including manual rows."""

        return self._read(
            lambda connection: self._select_many(
                connection,
                "WHERE current_phase NOT IN ('completed', 'aborted')",
            )
        )

    def list_all(self) -> tuple[CleanupTransaction, ...]:
        return self._read(lambda connection: self._select_many(connection, ""))

    def _read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return self._operate(operation, write=False)

    def _write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return self._operate(operation, write=True)

    def _operate(
        self,
        operation: Callable[[sqlite3.Connection], _T],
        *,
        write: bool,
    ) -> _T:
        with self._lock:
            self._require_ready()
            self._prepare_path()
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                self._validate_metadata(connection)
                result = operation(connection)
                connection.execute("COMMIT")
                return result
            except ModelCleanupJournalError:
                _rollback(connection)
                raise
            except (OSError, sqlite3.Error, TypeError, ValueError):
                _rollback(connection)
                raise ModelCleanupJournalIntegrityError(
                    "model_cleanup_journal_corrupt"
                ) from None
            finally:
                if connection is not None:
                    connection.close()
                self._secure_database_artifacts()

    def _require_ready(self) -> None:
        if self._closed or not self._initialized:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_unavailable"
            )

    def _timestamp(self) -> float:
        try:
            value = self._clock()
        except Exception:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_clock_invalid"
            ) from None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= _MAX_TIMESTAMP
        ):
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_clock_invalid"
            )
        return float(value)

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_model_cleanup_metadata_v1 (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                store_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS native_model_cleanup_transactions_v1 (
                transaction_id TEXT PRIMARY KEY,
                installation_id TEXT,
                alias_profile_fingerprint TEXT NOT NULL,
                original_config_revision TEXT NOT NULL,
                result_config_revision TEXT NOT NULL,
                cleanup_kind TEXT NOT NULL CHECK (cleanup_kind IN ({_KIND_SQL})),
                current_phase TEXT NOT NULL CHECK (current_phase IN ({_PHASE_SQL})),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error_code TEXT CHECK (
                    error_code IS NULL OR error_code IN ({_ERROR_SQL})
                ),
                CHECK (
                    (cleanup_kind = 'managed' AND installation_id IS NOT NULL)
                    OR
                    (cleanup_kind = 'imported' AND installation_id IS NULL)
                ),
                CHECK (original_config_revision != result_config_revision)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS native_model_cleanup_events_v1 (
                transaction_id TEXT NOT NULL REFERENCES
                    native_model_cleanup_transactions_v1(transaction_id)
                    ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence >= 1 AND sequence <= 6),
                phase TEXT NOT NULL CHECK (phase IN ({_PHASE_SQL})),
                recorded_at REAL NOT NULL,
                error_code TEXT CHECK (
                    error_code IS NULL OR error_code IN ({_ERROR_SQL})
                ),
                PRIMARY KEY (transaction_id, sequence),
                UNIQUE (transaction_id, phase)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS native_model_cleanup_phase_v1
                ON native_model_cleanup_transactions_v1(
                    current_phase, updated_at, transaction_id
                )
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS
                native_model_cleanup_authority_immutable_v1
            BEFORE UPDATE OF
                transaction_id, installation_id,
                alias_profile_fingerprint, original_config_revision,
                result_config_revision, cleanup_kind, created_at
            ON native_model_cleanup_transactions_v1
            BEGIN
                SELECT RAISE(ABORT, 'model_cleanup_authority_immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS
                native_model_cleanup_protected_delete_v1
            BEFORE DELETE ON native_model_cleanup_transactions_v1
            WHEN OLD.current_phase NOT IN ('completed', 'aborted')
            BEGIN
                SELECT RAISE(ABORT, 'model_cleanup_recovery_row_protected');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS
                native_model_cleanup_event_immutable_v1
            BEFORE UPDATE ON native_model_cleanup_events_v1
            BEGIN
                SELECT RAISE(ABORT, 'model_cleanup_event_immutable');
            END
            """
        )
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM native_model_cleanup_metadata_v1"
        ).fetchone()
        if row is None:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_corrupt"
            )
        if int(row["total"]) == 0:
            transaction_count = connection.execute(
                "SELECT COUNT(*) AS total FROM native_model_cleanup_transactions_v1"
            ).fetchone()
            if transaction_count is None or int(transaction_count["total"]) != 0:
                raise ModelCleanupJournalIntegrityError(
                    "model_cleanup_journal_identity_mismatch"
                )
            connection.execute(
                """
                INSERT INTO native_model_cleanup_metadata_v1 (
                    singleton, store_id, schema_version
                ) VALUES (1, ?, ?)
                """,
                (_STORE_ID, _STORE_SCHEMA_VERSION),
            )

    def _validate_metadata(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT singleton, store_id, schema_version
              FROM native_model_cleanup_metadata_v1
            """
        ).fetchall()
        if (
            len(rows) != 1
            or int(rows[0]["singleton"]) != 1
            or str(rows[0]["store_id"]) != _STORE_ID
            or int(rows[0]["schema_version"]) != _STORE_SCHEMA_VERSION
        ):
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_identity_mismatch"
            )

    def _validate_all(self, connection: sqlite3.Connection) -> None:
        check = connection.execute("PRAGMA integrity_check(1)").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_corrupt"
            )
        rows = connection.execute(
            """
            SELECT * FROM native_model_cleanup_transactions_v1
             ORDER BY created_at, transaction_id
            """
        ).fetchall()
        for row in rows:
            self._record_from_row(connection, row)

    def _validate_record(
        self,
        connection: sqlite3.Connection,
        record: CleanupTransaction,
    ) -> None:
        events = self._events(connection, record.transaction_id)
        if not events:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_corrupt"
            )
        phases = [phase for _, phase, _, _ in events]
        if phases[0] is not CleanupPhase.PREPARED:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_corrupt"
            )
        normal_prefix: list[CleanupPhase] = []
        manual_seen = False
        aborted_seen = False
        last_timestamp = -1.0
        for expected_sequence, (sequence, phase, recorded_at, error_code) in enumerate(
            events, start=1
        ):
            if sequence != expected_sequence or recorded_at < last_timestamp:
                raise ModelCleanupJournalIntegrityError(
                    "model_cleanup_journal_corrupt"
                )
            last_timestamp = recorded_at
            if phase is CleanupPhase.MANUAL_RECOVERY:
                if (
                    manual_seen
                    or aborted_seen
                    or expected_sequence == 1
                    or error_code is None
                ):
                    raise ModelCleanupJournalIntegrityError(
                        "model_cleanup_journal_corrupt"
                    )
                manual_seen = True
                if expected_sequence != len(events):
                    raise ModelCleanupJournalIntegrityError(
                        "model_cleanup_journal_corrupt"
                    )
            elif phase is CleanupPhase.ABORTED:
                if (
                    manual_seen
                    or aborted_seen
                    or expected_sequence != 2
                    or expected_sequence != len(events)
                    or error_code is not None
                    or tuple(normal_prefix) != (CleanupPhase.PREPARED,)
                ):
                    raise ModelCleanupJournalIntegrityError(
                        "model_cleanup_journal_corrupt"
                    )
                aborted_seen = True
            else:
                if manual_seen or aborted_seen or error_code is not None:
                    raise ModelCleanupJournalIntegrityError(
                        "model_cleanup_journal_corrupt"
                    )
                normal_prefix.append(phase)
        if tuple(normal_prefix) != _NORMAL_PHASES[: len(normal_prefix)]:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_corrupt"
            )
        if record.phase is not phases[-1]:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_corrupt"
            )
        if record.created_at != events[0][2] or record.updated_at != events[-1][2]:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_corrupt"
            )
        if record.phase is CleanupPhase.MANUAL_RECOVERY:
            if record.error_code != events[-1][3]:
                raise ModelCleanupJournalIntegrityError(
                    "model_cleanup_journal_corrupt"
                )
        elif record.error_code is not None:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_corrupt"
            )

    def _events(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
    ) -> tuple[tuple[int, CleanupPhase, float, str | None], ...]:
        rows = connection.execute(
            """
            SELECT sequence, phase, recorded_at, error_code
              FROM native_model_cleanup_events_v1
             WHERE transaction_id = ?
             ORDER BY sequence
            """,
            (transaction_id,),
        ).fetchall()
        events: list[tuple[int, CleanupPhase, float, str | None]] = []
        for row in rows:
            try:
                sequence = int(row["sequence"])
                phase = CleanupPhase(str(row["phase"]))
                recorded_at = _validate_timestamp(row["recorded_at"])
                error_code = (
                    str(row["error_code"])
                    if row["error_code"] is not None
                    else None
                )
            except (TypeError, ValueError):
                raise ModelCleanupJournalIntegrityError(
                    "model_cleanup_journal_corrupt"
                ) from None
            if error_code is not None and error_code not in MANUAL_RECOVERY_CODES:
                raise ModelCleanupJournalIntegrityError(
                    "model_cleanup_journal_corrupt"
                )
            events.append((sequence, phase, recorded_at, error_code))
        return tuple(events)

    def _record_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> CleanupTransaction:
        try:
            transaction_id = _canonical_uuid(str(row["transaction_id"]))
            installation_id = (
                _canonical_uuid(str(row["installation_id"]))
                if row["installation_id"] is not None
                else None
            )
            alias_profile_fingerprint = str(row["alias_profile_fingerprint"])
            original_config_revision = str(row["original_config_revision"])
            result_config_revision = str(row["result_config_revision"])
            _validate_digest(alias_profile_fingerprint)
            _validate_digest(original_config_revision)
            _validate_digest(result_config_revision)
            if original_config_revision == result_config_revision:
                raise ValueError
            cleanup_kind = CleanupKind(str(row["cleanup_kind"]))
            phase = CleanupPhase(str(row["current_phase"]))
            created_at = _validate_timestamp(row["created_at"])
            updated_at = _validate_timestamp(row["updated_at"])
            error_code = (
                str(row["error_code"])
                if row["error_code"] is not None
                else None
            )
            if cleanup_kind is CleanupKind.MANAGED:
                if installation_id is None:
                    raise ValueError
            elif installation_id is not None:
                raise ValueError
            if error_code is not None and error_code not in MANUAL_RECOVERY_CODES:
                raise ValueError
        except (ModelCleanupJournalError, TypeError, ValueError):
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_corrupt"
            ) from None
        record = CleanupTransaction(
            transaction_id=transaction_id,
            installation_id=installation_id,
            alias_profile_fingerprint=alias_profile_fingerprint,
            original_config_revision=original_config_revision,
            result_config_revision=result_config_revision,
            cleanup_kind=cleanup_kind,
            phase=phase,
            created_at=created_at,
            updated_at=updated_at,
            error_code=error_code,
        )
        self._validate_record(connection, record)
        return record

    def _select_transaction(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
    ) -> CleanupTransaction | None:
        row = connection.execute(
            """
            SELECT * FROM native_model_cleanup_transactions_v1
             WHERE transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()
        if row is None:
            return None
        return self._record_from_row(connection, row)

    def _require_transaction(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
    ) -> CleanupTransaction:
        record = self._select_transaction(connection, transaction_id)
        if record is None:
            raise ModelCleanupJournalNotFoundError(
                "model_cleanup_transaction_not_found"
            )
        return record

    def _select_many(
        self,
        connection: sqlite3.Connection,
        where: str,
    ) -> tuple[CleanupTransaction, ...]:
        rows = connection.execute(
            f"""
            SELECT * FROM native_model_cleanup_transactions_v1
             {where}
             ORDER BY created_at, transaction_id
            """
        ).fetchall()
        return tuple(self._record_from_row(connection, row) for row in rows)

    def _prune_to_limit(
        self,
        connection: sqlite3.Connection,
        *,
        reserve: int,
        fail_if_protected: bool,
    ) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM native_model_cleanup_transactions_v1"
        ).fetchone()
        if row is None:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_corrupt"
            )
        excess = int(row["total"]) + reserve - self._maximum_transactions
        if excess > 0:
            completed = connection.execute(
                """
                SELECT transaction_id
                  FROM native_model_cleanup_transactions_v1
                 WHERE current_phase IN ('completed', 'aborted')
                 ORDER BY updated_at, transaction_id
                 LIMIT ?
                """,
                (excess,),
            ).fetchall()
            for candidate in completed:
                connection.execute(
                    """
                    DELETE FROM native_model_cleanup_transactions_v1
                     WHERE transaction_id = ?
                       AND current_phase IN ('completed', 'aborted')
                    """,
                    (str(candidate["transaction_id"]),),
                )
            excess -= len(completed)
        if excess > 0 and fail_if_protected:
            raise ModelCleanupJournalCapacityError(
                "model_cleanup_journal_capacity_exhausted"
            )

    def _prepare_path(self) -> None:
        try:
            state_directory = self._directory.parent
            state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _secure_directory(state_directory)
            self._directory.mkdir(exist_ok=True, mode=0o700)
            _secure_directory(self._directory)
            try:
                status = self._path.lstat()
            except FileNotFoundError:
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self._path, flags, 0o600)
                try:
                    os.fchmod(descriptor, 0o600)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                _fsync_directory(self._directory)
                status = self._path.lstat()
            _secure_regular_file(self._path, status)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self._path}{suffix}")
                try:
                    sidecar_status = sidecar.lstat()
                except FileNotFoundError:
                    continue
                _secure_regular_file(sidecar, sidecar_status)
        except ModelCleanupJournalError:
            raise
        except OSError:
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_insecure_path"
            ) from None

    def _secure_database_artifacts(self) -> None:
        try:
            for path in (
                self._path,
                Path(f"{self._path}-wal"),
                Path(f"{self._path}-shm"),
            ):
                try:
                    status = path.lstat()
                except FileNotFoundError:
                    continue
                _secure_regular_file(path, status)
        except (OSError, ModelCleanupJournalError):
            # A primary operation already has a deterministic result.  A
            # subsequent call revalidates and fails closed before any access.
            return

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._path),
            timeout=10,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA trusted_schema=OFF")
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            connection.close()
            raise ModelCleanupJournalIntegrityError(
                "model_cleanup_journal_durability_unavailable"
            )
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA wal_autocheckpoint=1")
        return connection


def decide_cleanup_recovery(
    transaction: CleanupTransaction,
    *,
    destination_state: DestinationState | str,
    profile_fingerprint_present: bool,
    install_ledger_state: InstallLedgerState | str,
    config_state: CleanupConfigState | str,
) -> CleanupRecoveryDecision:
    """Return a safe recovery action without mutating any external resource.

    All observations must be exact and freshly normalized by the integrating
    layer.  Unknown, unavailable, mismatched, or cross-phase observations
    always resolve to manual recovery rather than inferred deletion authority.
    """

    if not isinstance(transaction, CleanupTransaction):
        raise TypeError("transaction must be a CleanupTransaction")
    destination_state = _coerce_enum(
        DestinationState,
        destination_state,
        "model_cleanup_recovery_observation_invalid",
    )
    install_ledger_state = _coerce_enum(
        InstallLedgerState,
        install_ledger_state,
        "model_cleanup_recovery_observation_invalid",
    )
    config_state = _coerce_enum(
        CleanupConfigState,
        config_state,
        "model_cleanup_recovery_observation_invalid",
    )
    if not isinstance(profile_fingerprint_present, bool):
        raise ModelCleanupJournalConflictError(
            "model_cleanup_recovery_observation_invalid"
        )

    if destination_state is DestinationState.MISMATCH:
        return _manual("destination_mismatch")
    if destination_state is DestinationState.UNAVAILABLE:
        return _manual("destination_unavailable")
    if config_state is CleanupConfigState.CONFLICT:
        return _manual("config_conflict")
    if config_state is CleanupConfigState.UNAVAILABLE:
        return _manual("config_unavailable")
    if install_ledger_state is InstallLedgerState.UNAVAILABLE:
        return _manual("install_store_unavailable")
    if install_ledger_state in {
        InstallLedgerState.MISSING,
        InstallLedgerState.CONFLICT,
    }:
        return _manual("install_status_conflict")

    if transaction.cleanup_kind is CleanupKind.MANAGED:
        if install_ledger_state not in {
            InstallLedgerState.INSTALLED,
            InstallLedgerState.TRASHED,
        }:
            return _manual("install_status_conflict")
        pre_ledger = install_ledger_state is InstallLedgerState.INSTALLED
        final_ledger = install_ledger_state is InstallLedgerState.TRASHED
    else:
        if install_ledger_state is not InstallLedgerState.NOT_APPLICABLE:
            return _manual("install_status_conflict")
        pre_ledger = True
        final_ledger = True

    original_config = (
        config_state is CleanupConfigState.ORIGINAL
        and profile_fingerprint_present
    )
    result_config = (
        config_state is CleanupConfigState.RESULT
        and not profile_fingerprint_present
    )
    if not original_config and not result_config:
        return _manual("profile_conflict")

    phase = transaction.phase
    if phase is CleanupPhase.MANUAL_RECOVERY:
        return _manual(transaction.error_code or "journal_conflict")
    if phase is CleanupPhase.ABORTED:
        if (
            destination_state is DestinationState.EXACT_PRESENT
            and original_config
            and pre_ledger
        ):
            return CleanupRecoveryDecision(CleanupRecoveryAction.NO_ACTION)
        return _manual("recovery_observation_conflict")

    if destination_state is DestinationState.EXACT_PRESENT:
        if (
            phase is CleanupPhase.PREPARED
            and original_config
            and pre_ledger
        ):
            return CleanupRecoveryDecision(
                CleanupRecoveryAction.ABORT_WITHOUT_MUTATION
            )
        return _manual("recovery_observation_conflict")

    # From here the exact authorized tree is missing.  No action below ever
    # asks the caller to remove another path.
    if phase is CleanupPhase.PREPARED:
        if not original_config or not pre_ledger:
            return _manual("recovery_observation_conflict")
        return CleanupRecoveryDecision(
            CleanupRecoveryAction.FINISH_LEDGER_AND_CONFIG
            if transaction.cleanup_kind is CleanupKind.MANAGED
            else CleanupRecoveryAction.FINISH_CONFIG
        )
    if phase is CleanupPhase.TRASH_CONFIRMED:
        if not original_config:
            return _manual("recovery_observation_conflict")
        if transaction.cleanup_kind is CleanupKind.MANAGED and pre_ledger:
            return CleanupRecoveryDecision(
                CleanupRecoveryAction.FINISH_LEDGER_AND_CONFIG
            )
        if final_ledger:
            return CleanupRecoveryDecision(CleanupRecoveryAction.FINISH_CONFIG)
        return _manual("install_status_conflict")
    if phase is CleanupPhase.LEDGER_MARKED:
        if not final_ledger:
            return _manual("install_status_conflict")
        if original_config:
            return CleanupRecoveryDecision(CleanupRecoveryAction.FINISH_CONFIG)
        if result_config:
            return CleanupRecoveryDecision(
                CleanupRecoveryAction.FINALIZE_JOURNAL
            )
        return _manual("config_conflict")
    if phase is CleanupPhase.CONFIG_SAVED:
        if final_ledger and result_config:
            return CleanupRecoveryDecision(
                CleanupRecoveryAction.FINALIZE_JOURNAL
            )
        return _manual("recovery_observation_conflict")
    if phase is CleanupPhase.COMPLETED:
        if final_ledger and result_config:
            return CleanupRecoveryDecision(CleanupRecoveryAction.NO_ACTION)
        return _manual("recovery_observation_conflict")
    return _manual("journal_conflict")


def _manual(reason_code: str) -> CleanupRecoveryDecision:
    if reason_code not in MANUAL_RECOVERY_CODES:
        reason_code = "journal_conflict"
    return CleanupRecoveryDecision(
        CleanupRecoveryAction.MANUAL_RECOVERY,
        reason_code,
    )


def _coerce_enum(
    enum_type: type[_T],
    value: _T | str,
    error_code: str,
) -> _T:
    try:
        return enum_type(value)  # type: ignore[call-arg,return-value]
    except (TypeError, ValueError):
        raise ModelCleanupJournalConflictError(error_code) from None


def _canonical_uuid(value: str) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ModelCleanupJournalConflictError(
            "model_cleanup_identifier_invalid"
        )
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ModelCleanupJournalConflictError(
            "model_cleanup_identifier_invalid"
        ) from None
    if value != canonical:
        raise ModelCleanupJournalConflictError(
            "model_cleanup_identifier_invalid"
        )
    return canonical


def _validate_digest(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _DIGEST_LENGTH
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ModelCleanupJournalConflictError(
            "model_cleanup_fingerprint_invalid"
        )


def _validate_timestamp(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= _MAX_TIMESTAMP
    ):
        raise ValueError("invalid timestamp")
    return float(value)


def _secure_directory(path: Path) -> None:
    status = path.lstat()
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise ModelCleanupJournalIntegrityError(
            "model_cleanup_journal_insecure_path"
        )
    os.chmod(path, 0o700, follow_symlinks=False)


def _secure_regular_file(path: Path, status: os.stat_result) -> None:
    if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise ModelCleanupJournalIntegrityError(
            "model_cleanup_journal_insecure_path"
        )
    os.chmod(path, 0o600, follow_symlinks=False)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback(connection: sqlite3.Connection | None) -> None:
    if connection is None:
        return
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        return


__all__ = [
    "CleanupConfigState",
    "CleanupJournalMutation",
    "CleanupKind",
    "CleanupPhase",
    "CleanupRecoveryAction",
    "CleanupRecoveryDecision",
    "CleanupTransaction",
    "DestinationState",
    "InstallLedgerState",
    "MANUAL_RECOVERY_CODES",
    "ModelCleanupJournal",
    "ModelCleanupJournalCapacityError",
    "ModelCleanupJournalConflictError",
    "ModelCleanupJournalError",
    "ModelCleanupJournalIntegrityError",
    "ModelCleanupJournalNotFoundError",
    "decide_cleanup_recovery",
]
