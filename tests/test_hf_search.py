"""Phase 5 — /manager/hf/search endpoint tests."""
from __future__ import annotations

import pytest
import time
import threading

from huggingface_hub.utils import GatedRepoError, HfHubHTTPError

import hf_search
from tests.fixtures.fake_hf import FakeHfApi, FakeModelInfo, FakeSibling, install_fakes


SUPPORTED = frozenset({"LlamaForCausalLM", "Qwen2ForCausalLM", "Qwen2_5VLForConditionalGeneration"})


@pytest.fixture(autouse=True)
def _install_supported_archs(monkeypatch):
    """Force lifespan's arch loader to return a known supported set so the
    /manager/hf/search response is deterministic. Tests that need a different
    state (e.g. 'empty') call hf_search.set_supported_architectures directly
    after the client is up."""
    monkeypatch.setattr(
        hf_search,
        "load_supported_architectures",
        lambda _path: (SUPPORTED, "vllm-registry"),
    )
    yield
    hf_search.set_supported_architectures(frozenset(), "empty")


def _build_response(monkeypatch, *, configs=None, list_results=None,
                   list_error=None, model_info_map=None,
                   model_info_error=None, config_errors=None):
    api = FakeHfApi(
        list_results=list_results or {},
        list_error=list_error,
        model_info_map=model_info_map,
        model_info_error=model_info_error,
    )
    fake_download = install_fakes(
        monkeypatch, api,
        configs=configs or {},
        config_errors=config_errors or {},
    )
    api.download_calls = fake_download.calls
    return api


# ── happy paths ────────────────────────────────────────────────────────


def test_compatible_row(client, monkeypatch):
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [
            FakeModelInfo("Qwen/Qwen2.5-7B", downloads=12345, likes=50),
        ]},
        configs={"Qwen/Qwen2.5-7B": {"architectures": ["Qwen2ForCausalLM"]}},
    )
    r = client.get("/manager/hf/search?q=qwen2.5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"] == "qwen2.5"
    assert body["limit"] == 20
    assert body["include_vision"] is False
    assert body["vllm_arch_source"] == "vllm-registry"
    assert body["vllm_arch_count"] == len(SUPPORTED)
    assert len(body["results"]) == 1
    row = body["results"][0]
    assert row["model_id"] == "Qwen/Qwen2.5-7B"
    assert row["architectures"] == ["Qwen2ForCausalLM"]
    assert row["is_compatible"] is True
    assert row["compat_reason"] is None
    assert row["downloads"] == 12345
    assert row["likes"] == 50


def test_incompatible_row_unsupported_architecture(client, monkeypatch):
    _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("acme/SomeModel")]},
        configs={"acme/SomeModel": {"architectures": ["AcmeNewArchForCausalLM"]}},
    )
    r = client.get("/manager/hf/search?q=acme")
    body = r.json()
    assert body["results"][0]["is_compatible"] is False
    assert body["results"][0]["compat_reason"] == "unsupported architecture: AcmeNewArchForCausalLM"


def test_missing_config_json_flagged_not_dropped(client, monkeypatch):
    _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y")]},
        configs={},  # no config — fake_download will raise EntryNotFoundError
    )
    r = client.get("/manager/hf/search?q=x")
    body = r.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["is_compatible"] is False
    assert body["results"][0]["compat_reason"] == "missing config.architectures"


def test_per_row_403_flagged_gated(client, monkeypatch):
    _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("gated/repo")]},
        config_errors={"gated/repo": GatedRepoError("nope")},
    )
    r = client.get("/manager/hf/search?q=gated")
    body = r.json()
    assert body["results"][0]["is_compatible"] is False
    assert body["results"][0]["compat_reason"] == "gated or unauthorized"


def test_endpoint_level_401_returns_502_with_token_hint(client, monkeypatch):
    class FakeResponse:
        status_code = 401
        headers: dict[str, str] = {}
        request = None
    err = HfHubHTTPError("unauthorized")
    err.response = FakeResponse()  # avoid HfHubHTTPError's constructor probing response.headers
    _build_response(monkeypatch, list_error=err)
    r = client.get("/manager/hf/search?q=anything")
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "HUGGING_FACE_HUB_TOKEN" in detail


def test_filter_compat_drops_incompatible(client, monkeypatch):
    _build_response(
        monkeypatch,
        list_results={"text-generation": [
            FakeModelInfo("ok/ok"),
            FakeModelInfo("bad/bad"),
        ]},
        configs={
            "ok/ok": {"architectures": ["LlamaForCausalLM"]},
            "bad/bad": {"architectures": ["UnsupportedArchForCausalLM"]},
        },
    )
    r = client.get("/manager/hf/search?q=foo&filter_compat=true")
    body = r.json()
    ids = [row["model_id"] for row in body["results"]]
    assert ids == ["ok/ok"]


def test_include_vision_makes_two_calls(client, monkeypatch):
    api = _build_response(
        monkeypatch,
        list_results={
            "text-generation": [FakeModelInfo("Qwen/Qwen2.5-7B")],
            "image-text-to-text": [FakeModelInfo("Qwen/Qwen2.5-VL-7B")],
        },
        configs={
            "Qwen/Qwen2.5-7B": {"architectures": ["Qwen2ForCausalLM"]},
            "Qwen/Qwen2.5-VL-7B": {"architectures": ["Qwen2_5VLForConditionalGeneration"]},
        },
    )
    r = client.get("/manager/hf/search?q=qwen2.5&include_vision=true")
    assert r.status_code == 200
    pipeline_tags = [c.get("pipeline_tag") for c in api.list_calls]
    assert pipeline_tags == ["text-generation", "image-text-to-text"]
    ids = sorted(row["model_id"] for row in r.json()["results"])
    assert ids == ["Qwen/Qwen2.5-7B", "Qwen/Qwen2.5-VL-7B"]


def test_include_vision_dedupes_by_id(client, monkeypatch):
    """The same model_id under both pipelines should appear once."""
    same = FakeModelInfo("dual/model", downloads=100)
    api = _build_response(
        monkeypatch,
        list_results={
            "text-generation": [same],
            "image-text-to-text": [FakeModelInfo("dual/model", downloads=100)],
        },
        configs={"dual/model": {"architectures": ["LlamaForCausalLM"]}},
    )
    r = client.get("/manager/hf/search?q=dual&include_vision=true")
    body = r.json()
    ids = [row["model_id"] for row in body["results"]]
    assert ids == ["dual/model"]
    assert len(api.list_calls) == 2


def test_include_vision_default_false_makes_one_call(client, monkeypatch):
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y")]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
    )
    client.get("/manager/hf/search?q=x")
    assert len(api.list_calls) == 1
    assert api.list_calls[0]["pipeline_tag"] == "text-generation"


def test_empty_query_returns_400(client):
    # Empty string is rejected by FastAPI's min_length=1 validation as 422.
    # Pass a whitespace-only string to exercise our own 400 path.
    r = client.get("/manager/hf/search?q=%20%20")
    assert r.status_code == 400
    assert "required" in r.json()["detail"]


def test_response_envelope_shape(client, monkeypatch):
    _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y")]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
    )
    body = client.get("/manager/hf/search?q=x").json()
    expected_keys = {
        "query", "limit", "include_vision",
        "vllm_arch_source", "vllm_arch_count", "results",
    }
    assert expected_keys.issubset(body.keys())


def test_size_estimate_from_siblings(client, monkeypatch):
    sib = [
        FakeSibling("model-00001-of-00002.safetensors", size=500_000_000),
        FakeSibling("model-00002-of-00002.safetensors", size=500_000_000),
        FakeSibling("README.md", size=1024),  # ignored
    ]
    info = FakeModelInfo("x/y", siblings=sib)
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [info]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
        model_info_map={"x/y": info},
    )
    body = client.get("/manager/hf/search?q=x").json()
    assert body["results"][0]["size_estimate_gb"] == 1.0
    assert api.model_info_calls[0]["timeout"] == 15


def test_config_cache_key_changes_with_sha(client, monkeypatch):
    api = _build_response(
        monkeypatch,
        list_results={
            "text-generation": [FakeModelInfo("x/y", sha="sha-a")],
        },
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
    )
    first = client.get("/manager/hf/search?q=x").json()["results"][0]
    assert first["is_compatible"] is True
    assert first["architectures"] == ["LlamaForCausalLM"]

    api.list_results["text-generation"] = [FakeModelInfo("x/y", sha="sha-b")]
    hf_search.hf_hub_download.configs["x/y"] = {
        "architectures": ["UnsupportedArchForCausalLM"],
    }
    second = client.get("/manager/hf/search?q=x").json()["results"][0]
    assert second["is_compatible"] is False
    assert second["compat_reason"] == "unsupported architecture: UnsupportedArchForCausalLM"
    assert len(api.download_calls) == 2


def test_unversioned_config_cache_expires_after_ttl(client, monkeypatch):
    monkeypatch.setattr(hf_search, "_CONFIG_CACHE_TTL_SECONDS", -1)
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y")]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
    )
    first = client.get("/manager/hf/search?q=x").json()["results"][0]
    assert first["is_compatible"] is True

    hf_search.hf_hub_download.configs["x/y"] = {
        "architectures": ["UnsupportedArchForCausalLM"],
    }
    second = client.get("/manager/hf/search?q=x").json()["results"][0]
    assert second["is_compatible"] is False
    assert len(api.download_calls) == 2


def test_config_fetch_uses_sha_as_revision(client, monkeypatch):
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y", sha="abc123")]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
    )
    client.get("/manager/hf/search?q=x")
    assert api.download_calls[0]["revision"] == "abc123"


def test_size_estimate_failure_does_not_change_compat(client, monkeypatch):
    """Per-row model_info failure must not flip is_compatible."""
    info = FakeModelInfo("x/y")
    _build_response(
        monkeypatch,
        list_results={"text-generation": [info]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
        model_info_error=RuntimeError("boom"),
    )
    body = client.get("/manager/hf/search?q=x").json()
    row = body["results"][0]
    assert row["is_compatible"] is True
    assert row["compat_reason"] is None
    assert row["size_estimate_gb"] is None


def test_limit_caps_at_50(client, monkeypatch):
    _build_response(monkeypatch, list_results={"text-generation": []}, configs={})
    # FastAPI rejects out-of-range with 422.
    r = client.get("/manager/hf/search?q=x&limit=51")
    assert r.status_code == 422


# ── plane separation ──────────────────────────────────────────────────


def test_inference_plane_returns_404(inference_client):
    r = inference_client.get("/manager/hf/search?q=x")
    assert r.status_code == 404


# ── auth ──────────────────────────────────────────────────────────────


def test_admin_no_auth_returns_401(admin_client_no_auth):
    r = admin_client_no_auth.get("/manager/hf/search?q=x")
    assert r.status_code == 401


# ── empty arch set degrades gracefully ────────────────────────────────


def test_empty_arch_source_flags_all_rows(client, monkeypatch):
    """When the registry import broke and the JSON fallback was empty,
    every row is flagged 'vllm registry unavailable' but still returned."""
    hf_search.set_supported_architectures(frozenset(), "empty")
    _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y")]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
    )
    body = client.get("/manager/hf/search?q=x").json()
    assert body["vllm_arch_source"] == "empty"
    assert body["vllm_arch_count"] == 0
    row = body["results"][0]
    assert row["is_compatible"] is False
    assert row["compat_reason"] == "vllm registry unavailable"


# ── search worker pool lifecycle ─────────────────────────────────────


def test_search_pool_shutdown_cancels_queued_jobs():
    pool = hf_search._DaemonSearchPool(max_workers=2, thread_name_prefix="test-hf")
    release = threading.Event()

    first = pool.submit(release.wait)
    second = pool.submit(release.wait)
    third = pool.submit(lambda: "queued")

    # Give both workers a moment to pick up the blocking jobs so the third
    # future remains queued.
    deadline = time.monotonic() + 1
    while not (first.running() and second.running()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert first.running()
    assert second.running()

    pool.shutdown(cancel_futures=True)
    assert third.cancelled()
    release.set()


@pytest.mark.asyncio
async def test_run_search_lazily_recreates_pool_after_shutdown(monkeypatch):
    hf_search.shutdown_search_pool()
    hf_search.set_supported_architectures(SUPPORTED, "vllm-registry")
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y")]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
    )
    result = await hf_search.run_search(
        q="x",
        limit=20,
        include_vision=False,
        filter_compat=False,
    )
    assert result["results"][0]["model_id"] == "x/y"
    assert len(api.list_calls) == 1
    assert hf_search._search_pool is not None
    hf_search.shutdown_search_pool()
