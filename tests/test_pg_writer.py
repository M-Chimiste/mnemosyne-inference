"""Unit tests for the PgWriter — no live Postgres required.

A fake psycopg.AsyncConnection records executemany calls so we can assert
the SQL shape, the per-row tuple, and the reconnect-on-OperationalError
behaviour without binding to a database.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402  — installed via requirements-dev.txt
import pg_writer  # noqa: E402


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def executemany(self, sql, params):
        if self.conn.fail_with is not None:
            err = self.conn.fail_with
            self.conn.fail_with = None  # one-shot
            raise err
        self.conn.calls.append((sql, list(params)))


class _FakeConn:
    """Minimal stand-in for psycopg.AsyncConnection.

    Tracks executemany calls in `calls`. Set `fail_with` to make the next
    call raise (use psycopg.OperationalError to exercise the reconnect path).
    """
    def __init__(self) -> None:
        self.calls: list = []
        self.closed = False
        self.fail_with: BaseException | None = None

    def cursor(self):
        return _FakeCursor(self)

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_connector(monkeypatch):
    """Replace AsyncConnection.connect with a factory that hands out
    fresh _FakeConn instances. Returns the list of created conns so tests
    can assert reconnect behaviour."""
    created: list[_FakeConn] = []

    async def _connect(*_args, **_kwargs):
        conn = _FakeConn()
        created.append(conn)
        return conn

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _connect)
    return created


@pytest.fixture
def writer():
    return pg_writer.PgWriter(
        dsn="postgresql://ignored",
        node_id="Mnemosyne",
    )


@pytest.mark.asyncio
async def test_write_batch_uses_expected_sql(writer, fake_connector):
    rows = [
        {
            "id": 1, "event_id": "abc",
            "ts": 1716000000.5,
            "requested_model": "a-model",
            "alias": "a-model",
            "backend": "vllm",
            "endpoint": "/v1/chat/completions",
            "streamed": 0,
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            "response_ms": 42.5, "status_code": 200,
        },
    ]
    sent = await writer.write_batch(rows)
    assert sent == 1
    conn = fake_connector[0]
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "INSERT INTO public.token_usage" in sql
    assert "ON CONFLICT (event_id) DO NOTHING" in sql
    # 10 columns: event_id, timestamp, node_id, model, prompt, completion,
    # total, response_ms, endpoint, status_code.
    assert sql.count("%s") == 10
    assert len(params) == 1
    tup = params[0]
    assert tup[0] == "abc"
    assert tup[1] == datetime.fromtimestamp(1716000000.5, tz=timezone.utc)
    assert tup[2] == "Mnemosyne"
    assert tup[3] == "a-model"
    assert tup[4:7] == (10, 5, 15)
    assert tup[7] == 42.5
    assert tup[8] == "/v1/chat/completions"
    assert tup[9] == 200


@pytest.mark.asyncio
async def test_write_batch_empty_is_noop(writer, fake_connector):
    sent = await writer.write_batch([])
    assert sent == 0
    # Never connected — empty batch short-circuits.
    assert fake_connector == []


@pytest.mark.asyncio
async def test_write_batch_falls_back_to_requested_model_when_alias_missing(
    writer, fake_connector,
):
    rows = [{
        "id": 1, "event_id": "e", "ts": 0.0,
        "requested_model": "org/raw", "alias": None, "backend": "vllm",
        "endpoint": "/v1/x", "streamed": 0,
        "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
        "response_ms": 1.0, "status_code": 200,
    }]
    await writer.write_batch(rows)
    _sql, params = fake_connector[0].calls[0]
    assert params[0][3] == "org/raw"


@pytest.mark.asyncio
async def test_reconnect_on_operational_error(writer, fake_connector):
    """First executemany raises OperationalError → conn is dropped and the
    next call connects fresh."""
    await writer.write_batch([{
        "id": 1, "event_id": "ok-prime", "ts": 0.0,
        "requested_model": "a", "alias": "a", "backend": "vllm",
        "endpoint": "/v1/x", "streamed": 0,
        "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
        "response_ms": 1.0, "status_code": 200,
    }])
    assert len(fake_connector) == 1
    conn1 = fake_connector[0]

    conn1.fail_with = psycopg.OperationalError("connection reset by peer")
    with pytest.raises(psycopg.OperationalError):
        await writer.write_batch([{
            "id": 2, "event_id": "fails", "ts": 0.0,
            "requested_model": "a", "alias": "a", "backend": "vllm",
            "endpoint": "/v1/x", "streamed": 0,
            "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
            "response_ms": 1.0, "status_code": 200,
        }])
    # Connection was reset → original conn closed, writer._conn cleared.
    assert conn1.closed
    assert writer._conn is None

    # Next write reconnects with a fresh fake.
    await writer.write_batch([{
        "id": 3, "event_id": "fresh", "ts": 0.0,
        "requested_model": "a", "alias": "a", "backend": "vllm",
        "endpoint": "/v1/x", "streamed": 0,
        "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
        "response_ms": 1.0, "status_code": 200,
    }])
    assert len(fake_connector) == 2
    assert fake_connector[1].calls  # the fresh conn took the write


@pytest.mark.asyncio
async def test_close_drops_connection(writer, fake_connector):
    await writer.write_batch([{
        "id": 1, "event_id": "e", "ts": 0.0,
        "requested_model": "a", "alias": "a", "backend": "vllm",
        "endpoint": "/v1/x", "streamed": 0,
        "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
        "response_ms": 1.0, "status_code": 200,
    }])
    conn = fake_connector[0]
    assert not conn.closed
    await writer.close()
    assert conn.closed
    assert writer._conn is None


@pytest.mark.asyncio
async def test_accepts_positional_tuple_rows(writer, fake_connector):
    """The writer should accept plain tuples in `Catalog.peek_pg_outbox` order
    too (no sqlite3 binding required)."""
    # (id, event_id, ts, requested_model, alias, backend, endpoint, streamed,
    #  prompt, completion, total, response_ms, status_code)
    row = (
        1, "abc", 1716000000.0, "a-model", "a-model", "vllm",
        "/v1/chat/completions", 0, 3, 2, 5, 12.5, 200,
    )
    await writer.write_batch([row])
    _sql, params = fake_connector[0].calls[0]
    assert params[0][0] == "abc"
    assert params[0][3] == "a-model"
    assert params[0][4:7] == (3, 2, 5)
