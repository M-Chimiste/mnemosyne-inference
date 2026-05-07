"""Phase 1 — SQLite catalog: schema, sync, reconcile, durable metadata."""
from __future__ import annotations

import json
import os
import time

import pytest

from catalog import (
    Catalog,
    is_cache_only_alias,
    open_catalog,
    synthetic_alias,
)
from config import Config, ModelProfile


# ── helpers ──────────────────────────────────────────────────────────

def _profiles(*specs) -> list[ModelProfile]:
    return [ModelProfile.model_validate(s) for s in specs]


def _make_cache(
    storage_root,
    hf_id: str,
    *,
    with_weights: bool = True,
    revision: str = "main",
    sha: str = "a" * 40,
) -> str:
    """Build a fake HF cache dir under <storage_root>/hub/models--<...>/snapshots/<sha>/.

    Also writes refs/<revision> pointing at the snapshot SHA so reconcile's
    revision-aware resolver finds it.
    """
    safe = "models--" + hf_id.replace("/", "--")
    repo = os.path.join(storage_root, "hub", safe)
    snap = os.path.join(repo, "snapshots", sha)
    os.makedirs(snap, exist_ok=True)
    if with_weights:
        with open(os.path.join(snap, "model.safetensors"), "wb") as f:
            f.write(b"\x00")
    refs_dir = os.path.join(repo, "refs")
    os.makedirs(refs_dir, exist_ok=True)
    with open(os.path.join(refs_dir, revision), "w") as f:
        f.write(sha)
    return snap


@pytest.fixture
def cat():
    c = open_catalog(":memory:")
    try:
        yield c
    finally:
        c.close()


# ── schema bootstrap ────────────────────────────────────────────────

def test_bootstrap_creates_tables_and_version(cat):
    assert cat.schema_version() == 1
    # tables exist
    rows = cat._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"models", "downloads", "schema_version"} <= names


def test_bootstrap_idempotent():
    c1 = open_catalog(":memory:")
    try:
        # Re-running on the same connection is a no-op (CREATE IF NOT EXISTS).
        c1._bootstrap()
        assert c1.schema_version() == 1
    finally:
        c1.close()


# ── sync_from_config ─────────────────────────────────────────────────

def test_sync_inserts_new_rows(cat):
    profiles = _profiles({"alias": "a", "model": "org/a"})
    res = cat.sync_from_config(profiles, default_storage="tmp")
    assert res.added == 1
    assert res.updated == 0
    assert res.removed_config_orphans == 0
    rows = cat.list_models()
    assert len(rows) == 1
    assert rows[0].alias == "a"
    assert rows[0].source == "config"
    assert rows[0].status == "partial"
    assert rows[0].storage_location == "tmp"
    assert json.loads(rows[0].gpus) == "all"


def test_sync_updates_declarative_columns(cat):
    cat.sync_from_config(_profiles({"alias": "a", "model": "org/a"}), default_storage="tmp")
    res = cat.sync_from_config(
        _profiles({"alias": "a", "model": "org/a-v2", "quantization": "awq"}),
        default_storage="tmp",
    )
    assert res.added == 0 and res.updated == 1
    row = cat.get_model("a")
    assert row.hf_model_id == "org/a-v2"
    assert row.quantization == "awq"


def test_sync_removes_orphaned_config_rows(cat):
    cat.sync_from_config(
        _profiles({"alias": "a", "model": "org/a"}, {"alias": "b", "model": "org/b"}),
        default_storage="tmp",
    )
    res = cat.sync_from_config(
        _profiles({"alias": "a", "model": "org/a"}),
        default_storage="tmp",
    )
    assert res.removed_config_orphans == 1
    aliases = [r.alias for r in cat.list_models()]
    assert aliases == ["a"]


def test_sync_preserves_ui_install_rows(cat):
    cat._raw_insert_model(
        alias="ui-foo",
        hf_model_id="org/ui-foo",
        source="ui_install",
        gpus='"all"',
        storage_location="tmp",
    )
    res = cat.sync_from_config(
        _profiles({"alias": "a", "model": "org/a"}),
        default_storage="tmp",
    )
    assert res.ui_preserved == 1
    assert res.ui_overwritten == 0
    aliases = sorted(r.alias for r in cat.list_models())
    assert aliases == ["a", "ui-foo"]
    assert cat.get_model("ui-foo").source == "ui_install"


def test_sync_config_wins_on_ui_install_collision(cat):
    cat._raw_insert_model(
        alias="dup",
        hf_model_id="evil/shadow",
        source="ui_install",
        gpus='"all"',
        storage_location="tmp",
        cache_path="/cached/path",
        resolved_sha="a" * 40,
        last_used_at=999,
        request_count=5,
    )
    res = cat.sync_from_config(
        _profiles({"alias": "dup", "model": "real/model"}),
        default_storage="tmp",
    )
    assert res.ui_overwritten == 1
    row = cat.get_model("dup")
    assert row.source == "config"
    assert row.hf_model_id == "real/model"
    assert row.status == "partial"
    assert row.cache_path is None
    assert row.resolved_sha is None
    # Usage metadata is preserved even on the source flip.
    assert row.last_used_at == 999
    assert row.request_count == 5


def test_sync_config_change_clears_cached_snapshot(cat):
    cat.sync_from_config(
        _profiles({"alias": "qw", "model": "org/qw", "revision": "main"}),
        default_storage="tmp",
    )
    cat._conn.execute(
        "UPDATE models SET status='installed', cache_path=?, resolved_sha=? "
        "WHERE alias='qw'",
        ("/cached/path", "b" * 40),
    )
    cat._conn.commit()

    cat.sync_from_config(
        _profiles({"alias": "qw", "model": "org/qw", "revision": "dev"}),
        default_storage="tmp",
    )
    row = cat.get_model("qw")
    assert row.revision == "dev"
    assert row.status == "partial"
    assert row.cache_path is None
    assert row.resolved_sha is None


def test_sync_preserves_durable_metadata(cat):
    """The big one: §5.4 column rules — usage history must survive reload."""
    cat.sync_from_config(_profiles({"alias": "a", "model": "org/a"}), default_storage="tmp")
    cat._conn.execute(
        """
        UPDATE models SET last_used_at=?, request_count=?, installed_at=?,
                          size_bytes=?, status='installed', cache_path=?
        WHERE alias='a'
        """,
        (12345, 42, 99000, 8_000_000_000, "/cached/path"),
    )
    cat._conn.commit()

    # Reload with mutated declarative fields.
    cat.sync_from_config(
        _profiles({"alias": "a", "model": "org/a", "quantization": "fp8"}),
        default_storage="tmp",
    )
    row = cat.get_model("a")
    # Declarative columns DID change.
    assert row.quantization == "fp8"
    # Durable columns DID NOT.
    assert row.last_used_at == 12345
    assert row.request_count == 42
    assert row.installed_at == 99000
    assert row.size_bytes == 8_000_000_000
    assert row.status == "installed"
    assert row.cache_path == "/cached/path"


# ── reconcile_cache ──────────────────────────────────────────────────

def test_reconcile_marks_installed_when_cache_present(cat, tmp_path):
    profiles = _profiles({"alias": "qw", "model": "Qwen/Qwen2.5-7B"})
    cat.sync_from_config(profiles, default_storage="tmp")
    snap = _make_cache(str(tmp_path), "Qwen/Qwen2.5-7B")
    res = cat.reconcile_cache({"tmp": str(tmp_path)})
    assert res.installed == 1
    assert res.partial == 0
    row = cat.get_model("qw")
    assert row.status == "installed"
    assert row.cache_path == snap


def test_reconcile_partial_when_no_weights(cat, tmp_path):
    cat.sync_from_config(_profiles({"alias": "qw", "model": "org/x"}), default_storage="tmp")
    _make_cache(str(tmp_path), "org/x", with_weights=False)
    res = cat.reconcile_cache({"tmp": str(tmp_path)})
    assert res.partial == 1
    assert res.installed == 0
    row = cat.get_model("qw")
    assert row.status == "partial"
    assert row.cache_path is None


def test_reconcile_partial_when_no_dir(cat, tmp_path):
    cat.sync_from_config(_profiles({"alias": "qw", "model": "org/missing"}), default_storage="tmp")
    res = cat.reconcile_cache({"tmp": str(tmp_path)})
    assert res.partial == 1
    row = cat.get_model("qw")
    assert row.status == "partial"
    assert row.cache_path is None


def test_reconcile_flips_back_to_partial_when_dir_vanishes(cat, tmp_path):
    cat.sync_from_config(_profiles({"alias": "qw", "model": "org/x"}), default_storage="tmp")
    _make_cache(str(tmp_path), "org/x")
    cat.reconcile_cache({"tmp": str(tmp_path)})
    assert cat.get_model("qw").status == "installed"

    # Delete the snapshot dir.
    import shutil
    shutil.rmtree(os.path.join(str(tmp_path), "hub"))
    cat.reconcile_cache({"tmp": str(tmp_path)})
    row = cat.get_model("qw")
    assert row.status == "partial"
    assert row.cache_path is None


def test_reconcile_storage_location_missing(cat):
    cat.sync_from_config(_profiles({"alias": "qw", "model": "org/x"}), default_storage="tmp")
    # Storage location 'tmp' is no longer in the dict.
    res = cat.reconcile_cache({})
    assert res.location_missing == 1
    assert res.partial == 1
    assert cat.get_model("qw").status == "partial"


def test_reconcile_preserves_durable_metadata(cat, tmp_path):
    """Reconciliation writes ONLY status + cache_path."""
    cat.sync_from_config(_profiles({"alias": "qw", "model": "org/x"}), default_storage="tmp")
    cat._conn.execute(
        "UPDATE models SET last_used_at=?, request_count=?, installed_at=?, size_bytes=? WHERE alias='qw'",
        (5000, 7, 1000, 12345),
    )
    cat._conn.commit()
    _make_cache(str(tmp_path), "org/x")
    cat.reconcile_cache({"tmp": str(tmp_path)})
    row = cat.get_model("qw")
    assert row.last_used_at == 5000
    assert row.request_count == 7
    assert row.installed_at == 1000
    assert row.size_bytes == 12345
    assert row.status == "installed"


# ── apply_config atomicity ────────────────────────────────────────────

def test_apply_config_rolls_back_sync_when_reconcile_fails(cat, monkeypatch):
    cat.sync_from_config(_profiles({"alias": "old", "model": "org/old"}), default_storage="tmp")
    cat._conn.execute(
        """
        UPDATE models SET last_used_at=?, request_count=?, installed_at=?,
                          size_bytes=?, status='installed', cache_path=?
        WHERE alias='old'
        """,
        (12345, 42, 99000, 8_000_000_000, "/cached/old"),
    )
    cat._conn.commit()

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated reconcile failure")

    monkeypatch.setattr(cat, "_reconcile_cache_uncommitted", boom)

    with pytest.raises(RuntimeError, match="simulated reconcile failure"):
        cat.apply_config(
            _profiles({"alias": "new", "model": "org/new"}),
            default_storage="tmp",
            storage_paths={"tmp": "/tmp/not-used"},
        )

    assert cat.get_model("new") is None
    old = cat.get_model("old")
    assert old is not None
    assert old.source == "config"
    assert old.hf_model_id == "org/old"
    assert old.last_used_at == 12345
    assert old.request_count == 42
    assert old.installed_at == 99000
    assert old.size_bytes == 8_000_000_000
    assert old.status == "installed"
    assert old.cache_path == "/cached/old"


# ── synthetic alias ──────────────────────────────────────────────────

def test_synthetic_alias_format():
    a = synthetic_alias("Qwen/Qwen2.5-7B-Instruct")
    assert a.startswith("__cache__:")
    assert len(a) == len("__cache__:") + 16
    # URL-safe (no slashes / uppercase letters)
    suffix = a[len("__cache__:"):]
    assert suffix.islower() or suffix.isdigit() or all(c in "0123456789abcdef" for c in suffix)
    assert "/" not in a


def test_synthetic_alias_deterministic():
    assert synthetic_alias("a/b") == synthetic_alias("a/b")
    assert synthetic_alias("a/b") != synthetic_alias("a/c")


def test_is_cache_only_alias():
    assert is_cache_only_alias("__cache__:abc")
    assert is_cache_only_alias("__cache__/legacy")
    assert not is_cache_only_alias("regular")
    assert not is_cache_only_alias("__cache_other")


def test_synthetic_alias_validation_rejected_by_pydantic():
    """ModelProfile.alias must reject both reserved prefixes."""
    from pydantic import ValidationError
    for bad in ("__cache__:abcd1234", "__cache__/legacy"):
        with pytest.raises(ValidationError):
            ModelProfile.model_validate({"alias": bad, "model": "org/m"})


# ── Phase 4: revision + resolved_sha plumbing ────────────────────────


def test_fresh_db_has_revision_and_resolved_sha_columns(cat):
    cols = {row["name"] for row in cat._conn.execute("PRAGMA table_info('models')")}
    assert "revision" in cols
    assert "resolved_sha" in cols


def test_sync_persists_revision(cat):
    cat.sync_from_config(
        _profiles({"alias": "qw", "model": "org/qw", "revision": "dev"}),
        default_storage="tmp",
    )
    row = cat.get_model("qw")
    assert row.revision == "dev"


def test_sync_updates_revision_on_yaml_edit(cat):
    cat.sync_from_config(
        _profiles({"alias": "qw", "model": "org/qw", "revision": "main"}),
        default_storage="tmp",
    )
    cat.sync_from_config(
        _profiles({"alias": "qw", "model": "org/qw", "revision": "v2"}),
        default_storage="tmp",
    )
    assert cat.get_model("qw").revision == "v2"


# ── Phase 4: install transitions ─────────────────────────────────────


def _seed_queued(cat, alias="qw", model="org/qw"):
    return cat.start_install_tx(
        alias=alias,
        hf_model_id=model,
        revision="main",
        gpus="all",
        storage_location="tmp",
    )


def test_start_install_tx_creates_both_rows(cat):
    download_id = _seed_queued(cat)
    row = cat.get_model("qw")
    assert row is not None
    assert row.source == "ui_install"
    assert row.status == "queued"
    assert row.resolved_sha is None
    assert isinstance(download_id, int) and download_id > 0
    download = cat.get_download("qw")
    assert download.status == "queued"


def test_mark_downloading_keeps_models_queued(cat):
    _seed_queued(cat)
    cat.mark_downloading("qw", pid=42, total_bytes=1234)
    row = cat.get_model("qw")
    assert row.status == "queued"  # models row stays queued
    download = cat.get_download("qw")
    assert download.status == "downloading"
    assert download.pid == 42
    assert download.total_bytes == 1234


def test_mark_complete_records_resolved_sha(cat):
    _seed_queued(cat)
    cat.mark_complete(
        "qw", cache_path="/path/snap/abc", size_bytes=99, resolved_sha="d" * 40,
    )
    row = cat.get_model("qw")
    assert row.status == "installed"
    assert row.resolved_sha == "d" * 40
    assert row.cache_path == "/path/snap/abc"


def test_mark_error_clears_resolved_sha(cat):
    _seed_queued(cat)
    cat.mark_complete("qw", cache_path="/p", size_bytes=1, resolved_sha="e" * 40)
    cat.mark_error("qw", "bang")
    row = cat.get_model("qw")
    assert row.status == "error"
    assert row.resolved_sha is None


def test_mark_cancelled_keeps_partial(cat):
    _seed_queued(cat)
    cat.mark_downloading("qw", pid=99, total_bytes=None)
    cat.mark_cancelled("qw")
    row = cat.get_model("qw")
    assert row.status == "partial"
    assert row.resolved_sha is None


def test_mark_partial_clears_cache_path(cat):
    _seed_queued(cat)
    cat.mark_complete("qw", cache_path="/p", size_bytes=1, resolved_sha="f" * 40)
    cat.mark_partial("qw")
    row = cat.get_model("qw")
    assert row.status == "partial"
    assert row.cache_path is None
    assert row.resolved_sha is None


def test_delete_install_row_cascades(cat):
    _seed_queued(cat)
    n = cat.delete_install_row("qw")
    assert n == 1
    assert cat.get_model("qw") is None
    assert cat.get_download("qw") is None  # CASCADE


def test_delete_downloads_keeps_install_row(cat):
    _seed_queued(cat)
    assert cat.get_download("qw") is not None
    n = cat.delete_downloads("qw")
    assert n == 1
    assert cat.get_model("qw") is not None
    assert cat.get_download("qw") is None


def test_find_active_for_revision_agnostic(cat):
    """v4: dedup uses (storage, hf_id) only — revision excluded."""
    _seed_queued(cat, alias="alpha", model="Qwen/X")
    cat.mark_downloading("alpha", pid=99, total_bytes=None)
    other = cat.find_active_for("tmp", "Qwen/X")
    assert other == "alpha"
    # Storage isolation:
    assert cat.find_active_for("other", "Qwen/X") is None


def test_find_active_by_hf_id(cat):
    _seed_queued(cat, alias="alpha", model="Qwen/Z")
    cat.mark_downloading("alpha", pid=99, total_bytes=None)
    assert cat.find_active_by_hf_id("Qwen/Z") == "alpha"
    cat.mark_complete("alpha", cache_path="/p", size_bytes=1, resolved_sha=None)
    assert cat.find_active_by_hf_id("Qwen/Z") is None


def test_recover_orphan_downloads(cat):
    _seed_queued(cat, alias="alpha")
    cat.mark_downloading("alpha", pid=42, total_bytes=None)
    n = cat.recover_orphan_downloads()
    assert n == 1
    row = cat.get_model("alpha")
    assert row.status == "partial"
    download = cat.get_download("alpha")
    assert download.status == "error"


def test_start_install_tx_clears_stale_resolved_sha(cat):
    """Reinstall after a previous successful install must clear the SHA pin
    so the resident vLLM doesn't latch onto stale weights."""
    _seed_queued(cat)
    cat.mark_complete("qw", cache_path="/p", size_bytes=1, resolved_sha="a" * 40)
    cat.start_install_tx(
        alias="qw",
        hf_model_id="org/qw",
        revision="main",
        gpus="all",
        storage_location="tmp",
    )
    row = cat.get_model("qw")
    assert row.resolved_sha is None


def test_lookup_by_hf_id_orders_ui_install_first(cat):
    cat.sync_from_config(_profiles({"alias": "config-row", "model": "Qwen/Y"}), default_storage="tmp")
    cat._raw_insert_model(
        alias="ui-row", hf_model_id="Qwen/Y", source="ui_install",
        gpus='"all"', storage_location="tmp",
    )
    rows = cat.lookup_by_hf_id("Qwen/Y")
    assert len(rows) == 2
    assert rows[0].alias == "ui-row"


def test_case_insensitive_model_lookups_preserve_canonical_rows(cat):
    cat._raw_insert_model(
        alias="Qwen-Alias",
        hf_model_id="Qwen/Qwen3.6-27B",
        source="ui_install",
        gpus='"all"',
        storage_location="tmp",
    )
    by_alias = cat.get_model_case_insensitive("qwen-alias")
    assert by_alias.alias == "Qwen-Alias"
    by_hf_id = cat.lookup_by_hf_id_case_insensitive("qwen/qwen3.6-27b")
    assert len(by_hf_id) == 1
    assert by_hf_id[0].hf_model_id == "Qwen/Qwen3.6-27B"


def test_legacy_db_migration_adds_revision(tmp_path):
    """A pre-Phase-4 DB without revision/resolved_sha columns gets
    migrated by _migrate_revision_column on open."""
    import sqlite3
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        CREATE TABLE models (
          alias TEXT PRIMARY KEY, hf_model_id TEXT NOT NULL,
          source TEXT NOT NULL, quantization TEXT, gpus TEXT NOT NULL,
          max_model_len INTEGER, storage_location TEXT NOT NULL,
          cache_path TEXT, size_bytes INTEGER, status TEXT NOT NULL,
          installed_at INTEGER, last_used_at INTEGER, request_count INTEGER DEFAULT 0,
          extra_args TEXT
        );
        CREATE TABLE downloads (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          alias TEXT NOT NULL REFERENCES models(alias) ON DELETE CASCADE,
          pid INTEGER, status TEXT NOT NULL, started_at INTEGER NOT NULL,
          finished_at INTEGER, bytes_downloaded INTEGER DEFAULT 0,
          total_bytes INTEGER, error TEXT
        );
        INSERT INTO models (alias, hf_model_id, source, gpus, storage_location, status)
        VALUES ('legacy', 'org/legacy', 'config', '"all"', 'tmp', 'partial');
    """)
    conn.commit()
    conn.close()

    cat = open_catalog(db)
    try:
        cols = {row["name"] for row in cat._conn.execute("PRAGMA table_info('models')")}
        assert "revision" in cols
        assert "resolved_sha" in cols
        row = cat.get_model("legacy")
        assert row.revision == "main"  # default backfill
        assert row.resolved_sha is None
    finally:
        cat.close()


# ── Phase 8 D4: corruption guard ─────────────────────────────────────


def test_corrupt_db_is_quarantined_and_replaced(tmp_path, caplog):
    """Phase 8 D4: open_catalog must quarantine a corrupt DB and return a
    fresh, working Catalog at the original path. Accept either quick_check
    failure or bootstrap/open failure as the trigger."""
    import glob
    import os

    db = str(tmp_path / "mnemosyne.db")

    # Seed a real catalog so a sibling -wal/-shm may exist, then close it.
    seeded = open_catalog(db)
    seeded._raw_insert_model(
        alias="seed", hf_model_id="org/seed", storage_location="tmp",
    )
    seeded.close()

    # Smash the file: write garbage over it. Either quick_check rejects it
    # or bootstrap fails on the executescript — both are caught and
    # quarantined. Wipe sidecar files so the corrupt main file is what
    # SQLite looks at.
    for suffix in ("-wal", "-shm"):
        sidecar = db + suffix
        if os.path.exists(sidecar):
            os.remove(sidecar)
    with open(db, "wb") as f:
        f.write(b"this is not a sqlite database, just garbage\n" * 1000)

    caplog.set_level("ERROR", logger="vllm-manager.catalog")
    cat = open_catalog(db)
    try:
        # Fresh DB at original path is usable.
        cat._raw_insert_model(
            alias="post-recovery",
            hf_model_id="org/post-recovery",
            storage_location="tmp",
        )
        assert cat.get_model("post-recovery") is not None
        # Old install rows did NOT survive — fresh DB.
        assert cat.get_model("seed") is None
    finally:
        cat.close()

    # A quarantine file exists.
    quarantined = glob.glob(db + ".corrupt-*")
    assert quarantined, f"expected *.corrupt-* sibling next to {db}"

    # ERROR log mentions the quarantine.
    assert any(
        "catalog corruption" in rec.getMessage().lower()
        for rec in caplog.records
    )


# ── llama.cpp / GGUF reconcile ───────────────────────────────────────


def _make_gguf_cache(
    storage_root: str,
    hf_id: str,
    *,
    filenames: list[str],
    revision: str = "main",
    sha: str = "c" * 40,
) -> str:
    """Build a fake HF cache dir with one or more `.gguf` files. Mirrors
    `_make_cache` but doesn't write a `.safetensors` placeholder."""
    safe = "models--" + hf_id.replace("/", "--")
    repo = os.path.join(storage_root, "hub", safe)
    snap = os.path.join(repo, "snapshots", sha)
    os.makedirs(snap, exist_ok=True)
    for name in filenames:
        with open(os.path.join(snap, name), "wb") as f:
            f.write(b"\x00")
    refs_dir = os.path.join(repo, "refs")
    os.makedirs(refs_dir, exist_ok=True)
    with open(os.path.join(refs_dir, revision), "w") as f:
        f.write(sha)
    return snap


def test_reconcile_llamacpp_installed_when_chosen_gguf_present(cat, tmp_path):
    cat._raw_insert_model(
        alias="qw-q4",
        hf_model_id="bartowski/Qwen2.5-7B-Instruct-GGUF",
        storage_location="tmp",
        backend="llama.cpp",
        gguf_filename="model-Q4_K_M.gguf",
        status="partial",
    )
    _make_gguf_cache(
        str(tmp_path),
        "bartowski/Qwen2.5-7B-Instruct-GGUF",
        filenames=["model-Q4_K_M.gguf", "model-Q8_0.gguf"],
    )
    cat.reconcile_cache({"tmp": str(tmp_path)})
    row = cat.get_model("qw-q4")
    assert row.status == "installed"
    assert row.backend == "llama.cpp"
    assert row.gguf_filename == "model-Q4_K_M.gguf"


def test_reconcile_llamacpp_partial_when_chosen_quant_missing(cat, tmp_path):
    """Two aliases share a repo, each pinning a different quant. The alias
    whose specific GGUF is missing must NOT be promoted to installed even
    though some other GGUF in the snapshot is present."""
    cat._raw_insert_model(
        alias="qw-q4",
        hf_model_id="bartowski/Qwen2.5-7B-Instruct-GGUF",
        storage_location="tmp",
        backend="llama.cpp",
        gguf_filename="model-Q4_K_M.gguf",
        status="partial",
    )
    cat._raw_insert_model(
        alias="qw-q8",
        hf_model_id="bartowski/Qwen2.5-7B-Instruct-GGUF",
        storage_location="tmp",
        backend="llama.cpp",
        gguf_filename="model-Q8_0.gguf",
        status="partial",
    )
    # Only Q8_0 is on disk.
    _make_gguf_cache(
        str(tmp_path),
        "bartowski/Qwen2.5-7B-Instruct-GGUF",
        filenames=["model-Q8_0.gguf"],
    )
    cat.reconcile_cache({"tmp": str(tmp_path)})
    assert cat.get_model("qw-q4").status == "partial"
    assert cat.get_model("qw-q8").status == "installed"


def test_reconcile_llamacpp_sharded_partial_when_one_shard_missing(cat, tmp_path):
    cat._raw_insert_model(
        alias="big-q8",
        hf_model_id="org/big-gguf",
        storage_location="tmp",
        backend="llama.cpp",
        gguf_filename="model-Q8_0-00001-of-00003.gguf",
        status="partial",
    )
    # Only 2 of 3 shards present.
    _make_gguf_cache(
        str(tmp_path),
        "org/big-gguf",
        filenames=[
            "model-Q8_0-00001-of-00003.gguf",
            "model-Q8_0-00002-of-00003.gguf",
        ],
    )
    cat.reconcile_cache({"tmp": str(tmp_path)})
    assert cat.get_model("big-q8").status == "partial"


def test_reconcile_llamacpp_sharded_installed_when_full(cat, tmp_path):
    cat._raw_insert_model(
        alias="big-q8",
        hf_model_id="org/big-gguf",
        storage_location="tmp",
        backend="llama.cpp",
        gguf_filename="model-Q8_0-00001-of-00003.gguf",
        status="partial",
    )
    _make_gguf_cache(
        str(tmp_path),
        "org/big-gguf",
        filenames=[
            "model-Q8_0-00001-of-00003.gguf",
            "model-Q8_0-00002-of-00003.gguf",
            "model-Q8_0-00003-of-00003.gguf",
        ],
    )
    cat.reconcile_cache({"tmp": str(tmp_path)})
    assert cat.get_model("big-q8").status == "installed"


def test_catalog_row_round_trips_backend_and_filename(cat):
    cat._raw_insert_model(
        alias="qw-q4",
        hf_model_id="bartowski/Qwen2.5-7B-Instruct-GGUF",
        storage_location="tmp",
        backend="llama.cpp",
        gguf_filename="model-Q4_K_M.gguf",
    )
    row = cat.get_model("qw-q4")
    assert row.backend == "llama.cpp"
    assert row.gguf_filename == "model-Q4_K_M.gguf"
    api = row.to_api_dict()
    assert api["backend"] == "llama.cpp"
    assert api["gguf_filename"] == "model-Q4_K_M.gguf"
