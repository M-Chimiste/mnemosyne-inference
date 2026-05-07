# Plan: llama.cpp backend for GGUF models

## Context

The inference manager currently runs vLLM exclusively as its inference subprocess. vLLM does not load GGUF weights well (and often not at all for quantized variants) and cannot serve many community-released quantized checkpoints that are GGUF-only. Today, GGUF repos surface in HF search as "incompatible" (because `config.architectures` doesn't match vLLM's registry, or there is no `config.json` at all), and even if a user installed one, the manager would fail to start vLLM against it.

The change adds **llama.cpp's `llama-server`** as a second supervised subprocess backend, selected automatically when a catalog row points to a GGUF-only repo. Everything else — proxy, swap queue, eviction, downloads, OpenAI `/v1/*` API surface, admin endpoints, UI — keeps working unchanged. Auto-detection happens at install time from the HF repo file listing; HF search is relaxed so any repo with a `.gguf` sibling is marked compatible (compatibility means "at least one backend can serve it" — see HF search section for the full rule).

Per user answers to clarifying questions:
- Detection: **auto from HF file listing** (presence of `.gguf` siblings).
- Quant selection: install-time **dropdown** of GGUF files; **sharded files grouped** under their first shard (`*-00001-of-NNNNN.gguf`).
- Install: **native `llama-server` binary** baked into the Dockerfile (CUDA build).
- Inner port: **reuse `127.0.0.1:8002`** — only one model is resident at a time, so both backends bind the same port sequentially.

## Scope

In scope:
- New backend dispatch keyed by `profile.backend` ∈ {`vllm`, `llama.cpp`}.
- Pure llama-server argv/env builders mirroring [runtime.py](runtime.py).
- New `_start_llama_cpp` / `_wait_for_llama_cpp` mirroring the vLLM lifecycle in [vllm_manager.py](vllm_manager.py).
- Catalog persists `backend` and `gguf_filename` on rows; reconcile validates the **specific** GGUF shard set, not just "any GGUF present".
- Auto-detection at install (HF file listing → recommended backend + filename dropdown), with deterministic single detector contract.
- HF search compat: GGUF-only siblings → compatible, regardless of architecture registry; mixed-format repos still surface vLLM compatibility.
- Selected-file/group download path with correct progress/size accounting.
- Server-side install validation: GGUF backend rows must have a valid `gguf_filename`.
- Install form UI: dropdown for GGUF file choice when backend=llama.cpp; backend badge in catalog rows.
- Status surface: `/manager/status`, dashboard, `vllm-ctl status` all show backend.
- CLI install support: `vllm-ctl install` learns `--backend` and `--gguf-filename` so users can install GGUF without the UI.
- Tests for argv builder, dispatch, compat, sharded-shard grouping, mixed-format detection, reconcile of selected files, install validation.

Out of scope (call out, don't build):
- llama.cpp-specific tuning UI beyond what the existing `extra_args` escape hatch covers.
- Hot-swap between backends without process restart (still kill + relaunch — same as today).
- CPU-only fallback. We build CUDA `llama-server`; CPU is incidental.

## Architecture

The integration treats llama.cpp as a *parallel subprocess backend*, not a rewrite. The key insight from exploration: vLLM is hardcoded only in two narrow places — the argv builder and `_start_vllm`. Everything else (`_runtime`, `_swap_lock`, `ensure_loaded`, `_proxy`, `_wait_for_*`, eviction, usage flushing) is already backend-agnostic so long as the inner port and `/health` endpoint behave the same way. llama-server already exposes OpenAI-compatible `/v1/*` and a `/health` endpoint, so the proxy needs no changes.

Backend selection flows through the existing five-tier resolver in [vllm_manager.py:475-535](vllm_manager.py#L475-L535) (`_resolve_request_model`). It returns a `ResolvedProfile` augmented with backend-aware fields; `_start_engine(profile)` then dispatches to the correct subprocess function. The single-resident invariant is unchanged.

### Decoupling "served name" from "engine target"

The proxy rewrites the request body's `model` field to `profile.model` before forwarding ([vllm_manager.py:1742-1749](vllm_manager.py#L1742-L1749)). For vLLM this works because `--model <id>` doubles as the served name. For llama.cpp the engine wants a filesystem path and that must **not** leak into the upstream request body. Resolution: split the overload.

`ResolvedProfile` (currently has a single `model` field) gets two distinct fields:
- `served_model_name: str` — the canonical name forwarded as `"model": "..."` in upstream requests (and used by `_canonicalize_model_field`). Replaces today's `model` field semantically.
- `engine_model_path: str` — the argument passed to the engine's `--model` / `-m` flag. For vLLM, equals `served_model_name`. For llama.cpp, the absolute path to the chosen GGUF (first shard).

The existing `model` field is **renamed to `served_model_name`**. All current call sites that use `profile.model` for upstream forwarding stay correct after rename. The argv builders consume `engine_model_path` exclusively. `llama-server` is launched with `--alias <served_model_name>` so the upstream `"model": "<alias>"` body matches the engine's served name.

**Deterministic naming rule** (no ambiguity):
- vLLM rows: `served_model_name = engine_model_path = <hf_model_id from row, or absolute path for path-based profiles>`. Preserves today's behavior exactly.
- llama.cpp rows: `served_model_name = alias` (the catalog `alias` field — the user-facing handle, always present and stable). `engine_model_path = <abs path to chosen GGUF>`.

This keeps the upstream `"model"` value short and predictable for llama.cpp rows, and avoids leaking either an HF id (which could collide across quants) or a filesystem path. Document this rule in `profiles.py` next to `resolve_profile`.

### Detector contract (single source of truth)

Replace the implicit "any .gguf → llama.cpp" rule with one explicit, well-typed function. Live in a new dependency-light module **`repo_probe.py`** (no pydantic/yaml/config imports) so it can be safely imported by `catalog.py`, `hf_search.py`, `vllm_manager.py`, **and** the standalone `download_worker.py` subprocess. Two layers so the pure grouper is testable independently of network metadata:

```python
# repo_probe.py — stdlib + dataclasses only.

# Pure: filename-only grouping. Caller supplies any sibling list.
def group_gguf_filenames(filenames: list[str]) -> list[GgufGroup]: ...
@dataclass(frozen=True)
class GgufGroup:
    primary_filename: str       # *-00001-of-NNNNN.gguf, or single file
    all_filenames: list[str]    # full shard set (length 1 for unsharded)
    shard_count: int

# Sibling metadata (rfilename + size). Matches huggingface_hub's siblings shape.
@dataclass(frozen=True)
class SiblingMeta:
    rfilename: str
    size: Optional[int]

# Probe: consumes sibling metadata, produces backend recommendation + sized candidates.
@dataclass(frozen=True)
class GgufCandidate:
    label: str                  # "Q4_K_M (4.4 GB)" or "Q8_0 (sharded, 3 files, 14 GB)"
    primary_filename: str
    all_filenames: list[str]
    shard_count: int
    size_bytes: Optional[int]   # sum across shards; None if any shard size missing

@dataclass(frozen=True)
class RepoFormatProbe:
    has_gguf: bool
    has_transformer_weights: bool   # any .safetensors / .bin sibling
    recommended_backend: str        # "vllm" | "llama.cpp" | "none"
    gguf_candidates: list[GgufCandidate]   # sized + labeled

def probe_repo_format(siblings: list[SiblingMeta]) -> RepoFormatProbe: ...
```

`group_gguf_filenames` is the pure tested-in-isolation grouper; `probe_repo_format` is the metadata-enriched layer that produces the `/manager/hf/files` payload directly. Both auto-detection (install) and HF-search compat-marking call `probe_repo_format` with the same `SiblingMeta` list. The `download_worker` imports only `group_gguf_filenames` for shard expansion, so its import surface stays standalone.

Decision rule (deterministic, no fallback ambiguity):
- `has_transformer_weights` → `recommended_backend = "vllm"` (even if `has_gguf` also true; mixed-format prefers vLLM).
- `has_gguf and not has_transformer_weights` → `recommended_backend = "llama.cpp"`.
- **Neither** → `recommended_backend = "none"`. The install endpoint rejects this with 400 ("no supported weight files in repo"); HF search marks the repo not compatible with reason `"no supported weight files"`.

UI displays the recommended backend; user can override (subject to install validation).

## Files to modify

### Backend dispatch

**[config.py](config.py)** — `ModelProfile`
- Add `backend: str = "vllm"` field.
- Add `gguf_filename: Optional[str] = None` (the **primary** filename — for sharded GGUF, the `*-00001-of-NNNNN.gguf` file; the engine auto-loads its peers).

**[profiles.py](profiles.py)** — `ResolvedProfile` (lines 21–33)
- **Rename** `model` → `served_model_name`.
- **Add** `engine_model_path: str`.
- Add `backend: str` (no default — always set by resolver).
- Add `gguf_filename: Optional[str] = None`.
- `resolve_profile()` (lines 36–118):
  - Merge `backend` and `gguf_filename` from config or catalog row; default to `"vllm"` when absent so legacy configs still resolve.
  - Apply the deterministic naming rule above.
  - For llama.cpp rows: compute `engine_model_path` from cache root + snapshot sha + `gguf_filename`. If `gguf_filename` is missing on a llama.cpp row, raise — install validation should have caught it earlier (defense in depth).

**[catalog.py](catalog.py)** — `CatalogRow` schema, migration, reconcile
- Add columns `backend TEXT NOT NULL DEFAULT 'vllm'` and `gguf_filename TEXT` (nullable).
- Add a versioned migration in the existing migration helper (search `catalog.py` for prior `ALTER TABLE` migrations and follow that pattern).
- Update `sync_from_config`, `start_install_tx`, `mark_*` helpers, and `SELECT *` → dataclass mappers to thread the new columns.
- Import `_has_expected_weights` logic uses `group_gguf_filenames` from `repo_probe.py` to expand the shard set when validating llama.cpp rows.
- **Reconcile fix** ([catalog.py:466-489](catalog.py#L466-L489)): `_has_weights` is currently file-extension-only and is too permissive for llama.cpp rows. Two changes:
  1. Reconcile's `SELECT` (line 467-470) now also reads `backend` and `gguf_filename`.
  2. New helper `_has_expected_weights(snapshot_dir, backend, gguf_filename) -> bool`:
     - vLLM: existing behavior — any `.safetensors` / `.bin` sibling counts.
     - llama.cpp: `gguf_filename` itself must exist on disk; if it matches the shard pattern `*-NNNNN-of-NNNNN.gguf`, **all NNNNN shards** must exist. Otherwise → `partial`. This prevents promoting a row to `installed` when its specific GGUF file is missing, even when other quants in the same repo (used by another alias) are present.
  3. Keep `_has_weights` only for the small number of call sites that genuinely want "any weights at all" (e.g. cache discovery for synthetic `__cache__:*` rows). Rename to `_has_any_weights` to avoid misuse.

**[runtime.py](runtime.py)** — new pure builders next to `build_vllm_argv`
- `build_llama_argv(profile, *, host, port) -> list[str]`:
  - argv: `[LLAMA_SERVER_BIN, "--model", profile.engine_model_path, "--alias", profile.served_model_name, "--host", host, "--port", str(port), "--n-gpu-layers", "999", "--jinja"]`
  - Conditional: `["-c", str(profile.max_model_len)]` if set.
  - Multi-GPU: when `profile.gpus` is an explicit list, pass `--tensor-split` with a uniform-weight comma list of the right cardinality (`"1,1,..."`). When `"all"`, omit and let llama-server pick up `CUDA_VISIBLE_DEVICES`.
  - Append `profile.extra_args` last (escape hatch — same convention as vLLM builder).
  - **No `--api-key` flag** — `ResolvedProfile` has no api_key field; admin auth is handled at the manager layer, not the engine.
- `build_llama_env(profile, *, base_env)`:
  - Same `CUDA_VISIBLE_DEVICES` handling as `build_vllm_env`.
  - `HF_HOME = profile.storage_path` (kept consistent so the GGUF lives in the same cache mount).
- `derive_tp_size` does not apply to llama.cpp; the engine starter for llama.cpp doesn't call it.

### Subprocess lifecycle ([vllm_manager.py](vllm_manager.py))

- Introduce `_start_engine(profile)` that branches on `profile.backend`. Keep `_start_vllm` as-is for the vLLM path; add `_start_llama_cpp(profile)` for llama. Both populate the shared `_runtime` (`resident_alias`, `resident_profile`, `model_load_time`, etc.) — already backend-agnostic.
- `_kill_vllm` → rename to `_kill_engine`. It already SIGTERMs whatever is in the shared process global; only the name is misleading. Mechanical rename; touches a handful of sites.
- New `_wait_for_llama_cpp(timeout)` — same poll pattern as `_wait_for_vllm` but hits `http://127.0.0.1:8002/health` (llama-server returns `{"status":"ok"}` once loaded). Extract a shared `_wait_for_health(url, process_handle, timeout)` and call it from both wait helpers.
- `ensure_loaded` (lines 253–305): no semantic change. Internally calls `_start_engine` instead of `_start_vllm`.
- `_check_inner_port_clash`: unchanged — both backends use 8002 sequentially, so the existing single-port check covers both.
- Globals: rename `vllm_process` → `engine_process` for honesty; type stays `Optional[subprocess.Popen]`.

The `_proxy` path (lines 1809–1902) needs **zero structural changes** — it already targets `http://127.0.0.1:VLLM_INNER_PORT` and `_canonicalize_model_field` rewrites the body's `model` to `profile.model`. After the rename, that becomes `profile.served_model_name`, which for llama.cpp is the same alias passed to `--alias`. The path stays a path inside the engine, never on the wire.

### Status / API / CLI surface

The status endpoint and CLI must reflect backend so verification step ("`./vllm-ctl status` shows backend=llama.cpp") works. Three coordinated touch points:

1. **[vllm_manager.py:766-784](vllm_manager.py#L766-L784) `/manager/status`** (the route returns a raw `dict`, not a Pydantic response model):
   - Replace the `loaded_model = profile.model` line with `loaded_model = profile.served_model_name`.
   - Add `"backend": profile.backend if profile else None` to the response dict.
   - Add `"gguf_filename": profile.gguf_filename if profile and profile.backend == "llama.cpp" else None`.
   - Add `"engine_pid": engine_process.pid if engine_process and engine_process.poll() is None else None`. Keep the legacy `vllm_pid` key as an alias of `engine_pid` for one release to avoid breaking dashboards; mark it deprecated in the docstring.
   - Update the matching `ManagerStatus` TypeScript interface in [ui/src/api/types.ts](ui/src/api/types.ts) to include the new fields.
2. **Dashboard / UI status panel** (find the component that consumes `/manager/status` — likely `ui/src/views/Dashboard.tsx` or similar): show the backend badge and (when llama.cpp) the chosen GGUF filename next to the loaded model name.
3. **[vllm-ctl](vllm-ctl) `status` subcommand**: include backend in the printed output. Read `backend` and (when present) `gguf_filename` from the JSON response and add a line.

### Install endpoint validation ([vllm_manager.py](vllm_manager.py) + [downloader.py](downloader.py))

Today's install endpoint (`/manager/install`) calls `_install_internal`, which writes the catalog row before spawning the worker ([vllm_manager.py:1104](vllm_manager.py#L1104)). Add validation **before** row insertion:

1. **Probe**: call `huggingface_hub.HfApi().model_info(model_id, revision, files_metadata=True)` and run `probe_repo_format(siblings)`.
2. **Resolve backend**: if request body has explicit `backend`, honor it (subject to validation below); else use `probe.recommended_backend`.
3. **Validate weight presence**: if `probe.recommended_backend == "none"` and the request did not override backend with one that the repo's siblings actually support, reject 400 with `"no supported weight files in repo"`. (No silent vLLM-with-no-weights queue.)
4. **Validate `gguf_filename`** (server-side, mandatory):
   - If resolved backend is `llama.cpp`: request body **must** include `gguf_filename`, and that filename must equal `primary_filename` of one of `probe.gguf_candidates`. Reject (400) otherwise. This prevents partial-state catalog rows that can't launch.
   - If resolved backend is `vllm`: request must include at least one `.safetensors` / `.bin` sibling (i.e. `probe.has_transformer_weights == True`). Reject 400 otherwise. `gguf_filename` must be absent or null.
5. **Persist** the chosen backend and gguf_filename on the row.

The install request body grows two optional fields (`backend`, `gguf_filename`); both default to "auto" / null but the server enforces consistency.

### CLI install support ([vllm-ctl](vllm-ctl))

Today `./vllm-ctl install` (around line 574) submits to `/manager/install` without backend awareness. Add two flags:

- `--backend <vllm|llama.cpp>` — optional override; default lets the server auto-detect.
- `--gguf-filename <name>` — required when `--backend llama.cpp` is passed (mirror the server-side validation).

Add a thin convenience: `./vllm-ctl install <repo> --list-gguf` queries `/manager/hf/files` and prints the candidate list (one line per group with label and size), so a user can see filenames without leaving the terminal. Document in `vllm-ctl --help`.

### Auto-detection helper endpoint

New: `GET /manager/hf/files?model_id=...&revision=...` — returns the result of `probe_repo_format` so the UI can render the dropdown:

```json
{
  "has_gguf": true,
  "has_transformer_weights": false,
  "recommended_backend": "llama.cpp",
  "gguf_candidates": [
    {"label": "Q4_K_M (4.4 GB)", "primary_filename": "model-Q4_K_M.gguf",
     "shard_count": 1, "size_bytes": 4400000000, "all_filenames": ["model-Q4_K_M.gguf"]},
    {"label": "Q8_0 (sharded, 3 files, 14 GB)",
     "primary_filename": "model-Q8_0-00001-of-00003.gguf",
     "shard_count": 3, "size_bytes": 14000000000,
     "all_filenames": ["model-Q8_0-00001-of-00003.gguf", "...00002...", "...00003..."]}
  ]
}
```

Implementation: route handler calls `HfApi().model_info(..., files_metadata=True)`, builds a `list[SiblingMeta]`, and returns `probe_repo_format(siblings)` directly serialized. Cache the metadata fetch with the same TTL as `_fetch_config` so back-to-back calls (search → install) don't hit Hub twice.

### HF search: single metadata fetch ([hf_search.py](hf_search.py))

Today `_row_for_model` calls `_fetch_config` (for architectures) and separately `_safetensor_total` ([hf_search.py:435-440](hf_search.py#L435-L440)) for size estimate, where `_safetensor_total` already calls `model_info(files_metadata=True)`. Adding *another* `model_info` for the GGUF probe would be a third round-trip per result. Consolidate:

- Introduce `_fetch_repo_metadata(repo_id, token) -> RepoMetadata` that calls `model_info(files_metadata=True)` once and returns:
  ```python
  @dataclass(frozen=True)
  class RepoMetadata:
      siblings: list[SiblingMeta]
      transformer_weight_total: Optional[int]   # what _safetensor_total used to compute
      probe: RepoFormatProbe                     # the GGUF/format probe
  ```
- Cache it (mirror the existing `_fetch_config` cache: same TTL, same key shape).
- `_safetensor_total` becomes a thin reader of `RepoMetadata.transformer_weight_total`. Both that helper and the new probe consumer read from one cached fetch.
- The `/manager/hf/files` endpoint above uses the same `_fetch_repo_metadata` so the UI dropdown and the search-row probe share a cache hit.

In `_decide_compat(archs, fetch_status, probe)` — semantics: **`is_compatible == "at least one backend can serve this repo"`**, so users searching with `filter_compat=true` see everything they could install via any path:
- If `probe.has_gguf` → compatible, reason `"gguf via llama.cpp"` (covers GGUF-only repos *and* mixed-format repos whose vLLM architecture is unsupported — they remain installable via the llama.cpp override).
- Else if `probe.has_transformer_weights` and architectures pass the existing vLLM registry check → compatible, reason `""` (today's vLLM-compat reason logic).
- Else → not compatible. Reason: existing failure reasons, plus `"no supported weight files"` for the `recommended_backend == "none"` case.

The `recommended_backend` field on the search result tells the UI which backend will be chosen by default; users can still flip it on the install form for mixed-format repos. This separates "compatible at all" (the filter) from "default backend" (the badge), eliminating the earlier ambiguity.

Add `recommended_backend` (and `has_gguf`) to the `HfSearchResult` payload so the UI can show a per-row backend badge.

### Download worker ([download_worker.py](download_worker.py))

Today the worker calls `snapshot_download` and computes `total_bytes` over all weight files (around line 254). For selected-file/group GGUF installs, both the file set and the size accounting must change.

- New args field: `gguf_primary_filename: Optional[str]`. Worker computes shard list itself: if filename matches `*-00001-of-NNNNN.gguf`, expand to all `NNNNN` shards by listing repo siblings via `HfApi`; else just `[gguf_primary_filename]`.
- When set:
  - Pass `allow_patterns=<list of shard files>` to `snapshot_download`. This still creates the snapshot dir structure (good — keeps cache layout uniform with vLLM repos and lets reconcile / cache discovery continue to work) but only fetches the chosen GGUF shards.
  - **Compute `total_bytes` over the shard set only**, not all weight files in the repo. Critical for multi-quant repos (e.g. bartowski's 8+ quants) where the all-weights total would massively over-report and break free-space estimates and progress percentages.
  - Emit `{"event": "start", "total_bytes": <selected-only>, "selected_files": [...]}` so the manager can sanity-check.
- Stdout protocol stays line-delimited JSON; one new optional field on the `start` event. Resolver later constructs the full path: `<cache>/snapshots/<sha>/<gguf_primary_filename>`.

### Frontend (`ui/src/`)

- **api/types.ts**:
  - Add `backend?: "vllm" | "llama.cpp"` and `gguf_filename?: string` to `CatalogRow`, `InstallRequest`, and `ManagerStatus`. (Persisted/submitted backend cannot be `"none"` — install validation prevents this.)
  - Add `recommended_backend?: "vllm" | "llama.cpp" | "none"` and `has_gguf?: boolean` to `HfSearchResult`. The `"none"` value indicates the repo has no supported weight files; UI should disable the install button for those rows.
  - New types for `/manager/hf/files` response, with `recommended_backend: "vllm" | "llama.cpp" | "none"`.
- **api/client.ts**: new `getHfFiles(model_id, revision)`.
- **components/InstallForm.tsx**:
  - When the source HfSearchResult has `recommended_backend === "llama.cpp"` (or backend explicitly chosen), fetch `/manager/hf/files` and render a **required** dropdown of GGUF candidates. Disable Install until one is picked. Submit `gguf_filename` = chosen group's `primary_filename`.
  - Surface a backend selector pre-filled with the recommended value when the probe shows mixed-format repos so the user can override (default keeps vLLM, per the detector rule).
  - Hide vLLM-only fields (quantization, tp explicit) when backend=llama.cpp; leave `extra_args` and `max_model_len` visible since they map.
- **views/Catalog.tsx**: small backend badge per row (vLLM vs llama.cpp). Reuse [SourceBadge](ui/src/components/SourceBadge.tsx) styling.
- **views/Search.tsx**: results carry the backend badge so users can see which engine will run.
- **Dashboard / status panel**: backend badge + (when llama.cpp) gguf_filename next to the loaded model name.

### Dockerfile

Bake `llama-server` into the image. Build from source with a pinned tag, mirroring the explicit-pin convention used for vLLM:

```dockerfile
RUN git clone --depth 1 --branch <pinned-tag> https://github.com/ggerganov/llama.cpp /tmp/llama.cpp \
 && cmake -S /tmp/llama.cpp -B /tmp/llama.cpp/build -DGGML_CUDA=ON -DLLAMA_CURL=OFF \
 && cmake --build /tmp/llama.cpp/build -j --target llama-server \
 && cp /tmp/llama.cpp/build/bin/llama-server /usr/local/bin/ \
 && rm -rf /tmp/llama.cpp
ENV LLAMA_SERVER_BIN=/usr/local/bin/llama-server
```

Note `cmake -S <source>` is required — building without it would configure the working directory, not the cloned repo. Ensure `cmake`, `g++`, CUDA dev headers are present (likely already in the CUDA base image; add `apt-get install -y build-essential cmake` if not).

The `docker-compose.example.yml` does **not** need updates — same ports, same volumes. Inner port 8002 is reused. (CLAUDE.md note: live compose file may live outside repo. No changes needed since no new env vars are *required* — `LLAMA_SERVER_BIN` has a default.)

### Tests

- **tests/test_runtime.py**: add `test_build_llama_argv_*` mirroring vLLM argv tests (engine_model_path vs served_model_name, host/port, n-gpu-layers, max_model_len, extra_args ordering, tensor-split when `gpus` is a list).
- **tests/test_proxy.py**: add a test that a profile with `backend="llama.cpp"` causes `_start_engine` to call the llama path, not vLLM. Mock both `_start_vllm` and `_start_llama_cpp`; assert exactly one is called. Also assert the canonicalized request body uses `served_model_name` (the alias) and **never** the file path.
- **tests/test_hf_search.py**: tests for `_decide_compat` with `has_gguf=True / has_transformer_weights=False / archs=[] / fetch_status="missing"` → compatible. Mixed-format `has_gguf=True / has_transformer_weights=True / archs=["LlamaForCausalLM"]` → compatible via vLLM (regression). Assert `_fetch_repo_metadata` is called once per result, not multiple times (cache hit assertion).
- **tests/test_catalog.py**:
  - Backend column round-trips via `sync_from_config` and `start_install_tx`.
  - Reconcile correctly classifies a llama.cpp row whose `gguf_filename` is **missing on disk** as `partial`, even when other GGUF files exist in the same snapshot (mixed-quant repos shared with another alias).
  - Reconcile correctly handles sharded GGUF: all shards present → `installed`; one shard missing → `partial`.
- **New: tests/test_repo_probe.py** — pure-function tests for `group_gguf_filenames` and `probe_repo_format`: GGUF-only, transformer-only, mixed (recommends vLLM), neither, sharded grouping (single file, one group with 3 shards, mixed bag of quants and shards), missing sibling sizes (size_bytes=None propagates).
- **tests/test_install.py**: install with `backend="llama.cpp"` and missing `gguf_filename` → 400. With `gguf_filename` not in the candidate list → 400. With valid combo → row written with both fields.
- **tests/test_status.py** (or extend existing): `/manager/status` response includes `backend` and `gguf_filename` keys with correct values for both backend types.

Existing tests will require signature updates where `ResolvedProfile.model` becomes `served_model_name` and `engine_model_path` is added — search tests for `profile.model` and update systematically. Run `python -m pytest -q` after implementation; lightweight syntax check `python -m py_compile vllm_manager.py runtime.py config.py catalog.py profiles.py download_worker.py hf_search.py`.

## Edge cases

- **Mixed-format repos**: handled by the single detector contract — `has_transformer_weights` wins over `has_gguf`. Recommended backend is vLLM. User can still override on install.
- **Sharded GGUF without 5-digit naming**: shard regex requires the canonical `-NNNNN-of-NNNNN.gguf` pattern; non-conforming names are treated as single-file (the install will likely fail at engine load — acceptable corner case, surfaced via runtime error).
- **Two aliases pointing at the same repo with different GGUF files**: the per-row reconcile check above ensures each alias only goes `installed` when *its* `gguf_filename` (and shards) are present.
- **Sibling metadata missing size**: `size_bytes` propagates as `None` from `SiblingMeta` through `GgufCandidate`. UI label drops the size suffix in that case ("Q4_K_M" instead of "Q4_K_M (4.4 GB)"). Download still works; only the pre-flight estimate is unavailable.
- **Resident `_runtime.resident_profile.backend` mismatch on rapid swap**: covered by existing `_swap_lock` + `_loading_target` piggyback. No new locking needed.
- **Eviction**: idle eviction calls the rename-target `_kill_engine`. Backend-agnostic. No change.
- **Usage flush**: `_flush_usage_best_effort` is called on teardown; backend-agnostic. No change.
- **Legacy `MODEL_ALIASES`**: never points at GGUF; default backend stays `"vllm"`.

## Verification

After implementation, verify in this order:

1. **Static**: `python -m py_compile <touched files>` and `bash -n vllm-ctl`.
2. **Unit**: `python -m pytest -q tests/test_runtime.py tests/test_proxy.py tests/test_hf_search.py tests/test_catalog.py tests/test_install.py tests/test_repo_probe.py tests/test_status.py`.
3. **Build**: `./vllm-ctl build` — confirm `llama-server` ends up in the image (`docker run --rm <img> which llama-server` and `docker run --rm <img> llama-server --version`).
4. **End-to-end vLLM regression**: load a known vLLM model (`./vllm-ctl load Qwen/Qwen3-8B`); curl `/v1/chat/completions`; confirm same behavior as before, *especially* that the upstream `model` field is still the canonical HF id (sanity check that the `model` → `served_model_name` rename didn't break canonicalization). `./vllm-ctl status` shows `backend: vllm`.
5. **End-to-end GGUF**: search HF for a known GGUF repo (e.g. `bartowski/Qwen2.5-7B-Instruct-GGUF`) via `/manager/hf/search?q=...&filter_compat=true` — should appear with `recommended_backend: "llama.cpp"`. Install via UI: confirm the GGUF file dropdown appears, picks a quant, installs. `./vllm-ctl status` shows `backend: llama.cpp` and the chosen `gguf_filename`. `./vllm-ctl chat "hi"` returns a completion. Inspect logs to confirm `llama-server` was the launched process. Confirm the request body forwarded by `_proxy` has `"model": "<alias>"`, not a filesystem path.
6. **CLI install**: `./vllm-ctl install <gguf-repo> --list-gguf` prints candidates. `./vllm-ctl install <gguf-repo> --backend llama.cpp --gguf-filename <name>` succeeds; same command without `--gguf-filename` returns a 400.
7. **Sharded GGUF**: install a sharded repo (a Q8_0 split into 3 files); confirm only the one shard group downloads, all shards land in the snapshot dir, llama-server loads them all from the first-shard path. Confirm `total_bytes` matches the shard-group size, not the whole-repo size.
8. **Reconcile**: manually delete one shard from a sharded llama.cpp row's snapshot dir; restart; confirm reconcile flips that row to `partial` (not still `installed`).
9. **Install validation**: POST `/manager/install` with `backend="llama.cpp"` and no `gguf_filename` → 400. With a `gguf_filename` not present in the repo → 400. Against a repo with no `.safetensors` / `.bin` / `.gguf` siblings (e.g. a config-only or dataset repo) → 400 with `"no supported weight files"`.
10. **Mixed-format repo**: install a repo with both `.safetensors` and `.gguf`; confirm `recommended_backend = "vllm"` and the vLLM path runs by default.
11. **Swap**: load vLLM model, then load GGUF — confirm clean teardown of vLLM and start of llama-server with no port collision. Status correctly flips backend label.
12. **Eviction**: leave a llama.cpp model idle past `eviction_idle_seconds`; confirm it gets killed cleanly (existing eviction loop unchanged).
13. **Search caching**: with logging on `_fetch_repo_metadata`, run `/manager/hf/search` then `/manager/hf/files` for the same repo — confirm only one Hub `model_info` call total (cache hit on second).

## Critical files (cheat sheet)

Modified:
- [config.py](config.py) — `ModelProfile` field additions
- [profiles.py](profiles.py) — `ResolvedProfile` field additions, `model` → `served_model_name` rename, `engine_model_path` field, `resolve_profile` merge, deterministic naming rule
- [catalog.py](catalog.py) — `CatalogRow` schema, migration, `sync_from_config`, reconcile uses `_has_expected_weights` with backend + gguf_filename (consumes `repo_probe.group_gguf_filenames`)
- [runtime.py](runtime.py) — `build_llama_argv`, `build_llama_env`, shared `_wait_for_health` if extracted
- [vllm_manager.py](vllm_manager.py) — `_start_engine` dispatch, `_start_llama_cpp`, `_wait_for_llama_cpp`, `_kill_vllm` → `_kill_engine` rename, `vllm_process` → `engine_process` rename, `/manager/hf/files` endpoint, `/manager/status` includes backend + gguf_filename + engine_pid, install endpoint accepts and validates `backend` + `gguf_filename`
- [download_worker.py](download_worker.py) — selected-file/shard download via `allow_patterns`; `total_bytes` over shard set only; `selected_files` on start event
- [hf_search.py](hf_search.py) — `_fetch_repo_metadata` consolidates `model_info(files_metadata=True)` for size + GGUF probe (architectures still come from `_fetch_config`); `_decide_compat` consumes `RepoFormatProbe`; `recommended_backend` in payload
- [vllm-ctl](vllm-ctl) — `install` learns `--backend`, `--gguf-filename`, `--list-gguf`; `status` prints backend
- [Dockerfile](Dockerfile) — build/install pinned `llama-server` (with `cmake -S` source flag)
- `ui/src/api/types.ts`, `ui/src/api/client.ts`, `ui/src/components/InstallForm.tsx`, `ui/src/views/Catalog.tsx`, `ui/src/views/Search.tsx`, dashboard/status panel — UI plumbing

Added:
- `repo_probe.py` — pure stdlib module with `group_gguf_filenames`, `probe_repo_format`, `SiblingMeta`, `GgufGroup`, `GgufCandidate`, `RepoFormatProbe`. Imported by `catalog.py`, `hf_search.py`, `vllm_manager.py`, and `download_worker.py` (the last imports only `group_gguf_filenames`).
- `tests/test_repo_probe.py`
- `tests/test_status.py` (or new test cases in an existing file covering status response)

Untouched (verify in regression):
- `_proxy`, `ensure_loaded`, `_swap_lock`, `_runtime`, eviction loop, usage flush, admin auth.
