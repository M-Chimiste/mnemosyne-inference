"""Engine adapter protocol and typed failures."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import time

from ..models import (
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
)


@dataclass(frozen=True)
class Deadline:
    monotonic_at: float

    @classmethod
    def after(cls, seconds: float) -> "Deadline":
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        return max(0.0, self.monotonic_at - time.monotonic())


@dataclass(frozen=True)
class CapacityHint:
    """Adapter-observed admission capacity for one resident model.

    The coordinator still applies its optional global ceiling. Keeping the
    engine's own limit separate makes status explain whether admission came
    from an authoritative runtime setting or a conservative fallback.
    """

    limit: int
    source: str
    confidence: str

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("capacity hint limit must be positive")


class AdapterError(RuntimeError):
    def __init__(
        self,
        engine: EngineName,
        operation: str,
        detail: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.engine = engine
        self.operation = operation
        self.detail = detail
        self.retryable = retryable
        super().__init__(f"{engine}:{operation}: {detail}")


class EngineAdapter(ABC):
    engine: EngineName
    ownership: str

    def capacity_hint(self, target: ResolvedTarget) -> CapacityHint | None:
        """Return a cached runtime capacity hint without performing I/O.

        Adapters that cannot authoritatively observe a scheduler limit leave
        this unset and the platform capacity derivation stays conservative.
        """

        del target
        return None

    async def runtime_fingerprint(self, *, deadline: Deadline) -> str | None:
        """Return a secret-free identity that changes with the runtime.

        ``None`` makes durable benchmark evidence ineligible for automatic
        selection. An adapter must not guess when no stable binary or upstream
        version can be observed.
        """

        del deadline
        return None

    @abstractmethod
    async def validate_control(self, *, deadline: Deadline) -> EngineSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def load(self, target: ResolvedTarget, *, deadline: Deadline) -> LoadedHandle:
        raise NotImplementedError

    @abstractmethod
    async def unload(
        self,
        instance: ResidentInstance,
        *,
        deadline: Deadline,
    ) -> None:
        raise NotImplementedError

    async def unload_all(self, *, deadline: Deadline) -> EngineSnapshot:
        snapshot = await self.inspect(deadline=deadline)
        if not snapshot.authoritative:
            raise AdapterError(
                self.engine,
                "unload_all",
                snapshot.diagnostic or "resident state is not authoritative",
            )
        for instance in snapshot.residents:
            await self.unload(instance, deadline=deadline)
        result = await self.inspect(deadline=deadline)
        if not result.empty:
            raise AdapterError(
                self.engine,
                "unload_all",
                "engine did not confirm an empty resident set",
            )
        return result

    @abstractmethod
    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        raise NotImplementedError

    @abstractmethod
    async def aclose(self) -> None:
        raise NotImplementedError
