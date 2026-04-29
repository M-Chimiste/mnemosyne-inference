# Phase 6 — Admin UI

## Context

[project_docs/implementation_plan.md](../implementation_plan.md) Phase 6 and
[PRD §5.8](../PRD.md) call for a React + Vite + TypeScript + Tailwind admin
SPA served only from the admin FastAPI app. Phases 0-5 provide the backend
management APIs; Phase 6 adds the browser surface, a static-serving shim, and
one read-only GPU telemetry endpoint needed to satisfy the dashboard
requirement honestly.

The implementation keeps the inference plane headless: no `/ui/*` routes and
no manager mutation routes are registered on `inference_app`.

## Review Mitigations Incorporated

- UI routes live on a dedicated `ui_router` defined before app construction
  and included only on `admin_app` with `Depends(require_admin_basic)`.
- Static UI routes are always registered and resolve
  `MNEMOSYNE_UI_DIR` or `/app/static` per request, so tests and dev runs can
  change the build directory after `vllm_manager` import.
- SPA fallback rejects paths that resolve outside the static root with 404;
  missing internal routes such as `/ui/catalog` serve `index.html`.
- Dashboard uses a new read-only `GET /manager/gpu` endpoint for live GPU
  memory/utilization and labels catalog `request_count` as a persisted catalog
  counter, not recent traffic.
- Catalog cache-only rows are derived from the reserved alias prefix
  `__cache__:` / `__cache__/`, not from `source` alone.
- Catalog actions mirror backend behavior:
  - Cache-only rows hide Load and offer Create alias.
  - Non-cache-only `ui_install` rows use
    `DELETE /manager/install/{alias}/cache` for cache deletion.
  - Config rows use `DELETE /manager/cache/{hf_model_id}` with stronger
    copy because sibling aliases/storage locations may be affected.
  - Row removal uses `DELETE /manager/install/{alias}` only for removable
    `ui_install` rows, including synthetic cache rows.
- Search keeps incompatible rows visible with `compat_reason`, but disables
  the primary Install action when `is_compatible === false`.
- Search carries `size_estimate_gb` into `POST /manager/install`; null sizes
  are shown as a warning that the backend free-space precheck will be skipped.
- `lucide-react` is used for icon buttons; destructive actions use a minimal
  native `<dialog>` confirmation component.

## Files

**New**

- `ui/` — Vite project root.
  - `package.json`, `package-lock.json`.
  - `vite.config.ts`, `vitest.config.ts`, `tsconfig*.json`.
  - `tailwind.config.ts`, `postcss.config.js`, `index.html`.
  - `src/main.tsx`, `src/App.tsx`, `src/styles.css`.
  - `src/api/client.ts`, `src/api/types.ts`, `src/api/queries.ts`,
    `src/api/mutations.ts`.
  - `src/views/Dashboard.tsx`, `Catalog.tsx`, `Search.tsx`,
    `Downloads.tsx`.
  - `src/components/*` for status/source badges, progress, install form,
    error display, and confirm dialog.
  - `src/lib/format.ts`.
  - `src/__tests__/*` Vitest + React Testing Library coverage.
- `.dockerignore` — excludes local dependencies, build output, cache noise,
  and git metadata from the Docker build context.
- `tests/test_ui_static.py` — admin-only UI serving, auth, SPA fallback, and
  traversal coverage.
- `tests/test_gpu_endpoint.py` — nvidia-smi parsing and unavailable states.

**Modified**

- `vllm_manager.py`
  - Adds `GET /manager/gpu`.
  - Adds admin-only `ui_router`:
    - `GET /` redirects to `/ui/`.
    - `GET /ui` and `/ui/` serve `index.html` if present, else 404.
    - `GET /ui/{full_path:path}` serves assets under the static root or
      falls back to `index.html` for internal SPA routes.
  - Does not include UI routes on `inference_app`.
- `Dockerfile`
  - Adds `node:22-alpine AS ui-builder`.
  - Runs `npm ci` and `npm run build` under `/ui`.
  - Copies `/ui/dist` into `/app/static` in the CUDA runtime stage.
- `.gitignore`
  - Adds `ui/node_modules/`, `ui/dist/`, and `ui/coverage/`.
- `project_docs/project_status.md`
  - Phase 6 status should be updated on landing. Phase 5 remains gated on the
    container-generated architecture snapshot.

## Backend Details

`GET /manager/gpu` returns:

```json
{
  "available": true,
  "gpus": [
    {
      "index": 0,
      "name": "NVIDIA RTX 6000 Ada",
      "memory_used_mb": 1024,
      "memory_total_mb": 49140,
      "utilization_pct": 12
    }
  ]
}
```

It shells out to:

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
```

If `nvidia-smi` is absent, times out, exits non-zero, or returns no parseable
rows, the endpoint returns `{"available": false, "gpus": []}` with HTTP 200.
This keeps macOS and non-GPU dev hosts usable.

Static serving resolves the root at request time:

```python
def _ui_static_root() -> Path:
    return Path(os.environ.get("MNEMOSYNE_UI_DIR", "/app/static")).resolve()
```

The catch-all route resolves `(root / full_path).resolve()` and returns 404
when the candidate is not the root and does not have the root in its parents.
Only contained files are served; contained missing paths fall back to
`index.html`.

## Frontend Details

- Stack: React 18, Vite 5, TypeScript 5, Tailwind 3, TanStack Query 5,
  react-router 6, lucide-react, Vitest.
- Router: `BrowserRouter basename="/ui"` with routes `/`, `/catalog`,
  `/search`, `/downloads`.
- API wrapper: `fetch(..., { credentials: "include" })`, JSON body helper,
  and `ApiError` carrying status and response text.
- Query keys:
  - `["status"]`
  - `["gpu"]`
  - `["catalog", { includeCacheOnly }]`
  - `["storage"]`
  - `["downloads"]`
  - `["install", alias]`
  - `["hf-search", q, includeVision, filterCompat]`

## Views

**Dashboard**

- Fetches `useStatus`, `useGpu`, and `useCatalog(false)`.
- Shows resident model, swap state, GPU plan/utilization cap, live GPU table,
  runtime detail, vLLM architecture source/count, and the resident alias's
  persisted catalog request count.
- Unload calls `POST /manager/unload`.

**Catalog**

- Fetches `GET /manager/catalog?include_cache_only=true`.
- Shows source/status badges, GPU plan, size, and request count.
- Cache-only detection uses alias prefix.
- Load is hidden for cache-only rows and disabled when the backend row is not
  loadable from the UI.
- Create alias opens the shared install form prefilled from the HF model ID.
- Destructive cache/row actions use a native confirm dialog.

**Search**

- Submits `/manager/hf/search` with `q`, `include_vision`, and
  `filter_compat`.
- Leaves incompatible results visible and disabled with their `compat_reason`.
- Install form posts `size_estimate_gb` from the search row and warns when the
  value is missing.

**Downloads**

- Lists `/manager/downloads`.
- Polls active details through `/manager/install/{alias}`.
- Shows progress from `bytes_downloaded` and `total_bytes`.
- Exposes cancel/retry/clear actions where backend status allows them.

## Verification

Local verification:

```bash
cd ui && npm install && npm run build && npm test
python3 -m pytest -q
python3 -m py_compile vllm_manager.py
```

Container smoke after build/start:

```bash
curl -u admin:$ADMIN_PASSWORD -I http://localhost:8001/ui/
curl -I http://localhost:8001/ui/
curl -I http://localhost:8000/ui/
curl -u admin:$ADMIN_PASSWORD -I http://localhost:8001/ui/catalog
```

Expected:

- Authenticated admin `/ui/` returns 200.
- Unauthenticated admin `/ui/` returns 401 when `ADMIN_PASSWORD` is set.
- Inference `/ui/` returns 404.
- Refreshing `/ui/catalog` serves the SPA.

## Remaining Gate

Phase 6 does not resolve the Phase 5 snapshot gate. The bundled
`vllm_supported_architectures.json` still needs to be regenerated inside the
pinned CUDA/vLLM container and committed before Phase 5 is marked complete.
