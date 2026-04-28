# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository scope

Three files do all the work:

- [vllm_manager.py](vllm_manager.py) — FastAPI service that supervises a vLLM subprocess and proxies an OpenAI-compatible API to it.
- [Dockerfile](Dockerfile) — CUDA 12.8 / Python 3.11 image that bakes in PyTorch (cu128), vLLM nightly (Blackwell sm_100 kernels), FastAPI/uvicorn/httpx/huggingface_hub. There is no `requirements.txt` or `pyproject.toml`; dependencies live only in the Dockerfile.
- [vllm-ctl](vllm-ctl) — Bash CLI that wraps `docker compose` + the manager HTTP API.

`docker-compose.yml` is **not** in this repo. `vllm-ctl` expects it at `$VLLM_COMPOSE_DIR` (default `~/vllm-manager`). When making changes that touch container config (env vars, volumes, ports), remember the compose file lives outside the repo — flag this so the user can update it.

## Architecture

**Two HTTP servers, one container.** The manager (FastAPI, port 8000, LAN-facing) runs as the entrypoint. It launches `vllm.entrypoints.openai.api_server` as a child `subprocess.Popen` on port 8001 (loopback). The manager proxies `/v1/*` requests to the inner vLLM and exposes `/manager/*` control endpoints (load, unload, status, models, download, aliases) plus `/health` and `/docs`.

**One model at a time.** `_start_vllm` always calls `_kill_vllm` first — switching models is destroy + relaunch, not hot-swap. Global state (`current_model`, `vllm_process`, `_current_tp`, `_current_gpu_mem`, `_loading`) is module-level; `loading_lock` (asyncio) serializes load/unload.

**Auto-swap on `/v1/*` proxy.** `_proxy` peeks at the JSON body's `model` field; if it differs from `current_model`, `_maybe_swap` triggers a load before forwarding. This is the implicit path most clients hit — they don't have to call `/manager/load` explicitly. Aliases (`MODEL_ALIASES`, populated via `/manager/aliases`) are resolved here too. Streaming responses (`text/event-stream`) are passed through with `StreamingResponse`; non-streaming bodies are read fully and re-emitted as JSON.

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

There is no test suite, no linter config, and no formatter config in this repo. Don't invent commands for them.

## Conventions worth noticing

- Config is environment-only (`VLLM_INNER_PORT`, `VLLM_DEFAULT_TP`, `VLLM_GPU_MEM_UTIL`, `VLLM_STARTUP_TIMEOUT`, `HF_HOME`, `HUGGING_FACE_HUB_TOKEN`). Set these in the external compose file, not in code.
- The vLLM subprocess inherits stdout/stderr from the manager — its logs interleave directly with manager logs.
- `--trust-remote-code` and `--disable-log-requests` are hardcoded into the vLLM launch command in `_start_vllm`.
- The container name is hardcoded to `vllm-manager` in `vllm-ctl` (`docker inspect`, `docker exec`). The compose file must use that service/container name.
