# Agents Guide

This repository contains **Mnemosyne Inference**, with two isolated
single-workstation deployments. The CUDA deployment runs vLLM, llama.cpp, or
SGLang Diffusion in a
container; the native Apple Silicon deployment owns an official llama.cpp
server for GGUF, coordinates oMLX and DS4, and uses a process-isolated MFLUX
worker without Docker. LM Studio remains only as an explicitly enabled
migration/soak fallback. Both remain thin managers around
upstream engines and must not fork or embed their serving implementations.

## Repository Shape

- `vllm_manager.py` is the FastAPI service entrypoint. It starts two uvicorn servers, owns manager state, launches the active inference engine, proxies `/v1/*`, serves the admin UI, and wires all HTTP routes.
- `config.py`, `catalog.py`, `profiles.py`, `runtime.py`, and `image_api.py` hold the core substrate: YAML/.env loading, SQLite catalog state, profile resolution, pure engine argv/env builders, and bounded Images API normalization.
- `downloader.py` and `download_worker.py` implement install/download orchestration. Installs run as killable subprocesses and persist state in SQLite.
- `hf_search.py`, `repo_probe.py`, `vllm_supported_architectures.json`, and `scripts/refresh_arch_list.py` support HuggingFace discovery, vLLM architecture filtering, and GGUF probing.
- `ui/` contains the React/Vite/TypeScript/Tailwind admin UI that is built into `/app/static` by the Dockerfile and served from the admin plane.
- `vllm-ctl` is the Bash CLI for Docker lifecycle, admin API calls, model loading, installs, cache deletion, status, logs, and one-shot chat.
- `Dockerfile` defines the CUDA/Python runtime, builds the UI, builds a pinned `llama-server`, installs PyTorch cu129, and installs pinned vLLM plus manager dependencies. Runtime dependencies live here, not in a runtime `requirements.txt` or `pyproject.toml`.
- `requirements-dev.txt`, `pytest.ini`, `tests/`, and `ui/package.json` define the host-side Python and UI test/build workflows.
- `pg_writer.py` and `scripts/probe_token_sidecar_schema.py` implement and inspect the optional Postgres token-usage sink; SQLite remains the local system of record and durable outbox.
- `project_docs/project_status.md` records current release status and feature history; `project_docs/smoke_checks.md` is the manual GPU-host checklist for behavior pytest cannot exercise.
- `macos/service/` is an independent Python package for the native inference/control planes, engine adapters, lease-based global residency coordinator, and durable usage outbox. Its dependencies and lock file stay below that directory.
- `macos/service/storage.py`, `model_library.py`, `install_store.py`, `installer.py`, and `download_worker.py` implement exact nested-folder/volume validation, engine-aware Hugging Face discovery, and process-isolated durable native downloads. Managed downloads must remain residency-neutral.
- `macos/service/local_models.py` scans Finder-selected GGUF/MLX libraries without loading or copying weights. `macos/service/engines/llamacpp.py` translates typed profiles into a manager-owned upstream `llama-server` process while reusing the hardened managed-process ownership proof.
- `macos/service/security_scopes.py` consumes Finder-created transfer
  bookmarks and creates receiver-owned durable bookmarks for protected model
  folders. `scope_process.py` and `scope_worker.py` perform receipt and
  reactivation in bounded, killable process groups; startup revalidates
  configured grants and prunes unreferenced private bookmarks. Bookmark bytes
  remain private state; configuration carries only their SHA-256 `scope_id`.
  Production scope storage is anchored beside the active config under
  `state/security-scopes`, never derived from the user-configurable SQLite
  path.
- `macos/service/filesystem.py` and `fs_worker.py` keep protected-path
  inspection, scans, containment/header validation, directory creation, and
  size measurement in bounded, killable subprocess groups.
  `scoped_process.py` and `scope_exec.py` reactivate a persisted grant in each
  scoped helper/engine/download process and then `exec` the upstream command.
- `macos/service/sidecar_discovery.py` migrates the previous token sidecar's
  identity and ledger DSN through its user LaunchAgent, then atomically
  persists missing values into Unified Inference's private `.env`. Unified
  Inference is the native token sidecar; the legacy process is not in the
  inference path and must not remain a permanent reporting dependency.
- `macos/service/runtime_updates.py` discovers releases directly from the
  official upstreams, verifies and installs official `ggml-org/llama.cpp`
  Apple Silicon assets, installs MFLUX from PyPI, builds an exact DS4 GitHub
  commit, and provides atomic activation/rollback. Runtime activation must use
  the coordinator's all-engines-empty maintenance barrier; never introduce a
  repository-owned dependency manifest.
- `macos/image-worker/` is the separately locked MFLUX runtime. It is launched only as a manager-owned child, binds loopback `:17324`, and must remain dependency-isolated from the macOS coordinator service.
- `macos/app/` is the SwiftPM menu bar controller, typed native settings UI, secret-safe credential store, and native service bootstrap. `macos/packaging/` stages the signed app, embedded LaunchAgent plist, direct `Contents/MacOS/mnemosyne-service-bootstrap` executable, and relocatable Python runtime. Keep this unsandboxed `SMAppService` LaunchAgent's `BundleProgram` pointed at that direct helper; introducing a second bundle identity is unnecessary here and broke launch-requirement refresh during in-place updates. A future sandboxed or restricted-entitlement job would require its own deliberate wrapper architecture.
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
- `GET /v1/models` is the deliberate proxy exception: the manager serves it locally from the catalog so it lists every installed alias across all backends even while idle. A raw/resident model without an installed catalog row is included as a fallback.
- Supported backends are `vllm`, `llama.cpp`, and `sglang-diffusion`.
  - vLLM launches `vllm.entrypoints.openai.api_server`.
  - llama.cpp launches `llama-server` with a selected GGUF file and serves under the alias.
  - SGLang Diffusion launches the separately pinned `/opt/sglang` runtime and serves image profiles through `POST /v1/images/generations` on the same sequential inner port.
  - vLLM profiles in known slow CUDA-graph-capture SSM/hybrid families default to `--enforce-eager`, based on the cached model `config.json`. Keep the detection in `runtime.vllm_wants_eager_default`; profile `extra_args` remain the final override layer.
- Runtime configuration lives in YAML, default `/config/config.yaml`, with secrets in `/config/.env`.
- Persistent catalog state lives in SQLite, default `/state/mnemosyne.db`. It stores config-synced rows, UI-installed rows, download rows, resolved revisions, usage counters, backend, selected GGUF filename, model kind, capabilities, and image defaults.
- Config reload is supported by `POST /manager/reload`, `vllm-ctl reload`, or SIGHUP. Reload re-syncs config into the catalog and reconciles caches; it does not change Docker mounts or published ports.
- Idle eviction is enabled by `server.idle_unload_seconds` unless set to `null`. Usage deltas are flushed periodically and during unload/shutdown.
- Installs use a subprocess worker (`python -m download_worker`) rather than in-process HuggingFace downloads. Interrupted installs are recovered as `partial` on startup and can be retried.
- Legacy `/manager/download*` endpoints are preserved as v0 shims using synthetic cache-only aliases and the same persistent install pipeline.
- HuggingFace search and `/manager/hf/files` run on the admin plane, include compatibility signals, and detect GGUF candidates for llama.cpp installs.
- Token usage tracking: every successful `/v1/{chat/completions,completions,embeddings}` call queues a row in `_runtime.usage_rows`; `_flush_loop` writes it to the SQLite `request_usage` analytics table every 30s and on orderly teardown. When `token_sidecar.enabled` is set in YAML and `TOKEN_SIDECAR_POSTGRES_DSN` is in `.env`, the same flush also writes to SQLite `pg_usage_outbox`, and `_pg_flush_loop` (via `pg_writer.PgWriter`) drains it to the central Postgres ledger (`public.token_usage`). Once flushed from memory, the outbox is the durable retry queue; `event_id` UUIDs plus `ON CONFLICT DO NOTHING` make DELETE-after-success retry-safe. Respect `max_outbox_rows`, which intentionally drops the oldest rows at the cap. `/manager/status.token_sidecar` exposes outbox depth and last-flush metadata.

### Native macOS deployment

- Inference is on `127.0.0.1:1240` so Unified Inference is a drop-in replacement for the previous token sidecar; control is on `127.0.0.1:17321`, oMLX uses `:17322`, the manager-owned DS4 child uses `:17323`, the manager-owned MFLUX worker uses `:17324`, and manager-owned llama.cpp uses `:17325`. Reserve `17320` and `17326-17329` for later native services. The legacy sidecar must be booted out and persistently disabled or removed before Unified Inference binds `:1240`; merely unloading it lets it return at the next login. Legacy migration configs may explicitly keep LM Studio on `:1234`; fresh configs disable it.
- A per-user LaunchAgent owns Mnemosyne Core. The controller uses an explicit AppKit `NSStatusItem` with a SwiftUI popover; quitting it must not terminate inference. `SMAppService.agent` registers the embedded plist and the bootstrap must `execve` the bundled Python without daemonizing.
- `ResidencyCoordinator` owns the cross-engine invariant. A request holds an epoch-tagged model lease through its complete stream. FIFO queuing stops old-target admission once a switch is pending, drains active leases, proves all enabled adapters empty, loads one target, and proves exactly one ready manager-owned resident.
- oMLX is an external loopback service controlled through its native lifecycle APIs. llama.cpp and DS4 are model-specific process groups started by Mnemosyne. Never kill an unknown PID or listener; persisted managed-process identity must match executable, argv, start identity, and process group before recovery or signaling.
- Persisted llama.cpp survivor metadata must also retain the exact storage
  root, scope ID, and volume UUID so restart recovery can reconstruct and
  revalidate the protected-path target before adopting or signaling a child.
- Official oMLX model-directory reloads may synchronously preload pinned
  models. Any directory rescan inside the all-engines-empty maintenance
  barrier must therefore unload through oMLX's authoritative admin inventory
  and prove emptiness before admission reopens. A maintenance drain timeout
  must enter an explicit recoverable degraded state, never leave admission
  silently wedged.
- Unified Inference is the token sidecar for every language engine and central
  reporting defaults on. During migration, it may inherit `node.id` and the
  ledger DSN from the previous sidecar's LaunchAgent; explicit native values
  win. Persist missing inherited values into the private native `.env` before
  retiring that LaunchAgent. Keep the Settings identity read-only and never
  expose or log the DSN.
- The primary local migration surface is the read-only
  `GET /manager/model-library/local-sources` hint list, followed by
  Finder-driven `POST /manager/model-library/local-scan` and explicit
  `POST /manager/model-library/imports`. Source hints read LM Studio's
  configured download root and documented default without contacting its
  server, probing model paths, or requiring its adapter to be enabled.
  Finder must still confirm the exact folder before bookmark creation or
  scanning. Scan again before persisting opaque candidate/projector IDs; never
  preselect every candidate, load a model, copy weights, treat `mmproj` as a
  primary model, or replace an exact nested/symlink path with its resolved
  target or mount root. Preserve matching aliases and compatible load
  settings. The LM Studio inventory endpoint remains only for the temporary
  soak fallback.
- The native Storage UI always uses the macOS directory picker. Preserve the exact selected folder (including paths such as `/Volumes/Athena/models`) separately from the containing mount and its UUID; never replace it with the volume root or make users type it.
- The menu app must create an ordinary bookmark while its `NSOpenPanel` grant
  is live. Its implicit extension is the Apple-supported single interprocess
  handoff; the service must explicitly consume it and create its own durable
  security-scoped bookmark. Do not claim or add App Sandbox bookmark
  entitlements without a deliberate signing/sandbox architecture change.
  Store only the receiver-owned bookmark below the mode-`0700` private
  `state/security-scopes` directory beside the active config, with mode-`0600`
  files. Store only its SHA-256 `scope_id` in YAML and never return or log
  bookmark bytes. Preflight every referenced grant before saving config;
  revalidate configured scopes before coordinator startup and prune
  unreferenced private bookmarks only after loading persisted config. Receipt
  and preflight must run in killable subprocess groups with deadlines. Scoped
  helpers and manager-owned model/download children must reactivate the durable
  bookmark in-process before `exec`.
- macOS can block bookmark and filesystem calls while a protected-folder
  decision is pending. Keep bookmark receipt/reactivation, storage inspection,
  local scans, profile/path resolution, GGUF/projector header validation,
  directory creation, and directory sizing in killable subprocess groups off
  the asyncio event loop and behind bounded deadlines. Timeout, request
  cancellation, and service shutdown must terminate the complete helper
  process group and fail closed with an actionable permission/volume diagnostic
  while both HTTP planes remain responsive.
- Native Hugging Face installs are engine-aware and durable. llama.cpp search
  requires explicit GGUF quant/shard selection and explicit optional projector
  pairing, pins the resolved Hub revision, and downloads only that exact file
  set. DS4 and MFLUX use verified curated candidates; oMLX search exposes
  metadata-derived compatibility honestly and downloaded snapshots must be
  classified before profile registration so they advertise only detected
  generation, embeddings, or rerank routes. Downloads run out of process,
  never load a model, and oMLX directory changes run only through the
  coordinator's all-engines-empty maintenance barrier. Treat
  downloaded-but-not-registered weights as a durable retryable state; retry
  profile registration without redownloading them.
- Runtime update checks are read-only. oMLX remains externally owned; never
  replace its app or Homebrew files. llama.cpp must come from the official
  `ggml-org/llama.cpp` macOS arm64 release asset and pass published size,
  SHA-256, safe-extraction, executable, and CLI-contract checks. MFLUX must
  come from its official PyPI project and DS4 from an exact commit in
  `antirez/ds4`. Staging may happen
  while a model is resident, but pointer activation and rollback must drain
  leases, unload every engine, and prove global emptiness. Keep old runtimes
  for recovery and never place model weights inside them.
- Keep the native MFLUX catalog limited to text-to-image configurations the pinned worker can dispatch through `/v1/images/generations`, and persist each candidate's model-specific defaults when an install becomes a profile. An official Hub checkpoint is not installable merely because it shares a family name: keep unsupported layouts, such as Krea 2 Raw under the current Turbo-only loader, visibly unavailable rather than creating a broken profile.
- A packaged installation must not persist a checkout virtualenv in
  `engines.mflux.python`. Leave that override unset so the app bootstrap or
  active managed runtime supplies the packaged MFLUX Python and worker source;
  checkout paths and `MNEMOSYNE_MFLUX_*` overrides are development-only.
- Adapter-observed state is authoritative. An unreachable/unauthorized/incompatible adapter is not implicitly empty. Startup defaults to `unload_all`; uncertain state fails closed until `/manager/reconcile` succeeds.
- The Mac proxy supports capability-gated Chat/Completions, Responses, Messages, Embeddings, Rerank, and Images Generations routes. MFLUX is terminated on unload/abort so Metal memory is released at the process boundary.
- The menu app reads and writes structured configuration through the control plane. The service remains the schema authority and atomically persists validated YAML; credential values are write-only in the UI and never returned by the API. Preserve `schema_version`; an older app must refuse to save a newer schema instead of dropping unknown fields. Config snapshots carry an optimistic revision that every save must echo. Serialize saves, download-completion profile creation, and local imports through the same mutation lock; reject a stale Settings save instead of overwriting a concurrent model addition.
- The ordinary Mac Models page must not create profiles from raw model or
  projector text fields. New profiles come from the engine-aware Model Library
  or Finder discovery; imported engine/source/storage/served-name/projector
  facts remain read-only, and users choose only engine-valid typed model roles
  (Generation, Embeddings, Rerank, or Image).
- `macos/packaging/build_app.sh` signs ad hoc by default and accepts
  `CODESIGN_IDENTITY` for a stable signing identity. Do not imply
  durable protected-folder grants survive arbitrary ad-hoc rebuilds; after a
  code-identity change, the user may need to reselect the folder.
- Mac usage events normalize OpenAI, Responses, and Anthropic token shapes and atomically write local analytics plus the SQLite Postgres outbox. The central schema and retry/idempotency behavior match the CUDA deployment.
- Image requests intentionally do not emit token-usage records. Do not add image prompt/output policy hooks; this repository is a local homelab tool.

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

Most CUDA runtime workflows happen through Docker because vLLM and the
CUDA-linked llama.cpp build are container-host concerns.

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
./vllm-ctl install local-model org/repo-gguf --backend llama.cpp --gguf-filename model.Q4_K_M.gguf
./vllm-ctl install qwen-image Qwen/Qwen-Image --backend sglang-diffusion --gpus 0
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
uv sync --project macos/image-worker --extra dev
uv run --project macos/image-worker --extra dev python -m pytest macos/image-worker/tests
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
`uv run --project macos/service --extra dev python -m pytest macos/service/tests`.
For MFLUX worker changes, run its independent suite under `macos/image-worker`.
For menu/bootstrap changes, run `swift build` and `swift test` from `macos/app`.
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
- If the CUDA llama.cpp pin is bumped, rebuild the image, check `llama-server`
  CLI compatibility, and verify the CUDA-linked binary with GPU passthrough.
  Native llama.cpp updates instead use the official-source integrity and
  activation checks described above.

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
