# Phase 0 — Project Foundation and Safety Rails

> Paths in this document assume the plan lives at `project_docs/plans/phase_0.md` in the repo.

## Context

The PRD describes a substantial evolution of the current single-file [vllm_manager.py](../../vllm_manager.py) (603 lines) into a multi-module config-driven service with a SQLite catalog, separate admin/inference planes, multi-drive storage, and a React admin UI. The first 7 phases of [implementation_plan.md](../implementation_plan.md) refactor or replace nearly every meaningful surface of the current code.

Before any of that lands, Phase 0 puts down safety rails so the refactor doesn't silently regress current behavior or stall on missing scaffolding. Concretely it must:

1. Stop the unbounded `vllm --pre` pip install from quietly drifting under us (the current Dockerfile pulls whichever nightly is current at build time — diagnosing a future regression is impossible without a known-good baseline).
2. Capture the current contract of the four routes (`/health`, `/manager/status`, `/manager/load`, `/v1/*` auto-swap) before the module split scrambles their internals.
3. Land the example files (`config.yaml.example`, `.env.example`, `vllm_supported_architectures.json`) so Phase 1+ implementers and the README have something to point at.
4. Set up a minimal pytest harness so Phase 1's config validation tests and Phase 3's plane-separation route tests have a place to live.

Out of scope for this phase: any change to `vllm_manager.py`, `vllm-ctl`, or the external `docker-compose.yml`. Phase 0 is purely additive, with one exception: the Dockerfile vLLM line.

## Phase 0 decisions

- **vLLM pin:** Replace `vllm --pre` with an explicit nightly version. Implementer queries the index at execution time and bakes the resulting version string in.
- **Test deps:** `requirements-dev.txt` (matches the current "no pyproject.toml" repo style; can graduate later if needed).
- **Smoke checks:** Pytest harness for routes that can run on the dev host (no CUDA), plus markdown checklist for routes that need a live container/GPU.

## Files to create

### `config.yaml.example`
Mirror of [PRD §5.1](../PRD.md) schema verbatim with explanatory comments. Three example aliases (qwen-72b-awq, qwen-coder-7b, llama-vision) covering the GPU-plan variants (`all`, `[0]`, `[1]`) and the `extra_args` escape hatch. Phase 1's `config.py` will validate against this exact shape.

### `.env.example`
Three vars from [PRD §5.13](../PRD.md):
- `ADMIN_PASSWORD` — comment notes admin port falls back to loopback if unset
- `HUGGING_FACE_HUB_TOKEN` — optional; required only for gated repos
- `INFERENCE_API_KEY` — optional; if set, `/v1/*` requires bearer auth

### `vllm_supported_architectures.json`
Documented stub. Phase 5's `scripts/refresh_arch_list.py` overwrites it. Shape:
```json
{
  "vllm_version": null,
  "generated_at": null,
  "architectures": [],
  "_note": "Placeholder — populated by scripts/refresh_arch_list.py (Phase 5). Until then, hf_search.py compatibility checks fall back to runtime introspection only."
}
```

### `requirements-dev.txt`
Minimum needed to import `vllm_manager.py` and run TestClient on the dev host without CUDA:
```
pytest
pytest-asyncio
httpx
fastapi
huggingface_hub
uvicorn
pyyaml
```
`fastapi`/`httpx`/`huggingface_hub`/`uvicorn` are listed because [vllm_manager.py:12-28](../../vllm_manager.py) imports them at module load — a host pytest run will fail collection without them. `vllm` itself is *not* listed: nothing in `vllm_manager.py` imports it (it's launched as a subprocess), so tests run on the macOS dev host. `pyyaml` is for Phase 1 but is cheap to include now.

### `pytest.ini`
```
[pytest]
testpaths = tests
asyncio_mode = auto
```

### `tests/__init__.py`
Empty.

### `tests/conftest.py`
One fixture: a `TestClient(app)` instance that imports `vllm_manager` and yields a fresh client per test. Resets the full set of module globals between tests so order doesn't matter and load/error-path tests added later don't leak state. Reset list (matches [vllm_manager.py:52-62, 436](../../vllm_manager.py)):
- `current_model` → `None`
- `vllm_process` → `None`
- `model_load_time` → `None`
- `_loading` → `False`
- `_current_tp` → `DEFAULT_TP`
- `_current_gpu_mem` → `DEFAULT_GPU_MEM`
- `_downloads` → `{}`
- `MODEL_ALIASES` → `{}`

### `tests/test_smoke.py`
Pytest cases — only the routes that don't need a running vLLM subprocess. **Each assertion captures the *current* response shape verbatim** so the test doubles as a contract snapshot for Phase 1+ regressions:
- `test_health_returns_ok` — `GET /health` returns 200 with exactly `{"status": "ok", "model_loaded": false, "loading": false}` ([vllm_manager.py:418-425](../../vllm_manager.py)).
- `test_status_no_model_loaded` — `GET /manager/status` returns 200 with the keys `loaded_model`, `loading`, `vllm_pid`, `loaded_at`, `loaded_at_human`, `tp_size`, `gpu_mem_util`, `inner_endpoint`. Assert `loaded_model is None`, `loading is False`, `vllm_pid is None`, `loaded_at is None`, and `tp_size == DEFAULT_TP` ([vllm_manager.py:173-185](../../vllm_manager.py)).
- `test_aliases_crud_roundtrip` — POST → GET → DELETE on `/manager/aliases`. Pure dict CRUD; no subprocess involved.
- `test_resolve_model_known_alias` and `test_resolve_model_passthrough` — direct unit tests on `_resolve_model()` ([vllm_manager.py:439-441](../../vllm_manager.py)).
- `test_downloads_empty_initially` — `GET /manager/downloads` returns `{"downloads": []}` before any downloads ([vllm_manager.py:398-401](../../vllm_manager.py)).

Aim: ~6 tests, all run in < 2s on the host. This is enough to (a) prove the harness works, (b) give Phase 1 a foundation to extend.

### `project_docs/smoke_checks.md`
Manual checklist covering the routes pytest can't reach without GPU + nightly + a real model. Each item is a curl command + the expected observable behavior:

1. **Cold load** — `POST /manager/load {"model": "Qwen/Qwen2.5-7B-Instruct"}` → returns 200, `/manager/status` shows the model resident within `STARTUP_TIMEOUT`.
2. **Direct proxy** — `POST /v1/chat/completions` with the loaded model → returns a completion.
3. **Streaming proxy** — same, with `"stream": true` → SSE response framed correctly (no buffering).
4. **Auto-swap** — `POST /v1/chat/completions` with a *different* `model` → triggers unload + load, then serves.
5. **Bad model id** — `POST /manager/load {"model": "nonsense/does-not-exist"}` → returns clear error, no zombie subprocess.
6. **Download lifecycle** — `POST /manager/download` returns `started`; `GET /manager/download/{id}` observes a valid download state and should eventually reach `complete` for a valid test model.
7. **Unload** — `POST /manager/unload` → `/manager/status` shows `loaded_model: null`, `vllm_pid: null`, `loaded_at: null` ([vllm_manager.py:173-185](../../vllm_manager.py)).

The doc captures expected response shapes verbatim so Phase 1+ regressions are easy to spot. **This file is the canonical "what worked before the refactor" record.**

## Files to modify

### `Dockerfile` — pin vLLM (lines 38-41)

**Current:**
```
RUN pip install --no-cache-dir \
      vllm \
      --pre \
      --extra-index-url https://wheels.vllm.ai/nightly
```

**Implementer steps:**
1. Run `pip index versions vllm --pre --index-url https://wheels.vllm.ai/nightly` (or fetch `https://wheels.vllm.ai/nightly/vllm/` directly) to discover the latest nightly version string.
2. Replace with an exact pin and a comment showing how to refresh:
   ```
   # vLLM nightly pin — refresh: pip index versions vllm --pre \
   #   --index-url https://wheels.vllm.ai/nightly | head -1
   # Last refreshed: YYYY-MM-DD against Blackwell sm_100 build.
   RUN pip install --no-cache-dir \
         "vllm==<EXACT_VERSION>" \
         --extra-index-url https://wheels.vllm.ai/nightly
   ```
3. Rebuild and run smoke checks before committing — pinned version must actually launch a model on the target hardware.

This is the only Dockerfile change in Phase 0. The multi-stage Node build comes in Phase 7.

## Files explicitly not touched

- `vllm_manager.py` — frozen for Phase 0. Phase 1 begins the split.
- `vllm-ctl` — frozen. Phase 3 updates URL/auth.
- `docker-compose.yml` — lives outside the repo. Phase 7 documents the new mounts (`/config`, `/state`, storage drives).
- `.gitignore` — already covers `__pycache__/`, `.pytest_cache/`, `.env`, `.venv/` (verified). No edits.

## Verification

On the dev host (macOS, no CUDA):
1. `python3 -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements-dev.txt`
3. `pytest -v` → all ~6 tests green.
4. `python -c "import vllm_manager"` → imports cleanly, no errors.

On the workstation (CUDA + Blackwell):
5. `vllm-ctl build` → succeeds with pinned vLLM version.
6. `vllm-ctl start && curl localhost:8000/health` → 200.
7. Walk through `project_docs/smoke_checks.md` end-to-end. Capture any unexpected responses as deltas (Phase 1+ must not regress these).

## Exit criteria (from implementation_plan.md §Phase 0)

- ✅ Current manager behavior is covered by basic tests (pytest harness) and documented smoke checks (markdown).
- ✅ Example config and env files exist (`config.yaml.example`, `.env.example`).
- ✅ Docker dependency pins are explicit enough to make future regressions diagnosable (vLLM pinned to a specific nightly version with a documented refresh recipe).

## Critical files referenced

- [vllm_manager.py:33-40](../../vllm_manager.py) — env-var config (kept verbatim for Phase 0).
- [vllm_manager.py:173-185](../../vllm_manager.py) — `/manager/status` shape, snapshotted by tests.
- [vllm_manager.py:398-401](../../vllm_manager.py) — `/manager/downloads` shape (`{"downloads": []}` when empty).
- [vllm_manager.py:418-425](../../vllm_manager.py) — `/health` shape, snapshotted by tests.
- [vllm_manager.py:439-441](../../vllm_manager.py) — `_resolve_model()` covered by unit test.
- [Dockerfile](../../Dockerfile) lines 38-41 — vLLM install line, the only file modified in this phase.
- [PRD.md §5.1](../PRD.md), [§5.13](../PRD.md) — schemas mirrored into the example files.

## What this phase does NOT prove

- That Phase 1's config loader works against `config.yaml.example` (Phase 1 task).
- That Phase 5's introspection produces a non-empty architecture list (Phase 5 task).
- That the multi-stage UI build works (Phase 7 task).

Phase 0 is scaffolding and a contract snapshot, nothing more. If any of the smoke-check items reveal current behavior is broken (e.g. download lifecycle has a bug), surface that as a separate issue rather than fixing in-flight — Phase 0 should be a faithful baseline.
