"""Mnemosyne Inference — pure runtime helpers and in-memory state.

Phase 2 substrate: turns a `ResolvedProfile` into the data needed to launch
the inference engine (CLI argv, subprocess env), and holds the live runtime
view that `/manager/status` and the eviction loop read.

Three backends share this substrate: vLLM, llama.cpp's `llama-server`, and
SGLang Diffusion.
The argv builders are pure functions; dispatch happens in `_start_engine`
inside `vllm_manager.py`. Both backends bind the same loopback inner port
sequentially because only one model is resident at a time.

Fully pure. No subprocess, no asyncio, no FastAPI, no filesystem, no
nvidia-smi. The caller probes GPUs via `config.gpu_indices_or_none()` and
passes the result into `derive_tp_size`. See project_docs/plans/phase_2.md §4.
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from typing import Mapping, NamedTuple, Optional

from profiles import ResolvedProfile


class UsageEntry(NamedTuple):
    """One idempotent per-request token-usage record.

    The first 8 fields back the existing SQLite `request_usage` analytics
    table (catalog.record_usage_batch slices them positionally). The
    remaining fields back the optional postgres token-sidecar outbox
    (event_id is a UUID, response_ms is wall-clock, status_code is the
    upstream HTTP status).
    """
    ts: float
    requested_model: Optional[str]
    alias: Optional[str]
    backend: Optional[str]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    usage_json: Optional[str]
    event_id: str
    endpoint: str
    streamed: bool
    response_ms: float
    status_code: int


@dataclass
class RuntimeState:
    """Live view of the resident engine subprocess. Reset to defaults by
    `_kill_engine`; populated by `_start_engine` on successful load."""
    resident_alias: Optional[str] = None
    resident_profile: Optional[ResolvedProfile] = None
    resident_tp_size: Optional[int] = None
    model_load_time: Optional[float] = None
    last_used_at: Optional[float] = None
    request_count_delta: int = 0
    inflight: int = 0
    # Retry-only queue for a usage event whose immediate atomic SQLite write
    # failed. `_flush_loop` removes rows only after a later successful commit.
    usage_rows: deque = field(default_factory=deque)


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


# Architecture families whose vLLM CUDA-graph capture is pathologically slow.
# These are state-space / hybrid-SSM models (Mamba and friends): a cold load
# spends minutes in "profile, create kv cache, warmup model" capturing graphs
# (measured: LiquidAI/LFM2.5-8B-A1B took 431s, → 5s with --enforce-eager).
# For an interactive, one-model-at-a-time swapping server that startup latency
# dominates the user experience, so we default these to eager mode. Matched
# case-insensitively as substrings against config.json `architectures` class
# names and `model_type` (underscores stripped). Dense transformers (Granite,
# Gemma, Laguna, Qwen, …) never match and keep their CUDA graphs.
_SLOW_CAPTURE_ARCH_SUBSTRINGS = (
    "lfm2", "mamba", "jamba", "zamba", "bamba",
    "falconh1", "nemotronh", "plamo2", "hybrid",
)


def vllm_wants_eager_default(
    architectures: Optional[list[str]],
    model_type: Optional[str] = None,
) -> bool:
    """True when the model's architecture is a known slow-graph-capture
    (SSM/hybrid) family that should default to `--enforce-eager`. Pure: the
    caller reads `architectures`/`model_type` from the cached config.json."""
    tokens = [a.lower() for a in (architectures or []) if isinstance(a, str)]
    if model_type:
        tokens.append(str(model_type).lower().replace("_", ""))
    return any(sub in tok for tok in tokens for sub in _SLOW_CAPTURE_ARCH_SUBSTRINGS)


def build_vllm_argv(
    profile: ResolvedProfile,
    *,
    host: str,
    port: int,
    tp_size: int,
    enforce_eager: bool = False,
) -> list[str]:
    """Translate a ResolvedProfile into the vLLM CLI invocation.

    `enforce_eager` (set by the caller for slow-graph-capture architectures —
    see `vllm_wants_eager_default`) appends `--enforce-eager` unless the profile
    already states it. extra_args is appended last so users can re-state our
    flags to override them — preserves Phase 0 behavior."""
    import sys
    argv: list[str] = [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model", profile.engine_model_path,
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
    if enforce_eager and "--enforce-eager" not in profile.extra_args:
        argv.append("--enforce-eager")
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


# ── llama.cpp (llama-server) ─────────────────────────────────────────


# Default that matches the Dockerfile's `cmake --build` install location.
# Override with the LLAMA_SERVER_BIN env var for dev hosts.
LLAMA_SERVER_BIN_DEFAULT = "/usr/local/bin/llama-server"


def build_llama_argv(
    profile: ResolvedProfile,
    *,
    host: str,
    port: int,
    bin_path: Optional[str] = None,
) -> list[str]:
    """Translate a ResolvedProfile (backend='llama.cpp') into the llama-server
    CLI invocation.

    Notes:
      - `--model` receives the absolute filesystem path to the chosen GGUF
        primary shard. Sharded models are auto-detected by llama-server when
        peers in the canonical `*-NNNNN-of-NNNNN.gguf` form sit alongside.
      - `--alias` makes llama-server serve under the user-facing alias so
        the proxy's `model` rewriting (to `served_model_name`) lands on a
        name the engine accepts.
      - `--n-gpu-layers 999` offloads everything to GPU; CPU fallback only
        happens when CUDA build is unavailable (out of scope here).
      - `--tensor-split` is set to a uniform-weight comma list when the
        profile names explicit GPUs; `'all'` is left to llama-server's
        default device picker (driven by CUDA_VISIBLE_DEVICES from the env).
      - `extra_args` is appended last (escape hatch).
    """
    bin_ = bin_path or os.environ.get("LLAMA_SERVER_BIN", LLAMA_SERVER_BIN_DEFAULT)
    argv: list[str] = [
        bin_,
        "--model", profile.engine_model_path,
        "--alias", profile.served_model_name,
        "--host", host,
        "--port", str(port),
        "--n-gpu-layers", "999",
        "--jinja",
    ]
    if profile.max_model_len is not None:
        argv += ["-c", str(profile.max_model_len)]
    if isinstance(profile.gpus, list) and len(profile.gpus) > 1:
        argv += ["--tensor-split", ",".join("1" for _ in profile.gpus)]
    capabilities = set(profile.capabilities)
    if capabilities == {"rerank"}:
        argv += ["--embedding", "--reranking", "--pooling", "rank"]
    elif capabilities == {"embeddings"}:
        argv += ["--embedding"]
    if profile.projector_model_path is not None:
        argv += ["--mmproj", profile.projector_model_path]
    argv += list(profile.extra_args)
    return argv


def build_llama_env(
    profile: ResolvedProfile,
    *,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Subprocess env for llama-server. Mirrors `build_vllm_env`'s
    CUDA_VISIBLE_DEVICES handling so a single GPU plan flag propagates
    consistently across backends."""
    env = dict(base_env)
    if profile.gpus == "all":
        env.pop("CUDA_VISIBLE_DEVICES", None)
    else:
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in profile.gpus)
    env["HF_HOME"] = profile.storage_path
    return env


# ── SGLang Diffusion ────────────────────────────────────────────────

SGLANG_BIN_DEFAULT = "/opt/sglang/bin/sglang"


def build_sglang_diffusion_argv(
    profile: ResolvedProfile,
    *,
    host: str,
    port: int,
    num_gpus: int,
    bin_path: Optional[str] = None,
) -> list[str]:
    """Build a persistent SGLang Diffusion OpenAI server invocation."""
    bin_ = bin_path or os.environ.get("SGLANG_BIN", SGLANG_BIN_DEFAULT)
    argv = [
        bin_, "serve",
        "--model-path", profile.engine_model_path,
        "--host", host,
        "--port", str(port),
        "--num-gpus", str(num_gpus),
    ]
    if profile.revision and profile.revision != "main":
        argv += ["--revision", profile.revision]
    argv += list(profile.extra_args)
    return argv


def build_sglang_diffusion_env(
    profile: ResolvedProfile,
    *,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Use the same GPU visibility and Hugging Face cache contract as vLLM."""
    return build_vllm_env(profile, base_env=base_env)
