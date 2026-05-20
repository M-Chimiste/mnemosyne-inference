"""Mnemosyne Inference — SQLite catalog.

Persistent state for the model registry, downloads, and usage history.
See project_docs/phase_1_plan.md §5.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from config import ModelProfile
from repo_probe import expand_shard_filenames

logger = logging.getLogger("vllm-manager.catalog")

DEFAULT_DB_PATH = "/state/mnemosyne.db"
SCHEMA_VERSION = 3

_RESERVED_PREFIX_COLON = "__cache__:"
_RESERVED_PREFIX_SLASH = "__cache__/"


class CatalogCorruptionError(Exception):
    """PRAGMA quick_check returned anything other than 'ok'."""


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


def _has_any_weights(snapshot_dir: str) -> bool:
    """Permissive check used by cache-discovery paths (e.g. synthetic
    `__cache__:*` rows): returns True when any recognizable weight file is
    present, regardless of which one. Reconcile of aliased rows uses the
    backend-aware `_has_expected_weights` instead."""
    try:
        for name in os.listdir(snapshot_dir):
            if name.endswith((".safetensors", ".bin", ".gguf")):
                return True
    except OSError:
        pass
    return False


# Back-compat shim: callers under tests / external helpers may still import
# `_has_weights`. New code should use `_has_any_weights` (cache discovery)
# or `_has_expected_weights` (reconcile of aliased rows).
_has_weights = _has_any_weights


def _has_expected_weights(
    snapshot_dir: str,
    backend: str,
    gguf_filename: Optional[str],
) -> bool:
    """Backend-aware existence check used by reconcile.

    For vLLM rows: any `.safetensors` / `.bin` sibling is enough (mirrors
    the legacy behavior).
    For llama.cpp rows: the row's specific `gguf_filename` must be on disk;
    sharded models must have *all* shards present. Prevents promoting an
    alias to `installed` when its specific quant is missing, even if some
    other quant in the same repo is present (mixed-quant repos shared
    between aliases).
    """
    try:
        names = os.listdir(snapshot_dir)
    except OSError:
        return False
    if backend == "llama.cpp":
        if not gguf_filename:
            return False
        expected = expand_shard_filenames(gguf_filename, names)
        # Re-list against actual files to confirm presence (case-sensitive,
        # exact filename match).
        on_disk = set(names)
        return all(name in on_disk for name in expected)
    # vLLM (or absent backend on legacy rows treated as vLLM).
    for name in names:
        if name.endswith((".safetensors", ".bin")):
            return True
    return False


_HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _snapshot_for_revision(cache_dir: str, revision: Optional[str]) -> Optional[str]:
    """Resolve a revision to its on-disk snapshot directory.

    Order:
      1. Path-safety guard. Reject revisions containing '..', leading '/',
         or anything that, after normalization + join, escapes
         <cache_dir>/refs/. Defensive against attacker-controlled revisions.
      2. Direct snapshot SHA. If revision matches /^[0-9a-f]{40}$/ and
         <repo>/snapshots/<revision>/ exists with weights, return it. HF
         cache stores commit SHAs as snapshot dirs without a refs entry.
      3. Branch/tag ref resolution. Read <repo>/refs/<revision>, look up
         the SHA, and return <repo>/snapshots/<sha>/ if it exists with
         weights.
    Returns None if no valid snapshot found.
    """
    if not revision:
        return None
    if ".." in revision or revision.startswith("/") or revision.startswith("\\"):
        return None
    refs_root = os.path.realpath(os.path.join(cache_dir, "refs"))

    # 2. Direct SHA path
    if _HEX_SHA_RE.match(revision):
        snap_dir = os.path.join(cache_dir, "snapshots", revision)
        if os.path.isdir(snap_dir) and _has_weights(snap_dir):
            return snap_dir
        # fall through to refs lookup

    # 3. Refs-based lookup
    refs_path = os.path.join(cache_dir, "refs", revision)
    try:
        # Path-safety check: ensure refs_path resolves under refs_root.
        normalized = os.path.realpath(refs_path)
        if not (
            normalized == refs_root
            or normalized.startswith(refs_root + os.sep)
        ):
            return None
    except OSError:
        return None
    try:
        with open(refs_path, "r") as f:
            sha = f.read().strip()
    except OSError:
        return None
    if not sha:
        return None
    snap_dir = os.path.join(cache_dir, "snapshots", sha)
    if os.path.isdir(snap_dir) and _has_weights(snap_dir):
        return snap_dir
    return None


def _snapshot_sha(snapshot_path: str) -> Optional[str]:
    """Extract the snapshot SHA from a path of the form
    .../snapshots/<sha>/. Returns None if it doesn't match."""
    base = os.path.basename(os.path.normpath(snapshot_path))
    if _HEX_SHA_RE.match(base):
        return base
    return None


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
    revision: str = "main"
    resolved_sha: Optional[str] = None
    backend: str = "vllm"               # "vllm" | "llama.cpp"
    gguf_filename: Optional[str] = None  # primary shard for llama.cpp rows

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
            "revision": self.revision,
            "resolved_sha": self.resolved_sha,
            "backend": self.backend,
            "gguf_filename": self.gguf_filename,
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
        self._lock = threading.RLock()
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # In-memory DBs don't support WAL; fall back silently.
            pass
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._quick_check()
        self._bootstrap()
        self._migrate_revision_column()
        self._wal_checkpoint_best_effort()

    def _quick_check(self) -> None:
        """Raise CatalogCorruptionError unless quick_check returns exactly
        one row with the literal string 'ok'."""
        rows = self._conn.execute("PRAGMA quick_check").fetchall()
        if len(rows) != 1 or rows[0][0] != "ok":
            detail = "; ".join(str(r[0]) for r in rows) or "no rows"
            raise CatalogCorruptionError(f"PRAGMA quick_check failed: {detail}")

    def _wal_checkpoint_best_effort(self) -> None:
        """Bound WAL growth across restarts. In-memory DBs don't support
        the checkpoint; ignore those failures."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        self._closed = True

    def _bootstrap(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                  version INTEGER PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS models (
                  alias                   TEXT PRIMARY KEY,
                  hf_model_id             TEXT NOT NULL,
                  source                  TEXT NOT NULL,
                  quantization            TEXT,
                  gpus                    TEXT NOT NULL,
                  max_model_len           INTEGER,
                  storage_location        TEXT NOT NULL,
                  cache_path              TEXT,
                  size_bytes              INTEGER,
                  status                  TEXT NOT NULL,
                  installed_at            INTEGER,
                  last_used_at            INTEGER,
                  request_count           INTEGER DEFAULT 0,
                  extra_args              TEXT,
                  revision                TEXT NOT NULL DEFAULT 'main',
                  resolved_sha            TEXT,
                  backend                 TEXT NOT NULL DEFAULT 'vllm',
                  gguf_filename           TEXT,
                  total_prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                  total_completion_tokens INTEGER NOT NULL DEFAULT 0
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
                CREATE TABLE IF NOT EXISTS request_usage (
                  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts                 REAL NOT NULL,
                  requested_model    TEXT,
                  alias              TEXT,
                  backend            TEXT,
                  prompt_tokens      INTEGER NOT NULL DEFAULT 0,
                  completion_tokens  INTEGER NOT NULL DEFAULT 0,
                  total_tokens       INTEGER NOT NULL DEFAULT 0,
                  usage_json         TEXT
                );
                CREATE TABLE IF NOT EXISTS pg_usage_outbox (
                  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id           TEXT    NOT NULL UNIQUE,
                  ts                 REAL    NOT NULL,
                  requested_model    TEXT,
                  alias              TEXT,
                  backend            TEXT,
                  endpoint           TEXT    NOT NULL,
                  streamed           INTEGER NOT NULL DEFAULT 0,
                  prompt_tokens      INTEGER NOT NULL DEFAULT 0,
                  completion_tokens  INTEGER NOT NULL DEFAULT 0,
                  total_tokens       INTEGER NOT NULL DEFAULT 0,
                  response_ms        REAL    NOT NULL DEFAULT 0,
                  status_code        INTEGER NOT NULL DEFAULT 200
                );
                CREATE INDEX IF NOT EXISTS idx_downloads_alias       ON downloads(alias);
                CREATE INDEX IF NOT EXISTS idx_models_last_used      ON models(last_used_at);
                CREATE INDEX IF NOT EXISTS idx_models_hf_id          ON models(hf_model_id);
                CREATE INDEX IF NOT EXISTS idx_request_usage_alias_ts ON request_usage(alias, ts);
                """
            )
            existing = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif int(existing["version"]) < SCHEMA_VERSION:
                # All additions through v3 are additive (CREATE TABLE IF NOT
                # EXISTS / additive ALTERs above), so the only step here is
                # bumping the stored version once the bootstrap script has
                # ensured every needed table exists.
                self._conn.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )

    def _migrate_revision_column(self) -> None:
        """Additive ALTER for legacy DBs predating later phases. On a fresh DB,
        _bootstrap creates these columns directly and both branches no-op."""
        with self._lock, self._conn:
            cols = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info('models')")
            }
            if "revision" not in cols:
                self._conn.execute(
                    "ALTER TABLE models ADD COLUMN revision TEXT NOT NULL DEFAULT 'main'"
                )
            if "resolved_sha" not in cols:
                self._conn.execute(
                    "ALTER TABLE models ADD COLUMN resolved_sha TEXT"
                )
            if "backend" not in cols:
                self._conn.execute(
                    "ALTER TABLE models ADD COLUMN backend TEXT NOT NULL DEFAULT 'vllm'"
                )
            if "gguf_filename" not in cols:
                self._conn.execute(
                    "ALTER TABLE models ADD COLUMN gguf_filename TEXT"
                )
            if "total_prompt_tokens" not in cols:
                self._conn.execute(
                    "ALTER TABLE models ADD COLUMN total_prompt_tokens "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "total_completion_tokens" not in cols:
                self._conn.execute(
                    "ALTER TABLE models ADD COLUMN total_completion_tokens "
                    "INTEGER NOT NULL DEFAULT 0"
                )

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
        with self._lock, self._conn:
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
        with self._lock, self._conn:
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
                "SELECT source, hf_model_id, storage_location, revision, "
                "backend, gguf_filename "
                "FROM models WHERE alias=?", (m.alias,)
            ).fetchone()

            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO models (
                      alias, hf_model_id, source, quantization, gpus,
                      max_model_len, storage_location, cache_path, size_bytes,
                      status, installed_at, last_used_at, request_count,
                      extra_args, revision, backend, gguf_filename
                    ) VALUES (?, ?, 'config', ?, ?, ?, ?, NULL, NULL,
                              'partial', NULL, NULL, 0, ?, ?, ?, ?)
                    """,
                    (m.alias, m.model, m.quantization, gpus_json,
                     m.max_model_len, storage_loc, extra_json, m.revision,
                     m.backend, m.gguf_filename),
                )
                added += 1
            else:
                if row["source"] == "ui_install":
                    logger.warning(
                        "alias '%s' was source='ui_install'; config wins (PRD §5.1)",
                        m.alias,
                    )
                    ui_overwritten += 1
                # Backend/file changes also imply a different on-disk layout,
                # so wipe the cached snapshot pin when they shift.
                row_backend = row["backend"] if "backend" in row.keys() else "vllm"
                row_gguf = row["gguf_filename"] if "gguf_filename" in row.keys() else None
                clears_cached_snapshot = (
                    row["source"] == "ui_install"
                    or row["hf_model_id"] != m.model
                    or row["storage_location"] != storage_loc
                    or row["revision"] != m.revision
                    or row_backend != m.backend
                    or row_gguf != m.gguf_filename
                )
                cache_reset_sql = (
                    "cache_path = NULL, status = 'partial', resolved_sha = NULL,"
                    if clears_cached_snapshot
                    else ""
                )
                self._conn.execute(
                    f"""
                    UPDATE models SET
                      hf_model_id      = ?,
                      source           = 'config',
                      quantization     = ?,
                      gpus             = ?,
                      max_model_len    = ?,
                      storage_location = ?,
                      {cache_reset_sql}
                      extra_args       = ?,
                      revision         = ?,
                      backend          = ?,
                      gguf_filename    = ?
                    WHERE alias = ?
                    """,
                    (m.model, m.quantization, gpus_json, m.max_model_len,
                     storage_loc, extra_json, m.revision,
                     m.backend, m.gguf_filename, m.alias),
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
        with self._lock, self._conn:
            return self._reconcile_cache_uncommitted(storage_paths)

    def _reconcile_cache_uncommitted(self, storage_paths: dict[str, str]) -> ReconcileResult:
        installed = 0
        partial = 0
        loc_missing = 0
        rows = list(
            self._conn.execute(
                "SELECT alias, hf_model_id, storage_location, status, revision, "
                "backend, gguf_filename FROM models"
            )
        )
        for row in rows:
            # Skip hard-failed installs — don't promote on a half-finished
            # snapshot dir; user-driven retry only.
            if row["status"] == "error":
                continue

            loc_path = storage_paths.get(row["storage_location"])
            if loc_path is None:
                self._conn.execute(
                    "UPDATE models SET cache_path=NULL, status='partial', "
                    "resolved_sha=NULL WHERE alias=?",
                    (row["alias"],),
                )
                loc_missing += 1
                partial += 1
                continue

            cache_dir = os.path.join(loc_path, "hub", _hf_dir_name(row["hf_model_id"]))
            revision = row["revision"] if "revision" in row.keys() else None
            backend = (row["backend"] if "backend" in row.keys() else "vllm") or "vllm"
            gguf_filename = (
                row["gguf_filename"] if "gguf_filename" in row.keys() else None
            )
            snap = _snapshot_for_revision(cache_dir, revision)
            if snap is None:
                # Fall back to newest-mtime for legacy rows with no revision
                # set (shouldn't happen post-migration, but defensive).
                if not revision:
                    snap_fallback = _newest_snapshot(cache_dir)
                    if snap_fallback and _has_any_weights(snap_fallback):
                        snap = snap_fallback
            # Backend-aware presence check: llama.cpp rows must have their
            # specific gguf_filename (and all shards) present, not just any
            # GGUF in a shared snapshot.
            if snap is not None and not _has_expected_weights(
                snap, backend, gguf_filename,
            ):
                snap = None
            if snap:
                sha = _snapshot_sha(snap)
                self._conn.execute(
                    "UPDATE models SET cache_path=?, status='installed', "
                    "resolved_sha=? WHERE alias=?",
                    (snap, sha, row["alias"]),
                )
                installed += 1
            else:
                self._conn.execute(
                    "UPDATE models SET cache_path=NULL, status='partial', "
                    "resolved_sha=NULL WHERE alias=?",
                    (row["alias"],),
                )
                partial += 1
        return ReconcileResult(installed=installed, partial=partial, location_missing=loc_missing)

    # ── reads ─────────────────────────────────────────────────────────

    def list_models(self) -> list[CatalogRow]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM models ORDER BY alias").fetchall()
        return [self._to_catalog_row(r) for r in rows]

    def get_model(self, alias: str) -> Optional[CatalogRow]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM models WHERE alias=?", (alias,)
            ).fetchone()
        return self._to_catalog_row(row) if row else None

    def get_model_case_insensitive(self, alias: str) -> Optional[CatalogRow]:
        """Return a row by alias, falling back to ASCII case-insensitive match.

        Exact-case matches win. HuggingFace ids and our generated aliases are
        ASCII, so SQLite NOCASE is sufficient and avoids changing stored casing.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM models WHERE alias=? COLLATE NOCASE "
                "ORDER BY CASE WHEN alias=? THEN 0 ELSE 1 END, alias "
                "LIMIT 1",
                (alias, alias),
            ).fetchone()
        return self._to_catalog_row(row) if row else None

    def list_downloads(self) -> list[DownloadRow]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM downloads ORDER BY id").fetchall()
        return [self._to_download_row(r) for r in rows]

    def get_download(self, alias: str) -> Optional[DownloadRow]:
        """Return the most recent download row for an alias, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM downloads WHERE alias=? ORDER BY id DESC LIMIT 1",
                (alias,),
            ).fetchone()
        return self._to_download_row(row) if row else None

    def delete_downloads(self, alias: str) -> int:
        """Delete download history rows for an alias without touching the model row."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM downloads WHERE alias=?",
                (alias,),
            )
            return cursor.rowcount

    def lookup_by_hf_id(self, hf_model_id: str) -> list[CatalogRow]:
        """Return all rows with the given HF model id, ui_install rows first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM models WHERE hf_model_id=? "
                "ORDER BY (source='ui_install') DESC, alias",
                (hf_model_id,),
            ).fetchall()
        return [self._to_catalog_row(r) for r in rows]

    def lookup_by_hf_id_case_insensitive(self, hf_model_id: str) -> list[CatalogRow]:
        """Return rows for an HF model id without requiring exact casing.

        Exact-case matches sort first, then ui_install rows, preserving the
        existing preference used by lookup_by_hf_id.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM models WHERE hf_model_id=? COLLATE NOCASE "
                "ORDER BY CASE WHEN hf_model_id=? THEN 0 ELSE 1 END, "
                "(source='ui_install') DESC, alias",
                (hf_model_id, hf_model_id),
            ).fetchall()
        return [self._to_catalog_row(r) for r in rows]

    def find_active_for(
        self, storage_location: str, hf_model_id: str
    ) -> Optional[str]:
        """Return the alias of any active download (queued or downloading)
        on the same (storage_location, hf_model_id) pair, or None.

        Revision is intentionally excluded from the tuple — HF stores all
        revisions of a repo under one shared cache dir; concurrent workers
        on different revisions corrupt each other's refs/blobs/.incomplete.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT m.alias FROM models m "
                "JOIN downloads d ON d.alias=m.alias "
                "WHERE m.storage_location=? AND m.hf_model_id=? "
                "AND d.status IN ('queued','downloading') "
                "LIMIT 1",
                (storage_location, hf_model_id),
            ).fetchone()
        return row["alias"] if row else None

    def find_active_by_hf_id(self, hf_model_id: str) -> Optional[str]:
        """Like find_active_for but storage-agnostic; used by legacy
        DELETE /manager/cache/{model_id} to refuse if any matching alias
        has an active download."""
        with self._lock:
            row = self._conn.execute(
                "SELECT m.alias FROM models m "
                "JOIN downloads d ON d.alias=m.alias "
                "WHERE m.hf_model_id=? "
                "AND d.status IN ('queued','downloading') "
                "LIMIT 1",
                (hf_model_id,),
            ).fetchone()
        return row["alias"] if row else None

    def find_repo_siblings(
        self,
        storage_location: str,
        hf_model_id: str,
        exclude_alias: Optional[str] = None,
    ) -> list[CatalogRow]:
        """Return all alias rows that share (storage_location, hf_model_id)
        — i.e. point at the same on-disk repo cache dir. Used by cache
        deletes to mark every sibling 'partial' since the wipe nukes them
        all together."""
        with self._lock:
            if exclude_alias is None:
                rows = self._conn.execute(
                    "SELECT * FROM models WHERE storage_location=? AND hf_model_id=?",
                    (storage_location, hf_model_id),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM models WHERE storage_location=? "
                    "AND hf_model_id=? AND alias!=?",
                    (storage_location, hf_model_id, exclude_alias),
                ).fetchall()
        return [self._to_catalog_row(r) for r in rows]

    # ── usage flush ───────────────────────────────────────────────────

    def bump_usage(self, alias: str, last_used_at: Optional[float], delta: int) -> None:
        """Buffered usage flush from the runtime hot path. Single UPDATE.

        No-op when nothing to flush, or when the alias doesn't have a row
        (raw-id passthrough — UPDATE silently matches zero rows). Caller
        is responsible for zeroing its in-memory delta after a successful
        call."""
        if delta <= 0 or last_used_at is None:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE models SET last_used_at=?, request_count = request_count + ? "
                "WHERE alias=?",
                (int(last_used_at), delta, alias),
            )

    def record_usage_batch(self, rows: Iterable[tuple]) -> None:
        """Drain pending token-usage rows from the proxy hot path.

        Each tuple: (ts, requested_model, alias, backend,
                     prompt_tokens, completion_tokens, total_tokens, usage_json).
        Inserts one row per request into request_usage and bumps per-alias
        aggregate columns on models in a single transaction. Aliases with
        no matching models row (raw HF id passthrough) are still logged in
        request_usage; the UPDATE silently matches zero rows.
        """
        rows = list(rows)
        if not rows:
            return
        agg: dict[str, tuple[int, int, float]] = {}
        for ts, _req, alias, _backend, prompt, completion, _total, _uj in rows:
            if not alias:
                continue
            p_sum, c_sum, last_ts = agg.get(alias, (0, 0, 0.0))
            agg[alias] = (p_sum + prompt, c_sum + completion, max(last_ts, ts))
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO request_usage "
                "(ts, requested_model, alias, backend, prompt_tokens, "
                " completion_tokens, total_tokens, usage_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            for alias, (p_sum, c_sum, last_ts) in agg.items():
                self._conn.execute(
                    "UPDATE models SET "
                    "total_prompt_tokens = COALESCE(total_prompt_tokens, 0) + ?, "
                    "total_completion_tokens = COALESCE(total_completion_tokens, 0) + ?, "
                    "last_used_at = MAX(COALESCE(last_used_at, 0), ?) "
                    "WHERE alias = ?",
                    (p_sum, c_sum, int(last_ts), alias),
                )

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        return int(row["version"]) if row else -1

    # ── token-sidecar outbox (v3) ─────────────────────────────────────
    #
    # Mirror of the postgres token_usage table, used as a local cache so a
    # postgres outage or container restart doesn't drop usage data. The
    # background pg-flush loop drains this table in event_id-order.

    def enqueue_pg_outbox(self, rows: Iterable[tuple]) -> None:
        """Append a batch to the postgres outbox.

        Each row is a 12-tuple:
          (event_id, ts, requested_model, alias, backend, endpoint,
           streamed, prompt, completion, total, response_ms, status_code)
        Caller is responsible for stable, unique event_ids (UUIDs); the
        UNIQUE index plus INSERT OR IGNORE drops accidental dupes silently.
        """
        rows = list(rows)
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO pg_usage_outbox "
                "(event_id, ts, requested_model, alias, backend, endpoint, "
                " streamed, prompt_tokens, completion_tokens, total_tokens, "
                " response_ms, status_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def peek_pg_outbox(self, limit: int) -> list[sqlite3.Row]:
        """Return up to `limit` oldest outbox rows in insertion order.

        Caller passes `r["event_id"]` etc. through the postgres writer, then
        calls `delete_pg_outbox([r["id"] for r in rows])` after a successful
        write. The SQLite `id` column is the durable handle for deletion;
        `event_id` is the de-dup key on the postgres side.
        """
        if limit <= 0:
            return []
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, event_id, ts, requested_model, alias, backend, "
                " endpoint, streamed, prompt_tokens, completion_tokens, "
                " total_tokens, response_ms, status_code "
                "FROM pg_usage_outbox ORDER BY id LIMIT ?",
                (int(limit),),
            )
            return cur.fetchall()

    def delete_pg_outbox(self, ids: Iterable[int]) -> int:
        """Delete the named outbox rows. Returns the count actually removed."""
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        with self._lock, self._conn:
            # Use a single DELETE with an IN-list to keep this atomic.
            placeholders = ",".join("?" * len(ids))
            cur = self._conn.execute(
                f"DELETE FROM pg_usage_outbox WHERE id IN ({placeholders})", ids,
            )
            return cur.rowcount

    def count_pg_outbox(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM pg_usage_outbox"
            ).fetchone()
        return int(row["c"]) if row else 0

    def prune_pg_outbox(self, keep: int) -> int:
        """Drop oldest outbox rows until at most `keep` remain.

        Called when the outbox is over `max_outbox_rows` so a long postgres
        outage can't fill the SQLite catalog indefinitely. Returns the
        number of rows actually dropped.
        """
        keep = max(0, int(keep))
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM pg_usage_outbox WHERE id IN ("
                "  SELECT id FROM pg_usage_outbox "
                "  ORDER BY id DESC LIMIT -1 OFFSET ?"
                ")",
                (keep,),
            )
            return cur.rowcount

    # ── install / download transitions (Phase 4 §3b) ──────────────────

    def start_install_tx(
        self,
        *,
        alias: str,
        hf_model_id: str,
        source: str = "ui_install",
        revision: str = "main",
        quantization: Optional[str] = None,
        gpus: list | str = "all",
        max_model_len: Optional[int] = None,
        storage_location: str,
        extra_args: Optional[list[str]] = None,
        total_bytes_hint: Optional[int] = None,
        backend: str = "vllm",
        gguf_filename: Optional[str] = None,
    ) -> int:
        """Atomically: insert/upsert the models row at status='queued' and
        insert a new downloads row at status='queued'. Returns the new
        downloads.id. Clears any stale resolved_sha pin on retry."""
        gpus_json = json.dumps(gpus)
        extra_json = json.dumps(extra_args or [])
        now = int(time.time())
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT alias FROM models WHERE alias=?", (alias,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO models (
                      alias, hf_model_id, source, quantization, gpus,
                      max_model_len, storage_location, cache_path, size_bytes,
                      status, installed_at, last_used_at, request_count,
                      extra_args, revision, resolved_sha, backend, gguf_filename
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                              'queued', NULL, NULL, 0, ?, ?, NULL, ?, ?)
                    """,
                    (alias, hf_model_id, source, quantization, gpus_json,
                     max_model_len, storage_location, extra_json, revision,
                     backend, gguf_filename),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE models SET
                      hf_model_id      = ?,
                      source           = ?,
                      quantization     = ?,
                      gpus             = ?,
                      max_model_len    = ?,
                      storage_location = ?,
                      cache_path       = NULL,
                      status           = 'queued',
                      extra_args       = ?,
                      revision         = ?,
                      resolved_sha     = NULL,
                      backend          = ?,
                      gguf_filename    = ?
                    WHERE alias = ?
                    """,
                    (hf_model_id, source, quantization, gpus_json,
                     max_model_len, storage_location, extra_json, revision,
                     backend, gguf_filename, alias),
                )
            cursor = self._conn.execute(
                "INSERT INTO downloads (alias, pid, status, started_at, "
                "bytes_downloaded, total_bytes) VALUES (?, NULL, 'queued', ?, 0, ?)",
                (alias, now, total_bytes_hint),
            )
            return cursor.lastrowid

    def mark_downloading(
        self, alias: str, *, pid: Optional[int], total_bytes: Optional[int]
    ) -> None:
        """Worker emitted 'start'. Update the active downloads row."""
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE downloads SET status='downloading', pid=?, "
                "total_bytes=COALESCE(?, total_bytes), started_at=? "
                "WHERE alias=? AND status IN ('queued','downloading')",
                (pid, total_bytes, now, alias),
            )

    def mark_progress(self, alias: str, bytes_downloaded: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE downloads SET bytes_downloaded=? "
                "WHERE alias=? AND status IN ('queued','downloading')",
                (bytes_downloaded, alias),
            )

    def mark_complete(
        self,
        alias: str,
        *,
        cache_path: str,
        size_bytes: Optional[int],
        resolved_sha: Optional[str],
    ) -> None:
        """Worker exited 0 with a complete event. Atomic: models→installed,
        downloads→complete, resolved_sha pinned to the snapshot's actual SHA.

        Also reconciles `bytes_downloaded` to the on-disk size and bumps
        `total_bytes` to match — the worker's 1-Hz progress emitter can drop
        the final sample, leaving the UI showing 93%-ish on a fully-complete
        row. Trusting `size_bytes` here keeps the row honest after restart.
        """
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE models SET status='installed', cache_path=?, "
                "size_bytes=?, installed_at=?, resolved_sha=? WHERE alias=?",
                (cache_path, size_bytes, now, resolved_sha, alias),
            )
            if size_bytes is not None:
                self._conn.execute(
                    "UPDATE downloads SET status='complete', finished_at=?, "
                    "bytes_downloaded=?, "
                    "total_bytes=COALESCE(total_bytes, ?) "
                    "WHERE alias=? AND status IN ('queued','downloading')",
                    (now, size_bytes, size_bytes, alias),
                )
            else:
                self._conn.execute(
                    "UPDATE downloads SET status='complete', finished_at=? "
                    "WHERE alias=? AND status IN ('queued','downloading')",
                    (now, alias),
                )

    def mark_error(self, alias: str, message: str) -> None:
        """Hard worker failure → models='error', downloads='error'.
        Distinguishes 'broken — investigate' from 'resumable'."""
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE models SET status='error', resolved_sha=NULL WHERE alias=?",
                (alias,),
            )
            self._conn.execute(
                "UPDATE downloads SET status='error', error=?, finished_at=? "
                "WHERE alias=? AND status IN ('queued','downloading')",
                (message, now, alias),
            )

    def mark_cancelled(self, alias: str) -> None:
        """User-cancelled (SIGTERM) → models='partial', downloads='cancelled'.
        Resumable; clears stale resolved_sha pin."""
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE models SET status='partial', resolved_sha=NULL WHERE alias=?",
                (alias,),
            )
            self._conn.execute(
                "UPDATE downloads SET status='cancelled', finished_at=? "
                "WHERE alias=? AND status IN ('queued','downloading')",
                (now, alias),
            )

    def mark_orphan_interrupted(self, alias: str) -> None:
        """Manager restart mid-download → models='partial', downloads='error'
        with explanatory message. Subsequent reconcile may promote back
        to 'installed' if the snapshot is actually complete on disk."""
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE models SET status='partial', resolved_sha=NULL WHERE alias=?",
                (alias,),
            )
            self._conn.execute(
                "UPDATE downloads SET status='error', "
                "error='interrupted by manager restart', finished_at=? "
                "WHERE alias=? AND status IN ('queued','downloading')",
                (now, alias),
            )

    def mark_partial(self, alias: str) -> None:
        """Cache-only delete on an aliased row → row stays, status flips.
        Clears cache_path and resolved_sha."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE models SET status='partial', cache_path=NULL, "
                "resolved_sha=NULL WHERE alias=?",
                (alias,),
            )

    def delete_install_row(self, alias: str) -> int:
        """Full removal — models row + cascading downloads. Returns rows
        affected. Used by full-removal endpoint and synthetic cache-only
        delete paths."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM models WHERE alias=? AND source='ui_install'",
                (alias,),
            )
            return cursor.rowcount

    def update_launch_settings(
        self,
        *,
        alias: str,
        quantization: Optional[str],
        gpus: list | str,
        max_model_len: Optional[int],
        extra_args: Optional[list[str]],
    ) -> Optional[CatalogRow]:
        """Update launch-time settings for an existing ui_install row.

        This leaves model identity, revision, storage, cache path, status, and
        resolved_sha untouched. The new settings take effect the next time the
        alias is loaded.
        """
        gpus_json = json.dumps(gpus)
        extra_json = json.dumps(extra_args or [])
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT source FROM models WHERE alias=?", (alias,)
            ).fetchone()
            if existing is None or existing["source"] != "ui_install":
                return None
            self._conn.execute(
                """
                UPDATE models SET
                  quantization  = ?,
                  gpus          = ?,
                  max_model_len = ?,
                  extra_args    = ?
                WHERE alias = ? AND source = 'ui_install'
                """,
                (quantization, gpus_json, max_model_len, extra_json, alias),
            )
            return self.get_model(alias)

    def recover_orphan_downloads(self) -> int:
        """Find downloads rows in queued/downloading state at startup and
        mark them interrupted. Returns the count. Called BEFORE
        apply_config() so reconcile may then promote any whose snapshot
        is actually complete on disk."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT alias FROM downloads WHERE status IN ('queued','downloading')"
            ).fetchall()
            count = 0
            now = int(time.time())
            for r in rows:
                alias = r["alias"]
                self._conn.execute(
                    "UPDATE models SET status='partial', resolved_sha=NULL WHERE alias=?",
                    (alias,),
                )
                self._conn.execute(
                    "UPDATE downloads SET status='error', "
                    "error='interrupted by manager restart', finished_at=? "
                    "WHERE alias=? AND status IN ('queued','downloading')",
                    (now, alias),
                )
                count += 1
            return count

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
        revision: str = "main",
        resolved_sha: Optional[str] = None,
        backend: str = "vllm",
        gguf_filename: Optional[str] = None,
    ) -> None:
        """Direct insert bypassing config validation. Used by Phase 4 for
        ui_install rows and by tests for synthetic-alias rows."""
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO models (
                  alias, hf_model_id, source, quantization, gpus,
                  max_model_len, storage_location, cache_path, size_bytes,
                  status, installed_at, last_used_at, request_count,
                  extra_args, revision, resolved_sha, backend, gguf_filename
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (alias, hf_model_id, source, quantization, gpus,
                 max_model_len, storage_location, cache_path, size_bytes,
                 status, installed_at, last_used_at, request_count,
                 extra_args, revision, resolved_sha, backend, gguf_filename),
            )

    def _raw_insert_download(
        self,
        *,
        alias: str,
        status: str,
        started_at: int,
        finished_at: Optional[int] = None,
        bytes_downloaded: int = 0,
        total_bytes: Optional[int] = None,
        error: Optional[str] = None,
        pid: Optional[int] = None,
    ) -> int:
        """Test/fixture helper for seeding downloads rows."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO downloads (alias, pid, status, started_at, "
                "finished_at, bytes_downloaded, total_bytes, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (alias, pid, status, started_at, finished_at,
                 bytes_downloaded, total_bytes, error),
            )
            return cursor.lastrowid

    def _to_catalog_row(self, row) -> CatalogRow:
        # SQLite Row supports keys() — older rows from the migration may
        # have None for new columns. Use .get-style fallbacks.
        keys = set(row.keys())
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
            revision=(row["revision"] if "revision" in keys and row["revision"] else "main"),
            resolved_sha=(row["resolved_sha"] if "resolved_sha" in keys else None),
            backend=(row["backend"] if "backend" in keys and row["backend"] else "vllm"),
            gguf_filename=(row["gguf_filename"] if "gguf_filename" in keys else None),
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


def _quarantine_corrupt_db(db_path: str) -> list[str]:
    """Rename `<db>` and any sibling `-wal`/`-shm` files to `*.corrupt-<ts>`.
    Returns the list of quarantine paths actually created."""
    stamp = time.strftime("%Y%m%d%H%M%S")
    quarantined: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        src = db_path + suffix
        if not os.path.exists(src):
            continue
        dst = f"{db_path}.corrupt-{stamp}{suffix}"
        try:
            os.rename(src, dst)
            quarantined.append(dst)
        except OSError as e:
            logger.warning("could not quarantine %s: %s", src, e)
    return quarantined


def open_catalog(path: str | None = None) -> Catalog:
    """Open or create the SQLite catalog. Path resolved at call time.

    If the on-disk DB is corrupt (PRAGMA quick_check fails, or open/bootstrap
    raises sqlite3.DatabaseError / OperationalError), quarantine the files to
    `<db>.corrupt-<timestamp>` and open a fresh DB at the original path.
    Startup apply_config + reconcile then repopulate config rows and recover
    installed/partial state from storage.
    """
    db_path = path or os.environ.get("MNEMOSYNE_DB_PATH", DEFAULT_DB_PATH)
    if db_path != ":memory:":
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        return Catalog(conn)
    except (CatalogCorruptionError, sqlite3.DatabaseError, sqlite3.OperationalError) as e:
        try:
            conn.close()
        except Exception:
            pass
        if db_path == ":memory:":
            raise
        quarantined = _quarantine_corrupt_db(db_path)
        logger.error(
            "catalog corruption detected at %s (%s: %s); quarantined to %s",
            db_path, type(e).__name__, e, quarantined or "<nothing>",
        )
        fresh = sqlite3.connect(db_path, check_same_thread=False)
        return Catalog(fresh)
