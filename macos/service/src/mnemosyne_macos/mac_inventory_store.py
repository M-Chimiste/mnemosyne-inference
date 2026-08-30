"""Private, path-bearing identity index for Mac pool inventory.

Only this node-local index may associate an opaque inventory identifier with a
configured storage/profile binding.  The wire producer consumes the returned
opaque records and never reads SQLite rows directly.  In particular, IDs are
random UUIDs rather than hashes of paths, volume identities, grants, aliases,
or hardware.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
import time
from typing import Iterable, Mapping
from uuid import UUID, uuid4


_STORE_ID = "mnemosyne-native-mac-inventory-index-v1"


class MacInventoryIndexError(RuntimeError):
    """Fixed-code local index failure safe for status surfaces."""

    def __init__(self, code: str = "inventory_index_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StorageBinding:
    local_key: str
    storage_location_id: str
    binding_generation: int
    exact_path: str
    volume_uuid: str | None
    scope_id: str | None


@dataclass(frozen=True, slots=True)
class InstallationBinding:
    source_key: str
    installation_id: str


class MacInventoryIndex:
    """Thread-safe facade over the private inventory identity tables."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path).expanduser()
        self._lock = threading.RLock()
        self._initialized = False
        self._closed = False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def reconcile_storage(
        self,
        bindings: Iterable[tuple[str, str, str | None, str | None]],
    ) -> dict[str, StorageBinding]:
        copied = tuple(bindings)
        return await asyncio.to_thread(self._reconcile_storage_sync, copied)

    async def reconcile_installations(
        self,
        bindings: Mapping[str, str],
    ) -> dict[str, InstallationBinding]:
        copied = dict(bindings)
        return await asyncio.to_thread(self._reconcile_installations_sync, copied)

    async def resolve_storage(
        self,
        storage_location_id: str,
        binding_generation: int,
    ) -> StorageBinding | None:
        """Resolve one exact active opaque storage binding.

        Callers that hold durable authority must name both the random location
        identifier and the generation observed when that authority was
        recorded.  A retired identifier or stale generation intentionally
        resolves to ``None`` rather than following the current local name.
        """

        return await asyncio.to_thread(
            self._resolve_storage_sync,
            storage_location_id,
            binding_generation,
        )

    async def active_storage_bindings(self) -> dict[str, StorageBinding]:
        """Return the exact active bindings without reconciling or mutating them."""

        return await asyncio.to_thread(self._active_storage_bindings_sync)

    async def close(self) -> None:
        self._closed = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._database_path),
            timeout=10,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _require_ready(self) -> None:
        if self._closed or not self._initialized:
            raise MacInventoryIndexError()

    def _initialize_sync(self) -> None:
        with self._lock:
            if self._closed:
                raise MacInventoryIndexError()
            try:
                self._database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with self._connect() as connection:
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS native_mac_inventory_metadata_v1 (
                            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                            store_id TEXT NOT NULL,
                            schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                        );
                        CREATE TABLE IF NOT EXISTS native_mac_inventory_storage_v1 (
                            storage_location_id TEXT PRIMARY KEY,
                            local_key TEXT NOT NULL,
                            binding_generation INTEGER NOT NULL CHECK (
                                binding_generation >= 1
                                AND binding_generation <= 2147483647
                            ),
                            exact_path TEXT NOT NULL,
                            volume_uuid TEXT,
                            scope_id TEXT,
                            active INTEGER NOT NULL CHECK (active IN (0, 1)),
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            retired_at REAL
                        );
                        CREATE UNIQUE INDEX IF NOT EXISTS
                            native_mac_inventory_storage_active_key_v1
                        ON native_mac_inventory_storage_v1(local_key)
                        WHERE active = 1;
                        CREATE TABLE IF NOT EXISTS native_mac_inventory_installations_v1 (
                            installation_id TEXT PRIMARY KEY,
                            source_key TEXT NOT NULL,
                            binding_fingerprint TEXT NOT NULL,
                            active INTEGER NOT NULL CHECK (active IN (0, 1)),
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            retired_at REAL
                        );
                        CREATE UNIQUE INDEX IF NOT EXISTS
                            native_mac_inventory_installations_active_key_v1
                        ON native_mac_inventory_installations_v1(source_key)
                        WHERE active = 1;
                        """
                    )
                    row = connection.execute(
                        """
                        SELECT store_id, schema_version
                          FROM native_mac_inventory_metadata_v1
                         WHERE singleton = 1
                        """
                    ).fetchone()
                    if row is None:
                        connection.execute(
                            """
                            INSERT INTO native_mac_inventory_metadata_v1 (
                                singleton, store_id, schema_version
                            ) VALUES (1, ?, 1)
                            """,
                            (_STORE_ID,),
                        )
                    elif row["store_id"] != _STORE_ID or int(row["schema_version"]) != 1:
                        raise MacInventoryIndexError("inventory_index_identity_mismatch")
                self._initialized = True
            except MacInventoryIndexError:
                raise
            except (OSError, sqlite3.Error):
                raise MacInventoryIndexError() from None

    def _new_uuid(self, connection: sqlite3.Connection) -> str:
        # UUID4 is deliberately independent of every local binding value. The
        # collision checks also ensure an ID retired by either index is never
        # reused by the other index.
        while True:
            candidate = str(uuid4())
            exists = connection.execute(
                """
                SELECT 1 FROM native_mac_inventory_storage_v1
                 WHERE storage_location_id = ?
                UNION ALL
                SELECT 1 FROM native_mac_inventory_installations_v1
                 WHERE installation_id = ?
                LIMIT 1
                """,
                (candidate, candidate),
            ).fetchone()
            if exists is None:
                return candidate

    def _storage_binding(self, row: sqlite3.Row) -> StorageBinding:
        return StorageBinding(
            local_key=str(row["local_key"]),
            storage_location_id=str(row["storage_location_id"]),
            binding_generation=int(row["binding_generation"]),
            exact_path=str(row["exact_path"]),
            volume_uuid=(str(row["volume_uuid"]) if row["volume_uuid"] is not None else None),
            scope_id=(str(row["scope_id"]) if row["scope_id"] is not None else None),
        )

    def _resolve_storage_sync(
        self,
        storage_location_id: str,
        binding_generation: int,
    ) -> StorageBinding | None:
        with self._lock:
            self._require_ready()
            if (
                canonical_uuid(storage_location_id) is None
                or isinstance(binding_generation, bool)
                or not 1 <= binding_generation <= 2147483647
            ):
                raise MacInventoryIndexError("inventory_storage_binding_invalid")
            try:
                with self._connect() as connection:
                    row = connection.execute(
                        """
                        SELECT * FROM native_mac_inventory_storage_v1
                         WHERE storage_location_id = ?
                           AND binding_generation = ?
                           AND active = 1
                        """,
                        (storage_location_id, binding_generation),
                    ).fetchone()
                return self._storage_binding(row) if row is not None else None
            except (OSError, sqlite3.Error):
                raise MacInventoryIndexError() from None

    def _active_storage_bindings_sync(self) -> dict[str, StorageBinding]:
        with self._lock:
            self._require_ready()
            try:
                with self._connect() as connection:
                    rows = connection.execute(
                        """
                        SELECT * FROM native_mac_inventory_storage_v1
                         WHERE active = 1
                         ORDER BY local_key
                        """
                    ).fetchall()
                bindings = {
                    str(row["local_key"]): self._storage_binding(row)
                    for row in rows
                }
                if len(bindings) != len(rows):
                    raise MacInventoryIndexError(
                        "inventory_storage_binding_invalid"
                    )
                return bindings
            except MacInventoryIndexError:
                raise
            except (OSError, sqlite3.Error):
                raise MacInventoryIndexError() from None

    def _reconcile_storage_sync(
        self,
        bindings: tuple[tuple[str, str, str | None, str | None], ...],
    ) -> dict[str, StorageBinding]:
        with self._lock:
            self._require_ready()
            keys = [item[0] for item in bindings]
            if any(not key for key in keys) or len(keys) != len(set(keys)):
                raise MacInventoryIndexError("inventory_storage_binding_invalid")
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    now = time.time()
                    current_rows = connection.execute(
                        """
                        SELECT * FROM native_mac_inventory_storage_v1
                         WHERE active = 1
                        """
                    ).fetchall()
                    current = {str(row["local_key"]): row for row in current_rows}
                    requested_keys = set(keys)
                    for local_key, row in current.items():
                        if local_key not in requested_keys:
                            connection.execute(
                                """
                                UPDATE native_mac_inventory_storage_v1
                                   SET active = 0, retired_at = ?, updated_at = ?
                                 WHERE storage_location_id = ? AND active = 1
                                """,
                                (now, now, row["storage_location_id"]),
                            )

                    result: dict[str, StorageBinding] = {}
                    for local_key, exact_path, volume_uuid, scope_id in bindings:
                        if not exact_path:
                            raise MacInventoryIndexError(
                                "inventory_storage_binding_invalid"
                            )
                        row = current.get(local_key)
                        if row is None:
                            storage_id = self._new_uuid(connection)
                            generation = 1
                            connection.execute(
                                """
                                INSERT INTO native_mac_inventory_storage_v1 (
                                    storage_location_id, local_key,
                                    binding_generation, exact_path, volume_uuid,
                                    scope_id, active, created_at, updated_at,
                                    retired_at
                                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
                                """,
                                (
                                    storage_id,
                                    local_key,
                                    generation,
                                    exact_path,
                                    volume_uuid,
                                    scope_id,
                                    now,
                                    now,
                                ),
                            )
                        else:
                            storage_id = str(row["storage_location_id"])
                            generation = int(row["binding_generation"])
                            changed = (
                                str(row["exact_path"]) != exact_path
                                or row["volume_uuid"] != volume_uuid
                                or row["scope_id"] != scope_id
                            )
                            if changed:
                                if generation >= 2147483647:
                                    # Never wrap a generation. Retiring the ID
                                    # makes every old desired binding fail closed.
                                    connection.execute(
                                        """
                                        UPDATE native_mac_inventory_storage_v1
                                           SET active = 0, retired_at = ?, updated_at = ?
                                         WHERE storage_location_id = ?
                                        """,
                                        (now, now, storage_id),
                                    )
                                    storage_id = self._new_uuid(connection)
                                    generation = 1
                                    connection.execute(
                                        """
                                        INSERT INTO native_mac_inventory_storage_v1 (
                                            storage_location_id, local_key,
                                            binding_generation, exact_path,
                                            volume_uuid, scope_id, active,
                                            created_at, updated_at, retired_at
                                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
                                        """,
                                        (
                                            storage_id,
                                            local_key,
                                            generation,
                                            exact_path,
                                            volume_uuid,
                                            scope_id,
                                            now,
                                            now,
                                        ),
                                    )
                                else:
                                    generation += 1
                                    connection.execute(
                                        """
                                        UPDATE native_mac_inventory_storage_v1
                                           SET binding_generation = ?, exact_path = ?,
                                               volume_uuid = ?, scope_id = ?,
                                               updated_at = ?
                                         WHERE storage_location_id = ? AND active = 1
                                        """,
                                        (
                                            generation,
                                            exact_path,
                                            volume_uuid,
                                            scope_id,
                                            now,
                                            storage_id,
                                        ),
                                    )
                        result[local_key] = StorageBinding(
                            local_key=local_key,
                            storage_location_id=storage_id,
                            binding_generation=generation,
                            exact_path=exact_path,
                            volume_uuid=volume_uuid,
                            scope_id=scope_id,
                        )
                    connection.commit()
                    return result
            except MacInventoryIndexError:
                raise
            except (OSError, sqlite3.Error):
                raise MacInventoryIndexError() from None

    def _reconcile_installations_sync(
        self,
        bindings: dict[str, str],
    ) -> dict[str, InstallationBinding]:
        with self._lock:
            self._require_ready()
            if any(not key or not fingerprint for key, fingerprint in bindings.items()):
                raise MacInventoryIndexError("inventory_installation_binding_invalid")
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    now = time.time()
                    rows = connection.execute(
                        """
                        SELECT * FROM native_mac_inventory_installations_v1
                         WHERE active = 1
                        """
                    ).fetchall()
                    current = {str(row["source_key"]): row for row in rows}
                    for source_key, row in current.items():
                        requested = bindings.get(source_key)
                        if requested is None or requested != row["binding_fingerprint"]:
                            connection.execute(
                                """
                                UPDATE native_mac_inventory_installations_v1
                                   SET active = 0, retired_at = ?, updated_at = ?
                                 WHERE installation_id = ? AND active = 1
                                """,
                                (now, now, row["installation_id"]),
                            )

                    result: dict[str, InstallationBinding] = {}
                    for source_key, fingerprint in bindings.items():
                        row = current.get(source_key)
                        if row is not None and row["binding_fingerprint"] == fingerprint:
                            installation_id = str(row["installation_id"])
                        else:
                            installation_id = self._new_uuid(connection)
                            connection.execute(
                                """
                                INSERT INTO native_mac_inventory_installations_v1 (
                                    installation_id, source_key,
                                    binding_fingerprint, active, created_at,
                                    updated_at, retired_at
                                ) VALUES (?, ?, ?, 1, ?, ?, NULL)
                                """,
                                (
                                    installation_id,
                                    source_key,
                                    fingerprint,
                                    now,
                                    now,
                                ),
                            )
                        result[source_key] = InstallationBinding(
                            source_key=source_key,
                            installation_id=installation_id,
                        )
                    connection.commit()
                    return result
            except MacInventoryIndexError:
                raise
            except (OSError, sqlite3.Error):
                raise MacInventoryIndexError() from None


def canonical_uuid(value: str) -> str | None:
    """Return one canonical UUID or ``None`` for legacy/corrupt identifiers."""

    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        return None
    canonical = str(parsed)
    return canonical if value.casefold() == canonical else None


__all__ = [
    "InstallationBinding",
    "MacInventoryIndex",
    "MacInventoryIndexError",
    "StorageBinding",
    "canonical_uuid",
]
