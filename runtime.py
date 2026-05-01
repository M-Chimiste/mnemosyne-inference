"""Mnemosyne Inference — pure runtime helpers and in-memory state.

Phase 2 substrate: turns a `ResolvedProfile` into the data needed to launch
vLLM (CLI argv, subprocess env), and holds the live runtime view that
`/manager/status` and the eviction loop read.

Fully pure. No subprocess, no asyncio, no FastAPI, no filesystem, no
nvidia-smi. The caller probes GPUs via `config.gpu_indices_or_none()` and
passes the result into `derive_tp_size`. See project_docs/plans/phase_2.md §4.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from profiles import ResolvedProfile


@dataclass
class RuntimeState:
    """Live view of the resident vLLM process. Reset to defaults by
    `_kill_vllm`; populated by `_start_vllm` on successful load."""
    resident_alias: Optional[str] = None
    resident_profile: Optional[ResolvedProfile] = None
    resident_tp_size: Optional[int] = None
    model_load_time: Optional[float] = None
    last_used_at: Optional[float] = None
    request_count_delta: int = 0
    inflight: int = 0


def derive_tp_size(
    profile: ResolvedProfile,
    *,
    visible_gpus: Optional[list[int]],
    default_tp: int,
) -> int:
    """tp = len(profile.gpus) for explicit lists.
    For 'all', tp = len(visible_gpus) when the probe returned a non-empty
    list; otherwise default_tp (caller is responsible for logging the WARN
    that signals the fallback)."""
    if isinstance(profile.gpus, list):
        return len(profile.gpus)
    if visible_gpus:
        return len(visible_gpus)
    return default_tp


def build_vllm_argv(
    profile: ResolvedProfile,
    *,
    host: str,
    port: int,
    tp_size: int,
) -> list[str]:
    """Translate a ResolvedProfile into the vLLM CLI invocation.

    extra_args is appended last so users can re-state our flags to override
    them — preserves Phase 0 behavior."""
    import sys
    argv: list[str] = [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model", profile.model,
        "--host", host,
        "--port", str(port),
        "--tensor-parallel-size", str(tp_size),
        "--gpu-memory-utilization", str(profile.gpu_memory_utilization),
        "--no-enable-log-requests",
    ]
    if profile.trust_remote_code:
        argv.append("--trust-remote-code")
    if profile.quantization is not None:
        argv += ["--quantization", profile.quantization]
    if profile.max_model_len is not None:
        argv += ["--max-model-len", str(profile.max_model_len)]
    if profile.revision and profile.revision != "main":
        argv += ["--revision", profile.revision]
    argv += list(profile.extra_args)
    return argv


def build_vllm_env(
    profile: ResolvedProfile,
    *,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Subprocess env. Returns a fresh dict — never mutates base_env.

    For gpus='all' we explicitly POP any inherited CUDA_VISIBLE_DEVICES so
    the container/parent's narrowed set doesn't silently mismatch tp_size.
    """
    env = dict(base_env)
    if profile.gpus == "all":
        env.pop("CUDA_VISIBLE_DEVICES", None)
    else:
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in profile.gpus)
    env["HF_HOME"] = profile.storage_path
    return env
