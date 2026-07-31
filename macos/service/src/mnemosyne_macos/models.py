"""Platform-neutral runtime types for the native macOS coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class EngineName(StrEnum):
    LLAMA_CPP = "llama.cpp"
    OMLX = "omlx"
    DS4 = "ds4"
    MFLUX = "mflux"


ENGINE_RELEASE_TIER: dict[EngineName, str] = {
    EngineName.LLAMA_CPP: "stable",
    EngineName.OMLX: "stable",
    EngineName.DS4: "preview",
    EngineName.MFLUX: "preview",
}


class Endpoint(StrEnum):
    CHAT_COMPLETIONS = "chat/completions"
    COMPLETIONS = "completions"
    RESPONSES = "responses"
    MESSAGES = "messages"
    EMBEDDINGS = "embeddings"
    RERANK = "rerank"
    IMAGES_GENERATIONS = "images/generations"


class ModelKind(StrEnum):
    LANGUAGE = "language"
    IMAGE = "image"


LLAMA_CPP_GENERATION_CAPABILITIES = frozenset(
    {
        Endpoint.CHAT_COMPLETIONS,
        Endpoint.COMPLETIONS,
        Endpoint.RESPONSES,
    }
)


DEFAULT_CAPABILITIES: dict[EngineName, frozenset[Endpoint]] = {
    EngineName.LLAMA_CPP: LLAMA_CPP_GENERATION_CAPABILITIES,
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
    EngineName.MFLUX: frozenset({Endpoint.IMAGES_GENERATIONS}),
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
class EffectiveLoadIdentity:
    """Every setting required to safely reuse a resident for a target.

    Fleet deployment identity deliberately remains stricter: it includes the
    complete request capability contract. This local identity exists only to
    decide whether a resident process can safely serve another lease without
    being relaunched.
    """

    key: TargetKey
    wire_model: str
    process_mode: str | None


@dataclass(frozen=True)
class ResolvedTarget:
    alias: str
    key: TargetKey
    wire_model: str
    capabilities: frozenset[Endpoint]
    load_options: Mapping[str, Any] = field(default_factory=dict)
    kind: ModelKind = ModelKind.LANGUAGE
    image_defaults: Mapping[str, Any] = field(default_factory=dict)
    storage_path: str | None = None
    scope_id: str | None = None
    storage_volume_uuid: str | None = None


def llama_cpp_process_mode(capabilities: frozenset[Endpoint]) -> str:
    """Mirror the capability-derived switches in ``build_llama_cpp_argv``."""

    generation_endpoints = {
        Endpoint.CHAT_COMPLETIONS,
        Endpoint.COMPLETIONS,
        Endpoint.RESPONSES,
        Endpoint.MESSAGES,
    }
    if capabilities & generation_endpoints:
        return "generation"
    if Endpoint.RERANK in capabilities:
        return "rerank"
    if Endpoint.EMBEDDINGS in capabilities:
        return "embeddings"
    # ModelProfile validation prevents this for configured llama.cpp models.
    # Preserve a distinct value for directly constructed test/integration
    # targets rather than accidentally treating them as a generation process.
    return "unsupported:" + ",".join(sorted(item.value for item in capabilities))


def effective_load_identity(target: ResolvedTarget) -> EffectiveLoadIdentity:
    """Return the resident-process equality key used by the coordinator."""

    return EffectiveLoadIdentity(
        key=target.key,
        wire_model=target.wire_model,
        process_mode=(
            llama_cpp_process_mode(target.capabilities)
            if target.key.engine == EngineName.LLAMA_CPP
            else None
        ),
    )


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
