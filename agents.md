# Agents Guide

This repository contains **Mnemosyne Inference**, with two isolated
single-workstation deployments. The CUDA deployment runs vLLM/llama.cpp in a
container; the native Apple Silicon deployment coordinates an existing LM
Studio server, oMLX, and DS4 without Docker. Both remain thin managers around
upstream engines and must not fork or embed their serving implementations.

## Repository Shape

- `vllm_manager.py` is the FastAPI service entrypoint. It starts two uvicorn servers, owns manager state, launches the active inference engine, proxies `/v1/*`, serves the admin UI, and wires all HTTP routes.
- `config.py`, `catalog.py`, `profiles.py`, and `runtime.py` hold the core substrate: YAML/.env loading, SQLite catalog state, profile resolution, and pure argv/env builders for vLLM and llama.cpp.
- `downloader.py` and `download_worker.py` implement install/download orchestration. Installs run as killable subprocesses and persist state in SQLite.
- `hf_search.py`, `repo_probe.py`, `vllm_supported_architectures.json`, and `scripts/refresh_arch_list.py` support HuggingFace discovery, vLLM architecture filtering, and GGUF probing.
- `ui/` contains the React/Vite/TypeScript/Tailwind admin UI that is built into `/app/static` by the Dockerfile and served from the admin plane.
- `vllm-ctl` is the Bash CLI for Docker lifecycle, admin API calls, model loading, installs, cache deletion, status, logs, and one-shot chat.
- `Dockerfile` defines the CUDA/Python runtime, builds the UI, builds a pinned `llama-server`, installs PyTorch cu129, and installs pinned vLLM plus manager dependencies. Runtime dependencies live here, not in a runtime `requirements.txt` or `pyproject.toml`.
- `requirements-dev.txt`, `pytest.ini`, `tests/`, and `ui/package.json` define the host-side Python and UI test/build workflows.
- `pg_writer.py` and `scripts/probe_token_sidecar_schema.py` implement and inspect the optional Postgres token-usage sink; SQLite remains the local system of record and durable outbox.
- `project_docs/project_status.md` records current release status and feature history; `project_docs/smoke_checks.md` is the manual GPU-host checklist for behavior pytest cannot exercise.
- `macos/service/` is an independent Python package for the native inference/control planes, engine adapters, lease-based global residency coordinator, and durable usage outbox. Its dependencies and lock file stay below that directory.
- `macos/app/` is the SwiftPM menu bar controller and native service bootstrap. `macos/packaging/` stages the signed app, embedded LaunchAgent plist, and relocatable Python runtime.
- `macos/config.yaml.example`, `macos/.env.example`, `macos/README.md`, and `macos/smoke_checks.md` are the native deployment's setup and validation surface. Mac settings must not be added to the external CUDA compose file.
- `agents.md` is the single repository guide for coding assistants and contributors. Keep it aligned with code, examples, and verification commands when architecture or workflows change.

The live `docker-compose.yml` is intentionally machine-specific and may live outside this repo. The CLI expects it under `$VLLM_COMPOSE_DIR`, defaulting to `~/vllm-manager`. Use `docker-compose.example.yml` as the maintained template. If a change affects ports, env vars, volumes, container names, build args, or mounts, call out the required external compose changes.

## Current Architecture

### CUDA deployment

- The container runs **two HTTP planes** in one Python process:
  - Inference plane on `:8000`: `/v1/*` and `/health`.
  - Admin plane on `:8001`: `/manager/*`, `/ui/`, `/docs`, `/openapi.json`, `/redoc`, and admin-authenticated `/v1/*`.
- The active engine runs behind the manager on loopback, default `127.0.0.1:8002` via `VLLM_INNER_PORT`. Do not collide this with the external inference or admin ports.
- Admin uses HTTP Basic as `admin:$ADMIN_PASSWORD`. If `ADMIN_PASSWORD` is unset, admin bind is forced to `127.0.0.1` inside the container, which makes the published Docker admin port unreachable from the host.
- Inference bearer auth is optional. If `INFERENCE_API_KEY` is set, `/v1/*` on the inference plane requires `Authorization: Bearer <key>`.
- Only one model is resident at a time. `_swap_lock` serializes load/unload transitions; same-target callers can piggyback on a single lazy load; different targets queue until `swap_queue_timeout_seconds`.
- Proxied `/v1/*` requests peek at the JSON `model` field, resolve it through config aliases, UI-installed catalog aliases, legacy aliases, installed HF IDs, raw HF IDs, or absolute paths, then rewrite the model field to the engine-served name. Streaming requests may also receive `stream_options.include_usage=true` for accounting; synthetic usage events are hidden unless the client requested them. All other request fields, including multimodal content and backend-specific extensions, stay opaque.
- `GET /v1/models` is the deliberate proxy exception: the manager serves it locally from the catalog so it lists every installed alias across both backends even while idle. A raw/resident model without an installed catalog row is included as a fallback.
- Supported backends are `vllm` and `llama.cpp`.
  - vLLM launches `vllm.entrypoints.openai.api_server`.
  - llama.cpp launches `llama-server` with a selected GGUF file and serves under the alias.
  - vLLM profiles in known slow CUDA-graph-capture SSM/hybrid families default to `--enforce-eager`, based on the cached model `config.json`. Keep the detection in `runtime.vllm_wants_eager_default`; profile `extra_args` remain the final override layer.
- Runtime configuration lives in YAML, default `/config/config.yaml`, with secrets in `/config/.env`.
- Persistent catalog state lives in SQLite, default `/state/mnemosyne.db`. It stores config-synced rows, UI-installed rows, download rows, resolved revisions, usage counters, backend, and selected GGUF filename.
- Config reload is supported by `POST /manager/reload`, `vllm-ctl reload`, or SIGHUP. Reload re-syncs config into the catalog and reconciles caches; it does not change Docker mounts or published ports.
- Idle eviction is enabled by `server.idle_unload_seconds` unless set to `null`. Usage deltas are flushed periodically and during unload/shutdown.
- Installs use a subprocess worker (`python -m download_worker`) rather than in-process HuggingFace downloads. Interrupted installs are recovered as `partial` on startup and can be retried.
- Legacy `/manager/download*` endpoints are preserved as v0 shims using synthetic cache-only aliases and the same persistent install pipeline.
- HuggingFace search and `/manager/hf/files` run on the admin plane, include compatibility signals, and detect GGUF candidates for llama.cpp installs.
- Token usage tracking: every successful `/v1/{chat/completions,completions,embeddings}` call queues a row in `_runtime.usage_rows`; `_flush_loop` writes it to the SQLite `request_usage` analytics table every 30s and on orderly teardown. When `token_sidecar.enabled` is set in YAML and `TOKEN_SIDECAR_POSTGRES_DSN` is in `.env`, the same flush also writes to SQLite `pg_usage_outbox`, and `_pg_flush_loop` (via `pg_writer.PgWriter`) drains it to the central Postgres ledger (`public.token_usage`). Once flushed from memory, the outbox is the durable retry queue; `event_id` UUIDs plus `ON CONFLICT DO NOTHING` make DELETE-after-success retry-safe. Respect `max_outbox_rows`, which intentionally drops the oldest rows at the cap. `/manager/status.token_sidecar` exposes outbox depth and last-flush metadata.

### Native macOS deployment

- Inference is on `127.0.0.1:17320`, control is on `127.0.0.1:17321`, LM Studio remains on `:1234`, oMLX uses `:17322`, and the manager-owned DS4 child uses `:17323`. Reserve `17324-17329` for later native services.
- A per-user LaunchAgent owns Mnemosyne Core. The SwiftUI `MenuBarExtra` app is a client; quitting it must not terminate inference. `SMAppService.agent` registers the embedded plist and the bootstrap must `execve` the bundled Python without daemonizing.
- `ResidencyCoordinator` owns the cross-engine invariant. A request holds an epoch-tagged model lease through its complete stream. FIFO queuing stops old-target admission once a switch is pending, drains active leases, proves all enabled adapters empty, loads one target, and proves exactly one ready manager-owned resident.
- LM Studio and oMLX are external loopback services controlled through native lifecycle APIs. DS4 is a model-specific process group started by Mnemosyne. Never kill an unknown PID or listener; persisted DS4 identity must match before recovery or signaling.
- Adapter-observed state is authoritative. An unreachable/unauthorized/incompatible adapter is not implicitly empty. Startup defaults to `unload_all`; uncertain state fails closed until `/manager/reconcile` succeeds.
- The Mac proxy supports capability-gated Chat/Completions, Responses, Messages, Embeddings, and Rerank routes. Aside from model canonicalization and OpenAI stream-usage opt-in, payload fields stay opaque.
- Mac usage events normalize OpenAI, Responses, and Anthropic token shapes and atomically write local analytics plus the SQLite Postgres outbox. The central schema and retry/idempotency behavior match the CUDA deployment.

## Configuration

The CUDA and Mac configurations are intentionally separate. The following
section describes CUDA; native settings live in `macos/config.yaml.example` and
are copied to `~/Library/Application Support/Mnemosyne/config.yaml`.

The canonical host setup is a workstation directory such as `~/vllm-manager` containing:

- `docker-compose.yml` copied from `docker-compose.example.yml`.
- `config.yaml` copied from `config.yaml.example`.
- `.env` copied from `.env.example`.
- `state/` for the SQLite database.

Important environment variables:

- `MNEMOSYNE_REPO_DIR`: lets the external compose file find this repo's Dockerfile.
- `MNEMOSYNE_CONFIG_PATH`: defaults to `/config/config.yaml`.
- `MNEMOSYNE_ENV_PATH`: defaults to `/config/.env`.
- `MNEMOSYNE_DB_PATH`: defaults to `/state/mnemosyne.db`.
- `VLLM_INNER_PORT`: defaults to `8002`.
- `ADMIN_PASSWORD`: required for host/LAN access to the admin plane.
- `INFERENCE_API_KEY`: optional bearer key for inference-plane `/v1/*`.
- `HUGGING_FACE_HUB_TOKEN`: optional token for gated HuggingFace repos, read by install workers after restart.
- `TOKEN_SIDECAR_POSTGRES_DSN`: optional secret-bearing DSN used only when `token_sidecar.enabled` is true.
- `MNEMOSYNE_LOG_FORMAT`: `json` by default; set `text` for locally readable logs.
- `LLAMA_SERVER_BIN` and `MNEMOSYNE_UI_DIR`: development overrides for the llama.cpp binary and built UI directory.

Model profiles support aliases, HF model IDs, revision, quantization, GPU plan, max context, storage location, backend, GGUF filename, and raw `extra_args`. Aliases must be lowercase alphanumeric/hyphen and cannot use the reserved `__cache__:` namespace.

## Development Constraints

- Preserve the thin-wrapper design. Do not embed custom serving logic or fork vLLM/llama.cpp behavior into the manager.
- Keep OpenAI-compatible request bodies as pass-through as possible. Intentional mutations are limited to model-name canonicalization and streaming usage opt-in for accounting; preserve all remaining JSON fields verbatim.
- Maintain backward-compatible shims where they exist, especially `POST /manager/load`, `POST /manager/download`, `/manager/download*`, legacy aliases, and existing `vllm-ctl` workflows.
- Treat the external compose file as user-managed. Do not assume it can be edited from this repo.
- Be careful with cache deletion. Deletion must stay under configured storage roots, refuse active installs/downloads, and refuse resident models.
- Keep admin-only mutation endpoints off the inference plane.
- Do not store secrets in `config.yaml`, committed examples, logs, catalog rows, or UI state.
- Preserve `extra_args` as the escape hatch for new engine flags and append them last.
- Prefer the existing module boundaries:
  - `config.py` for config/env loading and validation.
  - `catalog.py` for SQLite schema, migrations, reconcile, and durable state.
  - `profiles.py` for alias/profile resolution.
  - `runtime.py` for pure backend argv/env construction and runtime state shape.
  - `downloader.py` and `download_worker.py` for install subprocess lifecycle.
  - `hf_search.py` and `repo_probe.py` for Hub metadata and format compatibility.
  - `vllm_manager.py` for app wiring, auth, proxying, and engine lifecycle.
- Preserve the native boundary: Mac code may share protocol/usage concepts but
  must not import `vllm_manager.py`, CUDA profiles, or Docker runtime modules.
- In Mac code, engine mutation belongs to adapters and cross-engine ordering
  belongs to `ResidencyCoordinator`. The HTTP layer must acquire a lease before
  opening upstream and release it only after the complete body/stream closes.
- Keep inner Mac engines on loopback. A non-loopback Mnemosyne inference bind
  requires `INFERENCE_API_KEY`; a non-loopback control bind requires
  `ADMIN_PASSWORD`.

## Common Commands

Most real runtime workflows happen through Docker because vLLM/CUDA and llama.cpp CUDA builds are container-host concerns.

```bash
./vllm-ctl build
./vllm-ctl start
./vllm-ctl stop
./vllm-ctl restart
./vllm-ctl status
./vllm-ctl logs -f
```

Model, config, and storage operations:

```bash
./vllm-ctl load qwen-72b-awq
./vllm-ctl load Qwen/Qwen3-8B --tp 1 -- --max-model-len 32768
./vllm-ctl unload
./vllm-ctl list
./vllm-ctl models
./vllm-ctl reload
./vllm-ctl storage
./vllm-ctl chat "What model are you?"
```

Install and cache operations:

```bash
./vllm-ctl install qwen-coder Qwen/Qwen2.5-Coder-7B-Instruct --storage nvme-fast
./vllm-ctl install TheBloke/Some-GGUF --list-gguf
./vllm-ctl install local-gguf org/repo-gguf --backend llama.cpp --gguf-filename model.Q4_K_M.gguf
./vllm-ctl install-status
./vllm-ctl install-status qwen-coder
./vllm-ctl install-cancel qwen-coder
./vllm-ctl install-retry qwen-coder --force
./vllm-ctl cache-delete --alias qwen-coder
./vllm-ctl cache-delete --alias qwen-coder --remove-row
```

Legacy download shims:

```bash
./vllm-ctl download <model-id>
./vllm-ctl download-status <model-id>
./vllm-ctl downloads
```

Direct API examples:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/v1/models
curl -u admin:"$ADMIN_PASSWORD" http://localhost:8001/manager/status
curl -u admin:"$ADMIN_PASSWORD" -X POST http://localhost:8001/manager/load \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen-coder"}'
```

Native macOS development and verification:

```bash
uv sync --project macos/service --extra dev
uv run --project macos/service --extra dev python -m pytest macos/service/tests
uv run --project macos/service mnemosyne-macos --check-config \
  --config "$HOME/Library/Application Support/Mnemosyne/config.yaml" \
  --env "$HOME/Library/Application Support/Mnemosyne/.env"
cd macos/app && swift build && swift test
```

## Verification Expectations

For docs-only changes, a readback, relative-link audit, and `git diff --check` are enough. When deleting or renaming documentation, remove references from the README, examples, and this guide in the same change.

For Python or CLI changes, prefer at least:

- `python -m py_compile vllm_manager.py config.py catalog.py profiles.py runtime.py downloader.py download_worker.py hf_search.py repo_probe.py pg_writer.py logsetup.py`
- `bash -n vllm-ctl`
- `python -m pytest -q`

For UI changes, run from `ui/`:

```bash
npm test
npm run build
```

For native service changes, run
`uv run --project macos/service --extra dev python -m pytest macos/service/tests`. For
menu/bootstrap changes, run `swift build` and `swift test` from `macos/app`.
LaunchAgent registration, Metal memory release, and real engine swapping still
require the target Mac and `macos/smoke_checks.md`; full Xcode is required for
the packaged `SMAppService` smoke.

When behavior touches process launch, ports, engine argv construction, Docker mounts, or GPU behavior, add or run targeted tests and call out any manual Docker smoke checks that still need a CUDA host.

## Safety Notes

- Never discard user changes in this repository.
- Avoid destructive git commands unless the user explicitly requests them.
- Cache wiping must remain path-safe and catalog-aware.
- Do not let admin auth, inference bearer handling, cookies, or authorization headers leak to the inner engine.
- If `ADMIN_PASSWORD` is unset, admin bind must continue to fail safe to container loopback.
- Keep multimodal request payloads opaque through the proxy.
- If vLLM is bumped, rebuild the image and regenerate `vllm_supported_architectures.json` from the new runtime registry.
- If llama.cpp is bumped, rebuild the image, check `llama-server` CLI compatibility, and verify the CUDA-linked binary with GPU passthrough.

## Useful Reading Order

1. `README.md`
2. `project_docs/project_status.md`
3. `vllm_manager.py`
4. `config.py`, `catalog.py`, `profiles.py`, `runtime.py`
5. `downloader.py`, `download_worker.py`
6. `hf_search.py`, `repo_probe.py`
7. `pg_writer.py`
8. `vllm-ctl`
9. `Dockerfile`, `config.yaml.example`, `.env.example`, and `docker-compose.example.yml`
10. `ui/src/`
11. `project_docs/smoke_checks.md` for CUDA-host validation
12. `macos/README.md`, then `project_docs/macos_native_architecture.md`, for the native sibling
