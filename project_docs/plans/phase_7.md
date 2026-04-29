# Phase 7 — Packaging, Compose, and Operational Docs

## Context

[project_docs/implementation_plan.md](../implementation_plan.md)
Phase 7 wraps the v1 build by making the system easy to install, configure, and
operate on the target workstation. Phases 0–6 already shipped most of the
packaging artifacts inline — this phase is mostly verification + operational
documentation.

Survey results (verified by reading the files at the time of planning):

| Artifact | State | Source of truth |
|---|---|---|
| Multi-stage Dockerfile (Node 22 → CUDA) building `ui/dist` and copying to `/app/static` | ✅ Done | [Dockerfile](../../Dockerfile) lines 4–10, 70–74 |
| vLLM pinned to a specific nightly | ✅ Done | Dockerfile line 56 |
| `pyyaml`, `pydantic` installed, all new modules + `scripts/` + arch JSON COPYed | ✅ Done | Dockerfile lines 60–73 |
| Both ports EXPOSEd | ✅ Done | Dockerfile line 89 |
| Compose mounts `/config`, `/state`, HF cache volume, both ports, env_file | ✅ Done | [docker-compose.example.yml](../../docker-compose.example.yml) lines 22–47 |
| Healthcheck on `/health` | ✅ Done | docker-compose.example.yml lines 54–59 |
| Multi-drive storage example (commented) | ⚠️ Needs README/operator caveat | docker-compose.example.yml lines 49–52; `config.yaml.example` defaults to the commented storage paths |
| `vllm-ctl` help text covers `list`, `models`, `reload`, `storage`, `install*`, `cache-delete`, `downloads`, env vars, gated-token note | ⚠️ Mostly done; needs drift fixes | [vllm-ctl](../../vllm-ctl) lines 717–795 |
| `.env.example`, `config.yaml.example` | ✅ Done | Repo root |
| User-facing README / setup runbook | ❌ Missing | — no `README.md` at repo root |
| Architecture-list refresh script | ✅ Done | [scripts/refresh_arch_list.py](../../scripts/refresh_arch_list.py) |

The Phase 7 deliverable is therefore: (1) write the operational README,
(2) fix small packaging/help drift found during review, and (3) verify the
existing artifacts behave the way the README describes.

## Approach

Single `README.md` at the repo root, structured as a quickstart + topical
runbooks. Justification: `project_docs/` is design / working docs; user-facing
ops content should be the first thing a visitor to the repo sees, not buried.
One file is preferable to a `docs/` tree at this scale — every required topic
fits comfortably under H2 headings.

Only small packaging/CLI/documentation changes are planned. The CLAUDE.md note
that the live compose file may be outside this repo is preserved verbatim in
the README.

## Files

**New**

- `README.md` (repo root) — sections enumerated below.

**Modified (small edits only, if anything)**

- `docker-compose.example.yml` — verify the storage-location comment block is
  actually un-commentable as-is and references the same names used in
  `config.yaml.example` (`nvme-fast`, `archive`). If the default
  `config.yaml.example` still points at commented storage mounts, either make
  the examples runnable by default or document that the operator must uncomment
  / adapt the storage mounts before installing models.
- `vllm-ctl` help text and env handling — fix review drift:
  - `VLLM_MANAGER_URL` should remain a backward-compatible admin URL fallback,
    not only an inference URL fallback.
  - HF token guidance should point users at `${COMPOSE_DIR}/.env`, not editing
    `docker-compose.yml` environment directly.
  - The top usage block should mention `list`, `reload`, `storage`, installs,
    and cache commands so it does not contradict `cmd_help`.
- `Dockerfile` — only if a verification pass turns up a missing COPY (e.g. a
  module added in Phase 5/6 that isn't in line 70–74). Spot-checked: all
  current Python modules listed in `CLAUDE.md` are present.

**Not changed**

- `.env.example`, `config.yaml.example` — already current.
- `scripts/refresh_arch_list.py` — already documented inside the file.
- `project_docs/*` — design docs stay design docs.

## README structure

Single file, ~400–600 lines, GitHub-flavored markdown. Sections:

1. **What this is** — one paragraph: Ollama/LMStudio ergonomics on top of
   vLLM, single-workstation, two-plane (inference / admin), config-driven.
   Link to [project_docs/PRD.md](project_docs/PRD.md)
   for design context. State hardware target up front (CUDA 12.8+, Blackwell-class GPU).

2. **Quickstart** — copy-paste path from a clean machine to a working alias:
   1. Clone the repo.
   2. `mkdir -p ~/vllm-manager && cd ~/vllm-manager`.
   3. Copy `docker-compose.example.yml`, `config.yaml.example`, `.env.example`
      out of the repo into `~/vllm-manager/` (rename to drop `.example`).
   4. Set `MNEMOSYNE_REPO_DIR` in the shell and set `ADMIN_PASSWORD` inside
      `~/vllm-manager/.env` before the first start. Exporting
      `ADMIN_PASSWORD` only in the shell helps the CLI but does not configure
      the container.
   5. `vllm-ctl build && vllm-ctl start`.
   6. `curl http://localhost:8000/health`; visit `http://localhost:8001/ui` with
      `admin:$ADMIN_PASSWORD`.
   Frame as the canonical "fresh setup from examples" flow that the Phase 7
   exit criterion calls out.

3. **Configuration** — `config.yaml` walkthrough: aliases, `gpus: all` vs
   `[0]`, `quantization`, `storage`, `extra_args`, `defaults` block.
   Mention SIGHUP / `vllm-ctl reload` / `POST /manager/reload`. Link to
   `config.yaml.example` rather than duplicating it.

4. **Adding a drive** — the host-level workflow that PRD §5.12 declares as
   config-only:
   1. Bind-mount the host path under the container (uncomment / adapt the
      lines in `docker-compose.yml`).
   2. Add a matching `storage.locations[].name` + `path` in `config.yaml`.
   3. `docker compose up -d` (re-create) — drive mounts cannot be added by
      `vllm-ctl reload`.
   4. Verify with `vllm-ctl storage`.
   Show the exact compose snippet and the exact YAML snippet, side by side.
   Also call out that the shipped `config.yaml.example` uses `nvme-fast` and
   `archive`; if those mounts remain commented, installs to those locations
   will fail until the operator edits compose or config.

5. **Installing gated models** — the HF token flow:
   1. Generate a token at huggingface.co with `read` scope on the gated repo.
   2. Add `HUGGING_FACE_HUB_TOKEN=hf_…` to `~/vllm-manager/.env`.
   3. `vllm-ctl restart` (env is read once at process start; no live reload).
   4. Install via UI or `vllm-ctl install`. If the token is missing, the
      install errors with a clear "set HUGGING_FACE_HUB_TOKEN in .env and
      restart" message — call this out so users know what to expect.

6. **Recovering partial downloads** — covers the "manager restart during
   download" scenario from Phase 8 acceptance:
   - On startup the catalog reconciles missing cache files as `partial`.
   - In the UI, `partial` rows show **Retry**; `vllm-ctl install-retry <alias>`
     does the same from the CLI.
   - For a corrupt cache, `vllm-ctl install-retry <alias> --force` wipes
     the cache directory and re-spawns the worker.
   - Cache-deleting an aliased install demotes the row to `partial` rather
     than removing the alias (PRD §9, 2026-04-27 decision) — the user can
     recover with one click.

7. **Refreshing architecture support after a vLLM upgrade** — the maintenance
   task that lets us stay forward-compatible:
   1. Bump the pinned vLLM version in [Dockerfile](Dockerfile)
      line 56 (the existing comment block lists how to find a fresh tag).
   2. `vllm-ctl build && vllm-ctl restart`.
   3. Inside the container:
      `docker exec vllm-manager python /app/scripts/refresh_arch_list.py /tmp/vllm_supported_architectures.json`.
   4. Copy the generated file back to the checkout:
      `docker cp vllm-manager:/tmp/vllm_supported_architectures.json <repo>/vllm_supported_architectures.json`.
   5. Diff the regenerated `vllm_supported_architectures.json`; commit if
      changed.
   6. `vllm-ctl restart` so the runtime picks up the new bundled list.

8. **Security and LAN exposure** — translate PRD §5.10 / §5.13 into
   user-actionable rules:
   - Always set `ADMIN_PASSWORD`. With it unset, the admin port falls back to
     loopback inside the container and the published `:8001` is unreachable
     from the host (this is intentional fail-safe behavior).
   - To restrict admin to the Docker host only, use the
     `127.0.0.1:8001:8001` form already commented in the compose example.
   - Set `INFERENCE_API_KEY` if other people share your LAN. Without it,
     `/v1/*` is open to any device that can reach the inference port.
   - The `.env` file is gitignored. Don't commit it.
   - Firewall the admin port at the router for further defense if desired.

9. **Common operations cheat-sheet** — short table of `vllm-ctl` commands,
   grouped (Container, Models, Installs, Downloads, Diagnostics). Mirror the
   `vllm-ctl help` ordering so the two stay in sync visually.

10. **Troubleshooting** — concise FAQ:
    - `:8001` refuses connections → `ADMIN_PASSWORD` unset.
    - Install fails with HF 401/403 → gated repo without token.
    - `vllm-ctl status` shows no resident model → expected; first `/v1/*`
      request triggers lazy load.
    - `504` from `/v1/*` during a swap → `swap_queue_timeout_seconds` was hit.
    - `503` from `/v1/*` mid-load → vLLM child died; next request retriggers
      a fresh load (no auto-restart by design — PRD §5.3).

Closing pointers to: `agents.md`, `CLAUDE.md`, `project_docs/PRD.md`,
`project_docs/implementation_plan.md` for contributors.

## Verification

End-to-end check that the README's Quickstart actually works as written:

1. **Static checks (do these first):**
   - `python -m py_compile vllm_manager.py runtime.py config.py catalog.py profiles.py downloader.py download_worker.py hf_search.py` — no regressions to module surface.
   - `bash -n vllm-ctl` — shell syntax.
   - `python -m pytest -q` — committed test suite still green.

2. **Quickstart dry run** — on a CUDA host (cannot run from macOS dev env):
   ```bash
   mkdir -p ~/vllm-manager-test && cd ~/vllm-manager-test
   cp <repo>/docker-compose.example.yml docker-compose.yml
   cp <repo>/config.yaml.example       config.yaml
   cp <repo>/.env.example              .env
   # edit .env — set ADMIN_PASSWORD
   export MNEMOSYNE_REPO_DIR=<repo>
   <repo>/vllm-ctl build
   <repo>/vllm-ctl start
   ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' .env | head -1 | cut -d= -f2-)"
   curl -fsS http://localhost:8000/health
   curl -fsS -u admin:"$ADMIN_PASSWORD" http://localhost:8001/manager/status
   curl -fsS -u admin:"$ADMIN_PASSWORD" http://localhost:8001/ui/ >/dev/null
   <repo>/vllm-ctl stop
   ```
   This is the literal Phase 7 exit criterion ("fresh setup from examples can
   start the container and reach both ports"). If any step fails, fix the
   underlying artifact (compose, Dockerfile, or README) — not the test.

3. **CLI help drift check** — diff `vllm-ctl help` output against the README
   "Common operations" table; reconcile any mismatch. Explicitly verify:
   `VLLM_ADMIN_URL` defaults to `http://localhost:8001`,
   legacy `VLLM_MANAGER_URL` can still steer admin commands when
   `VLLM_ADMIN_URL` is unset, and `VLLM_INFERENCE_URL` is the preferred way to
   override `/v1/*` chat requests.

4. **Compose validity** — `docker compose -f docker-compose.example.yml config`
   on a host with Docker installed; should parse without error.

5. **No new test files required** — Phase 7 is mostly docs plus small
   packaging/CLI drift fixes. The Phase 8 acceptance pass will exercise the
   runtime behaviors the README describes.

## Out of scope

- Adding a `README.md` to `ui/` — not a Phase 7 ask; the SPA is served from
  `/ui` and documented from the top-level README.
- Adding a `CONTRIBUTING.md` — defer until there are external contributors.
- Producing pre-built images / a registry push — Phase 7 stays at "build it
  yourself with `vllm-ctl build`".
- Prometheus / metrics docs — stretch goal per PRD §8.
- Anything in [Phase 8](../implementation_plan.md)
  (acceptance scenarios, multimodal verification, structured-log polish).
