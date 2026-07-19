from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from mnemosyne_macos.usage import NormalizedUsage, UsageEvent
from mnemosyne_macos.usage_store import PersistResult, UsageStore


def _event(
    event_id: str,
    *,
    timestamp: float = 100.0,
    prompt: int = 10,
    completion: int = 5,
    engine: str = "omlx",
    alias: str = "frontier",
    streamed: bool = False,
) -> UsageEvent:
    return UsageEvent(
        usage=NormalizedUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            raw={
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        ),
        endpoint="v1/chat/completions",
        engine=engine,
        requested_model="requested/model",
        alias=alias,
        streamed=streamed,
        response_ms=42.5,
        status_code=200,
        event_id=event_id,
        timestamp=timestamp,
    )


@pytest.fixture
def store(tmp_path) -> UsageStore:
    usage_store = UsageStore.open(tmp_path / "usage.db")
    try:
        yield usage_store
    finally:
        usage_store.close()


def test_record_batch_atomically_writes_analytics_and_outbox(store: UsageStore) -> None:
    result = store.record_batch([_event("event-1")], enqueue_outbox=True)

    assert result == PersistResult(analytics_inserted=1, outbox_inserted=1)
    analytics = store.list_request_usage(limit=10)
    assert len(analytics) == 1
    assert analytics[0]["event_id"] == "event-1"
    assert analytics[0]["backend"] == "omlx"
    assert analytics[0]["endpoint"] == "/v1/chat/completions"
    assert json.loads(analytics[0]["usage_json"])["total_tokens"] == 15

    outbox = store.peek_outbox(limit=10)
    assert len(outbox) == 1
    assert outbox[0]["event_id"] == "event-1"
    assert outbox[0]["prompt_tokens"] == 10
    assert outbox[0]["completion_tokens"] == 5


def test_outbox_row_shape_matches_existing_pg_writer_contract(store: UsageStore) -> None:
    store.record_batch([_event("event-1", streamed=True)], enqueue_outbox=True)
    row = store.peek_pg_outbox(1)[0]

    assert list(row.keys()) == [
        "id",
        "event_id",
        "ts",
        "requested_model",
        "alias",
        "backend",
        "endpoint",
        "streamed",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "response_ms",
        "status_code",
    ]
    assert row["streamed"] == 1


def test_sidecar_disabled_still_records_local_analytics(store: UsageStore) -> None:
    result = store.record_batch([_event("local-only")], enqueue_outbox=False)

    assert result == PersistResult(analytics_inserted=1, outbox_inserted=0)
    assert store.count_request_usage() == 1
    assert store.count_outbox() == 0


def test_replay_is_idempotent_and_can_heal_missing_outbox(store: UsageStore) -> None:
    first = _event("stable", prompt=3)
    changed_replay = _event("stable", prompt=999)

    store.record_batch([first], enqueue_outbox=False)
    healed = store.record_batch([changed_replay], enqueue_outbox=True)
    replayed = store.record_batch([changed_replay], enqueue_outbox=True)

    assert healed == PersistResult(analytics_inserted=0, outbox_inserted=1)
    assert replayed == PersistResult(analytics_inserted=0, outbox_inserted=0)
    assert store.count_request_usage() == 1
    assert store.count_outbox() == 1
    # First committed analytics value wins; the independently healed outbox
    # carries the replay payload. Normal operation writes both atomically, so
    # this discrepancy is limited to deliberate analytics-only recovery.
    assert store.list_request_usage(limit=1)[0]["prompt_tokens"] == 3
    assert store.peek_outbox(limit=1)[0]["prompt_tokens"] == 999


def test_peek_is_non_destructive_and_ack_is_idempotent(store: UsageStore) -> None:
    store.record_batch(
        [_event("e1", timestamp=1), _event("e2", timestamp=2)],
        enqueue_outbox=True,
    )

    first_attempt = store.peek_outbox(limit=10)
    retry_attempt = store.dequeue_outbox(limit=10)
    assert [row["event_id"] for row in first_attempt] == ["e1", "e2"]
    assert [row["event_id"] for row in retry_attempt] == ["e1", "e2"]
    assert store.count_outbox() == 2

    assert store.ack_outbox([first_attempt[0]["id"]]) == 1
    assert store.delete_pg_outbox([first_attempt[0]["id"]]) == 0
    assert [row["event_id"] for row in store.peek_pg_outbox(10)] == ["e2"]


def test_prune_keeps_newest_rows(store: UsageStore) -> None:
    store.record_batch(
        [_event(f"e{i}", timestamp=float(i)) for i in range(6)],
        enqueue_outbox=True,
    )

    assert store.prune_pg_outbox(2) == 4
    assert [row["event_id"] for row in store.peek_outbox(limit=10)] == ["e4", "e5"]


def test_second_table_failure_rolls_back_entire_batch(store: UsageStore) -> None:
    store._conn.executescript(  # noqa: SLF001 - deliberate transaction fault injection
        """
        CREATE TRIGGER fail_selected_outbox
        BEFORE INSERT ON pg_usage_outbox
        WHEN NEW.event_id = 'explode'
        BEGIN
          SELECT RAISE(ABORT, 'injected outbox failure');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected outbox failure"):
        store.record_batch(
            [_event("before"), _event("explode")],
            enqueue_outbox=True,
        )

    assert store.count_request_usage() == 0
    assert store.count_outbox() == 0


def test_rows_survive_close_and_reopen(tmp_path) -> None:
    path = tmp_path / "persistent.db"
    with UsageStore.open(path) as first:
        first.record_batch([_event("persisted")], enqueue_outbox=True)

    with UsageStore.open(path) as reopened:
        assert reopened.count_request_usage() == 1
        assert reopened.count_outbox() == 1
        assert reopened.peek_outbox(limit=1)[0]["event_id"] == "persisted"


def test_usage_listing_returns_the_newest_rows_first(store: UsageStore) -> None:
    store.record_batch(
        [_event("oldest"), _event("middle"), _event("newest")],
        enqueue_outbox=False,
    )

    assert [
        row["event_id"] for row in store.list_request_usage(limit=2)
    ] == ["newest", "middle"]


def test_thread_safe_batch_recording(store: UsageStore) -> None:
    def write(worker: int) -> None:
        events = [_event(f"w{worker}-{i}") for i in range(10)]
        store.record_batch(events, enqueue_outbox=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(4)))

    assert store.count_request_usage() == 40
    assert store.count_pg_outbox() == 40


def test_empty_and_nonpositive_limits_are_noops(store: UsageStore) -> None:
    assert store.record_batch([], enqueue_outbox=True) == PersistResult(0, 0)
    assert store.peek_outbox(limit=0) == []
    assert store.peek_pg_outbox(-1) == []
    assert store.ack_outbox([]) == 0
