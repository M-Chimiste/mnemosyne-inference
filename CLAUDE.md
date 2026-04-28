# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository scope

Core runtime files:

- [vllm_manager.py](vllm_manager.py) — FastAPI service that supervises a vLLM subprocess and proxies an OpenAI-compatible API to it.
- [config.py](config.py), [catalog.py](catalog.py), [profiles.py](profiles.py), [runtime.py](runtime.py) — YAML/env loading, SQLite catalog, profile resolution, and pure vLLM argv/env builders.
- [Dockerfile](Dockerfile) — CUDA 12.8 / Python 3.11 image that bakes in PyTorch (cu128), vLLM nightly (Blackwell sm_100 kernels), FastAPI/uvicorn/httpx/huggingface_hub. There is no `requirements.txt` or `pyproject.toml`; dependencies live only in the Dockerfile.
- [vllm-ctl](vllm-ctl) — Bash CLI that wraps `docker compose` + the manager HTTP API.

`docker-compose.yml` is **not** in this repo. `vllm-ctl` expects it at `$VLLM_COMPOSE_DIR` (default `~/vllm-manager`). When making changes that touch container config (env vars, volumes, ports), remember the compose file lives outside the repo — flag this so the user can update it.

## Architecture

**Two HTTP servers, one container.** The manager (FastAPI, port 8000, LAN-facing) runs as the entrypoint. It launches `vllm.entrypoints.openai.api_server` as a child `subprocess.Popen` on port 8001 (loopback). The manager proxies `/v1/*` requests to the inner vLLM and exposes `/manager/*` control endpoints (load, unload, status, models, download, aliases) plus `/health` and `/docs`.

**One model at a time.** `_start_vllm(profile)` always calls `_kill_vllm` first — switching models is destroy + relaunch, not hot-swap. Live model state is held in `_runtime` (`RuntimeState`), with `vllm_process` kept separately. `_swap_lock` serializes load/unload transitions, while `_loading_target` + `_load_event` let same-target requests piggyback on one load.

**Auto-swap on `/v1/*` proxy.** `_proxy` peeks at the JSON body's `model` field, resolves it through config aliases, catalog `ui_install` rows, legacy `MODEL_ALIASES`, then gated raw `org/repo` or absolute path fallback. `ensure_loaded` queues swaps with a deadline instead of returning 409 during another load. Streaming responses (`text/event-stream`) are passed through with `StreamingResponse`; non-streaming bodies are read fully and re-emitted as JSON.

**Downloads run in an OS thread, not asyncio.** `snapshot_download` from `huggingface_hub` is blocking, so `/manager/download` spawns a `threading.Thread` and tracks state in the module-level `_downloads` dict. Endpoints read this dict directly. Status values: `queued | downloading | complete | error`. There is no persistence — `_downloads` is lost on container restart.

**HF cache is a Docker volume.** `HF_HOME=/hf-cache` in the Dockerfile; the compose file is expected to mount a named volume there so models survive container restarts. `/manager/models` walks `$HF_HOME/hub` and decodes `models--org--name` directory names back into `org/name`.

## Common commands

All real workflows go through Docker — `vllm_manager.py` cannot run on macOS (needs CUDA + vLLM).

```bash
# Build / lifecycle (wraps docker compose against $VLLM_COMPOSE_DIR/docker-compose.yml)
./vllm-ctl build
./vllm-ctl start            # waits for /health, then prints status
./vllm-ctl stop             # docker compose down — also unloads the model
./vllm-ctl logs -f

# Model control
./vllm-ctl load Qwen/Qwen2.5-72B-Instruct-AWQ --tp 2 --gpu-mem 0.90
./vllm-ctl load Qwen/Qwen3-8B -- --max-model-len 32768   # `--` passes extra args to vllm
./vllm-ctl unload
./vllm-ctl status
./vllm-ctl models           # list HF cache contents
./vllm-ctl chat "..."       # one-shot completion against the loaded model

# Downloads (background, pollable)
./vllm-ctl download <model-id>
./vllm-ctl download-status <model-id>

# Direct API (manager listens on $MANAGER_URL, default http://localhost:8000)
curl -X POST http://localhost:8000/manager/load -d '{"model":"...","tp":2}'
# Auto-generated FastAPI docs: http://localhost:8000/docs
```

Run `python -m pytest -q` for the committed test suite. Use `python -m py_compile vllm_manager.py runtime.py config.py catalog.py profiles.py` and `bash -n vllm-ctl` for lightweight syntax checks.

## Conventions worth noticing

- Runtime profiles come from YAML config + SQLite catalog resolution. Legacy env vars still provide fallback process defaults and outer/inner port settings.
- The vLLM subprocess inherits stdout/stderr from the manager — its logs interleave directly with manager logs.
- `--disable-log-requests` is always passed. `--trust-remote-code` is passed only when the resolved profile enables it. `extra_args` stays the escape hatch and is appended last.
- The container name is hardcoded to `vllm-manager` in `vllm-ctl` (`docker inspect`, `docker exec`). The compose file must use that service/container name.
