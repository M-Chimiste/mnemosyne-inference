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
from pathlib import Path
from typing import Awaitable, Callable, Optional, TypeVar

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
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
_legacy_alias_warned: set[str]              = set()

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
        revision="main",
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
        return resolve_profile(config_alias, _config, _catalog)
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
        # SIGTERM all in-flight installs so we exit cleanly. Worker's
        # prctl(PDEATHSIG) is belt-and-suspenders on Linux; this is the
        # primary cleanup path.
        for alias in list(downloader._active.keys()):
            try:
                downloader.cancel_install(alias)
            except Exception as e:
                logger.warning("cancel_install(%s) during shutdown: %s", alias, e)
        hf_search.shutdown_search_pool()
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
        # Phase 5 additions — surface fallback when vLLM registry import broke.
        "vllm_arch_count":   hf_search.get_arch_count(),
        "vllm_arch_source":  hf_search.get_arch_source(),
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


@admin_router.get("/manager/hf/search", tags=["manager"])
async def hf_search_route(
    q: str = Query("", description="Search query; blank returns top models by downloads"),
    limit: int = Query(20, ge=1, le=50),
    page: int = Query(1, ge=1, le=20),
    filter_compat: bool = Query(False),
    include_vision: bool = Query(False),
):
    """Search HuggingFace Hub for vLLM-compatible models. PRD §5.9.

    Returns both compatible and incompatible results (incompatible flagged
    with `compat_reason`). Pass `filter_compat=true` to drop incompatible
    rows server-side. Pass `include_vision=true` to also surface
    `image-text-to-text` models (Qwen-VL, Llava, etc.) — defaults to
    `false` to match PRD §5.9 literally.
    """
    try:
        return await hf_search.run_search(
            q=q,
            limit=limit,
            page=page,
            include_vision=include_vision,
            filter_compat=filter_compat,
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


async def _install_internal(
    request: InstallRequest,
    *,
    hf_token_override: Optional[str] = None,
    allow_cache_only_alias: bool = False,
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
    )

    hf_token = hf_token_override
    if hf_token is None:
        hf_token = os.environ.get("HUGGING_FACE_HUB_TOKEN") or None

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
        ),
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


def _canonicalize_model_field(body: bytes, profile: Optional[ResolvedProfile]) -> bytes:
    """Rewrite request JSON to the vLLM served model name after resolution.

    The manager accepts aliases and case-insensitive HF ids, but vLLM's OpenAI
    server validates the literal `model` field against its served name. Keep the
    public lookup flexible while sending the canonical HF id upstream.
    """
    if profile is None or not body:
        return body
    try:
        payload = json.loads(body)
    except Exception:
        return body
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        return body
    if payload["model"] == profile.model:
        return body
    payload["model"] = profile.model
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


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
      - The request body's `model` field is resolved through the managed
        lookup (config → ui_install → MODEL_ALIASES → installed HF id → raw).
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
    upstream_body = _canonicalize_model_field(body, profile)
    try:
        client, response = await _open_upstream(request, f"{path}", upstream_body)
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
