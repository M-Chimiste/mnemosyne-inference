"""Phase 3 — split inference/admin planes and auth boundaries."""
from __future__ import annotations

import pytest

import vllm_manager
from config import ConfigError, load_config


def test_inference_plane_excludes_manager_routes(inference_client):
    assert inference_client.get("/health").status_code == 200
    assert inference_client.get("/manager/status").status_code == 404
    assert inference_client.post("/manager/load", json={"model": "a-model"}).status_code == 404


def test_admin_plane_requires_basic_for_manager_and_docs(admin_client_no_auth):
    for path in ("/manager/status", "/docs", "/openapi.json", "/redoc"):
        r = admin_client_no_auth.get(path)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Basic"


def test_admin_plane_accepts_correct_basic(client):
    assert client.get("/health").status_code == 200
    assert client.get("/manager/status").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_admin_plane_rejects_wrong_basic(admin_client_no_auth):
    admin_client_no_auth.auth = ("admin", "wrong")
    assert admin_client_no_auth.get("/manager/status").status_code == 401


def test_inference_bearer_is_optional_by_default(inference_client):
    r = inference_client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 503


def test_inference_bearer_required_when_configured(inference_client, monkeypatch):
    monkeypatch.setenv("INFERENCE_API_KEY", "secret-token")
    r = inference_client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 401

    r = inference_client.post(
        "/v1/chat/completions",
        json={"messages": []},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert r.status_code == 503


def test_admin_v1_uses_basic_not_inference_bearer(client, monkeypatch):
    monkeypatch.setenv("INFERENCE_API_KEY", "secret-token")
    r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 503


def test_admin_bind_forces_loopback_when_password_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    assert vllm_manager._resolve_admin_bind("0.0.0.0") == "127.0.0.1"
    assert vllm_manager._resolve_admin_bind("127.0.0.1") == "127.0.0.1"


def test_admin_bind_respects_config_when_password_set(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")
    assert vllm_manager._resolve_admin_bind("0.0.0.0") == "0.0.0.0"


def test_config_rejects_same_inference_and_admin_port(tmp_paths):
    cfg = tmp_paths / "bad-ports.yaml"
    cfg.write_text(
        f"""\
server:
  inference_port: 8000
  admin_port: 8000
storage:
  default: tmp
  locations:
    - name: tmp
      path: {tmp_paths}
"""
    )
    with pytest.raises(ConfigError, match="inference_port and server.admin_port"):
        load_config(str(cfg))


def test_inner_port_clash_guard_rejects_external_port(tmp_config, monkeypatch):
    cfg = load_config(str(tmp_config))
    monkeypatch.setenv("VLLM_INNER_PORT", str(cfg.server.admin_port))
    with pytest.raises(SystemExit, match="VLLM_INNER_PORT"):
        vllm_manager._check_inner_port_clash(cfg)


def test_proxy_strips_auth_and_cookie_before_inner_vllm(rich_client, monkeypatch):
    client, _stub = rich_client
    captured: dict[str, dict[str, str]] = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        async def aread(self) -> bytes:
            return b'{"choices":[{"message":{"content":"ok"}}]}'

        async def aclose(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def build_request(self, method, url, headers, content, params):
            captured["headers"] = dict(headers)
            return object()

        async def send(self, request, stream=True):
            return FakeResponse()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(vllm_manager.httpx, "AsyncClient", FakeAsyncClient)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "a-model", "messages": []},
        headers={"Cookie": "session=do-not-forward"},
    )
    assert r.status_code == 200
    forwarded = {k.lower(): v for k, v in captured["headers"].items()}
    assert "authorization" not in forwarded
    assert "cookie" not in forwarded
