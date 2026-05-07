# Mnemosyne Inference — Project Status

**Last updated:** 2026-05-07

## Current state

**Active milestone:** M5 — Workstation-ready release
**Active phase:** Phase 9 (llama.cpp backend) code landed; Phase 8 verification
and Phase 5 snapshot gate still open.

M3 is implemented: `/manager/install` is end-to-end functional with
killable subprocess downloads, restart-recoverable state, multi-drive
storage routing, and a catalog-backed legacy `/manager/download` shim.
Phase 5 code has landed: `GET /manager/hf/search` returns compatibility-
flagged results sourced from runtime registry introspection (with a bundled
JSON snapshot fallback), and `/manager/status` reports `vllm_arch_count` /
`vllm_arch_source` so operators can see when the fallback is active. Phase 6
code has landed: the admin port now serves a React/Vite SPA, exposes live
best-effort GPU telemetry via `/manager/gpu`, and keeps `/ui/*` off the
inference plane. Phase 7 docs/CLI packaging work has landed with a top-level
README, executable `vllm-ctl`, and corrected admin auth examples. Phase 9
adds `llama-server` as a parallel backend so GGUF-only repos install and
serve without vLLM in the loop; auto-detection runs at install time, the
catalog persists per-row backend + chosen GGUF filename, and reconcile
validates the specific shard set. Phase 5 is still not marked complete
until the bundled architecture snapshot is regenerated inside the pinned
vLLM container and committed.

## Phase status

| Phase | Goal | Status |
|---|---|---|
| 0 — Foundation & safety rails | Examples, test harness, vLLM pin | ✅ Done; download baseline fixed (2026-04-28) |
| 1 — Config, profiles, storage, catalog core | Declarative YAML config + SQLite catalog + profile resolver | ✅ Done (2026-04-28) |
| 2 — Runtime lifecycle, lazy load, queue, idle eviction | Profile-driven `_start_vllm`, swap queue, idle eviction | ✅ Done (2026-04-28) |
| 3 — Plane separation & auth | Two FastAPI apps (inference :8000, admin :8001), HTTP Basic | ✅ Done (2026-04-28) |
| 4 — Install, download, cache, multi-drive | `/manager/install` + cancellable subprocess downloads | ✅ Done; review fixes landed (2026-04-28) |
| 5 — HF search & vLLM compatibility filter | `GET /manager/hf/search`, runtime registry introspection | ⚠️ Code landed; generated snapshot pending |
| 6 — Admin UI | React + Vite SPA on the admin port | ✅ Done; host verification complete (2026-04-29) |
| 7 — Packaging, compose, docs | Multi-stage Dockerfile, compose mounts, ops docs | ✅ Docs/CLI landed; CUDA quickstart smoke pending |
| 8 — Verification & hardening | PRD acceptance scenarios | ⚠️ Code/docs landed; workstation acceptance pending |
| 9 — llama.cpp backend for GGUF | Auto-detected llama-server dispatch alongside vLLM | ⚠️ Code landed (2026-05-07); CUDA workstation smoke pending |

## What has landed

**Phase 0**

- Baseline test harness, smoke checklist, example config/env files, and pinned
  Docker vLLM dependency.
- Archived plan: [plans/phase_0.md](plans/phase_0.md).

**Phase 1**

- `config.py` loads YAML config and `/config/.env`, validates storage/model
  references, and probes GPU indices when available.
- `catalog.py` manages SQLite state at `/state/mnemosyne.db`, syncs config
  aliases, preserves `ui_install` rows, and reconciles cache status.
- `profiles.py` resolves config/catalog aliases into `ResolvedProfile`.
- Manager endpoints now expose reload, configured profiles, storage locations,
  and catalog rows.
- Archived plan: [plans/phase_1.md](plans/phase_1.md).

**Phase 2**

- `runtime.py` provides pure vLLM argv/env builders and `RuntimeState`.
- `_start_vllm` consumes `ResolvedProfile`, sets per-profile `HF_HOME`,
  computes tensor-parallel size from GPU plan, and respects
  `trust_remote_code`, quantization, max context, and `extra_args`.
- `/v1/*` and `POST /manager/load` resolve models through config aliases,
  catalog `ui_install` rows, legacy `MODEL_ALIASES`, then gated raw
  `org/repo` or absolute-path fallback.
- Swap queueing replaces 409-on-race behavior with deadline-bounded waiting,
  same-target piggybacking, 504 on timeout, and 503 on vLLM load failure.
- Idle eviction and buffered usage writes update `last_used_at` and
  `request_count` without hot-path SQLite writes.
- `/manager/status` is additive: legacy keys remain, with alias, GPU plan,
  quantization, idle countdown, in-flight count, and swap target added.
- `vllm-ctl status` prints the new fields when present.
- Archived plan: [plans/phase_2.md](plans/phase_2.md).

**Phase 3**

- Split inference (`:8000`) and admin (`:8001`) FastAPI apps; admin is a
  superset that includes `/v1/*` for back-compat plus `/manager/*` and
  `/docs`/`/openapi.json`/`/redoc`.
- HTTP Basic auth on admin (`admin:$ADMIN_PASSWORD`); fail-safe bind to
  `127.0.0.1` inside the container when `ADMIN_PASSWORD` is unset.
- Optional bearer auth on inference (`INFERENCE_API_KEY`); admin Basic and
  inference Bearer headers are stripped before proxying to the inner vLLM.
- Inner vLLM moved from loopback `:8001` → `:8002` so the admin app and inner
  server don't collide in the container's network namespace; startup checks
  reject overrides that re-collide.
- Single SIGTERM handler at the asyncio gather level shuts down both
  uvicorn instances atomically.
- Archived plan: [plans/phase_3.md](plans/phase_3.md).

**Phase 4**

- `POST /manager/install` accepts a fully-typed install request (alias,
  HF model, revision, quantization, GPU plan, `max_model_len`, storage,
  `extra_args`) and persists `models` + `downloads` rows in one
  transaction, then spawns a killable subprocess download. It now returns
  `202 Accepted` for queued async work.
- New `download_worker.py` (subprocess) wraps `huggingface_hub.snapshot_download`,
  emits line-delimited JSON progress on stdout, exits 130 on SIGTERM, and
  links its lifetime to the manager via `prctl(PR_SET_PDEATHSIG)` on Linux.
- New `downloader.py` (manager-side) owns the live subprocess registry,
  parses worker stdout in a daemon thread, and writes catalog state through
  `mark_*` methods. HF token is threaded explicitly into a per-subprocess
  env dict — `os.environ` is never mutated.
- Catalog gains a `threading.RLock`, `revision TEXT NOT NULL DEFAULT 'main'`
  + `resolved_sha TEXT` columns (with additive ALTERs for legacy DBs), and
  9 transition methods (`start_install_tx`, `mark_downloading`,
  `mark_progress`, `mark_complete`, `mark_error`, `mark_cancelled`,
  `mark_orphan_interrupted`, `mark_partial`, `delete_install_row`) plus
  `find_active_for` (revision-agnostic), `find_active_by_hf_id`,
  `find_repo_siblings`, `lookup_by_hf_id`, and `recover_orphan_downloads`.
- Reconcile resolves snapshots per-revision via `<repo>/refs/<revision>` (or
  the direct `snapshots/<sha>/` path for 40-hex commit SHAs), refuses
  `..`/absolute paths, and skips `status='error'` rows so a hard-failed
  install isn't silently promoted by a half-finished snapshot.
- Resident vLLM is pinned to the exact downloaded snapshot: `mark_complete`
  records `resolved_sha`, and `resolve_profile` prefers it over the symbolic
  revision when emitting `--revision`. Every invalidation path clears stale
  SHA pins, including start/retry, error, cancel, orphan recovery, partial
  transitions, config-sync cache invalidation, and reconcile downgrades.
- Restart recovery: lifespan calls `reap_orphans_on_startup` **before**
  `apply_config` so reconcile may promote any whose snapshot landed cleanly
  before the crash; lifespan teardown SIGTERMs all in-flight installs.
- Cache delete has two flavors with sibling-aware cleanup:
  `DELETE /manager/install/{alias}/cache` (wipe disk + mark every sibling
  `partial`) and `DELETE /manager/install/{alias}` (wipe + remove this row);
  `DELETE /manager/cache/{model_id:path}` is the legacy by-HF-id form.
  All paths are gated on residency + active-download checks; wipes refuse
  paths outside `storage.locations[].path` and fail closed without mutating
  catalog rows when a wipe is refused or fails.
- `_resolve_request_model` gates `ui_install` rows on `status='installed'`
  so queued/partial/error installs return a 409 instead of falling through
  to raw-HF passthrough or launching vLLM with incomplete weights.
- Public `/manager/install` rejects reserved synthetic cache aliases
  (`__cache__:` / `__cache__/`). Only the legacy `/manager/download` shim may
  create synthetic cache rows internally.
- Worker-emitted error messages are preserved in the catalog instead of
  being collapsed to a generic subprocess exit code.
- Legacy `POST /manager/download` is now a catalog-backed shim that
  preserves the v0 body shape (including the default `ignore_patterns`
  list), creates a synthetic-alias `ui_install` row, and runs through the
  same subprocess pipeline. Status route resolves by exact synthetic alias
  so config/UI rows for the same HF id don't shadow it. Per-request
  `hf_token` is threaded into the worker env without polluting
  `os.environ`. The in-memory `_downloads` dict is retired.
- `vllm-ctl` adds `install`, `install-cancel`, `install-retry`,
  `install-status`, and `cache-delete` commands; `download` and
  `download-status` continue to work through the catalog-backed shim.
- Archived plan: [plans/phase_4.md](plans/phase_4.md).

**Phase 5**

- New `hf_search.py` wraps `HfApi.list_models` with a `filter="transformers"`
  + `pipeline_tag` pre-filter, fetches each candidate's `config.json`,
  and decides vLLM compatibility against the loaded architecture set.
  Per-row failures stay row-level (gated/missing/error reasons surface in
  `compat_reason`), endpoint-level auth/timeout failures map to 502/504.
- Architecture set sourced primarily by introspecting
  `vllm.model_executor.models.registry.ModelRegistry.get_supported_archs()`
  during `manager_lifespan` startup; falls back to the bundled
  `vllm_supported_architectures.json` snapshot when the import path moves
  on a vLLM bump, and to an empty set as a last resort (search still
  returns rows, all flagged `vllm registry unavailable`).
- `scripts/refresh_arch_list.py` regenerates the bundled snapshot from a
  live vLLM install (`docker exec vllm-manager python scripts/refresh_arch_list.py`);
  exits non-zero if vLLM cannot be imported or the registry API has shifted.
- New admin route `GET /manager/hf/search?q=...&limit=...&filter_compat=...&include_vision=...`
  returns the pinned envelope `{query, limit, include_vision,
  vllm_arch_source, vllm_arch_count, results}`. Each result row carries
  `model_id, architectures, is_compatible, compat_reason, size_estimate_gb,
  downloads, likes, last_modified, tags, pipeline_tag`. `include_vision`
  defaults `false` per PRD §5.9 but exposes a flag the UI can flip on for
  vision-LLM searches (Qwen-VL, Llava, etc.).
- Bounded daemon search workers cap `huggingface_hub` thread pile-up without
  blocking process exit; outer `asyncio.wait_for(timeout=30)` raises 504 on
  the response side. Lifespan teardown cancels queued search jobs.
- Config lookups are cached by `(repo_id, sha_or_last_modified)` with a
  10-minute TTL for unversioned rows, and `hf_hub_download` is pinned to the
  Hub sha when available.
- The Dockerfile sets `HF_HUB_ETAG_TIMEOUT` and `HF_HUB_DOWNLOAD_TIMEOUT`
  defaults that cover the per-row `hf_hub_download` HTTP path; `model_info`
  gets an explicit 15s timeout.
- Size estimate reuses the proven siblings approach from
  `download_worker._safetensor_total`; failures yield
  `size_estimate_gb: null` without flipping `is_compatible`.
- `/manager/status` gains `vllm_arch_count` and `vllm_arch_source` so
  operators can see when the bundled fallback is active.
- Archived plan: [plans/phase_5.md](plans/phase_5.md).

**Phase 6**

- New React/Vite/TypeScript/Tailwind admin UI under `ui/`, with TanStack
  Query data hooks, react-router routes, lucide icon buttons, and dense
  operational views for Dashboard, Catalog, HuggingFace Search, and Downloads.
- Dockerfile now builds the UI in a Node 22 stage and copies `ui/dist` into
  `/app/static` in the CUDA runtime image. `.dockerignore` keeps local
  `node_modules`, build output, caches, and git metadata out of the build
  context.
- `vllm_manager.py` registers a dedicated admin-only `ui_router` before app
  construction. `GET /` redirects to `/ui/`; `/ui` and `/ui/` serve
  `index.html`; `/ui/{full_path:path}` serves contained assets or falls back
  to the SPA for internal routes. Static root resolution happens per request
  from `MNEMOSYNE_UI_DIR` or `/app/static`, and traversal attempts return 404.
- New read-only `GET /manager/gpu` endpoint parses `nvidia-smi` output into
  `{available, gpus}` for live dashboard telemetry. Missing/failing
  `nvidia-smi` returns `available:false` instead of erroring, so macOS and
  no-GPU dev hosts remain usable.
- Dashboard shows live GPU metrics when available, GPU plan/utilization cap
  from `/manager/status`, and the resident alias's persisted catalog request
  count without presenting it as a recent traffic metric.
- Catalog UI derives cache-only rows from reserved alias prefixes
  (`__cache__:` / `__cache__/`) and mirrors backend action rules: cache-only
  rows hide Load and offer Create alias; config rows delete cache by HF ID;
  non-cache UI installs use alias-scoped cache deletion; removable UI/synthetic
  rows use `DELETE /manager/install/{alias}`.
- Search keeps incompatible HF rows visible with `compat_reason`, disables
  Install for them, and carries `size_estimate_gb` into `POST /manager/install`
  when present.
- Downloads view polls `/manager/downloads` plus selected
  `/manager/install/{alias}` detail and exposes cancel/retry/clear actions.
- Archived plan: [plans/phase_6.md](plans/phase_6.md).

**Phase 7**

- Added top-level [README](../README.md) covering quickstart, config reload,
  multi-drive storage, gated HF tokens, partial-download recovery,
  architecture-list refresh, LAN exposure, common CLI operations, and
  troubleshooting.
- `vllm-ctl` help/env handling now reflects the two-plane world:
  `VLLM_ADMIN_URL` defaults to `:8001`, legacy `VLLM_MANAGER_URL` is an admin
  fallback, and `VLLM_INFERENCE_URL` steers `/v1/*` chat requests.
- `vllm-ctl` is executable in git (`100755`) so README quickstart commands and
  PATH/symlink usage work without `bash vllm-ctl`.
- README raw admin `curl` examples load `ADMIN_PASSWORD` from
  `~/vllm-manager/.env`, preserving `.env` as the container secret source while
  making copy-paste API examples authenticate correctly.
- README terminology now matches the shipped UI navigation (`Search`, not
  `Discover`).
- Archived plan: [plans/phase_7.md](plans/phase_7.md).

**Phase 8**

- New `logsetup.py` installs a JSON formatter on the root logger; controlled
  by `MNEMOSYNE_LOG_FORMAT={json|text}` (default JSON). One-line JSON objects
  per record carry `ts`/`level`/`logger`/`msg`, fold `extra=` fields, and
  render `exc_info` tracebacks. No call sites changed.
- vLLM startup-failure error text now reports the inner subprocess exit code
  and points operators at container logs for vLLM stderr.
- Download worker tags HF errors as `auth` / `not_found`; the manager rewrites
  the catalog message to `"set HUGGING_FACE_HUB_TOKEN in /config/.env and
  restart"` (preserving the raw cause) for gated/private repos.
- `catalog.open_catalog` now runs `PRAGMA quick_check` + a passive WAL
  checkpoint at open time. Corrupt DBs are quarantined to `*.corrupt-<ts>`
  and a fresh DB is opened at the original path; startup `apply_config` +
  reconcile then repopulate config rows and recover storage state.
- New tests: multimodal proxy passthrough, JSON log formatter shape + text
  fallback, SQLite corruption quarantine.
- Docs: README "Known v1 limitations" section, smoke checks Section 8
  (vision-model multimodal smoke), and a new
  [phase_8_acceptance.md](phase_8_acceptance.md) acceptance log mapping each
  PRD §7 criterion to its test reference and smoke section.
- Workstation acceptance pass remains pending (CUDA host required); Phase 8
  flips ✅ once `phase_8_acceptance.md` is filled in on the workstation.
- Archived plan: [plans/phase_8.md](plans/phase_8.md).

**Phase 9**

- Second supervised inference backend: `llama-server` is now baked into
  the Docker image (CUDA build from a pinned llama.cpp tag) and runs as
  the resident subprocess for GGUF-only repos. The same `vllm_process`
  global, `_swap_lock`, `ensure_loaded`, eviction loop, and `_proxy` are
  shared with vLLM — only one model is resident at a time, so both
  backends bind `127.0.0.1:8002` sequentially.
- New pure module `repo_probe.py` (stdlib only) holds the GGUF grouping
  rules and the `probe_repo_format` decision: `has_transformer_weights`
  wins → vLLM (mixed-format included); GGUF-only → llama.cpp; neither →
  rejected at install time. Imported by the catalog, `hf_search`, manager,
  and the standalone `download_worker` (the latter only takes the pure
  shard expander to keep its cold-start surface unchanged).
- `runtime.py` gains pure `build_llama_argv` / `build_llama_env` mirroring
  the vLLM builders. `vllm_manager._start_engine(profile)` dispatches to
  `_start_vllm` or `_start_llama_cpp` based on `profile.backend`.
- `ResolvedProfile` now decouples `served_model_name` (forwarded as the
  upstream `"model"` field) from `engine_model_path` (the engine's
  `--model` / `-m` argument). For llama.cpp rows the served name is the
  user-facing alias and the engine is launched with `--alias <alias>`,
  so the GGUF path stays inside the engine and the proxy keeps a stable
  short name on the wire. A `model` back-compat property preserves
  existing callers.
- `config.ModelProfile`, `catalog.CatalogRow`, and the install request all
  carry `backend` + `gguf_filename`. Catalog migration is additive
  (`ALTER TABLE … ADD COLUMN`). Reconcile reads both columns and uses a new
  backend-aware `_has_expected_weights` so a llama.cpp row only goes
  `installed` when *its* specific GGUF (and all canonical
  `*-NNNNN-of-NNNNN.gguf` shards) are present — even when other quants in
  the same shared snapshot exist.
- New admin route `GET /manager/hf/files?model_id=…&revision=…` returns
  `{has_gguf, has_transformer_weights, recommended_backend,
  gguf_candidates: [{label, primary_filename, all_filenames, shard_count,
  size_bytes}, …]}` for the install-form dropdown. `hf_search.py` now
  consolidates `model_info(files_metadata=True)` into a single cached
  fetch shared by the size estimate, the GGUF probe, and the new files
  endpoint, so search-row enrichment doesn't pay extra round-trips.
- `_decide_compat` short-circuits to `is_compatible=true` when the repo
  has GGUF siblings (covers GGUF-only and mixed-format repos with
  unsupported architectures), so `filter_compat=true` no longer hides
  installable llama.cpp models. Search rows now carry `has_gguf` and
  `recommended_backend`.
- Install endpoint validates backend + gguf_filename consistency:
  llama.cpp without `gguf_filename` → 400; vLLM with `gguf_filename` →
  400; explicit backend on a no-weight repo → 400 (`"no supported weight
  files"`). When neither is supplied the install defaults to vLLM and
  skips the Hub probe (preserves legacy clients and offline tests).
  Retry trusts the row and skips the probe.
- `download_worker.py` accepts `gguf_primary_filename` and switches to a
  selected-only download: shards are expanded from the canonical filename
  pattern, passed to `snapshot_download` as `allow_patterns`, and
  `total_bytes` is summed across only the chosen shard set so progress
  bars and free-space estimates stay honest on multi-quant repos.
- `/manager/status` surfaces `backend`, `gguf_filename`, and `engine_pid`
  alongside the existing keys; `vllm_pid` stays as a deprecated alias for
  one release. `vllm-ctl status` prints the new fields. `vllm-ctl install`
  learns `--backend`, `--gguf-filename`, and a `--list-gguf` mode that
  prints the candidates a user can pick from before submitting.
- UI wiring: backend selector + required GGUF dropdown in `InstallForm`,
  per-row backend badge in `Catalog` and `Search`, backend + filename on
  the Dashboard's resident card. New `useHfFiles` query is cached.
- 17 new pure tests in `tests/test_repo_probe.py`. Existing suites
  extended with engine-dispatch tests, llama.cpp argv/env coverage,
  GGUF-vs-vLLM compat tests, install-validation rejections, and
  reconcile assertions for sharded / mixed-quant repos.
- Plan: [plans/llamacpp_plan.md](plans/llamacpp_plan.md). Workstation
  smoke (build + install + load + swap a real GGUF repo) remains pending.

## Verification

Latest host verification on macOS, no CUDA required:

- `python -m pytest -q` → `318 passed`.
- `cd ui && ./node_modules/.bin/tsc --noEmit` → clean.
- `cd ui && ./node_modules/.bin/vite build` → Vite production build succeeded.
- `python -m py_compile vllm_manager.py runtime.py config.py catalog.py profiles.py downloader.py download_worker.py hf_search.py logsetup.py repo_probe.py scripts/refresh_arch_list.py`
- `bash -n vllm-ctl`
- `./vllm-ctl help`
- `git ls-files -s vllm-ctl` → `100755`
- `python scripts/refresh_arch_list.py --help` (does not require vLLM import)
- `git diff --check`

Workstation/GPU smoke validation is still outstanding:

- Rebuild/start the container.
- Install a small model end-to-end (`vllm-ctl install qwen-coder-1_5b ...`)
  and watch progress through `install-status`.
- Install to a non-default storage location and confirm the cache lands on
  the right drive.
- Cancel a long install mid-flight, then `install-retry` (default and
  `--force`) to confirm resumable / wipe semantics.
- Restart the container during a download and confirm `partial` →
  `install-retry` recovery.
- Cache-delete (`--alias` cache-only, `--alias --remove-row`, and legacy
  by-HF-id) with the existing safety gates.
- Plane-separation regression: `POST /manager/install` is 404 on `:8000`,
  reachable on `:8001` behind Basic auth.
- Confirm `HUGGING_FACE_HUB_TOKEN` from the legacy `/manager/download` body
  does not appear in `docker exec vllm-manager env`.
- Phase 6 container UI smoke: authenticated admin `/ui/` returns 200,
  unauthenticated admin `/ui/` returns 401, inference `/ui/` returns 404, and
  refreshing `/ui/catalog` serves the SPA.
- Phase 7 CUDA quickstart smoke: copy examples into a clean compose dir,
  set `ADMIN_PASSWORD`, build/start on the workstation, confirm `/health`,
  authenticated `/manager/status`, and authenticated `/ui/`.
- Phase 9 GGUF / llama.cpp smoke: rebuild image and confirm
  `docker run --rm <img> which llama-server`. Search a known GGUF repo
  (e.g. `bartowski/Qwen2.5-7B-Instruct-GGUF`) and verify
  `recommended_backend == "llama.cpp"` and `has_gguf == true`. Install via
  the UI dropdown, confirm `/manager/status` shows `backend: llama.cpp`
  with the chosen `gguf_filename`, run a `/v1/chat/completions` call, and
  inspect logs for `Launching llama-server`. Repeat with a sharded quant
  (Q8_0 split into 3 files) to confirm only the shard set downloads and
  reconcile flips to `partial` when a shard is manually deleted. Swap from
  a vLLM model to a llama.cpp model and back to confirm clean teardown
  and no port collision on `127.0.0.1:8002`.

## Open follow-ups

- **External `docker-compose.yml`.** Lives outside the repo at
  `~/vllm-manager/`. Phase 4 expects each `storage.locations[].path` from
  `config.yaml` to be bind-mounted; the example file gets a multi-drive
  comment block update. Phase 7 documents the final canonical layout.
- **Phase 5 bundled architecture snapshot.** The committed
  `vllm_supported_architectures.json` is a temporary fallback. This host
  cannot import vLLM, so after the next workstation rebuild run
  `docker exec vllm-manager python scripts/refresh_arch_list.py` once and
  commit the regenerated file so the fallback exactly matches the pinned
  vLLM release.
- **vLLM pin staleness.** Refresh the pinned release deliberately after
  checking upstream release notes. After bumping vLLM, rerun
  `scripts/refresh_arch_list.py` to keep the bundled fallback aligned.
- **Free-space pre-check absent on manual installs.** `vllm-ctl install`
  warns when `--size-gb` is not supplied; the Phase 6 UI sets it from search
  results when available, so the warning should primarily appear on
  hand-crafted curl/CLI calls or search rows without a size estimate.
- **llama.cpp tag pin.** The Dockerfile pins `LLAMA_CPP_TAG=b6500`. Refresh
  deliberately after checking llama.cpp release notes (CLI flags can shift
  on minor bumps). The argv builders cover the documented stable flags;
  rare additions land via `extra_args` in the catalog row.
- **Phase 9 workstation smoke.** Phase 9 flips ✅ once the GGUF / llama.cpp
  smoke (above) is run on a CUDA host and a real install + chat round-trip
  is logged.

## Quick links

- [PRD](PRD.md) — product requirements and decision log.
- [Implementation plan](implementation_plan.md) — phase breakdown and milestones.
- [Phase 0 plan](plans/phase_0.md)
- [Phase 1 plan](plans/phase_1.md)
- [Phase 2 plan](plans/phase_2.md)
- [Phase 3 plan](plans/phase_3.md)
- [Phase 4 plan](plans/phase_4.md)
- [Phase 5 plan](plans/phase_5.md)
- [Phase 6 plan](plans/phase_6.md)
- [Phase 7 plan](plans/phase_7.md)
- [Phase 8 plan](plans/phase_8.md)
- [Phase 8 acceptance log](phase_8_acceptance.md)
- [Phase 9 plan (llama.cpp backend)](plans/llamacpp_plan.md)
- [Smoke checks](smoke_checks.md)
