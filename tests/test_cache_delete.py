"""Phase 4 — cache delete routes."""
from __future__ import annotations

import os
import pytest


def _seed_install(client, alias: str, model: str, *, complete: bool = True):
    """Run /manager/install through the stub_downloader. If complete=True,
    flip the catalog to 'installed' directly."""
    import vllm_manager
    r = client.post("/manager/install", json={"alias": alias, "model": model})
    assert r.status_code == 202
    if complete:
        cat = vllm_manager._catalog
        # Look up the storage path so we can record a real cache_path.
        row = cat.get_model(alias)
        loc = next(
            l for l in vllm_manager._config.storage.locations
            if l.name == row.storage_location
        )
        sha = "a" * 40
        snap = os.path.join(
            loc.path, "hub",
            "models--" + model.replace("/", "--"),
            "snapshots", sha,
        )
        os.makedirs(snap, exist_ok=True)
        with open(os.path.join(snap, "model.safetensors"), "wb") as f:
            f.write(b"\x00")
        cat.mark_complete(alias, cache_path=snap, size_bytes=1, resolved_sha=sha)


@pytest.fixture
def install_client(client, stub_downloader):
    """Client + stubbed downloader for install/delete route tests."""
    return client


def test_delete_install_cache_marks_partial(install_client, tmp_paths):
    _seed_install(install_client, "qw", "Qwen/Qwen2.5-7B")
    repo = os.path.join(str(tmp_paths), "hub", "models--Qwen--Qwen2.5-7B")
    assert os.path.exists(repo)
    r = install_client.delete("/manager/install/qw/cache")
    assert r.status_code == 200
    assert r.json()["status"] == "partial"
    assert not os.path.exists(repo)
    row = install_client.get("/manager/install/qw").json()
    assert row["status"] == "partial"
    assert row["cache_path"] is None


def test_delete_install_full_removes_row(install_client, tmp_paths):
    _seed_install(install_client, "qw", "Qwen/Qwen2.5-7B")
    repo = os.path.join(str(tmp_paths), "hub", "models--Qwen--Qwen2.5-7B")
    r = install_client.delete("/manager/install/qw")
    assert r.status_code == 200
    assert r.json()["status"] == "removed"
    assert not os.path.exists(repo)
    r2 = install_client.get("/manager/install/qw")
    assert r2.status_code == 404


def test_clear_install_download_keeps_installed_row(install_client):
    import vllm_manager
    _seed_install(install_client, "qw", "Qwen/Qwen2.5-7B")
    r = install_client.delete("/manager/install/qw/download")
    assert r.status_code == 200
    assert r.json()["deleted_downloads"] == 1
    assert install_client.get("/manager/install/qw").status_code == 200
    assert vllm_manager._catalog.get_download("qw") is None


def test_clear_install_download_refuses_active(install_client):
    _seed_install(install_client, "qw", "Qwen/Qwen2.5-7B", complete=False)
    r = install_client.delete("/manager/install/qw/download")
    assert r.status_code == 409


def test_delete_install_cache_resident_returns_409(install_client):
    import vllm_manager
    _seed_install(install_client, "qw", "Qwen/Qwen2.5-7B")
    vllm_manager._runtime.resident_alias = "qw"
    try:
        r = install_client.delete("/manager/install/qw/cache")
        assert r.status_code == 409
    finally:
        vllm_manager._runtime.resident_alias = None


def test_delete_install_cache_active_install_returns_409(install_client):
    import vllm_manager
    _seed_install(install_client, "qw", "Qwen/Qwen2.5-7B")
    class H:
        pass
    vllm_manager.downloader._active["qw"] = H()
    try:
        r = install_client.delete("/manager/install/qw/cache")
        assert r.status_code == 409
    finally:
        vllm_manager.downloader._active.pop("qw", None)


def test_delete_install_cache_marks_siblings_partial(install_client, tmp_paths):
    """v3: cache delete on alias A wipes the repo dir AND marks every
    sibling on the same (storage, hf_id) as partial."""
    import vllm_manager
    _seed_install(install_client, "alpha", "Qwen/Shared")
    # Add a sibling row pointing at the same model but a different alias.
    cat = vllm_manager._catalog
    cat._raw_insert_model(
        alias="beta",
        hf_model_id="Qwen/Shared",
        source="ui_install",
        gpus='"all"',
        storage_location="tmp",
        cache_path="/some/snap",
        status="installed",
        revision="dev",
    )
    r = install_client.delete("/manager/install/alpha/cache")
    assert r.status_code == 200
    body = r.json()
    assert "alpha" in body["siblings_marked"]
    assert "beta" in body["siblings_marked"]
    # Both rows are now partial.
    assert install_client.get("/manager/install/alpha").json()["status"] == "partial"
    assert cat.get_model("beta").status == "partial"


def test_delete_cache_legacy_active_download_returns_409(install_client):
    import vllm_manager
    _seed_install(install_client, "qw", "Qwen/Active", complete=False)
    cat = vllm_manager._catalog
    cat.mark_downloading("qw", pid=42, total_bytes=None)
    encoded = "Qwen%2FActive"
    r = install_client.delete(f"/manager/cache/{encoded}")
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["conflict_alias"] == "qw"


def test_force_wipe_refuses_paths_outside_storage_locations(tmp_paths):
    from downloader import CacheWipeError, force_wipe_cache
    bad_path = "/etc"
    with pytest.raises(CacheWipeError):
        force_wipe_cache(bad_path, allowed_roots=[str(tmp_paths)])
    assert os.path.isdir(bad_path)


def test_delete_install_cache_wipe_failure_leaves_catalog_unchanged(
    install_client, monkeypatch,
):
    import vllm_manager

    _seed_install(install_client, "qw", "Qwen/Qwen2.5-7B")

    def fail_wipe(*_args, **_kwargs):
        raise vllm_manager.downloader.CacheWipeError("refused by test")

    monkeypatch.setattr(vllm_manager.downloader, "force_wipe_cache", fail_wipe)
    r = install_client.delete("/manager/install/qw/cache")
    assert r.status_code == 400
    row = install_client.get("/manager/install/qw").json()
    assert row["status"] == "installed"
    assert row["resolved_sha"] == "a" * 40


def test_legacy_cache_delete_bad_storage_aborts_before_mutation(
    install_client, tmp_paths,
):
    import vllm_manager

    _seed_install(install_client, "good", "Qwen/Shared")
    repo = os.path.join(str(tmp_paths), "hub", "models--Qwen--Shared")
    assert os.path.exists(repo)
    cat = vllm_manager._catalog
    cat._raw_insert_model(
        alias="bad",
        hf_model_id="Qwen/Shared",
        source="ui_install",
        gpus='"all"',
        storage_location="missing",
        cache_path="/missing/snap",
        status="installed",
        revision="main",
        resolved_sha="c" * 40,
    )

    r = install_client.delete("/manager/cache/Qwen%2FShared")
    assert r.status_code == 400
    assert os.path.exists(repo)
    assert cat.get_model("good").status == "installed"
    assert cat.get_model("bad").status == "installed"
