from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Literal

from .config import SERVICE_CLASSES, ModelConfig, NodeConfig
from .protocol import Deployment
from .registry import NodeRecord, NodeRegistry


class UnknownModelError(LookupError):
    pass


class CapabilityError(LookupError):
    pass


class FleetBusyError(RuntimeError):
    def __init__(self, code: str, retry_after: int = 1) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class ModelMutationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


RequestPriority = Literal["interactive", "normal", "batch"]
FallbackPolicy = Literal["allow", "none"]
_PRIORITY_RANK: dict[RequestPriority, int] = {
    "interactive": 0,
    "normal": 1,
    "batch": 2,
}
_PRIORITY_AGING_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class RoutingControls:
    """Optional caller policy; defaults reproduce the original scheduler."""

    priority: RequestPriority = "normal"
    affinity_enrollment_id: str | None = None
    fallback: FallbackPolicy = "allow"
    max_wait_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    record: NodeRecord
    enrollment: NodeConfig
    deployment: Deployment
    service_class_rank: int
    tier: int
    load_score: float


@dataclass(slots=True)
class Reservation:
    route_id: str
    public_model: str
    capability: str
    enrollment_id: str
    node_id: str
    instance_id: str
    deployment_id: str
    local_alias: str
    enrollment: NodeConfig = field(repr=False)
    reserved_at: float
    queue_ms: float
    _scheduler: "Scheduler"
    admitted_at: float | None = None
    _released: bool = False
    _release_task: asyncio.Task[None] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    @property
    def reporting_node_id(self) -> str:
        return self.node_id

    async def release(self) -> None:
        if self._released:
            return
        task = self._release_task
        if task is None:
            # A caller can be cancelled while returning capacity. Keep the
            # actual release in a separately owned task so cancellation (even
            # repeated cancellation) cannot strand a local reservation.
            task = asyncio.create_task(
                self._scheduler.release(self),
                name=f"fleet-release-{self.route_id}",
            )
            self._release_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The shielded task remains responsible for returning capacity.
            raise
        except BaseException:
            # Scheduler release is expected to be infallible, but allow a
            # genuine failure to be retried instead of marking it complete.
            if self._release_task is task:
                self._release_task = None
            raise
        else:
            self._released = True

    def mark_admitted(self, admitted_at: float | None = None) -> None:
        """Record the first upstream headers that prove node admission.

        Until this happens, no subsequent poll can safely be assumed to have
        observed the reservation. Once admitted, a poll that started later may
        account for it in node-local active/queue state.
        """

        if self.admitted_at is None:
            self.admitted_at = (
                time.monotonic() if admitted_at is None else admitted_at
            )


@dataclass(frozen=True, slots=True)
class _Ticket:
    ticket_id: str
    capability: str
    enqueued_at: float
    excluded_enrollment_ids: frozenset[str]
    controls: RoutingControls


class Scheduler:
    """Service-class-first scheduler with bounded FIFO queues and reservations."""

    def __init__(
        self,
        *,
        registry: NodeRegistry,
        models: tuple[ModelConfig, ...],
        nodes: tuple[NodeConfig, ...],
    ) -> None:
        self._registry = registry
        self._models = {model.name: model for model in models}
        # Membership is owned by NodeRegistry. Keep the argument temporarily
        # for source compatibility with callers while dynamic enrollment is
        # introduced, but never retain a second routing-authority snapshot.
        del nodes
        self._condition = asyncio.Condition()
        self._queues: dict[str, deque[_Ticket]] = defaultdict(deque)
        self._reservations: dict[str, Reservation] = {}
        self._selection_counter = 0
        self._last_selected: dict[str, int] = defaultdict(int)

    def model(self, name: str) -> ModelConfig:
        try:
            return self._models[name]
        except KeyError as exc:
            raise UnknownModelError(name) from exc

    def models(self) -> tuple[ModelConfig, ...]:
        return tuple(self._models.values())

    def _model_has_work(self, public_model: str) -> bool:
        return bool(
            self._queues.get(public_model)
            or any(
                reservation.public_model == public_model
                for reservation in self._reservations.values()
            )
        )

    async def add_model(self, model: ModelConfig) -> None:
        """Publish or idempotently restore one exact public mapping."""

        async with self._condition:
            current = self._models.get(model.name)
            if current == model:
                return
            if current is not None:
                if self._model_has_work(model.name):
                    raise ModelMutationError("model_mapping_in_use")
                raise ModelMutationError("model_mapping_conflict")
            self._models[model.name] = model
            self._condition.notify_all()

    async def remove_model(self, public_model: str) -> ModelConfig:
        """Remove one mapping only while no request can still reference it."""

        async with self._condition:
            current = self._models.get(public_model)
            if current is None:
                raise ModelMutationError("model_mapping_unknown")
            if self._model_has_work(public_model):
                raise ModelMutationError("model_mapping_in_use")
            del self._models[public_model]
            self._queues.pop(public_model, None)
            self._condition.notify_all()
            return current

    async def wake(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    def _unaccounted_reservations(self, record: NodeRecord) -> int:
        return sum(
            1
            for reservation in self._reservations.values()
            if reservation.enrollment_id == record.enrollment.enrollment_id
            and reservation.instance_id == record.snapshot.node.instance_id
            and (
                reservation.admitted_at is None
                or reservation.admitted_at >= record.poll_started_monotonic
            )
        )

    def _candidate_deployment(
        self,
        record: NodeRecord,
        *,
        model: ModelConfig,
        capability: str,
    ) -> Deployment | None:
        for deployment in self._strict_deployments(record, model=model):
            if (
                deployment.identity_confidence == "authoritative"
                and deployment.fleet_eligible
                and capability in deployment.identity.capabilities
                and deployment.loadable
            ):
                return deployment
        return None

    @staticmethod
    def _id_deployments(
        record: NodeRecord,
        *,
        model: ModelConfig,
    ) -> tuple[Deployment, ...]:
        return tuple(
            deployment
            for deployment in record.snapshot.deployments
            if deployment.deployment_id == model.deployment_id
        )

    @classmethod
    def _strict_deployments(
        cls,
        record: NodeRecord,
        *,
        model: ModelConfig,
    ) -> tuple[Deployment, ...]:
        return tuple(
            deployment
            for deployment in cls._id_deployments(record, model=model)
            if set(deployment.identity.capabilities) == set(model.capabilities)
        )

    def _select(
        self,
        *,
        model: ModelConfig,
        capability: str,
        excluded_enrollment_ids: frozenset[str],
        controls: RoutingControls,
    ) -> Candidate | None:
        candidates: list[Candidate] = []
        for record in self._registry.live_records():
            enrollment_id = record.enrollment.enrollment_id
            enrollment = self._registry.enrollment(enrollment_id)
            if enrollment is None or record.enrollment is not enrollment:
                continue
            snapshot = record.snapshot
            if (
                enrollment_id in excluded_enrollment_ids
                or (
                    controls.affinity_enrollment_id is not None
                    and controls.fallback == "none"
                    and enrollment_id != controls.affinity_enrollment_id
                )
                or not snapshot.health.accepting
                or not snapshot.health.authoritative
                or snapshot.health.state in {"degraded", "stopping"}
            ):
                continue
            deployment = self._candidate_deployment(
                record,
                model=model,
                capability=capability,
            )
            if deployment is None:
                continue

            local = self._unaccounted_reservations(record)
            resident = snapshot.residency.deployment_id == model.deployment_id
            advertised_available = (
                snapshot.capacity.available
                if resident
                else deployment.capacity.available
            )
            available = max(0, advertised_available - local)
            node_queue_room = max(
                0, snapshot.admission.queue_limit - snapshot.admission.queue_depth - local
            )
            if resident and available > 0:
                tier = 1
            elif resident and node_queue_room > 0:
                tier = 2
            elif (
                snapshot.residency.deployment_id is None
                and node_queue_room > 0
                and deployment.capacity.effective_limit > 0
            ):
                tier = 3
            elif node_queue_room > 0 and deployment.capacity.effective_limit > 0:
                tier = 4
            else:
                continue

            configured_weight = enrollment.routing_weight
            weight = float(deployment.capacity.effective_limit)
            if configured_weight is not None:
                weight = min(weight, configured_weight)
            outstanding = snapshot.capacity.active + snapshot.admission.queue_depth + local
            candidates.append(
                Candidate(
                    record=record,
                    enrollment=enrollment,
                    deployment=deployment,
                    service_class_rank=SERVICE_CLASSES.index(
                        enrollment.service_class
                    ),
                    tier=tier,
                    load_score=outstanding / max(weight, 1e-9),
                )
            )
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda candidate: (
                0
                if (
                    controls.affinity_enrollment_id is None
                    or candidate.enrollment.enrollment_id
                    == controls.affinity_enrollment_id
                )
                else 1,
                candidate.service_class_rank,
                candidate.tier,
                candidate.load_score,
                self._last_selected[
                    candidate.record.enrollment.enrollment_id
                ],
                candidate.record.enrollment.enrollment_id,
            ),
        )

    async def acquire(
        self,
        *,
        public_model: str,
        capability: str,
        excluded_enrollment_ids: frozenset[str] = frozenset(),
        controls: RoutingControls = RoutingControls(),
    ) -> Reservation:
        started = time.monotonic()
        ticket: _Ticket | None = None

        async with self._condition:
            model = self.model(public_model)
            if capability not in model.capabilities:
                raise CapabilityError(capability)
            requested_wait = controls.max_wait_seconds
            wait_seconds = (
                model.queue_timeout_seconds
                if requested_wait is None
                else max(0.0, min(requested_wait, model.queue_timeout_seconds))
            )
            deadline = started + wait_seconds
            queue = self._queues[public_model]
            if not queue:
                candidate = self._select(
                    model=model,
                    capability=capability,
                    excluded_enrollment_ids=excluded_enrollment_ids,
                    controls=controls,
                )
                if candidate is not None:
                    return self._reserve(candidate, model, capability, started, started)
            if len(queue) >= model.queue_depth:
                raise FleetBusyError("fleet_queue_full")
            ticket = _Ticket(
                ticket_id=uuid.uuid4().hex,
                capability=capability,
                enqueued_at=started,
                excluded_enrollment_ids=excluded_enrollment_ids,
                controls=controls,
            )
            queue.append(ticket)
            try:
                while True:
                    if self._next_ticket(queue).ticket_id == ticket.ticket_id:
                        candidate = self._select(
                            model=model,
                            capability=capability,
                            excluded_enrollment_ids=excluded_enrollment_ids,
                            controls=controls,
                        )
                        if candidate is not None:
                            queue.remove(ticket)
                            reservation = self._reserve(
                                candidate,
                                model,
                                capability,
                                started,
                                time.monotonic(),
                            )
                            self._condition.notify_all()
                            return reservation
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise FleetBusyError("fleet_queue_timeout")
                    try:
                        await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                    except TimeoutError as exc:
                        raise FleetBusyError("fleet_queue_timeout") from exc
            finally:
                if ticket in queue:
                    queue.remove(ticket)
                    self._condition.notify_all()

    @staticmethod
    def _next_ticket(queue: deque[_Ticket]) -> _Ticket:
        """Prefer explicit priority while aging lower lanes toward fairness."""

        now = time.monotonic()
        return min(
            queue,
            key=lambda ticket: (
                max(
                    0,
                    _PRIORITY_RANK[ticket.controls.priority]
                    - int((now - ticket.enqueued_at) / _PRIORITY_AGING_SECONDS),
                ),
                ticket.enqueued_at,
                ticket.ticket_id,
            ),
        )

    def _reserve(
        self,
        candidate: Candidate,
        model: ModelConfig,
        capability: str,
        queued_at: float,
        reserved_at: float,
    ) -> Reservation:
        route_id = str(uuid.uuid4())
        record = candidate.record
        reservation = Reservation(
            route_id=route_id,
            public_model=model.name,
            capability=capability,
            enrollment_id=record.enrollment.enrollment_id,
            node_id=record.enrollment.reporting_node_id,
            instance_id=record.snapshot.node.instance_id,
            deployment_id=model.deployment_id,
            local_alias=candidate.deployment.alias,
            enrollment=candidate.enrollment,
            reserved_at=reserved_at,
            queue_ms=max(0.0, (reserved_at - queued_at) * 1000.0),
            _scheduler=self,
        )
        self._reservations[route_id] = reservation
        self._selection_counter += 1
        self._last_selected[reservation.enrollment_id] = self._selection_counter
        return reservation

    async def release(self, reservation: Reservation) -> None:
        async with self._condition:
            self._reservations.pop(reservation.route_id, None)
            self._condition.notify_all()

    def dispatch_is_current(self, reservation: Reservation) -> bool:
        """Check the enrollment generation immediately before dispatch.

        A reservation already owns scheduler capacity, but revocation between
        acquire and the upstream send must still stop pre-work dispatch. Once
        upstream headers are accepted, ordinary route ownership holds the
        response and reservation through full-stream cleanup instead.
        """

        return (
            self._reservations.get(reservation.route_id) is reservation
            and self._registry.enrollment(reservation.enrollment_id)
            is reservation.enrollment
        )

    def enrollment(self, enrollment_id: str) -> NodeConfig:
        enrollment = self._registry.enrollment(enrollment_id)
        if enrollment is None:
            raise KeyError(enrollment_id)
        return enrollment

    def has_enrollment(self, enrollment_id: str) -> bool:
        return self._registry.enrollment(enrollment_id) is not None

    @property
    def node_count(self) -> int:
        return self._registry.node_count

    def status(self) -> dict[str, object]:
        active_by_node: dict[str, int] = defaultdict(int)
        active_by_enrollment: dict[str, int] = defaultdict(int)
        active_by_model: dict[str, int] = defaultdict(int)
        for reservation in self._reservations.values():
            active_by_node[reservation.node_id] += 1
            active_by_enrollment[reservation.enrollment_id] += 1
            active_by_model[reservation.public_model] += 1
        return {
            "active_total": len(self._reservations),
            "active_by_node": dict(active_by_node),
            "active_by_enrollment": dict(active_by_enrollment),
            "active_by_model": dict(active_by_model),
            "queues": {
                model: {
                    "depth": len(self._queues[model]),
                    "by_priority": {
                        priority: sum(
                            1
                            for ticket in self._queues[model]
                            if ticket.controls.priority == priority
                        )
                        for priority in ("interactive", "normal", "batch")
                    },
                    "limit": config.queue_depth,
                    "timeout_seconds": config.queue_timeout_seconds,
                }
                for model, config in self._models.items()
            },
        }

    def model_matrix(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for model in self._models.values():
            nodes: list[dict[str, object]] = []
            for enrollment in self._registry.enrollments():
                enrollment_id = enrollment.enrollment_id
                reporting_node_id = enrollment.reporting_node_id
                record = self._registry.record(enrollment_id)
                snapshot_error_code = self._registry.error_code(enrollment_id)
                if record is None:
                    nodes.append(
                        {
                            "node_id": reporting_node_id,
                            "reporting_node_id": reporting_node_id,
                            "enrollment_id": enrollment_id,
                            "source": enrollment.source,
                            "service_class": enrollment.service_class,
                            "online": False,
                            "eligible": False,
                            "strict_match": False,
                            "warm": False,
                            "alias": None,
                            "aliases": [],
                            "capacity": None,
                            "reason_codes": ["snapshot_unavailable"],
                            "snapshot_error_code": snapshot_error_code,
                        }
                    )
                    continue

                online = self._registry.is_live(record)
                id_deployments = self._id_deployments(record, model=model)
                strict_deployments = self._strict_deployments(record, model=model)
                authoritative = tuple(
                    deployment
                    for deployment in strict_deployments
                    if deployment.identity_confidence == "authoritative"
                )
                fleet_eligible = tuple(
                    deployment
                    for deployment in authoritative
                    if deployment.fleet_eligible
                )
                routable = tuple(
                    deployment
                    for deployment in fleet_eligible
                    if deployment.loadable
                )
                health = record.snapshot.health
                schedulable_health = (
                    health.accepting
                    and health.authoritative
                    and health.state not in {"degraded", "stopping"}
                )
                reason_codes: list[str] = []
                if not online:
                    reason_codes.append("snapshot_stale")
                if not id_deployments:
                    reason_codes.append("deployment_not_advertised")
                elif not strict_deployments:
                    reason_codes.append("capabilities_mismatch")
                else:
                    if not authoritative:
                        reason_codes.append("identity_unverified")
                    elif not fleet_eligible:
                        reason_codes.append("fleet_ineligible")
                    elif not routable:
                        reason_codes.append("not_loadable")
                if not health.authoritative:
                    reason_codes.append("node_not_authoritative")
                if health.state == "degraded":
                    reason_codes.append("node_degraded")
                elif health.state == "stopping":
                    reason_codes.append("node_stopping")
                if not health.accepting:
                    reason_codes.append("node_not_accepting")

                display_deployment = (
                    routable[0]
                    if routable
                    else strict_deployments[0] if strict_deployments else None
                )
                nodes.append(
                    {
                        "node_id": reporting_node_id,
                        "reporting_node_id": reporting_node_id,
                        "enrollment_id": enrollment_id,
                        "source": enrollment.source,
                        "service_class": enrollment.service_class,
                        "online": online,
                        "eligible": bool(routable) and online and schedulable_health,
                        "strict_match": bool(strict_deployments),
                        "warm": any(
                            deployment.warm for deployment in strict_deployments
                        ),
                        "alias": (
                            display_deployment.alias
                            if display_deployment is not None
                            else None
                        ),
                        "aliases": sorted(
                            deployment.alias for deployment in strict_deployments
                        ),
                        "capacity": (
                            display_deployment.capacity.model_dump(mode="json")
                            if display_deployment is not None
                            else None
                        ),
                        "reason_codes": reason_codes,
                        "snapshot_error_code": snapshot_error_code,
                    }
                )
            rows.append(
                {
                    "name": model.name,
                    "deployment_id": model.deployment_id,
                    "capabilities": sorted(model.capabilities),
                    "strict_replica_count": sum(
                        1 for node in nodes if node["strict_match"]
                    ),
                    "online_strict_replica_count": sum(
                        1
                        for node in nodes
                        if node["strict_match"] and node["online"]
                    ),
                    "eligible_replica_count": sum(
                        1 for node in nodes if node["eligible"]
                    ),
                    "nodes": nodes,
                }
            )
        return rows
