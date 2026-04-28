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


def _make_cache(storage_root, hf_id: str, *, with_weights: bool = True) -> str:
    """Build a fake HF cache dir under <storage_root>/hub/models--<...>/snapshots/<rev>/."""
    safe = "models--" + hf_id.replace("/", "--")
    snap = os.path.join(storage_root, "hub", safe, "snapshots", "abc123")
    os.makedirs(snap, exist_ok=True)
    if with_weights:
        with open(os.path.join(snap, "model.safetensors"), "wb") as f:
            f.write(b"\x00")
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
    # Durable metadata is preserved even on the source flip.
    assert row.last_used_at == 999
    assert row.request_count == 5


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
