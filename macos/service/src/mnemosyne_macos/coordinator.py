"""Lease-based cross-engine model residency coordinator."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
import time
from typing import AsyncIterator, Awaitable, Callable, Mapping

from .engines.base import Deadline, EngineAdapter
from .fleet_protocol import Capacity, derive_macos_capacity
from .models import (
    EffectiveLoadIdentity,
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResolvedTarget,
    effective_load_identity,
)


class CoordinatorError(RuntimeError):
    pass


class QueueTimeout(CoordinatorError):
    pass


class QueueFull(CoordinatorError):
    """Admission was rejected before any engine inference work began."""


class CoordinatorState(StrEnum):
    IDLE = "idle"
    DRAINING = "draining"
    UNLOADING = "unloading"
    VERIFYING_EMPTY = "verifying_empty"
    LOADING = "loading"
    VERIFYING_TARGET = "verifying_target"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"


_TRANSITION_STATES = frozenset(
    {
        CoordinatorState.UNLOADING,
        CoordinatorState.VERIFYING_EMPTY,
        CoordinatorState.LOADING,
        CoordinatorState.VERIFYING_TARGET,
        CoordinatorState.DRAINING,
        CoordinatorState.STOPPING,
    }
)


@dataclass(frozen=True)
class CoordinatorStatus:
    state: CoordinatorState
    resident_alias: str | None
    resident_engine: EngineName | None
    resident_model: str | None
    epoch: int
    inflight: int
    queued: int
    diagnostic: str | None
    initialized: bool
    accepting: bool
    transition_target: str | None
    transition_engine: EngineName | None
    transition_model: str | None
    queued_by_deployment: Mapping[str, int]
    capacity: Capacity | None


@dataclass
class _Waiter:
    target: ResolvedTarget
    future: asyncio.Future[tuple[LoadedHandle, int]]
    cancelled: bool = False


class ModelLease:
    """A resident-model lease held through the complete response stream."""

    def __init__(
        self,
        coordinator: "ResidencyCoordinator",
        handle: LoadedHandle,
        epoch: int,
    ) -> None:
        self._coordinator = coordinator
        self.handle = handle
        self.epoch = epoch
        self._released = False
        self._release_task: asyncio.Task[None] | None = None

    def route(self, endpoint: Endpoint) -> ProxyRoute:
        adapter = self._coordinator.adapters[self.handle.target.key.engine]
        return adapter.route(self.handle, endpoint)

    async def release(self) -> None:
        if self._released:
            return
        if self._release_task is None:
            self._release_task = asyncio.create_task(
                self._coordinator._release(self.epoch),
                name=f"mnemosyne-model-lease-release-{self.epoch}",
            )
        try:
            await asyncio.shield(self._release_task)
        except asyncio.CancelledError:
            # The inner task remains alive and a later cleanup attempt can
            # await it. Never declare the lease released before the epoch
            # counter was actually decremented.
            raise
        except BaseException:
            self._release_task = None
            raise
        self._released = True

    async def abort(self) -> None:
        """Atomically close admission, release this lease, and unload its epoch."""

        if self._released:
            return
        if self._release_task is None:
            self._release_task = asyncio.create_task(
                self._coordinator._abort_epoch(self.epoch),
                name=f"mnemosyne-model-lease-abort-{self.epoch}",
            )
        try:
            await asyncio.shield(self._release_task)
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._release_task = None
            raise
        self._released = True

    async def __aenter__(self) -> "ModelLease":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release()


class ResidencyCoordinator:
    """Serializes engine transitions while allowing warm-target concurrency.

    FIFO waiters plus an epoch-tagged lease count ensure a queued target cannot
    be starved by new requests to the old target and an old response finalizer
    cannot decrement a newly loaded resident generation.
    """

    def __init__(
        self,
        adapters: Mapping[EngineName, EngineAdapter],
        *,
        queue_timeout_seconds: float = 300,
        transition_timeout_seconds: float = 900,
        cleanup_timeout_seconds: float = 60,
        shutdown_grace_seconds: float = 30,
        configured_max_concurrency: int | None = None,
        max_queue_depth: int = 128,
    ) -> None:
        self.adapters = dict(adapters)
        for engine, adapter in self.adapters.items():
            if engine != adapter.engine:
                raise ValueError(
                    f"adapter map key {engine} does not match adapter engine {adapter.engine}"
                )
        self.queue_timeout_seconds = queue_timeout_seconds
        self.transition_timeout_seconds = transition_timeout_seconds
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self.shutdown_grace_seconds = shutdown_grace_seconds
        if configured_max_concurrency is not None and configured_max_concurrency < 1:
            raise ValueError("configured_max_concurrency must be positive or null")
        if max_queue_depth < 1:
            raise ValueError("max_queue_depth must be positive")
        self.configured_max_concurrency = configured_max_concurrency
        self.max_queue_depth = max_queue_depth
        self._condition = asyncio.Condition()
        self._operation_lock = asyncio.Lock()
        self._queue: deque[_Waiter] = deque()
        self._resident: LoadedHandle | None = None
        self._epoch = 0
        self._inflight = 0
        self._state = CoordinatorState.IDLE
        self._diagnostic: str | None = None
        self._drive_task: asyncio.Task[None] | None = None
        self._transition_target: ResolvedTarget | None = None
        self._stopping = False
        self._initialized = False
        self._accepting = False
        self._last_used_monotonic = time.monotonic()

    def capacity_for(
        self,
        target: ResolvedTarget,
        *,
        active: int = 0,
        queued: int = 0,
        accepting: bool = True,
    ) -> Capacity:
        adapter = self.adapters.get(target.key.engine)
        hint = adapter.capacity_hint(target) if adapter is not None else None
        return derive_macos_capacity(
            target,
            configured_max_concurrency=self.configured_max_concurrency,
            adapter_limit=hint.limit if hint is not None else None,
            adapter_source=hint.source if hint is not None else None,
            adapter_confidence=hint.confidence if hint is not None else None,
            active=active,
            queued=queued,
            accepting=accepting,
        )

    async def initialize(self) -> None:
        """Fail closed unless every configured adapter confirms global empty."""
        async with self._condition:
            if self._stopping:
                raise CoordinatorError("coordinator is stopping")
            self._accepting = False
            self._state = CoordinatorState.UNLOADING
            self._diagnostic = None
        deadline = Deadline.after(self.transition_timeout_seconds)
        try:
            async with asyncio.timeout(self.transition_timeout_seconds):
                async with self._operation_lock:
                    for adapter in self.adapters.values():
                        snapshot = await adapter.validate_control(deadline=deadline)
                        self._validate_snapshot(adapter, snapshot)
                        if not snapshot.authoritative:
                            raise CoordinatorError(
                                f"{adapter.engine} control state is uncertain: "
                                f"{snapshot.diagnostic or snapshot.service_state}"
                            )
                    await self._unload_globally(deadline)
        except Exception as exc:
            async with self._condition:
                self._initialized = False
                self._accepting = False
                self._state = CoordinatorState.DEGRADED
                self._diagnostic = str(exc)
            raise
        async with self._condition:
            self._resident = None
            self._transition_target = None
            self._initialized = True
            self._accepting = True
            self._state = CoordinatorState.IDLE
            self._diagnostic = None
            self._last_used_monotonic = time.monotonic()

    async def acquire(
        self,
        target: ResolvedTarget,
        *,
        timeout_seconds: float | None = None,
    ) -> ModelLease:
        if target.key.engine not in self.adapters:
            raise CoordinatorError(f"engine {target.key.engine} is not configured")
        loop = asyncio.get_running_loop()
        waiter = _Waiter(target=target, future=loop.create_future())
        async with self._condition:
            if self._stopping:
                raise CoordinatorError("coordinator is stopping")
            if not self._initialized:
                raise CoordinatorError("coordinator is not initialized")
            if not self._accepting:
                raise CoordinatorError("coordinator is temporarily not accepting requests")
            self._purge_cancelled_locked()
            if (
                not self._queue
                and self._resident is not None
                and effective_load_identity(self._resident.target)
                == effective_load_identity(target)
                and self._state == CoordinatorState.READY
                and self._transition_target is None
            ):
                capacity = self.capacity_for(
                    self._resident.target,
                    active=self._inflight,
                    queued=0,
                )
                if capacity.available > 0:
                    lease_handle = replace(
                        self._resident,
                        target=target,
                        wire_model=target.wire_model,
                    )
                    self._inflight += 1
                    return ModelLease(self, lease_handle, self._epoch)
            if len(self._queue) >= self.max_queue_depth:
                raise QueueFull(
                    f"node admission queue is full ({self.max_queue_depth} waiters)"
                )
            self._queue.append(waiter)
            self._ensure_driver_locked()
            self._condition.notify_all()

        timeout = (
            self.queue_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        try:
            handle, epoch = await asyncio.wait_for(
                asyncio.shield(waiter.future), timeout=timeout
            )
            return ModelLease(self, handle, epoch)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            await self._cancel_waiter(waiter)
            if isinstance(exc, asyncio.TimeoutError):
                raise QueueTimeout(
                    f"timed out waiting for model '{target.alias}' after {timeout:g}s"
                ) from exc
            raise

    @asynccontextmanager
    async def lease(
        self,
        target: ResolvedTarget,
        *,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[ModelLease]:
        acquired = await self.acquire(target, timeout_seconds=timeout_seconds)
        try:
            yield acquired
        finally:
            await acquired.release()

    async def _cancel_waiter(self, waiter: _Waiter) -> None:
        async with self._condition:
            if waiter.cancelled:
                return
            waiter.cancelled = True
            if waiter.future.done() and not waiter.future.cancelled():
                try:
                    _handle, epoch = waiter.future.result()
                except Exception:
                    pass
                else:
                    self._release_locked(epoch)
            elif not waiter.future.done():
                waiter.future.cancel()
            self._condition.notify_all()

    def _ensure_driver_locked(self) -> None:
        if self._drive_task is None or self._drive_task.done():
            self._drive_task = asyncio.create_task(self._drive())

    def _purge_cancelled_locked(self) -> None:
        self._queue = deque(
            waiter
            for waiter in self._queue
            if not waiter.cancelled and not waiter.future.cancelled()
        )

    async def _drive(self) -> None:
        try:
            while True:
                transition_target: ResolvedTarget | None = None
                async with self._condition:
                    self._purge_cancelled_locked()
                    if self._stopping:
                        self._fail_all_locked(CoordinatorError("coordinator is stopping"))
                        return
                    if not self._initialized or not self._accepting:
                        self._fail_all_locked(
                            CoordinatorError("coordinator is temporarily not accepting requests")
                        )
                        return
                    if not self._queue:
                        self._transition_target = None
                        if self._resident is None:
                            if self._state != CoordinatorState.DEGRADED:
                                self._state = CoordinatorState.IDLE
                        elif self._state != CoordinatorState.DEGRADED:
                            self._state = CoordinatorState.READY
                        return

                    head = self._queue[0]
                    if (
                        self._resident is not None
                        and effective_load_identity(self._resident.target)
                        == effective_load_identity(head.target)
                    ):
                        self._transition_target = None
                        granted = self._grant_head_group_locked(
                            effective_load_identity(head.target)
                        )
                        if granted == 0:
                            if self._state != CoordinatorState.DEGRADED:
                                self._state = CoordinatorState.READY
                            await self._condition.wait()
                        continue

                    self._transition_target = head.target
                    if self._resident is not None and self._inflight > 0:
                        self._state = CoordinatorState.DRAINING
                        await self._condition.wait()
                        continue

                    transition_target = head.target

                assert transition_target is not None
                try:
                    handle = await self._transition(transition_target)
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    clean, cleanup_diagnostic = await self._cleanup_best_effort()
                    async with self._condition:
                        self._resident = None
                        self._state = CoordinatorState.DEGRADED
                        self._diagnostic = str(exc)
                        if cleanup_diagnostic:
                            self._diagnostic += f"; cleanup: {cleanup_diagnostic}"
                        if not clean:
                            self._initialized = False
                            self._accepting = False
                        self._transition_target = None
                        self._fail_head_group_locked(
                            effective_load_identity(transition_target),
                            exc,
                        )
                        self._condition.notify_all()
                    continue

                async with self._condition:
                    self._resident = handle
                    self._epoch += 1
                    self._transition_target = None
                    # A control-plane barrier or audit can fail closed in the
                    # narrow gap between verified load and publication. Keep
                    # the observed handle for reconciliation, but never reopen
                    # admission or erase its diagnostic from this stale turn.
                    if (
                        self._initialized
                        and self._accepting
                        and not self._stopping
                    ):
                        self._state = CoordinatorState.READY
                        self._diagnostic = None
                    self._condition.notify_all()
        finally:
            async with self._condition:
                if self._drive_task is asyncio.current_task():
                    self._drive_task = None
                    if self._queue and not self._stopping:
                        self._ensure_driver_locked()

    def _grant_head_group_locked(self, identity: EffectiveLoadIdentity) -> int:
        assert self._resident is not None
        capacity = self.capacity_for(
            self._resident.target,
            active=self._inflight,
            queued=len(self._queue),
        )
        available = capacity.available
        granted = 0
        while (
            available > 0
            and self._queue
            and effective_load_identity(self._queue[0].target) == identity
        ):
            waiter = self._queue.popleft()
            if waiter.cancelled or waiter.future.cancelled():
                continue
            lease_handle = replace(
                self._resident,
                target=waiter.target,
                wire_model=waiter.target.wire_model,
            )
            self._inflight += 1
            granted += 1
            available -= 1
            waiter.future.set_result((lease_handle, self._epoch))
        self._condition.notify_all()
        return granted

    def _fail_head_group_locked(
        self,
        identity: EffectiveLoadIdentity,
        exc: BaseException,
    ) -> None:
        while (
            self._queue
            and effective_load_identity(self._queue[0].target) == identity
        ):
            waiter = self._queue.popleft()
            if not waiter.future.done():
                waiter.future.set_exception(exc)

    def _fail_all_locked(self, exc: BaseException) -> None:
        while self._queue:
            waiter = self._queue.popleft()
            if not waiter.future.done():
                waiter.future.set_exception(exc)

    async def _transition(self, target: ResolvedTarget) -> LoadedHandle:
        deadline = Deadline.after(self.transition_timeout_seconds)
        try:
            async with asyncio.timeout(self.transition_timeout_seconds):
                async with self._operation_lock:
                    await self._publish_state(CoordinatorState.UNLOADING)
                    await self._unload_globally(deadline)
                    await self._publish_state(CoordinatorState.LOADING)
                    adapter = self.adapters[target.key.engine]
                    handle = await adapter.load(target, deadline=deadline)
                    await self._publish_state(CoordinatorState.VERIFYING_TARGET)
                    snapshots = await self._inspect_all(deadline)
                    residents = [
                        resident for snapshot in snapshots for resident in snapshot.residents
                    ]
                    if len(residents) != 1:
                        raise CoordinatorError(
                            f"post-load verification found {len(residents)} residents, expected one"
                        )
                    resident = residents[0]
                    if (
                        resident.engine != target.key.engine
                        or resident.canonical_model_id != target.key.canonical_model_id
                        or not resident.ready
                        or not resident.managed
                    ):
                        raise CoordinatorError(
                            "post-load verification found a different or unusable resident model"
                        )
                    return replace(handle, instance=resident)
        except TimeoutError as exc:
            raise CoordinatorError(
                f"transition to model '{target.alias}' timed out"
            ) from exc

    async def _inspect_all(self, deadline: Deadline) -> list[EngineSnapshot]:
        snapshots: list[EngineSnapshot] = []
        for adapter in self.adapters.values():
            snapshot = await adapter.inspect(deadline=deadline)
            self._validate_snapshot(adapter, snapshot)
            if not snapshot.authoritative:
                raise CoordinatorError(
                    f"{adapter.engine} resident state is uncertain: "
                    f"{snapshot.diagnostic or snapshot.service_state}"
                )
            snapshots.append(snapshot)
        return snapshots

    @staticmethod
    def _validate_snapshot(adapter: EngineAdapter, snapshot: EngineSnapshot) -> None:
        if snapshot.engine != adapter.engine:
            raise CoordinatorError(
                f"{adapter.engine} returned a snapshot for {snapshot.engine}"
            )
        if any(resident.engine != adapter.engine for resident in snapshot.residents):
            raise CoordinatorError(
                f"{adapter.engine} returned a resident owned by a different engine"
            )

    async def _unload_globally(self, deadline: Deadline) -> None:
        snapshots = await self._inspect_all(deadline)
        for snapshot in snapshots:
            if snapshot.residents:
                await self.adapters[snapshot.engine].unload_all(deadline=deadline)
        await self._publish_state(CoordinatorState.VERIFYING_EMPTY)
        after = await self._inspect_all(deadline)
        remaining = [resident for snapshot in after for resident in snapshot.residents]
        if remaining:
            names = [f"{resident.engine}:{resident.canonical_model_id}" for resident in remaining]
            raise CoordinatorError(f"global unload left resident models: {names}")

    async def _cleanup_best_effort(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[bool, str | None]:
        timeout = self.cleanup_timeout_seconds
        if timeout_seconds is not None:
            timeout = min(timeout, max(0.0, timeout_seconds))
        if timeout <= 0:
            return False, "cleanup deadline expired"
        deadline = Deadline.after(timeout)
        diagnostics: list[str] = []
        try:
            async with asyncio.timeout(timeout):
                for adapter in self.adapters.values():
                    try:
                        snapshot = await adapter.inspect(deadline=deadline)
                        if snapshot.authoritative and snapshot.residents:
                            await adapter.unload_all(deadline=deadline)
                        elif not snapshot.authoritative:
                            diagnostics.append(
                                f"{adapter.engine}: "
                                f"{snapshot.diagnostic or snapshot.service_state}"
                            )
                    except Exception as exc:
                        diagnostics.append(f"{adapter.engine}: {exc}")
                try:
                    snapshots = await self._inspect_all(deadline)
                    remaining = [resident for item in snapshots for resident in item.residents]
                    if remaining:
                        diagnostics.append(
                            "remaining residents: "
                            + ", ".join(
                                f"{resident.engine}:{resident.canonical_model_id}"
                                for resident in remaining
                            )
                        )
                except Exception as exc:
                    diagnostics.append(str(exc))
        except TimeoutError:
            diagnostics.append("cleanup deadline expired")
        return not diagnostics, "; ".join(diagnostics) or None

    async def _publish_state(self, state: CoordinatorState) -> None:
        async with self._condition:
            self._state = state
            self._condition.notify_all()

    async def _release(self, epoch: int) -> None:
        async with self._condition:
            self._release_locked(epoch)
            self._condition.notify_all()
            if self._queue:
                self._ensure_driver_locked()

    def _release_locked(self, epoch: int) -> None:
        if epoch != self._epoch:
            return
        if self._inflight <= 0:
            return
        self._inflight -= 1
        if self._inflight == 0:
            self._last_used_monotonic = time.monotonic()

    async def _abort_epoch(self, epoch: int) -> None:
        """Release one epoch lease while atomically fencing later admission."""

        deadline = Deadline.after(self.transition_timeout_seconds)
        async with self._condition:
            if epoch != self._epoch:
                return
            self._accepting = False
            self._fail_all_locked(CoordinatorError("resident request abort in progress"))
            self._release_locked(epoch)
            self._condition.notify_all()
            while self._inflight > 0:
                budget = deadline.remaining()
                if budget <= 0:
                    error = QueueTimeout(
                        "timed out draining active model leases after request abort"
                    )
                    self._initialized = False
                    self._state = CoordinatorState.DEGRADED
                    self._diagnostic = str(error)
                    raise error
                self._state = CoordinatorState.DRAINING
                try:
                    async with asyncio.timeout(budget):
                        await self._condition.wait()
                except TimeoutError as exc:
                    error = QueueTimeout(
                        "timed out draining active model leases after request abort"
                    )
                    self._initialized = False
                    self._state = CoordinatorState.DEGRADED
                    self._diagnostic = str(error)
                    raise error from exc
            drive_task = self._drive_task
        try:
            await self._await_driver_until(
                drive_task,
                deadline,
                operation="request abort",
            )
            budget = deadline.remaining()
            if budget <= 0:
                raise QueueTimeout("request abort transition deadline expired")
            async with asyncio.timeout(budget):
                async with self._operation_lock:
                    await self._publish_state(CoordinatorState.UNLOADING)
                    await self._unload_globally(deadline)
        except Exception as exc:
            async with self._condition:
                self._initialized = False
                self._accepting = False
                self._state = CoordinatorState.DEGRADED
                self._diagnostic = str(exc)
                self._condition.notify_all()
            raise
        async with self._condition:
            self._resident = None
            self._transition_target = None
            self._initialized = True
            self._accepting = True
            self._state = CoordinatorState.IDLE
            self._diagnostic = None
            self._last_used_monotonic = time.monotonic()
            self._condition.notify_all()

    async def status(self) -> CoordinatorStatus:
        async with self._condition:
            resident = self._resident
            queued_by_alias: dict[str, int] = {}
            queued = 0
            for waiter in self._queue:
                if waiter.cancelled or waiter.future.cancelled():
                    continue
                queued += 1
                queued_by_alias[waiter.target.alias] = (
                    queued_by_alias.get(waiter.target.alias, 0) + 1
                )
            resident_accepting = bool(
                resident is not None
                and self._initialized
                and self._accepting
                and not self._stopping
                and self._state == CoordinatorState.READY
                and (
                    not self._queue
                    or effective_load_identity(self._queue[0].target)
                    == effective_load_identity(resident.target)
                )
            )
            capacity = (
                self.capacity_for(
                    resident.target,
                    active=self._inflight,
                    queued=queued,
                    accepting=resident_accepting,
                )
                if resident is not None
                else None
            )
            return CoordinatorStatus(
                state=self._state,
                resident_alias=resident.target.alias if resident else None,
                resident_engine=resident.target.key.engine if resident else None,
                resident_model=(
                    resident.target.key.canonical_model_id if resident else None
                ),
                epoch=self._epoch,
                inflight=self._inflight,
                queued=queued,
                diagnostic=self._diagnostic,
                initialized=self._initialized,
                accepting=bool(
                    self._initialized and self._accepting and not self._stopping
                ),
                transition_target=(
                    self._transition_target.alias
                    if self._transition_target is not None
                    else None
                ),
                transition_engine=(
                    self._transition_target.key.engine
                    if self._transition_target is not None
                    else None
                ),
                transition_model=(
                    self._transition_target.key.canonical_model_id
                    if self._transition_target is not None
                    else None
                ),
                queued_by_deployment=queued_by_alias,
                capacity=capacity,
            )

    async def _await_driver_until(
        self,
        drive_task: asyncio.Task[None] | None,
        deadline: Deadline,
        *,
        operation: str,
    ) -> None:
        if drive_task is None or drive_task is asyncio.current_task():
            return
        if not drive_task.done():
            budget = deadline.remaining()
            if budget > 0:
                done, _pending = await asyncio.wait(
                    {drive_task}, timeout=budget
                )
            else:
                done = set()
            if not done:
                drive_task.cancel()
                raise QueueTimeout(
                    f"timed out waiting for transition driver during {operation}"
                )
        if drive_task.cancelled():
            raise CoordinatorError(
                f"transition driver was cancelled during {operation}"
            )
        error = drive_task.exception()
        if error is not None:
            raise CoordinatorError(
                f"transition driver failed during {operation}: {error}"
            ) from error

    async def unload(self) -> None:
        """Drain active leases and establish a globally empty state."""
        deadline = Deadline.after(self.transition_timeout_seconds)
        async with self._condition:
            self._accepting = False
            self._fail_all_locked(CoordinatorError("manual unload in progress"))
            self._condition.notify_all()
            while self._inflight > 0:
                budget = deadline.remaining()
                if budget <= 0:
                    self._accepting = True
                    self._state = (
                        CoordinatorState.READY
                        if self._resident
                        else CoordinatorState.IDLE
                    )
                    raise QueueTimeout("timed out draining active model leases")
                try:
                    async with asyncio.timeout(budget):
                        while self._inflight > 0:
                            self._state = CoordinatorState.DRAINING
                            await self._condition.wait()
                except TimeoutError as exc:
                    self._accepting = True
                    self._state = (
                        CoordinatorState.READY
                        if self._resident
                        else CoordinatorState.IDLE
                    )
                    raise QueueTimeout(
                        "timed out draining active model leases"
                    ) from exc
            drive_task = self._drive_task
        try:
            await self._await_driver_until(
                drive_task,
                deadline,
                operation="manual unload",
            )
            budget = deadline.remaining()
            if budget <= 0:
                raise QueueTimeout("manual unload transition deadline expired")
            async with asyncio.timeout(budget):
                async with self._operation_lock:
                    await self._publish_state(CoordinatorState.UNLOADING)
                    await self._unload_globally(deadline)
        except TimeoutError as exc:
            error = QueueTimeout("manual unload transition deadline expired")
            async with self._condition:
                self._initialized = False
                self._accepting = False
                self._state = CoordinatorState.DEGRADED
                self._diagnostic = str(error)
                self._condition.notify_all()
            raise error from exc
        except Exception as exc:
            async with self._condition:
                self._initialized = False
                self._accepting = False
                self._state = CoordinatorState.DEGRADED
                self._diagnostic = str(exc)
                self._condition.notify_all()
            raise
        async with self._condition:
            self._resident = None
            self._transition_target = None
            self._initialized = True
            self._accepting = True
            self._state = CoordinatorState.IDLE
            self._diagnostic = None
            self._last_used_monotonic = time.monotonic()
            self._condition.notify_all()

    async def run_empty_maintenance(
        self,
        operation: Callable[[Deadline], Awaitable[None]],
        *,
        name: str,
    ) -> None:
        """Drain, prove global empty, and hold admission closed for maintenance."""

        deadline = Deadline.after(self.transition_timeout_seconds)
        try:
            async with self._condition:
                self._accepting = False
                self._fail_all_locked(CoordinatorError(f"{name} in progress"))
                self._condition.notify_all()
                while self._inflight > 0:
                    budget = deadline.remaining()
                    if budget <= 0:
                        raise QueueTimeout(f"timed out draining leases for {name}")
                    try:
                        async with asyncio.timeout(budget):
                            while self._inflight > 0:
                                self._state = CoordinatorState.DRAINING
                                await self._condition.wait()
                    except TimeoutError as exc:
                        raise QueueTimeout(
                            f"timed out draining leases for {name}"
                        ) from exc
                drive_task = self._drive_task
            await self._await_driver_until(drive_task, deadline, operation=name)
            budget = deadline.remaining()
            if budget <= 0:
                raise QueueTimeout(f"{name} transition deadline expired")
            async with asyncio.timeout(budget):
                async with self._operation_lock:
                    await self._publish_state(CoordinatorState.UNLOADING)
                    await self._unload_globally(deadline)
                    await operation(deadline)
                    snapshots = await self._inspect_all(deadline)
                    residents = [item for snapshot in snapshots for item in snapshot.residents]
                    if residents:
                        raise CoordinatorError(f"{name} left a resident model")
        except Exception as exc:
            async with self._condition:
                self._initialized = False
                self._accepting = False
                self._state = CoordinatorState.DEGRADED
                self._diagnostic = str(exc)
                self._condition.notify_all()
            raise
        async with self._condition:
            self._resident = None
            self._transition_target = None
            self._initialized = True
            self._accepting = True
            self._state = CoordinatorState.IDLE
            self._diagnostic = None
            self._last_used_monotonic = time.monotonic()
            self._condition.notify_all()

    async def reconcile(self) -> bool:
        """Verify cached residency and safely clear any external drift.

        Returns ``True`` when the cached state already matched engine-observed
        state and ``False`` when reconciliation had to unload drift.
        """
        deadline = Deadline.after(self.transition_timeout_seconds)
        async with self._condition:
            if self._stopping:
                raise CoordinatorError("coordinator is stopping")
            self._accepting = False
            # Reconciliation is a lifecycle barrier. Existing queued callers
            # must not become fresh leases after the drain has completed.
            self._fail_all_locked(CoordinatorError("reconciliation in progress"))
            self._condition.notify_all()
            while self._inflight > 0:
                budget = deadline.remaining()
                if budget <= 0:
                    self._accepting = True
                    self._state = (
                        CoordinatorState.READY
                        if self._resident
                        else CoordinatorState.IDLE
                    )
                    raise QueueTimeout(
                        "timed out draining leases for reconciliation"
                    )
                try:
                    async with asyncio.timeout(budget):
                        while self._inflight > 0:
                            self._state = CoordinatorState.DRAINING
                            await self._condition.wait()
                except TimeoutError as exc:
                    self._accepting = True
                    self._state = (
                        CoordinatorState.READY
                        if self._resident
                        else CoordinatorState.IDLE
                    )
                    raise QueueTimeout(
                        "timed out draining leases for reconciliation"
                    ) from exc
            drive_task = self._drive_task
        # A driver may already have crossed the queue boundary and be inside
        # a transition. Let it publish (or clean up) before comparing cached
        # and engine-observed state, avoiding a verify-before-publish race.
        try:
            await self._await_driver_until(
                drive_task,
                deadline,
                operation="reconciliation",
            )
            budget = deadline.remaining()
            if budget <= 0:
                raise QueueTimeout("reconciliation transition deadline expired")
            async with asyncio.timeout(budget):
                async with self._operation_lock:
                    snapshots = await self._inspect_all(deadline)
                    residents = [r for snapshot in snapshots for r in snapshot.residents]
                    expected = self._resident
                    matches = (
                        (expected is None and not residents)
                        or (
                            expected is not None
                            and len(residents) == 1
                            and residents[0].engine == expected.target.key.engine
                            and residents[0].canonical_model_id
                            == expected.target.key.canonical_model_id
                            and residents[0].ready
                            and residents[0].managed
                        )
                    )
                    if not matches:
                        await self._unload_globally(deadline)
        except TimeoutError as exc:
            error = QueueTimeout("reconciliation transition deadline expired")
            async with self._condition:
                self._initialized = False
                self._accepting = False
                self._state = CoordinatorState.DEGRADED
                self._diagnostic = str(error)
                self._condition.notify_all()
            raise error from exc
        except Exception as exc:
            async with self._condition:
                self._initialized = False
                self._accepting = False
                self._state = CoordinatorState.DEGRADED
                self._diagnostic = str(exc)
                self._condition.notify_all()
            raise
        async with self._condition:
            self._transition_target = None
            if not matches:
                self._resident = None
                self._state = CoordinatorState.IDLE
            elif self._resident is None:
                self._state = CoordinatorState.IDLE
            else:
                self._state = CoordinatorState.READY
            self._initialized = True
            self._accepting = True
            self._diagnostic = None
            self._condition.notify_all()
        return matches

    async def audit(self, *, repair_when_quiescent: bool = True) -> bool:
        """Compare cached and observed residency without interrupting a lease.

        Drift fails closed immediately. When no request or waiter is active,
        the default behavior also routes repair through :meth:`reconcile`.
        """
        # A transition owns the operation lock for its full load/unload
        # deadline. Treat its published state as authoritative-in-progress so
        # a short periodic audit never times out behind a legitimate long model
        # load. The post-timeout check below closes the race where a transition
        # begins after this initial observation but before audit takes the lock.
        async with self._condition:
            if self._state in _TRANSITION_STATES:
                return True
        try:
            deadline = Deadline.after(self.cleanup_timeout_seconds)
            async with asyncio.timeout(self.cleanup_timeout_seconds):
                async with self._operation_lock:
                    snapshots = await self._inspect_all(deadline)
        except Exception as exc:
            async with self._condition:
                if isinstance(exc, TimeoutError) and self._state in _TRANSITION_STATES:
                    return True
                self._initialized = False
                self._accepting = False
                self._state = CoordinatorState.DEGRADED
                self._diagnostic = f"residency audit failed: {exc}"
                self._fail_all_locked(
                    CoordinatorError("residency audit could not establish engine state")
                )
                self._condition.notify_all()
            raise

        async with self._condition:
            # A transition can publish its verified handle just after it drops
            # the operation lock. Let the driver complete that publication.
            if self._state in _TRANSITION_STATES:
                return True
            residents = [r for snapshot in snapshots for r in snapshot.residents]
            expected = self._resident
            matches = (
                (expected is None and not residents)
                or (
                    expected is not None
                    and len(residents) == 1
                    and residents[0].engine == expected.target.key.engine
                    and residents[0].canonical_model_id
                    == expected.target.key.canonical_model_id
                    and residents[0].ready
                    and residents[0].managed
                )
            )
            if matches:
                return True
            self._initialized = False
            self._accepting = False
            self._state = CoordinatorState.DEGRADED
            self._diagnostic = "engine-observed residency drifted from coordinator state"
            self._fail_all_locked(
                CoordinatorError("engine-observed residency drifted from coordinator state")
            )
            repair = repair_when_quiescent and self._inflight == 0 and not self._queue
            self._condition.notify_all()
        if repair:
            await self.reconcile()
        return False

    async def evict_if_idle(self, idle_seconds: float) -> bool:
        async with self._condition:
            eligible = (
                self._resident is not None
                and self._inflight == 0
                and not self._queue
                and self._accepting
                and time.monotonic() - self._last_used_monotonic >= idle_seconds
            )
            if not eligible:
                return False
            self._accepting = False
        await self.unload()
        return True

    async def shutdown(self) -> None:
        loop = asyncio.get_running_loop()
        shutdown_deadline = loop.time() + self.shutdown_grace_seconds
        diagnostics: list[str] = []

        def remaining() -> float:
            return max(0.0, shutdown_deadline - loop.time())

        async with self._condition:
            self._stopping = True
            self._accepting = False
            self._state = CoordinatorState.STOPPING
            self._fail_all_locked(CoordinatorError("coordinator is stopping"))
            self._condition.notify_all()
            drive_task = self._drive_task
            if (
                drive_task is not None
                and drive_task is not asyncio.current_task()
                and not drive_task.done()
            ):
                # A transition owns the operation lock and may otherwise run
                # until transition_timeout_seconds. Cancellation first lets
                # adapters perform their own ownership-safe rollback (notably
                # DS4's exact-process cleanup) before global cleanup runs.
                drive_task.cancel()
            while self._inflight > 0:
                budget = remaining()
                if budget <= 0:
                    diagnostics.append(
                        "shutdown grace expired with active model leases"
                    )
                    break
                try:
                    async with asyncio.timeout(budget):
                        await self._condition.wait()
                except TimeoutError:
                    diagnostics.append(
                        "shutdown grace expired with active model leases"
                    )
                    break

        if drive_task is not None and drive_task is not asyncio.current_task():
            if not drive_task.done() and remaining() > 0:
                done, _pending = await asyncio.wait(
                    {drive_task}, timeout=remaining()
                )
                if not done:
                    diagnostics.append(
                        "shutdown grace expired waiting for the transition driver"
                    )
            if not drive_task.done():
                # A second cancellation interrupts an adapter rollback that
                # used the first cancellation as its cue. Never signal a PID
                # here; DS4 remains solely responsible for validating process
                # identity before either cancellation cleanup or aclose.
                drive_task.cancel()

        async def cleanup() -> tuple[bool, str | None]:
            async with self._operation_lock:
                return await self._cleanup_best_effort(
                    timeout_seconds=remaining()
                )

        cleanup_task = asyncio.create_task(cleanup())
        if remaining() > 0:
            done, _pending = await asyncio.wait(
                {cleanup_task}, timeout=remaining()
            )
        else:
            done = set()
        if cleanup_task in done:
            try:
                _clean, cleanup_diagnostic = cleanup_task.result()
            except asyncio.CancelledError:
                diagnostics.append("shutdown cleanup was cancelled")
            except Exception as exc:
                diagnostics.append(f"shutdown cleanup failed: {exc}")
            else:
                if cleanup_diagnostic:
                    diagnostics.append(f"shutdown cleanup: {cleanup_diagnostic}")
        else:
            cleanup_task.cancel()
            diagnostics.append("shutdown grace expired during global cleanup")

        close_tasks = {
            asyncio.create_task(adapter.aclose()): adapter.engine
            for adapter in self.adapters.values()
        }
        if close_tasks and remaining() > 0:
            closed, pending_closes = await asyncio.wait(
                close_tasks, timeout=remaining()
            )
        else:
            closed, pending_closes = set(), set(close_tasks)
        for task in closed:
            try:
                task.result()
            except asyncio.CancelledError:
                diagnostics.append(
                    f"{close_tasks[task]} adapter close was cancelled"
                )
            except Exception as exc:
                diagnostics.append(
                    f"{close_tasks[task]} adapter close failed: {exc}"
                )
        for task in pending_closes:
            task.cancel()
            diagnostics.append(
                f"shutdown grace expired closing {close_tasks[task]} adapter"
            )

        async with self._condition:
            self._resident = None
            self._transition_target = None
            self._initialized = False
            self._state = CoordinatorState.STOPPING
            if diagnostics:
                self._diagnostic = "; ".join(diagnostics)
            self._condition.notify_all()
