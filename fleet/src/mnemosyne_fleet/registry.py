from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from .config import NodeConfig
from .locator_policy import LocatorPolicy, LocatorPolicyError
from .paired_transport import (
    PairedTransportError,
    create_pinned_node_client,
)
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
PairedClientFactory = Callable[..., httpx.AsyncClient]


class NodeRegistry:
    def __init__(
        self,
        *,
        nodes: tuple[NodeConfig, ...],
        client: httpx.AsyncClient,
        poll_interval_seconds: float,
        ttl_seconds: float,
        on_change: ChangeCallback | None = None,
        paired_locator_policy: LocatorPolicy | None = None,
        paired_client_factory: PairedClientFactory = create_pinned_node_client,
    ) -> None:
        if len({node.enrollment_id for node in nodes}) != len(nodes):
            raise ValueError("node enrollments must have unique enrollment IDs")
        if len({node.reporting_node_id for node in nodes}) != len(nodes):
            raise ValueError("node enrollments must have unique reporting node IDs")
        if paired_locator_policy is None and any(
            node.source == "paired" for node in nodes
        ):
            raise ValueError("paired enrollments require a locator policy")
        self._nodes = {node.enrollment_id: node for node in nodes}
        self._client = client
        self._paired_locator_policy = paired_locator_policy
        self._paired_client_factory = paired_client_factory
        self._poll_interval = poll_interval_seconds
        self._ttl = ttl_seconds
        self._on_change = on_change
        self._records: dict[str, NodeRecord] = {}
        self._errors: dict[str, str | None] = {
            node.enrollment_id: "not_polled" for node in nodes
        }
        # A process instance may advance to a new random identity after a
        # restart, but it must never move back to an identity Nyx has already
        # retired. Without this fence, a delayed response from the predecessor
        # could replace the fresh snapshot merely because its instance_id
        # differs from the currently accepted one.
        self._retired_instance_ids: dict[str, set[str]] = {
            node.enrollment_id: set() for node in nodes
        }
        self._generation_counter = 0
        self._generations: dict[str, int] = {}
        for node in nodes:
            self._generation_counter += 1
            self._generations[node.enrollment_id] = self._generation_counter
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._membership_lock = asyncio.Lock()
        self._started = False
        self._stopping = asyncio.Event()
        self._log = logging.getLogger("mnemosyne-fleet.registry")

    def set_on_change(self, callback: ChangeCallback) -> None:
        self._on_change = callback

    async def start(self) -> None:
        async with self._membership_lock:
            if self._started:
                return
            self._started = True
            self._stopping.clear()
            for enrollment_id, node in self._nodes.items():
                self._tasks[enrollment_id] = self._new_poll_task(
                    node,
                    self._generations[enrollment_id],
                )

    async def stop(self) -> None:
        async with self._membership_lock:
            self._started = False
            self._stopping.set()
            tasks, self._tasks = tuple(self._tasks.values()), {}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _new_poll_task(
        self,
        node: NodeConfig,
        generation: int,
    ) -> asyncio.Task[None]:
        return asyncio.create_task(
            self._poll_loop(node, generation),
            name=f"fleet-poll-{node.enrollment_id}",
        )

    def _is_current(self, node: NodeConfig, generation: int) -> bool:
        return (
            self._nodes.get(node.enrollment_id) is node
            and self._generations.get(node.enrollment_id) == generation
        )

    def _set_error(
        self,
        node: NodeConfig,
        generation: int,
        value: str | None,
    ) -> bool:
        if not self._is_current(node, generation):
            return False
        self._errors[node.enrollment_id] = value
        return True

    async def _notify_change(self) -> None:
        if self._on_change is None:
            return
        try:
            await self._on_change()
        except Exception:
            self._log.warning("registry change callback failed")

    async def activate_enrollment(self, node: NodeConfig) -> int:
        """Publish an enrollment and start exactly one poller when running.

        Replacing an existing enrollment ID advances its generation and clears all
        snapshot authority before the new poller starts. Late work from the
        predecessor therefore cannot publish into the replacement.
        """

        if node.source == "paired" and self._paired_locator_policy is None:
            raise ValueError("paired enrollments require a locator policy")

        prior_task: asyncio.Task[None] | None = None
        async with self._membership_lock:
            current = self._nodes.get(node.enrollment_id)
            if current is node:
                return self._generations[node.enrollment_id]
            if current is not None and (
                current.reporting_node_id != node.reporting_node_id
                or current.source != node.source
            ):
                raise ValueError(
                    "an enrollment replacement cannot change identity or source"
                )
            if any(
                enrollment.enrollment_id != node.enrollment_id
                and enrollment.reporting_node_id == node.reporting_node_id
                for enrollment in self._nodes.values()
            ):
                raise ValueError("reporting node ID already has an active enrollment")
            prior_task = self._tasks.pop(node.enrollment_id, None)
            self._generation_counter += 1
            generation = self._generation_counter
            self._nodes[node.enrollment_id] = node
            self._generations[node.enrollment_id] = generation
            self._records.pop(node.enrollment_id, None)
            self._errors[node.enrollment_id] = "not_polled"
            self._retired_instance_ids[node.enrollment_id] = set()
            if self._started:
                self._tasks[node.enrollment_id] = self._new_poll_task(node, generation)
        if prior_task is not None:
            prior_task.cancel()
        await self._notify_change()
        if prior_task is not None:
            await asyncio.gather(prior_task, return_exceptions=True)
        return generation

    async def deactivate_enrollment(
        self,
        enrollment_id: str,
        *,
        expected: NodeConfig | None = None,
    ) -> NodeConfig | None:
        """Remove routing authority before waiting for the poller to stop."""

        async with self._membership_lock:
            current = self._nodes.get(enrollment_id)
            if current is None or (expected is not None and current is not expected):
                return None
            # These synchronous mutations happen before the first post-lock
            # await. Schedulers can no longer observe the node, and an old
            # poll generation can no longer publish, even if cancellation is
            # delayed in an HTTP transport.
            self._nodes.pop(enrollment_id, None)
            self._generations.pop(enrollment_id, None)
            self._records.pop(enrollment_id, None)
            self._errors.pop(enrollment_id, None)
            self._retired_instance_ids.pop(enrollment_id, None)
            task = self._tasks.pop(enrollment_id, None)
        if task is not None:
            task.cancel()
        await self._notify_change()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return current

    async def _poll_loop(self, node: NodeConfig, generation: int) -> None:
        while not self._stopping.is_set() and self._is_current(node, generation):
            await self._poll_once(node, generation)
            if not self._is_current(node, generation):
                return
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass

    async def poll_all_once(self) -> None:
        memberships = tuple(
            (node, self._generations[enrollment_id])
            for enrollment_id, node in self._nodes.items()
        )
        await asyncio.gather(
            *(self._poll_once(node, generation) for node, generation in memberships)
        )

    async def poll_once(self, node: NodeConfig) -> bool:
        generation = self._generations.get(node.enrollment_id)
        if generation is None or not self._is_current(node, generation):
            return False
        return await self._poll_once(node, generation)

    async def _poll_once(self, node: NodeConfig, generation: int) -> bool:
        if not self._is_current(node, generation):
            return False
        started = time.monotonic()
        owned_client: httpx.AsyncClient | None = None
        try:
            client = self._client
            request_timeout = min(max(self._poll_interval, 1.0), 10.0)
            if node.source == "paired":
                locator_policy = self._paired_locator_policy
                locator_transport = node.locator_transport
                if locator_policy is None or locator_transport is None:
                    self._set_error(
                        node,
                        generation,
                        "paired_transport_unavailable",
                    )
                    return False
                locator = await locator_policy.resolve(
                    node.url,
                    transport=locator_transport,
                )
                # Resolution is deliberately asynchronous and may race a
                # replacement or revocation. Never construct (and therefore
                # never connect) a credential-bearing client for a stale
                # membership generation.
                if not self._is_current(node, generation):
                    return False
                owned_client = self._paired_client_factory(
                    locator,
                    timeout=httpx.Timeout(
                        request_timeout,
                        connect=min(request_timeout, 5.0),
                    ),
                )
                if owned_client is self._client:
                    owned_client = None
                    raise PairedTransportError(
                        "paired_shared_client_forbidden"
                    )
                client = owned_client
                if not self._is_current(node, generation):
                    return False

            async with client.stream(
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
                timeout=request_timeout,
            ) as response:
                if response.status_code != 200:
                    self._set_error(node, generation, f"http_{response.status_code}")
                    return False
                content_encoding = response.headers.get("content-encoding")
                if content_encoding and content_encoding.lower() != "identity":
                    self._set_error(
                        node,
                        generation,
                        "snapshot_encoding_unsupported",
                    )
                    return False
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        self._set_error(node, generation, "invalid_snapshot")
                        return False
                    if declared_length < 0:
                        self._set_error(node, generation, "invalid_snapshot")
                        return False
                    if declared_length > MAX_SNAPSHOT_BYTES:
                        self._set_error(node, generation, "snapshot_too_large")
                        return False
                body = bytearray()
                if response.is_stream_consumed:
                    body.extend(response.content)
                    if len(body) > MAX_SNAPSHOT_BYTES:
                        self._set_error(node, generation, "snapshot_too_large")
                        return False
                else:
                    async for chunk in response.aiter_raw():
                        body.extend(chunk)
                        if len(body) > MAX_SNAPSHOT_BYTES:
                            self._set_error(node, generation, "snapshot_too_large")
                            return False
            snapshot = validate_snapshot(
                json.loads(body),
                expected_node_id=node.reporting_node_id,
                ttl_seconds=self._ttl,
            )
            if not self._is_current(node, generation):
                return False
            prior = self._records.get(node.enrollment_id)
            retired_instance_ids = self._retired_instance_ids.get(
                node.enrollment_id
            )
            if retired_instance_ids is None:
                return False
            instance_id = snapshot.node.instance_id
            if instance_id in retired_instance_ids:
                self._set_error(node, generation, "snapshot_instance_replayed")
                return False
            if (
                prior is not None
                and prior.snapshot.node.instance_id == instance_id
                and snapshot.snapshot_sequence <= prior.snapshot.snapshot_sequence
            ):
                self._set_error(node, generation, "snapshot_sequence_replayed")
                return False
            if (
                prior is not None
                and prior.snapshot.node.instance_id != instance_id
            ):
                if len(retired_instance_ids) >= MAX_RETIRED_INSTANCE_IDS:
                    # Keep the current record and every prior replay fence.
                    # Accepting another identity would require forgetting an
                    # older one and permit rollback; fail this enrollment
                    # closed until the one-process gateway restarts instead.
                    self._set_error(
                        node,
                        generation,
                        "snapshot_instance_churn_exhausted",
                    )
                    return False
                retired_instance_ids.add(prior.snapshot.node.instance_id)
            self._records[node.enrollment_id] = NodeRecord(
                enrollment=node,
                snapshot=snapshot,
                received_at=time.time(),
                received_monotonic=time.monotonic(),
                poll_started_monotonic=started,
            )
            self._set_error(node, generation, None)
            return True
        except httpx.TimeoutException:
            self._set_error(node, generation, "poll_timeout")
            return False
        except (LocatorPolicyError, PairedTransportError):
            self._set_error(node, generation, "paired_transport_rejected")
            return False
        except httpx.HTTPError:
            self._set_error(node, generation, "poll_transport_error")
            return False
        except (ValueError, TypeError, RecursionError):
            self._set_error(node, generation, "invalid_snapshot")
            return False
        finally:
            if owned_client is not None:
                try:
                    await owned_client.aclose()
                except BaseException:
                    self._log.warning("paired polling client close failed")
            await self._notify_change()

    def enrollments(self) -> tuple[NodeConfig, ...]:
        return tuple(self._nodes.values())

    def enrollment(self, enrollment_id: str) -> NodeConfig | None:
        return self._nodes.get(enrollment_id)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    def live_records(self, *, now_monotonic: float | None = None) -> tuple[NodeRecord, ...]:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return tuple(
            record
            for record in self._records.values()
            if self._nodes.get(record.enrollment.enrollment_id) is record.enrollment
            and now - record.received_monotonic <= self._ttl
        )

    def record(self, enrollment_id: str) -> NodeRecord | None:
        """Return the last authenticated snapshot, even after it becomes stale."""

        record = self._records.get(enrollment_id)
        if (
            record is not None
            and self._nodes.get(enrollment_id) is record.enrollment
        ):
            return record
        return None

    def is_live(
        self,
        record: NodeRecord,
        *,
        now_monotonic: float | None = None,
    ) -> bool:
        if (
            self._nodes.get(record.enrollment.enrollment_id)
            is not record.enrollment
        ):
            return False
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return now - record.received_monotonic <= self._ttl

    def error_code(self, enrollment_id: str) -> str | None:
        return self._errors.get(enrollment_id)

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
        for node in self._nodes.values():
            record = self._records.get(node.enrollment_id)
            live = record is not None and self.is_live(
                record,
                now_monotonic=now,
            )
            if record is None:
                output.append(
                    {
                        "node_id": node.reporting_node_id,
                        "reporting_node_id": node.reporting_node_id,
                        "enrollment_id": node.enrollment_id,
                        "source": node.source,
                        "service_class": node.service_class,
                        "online": False,
                        "last_seen": None,
                        "error_code": self._errors.get(node.enrollment_id),
                        "deployments": [],
                    }
                )
                continue
            snapshot = record.snapshot
            output.append(
                {
                    "node_id": node.reporting_node_id,
                    "reporting_node_id": node.reporting_node_id,
                    "enrollment_id": node.enrollment_id,
                    "source": node.source,
                    "service_class": node.service_class,
                    "online": live,
                    "last_seen": record.received_at,
                    "error_code": self._errors.get(node.enrollment_id),
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
