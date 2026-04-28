"""Phase 0 contract snapshot.

These tests pin the current response shapes of the routes that don't need
a running vLLM subprocess. Phase 1+ refactors must not regress them
without explicit intent — if a shape genuinely needs to change, update
the assertion AND the corresponding entry in project_docs/smoke_checks.md.
"""
import vllm_manager


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "model_loaded": False,
        "loading": False,
    }


def test_status_no_model_loaded(client):
    r = client.get("/manager/status")
    assert r.status_code == 200
    body = r.json()
    # Phase 0/1 keys must still be present (plan §8.5: subset, not exact).
    phase_0_keys = {
        "loaded_model",
        "loading",
        "vllm_pid",
        "loaded_at",
        "loaded_at_human",
        "tp_size",
        "gpu_mem_util",
        "inner_endpoint",
    }
    assert phase_0_keys.issubset(body.keys())
    assert body["loaded_model"] is None
    assert body["loading"] is False
    assert body["vllm_pid"] is None
    assert body["loaded_at"] is None
    assert body["loaded_at_human"] is None
    # Phase 2: tp_size and gpu_mem_util reflect the resident profile, so they
    # are None when nothing is loaded (Phase 0 returned the env-driven defaults).
    assert body["tp_size"] is None
    assert body["gpu_mem_util"] is None


def test_aliases_crud_roundtrip(client):
    assert client.get("/manager/aliases").json() == {"aliases": {}}

    r = client.post(
        "/manager/aliases",
        json={"alias": "q72", "model": "Qwen/Qwen2.5-72B-Instruct-AWQ"},
    )
    assert r.status_code == 200
    assert r.json() == {"alias": "q72", "model": "Qwen/Qwen2.5-72B-Instruct-AWQ"}

    assert client.get("/manager/aliases").json() == {
        "aliases": {"q72": "Qwen/Qwen2.5-72B-Instruct-AWQ"}
    }

    r = client.delete("/manager/aliases/q72")
    assert r.status_code == 200
    assert r.json() == {"deleted": "q72"}

    assert client.get("/manager/aliases").json() == {"aliases": {}}


def test_aliases_post_rejects_missing_fields(client):
    r = client.post("/manager/aliases", json={"alias": "incomplete"})
    assert r.status_code == 400


def test_resolve_request_model_legacy_alias_dict(client):
    """Phase 2 tier 3: legacy MODEL_ALIASES still resolves (with WARN logged)."""
    vllm_manager.MODEL_ALIASES["coder"] = "Qwen/Qwen2.5-Coder-7B-Instruct"
    profile = vllm_manager._resolve_request_model("coder")
    assert profile.model == "Qwen/Qwen2.5-Coder-7B-Instruct"


def test_resolve_request_model_raw_passthrough(client):
    """Phase 2 tier 4: org/repo form synthesizes a profile."""
    profile = vllm_manager._resolve_request_model("org/some-model")
    assert profile.model == "org/some-model"


def test_downloads_empty_initially(client):
    r = client.get("/manager/downloads")
    assert r.status_code == 200
    assert r.json() == {"downloads": []}


def test_download_enqueue_returns_current_contract(client, monkeypatch):
    """Phase 4: legacy /manager/download is now catalog-backed.

    Spawning is stubbed so we don't actually run the worker subprocess;
    the route still creates a synthetic-alias row at status='queued'.
    """
    captured: dict = {}

    def fake_start_install(**kwargs):
        captured.update(kwargs)
        # Don't spawn a real worker — leave the catalog row at queued
        # so the assertions below see it.
        class H:
            alias = kwargs["alias"]
        return H()

    monkeypatch.setattr(vllm_manager.downloader, "start_install", fake_start_install)

    model_id = "hf-internal-testing/tiny-random-gpt2"
    r = client.post("/manager/download", json={"model": model_id})

    assert r.status_code == 200
    assert r.json() == {
        "status": "started",
        "model": model_id,
        "poll": "/manager/download/hf-internal-testing%2Ftiny-random-gpt2",
    }

    from catalog import synthetic_alias
    alias = synthetic_alias(model_id)
    row = vllm_manager._catalog.get_model(alias)
    assert row is not None
    assert row.hf_model_id == model_id
    assert row.source == "ui_install"
    assert row.status == "queued"
    assert row.revision == "main"

    # The legacy default ignore_patterns must be passed to the worker.
    assert captured["ignore_patterns"] == [
        "*.pt", "*.bin", "*.msgpack",
        "flax_model*", "tf_model*", "rust_model*",
    ]
