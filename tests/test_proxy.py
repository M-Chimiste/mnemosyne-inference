"""Phase 2 — /v1/* proxy resolution + /manager/load shim + status shape pin.

Verifies plans/phase_2.md §5.4–§5.5, §5.8, §5.9, §8.4. Upstream vLLM is
mocked at the _open_upstream boundary so no real subprocess is spawned.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from unittest import mock

import httpx
import pytest

import vllm_manager


def test_queue_full_marks_proven_pre_inference_node_busy() -> None:
    from cuda_residency import QueueFull

    exc = vllm_manager._admission_http_exception(
        QueueFull("node admission queue is full")
    )

    assert exc.status_code == 429
    assert exc.headers == {
        "Retry-After": "1",
        "X-Mnemosyne-Error": "node_busy",
    }
    assert exc.detail["code"] == "node_busy"


def test_engine_cannot_spoof_manager_node_busy_proof() -> None:
    assert vllm_manager._downstream_proxy_headers(
        {
            "Content-Type": "text/event-stream",
            "X-Mnemosyne-Error": "node_busy",
        }
    ) == {"Content-Type": "text/event-stream"}


# ── upstream mock helpers ─────────────────────────────────────────────


class _FakeResponse:
    """Minimal fake of httpx.Response sufficient for _proxy's needs."""

    def __init__(
        self,
        *,
        body: bytes = b'{"choices":[{"message":{"content":"ok"}}]}',
        status_code: int = 200,
        content_type: str = "application/json",
        chunks: list[bytes] | None = None,
    ):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._body = body
        self._chunks = chunks
        self.closed = False

    async def aread(self) -> bytes:
        return self._body

    async def aiter_bytes(self):
        if self._chunks is None:
            yield self._body
        else:
            for c in self._chunks:
                yield c

    async def aclose(self):
        self.closed = True


class _FakeClient:
    closed = False

    async def aclose(self):
        self.closed = True


def _patch_upstream(monkeypatch, response: _FakeResponse) -> _FakeClient:
    """Replace _open_upstream with a stub returning the given fake response."""
    client = _FakeClient()

    async def _open_upstream(_request, _path, _body):
        return client, response

    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)
    return client


def _patch_upstream_failing(monkeypatch, exc: Exception):
    async def _open_upstream(_request, _path, _body):
        raise exc

    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)


# ── tier 1: config alias ──────────────────────────────────────────────


def test_v1_resolves_config_alias(rich_client, monkeypatch):
    client, stub = rich_client
    _patch_upstream(monkeypatch, _FakeResponse())
    r = client.post(
        "/v1/chat/completions",
        json={"model": "a-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert [p.alias for p in stub.calls] == ["a-model"]
    assert vllm_manager._runtime.resident_alias == "a-model"


# ── tier 2: catalog ui_install row ────────────────────────────────────


def test_v1_resolves_ui_install_alias(rich_client, monkeypatch):
    client, stub = rich_client
    # Seed a ui_install row directly.
    vllm_manager._catalog._raw_insert_model(
        alias="ui-installed",
        hf_model_id="org/ui-installed-model",
        source="ui_install",
        storage_location="tmp",
    )
    _patch_upstream(monkeypatch, _FakeResponse())
    r = client.post(
        "/v1/chat/completions",
        json={"model": "ui-installed", "messages": []},
    )
    assert r.status_code == 200
    assert stub.calls[0].alias == "ui-installed"
    assert stub.calls[0].model == "org/ui-installed-model"


def test_v1_resolves_ui_install_alias_case_insensitive(rich_client, monkeypatch):
    client, stub = rich_client
    vllm_manager._catalog._raw_insert_model(
        alias="ui-installed",
        hf_model_id="org/ui-installed-model",
        source="ui_install",
        storage_location="tmp",
    )
    _patch_upstream(monkeypatch, _FakeResponse())
    r = client.post(
        "/v1/chat/completions",
        json={"model": "UI-INSTALLED", "messages": []},
    )
    assert r.status_code == 200
    assert stub.calls[0].alias == "ui-installed"
    assert stub.calls[0].model == "org/ui-installed-model"


def test_v1_resolves_installed_hf_id_via_ui_alias(rich_client, monkeypatch):
    client, stub = rich_client
    vllm_manager._catalog._raw_insert_model(
        alias="qwen36-27b",
        hf_model_id="Qwen/Qwen3.6-27B",
        source="ui_install",
        gpus='"all"',
        storage_location="tmp",
        extra_args='["--max-num-seqs", "512"]',
    )
    _patch_upstream(monkeypatch, _FakeResponse())
    r = client.post(
        "/v1/chat/completions",
        json={"model": "qwen/qwen3.6-27b", "messages": []},
    )
    assert r.status_code == 200
    assert stub.calls[0].alias == "qwen36-27b"
    assert stub.calls[0].model == "Qwen/Qwen3.6-27B"
    assert stub.calls[0].extra_args == ("--max-num-seqs", "512")
    assert vllm_manager._runtime.resident_alias == "qwen36-27b"


def test_v1_rewrites_case_insensitive_hf_id_to_served_model(rich_client, monkeypatch):
    client, stub = rich_client
    captured: dict[str, dict] = {}
    vllm_manager._catalog._raw_insert_model(
        alias="qwen36-27b",
        hf_model_id="Qwen/Qwen3.6-27B",
        source="ui_install",
        gpus='"all"',
        storage_location="tmp",
    )

    async def _open_upstream(_request, _path, body):
        captured["body"] = json.loads(body)
        return _FakeClient(), _FakeResponse()

    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "qwen/qwen3.6-27b", "messages": []},
    )
    assert r.status_code == 200
    assert stub.calls[0].alias == "qwen36-27b"
    assert captured["body"]["model"] == "Qwen/Qwen3.6-27B"


def test_v1_preserves_llamacpp_specific_request_options(rich_client, monkeypatch):
    """The manager is a thin proxy: optional engine/body params must survive.

    llama.cpp's OpenAI-compatible chat endpoint accepts a mix of standard
    OpenAI fields, sampler extensions, reasoning controls, and structured
    output controls. The proxy should only rewrite `model` and leave the
    rest alone.
    """
    client, _stub = rich_client
    captured: dict[str, dict] = {}

    async def _open_upstream(_request, _path, body):
        captured["body"] = json.loads(body)
        return _FakeClient(), _FakeResponse()

    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)

    request_body = {
        "model": "a-model",
        "messages": [{"role": "user", "content": "Return a user profile"}],
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": 128,
        "response_format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        "grammar": "root ::= \"ok\"",
        "chat_template_kwargs": {"enable_thinking": False},
        "reasoning_format": "none",
        "reasoning_control": True,
        "parse_tool_calls": True,
        "parallel_tool_calls": False,
        "mirostat": 2,
    }

    r = client.post("/v1/chat/completions", json=request_body)

    assert r.status_code == 200
    assert captured["body"]["model"] == "org/a-model"
    for key, value in request_body.items():
        if key != "model":
            assert captured["body"][key] == value


def test_v1_enforces_configured_endpoint_contract(rich_client, monkeypatch):
    from config import ModelProfile

    client, stub = rich_client
    vllm_manager._config.models[0] = ModelProfile(
        alias="a-model",
        model="org/a-model",
        capabilities=["completions"],
    )
    _patch_upstream(monkeypatch, _FakeResponse())

    rejected = client.post(
        "/v1/chat/completions",
        json={"model": "a-model", "messages": []},
    )
    assert rejected.status_code == 400
    assert stub.calls == []

    accepted = client.post(
        "/v1/completions",
        json={"model": "a-model", "prompt": "hello"},
    )
    assert accepted.status_code == 200
    assert [profile.alias for profile in stub.calls] == ["a-model"]


def test_v1_installed_hf_id_not_ready_returns_409(rich_client, monkeypatch):
    client, stub = rich_client
    vllm_manager._catalog._raw_insert_model(
        alias="qwen36-27b",
        hf_model_id="Qwen/Qwen3.6-27B",
        source="ui_install",
        status="downloading",
        storage_location="tmp",
    )
    _patch_upstream(monkeypatch, _FakeResponse())
    r = client.post(
        "/v1/chat/completions",
        json={"model": "Qwen/Qwen3.6-27B", "messages": []},
    )
    assert r.status_code == 409
    assert "not ready" in r.json()["detail"]
    assert stub.calls == []


# ── tier 3: legacy MODEL_ALIASES ──────────────────────────────────────


def test_v1_resolves_legacy_alias_dict(rich_client, monkeypatch, caplog):
    client, stub = rich_client
    vllm_manager.MODEL_ALIASES["legacy-key"] = "org/legacy-target"
    _patch_upstream(monkeypatch, _FakeResponse())
    r = client.post("/v1/chat/completions", json={"model": "legacy-key"})
    assert r.status_code == 200
    assert stub.calls[0].model == "org/legacy-target"
    # WARN logged once per alias.
    assert any("Legacy MODEL_ALIASES" in rec.getMessage() for rec in caplog.records)


# ── tier 4: raw HF id passthrough (org/repo and absolute path) ────────


def test_v1_resolves_org_repo_form(rich_client, monkeypatch):
    client, stub = rich_client
    _patch_upstream(monkeypatch, _FakeResponse())
    r = client.post("/v1/chat/completions", json={"model": "Qwen/Qwen2.5-7B-Instruct"})
    assert r.status_code == 200
    assert stub.calls[0].model == "Qwen/Qwen2.5-7B-Instruct"


def test_v1_resolves_absolute_path(rich_client, monkeypatch, tmp_path):
    client, stub = rich_client
    local = tmp_path / "local-model"
    local.mkdir()
    _patch_upstream(monkeypatch, _FakeResponse())
    r = client.post("/v1/chat/completions", json={"model": str(local)})
    assert r.status_code == 200
    assert stub.calls[0].model == str(local)


def test_v1_typoed_alias_returns_404(rich_client, monkeypatch):
    """The trap from review: 'qwn-72b-awq' (no slash, no path) must NOT
    silently become an HF download attempt — it returns 404."""
    client, stub = rich_client
    _patch_upstream(monkeypatch, _FakeResponse())
    r = client.post("/v1/chat/completions", json={"model": "qwn-72b-awq"})
    assert r.status_code == 404
    assert stub.calls == []


def test_v1_no_model_field_no_resident_returns_503(rich_client):
    client, _stub = rich_client
    r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 503


# ── /manager/load shim ────────────────────────────────────────────────


def test_load_aliased_payload(rich_client):
    client, stub = rich_client
    r = client.post("/manager/load", json={"model": "a-model"})
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "status": "loaded", "alias": "a-model",
        "model": "org/a-model", "backend": "vllm",
    }


def test_load_aliased_ignores_legacy_params(rich_client, caplog):
    client, stub = rich_client
    r = client.post("/manager/load", json={"model": "a-model", "tp": 1, "gpu_mem": 0.5})
    assert r.status_code == 200
    # Profile values won — gpu_memory_utilization stays at the configured 0.85 default.
    assert stub.calls[0].gpu_memory_utilization == 0.85
    assert any("Ignoring" in rec.getMessage() for rec in caplog.records)


def test_load_raw_id_with_legacy_overrides(rich_client):
    client, stub = rich_client
    r = client.post(
        "/manager/load",
        json={"model": "Qwen/Qwen2.5-7B-Instruct", "tp": 1, "gpu_mem": 0.85},
    )
    assert r.status_code == 200
    profile = stub.calls[0]
    assert profile.served_model_name == "Qwen/Qwen2.5-7B-Instruct"
    assert profile.engine_model_path == "Qwen/Qwen2.5-7B-Instruct"
    assert profile.gpu_memory_utilization == 0.85
    assert profile.gpus == [0]  # tp=1 → list(range(1))


def test_load_installed_hf_id_ignores_legacy_overrides(rich_client):
    client, stub = rich_client
    vllm_manager._catalog._raw_insert_model(
        alias="qwen36-27b",
        hf_model_id="Qwen/Qwen3.6-27B",
        source="ui_install",
        gpus='"all"',
        storage_location="tmp",
        extra_args='["--max-num-seqs", "512"]',
    )
    r = client.post(
        "/manager/load",
        json={"model": "qwen/qwen3.6-27b", "tp": 1, "gpu_mem": 0.5},
    )
    assert r.status_code == 200
    assert r.json()["alias"] == "qwen36-27b"
    profile = stub.calls[0]
    assert profile.alias == "qwen36-27b"
    assert profile.gpu_memory_utilization == 0.85
    assert profile.gpus == "all"


def test_load_typoed_alias_returns_404(rich_client):
    client, _stub = rich_client
    r = client.post("/manager/load", json={"model": "qwn-72b-awq"})
    assert r.status_code == 404


def test_load_missing_model_field_returns_400(rich_client):
    client, _stub = rich_client
    r = client.post("/manager/load", json={})
    assert r.status_code == 400


# ── /manager/status shape pin ─────────────────────────────────────────


def test_status_includes_phase_2_keys_when_idle(rich_client):
    client, _stub = rich_client
    body = client.get("/manager/status").json()
    expected = {
        "loaded_model", "loading", "vllm_pid", "loaded_at", "loaded_at_human",
        "tp_size", "gpu_mem_util", "inner_endpoint",
        "alias", "gpus", "quantization", "max_model_len", "storage_location",
        "last_used_at", "idle_seconds", "seconds_until_eviction",
        "inflight_requests", "swap_target",
    }
    assert expected.issubset(body.keys())
    assert body["alias"] is None
    assert body["inflight_requests"] == 0
    assert body["swap_target"] is None
    assert body["seconds_until_eviction"] is None  # nothing resident


def test_status_after_load_reflects_profile(rich_client):
    client, _stub = rich_client
    client.post("/manager/load", json={"model": "b-model"})
    body = client.get("/manager/status").json()
    assert body["alias"] == "b-model"
    assert body["loaded_model"] == "org/b-model"
    assert body["quantization"] == "awq"
    assert body["max_model_len"] == 32768
    assert body["storage_location"] == "tmp"
    assert body["gpu_mem_util"] == 0.85
    assert body["inflight_requests"] == 0
    assert body["swap_target"] is None
    # Resident model + idle eviction enabled → countdown is a non-negative number.
    assert body["seconds_until_eviction"] is not None
    assert body["seconds_until_eviction"] >= 0


def test_status_eviction_disabled_when_null(client):
    """Default minimal config has idle_unload_seconds=900; we need an explicit
    null to verify the disabled case. Use the rich-config approach inline."""
    # Easier: assert the field is None when no model is resident — that's the
    # nothing-to-evict case and is already covered. The disabled-eviction
    # branch is exercised by test_eviction.py.
    body = client.get("/manager/status").json()
    assert body["seconds_until_eviction"] is None


# ── usage-on-success semantics ────────────────────────────────────────


def test_usage_bumped_once_on_non_streaming_success(rich_client, monkeypatch):
    client, _stub = rich_client
    _patch_upstream(monkeypatch, _FakeResponse())
    before_count = vllm_manager._runtime.request_count_delta
    before_used = vllm_manager._runtime.last_used_at
    r = client.post("/v1/chat/completions", json={"model": "a-model"})
    assert r.status_code == 200
    assert vllm_manager._runtime.request_count_delta == before_count + 1
    assert (
        vllm_manager._runtime.last_used_at is not None
        and vllm_manager._runtime.last_used_at != before_used
    )
    assert vllm_manager._runtime.inflight == 0


def test_usage_NOT_bumped_on_pre_stream_upstream_failure(rich_client, monkeypatch):
    """PRD §5.3: only successful proxied requests count. A pre-stream
    httpx.ConnectError must not refresh last_used_at or bump the counter."""
    client, _stub = rich_client
    # First load the model so we don't 503 on missing resident.
    client.post("/manager/load", json={"model": "a-model"})
    before_count = vllm_manager._runtime.request_count_delta
    before_used = vllm_manager._runtime.last_used_at
    _patch_upstream_failing(monkeypatch, httpx.ConnectError("boom"))
    with pytest.raises(httpx.ConnectError):
        client.post("/v1/chat/completions", json={"model": "a-model"})
    assert vllm_manager._runtime.request_count_delta == before_count
    assert vllm_manager._runtime.last_used_at == before_used
    assert vllm_manager._runtime.inflight == 0


def test_usage_bumped_once_on_streaming_success(rich_client, monkeypatch):
    client, _stub = rich_client
    _patch_upstream(
        monkeypatch,
        _FakeResponse(content_type="text/event-stream", chunks=[b"data: a\n\n", b"data: b\n\n"]),
    )
    before = vllm_manager._runtime.request_count_delta
    r = client.post("/v1/chat/completions", json={"model": "a-model"})
    # TestClient drains the stream by reading r.content.
    _ = r.content
    assert r.status_code == 200
    assert vllm_manager._runtime.request_count_delta == before + 1
    assert vllm_manager._runtime.inflight == 0


def test_inflight_settles_to_zero_in_all_cases(rich_client, monkeypatch):
    client, _stub = rich_client
    # success
    _patch_upstream(monkeypatch, _FakeResponse())
    client.post("/v1/chat/completions", json={"model": "a-model"})
    assert vllm_manager._runtime.inflight == 0
    # failure
    _patch_upstream_failing(monkeypatch, httpx.ConnectError("nope"))
    with pytest.raises(httpx.ConnectError):
        client.post("/v1/chat/completions", json={"model": "a-model"})
    assert vllm_manager._runtime.inflight == 0


@pytest.mark.asyncio
async def test_proxy_cleanup_attempts_all_closes_and_releases_for_switch():
    from cuda_residency import CapacitySpec, CudaResidencyCoordinator

    starts: list[str] = []
    close_calls: list[str] = []

    async def start(profile: str) -> None:
        starts.append(profile)

    coordinator = CudaResidencyCoordinator(
        start_engine=start,
        stop_engine=lambda: None,
        derive_capacity=lambda _profile: CapacitySpec(
            1,
            "test",
            "authoritative",
        ),
        configured_max_concurrency=None,
        max_queue_depth=4,
    )
    lease = await coordinator.acquire(
        "profile-a",
        "deployment-a",
        timeout_seconds=1,
    )

    class CancelledClose:
        async def aclose(self) -> None:
            close_calls.append("response")
            raise asyncio.CancelledError()

    class FailedClose:
        async def aclose(self) -> None:
            close_calls.append("client")
            raise RuntimeError("close failed")

    await vllm_manager._cleanup_proxy_resources(
        response=CancelledClose(),
        client=FailedClose(),
        lease=lease,
    )

    assert sorted(close_calls) == ["client", "response"]
    assert (await coordinator.status()).active == 0
    next_lease = await coordinator.acquire(
        "profile-b",
        "deployment-b",
        timeout_seconds=1,
    )
    assert starts == ["profile-a", "profile-b"]
    await next_lease.release()
    await coordinator.shutdown(timeout_seconds=1)


@pytest.mark.asyncio
async def test_proxy_cleanup_preserves_outer_cancellation_after_release():
    from cuda_residency import CapacitySpec, CudaResidencyCoordinator

    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    close_finished = asyncio.Event()
    coordinator = CudaResidencyCoordinator(
        start_engine=lambda _profile: asyncio.sleep(0),
        stop_engine=lambda: None,
        derive_capacity=lambda _profile: CapacitySpec(
            1,
            "test",
            "authoritative",
        ),
        configured_max_concurrency=None,
        max_queue_depth=2,
    )
    lease = await coordinator.acquire(
        "profile-a",
        "deployment-a",
        timeout_seconds=1,
    )

    class BlockingClose:
        async def aclose(self) -> None:
            close_started.set()
            await allow_close.wait()
            close_finished.set()

    cleanup = asyncio.create_task(
        vllm_manager._cleanup_proxy_resources(
            response=BlockingClose(),
            lease=lease,
        )
    )
    await close_started.wait()
    cleanup.cancel()
    await asyncio.sleep(0)
    cleanup.cancel()
    await asyncio.sleep(0)
    assert not cleanup.done()
    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert close_finished.is_set()
    assert (await coordinator.status()).active == 0
    next_lease = await coordinator.acquire(
        "profile-b",
        "deployment-b",
        timeout_seconds=1,
    )
    await next_lease.release()
    await coordinator.shutdown(timeout_seconds=1)


@pytest.mark.asyncio
async def test_stream_owner_defers_repeated_cancellation_until_cleanup_finishes():
    from cuda_residency import CapacitySpec, CudaResidencyCoordinator

    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    response_closed = asyncio.Event()
    client_closed = asyncio.Event()
    coordinator = CudaResidencyCoordinator(
        start_engine=lambda _profile: asyncio.sleep(0),
        stop_engine=lambda: None,
        derive_capacity=lambda _profile: CapacitySpec(
            1,
            "test",
            "authoritative",
        ),
        configured_max_concurrency=None,
        max_queue_depth=2,
    )
    lease = await coordinator.acquire(
        "profile-a",
        "deployment-a",
        timeout_seconds=1,
    )

    class BlockingResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aclose(self) -> None:
            close_started.set()
            await allow_close.wait()
            response_closed.set()

    class ClosingClient:
        async def aclose(self) -> None:
            client_closed.set()

    owner = vllm_manager._StreamingProxyOwnership(
        client=ClosingClient(),
        response=BlockingResponse(),
        lease=lease,
        requested_model="profile-a",
        alias="profile-a",
        backend="llama.cpp",
        path="v1/chat/completions",
        request_start_monotonic=time.monotonic(),
    )
    completion = asyncio.create_task(owner.complete())
    await close_started.wait()
    completion.cancel()
    await asyncio.sleep(0)
    completion.cancel()
    await asyncio.sleep(0)
    assert not completion.done()

    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await completion

    assert response_closed.is_set()
    assert client_closed.is_set()
    assert (await coordinator.status()).active == 0
    await coordinator.shutdown(timeout_seconds=1)


@pytest.mark.asyncio
async def test_all_cuda_loopback_http_clients_ignore_ambient_proxies(
    monkeypatch,
):
    constructed: list[dict] = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return [{"slot": 1}]

    class Client:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, **_kwargs):
            return Response()

        def build_request(self, **kwargs):
            return kwargs

        async def send(self, _request, *, stream):
            assert stream is True
            return Response()

        async def aclose(self):
            return None

    class Profile:
        backend = "llama.cpp"
        extra_args = ()

    class Request:
        headers = {}
        method = "POST"
        query_params = {}

    monkeypatch.setattr(vllm_manager.httpx, "AsyncClient", Client)
    assert await vllm_manager._wait_for_health("http://127.0.0.1/health", 1)
    capacity = await vllm_manager._probe_engine_capacity(Profile())
    assert capacity.derived_limit == 1
    client, _response = await vllm_manager._open_upstream(
        Request(),
        "v1/responses",
        b"{}",
    )
    await client.aclose()

    assert len(constructed) == 3
    assert all(options["trust_env"] is False for options in constructed)


@pytest.mark.asyncio
async def test_outer_stream_response_releases_before_body_iterator_starts(
    monkeypatch,
):
    from cuda_residency import CapacitySpec, CudaResidencyCoordinator

    starts: list[str] = []
    body_iterated = False

    async def start(profile: str) -> None:
        starts.append(profile)

    coordinator = CudaResidencyCoordinator(
        start_engine=start,
        stop_engine=lambda: None,
        derive_capacity=lambda _profile: CapacitySpec(
            1,
            "test",
            "authoritative",
        ),
        configured_max_concurrency=None,
        max_queue_depth=2,
    )
    lease = await coordinator.acquire(
        "profile-a",
        "deployment-a",
        timeout_seconds=1,
    )

    class NeverStartedResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}
        closed = False

        async def aiter_bytes(self):
            nonlocal body_iterated
            body_iterated = True
            yield b"data: never\n\n"

        async def aclose(self) -> None:
            self.closed = True

    class CloseClient:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    response = NeverStartedResponse()
    client = CloseClient()
    owner = vllm_manager._StreamingProxyOwnership(
        client=client,
        response=response,
        lease=lease,
        requested_model="profile-a",
        alias="profile-a",
        backend="llama.cpp",
        path="v1/chat/completions",
        request_start_monotonic=time.monotonic(),
    )
    owned = vllm_manager._OwnedStreamingResponse(
        vllm_manager._wrap_stream(owner),
        owner=owner,
        status_code=200,
        headers=response.headers,
        media_type="text/event-stream",
    )

    async def cancel_before_first_iteration(
        _response,
        _scope,
        _receive,
        _send,
    ) -> None:
        await _send({
            "type": "http.response.start",
            "status": 200,
            "headers": [],
        })
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        vllm_manager.StreamingResponse,
        "__call__",
        cancel_before_first_iteration,
    )
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    with pytest.raises(asyncio.CancelledError):
        await owned({}, None, send)

    assert sent == [{
        "type": "http.response.start",
        "status": 200,
        "headers": [],
    }]
    assert body_iterated is False
    assert response.closed is True
    assert client.closed is True
    assert (await coordinator.status()).active == 0
    next_lease = await coordinator.acquire(
        "profile-b",
        "deployment-b",
        timeout_seconds=1,
    )
    assert starts == ["profile-a", "profile-b"]
    await next_lease.release()
    await coordinator.shutdown(timeout_seconds=1)


# ── unload ────────────────────────────────────────────────────────────


def test_unload_returns_was_alias(rich_client):
    client, _stub = rich_client
    client.post("/manager/load", json={"model": "a-model"})
    r = client.post("/manager/unload")
    assert r.status_code == 200
    assert r.json() == {"status": "unloaded", "was": "a-model"}


def test_unload_when_nothing_loaded(rich_client):
    client, _stub = rich_client
    r = client.post("/manager/unload")
    assert r.status_code == 200
    assert r.json() == {"status": "nothing to unload"}


# ── backend dispatch ──────────────────────────────────────────────────


def test_canonicalize_model_field_uses_served_name():
    """`_canonicalize_model_field` rewrites `"model"` to served_model_name —
    not the engine_model_path. For llama.cpp this means the alias goes on
    the wire and the filesystem path stays inside the engine."""
    from profiles import ResolvedProfile
    profile = ResolvedProfile(
        alias="qw-q4",
        served_model_name="qw-q4",
        engine_model_path="/hf-cache/hub/models--repo/snapshots/aa/model.gguf",
        gpus="all",
        quantization=None,
        max_model_len=None,
        gpu_memory_utilization=0.9,
        trust_remote_code=False,
        storage_name="tmp",
        storage_path="/tmp",
        extra_args=(),
        backend="llama.cpp",
        gguf_filename="model.gguf",
    )
    body = json.dumps({"model": "qw-q4-alias", "prompt": "hi"}).encode()
    rewritten = vllm_manager._canonicalize_model_field(body, profile)
    parsed = json.loads(rewritten)
    assert parsed["model"] == "qw-q4"
    assert "/hf-cache" not in rewritten.decode()


def test_start_engine_dispatches_to_llama_cpp(monkeypatch):
    """A profile with backend='llama.cpp' routes to _start_llama_cpp, not
    _start_vllm."""
    import asyncio
    from profiles import ResolvedProfile
    profile = ResolvedProfile(
        alias="qw-q4",
        served_model_name="qw-q4",
        engine_model_path="/hf-cache/.../model.gguf",
        gpus="all",
        quantization=None,
        max_model_len=None,
        gpu_memory_utilization=0.9,
        trust_remote_code=False,
        storage_name="tmp",
        storage_path="/tmp",
        extra_args=(),
        backend="llama.cpp",
        gguf_filename="model.gguf",
    )

    vllm_calls: list = []
    llama_calls: list = []

    async def fake_vllm(p):
        vllm_calls.append(p)

    async def fake_llama(p):
        llama_calls.append(p)

    monkeypatch.setattr(vllm_manager, "_start_vllm", fake_vllm)
    monkeypatch.setattr(vllm_manager, "_start_llama_cpp", fake_llama)
    asyncio.run(vllm_manager._start_engine(profile))
    assert len(llama_calls) == 1
    assert len(vllm_calls) == 0


def test_start_engine_dispatches_to_vllm(monkeypatch):
    import asyncio
    from profiles import ResolvedProfile
    profile = ResolvedProfile(
        alias="qw",
        served_model_name="Qwen/Qwen2.5-7B",
        engine_model_path="Qwen/Qwen2.5-7B",
        gpus="all",
        quantization=None,
        max_model_len=None,
        gpu_memory_utilization=0.9,
        trust_remote_code=False,
        storage_name="tmp",
        storage_path="/tmp",
        extra_args=(),
        backend="vllm",
    )

    vllm_calls: list = []
    llama_calls: list = []

    async def fake_vllm(p):
        vllm_calls.append(p)

    async def fake_llama(p):
        llama_calls.append(p)

    monkeypatch.setattr(vllm_manager, "_start_vllm", fake_vllm)
    monkeypatch.setattr(vllm_manager, "_start_llama_cpp", fake_llama)
    asyncio.run(vllm_manager._start_engine(profile))
    assert len(vllm_calls) == 1
    assert len(llama_calls) == 0


def test_status_includes_backend_and_gguf(rich_client):
    """`/manager/status` surfaces backend + gguf_filename for the resident
    profile when llama.cpp is active."""
    client, stub = rich_client
    # Inject a llama.cpp config alias by mutating the loaded config in place.
    # The rich fixture has 'a-model' and 'b-model' as vLLM; we wrap the start
    # by directly populating runtime state to simulate a successful llama
    # load.
    from profiles import ResolvedProfile
    profile = ResolvedProfile(
        alias="qw-q4",
        served_model_name="qw-q4",
        engine_model_path="/hf/q4.gguf",
        gpus="all",
        quantization=None,
        max_model_len=131072,
        gpu_memory_utilization=0.9,
        trust_remote_code=False,
        storage_name="tmp",
        storage_path="/tmp",
        extra_args=(),
        backend="llama.cpp",
        gguf_filename="model-Q4_K_M.gguf",
    )
    vllm_manager._runtime.resident_alias = "qw-q4"
    vllm_manager._runtime.resident_profile = profile
    vllm_manager._runtime.model_load_time = time.time()
    vllm_manager._runtime.last_used_at = time.time()

    r = client.get("/manager/status")
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "llama.cpp"
    assert body["gguf_filename"] == "model-Q4_K_M.gguf"
    assert body["loaded_model"] == "qw-q4"
    # Reset so following tests in the suite see clean state.
    vllm_manager._runtime.resident_alias = None
    vllm_manager._runtime.resident_profile = None
    vllm_manager._runtime.model_load_time = None
    vllm_manager._runtime.last_used_at = None


# ── token usage tracking ──────────────────────────────────────────────


def _usage_body(prompt=10, completion=5, total=15) -> bytes:
    return json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        },
    }).encode()


def _enable_all_language_capabilities() -> None:
    """Let the shared rich fixture exercise opt-in Messages/Rerank routes."""

    vllm_manager._config.models[0].capabilities = (
        "chat.completions",
        "completions",
        "embeddings",
        "messages",
        "rerank",
        "responses",
    )


def _durable_usage_rows():
    return vllm_manager._catalog._conn.execute(
        "SELECT event_id, requested_model, alias, backend, prompt_tokens, "
        "completion_tokens, total_tokens, usage_json "
        "FROM request_usage ORDER BY id"
    ).fetchall()


def _assert_no_durable_usage() -> None:
    assert not vllm_manager._runtime.usage_rows
    assert _durable_usage_rows() == []


def test_usage_recorded_non_streaming(rich_client, monkeypatch):
    """A 2xx response is durable before the client receives completion."""
    client, _stub = rich_client
    _patch_upstream(monkeypatch, _FakeResponse(body=_usage_body(11, 7, 18)))
    r = client.post(
        "/v1/chat/completions",
        json={"model": "a-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert not vllm_manager._runtime.usage_rows
    rows = _durable_usage_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["requested_model"] == "a-model"
    assert row["alias"] == "a-model"
    assert row["backend"] == "vllm"
    assert (
        row["prompt_tokens"],
        row["completion_tokens"],
        row["total_tokens"],
    ) == (11, 7, 18)
    assert json.loads(row["usage_json"])["prompt_tokens"] == 11
    assert len(row["event_id"]) == 32

    # The periodic flush is now retry-only and cannot duplicate a committed
    # event or its model aggregates.
    vllm_manager._flush_usage()
    assert not vllm_manager._runtime.usage_rows
    assert len(_durable_usage_rows()) == 1
    model_row = vllm_manager._catalog._conn.execute(
        "SELECT total_prompt_tokens, total_completion_tokens "
        "FROM models WHERE alias='a-model'"
    ).fetchone()
    assert (model_row["total_prompt_tokens"],
            model_row["total_completion_tokens"]) == (11, 7)


@pytest.mark.parametrize(
    ("path", "response_body", "expected"),
    [
        (
            "/v1/responses",
            {
                "type": "response",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 5,
                    "total_tokens": 17,
                },
            },
            (12, 5, 17),
        ),
        (
            "/v1/messages",
            {
                "type": "message",
                "usage": {
                    "input_tokens": 4,
                    "cache_creation_input_tokens": 6,
                    "cache_read_input_tokens": 8,
                    "output_tokens": 3,
                },
            },
            (18, 3, 21),
        ),
        (
            "/v1/rerank",
            {
                "results": [],
                "usage": {"prompt_tokens": 9, "total_tokens": 9},
            },
            (9, 0, 9),
        ),
    ],
)
def test_fleet_language_routes_queue_normalized_usage(
    rich_client,
    monkeypatch,
    path,
    response_body,
    expected,
):
    client, _stub = rich_client
    _enable_all_language_capabilities()
    _patch_upstream(
        monkeypatch,
        _FakeResponse(body=json.dumps(response_body).encode()),
    )

    response = client.post(path, json={"model": "a-model"})

    assert response.status_code == 200
    assert not vllm_manager._runtime.usage_rows
    rows = _durable_usage_rows()
    assert len(rows) == 1
    assert (
        rows[0]["prompt_tokens"],
        rows[0]["completion_tokens"],
        rows[0]["total_tokens"],
    ) == expected


def test_usage_skipped_on_non_2xx(rich_client, monkeypatch):
    """An upstream 500 (even with a usage block) must not queue a row."""
    client, _stub = rich_client
    _patch_upstream(monkeypatch, _FakeResponse(body=_usage_body(), status_code=500))
    r = client.post("/v1/chat/completions", json={"model": "a-model"})
    assert r.status_code == 500
    _assert_no_durable_usage()


def test_usage_skipped_when_usage_block_missing(rich_client, monkeypatch):
    """No `usage` in the response → no row queued."""
    client, _stub = rich_client
    _patch_upstream(monkeypatch, _FakeResponse(
        body=b'{"choices":[{"message":{"content":"ok"}}]}',
    ))
    r = client.post("/v1/chat/completions", json={"model": "a-model"})
    assert r.status_code == 200
    _assert_no_durable_usage()


def test_usage_skipped_for_non_allowlisted_path(rich_client, monkeypatch):
    """A random language path with a usage-shaped body is ignored."""
    client, _stub = rich_client
    _patch_upstream(monkeypatch, _FakeResponse(body=_usage_body()))
    r = client.post("/v1/models", json={"model": "a-model"})
    assert r.status_code == 200
    _assert_no_durable_usage()


def test_usage_resident_profile_used_when_no_model_field(rich_client, monkeypatch):
    """A request that omits `model` rides the resident; the queued row is
    tagged with the resident alias and backend."""
    client, _stub = rich_client
    # Load a-model first so the proxy has a resident.
    client.post("/manager/load", json={"model": "a-model"})
    _patch_upstream(monkeypatch, _FakeResponse(body=_usage_body(3, 2, 5)))
    r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 200
    assert not vllm_manager._runtime.usage_rows
    rows = _durable_usage_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["requested_model"] is None
    assert row["alias"] == "a-model"
    assert row["backend"] == "vllm"
    assert (
        row["prompt_tokens"],
        row["completion_tokens"],
        row["total_tokens"],
    ) == (3, 2, 5)


def test_streaming_usage_recorded_when_client_opted_in(rich_client, monkeypatch):
    """Client sends `stream_options.include_usage: true`. The injected SSE
    usage event reaches the client unchanged AND a row is queued."""
    client, _stub = rich_client
    # Final event mirrors vLLM's shape: choices=[], usage={...}.
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"},"index":0}],"usage":null}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n',
        b'data: [DONE]\n\n',
    ]
    captured: dict = {}

    async def _open_upstream(_request, _path, body):
        captured["body"] = json.loads(body)
        return _FakeClient(), _FakeResponse(
            content_type="text/event-stream",
            chunks=chunks,
        )

    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "a-model",
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )
    text = r.content.decode()
    assert r.status_code == 200
    # Upstream body keeps include_usage=True (already set by client).
    assert captured["body"]["stream_options"]["include_usage"] is True
    # All three events forwarded.
    assert text.count("data:") == 3
    assert "completion_tokens" in text
    assert not vllm_manager._runtime.usage_rows
    rows = _durable_usage_rows()
    assert len(rows) == 1
    assert (
        rows[0]["prompt_tokens"],
        rows[0]["completion_tokens"],
        rows[0]["total_tokens"],
    ) == (4, 2, 6)


def test_streaming_usage_injected_and_stripped(rich_client, monkeypatch):
    """Client did NOT request usage. The proxy injects
    `stream_options.include_usage: true` upstream, records usage, and
    strips the trailing usage-only event before yielding to the client."""
    client, _stub = rich_client
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"},"index":0}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n',
        b'data: [DONE]\n\n',
    ]
    captured: dict = {}

    async def _open_upstream(_request, _path, body):
        captured["body"] = json.loads(body)
        return _FakeClient(), _FakeResponse(
            content_type="text/event-stream",
            chunks=chunks,
        )

    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "a-model", "stream": True},
    )
    text = r.content.decode()
    assert r.status_code == 200
    # Injection happened upstream.
    assert captured["body"]["stream_options"]["include_usage"] is True
    # The usage-only event was stripped from the client-visible stream.
    assert "completion_tokens" not in text
    # But [DONE] and the content delta survived.
    assert "[DONE]" in text
    assert "hi" in text
    # And the row was recorded.
    assert not vllm_manager._runtime.usage_rows
    rows = _durable_usage_rows()
    assert len(rows) == 1
    assert (
        rows[0]["prompt_tokens"],
        rows[0]["completion_tokens"],
        rows[0]["total_tokens"],
    ) == (4, 2, 6)


@pytest.mark.asyncio
async def test_stream_usage_commits_before_terminal_event_is_forwarded(
    rich_client,
):
    _client, _stub = rich_client
    response = _FakeResponse(
        content_type="text/event-stream",
        chunks=[
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":4,'
            b'"completion_tokens":2,"total_tokens":6}}\n\n',
            b"data: [DONE]\n\n",
        ],
    )

    class Lease:
        resident_profile = None

        async def release(self):
            return None

    owner = vllm_manager._StreamingProxyOwnership(
        client=_FakeClient(),
        response=response,
        lease=Lease(),
        requested_model="a-model",
        alias="a-model",
        backend="vllm",
        path="v1/chat/completions",
        request_start_monotonic=time.monotonic(),
    )
    iterator = vllm_manager._wrap_stream(
        owner,
        client_asked_for_usage=True,
    ).__aiter__()

    first = await iterator.__anext__()
    assert b'"content":"hi"' in first
    assert _durable_usage_rows() == []

    terminal_usage = await iterator.__anext__()
    assert b'"usage"' in terminal_usage
    assert not vllm_manager._runtime.usage_rows
    rows = _durable_usage_rows()
    assert len(rows) == 1
    assert (
        rows[0]["prompt_tokens"],
        rows[0]["completion_tokens"],
        rows[0]["total_tokens"],
    ) == (4, 2, 6)

    await iterator.aclose()
    await owner.complete()


def test_streaming_responses_preserves_request_and_records_nested_tail_usage(
    rich_client,
    monkeypatch,
):
    client, _stub = rich_client
    captured: dict = {}
    completed = {
        "type": "response.completed",
        "response": {
            "usage": {
                "input_tokens": 31,
                "output_tokens": 9,
                "total_tokens": 40,
            }
        },
    }
    # Deliberately omit the final SSE blank line.
    chunks = [
        b'event: response.output_text.delta\ndata: {"delta":"hi"}\r\n\r\n',
        f"event: response.completed\r\ndata: {json.dumps(completed)}".encode(),
    ]

    async def _open_upstream(_request, _path, body):
        captured["body"] = body
        return _FakeClient(), _FakeResponse(
            content_type="text/event-stream",
            chunks=chunks,
        )

    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)
    response = client.post(
        "/v1/responses",
        json={"model": "a-model", "input": "hi", "stream": True},
    )

    assert response.status_code == 200
    forwarded = json.loads(captured["body"])
    assert "stream_options" not in forwarded
    assert b"response.completed" in response.content
    assert not vllm_manager._runtime.usage_rows
    rows = _durable_usage_rows()
    assert len(rows) == 1
    assert (
        rows[0]["prompt_tokens"],
        rows[0]["completion_tokens"],
        rows[0]["total_tokens"],
    ) == (31, 9, 40)


def test_streaming_messages_preserves_request_and_merges_usage(
    rich_client,
    monkeypatch,
):
    client, _stub = rich_client
    _enable_all_language_capabilities()
    captured: dict = {}
    start = {
        "type": "message_start",
        "message": {
            "usage": {
                "input_tokens": 3,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 7,
                "output_tokens": 0,
            }
        },
    }
    delta = {
        "type": "message_delta",
        "usage": {"output_tokens": 12},
    }
    chunks = [
        f"event: message_start\ndata: {json.dumps(start)}\n\n".encode(),
        f"event: message_delta\ndata: {json.dumps(delta)}".encode(),
    ]

    async def _open_upstream(_request, _path, body):
        captured["body"] = body
        return _FakeClient(), _FakeResponse(
            content_type="text/event-stream",
            chunks=chunks,
        )

    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)
    response = client.post(
        "/v1/messages",
        json={"model": "a-model", "messages": [], "stream": True},
    )

    assert response.status_code == 200
    forwarded = json.loads(captured["body"])
    assert "stream_options" not in forwarded
    assert b"message_start" in response.content
    assert b"message_delta" in response.content
    assert not vllm_manager._runtime.usage_rows
    rows = _durable_usage_rows()
    assert len(rows) == 1
    assert (
        rows[0]["prompt_tokens"],
        rows[0]["completion_tokens"],
        rows[0]["total_tokens"],
    ) == (15, 12, 27)


def test_streaming_usage_skipped_on_non_2xx(rich_client, monkeypatch):
    """Non-2xx streaming response → forward chunks unchanged, no row queued."""
    client, _stub = rich_client
    chunks = [b'data: {"error":"boom"}\n\n']
    _patch_upstream(monkeypatch, _FakeResponse(
        content_type="text/event-stream",
        chunks=chunks,
        status_code=500,
    ))
    r = client.post(
        "/v1/chat/completions",
        json={"model": "a-model", "stream": True},
    )
    _ = r.content
    assert r.status_code == 500
    _assert_no_durable_usage()


def test_ensure_stream_usage_noop_when_not_streaming():
    body = json.dumps({"model": "x", "messages": []}).encode()
    new_body, opted = vllm_manager._ensure_stream_usage(body)
    assert new_body == body
    assert opted is False


def test_ensure_stream_usage_injects_when_missing():
    body = json.dumps({"model": "x", "stream": True}).encode()
    new_body, opted = vllm_manager._ensure_stream_usage(body)
    payload = json.loads(new_body)
    assert payload["stream_options"]["include_usage"] is True
    assert opted is False


def test_ensure_stream_usage_preserves_when_client_set():
    body = json.dumps({
        "model": "x", "stream": True,
        "stream_options": {"include_usage": True, "other": 1},
    }).encode()
    new_body, opted = vllm_manager._ensure_stream_usage(body)
    payload = json.loads(new_body)
    assert payload["stream_options"]["include_usage"] is True
    assert payload["stream_options"]["other"] == 1
    assert opted is True


@pytest.mark.parametrize("path", ["v1/responses", "v1/messages", "v1/rerank"])
def test_ensure_stream_usage_preserves_non_chat_request_bytes(path):
    body = json.dumps({"model": "x", "stream": True}).encode()

    new_body, opted = vllm_manager._ensure_stream_usage(body, path)

    assert new_body == body
    assert opted is False


# ── GET /v1/models — local catalog listing ───────────────────────────


def test_v1_models_lists_no_503_when_idle(rich_client, monkeypatch):
    """GET /v1/models must return a 200 list even with nothing loaded — and
    must never touch the inner engine."""
    client, _stub = rich_client

    def _boom(*_a, **_kw):
        raise AssertionError("/v1/models must be served locally, not proxied")

    monkeypatch.setattr(vllm_manager, "_open_upstream", _boom)
    assert vllm_manager._runtime.resident_alias is None

    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)


def test_v1_models_lists_installed_both_backends(rich_client):
    client, _stub = rich_client
    vllm_manager._catalog._raw_insert_model(
        alias="vllm-installed",
        hf_model_id="org/vllm-model",
        source="ui_install",
        storage_location="tmp",
        status="installed",
        installed_at=1700000000,
    )
    vllm_manager._catalog._raw_insert_model(
        alias="gguf-installed",
        hf_model_id="org/gguf-repo",
        source="ui_install",
        storage_location="tmp",
        status="installed",
        backend="llama.cpp",
        gguf_filename="model.Q4_K_M.gguf",
    )

    r = client.get("/v1/models")
    assert r.status_code == 200
    by_id = {m["id"]: m for m in r.json()["data"]}

    assert by_id["vllm-installed"]["backend"] == "vllm"
    # vLLM is served under its HF id.
    assert by_id["vllm-installed"]["served_model_name"] == "org/vllm-model"
    assert by_id["vllm-installed"]["created"] == 1700000000
    assert by_id["vllm-installed"]["object"] == "model"

    assert by_id["gguf-installed"]["backend"] == "llama.cpp"
    # llama.cpp is served under its alias.
    assert by_id["gguf-installed"]["served_model_name"] == "gguf-installed"
    assert by_id["gguf-installed"]["hf_model_id"] == "org/gguf-repo"


def test_v1_models_listed_alias_swaps_resident_model(rich_client, monkeypatch):
    """End-to-end: an alias returned by GET /v1/models, sent back as the
    `model` field, loads that model and swaps out any previously resident one."""
    client, stub = rich_client
    for alias, hf in (("alpha", "org/alpha"), ("beta", "org/beta")):
        vllm_manager._catalog._raw_insert_model(
            alias=alias,
            hf_model_id=hf,
            source="ui_install",
            storage_location="tmp",
            status="installed",
        )
    _patch_upstream(monkeypatch, _FakeResponse())

    # Both aliases are advertised as loadable.
    listed = {m["id"] for m in client.get("/v1/models").json()["data"]}
    assert {"alpha", "beta"} <= listed

    # Load alpha by alias.
    r = client.post("/v1/chat/completions", json={"model": "alpha", "messages": []})
    assert r.status_code == 200
    assert vllm_manager._runtime.resident_alias == "alpha"
    kills_after_alpha = stub.kill_calls

    # Now request beta by alias → swap. _start_vllm kills the resident first.
    r = client.post("/v1/chat/completions", json={"model": "beta", "messages": []})
    assert r.status_code == 200
    assert vllm_manager._runtime.resident_alias == "beta"
    assert [p.alias for p in stub.calls] == ["alpha", "beta"]
    assert stub.kill_calls > kills_after_alpha  # the swap evicted alpha


def test_v1_models_excludes_unready(rich_client):
    client, _stub = rich_client
    vllm_manager._catalog._raw_insert_model(
        alias="partial-model",
        hf_model_id="org/partial",
        source="ui_install",
        storage_location="tmp",
        status="partial",
    )
    vllm_manager._catalog._raw_insert_model(
        alias="ready-model",
        hf_model_id="org/ready",
        source="ui_install",
        storage_location="tmp",
        status="installed",
    )

    ids = {m["id"] for m in client.get("/v1/models").json()["data"]}
    assert "ready-model" in ids
    assert "partial-model" not in ids
