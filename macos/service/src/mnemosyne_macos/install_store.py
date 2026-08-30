"""Durable SQLite state for native Hugging Face installs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import wraps
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, TypeVar, cast
import uuid

from .install_launch import (
    InstallLaunchContract,
    InstallLaunchInput,
    install_launch_dict,
    install_launch_from_json,
    install_launch_json,
)
from .install_provenance import (
    ArtifactAuthority,
    CleanupEligibility,
    CleanupInstallation,
    CleanupReason,
    ExclusiveManagedProof,
    InstallationProvenance,
    MANAGED_CREATION_MARKER_PATH,
    ManagedCreationClaim,
    ManagedCreationIntent,
    OwnedFile,
    OwnershipClass,
    PROVENANCE_REVISION,
    ProvenanceConflictError,
    ProvenanceDataError,
    ProvenanceProofRejected,
    SourceKind,
    allowed_hf_local_metadata_paths,
    canonical_owned_files,
    canonical_owned_files_json,
    decide_cleanup_eligibility,
    default_provenance,
    is_hf_local_metadata_path,
    is_default_unknown,
    owned_files_from_canonical_json,
    owned_manifest_digest,
    provenance_database_fields,
    provenance_from_database_row,
    provenance_from_exclusive_proof,
    validate_claim_for_intent,
    validate_managed_creation_claim,
    validate_managed_creation_intent,
)


F = TypeVar("F", bound=Callable[..., Any])


def _synchronized(method: F) -> F:
    @wraps(method)
    def locked(self: "InstallStore", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(F, locked)


def _expected_manifest_fields(
    expected_files: tuple[OwnedFile, ...] | list[OwnedFile] | None,
    expected_manifest_digest: str | None,
    *,
    download_files: tuple[str, ...] | list[str],
    total_bytes: int | None,
) -> tuple[str | None, str | None]:
    if expected_files is None and expected_manifest_digest is None:
        return None, None
    if expected_files is None or expected_manifest_digest is None:
        raise ProvenanceDataError("expected_artifact_manifest_invalid")
    normalized = canonical_owned_files(expected_files)
    if (
        tuple(expected_files) != normalized
        or tuple(item.path for item in normalized) != tuple(download_files)
        or isinstance(total_bytes, bool)
        or not isinstance(total_bytes, int)
        or total_bytes <= 0
        or sum(item.size_bytes for item in normalized) != total_bytes
        or owned_manifest_digest(normalized) != expected_manifest_digest
    ):
        raise ProvenanceDataError("expected_artifact_manifest_invalid")
    return canonical_owned_files_json(normalized), expected_manifest_digest


@dataclass(frozen=True, slots=True)
class InstallRecord:
    id: str
    repo_id: str
    engine: str
    storage: str
    alias: str
    destination: str
    status: str
    revision: str | None = None
    filename: str | None = None
    projector_filename: str | None = None
    context_length: int | None = None
    files_json: str | None = None
    expected_files_json: str | None = None
    expected_manifest_digest: str | None = None
    capabilities_json: str | None = None
    family: str | None = None
    launch_json: str | None = None
    bytes_downloaded: int = 0
    total_bytes: int | None = None
    download_speed_bps: float | None = None
    hidden: int = 0
    error: str | None = None
    pid: int | None = None
    created_at: float = 0
    updated_at: float = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        encoded = payload.pop("files_json")
        payload.pop("expected_files_json")
        payload.pop("expected_manifest_digest")
        capabilities_encoded = payload.pop("capabilities_json")
        launch_encoded = payload.pop("launch_json")
        payload.pop("hidden")
        try:
            files = json.loads(encoded) if encoded else []
        except (TypeError, ValueError, json.JSONDecodeError):
            files = []
        try:
            capabilities = (
                json.loads(capabilities_encoded) if capabilities_encoded else None
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            capabilities = None
        payload["download_files"] = files if isinstance(files, list) else []
        payload["capabilities"] = (
            capabilities
            if isinstance(capabilities, list)
            and all(isinstance(item, str) for item in capabilities)
            else None
        )
        launch = install_launch_from_json(self.engine, launch_encoded)
        payload["launch_contract"] = (
            install_launch_dict(launch) if launch is not None else None
        )
        return payload

    @property
    def capabilities(self) -> tuple[str, ...] | None:
        try:
            value = json.loads(self.capabilities_json) if self.capabilities_json else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, str) for item in value)
        ):
            return tuple(value)
        return None

    @property
    def launch_contract(self) -> InstallLaunchContract | None:
        """Return the exact typed launch contract, refusing corrupt state."""

        return install_launch_from_json(self.engine, self.launch_json)

    @property
    def expected_files(self) -> tuple[OwnedFile, ...] | None:
        """Return the private signed expectation, refusing corrupt state."""

        if self.expected_files_json is None:
            return None
        return owned_files_from_canonical_json(self.expected_files_json)


# Installation identity is append-only.  Generic transitions may change only
# operational lifecycle/progress fields; repo, engine, storage, alias,
# destination, revision, selected artifacts, and declared totals are immutable
# even before an exclusive-ownership proof is attached.
_INSTALL_MUTABLE_FIELDS = frozenset(
    {
        "status",
        "bytes_downloaded",
        "download_speed_bps",
        "hidden",
        "error",
        "pid",
    }
)


@dataclass(frozen=True, slots=True)
class InstallEvent:
    sequence: int
    install_id: str
    event: str
    status: str
    bytes_downloaded: int
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CreationIntentRecord:
    intent: ManagedCreationIntent
    state: str
    created_at: float
    updated_at: float


class InstallStore:
    def __init__(self, database_path: str | Path) -> None:
        self._lock = threading.RLock()
        path = Path(database_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_model_installs (
                id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                engine TEXT NOT NULL,
                storage TEXT NOT NULL,
                alias TEXT NOT NULL,
                destination TEXT NOT NULL,
                status TEXT NOT NULL,
                revision TEXT,
                filename TEXT,
                projector_filename TEXT,
                context_length INTEGER,
                files_json TEXT,
                expected_files_json TEXT CHECK (
                    expected_files_json IS NULL
                    OR length(expected_files_json) <= 1048576
                ),
                expected_manifest_digest TEXT,
                capabilities_json TEXT,
                family TEXT,
                launch_json TEXT CHECK (
                    launch_json IS NULL OR length(launch_json) <= 1024
                ),
                bytes_downloaded INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER,
                download_speed_bps REAL,
                hidden INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                pid INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        columns = {
            str(row[1])
            for row in self._connection.execute(
                "PRAGMA table_info(native_model_installs)"
            ).fetchall()
        }
        if "projector_filename" not in columns:
            self._connection.execute(
                "ALTER TABLE native_model_installs ADD COLUMN projector_filename TEXT"
            )
        if "context_length" not in columns:
            self._connection.execute(
                "ALTER TABLE native_model_installs ADD COLUMN context_length INTEGER"
            )
        if "files_json" not in columns:
            self._connection.execute(
                "ALTER TABLE native_model_installs ADD COLUMN files_json TEXT"
            )
        if "expected_files_json" not in columns:
            self._connection.execute(
                "ALTER TABLE native_model_installs ADD COLUMN expected_files_json TEXT"
            )
        if "expected_manifest_digest" not in columns:
            self._connection.execute(
                "ALTER TABLE native_model_installs "
                "ADD COLUMN expected_manifest_digest TEXT"
            )
        if "capabilities_json" not in columns:
            self._connection.execute(
                "ALTER TABLE native_model_installs ADD COLUMN capabilities_json TEXT"
            )
        if "download_speed_bps" not in columns:
            self._connection.execute(
                "ALTER TABLE native_model_installs ADD COLUMN download_speed_bps REAL"
            )
        if "hidden" not in columns:
            self._connection.execute(
                "ALTER TABLE native_model_installs "
                "ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"
            )
        if "launch_json" not in columns:
            self._connection.execute(
                "ALTER TABLE native_model_installs ADD COLUMN launch_json TEXT"
            )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_model_install_provenance (
                installation_id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL DEFAULT 'managed_download' CHECK (
                    source_kind IN (
                        'managed_download', 'local_import',
                        'legacy_migration', 'external_reference'
                    )
                ),
                ownership_class TEXT NOT NULL DEFAULT 'unknown' CHECK (
                    ownership_class IN (
                        'exclusive_managed', 'user_owned', 'external_owned',
                        'shared', 'unknown'
                    )
                ),
                storage_location_id TEXT,
                storage_binding_generation INTEGER CHECK (
                    storage_binding_generation IS NULL OR (
                        storage_binding_generation >= 1
                        AND storage_binding_generation <= 2147483647
                    )
                ),
                storage_lexical_root TEXT,
                storage_volume_uuid TEXT,
                storage_scope_id TEXT,
                lexical_destination TEXT,
                destination_binding_digest TEXT,
                catalog_id TEXT,
                logical_model_id TEXT,
                artifact_id TEXT,
                recipe_id TEXT,
                resolved_revision TEXT,
                catalog_digest TEXT,
                artifact_authority TEXT CHECK (
                    artifact_authority IS NULL OR artifact_authority IN (
                        'signed_catalog', 'local_pinned_discovery'
                    )
                ),
                source_identity_digest TEXT,
                manifest_digest TEXT,
                owned_files_json TEXT CHECK (
                    owned_files_json IS NULL OR length(owned_files_json) <= 1048576
                ),
                destination_state_before TEXT NOT NULL DEFAULT 'unknown' CHECK (
                    destination_state_before IN (
                        'absent', 'empty', 'nonempty', 'unknown'
                    )
                ),
                destination_created_by_transaction INTEGER CHECK (
                    destination_created_by_transaction IS NULL
                    OR destination_created_by_transaction IN (0, 1)
                ),
                preexisting_entries_json TEXT CHECK (
                    preexisting_entries_json IS NULL
                    OR length(preexisting_entries_json) <= 1048576
                ),
                extra_entries_json TEXT CHECK (
                    extra_entries_json IS NULL
                    OR length(extra_entries_json) <= 1048576
                ),
                creation_transaction_id TEXT,
                directory_device INTEGER CHECK (
                    directory_device IS NULL OR directory_device >= 1
                ),
                directory_inode INTEGER CHECK (
                    directory_inode IS NULL OR directory_inode >= 1
                ),
                provenance_revision INTEGER NOT NULL DEFAULT 0 CHECK (
                    provenance_revision >= 0
                    AND provenance_revision <= 2147483647
                ),
                FOREIGN KEY (installation_id)
                    REFERENCES native_model_installs(id) ON DELETE RESTRICT
            )
            """
        )
        provenance_columns = {
            str(row[1])
            for row in self._connection.execute(
                "PRAGMA table_info(native_model_install_provenance)"
            ).fetchall()
        }
        if "artifact_authority" not in provenance_columns:
            self._connection.execute(
                "ALTER TABLE native_model_install_provenance "
                "ADD COLUMN artifact_authority TEXT CHECK ("
                "artifact_authority IS NULL OR artifact_authority IN ("
                "'signed_catalog', 'local_pinned_discovery'))"
            )
        if "source_identity_digest" not in provenance_columns:
            self._connection.execute(
                "ALTER TABLE native_model_install_provenance "
                "ADD COLUMN source_identity_digest TEXT"
            )
        if "directory_device" not in provenance_columns:
            self._connection.execute(
                "ALTER TABLE native_model_install_provenance "
                "ADD COLUMN directory_device INTEGER CHECK ("
                "directory_device IS NULL OR directory_device >= 1)"
            )
        if "directory_inode" not in provenance_columns:
            self._connection.execute(
                "ALTER TABLE native_model_install_provenance "
                "ADD COLUMN directory_inode INTEGER CHECK ("
                "directory_inode IS NULL OR directory_inode >= 1)"
            )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS native_model_install_provenance_ownership
                ON native_model_install_provenance (
                    source_kind, ownership_class, installation_id
                )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_model_install_creation_claims_v1 (
                installation_id TEXT PRIMARY KEY,
                storage_location_id TEXT NOT NULL,
                storage_binding_generation INTEGER NOT NULL CHECK (
                    storage_binding_generation >= 1
                    AND storage_binding_generation <= 2147483647
                ),
                storage_lexical_root TEXT NOT NULL,
                storage_volume_uuid TEXT,
                storage_scope_id TEXT,
                lexical_destination TEXT NOT NULL,
                destination_binding_digest TEXT NOT NULL,
                resolved_revision TEXT NOT NULL,
                artifact_authority TEXT NOT NULL CHECK (
                    artifact_authority IN (
                        'signed_catalog', 'local_pinned_discovery'
                    )
                ),
                source_identity_digest TEXT NOT NULL,
                catalog_id TEXT,
                logical_model_id TEXT,
                artifact_id TEXT,
                recipe_id TEXT,
                catalog_digest TEXT,
                creation_transaction_id TEXT NOT NULL,
                directory_device INTEGER NOT NULL CHECK (directory_device >= 1),
                directory_inode INTEGER NOT NULL CHECK (directory_inode >= 1),
                created_at REAL NOT NULL,
                FOREIGN KEY (installation_id)
                    REFERENCES native_model_installs(id) ON DELETE RESTRICT
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_model_install_creation_intents_v1 (
                installation_id TEXT PRIMARY KEY,
                storage_location_id TEXT NOT NULL,
                storage_binding_generation INTEGER NOT NULL CHECK (
                    storage_binding_generation >= 1
                    AND storage_binding_generation <= 2147483647
                ),
                storage_lexical_root TEXT NOT NULL,
                storage_volume_uuid TEXT,
                storage_scope_id TEXT,
                lexical_destination TEXT NOT NULL,
                destination_binding_digest TEXT NOT NULL,
                resolved_revision TEXT NOT NULL,
                artifact_authority TEXT NOT NULL CHECK (
                    artifact_authority IN (
                        'signed_catalog', 'local_pinned_discovery'
                    )
                ),
                source_identity_digest TEXT NOT NULL,
                catalog_id TEXT,
                logical_model_id TEXT,
                artifact_id TEXT,
                recipe_id TEXT,
                catalog_digest TEXT,
                creation_transaction_id TEXT NOT NULL,
                require_exclusive_proof INTEGER NOT NULL CHECK (
                    require_exclusive_proof IN (0, 1)
                ),
                state TEXT NOT NULL CHECK (
                    state IN (
                        'pending', 'claimed', 'unowned', 'recovery_required'
                    )
                ),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (installation_id)
                    REFERENCES native_model_installs(id) ON DELETE RESTRICT
            )
            """
        )
        # A legacy install row is evidence only that the native installer knew
        # about a download.  Status, alias, destination, and files_json cannot
        # prove who owned the destination before this schema existed.
        self._connection.execute(
            """
            INSERT OR IGNORE INTO native_model_install_provenance (
                installation_id, source_kind, ownership_class,
                destination_state_before, provenance_revision
            )
            SELECT id, 'managed_download', 'unknown', 'unknown', 0
              FROM native_model_installs
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS native_model_install_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                install_id TEXT NOT NULL,
                event TEXT NOT NULL,
                status TEXT NOT NULL,
                bytes_downloaded INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS native_model_install_events_install
                ON native_model_install_events (install_id, sequence)
            """
        )
        # Existing databases gain an honest starting snapshot. It does not
        # invent transitions that predate the journal, but it lets acceptance
        # evidence distinguish migrated history from events observed by this
        # version.
        self._connection.execute(
            """
            INSERT INTO native_model_install_events (
                install_id, event, status, bytes_downloaded, created_at
            )
            SELECT installs.id, 'snapshot', installs.status,
                   installs.bytes_downloaded, ?
              FROM native_model_installs AS installs
             WHERE NOT EXISTS (
                SELECT 1
                  FROM native_model_install_events AS events
                 WHERE events.install_id = installs.id
             )
            """,
            (time.time(),),
        )
        self._connection.commit()

    def _record_event(
        self,
        record: InstallRecord,
        event: str,
        *,
        created_at: float | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO native_model_install_events (
                install_id, event, status, bytes_downloaded, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.id,
                event,
                record.status,
                record.bytes_downloaded,
                created_at if created_at is not None else time.time(),
            ),
        )

    @_synchronized
    def create(
        self,
        *,
        installation_id: str | None = None,
        creation_claim: ManagedCreationClaim | None = None,
        creation_intent: ManagedCreationIntent | None = None,
        defer_until_creation_claim: bool = False,
        repo_id: str,
        engine: str,
        storage: str,
        alias: str,
        destination: str,
        revision: str | None,
        filename: str | None,
        projector_filename: str | None = None,
        context_length: int | None = None,
        download_files: list[str] | tuple[str, ...] = (),
        expected_files: list[OwnedFile] | tuple[OwnedFile, ...] | None = None,
        expected_manifest_digest: str | None = None,
        capabilities: list[str] | tuple[str, ...] | None = None,
        family: str | None,
        launch_contract: InstallLaunchInput | None = None,
        total_bytes: int | None = None,
    ) -> InstallRecord:
        now = time.time()
        record_id = installation_id or str(uuid.uuid4())
        if creation_intent is not None:
            validate_managed_creation_intent(creation_intent)
            if creation_intent.installation_id != record_id:
                raise ProvenanceDataError("creation_intent_installation_mismatch")
        if defer_until_creation_claim != (creation_intent is not None):
            raise ProvenanceDataError("creation_intent_state_invalid")
        if creation_claim is not None:
            validate_managed_creation_claim(creation_claim)
            if creation_claim.installation_id != record_id:
                raise ProvenanceDataError("creation_claim_installation_mismatch")
            if creation_intent is not None:
                validate_claim_for_intent(creation_claim, creation_intent)
        signed_exclusive = (
            creation_intent is not None
            and creation_intent.artifact_authority
            is ArtifactAuthority.SIGNED_CATALOG
            and creation_intent.require_exclusive_proof is True
        )
        if signed_exclusive:
            expected_files_json, expected_manifest_digest = (
                _expected_manifest_fields(
                    expected_files,
                    expected_manifest_digest,
                    download_files=download_files,
                    total_bytes=total_bytes,
                )
            )
            if expected_files_json is None:
                raise ProvenanceDataError("expected_artifact_manifest_required")
        elif expected_files is not None or expected_manifest_digest is not None:
            raise ProvenanceDataError("expected_artifact_manifest_not_allowed")
        else:
            expected_files_json = None
        record = InstallRecord(
            id=record_id,
            repo_id=repo_id,
            engine=engine,
            storage=storage,
            alias=alias,
            destination=destination,
            status="preparing" if defer_until_creation_claim else "queued",
            revision=revision,
            filename=filename,
            projector_filename=projector_filename,
            context_length=context_length,
            files_json=(
                json.dumps(list(download_files), separators=(",", ":"))
                if download_files
                else None
            ),
            expected_files_json=expected_files_json,
            expected_manifest_digest=expected_manifest_digest,
            capabilities_json=(
                json.dumps(sorted(set(capabilities)), separators=(",", ":"))
                if capabilities
                else None
            ),
            family=family,
            launch_json=install_launch_json(engine, launch_contract),
            total_bytes=total_bytes,
            created_at=now,
            updated_at=now,
        )
        fields = asdict(record)
        try:
            self._connection.execute(
                f"INSERT INTO native_model_installs ({', '.join(fields)}) "
                f"VALUES ({', '.join('?' for _ in fields)})",
                tuple(fields.values()),
            )
            provenance_fields = provenance_database_fields(
                default_provenance(record.id)
            )
            self._connection.execute(
                f"INSERT INTO native_model_install_provenance "
                f"({', '.join(provenance_fields)}) "
                f"VALUES ({', '.join('?' for _ in provenance_fields)})",
                tuple(provenance_fields.values()),
            )
            if creation_intent is not None:
                self._connection.execute(
                    """
                    INSERT INTO native_model_install_creation_intents_v1 (
                        installation_id, storage_location_id,
                        storage_binding_generation, storage_lexical_root,
                        storage_volume_uuid, storage_scope_id,
                        lexical_destination, destination_binding_digest,
                        resolved_revision, artifact_authority,
                        source_identity_digest, catalog_id, logical_model_id,
                        artifact_id, recipe_id, catalog_digest,
                        creation_transaction_id, require_exclusive_proof,
                        state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, 'pending', ?, ?)
                    """,
                    (
                        creation_intent.installation_id,
                        creation_intent.storage_location_id,
                        creation_intent.storage_binding_generation,
                        creation_intent.storage_lexical_root,
                        creation_intent.storage_volume_uuid,
                        creation_intent.storage_scope_id,
                        creation_intent.lexical_destination,
                        creation_intent.destination_binding_digest,
                        creation_intent.resolved_revision,
                        creation_intent.artifact_authority.value,
                        creation_intent.source_identity_digest,
                        creation_intent.catalog_id,
                        creation_intent.logical_model_id,
                        creation_intent.artifact_id,
                        creation_intent.recipe_id,
                        creation_intent.catalog_digest,
                        creation_intent.creation_transaction_id,
                        1 if creation_intent.require_exclusive_proof else 0,
                        now,
                        now,
                    ),
                )
            if creation_claim is not None:
                self._connection.execute(
                    """
                    INSERT INTO native_model_install_creation_claims_v1 (
                        installation_id, storage_location_id,
                        storage_binding_generation, storage_lexical_root,
                        storage_volume_uuid, storage_scope_id,
                        lexical_destination, destination_binding_digest,
                        resolved_revision, artifact_authority,
                        source_identity_digest, catalog_id, logical_model_id,
                        artifact_id, recipe_id, catalog_digest,
                        creation_transaction_id, directory_device,
                        directory_inode, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?)
                    """,
                    (
                        creation_claim.installation_id,
                        creation_claim.storage_location_id,
                        creation_claim.storage_binding_generation,
                        creation_claim.storage_lexical_root,
                        creation_claim.storage_volume_uuid,
                        creation_claim.storage_scope_id,
                        creation_claim.lexical_destination,
                        creation_claim.destination_binding_digest,
                        creation_claim.resolved_revision,
                        creation_claim.artifact_authority.value,
                        creation_claim.source_identity_digest,
                        creation_claim.catalog_id,
                        creation_claim.logical_model_id,
                        creation_claim.artifact_id,
                        creation_claim.recipe_id,
                        creation_claim.catalog_digest,
                        creation_claim.creation_transaction_id,
                        creation_claim.directory_device,
                        creation_claim.directory_inode,
                        now,
                    ),
                )
            self._record_event(
                record,
                "creation_intent" if defer_until_creation_claim else "created",
                created_at=now,
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return record

    @_synchronized
    def update(self, install_id: str, **changes: Any) -> InstallRecord:
        if not changes:
            return self.get_by_id(install_id)
        if any(key not in _INSTALL_MUTABLE_FIELDS for key in changes):
            raise ValueError("install_update_field_not_allowed")
        previous = self.get_by_id(install_id)
        changes["updated_at"] = time.time()
        columns = ", ".join(f"{key} = ?" for key in changes)
        cursor = self._connection.execute(
            f"UPDATE native_model_installs SET {columns} WHERE id = ?",
            (*changes.values(), install_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown install '{install_id}'")
        updated = self.get_by_id(install_id)
        if updated.status != previous.status:
            self._record_event(updated, "status")
        if updated.hidden and not previous.hidden:
            self._record_event(updated, "history_dismissed")
        self._connection.commit()
        return updated

    @_synchronized
    def update_if_status(
        self,
        install_id: str,
        expected_status: str,
        **changes: Any,
    ) -> InstallRecord:
        """Apply one state transition only while its observed source is current."""

        if not changes:
            raise ValueError("install_conditional_update_empty")
        if any(key not in _INSTALL_MUTABLE_FIELDS for key in changes):
            raise ValueError("install_update_field_not_allowed")
        previous = self.get_by_id(install_id)
        if previous.status != expected_status:
            raise ValueError("install status changed while retrying")
        changes["updated_at"] = time.time()
        columns = ", ".join(f"{key} = ?" for key in changes)
        cursor = self._connection.execute(
            f"UPDATE native_model_installs SET {columns} "
            "WHERE id = ? AND status = ?",
            (*changes.values(), install_id, expected_status),
        )
        if cursor.rowcount != 1:
            self._connection.rollback()
            raise ValueError("install status changed while retrying")
        updated = self.get_by_id(install_id)
        if updated.status != previous.status:
            self._record_event(updated, "status")
        if updated.hidden and not previous.hidden:
            self._record_event(updated, "history_dismissed")
        self._connection.commit()
        return updated

    @_synchronized
    def get_by_id(self, installation_id: str) -> InstallRecord:
        """Return only the row with this exact immutable installation ID."""

        row = self._connection.execute(
            "SELECT * FROM native_model_installs WHERE id = ?", (installation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown install '{installation_id}'")
        return InstallRecord(**dict(row))

    @_synchronized
    def get(self, install_id: str) -> InstallRecord:
        """Compatibility alias for exact-ID lookup."""

        return self.get_by_id(install_id)

    @_synchronized
    def get_provenance(self, installation_id: str) -> InstallationProvenance:
        self.get_by_id(installation_id)
        row = self._connection.execute(
            """
            SELECT * FROM native_model_install_provenance
             WHERE installation_id = ?
            """,
            (installation_id,),
        ).fetchone()
        if row is None:
            raise ProvenanceDataError("provenance_missing")
        return provenance_from_database_row(dict(row))

    @_synchronized
    def get_creation_claim(
        self,
        installation_id: str,
    ) -> ManagedCreationClaim | None:
        """Return only the immutable absent-and-created claim for this ID."""

        self.get_by_id(installation_id)
        row = self._connection.execute(
            """
            SELECT * FROM native_model_install_creation_claims_v1
             WHERE installation_id = ?
            """,
            (installation_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            claim = ManagedCreationClaim(
                installation_id=str(row["installation_id"]),
                storage_location_id=str(row["storage_location_id"]),
                storage_binding_generation=int(row["storage_binding_generation"]),
                storage_lexical_root=str(row["storage_lexical_root"]),
                storage_volume_uuid=(
                    str(row["storage_volume_uuid"])
                    if row["storage_volume_uuid"] is not None
                    else None
                ),
                storage_scope_id=(
                    str(row["storage_scope_id"])
                    if row["storage_scope_id"] is not None
                    else None
                ),
                lexical_destination=str(row["lexical_destination"]),
                destination_binding_digest=str(row["destination_binding_digest"]),
                resolved_revision=str(row["resolved_revision"]),
                artifact_authority=ArtifactAuthority(row["artifact_authority"]),
                source_identity_digest=str(row["source_identity_digest"]),
                catalog_id=(
                    str(row["catalog_id"])
                    if row["catalog_id"] is not None
                    else None
                ),
                logical_model_id=(
                    str(row["logical_model_id"])
                    if row["logical_model_id"] is not None
                    else None
                ),
                artifact_id=(
                    str(row["artifact_id"])
                    if row["artifact_id"] is not None
                    else None
                ),
                recipe_id=(
                    str(row["recipe_id"])
                    if row["recipe_id"] is not None
                    else None
                ),
                catalog_digest=(
                    str(row["catalog_digest"])
                    if row["catalog_digest"] is not None
                    else None
                ),
                creation_transaction_id=str(row["creation_transaction_id"]),
                directory_device=int(row["directory_device"]),
                directory_inode=int(row["directory_inode"]),
            )
        except (TypeError, ValueError) as exc:
            raise ProvenanceDataError("creation_claim_malformed") from exc
        try:
            return validate_managed_creation_claim(claim)
        except ProvenanceDataError as exc:
            raise ProvenanceDataError("creation_claim_malformed") from exc

    @_synchronized
    def get_creation_intent(
        self,
        installation_id: str,
    ) -> CreationIntentRecord | None:
        self.get_by_id(installation_id)
        row = self._connection.execute(
            """
            SELECT * FROM native_model_install_creation_intents_v1
             WHERE installation_id = ?
            """,
            (installation_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            intent = ManagedCreationIntent(
                installation_id=str(row["installation_id"]),
                storage_location_id=str(row["storage_location_id"]),
                storage_binding_generation=int(row["storage_binding_generation"]),
                storage_lexical_root=str(row["storage_lexical_root"]),
                storage_volume_uuid=(
                    str(row["storage_volume_uuid"])
                    if row["storage_volume_uuid"] is not None
                    else None
                ),
                storage_scope_id=(
                    str(row["storage_scope_id"])
                    if row["storage_scope_id"] is not None
                    else None
                ),
                lexical_destination=str(row["lexical_destination"]),
                destination_binding_digest=str(row["destination_binding_digest"]),
                resolved_revision=str(row["resolved_revision"]),
                artifact_authority=ArtifactAuthority(row["artifact_authority"]),
                source_identity_digest=str(row["source_identity_digest"]),
                catalog_id=(
                    str(row["catalog_id"]) if row["catalog_id"] is not None else None
                ),
                logical_model_id=(
                    str(row["logical_model_id"])
                    if row["logical_model_id"] is not None
                    else None
                ),
                artifact_id=(
                    str(row["artifact_id"])
                    if row["artifact_id"] is not None
                    else None
                ),
                recipe_id=(
                    str(row["recipe_id"])
                    if row["recipe_id"] is not None
                    else None
                ),
                catalog_digest=(
                    str(row["catalog_digest"])
                    if row["catalog_digest"] is not None
                    else None
                ),
                creation_transaction_id=str(row["creation_transaction_id"]),
                require_exclusive_proof=bool(row["require_exclusive_proof"]),
            )
            validate_managed_creation_intent(intent)
            state = str(row["state"])
            if state not in {
                "pending",
                "claimed",
                "unowned",
                "recovery_required",
            }:
                raise ValueError
            return CreationIntentRecord(
                intent=intent,
                state=state,
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
            )
        except (TypeError, ValueError, ProvenanceDataError) as exc:
            raise ProvenanceDataError("creation_intent_malformed") from exc

    @_synchronized
    def finalize_creation(
        self,
        installation_id: str,
        creation_claim: ManagedCreationClaim | None,
        *,
        retry_status: str = "queued",
    ) -> InstallRecord:
        """Atomically bind the observed effect before making work runnable."""

        if retry_status not in {"queued", "partial"}:
            raise ValueError("creation_finalize_status_invalid")
        current = self.get_by_id(installation_id)
        intent_record = self.get_creation_intent(installation_id)
        if current.status != "preparing" or intent_record is None:
            raise ProvenanceConflictError("creation_intent_state_conflict")
        intent = intent_record.intent
        if creation_claim is None:
            if intent.require_exclusive_proof:
                raise ProvenanceConflictError("creation_claim_required")
            intent_state = "unowned"
        else:
            creation_claim = validate_claim_for_intent(creation_claim, intent)
            intent_state = "claimed"
        now = time.time()
        try:
            if creation_claim is not None:
                self._connection.execute(
                    """
                    INSERT INTO native_model_install_creation_claims_v1 (
                        installation_id, storage_location_id,
                        storage_binding_generation, storage_lexical_root,
                        storage_volume_uuid, storage_scope_id,
                        lexical_destination, destination_binding_digest,
                        resolved_revision, artifact_authority,
                        source_identity_digest, catalog_id, logical_model_id,
                        artifact_id, recipe_id, catalog_digest,
                        creation_transaction_id, directory_device,
                        directory_inode, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?)
                    """,
                    (
                        creation_claim.installation_id,
                        creation_claim.storage_location_id,
                        creation_claim.storage_binding_generation,
                        creation_claim.storage_lexical_root,
                        creation_claim.storage_volume_uuid,
                        creation_claim.storage_scope_id,
                        creation_claim.lexical_destination,
                        creation_claim.destination_binding_digest,
                        creation_claim.resolved_revision,
                        creation_claim.artifact_authority.value,
                        creation_claim.source_identity_digest,
                        creation_claim.catalog_id,
                        creation_claim.logical_model_id,
                        creation_claim.artifact_id,
                        creation_claim.recipe_id,
                        creation_claim.catalog_digest,
                        creation_claim.creation_transaction_id,
                        creation_claim.directory_device,
                        creation_claim.directory_inode,
                        now,
                    ),
                )
            intent_cursor = self._connection.execute(
                """
                UPDATE native_model_install_creation_intents_v1
                   SET state = ?, updated_at = ?
                 WHERE installation_id = ? AND state IN ('pending', 'recovery_required')
                """,
                (intent_state, now, installation_id),
            )
            if intent_cursor.rowcount != 1:
                raise ProvenanceConflictError("creation_intent_state_conflict")
            cursor = self._connection.execute(
                """
                UPDATE native_model_installs
                   SET status = ?, error = NULL, updated_at = ?
                 WHERE id = ? AND status = 'preparing'
                """,
                (retry_status, now, installation_id),
            )
            if cursor.rowcount != 1:
                raise ProvenanceConflictError("creation_intent_state_conflict")
            updated = self.get_by_id(installation_id)
            self._record_event(updated, "created", created_at=now)
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    @_synchronized
    def mark_creation_recovery_required(
        self,
        installation_id: str,
    ) -> InstallRecord:
        current = self.get_by_id(installation_id)
        if current.status != "preparing":
            return current
        now = time.time()
        try:
            cursor = self._connection.execute(
                """
                UPDATE native_model_install_creation_intents_v1
                   SET state = 'recovery_required', updated_at = ?
                 WHERE installation_id = ?
                   AND state IN ('pending', 'recovery_required')
                """,
                (now, installation_id),
            )
            if cursor.rowcount != 1:
                raise ProvenanceConflictError("creation_intent_state_conflict")
            self._connection.execute(
                """
                UPDATE native_model_installs
                   SET error = ?, updated_at = ?
                 WHERE id = ? AND status = 'preparing'
                """,
                (
                    "managed destination creation requires exact marker recovery",
                    now,
                    installation_id,
                ),
            )
            updated = self.get_by_id(installation_id)
            self._record_event(updated, "creation_recovery_required", created_at=now)
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    @_synchronized
    def preparing(self, *, limit: int = 10_001) -> list[InstallRecord]:
        if not 1 <= limit <= 10_001:
            raise ValueError("install_preparing_limit_invalid")
        rows = self._connection.execute(
            """
            SELECT * FROM native_model_installs
             WHERE status = 'preparing'
             ORDER BY created_at, id
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [InstallRecord(**dict(row)) for row in rows]

    @_synchronized
    def cleanup_eligibility(self, installation_id: str) -> CleanupEligibility:
        """Evaluate local evidence without performing any filesystem action."""

        try:
            record = self.get_by_id(installation_id)
        except KeyError:
            return CleanupEligibility(
                eligible=False,
                reason=CleanupReason.INSTALLATION_MISSING,
            )
        try:
            provenance = self.get_provenance(installation_id)
        except ProvenanceDataError:
            return CleanupEligibility(
                eligible=False,
                reason=CleanupReason.PROVENANCE_MALFORMED,
            )
        installation = CleanupInstallation(
            installation_id=record.id,
            status=record.status,
            destination=record.destination,
            resolved_revision=record.revision,
            total_bytes=record.total_bytes,
        )
        return decide_cleanup_eligibility(
            installation_id,
            installation,
            provenance,
        )

    @_synchronized
    def require_cleanup_authority(
        self,
        installation_id: str,
    ) -> tuple[InstallRecord, InstallationProvenance]:
        """Return one exact immutable eligible install/provenance snapshot."""

        try:
            record = self.get_by_id(installation_id)
        except KeyError as exc:
            raise ProvenanceProofRejected(
                CleanupReason.INSTALLATION_MISSING
            ) from exc
        try:
            provenance = self.get_provenance(installation_id)
        except ProvenanceDataError as exc:
            raise ProvenanceProofRejected(
                CleanupReason.PROVENANCE_MALFORMED
            ) from exc
        decision = decide_cleanup_eligibility(
            installation_id,
            CleanupInstallation(
                installation_id=record.id,
                status=record.status,
                destination=record.destination,
                resolved_revision=record.revision,
                total_bytes=record.total_bytes,
            ),
            provenance,
        )
        if not decision.eligible:
            raise ProvenanceProofRejected(decision.reason)
        return record, provenance

    @_synchronized
    def mark_trashed(self, installation_id: str) -> InstallRecord:
        """Persist the sole successful managed-cleanup terminal state."""

        record = self.get_by_id(installation_id)
        if record.status == "trashed":
            return record
        if record.status != "installed":
            raise ValueError("install_not_trashable")
        return self.update(
            installation_id,
            status="trashed",
            hidden=1,
            pid=None,
            download_speed_bps=None,
            error=None,
        )

    @_synchronized
    def record_exclusive_managed_proof(
        self,
        installation_id: str,
        proof: ExclusiveManagedProof,
    ) -> InstallationProvenance:
        """Atomically record one complete, exact, immutable ownership proof.

        Existing and ordinary new installs begin unknown.  This method is the
        sole widening path, is idempotent for byte-equivalent canonical proof,
        and refuses replacement or partial evidence.
        """

        record = self.get_by_id(installation_id)
        try:
            candidate = provenance_from_exclusive_proof(proof)
        except ProvenanceDataError as exc:
            raise ProvenanceProofRejected(CleanupReason.PROVENANCE_MALFORMED) from exc
        if (
            candidate.artifact_authority is ArtifactAuthority.SIGNED_CATALOG
            or record.expected_files_json is not None
            or record.expected_manifest_digest is not None
        ):
            raise ProvenanceConflictError(
                "signed_artifact_pre_registration_verification_required"
            )
        decision = decide_cleanup_eligibility(
            installation_id,
            CleanupInstallation(
                installation_id=record.id,
                status=record.status,
                destination=record.destination,
                resolved_revision=record.revision,
                total_bytes=record.total_bytes,
            ),
            candidate,
        )
        if not decision.eligible:
            raise ProvenanceProofRejected(decision.reason)

        return self._persist_exclusive_provenance(
            record,
            candidate,
            event="provenance_proved",
        )

    @_synchronized
    def record_verified_signed_proof(
        self,
        installation_id: str,
        proof: ExclusiveManagedProof,
    ) -> InstallationProvenance:
        """Persist exact signed bytes while registration is still fenced.

        Cleanup remains unavailable until the ordinary ``installed`` status
        gate is reached. Persisting the proof first makes a callback failure or
        restart retryable without ever treating an unverified payload as a
        signed catalog artifact.
        """

        record = self.get_by_id(installation_id)
        if record.status != "registering":
            raise ProvenanceConflictError("signed_artifact_verification_state_invalid")
        try:
            candidate = provenance_from_exclusive_proof(proof)
            expected_files = record.expected_files
        except ProvenanceDataError as exc:
            raise ProvenanceProofRejected(CleanupReason.PROVENANCE_MALFORMED) from exc
        if (
            expected_files is None
            or record.expected_manifest_digest is None
            or candidate.source_kind is not SourceKind.MANAGED_DOWNLOAD
            or candidate.ownership_class is not OwnershipClass.EXCLUSIVE_MANAGED
            or candidate.artifact_authority is not ArtifactAuthority.SIGNED_CATALOG
            or candidate.provenance_revision != PROVENANCE_REVISION
            or owned_manifest_digest(expected_files)
            != record.expected_manifest_digest
            or tuple(item.path for item in expected_files)
            != tuple(json.loads(record.files_json or "[]"))
            or record.total_bytes != sum(item.size_bytes for item in expected_files)
            or candidate.owned_files is None
        ):
            raise ProvenanceProofRejected(CleanupReason.OWNED_MANIFEST_INVALID)
        allowed_metadata = allowed_hf_local_metadata_paths(
            tuple(item.path for item in expected_files)
        )
        payload_files = tuple(
            item
            for item in candidate.owned_files
            if not is_hf_local_metadata_path(item.path)
        )
        metadata_paths = {
            item.path
            for item in candidate.owned_files
            if is_hf_local_metadata_path(item.path)
        }
        if (
            payload_files != expected_files
            or MANAGED_CREATION_MARKER_PATH not in metadata_paths
            or not metadata_paths.issubset(allowed_metadata)
        ):
            raise ProvenanceProofRejected(CleanupReason.OWNED_MANIFEST_INVALID)
        # Reuse the complete cleanup validator with a virtual completed status.
        # The persisted row stays ``registering``, so cleanup and inventory
        # authority remain closed until registration itself succeeds.
        decision = decide_cleanup_eligibility(
            installation_id,
            CleanupInstallation(
                installation_id=record.id,
                status="installed",
                destination=record.destination,
                resolved_revision=record.revision,
                total_bytes=record.total_bytes,
            ),
            candidate,
        )
        if not decision.eligible:
            raise ProvenanceProofRejected(decision.reason)
        return self._persist_exclusive_provenance(
            record,
            candidate,
            event="signed_artifact_verified",
        )

    def _persist_exclusive_provenance(
        self,
        record: InstallRecord,
        candidate: InstallationProvenance,
        *,
        event: str,
    ) -> InstallationProvenance:
        """Write a fully validated candidate exactly once under the store lock."""

        installation_id = record.id
        current = self.get_provenance(installation_id)
        if current == candidate:
            return current
        if not is_default_unknown(current):
            raise ProvenanceConflictError("provenance_conflict")

        fields = provenance_database_fields(candidate)
        assignments = ", ".join(
            f"{key} = ?" for key in fields if key != "installation_id"
        )
        values = [
            value for key, value in fields.items() if key != "installation_id"
        ]
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE native_model_install_provenance
                   SET {assignments}
                 WHERE installation_id = ?
                   AND source_kind = 'managed_download'
                   AND ownership_class = 'unknown'
                   AND provenance_revision = 0
                """,
                (*values, installation_id),
            )
            if cursor.rowcount != 1:
                observed = self.get_provenance(installation_id)
                self._connection.rollback()
                if observed == candidate:
                    return observed
                raise ProvenanceConflictError("provenance_conflict")
            self._record_event(record, event)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return candidate

    @_synchronized
    def list(self, *, limit: int = 100) -> list[InstallRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM native_model_installs
             WHERE hidden = 0
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [InstallRecord(**dict(row)) for row in rows]

    @_synchronized
    def latest_for_alias(self, alias: str) -> InstallRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM native_model_installs
             WHERE alias = ?
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (alias,),
        ).fetchone()
        return InstallRecord(**dict(row)) if row is not None else None

    @_synchronized
    def signed_launch_records(
        self,
        *,
        alias: str,
        engine: str,
        limit: int = 1025,
    ) -> list[InstallRecord]:
        """Return bounded, hidden-inclusive signed launch bindings.

        These rows are management-plane authority loaded while profiles are
        applied; inference routing never queries SQLite. ``trashed`` rows no
        longer bind a profile because their exact managed destination has been
        removed. Every other state remains conservative: a crash after config
        persistence but before the final ``installed`` transition must not
        silently strip the signed oMLX guard on restart.
        """

        if not 1 <= limit <= 1025:
            raise ValueError("install_launch_record_limit_invalid")
        rows = self._connection.execute(
            """
            SELECT * FROM native_model_installs
             WHERE alias = ?
               AND engine = ?
               AND launch_json IS NOT NULL
               AND status != 'trashed'
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (alias, engine, limit),
        ).fetchall()
        return [InstallRecord(**dict(row)) for row in rows]

    @_synchronized
    def binding_records(
        self,
        *,
        engine: str,
        storage: str,
        limit: int = 10_001,
    ) -> list[InstallRecord]:
        """Return bounded hidden-inclusive rows for a denial-only ambiguity check."""

        if not 1 <= limit <= 10_001:
            raise ValueError("install_binding_limit_invalid")
        rows = self._connection.execute(
            """
            SELECT * FROM native_model_installs
             WHERE engine = ? AND storage = ?
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (engine, storage, limit),
        ).fetchall()
        return [InstallRecord(**dict(row)) for row in rows]

    @_synchronized
    def events(self, install_id: str) -> list[InstallEvent]:
        # Preserve the endpoint's normal unknown-ID behavior even for a
        # partially migrated/corrupt database with orphaned event rows.
        self.get(install_id)
        rows = self._connection.execute(
            """
            SELECT sequence, install_id, event, status,
                   bytes_downloaded, created_at
              FROM native_model_install_events
             WHERE install_id = ?
             ORDER BY sequence
            """,
            (install_id,),
        ).fetchall()
        return [InstallEvent(**dict(row)) for row in rows]

    @_synchronized
    def evidence(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT * FROM native_model_installs
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        evidence: list[dict[str, Any]] = []
        for row in rows:
            record = InstallRecord(**dict(row))
            payload = record.to_dict()
            payload["dismissed"] = bool(record.hidden)
            payload["events"] = [
                event.to_dict() for event in self.events(record.id)
            ]
            evidence.append(payload)
        return evidence

    @_synchronized
    def dismiss(self, install_id: str) -> InstallRecord:
        record = self.get(install_id)
        self.update(install_id, hidden=1)
        return record

    @_synchronized
    def recover_interrupted(self) -> None:
        interrupted = self._connection.execute(
            """
            SELECT id FROM native_model_installs
             WHERE status IN ('queued', 'downloading')
            """
        ).fetchall()
        for row in interrupted:
            self.update(
                str(row["id"]),
                status="partial",
                pid=None,
                download_speed_bps=None,
                error="service stopped before the download completed",
            )
        registering = self._connection.execute(
            """
            SELECT id FROM native_model_installs
             WHERE status = 'registering'
            """
        ).fetchall()
        for row in registering:
            self.update(
                str(row["id"]),
                status="downloaded",
                pid=None,
                download_speed_bps=None,
                error="download completed but profile registration was interrupted",
            )
        # Versions before the downloaded/registering state split marked these
        # rows installed and attached an error, which made the retry endpoint
        # reject them even though their weights were already durable.
        legacy = self._connection.execute(
            """
            SELECT id FROM native_model_installs
             WHERE status = 'installed'
               AND error LIKE 'download completed but profile registration failed:%'
            """,
        ).fetchall()
        for row in legacy:
            self.update(
                str(row["id"]),
                status="downloaded",
                pid=None,
                download_speed_bps=None,
            )

    @_synchronized
    def close(self) -> None:
        self._connection.close()


__all__ = [
    "CreationIntentRecord",
    "ExclusiveManagedProof",
    "InstallEvent",
    "InstallationProvenance",
    "InstallRecord",
    "InstallStore",
    "ManagedCreationClaim",
    "ProvenanceConflictError",
    "ProvenanceDataError",
    "ProvenanceProofRejected",
]
