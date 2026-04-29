# Phase 5 — HuggingFace Search and vLLM Compatibility Filter

## Context

[project_docs/implementation_plan.md](/Users/c/software_projects/mnemosyne-inference/project_docs/implementation_plan.md) Phase 5 and [PRD §5.9](/Users/c/software_projects/mnemosyne-inference/project_docs/PRD.md) call for a `GET /manager/hf/search` admin endpoint that:

1. Pre-filters HF Hub server-side to text-gen / transformers candidates.
2. Reads each candidate's `config.json#architectures` and cross-references against the architecture set the **installed vLLM** supports.
3. Returns both compatible and incompatible results (incompatible flagged with a reason) so the UI can dim, not hide, them.
4. Estimates download size from siblings.

The architecture set is sourced *primarily* by introspecting vLLM's model registry at startup; if that import path breaks on a vLLM bump, we fall back to a JSON snapshot bundled in the image. A `scripts/refresh_arch_list.py` helper regenerates that snapshot from a live vLLM install during the upgrade workflow.

This phase lands ahead of the UI (Phase 6), which will consume `/manager/hf/search` from the Search & Install view. No inference-plane changes; no catalog schema changes; no `vllm-ctl` changes.

## Files

**New**
- [hf_search.py](/Users/c/software_projects/mnemosyne-inference/hf_search.py) — `HfApi` wrapper, config.json fetch + parse, compatibility decision, size estimation, in-process result cache.
- [vllm_supported_architectures.json](/Users/c/software_projects/mnemosyne-inference/vllm_supported_architectures.json) — bundled fallback snapshot. Replace the current placeholder with a real generated snapshot before marking Phase 5 complete.
- [scripts/refresh_arch_list.py](/Users/c/software_projects/mnemosyne-inference/scripts/refresh_arch_list.py) — regenerates the JSON from a live vLLM install. Intended to run inside the image after a vLLM bump.
- `tests/test_hf_search.py`, `tests/test_arch_loader.py`, `tests/fixtures/fake_hf.py`.

**Modified**
- [vllm_manager.py](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py) — load architecture set in `manager_lifespan` (after `load_config`, before `apply_config`); store on a module global; register one new admin route `GET /manager/hf/search`. Add `vllm_arch_count` and `vllm_arch_source` to `/manager/status`.
- [Dockerfile](/Users/c/software_projects/mnemosyne-inference/Dockerfile) — extend the `COPY` at lines 59-60 to include `hf_search.py`, `vllm_supported_architectures.json`, and `scripts/`.
- [project_docs/project_status.md](/Users/c/software_projects/mnemosyne-inference/project_docs/project_status.md) — mark Phase 5 complete on landing.

## Architecture set loading (`hf_search.py`)

`load_supported_architectures(json_fallback_path: Path) -> tuple[frozenset[str], str]` returns `(archs, source)` where `source ∈ {"vllm-registry", "bundled-json", "empty"}`. Tried in order:

1. **Runtime introspection** (preferred). Try the documented module path first, then the package re-export:
   ```python
   try:
       from vllm.model_executor.models.registry import ModelRegistry
   except ImportError:
       from vllm.model_executor.models import ModelRegistry
   archs = frozenset(ModelRegistry.get_supported_archs())
   ```
   Wrapped in `try/except (ImportError, AttributeError, Exception)` per PRD §5.9. On success, log at INFO with the count. **Do not** poke at `_models`, `_LazyRegisteredModel`, or any underscore-prefixed attributes — they are internal.

2. **Bundled JSON fallback**: read `vllm_supported_architectures.json` from the repo root (next to `vllm_manager.py`). Schema: `{"vllm_version": "...", "generated_at": "...", "architectures": [...]}`. Log at WARNING with the file's `vllm_version` and a hint to run `scripts/refresh_arch_list.py`.

3. **Empty set**: log ERROR. Search still works — every result is flagged `is_compatible: false, compat_reason: "vllm registry unavailable"`. Better than failing boot.

The result is loaded once during `manager_lifespan` startup and stashed on module globals `_supported_archs: frozenset[str]` and `_arch_source: str`. `/manager/status` gains two new keys (`vllm_arch_count`, `vllm_arch_source`) so operators can see when fallback is active.

## Search endpoint

```
GET /manager/hf/search?q=<str>&limit=<int>&filter_compat=<bool>&include_vision=<bool>
```

Defaults: `limit=20` (cap at 50), `filter_compat=false` (return both, flag incompatible — matches PRD), `include_vision=false`.

**Top-level response envelope** (pinned now so Phase 6 does not have to guess):

```json
{
  "query": "qwen2.5",
  "limit": 20,
  "include_vision": false,
  "vllm_arch_source": "vllm-registry",
  "vllm_arch_count": 217,
  "results": [ /* per-row objects */ ]
}
```

Per-row: `model_id, architectures, is_compatible, compat_reason, size_estimate_gb, downloads, likes, last_modified, tags, pipeline_tag`.

### Pipeline (sync, run on a worker thread)

1. **Pre-filter via `HfApi.list_models`**. `pipeline_tag` is a single string (not a list) on `huggingface_hub` 0.36.x. To support both text-gen and vision LLMs without abusing the deprecated `task` list-arg, do **two calls** when `include_vision=true` and merge by `model.id`, dedupe, sort by `downloads` descending, then truncate to `limit`:
   ```python
   def _list_one(tag: str) -> list[ModelInfo]:
       return list(api.list_models(
           search=q, filter="transformers", pipeline_tag=tag,
           limit=limit, sort="downloads", direction=-1,
           token=token,
       ))
   rows = _list_one("text-generation")
   if include_vision:
       rows = _merge_by_id(rows, _list_one("image-text-to-text"))
   ```
   Token from `HUGGING_FACE_HUB_TOKEN` env even for public search — lifts rate limits and unlocks gated repos in result fetches downstream. Use `filter="transformers"` rather than the deprecated `library="transformers"` kwarg so the code survives `huggingface_hub` 1.x.

2. **Per-candidate `config.json`** via `hf_hub_download(repo_id, "config.json", cache_dir=<HF_HOME>, token=...)`. `huggingface_hub`'s built-in ETag cache covers re-fetches; an in-process `dict[str, dict]` keyed on `repo_id` (cap 256, simple FIFO) keeps repeat hits within a single search cheap. Per-row failure handling is row-level, not endpoint-level — see "Auth failure split" below.

3. **Compatibility decision**. Parse `config.get("architectures", [])`. Normalize: `[arch] if isinstance(arch, str) else (arch or [])`. Compatible iff the list is non-empty AND **every** entry is in `_supported_archs`. `compat_reason`:
   - `None` if compatible.
   - `"missing config.architectures"` if absent/empty/null.
   - `"unsupported architecture: <name>"` listing the first offender.
   - `"gated or unauthorized"` if the per-row `config.json` fetch returned 401/403.
   - `"config fetch failed: <type>"` for other per-row fetch errors.
   - `"vllm registry unavailable"` if `_arch_source == "empty"`.

4. **Size estimate**. Use the same approach already proven in [download_worker.py:167-185](/Users/c/software_projects/mnemosyne-inference/download_worker.py): `HfApi.model_info(repo_id, files_metadata=True, token=token)` → sum `siblings[*].size` for `.safetensors|.bin|.gguf|.pt` files. This is the primary path. Convert bytes → `size_estimate_gb: float | null` (null on any failure). Size-estimate failures must **not** change `is_compatible` or `compat_reason`; compatibility is based only on the architecture decision from `config.json`. The `get_safetensors_metadata` route was considered but its `SafetensorsFileMetadata` does not expose per-file byte size in a portable way; we'd be relying on `metadata["total_size"]` which only populates for sharded repos. Keep one code path; reuse the existing siblings helper (lift it into `hf_search.py` so it does not depend on `download_worker.py` import).

5. **Filter**: if `filter_compat=true`, drop `is_compatible=false` rows server-side after the per-row work completes (so the count of incompatible-but-dropped is still observable in logs).

### Auth failure split (corrected)

- **Endpoint-level (502)**: `HfApi.list_models` itself returns 401/403. The user can't search at all; surface clearly with `{"detail": "hub unauthorized — set HUGGING_FACE_HUB_TOKEN"}`.
- **Row-level config fetch (200, flagged)**: a *specific* candidate's `config.json` fetch returns 401/403 (gated repo, no token, or user lacks access). Row stays in the response with `is_compatible: false, compat_reason: "gated or unauthorized"`.
- **Row-level size fetch (200, partial metadata)**: a candidate's `model_info` fetch returns 401/403 or another error. Row stays in the response with its existing compatibility result and `size_estimate_gb: null`; do not overwrite `compat_reason`.

Other endpoint-level errors: empty `q` → 400. Network timeout → 504. Match existing manager error style ([vllm_manager.py:716-718](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py)).

## Concurrency and timeout story (corrected)

`huggingface_hub` is synchronous and `HfApi` does not expose a per-call timeout in its public constructor. Two layers of protection, both honest about their limits:

1. **Bounded executor**: `_search_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hf-search")` lives at module level. `loop.run_in_executor(_search_pool, _do_search, ...)` rather than the default `asyncio.to_thread` pool. Caps stuck-thread buildup at 2 — admin plane is single-user, this is plenty.
2. **Outer deadline**: `asyncio.wait_for(future, timeout=30)` protects the FastAPI response. On timeout, raise 504. **The worker thread keeps running** — `huggingface_hub` does not honor `Future.cancel()`. The bounded pool is what prevents pile-up; documented in module docstring.
3. **Env-var hint**: `HF_HUB_ETAG_TIMEOUT` and `HF_HUB_DOWNLOAD_TIMEOUT` (read by `huggingface_hub` for its own HTTP calls) get sensible defaults (10s / 30s) set in the Dockerfile if not already present. These cover the `hf_hub_download` path. They do **not** cover `list_models` / `model_info`, which use the underlying `requests` session's default timeout. Acknowledged; not a regression vs current behavior.

Per-request: 30s end-to-end. Single-user; no rate-limit story needed.

## `scripts/refresh_arch_list.py`

CLI script that runs inside the container. Imports vLLM, calls `ModelRegistry.get_supported_archs()`, writes the JSON snapshot to a path the operator chooses (default: same path the runtime loads from). Includes `vllm.__version__` and the current UTC timestamp in the file. Exits non-zero with a clear error if the import fails — that is a real signal the API has shifted.

`docker exec vllm-manager python scripts/refresh_arch_list.py` is the documented invocation. Operational docs land in Phase 7; the script ships now.

## Tests

All run on macOS dev hosts — no vLLM, no network. `monkeypatch` substitutes `HfApi` and `hf_hub_download`.

- `tests/test_arch_loader.py`
  - Successful registry import returns a non-empty set, source `"vllm-registry"`.
  - ImportError on both registry paths → falls through to bundled JSON, source `"bundled-json"`.
  - Missing JSON file → empty set, source `"empty"`, ERROR log.
  - Malformed JSON → empty set, source `"empty"`, ERROR log.
  - The bundled file in the repo parses and contains common architectures (`LlamaForCausalLM`, `Qwen2ForCausalLM`). This requires replacing the current placeholder JSON with a generated snapshot as part of Phase 5.

- `tests/test_hf_search.py` (uses the existing `client` fixture)
  - `monkeypatch.setattr("hf_search._api", FakeHfApi(...))` and `setattr("hf_search.hf_hub_download", fake_download)`.
  - Compatible row → `is_compatible=true, compat_reason=None`.
  - Incompatible row → `is_compatible=false, compat_reason="unsupported architecture: …"`.
  - Missing `config.json` → flagged `"missing config.architectures"`, not dropped.
  - Per-row 403 on `config.json` → flagged `"gated or unauthorized"`, not dropped.
  - Endpoint-level `list_models` raising 401 → 502 with token hint.
  - `filter_compat=true` drops incompatible rows.
  - `include_vision=true` causes **two** `list_models` calls (capture `pipeline_tag` kwarg via FakeHfApi); results merged by `model_id` and de-duped.
  - `include_vision=false` causes one call with `pipeline_tag="text-generation"`.
  - Empty `q` → 400.
  - Top-level response envelope shape asserted: `query`, `limit`, `include_vision`, `vllm_arch_source`, `vllm_arch_count`, `results`.
  - **Plane separation**: `inference_client.get("/manager/hf/search?q=x")` → 404. (Mirror existing test_planes.py pattern.)
  - Auth: `admin_client_no_auth` with `ADMIN_PASSWORD` set → 401.

- Reuse fakes via `tests/fixtures/fake_hf.py` so future install/UI tests can share them.

## Open question (worth flagging, defaulting per PRD)

PRD §5.9 specifies `task="text-generation"` for the HfApi pre-filter. Modern vision-LLMs (Qwen2.5-VL, Llava, etc.) are tagged `pipeline_tag="image-text-to-text"`, so the literal PRD filter excludes most of the vision models the user explicitly wants installable (PRD §S6 mentions "qwen vision"). The plan ships with `include_vision=false` default to match the spec exactly, but exposes the flag so the UI can flip it on for vision-aware searches. If the user prefers `include_vision=true` as default, that's a one-line change.

## Verification

Local (macOS, no GPU):
- `python -m pytest -q tests/test_arch_loader.py tests/test_hf_search.py` — all green.
- `python -m pytest -q` — full suite still green.
- `python -m py_compile vllm_manager.py hf_search.py scripts/refresh_arch_list.py` — syntax check.
- `python scripts/refresh_arch_list.py --help` runs without importing vLLM.

In-container (after `vllm-ctl build && vllm-ctl start`):
- `docker exec vllm-manager python scripts/refresh_arch_list.py /tmp/test.json` writes a JSON with 100+ archs and a populated `vllm_version`.
- `curl -u admin:$ADMIN_PASSWORD 'http://localhost:8001/manager/status' | jq '.vllm_arch_count, .vllm_arch_source'` reports a non-zero count from `vllm-registry`.
- `curl -u admin:$ADMIN_PASSWORD 'http://localhost:8001/manager/hf/search?q=qwen2.5-7b-instruct&limit=5'` returns at least one row with `is_compatible=true`; envelope has `vllm_arch_source: "vllm-registry"`.
- `curl -u admin:$ADMIN_PASSWORD 'http://localhost:8001/manager/hf/search?q=qwen2.5-vl&limit=5&include_vision=true'` returns vision-LLM rows.
- `curl -u admin:$ADMIN_PASSWORD 'http://localhost:8001/manager/hf/search?q=stable-diffusion&limit=5'` returns rows; all flagged `is_compatible=false` with non-empty `compat_reason`.
- `curl 'http://localhost:8000/manager/hf/search?q=x'` → 404 (plane separation).
- Temporarily move `vllm_supported_architectures.json` aside, `vllm-ctl restart`, `/manager/status` still reports `vllm_arch_source: "vllm-registry"` — confirms runtime path is primary, not just a code path.
