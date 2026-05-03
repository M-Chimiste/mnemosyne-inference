# PRD — Mnemosyne Inference v1

**Status:** Draft for review
**Owner:** Christian
**Last updated:** 2026-04-27

## 1. Background

[vllm_manager.py](../vllm_manager.py) was bootstrapped in a single Claude session as a quick, opinionated wrapper around vLLM. It works, but the UX is "hacky": models are loaded by hand-crafted JSON payloads with `extra_args` arrays, GPU usage is global (`tp` size only — no GPU pinning), there is no persistent model registry, no idle eviction, and configuration only happens via env vars baked into a `docker-compose.yml` that lives outside the repo.

We want to evolve this into a **config-driven model server for a single workstation** — Ollama/LMStudio ergonomics, vLLM's performance ceiling — with a thin admin web UI for discovery, install, and lifecycle ops. The OpenAI-compatible serving path stays headless; the UI is for human admin tasks only.

## 2. Goals

1. **Config-driven model registry.** Declare models, quantizations, and GPU plans in a file once; reference them by short name forever.
2. **GPU topology control.** Per-model choice of `all GPUs` (TP across both RTX 6000 Pro Blackwells) or `single GPU` (specify which), without the user knowing about `CUDA_VISIBLE_DEVICES`.
3. **Polished lazy loading.** First request triggers load; subsequent requests hit warm. Models evict on configurable idle timeout to free VRAM.
4. **First-class quantization support.** Any quantization vLLM supports (AWQ, GPTQ, FP8, BnB, …) is a profile field, not a hand-rolled `--quantization` flag.
5. **Multimodal passthrough.** Vision/audio models work via the OpenAI-compatible API exactly as vLLM exposes them — no special handling in the wrapper.
6. **Thin wrapper, no vLLM fork.** Every load is still a `vllm.entrypoints.openai.api_server` subprocess. New vLLM features should "just work" by adding flags to a profile, not by writing code.
7. **Admin UI with HF discovery.** Browser-based control plane: search HuggingFace for vLLM-compatible models, download, register as an alias, load/unload, see status — all without editing YAML or hitting `curl`. Backed by a persistent local catalog (§5.11) so model metadata, install history, and download state survive restarts.
8. **Inference / admin plane separation.** Inference clients (IDE agents, LAN devices hitting OpenAI-compatible endpoints) **cannot** reach download, install, load, unload, cache-delete, or alias-mutation operations. Those are reserved for the admin plane (UI + admin port). Enforced at the network layer (separate listener), not just by auth.
9. **Multi-drive storage.** Owner has several large-capacity drives. Each model can be installed to a named storage location (drive); the wrapper handles the bookkeeping so vLLM loads from the right path.

## 3. Non-goals

- **No vLLM modifications.** Treat vLLM as an opaque, upgradable dependency.
- **No chat playground in the UI.** v1 admin UI is operational only — no inference-from-the-browser. (Stretch — see §8.)
- **No on-the-fly quantization.** Use pre-quantized HF repos.
- **No multi-tenant auth/RBAC.** LAN-only, single-user assumption stays. Admin plane requires a password (set in env file); inference plane is LAN-open by default.
- **No model fine-tuning, training, or evaluation tooling.**

## 4. Users and primary scenarios

Single user (workstation owner) interacting via:
- Local IDE/agent clients pointed at the OpenAI-compatible endpoint.
- Other LAN devices (laptop, phone) hitting the same endpoint.
- Occasional terminal use via [vllm-ctl](../vllm-ctl) for admin tasks.

### Scenarios

- **S1 — Set up once, forget.** User edits `config.yaml`, lists their favorite models with quantization + GPU plan, starts the container. Everything is then addressable by short alias.
- **S2 — Single-GPU workload.** User wants a 7B coder model on GPU 0 only so a 72B AWQ model can stay resident on GPU 1. (Stretch — see §8.)
- **S3 — Auto-swap on the way in.** Agent client sends a chat completion with `"model": "qwen-72b-awq"`. If the resident model is `llama-3-vision`, the wrapper unloads it, loads `qwen-72b-awq`, and serves the request.
- **S4 — Idle eviction.** No traffic for 30 min → model unloads → VRAM is free for other GPU work without restarting the container.
- **S5 — Multimodal.** User sends an OpenAI-format chat completion with `image_url` content blocks to a vision model alias. The wrapper proxies untouched.
- **S6 — Discover and install via UI.** User opens the admin UI, types "qwen vision" into the search box, sees a filtered list of vLLM-compatible HF repos (architecture-validated), clicks one, picks a GPU plan and quantization in a small form, hits "Install". The wrapper downloads the repo in the background, persists a new alias entry to `config.yaml`, and the model becomes routable on the next `/v1/*` request.
- **S7 — Lifecycle from UI.** From the same UI, the user can see what's resident, manually load/unload, watch download progress, and delete cached models that are no longer wanted (recovers disk).

## 5. Functional requirements

### 5.1 Configuration file

A single declarative file (YAML proposed; see §10 Q1) at a known path inside the container (`/config/config.yaml`), bind-mounted from the host. Secrets do **not** live in this file — see §5.13 for the `.env` mechanism.

```yaml
server:
  inference_port: 8000
  admin_port: 8001
  inference_bind: 0.0.0.0
  admin_bind: 0.0.0.0
  idle_unload_seconds: 900      # 15 min default; null = never auto-unload
  startup_timeout_seconds: 600
  swap_queue_timeout_seconds: 300   # how long a /v1 request waits during a model swap

storage:
  default: nvme-fast
  locations:
    - name: nvme-fast
      path: /storage/nvme/hf-cache
    - name: archive
      path: /storage/raid/hf-cache

defaults:
  gpu_memory_utilization: 0.90
  trust_remote_code: true
  max_model_len: null            # null → vLLM derives from model config

models:
  - alias: qwen-72b-awq
    model: Qwen/Qwen2.5-72B-Instruct-AWQ
    quantization: awq
    gpus: all                    # or [0] or [1] or [0,1]
    max_model_len: 32768
    storage: nvme-fast

  - alias: qwen-coder-7b
    model: Qwen/Qwen2.5-Coder-7B-Instruct
    gpus: [0]

  - alias: llama-vision
    model: meta-llama/Llama-3.2-11B-Vision-Instruct
    gpus: [1]
    storage: archive
    extra_args:
      - --limit-mm-per-prompt
      - image=4
```

**Required behavior:**
- Aliases in `config.yaml` populate the in-memory registry merged with DB-stored UI-installed models (§5.11). Config-defined aliases take precedence on conflict.
- `extra_args` is appended to the vLLM command verbatim (escape hatch for vLLM features the schema doesn't model yet — preserves "thin wrapper" goal).
- File reload: SIGHUP or `POST /manager/reload` rereads the file without restarting the container. New aliases become available immediately; resident model is left alone.
- **Schema notes:**
  - `dtype` is **not** a profile field — vLLM auto-picks per model and `dtype` doesn't affect quant level. Use `extra_args: [--dtype, bfloat16]` if a manual override is ever needed.
  - `modality` is **not** a profile field — vLLM detects multimodal capability from the model itself.
  - `max_model_len` is first-class with a global default in `defaults:` and per-model override.
  - `storage` selects which entry from `storage.locations` this model lives in. If omitted, falls back to `storage.default`.

### 5.2 GPU topology control

- `gpus: all` → no `CUDA_VISIBLE_DEVICES` override; `--tensor-parallel-size = <count of visible GPUs>`.
- `gpus: [0]` → subprocess env has `CUDA_VISIBLE_DEVICES=0`; `--tensor-parallel-size = 1`.
- `gpus: [0,1]` → equivalent to `all` on a 2-GPU box, but explicit.
- The wrapper computes the subprocess env; users never set `CUDA_VISIBLE_DEVICES` themselves.
- Validation at config-load time: GPU indices must exist (probe via `nvidia-smi -L` at startup).

### 5.3 Lazy loading + idle eviction

- Current auto-swap on `/v1/*` is kept and becomes the **only** load path during normal use. `/manager/load` stays as a manual override.
- On every successful proxied request, update a `last_used_at` timestamp.
- Background asyncio task wakes every N seconds; if `now - last_used_at > idle_unload_seconds`, call `_kill_vllm`.
- Eviction is logged. `/manager/status` exposes `last_used_at` + `seconds_until_eviction`.

**Concurrency: queue, don't 409 (LMStudio-style).** When a `/v1/*` request arrives and a swap is in progress (or a different model is requested):
- The request is held until the target model becomes resident, then served.
- If the target model isn't the one currently loading, the in-progress load is allowed to finish first; the second request joins a queue keyed on its target model. The eviction-loader-serve cycle proceeds in arrival order.
- Hard timeout `swap_queue_timeout_seconds` (default 300s, configurable). On timeout the client gets `504 Gateway Timeout` so streaming clients aren't held indefinitely.
- The vLLM crash case fails open: if the subprocess dies during a load, the wrapper does **not** auto-restart. Pending queued requests get `503 Service Unavailable` with a clear error; the next request will retrigger a fresh load. Auto-restart loops can mask real bugs.

### 5.4 Quantization

Treated as a first-class config field, not free-form. Wrapper passes whatever is set as `--quantization <value>` to vLLM. The list of accepted values is **not** hardcoded — anything vLLM accepts works. (This keeps us forward-compatible.)

### 5.5 Multimodal

The proxy already forwards request bodies untouched. Confirm — and document — that:
- Image/audio content blocks in OpenAI-format requests round-trip correctly.
- The `extra_args` escape hatch covers vLLM's multimodal flags (`--limit-mm-per-prompt`, `--mm-processor-kwargs`, etc.).

No new wrapper code required, but we need an integration test against a small vision model to prove it.

### 5.6 CLI parity

Update [vllm-ctl](../vllm-ctl) so:
- `vllm-ctl load <alias>` works (no need to type the full HF ID or remember tp/gpu_mem).
- `vllm-ctl list` shows configured aliases (separate from `vllm-ctl models` which lists cached files on disk).
- `vllm-ctl reload` triggers config reload.

### 5.7 Observability

- `/manager/status` extended with: `last_used_at`, `idle_seconds`, GPU plan, quantization, profile alias.
- Structured JSON logs (we already log to stdout — formalize the format).
- Prometheus `/metrics` is a stretch goal (see §8).

### 5.8 Admin UI

A React single-page app served from the **admin port** at `/ui` (FastAPI redirects `/` → `/ui`). It is a thin client over the `/manager/*` API plus the new endpoints in §5.9. The UI is **not** reachable on the inference port — see §5.10.

**Stack:**
- **React + Vite + TypeScript.** Vite for the dev server and production build; TypeScript because it's the modern default and keeps the API surface honest.
- **Styling:** Tailwind CSS (no custom design system needed for an internal admin tool).
- **Data fetching:** TanStack Query (a.k.a. React Query) for polling-based live state — handles caching, retry, and background refresh out of the box.
- **State:** local `useState` / Query cache only. No Redux/Zustand.
- **Routing:** `react-router` for the four to five views below.
- No SSR, no Next.js. Plain Vite SPA.

**Hosting model:**
- The UI lives in `ui/` at the repo root with its own `package.json`.
- The Dockerfile uses a **multi-stage build**: a Node 22 stage runs `npm ci && npm run build`, then the final CUDA stage copies `ui/dist/` into `/app/static/`.
- The admin FastAPI app mounts `/app/static/` via `StaticFiles` at `/ui` and serves `index.html` as the SPA fallback for client-side routing.
- The UI is reachable on the **admin port only** at `http://<workstation-ip>:<admin_port>/ui` (default `:8001`). See §5.10.
- Dev workflow: `cd ui && npm run dev` runs Vite at `:5173` with a proxy to the admin port at `:8001`. The container is the production target only.

**Views (v1):**

1. **Dashboard** — resident model, GPU usage, idle countdown, recent requests counter, button to unload.
2. **Catalog** — single unified view backed by the persistent catalog DB (§5.11). Every model the system knows about: config-defined aliases, UI-installed aliases, and on-disk cache entries that aren't yet aliased. Per row: alias, HF model ID, quantization, GPU plan, storage location, on-disk size, install date, last used, status (resident / cached / not downloaded / partial). Per-row actions: load, evict, retry-download, cancel-download, delete-from-disk, remove-from-catalog.
3. **Search & Install** — text box → HF Hub search → filtered list (§5.9). Each result row: model card link, size estimate, quick-install form (alias, quantization, GPU plan, storage location, optional max_model_len). Submitting kicks off a background download (cancellable) and writes a catalog entry.
4. **Downloads** — live status of in-flight and recent downloads (from the DB — survives restarts). Progress, throughput, error messages, cancel and retry buttons.

**Behavior requirements:**
- Initial paint is server-rendered HTML index; data via TanStack Query polling (every 2-5s). No WebSockets in v1.
- All UI actions resolve to existing or §5.9 endpoints; the UI ships no business logic the API doesn't already expose.
- Login: HTTP basic auth gate on the entire admin app, password from `.env` (§5.13). If the password is unset, the admin port refuses to bind to a non-loopback address — see §5.10.

### 5.9 HuggingFace search + vLLM-compatibility filter

> **Note:** HF Hub does **not** expose a first-class vLLM-compatibility flag. The `library_name` enum has no `vllm` value, and known-good vLLM models (e.g. Qwen2.5-7B-Instruct) carry no `vllm` tag in their API response. vLLM's own docs confirm it determines compatibility by reading `config.json#architectures` against its internal registry. So we have to do the cross-reference ourselves.

New endpoint: `GET /manager/hf/search?q=<query>&limit=<n>&filter_compat=true`.

Implementation:
- Call `HfApi.list_models(search=q, filter=ModelFilter(library="transformers", task="text-generation"), limit=n)` to pre-filter server-side. This eliminates obvious non-candidates (Diffusers, sentence-transformers, audio models without text-gen, etc.) without a per-model fetch.
- For each remaining candidate, fetch `config.json` (small file, cached locally with TTL) and read the `architectures` field.
- A model is "vLLM-compatible" if every entry in `architectures` is present in our supported set.
- Response includes per-result: `model_id`, `architectures`, `is_compatible`, `compat_reason`, `size_estimate_gb` (sum of safetensors siblings), `downloads`, `likes`, `last_modified`, `tags`, `pipeline_tag`.
- Incompatible results are returned but flagged — the UI dims them with a tooltip explaining the reason (so users see what they searched for, not a silent empty list).

**Supported architectures set — hybrid sourcing:**
1. **Primary: runtime introspection.** At startup, import vLLM and read its model registry directly (e.g. `from vllm.model_executor.models.registry import ModelRegistry`). Use whatever method/attribute it exposes for "all registered architectures". Wrap in `try/except` — vLLM doesn't promise this is a stable API.
2. **Fallback: bundled JSON** (`vllm_supported_architectures.json`) committed to the repo. Used if the introspection import path moves on a vLLM bump. Logged loudly so the gap gets noticed.
3. **Refresh helper:** `scripts/refresh_arch_list.py` re-exports the introspected set into the bundled JSON. Run during the vLLM upgrade workflow.

This way: as long as vLLM's internals stay stable, the supported list is always exactly correct; if they shift, search degrades gracefully rather than failing.

**Install endpoint:** `POST /manager/install`. Body:
```json
{
  "alias": "qwen-vision",
  "model": "Qwen/Qwen2.5-VL-7B-Instruct",
  "quantization": null,
  "gpus": [1],
  "max_model_len": 16384,
  "storage": "nvme-fast",
  "extra_args": []
}
```

Effects in order:
1. **Pre-flight checks.** Resolve the storage location → check free space at that path > `size_estimate_gb * 1.1`. Fail fast with a clear error if not (refuses; user picks a different drive or frees space). Resolve the HF token from env (§5.13).
2. **Catalog row.** Insert/update the row in the SQLite catalog (§5.11) with status=`queued`. The UI starts showing it immediately.
3. **Download.** Spawn a download **subprocess** (not a thread — so it's killable). Subprocess wraps `huggingface_hub.snapshot_download` with `cache_dir` set to the storage location's path. Progress events are written back via a small IPC channel (line-delimited JSON on a pipe, or a watched file).
4. **Cancel.** `POST /manager/install/{alias}/cancel` SIGTERMs the subprocess. HF leaves `.incomplete` files in the cache, which `snapshot_download` can resume on retry.
5. **Resume / retry partial.** `POST /manager/install/{alias}/retry` re-spawns the download against the same `cache_dir`. HF's resumable downloads pick up from where they left off; if the cache state is corrupted, the retry endpoint accepts a `?force=true` param that wipes the cache directory first.
6. **Completion.** Mark catalog row `installed`, record `installed_at`, `size_bytes`, `cache_path`. Alias is now usable on the inference plane.
7. **Failure.** Mark catalog row `error` with the error message. UI shows it for the user to retry or remove.

The legacy `POST /manager/download` endpoint is kept as a thin shim that calls `/manager/install` with no alias (cache-only download); see §10 Q5 for the backwards-compat decision.

**Delete endpoint:** `DELETE /manager/cache/{model_id:path}` removes the on-disk HF cache directory at the catalog-recorded location. Refuses if the model is currently resident.

### 5.10 Inference / admin plane separation

Mutating operations (download, install, manual load/unload, alias mutation, cache delete, config reload) **must not be reachable from the inference port**. Inference users — IDE agents, LAN devices, anything pointed at `/v1/*` — should never be able to install or evict a model.

**Enforcement model: two listeners, one process.**

| Plane | Default port | Bind | Endpoints | Auth |
|---|---|---|---|---|
| **Inference** | 8000 | `0.0.0.0` | `/v1/*`, `/health` | none by default; optional bearer key in `.env` |
| **Admin** | 8001 | `0.0.0.0` (only if `ADMIN_PASSWORD` set, else falls back to `127.0.0.1`) | `/manager/*`, `/ui/*`, `/v1/*` (chat playground stretch) | HTTP Basic — username `admin`, password from `.env` |

- Both listeners are served by the same Python process and share the same global state and the same SQLite catalog (§5.11) — no IPC layer.
- Implementation: two `FastAPI` apps run under one `asyncio` loop via `asyncio.gather(uvicorn.Server(inference_cfg).serve(), uvicorn.Server(admin_cfg).serve())`.
- Auto-swap on `/v1/*` still works on the inference plane, because that's part of normal request flow (the user is asking for a model by name); it's not "admin" the way `POST /manager/load` is.
- The admin plane is a **superset** of the inference plane: anything you can do on `:8000` you can also do on `:8001`. This keeps the chat-playground stretch goal workable from the UI without proxying through the inference port.
- **Fail-safe bind:** if `ADMIN_PASSWORD` is not set in `.env`, the admin port refuses to bind to anything other than `127.0.0.1` and logs a warning. Prevents accidental open-LAN admin exposure when secrets aren't configured.
- Container exposes both ports; compose file maps both. Owner is free to firewall the admin port at the host/router level for additional defense.

**What this means for `vllm-ctl`:** all existing `vllm-ctl` commands now talk to the admin port. The CLI reads `ADMIN_PASSWORD` from the same `.env` file (or the user's environment) and sends HTTP Basic credentials.

### 5.11 Persistence layer

The wrapper needs durable, queryable state for: download progress (so it survives restarts), the model catalog (alias → HF ID → storage location → install timestamps → file sizes), and request usage history (per-model `last_used_at`, request counts).

**Choice: SQLite.** Stored at `/state/mnemosyne.db` (volume-mounted from the host). Reasons:
- Single process, single writer — no concurrency contention.
- Schema is well-known and stable (catalog rows have a fixed shape).
- Stdlib (`sqlite3`), no new runtime dep.
- Atomic transactions across catalog + downloads tables matter (avoid orphan downloads).
- Single file → trivial backup / portability across drives.

The user asked for "NoSQL or something". I'm proposing SQLite as the right answer for this scale (<1000 catalog rows, <10 active downloads); if you'd rather a JSON/TinyDB approach, see §10 Q7.

**Schema sketch:**

```sql
CREATE TABLE models (
  alias              TEXT PRIMARY KEY,
  hf_model_id        TEXT NOT NULL,
  source             TEXT NOT NULL,    -- 'config' | 'ui_install'
  quantization       TEXT,
  gpus               TEXT NOT NULL,    -- JSON: "all" or [0,1]
  max_model_len      INTEGER,
  storage_location   TEXT NOT NULL,    -- references storage.locations[].name
  cache_path         TEXT,             -- absolute path on disk once installed
  size_bytes         INTEGER,
  status             TEXT NOT NULL,    -- 'queued' | 'downloading' | 'installed' | 'partial' | 'error'
  installed_at       INTEGER,
  last_used_at       INTEGER,
  request_count      INTEGER DEFAULT 0,
  extra_args         TEXT              -- JSON array
);

CREATE TABLE downloads (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  alias              TEXT NOT NULL REFERENCES models(alias) ON DELETE CASCADE,
  pid                INTEGER,          -- subprocess PID while active
  status             TEXT NOT NULL,    -- 'queued' | 'downloading' | 'complete' | 'cancelled' | 'error'
  started_at         INTEGER NOT NULL,
  finished_at        INTEGER,
  bytes_downloaded   INTEGER DEFAULT 0,
  total_bytes        INTEGER,
  error              TEXT
);

CREATE INDEX idx_downloads_alias ON downloads(alias);
CREATE INDEX idx_models_last_used ON models(last_used_at);
```

**Sync semantics:**
- On startup: load `config.yaml` aliases → upsert into `models` with `source='config'`. Read existing `source='ui_install'` rows from DB. Reconcile against on-disk storage (mark missing files as `partial`).
- On config reload: re-upsert `config` rows; `ui_install` rows untouched.
- On `/manager/install`: insert a `models` row (`source='ui_install'`, `status='queued'`) + a `downloads` row, both in one transaction.
- On every successful inference request: increment `request_count`, update `last_used_at`. Buffered (write every N seconds) to avoid hot-path DB writes.

### 5.12 Storage locations

Workstation has multiple drives. Each model can live on a different one.

- Locations are declared in `config.yaml` under `storage.locations` (§5.1). Each has a `name` and a `path` inside the container.
- Compose file bind-mounts each host drive to the corresponding container path.
- `storage.default` names the location used when an install doesn't specify.
- Per-install: the UI's install form has a "Storage" dropdown populated from `GET /manager/storage`. Each option shows the location name and current free space.
- Per-load: `_start_vllm` sets `HF_HOME=<location.path>` in the subprocess environment so vLLM finds the model. (Different models on different drives = different `HF_HOME` per launch — this is the only reason the wrapper has to think about storage.)
- Validation at startup: every `path` must exist and be writable. Missing paths log a warning but don't fail boot (you may have unmounted that drive temporarily).
- Locations are **config-only** — not addable from the UI in v1. Adding a drive is a host-level operation that requires a compose-file edit anyway.

### 5.13 Secrets and runtime env (`.env`)

Secrets live in a separate `.env` file at `/config/.env` (bind-mounted from `~/vllm-manager/.env`). The container reads it at startup; values become process env vars. The file is **not** read by FastAPI on each request — changes require a container restart.

| Variable | Required? | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | Recommended | HTTP Basic password for `/ui` and `/manager/*`. If unset, admin port falls back to loopback bind only. |
| `HUGGING_FACE_HUB_TOKEN` | Optional | Used for gated repos. Read by the install subprocess. |
| `INFERENCE_API_KEY` | Optional | If set, `/v1/*` requires `Authorization: Bearer <key>`. If unset, inference port is open on the LAN. |

- The HF token is read from env only — there is no per-install token field in the UI install form. Gated repos require the env var to be set; failing that, install errors with a clear "set HUGGING_FACE_HUB_TOKEN in .env and restart" message.
- The `.env` file is gitignored; we ship a `.env.example` showing all variables.

## 6. Architecture impact

Minimal. Roughly:

| Area | Change |
|---|---|
| `vllm_manager.py` | Split into two FastAPI apps (`inference_app`, `admin_app`); run both via `asyncio.gather` of two uvicorn servers. |
| New `config.py` | Load + validate YAML (`pydantic` schema), watch for reload. No write-back needed (catalog lives in DB, not YAML). |
| New `catalog.py` | SQLite layer: schema migrations, CRUD for `models` and `downloads` tables, sync from `config.yaml`. |
| New `downloader.py` | Manages download subprocesses: spawn, cancel (SIGTERM), retry, progress IPC (line-delimited JSON over a pipe). |
| New `hf_search.py` | Wraps `HfApi.list_models`, filters via bundled `vllm_supported_architectures.json` + runtime introspection, caches `config.json` lookups. |
| `_start_vllm` | Take a `Profile` object. Build subprocess env: `CUDA_VISIBLE_DEVICES` from `gpus` field, `HF_HOME` from the model's storage location. |
| `_proxy` | Queue requests during a swap (`asyncio.Event` per target alias). Update DB-backed `last_used_at`/`request_count` on success (buffered). |
| New | Asyncio idle-eviction task in `lifespan`. |
| `vllm-ctl` | Talks to admin port with HTTP Basic auth. `VLLM_ADMIN_URL` env var (alias for old `VLLM_MANAGER_URL`). |
| Dockerfile | Multi-stage: Node 22 stage builds the React SPA, final CUDA stage copies `ui/dist/` to `/app/static/`. Add `pyyaml`, `pydantic`. Mount points for `/config` (config + .env), `/state` (SQLite), and one per storage location. **Pin vLLM** to a specific release version. |
| New | `ui/` — React + Vite + TypeScript + Tailwind project. Own `package.json`, lockfile. |
| New | `vllm_supported_architectures.json` — checked into the repo, regenerated on vLLM version bumps (see §10 Q3 for refresh strategy). |
| New | `.env.example` documenting all secret variables. |

Python module split: `vllm_manager.py` (HTTP + subprocess + plane wiring), `config.py` (YAML), `catalog.py` (SQLite), `downloader.py` (download subprocesses), `hf_search.py` (discovery). Five Python modules — already past the "keep it in one file" line. The React app structures itself however idiomatic React wants.

## 7. Success criteria

- A new model can be added by editing `config.yaml` and running `vllm-ctl reload` — zero code changes, zero container restarts.
- A user with no knowledge of vLLM internals can pick "single GPU vs both GPUs" from a config field.
- Idle eviction reclaims VRAM within `idle_unload_seconds + 60s` of the last request.
- A vision model serves an `image_url` request end-to-end with no special-case wrapper code.
- Upgrading to a newer vLLM (e.g. for a new quantization format) requires only bumping the Dockerfile pin — no wrapper changes (refreshing `vllm_supported_architectures.json` is the one allowed manual step).
- A user can go from "I want to try Qwen2.5-VL" to a working alias in under 60 seconds of clicking, including download time being correctly displayed.
- A request to `POST /manager/download` (or any other admin operation) on the inference port returns 404 — the route doesn't exist on that plane.
- A download can be cancelled mid-flight from the UI; the cache state recovers cleanly on retry.
- A model installs to a non-default drive when the user picks it from the storage dropdown, and `_start_vllm` finds the weights without manual symlinks.
- After `docker compose down && up`, in-flight downloads are recoverable: the catalog still shows them as `partial`, and the UI offers "Retry" rather than asking the user to start over.

## 8. Stretch / later

- **Multi-model concurrent serving.** Run a 7B on GPU 0 and a 72B-AWQ on GPU 1 simultaneously. Requires multiple `vllm` subprocesses with different `CUDA_VISIBLE_DEVICES` and a routing layer. Significant scope — defer unless it becomes a hard requirement.
- **Chat playground in UI.** A "Try it" panel that hits `/v1/chat/completions` against the resident model, including image upload. Convenient for sanity-checking vision models post-install.
- **Prometheus `/metrics` endpoint** with load count, swap latency, request count by model.
- **Pre-warm on startup.** Optional `preload: true` per profile, useful for the "default" model.
- **Auto-detect compatible quantization variants.** On install, surface "this base model has AWQ/GPTQ/FP8 forks on the hub — want one of those instead?" by querying related repos.
- **vLLM auto-restart.** Crash recovery loop with backoff for the inner subprocess. Currently fail-open (§5.3).

## 9. Resolved decisions

- **Auth model.** Admin plane gated by HTTP Basic, password from `.env`. Inference plane open by default; optional bearer key via `INFERENCE_API_KEY` in `.env`. Admin port refuses non-loopback bind if `ADMIN_PASSWORD` is unset (fail-safe). (2026-04-26)
- **UI tech.** React + Vite + TypeScript + Tailwind + TanStack Query SPA, served from the admin port. Multi-stage Dockerfile builds it. (2026-04-26)
- **Plane separation.** Two listeners in one process via `asyncio.gather`. Inference 8000, admin 8001. Admin is a superset. (2026-04-26)
- **HF compat-filter source.** Hybrid: runtime introspection of vLLM's model registry → fallback to bundled `vllm_supported_architectures.json`. HF Hub has **no** vLLM-compatibility flag (verified). (2026-04-26)
- **Concurrency on swap.** Queue with timeout (LMStudio-style). Default 300s; configurable. 504 on timeout. (2026-04-26)
- **vLLM crash recovery.** Fail open in v1 — no auto-restart. Surface to user as 503; next request retriggers a fresh load. (2026-04-26)
- **Malformed config.** Hard fail on container start. (2026-04-26)
- **Secrets.** `.env` file bind-mounted at `/config/.env`. Holds `ADMIN_PASSWORD`, `HUGGING_FACE_HUB_TOKEN`, optional `INFERENCE_API_KEY`. (2026-04-26)
- **Disk-space pre-check.** Yes — refuse install if free space at the chosen storage location < `size_estimate_gb * 1.1`. (2026-04-26)
- **Multi-drive storage.** First-class. `storage.locations` in config; per-model `storage:` field; `HF_HOME` set per-launch. Locations are config-only, not addable from UI. (2026-04-26)
- **`max_model_len`.** First-class profile field with a global default in `defaults:`. (2026-04-26)
- **`dtype` field.** Dropped from schema. Does not affect quantization level. Override via `extra_args` if ever needed. (2026-04-26)
- **`modality` field.** Dropped from schema. vLLM detects from the model itself; OpenAI-compatible image/audio passthrough works regardless. (2026-04-26)
- **vLLM version pin.** Pin to a specific release tag in the Dockerfile. Upgrades are deliberate. (Updated 2026-05-03)
- **Cancellable downloads.** Yes — implemented via download subprocess + SIGTERM. HF cache state recovers via resumable `snapshot_download`; force-wipe option for corrupt state. (2026-04-26)
- **Persistence.** SQLite at `/state/mnemosyne.db`. Tables: `models`, `downloads`. Survives container restart. (2026-04-26)
- **HF gated-token UX.** Hard-fail at install time with a clear "set HUGGING_FACE_HUB_TOKEN in `.env` and restart" message. No per-install token field in the UI — secrets only in `.env`. (2026-04-26)
- **Config format.** YAML. (2026-04-27)
- **Multi-model concurrent serving.** Out of scope for v1; deferred to v2. (2026-04-27)
- **Default idle timeout.** 15 minutes (`idle_unload_seconds: 900`). User-configurable; `null` allowed for "never evict". (2026-04-27)
- **Config file location.** `~/vllm-manager/config.yaml` on the host → `/config/config.yaml` in the container. Same directory holds `.env`. (2026-04-27)
- **Backwards compatibility.** `POST /manager/load` and `POST /manager/download` kept as shim endpoints that adapt to the new internals. Existing scripts continue to work. (2026-04-27)
- **Cache delete behavior.** Deleting on-disk cache for an aliased model marks the catalog row `partial` rather than removing it. UI shows "not downloaded — retry?" so the user can recover the install with one click. (2026-04-27)

## 10. Open questions

_None — all resolved. See §9._
