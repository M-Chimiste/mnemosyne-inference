# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

[agents.md](agents.md) is the longer-form companion (module boundaries, development constraints, reading order); keep the two consistent when architecture changes.

## Repository scope

Core runtime files:

- [vllm_manager.py](vllm_manager.py) — FastAPI service that supervises an inference engine subprocess (vLLM or llama.cpp), proxies an OpenAI-compatible API, serves the admin UI, and wires every HTTP route.
- [config.py](config.py), [catalog.py](catalog.py), [profiles.py](profiles.py), [runtime.py](runtime.py) — YAML/env loading, SQLite catalog (schema, reconcile, durable install state), profile/alias resolution, and pure backend argv/env builders.
- [downloader.py](downloader.py), [download_worker.py](download_worker.py) — install orchestration. Installs run as killable `python -m download_worker` subprocesses; state and resume metadata live in SQLite.
- [hf_search.py](hf_search.py), [repo_probe.py](repo_probe.py), [vllm_supported_architectures.json](vllm_supported_architectures.json), [scripts/refresh_arch_list.py](scripts/refresh_arch_list.py) — HuggingFace search, vLLM architecture filtering, and GGUF candidate detection for llama.cpp installs.
- [ui/](ui/) — React/Vite/TypeScript/Tailwind admin UI built into `/app/static` by the Dockerfile and served from the admin plane.
- [Dockerfile](Dockerfile) — CUDA 13 / Python image that builds the UI, compiles a pinned `llama-server` with CUDA, then installs PyTorch (cu129) and a pinned stable vLLM. There is no runtime `requirements.txt` / `pyproject.toml`; runtime deps live only here. Host-side test deps are in [requirements-dev.txt](requirements-dev.txt).
- [vllm-ctl](vllm-ctl) — Bash CLI that wraps `docker compose` + the manager HTTP API.

The live `docker-compose.yml` is machine-specific and may live outside this repo. `vllm-ctl` expects it at `$VLLM_COMPOSE_DIR` (default `~/vllm-manager`). Use [docker-compose.example.yml](docker-compose.example.yml) as the maintained template. When making changes that touch container config (env vars, volumes, ports, mounts, container name, build args), flag that the external compose file may need updating too.

## Architecture

**Two HTTP planes, one container.** The entrypoint starts an inference FastAPI app on `:8000` and an admin FastAPI app on `:8001`, sharing module state. Inference exposes `/v1/*` and `/health`; optional bearer auth is enabled only when `INFERENCE_API_KEY` is set. Admin exposes `/manager/*`, `/ui/`, `/docs`, `/openapi.json`, `/redoc`, and a superset `/v1/*` behind HTTP Basic (`admin:$ADMIN_PASSWORD`). If `ADMIN_PASSWORD` is unset, admin bind is forced to `127.0.0.1` inside the container — published `:8001` becomes unreachable from the host. This is intentional fail-safe behavior; keep it.

**Inner engine on loopback `:8002`.** The manager launches either `vllm.entrypoints.openai.api_server` or `llama-server` (selected per profile via `backend: vllm | llama.cpp`) as a child `subprocess.Popen` on `127.0.0.1:$VLLM_INNER_PORT` (default `8002`). Do not reuse `8000`/`8001`; startup rejects collisions because all three listeners share the same container netns.

**One model at a time.** `_start_vllm(profile)` always calls `_kill_vllm` first — switching models is destroy + relaunch, not hot-swap. Live state is held in `_runtime` (`RuntimeState`); `_swap_lock` serializes load/unload transitions; `_loading_target` + `_load_event` let same-target callers piggyback on a single load. `server.idle_unload_seconds` (default 900s; `null` to disable) evicts the resident model after inactivity.

**Auto-swap on `/v1/*` proxy.** `_proxy` peeks at the JSON body's `model` field and resolves it through (in order) config aliases, catalog `ui_install` rows, installed HF IDs, legacy `MODEL_ALIASES`, then gated raw `org/repo` / absolute path fallback. The resolved engine-served name is rewritten into the body before proxying. Different-target callers queue up to `server.swap_queue_timeout_seconds` (default 300s) before getting a 504. Streaming responses (`text/event-stream`) pass through via `StreamingResponse`; non-streaming bodies are read fully and re-emitted as JSON. Multimodal payloads stay opaque.

**Installs are killable subprocesses with persistent state.** `/manager/install` (and `vllm-ctl install`) spawns `python -m download_worker`; status, revision, backend, and GGUF filename persist in SQLite at `/state/mnemosyne.db`. On startup and on `vllm-ctl reload`, the manager reconciles each aliased install against its cache directory and marks anything missing/incomplete as `partial` — re-spawn via `install-retry` (add `--force` to wipe first). The legacy `/manager/download*` endpoints survive as v0 shims backed by synthetic `__cache__:` aliases and the same install pipeline; the `__cache__:` namespace is reserved and aliases must be lowercase alphanumeric/hyphen.

**Storage.** `HF_HOME=/hf-cache` is the default cache; additional drives are declared in `config.yaml`'s `storage.locations` and must be backed by host bind mounts in `docker-compose.yml` (adding a drive requires `docker compose up -d`, not just `vllm-ctl reload`). `/manager/models` walks `$HF_HOME/hub` and decodes `models--org--name` directory names back into `org/name`. Cache deletion must stay under configured storage roots and refuses active installs/downloads and the resident model.

**Config reload.** `POST /manager/reload`, `vllm-ctl reload`, or SIGHUP re-syncs config into the catalog and reconciles caches. It does not change Docker mounts or published ports — for those, edit `docker-compose.yml` and `docker compose up -d`.

**Token usage tracking.** Every successful `/v1/{chat/completions,completions,embeddings}` call (streaming and non-streaming) is accounted for. `_append_usage_row` queues a row in `_runtime.usage_rows`; `_flush_loop` drains it every 30s into the SQLite `request_usage` table for local analytics. Streaming usage relies on `_ensure_stream_usage` injecting `stream_options.include_usage: true` so vLLM emits the trailing SSE `usage` event (which the proxy then strips back out for clients that didn't ask for it). When `token_sidecar.enabled` is set in `config.yaml` and `TOKEN_SIDECAR_POSTGRES_DSN` is in `.env`, the same rows are mirrored into the SQLite `pg_usage_outbox` table and a separate `_pg_flush_loop` drains the outbox to a central Postgres ledger (`public.token_usage`) via [pg_writer.py](pg_writer.py). The outbox is the durable cache: a Postgres outage or container restart never drops data, and the postgres path uses `event_id` UUIDs + `ON CONFLICT DO NOTHING` so DELETE-after-success is safe to retry. `/manager/status.token_sidecar` reports `outbox_pending`, `last_flush_count`, and `last_error`.

## Common commands

All real runtime workflows go through Docker — `vllm_manager.py` cannot run on macOS (needs CUDA + vLLM/llama.cpp). The Python test suite, however, runs anywhere.

```bash
# Container lifecycle (wraps docker compose against $VLLM_COMPOSE_DIR/docker-compose.yml)
./vllm-ctl build
./vllm-ctl start            # waits for /health, then prints status
./vllm-ctl stop             # docker compose down — also unloads the model
./vllm-ctl restart
./vllm-ctl logs -f
./vllm-ctl shell

# Model control
./vllm-ctl load qwen-72b-awq                              # alias from config.yaml
./vllm-ctl load Qwen/Qwen3-8B -- --max-model-len 32768    # `--` passes extra args to vllm
./vllm-ctl unload
./vllm-ctl status
./vllm-ctl list                                           # configured profiles
./vllm-ctl models                                         # HF cache contents
./vllm-ctl chat "..."                                     # one-shot completion

# Installs (persistent, resumable)
./vllm-ctl install qwen-coder Qwen/Qwen2.5-Coder-7B-Instruct --storage nvme-fast
./vllm-ctl install local-gguf org/repo-gguf --backend llama.cpp --gguf-filename model.Q4_K_M.gguf
./vllm-ctl install-status [alias]
./vllm-ctl install-cancel <alias>
./vllm-ctl install-retry  <alias> [--force]
./vllm-ctl cache-delete --alias <alias> [--remove-row]

# Config / storage
./vllm-ctl reload
./vllm-ctl storage

# Direct API
curl http://localhost:8000/health
ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' ~/vllm-manager/.env | head -1 | cut -d= -f2-)"
curl -u admin:"$ADMIN_PASSWORD" http://localhost:8001/manager/status
curl -u admin:"$ADMIN_PASSWORD" -X POST http://localhost:8001/manager/load \
  -H 'Content-Type: application/json' -d '{"model":"qwen-coder"}'
# Admin docs/UI: http://localhost:8001/docs, http://localhost:8001/ui/
```

### Tests and syntax checks

Host-side Python tests run without CUDA/vLLM (`requirements-dev.txt` lists what's needed; nothing imports `vllm` at module load):

```bash
pip install -r requirements-dev.txt    # one-time on dev host
python -m pytest -q                    # full suite (pytest.ini sets asyncio_mode=auto)
python -m pytest -q tests/test_proxy.py::test_streaming_passthrough   # single test
python -m py_compile vllm_manager.py config.py catalog.py profiles.py runtime.py \
  downloader.py download_worker.py hf_search.py repo_probe.py logsetup.py
bash -n vllm-ctl
```

UI tests / build (from `ui/`):

```bash
npm test          # vitest run
npm run build     # tsc + vite build → dist/ (Dockerfile copies this into /app/static)
```

When behavior touches process launch, ports, engine argv, Docker mounts, or GPU code, add or run a targeted test in `tests/` and call out any Docker smoke check that still needs a CUDA host.

## Conventions worth noticing

- Profiles come from YAML config + SQLite catalog resolution; legacy env vars (`MODEL_ALIASES`, `VLLM_DEFAULT_TP`, etc.) are fallback only. `MNEMOSYNE_CONFIG_PATH`, `MNEMOSYNE_ENV_PATH`, `MNEMOSYNE_DB_PATH` override the `/config/*` / `/state/*` defaults.
- The vLLM/llama.cpp subprocess inherits stdout/stderr from the manager — engine logs interleave with manager logs.
- For vLLM: `--disable-log-requests` is always passed; `--trust-remote-code` only when the resolved profile enables it; `extra_args` is the escape hatch and is appended last. Preserve this pattern for any new flags rather than embedding logic in the manager.
- The container name is hardcoded to `vllm-manager` in `vllm-ctl` (`docker inspect`, `docker exec`); the compose file must use that service/container name.
- The external compose file must publish both `8000:8000` and `8001:8001` for host-side admin commands. Without `ADMIN_PASSWORD`, the admin app intentionally binds loopback inside the container and the published admin port will not be reachable.
- Keep the manager a thin wrapper: don't fork or embed vLLM/llama.cpp serving logic, don't leak admin Basic auth or inference bearer headers into the inner engine, and don't move mutation endpoints onto the inference plane.
- If vLLM is bumped: regenerate `vllm_supported_architectures.json` (see README §"Refreshing architecture support"). If llama.cpp is bumped: re-check `llama-server` CLI flags and the `CMAKE_CUDA_ARCHITECTURES` build arg in the Dockerfile.
