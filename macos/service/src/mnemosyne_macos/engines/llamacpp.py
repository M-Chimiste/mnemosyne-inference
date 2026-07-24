"""Manager-owned llama.cpp server adapter for native GGUF inference.

The lifecycle and ownership proof intentionally reuse the hardened managed-
process machinery used by DS4.  llama-server is still an upstream executable;
this adapter only translates typed profile settings into its CLI and keeps the
single resident process inside the global residency coordinator.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .base import AdapterError, Deadline
from .ds4 import DS4Adapter
from ..config import LlamaCppConfig
from ..filesystem import FilesystemProbe, FilesystemProbeError
from ..models import Endpoint, EngineName, LoadedHandle, ProxyRoute, ResolvedTarget
from ..runtime_updates import resolve_active_runtime


_RESERVED_EXTRA_ARGS = {
    "-m",
    "--model",
    "-a",
    "--alias",
    "--host",
    "--port",
    "-c",
    "--ctx-size",
    "-b",
    "--batch-size",
    "-ub",
    "--ubatch-size",
    "-t",
    "--threads",
    "-np",
    "--parallel",
    "-ngl",
    "--gpu-layers",
    "--n-gpu-layers",
    "-fa",
    "--flash-attn",
    "-kvo",
    "--kv-offload",
    "-nkvo",
    "--no-kv-offload",
    "-mm",
    "--mmproj",
    "--embedding",
    "--embeddings",
    "--rerank",
    "--reranking",
    "--pooling",
}


def _validated_gguf(path_value: object, *, option: str) -> Path:
    path = Path(str(path_value)).expanduser().resolve(strict=False)
    if not path.is_file():
        raise AdapterError(
            EngineName.LLAMA_CPP,
            "build_argv",
            f"{option} GGUF file does not exist: {path}",
        )
    if path.suffix.casefold() != ".gguf":
        raise AdapterError(
            EngineName.LLAMA_CPP,
            "build_argv",
            f"{option} must select a .gguf file: {path}",
        )
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError as exc:
        raise AdapterError(
            EngineName.LLAMA_CPP,
            "build_argv",
            f"could not read {option} GGUF file: {exc}",
        ) from exc
    if magic != b"GGUF":
        raise AdapterError(
            EngineName.LLAMA_CPP,
            "build_argv",
            f"{option} does not have a GGUF header: {path}",
        )
    return path


def build_llama_cpp_argv(
    config: LlamaCppConfig,
    target: ResolvedTarget,
    *,
    validated_model: Path | None = None,
    validated_projector: Path | None = None,
) -> list[str]:
    """Build a pinned, loopback-only llama-server invocation."""

    options = target.load_options
    extra_args = [str(value) for value in options.get("extra_args", [])]
    for argument in extra_args:
        name = argument.split("=", 1)[0]
        if name in _RESERVED_EXTRA_ARGS:
            raise AdapterError(
                EngineName.LLAMA_CPP,
                "build_argv",
                f"extra_args may not override managed option {name}",
            )

    model = validated_model or _validated_gguf(
        target.key.canonical_model_id, option="model"
    )
    argv = [
        str(Path(config.binary).expanduser()),
        "--model",
        str(model),
        "--alias",
        target.wire_model,
        "--host",
        config.host,
        "--port",
        str(config.port),
        "--jinja",
        "--no-webui",
    ]
    if options.get("gpu_layers") is not None:
        argv.extend(["--n-gpu-layers", str(options["gpu_layers"])])
    if options.get("context_length") is not None:
        argv.extend(["--ctx-size", str(options["context_length"])])
    if options.get("eval_batch_size") is not None:
        argv.extend(["--batch-size", str(options["eval_batch_size"])])
    if options.get("ubatch_size") is not None:
        argv.extend(["--ubatch-size", str(options["ubatch_size"])])
    if options.get("threads") is not None:
        argv.extend(["--threads", str(options["threads"])])
    if options.get("parallel") is not None:
        argv.extend(["--parallel", str(options["parallel"])])
    if options.get("flash_attention") is not None:
        argv.extend(
            ["--flash-attn", "on" if options["flash_attention"] else "off"]
        )
    if options.get("offload_kv_cache_to_gpu") is False:
        argv.append("--no-kv-offload")
    if options.get("projector_path") is not None:
        projector = validated_projector or _validated_gguf(
            options["projector_path"], option="multimodal projector"
        )
        if projector == model:
            raise AdapterError(
                EngineName.LLAMA_CPP,
                "build_argv",
                "the model and multimodal projector must be different GGUF files",
            )
        argv.extend(["--mmproj", str(projector)])

    generation_endpoints = {
        Endpoint.CHAT_COMPLETIONS,
        Endpoint.COMPLETIONS,
        Endpoint.RESPONSES,
        Endpoint.MESSAGES,
    }
    generation_enabled = bool(target.capabilities & generation_endpoints)
    if not generation_enabled and Endpoint.RERANK in target.capabilities:
        argv.extend(["--embedding", "--reranking", "--pooling", "rank"])
    elif not generation_enabled and Endpoint.EMBEDDINGS in target.capabilities:
        argv.append("--embedding")
        if options.get("pooling") is not None:
            argv.extend(["--pooling", str(options["pooling"])])
    elif options.get("pooling") is not None:
        raise AdapterError(
            EngineName.LLAMA_CPP,
            "build_argv",
            "pooling is only valid for an embeddings-only or rerank profile",
        )

    argv.extend(extra_args)
    return argv


class LlamaCppAdapter(DS4Adapter):
    """A DS4-style managed process with llama.cpp-specific launch semantics."""

    engine = EngineName.LLAMA_CPP
    ownership = "managed_process"
    display_name = "llama.cpp"

    def __init__(
        self,
        config: LlamaCppConfig,
        *,
        runtime_root: str | Path | None = None,
        filesystem_probe: FilesystemProbe | None = None,
        **kwargs,
    ) -> None:
        super().__init__(config, runtime_root=runtime_root, **kwargs)  # type: ignore[arg-type]
        scope_root = kwargs.get("security_scope_root") or (
            Path(config.process_state_path).expanduser().parent / "security-scopes"
        )
        self._filesystem_probe = filesystem_probe or FilesystemProbe(
            scope_root=scope_root,
            timeout_seconds=config.request_timeout_seconds,
        )

    def _effective_config(self) -> LlamaCppConfig:
        managed = resolve_active_runtime("llama.cpp", root=self._runtime_root)
        if managed is None:
            return self.config  # type: ignore[return-value]
        return self.config.model_copy(  # type: ignore[union-attr,return-value]
            update={
                "binary": str(managed.path("binary")),
                "working_directory": str(managed.path("working_directory")),
            }
        )

    def _build_argv(
        self,
        config: LlamaCppConfig,
        target: ResolvedTarget,
    ) -> list[str]:
        return build_llama_cpp_argv(config, target)

    async def _build_argv_async(
        self,
        config: LlamaCppConfig,
        target: ResolvedTarget,
        *,
        deadline: Deadline,
    ) -> list[str]:
        """Validate selected GGUF files without freezing either HTTP plane.

        macOS can hold an ``open(2)`` call while a protected-folder access
        decision is pending. The model and projector header checks therefore
        must not run on the asyncio event-loop thread. Bound the wait by the
        engine request timeout so control/status stays available and callers
        receive an actionable error instead of an hour-long transition stall.
        """

        timeout = min(deadline.remaining(), config.request_timeout_seconds)
        if timeout <= 0:
            raise AdapterError(
                self.engine,
                "build_argv",
                "GGUF file validation deadline expired",
                retryable=True,
            )
        model_value = target.key.canonical_model_id
        storage_root = target.storage_path or str(Path(model_value).expanduser().parent)
        projector_value = target.load_options.get("projector_path")
        try:
            result = await self._filesystem_probe.validate_llama(
                root=storage_root,
                model=model_value,
                projector=(
                    str(projector_value) if projector_value is not None else None
                ),
                expected_volume_uuid=target.storage_volume_uuid,
                scope_id=target.scope_id,
                timeout_seconds=timeout,
            )
            return build_llama_cpp_argv(
                config,
                target,
                validated_model=Path(result["model"]),
                validated_projector=(
                    Path(result["projector"])
                    if result.get("projector") is not None
                    else None
                ),
            )
        except FilesystemProbeError as exc:
            raise AdapterError(
                self.engine,
                "build_argv",
                str(exc),
                retryable=True,
            ) from exc

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        if endpoint not in handle.target.capabilities:
            raise AdapterError(self.engine, "route", f"endpoint {endpoint} is unsupported")
        return ProxyRoute(
            base_url=self.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model=handle.wire_model,
            usage_dialect="anthropic" if endpoint == Endpoint.MESSAGES else "openai",
        )


__all__ = ["LlamaCppAdapter", "build_llama_cpp_argv"]
