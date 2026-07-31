#!/usr/bin/env python3
"""
vLLM Manager — Dynamic model loader/unloader with OpenAI-compatible proxy.

Runs inside a Docker container. Manages a vLLM subprocess, loading models
on demand and swapping when requested. Exposes:
  - OpenAI-compatible API at /v1/*  (proxied to inner vLLM)
  - Manager API at /manager/*       (load, unload, status, list cache)
"""

import asyncio
import contextlib
import dataclasses
import httpx
import re
import secrets
import shutil
import signal
import subprocess
import sys
import logging
import os
import json
import time
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TypeVar

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
import uvicorn

import config as config_mod
import downloader
import hf_search
import logsetup
from catalog import (
    Catalog,
    ReconcileResult,
    SyncResult,
    is_cache_only_alias,
    open_catalog,
    synthetic_alias,
)
from config import Config, ConfigError, GpuPlan, load_config, load_env
from downloader import ConflictError, repo_cache_dir
from cuda_residency import (
    CapacitySpec,
    CoordinatorState,
    CudaResidencyCoordinator,
    ModelLease,
    NotAccepting,
    QueueFull,
    QueueTimeout,
    ResidencyError,
)
from fleet_protocol import (
    FLEET_SCHEMA_VERSION,
    deployment_identity,
    derive_cuda_capacity,
    effective_capacity,
    gguf_quantization,
    identity_is_authoritative,
    positive_int_flag,
    semantic_extra_args,
    sha256_id,
)
from image_api import ImageRequestError, normalize_image_request
from profiles import ProfileNotReady, ResolvedProfile, resolve_profile
from runtime import (
    RuntimeState,
    UsageEntry,
    build_llama_argv,
    build_llama_env,
    build_sglang_diffusion_argv,
    build_sglang_diffusion_env,
    build_vllm_argv,
    build_vllm_env,
    derive_tp_size,
    vllm_wants_eager_default,
)
from usage_normalization import NormalizedUsage, StreamingUsageParser, normalize_usage

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
# Inner vLLM listens on container-loopback only. Default moved 8001 → 8002 in
# Phase 3 because the admin app now binds 8001 in the same network namespace.
# `_check_inner_port_clash` in main() catches user overrides that re-collide.
VLLM_INNER_HOST = os.getenv("VLLM_INNER_HOST", "127.0.0.1")
VLLM_INNER_PORT = int(os.getenv("VLLM_INNER_PORT", "8002"))
DEFAULT_TP      = int(os.getenv("VLLM_DEFAULT_TP", "2"))
DEFAULT_GPU_MEM = float(os.getenv("VLLM_GPU_MEM_UTIL", "0.90"))
STARTUP_TIMEOUT = int(os.getenv("VLLM_STARTUP_TIMEOUT", "600"))
HF_HOME         = os.getenv("HF_HOME", "/hf-cache")

logsetup.configure_logging(level=logging.INFO)
logger = logging.getLogger("vllm-manager")

T = TypeVar("T")

# ──────────────────────────────────────────────
# Auth (Phase 3 §5.10)
# ──────────────────────────────────────────────
# Admin: HTTP Basic, username "admin", password from ADMIN_PASSWORD env. If
#   ADMIN_PASSWORD is unset, the admin port is forced to loopback by
#   _resolve_admin_bind, so any reachable request is from inside the container —
#   we accept without creds in that mode (no password to compare against).
# Inference: optional bearer; auth disabled when INFERENCE_API_KEY is unset.
_basic = HTTPBasic(auto_error=False)


def require_admin_basic(
    creds: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    expected = os.environ.get("ADMIN_PASSWORD")
    if not expected:
        # Loopback-only mode (fail-safe bind). Anyone reaching here is inside
        # the container's network namespace; allow.
        return "admin"
    if creds is None:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    user_ok = secrets.compare_digest(creds.username, "admin")
    pw_ok = secrets.compare_digest(creds.password, expected)
    if not (user_ok and pw_ok):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    return creds.username


async def require_inference_bearer(request: Request) -> None:
    expected = os.environ.get("INFERENCE_API_KEY")
    if not expected:
        return
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401)
    if not secrets.compare_digest(auth[len("Bearer "):], expected):
        raise HTTPException(401)

# ──────────────────────────────────────────────
# Global state
# ──────────────────────────────────────────────
# Phase 2: vLLM subprocess + runtime view + swap queue.
vllm_process:    Optional[subprocess.Popen] = None
_runtime:        RuntimeState               = RuntimeState()
_swap_lock                                  = asyncio.Lock()
_loading_target: Optional[str]              = None
_load_event:     Optional[asyncio.Event]    = None
_load_error:     Optional[BaseException]    = None
_eviction_task:  Optional[asyncio.Task]     = None
_flush_task:     Optional[asyncio.Task]     = None
_pg_flush_task:  Optional[asyncio.Task]     = None
_legacy_alias_warned: set[str]              = set()
_coordinator: Optional[CudaResidencyCoordinator] = None
_fleet_instance_id: str = uuid.uuid4().hex
_fleet_snapshot_sequence: int = 0
_fleet_snapshot_sequence_lock = threading.Lock()

# Optional postgres token-usage sink (token_sidecar). `_pg_writer` is None
# when the sidecar is disabled or its DSN is missing; `_pg_last_*` feeds the
# /manager/status payload so the health of the path is visible without
# psql-ing the central DB.
_pg_writer = None  # type: Optional["pg_writer.PgWriter"]
_pg_last_flush_at: Optional[float] = None
_pg_last_flush_count: int = 0
_pg_last_error: Optional[str] = None

# Phase 1 globals — populated by lifespan, reset by tests/conftest.py::client.
_config: Optional[Config] = None
_catalog: Optional[Catalog] = None

# ──────────────────────────────────────────────
# Engine process management (vLLM + llama.cpp)
# ──────────────────────────────────────────────
# `vllm_process` is preserved as the public name (tests/external code reach
# in to reset it). It holds whichever backend's subprocess is currently
# resident — vLLM or llama-server. New code prefers the alias `engine_process`.


async def _wait_for_health(url: str, timeout: int) -> bool:
    """Poll a /health URL until the engine reports ready or the deadline
    expires. Returns False if the process died early."""
    async with httpx.AsyncClient(trust_env=False) as client:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if vllm_process and vllm_process.poll() is not None:
                logger.error("Engine subprocess exited unexpectedly during startup")
                return False
            try:
                r = await client.get(url, timeout=3)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(2)
    return False


async def _wait_for_vllm(timeout: int = STARTUP_TIMEOUT) -> bool:
    return await _wait_for_health(
        f"http://{VLLM_INNER_HOST}:{VLLM_INNER_PORT}/health", timeout,
    )


async def _wait_for_llama_cpp(timeout: int = STARTUP_TIMEOUT) -> bool:
    # llama-server returns {"status":"ok"} on /health once the model is
    # fully memory-mapped; same poll cadence as vLLM.
    return await _wait_for_health(
        f"http://{VLLM_INNER_HOST}:{VLLM_INNER_PORT}/health", timeout,
    )


async def _wait_for_sglang_diffusion(timeout: int = STARTUP_TIMEOUT) -> bool:
    return await _wait_for_health(
        f"http://{VLLM_INNER_HOST}:{VLLM_INNER_PORT}/v1/models", timeout,
    )


def _kill_engine():
    """Stop the resident engine subprocess (idempotent) and reset runtime
    state. Backend-agnostic — retries any failed usage writes before
    clearing the resident alias so the just-evicted model's last activity
    makes it to disk."""
    global vllm_process
    _flush_usage_best_effort("engine teardown")
    if vllm_process and vllm_process.poll() is None:
        logger.info(f"Stopping engine (pid={vllm_process.pid}) ...")
        vllm_process.terminate()
        try:
            vllm_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logger.warning("Graceful stop timed out — sending SIGKILL")
            vllm_process.kill()
            vllm_process.wait()
        logger.info("Engine stopped.")
    vllm_process = None
    _runtime.resident_alias = None
    _runtime.resident_profile = None
    _runtime.resident_tp_size = None
    _runtime.model_load_time = None
    _runtime.last_used_at = None
    # If the best-effort flush failed, drop the buffered count rather than
    # risking attribution to the next resident profile.
    _runtime.request_count_delta = 0


# Back-compat alias — older test fixtures patch this name directly.
_kill_vllm = _kill_engine


def _vllm_model_architectures(
    profile: ResolvedProfile,
) -> tuple[list[str], Optional[str]]:
    """Best-effort read of `architectures` + `model_type` from the model's
    cached config.json. Returns ([], None) when it can't be read — callers
    treat that as "apply no eager default". vLLM profiles only: for HF-id
    targets we reconstruct the HF cache snapshot path; for absolute local
    paths we read config.json directly."""
    path = profile.engine_model_path
    try:
        if os.path.isdir(path):
            cfg_path = os.path.join(path, "config.json")
        else:
            safe = "models--" + path.replace("/", "--")
            sha = profile.revision or "main"
            cfg_path = os.path.join(
                profile.storage_path, "hub", safe, "snapshots", sha, "config.json"
            )
        with open(cfg_path) as f:
            data = json.load(f)
        arch = data.get("architectures")
        return (arch if isinstance(arch, list) else []), data.get("model_type")
    except Exception:
        return [], None


async def _start_vllm(profile: ResolvedProfile) -> None:
    """Launch vLLM for `profile`. Cleans up half-launched subprocesses on any
    failure, including asyncio.CancelledError from a deadline-induced wait_for
    (called inside ensure_loaded). See plans/phase_2.md §5.2."""
    global vllm_process

    _kill_engine()
    visible = config_mod.gpu_indices_or_none()
    tp_size = derive_tp_size(profile, visible_gpus=visible, default_tp=DEFAULT_TP)
    if profile.gpus == "all" and not visible:
        logger.warning(
            "gpus='all' but nvidia-smi probe returned no GPUs; falling back to "
            "DEFAULT_TP=%d. Production CUDA hosts should never hit this path.",
            DEFAULT_TP,
        )

    arch, model_type = _vllm_model_architectures(profile)
    enforce_eager = vllm_wants_eager_default(arch, model_type)
    if enforce_eager and "--enforce-eager" not in profile.extra_args:
        logger.info(
            "Defaulting alias=%s to --enforce-eager: arch=%s is a slow-graph-capture "
            "(SSM/hybrid) family; CUDA-graph capture would add minutes to cold load. "
            "Add '--enforce-eager' (or override) in extra_args to silence.",
            profile.alias, arch or model_type,
        )
    argv = build_vllm_argv(
        profile, host=VLLM_INNER_HOST, port=VLLM_INNER_PORT,
        tp_size=tp_size, enforce_eager=enforce_eager,
    )
    env = build_vllm_env(profile, base_env=os.environ)
    logger.info("Launching vLLM (alias=%s tp=%d): %s", profile.alias, tp_size, " ".join(argv))
    vllm_process = subprocess.Popen(argv, env=env, stdout=sys.stdout, stderr=sys.stderr)

    try:
        startup_timeout = (
            _config.server.startup_timeout_seconds
            if _config is not None
            else STARTUP_TIMEOUT
        )
        if not await _wait_for_vllm(timeout=startup_timeout):
            exit_code = vllm_process.poll() if vllm_process else None
            raise RuntimeError(
                f"vLLM failed to become ready for alias '{profile.alias}' "
                f"(exit_code={exit_code}; see container logs for vLLM stderr — "
                f"common causes: OOM, invalid quantization, missing weights)"
            )
    except (Exception, asyncio.CancelledError):
        # Includes wait_for-induced CancelledError when ensure_loaded times out.
        # Always clean up the half-launched subprocess.
        _kill_engine()
        raise

    _runtime.resident_alias = profile.alias
    _runtime.resident_profile = profile
    _runtime.resident_tp_size = tp_size
    now = time.time()
    _runtime.model_load_time = now
    _runtime.last_used_at = now
    logger.info(
        "✓ Loaded alias='%s' model='%s' tp=%d gpu_mem=%.2f",
        profile.alias, profile.served_model_name, tp_size,
        profile.gpu_memory_utilization,
    )


async def _start_llama_cpp(profile: ResolvedProfile) -> None:
    """Launch llama-server for `profile`. Mirrors `_start_vllm` lifecycle but
    skips tp/gpu_mem (llama-server has its own knobs) and points
    `--model` at the absolute GGUF path."""
    global vllm_process

    _kill_engine()
    argv = build_llama_argv(profile, host=VLLM_INNER_HOST, port=VLLM_INNER_PORT)
    env = build_llama_env(profile, base_env=os.environ)
    logger.info(
        "Launching llama-server (alias=%s gguf=%s): %s",
        profile.alias, profile.gguf_filename, " ".join(argv),
    )
    vllm_process = subprocess.Popen(argv, env=env, stdout=sys.stdout, stderr=sys.stderr)

    try:
        startup_timeout = (
            _config.server.startup_timeout_seconds
            if _config is not None
            else STARTUP_TIMEOUT
        )
        if not await _wait_for_llama_cpp(timeout=startup_timeout):
            exit_code = vllm_process.poll() if vllm_process else None
            raise RuntimeError(
                f"llama-server failed to become ready for alias '{profile.alias}' "
                f"(exit_code={exit_code}; see container logs — common causes: "
                f"missing GGUF file, OOM, missing CUDA runtime)"
            )
    except (Exception, asyncio.CancelledError):
        _kill_engine()
        raise

    _runtime.resident_alias = profile.alias
    _runtime.resident_profile = profile
    # llama.cpp doesn't have a tp_size concept; record the GPU plan length so
    # /manager/status surfaces a meaningful number when the profile pinned
    # specific GPUs.
    if isinstance(profile.gpus, list):
        _runtime.resident_tp_size = len(profile.gpus)
    else:
        _runtime.resident_tp_size = None
    now = time.time()
    _runtime.model_load_time = now
    _runtime.last_used_at = now
    logger.info(
        "✓ Loaded alias='%s' gguf='%s' via llama.cpp",
        profile.alias, profile.gguf_filename,
    )


async def _start_sglang_diffusion(profile: ResolvedProfile) -> None:
    """Launch one persistent SGLang Diffusion image server."""
    global vllm_process

    _kill_engine()
    visible = config_mod.gpu_indices_or_none()
    num_gpus = derive_tp_size(profile, visible_gpus=visible, default_tp=DEFAULT_TP)
    if profile.gpus == "all" and not visible:
        logger.warning(
            "gpus='all' but nvidia-smi probe returned no GPUs; falling back to "
            "DEFAULT_TP=%d for SGLang Diffusion",
            DEFAULT_TP,
        )
    argv = build_sglang_diffusion_argv(
        profile,
        host=VLLM_INNER_HOST,
        port=VLLM_INNER_PORT,
        num_gpus=num_gpus,
    )
    env = build_sglang_diffusion_env(profile, base_env=os.environ)
    logger.info(
        "Launching SGLang Diffusion (alias=%s gpus=%d): %s",
        profile.alias, num_gpus, " ".join(argv),
    )
    vllm_process = subprocess.Popen(argv, env=env, stdout=sys.stdout, stderr=sys.stderr)
    try:
        startup_timeout = (
            _config.server.startup_timeout_seconds
            if _config is not None
            else STARTUP_TIMEOUT
        )
        if not await _wait_for_sglang_diffusion(timeout=startup_timeout):
            exit_code = vllm_process.poll() if vllm_process else None
            raise RuntimeError(
                f"SGLang Diffusion failed to become ready for alias '{profile.alias}' "
                f"(exit_code={exit_code}; see container logs for OOM or model errors)"
            )
    except (Exception, asyncio.CancelledError):
        _kill_engine()
        raise

    _runtime.resident_alias = profile.alias
    _runtime.resident_profile = profile
    _runtime.resident_tp_size = num_gpus
    now = time.time()
    _runtime.model_load_time = now
    _runtime.last_used_at = now
    logger.info(
        "✓ Loaded image alias='%s' model='%s' via SGLang Diffusion",
        profile.alias, profile.served_model_name,
    )


async def _start_engine(profile: ResolvedProfile) -> None:
    """Backend dispatch — selects vLLM or llama-server based on
    `profile.backend`. The shared `vllm_process` global holds whichever
    subprocess wins the lock, since only one model is resident at a time."""
    if profile.backend == "llama.cpp":
        await _start_llama_cpp(profile)
    elif profile.backend == "sglang-diffusion":
        await _start_sglang_diffusion(profile)
    else:
        await _start_vllm(profile)


def _profile_artifact(profile: ResolvedProfile) -> dict:
    if profile.backend == "llama.cpp":
        # Canonical v1 selection names only files explicitly chosen by the
        # profile. llama-server discovers peer shards from the primary GGUF,
        # but automatic peers are deliberately absent from strict identity.
        selected = sorted(
            {
                filename
                for filename in (
                    profile.gguf_filename,
                    profile.projector_filename,
                )
                if filename
            }
        )
        artifact_format = "gguf"
    elif profile.backend == "sglang-diffusion":
        selected = []
        artifact_format = "diffusers"
    else:
        selected = []
        artifact_format = "safetensors"
    return {
        "format": artifact_format,
        "selected_files": selected,
        "quantization": (
            profile.quantization
            or (
                gguf_quantization(profile.gguf_filename)
                if profile.backend == "llama.cpp"
                else None
            )
        ),
        # The installed HF commit pins the selected tree, but it is not itself
        # a SHA-256 digest of the weight bytes.
        "content_digest": None,
    }


def _valid_int(value: str, *, minimum: int = 1) -> bool:
    try:
        return int(value) >= minimum
    except (TypeError, ValueError):
        return False


def _valid_gpu_fraction(value: str) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return 0 < parsed <= 1


def _valid_non_option(value: str) -> bool:
    return bool(value) and not value.startswith("-")


def _last_valid_flag(
    args: tuple[str, ...],
    names: set[str],
    validator: Callable[[str], bool],
) -> str | None:
    result: str | None = None
    index = 0
    while index < len(args):
        item = args[index]
        name, equals, inline = item.partition("=")
        if name not in names:
            index += 1
            continue
        if equals:
            if validator(inline):
                result = inline
            index += 1
            continue
        if index + 1 < len(args) and validator(args[index + 1]):
            result = args[index + 1]
            index += 2
            continue
        index += 1
    return result


def _strip_valid_flags(
    args: tuple[str, ...],
    value_flags: dict[str, Callable[[str], bool]],
    boolean_flags: set[str],
) -> tuple[str, ...]:
    """Remove only unambiguous well-formed non-semantic options.

    A malformed option remains ordered in the identity input. In particular,
    it never consumes a following semantic option that merely looks like its
    missing value.
    """

    output: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        name, equals, inline = item.partition("=")
        if name in boolean_flags and not equals:
            index += 1
            continue
        validator = value_flags.get(name)
        if validator is None:
            output.append(item)
            index += 1
            continue
        if equals:
            if validator(inline):
                index += 1
            else:
                output.append(item)
                index += 1
            continue
        if index + 1 < len(args) and validator(args[index + 1]):
            index += 2
            continue
        output.append(item)
        index += 1
    return tuple(output)


def _semantic_load_config(profile: ResolvedProfile) -> dict:
    """Normalize output-affecting settings and exclude placement/capacity."""

    args = tuple(semantic_extra_args(profile.extra_args, backend=profile.backend))
    positive = lambda value: _valid_int(value, minimum=1)
    nonnegative = lambda value: _valid_int(value, minimum=0)
    if profile.backend == "vllm":
        value_flags: dict[str, Callable[[str], bool]] = {
            "--gpu-memory-utilization": _valid_gpu_fraction,
            "--tensor-parallel-size": positive,
            "--pipeline-parallel-size": positive,
            "--data-parallel-size": positive,
            "--max-model-len": positive,
        }
        context_length = (
            positive_int_flag(args, "--max-model-len")
            or profile.max_model_len
        )
        semantic_base = {
            "context_length": context_length,
            "trust_remote_code": profile.trust_remote_code,
        }
        boolean_flags: set[str] = set()
    elif profile.backend == "llama.cpp":
        pooling_values = {
            "none",
            "mean",
            "cls",
            "last",
            "rank",
            "unspecified",
        }
        enum_pooling = lambda value: value.lower() in pooling_values
        value_flags = {
            "--threads": positive,
            "-t": positive,
            "--threads-batch": positive,
            "-tb": positive,
            "--batch-size": positive,
            "-b": positive,
            "--ubatch-size": positive,
            "-ub": positive,
            "--n-gpu-layers": lambda value: _valid_int(value, minimum=-1),
            "--gpu-layers": lambda value: _valid_int(value, minimum=-1),
            "-ngl": lambda value: _valid_int(value, minimum=-1),
            "--split-mode": lambda value: value in {"none", "layer", "row"},
            "--tensor-split": _valid_non_option,
            "--main-gpu": nonnegative,
            "--flash-attn": lambda value: value.lower()
            in {"on", "off", "auto", "true", "false", "0", "1"},
            "--ctx-size": positive,
            "-c": positive,
            "--pooling": enum_pooling,
            "--pooling-type": enum_pooling,
        }
        boolean_flags = {
            "--no-kv-offload",
            "--kv-unified",
        }
        context_length = (
            positive_int_flag(args, "--ctx-size", "-c")
            or profile.max_model_len
        )
        semantic_base = {
            "context_length": context_length,
            "pooling": _last_valid_flag(
                args,
                {"--pooling", "--pooling-type"},
                enum_pooling,
            ),
        }
    else:
        value_flags = {
            "--num-gpus": positive,
            "--gpu-memory-utilization": _valid_gpu_fraction,
        }
        boolean_flags = set()
        semantic_base = {
            "image_defaults": profile.image_defaults,
        }
    semantic_base["semantic_extra_args"] = list(
        _strip_valid_flags(args, value_flags, boolean_flags)
    )
    return semantic_base


def _profile_deployment(
    profile: ResolvedProfile,
) -> tuple[str, dict, bool]:
    """Return strict identity, safe public identity document, and confidence."""

    upstream = profile.upstream_model or profile.served_model_name
    portable_upstream = bool(_RAW_HF_ID_RE.fullmatch(upstream))
    if not portable_upstream:
        # Raw/local/unusual engine targets remain callable for compatibility
        # but are neither disclosed nor automatically groupable across nodes.
        upstream = f"local-artifact-{sha256_id(upstream)}"
    artifact = _profile_artifact(profile)
    deployment_id, _load_digest, identity = deployment_identity(
        engine=profile.backend,
        upstream_model=upstream,
        resolved_revision=profile.revision or None,
        artifact=artifact,
        kind=profile.kind,
        capabilities=profile.capabilities,
        load_config=_semantic_load_config(profile),
    )
    authoritative = identity_is_authoritative(
        resolved_revision=profile.revision or None,
        artifact=artifact,
    ) and portable_upstream
    return deployment_id, identity, authoritative


async def _probe_engine_capacity(profile: ResolvedProfile) -> CapacitySpec:
    """Derive safe concurrency, preferring live engine-native state."""

    fallback = derive_cuda_capacity(
        backend=profile.backend,
        extra_args=profile.extra_args,
        configured_max_concurrency=None,
        active=0,
        queued=0,
    )
    derived = fallback.derived_limit
    source = fallback.source
    confidence = fallback.confidence
    if profile.backend != "llama.cpp":
        # vLLM 0.22.1 logs a theoretical KV concurrency estimate at startup
        # but does not expose it as a stable Prometheus value. A valid
        # --max-num-seqs is therefore the configured adapter limit; otherwise
        # admission remains conservatively single-request.
        return CapacitySpec(derived, source, confidence)
    try:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            response = await client.get(f"{VLLM_BASE}/slots")
            if response.status_code == 200:
                payload = response.json()
                slots = (
                    payload
                    if isinstance(payload, list)
                    else payload.get("slots", [])
                    if isinstance(payload, dict)
                    else []
                )
                if isinstance(slots, list) and slots:
                    derived = len(slots)
                    source = "llama.cpp-slots"
                    confidence = "authoritative"
    except Exception:
        # Metrics are an optimization over conservative adapter defaults.
        # Engine readiness has already been established by the start path.
        pass
    return CapacitySpec(derived, source, confidence)


def _set_coordinator_inflight(value: int) -> None:
    _runtime.inflight = value


def _set_coordinator_transition(profile: ResolvedProfile | None) -> None:
    global _loading_target
    _loading_target = profile.alias if profile is not None else None


def _new_coordinator() -> CudaResidencyCoordinator:
    if _config is None:
        raise RuntimeError("config not loaded")

    async def start(profile: ResolvedProfile) -> None:
        await _start_engine(profile)

    def stop() -> None:
        # Resolve through the module global at call time so compatibility
        # fixtures and operators patching the legacy hook still work.
        _kill_vllm()

    return CudaResidencyCoordinator(
        start_engine=start,
        stop_engine=stop,
        derive_capacity=_probe_engine_capacity,
        configured_max_concurrency=_config.server.max_concurrency,
        max_queue_depth=_config.server.max_queue_depth,
        on_inflight_changed=_set_coordinator_inflight,
        on_transition_changed=_set_coordinator_transition,
    )


def _get_coordinator() -> CudaResidencyCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = _new_coordinator()
    return _coordinator


def _admission_http_exception(exc: BaseException) -> HTTPException:
    if isinstance(exc, QueueFull):
        return HTTPException(
            429,
            detail={"code": "node_busy", "message": "node admission queue is full"},
            headers={
                "Retry-After": "1",
                "X-Mnemosyne-Error": "node_busy",
            },
        )
    if isinstance(exc, QueueTimeout):
        return HTTPException(
            504,
            detail={"code": "node_queue_timeout", "message": "node admission timed out"},
        )
    if isinstance(exc, NotAccepting):
        return HTTPException(
            503,
            detail={"code": "node_not_accepting", "message": "node is not accepting requests"},
            headers={"Retry-After": "1"},
        )
    return HTTPException(503, f"engine load failed: {exc}")


async def _acquire_profile_lease(
    profile: ResolvedProfile,
    deadline: float,
) -> ModelLease:
    deployment_id, _identity, _authoritative = _profile_deployment(profile)
    timeout = max(0.0, deadline - time.monotonic())
    try:
        return await _get_coordinator().acquire(
            profile,
            deployment_id,
            timeout_seconds=timeout,
        )
    except (QueueFull, QueueTimeout, NotAccepting) as exc:
        raise _admission_http_exception(exc) from exc
    except ResidencyError as exc:
        raise HTTPException(503, f"engine load failed: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"engine load failed: {exc}") from exc


# ──────────────────────────────────────────────
# Swap queue + deadline helpers (Phase 2 §5.3)
# ──────────────────────────────────────────────

async def _run_until(
    awaitable_factory: Callable[[], Awaitable[T]],
    deadline: float,
) -> T:
    """Wrap any awaitable in a deadline-relative timeout. Raises
    asyncio.TimeoutError if the deadline is past or the coro doesn't finish
    in time. Used by ensure_loaded so a single arrival-time deadline gates
    every await in the swap path — including _start_vllm itself.

    The factory shape avoids creating an unawaited coroutine when the deadline
    is already expired.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError()
    coro = awaitable_factory()
    return await asyncio.wait_for(coro, timeout=remaining)


async def ensure_loaded(profile: ResolvedProfile, deadline: float) -> None:
    """Compatibility load shim routed through the lease coordinator.

    The transient lease prevents this explicit load from swapping beneath an
    existing request.  Data-plane proxying acquires its own single lease and
    does not call this helper first.
    """

    lease = await _acquire_profile_lease(profile, deadline)
    await lease.release()


# ──────────────────────────────────────────────
# Usage buffer flush (Phase 2 §5.7)
# ──────────────────────────────────────────────

def _persist_usage_entries(entries: list[UsageEntry]) -> None:
    """Atomically persist analytics and optional delivery-outbox rows."""

    if not entries:
        return
    if _catalog is None:
        raise RuntimeError("catalog is not initialized")
    analytics_rows = [
        (
            entry.ts,
            entry.requested_model,
            entry.alias,
            entry.backend,
            entry.prompt_tokens,
            entry.completion_tokens,
            entry.total_tokens,
            entry.usage_json,
        )
        for entry in entries
    ]
    outbox_rows = (
        [
            (
                entry.event_id,
                entry.ts,
                entry.requested_model,
                entry.alias,
                entry.backend,
                entry.endpoint,
                1 if entry.streamed else 0,
                entry.prompt_tokens,
                entry.completion_tokens,
                entry.total_tokens,
                entry.response_ms,
                entry.status_code,
            )
            for entry in entries
        ]
        if _config is not None and _config.token_sidecar.enabled
        else []
    )
    _catalog.record_usage_batch(
        analytics_rows,
        event_ids=[entry.event_id for entry in entries],
        outbox_rows=outbox_rows,
    )


def _flush_usage() -> None:
    """Sync. Safe from any context — no-op if there's nothing to flush.
    Called from _flush_loop, _kill_vllm, and lifespan exit.

    Drains two buffers:
      - `request_count_delta` for the currently-resident alias (legacy
        per-resident counter on `models.request_count`).
      - `usage_rows`, now an in-memory retry buffer populated only when an
        immediate SQLite transaction fails.

    Analytics and, when token_sidecar is enabled, `pg_usage_outbox` are
    committed atomically. Rows leave the retry deque only after that commit.
    """
    if _catalog is None:
        return
    alias = _runtime.resident_alias
    delta = _runtime.request_count_delta
    if alias is not None and delta > 0:
        _catalog.bump_usage(alias, _runtime.last_used_at, delta)
        _runtime.request_count_delta = 0
    if not _runtime.usage_rows:
        return
    # This deque is now a retry buffer used only after an immediate durable
    # write fails. Peek first and remove rows only after the atomic SQLite
    # transaction commits; event IDs make a retry idempotent.
    entries = list(_runtime.usage_rows)
    _persist_usage_entries(entries)
    for _entry in entries:
        _runtime.usage_rows.popleft()


def _flush_usage_best_effort(context: str) -> None:
    """Flush usage without letting catalog/SQLite failures block teardown."""
    try:
        _flush_usage()
    except Exception as e:
        logger.warning("Usage flush failed during %s: %s", context, e)


async def _flush_loop() -> None:
    """Retry failed immediate usage writes every 30s while the manager is up."""
    while True:
        await asyncio.sleep(30)
        try:
            _flush_usage()
        except Exception as e:
            logger.warning("Usage flush failed: %s", e)


# ──────────────────────────────────────────────
# Postgres token-sidecar outbox flush
# ──────────────────────────────────────────────

async def _pg_flush_once() -> None:
    """Drain one batch of outbox rows to postgres.

    Order of operations:
      1. Prune the outbox if it's over the configured cap (independent of
         postgres reachability — keeps SQLite bounded during long outages).
      2. SELECT up to `batch_size` oldest rows.
      3. Write them via PgWriter.write_batch (ON CONFLICT DO NOTHING).
      4. Delete the SQLite rows whose write succeeded.

    Errors raise; the caller (loop / best-effort shutdown helper) catches.
    """
    global _pg_last_flush_at, _pg_last_flush_count, _pg_last_error
    if _catalog is None or _pg_writer is None or _config is None:
        return
    cfg = _config.token_sidecar
    pending = _catalog.count_pg_outbox()
    if pending > cfg.max_outbox_rows:
        dropped = _catalog.prune_pg_outbox(keep=cfg.max_outbox_rows)
        logger.warning(
            "Token-sidecar outbox over cap (%d > %d); dropped %d oldest rows",
            pending, cfg.max_outbox_rows, dropped,
        )
    rows = _catalog.peek_pg_outbox(limit=cfg.batch_size)
    if not rows:
        return
    try:
        sent = await _pg_writer.write_batch(rows)
    except Exception as e:
        _pg_last_error = f"{type(e).__name__}: {e}"
        raise
    _catalog.delete_pg_outbox([r["id"] for r in rows])
    _pg_last_flush_at = time.time()
    _pg_last_flush_count = sent
    _pg_last_error = None


async def _pg_flush_loop() -> None:
    """Background task: drain the outbox every `flush_interval_seconds`."""
    if _config is None or not _config.token_sidecar.enabled:
        return
    interval = max(1, int(_config.token_sidecar.flush_interval_seconds))
    while True:
        await asyncio.sleep(interval)
        try:
            await _pg_flush_once()
        except Exception as e:
            logger.warning("Postgres usage flush failed: %s", e)


async def _pg_flush_once_best_effort(context: str) -> None:
    """Flush the postgres outbox without raising. Used during shutdown."""
    try:
        await _pg_flush_once()
    except Exception as e:
        logger.warning("Postgres flush failed during %s: %s", context, e)


# ──────────────────────────────────────────────
# Idle eviction loop (Phase 2 §5.6)
# ──────────────────────────────────────────────

async def _eviction_loop() -> None:
    """Periodically unload the resident model when it's been idle past the
    configured threshold. Returns immediately if eviction is disabled
    (idle_unload_seconds=null)."""
    if _config is None or _config.server.idle_unload_seconds is None:
        return
    threshold = _config.server.idle_unload_seconds
    period = max(5, min(threshold // 4, 30))
    logger.info("Idle eviction enabled (threshold=%ds, period=%ds)", threshold, period)
    while True:
        await asyncio.sleep(period)
        coordinator = _coordinator
        if coordinator is None:
            continue
        try:
            evicted = await coordinator.evict_if_idle(
                threshold,
                timeout_seconds=_config.server.swap_queue_timeout_seconds,
            )
            if evicted:
                logger.info(
                    "Idle eviction safely drained the resident model "
                    "(threshold %ds)",
                    threshold,
                )
        except QueueTimeout as exc:
            logger.warning("Idle eviction drain timed out: %s", exc)


# ──────────────────────────────────────────────
# Request-model resolver (Phase 2 §5.5)
# ──────────────────────────────────────────────

# org/repo form — exactly one '/', conservative chars on both halves.
_RAW_HF_ID_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _warn_legacy_alias_once(alias: str) -> None:
    if alias in _legacy_alias_warned:
        return
    _legacy_alias_warned.add(alias)
    logger.warning(
        "Legacy MODEL_ALIASES used for '%s'; prefer config.yaml or "
        "/manager/install (Phase 4)", alias,
    )


def _synthesize_profile(model_id: str) -> ResolvedProfile:
    """Build an inline ResolvedProfile for a raw HF id or a legacy alias.
    Uses defaults from the loaded config; storage falls back to storage.default.
    Synthetic profiles are always vLLM — llama.cpp installs require the
    full install path so a `gguf_filename` is on file."""
    if _config is None:
        raise RuntimeError("config not loaded")
    storage_name = _config.storage.default
    storage_path = next(
        l.path for l in _config.storage.locations if l.name == storage_name
    )
    return ResolvedProfile(
        alias=model_id,  # synthetic — only used for log lines and status
        served_model_name=model_id,
        engine_model_path=model_id,
        gpus="all",
        quantization=None,
        max_model_len=_config.defaults.max_model_len,
        gpu_memory_utilization=_config.defaults.gpu_memory_utilization,
        trust_remote_code=_config.defaults.trust_remote_code,
        storage_name=storage_name,
        storage_path=storage_path,
        extra_args=(),
        revision="main",
        upstream_model=model_id,
    )


def _config_alias_for(requested: str) -> Optional[str]:
    if _config is None:
        return None
    for m in _config.models:
        if m.alias == requested:
            return m.alias
    folded = requested.casefold()
    for m in _config.models:
        if m.alias.casefold() == folded:
            return m.alias
    return None


def _legacy_alias_for(requested: str) -> Optional[str]:
    if requested in MODEL_ALIASES:
        return requested
    folded = requested.casefold()
    for alias in MODEL_ALIASES:
        if alias.casefold() == folded:
            return alias
    return None


def _ui_install_row_for_alias(requested: str):
    if _catalog is None:
        return None
    row = _catalog.get_model(requested)
    if row is None:
        row = _catalog.get_model_case_insensitive(requested)
    if row is not None and row.source == "ui_install":
        return row
    return None


def _ui_install_rows_for_hf_id(requested: str):
    if _catalog is None:
        return []
    return [
        row for row in _catalog.lookup_by_hf_id_case_insensitive(requested)
        if row.source == "ui_install"
    ]


def _request_is_aliased(requested: str, profile: ResolvedProfile) -> bool:
    """Whether a load request resolved to a managed alias/profile.

    Raw HF passthrough synthesizes alias == requested. Managed aliases may be
    addressed case-insensitively or by HF id, but still resolve to a canonical
    alias and should ignore legacy tp/gpu_mem/extra_args overrides.
    """
    return (
        _config_alias_for(requested) is not None
        or _ui_install_row_for_alias(requested) is not None
        or _legacy_alias_for(requested) is not None
        or profile.alias != requested
    )


def _resolve_request_model(requested: str) -> ResolvedProfile:
    """Five-tier lookup: config alias → catalog ui_install alias → legacy
    MODEL_ALIASES dict → installed catalog HF id → raw HF id passthrough
    (gated by _RAW_HF_ID_RE or absolute existing path). Anything else raises
    KeyError, which the caller translates to 404. See plans/phase_2.md §5.5."""
    if _config is None or _catalog is None:
        raise RuntimeError("manager not initialized")

    # Tier 1 — config alias
    config_alias = _config_alias_for(requested)
    if config_alias is not None:
        try:
            return resolve_profile(config_alias, _config, _catalog)
        except ProfileNotReady as e:
            raise HTTPException(status_code=409, detail=str(e))
    # Tier 2 — catalog ui_install row (must be fully installed)
    row = _ui_install_row_for_alias(requested)
    if row is not None:
        if row.status != "installed":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"alias '{row.alias}' is not ready (status='{row.status}'); "
                    "complete the install before routing requests to it"
                ),
            )
        return resolve_profile(row.alias, _config, _catalog)
    # Tier 3 — legacy MODEL_ALIASES dict
    legacy_alias = _legacy_alias_for(requested)
    if legacy_alias is not None:
        _warn_legacy_alias_once(legacy_alias)
        return _synthesize_profile(MODEL_ALIASES[legacy_alias])

    is_hf_id = _RAW_HF_ID_RE.match(requested) is not None
    # Tier 4 — an installed catalog row can also be addressed by HF id. This
    # keeps OpenAI-compatible clients that send the provider model id on the
    # saved alias profile instead of launching an unsafe raw profile.
    if is_hf_id:
        rows = _ui_install_rows_for_hf_id(requested)
        for hf_row in rows:
            if hf_row.status == "installed":
                logger.info(
                    "Resolving HF model id '%s' via installed alias '%s'",
                    requested,
                    hf_row.alias,
                )
                return resolve_profile(hf_row.alias, _config, _catalog)
        for hf_row in rows:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"model '{requested}' is installed as alias "
                    f"'{hf_row.alias}' but is not ready "
                    f"(status='{hf_row.status}')"
                ),
            )

    # Tier 5 — raw HF id or absolute path
    if _RAW_HF_ID_RE.match(requested) or (
        requested.startswith("/") and os.path.isdir(requested)
    ):
        logger.info("Resolving '%s' as raw model id (no alias match)", requested)
        return _synthesize_profile(requested)
    raise KeyError(requested)


def _apply_legacy_overrides(
    profile: ResolvedProfile,
    legacy: dict,
) -> ResolvedProfile:
    """Honor legacy {tp, gpu_mem, extra_args} on top of a synthesized raw-id
    profile so `vllm-ctl load <raw-id> --gpu-mem 0.85` keeps working. Aliased
    profiles are NOT overridden — see /manager/load shim."""
    updates = {}
    if "gpu_mem" in legacy:
        updates["gpu_memory_utilization"] = float(legacy["gpu_mem"])
    if "tp" in legacy:
        n = int(legacy["tp"])
        updates["gpus"] = list(range(n))
    if "extra_args" in legacy:
        updates["extra_args"] = tuple(legacy["extra_args"])
    return dataclasses.replace(profile, **updates)


# ──────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────

@dataclass
class ReloadResult:
    sync: SyncResult
    reconcile: ReconcileResult

    def to_dict(self) -> dict:
        return {"sync": asdict(self.sync), "reconcile": asdict(self.reconcile)}


def _install_sighup_handler() -> None:
    """Install SIGHUP → _reload_config. Skips cleanly under TestClient or
    on platforms without signal-handler support."""
    if sys.platform == "win32":
        logger.debug("SIGHUP not available on Windows; skipping.")
        return
    if threading.current_thread() is not threading.main_thread():
        logger.debug("SIGHUP handler skipped: not on main thread (likely TestClient).")
        return
    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, _on_sighup)
        logger.info("SIGHUP handler installed.")
    except (NotImplementedError, RuntimeError) as e:
        logger.warning("SIGHUP install failed: %s", e)


def _on_sighup() -> None:
    async def runner():
        try:
            res = await _reload_config()
            logger.info("SIGHUP reload complete: %s", res.to_dict())
        except Exception as e:
            logger.error("SIGHUP reload failed: %s", e)
    asyncio.create_task(runner())


async def _reload_config() -> ReloadResult:
    """Reread config, re-sync catalog. Sync first, swap _config last so a
    failure leaves both DB state and in-memory config untouched.
    Resident vLLM model is not affected."""
    global _config
    if _catalog is None:
        raise RuntimeError("catalog not initialized")
    new = load_config()
    sync, rec = _catalog.apply_config(
        new.models,
        new.storage.default,
        {l.name: l.path for l in new.storage.locations},
    )
    _config = new
    if _coordinator is not None:
        await _coordinator.reconfigure(
            configured_max_concurrency=new.server.max_concurrency,
            max_queue_depth=new.server.max_queue_depth,
        )
    return ReloadResult(sync=sync, reconcile=rec)


@asynccontextmanager
async def manager_lifespan(
    cfg: Config | None = None,
    *,
    install_signals: bool = True,
    spawn_background: bool = True,
):
    """Process-level startup/teardown for the manager.

    Phase 3: extracted from FastAPI's app lifespan so the same coroutine can
    drive both production (under asyncio.gather of two uvicorn servers) and
    tests (under a private event loop).

    Args:
      cfg: pre-loaded config, or None to load from MNEMOSYNE_* paths. The
        production path passes cfg from main() so port resolution can happen
        before async startup.
      install_signals: if False, skip _install_sighup_handler. Test fixtures
        pass False so SIGHUP isn't attached to a loop that's about to close.
      spawn_background: if False, don't spawn _eviction_task / _flush_task.
        Tests pass False because TestClient serves on a different loop and
        the tasks would never run.
    """
    global _config, _catalog, _runtime, _eviction_task, _flush_task
    global _pg_writer, _pg_flush_task, _pg_last_flush_at
    global _pg_last_flush_count, _pg_last_error
    global _coordinator, _fleet_instance_id, _fleet_snapshot_sequence
    if cfg is None:
        load_env()
        cfg = load_config()
    _config = cfg
    _catalog = open_catalog()
    _runtime = RuntimeState()
    _fleet_instance_id = uuid.uuid4().hex
    _fleet_snapshot_sequence = 0
    _legacy_alias_warned.clear()
    # Phase 5 — load vLLM-supported architectures for /manager/hf/search.
    # Primary: runtime introspection of vllm.model_executor.models.registry.
    # Fallback: bundled JSON snapshot at the repo root, regenerated by
    # scripts/refresh_arch_list.py after a vLLM bump.
    arch_fallback = Path(__file__).resolve().parent / "vllm_supported_architectures.json"
    archs, arch_source = hf_search.load_supported_architectures(arch_fallback)
    hf_search.set_supported_architectures(archs, arch_source)
    # Recover any in-flight downloads from a prior run BEFORE apply_config —
    # reconcile inside apply_config may then promote any whose snapshot is
    # actually complete on disk back to 'installed'. Reverse order would
    # let reconcile promote first, then recovery would clobber back.
    recovered = downloader.reap_orphans_on_startup(_catalog)
    if recovered:
        logger.warning(
            "Recovered %d interrupted download(s) from previous run — "
            "marked partial; user can retry from UI/CLI.",
            recovered,
        )
    sync, rec = _catalog.apply_config(
        _config.models,
        _config.storage.default,
        {l.name: l.path for l in _config.storage.locations},
    )
    logger.info(
        "Catalog ready: sync=%s reconcile=%s",
        asdict(sync), asdict(rec),
    )
    if install_signals:
        _install_sighup_handler()

    if spawn_background:
        if _config.server.idle_unload_seconds is not None:
            _eviction_task = asyncio.create_task(_eviction_loop(), name="eviction")
        else:
            _eviction_task = None
            logger.info("Idle eviction disabled (idle_unload_seconds=null)")
        _flush_task = asyncio.create_task(_flush_loop(), name="usage-flush")
    else:
        _eviction_task = None
        _flush_task = None

    # Token-sidecar postgres writer + flush loop (optional). Lazy-import
    # so a broken psycopg install doesn't take down the whole manager.
    _pg_writer = None
    _pg_flush_task = None
    _pg_last_flush_at = None
    _pg_last_flush_count = 0
    _pg_last_error = None
    ts_cfg = _config.token_sidecar
    if ts_cfg.enabled:
        dsn = os.environ.get("TOKEN_SIDECAR_POSTGRES_DSN", "").strip()
        if not dsn:
            logger.warning(
                "token_sidecar.enabled=true but TOKEN_SIDECAR_POSTGRES_DSN "
                "is unset; sink disabled (rows will still buffer in SQLite)"
            )
        else:
            try:
                import pg_writer as pg_writer_mod
                _pg_writer = pg_writer_mod.PgWriter(
                    dsn=dsn,
                    node_id=ts_cfg.node_id,
                    connect_timeout=ts_cfg.connect_timeout_seconds,
                    logger=logger,
                )
                if spawn_background:
                    _pg_flush_task = asyncio.create_task(
                        _pg_flush_loop(), name="pg-usage-flush",
                    )
                logger.info(
                    "Token sidecar enabled (node_id=%s, batch=%d, interval=%ds)",
                    ts_cfg.node_id,
                    ts_cfg.batch_size,
                    ts_cfg.flush_interval_seconds,
                )
            except Exception as e:
                logger.error("Token sidecar setup failed: %s", e)
                _pg_writer = None
                _pg_flush_task = None

    logger.info(
        f"\n"
        f"  ┌─────────────────────────────────────────────────────┐\n"
        f"  │         Mnemosyne Inference (Phase 3) Ready         │\n"
        f"  │                                                     │\n"
        f"  │  Inference :{cfg.server.inference_port:<5} (LAN)    /v1/* + /health     │\n"
        f"  │  Admin     :{cfg.server.admin_port:<5}        /manager/* + /docs   │\n"
        f"  │  vLLM inner: 127.0.0.1:{VLLM_INNER_PORT:<5}                          │\n"
        f"  │                                                     │\n"
        f"  │  No model loaded. POST /manager/load first.         │\n"
        f"  └─────────────────────────────────────────────────────┘\n"
    )
    _coordinator = _new_coordinator()
    try:
        yield
    finally:
        # Cancel infinite tasks first so they don't fight teardown.
        for t in (_eviction_task, _flush_task, _pg_flush_task):
            if t is not None and not t.done():
                t.cancel()
        for t in (_eviction_task, _flush_task, _pg_flush_task):
            if t is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        _eviction_task = None
        _flush_task = None
        _pg_flush_task = None
        # SIGTERM all in-flight installs so we exit cleanly. Worker's
        # prctl(PDEATHSIG) is belt-and-suspenders on Linux; this is the
        # primary cleanup path.
        for alias in list(downloader._active.keys()):
            try:
                downloader.cancel_install(alias)
            except Exception as e:
                logger.warning("cancel_install(%s) during shutdown: %s", alias, e)
        hf_search.shutdown_search_pool()
        coordinator_stopped = True
        if _coordinator is not None:
            try:
                await _coordinator.shutdown(
                    timeout_seconds=(
                        _config.server.swap_queue_timeout_seconds
                        if _config is not None
                        else 300
                    )
                )
            except Exception as exc:
                # A drain timeout intentionally leaves the engine untouched;
                # killing here would violate the full-stream lease invariant.
                coordinator_stopped = False
                logger.error("Safe coordinator shutdown incomplete: %s", exc)
        # Final flush after all successfully drained request leases settle.
        _flush_usage_best_effort("lifespan shutdown")
        # Last chance for the outbox to ship rows that just landed in the
        # final SQLite flush above. Best-effort: never block teardown on
        # postgres reachability.
        if _pg_writer is not None:
            await _pg_flush_once_best_effort("lifespan shutdown")
            with contextlib.suppress(Exception):
                await _pg_writer.close()
            _pg_writer = None
        if coordinator_stopped:
            logger.info("Shutting down — engine is safely drained and stopped")
            _kill_vllm()
        else:
            logger.error(
                "Engine stop skipped because active request drain was not proven"
            )
        _coordinator = None
        if _catalog is not None:
            _catalog.close()
            _catalog = None
        _config = None




# ──────────────────────────────────────────────
# Routers (Phase 3 §5.10)
# ──────────────────────────────────────────────
# Routers are populated by the @router.<verb>(...) decorators below. The two
# FastAPI apps that include them are constructed at the BOTTOM of this file —
# include_router() snapshots the route table at call time, so decorators
# registered after include_router() run are silently ignored.
health_router = APIRouter()
inference_router = APIRouter()
fleet_router = APIRouter()
admin_router = APIRouter()
docs_router = APIRouter()
ui_router = APIRouter()


@docs_router.get("/openapi.json", include_in_schema=False)
async def _admin_openapi():
    # Forward-reference admin_app — resolved at call time, after construction.
    return admin_app.openapi()


@docs_router.get("/docs", include_in_schema=False)
async def _admin_docs():
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Mnemosyne Admin")


@docs_router.get("/redoc", include_in_schema=False)
async def _admin_redoc():
    return get_redoc_html(openapi_url="/openapi.json", title="Mnemosyne Admin")


# ──────────────────────────────────────────────
# Manager control endpoints
# ──────────────────────────────────────────────

@admin_router.get("/manager/status", tags=["manager"])
async def status():
    """Current state of the manager and loaded model.

    Phase 0/1 keys (loaded_model, loading, vllm_pid, loaded_at,
    loaded_at_human, tp_size, gpu_mem_util, inner_endpoint) are preserved.
    Phase 2 adds resident-profile detail and idle-eviction countdown.
    The llama.cpp integration adds `backend`, `gguf_filename`, and
    `engine_pid` (vllm_pid is kept as a deprecated alias for one release).
    """
    profile = _runtime.resident_profile
    coordinator_view = (
        await _coordinator.status() if _coordinator is not None else None
    )
    loaded_model = profile.served_model_name if profile else None
    load_time = _runtime.model_load_time
    engine_pid = (
        vllm_process.pid
        if vllm_process and vllm_process.poll() is None
        else None
    )

    last_used = _runtime.last_used_at
    idle_seconds = (time.time() - last_used) if last_used else None
    threshold = _config.server.idle_unload_seconds if _config else None
    seconds_until_eviction = (
        max(0, threshold - idle_seconds)
        if threshold is not None and idle_seconds is not None
        else None
    )

    return {
        # Phase 0/1 keys
        "loaded_model":   loaded_model,
        "loading":        _loading_target is not None,
        "vllm_pid":       engine_pid,    # deprecated alias of engine_pid
        "engine_pid":     engine_pid,
        "loaded_at":      load_time,
        "loaded_at_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(load_time)) if load_time else None,
        "tp_size":        _runtime.resident_tp_size,
        "gpu_mem_util":   profile.gpu_memory_utilization if profile else None,
        "inner_endpoint": f"http://{VLLM_INNER_HOST}:{VLLM_INNER_PORT}",
        # Phase 2 additions
        "alias":            profile.alias if profile else None,
        "gpus":             profile.gpus if profile else None,
        "quantization":     profile.quantization if profile else None,
        "max_model_len":    profile.max_model_len if profile else None,
        "storage_location": profile.storage_name if profile else None,
        "last_used_at":     last_used,
        "idle_seconds":     round(idle_seconds, 1) if idle_seconds is not None else None,
        "seconds_until_eviction": (
            round(seconds_until_eviction, 1) if seconds_until_eviction is not None else None
        ),
        "inflight_requests": _runtime.inflight,
        "swap_target":       _loading_target,
        "residency": (
            {
                "state": coordinator_view.state.value,
                "epoch": coordinator_view.epoch,
                "queued": coordinator_view.queued,
                "queue_limit": coordinator_view.queue_limit,
                "transition_target": coordinator_view.transition_target,
                "accepting": coordinator_view.accepting,
                "authoritative": coordinator_view.authoritative,
            }
            if coordinator_view is not None
            else None
        ),
        "max_concurrency": (
            coordinator_view.configured_max_concurrency
            if coordinator_view is not None
            else None
        ),
        "effective_concurrency": (
            coordinator_view.effective_limit
            if coordinator_view is not None
            else 1
        ),
        # Backend dispatch surface
        "backend":           profile.backend if profile else None,
        "model_kind":        profile.kind if profile else None,
        "capabilities":      list(profile.capabilities) if profile else [],
        "gguf_filename": (
            profile.gguf_filename
            if profile and profile.backend == "llama.cpp"
            else None
        ),
        # Phase 5 additions — surface fallback when vLLM registry import broke.
        "vllm_arch_count":   hf_search.get_arch_count(),
        "vllm_arch_source":  hf_search.get_arch_source(),
        # Token-sidecar postgres path. `enabled` reflects config; `writer_ready`
        # tells you whether the DSN was set + the writer imported cleanly.
        "token_sidecar": {
            "enabled":          _config.token_sidecar.enabled if _config else False,
            "node_id":          _config.token_sidecar.node_id if _config else "",
            "writer_ready":     _pg_writer is not None,
            "outbox_pending":   _catalog.count_pg_outbox() if _catalog else 0,
            "last_flush_at":    _pg_last_flush_at,
            "last_flush_count": _pg_last_flush_count,
            "last_error":       _pg_last_error,
        },
    }


@admin_router.post("/manager/reload", tags=["manager"])
async def reload_endpoint():
    """Reread config.yaml, re-sync the catalog. Resident vLLM model is
    untouched. Soft-fails with 400 on bad config (existing config remains
    loaded)."""
    try:
        result = await _reload_config()
    except Exception as e:
        logger.error("reload failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    return result.to_dict()


@admin_router.get("/manager/profiles", tags=["manager"])
async def list_profiles():
    """List config-defined aliases. Reflects YAML state, not the catalog."""
    if _config is None:
        return {"profiles": []}
    return {
        "profiles": [
            {
                "alias": m.alias,
                "model": m.model,
                "quantization": m.quantization,
                "gpus": m.gpus,
                "storage": m.storage if m.storage is not None else _config.storage.default,
                "max_model_len": m.max_model_len,
                "extra_args": list(m.extra_args),
            }
            for m in _config.models
        ]
    }


@admin_router.get("/manager/storage", tags=["manager"])
async def list_storage():
    """List configured storage locations with current free space."""
    if _config is None:
        return {"locations": []}
    out = []
    for loc in _config.storage.locations:
        free_bytes: Optional[int] = None
        total_bytes: Optional[int] = None
        try:
            usage = shutil.disk_usage(loc.path)
            free_bytes = usage.free
            total_bytes = usage.total
        except (FileNotFoundError, OSError):
            pass
        writable = os.path.isdir(loc.path) and os.access(loc.path, os.W_OK)
        out.append({
            "name": loc.name,
            "path": loc.path,
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "writable": writable,
            "is_default": loc.name == _config.storage.default,
        })
    return {"locations": out}


def _parse_gpu_query(stdout: str) -> list[dict]:
    """Parse nvidia-smi CSV output for the read-only dashboard GPU endpoint."""
    gpus = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        name = ",".join(parts[1:-3]).strip()
        try:
            gpus.append({
                "index": int(parts[0]),
                "name": name,
                "memory_used_mb": int(parts[-3]),
                "memory_total_mb": int(parts[-2]),
                "utilization_pct": int(parts[-1]),
            })
        except ValueError:
            logger.warning("Skipping unparsable nvidia-smi row: %r", line)
    return gpus


@admin_router.get("/manager/gpu", tags=["manager"])
async def gpu_status():
    """Best-effort read-only GPU visibility for the admin dashboard.

    Development hosts often lack nvidia-smi; fail closed to an empty response
    instead of making the dashboard error.
    """
    cmd = [
        "nvidia-smi",
        "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {"available": False, "gpus": []}
    if result.returncode != 0:
        return {"available": False, "gpus": []}
    gpus = _parse_gpu_query(result.stdout)
    return {"available": bool(gpus), "gpus": gpus}


@admin_router.get("/manager/hf/files", tags=["manager"])
async def hf_files_route(
    model_id: str = Query(..., description="HuggingFace model id (org/repo)"),
    revision: Optional[str] = Query(None, description="Optional revision/sha; default = repo's default branch"),
):
    """Probe an HF repo for installable file groups (the install form's
    GGUF dropdown). Returns recommended_backend + sized GGUF candidates.
    """
    try:
        return hf_search.fetch_repo_files(model_id, revision=revision)
    except hf_search.HFSearchError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@admin_router.get("/manager/hf/search", tags=["manager"])
async def hf_search_route(
    q: str = Query("", description="Search query; blank returns top models for the chosen sort"),
    limit: int = Query(20, ge=1, le=50),
    page: int = Query(1, ge=1, le=20),
    filter_compat: bool = Query(False),
    sort: str = Query("trending", description="trending | downloads | likes | recent"),
    pipeline_tags: Optional[str] = Query(
        None,
        description=(
            "CSV of pipeline tags to include "
            "(text-generation, image-text-to-text, audio-text-to-text, any-to-any, text-to-image). "
            "Defaults to all five."
        ),
    ),
    include_vision: Optional[bool] = Query(
        None,
        description="Legacy alias: true → text+vision, false → text only. Ignored when pipeline_tags is set.",
    ),
):
    """Search HuggingFace Hub for vLLM-compatible models.

    Returns both compatible and incompatible results (incompatible flagged
    with `compat_reason`). Pass `filter_compat=true` to drop incompatible
    rows server-side. `sort` defaults to `trending` (matches huggingface.co's
    homepage). `pipeline_tags` lets the caller restrict modalities; the
    legacy `include_vision` flag is honored when `pipeline_tags` is omitted.
    """
    tags_list: Optional[list[str]] = None
    if pipeline_tags is not None:
        tags_list = [t.strip() for t in pipeline_tags.split(",") if t.strip()]
    try:
        return await hf_search.run_search(
            q=q,
            limit=limit,
            page=page,
            include_vision=include_vision,
            filter_compat=filter_compat,
            pipeline_tags=tags_list,
            sort=sort,
        )
    except hf_search.HFSearchError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


def _parse_strict_bool(raw: str, *, field: str) -> bool:
    s = raw.lower()
    if s in ("true", "1"):
        return True
    if s in ("false", "0"):
        return False
    raise HTTPException(
        status_code=422,
        detail=f"{field} must be true/false/1/0, got {raw!r}",
    )


@admin_router.get("/manager/catalog", tags=["manager"])
async def list_catalog(include_cache_only: str = Query("false")):
    """List catalog rows. Default-excludes synthetic cache-only rows."""
    include = _parse_strict_bool(include_cache_only, field="include_cache_only")
    if _catalog is None:
        return {"models": []}
    rows = _catalog.list_models()
    if not include:
        rows = [r for r in rows if not is_cache_only_alias(r.alias)]
    return {"models": [r.to_api_dict() for r in rows]}


@admin_router.post("/manager/load", tags=["manager"])
async def load_model(request: Request):
    """
    Load a model — Phase 2 alias-aware shim over `ensure_loaded`.

    Aliased payload (config alias, ui_install row, or legacy MODEL_ALIASES key):
        {"model": "qwen-72b-awq"}
    Legacy raw-id payload (Phase 0 compatibility):
        {"model": "Qwen/Qwen2.5-7B-Instruct", "tp": 1, "gpu_mem": 0.85,
         "extra_args": ["--max-model-len", "32768"]}

    For aliases, the resolved profile is authoritative — tp/gpu_mem/extra_args
    on the payload are ignored with a warning (PRD §5.1: config wins).
    For raw IDs, the legacy params override the synthesized profile defaults.
    """
    body = await request.json()
    requested = body.get("model")
    if not requested:
        raise HTTPException(status_code=400, detail="'model' field required")

    try:
        profile = _resolve_request_model(requested)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown alias '{requested}'")

    is_aliased = _request_is_aliased(requested, profile)
    legacy_params = {
        k: body[k] for k in ("tp", "gpu_mem", "extra_args") if k in body
    }
    if is_aliased and legacy_params:
        logger.warning(
            "Ignoring %s — profile '%s' wins (PRD §5.1)",
            sorted(legacy_params), profile.alias,
        )
    elif not is_aliased and legacy_params:
        profile = _apply_legacy_overrides(profile, legacy_params)

    if _config is None:
        swap_budget = 300
    elif profile.kind == "image":
        swap_budget = max(
            _config.server.swap_queue_timeout_seconds,
            _config.server.startup_timeout_seconds,
        )
    else:
        swap_budget = _config.server.swap_queue_timeout_seconds
    deadline = time.monotonic() + swap_budget
    await ensure_loaded(profile, deadline)
    return {
        "status": "loaded",
        "alias": profile.alias,
        "model": profile.served_model_name,
        "backend": profile.backend,
    }


@admin_router.post("/manager/unload", tags=["manager"])
async def unload_model():
    """Drain active response leases, then unload and free GPU memory."""
    coordinator = _get_coordinator()
    before = await coordinator.status()
    if (
        before.resident_profile is None
        and before.state == CoordinatorState.IDLE
        and before.queued == 0
    ):
        return {"status": "nothing to unload"}
    was_alias = (
        before.resident_profile.alias
        if before.resident_profile is not None
        else _loading_target
    )
    try:
        await coordinator.unload(
            timeout_seconds=(
                _config.server.swap_queue_timeout_seconds if _config else 300
            )
        )
    except QueueTimeout as exc:
        raise HTTPException(504, "timeout draining active model requests") from exc
    except ResidencyError as exc:
        raise HTTPException(503, "safe engine unload failed") from exc
    return {
        "status": "unloaded",
        "was": was_alias,
    }


@admin_router.get("/manager/models", tags=["manager"])
async def list_cached_models():
    """List models already downloaded in the HuggingFace cache volume."""
    hub_cache = os.path.join(HF_HOME, "hub")
    models = []
    if os.path.isdir(hub_cache):
        for entry in os.listdir(hub_cache):
            if entry.startswith("models--"):
                model_id = entry[len("models--"):].replace("--", "/")
                # Try to get size
                model_path = os.path.join(hub_cache, entry)
                try:
                    size_bytes = sum(
                        f.stat().st_size
                        for f in os.scandir(model_path)
                        if f.is_file()
                    )
                    # Recurse one level for snapshots
                    for sub in os.scandir(model_path):
                        if sub.is_dir():
                            for f in os.scandir(sub.path):
                                if f.is_file():
                                    size_bytes += f.stat().st_size
                    size_gb = round(size_bytes / 1e9, 1)
                except Exception:
                    size_gb = None
                models.append({"model": model_id, "size_gb": size_gb})

    models.sort(key=lambda x: x["model"])
    return {"cached_models": models, "hf_cache": hub_cache}


# ──────────────────────────────────────────────
# Install / download endpoints (Phase 4)
# ──────────────────────────────────────────────


_LEGACY_DEFAULT_IGNORE = [
    "*.pt", "*.bin", "*.msgpack",
    "flax_model*", "tf_model*", "rust_model*",
]


class InstallRequest(BaseModel):
    alias: str
    model: str
    revision: str = "main"
    quantization: Optional[str] = None
    gpus: GpuPlan = "all"
    max_model_len: Optional[int] = None
    storage: Optional[str] = None
    extra_args: list[str] = Field(default_factory=list)
    size_estimate_gb: Optional[float] = None
    ignore_patterns: Optional[list[str]] = None
    # Optional explicit backend override; when omitted the install endpoint
    # auto-detects from HF siblings. "none" cannot be persisted.
    backend: Optional[str] = None
    gguf_filename: Optional[str] = None
    # SGLang Diffusion installs are image profiles.  `kind` is optional so
    # older clients remain valid; backend='sglang-diffusion' implies image.
    kind: Optional[str] = None
    image: Optional[config_mod.ImageDefaults] = None


class CatalogUpdateRequest(BaseModel):
    quantization: Optional[str] = None
    gpus: GpuPlan = "all"
    max_model_len: Optional[int] = None
    extra_args: list[str] = Field(default_factory=list)


def _resolve_storage(name: Optional[str]) -> tuple[str, str]:
    """Return (storage_name, storage_path). 400 on missing/unwritable."""
    if _config is None:
        raise HTTPException(503, "config not loaded")
    target = name if name is not None else _config.storage.default
    for loc in _config.storage.locations:
        if loc.name == target:
            if not os.path.isdir(loc.path):
                raise HTTPException(400, f"storage path '{loc.path}' does not exist")
            if not os.access(loc.path, os.W_OK):
                raise HTTPException(400, f"storage path '{loc.path}' is not writable")
            return loc.name, loc.path
    raise HTTPException(400, f"unknown storage location '{target}'")


def _free_space_check(storage_path: str, size_estimate_gb: Optional[float], model_id: str) -> None:
    if size_estimate_gb is None:
        logger.warning(
            "install for '%s': size_estimate_gb not provided — skipping free-space check",
            model_id,
        )
        return
    try:
        usage = shutil.disk_usage(storage_path)
    except OSError as e:
        logger.warning("disk_usage failed on '%s': %s", storage_path, e)
        return
    needed = size_estimate_gb * 1.1 * 1e9
    if usage.free < needed:
        raise HTTPException(
            400,
            f"insufficient free space at '{storage_path}': "
            f"have {usage.free / 1e9:.1f} GB, need ~{needed / 1e9:.1f} GB",
        )


def _gpus_to_json(gpus: GpuPlan) -> list:
    """Normalize a GpuPlan to a JSON-serializable list shape that
    catalog.start_install_tx accepts."""
    if gpus == "all":
        return "all"
    return list(gpus)


def _resolve_install_backend(
    request: InstallRequest,
    *,
    hf_token: Optional[str],
    skip_probe: bool = False,
) -> tuple[str, Optional[str]]:
    """Decide the backend for an install and validate the chosen GGUF
    filename when applicable.

    Returns (backend, gguf_filename). Raises HTTPException(400) on any
    inconsistency: backend=llama.cpp requires gguf_filename pointing at a
    real candidate; vLLM/SGLang forbid gguf_filename and require transformer
    weights when the probe runs. SGLang Diffusion is image-only.

    Probe behavior:
      - Normal /manager/install always probes the repo so GGUF-only repos
        cannot be silently queued as vLLM installs.
      - `skip_probe=True` is used by the legacy /manager/download shim,
        which historically defaults to vLLM with no probe.
    """
    explicit_backend = request.backend
    requested_filename = request.gguf_filename

    if explicit_backend not in (None, "vllm", "llama.cpp", "sglang-diffusion"):
        raise HTTPException(
            400,
            "backend must be 'vllm', 'llama.cpp', or "
            f"'sglang-diffusion', got {explicit_backend!r}",
        )
    if request.kind not in (None, "language", "image"):
        raise HTTPException(400, "kind must be 'language' or 'image'")

    if skip_probe:
        # Trust the caller (retry path / legacy /manager/download): honor
        # the explicit backend, but still enforce the gguf_filename ↔ backend
        # invariant.
        backend_choice = explicit_backend or "vllm"
        if backend_choice == "llama.cpp" and not requested_filename:
            raise HTTPException(
                400,
                "backend='llama.cpp' requires gguf_filename",
            )
        if backend_choice != "llama.cpp" and requested_filename:
            raise HTTPException(
                400,
                "gguf_filename is only valid when backend='llama.cpp'",
            )
        if backend_choice == "sglang-diffusion":
            if request.kind not in (None, "image"):
                raise HTTPException(400, "sglang-diffusion profiles must use kind='image'")
        elif request.kind == "image" or request.image is not None:
            raise HTTPException(
                400,
                "image profiles require backend='sglang-diffusion'",
            )
        return backend_choice, requested_filename
    try:
        probe_payload = hf_search.fetch_repo_files(
            request.model, revision=request.revision,
        )
    except hf_search.HFSearchError as e:
        raise HTTPException(e.status_code, e.detail)

    has_gguf = bool(probe_payload.get("has_gguf"))
    has_transformer = bool(probe_payload.get("has_transformer_weights"))
    candidates = probe_payload.get("gguf_candidates") or []
    primaries = {c["primary_filename"] for c in candidates}

    backend = explicit_backend or probe_payload.get("recommended_backend") or "vllm"
    if backend == "none":
        raise HTTPException(
            400,
            f"repo '{request.model}' has no supported weight files",
        )

    if backend == "llama.cpp":
        if request.kind == "image" or request.image is not None:
            raise HTTPException(400, "image profiles require backend='sglang-diffusion'")
        if not has_gguf:
            raise HTTPException(
                400,
                f"repo '{request.model}' has no .gguf siblings; cannot install via llama.cpp",
            )
        if not requested_filename:
            raise HTTPException(
                400,
                "backend='llama.cpp' requires gguf_filename — pick one from /manager/hf/files",
            )
        if requested_filename not in primaries:
            raise HTTPException(
                400,
                f"gguf_filename '{requested_filename}' is not a recognized "
                f"GGUF candidate for '{request.model}'",
            )
        return "llama.cpp", requested_filename

    # Transformer-based backends.
    if requested_filename:
        raise HTTPException(
            400,
            "gguf_filename is only valid when backend='llama.cpp'",
        )
    if not has_transformer:
        raise HTTPException(
            400,
            f"repo '{request.model}' has no .safetensors / .bin siblings; "
            "use backend='llama.cpp' with a gguf_filename instead",
        )
    if backend == "sglang-diffusion":
        if request.kind not in (None, "image"):
            raise HTTPException(400, "sglang-diffusion profiles must use kind='image'")
        return backend, None
    if request.kind == "image" or request.image is not None:
        raise HTTPException(400, "image profiles require backend='sglang-diffusion'")
    return "vllm", None


async def _install_internal(
    request: InstallRequest,
    *,
    hf_token_override: Optional[str] = None,
    allow_cache_only_alias: bool = False,
    skip_backend_probe: bool = False,
) -> dict:
    """Body of POST /manager/install, factored out so the legacy shim can
    reuse it without going through HTTP."""
    if _config is None or _catalog is None:
        raise HTTPException(503, "manager not initialized")

    alias = request.alias
    model_id = request.model

    # Validate alias shape (defense in depth — Pydantic does most). Synthetic
    # cache aliases are internal to the legacy /manager/download shim.
    if is_cache_only_alias(alias):
        if not allow_cache_only_alias:
            raise HTTPException(
                400,
                f"alias '{alias}' uses reserved synthetic cache namespace",
            )
    elif not config_mod._ALIAS_RE.match(alias):
        raise HTTPException(400, f"alias '{alias}' has invalid shape")

    # 1. Refuse if alias is in config.yaml — config wins.
    if any(m.alias == alias for m in _config.models):
        raise HTTPException(409, f"alias '{alias}' is defined in config.yaml; config wins")
    # 2. Refuse if alias is currently resident.
    if _runtime.resident_alias == alias:
        raise HTTPException(409, f"alias '{alias}' is currently loaded; unload first")
    # 3. Refuse if there is an active install for this alias.
    if downloader.is_active(alias):
        raise HTTPException(409, f"alias '{alias}' has an install in progress")

    storage_name, storage_path = _resolve_storage(request.storage)

    # 4. Refuse if (storage, hf_model_id) already has an active install.
    other = _catalog.find_active_for(storage_name, model_id)
    if other and other != alias:
        raise HTTPException(
            409,
            {
                "message": f"another install is in progress for '{model_id}' on storage '{storage_name}'",
                "conflict_alias": other,
            },
        )

    _free_space_check(storage_path, request.size_estimate_gb, model_id)

    hf_token = hf_token_override
    if hf_token is None:
        hf_token = os.environ.get("HUGGING_FACE_HUB_TOKEN") or None

    backend, gguf_filename = _resolve_install_backend(
        request, hf_token=hf_token, skip_probe=skip_backend_probe,
    )
    model_kind = "image" if backend == "sglang-diffusion" else "language"
    image_config = (
        (request.image or config_mod.ImageDefaults()).model_dump()
        if model_kind == "image"
        else None
    )

    gpus_for_catalog = _gpus_to_json(request.gpus)

    _catalog.start_install_tx(
        alias=alias,
        hf_model_id=model_id,
        source="ui_install",
        revision=request.revision,
        quantization=request.quantization,
        gpus=gpus_for_catalog,
        max_model_len=request.max_model_len,
        storage_location=storage_name,
        extra_args=list(request.extra_args),
        backend=backend,
        gguf_filename=gguf_filename,
        model_kind=model_kind,
        capabilities=list(
            config_mod.default_model_capabilities(
                backend=backend,
                kind=model_kind,
            )
        ),
        image_config=image_config,
    )

    cache_dir = os.path.join(storage_path, "hub")
    try:
        downloader.start_install(
            alias=alias,
            model_id=model_id,
            revision=request.revision,
            cache_dir=cache_dir,
            ignore_patterns=request.ignore_patterns,
            hf_token=hf_token,
            catalog=_catalog,
            storage_location=storage_name,
            gguf_primary_filename=gguf_filename,
        )
    except ConflictError as e:
        # Race: another worker came in between our checks and the spawn.
        # Roll back the catalog row we just inserted to avoid a stuck
        # 'queued' state.
        _catalog.mark_error(alias, "race with concurrent install")
        raise HTTPException(
            409,
            {"message": "concurrent install conflict", "conflict_alias": e.conflict_alias},
        )
    except Exception as e:
        _catalog.mark_error(alias, f"failed to spawn worker: {e}")
        raise HTTPException(500, f"failed to spawn worker: {e}")

    return {
        "alias": alias,
        "status": "queued",
        "backend": backend,
        "gguf_filename": gguf_filename,
        "poll": f"/manager/install/{alias}",
    }


@admin_router.post("/manager/install", status_code=202, tags=["installs"])
async def install_model(request: InstallRequest):
    """Install a model: queue a download, run it in a killable subprocess,
    and add an aliased catalog row.

    On 202: poll `/manager/install/{alias}` for status. The catalog row
    starts at status='queued'; on completion it transitions to 'installed'.
    """
    return await _install_internal(request)


@admin_router.post("/manager/install/{alias}/cancel", tags=["installs"])
async def cancel_install_route(alias: str):
    if not downloader.is_active(alias):
        raise HTTPException(404, f"no active install for alias '{alias}'")
    downloader.cancel_install(alias)
    return {"alias": alias, "status": "cancelling"}


def _wipe_cache_or_error(cache_dir: str) -> bool:
    if _config is None:
        raise HTTPException(503, "manager not initialized")
    try:
        return downloader.force_wipe_cache(
            cache_dir,
            allowed_roots=[loc.path for loc in _config.storage.locations],
        )
    except downloader.CacheWipeError as e:
        raise HTTPException(400, str(e)) from e


@admin_router.post("/manager/install/{alias}/retry", status_code=202, tags=["installs"])
async def retry_install_route(alias: str, force: bool = False):
    if _config is None or _catalog is None:
        raise HTTPException(503, "manager not initialized")
    row = _catalog.get_model(alias)
    if row is None or row.source != "ui_install":
        raise HTTPException(404, f"no installable row for alias '{alias}'")
    if downloader.is_active(alias):
        raise HTTPException(409, f"alias '{alias}' already has an active install")
    other = _catalog.find_active_for(row.storage_location, row.hf_model_id)
    if other and other != alias:
        raise HTTPException(
            409,
            {"message": "concurrent install conflict", "conflict_alias": other},
        )

    _, storage_path = _resolve_storage(row.storage_location)
    if force:
        _wipe_cache_or_error(repo_cache_dir(storage_path, row.hf_model_id))

    extra_args = json.loads(row.extra_args) if row.extra_args else []
    gpus = json.loads(row.gpus)
    return await _install_internal(
        InstallRequest(
            alias=alias,
            model=row.hf_model_id,
            revision=row.revision,
            quantization=row.quantization,
            gpus=gpus,
            max_model_len=row.max_model_len,
            storage=row.storage_location,
            extra_args=extra_args,
            backend=row.backend,
            gguf_filename=row.gguf_filename,
            kind=row.model_kind,
            image=(
                config_mod.ImageDefaults.model_validate(json.loads(row.image_config))
                if row.image_config
                else None
            ),
        ),
        # The row was validated when first installed; trust it on retry to
        # avoid an extra Hub round-trip and keep retries usable offline.
        skip_backend_probe=True,
    )


def _install_status_payload(alias: str) -> dict:
    """Compose the API shape for an install-status query."""
    if _catalog is None:
        raise HTTPException(503, "manager not initialized")
    row = _catalog.get_model(alias)
    if row is None:
        raise HTTPException(404, f"no install row for alias '{alias}'")
    download = _catalog.get_download(alias)
    payload = row.to_api_dict()
    payload["alias"] = alias
    if download is not None:
        payload["download"] = {
            "status": download.status,
            "started_at": download.started_at,
            "finished_at": download.finished_at,
            "bytes_downloaded": download.bytes_downloaded,
            "total_bytes": download.total_bytes,
            "error": download.error,
            "pid": download.pid,
        }
        if download.started_at:
            end = download.finished_at or int(time.time())
            payload["download"]["elapsed_seconds"] = round(end - download.started_at, 1)
    payload["active"] = downloader.is_active(alias)
    return payload


@admin_router.get("/manager/install/{alias}", tags=["installs"])
async def install_status_route(alias: str):
    return _install_status_payload(alias)


@admin_router.delete("/manager/install/{alias}/download", tags=["installs"])
async def clear_install_download(alias: str):
    """Clear a completed/failed download record by alias without deleting
    cached weights. Legacy synthetic rows are removed entirely to preserve the
    v0 clear-record behavior."""
    if _catalog is None:
        raise HTTPException(503, "manager not initialized")
    row = _catalog.get_model(alias)
    if row is None or row.source != "ui_install":
        raise HTTPException(404, f"no installable row for alias '{alias}'")
    download = _catalog.get_download(alias)
    if download is None:
        raise HTTPException(404, f"no download record for alias '{alias}'")
    if downloader.is_active(alias) or download.status in ("queued", "downloading"):
        raise HTTPException(
            409,
            "Download is in progress — cannot clear an active download",
        )
    if is_cache_only_alias(alias):
        _catalog.delete_install_row(alias)
        return {"cleared": row.hf_model_id, "alias": alias, "removed_row": True}
    deleted = _catalog.delete_downloads(alias)
    return {"cleared": row.hf_model_id, "alias": alias, "deleted_downloads": deleted}


@admin_router.patch("/manager/install/{alias}", tags=["installs"])
async def update_install_route(alias: str, request: CatalogUpdateRequest):
    if _catalog is None:
        raise HTTPException(503, "manager not initialized")
    row = _catalog.get_model(alias)
    if row is None:
        raise HTTPException(404, f"no install row for alias '{alias}'")
    if row.source != "ui_install":
        raise HTTPException(409, f"alias '{alias}' is defined in config.yaml; config wins")
    if is_cache_only_alias(alias):
        raise HTTPException(409, f"alias '{alias}' is cache-only; create an alias first")
    if downloader.is_active(alias):
        raise HTTPException(409, f"alias '{alias}' has an install in progress")
    if request.max_model_len is not None and request.max_model_len < 1:
        raise HTTPException(400, "max_model_len must be a positive integer or null")
    if isinstance(request.gpus, list):
        if not request.gpus:
            raise HTTPException(400, "gpus list must not be empty")
        if any((not isinstance(idx, int)) or isinstance(idx, bool) or idx < 0 for idx in request.gpus):
            raise HTTPException(400, "gpus must be 'all' or a list of non-negative integers")

    updated = _catalog.update_launch_settings(
        alias=alias,
        quantization=request.quantization,
        gpus=_gpus_to_json(request.gpus),
        max_model_len=request.max_model_len,
        extra_args=list(request.extra_args),
    )
    if updated is None:
        raise HTTPException(404, f"no editable install row for alias '{alias}'")
    return updated.to_api_dict()


def _check_aliased_delete_safety(row, exclude_alias: Optional[str] = None) -> None:
    """Refuse deletes when any sibling is resident or has an active install."""
    if _catalog is None:
        raise HTTPException(503, "manager not initialized")
    siblings = _catalog.find_repo_siblings(
        row.storage_location, row.hf_model_id, exclude_alias=None,
    )
    for s in siblings:
        if _runtime.resident_alias == s.alias:
            raise HTTPException(
                409,
                f"alias '{s.alias}' is currently loaded; unload first",
            )
        if downloader.is_active(s.alias):
            raise HTTPException(
                409,
                f"alias '{s.alias}' has an active install; cancel first",
            )


@admin_router.delete("/manager/install/{alias}/cache", tags=["installs"])
async def delete_install_cache(alias: str):
    """Wipe the on-disk cache for the alias's repo cache dir; mark every
    sibling 'partial'. Row stays. Used by the UI's 'remove from disk' action."""
    if _config is None or _catalog is None:
        raise HTTPException(503, "manager not initialized")
    row = _catalog.get_model(alias)
    if row is None or row.source != "ui_install":
        raise HTTPException(404, f"no installable row for alias '{alias}'")
    _check_aliased_delete_safety(row)

    _, storage_path = _resolve_storage(row.storage_location)
    cache_dir = repo_cache_dir(storage_path, row.hf_model_id)
    _wipe_cache_or_error(cache_dir)
    # Mark every sibling 'partial' — the wipe nuked their cache too.
    siblings = _catalog.find_repo_siblings(row.storage_location, row.hf_model_id)
    for s in siblings:
        _catalog.mark_partial(s.alias)
    return {"alias": alias, "status": "partial", "siblings_marked": [s.alias for s in siblings]}


@admin_router.delete("/manager/install/{alias}", tags=["installs"])
async def delete_install_full(alias: str):
    """Wipe the on-disk cache AND remove the catalog row entirely.
    Sibling rows are NOT removed; they get marked 'partial'."""
    if _config is None or _catalog is None:
        raise HTTPException(503, "manager not initialized")
    row = _catalog.get_model(alias)
    if row is None or row.source != "ui_install":
        raise HTTPException(404, f"no installable row for alias '{alias}'")
    _check_aliased_delete_safety(row)

    _, storage_path = _resolve_storage(row.storage_location)
    cache_dir = repo_cache_dir(storage_path, row.hf_model_id)
    _wipe_cache_or_error(cache_dir)
    siblings = _catalog.find_repo_siblings(
        row.storage_location, row.hf_model_id, exclude_alias=alias,
    )
    _catalog.delete_install_row(alias)
    for s in siblings:
        _catalog.mark_partial(s.alias)
    return {"alias": alias, "status": "removed"}


@admin_router.delete("/manager/cache/{model_id:path}", tags=["installs"])
async def delete_cache_legacy(model_id: str):
    """Legacy by-HF-id cache delete. Wipes the repo cache dir on every
    storage location it appears, marks aliased rows 'partial', deletes
    synthetic cache-only rows.
    """
    if _config is None or _catalog is None:
        raise HTTPException(503, "manager not initialized")
    rows = _catalog.lookup_by_hf_id(model_id)
    if not rows:
        raise HTTPException(404, f"no catalog rows for HF id '{model_id}'")

    # Refuse if any matched alias (or any sibling) is resident/active.
    for r in rows:
        if _runtime.resident_alias == r.alias:
            raise HTTPException(409, f"alias '{r.alias}' is currently loaded; unload first")
    other = _catalog.find_active_by_hf_id(model_id)
    if other:
        raise HTTPException(
            409,
            {"message": "active download in progress", "conflict_alias": other},
        )

    # Resolve every storage location before wiping anything so catalog and disk
    # cannot be partially mutated if one row references a bad location.
    storage_paths: dict[str, str] = {}
    for r in rows:
        if r.storage_location not in storage_paths:
            _, storage_path = _resolve_storage(r.storage_location)
            storage_paths[r.storage_location] = storage_path

    # Group by storage_location and wipe each repo dir once.
    wiped_locations: set[str] = set()
    for storage_location, storage_path in storage_paths.items():
        cache_dir = repo_cache_dir(storage_path, model_id)
        _wipe_cache_or_error(cache_dir)
        wiped_locations.add(storage_location)

    removed: list[str] = []
    marked_partial: list[str] = []
    for r in rows:
        if is_cache_only_alias(r.alias):
            _catalog.delete_install_row(r.alias)
            removed.append(r.alias)
        else:
            _catalog.mark_partial(r.alias)
            marked_partial.append(r.alias)
    return {
        "model": model_id,
        "wiped": sorted(wiped_locations),
        "removed_rows": removed,
        "marked_partial": marked_partial,
    }


# ── Legacy /manager/download* shim (Phase 4 §4c) ─────────────────────

@admin_router.post("/manager/download", tags=["downloads"])
async def download_model(request: Request):
    """Legacy v0 endpoint preserved for back-compat. Body shape:

    ```json
    {
      "model": "Qwen/Qwen2.5-72B-Instruct-AWQ",
      "revision": "main",
      "ignore_patterns": ["*.pt", "*.bin"],
      "hf_token": "hf_..."
    }
    ```

    Internally creates a synthetic-alias `ui_install` row keyed on the
    model id and runs through the same subprocess pipeline as
    /manager/install. `hf_token` is threaded into the subprocess env only;
    the manager's os.environ is not mutated.
    """
    body = await request.json()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="'model' field required")

    alias = synthetic_alias(model_id)
    revision = body.get("revision") or "main"
    hf_token = body.get("hf_token")
    if "ignore_patterns" in body:
        ignore = body.get("ignore_patterns")
    else:
        # v0 default: skip non-safetensor formats.
        ignore = list(_LEGACY_DEFAULT_IGNORE)

    # If the same synthetic alias is already running, return the v0
    # 'already_downloading' shape rather than 409.
    if downloader.is_active(alias):
        return {
            "status": "already_downloading",
            "model": model_id,
            "poll": f"/manager/download/{model_id.replace('/', '%2F')}",
        }

    try:
        await _install_internal(
            InstallRequest(
                alias=alias,
                model=model_id,
                revision=revision,
                gpus="all",
                ignore_patterns=ignore,
            ),
            hf_token_override=hf_token,
            allow_cache_only_alias=True,
            skip_backend_probe=True,
        )
    except HTTPException as e:
        # Surface as v0-shaped failure (no started_at / poll).
        raise

    return {
        "status": "started",
        "model": model_id,
        "poll": f"/manager/download/{model_id.replace('/', '%2F')}",
    }


def _legacy_download_payload(alias: str, model_id: str) -> dict:
    """v0-shaped status object built from the catalog. Returned shape
    matches Phase 0: {model, status, started_at, finished_at, path, error,
    revision, elapsed_seconds?}."""
    if _catalog is None:
        raise HTTPException(503, "manager not initialized")
    row = _catalog.get_model(alias)
    if row is None:
        raise HTTPException(404, f"No download record for '{model_id}'")
    download = _catalog.get_download(alias)
    out: dict = {
        "model": model_id,
        "status": _legacy_status(row.status, download.status if download else None),
        "started_at": download.started_at if download else None,
        "finished_at": download.finished_at if download else None,
        "path": row.cache_path,
        "error": download.error if download else None,
        "revision": row.revision,
    }
    if download and download.started_at:
        end = download.finished_at or int(time.time())
        out["elapsed_seconds"] = round(end - download.started_at, 1)
    return out


def _legacy_status(model_status: str, download_status: Optional[str]) -> str:
    """Map (models.status, downloads.status) → v0 enum
    {queued, downloading, complete, error}."""
    if model_status == "installed":
        return "complete"
    if model_status == "error":
        return "error"
    if download_status == "downloading":
        return "downloading"
    if download_status == "complete":
        return "complete"
    if download_status == "error":
        return "error"
    if download_status == "cancelled":
        return "error"
    return "queued"


@admin_router.get("/manager/download/{model_id:path}", tags=["downloads"])
async def download_status(model_id: str):
    """Legacy by-HF-id download status. Resolves via the synthetic alias
    so config or ui_install rows for the same HF id don't shadow it."""
    alias = synthetic_alias(model_id)
    return _legacy_download_payload(alias, model_id)


@admin_router.get("/manager/downloads", tags=["downloads"])
async def list_downloads():
    """List every catalog row that has an associated download (any status).
    Returns v0-shaped records for back-compat."""
    if _catalog is None:
        return {"downloads": []}
    out = []
    for r in _catalog.list_models():
        download = _catalog.get_download(r.alias)
        if download is None:
            continue
        record = {
            "model": r.hf_model_id,
            "alias": r.alias,
            "status": _legacy_status(r.status, download.status),
            "started_at": download.started_at,
            "finished_at": download.finished_at,
            "path": r.cache_path,
            "error": download.error,
            "revision": r.revision,
            "bytes_downloaded": download.bytes_downloaded,
            "total_bytes": download.total_bytes,
        }
        if download.started_at:
            end = download.finished_at or int(time.time())
            record["elapsed_seconds"] = round(end - download.started_at, 1)
        out.append(record)
    return {"downloads": out}


@admin_router.delete("/manager/download/{model_id:path}", tags=["downloads"])
async def clear_download_record(model_id: str):
    """Legacy clear-record. Removes the synthetic-alias row only — does
    not delete cached files. Refuses while a download is active."""
    if _catalog is None:
        raise HTTPException(503, "manager not initialized")
    alias = synthetic_alias(model_id)
    row = _catalog.get_model(alias)
    if row is None:
        raise HTTPException(404, f"No record for '{model_id}'")
    if downloader.is_active(alias):
        raise HTTPException(
            409, "Download is in progress — cannot clear an active download"
        )
    _catalog.delete_install_row(alias)
    return {"cleared": model_id}


@health_router.get("/health", tags=["manager"])
async def health():
    """Health check — always returns 200 from the manager itself."""
    return {
        "status": "ok",
        "model_loaded": _runtime.resident_alias is not None,
        "loading": _loading_target is not None,
    }


async def require_fleet_bearer(request: Request) -> None:
    """Authenticate Nyx independently from ordinary inference clients."""

    expected = os.environ.get("FLEET_API_KEY", "")
    if not expected:
        # Do not advertise the fleet inventory surface until it is explicitly
        # enrolled with a credential.
        raise HTTPException(404)
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
    if not secrets.compare_digest(auth[len("Bearer ") :], expected):
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})


def _fleet_node_id() -> str:
    if _config is None:
        return ""
    return (
        _config.fleet.node_id.strip()
        or _config.token_sidecar.node_id.strip()
    )


def _next_fleet_snapshot_sequence() -> int:
    global _fleet_snapshot_sequence
    with _fleet_snapshot_sequence_lock:
        value = _fleet_snapshot_sequence
        _fleet_snapshot_sequence += 1
    return value


def _fleet_profiles() -> list[ResolvedProfile]:
    """Resolve only declarative/installed profiles; never raw request paths."""

    if _config is None:
        return []
    aliases = [model.alias for model in _config.models]
    if _catalog is not None:
        aliases.extend(
            row.alias
            for row in _catalog.list_models()
            if row.source == "ui_install"
            and row.status == "installed"
            and not is_cache_only_alias(row.alias)
        )
    profiles: list[ResolvedProfile] = []
    for alias in dict.fromkeys(aliases):
        if not alias or len(alias) > 128:
            continue
        try:
            profile = resolve_profile(alias, _config, _catalog)
            _deployment_id, identity, _authoritative = _profile_deployment(profile)
        except (KeyError, ProfileNotReady, ValueError, TypeError):
            continue
        artifact = identity["artifact"]
        selected = artifact["selected_files"]
        if (
            len(identity["upstream_model"]) > 512
            or (
                identity["resolved_revision"] is not None
                and len(identity["resolved_revision"]) > 256
            )
            or (
                artifact["quantization"] is not None
                and len(artifact["quantization"]) > 128
            )
            or any(
                not item
                or len(item) > 512
                or os.path.isabs(item)
                or "\\" in item
                or ".." in Path(item.replace("\\", "/")).parts
                for item in selected
            )
        ):
            continue
        profiles.append(profile)
    return profiles


def _capacity_dict(
    spec: CapacitySpec,
    *,
    configured_max_concurrency: int | None,
    active: int,
    queued: int,
    admission_open: bool,
) -> dict:
    return effective_capacity(
        derived_limit=spec.derived_limit,
        configured_max_concurrency=configured_max_concurrency,
        active=active,
        queued=queued,
        source=spec.source,
        confidence=spec.confidence,
        admission_open=admission_open,
    ).to_dict()


def _static_profile_capacity(
    profile: ResolvedProfile,
    *,
    active: int,
    queued: int,
    admission_open: bool,
) -> dict:
    return derive_cuda_capacity(
        backend=profile.backend,
        extra_args=profile.extra_args,
        configured_max_concurrency=(
            _config.server.max_concurrency if _config is not None else None
        ),
        active=active,
        queued=queued,
        admission_open=admission_open,
    ).to_dict()


@fleet_router.get("/fleet/v1/snapshot", tags=["fleet"])
async def fleet_snapshot(
    response: Response,
    _auth: None = Depends(require_fleet_bearer),
):
    """Return one schema-v1, secret-free authoritative node snapshot."""

    response.headers["Cache-Control"] = "no-store"
    if not os.environ.get("INFERENCE_API_KEY", ""):
        # A discoverable node must not claim routable capacity when the
        # enrolled per-node inference credential would be ignored.
        raise HTTPException(
            503,
            detail={
                "code": "fleet_inference_auth_unconfigured",
                "message": (
                    "configure a node-specific INFERENCE_API_KEY before "
                    "enabling fleet discovery"
                ),
            },
        )
    node_id = _fleet_node_id()
    if not node_id:
        raise HTTPException(
            503,
            detail={
                "code": "fleet_node_id_unconfigured",
                "message": (
                    "configure fleet.node_id or token_sidecar.node_id "
                    "before enrollment"
                ),
            },
        )
    if len(node_id) > 128 or any(ord(char) < 0x20 for char in node_id):
        raise HTTPException(
            503,
            detail={
                "code": "fleet_node_id_invalid",
                "message": "configured fleet node identity is not protocol-safe",
            },
        )
    coordinator = _get_coordinator()
    status_view = await coordinator.status()

    engine_alive = bool(
        vllm_process is not None and vllm_process.poll() is None
    )
    process_mismatch = (
        (
            status_view.state == CoordinatorState.READY
            and status_view.resident_profile is not None
            and not engine_alive
        )
        or (
            status_view.state == CoordinatorState.IDLE
            and status_view.resident_profile is None
            and engine_alive
        )
    )
    authoritative = status_view.authoritative and not process_mismatch
    diagnostic_code = status_view.diagnostic_code
    if process_mismatch:
        diagnostic_code = (
            "engine_process_missing"
            if status_view.resident_profile is not None
            else "unexpected_engine_process"
        )
    accepting = status_view.accepting and authoritative
    root_admission_open = accepting and status_view.state in {
        CoordinatorState.IDLE,
        CoordinatorState.READY,
    }
    root_capacity = _capacity_dict(
        status_view.capacity,
        configured_max_concurrency=status_view.configured_max_concurrency,
        active=status_view.active,
        queued=status_view.queued,
        admission_open=root_admission_open,
    )

    deployments: list[dict] = []
    for profile in _fleet_profiles():
        deployment_id, identity, identity_authoritative = _profile_deployment(profile)
        warm = deployment_id == status_view.resident_deployment_id
        queued = status_view.queued_by_deployment.get(deployment_id, 0)
        if warm:
            deployment_capacity = _capacity_dict(
                status_view.capacity,
                configured_max_concurrency=status_view.configured_max_concurrency,
                active=status_view.active,
                queued=queued,
                admission_open=root_admission_open,
            )
        else:
            # Cold estimates remain visible while the node is accepting so
            # Nyx can safely choose a bounded drain/switch tier.
            deployment_capacity = _static_profile_capacity(
                profile,
                active=0,
                queued=queued,
                admission_open=accepting,
            )
        deployments.append(
            {
                "alias": profile.alias,
                "deployment_id": deployment_id,
                "identity": identity,
                "identity_confidence": (
                    "authoritative" if identity_authoritative else "unverified"
                ),
                "fleet_eligible": identity_authoritative,
                "loadable": True,
                "warm": warm,
                "capacity": deployment_capacity,
            }
        )
    deployments.sort(key=lambda item: item["alias"])

    if status_view.state == CoordinatorState.DEGRADED:
        health_state = "degraded"
    else:
        health_state = status_view.state.value
    if process_mismatch:
        health_state = "degraded"
    usage_enabled = bool(_config and _config.token_sidecar.enabled)
    return {
        "schema_version": FLEET_SCHEMA_VERSION,
        "snapshot_sequence": _next_fleet_snapshot_sequence(),
        "observed_at": time.time(),
        "node": {
            "node_id": node_id,
            "instance_id": _fleet_instance_id,
            "platform": "cuda",
            "version": "1.0.0",
        },
        "health": {
            "state": health_state,
            "accepting": accepting,
            "authoritative": authoritative,
            "diagnostic_code": diagnostic_code,
        },
        "residency": {
            "alias": (
                status_view.resident_profile.alias
                if status_view.resident_profile is not None
                else None
            ),
            "deployment_id": status_view.resident_deployment_id,
            "engine": (
                status_view.resident_profile.backend
                if status_view.resident_profile is not None
                else None
            ),
            "epoch": status_view.epoch,
            "transition_target": status_view.transition_target,
        },
        "admission": {
            "queue_depth": status_view.queued,
            "queue_limit": status_view.queue_limit,
            "queued_by_deployment": status_view.queued_by_deployment,
        },
        "capacity": root_capacity,
        "deployments": deployments,
        "usage_delivery": {
            "enabled": usage_enabled,
            "writer_ready": _pg_writer is not None,
            "outbox_pending": _catalog.count_pg_outbox() if _catalog else 0,
            "last_flush_at": _pg_last_flush_at,
            "last_error_code": (
                "delivery_failed" if _pg_last_error is not None else None
            ),
        },
    }


# ──────────────────────────────────────────────
# OpenAI-compatible proxy → inner vLLM
# ──────────────────────────────────────────────

VLLM_BASE = f"http://{VLLM_INNER_HOST}:{VLLM_INNER_PORT}"

# Language endpoints whose successful responses can carry recognized token
# usage. Images intentionally do not emit token events.
_USAGE_ENDPOINTS = frozenset({
    "v1/chat/completions",
    "v1/completions",
    "v1/embeddings",
    "v1/messages",
    "v1/rerank",
    "v1/responses",
})

# Only these OpenAI request shapes accept `stream_options.include_usage`.
_FORCED_STREAM_USAGE_ENDPOINTS = frozenset({
    "v1/chat/completions",
    "v1/completions",
})

# Legacy in-memory aliases (deprecated; tier 3 in _resolve_request_model).
# Kept for /manager/aliases CRUD compatibility — Phase 3/4 retire it.
MODEL_ALIASES: dict[str, str] = {}


def _peek_model_field(body: bytes) -> Optional[str]:
    """Extract the `model` field from a JSON body, or None on any failure."""
    if not body:
        return None
    try:
        payload = json.loads(body)
    except Exception:
        return None
    val = payload.get("model")
    return val if isinstance(val, str) else None


def _canonicalize_model_field(body: bytes, profile: Optional[ResolvedProfile]) -> bytes:
    """Rewrite request JSON to the engine-served model name after resolution.

    The manager accepts aliases and case-insensitive HF ids, but the inner
    engine validates the literal `model` field against its served name (vLLM:
    the HF id passed to --model; llama-server: the alias passed to --alias).
    Keep the public lookup flexible while sending the canonical name upstream.
    """
    if profile is None or not body:
        return body
    try:
        payload = json.loads(body)
    except Exception:
        return body
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        return body
    if payload["model"] == profile.served_model_name:
        return body
    payload["model"] = profile.served_model_name
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _ensure_stream_usage(
    body: bytes,
    path: str = "v1/chat/completions",
) -> tuple[bytes, bool]:
    """Inject `stream_options.include_usage: true` into streaming requests.

    Chat Completions and legacy Completions emit the trailing `usage` SSE
    event when the client opts in via stream_options. Force that opt-in so the
    proxy can account for tokens, and return whether the client had already
    requested it so the synthetic event can be hidden when necessary.

    Responses and Anthropic Messages have their own streaming usage events
    and do not accept this Chat-specific field. Their request bytes remain
    unchanged.

    Returns (possibly-rewritten body, client_asked_for_usage).
    """
    if not body:
        return body, False
    try:
        payload = json.loads(body)
    except Exception:
        return body, False
    if not isinstance(payload, dict) or not payload.get("stream"):
        return body, False
    opts = payload.get("stream_options")
    client_asked_for_usage = (
        isinstance(opts, dict) and opts.get("include_usage") is True
    )
    if path not in _FORCED_STREAM_USAGE_ENDPOINTS:
        return body, client_asked_for_usage
    if client_asked_for_usage:
        return body, True
    new_opts = dict(opts) if isinstance(opts, dict) else {}
    new_opts["include_usage"] = True
    payload["stream_options"] = new_opts
    return json.dumps(payload, separators=(",", ":")).encode("utf-8"), False


def _process_sse_event(
    event_bytes: bytes,
    client_asked_for_usage: bool,
) -> tuple[bool, Optional[dict]]:
    """Inspect one complete SSE event for an OpenAI `usage` block.

    Returns (forward, usage). `forward=False` means drop this event from the
    downstream stream — used to hide the synthetic usage event from clients
    that did not request it. `usage` is the parsed dict when present.
    """
    text = event_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
    data_lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return True, None
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return True, None
    try:
        payload = json.loads(data)
    except Exception:
        return True, None
    if not isinstance(payload, dict):
        return True, None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return True, None
    if not client_asked_for_usage:
        choices = payload.get("choices")
        if isinstance(choices, list) and len(choices) == 0:
            return False, usage
    return True, usage


def _sse_event_completes_usage(event_bytes: bytes, path: str) -> bool:
    """Return whether an SSE event carries the route's final usage totals."""

    text = event_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
    data_lines = [
        line[5:].lstrip()
        for raw in text.split("\n")
        if (line := raw.rstrip("\r")).startswith("data:")
    ]
    if not data_lines:
        return False
    try:
        payload = json.loads("\n".join(data_lines))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    if normalize_usage(payload, endpoint=f"/{path}") is None:
        return False

    event_type = str(payload.get("type") or "")
    if path == "v1/responses":
        return event_type == "response.completed"
    if path == "v1/messages":
        return event_type == "message_delta"
    # Chat/Completions usage events and the unlikely streaming
    # Embeddings/Rerank usage block are complete when emitted.
    return True


def _make_usage_entry(
    *,
    requested_model: Optional[str],
    alias: Optional[str],
    backend: Optional[str],
    usage: NormalizedUsage,
    endpoint: str,
    streamed: bool,
    response_ms: float,
    status_code: int,
) -> UsageEntry | None:
    """Build one idempotent usage event for the durable SQLite path.

    No-op when `alias` is unknown (raw HF id passthrough with no resident
    profile). `event_id` is generated before persistence so retries use the
    same identity in local analytics, the durable outbox, and Postgres.
    """
    if not alias:
        return None
    try:
        usage_json = json.dumps(dict(usage.raw), separators=(",", ":"))
    except Exception:
        usage_json = None
    return UsageEntry(
        ts=time.time(),
        requested_model=requested_model,
        alias=alias,
        backend=backend or "vllm",
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        usage_json=usage_json,
        event_id=uuid.uuid4().hex,
        endpoint=endpoint,
        streamed=streamed,
        response_ms=response_ms,
        status_code=status_code,
    )


async def _record_usage_entry(entry: UsageEntry) -> None:
    """Persist before response completion, retaining a retry on failure.

    SQLite work runs off the event loop. Even repeated cancellation is delayed
    until the transaction reaches a known outcome. A failed transaction leaves
    the event in the in-memory retry deque, and `_flush_loop` removes it only
    after a later atomic commit.
    """

    persist_task = asyncio.create_task(
        asyncio.to_thread(_persist_usage_entries, [entry]),
        name="cuda-usage-persist",
    )
    cancellation: asyncio.CancelledError | None = None
    persist_error: BaseException | None = None

    # A client disconnect can call cancel more than once. Keep putting a
    # shield between this wrapper and the persistence task until SQLite has
    # definitely committed or failed; awaiting the task unshielded after the
    # first cancellation would let a second cancellation cancel the wrapper
    # before it can retain the event for retry.
    while not persist_task.done():
        try:
            await asyncio.shield(persist_task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
        except Exception:
            # The task is complete when its exception reaches the shield.
            # Inspect it below so success/failure has one authoritative path.
            break

    if persist_task.cancelled():
        persist_error = RuntimeError(
            "usage persistence task was cancelled before a known outcome"
        )
    else:
        try:
            persist_task.result()
        except Exception as exc:
            persist_error = exc

    if persist_error is not None:
        if not any(
            queued.event_id == entry.event_id
            for queued in _runtime.usage_rows
        ):
            _runtime.usage_rows.append(entry)
        logger.warning(
            "Immediate usage persistence failed; retained for retry (%s)",
            type(persist_error).__name__,
        )
    if cancellation is not None:
        raise cancellation


async def _record_usage_row(
    *,
    requested_model: Optional[str],
    alias: Optional[str],
    backend: Optional[str],
    usage: NormalizedUsage,
    endpoint: str,
    streamed: bool,
    response_ms: float,
    status_code: int,
) -> None:
    entry = _make_usage_entry(
        requested_model=requested_model,
        alias=alias,
        backend=backend,
        usage=usage,
        endpoint=endpoint,
        streamed=streamed,
        response_ms=response_ms,
        status_code=status_code,
    )
    if entry is not None:
        await _record_usage_entry(entry)


async def _cleanup_proxy_resources(
    *,
    response: Any | None = None,
    client: Any | None = None,
    lease: ModelLease | None = None,
) -> None:
    """Attempt every proxy cleanup operation without losing cancellation.

    Response/client close implementations are outside the coordinator's
    trust boundary and may raise (including ``CancelledError``). Each cleanup
    therefore runs independently, while the complete drain is shielded from
    cancellation of the request task. If the caller was cancelled, that
    cancellation is re-raised only after the model lease release completed.
    """

    operations: list[tuple[str, Callable[[], Awaitable[None]], bool]] = []
    if response is not None:
        operations.append(("upstream_response_close", response.aclose, False))
    if client is not None:
        operations.append(("upstream_client_close", client.aclose, False))
    if lease is not None:
        operations.append(("model_lease_release", lease.release, True))
    if not operations:
        return

    async def run_operation(
        label: str,
        operation: Callable[[], Awaitable[None]],
        release_operation: bool,
    ) -> None:
        try:
            if release_operation:
                release_task = asyncio.create_task(
                    operation(),
                    name="cuda-proxy-model-lease-release",
                )
                await _await_task_to_known_outcome(release_task)
            else:
                await operation()
        except asyncio.CancelledError:
            logger.error("%s was cancelled during proxy cleanup", label)
        except Exception as exc:
            logger.error(
                "%s failed during proxy cleanup (%s)",
                label,
                type(exc).__name__,
            )

    async def drain() -> None:
        tasks = [
            asyncio.create_task(
                run_operation(label, operation, release_operation),
                name=f"cuda-proxy-cleanup-{label}",
            )
            for label, operation, release_operation in operations
        ]
        await asyncio.gather(*tasks)

    cleanup_task = asyncio.create_task(drain(), name="cuda-proxy-cleanup")
    await _await_task_to_known_outcome(cleanup_task)


async def _await_task_to_known_outcome(task: asyncio.Task):
    """Defer repeated caller cancellation until an owned task is complete."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
        except Exception:
            # The task is complete (or about to publish its exception). Inspect
            # its authoritative result below.
            break

    if task.cancelled():
        return task.result()
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def _open_upstream(
    request: Request, path: str, body: bytes, *, timeout: float | None = None
) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Open a streaming request against the inner engine. Returns the client
    (kept open for the lifetime of the response) and the response object.
    Caller is responsible for closing the client when finished."""
    # Phase 3: strip auth headers so admin Basic creds and the inference
    # bearer token don't leak into vLLM's request logs.
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "authorization", "cookie")
    }
    client = httpx.AsyncClient(timeout=timeout, trust_env=False)
    try:
        req = client.build_request(
            method=request.method,
            url=f"{VLLM_BASE}/{path}",
            headers=headers,
            content=body,
            params=dict(request.query_params),
        )
        response = await client.send(req, stream=True)
        return client, response
    except BaseException:
        await _cleanup_proxy_resources(client=client)
        raise


def _downstream_proxy_headers(incoming) -> dict[str, str]:
    """Strip manager-reserved proof headers from engine responses."""

    return {
        key: value
        for key, value in incoming.items()
        if key.lower() != "x-mnemosyne-error"
    }


class _StreamingProxyOwnership:
    """Exactly-once ownership of a streamed upstream response and lease."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        response: httpx.Response,
        lease: ModelLease,
        requested_model: str | None,
        alias: str | None,
        backend: str | None,
        path: str,
        request_start_monotonic: float | None,
    ) -> None:
        self.client = client
        self.response = response
        self.lease = lease
        self.requested_model = requested_model
        self.alias = alias
        self.backend = backend
        self.path = path
        self.request_start_monotonic = request_start_monotonic
        self.last_usage: NormalizedUsage | None = None
        self.usage_parser = StreamingUsageParser(endpoint=f"/{path}")
        self._usage_recorded = False
        self._completion_task: asyncio.Task[None] | None = None

    @property
    def track_usage(self) -> bool:
        return (
            200 <= self.response.status_code < 300
            and self.path in _USAGE_ENDPOINTS
        )

    async def record_usage(self) -> None:
        if (
            self._usage_recorded
            or not self.track_usage
            or self.last_usage is None
        ):
            return
        self._usage_recorded = True
        try:
            response_ms = (
                (
                    time.monotonic() - self.request_start_monotonic
                )
                * 1000.0
                if self.request_start_monotonic is not None
                else 0.0
            )
            await _record_usage_row(
                requested_model=self.requested_model,
                alias=self.alias,
                backend=self.backend,
                usage=self.last_usage,
                endpoint=(
                    f"/{self.path}"
                    if not self.path.startswith("/")
                    else self.path
                ),
                streamed=True,
                response_ms=response_ms,
                status_code=self.response.status_code,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._usage_recorded = False
            raise

    async def _finish(self) -> None:
        _runtime.last_used_at = time.time()
        _runtime.request_count_delta += 1
        if self.track_usage and self.last_usage is not None:
            try:
                await self.record_usage()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Failed to finalize streaming usage row (%s)",
                    type(exc).__name__,
                )
        await _cleanup_proxy_resources(
            response=self.response,
            client=self.client,
            lease=self.lease,
        )

    async def complete(self) -> None:
        if self._completion_task is None:
            self._completion_task = asyncio.create_task(
                self._finish(),
                name="cuda-streaming-proxy-finish",
            )
        await _await_task_to_known_outcome(self._completion_task)


class _OwnedStreamingResponse(StreamingResponse):
    """Streaming response whose outer ASGI lifetime owns cleanup.

    The response lifetime starts before Starlette asks the body iterator for
    its first item, closing the ownership gap where an unstarted async
    generator's ``finally`` block would never run.
    """

    def __init__(
        self,
        content,
        *,
        owner: _StreamingProxyOwnership,
        status_code: int,
        headers: dict[str, str],
        media_type: str | None,
    ) -> None:
        super().__init__(
            content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )
        self._owner = owner

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._owner.complete()


async def _wrap_stream(
    owner: _StreamingProxyOwnership,
    *,
    client_asked_for_usage: bool = False,
):
    """Stream upstream chunks and own the inflight + usage accounting for
    this request. Reaching here means upstream returned headers, so usage
    IS counted — even on client disconnect mid-stream (model performed work).
    See plans/phase_2.md §5.4.

    When the upstream returned a 2xx on a `_USAGE_ENDPOINTS` path, the wrapper
    additionally parses the SSE stream event-by-event, captures the trailing
    `usage` block, and (when the client did not opt in to that event) strips
    it from the bytes forwarded to the client.
    """
    buffer = bytearray()
    async for chunk in owner.response.aiter_bytes():
        if not owner.track_usage:
            yield chunk
            continue
        owner.usage_parser.feed(chunk)
        buffer.extend(chunk)
        while True:
            idx = buffer.find(b"\n\n")
            idx_crlf = buffer.find(b"\r\n\r\n")
            if idx == -1 and idx_crlf == -1:
                break
            if idx == -1 or (idx_crlf != -1 and idx_crlf < idx):
                boundary, sep_len = idx_crlf, 4
            else:
                boundary, sep_len = idx, 2
            event_bytes = bytes(buffer[:boundary + sep_len])
            del buffer[:boundary + sep_len]
            forward, _usage = _process_sse_event(
                event_bytes,
                (
                    client_asked_for_usage
                    or owner.path not in _FORCED_STREAM_USAGE_ENDPOINTS
                ),
            )
            if _sse_event_completes_usage(event_bytes, owner.path):
                owner.last_usage = owner.usage_parser.usage
                await owner.record_usage()
            if forward:
                yield event_bytes
    if owner.track_usage:
        owner.last_usage = owner.usage_parser.finish()
        # Commit before the response iterator terminates (and, for an
        # unterminated tail, before forwarding that final SSE bytestring).
        await owner.record_usage()
        if buffer:
            # Stream ended without a trailing event terminator — forward the
            # tail verbatim so the client doesn't see truncation.
            yield bytes(buffer)


async def _proxy(request: Request, path: str, body: bytes):
    """
    Forward a request to the inner OpenAI-compatible engine.

    Phase 2 semantics (plans/phase_2.md §5.4):
      - The request body's `model` field is resolved through the managed
        lookup (config → ui_install → MODEL_ALIASES → installed HF id → raw).
        Unknown values raise 404. Org/repo and absolute paths fall through
        to tier 4.
      - One strict-deployment FIFO lease covers admission, model loading, the
        upstream request, and the complete response body/stream.
      - Usage (last_used_at, request_count_delta) bumps only on a SUCCESSFUL
        proxied request — pre-stream upstream errors don't count.
      - Single deadline computed at arrival, gates lock-wait, event-wait,
        and _start_vllm itself.
      - Aside from canonicalizing `model` and forcing streaming usage for
        accounting, the JSON body is forwarded as-is. This preserves
        provider-specific request knobs such as llama.cpp's
        `chat_template_kwargs`, `reasoning_format`, `grammar`, and
        schema-bearing `response_format`.
    """
    request_start_monotonic = time.monotonic()
    requested = _peek_model_field(body)
    if requested is None and _runtime.resident_alias is None:
        raise HTTPException(
            status_code=503,
            detail="No model loaded and no 'model' field in request. POST /manager/load first.",
        )

    profile: Optional[ResolvedProfile] = None
    if requested is not None:
        try:
            profile = _resolve_request_model(requested)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Unknown alias '{requested}'")

    is_image_request = path == "v1/images/generations"
    required_capability = (
        "images.generations"
        if is_image_request
        else path.removeprefix("v1/").replace("/", ".")
    )
    if profile is not None:
        if (
            required_capability in config_mod.SUPPORTED_MODEL_CAPABILITIES
            and required_capability not in profile.capabilities
        ):
            raise HTTPException(
                400,
                f"model '{profile.alias}' does not support /{path}",
            )
        if not is_image_request and profile.kind == "image":
            raise HTTPException(
                400,
                f"image model '{profile.alias}' only supports /v1/images/generations",
            )

    if _config is None:
        swap_budget = 300
    elif is_image_request:
        swap_budget = max(
            _config.server.swap_queue_timeout_seconds,
            _config.server.startup_timeout_seconds,
        )
    else:
        swap_budget = _config.server.swap_queue_timeout_seconds
    deadline = time.monotonic() + swap_budget

    if profile is not None:
        lease = await _acquire_profile_lease(profile, deadline)
    else:
        try:
            lease = await _get_coordinator().acquire_current()
        except (QueueFull, QueueTimeout, NotAccepting) as exc:
            raise _admission_http_exception(exc) from exc
    wire_profile = lease.resident_profile

    if profile is not None:
        usage_alias: Optional[str] = profile.alias
        usage_backend: Optional[str] = profile.backend
    else:
        usage_alias = wire_profile.alias
        usage_backend = wire_profile.backend

    if (
        required_capability in config_mod.SUPPORTED_MODEL_CAPABILITIES
        and required_capability not in wire_profile.capabilities
    ):
        await _cleanup_proxy_resources(lease=lease)
        raise HTTPException(
            400,
            f"model '{wire_profile.alias}' does not support /{path}",
        )

    is_streaming = False
    upstream_ok = False
    client: Optional[httpx.AsyncClient] = None
    response: Optional[httpx.Response] = None
    if is_image_request:
        if (
            wire_profile.kind != "image"
            or "images.generations" not in wire_profile.capabilities
        ):
            await _cleanup_proxy_resources(lease=lease)
            raise HTTPException(400, "the selected model does not support image generation")
        try:
            upstream_body = normalize_image_request(
                body,
                wire_model=wire_profile.served_model_name,
                defaults=wire_profile.image_defaults or {},
                max_pixels=(
                    _config.server.image_max_pixels if _config is not None else 4_194_304
                ),
            )
        except ImageRequestError as exc:
            await _cleanup_proxy_resources(lease=lease)
            raise HTTPException(400, str(exc)) from exc
        client_asked_for_usage = False
    else:
        upstream_body = _canonicalize_model_field(body, wire_profile)
        upstream_body, client_asked_for_usage = _ensure_stream_usage(
            upstream_body,
            path,
        )
    try:
        upstream_timeout = (
            float(_config.server.image_request_timeout_seconds)
            if is_image_request and _config is not None
            else None
        )
        if upstream_timeout is None:
            client, response = await _open_upstream(request, f"{path}", upstream_body)
        else:
            client, response = await _open_upstream(
                request, f"{path}", upstream_body, timeout=upstream_timeout,
            )
        upstream_ok = True
        if "text/event-stream" in response.headers.get("content-type", ""):
            owner = _StreamingProxyOwnership(
                client=client,
                response=response,
                lease=lease,
                requested_model=requested,
                alias=usage_alias,
                backend=usage_backend,
                path=path,
                request_start_monotonic=request_start_monotonic,
            )
            owned_response = _OwnedStreamingResponse(
                _wrap_stream(
                    owner,
                    client_asked_for_usage=client_asked_for_usage,
                ),
                owner=owner,
                status_code=response.status_code,
                headers=_downstream_proxy_headers(response.headers),
                media_type="text/event-stream",
            )
            # Ownership transfers only after the outer response object exists.
            # Its ASGI finally runs even when the body iterator never starts.
            client, response = None, None
            is_streaming = True
            return owned_response
        content = await response.aread()
        try:
            body_json = json.loads(content)
        except Exception:
            body_json = {"raw": content.decode(errors="replace")}
        if (
            200 <= response.status_code < 300
            and path in _USAGE_ENDPOINTS
            and isinstance(body_json, dict)
        ):
            usage = normalize_usage(body_json, endpoint=f"/{path}")
            if usage is not None:
                try:
                    response_ms = (time.monotonic() - request_start_monotonic) * 1000.0
                    await _record_usage_row(
                        requested_model=requested,
                        alias=usage_alias,
                        backend=usage_backend,
                        usage=usage,
                        endpoint=f"/{path}",
                        streamed=False,
                        response_ms=response_ms,
                        status_code=response.status_code,
                    )
                except Exception as e:
                    logger.warning("Failed to queue usage row: %s", e)
        return JSONResponse(content=body_json, status_code=response.status_code)
    except httpx.TimeoutException as exc:
        if is_image_request:
            raise HTTPException(504, "image generation timed out") from exc
        raise
    finally:
        if not is_streaming:
            if upstream_ok:
                _runtime.last_used_at = time.time()
                _runtime.request_count_delta += 1
            await _cleanup_proxy_resources(
                response=response,
                client=client,
                lease=lease,
            )


def _models_list_payload() -> dict:
    """Synthesize an OpenAI-compatible `/v1/models` list from the catalog.

    Unlike a blind proxy to the inner engine (which only knows the one resident
    model, and 503s when nothing is loaded), this lists every fully-downloaded
    model — `status == "installed"` — across both vLLM and llama.cpp backends.
    Each entry is keyed by its catalog `alias`, the stable handle a client sends
    back as the request `model` field to trigger an auto-swap load.

    `served_model_name` follows the profiles.py rule: HF id for vLLM, alias for
    llama.cpp (what the inner engine actually reports once loaded).
    """
    data: list[dict] = []
    seen: set[str] = set()
    if _catalog is not None:
        for row in _catalog.list_models():
            if row.status != "installed":
                continue
            served = row.alias if row.backend == "llama.cpp" else row.hf_model_id
            data.append({
                "id": row.alias,
                "object": "model",
                "created": row.installed_at or 0,
                "owned_by": "mnemosyne",
                "backend": row.backend,
                "model_kind": row.model_kind,
                "capabilities": json.loads(row.capabilities),
                "served_model_name": served,
                "hf_model_id": row.hf_model_id,
                "status": "installed",
            })
            seen.add(row.alias)

    # Keep the resident model visible even if it has no installed catalog row
    # (e.g. loaded via a raw HF-id / absolute-path fallback).
    resident_alias = _runtime.resident_alias
    if resident_alias is not None and resident_alias not in seen:
        profile = _runtime.resident_profile
        data.append({
            "id": resident_alias,
            "object": "model",
            "created": 0,
            "owned_by": "mnemosyne",
            "backend": profile.backend if profile else "vllm",
            "model_kind": profile.kind if profile else "language",
            "capabilities": list(profile.capabilities) if profile else [],
            "served_model_name": (
                profile.served_model_name if profile else resident_alias
            ),
            "hf_model_id": profile.engine_model_path if profile else resident_alias,
            "status": "installed",
        })

    data.sort(key=lambda m: m["id"])
    return {"object": "list", "data": data}


@inference_router.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    tags=["openai"],
    include_in_schema=False,
)
async def openai_proxy(path: str, request: Request):
    """
    Proxy all OpenAI-compatible requests to the inner vLLM server.

    If the request includes a `model` field that differs from the currently
    loaded model, the manager will automatically swap to it before serving
    the request. No need to call /manager/load explicitly.

    `GET /v1/models` is the exception: it is served locally from the catalog so
    clients can discover every installed (loadable) model, not just the resident
    one — and it never 503s when nothing is loaded.
    """
    if path == "models" and request.method == "GET":
        return JSONResponse(_models_list_payload())
    body = await request.body()
    return await _proxy(request, f"v1/{path}", body)


# ──────────────────────────────────────────────
# Alias management (optional quality-of-life)
# ──────────────────────────────────────────────

@admin_router.get("/manager/aliases", tags=["manager"])
async def get_aliases():
    """List current model string aliases."""
    return {"aliases": MODEL_ALIASES}


@admin_router.post("/manager/aliases", tags=["manager"])
async def set_alias(request: Request):
    """
    Add or update a model alias.

    ```json
    { "alias": "qwen72b", "model": "Qwen/Qwen2.5-72B-Instruct-AWQ" }
    ```
    Then you can use `"model": "qwen72b"` in any /v1 request.
    """
    body = await request.json()
    alias = body.get("alias")
    model = body.get("model")
    if not alias or not model:
        raise HTTPException(status_code=400, detail="Both 'alias' and 'model' are required")
    MODEL_ALIASES[alias] = model
    logger.info(f"Alias set: '{alias}' → '{model}'")
    return {"alias": alias, "model": model}


@admin_router.delete("/manager/aliases/{alias}", tags=["manager"])
async def delete_alias(alias: str):
    """Remove a model alias."""
    if alias not in MODEL_ALIASES:
        raise HTTPException(status_code=404, detail=f"Alias '{alias}' not found")
    MODEL_ALIASES.pop(alias)
    return {"deleted": alias}


# ──────────────────────────────────────────────
# Admin UI static serving (Phase 6)
# ──────────────────────────────────────────────

def _ui_static_root() -> Path:
    return Path(os.environ.get("MNEMOSYNE_UI_DIR", "/app/static")).resolve()


def _ui_index_or_404(root: Path) -> FileResponse:
    index = root / "index.html"
    if not root.is_dir() or not index.is_file():
        raise HTTPException(404, "admin UI build not found")
    return FileResponse(index)


@ui_router.get("/", include_in_schema=False)
async def _admin_root():
    return RedirectResponse("/ui/", status_code=307)


@ui_router.get("/ui", include_in_schema=False)
@ui_router.get("/ui/", include_in_schema=False)
async def _ui_index():
    return _ui_index_or_404(_ui_static_root())


@ui_router.get("/ui/{full_path:path}", include_in_schema=False)
async def _ui_spa(full_path: str):
    root = _ui_static_root()
    candidate = (root / full_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(404, "invalid UI asset path")
    if candidate.is_file():
        return FileResponse(candidate)
    return _ui_index_or_404(root)


# ──────────────────────────────────────────────
# App construction (Phase 3 §5.10)
# ──────────────────────────────────────────────
# MUST run after every @<router>.<verb>(...) decorator above, because
# FastAPI.include_router() copies routes by value at call time. Routes
# registered on a router *after* include_router is called are silently
# ignored.

inference_app = FastAPI(
    title="Mnemosyne Inference",
    version="1.0.0",
    docs_url=None, redoc_url=None, openapi_url=None,
)
inference_app.include_router(health_router)
inference_app.include_router(fleet_router)
inference_app.include_router(
    inference_router,
    dependencies=[Depends(require_inference_bearer)],
)

admin_app = FastAPI(
    title="Mnemosyne Admin",
    version="1.0.0",
    description="Admin plane: /manager/*, /docs, /v1/* superset.",
    # Disable FastAPI's defaults; we re-serve them via docs_router behind
    # require_admin_basic so the schema is not LAN-readable when ADMIN_PASSWORD
    # is set.
    docs_url=None, redoc_url=None, openapi_url=None,
)
admin_app.include_router(health_router)
admin_app.include_router(
    admin_router,
    dependencies=[Depends(require_admin_basic)],
)
admin_app.include_router(
    inference_router,
    dependencies=[Depends(require_admin_basic)],
)
admin_app.include_router(
    docs_router,
    dependencies=[Depends(require_admin_basic)],
)
admin_app.include_router(
    ui_router,
    dependencies=[Depends(require_admin_basic)],
)

# Back-compat alias for tests and any importer using `from vllm_manager
# import app`. Admin is the superset, so this is the safe default.
app = admin_app


# ──────────────────────────────────────────────
# Entry point (Phase 3 §5.10)
# ──────────────────────────────────────────────

class _ManagedServer(uvicorn.Server):
    """uvicorn.Server with signal-handler installation suppressed.

    We install one handler at the gather level so a single SIGTERM sets
    `should_exit` on both server instances atomically.

    Modern uvicorn (≥0.30) wraps `serve()` in `with self.capture_signals():`.
    Older versions called `self.install_signal_handlers()` directly. Override
    both so this works regardless of the installed uvicorn version.
    """
    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self) -> None:  # legacy uvicorn
        return


def _resolve_admin_bind(cfg_bind: str) -> str:
    """If ADMIN_PASSWORD is unset, force admin to loopback (PRD §5.10
    fail-safe). Note: in Docker, container loopback is not reachable through
    `-p 8001:8001` — the bridge forwards to container 0.0.0.0 only. So this
    mode means admin is only reachable via `docker exec`.
    """
    if not os.environ.get("ADMIN_PASSWORD") and cfg_bind != "127.0.0.1":
        logger.warning(
            "ADMIN_PASSWORD unset; forcing admin bind from %s to 127.0.0.1 "
            "(fail-safe). Admin port will be unreachable from outside the "
            "container — set ADMIN_PASSWORD in /config/.env for LAN admin.",
            cfg_bind,
        )
        return "127.0.0.1"
    return cfg_bind


def _check_inner_port_clash(cfg: Config) -> None:
    """Reject configs where VLLM_INNER_PORT collides with either external
    port. Inner vLLM and the admin app share the container's network
    namespace, so 0.0.0.0:8001 (admin) and 127.0.0.1:8001 (inner) cannot
    coexist."""
    inner = int(os.environ.get("VLLM_INNER_PORT", "8002"))
    if inner in (cfg.server.inference_port, cfg.server.admin_port):
        raise SystemExit(
            f"VLLM_INNER_PORT={inner} collides with "
            f"server.inference_port={cfg.server.inference_port} or "
            f"server.admin_port={cfg.server.admin_port}. "
            f"Pick an unused port (default 8002)."
        )


async def _serve_both(cfg: Config) -> None:
    inf_cfg = uvicorn.Config(
        inference_app,
        host=cfg.server.inference_bind,
        port=cfg.server.inference_port,
        log_level="info",
        lifespan="off",
    )
    adm_cfg = uvicorn.Config(
        admin_app,
        host=_resolve_admin_bind(cfg.server.admin_bind),
        port=cfg.server.admin_port,
        log_level="info",
        lifespan="off",
    )
    inf_server = _ManagedServer(inf_cfg)
    adm_server = _ManagedServer(adm_cfg)

    loop = asyncio.get_running_loop()
    def _shutdown():
        inf_server.should_exit = True
        adm_server.should_exit = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown)

    async with manager_lifespan(cfg):
        # If one server fails (bind error, etc.) we want the other to wind
        # down too, not be torn from under itself by gather() raising. Wait
        # for FIRST_EXCEPTION, signal both, then drain.
        inf_task = asyncio.create_task(inf_server.serve(), name="inference-uvicorn")
        adm_task = asyncio.create_task(adm_server.serve(), name="admin-uvicorn")
        await asyncio.wait(
            {inf_task, adm_task}, return_when=asyncio.FIRST_EXCEPTION
        )
        _shutdown()
        results = await asyncio.gather(inf_task, adm_task, return_exceptions=True)
        for name, result in zip(("inference", "admin"), results):
            if isinstance(result, BaseException):
                logger.error("%s uvicorn exited with error: %r", name, result)
        for result in results:
            if isinstance(result, BaseException):
                raise result


if __name__ == "__main__":
    load_env()
    cfg_at_boot = load_config()
    _check_inner_port_clash(cfg_at_boot)
    asyncio.run(_serve_both(cfg_at_boot))
