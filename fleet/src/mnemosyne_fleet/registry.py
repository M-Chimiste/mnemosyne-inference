from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from .config import NodeConfig
from .protocol import Snapshot, validate_snapshot


MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_RETIRED_INSTANCE_IDS = 1024


@dataclass(frozen=True, slots=True)
class NodeRecord:
    enrollment: NodeConfig
    snapshot: Snapshot
    received_at: float
    received_monotonic: float
    poll_started_monotonic: float


ChangeCallback = Callable[[], Awaitable[None]]


class NodeRegistry:
    def __init__(
        self,
        *,
        nodes: tuple[NodeConfig, ...],
        client: httpx.AsyncClient,
        poll_interval_seconds: float,
        ttl_seconds: float,
        on_change: ChangeCallback | None = None,
    ) -> None:
        self._nodes = nodes
        self._client = client
        self._poll_interval = poll_interval_seconds
        self._ttl = ttl_seconds
        self._on_change = on_change
        self._records: dict[str, NodeRecord] = {}
        self._errors: dict[str, str | None] = {node.node_id: "not_polled" for node in nodes}
        # A process instance may advance to a new random identity after a
        # restart, but it must never move back to an identity Nyx has already
        # retired. Without this fence, a delayed response from the predecessor
        # could replace the fresh snapshot merely because its instance_id
        # differs from the currently accepted one.
        self._retired_instance_ids: dict[str, set[str]] = {
            node.node_id: set() for node in nodes
        }
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self._log = logging.getLogger("mnemosyne-fleet.registry")

    def set_on_change(self, callback: ChangeCallback) -> None:
        self._on_change = callback

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping.clear()
        self._tasks = [
            asyncio.create_task(self._poll_loop(node), name=f"fleet-poll-{node.node_id}")
            for node in self._nodes
        ]

    async def stop(self) -> None:
        self._stopping.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_loop(self, node: NodeConfig) -> None:
        while not self._stopping.is_set():
            await self.poll_once(node)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass

    async def poll_all_once(self) -> None:
        await asyncio.gather(*(self.poll_once(node) for node in self._nodes))

    async def poll_once(self, node: NodeConfig) -> bool:
        started = time.monotonic()
        try:
            async with self._client.stream(
                "GET",
                f"{node.url}/fleet/v1/snapshot",
                headers={
                    "Authorization": f"Bearer {node.fleet_token}",
                    "Accept": "application/json",
                    # Snapshot v1 is a bounded JSON control document. Reading
                    # raw identity-encoded bytes avoids decompression bombs
                    # before the schema can enforce its field limits.
                    "Accept-Encoding": "identity",
                },
                timeout=min(max(self._poll_interval, 1.0), 10.0),
            ) as response:
                if response.status_code != 200:
                    self._errors[node.node_id] = f"http_{response.status_code}"
                    return False
                content_encoding = response.headers.get("content-encoding")
                if content_encoding and content_encoding.lower() != "identity":
                    self._errors[node.node_id] = "snapshot_encoding_unsupported"
                    return False
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        self._errors[node.node_id] = "invalid_snapshot"
                        return False
                    if declared_length < 0:
                        self._errors[node.node_id] = "invalid_snapshot"
                        return False
                    if declared_length > MAX_SNAPSHOT_BYTES:
                        self._errors[node.node_id] = "snapshot_too_large"
                        return False
                body = bytearray()
                if response.is_stream_consumed:
                    body.extend(response.content)
                    if len(body) > MAX_SNAPSHOT_BYTES:
                        self._errors[node.node_id] = "snapshot_too_large"
                        return False
                else:
                    async for chunk in response.aiter_raw():
                        body.extend(chunk)
                        if len(body) > MAX_SNAPSHOT_BYTES:
                            self._errors[node.node_id] = "snapshot_too_large"
                            return False
            snapshot = validate_snapshot(
                json.loads(body),
                expected_node_id=node.node_id,
                ttl_seconds=self._ttl,
            )
            prior = self._records.get(node.node_id)
            instance_id = snapshot.node.instance_id
            if instance_id in self._retired_instance_ids[node.node_id]:
                self._errors[node.node_id] = "snapshot_instance_replayed"
                return False
            if (
                prior is not None
                and prior.snapshot.node.instance_id == instance_id
                and snapshot.snapshot_sequence <= prior.snapshot.snapshot_sequence
            ):
                self._errors[node.node_id] = "snapshot_sequence_replayed"
                return False
            if (
                prior is not None
                and prior.snapshot.node.instance_id != instance_id
            ):
                if (
                    len(self._retired_instance_ids[node.node_id])
                    >= MAX_RETIRED_INSTANCE_IDS
                ):
                    # Keep the current record and every prior replay fence.
                    # Accepting another identity would require forgetting an
                    # older one and permit rollback; fail this enrollment
                    # closed until the one-process gateway restarts instead.
                    self._errors[node.node_id] = (
                        "snapshot_instance_churn_exhausted"
                    )
                    return False
                self._retired_instance_ids[node.node_id].add(
                    prior.snapshot.node.instance_id
                )
            self._records[node.node_id] = NodeRecord(
                enrollment=node,
                snapshot=snapshot,
                received_at=time.time(),
                received_monotonic=time.monotonic(),
                poll_started_monotonic=started,
            )
            self._errors[node.node_id] = None
            return True
        except httpx.TimeoutException:
            self._errors[node.node_id] = "poll_timeout"
            return False
        except httpx.HTTPError:
            self._errors[node.node_id] = "poll_transport_error"
            return False
        except (ValueError, TypeError, RecursionError):
            self._errors[node.node_id] = "invalid_snapshot"
            return False
        finally:
            if self._on_change is not None:
                try:
                    await self._on_change()
                except Exception:
                    self._log.warning("registry change callback failed")

    def live_records(self, *, now_monotonic: float | None = None) -> tuple[NodeRecord, ...]:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return tuple(
            record
            for record in self._records.values()
            if now - record.received_monotonic <= self._ttl
        )

    def record(self, node_id: str) -> NodeRecord | None:
        """Return the last authenticated snapshot, even after it becomes stale."""

        return self._records.get(node_id)

    def is_live(
        self,
        record: NodeRecord,
        *,
        now_monotonic: float | None = None,
    ) -> bool:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return now - record.received_monotonic <= self._ttl

    def error_code(self, node_id: str) -> str | None:
        return self._errors[node_id]

    @staticmethod
    def _deployment_inventory(snapshot: Snapshot) -> list[dict[str, object]]:
        """Build the secret-free operator view of a node's advertised catalog."""

        output: list[dict[str, object]] = []
        for deployment in snapshot.deployments:
            identity = deployment.identity
            artifact = identity.artifact
            output.append(
                {
                    "alias": deployment.alias,
                    "deployment_id": deployment.deployment_id,
                    "engine": identity.engine,
                    "upstream_model": identity.upstream_model,
                    "resolved_revision": identity.resolved_revision,
                    "artifact": {
                        "format": artifact.format,
                        "quantization": artifact.quantization,
                        "content_digest": artifact.content_digest,
                    },
                    "kind": identity.kind,
                    "capabilities": list(identity.capabilities),
                    "load_config_digest": identity.load_config_digest,
                    "identity_confidence": deployment.identity_confidence,
                    "fleet_eligible": deployment.fleet_eligible,
                    "loadable": deployment.loadable,
                    "warm": deployment.warm,
                    "capacity": deployment.capacity.model_dump(mode="json"),
                }
            )
        return output

    def status(self) -> list[dict[str, object]]:
        now = time.monotonic()
        output: list[dict[str, object]] = []
        for node in self._nodes:
            record = self._records.get(node.node_id)
            live = record is not None and self.is_live(
                record,
                now_monotonic=now,
            )
            if record is None:
                output.append(
                    {
                        "node_id": node.node_id,
                        "online": False,
                        "last_seen": None,
                        "error_code": self._errors[node.node_id],
                        "deployments": [],
                    }
                )
                continue
            snapshot = record.snapshot
            output.append(
                {
                    "node_id": node.node_id,
                    "online": live,
                    "last_seen": record.received_at,
                    "error_code": self._errors[node.node_id],
                    "instance_id": snapshot.node.instance_id,
                    "platform": snapshot.node.platform,
                    "version": snapshot.node.version,
                    "health": {
                        "state": snapshot.health.state,
                        "accepting": snapshot.health.accepting,
                        "authoritative": snapshot.health.authoritative,
                        "diagnostic_code": snapshot.health.diagnostic_code,
                    },
                    "residency": snapshot.residency.model_dump(mode="json"),
                    "admission": snapshot.admission.model_dump(mode="json"),
                    "capacity": snapshot.capacity.model_dump(mode="json"),
                    "usage_delivery": snapshot.usage_delivery.model_dump(mode="json"),
                    "deployments": self._deployment_inventory(snapshot),
                }
            )
        return output
