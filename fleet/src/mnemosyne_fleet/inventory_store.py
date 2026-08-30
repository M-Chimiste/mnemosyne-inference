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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

from .inventory_protocol import (
    InventoryProtocolError,
    MAX_INVENTORY_REQUEST_BYTES,
    MacInventoryDocument,
    validate_inventory,
)


_SCHEMA_VERSION: Final[int] = 1
_STORE_ID: Final[str] = "mnemosyne-fleet-mac-inventory-v1"
_T = TypeVar("_T")


class InventoryStoreError(RuntimeError):
    """A fixed-code inventory failure without document or topology detail."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class InventoryStoreConflictError(InventoryStoreError):
    pass


class InventoryStoreIntegrityError(InventoryStoreError):
    pass


@dataclass(frozen=True, slots=True)
class InventoryRecord:
    pairing_id: str
    credential_generation: int
    inventory_instance_id: str
    inventory_sequence: int
    observed_at: float
    received_at: float
    received_monotonic: float
    hub_process_instance_id: str
    payload_digest: str
    inventory: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class InventoryAcceptance:
    record: InventoryRecord
    replayed: bool


class InventoryStore:
    """Durable, secret-free MacInventory observations.

    The store is intentionally independent from the routing registry and Fleet
    snapshot database. Its only accepted payload is the strict path-free
    version-1 inventory document. Hub-process identity and monotonic receipt
    time are persisted so a restarted Hub can display an observation without
    accidentally restoring its freshness authority.
    """

    def __init__(
        self,
        path: Path,
        *,
        freshness_ttl_seconds: float = 60.0,
        process_instance_id: str | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not math.isfinite(freshness_ttl_seconds)
            or freshness_ttl_seconds < 15
            or freshness_ttl_seconds > 300
        ):
            raise ValueError("inventory freshness TTL must be between 15 and 300")
        process_id = process_instance_id or str(uuid.uuid4())
        try:
            if str(uuid.UUID(process_id)) != process_id:
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError(
                "inventory process instance ID must be canonical UUID"
            ) from None
        self._path = path
        self._freshness_ttl_seconds = float(freshness_ttl_seconds)
        self._process_instance_id = process_id
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._io_lock = asyncio.Lock()
        self._initialized = False

    @property
    def process_instance_id(self) -> str:
        return self._process_instance_id

    @property
    def freshness_ttl_seconds(self) -> float:
        return self._freshness_ttl_seconds

    async def initialize(self) -> None:
        async with self._io_lock:
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    async def accept(
        self,
        document: MacInventoryDocument,
    ) -> InventoryAcceptance:
        self._require_initialized()
        # Keep the persistence boundary independently strict even when a
        # caller bypasses the HTTP parser or mutates a previously validated
        # in-memory mapping.
        document = validate_inventory(document.value)
        received_at = float(self._wall_clock())
        received_monotonic = float(self._monotonic_clock())
        if (
            not math.isfinite(received_at)
            or received_at < 0
            or not math.isfinite(received_monotonic)
            or received_monotonic < 0
        ):
            raise InventoryStoreIntegrityError("inventory_clock_unavailable")
        return await self._run_sync(
            lambda: self._accept_sync(
                document,
                received_at=received_at,
                received_monotonic=received_monotonic,
            )
        )

    async def record(self, pairing_id: str) -> InventoryRecord | None:
        self._require_initialized()
        pairing_id = _canonical_uuid(pairing_id)
        return await self._run_sync(lambda: self._record_sync(pairing_id))

    async def records(self, *, limit: int = 100) -> tuple[InventoryRecord, ...]:
        self._require_initialized()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 1000
        ):
            raise InventoryStoreError("inventory_invalid_request")
        return await self._run_sync(lambda: self._records_sync(limit))

    def freshness(
        self,
        record: InventoryRecord,
        *,
        enrollment_active: bool,
        active_credential_generation: int | None,
    ) -> dict[str, object]:
        """Return fixed freshness metadata; never inference authority."""

        if not enrollment_active:
            reason = "enrollment_inactive"
            age: float | None = None
        elif active_credential_generation != record.credential_generation:
            reason = "credential_generation_changed"
            age = None
        elif record.hub_process_instance_id != self._process_instance_id:
            reason = "hub_restarted"
            age = None
        else:
            age = max(0.0, self._monotonic_clock() - record.received_monotonic)
            reason = None if age <= self._freshness_ttl_seconds else "expired"
        fresh = reason is None
        return {
            "state": "fresh" if fresh else "stale",
            "reason": reason,
            "receipt_age_seconds": age,
            "authoritative_for_placement": fresh,
            "authoritative_for_inference": False,
        }

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

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise InventoryStoreIntegrityError("inventory_store_unavailable")

    def _initialize_sync(self) -> None:
        self._prepare_path()
        try:
            with self._connect() as conn:
                existing_tables = {
                    str(row["name"])
                    for row in conn.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table' AND name NOT LIKE 'sqlite_%'
                        """
                    ).fetchall()
                }
                allowed_tables = {
                    "inventory_metadata",
                    "inventory_observations",
                    "inventory_instances",
                }
                if existing_tables - allowed_tables:
                    raise InventoryStoreIntegrityError(
                        "inventory_store_identity_mismatch"
                    )
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS inventory_metadata (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL,
                        store_id TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS inventory_observations (
                        pairing_id TEXT PRIMARY KEY,
                        credential_generation INTEGER NOT NULL CHECK (
                            credential_generation > 0
                        ),
                        inventory_instance_id TEXT NOT NULL,
                        inventory_sequence INTEGER NOT NULL CHECK (
                            inventory_sequence >= 0
                        ),
                        observed_at REAL NOT NULL,
                        received_at REAL NOT NULL,
                        received_monotonic REAL NOT NULL,
                        hub_process_instance_id TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        inventory_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS inventory_instances (
                        pairing_id TEXT NOT NULL,
                        credential_generation INTEGER NOT NULL CHECK (
                            credential_generation > 0
                        ),
                        inventory_instance_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('active', 'retired')),
                        maximum_sequence INTEGER NOT NULL CHECK (
                            maximum_sequence >= 0
                        ),
                        last_payload_digest TEXT NOT NULL,
                        first_received_at REAL NOT NULL,
                        last_received_at REAL NOT NULL,
                        PRIMARY KEY (
                            pairing_id,
                            credential_generation,
                            inventory_instance_id
                        )
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS inventory_active_instance_idx
                        ON inventory_instances(pairing_id)
                        WHERE state='active';
                    """
                )
                conn.execute("BEGIN IMMEDIATE")
                try:
                    metadata = conn.execute(
                        """
                        SELECT schema_version, store_id
                        FROM inventory_metadata WHERE singleton=1
                        """
                    ).fetchone()
                    if metadata is None:
                        conn.execute(
                            """
                            INSERT INTO inventory_metadata(
                                singleton, schema_version, store_id
                            ) VALUES (1, ?, ?)
                            """,
                            (_SCHEMA_VERSION, _STORE_ID),
                        )
                    elif (
                        int(metadata["schema_version"]) != _SCHEMA_VERSION
                        or str(metadata["store_id"]) != _STORE_ID
                    ):
                        raise InventoryStoreIntegrityError(
                            "inventory_store_identity_mismatch"
                        )
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        except InventoryStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise InventoryStoreIntegrityError("inventory_store_unavailable") from None

    def _accept_sync(
        self,
        document: MacInventoryDocument,
        *,
        received_at: float,
        received_monotonic: float,
    ) -> InventoryAcceptance:
        inventory_text = document.canonical_json.decode("utf-8")
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    instance = conn.execute(
                        """
                        SELECT state, maximum_sequence, last_payload_digest
                        FROM inventory_instances
                        WHERE pairing_id=? AND credential_generation=?
                          AND inventory_instance_id=?
                        """,
                        (
                            document.pairing_id,
                            document.credential_generation,
                            document.inventory_instance_id,
                        ),
                    ).fetchone()
                    if instance is not None:
                        if str(instance["state"]) != "active":
                            raise InventoryStoreConflictError(
                                "inventory_instance_retired"
                            )
                        maximum = int(instance["maximum_sequence"])
                        if document.inventory_sequence < maximum:
                            raise InventoryStoreConflictError(
                                "inventory_sequence_stale"
                            )
                        if document.inventory_sequence == maximum:
                            if not _constant_digest_equal(
                                document.payload_digest,
                                str(instance["last_payload_digest"]),
                            ):
                                raise InventoryStoreConflictError(
                                    "inventory_sequence_conflict"
                                )
                            current = self._record_with_conn(
                                conn,
                                document.pairing_id,
                            )
                            if current is None:
                                raise InventoryStoreIntegrityError(
                                    "inventory_store_corrupt"
                                )
                            conn.commit()
                            return InventoryAcceptance(
                                record=current,
                                replayed=True,
                            )
                        current = conn.execute(
                            """
                            SELECT credential_generation, inventory_instance_id
                            FROM inventory_observations WHERE pairing_id=?
                            """,
                            (document.pairing_id,),
                        ).fetchone()
                        if current is None or (
                            int(current["credential_generation"]),
                            str(current["inventory_instance_id"]),
                        ) != (
                            document.credential_generation,
                            document.inventory_instance_id,
                        ):
                            raise InventoryStoreIntegrityError(
                                "inventory_store_corrupt"
                            )
                        conn.execute(
                            """
                            UPDATE inventory_instances
                            SET maximum_sequence=?, last_payload_digest=?,
                                last_received_at=?
                            WHERE pairing_id=? AND credential_generation=?
                              AND inventory_instance_id=? AND state='active'
                            """,
                            (
                                document.inventory_sequence,
                                document.payload_digest,
                                received_at,
                                document.pairing_id,
                                document.credential_generation,
                                document.inventory_instance_id,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE inventory_instances SET state='retired'
                            WHERE pairing_id=? AND state='active'
                            """,
                            (document.pairing_id,),
                        )
                        conn.execute(
                            """
                            INSERT INTO inventory_instances(
                                pairing_id, credential_generation,
                                inventory_instance_id, state, maximum_sequence,
                                last_payload_digest, first_received_at,
                                last_received_at
                            ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                            """,
                            (
                                document.pairing_id,
                                document.credential_generation,
                                document.inventory_instance_id,
                                document.inventory_sequence,
                                document.payload_digest,
                                received_at,
                                received_at,
                            ),
                        )

                    conn.execute(
                        """
                        INSERT INTO inventory_observations(
                            pairing_id, credential_generation,
                            inventory_instance_id, inventory_sequence,
                            observed_at, received_at, received_monotonic,
                            hub_process_instance_id, payload_digest,
                            inventory_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(pairing_id) DO UPDATE SET
                            credential_generation=excluded.credential_generation,
                            inventory_instance_id=excluded.inventory_instance_id,
                            inventory_sequence=excluded.inventory_sequence,
                            observed_at=excluded.observed_at,
                            received_at=excluded.received_at,
                            received_monotonic=excluded.received_monotonic,
                            hub_process_instance_id=excluded.hub_process_instance_id,
                            payload_digest=excluded.payload_digest,
                            inventory_json=excluded.inventory_json
                        """,
                        (
                            document.pairing_id,
                            document.credential_generation,
                            document.inventory_instance_id,
                            document.inventory_sequence,
                            document.observed_at,
                            received_at,
                            received_monotonic,
                            self._process_instance_id,
                            document.payload_digest,
                            inventory_text,
                        ),
                    )
                    record = self._record_with_conn(conn, document.pairing_id)
                    if record is None:
                        raise InventoryStoreIntegrityError(
                            "inventory_store_corrupt"
                        )
                    conn.commit()
                    return InventoryAcceptance(record=record, replayed=False)
                except BaseException:
                    conn.rollback()
                    raise
        except InventoryStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise InventoryStoreIntegrityError("inventory_store_unavailable") from None

    def _record_sync(self, pairing_id: str) -> InventoryRecord | None:
        try:
            with self._connect() as conn:
                return self._record_with_conn(conn, pairing_id)
        except InventoryStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise InventoryStoreIntegrityError("inventory_store_unavailable") from None

    def _records_sync(self, limit: int) -> tuple[InventoryRecord, ...]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM inventory_observations
                    ORDER BY received_at DESC, pairing_id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return tuple(self._record_from_row(row) for row in rows)
        except InventoryStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise InventoryStoreIntegrityError("inventory_store_unavailable") from None

    def _record_with_conn(
        self,
        conn: sqlite3.Connection,
        pairing_id: str,
    ) -> InventoryRecord | None:
        row = conn.execute(
            "SELECT * FROM inventory_observations WHERE pairing_id=?",
            (pairing_id,),
        ).fetchone()
        return None if row is None else self._record_from_row(row)

    def _record_from_row(self, row: sqlite3.Row) -> InventoryRecord:
        raw = str(row["inventory_json"]).encode("utf-8")
        if len(raw) > MAX_INVENTORY_REQUEST_BYTES:
            raise InventoryStoreIntegrityError("inventory_store_corrupt")
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if not _constant_digest_equal(digest, str(row["payload_digest"])):
            raise InventoryStoreIntegrityError("inventory_store_corrupt")
        try:
            value = json.loads(raw)
            document = validate_inventory(value)
        except (ValueError, TypeError, InventoryProtocolError):
            raise InventoryStoreIntegrityError("inventory_store_corrupt") from None
        indexed = (
            str(row["pairing_id"]),
            int(row["credential_generation"]),
            str(row["inventory_instance_id"]),
            int(row["inventory_sequence"]),
            float(row["observed_at"]),
        )
        if indexed != (
            document.pairing_id,
            document.credential_generation,
            document.inventory_instance_id,
            document.inventory_sequence,
            document.observed_at,
        ):
            raise InventoryStoreIntegrityError("inventory_store_corrupt")
        return InventoryRecord(
            pairing_id=document.pairing_id,
            credential_generation=document.credential_generation,
            inventory_instance_id=document.inventory_instance_id,
            inventory_sequence=document.inventory_sequence,
            observed_at=document.observed_at,
            received_at=float(row["received_at"]),
            received_monotonic=float(row["received_monotonic"]),
            hub_process_instance_id=str(row["hub_process_instance_id"]),
            payload_digest=document.payload_digest,
            inventory=document.value,
        )

    def _prepare_path(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            parent_status = self._path.parent.lstat()
            if not stat.S_ISDIR(parent_status.st_mode) or stat.S_ISLNK(
                parent_status.st_mode
            ):
                raise InventoryStoreIntegrityError(
                    "inventory_store_insecure_path"
                )
            try:
                status = self._path.lstat()
            except FileNotFoundError:
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self._path, flags, 0o600)
                os.close(descriptor)
                status = self._path.lstat()
            if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
                raise InventoryStoreIntegrityError(
                    "inventory_store_insecure_path"
                )
            os.chmod(self._path, 0o600, follow_symlinks=False)
        except InventoryStoreError:
            raise
        except OSError:
            raise InventoryStoreIntegrityError(
                "inventory_store_insecure_path"
            ) from None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA trusted_schema=OFF")
        return conn


def _canonical_uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise InventoryStoreError("inventory_invalid_request") from None
    return value


def _constant_digest_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
