"""Managed Preview adapters for native Apple language runtimes.

Both adapters deliberately reuse the hardened process-ownership implementation
used by DS4 and llama.cpp.  Mnemosyne launches one exact model process, proves
its PID/process-group/start identity and complete argv, and never signals an
unrecognized listener.
"""

from __future__ import annotations

from pathlib import Path

from .base import AdapterError, CapacityHint
from .ds4 import DS4Adapter
from ..config import MLXcelConfig, MistralRSConfig
from ..models import (
    Endpoint,
    EngineName,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
)


def _reject_reserved_args(
    engine: EngineName,
    values: list[str],
    reserved: frozenset[str],
) -> None:
    for value in values:
        option = value.split("=", 1)[0]
        if option in reserved:
            raise AdapterError(
                engine,
                "build_argv",
                f"extra_args cannot override manager-owned option {option}",
            )


class MLXcelAdapter(DS4Adapter):
    """One-model manager-owned ``mlxcel-server`` process."""

    engine = EngineName.MLXCEL
    ownership = "managed_process"
    display_name = "mlxcel"

    config: MLXcelConfig

    def _effective_config(self) -> MLXcelConfig:
        # mlxcel remains Homebrew/external-file owned. Runtime discovery and
        # upgrades may point this setting at another official binary, but the
        # coordinator never mutates that installation while loading a model.
        return self.config

    def capacity_hint(self, target: ResolvedTarget) -> CapacityHint:
        parallel = target.load_options.get("parallel", 4)
        if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
            parallel = 4
        return CapacityHint(
            limit=parallel,
            source="mlxcel-parallel",
            confidence="configured",
        )

    def _build_argv(
        self,
        config: MLXcelConfig,
        target: ResolvedTarget,
    ) -> list[str]:
        extra = list(target.load_options.get("extra_args", []))
        _reject_reserved_args(
            self.engine,
            extra,
            frozenset(
                {
                    "-m",
                    "--model",
                    "--host",
                    "-p",
                    "--port",
                    "--parallel",
                    "--ctx-size",
                }
            ),
        )
        parallel = target.load_options.get("parallel", 4)
        argv = [
            str(Path(config.binary).expanduser()),
            "-m",
            target.key.canonical_model_id,
            "--host",
            config.host,
            "--port",
            str(config.port),
            "--parallel",
            str(parallel),
        ]
        context_length = target.load_options.get("context_length")
        if context_length is not None:
            argv.extend(("--ctx-size", str(context_length)))
        argv.extend(extra)
        return argv

    def _handle(
        self,
        target: ResolvedTarget,
        instance: ResidentInstance,
    ) -> LoadedHandle:
        reported = instance.raw.get("reported_model")
        wire_model = (
            reported
            if isinstance(reported, str) and reported
            else target.wire_model
        )
        return LoadedHandle(
            target=target,
            instance=instance,
            base_url=self.base_url,
            wire_model=wire_model,
        )

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        if endpoint not in handle.target.capabilities:
            raise AdapterError(
                self.engine,
                "route",
                f"endpoint {endpoint} is unsupported",
            )
        return ProxyRoute(
            base_url=self.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model=handle.wire_model,
        )


class MistralRSAdapter(DS4Adapter):
    """One-model manager-owned ``mistralrs serve`` process."""

    engine = EngineName.MISTRAL_RS
    ownership = "managed_process"
    display_name = "mistral.rs"

    config: MistralRSConfig

    def _effective_config(self) -> MistralRSConfig:
        return self.config

    def capacity_hint(self, target: ResolvedTarget) -> CapacityHint:
        del target
        # No stable server-side scheduler-capacity contract is currently
        # exposed for Metal. Stay conservative until upstream adds one.
        return CapacityHint(
            limit=1,
            source="mistral-rs-conservative",
            confidence="fallback",
        )

    def _build_argv(
        self,
        config: MistralRSConfig,
        target: ResolvedTarget,
    ) -> list[str]:
        extra = list(target.load_options.get("extra_args", []))
        _reject_reserved_args(
            self.engine,
            extra,
            frozenset(
                {
                    "-m",
                    "--model",
                    "--host",
                    "-p",
                    "--port",
                    "--no-ui",
                    "--token-source",
                }
            ),
        )
        return [
            str(Path(config.binary).expanduser()),
            "--token-source",
            "none",
            "serve",
            "-m",
            target.key.canonical_model_id,
            "--host",
            config.host,
            "--port",
            str(config.port),
            "--no-ui",
            *extra,
        ]

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        if endpoint not in handle.target.capabilities:
            raise AdapterError(
                self.engine,
                "route",
                f"endpoint {endpoint} is unsupported",
            )
        return ProxyRoute(
            base_url=self.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model="default",
            usage_dialect="anthropic" if endpoint == Endpoint.MESSAGES else "openai",
        )


__all__ = ["MLXcelAdapter", "MistralRSAdapter"]
