# Mnemosyne Inference v1 Implementation Plan

**Source:** [PRD.md](PRD.md)  
**Scope:** High-level phased plan for delivering Mnemosyne Inference v1 from the current single-file manager into a config-driven model server with admin UI, persistent catalog, multi-drive storage, lazy loading, and inference/admin plane separation.

## Guiding Principles

- Preserve the thin-wrapper architecture: vLLM remains an opaque subprocess launched via `vllm.entrypoints.openai.api_server`.
- Keep the OpenAI-compatible inference path headless and stable while moving admin capabilities onto a separate listener.
- Build durable foundations before UI polish: config schema, catalog, process lifecycle, and route separation should land before the React app depends on them.
- Maintain backward compatibility where the PRD calls for it: existing `POST /manager/load`, `POST /manager/download`, and current CLI workflows become shims over the new internals.
- Treat the system as single-user, single-workstation software, but still fail safe for LAN exposure.

## Phase 0: Project Foundation and Safety Rails

**Goal:** Create the scaffolding needed to evolve the current prototype without losing working behavior.

**Primary work:**

- Confirm repository layout for the Python module split:
  - `vllm_manager.py` for app wiring, route registration, proxying, and subprocess lifecycle.
  - `config.py` for YAML and `.env` loading.
  - `catalog.py` for SQLite persistence.
  - `downloader.py` for background install/download subprocess orchestration.
  - `hf_search.py` for HuggingFace discovery and compatibility checks.
- Add baseline examples:
  - `config.yaml.example`.
  - `.env.example`.
  - Initial `vllm_supported_architectures.json` placeholder or generated snapshot.
- Decide the test harness shape before refactoring too far:
  - Unit tests for config validation and catalog CRUD.
  - FastAPI route tests for plane separation and auth.
  - Lightweight process lifecycle tests using mocks instead of real vLLM where possible.
- Pin the vLLM nightly version in the Dockerfile rather than installing an unbounded `--pre`.
- Preserve current behavior with smoke tests around:
  - `/health`.
  - `/manager/status`.
  - `/manager/load`.
  - `/v1/*` proxy auto-swap behavior.

**Exit criteria:**

- Current manager behavior is covered by basic tests or documented smoke checks.
- Example config and env files exist.
- Docker dependency pins are explicit enough to make future regressions diagnosable.

## Phase 1: Config, Profiles, Storage, and Catalog Core

**Goal:** Replace env-var driven model loading with a durable registry model while leaving runtime serving behavior mostly intact.

**Primary work:**

- Implement `config.py`:
  - Load `/config/config.yaml`.
  - Validate `server`, `storage`, `defaults`, and `models` sections.
  - Support `gpus: all`, explicit GPU lists, `quantization`, `max_model_len`, `storage`, and `extra_args`.
  - Probe GPU indices via `nvidia-smi -L`.
  - Validate storage names and warn on missing or unwritable storage paths.
  - Hard fail on malformed config at container start.
- Implement `.env` loading from `/config/.env` at process startup.
- Implement `catalog.py`:
  - Create SQLite database at `/state/mnemosyne.db`.
  - Add `models` and `downloads` tables from the PRD schema.
  - Sync config-defined aliases into the catalog with `source='config'`.
  - Preserve `source='ui_install'` rows across config reloads.
  - Reconcile missing cache paths as `partial`.
- Add config reload path:
  - SIGHUP support.
  - `POST /manager/reload` shim on the admin plane once plane separation lands.
- Extend status/list endpoints enough for early CLI and debugging:
  - Configured aliases.
  - Storage locations and free space.
  - Catalog rows.

**Exit criteria:**

- Editing `config.yaml` and triggering reload makes new aliases visible without a container restart.
- Config aliases and persisted UI-installed aliases merge with config aliases taking precedence.
- The system can resolve an alias into a concrete runtime profile object.

## Phase 2: Runtime Lifecycle, Lazy Loading, Queueing, and Idle Eviction

**Goal:** Make inference requests operate against config/catalog profiles instead of raw model IDs.

**Primary work:**

- Change `_start_vllm` to accept a resolved profile object.
- Build vLLM command arguments from profile fields:
  - `--model`.
  - `--tensor-parallel-size`.
  - `--gpu-memory-utilization`.
  - `--quantization` when set.
  - `--max-model-len` when set.
  - `extra_args` appended verbatim.
- Build subprocess environment from profile fields:
  - `CUDA_VISIBLE_DEVICES` for explicit GPU lists.
  - No CUDA override for `gpus: all`.
  - Per-profile `HF_HOME` from the selected storage location.
- Update `/v1/*` auto-swap:
  - Resolve `model` as alias first, then raw model ID fallback if desired.
  - Queue requests during swaps instead of returning 409.
  - Enforce `swap_queue_timeout_seconds` with 504 on timeout.
  - Return 503 to pending requests if vLLM fails during load.
  - Do not auto-restart crashed vLLM in v1.
- Implement idle eviction:
  - Track `last_used_at` on successful proxied requests.
  - Background task unloads after `idle_unload_seconds`.
  - Support `null` for never evict.
- Buffer usage writes:
  - Periodically persist `last_used_at` and `request_count`.
  - Avoid SQLite writes directly in the hot path.
- Extend `/manager/status`:
  - Resident alias/profile.
  - GPU plan.
  - Quantization.
  - `last_used_at`.
  - Idle seconds and seconds until eviction.

**Exit criteria:**

- A request to `/v1/chat/completions` with `"model": "<alias>"` lazy-loads the correct profile and serves the request.
- Concurrent requests during model swaps wait in arrival order and either complete or time out predictably.
- Idle eviction frees the resident model within the configured window.
- Existing `POST /manager/load` still works as a compatibility shim.

## Phase 3: Inference/Admin Plane Separation and Auth

**Goal:** Enforce the PRD's security boundary at the network listener level.

**Primary work:**

- Split the single FastAPI app into:
  - `inference_app` for `/v1/*` and `/health`.
  - `admin_app` for `/manager/*`, `/ui/*`, and admin-superset `/v1/*`.
- Run both apps in one process and event loop using two uvicorn servers.
- Apply admin authentication:
  - HTTP Basic for all admin routes and static UI.
  - Username `admin`.
  - Password from `ADMIN_PASSWORD`.
- Apply inference authentication:
  - Optional bearer token when `INFERENCE_API_KEY` is set.
  - LAN-open when unset.
- Implement fail-safe admin bind:
  - If `ADMIN_PASSWORD` is unset, force admin bind to `127.0.0.1` unless config already specifies loopback.
  - Log a clear warning.
- Move mutating routes exclusively to the admin app:
  - Load/unload.
  - Reload.
  - Install/download/cancel/retry.
  - Alias/catalog mutation.
  - Cache delete.
- Update `vllm-ctl`:
  - Use `VLLM_ADMIN_URL`, with backward-compatible fallback from `VLLM_MANAGER_URL`.
  - Read `ADMIN_PASSWORD` from env or the configured `.env`.
  - Send HTTP Basic credentials for admin commands.
  - Keep inference commands pointed at the inference URL when appropriate.

**Exit criteria:**

- `POST /manager/*` on the inference port returns 404.
- Admin routes require HTTP Basic when exposed beyond loopback.
- Existing CLI commands continue to work against the admin port.

## Phase 4: Install, Download, Cache, and Multi-Drive Storage Workflows

**Goal:** Make the persistent catalog operational for installs, resumable downloads, and cache management.

**Primary work:**

- Implement `GET /manager/storage`:
  - Return configured storage locations.
  - Include current free space and writeability status.
- Implement `POST /manager/install`:
  - Validate alias, model ID, quantization, GPU plan, storage, `max_model_len`, and `extra_args`.
  - Check free space at the selected storage location using `size_estimate_gb * 1.1`.
  - Resolve `HUGGING_FACE_HUB_TOKEN` from env for gated repos.
  - Insert/update catalog and download rows in one transaction.
  - Spawn a killable download subprocess.
- Implement download subprocess orchestration in `downloader.py`:
  - Wrap `huggingface_hub.snapshot_download`.
  - Set `cache_dir` to the chosen storage path.
  - Emit line-delimited JSON progress events.
  - Persist progress to the `downloads` table.
- Add lifecycle routes:
  - `POST /manager/install/{alias}/cancel`.
  - `POST /manager/install/{alias}/retry`.
  - `POST /manager/install/{alias}/retry?force=true`.
  - `GET /manager/downloads`.
  - `GET /manager/download/{alias}` or equivalent catalog-backed status route.
- Implement cache deletion:
  - `DELETE /manager/cache/{model_id:path}`.
  - Refuse if the model is resident.
  - Mark aliased rows as `partial` rather than deleting the catalog row.
- Keep `POST /manager/download` as a cache-only compatibility shim over the install/download internals.

**Exit criteria:**

- A model can be installed to a non-default storage location and later loaded with the right `HF_HOME`.
- Downloads survive manager restart as recoverable `partial` or retryable records.
- Cancel, retry, and force retry have predictable catalog states.
- Deleting cache for an aliased model leaves the alias recoverable from the UI/CLI.

## Phase 5: HuggingFace Search and vLLM Compatibility Filter

**Goal:** Provide reliable model discovery without pretending HuggingFace exposes a vLLM compatibility flag.

**Primary work:**

- Implement architecture support source:
  - Runtime vLLM registry introspection at startup.
  - Fallback to bundled `vllm_supported_architectures.json`.
  - Loud logging when fallback is used.
- Add `scripts/refresh_arch_list.py`:
  - Introspect the installed vLLM registry.
  - Regenerate the bundled JSON.
  - Use it during vLLM upgrade workflows.
- Implement `hf_search.py`:
  - Call `HfApi.list_models` with a transformers/text-generation prefilter.
  - Fetch and cache each candidate's `config.json`.
  - Read `architectures`.
  - Flag compatibility and explain incompatible results.
  - Estimate size by summing safetensors siblings where available.
- Add `GET /manager/hf/search`:
  - Parameters: `q`, `limit`, `filter_compat`.
  - Return compatible and incompatible results with reasons.

**Exit criteria:**

- Searching for common text-generation models returns architecture metadata and compatibility status.
- Incompatible results are visible but clearly explained.
- vLLM registry introspection can fail without breaking search entirely.

## Phase 6: Admin UI

**Goal:** Deliver the browser-based control plane described in the PRD using the completed admin APIs.

**Primary work:**

- Scaffold `ui/` with:
  - React.
  - Vite.
  - TypeScript.
  - Tailwind CSS.
  - TanStack Query.
  - `react-router`.
- Configure local dev:
  - Vite dev server on `:5173`.
  - Proxy API calls to the admin port on `:8001`.
- Implement shared API client and polling queries.
- Build v1 views:
  - Dashboard: resident model, GPU/status summary, idle countdown, recent request count, unload action.
  - Catalog: unified model catalog with load, evict, retry-download, cancel-download, delete-from-disk, and remove-from-catalog actions.
  - Search and Install: HF search, compatibility flags, quick install form, storage selector, quantization/GPU/max length fields.
  - Downloads: in-flight and recent downloads with progress, throughput, errors, cancel, and retry.
- Keep business logic server-side:
  - UI calls API endpoints.
  - UI does not implement compatibility, install state transitions, or storage rules independently.
- Serve UI from admin app:
  - Mount built assets at `/ui`.
  - Redirect `/` to `/ui` on the admin port.
  - Add SPA fallback to `index.html`.

**Exit criteria:**

- A user can search, install, monitor, load, unload, and delete cache from the UI.
- UI polling reflects manager state within 2-5 seconds.
- The UI is not reachable from the inference port.

## Phase 7: Packaging, Compose, and Operational Docs

**Goal:** Make the system easy to build, run, upgrade, and recover on the target workstation.

**Primary work:**

- Update Dockerfile:
  - Multi-stage Node 22 build for `ui/dist`.
  - Final CUDA stage copies static UI into `/app/static`.
  - Add Python dependencies: `pyyaml`, `pydantic`, and any explicitly chosen test/runtime deps.
  - Copy new Python modules, JSON architecture file, and scripts.
  - Expose both inference and admin ports.
- Update compose guidance:
  - Mount `/config`.
  - Mount `/state`.
  - Mount each storage location declared in config.
  - Map both ports.
  - Document host paths, especially `~/vllm-manager/config.yaml` and `~/vllm-manager/.env`.
- Update `vllm-ctl` help text and examples:
  - `list` for configured aliases.
  - `models` for cached files.
  - `reload`.
  - Auth and URL environment variables.
- Add operational documentation:
  - First-time setup.
  - Adding a drive.
  - Installing gated models.
  - Recovering partial downloads.
  - Refreshing architecture support after vLLM upgrade.
  - Security expectations for LAN exposure.

**Exit criteria:**

- Fresh setup from examples can start the container and reach both ports.
- Docker build includes the UI and all new Python modules.
- CLI help matches the new behavior.

## Phase 8: Verification, Hardening, and Release Readiness

**Goal:** Prove the v1 success criteria and remove sharp edges before treating the PRD as implemented.

**Primary work:**

- Run acceptance scenarios from the PRD:
  - Config alias reload without restart.
  - Single GPU and all-GPU launch command generation.
  - Lazy load and auto-swap through `/v1/*`.
  - Swap queue timeout.
  - Idle eviction.
  - Admin operation unavailable on inference port.
  - Install, cancel, retry, and restart recovery.
  - Multi-drive install and load.
  - Cache deletion behavior.
- Add multimodal validation:
  - Confirm OpenAI-format image content blocks proxy unchanged.
  - Run at least one small vision-model integration test where practical.
- Improve observability:
  - Structured JSON logs.
  - Clear errors for missing HF token, bad config, insufficient disk, bad GPU index, and vLLM startup failure.
- Review failure modes:
  - vLLM crash during load.
  - Manager restart during download.
  - Missing storage mount.
  - Corrupt or stale SQLite rows.
  - Admin password missing with non-loopback bind requested.
- Decide what remains deferred:
  - Multi-model concurrent serving.
  - Chat playground.
  - Prometheus metrics.
  - Startup prewarm.
  - Quantization variant discovery.
  - vLLM auto-restart.

**Exit criteria:**

- All PRD success criteria are either passing or explicitly marked deferred with rationale.
- Known v1 limitations are documented.
- The system is ready for daily use on the workstation.

## Suggested Delivery Milestones

1. **M1: Registry Server Core**
   - Phases 0-2.
   - User can run config-driven aliases, lazy loading, queueing, and idle eviction without the UI.

2. **M2: Safe Admin Boundary**
   - Phase 3.
   - Inference/admin separation and auth are enforced.

3. **M3: Durable Install Manager**
   - Phases 4-5.
   - Catalog-backed installs, downloads, storage locations, and HF discovery work through APIs and CLI.

4. **M4: Admin UI**
   - Phase 6.
   - Browser control plane is usable for normal admin workflows.

5. **M5: Workstation-Ready Release**
   - Phases 7-8.
   - Docker, compose, docs, and acceptance checks are complete.

## Key Dependencies and Ordering Notes

- The UI should wait until the catalog, storage, install, and search APIs are stable enough to avoid rewriting client-side state assumptions.
- Plane separation should happen before broadening admin capabilities, so new mutating endpoints do not accidentally appear on the inference port.
- The download subprocess model should land before cancellable UI workflows, because cancel/retry behavior is a backend state-machine problem.
- vLLM architecture introspection should be built before Search and Install, but it can be backed by the bundled JSON first if runtime introspection is temporarily brittle.
- Multimodal support should remain mostly a verification task unless tests reveal the proxy mutates request bodies.

## Major Risks

- **vLLM internal registry instability:** Mitigate with fallback JSON and refresh script.
- **Queueing complexity around swaps:** Keep a simple arrival-ordered queue first; add per-target optimization only after correctness is clear.
- **Download progress fidelity:** Start with durable status and coarse byte progress before optimizing throughput details.
- **Storage mount mismatch:** Validate and surface warnings early; do not fail boot solely because a non-default drive is temporarily missing.
- **Auth and bind mistakes:** Test route availability by port as part of acceptance, not only by code inspection.
- **UI scope creep:** Keep v1 operational only. No chat playground until the admin workflows are stable.
