#!/usr/bin/env python3
"""
vLLM Manager — Dynamic model loader/unloader with OpenAI-compatible proxy.

Runs inside a Docker container. Manages a vLLM subprocess, loading models
on demand and swapping when requested. Exposes:
  - OpenAI-compatible API at /v1/*  (proxied to inner vLLM)
  - Manager API at /manager/*       (load, unload, status, list cache)
"""

import asyncio
import httpx
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
from typing import Optional, AsyncIterator

from huggingface_hub import snapshot_download, HfApi
from huggingface_hub.utils import RepositoryNotFoundError, GatedRepoError

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

from catalog import Catalog, ReconcileResult, SyncResult, is_cache_only_alias, open_catalog
from config import Config, ConfigError, load_config, load_env

# ──────────────────────────────────────────────
# Configuration (set via docker-compose environment)
# ──────────────────────────────────────────────
VLLM_INNER_HOST = os.getenv("VLLM_INNER_HOST", "127.0.0.1")
VLLM_INNER_PORT = int(os.getenv("VLLM_INNER_PORT", "8001"))
MANAGER_HOST    = os.getenv("MANAGER_HOST", "0.0.0.0")
MANAGER_PORT    = int(os.getenv("MANAGER_PORT", "8000"))
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

# ──────────────────────────────────────────────
# Global state
# ──────────────────────────────────────────────
current_model:   Optional[str]              = None
vllm_process:    Optional[subprocess.Popen] = None
model_load_time: Optional[float]            = None
loading_lock                                = asyncio.Lock()
_loading:        bool                       = False
_current_tp:     int                        = DEFAULT_TP
_current_gpu_mem: float                     = DEFAULT_GPU_MEM

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
    global vllm_process, current_model, model_load_time
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
    vllm_process    = None
    current_model   = None
    model_load_time = None


async def _start_vllm(model_id: str, tp: int, gpu_mem: float, extra_args: list[str]) -> None:
    global vllm_process, current_model, model_load_time, _loading, _current_tp, _current_gpu_mem

    _kill_vllm()
    _loading = True

    cmd = [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model",                  model_id,
        "--host",                   VLLM_INNER_HOST,
        "--port",                   str(VLLM_INNER_PORT),
        "--tensor-parallel-size",   str(tp),
        "--gpu-memory-utilization", str(gpu_mem),
        "--trust-remote-code",
        "--disable-log-requests",
    ] + extra_args

    logger.info(f"Launching vLLM: {' '.join(cmd)}")
    vllm_process = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)

    ready = await _wait_for_vllm()
    _loading = False

    if not ready:
        _kill_vllm()
        raise RuntimeError(f"vLLM failed to become ready for model '{model_id}'")

    current_model    = model_id
    model_load_time  = time.time()
    _current_tp      = tp
    _current_gpu_mem = gpu_mem
    logger.info(f"✓ Model '{model_id}' ready (tp={tp}, gpu_mem={gpu_mem})")


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
async def lifespan(app: FastAPI):
    global _config, _catalog
    load_env()
    _config = load_config()
    _catalog = open_catalog()
    sync, rec = _catalog.apply_config(
        _config.models,
        _config.storage.default,
        {l.name: l.path for l in _config.storage.locations},
    )
    logger.info(
        "Catalog ready: sync=%s reconcile=%s",
        asdict(sync), asdict(rec),
    )
    logger.info(
        "Phase 1: server.{inference,admin}_{port,bind} parsed but not bound. "
        "Listening via legacy MANAGER_HOST/MANAGER_PORT. "
        "config.server: inference_port=%d admin_port=%d",
        _config.server.inference_port, _config.server.admin_port,
    )
    _install_sighup_handler()

    logger.info(
        f"\n"
        f"  ┌─────────────────────────────────────────────┐\n"
        f"  │         vLLM Manager Ready                  │\n"
        f"  │                                             │\n"
        f"  │  OpenAI API : :<port>/v1                    │\n"
        f"  │  Manager    : :<port>/manager               │\n"
        f"  │  Docs       : :<port>/docs                  │\n"
        f"  │                                             │\n"
        f"  │  No model loaded. POST /manager/load first. │\n"
        f"  └─────────────────────────────────────────────┘\n"
        .replace("<port>", str(MANAGER_PORT))
    )
    yield
    logger.info("Shutting down — stopping vLLM ...")
    _kill_vllm()
    if _catalog is not None:
        _catalog.close()
        _catalog = None
    _config = None


app = FastAPI(
    title="vLLM Manager",
    version="1.0.0",
    description="Dynamic vLLM model loader with OpenAI-compatible proxy",
    lifespan=lifespan,
)


# ──────────────────────────────────────────────
# Manager control endpoints
# ──────────────────────────────────────────────

@app.get("/manager/status", tags=["manager"])
async def status():
    """Current state of the manager and loaded model."""
    return {
        "loaded_model":   current_model,
        "loading":        _loading,
        "vllm_pid":       vllm_process.pid if vllm_process else None,
        "loaded_at":      model_load_time,
        "loaded_at_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(model_load_time)) if model_load_time else None,
        "tp_size":        _current_tp,
        "gpu_mem_util":   _current_gpu_mem,
        "inner_endpoint": f"http://{VLLM_INNER_HOST}:{VLLM_INNER_PORT}",
    }


@app.post("/manager/reload", tags=["manager"])
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


@app.get("/manager/profiles", tags=["manager"])
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


@app.get("/manager/storage", tags=["manager"])
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


@app.get("/manager/catalog", tags=["manager"])
async def list_catalog(include_cache_only: str = Query("false")):
    """List catalog rows. Default-excludes synthetic cache-only rows."""
    include = _parse_strict_bool(include_cache_only, field="include_cache_only")
    if _catalog is None:
        return {"models": []}
    rows = _catalog.list_models()
    if not include:
        rows = [r for r in rows if not is_cache_only_alias(r.alias)]
    return {"models": [r.to_api_dict() for r in rows]}


@app.post("/manager/load", tags=["manager"])
async def load_model(request: Request):
    """
    Load a model. Unloads any currently loaded model first.

    ```json
    {
      "model": "Qwen/Qwen2.5-72B-Instruct-AWQ",
      "tp": 2,
      "gpu_mem": 0.90,
      "extra_args": ["--max-model-len", "32768"]
    }
    ```
    Only `model` is required. `tp` and `gpu_mem` default to the values
    set in docker-compose.yml environment variables.
    """
    if _loading:
        raise HTTPException(status_code=409, detail="Already loading a model. Wait or unload first.")

    body = await request.json()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="'model' field required")

    tp      = int(body.get("tp", DEFAULT_TP))
    gpu_mem = float(body.get("gpu_mem", DEFAULT_GPU_MEM))
    extra   = body.get("extra_args", [])

    async with loading_lock:
        try:
            await _start_vllm(model_id, tp, gpu_mem, extra)
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

    return {"status": "loaded", "model": current_model, "tp": _current_tp}


@app.post("/manager/unload", tags=["manager"])
async def unload_model():
    """Unload the current model and free all GPU memory."""
    if not current_model:
        return {"status": "nothing to unload"}
    was = current_model
    _kill_vllm()
    return {"status": "unloaded", "was": was}


@app.get("/manager/models", tags=["manager"])
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


@app.post("/manager/download", tags=["downloads"])
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


@app.get("/manager/download/{model_id:path}", tags=["downloads"])
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


@app.get("/manager/downloads", tags=["downloads"])
async def list_downloads():
    """List all download records (active, complete, and failed)."""
    return {"downloads": list(_downloads.values())}


@app.delete("/manager/download/{model_id:path}", tags=["downloads"])
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


@app.get("/health", tags=["manager"])
async def health():
    """Health check — always returns 200 from the manager itself."""
    return {
        "status": "ok",
        "model_loaded": current_model is not None,
        "loading": _loading,
    }


# ──────────────────────────────────────────────
# OpenAI-compatible proxy → inner vLLM
# ──────────────────────────────────────────────

VLLM_BASE = f"http://{VLLM_INNER_HOST}:{VLLM_INNER_PORT}"

# Model string aliases — map shorthand names to full HF model IDs.
# Add your own here or POST to /manager/aliases to add at runtime.
MODEL_ALIASES: dict[str, str] = {}


def _resolve_model(name: str) -> str:
    """Resolve a model alias to its full ID, or return as-is."""
    return MODEL_ALIASES.get(name, name)


async def _maybe_swap(requested_model: str) -> None:
    """
    If requested_model differs from the currently loaded model, swap to it.
    Uses the same tp/gpu_mem as the current session, or defaults if nothing
    is loaded yet. Blocks until the new model is ready.
    """
    resolved = _resolve_model(requested_model)

    if resolved == current_model:
        return  # already loaded, nothing to do

    if _loading:
        # Another swap is already in progress — wait for it, then check again
        while _loading:
            await asyncio.sleep(1)
        if current_model == resolved:
            return
        raise HTTPException(
            status_code=409,
            detail=f"A different model ('{current_model}') just finished loading. Retry your request."
        )

    logger.info(f"Auto-swap: '{current_model}' → '{resolved}'")
    async with loading_lock:
        # Re-check inside the lock in case another request beat us here
        if current_model == resolved:
            return
        try:
            await _start_vllm(resolved, _current_tp, _current_gpu_mem, [])
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))


async def _stream_vllm(response: httpx.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_bytes():
        yield chunk


async def _proxy(request: Request, path: str, body: bytes):
    """
    Forward a request to the inner vLLM server.
    If the request body contains a 'model' field that differs from the
    currently loaded model, swap to it first (auto-swap).
    """
    # ── Auto-swap on model mismatch ────────────────────────────────
    requested_model: Optional[str] = None
    if body:
        try:
            payload = json.loads(body)
            requested_model = payload.get("model")
        except Exception:
            pass

    if requested_model:
        await _maybe_swap(requested_model)
    elif not current_model:
        raise HTTPException(
            status_code=503,
            detail="No model loaded and no 'model' field in request. POST /manager/load first."
        )

    # ── Proxy to inner vLLM ────────────────────────────────────────
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    async with httpx.AsyncClient(timeout=None) as client:
        req = client.build_request(
            method=request.method,
            url=f"{VLLM_BASE}/{path}",
            headers=headers,
            content=body,
            params=dict(request.query_params),
        )
        response = await client.send(req, stream=True)

        if "text/event-stream" in response.headers.get("content-type", ""):
            return StreamingResponse(
                _stream_vllm(response),
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type="text/event-stream",
            )
        else:
            content = await response.aread()
            try:
                body_json = json.loads(content)
            except Exception:
                body_json = {"raw": content.decode(errors="replace")}
            return JSONResponse(
                content=body_json,
                status_code=response.status_code,
            )


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], tags=["openai"])
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

@app.get("/manager/aliases", tags=["manager"])
async def get_aliases():
    """List current model string aliases."""
    return {"aliases": MODEL_ALIASES}


@app.post("/manager/aliases", tags=["manager"])
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


@app.delete("/manager/aliases/{alias}", tags=["manager"])
async def delete_alias(alias: str):
    """Remove a model alias."""
    if alias not in MODEL_ALIASES:
        raise HTTPException(status_code=404, detail=f"Alias '{alias}' not found")
    MODEL_ALIASES.pop(alias)
    return {"deleted": alias}


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "vllm_manager:app",
        host=MANAGER_HOST,
        port=MANAGER_PORT,
        log_level="info",
    )
