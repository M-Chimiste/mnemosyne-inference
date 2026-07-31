from __future__ import annotations

import asyncio

import httpx

from mnemosyne_fleet.config import NodeConfig
from mnemosyne_fleet.registry import (
    MAX_RETIRED_INSTANCE_IDS,
    MAX_SNAPSHOT_BYTES,
    NodeRegistry,
)

from .helpers import snapshot_payload


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
