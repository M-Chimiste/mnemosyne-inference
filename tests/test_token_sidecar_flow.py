"""End-to-end token-sidecar flow:
  /v1/* hit → in-memory deque → _flush_usage tees to SQLite outbox →
  _pg_flush_once drains outbox → fake PgWriter receives rows.

These tests stub the upstream engine and the postgres writer so they run
without CUDA, vLLM, or a live Postgres instance.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import vllm_manager  # noqa: E402

# Share the fake httpx response/client primitives test_proxy.py defines.
from tests.test_proxy import _FakeClient, _FakeResponse, _patch_upstream  # noqa: E402


SIDECAR_CONFIG_YAML = """\
server:
  idle_unload_seconds: 900
  swap_queue_timeout_seconds: 5

storage:
  default: tmp
  locations:
    - name: tmp
      path: {tmp_path}

defaults:
  gpu_memory_utilization: 0.85
  trust_remote_code: true
  max_model_len: null

models:
  - alias: a-model
    model: org/a-model
    gpus: all

token_sidecar:
  enabled: true
  node_id: TestNode
  flush_interval_seconds: 30
  batch_size: 100
  max_outbox_rows: 100000
"""


class FakeWriter:
    """In-process stand-in for PgWriter. Records all batches the loop
    sends and can be told to raise on the next call to simulate outage."""

    def __init__(self, *, dsn, node_id, connect_timeout, logger):
        self.dsn = dsn
        self.node_id = node_id
        self.batches: list[list] = []
        self.fail_with: BaseException | None = None
        self.closed = False

    async def write_batch(self, rows):
        rows = list(rows)
        if self.fail_with is not None:
            err = self.fail_with
            self.fail_with = None
            raise err
        self.batches.append(rows)
        return len(rows)

    async def close(self):
        self.closed = True


@pytest.fixture
def sidecar_client(tmp_paths, stub_vllm, monkeypatch):
    """TestClient with token_sidecar enabled and PgWriter swapped for FakeWriter.

    Background loops are still off (TestClient runs on a separate event
    loop); tests drive `_pg_flush_once()` directly.
    """
    cfg = tmp_paths / "config.yaml"
    cfg.write_text(SIDECAR_CONFIG_YAML.format(tmp_path=tmp_paths))

    # Force the writer factory to hand back our fake instead of binding
    # to psycopg / nyx.
    import pg_writer
    monkeypatch.setattr(pg_writer, "PgWriter", FakeWriter)

    # DSN just needs to be truthy for the lifespan branch to wire the writer.
    monkeypatch.setenv("TOKEN_SIDECAR_POSTGRES_DSN", "postgresql://fake")

    from tests.conftest import _reset_globals, _running_lifespan
    _reset_globals()
    with _running_lifespan():
        with TestClient(vllm_manager.app) as c:
            c.auth = ("admin", "test-pw")
            yield c, stub_vllm


def _usage_body(prompt=4, completion=3, total=7) -> bytes:
    return json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        },
    }).encode()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_writer_wired_when_sidecar_enabled(sidecar_client):
    _client, _stub = sidecar_client
    # Writer was instantiated at lifespan startup.
    assert isinstance(vllm_manager._pg_writer, FakeWriter)
    assert vllm_manager._pg_writer.node_id == "TestNode"
    # Status surface reflects the wiring.
    r = _client.get("/manager/status")
    assert r.status_code == 200
    s = r.json()["token_sidecar"]
    assert s == {
        "enabled": True,
        "node_id": "TestNode",
        "writer_ready": True,
        "outbox_pending": 0,
        "last_flush_at": None,
        "last_flush_count": 0,
        "last_error": None,
    }


def test_non_streaming_request_round_trips_to_fake_writer(sidecar_client, monkeypatch):
    client, _stub = sidecar_client
    _patch_upstream(monkeypatch, _FakeResponse(body=_usage_body(11, 7, 18)))
    r = client.post(
        "/v1/chat/completions",
        json={"model": "a-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200

    # Deque has the row; outbox doesn't yet.
    assert len(vllm_manager._runtime.usage_rows) == 1
    assert vllm_manager._catalog.count_pg_outbox() == 0

    # Flush deque → SQLite analytics + outbox.
    vllm_manager._flush_usage()
    assert not vllm_manager._runtime.usage_rows
    assert vllm_manager._catalog.count_pg_outbox() == 1

    # Drain outbox → fake postgres.
    _run(vllm_manager._pg_flush_once())
    writer: FakeWriter = vllm_manager._pg_writer
    assert len(writer.batches) == 1
    assert len(writer.batches[0]) == 1
    sent = writer.batches[0][0]
    assert sent["alias"] == "a-model"
    assert sent["prompt_tokens"] == 11
    assert sent["completion_tokens"] == 7
    assert sent["total_tokens"] == 18
    assert sent["endpoint"] == "/v1/chat/completions"
    assert sent["streamed"] == 0
    assert sent["status_code"] == 200
    assert sent["response_ms"] >= 0

    # Outbox is now empty; status reflects last successful flush.
    assert vllm_manager._catalog.count_pg_outbox() == 0
    assert vllm_manager._pg_last_flush_count == 1
    assert vllm_manager._pg_last_error is None


def test_streaming_request_marks_streamed_true(sidecar_client, monkeypatch):
    client, _stub = sidecar_client
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"},"index":0}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n',
        b'data: [DONE]\n\n',
    ]

    async def _open_upstream(_request, _path, _body):
        return _FakeClient(), _FakeResponse(
            content_type="text/event-stream",
            chunks=chunks,
        )

    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "a-model", "stream": True},
    )
    _ = r.content  # drain the stream
    assert r.status_code == 200

    vllm_manager._flush_usage()
    _run(vllm_manager._pg_flush_once())
    writer: FakeWriter = vllm_manager._pg_writer
    sent = writer.batches[0][0]
    assert sent["streamed"] == 1
    assert (sent["prompt_tokens"], sent["completion_tokens"], sent["total_tokens"]) == (4, 2, 6)


def test_outage_keeps_rows_in_outbox(sidecar_client, monkeypatch):
    """Writer raises → outbox is preserved → next flush succeeds."""
    client, _stub = sidecar_client
    _patch_upstream(monkeypatch, _FakeResponse(body=_usage_body()))
    client.post(
        "/v1/chat/completions",
        json={"model": "a-model", "messages": []},
    )
    vllm_manager._flush_usage()
    assert vllm_manager._catalog.count_pg_outbox() == 1

    # Simulate Postgres being unreachable.
    import psycopg
    vllm_manager._pg_writer.fail_with = psycopg.OperationalError("boom")
    with pytest.raises(psycopg.OperationalError):
        _run(vllm_manager._pg_flush_once())

    # Row is still in the outbox; last_error captured.
    assert vllm_manager._catalog.count_pg_outbox() == 1
    assert vllm_manager._pg_last_error is not None
    assert "OperationalError" in vllm_manager._pg_last_error

    # Next tick succeeds and drains.
    _run(vllm_manager._pg_flush_once())
    assert vllm_manager._catalog.count_pg_outbox() == 0
    assert vllm_manager._pg_last_error is None
    writer: FakeWriter = vllm_manager._pg_writer
    assert len(writer.batches) == 1
    assert writer.batches[0][0]["alias"] == "a-model"


def test_prune_runs_when_outbox_over_cap(sidecar_client, monkeypatch):
    """Drop the cap to 3 and pre-seed 10 rows directly; one flush should
    prune to 3 and ship them."""
    _client, _stub = sidecar_client
    # Lower the cap in place — config is loaded; mutate the pydantic field.
    vllm_manager._config.token_sidecar.max_outbox_rows = 3

    for i in range(10):
        vllm_manager._catalog.enqueue_pg_outbox([(
            f"e{i}", float(100 + i), "a", "a", "vllm",
            "/v1/chat/completions", 0,
            1, 1, 2, 1.0, 200,
        )])
    assert vllm_manager._catalog.count_pg_outbox() == 10

    _run(vllm_manager._pg_flush_once())
    # Prune drops 7 oldest, then the remaining 3 are sent and deleted.
    writer: FakeWriter = vllm_manager._pg_writer
    sent_event_ids = [r["event_id"] for r in writer.batches[0]]
    assert sent_event_ids == ["e7", "e8", "e9"]
    assert vllm_manager._catalog.count_pg_outbox() == 0


def test_status_endpoint_reports_pending(sidecar_client, monkeypatch):
    client, _stub = sidecar_client
    _patch_upstream(monkeypatch, _FakeResponse(body=_usage_body()))
    client.post("/v1/chat/completions", json={"model": "a-model", "messages": []})
    vllm_manager._flush_usage()
    r = client.get("/manager/status").json()
    assert r["token_sidecar"]["outbox_pending"] == 1
    assert r["token_sidecar"]["last_flush_count"] == 0  # not flushed yet


def test_disabled_sidecar_does_not_tee_to_outbox(rich_client, monkeypatch):
    """When token_sidecar.enabled=false (default rich_client config),
    flushing leaves the outbox empty even though usage rows ran through."""
    client, _stub = rich_client
    _patch_upstream(monkeypatch, _FakeResponse(body=_usage_body()))
    client.post("/v1/chat/completions", json={"model": "a-model", "messages": []})
    vllm_manager._flush_usage()
    assert vllm_manager._catalog.count_pg_outbox() == 0
    # And the analytics table still got the row.
    n = vllm_manager._catalog._conn.execute(
        "SELECT COUNT(*) AS c FROM request_usage"
    ).fetchone()["c"]
    assert n == 1
