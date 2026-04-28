# Mnemosyne Inference — Project Status

**Last updated:** 2026-04-28

## Current state

**Active milestone:** M2 — Safe Admin Boundary  
**Active phase:** Phase 3 next.

M1 is implemented: the manager now has the config/catalog/profile substrate
from Phase 1 and the profile-driven runtime lifecycle from Phase 2. The
runtime still uses one FastAPI app and the legacy single listener; inference
and admin plane separation remains Phase 3.

## Phase status

| Phase | Goal | Status |
|---|---|---|
| 0 — Foundation & safety rails | Examples, test harness, vLLM pin | ✅ Done; download baseline fixed (2026-04-28) |
| 1 — Config, profiles, storage, catalog core | Declarative YAML config + SQLite catalog + profile resolver | ✅ Done (2026-04-28) |
| 2 — Runtime lifecycle, lazy load, queue, idle eviction | Profile-driven `_start_vllm`, swap queue, idle eviction | ✅ Done (2026-04-28) |
| 3 — Plane separation & auth | Two FastAPI apps (inference :8000, admin :8001), HTTP Basic | ⏭ Next |
| 4 — Install, download, cache, multi-drive | `/manager/install` + cancellable subprocess downloads | ⏳ Pending |
| 5 — HF search & vLLM compatibility filter | `GET /manager/hf/search`, runtime registry introspection | ⏳ Pending |
| 6 — Admin UI | React + Vite SPA on the admin port | ⏳ Pending |
| 7 — Packaging, compose, docs | Multi-stage Dockerfile, compose mounts, ops docs | ⏳ Pending |
| 8 — Verification & hardening | PRD acceptance scenarios | ⏳ Pending |

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
- Archived plan: [plans/phase_1_plan.md](plans/phase_1_plan.md).

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

## Verification

Latest host verification on macOS, no CUDA required:

- `python -m pytest -q` → `133 passed`
- `python -m py_compile vllm_manager.py runtime.py config.py catalog.py profiles.py`
- `bash -n vllm-ctl`

Workstation/GPU smoke validation is still outstanding:

- Rebuild/start the container.
- Load a configured alias and confirm argv/env behavior in logs.
- Swap through `/v1/chat/completions`.
- Run same-target concurrent request smoke.
- Confirm idle eviction and catalog usage increments.

## Open follow-ups

- **Phase 3 security boundary.** Split inference/admin listeners before adding
  more mutating admin routes.
- **Inner/admin port collision.** The inner vLLM server still defaults to
  loopback `8001`, which conflicts with the future `admin_port=8001`; Phase 3
  must move or isolate that binding.
- **External `docker-compose.yml`.** Lives outside the repo at
  `~/vllm-manager/`. Phase 7 documents final mounts and exposed ports.
- **vLLM pin staleness.** Refresh the pinned nightly before the next workstation
  rebuild if the nightly index has moved.

## Quick links

- [PRD](PRD.md) — product requirements and decision log.
- [Implementation plan](implementation_plan.md) — phase breakdown and milestones.
- [Phase 0 plan](plans/phase_0.md)
- [Phase 1 plan](plans/phase_1_plan.md)
- [Phase 2 plan](plans/phase_2.md)
- [Smoke checks](smoke_checks.md)
