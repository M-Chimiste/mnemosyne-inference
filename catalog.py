"""Mnemosyne Inference — SQLite catalog.

Persistent state for the model registry, downloads, and usage history.
See project_docs/phase_1_plan.md §5.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

from config import ModelProfile

logger = logging.getLogger("vllm-manager.catalog")

DEFAULT_DB_PATH = "/state/mnemosyne.db"
SCHEMA_VERSION = 1

_RESERVED_PREFIX_COLON = "__cache__:"
_RESERVED_PREFIX_SLASH = "__cache__/"


def synthetic_alias(hf_model_id: str) -> str:
    """Deterministic, URL-safe synthetic alias for cache-only catalog entries.

    Phase 4 will use this when the legacy POST /manager/download shim
    inserts a row without a user-supplied alias. Phase 1 only commits to
    the encoding contract.
    """
    h = hashlib.sha256(hf_model_id.encode("utf-8")).hexdigest()[:16]
    return f"{_RESERVED_PREFIX_COLON}{h}"


def is_cache_only_alias(alias: str) -> bool:
    return alias.startswith(_RESERVED_PREFIX_COLON) or alias.startswith(_RESERVED_PREFIX_SLASH)


def _hf_dir_name(hf_model_id: str) -> str:
    """'Qwen/Qwen2.5-7B' → 'models--Qwen--Qwen2.5-7B' (HF cache convention)."""
    return "models--" + hf_model_id.replace("/", "--")


def _newest_snapshot(cache_dir: str) -> Optional[str]:
    snap_dir = os.path.join(cache_dir, "snapshots")
    if not os.path.isdir(snap_dir):
        return None
    entries: list[tuple[float, str]] = []
    try:
        for name in os.listdir(snap_dir):
            full = os.path.join(snap_dir, name)
            if os.path.isdir(full):
                try:
                    entries.append((os.path.getmtime(full), full))
                except OSError:
                    continue
    except OSError:
        return None
    if not entries:
        return None
    entries.sort(reverse=True)
    return entries[0][1]


def _has_weights(snapshot_dir: str) -> bool:
    try:
        for name in os.listdir(snapshot_dir):
            if name.endswith((".safetensors", ".bin", ".gguf")):
                return True
    except OSError:
        pass
    return False


@dataclass(frozen=True)
class CatalogRow:
    alias: str
    hf_model_id: str
    source: str
    quantization: Optional[str]
    gpus: str           # JSON
    max_model_len: Optional[int]
    storage_location: str
    cache_path: Optional[str]
    size_bytes: Optional[int]
    status: str
    installed_at: Optional[int]
    last_used_at: Optional[int]
    request_count: int
    extra_args: str     # JSON

    def to_api_dict(self) -> dict:
        return {
            "alias": self.alias,
            "hf_model_id": self.hf_model_id,
            "source": self.source,
            "quantization": self.quantization,
            "gpus": json.loads(self.gpus),
            "max_model_len": self.max_model_len,
            "storage_location": self.storage_location,
            "cache_path": self.cache_path,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "installed_at": self.installed_at,
            "last_used_at": self.last_used_at,
            "request_count": self.request_count,
            "extra_args": json.loads(self.extra_args) if self.extra_args else [],
        }


@dataclass(frozen=True)
class DownloadRow:
    id: int
    alias: str
    pid: Optional[int]
    status: str
    started_at: int
    finished_at: Optional[int]
    bytes_downloaded: int
    total_bytes: Optional[int]
    error: Optional[str]


@dataclass
class SyncResult:
    added: int
    updated: int
    removed_config_orphans: int
    ui_preserved: int
    ui_overwritten: int


@dataclass
class ReconcileResult:
    installed: int
    partial: int
    location_missing: int


class Catalog:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # In-memory DBs don't support WAL; fall back silently.
            pass
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._bootstrap()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        self._closed = True

    def _bootstrap(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                  version INTEGER PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS models (
                  alias              TEXT PRIMARY KEY,
                  hf_model_id        TEXT NOT NULL,
                  source             TEXT NOT NULL,
                  quantization       TEXT,
                  gpus               TEXT NOT NULL,
                  max_model_len      INTEGER,
                  storage_location   TEXT NOT NULL,
                  cache_path         TEXT,
                  size_bytes         INTEGER,
                  status             TEXT NOT NULL,
                  installed_at       INTEGER,
                  last_used_at       INTEGER,
                  request_count      INTEGER DEFAULT 0,
                  extra_args         TEXT
                );
                CREATE TABLE IF NOT EXISTS downloads (
                  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                  alias              TEXT NOT NULL REFERENCES models(alias) ON DELETE CASCADE,
                  pid                INTEGER,
                  status             TEXT NOT NULL,
                  started_at         INTEGER NOT NULL,
                  finished_at        INTEGER,
                  bytes_downloaded   INTEGER DEFAULT 0,
                  total_bytes        INTEGER,
                  error              TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_downloads_alias  ON downloads(alias);
                CREATE INDEX IF NOT EXISTS idx_models_last_used ON models(last_used_at);
                """
            )
            existing = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            # Future: elif existing["version"] < SCHEMA_VERSION: run migrations.

    # ── sync ──────────────────────────────────────────────────────────

    def sync_from_config(
        self,
        models: list[ModelProfile],
        default_storage: str,
    ) -> SyncResult:
        """Upsert config rows; preserve durable metadata (PRD §5.11).

        Writes only declarative columns. Never touches `cache_path`,
        `status`, `size_bytes`, `installed_at`, `last_used_at`,
        `request_count` on existing rows — reconcile_cache handles
        status/cache_path; Phase 4 writes the rest.
        """
        with self._conn:
            return self._sync_from_config_uncommitted(models, default_storage)

    def apply_config(
        self,
        models: list[ModelProfile],
        default_storage: str,
        storage_paths: dict[str, str],
    ) -> tuple[SyncResult, ReconcileResult]:
        """Atomically sync config rows and reconcile cache state.

        Used by startup/reload so a failure in either step rolls back all DB
        writes and keeps catalog state aligned with the still-loaded config.
        """
        with self._conn:
            sync = self._sync_from_config_uncommitted(models, default_storage)
            rec = self._reconcile_cache_uncommitted(storage_paths)
            return sync, rec

    def _sync_from_config_uncommitted(
        self,
        models: list[ModelProfile],
        default_storage: str,
    ) -> SyncResult:
        added = 0
        updated = 0
        ui_overwritten = 0
        removed_orphans = 0
        new_aliases = {m.alias for m in models}

        existing_config = {
            row["alias"]
            for row in self._conn.execute(
                "SELECT alias FROM models WHERE source='config'"
            )
        }
        for alias in existing_config - new_aliases:
            self._conn.execute("DELETE FROM models WHERE alias=?", (alias,))
            removed_orphans += 1

        for m in models:
            storage_loc = m.storage if m.storage is not None else default_storage
            gpus_json = json.dumps(m.gpus)
            extra_json = json.dumps(m.extra_args)

            row = self._conn.execute(
                "SELECT source FROM models WHERE alias=?", (m.alias,)
            ).fetchone()

            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO models (
                      alias, hf_model_id, source, quantization, gpus,
                      max_model_len, storage_location, cache_path, size_bytes,
                      status, installed_at, last_used_at, request_count, extra_args
                    ) VALUES (?, ?, 'config', ?, ?, ?, ?, NULL, NULL, 'partial', NULL, NULL, 0, ?)
                    """,
                    (m.alias, m.model, m.quantization, gpus_json,
                     m.max_model_len, storage_loc, extra_json),
                )
                added += 1
            else:
                if row["source"] == "ui_install":
                    logger.warning(
                        "alias '%s' was source='ui_install'; config wins (PRD §5.1)",
                        m.alias,
                    )
                    ui_overwritten += 1
                self._conn.execute(
                    """
                    UPDATE models SET
                      hf_model_id      = ?,
                      source           = 'config',
                      quantization     = ?,
                      gpus             = ?,
                      max_model_len    = ?,
                      storage_location = ?,
                      extra_args       = ?
                    WHERE alias = ?
                    """,
                    (m.model, m.quantization, gpus_json, m.max_model_len,
                     storage_loc, extra_json, m.alias),
                )
                updated += 1

        ui_preserved = self._conn.execute(
            "SELECT COUNT(*) AS c FROM models WHERE source='ui_install'"
        ).fetchone()["c"]

        return SyncResult(
            added=added,
            updated=updated,
            removed_config_orphans=removed_orphans,
            ui_preserved=ui_preserved,
            ui_overwritten=ui_overwritten,
        )

    # ── reconcile ─────────────────────────────────────────────────────

    def reconcile_cache(self, storage_paths: dict[str, str]) -> ReconcileResult:
        """Walk each row's expected on-disk cache. Writes ONLY status and
        cache_path — preserves all durable metadata."""
        with self._conn:
            return self._reconcile_cache_uncommitted(storage_paths)

    def _reconcile_cache_uncommitted(self, storage_paths: dict[str, str]) -> ReconcileResult:
        installed = 0
        partial = 0
        loc_missing = 0
        rows = list(
            self._conn.execute(
                "SELECT alias, hf_model_id, storage_location FROM models"
            )
        )
        for row in rows:
            loc_path = storage_paths.get(row["storage_location"])
            if loc_path is None:
                self._conn.execute(
                    "UPDATE models SET cache_path=NULL, status='partial' WHERE alias=?",
                    (row["alias"],),
                )
                loc_missing += 1
                partial += 1
                continue

            cache_dir = os.path.join(loc_path, "hub", _hf_dir_name(row["hf_model_id"]))
            snap = _newest_snapshot(cache_dir)
            if snap and _has_weights(snap):
                self._conn.execute(
                    "UPDATE models SET cache_path=?, status='installed' WHERE alias=?",
                    (snap, row["alias"]),
                )
                installed += 1
            else:
                self._conn.execute(
                    "UPDATE models SET cache_path=NULL, status='partial' WHERE alias=?",
                    (row["alias"],),
                )
                partial += 1
        return ReconcileResult(installed=installed, partial=partial, location_missing=loc_missing)

    # ── reads ─────────────────────────────────────────────────────────

    def list_models(self) -> list[CatalogRow]:
        rows = self._conn.execute("SELECT * FROM models ORDER BY alias").fetchall()
        return [self._to_catalog_row(r) for r in rows]

    def get_model(self, alias: str) -> Optional[CatalogRow]:
        row = self._conn.execute("SELECT * FROM models WHERE alias=?", (alias,)).fetchone()
        return self._to_catalog_row(row) if row else None

    def list_downloads(self) -> list[DownloadRow]:
        rows = self._conn.execute("SELECT * FROM downloads ORDER BY id").fetchall()
        return [self._to_download_row(r) for r in rows]

    # ── usage flush ───────────────────────────────────────────────────

    def bump_usage(self, alias: str, last_used_at: Optional[float], delta: int) -> None:
        """Buffered usage flush from the runtime hot path. Single UPDATE.

        No-op when nothing to flush, or when the alias doesn't have a row
        (raw-id passthrough — UPDATE silently matches zero rows). Caller
        is responsible for zeroing its in-memory delta after a successful
        call."""
        if delta <= 0 or last_used_at is None:
            return
        with self._conn:
            self._conn.execute(
                "UPDATE models SET last_used_at=?, request_count = request_count + ? "
                "WHERE alias=?",
                (int(last_used_at), delta, alias),
            )

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        return int(row["version"]) if row else -1

    # ── test/internal helpers ─────────────────────────────────────────

    def _raw_insert_model(
        self,
        *,
        alias: str,
        hf_model_id: str,
        source: str = "ui_install",
        quantization: Optional[str] = None,
        gpus: str = '"all"',
        max_model_len: Optional[int] = None,
        storage_location: str,
        cache_path: Optional[str] = None,
        size_bytes: Optional[int] = None,
        status: str = "installed",
        installed_at: Optional[int] = None,
        last_used_at: Optional[int] = None,
        request_count: int = 0,
        extra_args: str = "[]",
    ) -> None:
        """Direct insert bypassing config validation. Used by Phase 4 for
        ui_install rows and by tests for synthetic-alias rows."""
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO models (
                  alias, hf_model_id, source, quantization, gpus,
                  max_model_len, storage_location, cache_path, size_bytes,
                  status, installed_at, last_used_at, request_count, extra_args
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (alias, hf_model_id, source, quantization, gpus,
                 max_model_len, storage_location, cache_path, size_bytes,
                 status, installed_at, last_used_at, request_count, extra_args),
            )

    def _to_catalog_row(self, row) -> CatalogRow:
        return CatalogRow(
            alias=row["alias"],
            hf_model_id=row["hf_model_id"],
            source=row["source"],
            quantization=row["quantization"],
            gpus=row["gpus"],
            max_model_len=row["max_model_len"],
            storage_location=row["storage_location"],
            cache_path=row["cache_path"],
            size_bytes=row["size_bytes"],
            status=row["status"],
            installed_at=row["installed_at"],
            last_used_at=row["last_used_at"],
            request_count=row["request_count"] or 0,
            extra_args=row["extra_args"] or "[]",
        )

    def _to_download_row(self, row) -> DownloadRow:
        return DownloadRow(
            id=row["id"],
            alias=row["alias"],
            pid=row["pid"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            bytes_downloaded=row["bytes_downloaded"] or 0,
            total_bytes=row["total_bytes"],
            error=row["error"],
        )


def open_catalog(path: str | None = None) -> Catalog:
    """Open or create the SQLite catalog. Path resolved at call time."""
    db_path = path or os.environ.get("MNEMOSYNE_DB_PATH", DEFAULT_DB_PATH)
    if db_path != ":memory:":
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return Catalog(conn)
