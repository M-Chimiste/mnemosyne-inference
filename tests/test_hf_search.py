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
    r = client.get("/manager/hf/search?q=qwen2.5&pipeline_tags=text-generation")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"] == "qwen2.5"
    assert body["limit"] == 20
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["has_next"] is False
    assert body["next_page"] is None
    assert body["include_vision"] is False
    assert body["pipeline_tags"] == ["text-generation"]
    assert body["sort"] == "trending"
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
    # Default sort = trending; the library accepts the snake_case literal
    # `trending_score` (huggingface_hub 1.x) and descends by default — we
    # never pass a `direction` kwarg because 1.x removed it.
    assert api.list_calls[0]["sort"] == "trending_score"
    assert "direction" not in api.list_calls[0]
    # Pre-filter by `library:transformers` was dropped: it was hiding newer
    # repos that simply don't carry the tag.
    assert "filter" not in api.list_calls[0]


def test_default_sort_is_trending_and_overridable(client, monkeypatch):
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("a/b")]},
        configs={"a/b": {"architectures": ["LlamaForCausalLM"]}},
    )
    client.get("/manager/hf/search?q=&pipeline_tags=text-generation")
    assert api.list_calls[-1]["sort"] == "trending_score"

    client.get("/manager/hf/search?q=&pipeline_tags=text-generation&sort=downloads")
    assert api.list_calls[-1]["sort"] == "downloads"

    client.get("/manager/hf/search?q=&pipeline_tags=text-generation&sort=likes")
    assert api.list_calls[-1]["sort"] == "likes"

    client.get("/manager/hf/search?q=&pipeline_tags=text-generation&sort=recent")
    assert api.list_calls[-1]["sort"] == "last_modified"


def test_default_modalities_query_all_four(client, monkeypatch):
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y")]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
    )
    client.get("/manager/hf/search?q=anything")
    pipeline_tags = [c.get("pipeline_tag") for c in api.list_calls]
    assert pipeline_tags == [
        "text-generation",
        "image-text-to-text",
        "audio-text-to-text",
        "any-to-any",
    ]


def test_pipeline_tags_csv_filters_calls(client, monkeypatch):
    api = _build_response(
        monkeypatch,
        list_results={"any-to-any": [FakeModelInfo("nv/Nemotron-Omni")]},
        configs={"nv/Nemotron-Omni": {"architectures": ["LlamaForCausalLM"]}},
    )
    r = client.get(
        "/manager/hf/search?q=nemotron&pipeline_tags=any-to-any,audio-text-to-text"
    )
    assert r.status_code == 200
    pipeline_tags = [c.get("pipeline_tag") for c in api.list_calls]
    assert pipeline_tags == ["any-to-any", "audio-text-to-text"]


def test_exact_repo_id_lookup_pinned_to_head(client, monkeypatch):
    """A query like `org/repo` triggers a direct model_info lookup, so newer
    repos that don't carry a pipeline_tag still appear at the top."""
    listed = FakeModelInfo("other/related", downloads=999_999)
    exact = FakeModelInfo("poolside/Laguna-XS.2", downloads=12, pipeline_tag=None)
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [listed]},
        configs={
            "poolside/Laguna-XS.2": {"architectures": ["LlamaForCausalLM"]},
            "other/related": {"architectures": ["LlamaForCausalLM"]},
        },
        model_info_map={"poolside/Laguna-XS.2": exact},
    )
    body = client.get(
        "/manager/hf/search?q=poolside/Laguna-XS.2&pipeline_tags=text-generation"
    ).json()
    ids = [row["model_id"] for row in body["results"]]
    assert ids[0] == "poolside/Laguna-XS.2"
    # The direct model_info lookup ran with the trimmed repo id.
    assert any(c["repo_id"] == "poolside/Laguna-XS.2" for c in api.model_info_calls)


def test_incompatible_row_unsupported_architecture(client, monkeypatch):
    _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("acme/SomeModel")]},
        configs={"acme/SomeModel": {"architectures": ["AcmeNewArchForCausalLM"]}},
    )
    r = client.get("/manager/hf/search?q=acme&pipeline_tags=text-generation")
    body = r.json()
    assert body["results"][0]["is_compatible"] is False
    assert body["results"][0]["compat_reason"] == "unsupported architecture: AcmeNewArchForCausalLM"


def test_missing_config_json_flagged_not_dropped(client, monkeypatch):
    _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y")]},
        configs={},  # no config — fake_download will raise EntryNotFoundError
    )
    r = client.get("/manager/hf/search?q=x&pipeline_tags=text-generation")
    body = r.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["is_compatible"] is False
    assert body["results"][0]["compat_reason"] == "missing config.architectures"


def _make_hf_http_error(message: str, status_code: int) -> HfHubHTTPError:
    """Build an HfHubHTTPError without going through the library's stricter
    constructor (which now requires `response`). Covers GatedRepoError too —
    it's a subclass that delegates to the same init."""
    class FakeResponse:
        def __init__(self, code: int):
            self.status_code = code
            self.headers: dict[str, str] = {}
            self.request = None
    err = HfHubHTTPError.__new__(HfHubHTTPError)
    Exception.__init__(err, message)
    err.response = FakeResponse(status_code)
    err.server_message = message
    err.request_id = None
    return err


def test_per_row_403_flagged_gated(client, monkeypatch):
    err = _make_hf_http_error("nope", 403)
    err.__class__ = GatedRepoError
    _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("gated/repo")]},
        config_errors={"gated/repo": err},
    )
    r = client.get("/manager/hf/search?q=gated&pipeline_tags=text-generation")
    body = r.json()
    assert body["results"][0]["is_compatible"] is False
    assert body["results"][0]["compat_reason"] == "gated or unauthorized"


def test_endpoint_level_401_returns_502_with_token_hint(client, monkeypatch):
    err = _make_hf_http_error("unauthorized", 401)
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
    r = client.get(
        "/manager/hf/search?q=foo&filter_compat=true&pipeline_tags=text-generation"
    )
    body = r.json()
    ids = [row["model_id"] for row in body["results"]]
    assert ids == ["ok/ok"]


def test_include_vision_legacy_alias_true_makes_two_calls(client, monkeypatch):
    """`include_vision=true` is honored when `pipeline_tags` is omitted."""
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


def test_include_vision_legacy_alias_false_makes_one_call(client, monkeypatch):
    """`include_vision=false` is the legacy text-only path."""
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y")]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
    )
    client.get("/manager/hf/search?q=x&include_vision=false")
    assert len(api.list_calls) == 1
    assert api.list_calls[0]["pipeline_tag"] == "text-generation"


def test_empty_query_returns_top_models(client, monkeypatch):
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("top/model")]},
        configs={"top/model": {"architectures": ["LlamaForCausalLM"]}},
    )
    r = client.get("/manager/hf/search?q=%20%20&pipeline_tags=text-generation")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == ""
    assert body["results"][0]["model_id"] == "top/model"
    assert "search" not in api.list_calls[0]


def test_response_envelope_shape(client, monkeypatch):
    _build_response(
        monkeypatch,
        list_results={"text-generation": [FakeModelInfo("x/y")]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
    )
    body = client.get("/manager/hf/search?q=x&pipeline_tags=text-generation").json()
    expected_keys = {
        "query", "limit", "page", "page_size", "has_next", "next_page",
        "include_vision", "pipeline_tags", "sort",
        "vllm_arch_source", "vllm_arch_count", "results",
    }
    assert expected_keys.issubset(body.keys())


def test_search_paginates_ranked_results(client, monkeypatch):
    models = [
        FakeModelInfo(f"org/model-{i:02d}", downloads=100 - i)
        for i in range(25)
    ]
    _build_response(
        monkeypatch,
        list_results={"text-generation": models},
        configs={m.id: {"architectures": ["LlamaForCausalLM"]} for m in models},
    )
    body = client.get(
        "/manager/hf/search?q=org&page=2&limit=10&pipeline_tags=text-generation&sort=downloads"
    ).json()
    assert body["page"] == 2
    assert body["page_size"] == 10
    assert body["has_next"] is True
    assert body["next_page"] == 3
    assert [row["model_id"] for row in body["results"]] == [
        f"org/model-{i:02d}" for i in range(10, 20)
    ]


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


def test_gguf_only_repo_is_compatible_via_llama_cpp(client, monkeypatch):
    """A repo with .gguf siblings and no transformer weights is compatible
    via llama.cpp regardless of config.architectures (the file is often
    missing on community GGUF repos)."""
    sib = [
        FakeSibling("model-Q4_K_M.gguf", size=4_000_000_000),
        FakeSibling("model-Q8_0.gguf", size=8_000_000_000),
    ]
    info = FakeModelInfo("bartowski/Qwen2.5-7B-Instruct-GGUF", siblings=sib)
    _build_response(
        monkeypatch,
        list_results={"text-generation": [info]},
        # No config.json — the GGUF override should kick in regardless.
        model_info_map={"bartowski/Qwen2.5-7B-Instruct-GGUF": info},
    )
    body = client.get(
        "/manager/hf/search?q=qwen+gguf&pipeline_tags=text-generation"
    ).json()
    row = body["results"][0]
    assert row["is_compatible"] is True
    assert row["compat_reason"] == "gguf via llama.cpp"
    assert row["has_gguf"] is True
    assert row["recommended_backend"] == "llama.cpp"


def test_mixed_format_repo_keeps_vllm_compat(client, monkeypatch):
    """A repo that ships both .safetensors and .gguf still resolves via vLLM
    when its architecture is supported — only the recommended backend reflects
    the mixed-format nature."""
    sib = [
        FakeSibling("model.safetensors", size=5_000_000_000),
        FakeSibling("model-Q4_K_M.gguf", size=4_000_000_000),
    ]
    info = FakeModelInfo("org/mixed", siblings=sib)
    _build_response(
        monkeypatch,
        list_results={"text-generation": [info]},
        configs={"org/mixed": {"architectures": ["Qwen2ForCausalLM"]}},
        model_info_map={"org/mixed": info},
    )
    body = client.get("/manager/hf/search?q=mixed").json()
    row = body["results"][0]
    # Recommends vLLM because transformer weights are present...
    assert row["recommended_backend"] == "vllm"
    assert row["size_estimate_gb"] == 5.0
    # ...and is still compatible (the GGUF short-circuit fires first when
    # .gguf siblings exist, so reason == "gguf via llama.cpp"; what matters
    # is is_compatible=True).
    assert row["is_compatible"] is True


def test_search_uses_one_model_info_per_row(client, monkeypatch):
    """`_safetensor_total` and the GGUF probe share a single
    model_info(files_metadata=True) call per result — the consolidated
    metadata cache."""
    info = FakeModelInfo(
        "x/y",
        siblings=[FakeSibling("model.safetensors", size=1_000_000_000)],
    )
    api = _build_response(
        monkeypatch,
        list_results={"text-generation": [info]},
        configs={"x/y": {"architectures": ["LlamaForCausalLM"]}},
        model_info_map={"x/y": info},
    )
    client.get("/manager/hf/search?q=x").json()
    # Exactly one model_info call for the row (size + probe in one fetch).
    assert len(api.model_info_calls) == 1


def test_repo_with_no_weights_marks_incompatible(client, monkeypatch):
    info = FakeModelInfo(
        "some/dataset-only",
        siblings=[FakeSibling("README.md", size=200)],
    )
    _build_response(
        monkeypatch,
        list_results={"text-generation": [info]},
        # Missing config.json — fetch_status='missing'.
        model_info_map={"some/dataset-only": info},
    )
    body = client.get("/manager/hf/search?q=dataset").json()
    row = body["results"][0]
    assert row["is_compatible"] is False
    assert row["recommended_backend"] == "none"


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
    assert api.model_info_calls[0]["revision"] == "abc123"


def test_hf_files_uses_requested_revision(client, monkeypatch):
    info = FakeModelInfo(
        "x/y",
        siblings=[FakeSibling("model-Q4_K_M.gguf", size=4_000_000_000)],
    )
    api = _build_response(
        monkeypatch,
        model_info_map={"x/y": info},
    )
    r = client.get("/manager/hf/files?model_id=x/y&revision=dev")
    assert r.status_code == 200, r.text
    assert api.model_info_calls[0]["revision"] == "dev"
    assert r.json()["recommended_backend"] == "llama.cpp"


def test_hf_files_cache_distinguishes_revisions(client, monkeypatch):
    info = FakeModelInfo(
        "x/y",
        siblings=[FakeSibling("model-Q4_K_M.gguf", size=4_000_000_000)],
    )
    api = _build_response(
        monkeypatch,
        model_info_map={"x/y": info},
    )
    client.get("/manager/hf/files?model_id=x/y&revision=dev")
    client.get("/manager/hf/files?model_id=x/y&revision=main")
    revisions = [c["revision"] for c in api.model_info_calls]
    assert revisions == ["dev", "main"]


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
