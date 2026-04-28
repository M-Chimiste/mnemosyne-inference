"""Phase 4 — legacy /manager/download* shim is now catalog-backed."""
from __future__ import annotations

import os
import time

import pytest

from catalog import synthetic_alias


def test_legacy_download_creates_synthetic_row(client, stub_downloader):
    model_id = "Qwen/Qwen2.5-7B"
    r = client.post("/manager/download", json={
        "model": model_id,
        "revision": "dev",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "started"
    assert body["model"] == model_id

    import vllm_manager
    alias = synthetic_alias(model_id)
    row = vllm_manager._catalog.get_model(alias)
    assert row is not None
    assert row.source == "ui_install"
    assert row.revision == "dev"


def test_legacy_download_default_ignore_patterns(client, stub_downloader):
    """v0 default: skip non-safetensor formats unless caller opts in."""
    client.post("/manager/download", json={"model": "org/x"})
    assert len(stub_downloader.calls) == 1
    assert stub_downloader.calls[0]["ignore_patterns"] == [
        "*.pt", "*.bin", "*.msgpack",
        "flax_model*", "tf_model*", "rust_model*",
    ]


def test_legacy_download_explicit_empty_ignore(client, stub_downloader):
    """Caller can override the default to download every format."""
    client.post("/manager/download", json={
        "model": "org/x",
        "ignore_patterns": [],
    })
    assert stub_downloader.calls[0]["ignore_patterns"] == []


def test_legacy_download_token_isolation(client, stub_downloader, monkeypatch):
    """hf_token in body is threaded into the worker env, not os.environ."""
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    before = os.environ.get("HUGGING_FACE_HUB_TOKEN")
    r = client.post("/manager/download", json={
        "model": "org/y",
        "hf_token": "hf_secret_token",
    })
    assert r.status_code == 200
    after = os.environ.get("HUGGING_FACE_HUB_TOKEN")
    assert before == after  # main process env not mutated.
    assert stub_downloader.calls[0]["hf_token"] == "hf_secret_token"


def test_legacy_status_returns_v0_shape(client, stub_downloader):
    model_id = "org/foo"
    r = client.post("/manager/download", json={"model": model_id})
    assert r.status_code == 200
    encoded = model_id.replace("/", "%2F")
    status = client.get(f"/manager/download/{encoded}").json()
    assert set(status.keys()) >= {
        "model", "status", "started_at", "finished_at", "path", "error", "revision",
    }
    assert status["model"] == model_id
    assert status["status"] in ("queued", "downloading")
    assert status["revision"] == "main"


def test_legacy_status_404_when_no_synthetic_alias(client, rich_config):
    """A config-aliased row for the same HF id must not shadow the
    legacy synthetic-alias status slot."""
    # rich_config seeds two aliases, both with org/ prefixed model ids.
    encoded = "org/a-model".replace("/", "%2F")
    r = client.get(f"/manager/download/{encoded}")
    # No synthetic alias for this model exists yet — must be 404
    # even though there's a config row with the same HF id.
    assert r.status_code == 404


def test_legacy_clear_record_404(client):
    encoded = "org/never-existed".replace("/", "%2F")
    r = client.delete(f"/manager/download/{encoded}")
    assert r.status_code == 404


def test_legacy_clear_record_removes_row(client, stub_downloader):
    model_id = "org/clear-me"
    client.post("/manager/download", json={"model": model_id})
    encoded = model_id.replace("/", "%2F")
    r = client.delete(f"/manager/download/{encoded}")
    assert r.status_code == 200
    assert r.json() == {"cleared": model_id}
