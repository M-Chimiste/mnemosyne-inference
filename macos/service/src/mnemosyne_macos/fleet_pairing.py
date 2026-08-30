"""Durable local foundation for future Nyx pairing.

This module deliberately has no HTTP, runtime, snapshot, engine, model, or
storage integration.  It owns only sanitized pairing metadata in SQLite and
three pairing credentials in the service's private environment file.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
import errno
import fcntl
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Any, Callable, Iterator, Mapping, MutableMapping, TypeVar
from urllib.parse import urlsplit
from uuid import UUID, uuid4


FLEET_SNAPSHOT_KEY = "FLEET_API_KEY"
FLEET_DISPATCH_KEY = "FLEET_INFERENCE_API_KEY"
FLEET_MANAGEMENT_KEY = "FLEET_MANAGEMENT_API_KEY"
MANAGED_FLEET_ENV_KEYS = (
    FLEET_SNAPSHOT_KEY,
    FLEET_DISPATCH_KEY,
    FLEET_MANAGEMENT_KEY,
)
MAX_FLEET_CREDENTIAL_LENGTH = 4096
PRIVATE_ENVIRONMENT_LOCK_SUFFIX = ".lock"


def _credential_fingerprint(values: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for key in MANAGED_FLEET_ENV_KEYS:
        value = values[key]
        encoded_key = key.encode("ascii")
        encoded_value = value.encode("utf-8")
        digest.update(len(encoded_key).to_bytes(2, "big"))
        digest.update(encoded_key)
        digest.update(len(encoded_value).to_bytes(8, "big"))
        digest.update(encoded_value)
    return digest.hexdigest()


class PairingState(StrEnum):
    UNPAIRED = "unpaired"
    PENDING = "pending"
    PAIRED = "paired"
    REVOKED = "revoked"
    RECOVERY_REQUIRED = "recovery_required"


class PairingErrorCode(StrEnum):
    LEGACY_FLEET_CREDENTIALS_PRESENT = "legacy_fleet_credentials_present"
    CREDENTIAL_ENVIRONMENT_OVERRIDE = "credential_environment_override"
    PRIVATE_ENVIRONMENT_INVALID = "private_environment_invalid"
    PRIVATE_ENVIRONMENT_WRITE_FAILED = "private_environment_write_failed"
    PAIRING_EXPIRED = "pairing_expired"
    PAIRING_VERIFICATION_FAILED = "pairing_verification_failed"
    REVOKED_BY_HUB = "revoked_by_hub"
    STATE_INCONSISTENT = "state_inconsistent"


class FleetPairingError(RuntimeError):
    """Base error whose message never includes secret values."""


class InvalidPairingTransition(FleetPairingError):
    pass


class LegacyFleetCredentialsPresent(FleetPairingError):
    pass


class PrivateEnvironmentError(FleetPairingError):
    pass


class PrivateEnvironmentInvalid(PrivateEnvironmentError):
    pass


class PrivateEnvironmentWriteFailed(PrivateEnvironmentError):
    pass


class PairingStoreClosed(FleetPairingError):
    pass


@dataclass(frozen=True, slots=True)
class PairingCredentials:
    snapshot_key: str = field(repr=False)
    dispatch_key: str = field(repr=False)
    management_key: str = field(repr=False)

    def validated(self) -> "PairingCredentials":
        values = (self.snapshot_key, self.dispatch_key, self.management_key)
        if any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > MAX_FLEET_CREDENTIAL_LENGTH
            or value[0] in {'"', "'"}
            or value[-1] in {'"', "'"}
            or any(character in value for character in "\r\n\0")
            for value in values
        ):
            raise ValueError(
                "Fleet pairing credentials must be bounded, stripped, non-empty "
                "single lines"
            )
        if len(set(values)) != len(values):
            raise ValueError("Fleet pairing credentials must be distinct")
        return self

    def environment(self) -> dict[str, str]:
        return {
            FLEET_SNAPSHOT_KEY: self.snapshot_key,
            FLEET_DISPATCH_KEY: self.dispatch_key,
            FLEET_MANAGEMENT_KEY: self.management_key,
        }

    def fingerprint(self) -> str:
        return _credential_fingerprint(self.environment())


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> "_FileIdentity":
        return cls(device=metadata.st_dev, inode=metadata.st_ino)


@dataclass(frozen=True, slots=True)
class PairingRecord:
    state: PairingState
    device_id: str
    attempt_id: str | None
    pairing_id: str | None
    node_id: str | None
    hub_origin: str | None
    node_url: str | None
    credential_epoch: int | None
    credentials_owned: bool
    credential_write_pending: bool
    credential_fingerprint: str | None
    created_at: float
    updated_at: float
    paired_at: float | None
    revoked_at: float | None
    last_error_code: PairingErrorCode | None
    legacy_credentials_present: bool = False


T = TypeVar("T")


async def _await_blocking_outcome(
    function: Callable[..., T],
    *args: Any,
) -> T:
    """Drive one blocking operation to completion before replaying cancellation."""

    task = asyncio.create_task(
        asyncio.to_thread(function, *args),
        name="mnemosyne-fleet-pairing-state",
    )
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    if cancellation is not None:
        try:
            task.result()
        except BaseException:
            # Retrieve a completed worker failure, but preserve the caller's
            # already-observed cancellation as the public outcome.
            pass
        raise cancellation
    return task.result()


def _canonical_uuid(value: str, *, field: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise ValueError(f"{field} must be a canonical UUID")
    return canonical


def _safe_node_id(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(character.isspace() or ord(character) < 33 for character in normalized)
    ):
        raise ValueError("node_id must be a bounded opaque identifier")
    return normalized


def _safe_origin(value: str) -> str:
    if len(value) > 2048:
        raise ValueError("hub_origin is too long")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("hub_origin must be an HTTP(S) origin")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("hub_origin contains an invalid port") from exc
    port = f":{parsed_port}" if parsed_port is not None else ""
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}{port}"


def _safe_node_url(value: str) -> str:
    if len(value) > 2048:
        raise ValueError("node_url is too long")
    parsed = urlsplit(value.rstrip("/"))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("node_url must be an HTTP(S) origin")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("node_url contains an invalid port") from exc
    port = f":{parsed_port}" if parsed_port is not None else ""
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}{port}"


def _assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    return key.strip(), value.strip().strip('"').strip("'")


class FleetPairingStore:
    """Async facade over thread-confined pairing SQLite and private-file work."""

    def __init__(
        self,
        database_path: str | Path,
        environment_path: str | Path,
        *,
        process_environment: MutableMapping[str, str] | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self.environment_path = Path(environment_path).expanduser()
        self._process_environment = (
            os.environ if process_environment is None else process_environment
        )
        self._lock = asyncio.Lock()
        self._closed = False
        self._initialized = False

    async def initialize(self) -> PairingRecord:
        async with self._lock:
            self._ensure_open()
            record = await _await_blocking_outcome(self._initialize_sync)
            self._initialized = True
            return record

    async def status(self) -> PairingRecord:
        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(self._status_sync)

    async def begin_attempt(
        self,
        *,
        hub_origin: str,
        node_url: str,
        attempt_id: str | None = None,
    ) -> PairingRecord:
        normalized_hub = _safe_origin(hub_origin)
        normalized_node = _safe_node_url(node_url)
        normalized_attempt = _canonical_uuid(
            attempt_id or str(uuid4()),
            field="attempt_id",
        )
        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(
                self._begin_attempt_sync,
                normalized_hub,
                normalized_node,
                normalized_attempt,
            )

    async def record_assignment(
        self,
        *,
        pairing_id: str,
        node_id: str,
        credential_epoch: int,
    ) -> PairingRecord:
        normalized_pairing = _canonical_uuid(pairing_id, field="pairing_id")
        normalized_node = _safe_node_id(node_id)
        if (
            isinstance(credential_epoch, bool)
            or not isinstance(credential_epoch, int)
            or credential_epoch < 1
        ):
            raise ValueError("credential_epoch must be a positive integer")
        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(
                self._record_assignment_sync,
                normalized_pairing,
                normalized_node,
                credential_epoch,
            )

    async def activate_credentials(
        self,
        credentials: PairingCredentials,
    ) -> PairingRecord:
        validated = credentials.validated()
        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(
                self._activate_credentials_sync,
                validated,
            )

    async def staged_credentials(self) -> PairingCredentials:
        """Load the exact owned bundle for an idempotent activation retry.

        This is intentionally narrower than a generic credential export: it
        succeeds only for a durable manager-owned pairing generation whose
        private environment still matches its journaled fingerprint.
        """

        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(self._staged_credentials_sync)

    async def mark_paired(self) -> PairingRecord:
        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(self._mark_paired_sync)

    async def mark_revoked(self) -> PairingRecord:
        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(self._mark_revoked_sync)

    async def retire_revoked_credentials(self) -> PairingRecord:
        """Idempotently remove only the exact pairing-owned credential bundle.

        The revoked identity and tombstone remain durable. A failed file
        update leaves retirement pending in REVOKED so admission stays closed
        and an exact management replay can finish cleanup locally.
        """

        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(
                self._retire_revoked_credentials_sync
            )

    async def mark_recovery_required(
        self,
        error_code: PairingErrorCode,
    ) -> PairingRecord:
        if not isinstance(error_code, PairingErrorCode):
            raise ValueError("error_code must be a fixed PairingErrorCode")
        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(
                self._mark_recovery_required_sync,
                error_code,
            )

    async def clear_pairing(self) -> PairingRecord:
        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(self._clear_pairing_sync)

    async def _begin_repairing_after_completed_revoke(
        self,
        hub_origin: str,
        node_url: str,
        attempt_id: str,
        revoked_pairing_id: str,
        revoked_node_id: str,
        revoked_credential_epoch: int,
        revoked_attempt_id: str,
        journal_transition: Callable[[sqlite3.Connection, sqlite3.Row], T],
    ) -> T:
        """Atomically replace completed client authority and begin one attempt.

        This private seam exists only for ``FleetPairingClient.begin``.  It
        holds both the store lock and private-environment lock, rechecks the
        exact credential-free revoked tombstone in the shared SQLite
        transaction, and lets the client replace its two singleton rows in
        that transaction.
        """

        async with self._lock:
            self._ensure_ready()
            return await _await_blocking_outcome(
                self._begin_repairing_after_completed_revoke_sync,
                hub_origin,
                node_url,
                attempt_id,
                revoked_pairing_id,
                revoked_node_id,
                revoked_credential_epoch,
                revoked_attempt_id,
                journal_transition,
            )

    async def close(self) -> None:
        async with self._lock:
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise PairingStoreClosed("Fleet pairing store is closed")

    def _ensure_ready(self) -> None:
        self._ensure_open()
        if not self._initialized:
            raise FleetPairingError("Fleet pairing store is not initialized")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize_sync(self) -> PairingRecord:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS native_fleet_pairing (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'unpaired', 'pending', 'paired', 'revoked',
                            'recovery_required'
                        )
                    ),
                    device_id TEXT NOT NULL,
                    attempt_id TEXT,
                    pairing_id TEXT,
                    node_id TEXT,
                    hub_origin TEXT,
                    node_url TEXT,
                    credential_epoch INTEGER,
                    credentials_owned INTEGER NOT NULL DEFAULT 0 CHECK (
                        credentials_owned IN (0, 1)
                    ),
                    credential_write_pending INTEGER NOT NULL DEFAULT 0 CHECK (
                        credential_write_pending IN (0, 1)
                    ),
                    credential_fingerprint TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    paired_at REAL,
                    revoked_at REAL,
                    last_error_code TEXT CHECK (
                        last_error_code IS NULL OR last_error_code IN (
                            'legacy_fleet_credentials_present',
                            'credential_environment_override',
                            'private_environment_invalid',
                            'private_environment_write_failed',
                            'pairing_expired',
                            'pairing_verification_failed',
                            'revoked_by_hub',
                            'state_inconsistent'
                        )
                    )
                )
                """
            )
            columns = {
                str(column["name"])
                for column in connection.execute(
                    "PRAGMA table_info(native_fleet_pairing)"
                ).fetchall()
            }
            if "credential_fingerprint" not in columns:
                connection.execute(
                    "ALTER TABLE native_fleet_pairing "
                    "ADD COLUMN credential_fingerprint TEXT"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO native_fleet_pairing (
                    singleton, state, device_id, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (
                    PairingState.UNPAIRED.value,
                    str(uuid4()),
                    now,
                    now,
                ),
            )
            row = self._row(connection)
            if (
                bool(row["credential_write_pending"])
                and PairingState(row["state"]) is not PairingState.REVOKED
            ) or (
                bool(row["credentials_owned"])
                and not row["credential_fingerprint"]
            ):
                connection.execute(
                    """
                    UPDATE native_fleet_pairing
                       SET state=?, last_error_code=?, updated_at=?
                     WHERE singleton=1
                    """,
                    (
                        PairingState.RECOVERY_REQUIRED.value,
                        PairingErrorCode.STATE_INCONSISTENT.value,
                        now,
                    ),
                )
        return self._status_sync()

    def _row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM native_fleet_pairing WHERE singleton=1"
        ).fetchone()
        if row is None:  # pragma: no cover - protected by initialization
            raise FleetPairingError("Fleet pairing state is missing")
        return row

    def _record(self, row: sqlite3.Row, *, legacy: bool) -> PairingRecord:
        error = row["last_error_code"]
        return PairingRecord(
            state=PairingState(row["state"]),
            device_id=str(row["device_id"]),
            attempt_id=row["attempt_id"],
            pairing_id=row["pairing_id"],
            node_id=row["node_id"],
            hub_origin=row["hub_origin"],
            node_url=row["node_url"],
            credential_epoch=row["credential_epoch"],
            credentials_owned=bool(row["credentials_owned"]),
            credential_write_pending=bool(row["credential_write_pending"]),
            credential_fingerprint=row["credential_fingerprint"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            paired_at=(
                float(row["paired_at"]) if row["paired_at"] is not None else None
            ),
            revoked_at=(
                float(row["revoked_at"])
                if row["revoked_at"] is not None
                else None
            ),
            last_error_code=PairingErrorCode(error) if error else None,
            legacy_credentials_present=legacy,
        )

    def _status_sync(self) -> PairingRecord:
        with self._connect() as connection:
            row = self._row(connection)
        if bool(row["credentials_owned"]):
            row = self._reconcile_owned_environment(row)
            return self._record(row, legacy=False)
        if bool(row["credential_write_pending"]):
            return self._record(row, legacy=False)
        try:
            legacy = bool(self._effective_managed_credentials())
        except PrivateEnvironmentInvalid:
            if (
                PairingState(row["state"]) == PairingState.RECOVERY_REQUIRED
                and row["last_error_code"]
                == PairingErrorCode.PRIVATE_ENVIRONMENT_INVALID.value
            ):
                legacy = False
            else:
                raise
        return self._record(row, legacy=legacy)

    def _reconcile_owned_environment(self, row: sqlite3.Row) -> sqlite3.Row:
        state = PairingState(row["state"])
        if state in {PairingState.UNPAIRED, PairingState.REVOKED}:
            return row
        if not row["credential_fingerprint"] or bool(
            row["credential_write_pending"]
        ):
            self._record_recovery_error(PairingErrorCode.STATE_INCONSISTENT)
        else:
            try:
                file_values = self._file_managed_credentials()
            except PrivateEnvironmentInvalid:
                self._record_recovery_error(
                    PairingErrorCode.PRIVATE_ENVIRONMENT_INVALID
                )
            else:
                if self._has_environment_override(file_values=file_values):
                    self._record_recovery_error(
                        PairingErrorCode.CREDENTIAL_ENVIRONMENT_OVERRIDE
                    )
                else:
                    effective = self._effective_managed_credentials(
                        file_values=file_values
                    )
                    if (
                        set(effective) != set(MANAGED_FLEET_ENV_KEYS)
                        or _credential_fingerprint(effective)
                        != row["credential_fingerprint"]
                    ):
                        self._record_recovery_error(
                            PairingErrorCode.PRIVATE_ENVIRONMENT_INVALID
                        )
        with self._connect() as connection:
            return self._row(connection)

    def _begin_attempt_sync(
        self,
        hub_origin: str,
        node_url: str,
        attempt_id: str,
    ) -> PairingRecord:
        with self._connect() as connection:
            row = self._row(connection)
            state = PairingState(row["state"])
            if state == PairingState.PENDING and row["attempt_id"] == attempt_id:
                if (
                    row["hub_origin"] != hub_origin
                    or row["node_url"] != node_url
                ):
                    raise InvalidPairingTransition(
                        "attempt replay does not match its original endpoints"
                    )
                return self._record(row, legacy=False)
            if state != PairingState.UNPAIRED:
                raise InvalidPairingTransition(
                    f"cannot begin pairing while state is {state.value}"
                )
            if not bool(row["credentials_owned"]) and self._effective_managed_credentials():
                now = time.time()
                connection.execute(
                    """
                    UPDATE native_fleet_pairing
                       SET last_error_code=?, updated_at=? WHERE singleton=1
                    """,
                    (
                        PairingErrorCode.LEGACY_FLEET_CREDENTIALS_PRESENT.value,
                        now,
                    ),
                )
                # Persist the fixed refusal code even though the public call
                # raises after this transaction.
                connection.commit()
                raise LegacyFleetCredentialsPresent(
                    "existing Fleet credentials require an explicit migration"
                )
            now = time.time()
            connection.execute(
                """
                UPDATE native_fleet_pairing
                   SET state=?, attempt_id=?, pairing_id=NULL, node_id=NULL,
                       hub_origin=?, node_url=?, credential_epoch=NULL,
                       paired_at=NULL, revoked_at=NULL, last_error_code=NULL,
                       updated_at=?
                 WHERE singleton=1
                """,
                (
                    PairingState.PENDING.value,
                    attempt_id,
                    hub_origin,
                    node_url,
                    now,
                ),
            )
            return self._record(self._row(connection), legacy=False)

    def _begin_repairing_after_completed_revoke_sync(
        self,
        hub_origin: str,
        node_url: str,
        attempt_id: str,
        revoked_pairing_id: str,
        revoked_node_id: str,
        revoked_credential_epoch: int,
        revoked_attempt_id: str,
        journal_transition: Callable[[sqlite3.Connection, sqlite3.Row], T],
    ) -> T:
        with self._locked_environment(
            exclusive=True,
            create_parent=True,
        ) as locked:
            assert locked is not None
            parent_descriptor, lock_descriptor, lock_identity = locked
            original_text, original_identity = self._read_environment_locked(
                parent_descriptor
            )
            file_values = self._managed_credentials_from_text(original_text)
            if self._effective_managed_credentials(file_values=file_values):
                raise LegacyFleetCredentialsPresent(
                    "existing Fleet credentials require an explicit migration"
                )

            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._row(connection)
                state = PairingState(row["state"])
                no_credentials = (
                    not bool(row["credentials_owned"])
                    and not bool(row["credential_write_pending"])
                    and row["credential_fingerprint"] is None
                )
                exact_revoked_tombstone = (
                    state is PairingState.REVOKED
                    and no_credentials
                    and row["pairing_id"] == revoked_pairing_id
                    and row["node_id"] == revoked_node_id
                    and row["credential_epoch"] == revoked_credential_epoch
                    and row["attempt_id"] == revoked_attempt_id
                    and row["hub_origin"] is not None
                    and row["node_url"] is not None
                    and row["paired_at"] is not None
                    and row["revoked_at"] is not None
                    and row["last_error_code"]
                    == PairingErrorCode.REVOKED_BY_HUB.value
                )
                if not exact_revoked_tombstone:
                    raise InvalidPairingTransition(
                        "re-pairing reset requires the exact revoked tombstone"
                    )

                result = journal_transition(connection, row)
                now = time.time()
                connection.execute(
                    """
                    UPDATE native_fleet_pairing
                       SET state=?, attempt_id=?, pairing_id=NULL, node_id=NULL,
                           hub_origin=?, node_url=?, credential_epoch=NULL,
                           credentials_owned=0, credential_write_pending=0,
                           credential_fingerprint=NULL, paired_at=NULL,
                           revoked_at=NULL, last_error_code=NULL, updated_at=?
                     WHERE singleton=1
                    """,
                    (
                        PairingState.PENDING.value,
                        attempt_id,
                        hub_origin,
                        node_url,
                        now,
                    ),
                )

                current_text, current_identity = self._read_environment_locked(
                    parent_descriptor
                )
                current_values = self._managed_credentials_from_text(current_text)
                if (
                    current_text != original_text
                    or current_identity != original_identity
                    or self._effective_managed_credentials(
                        file_values=current_values
                    )
                ):
                    raise PrivateEnvironmentInvalid(
                        "private Fleet credentials changed during re-pairing reset"
                    )
                self._validate_locked_paths(
                    parent_descriptor,
                    lock_descriptor,
                    lock_identity,
                )
                connection.commit()
                return result

    def _record_assignment_sync(
        self,
        pairing_id: str,
        node_id: str,
        credential_epoch: int,
    ) -> PairingRecord:
        with self._connect() as connection:
            row = self._row(connection)
            if PairingState(row["state"]) != PairingState.PENDING:
                raise InvalidPairingTransition("assignment requires pending pairing")
            existing = (
                row["pairing_id"],
                row["node_id"],
                row["credential_epoch"],
            )
            requested = (pairing_id, node_id, credential_epoch)
            if any(value is not None for value in existing):
                if existing != requested:
                    raise InvalidPairingTransition(
                        "assignment cannot replace the original pairing identity"
                    )
                return self._record(row, legacy=False)
            now = time.time()
            connection.execute(
                """
                UPDATE native_fleet_pairing
                   SET pairing_id=?, node_id=?, credential_epoch=?,
                       last_error_code=NULL, updated_at=?
                 WHERE singleton=1
                """,
                (pairing_id, node_id, credential_epoch, now),
            )
            return self._record(self._row(connection), legacy=False)

    def _activate_credentials_sync(
        self,
        credentials: PairingCredentials,
    ) -> PairingRecord:
        requested_environment = credentials.environment()
        requested_fingerprint = credentials.fingerprint()
        with self._connect() as connection:
            preflight = self._row(connection)
            preflight_state = PairingState(preflight["state"])
            preflight_owned = bool(preflight["credentials_owned"])
            if preflight_owned:
                if preflight["credential_fingerprint"] != requested_fingerprint:
                    raise InvalidPairingTransition(
                        "paired credentials cannot be rotated implicitly"
                    )
            elif preflight_state not in {
                PairingState.PENDING,
                PairingState.RECOVERY_REQUIRED,
            }:
                raise InvalidPairingTransition(
                    "credential activation requires pending pairing"
                )
            elif (
                not preflight["pairing_id"]
                or not preflight["node_id"]
                or not preflight["credential_epoch"]
            ):
                raise InvalidPairingTransition(
                    "credential activation requires a durable assignment"
                )
        try:
            file_values = self._file_managed_credentials()
        except PrivateEnvironmentInvalid as exc:
            self._record_environment_failure(exc)
            raise
        effective = self._effective_managed_credentials(file_values=file_values)
        environment_override = self._has_environment_override(
            file_values=file_values
        )

        with self._connect() as connection:
            row = self._row(connection)
            state = PairingState(row["state"])
            owned = bool(row["credentials_owned"])
            write_pending = bool(row["credential_write_pending"])
            stored_fingerprint = row["credential_fingerprint"]

            if owned:
                if stored_fingerprint != requested_fingerprint:
                    raise InvalidPairingTransition(
                        "paired credentials cannot be rotated implicitly"
                    )
                if environment_override:
                    connection.execute(
                        """
                        UPDATE native_fleet_pairing
                           SET state=?, last_error_code=?, updated_at=?
                         WHERE singleton=1
                        """,
                        (
                            PairingState.RECOVERY_REQUIRED.value,
                            PairingErrorCode.CREDENTIAL_ENVIRONMENT_OVERRIDE.value,
                            time.time(),
                        ),
                    )
                    connection.commit()
                    raise PrivateEnvironmentInvalid(
                        "launch environment overrides the paired credential file"
                    )
                if effective == requested_environment:
                    return self._record(row, legacy=False)
                if state not in {
                    PairingState.PENDING,
                    PairingState.PAIRED,
                    PairingState.RECOVERY_REQUIRED,
                }:
                    raise InvalidPairingTransition(
                        "revoked credentials cannot be restored"
                    )
            elif state not in {
                PairingState.PENDING,
                PairingState.RECOVERY_REQUIRED,
            }:
                raise InvalidPairingTransition(
                    "credential activation requires pending pairing"
                )
            elif (
                not row["pairing_id"]
                or not row["node_id"]
                or not row["credential_epoch"]
            ):
                raise InvalidPairingTransition(
                    "credential activation requires a durable assignment"
                )

            if (
                write_pending
                and stored_fingerprint
                and stored_fingerprint != requested_fingerprint
            ):
                raise InvalidPairingTransition(
                    "credential recovery must replay the original bundle"
                )
            if not owned and not write_pending and effective:
                connection.execute(
                    """
                    UPDATE native_fleet_pairing SET last_error_code=?, updated_at=?
                    WHERE singleton=1
                    """,
                    (
                        PairingErrorCode.LEGACY_FLEET_CREDENTIALS_PRESENT.value,
                        time.time(),
                    ),
                )
                connection.commit()
                raise LegacyFleetCredentialsPresent(
                    "existing Fleet credentials require an explicit migration"
                )
            if write_pending and environment_override:
                connection.execute(
                    """
                    UPDATE native_fleet_pairing
                       SET state=?, last_error_code=?, updated_at=?
                     WHERE singleton=1
                    """,
                    (
                        PairingState.RECOVERY_REQUIRED.value,
                        PairingErrorCode.CREDENTIAL_ENVIRONMENT_OVERRIDE.value,
                        time.time(),
                    ),
                )
                connection.commit()
                raise PrivateEnvironmentInvalid(
                    "launch environment overrides the paired credential file"
                )
            connection.execute(
                """
                UPDATE native_fleet_pairing
                   SET credential_write_pending=1, credential_fingerprint=?,
                       updated_at=?
                 WHERE singleton=1
                """,
                (requested_fingerprint, time.time()),
            )

        try:
            self._update_environment_file(replacements=requested_environment)
        except Exception as exc:
            self._record_environment_failure(exc)
            if isinstance(exc, FleetPairingError):
                raise
            raise PrivateEnvironmentWriteFailed(
                "could not update the private Fleet environment"
            ) from exc

        with self._connect() as connection:
            if owned:
                connection.execute(
                    """
                    UPDATE native_fleet_pairing
                       SET credentials_owned=1, credential_write_pending=0,
                           credential_fingerprint=?, updated_at=?
                     WHERE singleton=1
                    """,
                    (requested_fingerprint, time.time()),
                )
            else:
                connection.execute(
                    """
                    UPDATE native_fleet_pairing
                       SET credentials_owned=1, credential_write_pending=0,
                           credential_fingerprint=?, state=?,
                           last_error_code=NULL, updated_at=?
                     WHERE singleton=1
                    """,
                    (
                        requested_fingerprint,
                        PairingState.PENDING.value,
                        time.time(),
                    ),
                )
            return self._record(self._row(connection), legacy=False)

    def _record_environment_failure(self, exc: BaseException) -> None:
        code = (
            PairingErrorCode.PRIVATE_ENVIRONMENT_INVALID
            if isinstance(exc, PrivateEnvironmentInvalid)
            else PairingErrorCode.PRIVATE_ENVIRONMENT_WRITE_FAILED
        )
        self._record_recovery_error(code)

    def _staged_credentials_sync(self) -> PairingCredentials:
        with self._connect() as connection:
            row = self._row(connection)
        if (
            PairingState(row["state"])
            not in {
                PairingState.PENDING,
                PairingState.PAIRED,
                PairingState.RECOVERY_REQUIRED,
            }
            or not bool(row["credentials_owned"])
            or bool(row["credential_write_pending"])
            or not row["credential_fingerprint"]
        ):
            raise InvalidPairingTransition(
                "activation retry requires one durable owned credential bundle"
            )
        try:
            file_values = self._file_managed_credentials()
        except PrivateEnvironmentInvalid as exc:
            self._record_environment_failure(exc)
            raise
        if self._has_environment_override(file_values=file_values):
            self._record_recovery_error(
                PairingErrorCode.CREDENTIAL_ENVIRONMENT_OVERRIDE
            )
            raise PrivateEnvironmentInvalid(
                "launch environment overrides the paired credential file"
            )
        effective = self._effective_managed_credentials(file_values=file_values)
        if set(effective) != set(MANAGED_FLEET_ENV_KEYS):
            invalid = PrivateEnvironmentInvalid(
                "private Fleet credentials do not match the pending pairing"
            )
            self._record_environment_failure(invalid)
            raise invalid
        credentials = PairingCredentials(
            snapshot_key=effective[FLEET_SNAPSHOT_KEY],
            dispatch_key=effective[FLEET_DISPATCH_KEY],
            management_key=effective[FLEET_MANAGEMENT_KEY],
        ).validated()
        if not hmac.compare_digest(
            credentials.fingerprint(),
            str(row["credential_fingerprint"]),
        ):
            invalid = PrivateEnvironmentInvalid(
                "private Fleet credentials do not match the pending pairing"
            )
            self._record_environment_failure(invalid)
            raise invalid
        return credentials

    def _record_recovery_error(self, code: PairingErrorCode) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE native_fleet_pairing
                   SET state=?, last_error_code=?, updated_at=?
                 WHERE singleton=1
                """,
                (
                    PairingState.RECOVERY_REQUIRED.value,
                    code.value,
                    time.time(),
                ),
            )

    def _mark_paired_sync(self) -> PairingRecord:
        with self._connect() as connection:
            preflight = self._row(connection)
            if PairingState(preflight["state"]) not in {
                PairingState.PENDING,
                PairingState.RECOVERY_REQUIRED,
            }:
                raise InvalidPairingTransition(
                    "pair confirmation requires pending state"
                )
            if (
                not preflight["pairing_id"]
                or not preflight["node_id"]
                or not preflight["credential_epoch"]
                or not bool(preflight["credentials_owned"])
                or bool(preflight["credential_write_pending"])
                or not preflight["credential_fingerprint"]
            ):
                raise InvalidPairingTransition(
                    "pair confirmation requires identity and durable credentials"
                )
            expected_fingerprint = str(preflight["credential_fingerprint"])

        try:
            file_values = self._file_managed_credentials()
        except PrivateEnvironmentInvalid as exc:
            self._record_environment_failure(exc)
            raise
        if self._has_environment_override(file_values=file_values):
            self._record_recovery_error(
                PairingErrorCode.CREDENTIAL_ENVIRONMENT_OVERRIDE
            )
            raise PrivateEnvironmentInvalid(
                "launch environment overrides the paired credential file"
            )
        effective = self._effective_managed_credentials(file_values=file_values)
        if (
            set(effective) != set(MANAGED_FLEET_ENV_KEYS)
            or _credential_fingerprint(effective) != expected_fingerprint
        ):
            invalid = PrivateEnvironmentInvalid(
                "private Fleet credentials do not match the pending pairing"
            )
            self._record_environment_failure(invalid)
            raise invalid

        with self._connect() as connection:
            row = self._row(connection)
            if PairingState(row["state"]) not in {
                PairingState.PENDING,
                PairingState.RECOVERY_REQUIRED,
            }:
                raise InvalidPairingTransition("pair confirmation requires pending state")
            if (
                not row["pairing_id"]
                or not row["node_id"]
                or not row["credential_epoch"]
                or not bool(row["credentials_owned"])
                or bool(row["credential_write_pending"])
                or row["credential_fingerprint"] != expected_fingerprint
            ):
                raise InvalidPairingTransition(
                    "pair confirmation requires identity and durable credentials"
                )
            now = time.time()
            connection.execute(
                """
                UPDATE native_fleet_pairing
                   SET state=?, paired_at=?, revoked_at=NULL,
                       last_error_code=NULL, updated_at=?
                 WHERE singleton=1
                """,
                (PairingState.PAIRED.value, now, now),
            )
            return self._record(self._row(connection), legacy=False)

    def _mark_revoked_sync(self) -> PairingRecord:
        with self._connect() as connection:
            row = self._row(connection)
            if PairingState(row["state"]) is PairingState.REVOKED:
                return self._record(row, legacy=False)
            if PairingState(row["state"]) not in {
                PairingState.PENDING,
                PairingState.PAIRED,
                PairingState.RECOVERY_REQUIRED,
            }:
                raise InvalidPairingTransition("only an enrolled pairing can be revoked")
            now = time.time()
            connection.execute(
                """
                UPDATE native_fleet_pairing
                   SET state=?, revoked_at=?, last_error_code=?, updated_at=?
                 WHERE singleton=1
                """,
                (
                    PairingState.REVOKED.value,
                    now,
                    PairingErrorCode.REVOKED_BY_HUB.value,
                    now,
                ),
            )
            return self._record(self._row(connection), legacy=False)

    def _retire_revoked_credentials_sync(self) -> PairingRecord:
        with self._connect() as connection:
            row = self._row(connection)
            if PairingState(row["state"]) is not PairingState.REVOKED:
                raise InvalidPairingTransition(
                    "credential retirement requires a revoked tombstone"
                )
            owned = bool(row["credentials_owned"])
            pending = bool(row["credential_write_pending"])
            fingerprint = row["credential_fingerprint"]
            if not owned and not pending:
                return self._record(row, legacy=False)
            if not owned or not fingerprint:
                raise InvalidPairingTransition(
                    "revoked credential retirement state is inconsistent"
                )
            connection.execute(
                """
                UPDATE native_fleet_pairing
                   SET credential_write_pending=1, updated_at=?
                 WHERE singleton=1
                """,
                (time.time(),),
            )

        try:
            file_values = self._file_managed_credentials()
            process_values = {
                key: value
                for key in MANAGED_FLEET_ENV_KEYS
                if (value := self._process_environment.get(key, "").strip())
            }
            candidates = [
                values for values in (file_values, process_values) if values
            ]
            for values in candidates:
                if (
                    set(values) != set(MANAGED_FLEET_ENV_KEYS)
                    or not hmac.compare_digest(
                        _credential_fingerprint(values), str(fingerprint)
                    )
                ):
                    raise PrivateEnvironmentInvalid(
                        "private Fleet credentials no longer match revoked ownership"
                    )
            if len(candidates) == 2 and candidates[0] != candidates[1]:
                raise PrivateEnvironmentInvalid(
                    "launch environment overrides revoked paired credentials"
                )
            if candidates:
                self._update_environment_file(
                    clearing=set(MANAGED_FLEET_ENV_KEYS),
                    expected_values=candidates[0],
                )
        except Exception as exc:
            code = (
                PairingErrorCode.PRIVATE_ENVIRONMENT_INVALID
                if isinstance(exc, PrivateEnvironmentInvalid)
                else PairingErrorCode.PRIVATE_ENVIRONMENT_WRITE_FAILED
            )
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE native_fleet_pairing
                       SET state=?, credential_write_pending=1,
                           last_error_code=?, updated_at=?
                     WHERE singleton=1
                    """,
                    (
                        PairingState.REVOKED.value,
                        code.value,
                        time.time(),
                    ),
                )
            if isinstance(exc, FleetPairingError):
                raise
            raise PrivateEnvironmentWriteFailed(
                "could not retire the private Fleet environment"
            ) from exc

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE native_fleet_pairing
                   SET state=?, credentials_owned=0,
                       credential_write_pending=0,
                       credential_fingerprint=NULL,
                       last_error_code=?, updated_at=?
                 WHERE singleton=1
                """,
                (
                    PairingState.REVOKED.value,
                    PairingErrorCode.REVOKED_BY_HUB.value,
                    time.time(),
                ),
            )
            return self._record(self._row(connection), legacy=False)

    def _mark_recovery_required_sync(
        self,
        error_code: PairingErrorCode,
    ) -> PairingRecord:
        with self._connect() as connection:
            row = self._row(connection)
            if PairingState(row["state"]) not in {
                PairingState.PENDING,
                PairingState.PAIRED,
                PairingState.RECOVERY_REQUIRED,
            }:
                raise InvalidPairingTransition(
                    "this pairing state cannot enter recovery"
                )
            connection.execute(
                """
                UPDATE native_fleet_pairing
                   SET state=?, last_error_code=?, updated_at=? WHERE singleton=1
                """,
                (
                    PairingState.RECOVERY_REQUIRED.value,
                    error_code.value,
                    time.time(),
                ),
            )
            return self._record(self._row(connection), legacy=False)

    def _clear_pairing_sync(self) -> PairingRecord:
        with self._connect() as connection:
            row = self._row(connection)
            state = PairingState(row["state"])
            if state == PairingState.PAIRED:
                raise InvalidPairingTransition(
                    "paired credentials must be revoked before they are cleared"
                )
            owned = bool(row["credentials_owned"]) or bool(
                row["credential_write_pending"]
            )
            if owned:
                connection.execute(
                    """
                    UPDATE native_fleet_pairing
                       SET credential_write_pending=1, updated_at=? WHERE singleton=1
                    """,
                    (time.time(),),
                )
        if owned:
            try:
                self._update_environment_file(clearing=set(MANAGED_FLEET_ENV_KEYS))
            except Exception as exc:
                self._record_environment_failure(exc)
                if isinstance(exc, FleetPairingError):
                    raise
                raise PrivateEnvironmentWriteFailed(
                    "could not clear the private Fleet environment"
                ) from exc
        with self._connect() as connection:
            now = time.time()
            connection.execute(
                """
                UPDATE native_fleet_pairing
                   SET state=?, attempt_id=NULL, pairing_id=NULL, node_id=NULL,
                       hub_origin=NULL, node_url=NULL, credential_epoch=NULL,
                       credentials_owned=0, credential_write_pending=0,
                       credential_fingerprint=NULL,
                       paired_at=NULL, revoked_at=NULL, last_error_code=NULL,
                       updated_at=?
                 WHERE singleton=1
                """,
                (PairingState.UNPAIRED.value, now),
            )
            return self._record(
                self._row(connection),
                legacy=bool(self._effective_managed_credentials()),
            )

    def _environment_text(self) -> str:
        parent = self.environment_path.parent
        try:
            parent.lstat()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise PrivateEnvironmentInvalid(
                "could not inspect the private environment directory"
            ) from exc

        with self._locked_environment(exclusive=False, create_parent=False) as locked:
            if locked is None:
                return ""
            parent_descriptor, lock_descriptor, lock_identity = locked
            text, identity = self._read_environment_locked(parent_descriptor)
            self._validate_locked_paths(
                parent_descriptor,
                lock_descriptor,
                lock_identity,
            )
            if self._current_environment_identity(parent_descriptor) != identity:
                raise PrivateEnvironmentInvalid(
                    "private Fleet environment changed while it was read"
                )
            return text

    @property
    def _environment_lock_name(self) -> str:
        return f"{self.environment_path.name}{PRIVATE_ENVIRONMENT_LOCK_SUFFIX}"

    @staticmethod
    def _identity(metadata: os.stat_result) -> _FileIdentity:
        return _FileIdentity.from_stat(metadata)

    @staticmethod
    def _validate_owned_regular(
        metadata: os.stat_result,
        *,
        label: str,
    ) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise PrivateEnvironmentInvalid(f"{label} must be a regular file")
        if metadata.st_uid != os.geteuid():
            raise PrivateEnvironmentInvalid(f"{label} must be owned by this user")
        if metadata.st_nlink != 1:
            raise PrivateEnvironmentInvalid(f"{label} must not be hard linked")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise PrivateEnvironmentInvalid(
                f"{label} must not be writable by group or other users"
            )

    def _open_environment_directory(self, *, create: bool) -> int | None:
        parent = self.environment_path.parent
        try:
            metadata = parent.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                parent.mkdir(parents=True, mode=0o700)
                metadata = parent.lstat()
            except (OSError, FileNotFoundError) as exc:
                raise PrivateEnvironmentWriteFailed(
                    "could not create the private environment directory"
                ) from exc
        except OSError as exc:
            raise PrivateEnvironmentInvalid(
                "could not inspect the private environment directory"
            ) from exc

        if stat.S_ISLNK(metadata.st_mode):
            raise PrivateEnvironmentInvalid(
                "refusing a symlinked private environment directory"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise PrivateEnvironmentInvalid(
                "private environment parent must be a directory"
            )
        if metadata.st_uid != os.geteuid():
            raise PrivateEnvironmentInvalid(
                "private environment directory must be owned by this user"
            )
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise PrivateEnvironmentInvalid(
                "private environment directory must not be writable by other users"
            )

        flags = os.O_RDONLY | os.O_CLOEXEC
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(parent, flags)
        except OSError as exc:
            raise PrivateEnvironmentInvalid(
                "could not securely open the private environment directory"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or self._identity(opened) != self._identity(metadata)
            ):
                raise PrivateEnvironmentInvalid(
                    "private environment directory changed while it was opened"
                )
            if create:
                os.fchmod(descriptor, 0o700)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _validate_directory_binding(self, descriptor: int) -> None:
        try:
            opened = os.fstat(descriptor)
            current = self.environment_path.parent.lstat()
        except OSError as exc:
            raise PrivateEnvironmentInvalid(
                "could not revalidate the private environment directory"
            ) from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or current.st_uid != os.geteuid()
            or self._identity(current) != self._identity(opened)
        ):
            raise PrivateEnvironmentInvalid(
                "private environment directory changed during the update"
            )

    def _validate_lock_binding(
        self,
        parent_descriptor: int,
        lock_descriptor: int,
        expected: _FileIdentity,
    ) -> None:
        try:
            opened = os.fstat(lock_descriptor)
            current = os.stat(
                self._environment_lock_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise PrivateEnvironmentInvalid(
                "could not revalidate the private environment lock"
            ) from exc
        self._validate_owned_regular(opened, label="private environment lock")
        self._validate_owned_regular(current, label="private environment lock")
        if self._identity(opened) != expected or self._identity(current) != expected:
            raise PrivateEnvironmentInvalid(
                "private environment lock changed during the update"
            )

    def _validate_locked_paths(
        self,
        parent_descriptor: int,
        lock_descriptor: int,
        lock_identity: _FileIdentity,
    ) -> None:
        self._validate_directory_binding(parent_descriptor)
        self._validate_lock_binding(
            parent_descriptor,
            lock_descriptor,
            lock_identity,
        )

    @contextmanager
    def _locked_environment(
        self,
        *,
        exclusive: bool,
        create_parent: bool,
    ) -> Iterator[tuple[int, int, _FileIdentity] | None]:
        parent_descriptor = self._open_environment_directory(create=create_parent)
        if parent_descriptor is None:
            yield None
            return
        lock_descriptor = -1
        locked = False
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                lock_descriptor = os.open(
                    self._environment_lock_name,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except OSError as exc:
                message = "refusing an unsafe private environment lock"
                if exc.errno not in {errno.ELOOP, errno.EMLINK}:
                    message = "could not open the private environment lock"
                raise PrivateEnvironmentInvalid(message) from exc
            lock_metadata = os.fstat(lock_descriptor)
            self._validate_owned_regular(
                lock_metadata,
                label="private environment lock",
            )
            os.fchmod(lock_descriptor, 0o600)
            try:
                fcntl.flock(
                    lock_descriptor,
                    fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                )
            except OSError as exc:
                raise PrivateEnvironmentInvalid(
                    "could not acquire the private environment lock"
                ) from exc
            locked = True
            lock_identity = self._identity(os.fstat(lock_descriptor))
            self._validate_locked_paths(
                parent_descriptor,
                lock_descriptor,
                lock_identity,
            )
            yield parent_descriptor, lock_descriptor, lock_identity
        finally:
            if locked:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            if lock_descriptor >= 0:
                os.close(lock_descriptor)
            os.close(parent_descriptor)

    def _current_environment_identity(
        self,
        parent_descriptor: int,
    ) -> _FileIdentity | None:
        try:
            metadata = os.stat(
                self.environment_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PrivateEnvironmentInvalid(
                "could not inspect the private Fleet environment"
            ) from exc
        self._validate_owned_regular(
            metadata,
            label="private Fleet environment",
        )
        return self._identity(metadata)

    def _read_environment_locked(
        self,
        parent_descriptor: int,
    ) -> tuple[str, _FileIdentity | None]:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                self.environment_path.name,
                flags,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return "", None
        except OSError as exc:
            raise PrivateEnvironmentInvalid(
                "could not securely open the private Fleet environment"
            ) from exc
        descriptor_open = True
        try:
            metadata = os.fstat(descriptor)
            self._validate_owned_regular(
                metadata,
                label="private Fleet environment",
            )
            identity = self._identity(metadata)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor_open = False
                text = stream.read()
            return text, identity
        except (OSError, UnicodeError) as exc:
            raise PrivateEnvironmentInvalid(
                "could not read the private Fleet environment"
            ) from exc
        finally:
            if descriptor_open:
                os.close(descriptor)

    @staticmethod
    def _managed_credentials_from_text(text: str) -> dict[str, str]:
        output: dict[str, str] = {}
        seen: set[str] = set()
        for raw_line in text.splitlines():
            parsed = _assignment(raw_line)
            if parsed is None:
                continue
            key, value = parsed
            if key not in MANAGED_FLEET_ENV_KEYS:
                continue
            if key in seen:
                raise PrivateEnvironmentInvalid(
                    "private Fleet environment contains duplicate managed keys"
                )
            seen.add(key)
            if value:
                output[key] = value
        return output

    def _file_managed_credentials(self) -> dict[str, str]:
        return self._managed_credentials_from_text(self._environment_text())

    def _effective_managed_credentials(
        self,
        *,
        file_values: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        if file_values is None:
            file_values = self._file_managed_credentials()
        output: dict[str, str] = {}
        for key in MANAGED_FLEET_ENV_KEYS:
            value = self._process_environment.get(key, "").strip()
            if not value:
                value = file_values.get(key, "").strip()
            if value:
                output[key] = value
        return output

    def _has_environment_override(
        self,
        *,
        file_values: Mapping[str, str] | None = None,
    ) -> bool:
        if file_values is None:
            file_values = self._file_managed_credentials()
        return any(
            bool(process_value := self._process_environment.get(key, "").strip())
            and process_value != file_values.get(key, "")
            for key in MANAGED_FLEET_ENV_KEYS
        )

    def _render_environment(
        self,
        existing: str,
        *,
        replacements: Mapping[str, str],
        clearing: set[str],
    ) -> str:
        output: list[str] = []
        handled: set[str] = set()
        for line in existing.splitlines(keepends=True):
            parsed = _assignment(line.rstrip("\r\n"))
            if parsed is None or parsed[0] not in MANAGED_FLEET_ENV_KEYS:
                output.append(line)
                continue
            key = parsed[0]
            if key in handled:
                continue
            handled.add(key)
            if key in clearing:
                continue
            if key in replacements:
                output.append(f"{key}={replacements[key]}\n")
            else:
                output.append(line)
        for key in MANAGED_FLEET_ENV_KEYS:
            if key in handled or key in clearing or key not in replacements:
                continue
            if output and not output[-1].endswith(("\n", "\r")):
                output.append("\n")
            output.append(f"{key}={replacements[key]}\n")
        return "".join(output)

    def _update_environment_file(
        self,
        *,
        replacements: Mapping[str, str] | None = None,
        clearing: set[str] | None = None,
        expected_values: Mapping[str, str] | None = None,
    ) -> None:
        replacements = dict(replacements or {})
        clearing = set(clearing or set())
        expected_values = (
            dict(expected_values) if expected_values is not None else None
        )
        if not set(replacements).issubset(MANAGED_FLEET_ENV_KEYS) or not clearing.issubset(
            MANAGED_FLEET_ENV_KEYS
        ):
            raise ValueError("only Fleet pairing credentials may be updated")
        if expected_values is not None and set(expected_values) != set(
            MANAGED_FLEET_ENV_KEYS
        ):
            raise ValueError("expected Fleet pairing credentials are incomplete")
        previous: dict[str, str]
        try:
            with self._locked_environment(
                exclusive=True,
                create_parent=True,
            ) as locked:
                assert locked is not None
                parent_descriptor, lock_descriptor, lock_identity = locked
                existing, original_identity = self._read_environment_locked(
                    parent_descriptor
                )
                previous = self._managed_credentials_from_text(existing)
                if expected_values is not None:
                    if previous and previous != expected_values:
                        raise PrivateEnvironmentInvalid(
                            "private Fleet credentials changed before retirement"
                        )
                    for key, expected in expected_values.items():
                        process_value = self._process_environment.get(
                            key, ""
                        ).strip()
                        if process_value and process_value != expected:
                            raise PrivateEnvironmentInvalid(
                                "launch environment overrides paired credentials"
                            )
                rendered = self._render_environment(
                    existing,
                    replacements=replacements,
                    clearing=clearing,
                )
                temporary_name = (
                    f".{self.environment_path.name}.{uuid4().hex}.tmp"
                )
                temporary_descriptor = -1
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    temporary_descriptor = os.open(
                        temporary_name,
                        flags,
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                    os.fchmod(temporary_descriptor, 0o600)
                    payload = rendered.encode("utf-8")
                    offset = 0
                    while offset < len(payload):
                        written = os.write(temporary_descriptor, payload[offset:])
                        if written <= 0:
                            raise OSError("short private environment write")
                        offset += written
                    os.fsync(temporary_descriptor)
                    os.close(temporary_descriptor)
                    temporary_descriptor = -1

                    self._validate_locked_paths(
                        parent_descriptor,
                        lock_descriptor,
                        lock_identity,
                    )
                    if (
                        self._current_environment_identity(parent_descriptor)
                        != original_identity
                    ):
                        raise PrivateEnvironmentInvalid(
                            "private Fleet environment changed during the update"
                        )
                    os.rename(
                        temporary_name,
                        self.environment_path.name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                    os.fsync(parent_descriptor)
                finally:
                    if temporary_descriptor >= 0:
                        os.close(temporary_descriptor)
                    try:
                        os.unlink(temporary_name, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
        except PrivateEnvironmentError:
            raise
        except (OSError, UnicodeError) as exc:
            raise PrivateEnvironmentWriteFailed(
                "could not write the private Fleet environment"
            ) from exc

        for key, value in replacements.items():
            self._process_environment[key] = value
        for key in clearing:
            expected = (
                expected_values.get(key)
                if expected_values is not None
                else previous.get(key)
            )
            if self._process_environment.get(key) == expected:
                self._process_environment.pop(key, None)


__all__ = [
    "FLEET_DISPATCH_KEY",
    "FLEET_MANAGEMENT_KEY",
    "FLEET_SNAPSHOT_KEY",
    "MANAGED_FLEET_ENV_KEYS",
    "FleetPairingError",
    "FleetPairingStore",
    "InvalidPairingTransition",
    "LegacyFleetCredentialsPresent",
    "PairingCredentials",
    "PairingErrorCode",
    "PairingRecord",
    "PairingState",
    "PairingStoreClosed",
    "PrivateEnvironmentError",
    "PrivateEnvironmentInvalid",
    "PrivateEnvironmentWriteFailed",
]
