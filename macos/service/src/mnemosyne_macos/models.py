"""Platform-neutral runtime types for the native macOS coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class EngineName(StrEnum):
    LMSTUDIO = "lmstudio"
    OMLX = "omlx"
    DS4 = "ds4"


class Endpoint(StrEnum):
    CHAT_COMPLETIONS = "chat/completions"
    COMPLETIONS = "completions"
    RESPONSES = "responses"
    MESSAGES = "messages"
    EMBEDDINGS = "embeddings"
    RERANK = "rerank"


DEFAULT_CAPABILITIES: dict[EngineName, frozenset[Endpoint]] = {
    EngineName.LMSTUDIO: frozenset(
        {
            Endpoint.CHAT_COMPLETIONS,
            Endpoint.COMPLETIONS,
            Endpoint.RESPONSES,
            Endpoint.MESSAGES,
            Endpoint.EMBEDDINGS,
        }
    ),
    EngineName.OMLX: frozenset(
        {
            Endpoint.CHAT_COMPLETIONS,
            Endpoint.COMPLETIONS,
            Endpoint.RESPONSES,
            Endpoint.MESSAGES,
            Endpoint.EMBEDDINGS,
            Endpoint.RERANK,
        }
    ),
    EngineName.DS4: frozenset(
        {
            Endpoint.CHAT_COMPLETIONS,
            Endpoint.COMPLETIONS,
            Endpoint.RESPONSES,
            Endpoint.MESSAGES,
        }
    ),
}


class ServiceState(StrEnum):
    READY = "ready"
    STOPPED = "stopped"
    UNREACHABLE = "unreachable"
    UNAUTHORIZED = "unauthorized"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class TargetKey:
    """Identity of an effective engine load, not merely its public alias."""

    engine: EngineName
    canonical_model_id: str
    load_config_digest: str


@dataclass(frozen=True)
class ResolvedTarget:
    alias: str
    key: TargetKey
    wire_model: str
    capabilities: frozenset[Endpoint]
    load_options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResidentInstance:
    engine: EngineName
    canonical_model_id: str
    instance_id: str | None = None
    ready: bool = True
    managed: bool = True
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineSnapshot:
    engine: EngineName
    residents: tuple[ResidentInstance, ...]
    authoritative: bool
    service_state: ServiceState
    diagnostic: str | None = None

    @property
    def empty(self) -> bool:
        return self.authoritative and not self.residents


@dataclass(frozen=True)
class LoadedHandle:
    target: ResolvedTarget
    instance: ResidentInstance
    base_url: str
    wire_model: str


@dataclass(frozen=True)
class ProxyRoute:
    base_url: str
    path: str
    wire_model: str
    headers: Mapping[str, str] = field(default_factory=dict)
    usage_dialect: str = "openai"
    supports_stream_usage: bool = True

