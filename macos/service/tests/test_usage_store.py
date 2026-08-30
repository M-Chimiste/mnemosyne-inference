from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from mnemosyne_macos.usage import NormalizedUsage, UsageEvent
from mnemosyne_macos.usage_store import (
    PersistResult,
    UsageEventDuplicate,
    UsageOutboxFull,
    UsageStore,
)


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


def test_outbox_capacity_fails_before_mutation_and_never_prunes(store: UsageStore) -> None:
    store.record_batch(
        [_event("retained")],
        enqueue_outbox=True,
        max_outbox_rows=1,
    )

    with pytest.raises(UsageOutboxFull, match="configured capacity"):
        store.record_batch(
            [_event("refused")],
            enqueue_outbox=True,
            max_outbox_rows=1,
        )

    assert [row["event_id"] for row in store.peek_outbox(limit=10)] == [
        "retained"
    ]
    assert [row["event_id"] for row in store.list_request_usage(limit=10)] == [
        "retained"
    ]
    assert store.outbox_has_capacity(maximum=1) is False


def test_exact_replay_at_outbox_capacity_remains_idempotent(store: UsageStore) -> None:
    event = _event("stable")
    first = store.record_batch(
        [event],
        enqueue_outbox=True,
        max_outbox_rows=1,
    )
    replay = store.record_batch(
        [event],
        enqueue_outbox=True,
        max_outbox_rows=1,
    )

    assert first == PersistResult(analytics_inserted=1, outbox_inserted=1)
    assert replay == PersistResult(analytics_inserted=0, outbox_inserted=0)
    assert store.count_request_usage() == 1
    assert store.count_outbox() == 1


def test_last_outbox_slot_is_atomically_reserved_across_connections(tmp_path) -> None:
    path = tmp_path / "shared.db"
    first = UsageStore.open(path)
    second = UsageStore.open(path)
    barrier = Barrier(2)

    def contend(owner: UsageStore, event_id: str):
        barrier.wait(timeout=5)
        try:
            return owner.reserve_event(
                event_id,
                fleet_route=True,
                reserve_outbox=True,
                max_outbox_rows=1,
            )
        except UsageOutboxFull:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda item: contend(*item),
                    ((first, "route-a"), (second, "route-b")),
                )
            )

        admitted = [result for result in results if result is not None]
        assert len(admitted) == 1
        assert first.count_active_reservations() == 1
        assert first.outbox_capacity_status(maximum=1) == (0, 1, False)
    finally:
        first.close()
        second.close()


def test_fleet_reservation_rejects_active_and_completed_replay(
    store: UsageStore,
) -> None:
    reservation = store.reserve_event(
        "fleet-route",
        fleet_route=True,
        reserve_outbox=True,
        max_outbox_rows=1,
    )

    with pytest.raises(UsageEventDuplicate) as active:
        store.reserve_event(
            "fleet-route",
            fleet_route=True,
            reserve_outbox=True,
            max_outbox_rows=1,
        )
    assert active.value.state == "active"

    store.mark_event_started(reservation)
    result = store.finalize_reserved_event(reservation, _event("fleet-route"))

    assert result == PersistResult(analytics_inserted=1, outbox_inserted=1)
    assert store.count_active_reservations() == 0
    # Successful usage is permanently fenced by request_usage, so the
    # redundant bounded no-usage tombstone is removed.
    assert store.reservation_state("fleet-route") is None
    with pytest.raises(UsageEventDuplicate) as completed:
        store.reserve_event(
            "fleet-route",
            fleet_route=True,
            reserve_outbox=True,
            max_outbox_rows=1,
        )
    assert completed.value.state == "completed"


def test_finish_releases_prework_but_fences_started_fleet_work(
    store: UsageStore,
) -> None:
    prework = store.reserve_event(
        "prework",
        fleet_route=True,
        reserve_outbox=True,
        max_outbox_rows=2,
    )
    store.finish_reserved_event(prework)
    assert store.reservation_state("prework") is None

    started = store.reserve_event(
        "started",
        fleet_route=True,
        reserve_outbox=True,
        max_outbox_rows=2,
    )
    store.mark_event_started(started)
    store.finish_reserved_event(started)
    assert store.reservation_state("started") == "completed"
    assert store.outbox_capacity_status(maximum=2) == (0, 0, True)

    standalone = store.reserve_event(
        "standalone",
        fleet_route=False,
        reserve_outbox=True,
        max_outbox_rows=2,
    )
    store.mark_event_started(standalone)
    store.finish_reserved_event(standalone)
    assert store.reservation_state("standalone") is None


def test_completed_no_usage_tombstones_are_bounded_without_pruning_active(
    tmp_path,
) -> None:
    store = UsageStore.open(
        tmp_path / "bounded.db",
        completed_fleet_route_limit=2,
    )
    try:
        active = store.reserve_event(
            "active",
            fleet_route=True,
            reserve_outbox=False,
            max_outbox_rows=None,
        )
        assert active.event_id == "active"

        for index in range(4):
            reservation = store.reserve_event(
                f"completed-{index}",
                fleet_route=True,
                reserve_outbox=False,
                max_outbox_rows=None,
            )
            store.mark_event_started(reservation)
            store.finish_reserved_event(reservation)

        assert store.count_completed_reservations() == 2
        assert store.reservation_state("completed-0") is None
        assert store.reservation_state("completed-1") is None
        assert store.reservation_state("completed-2") == "completed"
        assert store.reservation_state("completed-3") == "completed"
        assert store.reservation_state("active") == "reserved"
        assert store.count_active_reservations() == 1
    finally:
        store.close()


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
