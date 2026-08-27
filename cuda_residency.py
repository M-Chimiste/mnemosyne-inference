"""FIFO, epoch-tagged residency and admission control for the CUDA manager.

The coordinator owns the safety boundary between HTTP request lifetimes and
the single manager-owned engine process.  It deliberately knows nothing about
FastAPI or engine command lines: callers provide start/stop/capacity callbacks
and hold the returned lease until the complete response body or stream closes.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass
from enum import StrEnum
import inspect
import time
from typing import Any, Awaitable, Callable, Deque


class ResidencyError(RuntimeError):
    """Base class for safe, pre-inference coordinator failures."""


class QueueFull(ResidencyError):
    """The bounded local admission queue has no free slot."""


class QueueTimeout(ResidencyError):
    """The caller's bounded admission/drain deadline expired."""


class NotAccepting(ResidencyError):
    """Admission is closed for maintenance, shutdown, or degraded state."""


class CoordinatorState(StrEnum):
    IDLE = "idle"
    DRAINING = "draining"
    UNLOADING = "unloading"
    LOADING = "loading"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"


@dataclass(frozen=True)
class CapacitySpec:
    derived_limit: int
    source: str
    confidence: str

    def __post_init__(self) -> None:
        if self.derived_limit < 1:
            raise ValueError("derived_limit must be positive")


@dataclass(frozen=True)
class CoordinatorStatus:
    state: CoordinatorState
    resident_profile: Any | None
    resident_deployment_id: str | None
    epoch: int
    active: int
    queued: int
    queue_limit: int
    queued_by_deployment: dict[str, int]
    transition_target: str | None
    accepting: bool
    authoritative: bool
    diagnostic_code: str | None
    configured_max_concurrency: int | None
    capacity: CapacitySpec
    sequence: int

    @property
    def effective_limit(self) -> int:
        configured = self.configured_max_concurrency
        if configured is None:
            return self.capacity.derived_limit
        return min(self.capacity.derived_limit, configured)


@dataclass
class _Waiter:
    profile: Any
    deployment_id: str
    future: asyncio.Future[tuple[Any, int]]
    cancelled: bool = False


class ModelLease:
    """One epoch permit held through the complete upstream response."""

    def __init__(
        self,
        coordinator: "CudaResidencyCoordinator",
        *,
        resident_profile: Any,
        requested_profile: Any,
        deployment_id: str,
        epoch: int,
    ) -> None:
        self._coordinator = coordinator
        self.resident_profile = resident_profile
        self.requested_profile = requested_profile
        self.deployment_id = deployment_id
        self.epoch = epoch
        self._released = False
        self._release_task: asyncio.Task[None] | None = None

    async def release(self) -> None:
        """Release exactly once, even if the outer cleanup is cancelled."""

        if self._released:
            return
        if self._release_task is None:
            self._release_task = asyncio.create_task(
                self._coordinator._release(self.epoch),
                name=f"cuda-residency-release-{self.epoch}",
            )
        try:
            await asyncio.shield(self._release_task)
        except asyncio.CancelledError:
            # The shielded task remains alive. A later cleanup attempt can
            # await it; never claim release before the epoch count changed.
            raise
        except BaseException:
            self._release_task = None
            raise
        self._released = True


class CudaResidencyCoordinator:
    """Serialize model transitions while permitting bounded warm concurrency."""

    def __init__(
        self,
        *,
        start_engine: Callable[[Any], Awaitable[None]],
        stop_engine: Callable[[], Any],
        derive_capacity: Callable[[Any], CapacitySpec | Awaitable[CapacitySpec]],
        configured_max_concurrency: int | None,
        max_queue_depth: int,
        on_inflight_changed: Callable[[int], None] | None = None,
        on_transition_changed: Callable[[Any | None], None] | None = None,
    ) -> None:
        if configured_max_concurrency is not None and configured_max_concurrency < 1:
            raise ValueError("configured_max_concurrency must be positive or null")
        if max_queue_depth < 1:
            raise ValueError("max_queue_depth must be at least one")
        self._start_engine = start_engine
        self._stop_engine = stop_engine
        self._derive_capacity = derive_capacity
        self._on_inflight_changed = on_inflight_changed
        self._on_transition_changed = on_transition_changed
        self._condition = asyncio.Condition()
        self._operation_lock = asyncio.Lock()
        # Serializes the *complete* admission-close/drain/stop/reopen barrier.
        # The narrower operation lock alone is insufficient: one maintenance
        # caller could otherwise reopen admission while a second caller was
        # waiting to stop the engine.
        self._maintenance_lock = asyncio.Lock()
        self._queue: Deque[_Waiter] = deque()
        self._resident_profile: Any | None = None
        self._resident_deployment_id: str | None = None
        self._capacity = CapacitySpec(1, "no-resident-conservative", "conservative")
        self._configured_max_concurrency = configured_max_concurrency
        self._max_queue_depth = max_queue_depth
        self._epoch = 0
        self._active = 0
        self._state = CoordinatorState.IDLE
        self._diagnostic_code: str | None = None
        self._transition_target: str | None = None
        self._driver_task: asyncio.Task[None] | None = None
        self._accepting = True
        self._authoritative = True
        self._stopping = False
        self._last_used_monotonic = time.monotonic()
        self._sequence = 0

    def _bump_locked(self) -> None:
        self._sequence += 1

    def _set_state_locked(
        self,
        state: CoordinatorState,
        *,
        transition_target: str | None = None,
        diagnostic_code: str | None = None,
    ) -> None:
        changed = (
            self._state != state
            or self._transition_target != transition_target
            or self._diagnostic_code != diagnostic_code
        )
        self._state = state
        self._transition_target = transition_target
        self._diagnostic_code = diagnostic_code
        if changed:
            self._bump_locked()
        if self._on_transition_changed is not None:
            profile = None
            if transition_target is not None:
                for waiter in self._queue:
                    if waiter.deployment_id == transition_target and not waiter.cancelled:
                        profile = waiter.profile
                        break
            self._on_transition_changed(profile)

    def _effective_limit_locked(self) -> int:
        configured = self._configured_max_concurrency
        if configured is None:
            return self._capacity.derived_limit
        return min(self._capacity.derived_limit, configured)

    def _notify_inflight_locked(self) -> None:
        if self._on_inflight_changed is not None:
            self._on_inflight_changed(self._active)

    async def reconfigure(
        self,
        *,
        configured_max_concurrency: int | None,
        max_queue_depth: int,
    ) -> None:
        if configured_max_concurrency is not None and configured_max_concurrency < 1:
            raise ValueError("configured_max_concurrency must be positive or null")
        if max_queue_depth < 1:
            raise ValueError("max_queue_depth must be at least one")
        async with self._condition:
            self._configured_max_concurrency = configured_max_concurrency
            self._max_queue_depth = max_queue_depth
            self._bump_locked()
            self._ensure_driver_locked()
            self._condition.notify_all()

    async def acquire(
        self,
        profile: Any,
        deployment_id: str,
        *,
        timeout_seconds: float,
    ) -> ModelLease:
        """Acquire a permit for one strict deployment identity."""

        loop = asyncio.get_running_loop()
        waiter = _Waiter(
            profile=profile,
            deployment_id=deployment_id,
            future=loop.create_future(),
        )
        async with self._condition:
            self._assert_accepting_locked()
            self._purge_cancelled_locked()
            if (
                not self._queue
                and self._resident_deployment_id == deployment_id
                and self._state == CoordinatorState.READY
                and self._active < self._effective_limit_locked()
            ):
                self._active += 1
                self._bump_locked()
                self._notify_inflight_locked()
                assert self._resident_profile is not None
                return ModelLease(
                    self,
                    resident_profile=self._resident_profile,
                    requested_profile=profile,
                    deployment_id=deployment_id,
                    epoch=self._epoch,
                )
            if len(self._queue) >= self._max_queue_depth:
                raise QueueFull(
                    f"node admission queue is full ({self._max_queue_depth} waiters)"
                )
            self._queue.append(waiter)
            self._bump_locked()
            self._ensure_driver_locked()
            self._condition.notify_all()

        try:
            resident_profile, epoch = await asyncio.wait_for(
                asyncio.shield(waiter.future),
                timeout=max(0.0, timeout_seconds),
            )
            return ModelLease(
                self,
                resident_profile=resident_profile,
                requested_profile=profile,
                deployment_id=deployment_id,
                epoch=epoch,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            await self._cancel_waiter(waiter)
            if isinstance(exc, asyncio.TimeoutError):
                raise QueueTimeout(
                    f"timed out waiting for deployment after {timeout_seconds:g}s"
                ) from exc
            raise

    async def acquire_current(self) -> ModelLease:
        """Acquire the current resident only when it is immediately available.

        Requests without a model identifier must never queue behind a switch
        and later cause the former model to be reloaded.
        """

        async with self._condition:
            self._assert_accepting_locked()
            if (
                self._resident_profile is None
                or self._resident_deployment_id is None
                or self._queue
                or self._state != CoordinatorState.READY
                or self._active >= self._effective_limit_locked()
            ):
                raise QueueFull("current resident is not immediately admissible")
            self._active += 1
            self._bump_locked()
            self._notify_inflight_locked()
            return ModelLease(
                self,
                resident_profile=self._resident_profile,
                requested_profile=self._resident_profile,
                deployment_id=self._resident_deployment_id,
                epoch=self._epoch,
            )

    def _assert_accepting_locked(self) -> None:
        if self._stopping:
            raise NotAccepting("coordinator is stopping")
        if not self._accepting or not self._authoritative:
            raise NotAccepting("coordinator is not accepting requests")

    async def _cancel_waiter(self, waiter: _Waiter) -> None:
        async with self._condition:
            if waiter.cancelled:
                return
            waiter.cancelled = True
            if waiter.future.done() and not waiter.future.cancelled():
                try:
                    _profile, epoch = waiter.future.result()
                except Exception:
                    pass
                else:
                    self._release_locked(epoch)
            elif not waiter.future.done():
                waiter.future.cancel()
            self._purge_cancelled_locked()
            self._bump_locked()
            self._condition.notify_all()

    def _purge_cancelled_locked(self) -> None:
        self._queue = deque(
            waiter
            for waiter in self._queue
            if not waiter.cancelled and not waiter.future.cancelled()
        )

    def _ensure_driver_locked(self) -> None:
        if self._driver_task is None or self._driver_task.done():
            self._driver_task = asyncio.create_task(
                self._drive(),
                name="cuda-residency-driver",
            )

    async def _drive(self) -> None:
        try:
            while True:
                transition_profile: Any | None = None
                transition_id: str | None = None
                async with self._condition:
                    self._purge_cancelled_locked()
                    if not self._accepting or not self._authoritative or self._stopping:
                        self._fail_all_locked(NotAccepting("coordinator is not accepting"))
                        return
                    if not self._queue:
                        self._set_state_locked(
                            CoordinatorState.READY
                            if self._resident_profile is not None
                            else CoordinatorState.IDLE
                        )
                        return

                    head = self._queue[0]
                    if self._resident_deployment_id == head.deployment_id:
                        granted = self._grant_head_locked(head.deployment_id)
                        if granted:
                            continue
                        self._set_state_locked(CoordinatorState.READY)
                        await self._condition.wait()
                        continue

                    if self._active > 0:
                        self._set_state_locked(
                            CoordinatorState.DRAINING,
                            transition_target=head.deployment_id,
                        )
                        await self._condition.wait()
                        continue

                    transition_profile = head.profile
                    transition_id = head.deployment_id
                    self._set_state_locked(
                        CoordinatorState.UNLOADING,
                        transition_target=transition_id,
                    )

                assert transition_profile is not None and transition_id is not None
                try:
                    capacity = await self._transition(transition_profile, transition_id)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    clean = await self._cleanup_after_failed_transition()
                    async with self._condition:
                        self._resident_profile = None
                        self._resident_deployment_id = None
                        self._capacity = CapacitySpec(
                            1, "no-resident-conservative", "conservative"
                        )
                        self._transition_target = None
                        self._authoritative = clean
                        self._accepting = clean and not self._stopping
                        self._set_state_locked(
                            CoordinatorState.IDLE if clean else CoordinatorState.DEGRADED,
                            diagnostic_code=None if clean else "transition_cleanup_failed",
                        )
                        self._fail_head_locked(transition_id, exc)
                        self._bump_locked()
                        self._condition.notify_all()
                    continue

                async with self._condition:
                    self._resident_profile = transition_profile
                    self._resident_deployment_id = transition_id
                    self._capacity = capacity
                    self._epoch += 1
                    if (
                        self._accepting
                        and self._authoritative
                        and not self._stopping
                    ):
                        self._set_state_locked(CoordinatorState.READY)
                    else:
                        self._set_state_locked(
                            CoordinatorState.STOPPING
                            if self._stopping
                            else CoordinatorState.DEGRADED,
                            diagnostic_code=self._diagnostic_code
                            or "admission_barrier_incomplete",
                        )
                    self._bump_locked()
                    self._condition.notify_all()
        finally:
            async with self._condition:
                if self._driver_task is asyncio.current_task():
                    self._driver_task = None
                    if self._queue and self._accepting and not self._stopping:
                        self._ensure_driver_locked()

    async def _transition(
        self,
        profile: Any,
        deployment_id: str,
    ) -> CapacitySpec:
        async with self._operation_lock:
            await self._call_stop()
            async with self._condition:
                self._set_state_locked(
                    CoordinatorState.LOADING,
                    transition_target=deployment_id,
                )
                self._condition.notify_all()
            await self._start_engine(profile)
            result = self._derive_capacity(profile)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, CapacitySpec):
                raise TypeError("derive_capacity callback must return CapacitySpec")
            return result

    async def _call_stop(self) -> None:
        result = self._stop_engine()
        if inspect.isawaitable(result):
            await result

    async def _cleanup_after_failed_transition(self) -> bool:
        try:
            async with self._operation_lock:
                await self._call_stop()
        except BaseException:
            return False
        return True

    def _grant_head_locked(self, deployment_id: str) -> int:
        assert self._resident_profile is not None
        granted = 0
        limit = self._effective_limit_locked()
        while (
            self._queue
            and self._queue[0].deployment_id == deployment_id
            and self._active < limit
        ):
            waiter = self._queue.popleft()
            if waiter.cancelled or waiter.future.cancelled():
                continue
            self._active += 1
            granted += 1
            waiter.future.set_result((self._resident_profile, self._epoch))
        if granted:
            self._bump_locked()
            self._notify_inflight_locked()
            self._condition.notify_all()
        return granted

    def _fail_head_locked(self, deployment_id: str, exc: BaseException) -> None:
        while self._queue and self._queue[0].deployment_id == deployment_id:
            waiter = self._queue.popleft()
            if not waiter.future.done():
                waiter.future.set_exception(exc)

    def _fail_all_locked(self, exc: BaseException) -> None:
        while self._queue:
            waiter = self._queue.popleft()
            if not waiter.future.done():
                waiter.future.set_exception(exc)
        self._bump_locked()

    async def _release(self, epoch: int) -> None:
        async with self._condition:
            self._release_locked(epoch)
            if self._queue and self._accepting:
                self._ensure_driver_locked()
            self._condition.notify_all()

    def _release_locked(self, epoch: int) -> None:
        if epoch != self._epoch or self._active <= 0:
            return
        self._active -= 1
        self._last_used_monotonic = time.monotonic()
        self._bump_locked()
        self._notify_inflight_locked()

    async def status(self) -> CoordinatorStatus:
        async with self._condition:
            self._purge_cancelled_locked()
            queued_by = Counter(
                waiter.deployment_id for waiter in self._queue if not waiter.cancelled
            )
            return CoordinatorStatus(
                state=self._state,
                resident_profile=self._resident_profile,
                resident_deployment_id=self._resident_deployment_id,
                epoch=self._epoch,
                active=self._active,
                queued=sum(queued_by.values()),
                queue_limit=self._max_queue_depth,
                queued_by_deployment=dict(sorted(queued_by.items())),
                transition_target=self._transition_target,
                accepting=self._accepting and not self._stopping,
                authoritative=self._authoritative,
                diagnostic_code=self._diagnostic_code,
                configured_max_concurrency=self._configured_max_concurrency,
                capacity=self._capacity,
                sequence=self._sequence,
            )

    async def unload(self, *, timeout_seconds: float) -> bool:
        """Close admission, drain every lease, then stop the engine."""

        async with self._maintenance_lock:
            return await self._drain_and_unload(
                timeout_seconds=timeout_seconds,
                reopen=True,
                stopping=False,
            )

    async def evict_if_idle(self, idle_seconds: float, *, timeout_seconds: float) -> bool:
        async with self._maintenance_lock:
            async with self._condition:
                eligible = (
                    self._resident_profile is not None
                    and self._active == 0
                    and not self._queue
                    and self._accepting
                    and self._authoritative
                    and time.monotonic() - self._last_used_monotonic >= idle_seconds
                )
                if not eligible:
                    return False
                self._accepting = False
                self._bump_locked()
            await self._drain_and_unload(
                timeout_seconds=timeout_seconds,
                reopen=True,
                stopping=False,
                admission_already_closed=True,
            )
        return True

    async def shutdown(self, *, timeout_seconds: float) -> bool:
        """Drain safely and stop. A timeout never kills an active epoch."""

        async with self._maintenance_lock:
            self._stopping = True
            return await self._drain_and_unload(
                timeout_seconds=timeout_seconds,
                reopen=False,
                stopping=True,
            )

    async def _drain_and_unload(
        self,
        *,
        timeout_seconds: float,
        reopen: bool,
        stopping: bool,
        admission_already_closed: bool = False,
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        driver: asyncio.Task[None] | None
        async with self._condition:
            if not admission_already_closed:
                self._accepting = False
                self._bump_locked()
            if stopping:
                self._set_state_locked(CoordinatorState.STOPPING)
            self._fail_all_locked(
                NotAccepting("coordinator is stopping" if stopping else "unload in progress")
            )
            self._condition.notify_all()
            while self._active > 0:
                self._set_state_locked(
                    CoordinatorState.STOPPING if stopping else CoordinatorState.DRAINING
                )
                remaining = deadline - loop.time()
                if remaining <= 0:
                    if reopen and not stopping:
                        self._accepting = True
                        self._set_state_locked(
                            CoordinatorState.READY
                            if self._resident_profile is not None
                            else CoordinatorState.IDLE
                        )
                    else:
                        self._diagnostic_code = "shutdown_drain_timeout"
                        self._bump_locked()
                    raise QueueTimeout("timed out draining active model leases")
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    if reopen and not stopping:
                        self._accepting = True
                        self._set_state_locked(
                            CoordinatorState.READY
                            if self._resident_profile is not None
                            else CoordinatorState.IDLE
                        )
                    else:
                        self._diagnostic_code = "shutdown_drain_timeout"
                        self._bump_locked()
                    raise QueueTimeout("timed out draining active model leases") from exc
            driver = self._driver_task

        if (
            driver is not None
            and driver is not asyncio.current_task()
            and not driver.done()
        ):
            remaining = deadline - loop.time()
            if remaining <= 0:
                async with self._condition:
                    self._authoritative = False
                    self._accepting = False
                    self._set_state_locked(
                        CoordinatorState.STOPPING
                        if stopping
                        else CoordinatorState.DEGRADED,
                        diagnostic_code="transition_drain_timeout",
                    )
                raise QueueTimeout("timed out waiting for residency transition")
            try:
                await asyncio.wait_for(asyncio.shield(driver), timeout=remaining)
            except asyncio.TimeoutError as exc:
                async with self._condition:
                    self._authoritative = False
                    self._accepting = False
                    self._set_state_locked(
                        CoordinatorState.STOPPING
                        if stopping
                        else CoordinatorState.DEGRADED,
                        diagnostic_code="transition_drain_timeout",
                    )
                raise QueueTimeout("timed out waiting for residency transition") from exc

        remaining = deadline - loop.time()
        if remaining <= 0:
            async with self._condition:
                self._authoritative = False
                self._accepting = False
                self._set_state_locked(
                    CoordinatorState.STOPPING
                    if stopping
                    else CoordinatorState.DEGRADED,
                    diagnostic_code="unload_deadline_expired",
                )
            raise QueueTimeout("unload deadline expired")
        try:
            async with asyncio.timeout(remaining):
                async with self._operation_lock:
                    async with self._condition:
                        self._set_state_locked(
                            CoordinatorState.STOPPING
                            if stopping
                            else CoordinatorState.UNLOADING
                        )
                        self._condition.notify_all()
                    await self._call_stop()
        except TimeoutError as exc:
            async with self._condition:
                self._authoritative = False
                self._accepting = False
                self._set_state_locked(
                    CoordinatorState.STOPPING if stopping else CoordinatorState.DEGRADED,
                    diagnostic_code="engine_stop_timeout",
                )
            raise QueueTimeout("engine stop timed out") from exc
        except BaseException:
            async with self._condition:
                self._authoritative = False
                self._accepting = False
                self._set_state_locked(
                    CoordinatorState.STOPPING if stopping else CoordinatorState.DEGRADED,
                    diagnostic_code="engine_stop_failed",
                )
            raise

        async with self._condition:
            self._resident_profile = None
            self._resident_deployment_id = None
            self._capacity = CapacitySpec(
                1, "no-resident-conservative", "conservative"
            )
            self._epoch += 1
            self._authoritative = True
            self._diagnostic_code = None
            self._accepting = reopen and not stopping
            self._set_state_locked(
                CoordinatorState.STOPPING if stopping else CoordinatorState.IDLE
            )
            self._bump_locked()
            self._condition.notify_all()
        return True
