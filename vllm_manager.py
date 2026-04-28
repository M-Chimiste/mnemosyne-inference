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
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, Optional, TypeVar

from huggingface_hub import snapshot_download, HfApi
from huggingface_hub.utils import RepositoryNotFoundError, GatedRepoError

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

import config as config_mod
from catalog import Catalog, ReconcileResult, SyncResult, is_cache_only_alias, open_catalog
from config import Config, ConfigError, load_config, load_env
from profiles import ResolvedProfile, resolve_profile
from runtime import RuntimeState, build_vllm_argv, build_vllm_env, derive_tp_size

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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
_legacy_alias_warned: set[str]              = set()

# Download state — keyed by model_id
# Each entry: {status, started_at, finished_at, error, path}
_downloads: dict[str, dict] = {}

# Phase 1 globals — populated by lifespan, reset by tests/conftest.py::client.
_config: Optional[Config] = None
_catalog: Optional[Catalog] = None

# ──────────────────────────────────────────────
# vLLM process management
# ──────────────────────────────────────────────

async def _wait_for_vllm(timeout: int = STARTUP_TIMEOUT) -> bool:
    url = f"http://{VLLM_INNER_HOST}:{VLLM_INNER_PORT}/health"
    async with httpx.AsyncClient() as client:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if vllm_process and vllm_process.poll() is not None:
                logger.error("vLLM process exited unexpectedly during startup")
                return False
            try:
                r = await client.get(url, timeout=3)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(2)
    return False


def _kill_vllm():
    """Stop the resident vLLM subprocess (idempotent) and reset runtime state.
    Flushes buffered usage to the catalog before clearing the resident alias
    so the just-evicted model's last activity makes it to disk."""
    global vllm_process
    _flush_usage_best_effort("vLLM teardown")
    if vllm_process and vllm_process.poll() is None:
        logger.info(f"Stopping vLLM (pid={vllm_process.pid}) ...")
        vllm_process.terminate()
        try:
            vllm_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logger.warning("Graceful stop timed out — sending SIGKILL")
            vllm_process.kill()
            vllm_process.wait()
        logger.info("vLLM stopped.")
    vllm_process = None
    _runtime.resident_alias = None
    _runtime.resident_profile = None
    _runtime.resident_tp_size = None
    _runtime.model_load_time = None
    _runtime.last_used_at = None
    # If the best-effort flush failed, drop the buffered count rather than
    # risking attribution to the next resident profile.
    _runtime.request_count_delta = 0


async def _start_vllm(profile: ResolvedProfile) -> None:
    """Launch vLLM for `profile`. Cleans up half-launched subprocesses on any
    failure, including asyncio.CancelledError from a deadline-induced wait_for
    (called inside ensure_loaded). See plans/phase_2.md §5.2."""
    global vllm_process

    _kill_vllm()
    visible = config_mod.gpu_indices_or_none()
    tp_size = derive_tp_size(profile, visible_gpus=visible, default_tp=DEFAULT_TP)
    if profile.gpus == "all" and not visible:
        logger.warning(
            "gpus='all' but nvidia-smi probe returned no GPUs; falling back to "
            "DEFAULT_TP=%d. Production CUDA hosts should never hit this path.",
            DEFAULT_TP,
        )

    argv = build_vllm_argv(profile, host=VLLM_INNER_HOST, port=VLLM_INNER_PORT, tp_size=tp_size)
    env = build_vllm_env(profile, base_env=os.environ)
    logger.info("Launching vLLM (alias=%s tp=%d): %s", profile.alias, tp_size, " ".join(argv))
    vllm_process = subprocess.Popen(argv, env=env, stdout=sys.stdout, stderr=sys.stderr)

    try:
        if not await _wait_for_vllm():
            raise RuntimeError(f"vLLM failed to become ready for alias '{profile.alias}'")
    except (Exception, asyncio.CancelledError):
        # Includes wait_for-induced CancelledError when ensure_loaded times out.
        # Always clean up the half-launched subprocess.
        _kill_vllm()
        raise

    _runtime.resident_alias = profile.alias
    _runtime.resident_profile = profile
    _runtime.resident_tp_size = tp_size
    now = time.time()
    _runtime.model_load_time = now
    _runtime.last_used_at = now
    logger.info(
        "✓ Loaded alias='%s' model='%s' tp=%d gpu_mem=%.2f",
        profile.alias, profile.model, tp_size, profile.gpu_memory_utilization,
    )


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
    """Block until vLLM is serving `profile.alias`. Raises HTTPException(504)
    on deadline expiry, HTTPException(503) on vLLM load failure.

    Concurrency model (LMStudio-style): single _swap_lock for cross-target
    transitions; per-target asyncio.Event for piggyback so multiple requests
    for the same loading target wait on one load. See plans/phase_2.md §5.3.
    """
    global _loading_target, _load_event, _load_error
    target = profile.alias
    while True:
        if _runtime.resident_alias == target and _loading_target is None:
            return  # warm path
        if _loading_target == target:  # piggyback an in-flight load
            try:
                await _run_until(_load_event.wait, deadline)
            except asyncio.TimeoutError:
                raise HTTPException(504, f"swap queue timeout waiting for '{target}'")
            if _load_error is not None:
                raise HTTPException(503, f"vLLM load failed: {_load_error}")
            continue  # re-check resident
        try:
            await _run_until(_swap_lock.acquire, deadline)
        except asyncio.TimeoutError:
            raise HTTPException(504, f"swap queue timeout acquiring lock for '{target}'")
        try:
            if _runtime.resident_alias == target:  # raced
                return
            _loading_target = target
            _load_event = asyncio.Event()
            _load_error = None
            try:
                await _run_until(lambda: _start_vllm(profile), deadline)
            except asyncio.TimeoutError:
                # _start_vllm cleans up the half-launched subprocess in its
                # own except block. Surface as 504.
                raise HTTPException(504, f"vLLM load did not complete in time for '{target}'")
            except asyncio.CancelledError:
                # Caller cancelled. Don't stash _load_error — piggybackers
                # will re-check resident, see no model loaded, and retry
                # against their own deadline.
                raise
            except HTTPException:
                raise
            except Exception as e:
                _load_error = e
                raise HTTPException(503, f"vLLM load failed: {e}")
            finally:
                _loading_target = None
                _load_event.set()
        finally:
            _swap_lock.release()
        # loop falls through to warm-path check


# ──────────────────────────────────────────────
# Usage buffer flush (Phase 2 §5.7)
# ──────────────────────────────────────────────

def _flush_usage() -> None:
    """Sync. Single UPDATE per call. Safe from any context — no-op if there's
    nothing to flush. Called from _flush_loop, _kill_vllm, and lifespan exit."""
    if _catalog is None:
        return
    alias = _runtime.resident_alias
    delta = _runtime.request_count_delta
    if alias is None or delta == 0:
        return
    _catalog.bump_usage(alias, _runtime.last_used_at, delta)
    _runtime.request_count_delta = 0


def _flush_usage_best_effort(context: str) -> None:
    """Flush usage without letting catalog/SQLite failures block teardown."""
    try:
        _flush_usage()
    except Exception as e:
        logger.warning("Usage flush failed during %s: %s", context, e)


async def _flush_loop() -> None:
    """Background task: flush every 30s while the manager is up."""
    while True:
        await asyncio.sleep(30)
        try:
            _flush_usage()
        except Exception as e:
            logger.warning("Usage flush failed: %s", e)


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
        async with _swap_lock:
            if _runtime.resident_alias is None:
                continue
            if _runtime.inflight > 0:
                continue
            if _runtime.last_used_at is None:
                continue
            idle = time.time() - _runtime.last_used_at
            if idle > threshold:
                logger.info(
                    "Idle eviction: '%s' idle %ds (threshold %ds)",
                    _runtime.resident_alias, int(idle), threshold,
                )
                _kill_vllm()


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
    """
    if _config is None:
        raise RuntimeError("config not loaded")
    storage_name = _config.storage.default
    storage_path = next(
        l.path for l in _config.storage.locations if l.name == storage_name
    )
    return ResolvedProfile(
        alias=model_id,  # synthetic — only used for log lines and status
        model=model_id,
        gpus="all",
        quantization=None,
        max_model_len=_config.defaults.max_model_len,
        gpu_memory_utilization=_config.defaults.gpu_memory_utilization,
        trust_remote_code=_config.defaults.trust_remote_code,
        storage_name=storage_name,
        storage_path=storage_path,
        extra_args=(),
    )


def _resolve_request_model(requested: str) -> ResolvedProfile:
    """Four-tier lookup: config alias → catalog ui_install row → legacy
    MODEL_ALIASES dict → raw HF id passthrough (gated by _RAW_HF_ID_RE or
    absolute existing path). Anything else raises KeyError, which the caller
    translates to 404. See plans/phase_2.md §5.5."""
    if _config is None or _catalog is None:
        raise RuntimeError("manager not initialized")

    # Tier 1 — config alias
    if any(m.alias == requested for m in _config.models):
        return resolve_profile(requested, _config, _catalog)
    # Tier 2 — catalog ui_install row
    row = _catalog.get_model(requested)
    if row is not None and row.source == "ui_install":
        return resolve_profile(requested, _config, _catalog)
    # Tier 3 — legacy MODEL_ALIASES dict
    if requested in MODEL_ALIASES:
        _warn_legacy_alias_once(requested)
        return _synthesize_profile(MODEL_ALIASES[requested])
    # Tier 4 — raw HF id or absolute path
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
    if cfg is None:
        load_env()
        cfg = load_config()
    _config = cfg
    _catalog = open_catalog()
    _runtime = RuntimeState()
    _legacy_alias_warned.clear()
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
    try:
        yield
    finally:
        # Cancel infinite tasks first so they don't fight teardown.
        for t in (_eviction_task, _flush_task):
            if t is not None and not t.done():
                t.cancel()
        for t in (_eviction_task, _flush_task):
            if t is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        _eviction_task = None
        _flush_task = None
        # Final flush before _kill_vllm wipes _runtime — belt-and-suspenders
        # in case _kill_vllm's internal flush ever moves.
        _flush_usage_best_effort("lifespan shutdown")
        logger.info("Shutting down — stopping vLLM ...")
        _kill_vllm()
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
admin_router = APIRouter()
docs_router = APIRouter()


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
    Phase 2 adds resident-profile detail and idle-eviction countdown."""
    profile = _runtime.resident_profile
    loaded_model = profile.model if profile else None
    load_time = _runtime.model_load_time

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
        "vllm_pid":       vllm_process.pid if vllm_process else None,
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

    is_aliased = (
        any(m.alias == requested for m in (_config.models if _config else []))
        or (
            _catalog is not None
            and (row := _catalog.get_model(requested)) is not None
            and row.source == "ui_install"
        )
        or requested in MODEL_ALIASES
    )
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

    deadline = time.monotonic() + (
        _config.server.swap_queue_timeout_seconds if _config else 300
    )
    await ensure_loaded(profile, deadline)
    return {"status": "loaded", "alias": profile.alias, "model": profile.model}


@admin_router.post("/manager/unload", tags=["manager"])
async def unload_model():
    """Unload the current model and free all GPU memory."""
    deadline = time.monotonic() + (
        _config.server.swap_queue_timeout_seconds if _config else 300
    )
    try:
        await _run_until(_swap_lock.acquire, deadline)
    except asyncio.TimeoutError:
        raise HTTPException(504, "timeout waiting for active model load to finish")
    try:
        if _runtime.resident_alias is None:
            return {"status": "nothing to unload"}
        was = _runtime.resident_alias
        _kill_vllm()
        return {"status": "unloaded", "was": was}
    finally:
        _swap_lock.release()


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
# HuggingFace Hub download endpoints
# ──────────────────────────────────────────────

def _run_download(model_id: str, revision: str, ignore_patterns: list[str], hf_token: Optional[str]):
    """
    Blocking download — runs in a background thread so it doesn't
    tie up the async event loop. Updates _downloads[model_id] in place.
    """
    _downloads[model_id]["status"]     = "downloading"
    _downloads[model_id]["started_at"] = time.time()

    try:
        path = snapshot_download(
            repo_id=model_id,
            revision=revision or None,
            ignore_patterns=ignore_patterns or None,
            cache_dir=os.path.join(HF_HOME, "hub"),
            token=hf_token or os.getenv("HUGGING_FACE_HUB_TOKEN") or None,
            local_files_only=False,
        )
        _downloads[model_id]["status"]      = "complete"
        _downloads[model_id]["path"]        = path
        _downloads[model_id]["finished_at"] = time.time()
        logger.info(f"✓ Download complete: {model_id} → {path}")

    except RepositoryNotFoundError:
        _downloads[model_id]["status"] = "error"
        _downloads[model_id]["error"]  = f"Repository '{model_id}' not found on HF Hub"
        logger.error(_downloads[model_id]["error"])

    except GatedRepoError:
        _downloads[model_id]["status"] = "error"
        _downloads[model_id]["error"]  = (
            f"'{model_id}' is a gated model. "
            "Set HUGGING_FACE_HUB_TOKEN in docker-compose.yml and accept the model license on huggingface.co"
        )
        logger.error(_downloads[model_id]["error"])

    except Exception as e:
        _downloads[model_id]["status"] = "error"
        _downloads[model_id]["error"]  = str(e)
        logger.error(f"Download failed for {model_id}: {e}")


@admin_router.post("/manager/download", tags=["downloads"])
async def download_model(request: Request):
    """
    Download a model from HuggingFace Hub into the cache volume.
    Runs in the background — returns immediately and tracks progress.

    ```json
    {
      "model": "Qwen/Qwen2.5-72B-Instruct-AWQ",
      "revision": "main",
      "ignore_patterns": ["*.pt", "*.bin"],
      "hf_token": "hf_..."
    }
    ```

    Only `model` is required. Use `ignore_patterns` to skip non-safetensor
    formats and cut download size (e.g. `["*.pt", "*.bin"]`).
    `hf_token` overrides the container environment variable for this request.

    Poll `/manager/download/{model}` to track progress.
    """
    body = await request.json()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="'model' field required")

    existing = _downloads.get(model_id, {})
    if existing.get("status") == "downloading":
        return {
            "status":  "already_downloading",
            "model":   model_id,
            "poll":    f"/manager/download/{model_id.replace('/', '%2F')}",
        }

    revision        = body.get("revision", "main")
    ignore_patterns = body.get("ignore_patterns", ["*.pt", "*.bin", "*.msgpack", "flax_model*", "tf_model*", "rust_model*"])
    hf_token        = body.get("hf_token")

    # Seed the status entry before the thread starts
    _downloads[model_id] = {
        "model":       model_id,
        "status":      "queued",
        "revision":    revision,
        "started_at":  None,
        "finished_at": None,
        "path":        None,
        "error":       None,
    }

    thread = threading.Thread(
        target=_run_download,
        args=(model_id, revision, ignore_patterns, hf_token),
        daemon=True,
        name=f"download-{model_id}",
    )
    thread.start()

    logger.info(f"Download started (background): {model_id}")
    return {
        "status":  "started",
        "model":   model_id,
        "poll":    f"/manager/download/{model_id.replace('/', '%2F')}",
    }


@admin_router.get("/manager/download/{model_id:path}", tags=["downloads"])
async def download_status(model_id: str):
    """
    Check the status of a download.

    Status values: queued | downloading | complete | error
    """
    entry = _downloads.get(model_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"No download record for '{model_id}'")

    result = dict(entry)

    # Add human-readable elapsed / duration
    if entry.get("started_at"):
        end = entry.get("finished_at") or time.time()
        result["elapsed_seconds"] = round(end - entry["started_at"], 1)

    return result


@admin_router.get("/manager/downloads", tags=["downloads"])
async def list_downloads():
    """List all download records (active, complete, and failed)."""
    return {"downloads": list(_downloads.values())}


@admin_router.delete("/manager/download/{model_id:path}", tags=["downloads"])
async def clear_download_record(model_id: str):
    """
    Remove a download status record (does not delete the cached files).
    Useful for clearing errors before retrying.
    """
    if model_id not in _downloads:
        raise HTTPException(status_code=404, detail=f"No record for '{model_id}'")
    if _downloads[model_id].get("status") == "downloading":
        raise HTTPException(status_code=409, detail="Download is in progress — cannot clear an active download")
    _downloads.pop(model_id)
    return {"cleared": model_id}


@health_router.get("/health", tags=["manager"])
async def health():
    """Health check — always returns 200 from the manager itself."""
    return {
        "status": "ok",
        "model_loaded": _runtime.resident_alias is not None,
        "loading": _loading_target is not None,
    }


# ──────────────────────────────────────────────
# OpenAI-compatible proxy → inner vLLM
# ──────────────────────────────────────────────

VLLM_BASE = f"http://{VLLM_INNER_HOST}:{VLLM_INNER_PORT}"

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


async def _open_upstream(
    request: Request, path: str, body: bytes
) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Open a streaming request against the inner vLLM. Returns the client
    (kept open for the lifetime of the response) and the response object.
    Caller is responsible for closing the client when finished."""
    # Phase 3: strip auth headers so admin Basic creds and the inference
    # bearer token don't leak into vLLM's request logs.
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "authorization", "cookie")
    }
    client = httpx.AsyncClient(timeout=None)
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
        await client.aclose()
        raise


async def _wrap_stream(client: httpx.AsyncClient, response: httpx.Response):
    """Stream upstream chunks and own the inflight + usage accounting for
    this request. Reaching here means upstream returned headers, so usage
    IS counted — even on client disconnect mid-stream (model performed work).
    See plans/phase_2.md §5.4."""
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        _runtime.inflight -= 1
        _runtime.last_used_at = time.time()
        _runtime.request_count_delta += 1
        with contextlib.suppress(Exception):
            await response.aclose()
        with contextlib.suppress(Exception):
            await client.aclose()


async def _proxy(request: Request, path: str, body: bytes):
    """
    Forward a request to the inner vLLM server.

    Phase 2 semantics (plans/phase_2.md §5.4):
      - The request body's `model` field is resolved through the four-tier
        lookup (config → ui_install → MODEL_ALIASES → raw passthrough).
        Unknown values raise 404. Org/repo and absolute paths fall through
        to tier 4.
      - Swap queueing via ensure_loaded — multiple requests for the same
        loading target piggyback on one load.
      - Inflight counter incremented under _swap_lock with a resident-alias
        re-check, closing the eviction TOCTOU window.
      - Usage (last_used_at, request_count_delta) bumps only on a SUCCESSFUL
        proxied request — pre-stream upstream errors don't count.
      - Single deadline computed at arrival, gates lock-wait, event-wait,
        and _start_vllm itself.
    """
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

    deadline = time.monotonic() + (
        _config.server.swap_queue_timeout_seconds if _config else 300
    )

    # Loop with one deadline, one lock-release path. continue triggers
    # a retry of ensure_loaded if eviction or another swap raced us.
    while True:
        if profile is not None:
            await ensure_loaded(profile, deadline)

        try:
            await _run_until(_swap_lock.acquire, deadline)
        except asyncio.TimeoutError:
            raise HTTPException(504, "swap queue timeout acquiring inflight lock")
        try:
            if profile is not None and _runtime.resident_alias != profile.alias:
                continue  # finally releases; loop reruns
            if profile is None and _runtime.resident_alias is None:
                raise HTTPException(503, "Model evicted before request started")
            _runtime.inflight += 1
            break
        finally:
            _swap_lock.release()

    is_streaming = False
    upstream_ok = False
    client: Optional[httpx.AsyncClient] = None
    response: Optional[httpx.Response] = None
    try:
        client, response = await _open_upstream(request, f"{path}", body)
        upstream_ok = True
        if "text/event-stream" in response.headers.get("content-type", ""):
            is_streaming = True
            # Ownership transfers to _wrap_stream — its finally handles
            # inflight + usage accounting.
            wrapped_client, wrapped_response = client, response
            client, response = None, None  # don't close in this finally
            return StreamingResponse(
                _wrap_stream(wrapped_client, wrapped_response),
                status_code=wrapped_response.status_code,
                headers=dict(wrapped_response.headers),
                media_type="text/event-stream",
            )
        content = await response.aread()
        try:
            body_json = json.loads(content)
        except Exception:
            body_json = {"raw": content.decode(errors="replace")}
        return JSONResponse(content=body_json, status_code=response.status_code)
    finally:
        if not is_streaming:
            _runtime.inflight -= 1
            if upstream_ok:
                _runtime.last_used_at = time.time()
                _runtime.request_count_delta += 1
            if response is not None:
                with contextlib.suppress(Exception):
                    await response.aclose()
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aclose()


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
    """
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
