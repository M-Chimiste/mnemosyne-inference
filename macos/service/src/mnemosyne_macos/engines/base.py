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

