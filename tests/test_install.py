"""Phase 4 — /manager/install routes."""
from __future__ import annotations

import os
import time

import pytest


def _wait_for_status(client, alias: str, status: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/manager/install/{alias}")
        if r.status_code == 200 and r.json().get("status") == status:
            return r.json()
        time.sleep(0.02)
    raise AssertionError(f"alias '{alias}' did not reach status='{status}'")


# ── happy path ──────────────────────────────────────────────────────


def test_install_creates_queued_row(client, stub_downloader):
    r = client.post("/manager/install", json={
        "alias": "qw",
        "model": "Qwen/Qwen2.5-7B",
        "revision": "main",
    })
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["alias"] == "qw"
    assert body["status"] == "queued"
    assert body["poll"] == "/manager/install/qw"

    # Catalog row appears at status='queued'.
    status = client.get("/manager/install/qw").json()
    assert status["status"] == "queued"
    assert status["hf_model_id"] == "Qwen/Qwen2.5-7B"
    assert status["revision"] == "main"
    assert status["source"] == "ui_install"

    assert len(stub_downloader.calls) == 1
    call = stub_downloader.calls[0]
    assert call["alias"] == "qw"
    assert call["model_id"] == "Qwen/Qwen2.5-7B"
    assert call["revision"] == "main"


def test_install_revision_propagates_end_to_end(client, stub_downloader):
    r = client.post("/manager/install", json={
        "alias": "qw-dev",
        "model": "Qwen/Qwen2.5-7B",
        "revision": "dev",
    })
    assert r.status_code == 202
    assert stub_downloader.calls[0]["revision"] == "dev"
    row = client.get("/manager/install/qw-dev").json()
    assert row["revision"] == "dev"


def test_install_resolved_sha_recorded_on_complete(client, stub_downloader):
    stub_downloader.auto_complete = True
    r = client.post("/manager/install", json={
        "alias": "qw",
        "model": "Qwen/Qwen2.5-7B",
    })
    assert r.status_code == 202
    body = client.get("/manager/install/qw").json()
    assert body["status"] == "installed"
    assert body["resolved_sha"] == "b" * 40


# ── conflicts ───────────────────────────────────────────────────────


def test_install_alias_in_config_returns_409(rich_client):
    """Aliases declared in config.yaml win — install must refuse."""
    client, _stub = rich_client
    r = client.post("/manager/install", json={
        "alias": "a-model",
        "model": "org/somewhere-else",
    })
    assert r.status_code == 409


def test_install_rejects_reserved_synthetic_alias(client, stub_downloader):
    r = client.post("/manager/install", json={
        "alias": "__cache__:abcdef0123456789",
        "model": "Qwen/Qwen2.5-7B",
    })
    assert r.status_code == 400
    assert not stub_downloader.calls


def test_install_resident_alias_returns_409(client, stub_downloader, monkeypatch):
    """Cannot install an alias whose weights are mmap'd into vLLM."""
    import vllm_manager
    vllm_manager._runtime.resident_alias = "qw"
    r = client.post("/manager/install", json={
        "alias": "qw",
        "model": "Qwen/Qwen2.5-7B",
    })
    assert r.status_code == 409
    vllm_manager._runtime.resident_alias = None


def test_install_active_install_returns_409(client, stub_downloader):
    """Second install for the same alias while one is active → 409."""
    import vllm_manager
    # First install — leaves the catalog in queued state.
    r = client.post("/manager/install", json={
        "alias": "qw",
        "model": "Qwen/Qwen2.5-7B",
    })
    assert r.status_code == 202
    # Manually mark it active in the downloader registry.
    class FakeHandle:
        pass
    vllm_manager.downloader._active["qw"] = FakeHandle()
    try:
        r2 = client.post("/manager/install", json={
            "alias": "qw",
            "model": "Qwen/Qwen2.5-7B",
        })
        assert r2.status_code == 409
    finally:
        vllm_manager.downloader._active.pop("qw", None)


def test_install_cross_revision_dedup_409(client, stub_downloader):
    """Same (storage, hf_model_id) pair, different alias and revision —
    still 409. v4: HF repo cache is shared across revisions."""
    import vllm_manager
    # Seed the catalog with a queued+downloading row for foo.
    cat = vllm_manager._catalog
    cat.start_install_tx(
        alias="foo",
        hf_model_id="Qwen/Qwen2.5-7B",
        revision="main",
        gpus="all",
        storage_location="tmp",
    )
    cat.mark_downloading("foo", pid=12345, total_bytes=None)
    r = client.post("/manager/install", json={
        "alias": "bar",
        "model": "Qwen/Qwen2.5-7B",
        "revision": "dev",
    })
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["conflict_alias"] == "foo"


def test_install_bad_storage_returns_400(client, stub_downloader):
    r = client.post("/manager/install", json={
        "alias": "qw",
        "model": "Qwen/Qwen2.5-7B",
        "storage": "nonexistent",
    })
    assert r.status_code == 400


def test_install_size_estimate_warns_when_absent(client, stub_downloader, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        r = client.post("/manager/install", json={
            "alias": "qw",
            "model": "Qwen/Qwen2.5-7B",
        })
    assert r.status_code == 202
    assert any("size_estimate_gb not provided" in m for m in caplog.messages)


def test_install_size_estimate_too_large_returns_400(client, stub_downloader):
    r = client.post("/manager/install", json={
        "alias": "qw",
        "model": "Qwen/Qwen2.5-7B",
        "size_estimate_gb": 1e9,  # 1 exabyte; nothing has that
    })
    assert r.status_code == 400


# ── cancel / retry ──────────────────────────────────────────────────


def test_install_cancel_404_when_inactive(client):
    r = client.post("/manager/install/missing/cancel")
    assert r.status_code == 404


def test_install_retry_404_for_missing_row(client):
    r = client.post("/manager/install/missing/retry")
    assert r.status_code == 404


def test_install_retry_recreates_install(client, stub_downloader):
    """After a failed install, retry re-spawns with the same params."""
    import vllm_manager
    cat = vllm_manager._catalog
    # Seed an errored install row.
    cat.start_install_tx(
        alias="qw",
        hf_model_id="Qwen/Qwen2.5-7B",
        revision="main",
        gpus="all",
        storage_location="tmp",
    )
    cat.mark_error("qw", "fake error")
    r = client.post("/manager/install/qw/retry")
    assert r.status_code == 202
    # Should have spawned again.
    assert any(c["alias"] == "qw" for c in stub_downloader.calls)


def test_install_retry_force_wipes_cache(client, stub_downloader, tmp_paths):
    """retry?force=true wipes the repo cache dir first."""
    import vllm_manager
    cat = vllm_manager._catalog
    cat.start_install_tx(
        alias="qw",
        hf_model_id="Qwen/Qwen2.5-7B",
        revision="main",
        gpus="all",
        storage_location="tmp",
    )
    cat.mark_partial("qw")
    # Pre-populate a fake repo cache dir.
    repo_dir = os.path.join(
        str(tmp_paths), "hub", "models--Qwen--Qwen2.5-7B"
    )
    os.makedirs(repo_dir, exist_ok=True)
    with open(os.path.join(repo_dir, "marker"), "w") as f:
        f.write("hi")
    assert os.path.exists(os.path.join(repo_dir, "marker"))
    r = client.post("/manager/install/qw/retry?force=true")
    assert r.status_code == 202
    assert not os.path.exists(repo_dir)
