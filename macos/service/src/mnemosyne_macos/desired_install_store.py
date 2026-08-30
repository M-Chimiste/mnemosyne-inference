"""Durable, unactioned local journal for DesiredInstall v1.

Receipt records authority before any future executor may act.  This module has
no installer, configuration, storage, residency, profile, or network imports.
The first valid ``run`` receipt always waits for local approval; the only wire
revision accepted after it is the exact next ``cancel`` revision.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import ctypes
import ctypes.util
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import platform
import sqlite3
import stat
import threading
import time
from typing import Any, Final, TypeVar
import uuid

from .desired_install_protocol import (
    JOB_RESULT_CODES,
    JOB_STATES,
    MAX_JOB_ACKNOWLEDGEMENTS,
    TERMINAL_JOB_STATES,
    DesiredInstallDocument,
    DesiredInstallProtocolError,
    JobAcknowledgement,
    build_job_acknowledgement,
    desired_install_digest_equal,
    parse_desired_install,
    validate_desired_install,
)


_STORE_ID: Final[str] = "mnemosyne-macos-desired-install-journal-v1"
_STORE_SCHEMA_VERSION: Final[int] = 1
_MAX_GENERATION: Final[int] = 2_147_483_647
_MAX_TIMESTAMP: Final[float] = 4_102_444_800.0
_MAX_BYTES: Final[int] = 1_152_921_504_606_846_976
_RETIRED_FILTER_BYTES: Final[int] = 1024 * 1024
_RETIRED_FILTER_HASHES: Final[int] = 7
_RETIRED_FILTER_DOMAIN: Final[bytes] = b"mnemosyne-desired-install-retired-v1\x00"
_STATE_SQL = ",".join(f"'{state}'" for state in JOB_STATES)
_NONTERMINAL_STATES: Final[frozenset[str]] = frozenset(JOB_STATES) - (
    TERMINAL_JOB_STATES
)
_NORMAL_RESULT_STATES: Final[frozenset[str]] = frozenset(
    {
        "received",
        "accepted",
        "downloading",
        "verifying",
        "downloaded_unregistered",
        "registered",
        "completed",
    }
)
_REFUSAL_CODES: Final[frozenset[str]] = frozenset(
    {
        "local_policy_refused",
        "pairing_generation_changed",
        "inventory_basis_stale",
        "catalog_changed",
        "recipe_unknown",
        "artifact_mismatch",
        "storage_location_unknown",
        "storage_binding_changed",
        "storage_unavailable",
        "storage_read_only",
        "insufficient_storage",
        "runtime_unavailable",
        "idempotency_conflict",
    }
)
_CANCEL_CODES: Final[frozenset[str]] = frozenset(
    {"cancelled_by_hub", "cancelled_locally"}
)
_T = TypeVar("_T")


class DesiredInstallStoreError(RuntimeError):
    """A fixed-code local journal failure without document content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DesiredInstallConflictError(DesiredInstallStoreError):
    pass


class DesiredInstallIntegrityError(DesiredInstallStoreError):
    pass


class DesiredInstallNotFoundError(DesiredInstallStoreError):
    pass


@dataclass(frozen=True, slots=True)
class DesiredInstallRecord:
    document: DesiredInstallDocument
    state: str
    installation_id: str | None
    bytes_downloaded: int
    total_bytes: int | None
    updated_at: float
    result_code: str | None
    first_received_at: float
    current_received_at: float
    current_received_monotonic: float
    current_valid_until_monotonic: float
    terminal: bool

    def acknowledgement(self) -> JobAcknowledgement:
        return build_job_acknowledgement(
            job_id=self.document.job_id,
            job_revision=self.document.job_revision,
            installation_id=self.installation_id,
            state=self.state,
            bytes_downloaded=self.bytes_downloaded,
            total_bytes=self.total_bytes,
            updated_at=self.updated_at,
            result_code=self.result_code,
        )


@dataclass(frozen=True, slots=True)
class DesiredInstallReceipt:
    record: DesiredInstallRecord
    replayed: bool
    cancelled: bool


@dataclass(frozen=True, slots=True)
class DesiredInstallTransition:
    record: DesiredInstallRecord
    replayed: bool


@dataclass(frozen=True, slots=True)
class AcknowledgementPage:
    acknowledgements: tuple[JobAcknowledgement, ...]
    next_cursor: str | None
    total: int


class DesiredInstallStore:
    """Bounded, restart-safe journal with no remote-install execution powers."""

    def __init__(
        self,
        path: str | Path,
        *,
        maximum_jobs: int = 10_000,
        maximum_active_jobs: int = 256,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        boot_identity: Callable[[], str] | None = None,
    ) -> None:
        if not _is_integer_between(maximum_jobs, 1, 10_000):
            raise ValueError("invalid desired-install history limit")
        if not _is_integer_between(maximum_active_jobs, 1, 256):
            raise ValueError("invalid desired-install active limit")
        if maximum_active_jobs > maximum_jobs:
            raise ValueError("invalid desired-install active limit")
        self._path = Path(path).expanduser()
        self._maximum_jobs = maximum_jobs
        self._maximum_active_jobs = maximum_active_jobs
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._boot_identity_provider = boot_identity or _default_boot_identity
        self._lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._initialized = False
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    async def initialize(self) -> None:
        async with self._lock:
            if self._closed:
                raise DesiredInstallIntegrityError(
                    "desired_install_store_unavailable"
                )
            wall, monotonic, boot_id = self._clock_observation()
            await asyncio.to_thread(
                self._initialize_sync, wall, monotonic, boot_id
            )
            self._initialized = True

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._initialized = False

    async def receive(
        self,
        value: DesiredInstallDocument | dict[str, Any] | bytes | str,
        *,
        expected_pairing_id: str | None = None,
        expected_credential_generation: int | None = None,
        expected_inventory_instance_id: str | None = None,
        current_inventory_sequence: int | None = None,
    ) -> DesiredInstallReceipt:
        """Persist one authenticated receipt without starting any work.

        Optional expected fields let the future sync integration bind the
        document to its authenticated channel and exact inventory authority.
        They are checked before SQLite is touched.
        """

        document = _coerce_document(value)
        _assert_expected_authority(
            document,
            expected_pairing_id=expected_pairing_id,
            expected_credential_generation=expected_credential_generation,
            expected_inventory_instance_id=expected_inventory_instance_id,
            current_inventory_sequence=current_inventory_sequence,
        )
        self._require_ready()
        wall, monotonic, boot_id = self._clock_observation()
        return await self._run_sync(
            lambda: self._receive_sync(document, wall, monotonic, boot_id)
        )

    async def get(self, job_id: str) -> DesiredInstallRecord | None:
        self._require_ready()
        job_id = _canonical_uuid(job_id)
        wall, monotonic, boot_id = self._clock_observation()
        return await self._run_sync(
            lambda: self._get_sync(job_id, wall, monotonic, boot_id)
        )

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[tuple[DesiredInstallRecord, ...], int]:
        self._require_ready()
        if (
            not _is_integer_between(offset, 0, 10_000)
            or not _is_integer_between(limit, 1, 256)
        ):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        wall, monotonic, boot_id = self._clock_observation()
        return await self._run_sync(
            lambda: self._list_sync(
                offset, limit, wall, monotonic, boot_id
            )
        )

    async def list_reconcilable(
        self,
        *,
        offset: int = 0,
        limit: int = 256,
    ) -> tuple[tuple[DesiredInstallRecord, ...], int]:
        """Return only work that can still require local reconciliation.

        Nonterminal jobs sort before cancellation receipts so terminal UI
        history can never hide an active install.  A cancelled row remains in
        this bounded scan when it owns an installation ID: after a crash the
        executor must re-prove that its stop-only intent reached the durable
        installer before treating cancellation as settled.
        """

        self._require_ready()
        if (
            not _is_integer_between(offset, 0, 10_000)
            or not _is_integer_between(limit, 1, 256)
        ):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        wall, monotonic, boot_id = self._clock_observation()
        return await self._run_sync(
            lambda: self._list_reconcilable_sync(
                offset, limit, wall, monotonic, boot_id
            )
        )

    async def transition(
        self,
        *,
        job_id: str,
        job_revision: int,
        state: str,
        bytes_downloaded: int,
        total_bytes: int | None,
        result_code: str | None,
        installation_id: str | None = None,
    ) -> DesiredInstallTransition:
        """Record a local state observation; this method performs no work."""

        self._require_ready()
        job_id = _canonical_uuid(job_id)
        if not _is_integer_between(job_revision, 1, _MAX_GENERATION):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        if state not in JOB_STATES:
            raise DesiredInstallStoreError("desired_install_state_invalid")
        if not _is_integer_between(bytes_downloaded, 0, _MAX_BYTES):
            raise DesiredInstallStoreError("desired_install_progress_invalid")
        if total_bytes is not None and not _is_integer_between(
            total_bytes, 0, _MAX_BYTES
        ):
            raise DesiredInstallStoreError("desired_install_progress_invalid")
        if total_bytes is not None and bytes_downloaded > total_bytes:
            raise DesiredInstallStoreError("desired_install_progress_invalid")
        if installation_id is not None:
            installation_id = _canonical_uuid(installation_id)
        _assert_state_result(state, result_code)
        wall, monotonic, boot_id = self._clock_observation()
        return await self._run_sync(
            lambda: self._transition_sync(
                job_id=job_id,
                job_revision=job_revision,
                state=state,
                bytes_downloaded=bytes_downloaded,
                total_bytes=total_bytes,
                result_code=result_code,
                installation_id=installation_id,
                wall=wall,
                monotonic=monotonic,
                boot_id=boot_id,
            )
        )

    async def acknowledgements(
        self,
        *,
        limit: int = MAX_JOB_ACKNOWLEDGEMENTS,
    ) -> tuple[JobAcknowledgement, ...]:
        """Return active-first current acknowledgements, sorted canonically."""

        self._require_ready()
        if not _is_integer_between(limit, 1, MAX_JOB_ACKNOWLEDGEMENTS):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        page = await self.acknowledgement_page(limit=limit)
        return page.acknowledgements

    async def acknowledgement_page(
        self,
        *,
        after_job_id: str | None = None,
        limit: int = MAX_JOB_ACKNOWLEDGEMENTS,
    ) -> AcknowledgementPage:
        """Return a stable canonical page so bounded sync can rotate all jobs.

        Passing the returned cursor fetches the next lexicographic page.  A
        ``None`` cursor means the scan is complete; a caller starts another
        cycle from ``None`` to observe later state changes and newly inserted
        lower UUIDs.
        """

        self._require_ready()
        if after_job_id is not None:
            after_job_id = _canonical_uuid(after_job_id)
        if not _is_integer_between(limit, 1, MAX_JOB_ACKNOWLEDGEMENTS):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        wall, monotonic, boot_id = self._clock_observation()
        return await self._run_sync(
            lambda: self._acknowledgement_page_sync(
                after_job_id, limit, wall, monotonic, boot_id
            )
        )

    async def acknowledgement_values(
        self,
        *,
        limit: int = MAX_JOB_ACKNOWLEDGEMENTS,
    ) -> tuple[dict[str, Any], ...]:
        acknowledgements = await self.acknowledgements(limit=limit)
        return tuple(item.value for item in acknowledgements)

    def _require_ready(self) -> None:
        if self._closed or not self._initialized:
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            )

    async def _run_sync(self, operation: Callable[[], _T]) -> _T:
        async with self._lock:
            self._require_ready()
            worker = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                try:
                    await worker
                except BaseException:
                    pass
                raise

    def _clock_observation(self) -> tuple[float, float, str]:
        try:
            wall = float(self._wall_clock())
            monotonic = float(self._monotonic_clock())
            boot_id = _boot_digest(self._boot_identity_provider())
        except DesiredInstallStoreError:
            raise
        except BaseException:
            raise DesiredInstallStoreError(
                "desired_install_clock_unavailable"
            ) from None
        if (
            not math.isfinite(wall)
            or not 0 <= wall <= _MAX_TIMESTAMP
            or not math.isfinite(monotonic)
            or monotonic < 0
        ):
            raise DesiredInstallStoreError("desired_install_clock_unavailable")
        return wall, monotonic, boot_id

    def _initialize_sync(
        self, wall: float, monotonic: float, boot_id: str
    ) -> None:
        with self._state_lock:
            self._prepare_path()
            try:
                with self._connect() as connection:
                    check = connection.execute("PRAGMA quick_check").fetchone()
                    if check is None or str(check[0]) != "ok":
                        raise DesiredInstallIntegrityError(
                            "desired_install_store_corrupt"
                        )
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                        ).fetchall()
                    }
                    allowed = {
                        "native_desired_install_metadata_v1",
                        "native_desired_install_jobs_v1",
                        "native_desired_install_revisions_v1",
                        "native_desired_install_retired_v1",
                    }
                    new_store = not tables
                    if tables and tables != allowed:
                        raise DesiredInstallIntegrityError(
                            "desired_install_store_identity_mismatch"
                        )
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        self._create_schema(connection)
                        metadata = connection.execute(
                            """
                            SELECT schema_version, store_id
                              FROM native_desired_install_metadata_v1
                             WHERE singleton = 1
                            """
                        ).fetchone()
                        if metadata is None and new_store:
                            connection.execute(
                                """
                                INSERT INTO native_desired_install_metadata_v1 (
                                    singleton, schema_version, store_id
                                ) VALUES (1, ?, ?)
                                """,
                                (_STORE_SCHEMA_VERSION, _STORE_ID),
                            )
                        elif metadata is None:
                            raise DesiredInstallIntegrityError(
                                "desired_install_store_identity_mismatch"
                            )
                        elif (
                            int(metadata["schema_version"])
                            != _STORE_SCHEMA_VERSION
                            or str(metadata["store_id"]) != _STORE_ID
                        ):
                            raise DesiredInstallIntegrityError(
                                "desired_install_store_identity_mismatch"
                            )
                        retired = connection.execute(
                            """
                            SELECT filter_blob, filter_digest, retired_count
                              FROM native_desired_install_retired_v1
                             WHERE singleton = 1
                            """
                        ).fetchone()
                        if retired is None and new_store:
                            empty_filter = bytes(_RETIRED_FILTER_BYTES)
                            connection.execute(
                                """
                                INSERT INTO native_desired_install_retired_v1 (
                                    singleton, filter_blob, filter_digest,
                                    retired_count
                                ) VALUES (1, ?, ?, 0)
                                """,
                                (
                                    empty_filter,
                                    _blob_digest(empty_filter),
                                ),
                            )
                        elif retired is None:
                            raise DesiredInstallIntegrityError(
                                "desired_install_store_identity_mismatch"
                            )
                        else:
                            self._validate_retired_filter(retired)
                        user_version = int(
                            connection.execute("PRAGMA user_version").fetchone()[0]
                        )
                        if user_version == 0:
                            # Early v1 drafts did not stamp user_version.  The
                            # tables and identity above prove this narrow,
                            # data-preserving migration is safe.
                            connection.execute(
                                f"PRAGMA user_version={_STORE_SCHEMA_VERSION}"
                            )
                        elif user_version != _STORE_SCHEMA_VERSION:
                            raise DesiredInstallIntegrityError(
                                "desired_install_store_identity_mismatch"
                            )
                        foreign = connection.execute(
                            "PRAGMA foreign_key_check"
                        ).fetchall()
                        if foreign:
                            raise DesiredInstallIntegrityError(
                                "desired_install_store_corrupt"
                            )
                        self._validate_all_rows(connection)
                        self._expire_due_jobs(
                            connection,
                            wall=wall,
                            monotonic=monotonic,
                            boot_id=boot_id,
                        )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
            except DesiredInstallStoreError:
                raise
            except (OSError, sqlite3.DatabaseError):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_unavailable"
                ) from None

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS native_desired_install_metadata_v1 (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                store_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS native_desired_install_retired_v1 (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                filter_blob BLOB NOT NULL,
                filter_digest TEXT NOT NULL,
                retired_count INTEGER NOT NULL CHECK (retired_count >= 0)
            );
            CREATE TABLE IF NOT EXISTS native_desired_install_jobs_v1 (
                job_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                identity_digest TEXT NOT NULL,
                current_revision INTEGER NOT NULL CHECK (
                    current_revision >= 1 AND current_revision <= {_MAX_GENERATION}
                ),
                desired_state TEXT NOT NULL CHECK (
                    desired_state IN ('run', 'cancel')
                ),
                document_digest TEXT NOT NULL,
                document_json TEXT NOT NULL,
                first_received_at REAL NOT NULL,
                current_received_at REAL NOT NULL,
                current_received_monotonic REAL NOT NULL CHECK (
                    current_received_monotonic >= 0
                ),
                valid_until_monotonic REAL NOT NULL CHECK (
                    valid_until_monotonic >= current_received_monotonic
                ),
                receipt_boot_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ({_STATE_SQL})),
                installation_id TEXT,
                bytes_downloaded INTEGER NOT NULL CHECK (
                    bytes_downloaded >= 0 AND bytes_downloaded <= {_MAX_BYTES}
                ),
                total_bytes INTEGER CHECK (
                    total_bytes IS NULL OR
                    (total_bytes >= 0 AND total_bytes <= {_MAX_BYTES})
                ),
                updated_at REAL NOT NULL,
                result_code TEXT,
                acknowledgement_digest TEXT NOT NULL,
                acknowledgement_json TEXT NOT NULL,
                terminal INTEGER NOT NULL CHECK (terminal IN (0, 1))
            );
            CREATE INDEX IF NOT EXISTS native_desired_install_state_v1
                ON native_desired_install_jobs_v1(terminal, updated_at, job_id);
            CREATE TABLE IF NOT EXISTS native_desired_install_revisions_v1 (
                job_id TEXT NOT NULL REFERENCES native_desired_install_jobs_v1(job_id)
                    ON DELETE CASCADE,
                job_revision INTEGER NOT NULL CHECK (
                    job_revision >= 1 AND job_revision <= {_MAX_GENERATION}
                ),
                desired_state TEXT NOT NULL CHECK (
                    desired_state IN ('run', 'cancel')
                ),
                document_digest TEXT NOT NULL,
                document_json TEXT NOT NULL,
                received_at REAL NOT NULL,
                received_monotonic REAL NOT NULL CHECK (received_monotonic >= 0),
                valid_until_monotonic REAL NOT NULL CHECK (
                    valid_until_monotonic >= received_monotonic
                ),
                receipt_boot_id TEXT NOT NULL,
                PRIMARY KEY (job_id, job_revision)
            );
            """
        )

    def _receive_sync(
        self,
        document: DesiredInstallDocument,
        wall: float,
        monotonic: float,
        boot_id: str,
    ) -> DesiredInstallReceipt:
        with self._state_lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        self._expire_due_jobs(
                            connection,
                            wall=wall,
                            monotonic=monotonic,
                            boot_id=boot_id,
                        )
                        row = connection.execute(
                            "SELECT * FROM native_desired_install_jobs_v1 "
                            "WHERE job_id = ?",
                            (document.job_id,),
                        ).fetchone()
                        key_row = connection.execute(
                            "SELECT job_id FROM native_desired_install_jobs_v1 "
                            "WHERE idempotency_key = ?",
                            (document.idempotency_key,),
                        ).fetchone()
                        if row is None:
                            if self._retired_maybe_contains_any(
                                connection,
                                (
                                    ("job", document.job_id),
                                    (
                                        "idempotency",
                                        document.idempotency_key,
                                    ),
                                ),
                            ):
                                raise DesiredInstallConflictError(
                                    "desired_install_history_retired"
                                )
                            if key_row is not None:
                                raise DesiredInstallConflictError(
                                    "desired_install_idempotency_conflict"
                                )
                            if document.desired_state != "run":
                                raise DesiredInstallConflictError(
                                    "desired_install_cancel_unknown"
                                )
                            if document.job_revision != 1:
                                raise DesiredInstallConflictError(
                                    "desired_install_revision_conflict"
                                )
                            deadline = _receipt_deadline(
                                document, wall=wall, monotonic=monotonic
                            )
                            self._prune_terminal_for_capacity(
                                connection, reserve=1
                            )
                            total = int(
                                connection.execute(
                                    "SELECT COUNT(*) FROM "
                                    "native_desired_install_jobs_v1"
                                ).fetchone()[0]
                            )
                            if total >= self._maximum_jobs:
                                raise DesiredInstallConflictError(
                                    "desired_install_store_full"
                                )
                            active = int(
                                connection.execute(
                                    "SELECT COUNT(*) FROM "
                                    "native_desired_install_jobs_v1 "
                                    "WHERE terminal = 0"
                                ).fetchone()[0]
                            )
                            if active >= self._maximum_active_jobs:
                                raise DesiredInstallConflictError(
                                    "desired_install_active_limit_reached"
                                )
                            record = self._insert_run(
                                connection,
                                document=document,
                                wall=wall,
                                monotonic=monotonic,
                                boot_id=boot_id,
                                deadline=deadline,
                            )
                            connection.commit()
                            return DesiredInstallReceipt(
                                record=record,
                                replayed=False,
                                cancelled=False,
                            )

                        current = self._record_from_row(row)
                        if document.job_revision == current.document.job_revision:
                            if not desired_install_digest_equal(
                                document.payload_digest,
                                current.document.payload_digest,
                            ):
                                raise DesiredInstallConflictError(
                                    "desired_install_revision_conflict"
                                )
                            connection.commit()
                            return DesiredInstallReceipt(
                                record=current,
                                replayed=True,
                                cancelled=current.state == "cancelled",
                            )
                        if document.job_revision < current.document.job_revision:
                            raise DesiredInstallConflictError(
                                "desired_install_revision_stale"
                            )
                        if key_row is None or str(key_row["job_id"]) != document.job_id:
                            raise DesiredInstallConflictError(
                                "desired_install_idempotency_conflict"
                            )
                        if not desired_install_digest_equal(
                            document.identity_digest,
                            current.document.identity_digest,
                        ):
                            raise DesiredInstallConflictError(
                                "desired_install_idempotency_conflict"
                            )
                        if (
                            document.job_revision
                            != current.document.job_revision + 1
                            or document.desired_state != "cancel"
                            or current.document.desired_state != "run"
                        ):
                            raise DesiredInstallConflictError(
                                "desired_install_revision_conflict"
                            )
                        deadline = _receipt_deadline(
                            document, wall=wall, monotonic=monotonic
                        )
                        if current.terminal or current.state not in _NONTERMINAL_STATES:
                            raise DesiredInstallConflictError(
                                "desired_install_job_terminal"
                            )
                        record = self._apply_hub_cancel(
                            connection,
                            current=current,
                            document=document,
                            wall=wall,
                            monotonic=monotonic,
                            boot_id=boot_id,
                            deadline=deadline,
                        )
                        connection.commit()
                        return DesiredInstallReceipt(
                            record=record,
                            replayed=False,
                            cancelled=True,
                        )
                    except BaseException:
                        connection.rollback()
                        raise
            except DesiredInstallStoreError:
                raise
            except (OSError, sqlite3.DatabaseError):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_unavailable"
                ) from None

    def _insert_run(
        self,
        connection: sqlite3.Connection,
        *,
        document: DesiredInstallDocument,
        wall: float,
        monotonic: float,
        boot_id: str,
        deadline: float,
    ) -> DesiredInstallRecord:
        acknowledgement = build_job_acknowledgement(
            job_id=document.job_id,
            job_revision=document.job_revision,
            state="awaiting_local_approval",
            bytes_downloaded=0,
            total_bytes=None,
            updated_at=wall,
            result_code="local_approval_required",
        )
        text = document.canonical_json.decode("utf-8")
        ack_text = acknowledgement.canonical_json.decode("utf-8")
        connection.execute(
            """
            INSERT INTO native_desired_install_jobs_v1 (
                job_id, idempotency_key, identity_digest,
                current_revision, desired_state, document_digest,
                document_json, first_received_at, current_received_at,
                current_received_monotonic, valid_until_monotonic,
                receipt_boot_id, state, installation_id,
                bytes_downloaded, total_bytes, updated_at, result_code,
                acknowledgement_digest, acknowledgement_json, terminal
            ) VALUES (?, ?, ?, ?, 'run', ?, ?, ?, ?, ?, ?, ?,
                      'awaiting_local_approval', NULL, 0, NULL, ?,
                      'local_approval_required', ?, ?, 0)
            """,
            (
                document.job_id,
                document.idempotency_key,
                document.identity_digest,
                document.job_revision,
                document.payload_digest,
                text,
                wall,
                wall,
                monotonic,
                deadline,
                boot_id,
                wall,
                acknowledgement.payload_digest,
                ack_text,
            ),
        )
        self._insert_revision(
            connection,
            document=document,
            received_at=wall,
            received_monotonic=monotonic,
            valid_until_monotonic=deadline,
            boot_id=boot_id,
        )
        return self._job_record(connection, document.job_id)

    def _apply_hub_cancel(
        self,
        connection: sqlite3.Connection,
        *,
        current: DesiredInstallRecord,
        document: DesiredInstallDocument,
        wall: float,
        monotonic: float,
        boot_id: str,
        deadline: float,
    ) -> DesiredInstallRecord:
        updated_at = max(wall, current.updated_at)
        acknowledgement = build_job_acknowledgement(
            job_id=document.job_id,
            job_revision=document.job_revision,
            installation_id=current.installation_id,
            state="cancelled",
            bytes_downloaded=current.bytes_downloaded,
            total_bytes=current.total_bytes,
            updated_at=updated_at,
            result_code="cancelled_by_hub",
        )
        connection.execute(
            """
            UPDATE native_desired_install_jobs_v1 SET
                current_revision = ?, desired_state = 'cancel',
                document_digest = ?, document_json = ?,
                current_received_at = ?, current_received_monotonic = ?,
                valid_until_monotonic = ?, receipt_boot_id = ?,
                state = 'cancelled', updated_at = ?,
                result_code = 'cancelled_by_hub',
                acknowledgement_digest = ?, acknowledgement_json = ?,
                terminal = 1
            WHERE job_id = ?
            """,
            (
                document.job_revision,
                document.payload_digest,
                document.canonical_json.decode("utf-8"),
                wall,
                monotonic,
                deadline,
                boot_id,
                updated_at,
                acknowledgement.payload_digest,
                acknowledgement.canonical_json.decode("utf-8"),
                document.job_id,
            ),
        )
        self._insert_revision(
            connection,
            document=document,
            received_at=wall,
            received_monotonic=monotonic,
            valid_until_monotonic=deadline,
            boot_id=boot_id,
        )
        return self._job_record(connection, document.job_id)

    def _get_sync(
        self,
        job_id: str,
        wall: float,
        monotonic: float,
        boot_id: str,
    ) -> DesiredInstallRecord | None:
        with self._state_lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._expire_due_jobs(
                        connection,
                        wall=wall,
                        monotonic=monotonic,
                        boot_id=boot_id,
                    )
                    row = connection.execute(
                        "SELECT * FROM native_desired_install_jobs_v1 "
                        "WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    result = None if row is None else self._record_from_row(row)
                    connection.commit()
                    return result
            except DesiredInstallStoreError:
                raise
            except (OSError, sqlite3.DatabaseError):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_unavailable"
                ) from None

    def _list_sync(
        self,
        offset: int,
        limit: int,
        wall: float,
        monotonic: float,
        boot_id: str,
    ) -> tuple[tuple[DesiredInstallRecord, ...], int]:
        with self._state_lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._expire_due_jobs(
                        connection,
                        wall=wall,
                        monotonic=monotonic,
                        boot_id=boot_id,
                    )
                    total = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM native_desired_install_jobs_v1"
                        ).fetchone()[0]
                    )
                    rows = connection.execute(
                        """
                        SELECT * FROM native_desired_install_jobs_v1
                        ORDER BY terminal ASC, first_received_at DESC, job_id
                        LIMIT ? OFFSET ?
                        """,
                        (limit, offset),
                    ).fetchall()
                    result = tuple(self._record_from_row(row) for row in rows)
                    connection.commit()
                    return result, total
            except DesiredInstallStoreError:
                raise
            except (OSError, sqlite3.DatabaseError):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_unavailable"
                ) from None

    def _list_reconcilable_sync(
        self,
        offset: int,
        limit: int,
        wall: float,
        monotonic: float,
        boot_id: str,
    ) -> tuple[tuple[DesiredInstallRecord, ...], int]:
        with self._state_lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._expire_due_jobs(
                        connection,
                        wall=wall,
                        monotonic=monotonic,
                        boot_id=boot_id,
                    )
                    predicate = (
                        "terminal = 0 OR "
                        "(state = 'cancelled' AND installation_id IS NOT NULL)"
                    )
                    total = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM "
                            "native_desired_install_jobs_v1 WHERE "
                            + predicate
                        ).fetchone()[0]
                    )
                    rows = connection.execute(
                        f"""
                        SELECT * FROM native_desired_install_jobs_v1
                        WHERE {predicate}
                        ORDER BY terminal ASC, first_received_at DESC, job_id
                        LIMIT ? OFFSET ?
                        """,
                        (limit, offset),
                    ).fetchall()
                    result = tuple(self._record_from_row(row) for row in rows)
                    connection.commit()
                    return result, total
            except DesiredInstallStoreError:
                raise
            except (OSError, sqlite3.DatabaseError):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_unavailable"
                ) from None

    def _transition_sync(
        self,
        *,
        job_id: str,
        job_revision: int,
        state: str,
        bytes_downloaded: int,
        total_bytes: int | None,
        result_code: str | None,
        installation_id: str | None,
        wall: float,
        monotonic: float,
        boot_id: str,
    ) -> DesiredInstallTransition:
        with self._state_lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._expire_due_jobs(
                        connection,
                        wall=wall,
                        monotonic=monotonic,
                        boot_id=boot_id,
                    )
                    row = connection.execute(
                        "SELECT * FROM native_desired_install_jobs_v1 "
                        "WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    if row is None:
                        raise DesiredInstallNotFoundError(
                            "desired_install_job_unknown"
                        )
                    current = self._record_from_row(row)
                    if current.document.job_revision != job_revision:
                        raise DesiredInstallConflictError(
                            "desired_install_revision_changed"
                        )
                    if current.document.desired_state != "run":
                        raise DesiredInstallConflictError(
                            "desired_install_job_terminal"
                        )
                    candidate_installation_id = (
                        current.installation_id
                        if installation_id is None
                        else installation_id
                    )
                    if current.terminal:
                        if (
                            current.state == state
                            and current.installation_id
                            == candidate_installation_id
                            and current.bytes_downloaded == bytes_downloaded
                            and current.total_bytes == total_bytes
                            and current.result_code == result_code
                        ):
                            connection.commit()
                            return DesiredInstallTransition(current, replayed=True)
                        raise DesiredInstallConflictError(
                            "desired_install_job_terminal"
                        )
                    if (
                        current.installation_id is not None
                        and candidate_installation_id != current.installation_id
                    ):
                        raise DesiredInstallConflictError(
                            "desired_install_installation_changed"
                        )
                    if bytes_downloaded < current.bytes_downloaded:
                        raise DesiredInstallConflictError(
                            "desired_install_progress_conflict"
                        )
                    if (
                        current.total_bytes is not None
                        and total_bytes != current.total_bytes
                    ):
                        raise DesiredInstallConflictError(
                            "desired_install_progress_conflict"
                        )
                    if not _allowed_transition(current.state, state):
                        raise DesiredInstallConflictError(
                            "desired_install_state_conflict"
                        )
                    if (
                        current.state == state
                        and current.installation_id == candidate_installation_id
                        and current.bytes_downloaded == bytes_downloaded
                        and current.total_bytes == total_bytes
                        and current.result_code == result_code
                    ):
                        connection.commit()
                        return DesiredInstallTransition(current, replayed=True)
                    updated_at = max(wall, current.updated_at)
                    candidate = build_job_acknowledgement(
                        job_id=job_id,
                        job_revision=job_revision,
                        installation_id=candidate_installation_id,
                        state=state,
                        bytes_downloaded=bytes_downloaded,
                        total_bytes=total_bytes,
                        updated_at=updated_at,
                        result_code=result_code,
                    )
                    previous = current.acknowledgement()
                    if desired_install_digest_equal(
                        candidate.payload_digest, previous.payload_digest
                    ):
                        connection.commit()
                        return DesiredInstallTransition(current, replayed=True)
                    terminal = int(state in TERMINAL_JOB_STATES)
                    connection.execute(
                        """
                        UPDATE native_desired_install_jobs_v1 SET
                            state = ?, installation_id = ?,
                            bytes_downloaded = ?, total_bytes = ?,
                            updated_at = ?, result_code = ?,
                            acknowledgement_digest = ?,
                            acknowledgement_json = ?, terminal = ?
                        WHERE job_id = ?
                        """,
                        (
                            state,
                            candidate_installation_id,
                            bytes_downloaded,
                            total_bytes,
                            updated_at,
                            result_code,
                            candidate.payload_digest,
                            candidate.canonical_json.decode("utf-8"),
                            terminal,
                            job_id,
                        ),
                    )
                    result = self._job_record(connection, job_id)
                    connection.commit()
                    return DesiredInstallTransition(result, replayed=False)
            except DesiredInstallStoreError:
                raise
            except (OSError, sqlite3.DatabaseError):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_unavailable"
                ) from None

    def _acknowledgement_page_sync(
        self,
        after_job_id: str | None,
        limit: int,
        wall: float,
        monotonic: float,
        boot_id: str,
    ) -> AcknowledgementPage:
        with self._state_lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._expire_due_jobs(
                        connection,
                        wall=wall,
                        monotonic=monotonic,
                        boot_id=boot_id,
                    )
                    total = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM native_desired_install_jobs_v1"
                        ).fetchone()[0]
                    )
                    if after_job_id is None:
                        rows = connection.execute(
                            """
                            SELECT * FROM native_desired_install_jobs_v1
                            ORDER BY job_id ASC LIMIT ?
                            """,
                            (limit + 1,),
                        ).fetchall()
                    else:
                        rows = connection.execute(
                            """
                            SELECT * FROM native_desired_install_jobs_v1
                            WHERE job_id > ? ORDER BY job_id ASC LIMIT ?
                            """,
                            (after_job_id, limit + 1),
                        ).fetchall()
                    has_more = len(rows) > limit
                    records = [
                        self._record_from_row(row) for row in rows[:limit]
                    ]
                    connection.commit()
                return AcknowledgementPage(
                    acknowledgements=tuple(
                        record.acknowledgement() for record in records
                    ),
                    next_cursor=(
                        records[-1].document.job_id
                        if has_more and records
                        else None
                    ),
                    total=total,
                )
            except DesiredInstallStoreError:
                raise
            except (OSError, sqlite3.DatabaseError):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_unavailable"
                ) from None

    def _expire_due_jobs(
        self,
        connection: sqlite3.Connection,
        *,
        wall: float,
        monotonic: float,
        boot_id: str,
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM native_desired_install_jobs_v1 WHERE terminal = 0"
        ).fetchall()
        for row in rows:
            record = self._record_from_row(row)
            expired = (
                str(row["receipt_boot_id"]) != boot_id
                or monotonic >= float(row["valid_until_monotonic"])
            )
            if not expired:
                continue
            updated_at = max(wall, record.updated_at)
            acknowledgement = build_job_acknowledgement(
                job_id=record.document.job_id,
                job_revision=record.document.job_revision,
                installation_id=record.installation_id,
                state="refused",
                bytes_downloaded=record.bytes_downloaded,
                total_bytes=record.total_bytes,
                updated_at=updated_at,
                result_code="inventory_basis_stale",
            )
            connection.execute(
                """
                UPDATE native_desired_install_jobs_v1 SET
                    state = 'refused', updated_at = ?,
                    result_code = 'inventory_basis_stale',
                    acknowledgement_digest = ?, acknowledgement_json = ?,
                    terminal = 1
                WHERE job_id = ? AND terminal = 0
                """,
                (
                    updated_at,
                    acknowledgement.payload_digest,
                    acknowledgement.canonical_json.decode("utf-8"),
                    record.document.job_id,
                ),
            )

    def _prune_terminal_for_capacity(
        self, connection: sqlite3.Connection, *, reserve: int
    ) -> None:
        """Retire only the oldest terminal history under hard capacity pressure."""

        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM native_desired_install_jobs_v1"
            ).fetchone()[0]
        )
        excess = max(0, total + reserve - self._maximum_jobs)
        if excess == 0:
            return
        rows = connection.execute(
            """
            SELECT job_id, idempotency_key
              FROM native_desired_install_jobs_v1
             WHERE terminal = 1
             ORDER BY updated_at ASC, job_id ASC
             LIMIT ?
            """,
            (excess,),
        ).fetchall()
        if not rows:
            return
        retired = connection.execute(
            """
            SELECT filter_blob, filter_digest, retired_count
              FROM native_desired_install_retired_v1 WHERE singleton = 1
            """
        ).fetchone()
        filter_bytes, retired_count = self._validate_retired_filter(retired)
        mutable = bytearray(filter_bytes)
        for row in rows:
            _retired_filter_add(mutable, "job", str(row["job_id"]))
            _retired_filter_add(
                mutable, "idempotency", str(row["idempotency_key"])
            )
        updated = bytes(mutable)
        connection.execute(
            """
            UPDATE native_desired_install_retired_v1
               SET filter_blob = ?, filter_digest = ?, retired_count = ?
             WHERE singleton = 1
            """,
            (
                updated,
                _blob_digest(updated),
                min(9_007_199_254_740_991, retired_count + len(rows)),
            ),
        )
        connection.executemany(
            "DELETE FROM native_desired_install_jobs_v1 WHERE job_id = ?",
            ((str(row["job_id"]),) for row in rows),
        )

    def _validate_retired_filter(
        self, row: sqlite3.Row | None
    ) -> tuple[bytes, int]:
        try:
            if row is None:
                raise ValueError
            value = bytes(row["filter_blob"])
            digest = str(row["filter_digest"])
            count = int(row["retired_count"])
            if (
                len(value) != _RETIRED_FILTER_BYTES
                or not desired_install_digest_equal(
                    _blob_digest(value), digest
                )
                or not 0 <= count <= 9_007_199_254_740_991
            ):
                raise ValueError
            return value, count
        except (KeyError, TypeError, ValueError, OverflowError):
            raise DesiredInstallIntegrityError(
                "desired_install_store_corrupt"
            ) from None

    def _retired_maybe_contains_any(
        self,
        connection: sqlite3.Connection,
        identities: tuple[tuple[str, str], ...],
    ) -> bool:
        row = connection.execute(
            """
            SELECT filter_blob, filter_digest, retired_count
              FROM native_desired_install_retired_v1 WHERE singleton = 1
            """
        ).fetchone()
        filter_bytes, _count = self._validate_retired_filter(row)
        return any(
            _retired_filter_contains(filter_bytes, kind, value)
            for kind, value in identities
        )

    def _insert_revision(
        self,
        connection: sqlite3.Connection,
        *,
        document: DesiredInstallDocument,
        received_at: float,
        received_monotonic: float,
        valid_until_monotonic: float,
        boot_id: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO native_desired_install_revisions_v1 (
                job_id, job_revision, desired_state, document_digest,
                document_json, received_at, received_monotonic,
                valid_until_monotonic, receipt_boot_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document.job_id,
                document.job_revision,
                document.desired_state,
                document.payload_digest,
                document.canonical_json.decode("utf-8"),
                received_at,
                received_monotonic,
                valid_until_monotonic,
                boot_id,
            ),
        )

    def _validate_all_rows(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT * FROM native_desired_install_jobs_v1"
        ).fetchall()
        if len(rows) > self._maximum_jobs:
            raise DesiredInstallIntegrityError("desired_install_store_corrupt")
        for row in rows:
            current = self._record_from_row(row)
            revisions = connection.execute(
                """
                SELECT * FROM native_desired_install_revisions_v1
                WHERE job_id = ? ORDER BY job_revision
                """,
                (current.document.job_id,),
            ).fetchall()
            if not 1 <= len(revisions) <= 2:
                raise DesiredInstallIntegrityError(
                    "desired_install_store_corrupt"
                )
            previous: DesiredInstallDocument | None = None
            for revision in revisions:
                document = self._document_from_json(
                    str(revision["document_json"]),
                    str(revision["document_digest"]),
                )
                if (
                    document.job_id != current.document.job_id
                    or int(revision["job_revision"]) != document.job_revision
                    or str(revision["desired_state"]) != document.desired_state
                    or not desired_install_digest_equal(
                        document.identity_digest,
                        current.document.identity_digest,
                    )
                    or float(revision["received_monotonic"]) < 0
                    or float(revision["valid_until_monotonic"])
                    < float(revision["received_monotonic"])
                    or _boot_digest(str(revision["receipt_boot_id"]))
                    != str(revision["receipt_boot_id"])
                ):
                    raise DesiredInstallIntegrityError(
                        "desired_install_store_corrupt"
                    )
                if previous is None:
                    if document.desired_state != "run":
                        raise DesiredInstallIntegrityError(
                            "desired_install_store_corrupt"
                        )
                elif (
                    document.job_revision != previous.job_revision + 1
                    or previous.desired_state != "run"
                    or document.desired_state != "cancel"
                ):
                    raise DesiredInstallIntegrityError(
                        "desired_install_store_corrupt"
                    )
                previous = document
            if previous is None or previous.payload_digest != current.document.payload_digest:
                raise DesiredInstallIntegrityError(
                    "desired_install_store_corrupt"
                )
            latest = revisions[-1]
            earliest = revisions[0]
            if (
                float(row["first_received_at"])
                != float(earliest["received_at"])
                or float(row["current_received_at"])
                != float(latest["received_at"])
                or float(row["current_received_monotonic"])
                != float(latest["received_monotonic"])
                or float(row["valid_until_monotonic"])
                != float(latest["valid_until_monotonic"])
                or str(row["receipt_boot_id"])
                != str(latest["receipt_boot_id"])
            ):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_corrupt"
                )

    def _record_from_row(self, row: sqlite3.Row) -> DesiredInstallRecord:
        document = self._document_from_json(
            str(row["document_json"]), str(row["document_digest"])
        )
        try:
            indexed = (
                str(row["job_id"]),
                str(row["idempotency_key"]),
                str(row["identity_digest"]),
                int(row["current_revision"]),
                str(row["desired_state"]),
            )
            expected = (
                document.job_id,
                document.idempotency_key,
                document.identity_digest,
                document.job_revision,
                document.desired_state,
            )
            state = str(row["state"])
            installation_id = (
                None
                if row["installation_id"] is None
                else _canonical_uuid(str(row["installation_id"]))
            )
            downloaded = int(row["bytes_downloaded"])
            total = (
                None if row["total_bytes"] is None else int(row["total_bytes"])
            )
            updated_at = _timestamp(float(row["updated_at"]))
            result_code = row["result_code"]
            _assert_state_result(state, result_code)
            if (
                indexed != expected
                or not _is_integer_between(downloaded, 0, _MAX_BYTES)
                or (
                    total is not None
                    and (
                        not _is_integer_between(total, 0, _MAX_BYTES)
                        or downloaded > total
                    )
                )
                or int(row["terminal"])
                != int(state in TERMINAL_JOB_STATES)
                or _boot_digest(str(row["receipt_boot_id"]))
                != str(row["receipt_boot_id"])
            ):
                raise ValueError
            acknowledgement = build_job_acknowledgement(
                job_id=document.job_id,
                job_revision=document.job_revision,
                installation_id=installation_id,
                state=state,
                bytes_downloaded=downloaded,
                total_bytes=total,
                updated_at=updated_at,
                result_code=result_code,
            )
            if (
                not desired_install_digest_equal(
                    acknowledgement.payload_digest,
                    str(row["acknowledgement_digest"]),
                )
                or acknowledgement.canonical_json.decode("utf-8")
                != str(row["acknowledgement_json"])
            ):
                raise ValueError
            first_received = _timestamp(float(row["first_received_at"]))
            current_received = _timestamp(float(row["current_received_at"]))
            received_monotonic = float(row["current_received_monotonic"])
            valid_until = float(row["valid_until_monotonic"])
            if (
                not math.isfinite(received_monotonic)
                or received_monotonic < 0
                or not math.isfinite(valid_until)
                or valid_until < received_monotonic
            ):
                raise ValueError
        except (DesiredInstallProtocolError, DesiredInstallStoreError):
            raise DesiredInstallIntegrityError(
                "desired_install_store_corrupt"
            ) from None
        except (KeyError, TypeError, ValueError, OverflowError):
            raise DesiredInstallIntegrityError(
                "desired_install_store_corrupt"
            ) from None
        return DesiredInstallRecord(
            document=document,
            state=state,
            installation_id=installation_id,
            bytes_downloaded=downloaded,
            total_bytes=total,
            updated_at=updated_at,
            result_code=result_code,
            first_received_at=first_received,
            current_received_at=current_received,
            current_received_monotonic=received_monotonic,
            current_valid_until_monotonic=valid_until,
            terminal=bool(row["terminal"]),
        )

    def _document_from_json(
        self, raw: str, expected_digest: str
    ) -> DesiredInstallDocument:
        try:
            encoded = raw.encode("utf-8")
            document = parse_desired_install(encoded)
        except (UnicodeError, DesiredInstallProtocolError):
            raise DesiredInstallIntegrityError(
                "desired_install_store_corrupt"
            ) from None
        if (
            encoded != document.canonical_json
            or not desired_install_digest_equal(
                document.payload_digest, expected_digest
            )
        ):
            raise DesiredInstallIntegrityError(
                "desired_install_store_corrupt"
            )
        return document

    def _job_record(
        self, connection: sqlite3.Connection, job_id: str
    ) -> DesiredInstallRecord:
        row = connection.execute(
            "SELECT * FROM native_desired_install_jobs_v1 WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise DesiredInstallIntegrityError("desired_install_store_corrupt")
        return self._record_from_row(row)

    def _prepare_path(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            parent = self._path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_insecure_path"
                )
            os.chmod(self._path.parent, 0o700, follow_symlinks=False)
            try:
                status = self._path.lstat()
            except FileNotFoundError:
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self._path, flags, 0o600)
                os.close(descriptor)
                status = self._path.lstat()
            if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_insecure_path"
                )
            os.chmod(self._path, 0o600, follow_symlinks=False)
        except DesiredInstallStoreError:
            raise
        except OSError:
            raise DesiredInstallIntegrityError(
                "desired_install_store_insecure_path"
            ) from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._path), timeout=10, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA journal_mode=DELETE")
        return connection


def _coerce_document(
    value: DesiredInstallDocument | dict[str, Any] | bytes | str,
) -> DesiredInstallDocument:
    if isinstance(value, DesiredInstallDocument):
        return validate_desired_install(value.value)
    if isinstance(value, (bytes, str)):
        return parse_desired_install(value)
    return validate_desired_install(value)


def _assert_expected_authority(
    document: DesiredInstallDocument,
    *,
    expected_pairing_id: str | None,
    expected_credential_generation: int | None,
    expected_inventory_instance_id: str | None,
    current_inventory_sequence: int | None,
) -> None:
    if expected_pairing_id is not None:
        expected_pairing_id = _canonical_uuid(expected_pairing_id)
        if document.pairing_id != expected_pairing_id:
            raise DesiredInstallConflictError(
                "desired_install_recipient_mismatch"
            )
    if expected_credential_generation is not None:
        if not _is_integer_between(
            expected_credential_generation, 1, _MAX_GENERATION
        ):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        if document.credential_generation != expected_credential_generation:
            raise DesiredInstallConflictError(
                "desired_install_recipient_mismatch"
            )
    if expected_inventory_instance_id is not None:
        expected_inventory_instance_id = _canonical_uuid(
            expected_inventory_instance_id
        )
        if document.inventory_instance_id != expected_inventory_instance_id:
            raise DesiredInstallConflictError(
                "desired_install_inventory_basis_mismatch"
            )
    if current_inventory_sequence is not None:
        if not _is_integer_between(
            current_inventory_sequence, 0, 9_007_199_254_740_991
        ):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        if document.inventory_sequence > current_inventory_sequence:
            raise DesiredInstallConflictError(
                "desired_install_inventory_basis_mismatch"
            )


def _receipt_deadline(
    document: DesiredInstallDocument,
    *,
    wall: float,
    monotonic: float,
) -> float:
    """Translate remaining signed wall lifetime into non-extendable monotonic TTL."""

    remaining = min(
        float(document.valid_for_seconds),
        float(document.expires_at) - wall,
    )
    if not math.isfinite(remaining) or remaining <= 0:
        raise DesiredInstallConflictError("desired_install_expired")
    deadline = monotonic + remaining
    if not math.isfinite(deadline) or deadline < monotonic:
        raise DesiredInstallStoreError("desired_install_clock_unavailable")
    return deadline


def _assert_state_result(state: str, result_code: str | None) -> None:
    if result_code not in JOB_RESULT_CODES:
        raise DesiredInstallStoreError("desired_install_result_invalid")
    valid = False
    if state == "awaiting_local_approval":
        valid = result_code == "local_approval_required"
    elif state in _NORMAL_RESULT_STATES:
        valid = result_code is None
    elif state == "refused":
        valid = result_code in _REFUSAL_CODES
    elif state == "cancelled":
        valid = result_code in _CANCEL_CODES
    elif state == "failed":
        valid = result_code not in (
            {None, "local_approval_required"} | _CANCEL_CODES
        )
    if not valid:
        raise DesiredInstallStoreError("desired_install_result_invalid")


def _allowed_transition(current: str, candidate: str) -> bool:
    if current == candidate:
        return True
    if candidate in {"refused", "cancelled", "failed"}:
        return current in _NONTERMINAL_STATES
    return candidate in {
        "received": {"awaiting_local_approval"},
        "awaiting_local_approval": {"accepted"},
        "accepted": {"downloading"},
        "downloading": {"verifying"},
        "verifying": {"downloaded_unregistered"},
        "downloaded_unregistered": {"registered"},
        "registered": {"completed"},
    }.get(current, set())


def _canonical_uuid(value: object) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise DesiredInstallStoreError("desired_install_invalid_request") from None
    return value


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DesiredInstallStoreError("desired_install_store_corrupt")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= _MAX_TIMESTAMP:
        raise DesiredInstallStoreError("desired_install_store_corrupt")
    return result


def _is_integer_between(value: object, minimum: int, maximum: int) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and minimum <= value <= maximum
    )


def _boot_digest(value: object) -> str:
    if (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
    ):
        try:
            decoded = bytes.fromhex(value[7:])
            if value == "sha256:" + decoded.hex():
                return value
        except ValueError:
            pass
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise DesiredInstallStoreError("desired_install_clock_unavailable")
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _blob_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _retired_positions(kind: str, value: str) -> tuple[int, ...]:
    encoded = (
        _RETIRED_FILTER_DOMAIN
        + kind.encode("ascii")
        + b"\x00"
        + value.encode("ascii")
    )
    digest = hashlib.sha256(encoded).digest()
    first = int.from_bytes(digest[:8], "big")
    second = int.from_bytes(digest[8:16], "big") | 1
    bit_count = _RETIRED_FILTER_BYTES * 8
    return tuple(
        (first + index * second + index * index) % bit_count
        for index in range(_RETIRED_FILTER_HASHES)
    )


def _retired_filter_add(target: bytearray, kind: str, value: str) -> None:
    for position in _retired_positions(kind, value):
        target[position // 8] |= 1 << (position % 8)


def _retired_filter_contains(target: bytes, kind: str, value: str) -> bool:
    return all(
        target[position // 8] & (1 << (position % 8))
        for position in _retired_positions(kind, value)
    )


def _default_boot_identity() -> str:
    linux_boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        if linux_boot_id.is_file():
            value = linux_boot_id.read_text(encoding="ascii").strip()
            if value:
                return value
    except OSError:
        pass
    if platform.system() == "Darwin":
        try:
            library = ctypes.util.find_library("c")
            libc = ctypes.CDLL(library or None, use_errno=True)
            sysctlbyname = libc.sysctlbyname
            sysctlbyname.argtypes = (
                ctypes.c_char_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.c_void_p,
                ctypes.c_size_t,
            )
            sysctlbyname.restype = ctypes.c_int
            size = ctypes.c_size_t()
            name = b"kern.bootsessionuuid"
            if sysctlbyname(name, None, ctypes.byref(size), None, 0) != 0:
                raise OSError
            if not 1 <= size.value <= 4096:
                raise OSError
            buffer = ctypes.create_string_buffer(size.value)
            if (
                sysctlbyname(
                    name, buffer, ctypes.byref(size), None, 0
                )
                != 0
            ):
                raise OSError
            value = buffer.value.decode("ascii").strip()
            if value:
                return value
        except (OSError, AttributeError, UnicodeError, ValueError):
            pass
    # ``wall - monotonic`` estimates the boot epoch and is stable across a
    # process restart. A wall-clock adjustment changes it and therefore fails
    # closed by expiring outstanding work. Second precision avoids extending
    # authority across an ordinary reboot while tolerating measurement jitter.
    return f"boot-epoch:{int(round(time.time() - time.monotonic()))}"


__all__ = [
    "AcknowledgementPage",
    "DesiredInstallConflictError",
    "DesiredInstallIntegrityError",
    "DesiredInstallNotFoundError",
    "DesiredInstallReceipt",
    "DesiredInstallRecord",
    "DesiredInstallStore",
    "DesiredInstallStoreError",
    "DesiredInstallTransition",
]
