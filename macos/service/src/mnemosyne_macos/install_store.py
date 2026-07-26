"""Durable SQLite state for native Hugging Face installs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
import uuid


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
    capabilities_json: str | None = None
    family: str | None = None
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
        capabilities_encoded = payload.pop("capabilities_json")
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


class InstallStore:
    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
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
                capabilities_json TEXT,
                family TEXT,
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
        self._connection.commit()

    def create(
        self,
        *,
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
        capabilities: list[str] | tuple[str, ...] | None = None,
        family: str | None,
        total_bytes: int | None = None,
    ) -> InstallRecord:
        now = time.time()
        record = InstallRecord(
            id=str(uuid.uuid4()),
            repo_id=repo_id,
            engine=engine,
            storage=storage,
            alias=alias,
            destination=destination,
            status="queued",
            revision=revision,
            filename=filename,
            projector_filename=projector_filename,
            context_length=context_length,
            files_json=(
                json.dumps(list(download_files), separators=(",", ":"))
                if download_files
                else None
            ),
            capabilities_json=(
                json.dumps(sorted(set(capabilities)), separators=(",", ":"))
                if capabilities
                else None
            ),
            family=family,
            total_bytes=total_bytes,
            created_at=now,
            updated_at=now,
        )
        fields = asdict(record)
        self._connection.execute(
            f"INSERT INTO native_model_installs ({', '.join(fields)}) "
            f"VALUES ({', '.join('?' for _ in fields)})",
            tuple(fields.values()),
        )
        self._connection.commit()
        return record

    def update(self, install_id: str, **changes: Any) -> InstallRecord:
        if not changes:
            return self.get(install_id)
        changes["updated_at"] = time.time()
        columns = ", ".join(f"{key} = ?" for key in changes)
        cursor = self._connection.execute(
            f"UPDATE native_model_installs SET {columns} WHERE id = ?",
            (*changes.values(), install_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown install '{install_id}'")
        self._connection.commit()
        return self.get(install_id)

    def get(self, install_id: str) -> InstallRecord:
        row = self._connection.execute(
            "SELECT * FROM native_model_installs WHERE id = ?", (install_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown install '{install_id}'")
        return InstallRecord(**dict(row))

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

    def dismiss(self, install_id: str) -> InstallRecord:
        record = self.get(install_id)
        cursor = self._connection.execute(
            """
            UPDATE native_model_installs
               SET hidden = 1, updated_at = ?
             WHERE id = ?
            """,
            (time.time(), install_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown install '{install_id}'")
        self._connection.commit()
        return record

    def recover_interrupted(self) -> None:
        self._connection.execute(
            """
            UPDATE native_model_installs
               SET status = 'partial', pid = NULL,
                   download_speed_bps = NULL,
                   error = 'service stopped before the download completed', updated_at = ?
             WHERE status IN ('queued', 'downloading')
            """,
            (time.time(),),
        )
        self._connection.execute(
            """
            UPDATE native_model_installs
               SET status = 'downloaded', pid = NULL,
                   download_speed_bps = NULL,
                   error = 'download completed but profile registration was interrupted',
                   updated_at = ?
             WHERE status = 'registering'
            """,
            (time.time(),),
        )
        # Versions before the downloaded/registering state split marked these
        # rows installed and attached an error, which made the retry endpoint
        # reject them even though their weights were already durable.
        self._connection.execute(
            """
            UPDATE native_model_installs
               SET status = 'downloaded', pid = NULL,
                   download_speed_bps = NULL, updated_at = ?
             WHERE status = 'installed'
               AND error LIKE 'download completed but profile registration failed:%'
            """,
            (time.time(),),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


__all__ = ["InstallRecord", "InstallStore"]
