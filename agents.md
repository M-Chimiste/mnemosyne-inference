# Agents Guide

This repository contains **Mnemosyne Inference**, a containerized vLLM manager for a single workstation. Agents working here should preserve the thin-wrapper approach: the manager supervises and proxies to vLLM, but does not fork or modify vLLM itself.

## Repository Shape

- `vllm_manager.py` is the current FastAPI service. It launches `vllm.entrypoints.openai.api_server` as a subprocess, proxies `/v1/*`, and exposes manager endpoints.
- `vllm-ctl` is the Bash CLI for Docker lifecycle, manager API calls, model loading, downloads, status, logs, and one-shot chat.
- `Dockerfile` defines the CUDA/Python/vLLM runtime. There is no `requirements.txt` or `pyproject.toml` yet.
- `project_docs/PRD.md` is the product requirements document for Mnemosyne Inference v1.
- `project_docs/implementation_plan.md` is the phased implementation plan derived from the PRD.
- `CLAUDE.md` contains Claude-specific repository guidance. Keep it in sync with this file when architecture or common commands change.

`docker-compose.yml` is intentionally outside this repo. The CLI expects it under `$VLLM_COMPOSE_DIR`, defaulting to `~/vllm-manager`. If a change affects ports, env vars, volumes, container names, or mounts, call out the required external compose changes.

## Current Architecture

The current implementation is still prototype-shaped:

- One FastAPI app listens on port `8000`.
- The inner vLLM OpenAI-compatible server listens on loopback port `8001`.
- `/v1/*` requests are proxied to the inner vLLM server.
- `/manager/*` endpoints handle load, unload, status, aliases, downloads, and cache listing.
- Only one vLLM subprocess/model is resident at a time.
- Switching models means killing the existing subprocess and launching a new one.
- Downloads use a background OS thread and an in-memory `_downloads` dictionary.
- Download state, aliases, and usage data are not persistent yet.
- Configuration is currently environment-variable driven.

## Target Direction

The v1 PRD and implementation plan move the project toward:

- YAML config at `/config/config.yaml`.
- Secrets in `/config/.env`.
- SQLite catalog at `/state/mnemosyne.db`.
- Per-model aliases, quantization, GPU plans, max context, storage location, and extra vLLM args.
- Lazy loading with queued model swaps and idle eviction.
- Two listeners in one process:
  - Inference plane: `/v1/*` and `/health`.
  - Admin plane: `/manager/*`, `/ui/*`, and admin-only operations.
- Admin auth via HTTP Basic.
- Optional inference bearer key.
- Multi-drive storage via per-launch `HF_HOME`.
- HuggingFace search with vLLM architecture compatibility filtering.
- React/Vite/TypeScript/Tailwind admin UI served from the admin port.

When implementing changes, use `project_docs/implementation_plan.md` for ordering unless the user explicitly directs otherwise.

## Development Constraints

- Prefer small, staged changes that preserve working runtime behavior.
- Do not introduce a vLLM fork or custom serving engine.
- Keep OpenAI-compatible request bodies as pass-through as possible, especially for multimodal content.
- Maintain backward-compatible shims where the PRD requires them:
  - `POST /manager/load`.
  - `POST /manager/download`.
  - Existing CLI workflows.
- Treat the external compose file as user-managed. Do not assume it can be edited from this repo.
- Avoid adding broad dependencies unless they are called for by the PRD or implementation plan.
- If adding Python modules, keep boundaries clear:
  - `config.py` for YAML/env loading and validation.
  - `catalog.py` for SQLite.
  - `downloader.py` for install/download subprocesses.
  - `hf_search.py` for HuggingFace discovery and compatibility checks.
  - `vllm_manager.py` for app wiring, proxying, and vLLM process lifecycle.

## Common Commands

Most real workflows happen through Docker because vLLM/CUDA cannot run directly on typical macOS development hosts.

```bash
./vllm-ctl build
./vllm-ctl start
./vllm-ctl stop
./vllm-ctl restart
./vllm-ctl status
./vllm-ctl logs -f
```

Model and cache operations:

```bash
./vllm-ctl load Qwen/Qwen2.5-72B-Instruct-AWQ --tp 2 --gpu-mem 0.90
./vllm-ctl load Qwen/Qwen3-8B -- --max-model-len 32768
./vllm-ctl unload
./vllm-ctl models
./vllm-ctl download <model-id>
./vllm-ctl download-status <model-id>
./vllm-ctl downloads
```

Direct API examples:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/manager/status
curl -X POST http://localhost:8000/manager/load \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-72B-Instruct-AWQ","tp":2}'
```

There is currently no committed test suite, linter config, or formatter config. Do not claim tests exist unless you add them or verify them in the repo.

## Verification Expectations

For docs-only changes, a readback or diff check is enough.

For Python or CLI changes, prefer at least:

- Syntax validation for edited Python files.
- Shell syntax validation for `vllm-ctl`.
- Targeted route or function tests if a test harness exists or is being introduced.
- Manual smoke checks in Docker when the change affects process launch, ports, or vLLM command construction.

For future UI changes:

- Run the Vite build/check commands once the UI project exists.
- Verify key screens in a browser when changing layout or interaction behavior.

## Safety Notes

- Never discard user changes in this repository.
- Be careful with cache deletion code. Refuse to delete a model that is currently resident.
- Keep admin-only mutation endpoints off the inference plane.
- If `ADMIN_PASSWORD` is unset in the target design, admin bind must fail safe to loopback.
- Do not store secrets in `config.yaml`, committed examples, logs, or UI state.
- Preserve `extra_args` as the escape hatch for new vLLM flags.

## Useful Reading Order

1. `project_docs/PRD.md`
2. `project_docs/implementation_plan.md`
3. `CLAUDE.md`
4. `vllm_manager.py`
5. `vllm-ctl`
6. `Dockerfile`
