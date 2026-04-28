"""Phase 4 — restart recovery and reconcile-after-orphan ordering."""
from __future__ import annotations

import os
import time

from catalog import open_catalog


def _seed_storage(tmp_path) -> str:
    storage = os.path.join(str(tmp_path), "storage")
    os.makedirs(storage, exist_ok=True)
    return storage


def _make_snapshot(
    storage: str,
    hf_id: str,
    revision: str = "main",
    sha: str = "a" * 40,
    *,
    with_weights: bool = True,
) -> str:
    safe = "models--" + hf_id.replace("/", "--")
    repo = os.path.join(storage, "hub", safe)
    snap = os.path.join(repo, "snapshots", sha)
    os.makedirs(snap, exist_ok=True)
    if with_weights:
        with open(os.path.join(snap, "model.safetensors"), "wb") as f:
            f.write(b"\x00")
    refs = os.path.join(repo, "refs")
    os.makedirs(refs, exist_ok=True)
    with open(os.path.join(refs, revision), "w") as f:
        f.write(sha)
    return snap


def test_orphan_recovery_marks_partial(tmp_paths, monkeypatch):
    db_path = str(tmp_paths / "mnemosyne.db")
    cat = open_catalog(db_path)
    cat.start_install_tx(
        alias="qw",
        hf_model_id="Qwen/Qwen2.5-7B",
        revision="main",
        gpus="all",
        storage_location="tmp",
    )
    cat.mark_downloading("qw", pid=999, total_bytes=10_000)
    cat.close()

    cat2 = open_catalog(db_path)
    n = cat2.recover_orphan_downloads()
    assert n == 1
    row = cat2.get_model("qw")
    assert row.status == "partial"
    download = cat2.get_download("qw")
    assert download.status == "error"
    assert "interrupted" in (download.error or "")
    cat2.close()


def test_recovery_then_reconcile_promotes_when_snapshot_complete(tmp_paths):
    """After orphan recovery downgrades to partial, reconcile (inside
    apply_config) walks the disk and promotes back to installed if the
    snapshot landed cleanly before the crash. The lifespan ordering
    test in production form (smoke #4)."""
    db_path = str(tmp_paths / "mnemosyne.db")
    storage = _seed_storage(tmp_paths)
    snap = _make_snapshot(storage, "Qwen/Qwen2.5-7B")

    cat = open_catalog(db_path)
    cat.start_install_tx(
        alias="qw",
        hf_model_id="Qwen/Qwen2.5-7B",
        revision="main",
        gpus="all",
        storage_location="store",
    )
    cat.mark_downloading("qw", pid=999, total_bytes=None)
    cat.close()

    cat2 = open_catalog(db_path)
    cat2.recover_orphan_downloads()
    rec = cat2.reconcile_cache({"store": storage})
    assert rec.installed == 1
    row = cat2.get_model("qw")
    assert row.status == "installed"
    assert row.cache_path == snap
    assert row.resolved_sha == "a" * 40
    cat2.close()


def test_reconcile_skips_error_rows(tmp_paths):
    """A hard-failed install must not be silently promoted by a
    half-finished snapshot dir."""
    db_path = str(tmp_paths / "mnemosyne.db")
    storage = _seed_storage(tmp_paths)
    _make_snapshot(storage, "Qwen/Qwen2.5-7B")

    cat = open_catalog(db_path)
    cat.start_install_tx(
        alias="qw",
        hf_model_id="Qwen/Qwen2.5-7B",
        revision="main",
        gpus="all",
        storage_location="store",
    )
    cat.mark_error("qw", "boom")
    rec = cat.reconcile_cache({"store": storage})
    assert rec.installed == 0
    row = cat.get_model("qw")
    assert row.status == "error"
    cat.close()


def test_reconcile_picks_correct_revision_per_row(tmp_paths):
    """Multi-revision repo: each row's refs/<revision> picks its own snapshot."""
    db_path = str(tmp_paths / "mnemosyne.db")
    storage = _seed_storage(tmp_paths)
    _make_snapshot(storage, "Qwen/X", revision="main", sha="a" * 40)
    _make_snapshot(storage, "Qwen/X", revision="dev", sha="b" * 40)

    cat = open_catalog(db_path)
    cat.start_install_tx(
        alias="x-main",
        hf_model_id="Qwen/X",
        revision="main",
        gpus="all",
        storage_location="store",
    )
    cat.start_install_tx(
        alias="x-dev",
        hf_model_id="Qwen/X",
        revision="dev",
        gpus="all",
        storage_location="store",
    )
    cat.reconcile_cache({"store": storage})
    main_row = cat.get_model("x-main")
    dev_row = cat.get_model("x-dev")
    assert main_row.cache_path.endswith("a" * 40)
    assert dev_row.cache_path.endswith("b" * 40)
    cat.close()


def test_reconcile_direct_sha_path_without_refs(tmp_paths):
    """v4 case: 40-char hex SHA with snapshots/<sha>/ but no refs entry."""
    db_path = str(tmp_paths / "mnemosyne.db")
    storage = _seed_storage(tmp_paths)
    sha = "c" * 40
    safe = "models--Qwen--X"
    snap = os.path.join(storage, "hub", safe, "snapshots", sha)
    os.makedirs(snap, exist_ok=True)
    with open(os.path.join(snap, "model.safetensors"), "wb") as f:
        f.write(b"\x00")
    # Note: no refs/<sha> file written.

    cat = open_catalog(db_path)
    cat.start_install_tx(
        alias="x",
        hf_model_id="Qwen/X",
        revision=sha,
        gpus="all",
        storage_location="store",
    )
    rec = cat.reconcile_cache({"store": storage})
    assert rec.installed == 1
    row = cat.get_model("x")
    assert row.status == "installed"
    assert row.cache_path == snap
    cat.close()


def test_reconcile_rejects_path_traversal_revision(tmp_paths):
    """v4 hardening: revisions containing '..' or absolute paths are rejected."""
    db_path = str(tmp_paths / "mnemosyne.db")
    storage = _seed_storage(tmp_paths)
    _make_snapshot(storage, "Qwen/X", revision="main")

    cat = open_catalog(db_path)
    cat.start_install_tx(
        alias="x",
        hf_model_id="Qwen/X",
        revision="../../../etc/passwd",
        gpus="all",
        storage_location="store",
    )
    rec = cat.reconcile_cache({"store": storage})
    assert rec.installed == 0
    row = cat.get_model("x")
    assert row.status == "partial"
    cat.close()


def test_reconcile_missing_storage_clears_stale_resolved_sha(tmp_paths):
    db_path = str(tmp_paths / "mnemosyne.db")
    cat = open_catalog(db_path)
    cat.start_install_tx(
        alias="x",
        hf_model_id="Qwen/X",
        revision="main",
        gpus="all",
        storage_location="missing",
    )
    cat.mark_complete("x", cache_path="/stale", size_bytes=1, resolved_sha="d" * 40)
    rec = cat.reconcile_cache({})
    assert rec.partial == 1
    row = cat.get_model("x")
    assert row.status == "partial"
    assert row.cache_path is None
    assert row.resolved_sha is None
    cat.close()


def test_reconcile_missing_snapshot_clears_stale_resolved_sha(tmp_paths):
    db_path = str(tmp_paths / "mnemosyne.db")
    storage = _seed_storage(tmp_paths)
    cat = open_catalog(db_path)
    cat.start_install_tx(
        alias="x",
        hf_model_id="Qwen/X",
        revision="main",
        gpus="all",
        storage_location="store",
    )
    cat.mark_complete("x", cache_path="/stale", size_bytes=1, resolved_sha="d" * 40)
    rec = cat.reconcile_cache({"store": storage})
    assert rec.partial == 1
    row = cat.get_model("x")
    assert row.status == "partial"
    assert row.cache_path is None
    assert row.resolved_sha is None
    cat.close()


def test_reconcile_replaces_stale_resolved_sha_on_promotion(tmp_paths):
    db_path = str(tmp_paths / "mnemosyne.db")
    storage = _seed_storage(tmp_paths)
    snap = _make_snapshot(storage, "Qwen/X", revision="main", sha="e" * 40)
    cat = open_catalog(db_path)
    cat.start_install_tx(
        alias="x",
        hf_model_id="Qwen/X",
        revision="main",
        gpus="all",
        storage_location="store",
    )
    cat.mark_complete("x", cache_path="/stale", size_bytes=1, resolved_sha="d" * 40)
    rec = cat.reconcile_cache({"store": storage})
    assert rec.installed == 1
    row = cat.get_model("x")
    assert row.status == "installed"
    assert row.cache_path == snap
    assert row.resolved_sha == "e" * 40
    cat.close()
