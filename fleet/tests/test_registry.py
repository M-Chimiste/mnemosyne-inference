from __future__ import annotations

import asyncio
import json

import httpx

from mnemosyne_fleet.config import NodeConfig
from mnemosyne_fleet.locator_policy import LocatorPolicy
from mnemosyne_fleet.registry import (
    MAX_RETIRED_INSTANCE_IDS,
    MAX_SNAPSHOT_BYTES,
    NodeRegistry,
)

from .helpers import snapshot_payload


def _paired_policy(*, resolver=None) -> LocatorPolicy:
    return LocatorPolicy(
        cidr_allowlists={
            "https": ("10.0.0.0/8",),
            "tailscale": (),
            "trusted_lan_http": ("10.0.0.0/8",),
        },
        allowed_ports=(1240,),
        resolver=resolver or (lambda _host, _port: ("10.20.30.40",)),
    )


def _paired_factory(handler):
    def create(locator, **_kwargs):
        return httpx.AsyncClient(
            base_url=locator.origin,
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )

    return create


async def test_registry_uses_fleet_credential_and_replay_does_not_refresh_ttl() -> None:
    seen_authorization: list[str] = []
    payload = snapshot_payload("node-a", sequence=7)

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers["authorization"])
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        node = NodeConfig(
            node_id="node-a",
            url="http://node-a",
            fleet_token="fleet-only",
            inference_token="inference-only",
        )
        registry = NodeRegistry(
            nodes=(node,),
            client=client,
            poll_interval_seconds=0.1,
            ttl_seconds=1,
        )
        assert await registry.poll_once(node) is True
        received = registry.live_records()[0].received_monotonic
        assert await registry.poll_once(node) is False
        assert registry.live_records()[0].received_monotonic == received
        assert registry.status()[0]["error_code"] == "snapshot_sequence_replayed"

    assert seen_authorization == ["Bearer fleet-only", "Bearer fleet-only"]


async def test_paired_registry_keys_membership_separately_from_snapshot_identity() -> None:
    pairing_id = "11111111-1111-4111-8111-111111111111"
    payloads = [
        snapshot_payload(pairing_id),
        snapshot_payload("stable-reporting-node"),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        node = NodeConfig(
            node_id="stable-reporting-node",
            url="https://paired-node.invalid:1240",
            fleet_token="snapshot-secret",
            inference_token="dispatch-secret",
            source="paired",
            enrollment_id=pairing_id,
            locator_transport="https",
        )
        registry = NodeRegistry(
            nodes=(node,),
            client=client,
            poll_interval_seconds=0.1,
            ttl_seconds=1,
            paired_locator_policy=_paired_policy(),
            paired_client_factory=_paired_factory(handler),
        )

        assert await registry.poll_once(node) is False
        assert registry.error_code(pairing_id) == "invalid_snapshot"
        assert registry.record(pairing_id) is None
        assert await registry.poll_once(node) is True
        assert registry.record(pairing_id) is not None
        assert registry.record("stable-reporting-node") is None
        assert tuple(registry._records) == (pairing_id,)
        status = registry.status()[0]
        assert status["node_id"] == "stable-reporting-node"
        assert status["reporting_node_id"] == "stable-reporting-node"
        assert status["enrollment_id"] == pairing_id
        assert status["source"] == "paired"


async def test_registry_rejects_duplicate_active_reporting_identity() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)) as client:
        static = NodeConfig(
            node_id="stable-reporting-node",
            url="http://static-node",
            fleet_token="static-snapshot",
            inference_token="static-dispatch",
        )
        paired = NodeConfig(
            node_id="stable-reporting-node",
            url="https://paired-node.invalid:1240",
            fleet_token="paired-snapshot",
            inference_token="paired-dispatch",
            source="paired",
            enrollment_id="11111111-1111-4111-8111-111111111111",
            locator_transport="https",
        )
        registry = NodeRegistry(
            nodes=(static,),
            client=client,
            poll_interval_seconds=1,
            ttl_seconds=2,
            paired_locator_policy=_paired_policy(),
        )

        try:
            await registry.activate_enrollment(paired)
        except ValueError as exc:
            assert str(exc) == "reporting node ID already has an active enrollment"
        else:  # pragma: no cover - fail explicitly without pytest dependency
            raise AssertionError("duplicate reporting identity was accepted")


async def test_paired_replacement_cannot_relabel_reporting_identity() -> None:
    pairing_id = "11111111-1111-4111-8111-111111111111"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)) as client:
        original = NodeConfig(
            node_id="stable-reporting-node",
            url="https://paired-node.invalid:1240",
            fleet_token="old-snapshot",
            inference_token="old-dispatch",
            source="paired",
            enrollment_id=pairing_id,
            locator_transport="https",
        )
        relabeled = NodeConfig(
            node_id="different-reporting-node",
            url="https://paired-node.invalid:1240",
            fleet_token="new-snapshot",
            inference_token="new-dispatch",
            source="paired",
            enrollment_id=pairing_id,
            locator_transport="https",
        )
        registry = NodeRegistry(
            nodes=(original,),
            client=client,
            poll_interval_seconds=1,
            ttl_seconds=2,
            paired_locator_policy=_paired_policy(),
        )

        try:
            await registry.activate_enrollment(relabeled)
        except ValueError as exc:
            assert str(exc) == (
                "an enrollment replacement cannot change identity or source"
            )
        else:  # pragma: no cover - fail explicitly without pytest dependency
            raise AssertionError("paired reporting identity was relabeled")
        assert registry.enrollment(pairing_id) is original


async def test_changed_instance_may_restart_snapshot_sequence() -> None:
    payloads = [
        snapshot_payload("node-a", sequence=9, instance_id="old"),
        snapshot_payload("node-a", sequence=0, instance_id="new"),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        node = NodeConfig(
            node_id="node-a",
            url="http://node-a",
            fleet_token="fleet",
            inference_token="infer",
        )
        registry = NodeRegistry(
            nodes=(node,),
            client=client,
            poll_interval_seconds=0.1,
            ttl_seconds=1,
        )
        assert await registry.poll_once(node)
        assert await registry.poll_once(node)
        assert registry.live_records()[0].snapshot.node.instance_id == "new"


async def test_retired_instance_cannot_replace_fresh_restart_snapshot() -> None:
    payloads = [
        snapshot_payload("node-a", sequence=9, instance_id="old"),
        snapshot_payload("node-a", sequence=0, instance_id="new"),
        snapshot_payload("node-a", sequence=10, instance_id="old"),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        node = NodeConfig(
            node_id="node-a",
            url="http://node-a",
            fleet_token="fleet",
            inference_token="infer",
        )
        registry = NodeRegistry(
            nodes=(node,),
            client=client,
            poll_interval_seconds=0.1,
            ttl_seconds=1,
        )
        assert await registry.poll_once(node)
        assert await registry.poll_once(node)
        received = registry.live_records()[0].received_monotonic

        assert await registry.poll_once(node) is False
        assert registry.live_records()[0].snapshot.node.instance_id == "new"
        assert registry.live_records()[0].received_monotonic == received
        assert registry.status()[0]["error_code"] == "snapshot_instance_replayed"


async def test_expired_node_rejoins_only_after_fresh_instance_snapshot() -> None:
    payloads = [
        snapshot_payload("node-a", sequence=3, instance_id="before"),
        snapshot_payload("node-a", sequence=0, instance_id="after"),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        node = NodeConfig(
            node_id="node-a",
            url="http://node-a",
            fleet_token="fleet",
            inference_token="infer",
        )
        registry = NodeRegistry(
            nodes=(node,),
            client=client,
            poll_interval_seconds=0.1,
            ttl_seconds=1,
        )
        assert await registry.poll_once(node)
        received = registry.live_records()[0].received_monotonic
        assert registry.live_records(now_monotonic=received + 1.01) == ()

        assert await registry.poll_once(node)
        live = registry.live_records()
        assert len(live) == 1
        assert live[0].snapshot.node.instance_id == "after"


class _TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.iterated = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.iterated.set()
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed.set()


class _BlockedSnapshotStream(httpx.AsyncByteStream):
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.started = asyncio.Event()
        self.allow_response = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await self.allow_response.wait()
        yield json.dumps(self.payload).encode("utf-8")

    async def aclose(self) -> None:
        self.closed.set()


async def test_activate_and_deactivate_own_exactly_one_live_poller() -> None:
    polled = asyncio.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        polled.set()
        return httpx.Response(200, json=snapshot_payload("dynamic"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry = NodeRegistry(
            nodes=(),
            client=client,
            poll_interval_seconds=100,
            ttl_seconds=200,
        )
        await registry.start()
        node = NodeConfig(
            node_id="dynamic",
            url="http://dynamic",
            fleet_token="fleet-dynamic",
            inference_token="infer-dynamic",
        )
        generation = await registry.activate_enrollment(node)
        assert await registry.activate_enrollment(node) == generation
        await polled.wait()
        async with asyncio.timeout(1):
            while registry.record("dynamic") is None:
                await asyncio.sleep(0)
        assert registry.enrollments() == (node,)
        assert registry.node_count == 1
        assert tuple(registry._tasks) == ("dynamic",)

        assert await registry.deactivate_enrollment(
            "dynamic",
            expected=node,
        ) is node
        assert registry.enrollments() == ()
        assert registry.node_count == 0
        assert registry.record("dynamic") is None
        assert registry.error_code("dynamic") is None
        assert registry.status() == []
        assert registry._tasks == {}
        await registry.stop()


async def test_late_poll_cannot_publish_into_replacement_generation() -> None:
    pairing_id = "11111111-1111-4111-8111-111111111111"
    old_stream = _BlockedSnapshotStream(
        snapshot_payload("node-a", instance_id="old-instance")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer old-fleet":
            return httpx.Response(200, stream=old_stream)
        return httpx.Response(
            200,
            json=snapshot_payload("node-a", instance_id="new-instance"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        old = NodeConfig(
            node_id="node-a",
            url="http://old-node-a:1240",
            fleet_token="old-fleet",
            inference_token="old-infer",
            source="paired",
            enrollment_id=pairing_id,
            locator_transport="trusted_lan_http",
        )
        replacement = NodeConfig(
            node_id="node-a",
            url="http://new-node-a:1240",
            fleet_token="new-fleet",
            inference_token="new-infer",
            source="paired",
            enrollment_id=pairing_id,
            locator_transport="trusted_lan_http",
        )
        registry = NodeRegistry(
            nodes=(old,),
            client=client,
            poll_interval_seconds=1,
            ttl_seconds=2,
            paired_locator_policy=_paired_policy(),
            paired_client_factory=_paired_factory(handler),
        )
        old_poll = asyncio.create_task(registry.poll_once(old))
        await old_stream.started.wait()

        assert await registry.deactivate_enrollment(
            "node-a",
            expected=old,
        ) is None
        assert await registry.deactivate_enrollment(
            pairing_id,
            expected=old,
        ) is old
        await registry.activate_enrollment(replacement)
        old_stream.allow_response.set()
        assert await old_poll is False
        assert old_stream.closed.is_set()
        assert registry.record(pairing_id) is None
        assert registry.error_code(pairing_id) == "not_polled"

        assert await registry.poll_once(replacement) is True
        record = registry.record(pairing_id)
        assert record is not None
        assert record.enrollment is replacement
        assert record.snapshot.node.instance_id == "new-instance"


async def test_declared_oversize_snapshot_is_rejected_without_reading_body() -> None:
    stream = _TrackingStream((b"unreachable",))
    seen_encoding: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_encoding.append(request.headers["accept-encoding"])
        return httpx.Response(
            200,
            headers={"Content-Length": str(MAX_SNAPSHOT_BYTES + 1)},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        node = NodeConfig(
            node_id="node-a",
            url="http://node-a",
            fleet_token="fleet",
            inference_token="infer",
        )
        registry = NodeRegistry(
            nodes=(node,),
            client=client,
            poll_interval_seconds=0.1,
            ttl_seconds=1,
        )
        assert await registry.poll_once(node) is False
        assert registry.error_code("node-a") == "snapshot_too_large"

    assert seen_encoding == ["identity"]
    assert not stream.iterated.is_set()
    assert stream.closed.is_set()


async def test_chunked_oversize_snapshot_is_stopped_at_fixed_cap() -> None:
    stream = _TrackingStream(
        (
            b"x" * MAX_SNAPSHOT_BYTES,
            b"x",
            b"unreachable",
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        node = NodeConfig(
            node_id="node-a",
            url="http://node-a",
            fleet_token="fleet",
            inference_token="infer",
        )
        registry = NodeRegistry(
            nodes=(node,),
            client=client,
            poll_interval_seconds=0.1,
            ttl_seconds=1,
        )
        assert await registry.poll_once(node) is False
        assert registry.error_code("node-a") == "snapshot_too_large"

    assert stream.iterated.is_set()
    assert stream.closed.is_set()


async def test_instance_churn_fails_closed_without_forgetting_replay_fences() -> None:
    payloads = [
        snapshot_payload("node-a", sequence=1, instance_id="current"),
        snapshot_payload("node-a", sequence=0, instance_id="next"),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        node = NodeConfig(
            node_id="node-a",
            url="http://node-a",
            fleet_token="fleet",
            inference_token="infer",
        )
        registry = NodeRegistry(
            nodes=(node,),
            client=client,
            poll_interval_seconds=0.1,
            ttl_seconds=1,
        )
        assert await registry.poll_once(node) is True
        retired = registry._retired_instance_ids["node-a"]
        retired.update(
            f"retired-{index}" for index in range(MAX_RETIRED_INSTANCE_IDS)
        )

        assert await registry.poll_once(node) is False
        assert registry.error_code("node-a") == (
            "snapshot_instance_churn_exhausted"
        )
        assert registry.record("node-a").snapshot.node.instance_id == "current"
        assert len(retired) == MAX_RETIRED_INSTANCE_IDS


async def test_deeply_nested_json_does_not_kill_subsequent_polling() -> None:
    responses = [
        httpx.Response(200, content=b"[" * 2000 + b"]" * 2000),
        httpx.Response(200, json=snapshot_payload("node-a")),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        node = NodeConfig(
            node_id="node-a",
            url="http://node-a",
            fleet_token="fleet",
            inference_token="infer",
        )
        registry = NodeRegistry(
            nodes=(node,),
            client=client,
            poll_interval_seconds=0.1,
            ttl_seconds=1,
        )
        assert await registry.poll_once(node) is False
        assert registry.error_code("node-a") == "invalid_snapshot"
        assert await registry.poll_once(node) is True
        assert registry.record("node-a") is not None
