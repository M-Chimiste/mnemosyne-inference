# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository scope

Core runtime files:

- [vllm_manager.py](vllm_manager.py) — FastAPI service that supervises a vLLM subprocess and proxies an OpenAI-compatible API to it.
- [config.py](config.py), [catalog.py](catalog.py), [profiles.py](profiles.py), [runtime.py](runtime.py) — YAML/env loading, SQLite catalog, profile resolution, and pure vLLM argv/env builders.
- [Dockerfile](Dockerfile) — CUDA 12.8 / Python 3.11 image that bakes in PyTorch (cu128), vLLM nightly (Blackwell sm_100 kernels), FastAPI/uvicorn/httpx/huggingface_hub. There is no `requirements.txt` or `pyproject.toml`; dependencies live only in the Dockerfile.
- [vllm-ctl](vllm-ctl) — Bash CLI that wraps `docker compose` + the manager HTTP API.

The live `docker-compose.yml` is expected to be machine-specific and may live outside this repo. `vllm-ctl` expects it at `$VLLM_COMPOSE_DIR` (default `~/vllm-manager`). Use [docker-compose.example.yml](docker-compose.example.yml) as the maintained template. When making changes that touch container config (env vars, volumes, ports), remember the live compose file may be outside the repo — flag this so the user can update it.

## Architecture

**Two HTTP servers, one container.** The entrypoint starts an inference FastAPI app on `:8000` and an admin FastAPI app on `:8001`, sharing the same module state. Inference exposes `/v1/*` and `/health`; optional bearer auth is enabled only when `INFERENCE_API_KEY` is set. Admin exposes `/manager/*`, `/docs`, `/openapi.json`, `/redoc`, and `/v1/*` as a superset behind HTTP Basic (`admin:$ADMIN_PASSWORD`). If `ADMIN_PASSWORD` is unset, admin bind is forced to `127.0.0.1` inside the container, which is not reachable through Docker `-p 8001:8001`; use `docker exec` or set `ADMIN_PASSWORD` for host-side admin access.

**Inner vLLM moved to loopback `:8002`.** The manager launches `vllm.entrypoints.openai.api_server` as a child `subprocess.Popen` on `127.0.0.1:8002` by default. Do not reuse `8000` or `8001` for `VLLM_INNER_PORT`; startup rejects collisions because all three listeners share the same container network namespace.

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

# Direct API
curl http://localhost:8000/health
curl -u admin:"$ADMIN_PASSWORD" http://localhost:8001/manager/status
curl -u admin:"$ADMIN_PASSWORD" -X POST http://localhost:8001/manager/load \
  -H 'Content-Type: application/json' -d '{"model":"...","tp":2}'
# Admin docs: http://localhost:8001/docs (Basic auth)
```

Run `python -m pytest -q` for the committed test suite. Use `python -m py_compile vllm_manager.py runtime.py config.py catalog.py profiles.py` and `bash -n vllm-ctl` for lightweight syntax checks.

## Conventions worth noticing

- Runtime profiles come from YAML config + SQLite catalog resolution. Legacy env vars still provide fallback process defaults and inner port settings.
- The vLLM subprocess inherits stdout/stderr from the manager — its logs interleave directly with manager logs.
- `--disable-log-requests` is always passed. `--trust-remote-code` is passed only when the resolved profile enables it. `extra_args` stays the escape hatch and is appended last.
- The container name is hardcoded to `vllm-manager` in `vllm-ctl` (`docker inspect`, `docker exec`). The compose file must use that service/container name.
- The external compose file must publish both `8000:8000` and `8001:8001` for host-side admin commands. Without `ADMIN_PASSWORD`, the admin app intentionally binds loopback inside the container and the published admin port will not be reachable.
