# Mnemosyne Inference — Project Status

**Last updated:** 2026-04-28

## Current state

**Active milestone:** M1 — Registry Server Core (phases 0–2)
**Active phase:** Phase 0 *complete, baseline gap fixed* → Phase 1 next.

The repo still serves the same single-file [vllm_manager.py](../vllm_manager.py) it
started with. Phase 0 added scaffolding (examples, pytest harness, smoke
checklist, pinned vLLM) and then corrected the download lifecycle baseline to
match current behavior, without changing runtime behavior. Phase 1 is the first
phase that actually edits `vllm_manager.py`.

## Phase status

| Phase | Goal | Status |
|---|---|---|
| 0 — Foundation & safety rails | Examples, test harness, vLLM pin | ✅ Done; download baseline fixed (2026-04-28) |
| 1 — Config, profiles, storage, catalog core | Replace env-var loading with declarative YAML + SQLite catalog | ⏭ Next |
| 2 — Runtime lifecycle, lazy load, queue, idle eviction | Profile-driven `_start_vllm`, swap queue, idle eviction | ⏳ Pending |
| 3 — Plane separation & auth | Two FastAPI apps (inference :8000, admin :8001), HTTP Basic | ⏳ Pending |
| 4 — Install, download, cache, multi-drive | `/manager/install` + cancellable subprocess downloads | ⏳ Pending |
| 5 — HF search & vLLM compatibility filter | `GET /manager/hf/search`, runtime registry introspection | ⏳ Pending |
| 6 — Admin UI | React + Vite SPA on the admin port | ⏳ Pending |
| 7 — Packaging, compose, docs | Multi-stage Dockerfile, compose mounts, ops docs | ⏳ Pending |
| 8 — Verification & hardening | PRD acceptance scenarios | ⏳ Pending |

## Phase 0 — what landed

**Modified**

- [Dockerfile](../Dockerfile) lines 38-46 — vLLM pinned to
  `0.20.1rc1.dev10+g2c8b76c5c` (commit `2c8b76c5c`, latest on the nightly
  index as of 2026-04-27). Inline refresh recipe documented.

**New files**

- [config.yaml.example](../config.yaml.example) — full PRD §5.1 schema with three illustrative aliases.
- [.env.example](../.env.example) — `ADMIN_PASSWORD`, `HUGGING_FACE_HUB_TOKEN`, `INFERENCE_API_KEY`.
- [vllm_supported_architectures.json](../vllm_supported_architectures.json) — placeholder; Phase 5 populates.
- [requirements-dev.txt](../requirements-dev.txt), [pytest.ini](../pytest.ini) — host-runnable test deps + pytest config.
- [tests/conftest.py](../tests/conftest.py), [tests/test_smoke.py](../tests/test_smoke.py) — 8 tests, including a no-network `/manager/download` enqueue contract test.
- [smoke_checks.md](smoke_checks.md) — manual checklist for routes that need GPU + container; download lifecycle now documents current `200 {"status":"started"}` behavior.
- [plans/phase_0.md](plans/phase_0.md) — approved Phase 0 plan, archived.

**Verified**

- Host: `python -m pytest -v` → 8/8 pass on macOS without CUDA.
- Host: `python -c "import vllm_manager; print('import ok')"` → import ok.
- Workstation: smoke checklist is the next manual run; not yet executed against the pinned vLLM build.

## Phase 1 — what's next

Goal: replace env-var-driven model loading with a declarative registry,
without changing runtime serving behavior yet.

Deliverables (per [implementation_plan.md §Phase 1](implementation_plan.md)):

- `config.py` — load + validate `/config/config.yaml` (pydantic), probe GPU indices, validate storage paths.
- `.env` loader at `/config/.env`.
- `catalog.py` — SQLite at `/state/mnemosyne.db`, `models` + `downloads` tables, sync config aliases as `source='config'`.
- Reload path: SIGHUP + `POST /manager/reload`.
- Status/list endpoints surface configured aliases, storage locations, free space, catalog rows.

Exit criteria: editing `config.yaml` + reload makes new aliases visible
without a container restart; config + UI-installed aliases merge with
config taking precedence; alias → runtime profile resolution works.

## Open follow-ups

- **vLLM pin staleness.** The nightly index only retains the latest commit's
  wheels. The current pin (`g2c8b76c5c`) becomes unfetchable as soon as a
  new commit lands. Refresh proactively before the next workstation rebuild.
- **Workstation smoke run.** [smoke_checks.md](smoke_checks.md) has not yet
  been walked end-to-end against the pinned vLLM build — do this before
  Phase 1 starts editing `vllm_manager.py`.
- **External `docker-compose.yml`.** Lives outside the repo at
  `~/vllm-manager/`. Phase 7 documents the new mounts (`/config`, `/state`,
  storage drives); for now, no compose change is required.

## Quick links

- [PRD](PRD.md) — product requirements; see §9 for the decision log.
- [Implementation plan](implementation_plan.md) — phase breakdown and milestones.
- [Phase 0 plan](plans/phase_0.md) — what shipped in Phase 0.
- [Smoke checks](smoke_checks.md) — manual baseline checklist.
