from __future__ import annotations

import asyncio
import time

import pytest

from mnemosyne_fleet.config import ModelConfig, NodeConfig
from mnemosyne_fleet.protocol import Snapshot
from mnemosyne_fleet.registry import NodeRecord
from mnemosyne_fleet.scheduler import FleetBusyError, Scheduler

from .helpers import capacity, identity, snapshot_payload


class FakeRegistry:
    def __init__(
        self,
        records: list[NodeRecord],
        *,
        live_node_ids: set[str] | None = None,
        errors: dict[str, str | None] | None = None,
    ) -> None:
        self.records = {record.enrollment.node_id: record for record in records}
        self.live_node_ids = (
            set(self.records) if live_node_ids is None else live_node_ids
        )
        self.errors = errors or {}

    def live_records(self):
        return tuple(
            record
            for node_id, record in self.records.items()
            if node_id in self.live_node_ids
        )

    def record(self, node_id: str):
        return self.records.get(node_id)

    def is_live(self, record: NodeRecord):
        return record.enrollment.node_id in self.live_node_ids

    def error_code(self, node_id: str):
        return self.errors.get(node_id)


def record(node_id: str, *, warm: bool = True) -> NodeRecord:
    node = NodeConfig(
        node_id=node_id,
        url=f"http://{node_id}",
        fleet_token=f"fleet-{node_id}",
        inference_token=f"infer-{node_id}",
    )
    now = time.monotonic()
    return NodeRecord(
        enrollment=node,
        snapshot=Snapshot.model_validate(snapshot_payload(node_id, warm=warm)),
        received_at=time.time(),
        received_monotonic=now,
        poll_started_monotonic=now - 0.01,
    )


def scheduler(records: list[NodeRecord], *, queue_depth: int = 2) -> Scheduler:
    _, deployment_id = identity()
    nodes = tuple(row.enrollment for row in records) or (
        NodeConfig(
            node_id="offline",
            url="http://offline",
            fleet_token="fleet",
            inference_token="infer",
        ),
    )
    model = ModelConfig(
        name="qwen",
        deployment_id=deployment_id,
        capabilities=frozenset({"chat/completions", "completions", "responses"}),
        queue_depth=queue_depth,
        queue_timeout_seconds=0.1,
    )
    return Scheduler(registry=FakeRegistry(records), models=(model,), nodes=nodes)


async def test_equal_warm_replicas_fan_out_with_local_reservations() -> None:
    target = scheduler([record("a"), record("b")])
    first = await target.acquire(public_model="qwen", capability="responses")
    second = await target.acquire(public_model="qwen", capability="responses")
    assert {first.node_id, second.node_id} == {"a", "b"}
    await first.release()
    await second.release()


async def test_pending_reservation_is_not_forgotten_by_a_later_poll() -> None:
    row = record("a")
    target = scheduler([row], queue_depth=0)
    reservation = await target.acquire(
        public_model="qwen",
        capability="responses",
    )

    # The poll began after Fleet reserved the route, but before upstream
    # headers proved that the node had admitted it. The pending reservation
    # must still cover the stale free slot.
    later_poll = NodeRecord(
        enrollment=row.enrollment,
        snapshot=row.snapshot,
        received_at=row.received_at,
        received_monotonic=row.received_monotonic,
        poll_started_monotonic=reservation.reserved_at + 1,
    )
    assert target._unaccounted_reservations(later_poll) == 1

    admitted_at = reservation.reserved_at + 2
    reservation.mark_admitted(admitted_at)
    assert target._unaccounted_reservations(later_poll) == 1

    post_admission_poll = NodeRecord(
        enrollment=row.enrollment,
        snapshot=row.snapshot,
        received_at=row.received_at,
        received_monotonic=row.received_monotonic,
        poll_started_monotonic=admitted_at + 1,
    )
    assert target._unaccounted_reservations(post_admission_poll) == 0
    await reservation.release()


async def test_warm_node_precedes_cold_node() -> None:
    target = scheduler([record("warm", warm=True), record("cold", warm=False)])
    reservation = await target.acquire(public_model="qwen", capability="responses")
    assert reservation.node_id == "warm"
    await reservation.release()


async def test_fleet_queue_is_bounded() -> None:
    target = scheduler([], queue_depth=1)
    waiter = asyncio.create_task(
        target.acquire(public_model="qwen", capability="responses")
    )
    await asyncio.sleep(0)
    with pytest.raises(FleetBusyError, match="fleet_queue_full"):
        await target.acquire(public_model="qwen", capability="responses")
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert target.status()["queues"]["qwen"]["depth"] == 0


async def test_capability_set_must_match_exactly() -> None:
    row = record("a")
    payload = snapshot_payload("a")
    identity_value, deployment_id = identity(capabilities=("responses",))
    payload["deployments"][0]["identity"] = identity_value
    payload["deployments"][0]["deployment_id"] = deployment_id
    payload["residency"]["deployment_id"] = deployment_id
    row = NodeRecord(
        enrollment=row.enrollment,
        snapshot=Snapshot.model_validate(payload),
        received_at=row.received_at,
        received_monotonic=row.received_monotonic,
        poll_started_monotonic=row.poll_started_monotonic,
    )
    target = scheduler([row], queue_depth=0)
    with pytest.raises(FleetBusyError, match="fleet_queue_full"):
        await target.acquire(public_model="qwen", capability="responses")


async def test_reservation_release_survives_caller_cancellation() -> None:
    target = scheduler([record("a")])
    reservation = await target.acquire(
        public_model="qwen",
        capability="responses",
    )
    original_release = target.release
    release_started = asyncio.Event()
    allow_release = asyncio.Event()

    async def blocked_release(row) -> None:
        release_started.set()
        await allow_release.wait()
        await original_release(row)

    target.release = blocked_release
    task = asyncio.create_task(reservation.release())
    await release_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert target.status()["active_total"] == 1

    allow_release.set()
    async with asyncio.timeout(1):
        while target.status()["active_total"]:
            await asyncio.sleep(0)

    # A later cleanup caller joins the completed release instead of
    # double-decrementing scheduler accounting.
    await reservation.release()
    assert target.status()["active_total"] == 0


def test_model_matrix_distinguishes_strict_match_from_schedulable_health() -> None:
    row = record("degraded")
    payload = snapshot_payload("degraded", accepting=False)
    payload["health"]["state"] = "degraded"
    degraded = NodeRecord(
        enrollment=row.enrollment,
        snapshot=Snapshot.model_validate(payload),
        received_at=row.received_at,
        received_monotonic=row.received_monotonic,
        poll_started_monotonic=row.poll_started_monotonic,
    )
    matrix = scheduler([degraded]).model_matrix()
    assert matrix[0]["nodes"] == [
        {
            "node_id": "degraded",
            "online": True,
            "eligible": False,
            "strict_match": True,
            "warm": True,
            "alias": "degraded-qwen",
            "aliases": ["degraded-qwen"],
            "capacity": capacity(),
            "reason_codes": ["node_degraded", "node_not_accepting"],
            "snapshot_error_code": None,
        }
    ]


def test_model_matrix_includes_all_enrollments_and_stale_strict_replicas() -> None:
    live = record("live")
    stale = record("stale", warm=False)
    never = NodeConfig(
        node_id="never",
        url="http://never",
        fleet_token="fleet-never",
        inference_token="infer-never",
    )
    model = ModelConfig(
        name="qwen",
        deployment_id=live.snapshot.deployments[0].deployment_id,
        capabilities=frozenset({"chat/completions", "completions", "responses"}),
        queue_depth=2,
        queue_timeout_seconds=0.1,
    )
    target = Scheduler(
        registry=FakeRegistry(
            [live, stale],
            live_node_ids={"live"},
            errors={"never": "poll_timeout"},
        ),
        models=(model,),
        nodes=(live.enrollment, stale.enrollment, never),
    )

    matrix = target.model_matrix()[0]
    assert matrix["strict_replica_count"] == 2
    assert matrix["online_strict_replica_count"] == 1
    assert matrix["eligible_replica_count"] == 1
    assert [node["node_id"] for node in matrix["nodes"]] == [
        "live",
        "stale",
        "never",
    ]
    assert matrix["nodes"][0]["reason_codes"] == []
    assert matrix["nodes"][1]["strict_match"] is True
    assert matrix["nodes"][1]["eligible"] is False
    assert matrix["nodes"][1]["reason_codes"] == ["snapshot_stale"]
    assert matrix["nodes"][2] == {
        "node_id": "never",
        "online": False,
        "eligible": False,
        "strict_match": False,
        "warm": False,
        "alias": None,
        "aliases": [],
        "capacity": None,
        "reason_codes": ["snapshot_unavailable"],
        "snapshot_error_code": "poll_timeout",
    }


def test_model_matrix_reports_capability_mismatch_separately_from_health() -> None:
    row = record("mismatch")
    identity_value, deployment_id = identity(capabilities=("responses",))
    payload = snapshot_payload(
        "mismatch",
        deployment_id=deployment_id,
        identity_value=identity_value,
    )
    mismatch = NodeRecord(
        enrollment=row.enrollment,
        snapshot=Snapshot.model_validate(payload),
        received_at=row.received_at,
        received_monotonic=row.received_monotonic,
        poll_started_monotonic=row.poll_started_monotonic,
    )
    model = ModelConfig(
        name="qwen",
        deployment_id=deployment_id,
        capabilities=frozenset({"chat/completions", "completions", "responses"}),
        queue_depth=2,
        queue_timeout_seconds=0.1,
    )
    target = Scheduler(
        registry=FakeRegistry([mismatch]),
        models=(model,),
        nodes=(mismatch.enrollment,),
    )

    node = target.model_matrix()[0]["nodes"][0]
    assert node["online"] is True
    assert node["strict_match"] is False
    assert node["eligible"] is False
    assert node["aliases"] == []
    assert node["reason_codes"] == ["capabilities_mismatch"]
