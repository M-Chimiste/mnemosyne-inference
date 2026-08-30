from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeVar

from .desired_install_protocol import (
    ACK_STATES,
    DesiredInstallDocument,
    DesiredInstallProtocolError,
    JobAcknowledgement,
    desired_install_digest_equal,
    validate_desired_install,
    validate_job_acknowledgement,
)


_SCHEMA_VERSION: Final[int] = 1
_STORE_ID: Final[str] = "mnemosyne-fleet-desired-install-journal-v1"
_TERMINAL_ACK_STATES: Final[frozenset[str]] = frozenset(
    {"completed", "refused", "cancelled", "failed"}
)
_ACK_ORDER: Final[dict[str, int]] = {
    state: index for index, state in enumerate(ACK_STATES)
}
_T = TypeVar("_T")


class DesiredInstallStoreError(RuntimeError):
    """A fixed-code journal failure without job or machine details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DesiredInstallConflictError(DesiredInstallStoreError):
    pass


class DesiredInstallNotFoundError(DesiredInstallStoreError):
    pass


class DesiredInstallIntegrityError(DesiredInstallStoreError):
    pass


@dataclass(frozen=True, slots=True)
class DesiredInstallRecord:
    document: DesiredInstallDocument
    intent_digest: str
    intent: dict[str, Any]
    first_created_at: float
    delivery_count: int
    last_delivered_revision: int | None
    last_delivered_at: float | None
    acknowledgement: JobAcknowledgement | None

    @property
    def terminal(self) -> bool:
        return (
            self.acknowledgement is not None
            and self.acknowledgement.job_revision == self.document.job_revision
            and self.acknowledgement.state in _TERMINAL_ACK_STATES
        )


@dataclass(frozen=True, slots=True)
class DesiredInstallCreation:
    record: DesiredInstallRecord
    replayed: bool


@dataclass(frozen=True, slots=True)
class AcknowledgementAcceptance:
    record: DesiredInstallRecord | None
    replayed: bool
    stale_revision: bool
    retired_unknown: bool = False


class DesiredInstallStore:
    """Private, bounded DesiredInstall authority independent of routing state."""

    def __init__(
        self,
        path: Path,
        *,
        maximum_active_jobs: int = 1_000,
        history_limit: int = 10_000,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or path in {Path("/"), Path.home()}
            or path.parent in {Path("/"), Path.home()}
        ):
            raise ValueError("desired install database path is unsafe")
        if (
            isinstance(maximum_active_jobs, bool)
            or not isinstance(maximum_active_jobs, int)
            or not 1 <= maximum_active_jobs <= 10_000
        ):
            raise ValueError("desired install active limit is invalid")
        if (
            isinstance(history_limit, bool)
            or not isinstance(history_limit, int)
            or not 1 <= history_limit <= 100_000
        ):
            raise ValueError("desired install history limit is invalid")
        self._path = path
        self._maximum_active_jobs = maximum_active_jobs
        self._history_limit = history_limit
        self._wall_clock = wall_clock
        self._io_lock = asyncio.Lock()
        self._initialized = False

    @property
    def path(self) -> Path:
        return self._path

    async def initialize(self) -> None:
        async with self._io_lock:
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    async def find_idempotent(
        self,
        *,
        idempotency_key: str,
        intent_digest: str,
    ) -> DesiredInstallRecord | None:
        self._require_initialized()
        _canonical_uuid(idempotency_key)
        _sha256(intent_digest)
        return await self._run_sync(
            lambda: self._find_idempotent_sync(idempotency_key, intent_digest)
        )

    async def create(
        self,
        document: DesiredInstallDocument,
        *,
        intent_digest: str,
        intent: dict[str, Any],
    ) -> DesiredInstallCreation:
        self._require_initialized()
        document = validate_desired_install(document.value)
        _sha256(intent_digest)
        intent_json = _canonical_intent(intent, intent_digest)
        return await self._run_sync(
            lambda: self._create_sync(
                document,
                intent_digest=intent_digest,
                intent_json=intent_json,
            )
        )

    async def get(self, job_id: str) -> DesiredInstallRecord | None:
        self._require_initialized()
        _canonical_uuid(job_id)
        return await self._run_sync(lambda: self._get_sync(job_id))

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[tuple[DesiredInstallRecord, ...], int]:
        self._require_initialized()
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or offset > 100_000
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        return await self._run_sync(lambda: self._list_sync(offset, limit))

    async def cancel(
        self,
        job_id: str,
        *,
        expected_revision: int,
        issued_at: float,
        valid_for_seconds: int,
    ) -> DesiredInstallRecord:
        self._require_initialized()
        _canonical_uuid(job_id)
        _timestamp(issued_at)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or not 1 <= expected_revision <= 2_147_483_647
            or isinstance(valid_for_seconds, bool)
            or not isinstance(valid_for_seconds, int)
            or not 1 <= valid_for_seconds <= 604_800
        ):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        return await self._run_sync(
            lambda: self._cancel_sync(
                job_id,
                expected_revision=expected_revision,
                issued_at=float(issued_at),
                valid_for_seconds=valid_for_seconds,
            )
        )

    async def accept_acknowledgements(
        self,
        *,
        pairing_id: str,
        credential_generation: int,
        acknowledgements: list[dict[str, Any]],
    ) -> tuple[AcknowledgementAcceptance, ...]:
        self._require_initialized()
        _canonical_uuid(pairing_id)
        if (
            isinstance(credential_generation, bool)
            or not isinstance(credential_generation, int)
            or not 1 <= credential_generation <= 2_147_483_647
            or not isinstance(acknowledgements, list)
            or len(acknowledgements) > 256
        ):
            raise DesiredInstallStoreError("desired_install_ack_invalid")
        try:
            documents = tuple(
                validate_job_acknowledgement(value)
                for value in acknowledgements
            )
        except DesiredInstallProtocolError as error:
            raise DesiredInstallStoreError(error.code) from None
        return await self._run_sync(
            lambda: self._accept_acknowledgements_sync(
                pairing_id=pairing_id,
                credential_generation=credential_generation,
                acknowledgements=documents,
            )
        )

    async def pending_for_delivery(
        self,
        *,
        pairing_id: str,
        credential_generation: int,
        inventory_instance_id: str,
        inventory_sequence: int,
        limit: int = 64,
    ) -> tuple[DesiredInstallRecord, ...]:
        self._require_initialized()
        _canonical_uuid(pairing_id)
        _canonical_uuid(inventory_instance_id)
        if (
            isinstance(credential_generation, bool)
            or not isinstance(credential_generation, int)
            or not 1 <= credential_generation <= 2_147_483_647
            or isinstance(inventory_sequence, bool)
            or not isinstance(inventory_sequence, int)
            or not 0 <= inventory_sequence <= 9_007_199_254_740_991
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 256
        ):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        now = self._now()
        return await self._run_sync(
            lambda: self._pending_for_delivery_sync(
                pairing_id=pairing_id,
                credential_generation=credential_generation,
                inventory_instance_id=inventory_instance_id,
                inventory_sequence=inventory_sequence,
                limit=limit,
                now=now,
            )
        )

    async def mark_delivered(
        self,
        *,
        job_id: str,
        job_revision: int,
        delivered_at: float | None = None,
    ) -> DesiredInstallRecord:
        self._require_initialized()
        _canonical_uuid(job_id)
        if (
            isinstance(job_revision, bool)
            or not isinstance(job_revision, int)
            or not 1 <= job_revision <= 2_147_483_647
        ):
            raise DesiredInstallStoreError("desired_install_invalid_request")
        stamped = self._now() if delivered_at is None else float(delivered_at)
        _timestamp(stamped)
        return await self._run_sync(
            lambda: self._mark_delivered_sync(job_id, job_revision, stamped)
        )

    async def _run_sync(self, operation: Callable[[], _T]) -> _T:
        async with self._io_lock:
            worker = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                try:
                    await worker
                except BaseException:
                    pass
                raise

    def _now(self) -> float:
        now = float(self._wall_clock())
        _timestamp(now, unavailable=True)
        return now

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise DesiredInstallIntegrityError("desired_install_store_unavailable")

    def _initialize_sync(self) -> None:
        self._prepare_path()
        try:
            with self._connect() as conn:
                existing = {
                    str(row["name"])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                allowed = {
                    "desired_install_metadata",
                    "desired_install_jobs",
                    "desired_install_revisions",
                    "desired_install_acknowledgements",
                }
                if existing - allowed:
                    raise DesiredInstallIntegrityError(
                        "desired_install_store_identity_mismatch"
                    )
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS desired_install_metadata (
                        singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                        schema_version INTEGER NOT NULL,
                        store_id TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS desired_install_jobs (
                        job_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        intent_digest TEXT NOT NULL,
                        intent_json TEXT NOT NULL,
                        current_revision INTEGER NOT NULL CHECK(current_revision > 0),
                        desired_state TEXT NOT NULL CHECK(desired_state IN ('run','cancel')),
                        first_created_at REAL NOT NULL,
                        current_created_at REAL NOT NULL,
                        current_expires_at REAL NOT NULL,
                        pairing_id TEXT NOT NULL,
                        credential_generation INTEGER NOT NULL CHECK(credential_generation > 0),
                        inventory_instance_id TEXT NOT NULL,
                        inventory_sequence INTEGER NOT NULL CHECK(inventory_sequence >= 0),
                        storage_location_id TEXT NOT NULL,
                        storage_binding_generation INTEGER NOT NULL CHECK(storage_binding_generation > 0),
                        document_digest TEXT NOT NULL,
                        document_json TEXT NOT NULL,
                        delivery_count INTEGER NOT NULL DEFAULT 0 CHECK(delivery_count >= 0),
                        last_delivered_revision INTEGER,
                        last_delivered_at REAL,
                        current_ack_revision INTEGER,
                        current_ack_state TEXT,
                        current_ack_digest TEXT,
                        current_ack_json TEXT,
                        terminal_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS desired_install_pairing_idx
                        ON desired_install_jobs(pairing_id, credential_generation,
                            inventory_instance_id, inventory_sequence);
                    CREATE TABLE IF NOT EXISTS desired_install_revisions (
                        job_id TEXT NOT NULL REFERENCES desired_install_jobs(job_id)
                            ON DELETE CASCADE,
                        job_revision INTEGER NOT NULL,
                        desired_state TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        document_digest TEXT NOT NULL,
                        document_json TEXT NOT NULL,
                        PRIMARY KEY(job_id, job_revision)
                    );
                    CREATE TABLE IF NOT EXISTS desired_install_acknowledgements (
                        job_id TEXT NOT NULL REFERENCES desired_install_jobs(job_id)
                            ON DELETE CASCADE,
                        job_revision INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        payload_digest TEXT NOT NULL,
                        acknowledgement_json TEXT NOT NULL,
                        PRIMARY KEY(job_id, job_revision)
                    );
                    """
                )
                conn.execute("BEGIN IMMEDIATE")
                try:
                    metadata = conn.execute(
                        "SELECT schema_version, store_id FROM "
                        "desired_install_metadata WHERE singleton=1"
                    ).fetchone()
                    if metadata is None:
                        conn.execute(
                            "INSERT INTO desired_install_metadata VALUES (1, ?, ?)",
                            (_SCHEMA_VERSION, _STORE_ID),
                        )
                    elif (
                        int(metadata["schema_version"]) != _SCHEMA_VERSION
                        or str(metadata["store_id"]) != _STORE_ID
                    ):
                        raise DesiredInstallIntegrityError(
                            "desired_install_store_identity_mismatch"
                        )
                    self._prune_with_conn(conn, self._now())
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        except DesiredInstallStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            ) from None

    def _find_idempotent_sync(
        self, idempotency_key: str, intent_digest: str
    ) -> DesiredInstallRecord | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM desired_install_jobs WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if row is None:
                    return None
                if not desired_install_digest_equal(
                    intent_digest, str(row["intent_digest"])
                ):
                    raise DesiredInstallConflictError(
                        "desired_install_idempotency_conflict"
                    )
                return self._record_from_row(row)
        except DesiredInstallStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            ) from None

    def _create_sync(
        self,
        document: DesiredInstallDocument,
        *,
        intent_digest: str,
        intent_json: bytes,
    ) -> DesiredInstallCreation:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    existing = conn.execute(
                        "SELECT * FROM desired_install_jobs WHERE idempotency_key=?",
                        (document.idempotency_key,),
                    ).fetchone()
                    if existing is not None:
                        if not desired_install_digest_equal(
                            intent_digest, str(existing["intent_digest"])
                        ):
                            raise DesiredInstallConflictError(
                                "desired_install_idempotency_conflict"
                            )
                        record = self._record_from_row(existing)
                        conn.commit()
                        return DesiredInstallCreation(record, replayed=True)
                    now = self._now()
                    self._prune_with_conn(conn, now)
                    active = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM desired_install_jobs
                            WHERE current_expires_at > ? AND terminal_at IS NULL
                            """,
                            (now,),
                        ).fetchone()[0]
                    )
                    if active >= self._maximum_active_jobs:
                        raise DesiredInstallConflictError(
                            "desired_install_active_limit_reached"
                        )
                    text = document.canonical_json.decode("utf-8")
                    conn.execute(
                        """
                        INSERT INTO desired_install_jobs(
                            job_id, idempotency_key, intent_digest,
                            intent_json,
                            current_revision, desired_state, first_created_at,
                            current_created_at, current_expires_at, pairing_id,
                            credential_generation, inventory_instance_id,
                            inventory_sequence, storage_location_id,
                            storage_binding_generation, document_digest,
                            document_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document.job_id,
                            document.idempotency_key,
                            intent_digest,
                            intent_json.decode("utf-8"),
                            document.job_revision,
                            document.desired_state,
                            document.created_at,
                            document.created_at,
                            document.expires_at,
                            document.pairing_id,
                            document.credential_generation,
                            document.inventory_instance_id,
                            document.inventory_sequence,
                            document.storage_location_id,
                            document.storage_binding_generation,
                            document.payload_digest,
                            text,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO desired_install_revisions(
                            job_id, job_revision, desired_state, created_at,
                            expires_at, document_digest, document_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document.job_id,
                            document.job_revision,
                            document.desired_state,
                            document.created_at,
                            document.expires_at,
                            document.payload_digest,
                            text,
                        ),
                    )
                    row = self._job_row(conn, document.job_id)
                    record = self._record_from_row(row)
                    conn.commit()
                    return DesiredInstallCreation(record, replayed=False)
                except BaseException:
                    conn.rollback()
                    raise
        except DesiredInstallStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            ) from None

    def _get_sync(self, job_id: str) -> DesiredInstallRecord | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM desired_install_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                return None if row is None else self._record_from_row(row)
        except DesiredInstallStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            ) from None

    def _list_sync(
        self, offset: int, limit: int
    ) -> tuple[tuple[DesiredInstallRecord, ...], int]:
        try:
            with self._connect() as conn:
                total = int(
                    conn.execute("SELECT COUNT(*) FROM desired_install_jobs").fetchone()[0]
                )
                rows = conn.execute(
                    """
                    SELECT * FROM desired_install_jobs
                    ORDER BY first_created_at DESC, job_id
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
                return tuple(self._record_from_row(row) for row in rows), total
        except DesiredInstallStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            ) from None

    def _cancel_sync(
        self,
        job_id: str,
        *,
        expected_revision: int,
        issued_at: float,
        valid_for_seconds: int,
    ) -> DesiredInstallRecord:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM desired_install_jobs WHERE job_id=?",
                        (job_id,),
                    ).fetchone()
                    if row is None:
                        raise DesiredInstallNotFoundError(
                            "desired_install_job_unknown"
                        )
                    current = self._record_from_row(row)
                    if current.document.job_revision != expected_revision:
                        # Preserve retry idempotency for an ambiguous response
                        # to the exact run->cancel transition. No other stale
                        # revision is allowed to mutate or masquerade as the
                        # current command.
                        if (
                            current.document.desired_state == "cancel"
                            and current.document.job_revision
                            == expected_revision + 1
                        ):
                            conn.commit()
                            return current
                        raise DesiredInstallConflictError(
                            "desired_install_revision_conflict"
                        )
                    if current.document.desired_state == "cancel":
                        conn.commit()
                        return current
                    if current.terminal or current.document.expires_at <= issued_at:
                        raise DesiredInstallConflictError(
                            "desired_install_job_terminal"
                        )
                    if current.document.job_revision >= 2_147_483_647:
                        raise DesiredInstallConflictError(
                            "desired_install_revision_exhausted"
                        )
                    value = dict(current.document.value)
                    value.update(
                        {
                            "job_revision": current.document.job_revision + 1,
                            "desired_state": "cancel",
                            "created_at": issued_at,
                            "expires_at": issued_at + valid_for_seconds,
                            "valid_for_seconds": valid_for_seconds,
                        }
                    )
                    document = validate_desired_install(value)
                    text = document.canonical_json.decode("utf-8")
                    conn.execute(
                        """
                        UPDATE desired_install_jobs SET
                            current_revision=?, desired_state='cancel',
                            current_created_at=?, current_expires_at=?,
                            document_digest=?, document_json=?,
                            last_delivered_revision=NULL, last_delivered_at=NULL,
                            current_ack_revision=NULL, current_ack_state=NULL,
                            current_ack_digest=NULL, current_ack_json=NULL,
                            terminal_at=NULL
                        WHERE job_id=?
                        """,
                        (
                            document.job_revision,
                            document.created_at,
                            document.expires_at,
                            document.payload_digest,
                            text,
                            job_id,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO desired_install_revisions(
                            job_id, job_revision, desired_state, created_at,
                            expires_at, document_digest, document_json
                        ) VALUES (?, ?, 'cancel', ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            document.job_revision,
                            document.created_at,
                            document.expires_at,
                            document.payload_digest,
                            text,
                        ),
                    )
                    result = self._record_from_row(self._job_row(conn, job_id))
                    conn.commit()
                    return result
                except BaseException:
                    conn.rollback()
                    raise
        except DesiredInstallStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            ) from None

    def _accept_acknowledgements_sync(
        self,
        *,
        pairing_id: str,
        credential_generation: int,
        acknowledgements: tuple[JobAcknowledgement, ...],
    ) -> tuple[AcknowledgementAcceptance, ...]:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                results: list[AcknowledgementAcceptance] = []
                try:
                    for acknowledgement in acknowledgements:
                        row = conn.execute(
                            "SELECT * FROM desired_install_jobs WHERE job_id=?",
                            (acknowledgement.job_id,),
                        ).fetchone()
                        if row is None:
                            if acknowledgement.state not in _TERMINAL_ACK_STATES:
                                raise DesiredInstallConflictError(
                                    "desired_install_ack_unknown"
                                )
                            # A bounded Hub journal may already have retired a
                            # terminal job that the Mac continues to include in
                            # its bounded acknowledgement history. A
                            # well-formed terminal acknowledgement is safe to
                            # ignore and cannot create or mutate Hub state.
                            results.append(
                                AcknowledgementAcceptance(
                                    None,
                                    replayed=True,
                                    stale_revision=False,
                                    retired_unknown=True,
                                )
                            )
                            continue
                        current = self._record_from_row(row)
                        if (
                            current.document.pairing_id != pairing_id
                            or current.document.credential_generation
                            != credential_generation
                        ):
                            raise DesiredInstallConflictError(
                                "desired_install_ack_identity_changed"
                            )
                        if acknowledgement.job_revision > current.document.job_revision:
                            raise DesiredInstallConflictError(
                                "desired_install_ack_revision_unknown"
                            )
                        previous = conn.execute(
                            """
                            SELECT * FROM desired_install_acknowledgements
                            WHERE job_id=? AND job_revision=?
                            """,
                            (acknowledgement.job_id, acknowledgement.job_revision),
                        ).fetchone()
                        if acknowledgement.job_revision < current.document.job_revision:
                            # A delayed acknowledgement for an older run revision
                            # cannot unwind a newer cancellation. Preserve it only
                            # when it is the first well-formed observation.
                            if previous is None:
                                self._insert_ack(conn, acknowledgement)
                            elif not desired_install_digest_equal(
                                acknowledgement.payload_digest,
                                str(previous["payload_digest"]),
                            ):
                                self._assert_ack_progress(previous, acknowledgement)
                                self._update_ack(conn, acknowledgement)
                            results.append(
                                AcknowledgementAcceptance(
                                    current,
                                    replayed=previous is not None and desired_install_digest_equal(
                                        acknowledgement.payload_digest,
                                        str(previous["payload_digest"]),
                                    ),
                                    stale_revision=True,
                                )
                            )
                            continue
                        replayed = False
                        if previous is None:
                            self._insert_ack(conn, acknowledgement)
                        elif desired_install_digest_equal(
                            acknowledgement.payload_digest,
                            str(previous["payload_digest"]),
                        ):
                            replayed = True
                        else:
                            self._assert_ack_progress(previous, acknowledgement)
                            self._update_ack(conn, acknowledgement)
                        terminal_at = (
                            acknowledgement.updated_at
                            if acknowledgement.state in _TERMINAL_ACK_STATES
                            else None
                        )
                        conn.execute(
                            """
                            UPDATE desired_install_jobs SET
                                current_ack_revision=?, current_ack_state=?,
                                current_ack_digest=?, current_ack_json=?,
                                terminal_at=? WHERE job_id=?
                            """,
                            (
                                acknowledgement.job_revision,
                                acknowledgement.state,
                                acknowledgement.payload_digest,
                                acknowledgement.canonical_json.decode("utf-8"),
                                terminal_at,
                                acknowledgement.job_id,
                            ),
                        )
                        updated = self._record_from_row(
                            self._job_row(conn, acknowledgement.job_id)
                        )
                        results.append(
                            AcknowledgementAcceptance(
                                updated,
                                replayed=replayed,
                                stale_revision=False,
                            )
                        )
                    self._prune_with_conn(conn, self._now())
                    conn.commit()
                    return tuple(results)
                except BaseException:
                    conn.rollback()
                    raise
        except DesiredInstallStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            ) from None

    def _pending_for_delivery_sync(
        self,
        *,
        pairing_id: str,
        credential_generation: int,
        inventory_instance_id: str,
        inventory_sequence: int,
        limit: int,
        now: float,
    ) -> tuple[DesiredInstallRecord, ...]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM desired_install_jobs
                    WHERE pairing_id=? AND credential_generation=?
                      AND inventory_instance_id=? AND inventory_sequence <= ?
                      AND current_expires_at > ? AND terminal_at IS NULL
                      AND current_ack_revision IS NULL
                    ORDER BY first_created_at, job_id
                    LIMIT ?
                    """,
                    (
                        pairing_id,
                        credential_generation,
                        inventory_instance_id,
                        inventory_sequence,
                        now,
                        limit,
                    ),
                ).fetchall()
                return tuple(self._record_from_row(row) for row in rows)
        except DesiredInstallStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            ) from None

    def _mark_delivered_sync(
        self, job_id: str, job_revision: int, delivered_at: float
    ) -> DesiredInstallRecord:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM desired_install_jobs WHERE job_id=?",
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
                    conn.execute(
                        """
                        UPDATE desired_install_jobs SET
                            delivery_count=delivery_count+1,
                            last_delivered_revision=?, last_delivered_at=?
                        WHERE job_id=?
                        """,
                        (job_revision, delivered_at, job_id),
                    )
                    result = self._record_from_row(self._job_row(conn, job_id))
                    conn.commit()
                    return result
                except BaseException:
                    conn.rollback()
                    raise
        except DesiredInstallStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            ) from None

    def _insert_ack(
        self, conn: sqlite3.Connection, acknowledgement: JobAcknowledgement
    ) -> None:
        conn.execute(
            """
            INSERT INTO desired_install_acknowledgements(
                job_id, job_revision, state, updated_at,
                payload_digest, acknowledgement_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                acknowledgement.job_id,
                acknowledgement.job_revision,
                acknowledgement.state,
                acknowledgement.updated_at,
                acknowledgement.payload_digest,
                acknowledgement.canonical_json.decode("utf-8"),
            ),
        )

    def _update_ack(
        self, conn: sqlite3.Connection, acknowledgement: JobAcknowledgement
    ) -> None:
        conn.execute(
            """
            UPDATE desired_install_acknowledgements SET
                state=?, updated_at=?, payload_digest=?, acknowledgement_json=?
            WHERE job_id=? AND job_revision=?
            """,
            (
                acknowledgement.state,
                acknowledgement.updated_at,
                acknowledgement.payload_digest,
                acknowledgement.canonical_json.decode("utf-8"),
                acknowledgement.job_id,
                acknowledgement.job_revision,
            ),
        )

    def _assert_ack_progress(
        self, previous: sqlite3.Row, acknowledgement: JobAcknowledgement
    ) -> None:
        try:
            old = validate_job_acknowledgement(
                json.loads(str(previous["acknowledgement_json"]))
            )
        except (json.JSONDecodeError, DesiredInstallProtocolError):
            raise DesiredInstallIntegrityError("desired_install_store_corrupt") from None
        if (
            old.state in _TERMINAL_ACK_STATES
            or acknowledgement.updated_at < old.updated_at
            or acknowledgement.bytes_downloaded < old.bytes_downloaded
            or (
                old.total_bytes is not None
                and acknowledgement.total_bytes != old.total_bytes
            )
            or _ACK_ORDER[acknowledgement.state] < _ACK_ORDER[old.state]
            or (
                old.value.get("installation_id") is not None
                and acknowledgement.value.get("installation_id")
                != old.value.get("installation_id")
            )
        ):
            raise DesiredInstallConflictError(
                "desired_install_ack_conflict"
            )

    def _record_from_row(self, row: sqlite3.Row) -> DesiredInstallRecord:
        raw = str(row["document_json"]).encode("utf-8")
        try:
            document = validate_desired_install(json.loads(raw))
        except (json.JSONDecodeError, DesiredInstallProtocolError):
            raise DesiredInstallIntegrityError("desired_install_store_corrupt") from None
        if not desired_install_digest_equal(
            document.payload_digest, str(row["document_digest"])
        ):
            raise DesiredInstallIntegrityError("desired_install_store_corrupt")
        indexed = (
            str(row["job_id"]),
            str(row["idempotency_key"]),
            int(row["current_revision"]),
            str(row["desired_state"]),
            float(row["current_created_at"]),
            float(row["current_expires_at"]),
            str(row["pairing_id"]),
            int(row["credential_generation"]),
            str(row["inventory_instance_id"]),
            int(row["inventory_sequence"]),
            str(row["storage_location_id"]),
            int(row["storage_binding_generation"]),
        )
        document_index = (
            document.job_id,
            document.idempotency_key,
            document.job_revision,
            document.desired_state,
            document.created_at,
            document.expires_at,
            document.pairing_id,
            document.credential_generation,
            document.inventory_instance_id,
            document.inventory_sequence,
            document.storage_location_id,
            document.storage_binding_generation,
        )
        if indexed != document_index:
            raise DesiredInstallIntegrityError("desired_install_store_corrupt")
        acknowledgement = None
        ack_json = row["current_ack_json"]
        if ack_json is not None:
            if any(
                row[column] is None
                for column in (
                    "current_ack_revision",
                    "current_ack_state",
                    "current_ack_digest",
                )
            ):
                raise DesiredInstallIntegrityError("desired_install_store_corrupt")
            try:
                acknowledgement = validate_job_acknowledgement(
                    json.loads(str(ack_json))
                )
            except (json.JSONDecodeError, DesiredInstallProtocolError):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_corrupt"
                ) from None
            if (
                acknowledgement.job_id != document.job_id
                or acknowledgement.job_revision != document.job_revision
                or not desired_install_digest_equal(
                    acknowledgement.payload_digest,
                    str(row["current_ack_digest"]),
                )
                or acknowledgement.state != str(row["current_ack_state"])
            ):
                raise DesiredInstallIntegrityError("desired_install_store_corrupt")
            terminal_at = row["terminal_at"]
            if (acknowledgement.state in _TERMINAL_ACK_STATES) != (
                terminal_at is not None
            ):
                raise DesiredInstallIntegrityError("desired_install_store_corrupt")
        elif any(
            row[column] is not None
            for column in (
                "current_ack_revision",
                "current_ack_state",
                "current_ack_digest",
                "terminal_at",
            )
        ):
            raise DesiredInstallIntegrityError("desired_install_store_corrupt")
        return DesiredInstallRecord(
            document=document,
            intent_digest=_sha256(str(row["intent_digest"])),
            intent=_load_intent(
                str(row["intent_json"]), str(row["intent_digest"])
            ),
            first_created_at=float(row["first_created_at"]),
            delivery_count=int(row["delivery_count"]),
            last_delivered_revision=(
                None
                if row["last_delivered_revision"] is None
                else int(row["last_delivered_revision"])
            ),
            last_delivered_at=(
                None
                if row["last_delivered_at"] is None
                else float(row["last_delivered_at"])
            ),
            acknowledgement=acknowledgement,
        )

    def _job_row(self, conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM desired_install_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise DesiredInstallIntegrityError("desired_install_store_corrupt")
        return row

    def _prune_with_conn(self, conn: sqlite3.Connection, now: float) -> None:
        # Active jobs are never pruned. Retain a bounded newest history of
        # terminal or expired jobs and cascade their revision/ack journal.
        conn.execute(
            """
            DELETE FROM desired_install_jobs WHERE job_id IN (
                SELECT job_id FROM desired_install_jobs
                WHERE terminal_at IS NOT NULL OR current_expires_at <= ?
                ORDER BY COALESCE(terminal_at, current_expires_at) DESC, job_id
                LIMIT -1 OFFSET ?
            )
            """,
            (now, self._history_limit),
        )

    def _prepare_path(self) -> None:
        try:
            parent_existed = self._path.parent.exists()
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            parent = self._path.parent.lstat()
            if (
                not stat.S_ISDIR(parent.st_mode)
                or stat.S_ISLNK(parent.st_mode)
                or parent.st_uid != os.geteuid()
                or (
                    parent_existed
                    and stat.S_IMODE(parent.st_mode) != 0o700
                )
            ):
                raise DesiredInstallIntegrityError(
                    "desired_install_store_insecure_path"
                )
            try:
                status = self._path.lstat()
            except FileNotFoundError:
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self._path, flags, 0o600)
                os.close(descriptor)
                status = self._path.lstat()
            if (
                not stat.S_ISREG(status.st_mode)
                or stat.S_ISLNK(status.st_mode)
                or status.st_uid != os.geteuid()
            ):
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
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA trusted_schema=OFF")
        return conn


def _canonical_uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise DesiredInstallStoreError("desired_install_invalid_request") from None
    return value


def _sha256(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
    ):
        raise DesiredInstallStoreError("desired_install_invalid_request")
    try:
        bytes.fromhex(value[7:])
    except ValueError:
        raise DesiredInstallStoreError("desired_install_invalid_request") from None
    return value


def _timestamp(value: float, *, unavailable: bool = False) -> None:
    if not math.isfinite(value) or not 0 <= value <= 4_102_444_800:
        code = (
            "desired_install_clock_unavailable"
            if unavailable
            else "desired_install_invalid_request"
        )
        raise DesiredInstallStoreError(code)


def _canonical_intent(value: dict[str, Any], expected_digest: str) -> bytes:
    try:
        if not isinstance(value, dict):
            raise TypeError
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise DesiredInstallStoreError("desired_install_invalid_request") from None
    if len(encoded) > 32 * 1024:
        raise DesiredInstallStoreError("desired_install_invalid_request")
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if not desired_install_digest_equal(digest, expected_digest):
        raise DesiredInstallStoreError("desired_install_invalid_request")
    return encoded


def _load_intent(raw: str, expected_digest: str) -> dict[str, Any]:
    try:
        encoded = raw.encode("utf-8")
        value = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError, TypeError):
        raise DesiredInstallIntegrityError("desired_install_store_corrupt") from None
    try:
        canonical = _canonical_intent(value, expected_digest)
    except DesiredInstallStoreError:
        raise DesiredInstallIntegrityError("desired_install_store_corrupt") from None
    if canonical != encoded or not isinstance(value, dict):
        raise DesiredInstallIntegrityError("desired_install_store_corrupt")
    return value


__all__ = [
    "AcknowledgementAcceptance",
    "DesiredInstallConflictError",
    "DesiredInstallCreation",
    "DesiredInstallIntegrityError",
    "DesiredInstallNotFoundError",
    "DesiredInstallRecord",
    "DesiredInstallStore",
    "DesiredInstallStoreError",
]
