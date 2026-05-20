# Mnemosyne Inference

## What this is

Mnemosyne Inference gives a single workstation Ollama/LMStudio-style ergonomics
on top of [vLLM](https://docs.vllm.ai/): one container supervises a vLLM
subprocess, exposes an OpenAI-compatible API, and keeps the rest of the
machine — installs, model swaps, multi-drive storage — driven by a YAML config
and a small CLI/UI.

The container runs **two HTTP planes** in one process:

- **Inference** on `:8000` — `/v1/*` (OpenAI-compatible) and `/health`.
- **Admin** on `:8001` — `/manager/*`, `/ui/`, `/docs`, plus admin-only `/v1/*`.

There is **one model resident at a time**. `/v1/*` requests trigger a lazy
load if the requested model is not the resident one; concurrent callers for
the same target piggyback on a single load.

Hardware target: a CUDA 12.8+ host with a Blackwell-class NVIDIA GPU (RTX 50
or workstation Blackwell). The image bakes in PyTorch cu129 and a pinned vLLM
stable release. Ampere/Hopper cards generally also work — see [Refreshing
architecture support](#refreshing-architecture-support) if you need to bump or
change the pinned vLLM build.

Design context: [project_docs/PRD.md](project_docs/PRD.md) and
[project_docs/implementation_plan.md](project_docs/implementation_plan.md).

## Quickstart

This is the canonical "fresh machine to a working API" flow. It assumes
Docker + the NVIDIA container toolkit are already installed on the host.

```bash
# 1. Clone the repo somewhere stable.
git clone https://github.com/<your-fork>/mnemosyne-inference.git ~/src/mnemosyne-inference

# 2. Create a workstation-specific compose dir alongside the checkout.
mkdir -p ~/vllm-manager && cd ~/vllm-manager

# 3. Copy the three example files out of the repo and drop the .example suffix.
cp ~/src/mnemosyne-inference/docker-compose.example.yml docker-compose.yml
cp ~/src/mnemosyne-inference/config.yaml.example       config.yaml
cp ~/src/mnemosyne-inference/.env.example              .env

# 4. Tell the compose file where the Dockerfile lives, and set ADMIN_PASSWORD.
export MNEMOSYNE_REPO_DIR=~/src/mnemosyne-inference
$EDITOR .env             # set ADMIN_PASSWORD=...   (and HUGGING_FACE_HUB_TOKEN if needed)
ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' .env | head -1 | cut -d= -f2-)"

# 5. Build and start. First build pulls CUDA/PyTorch/vLLM and takes several minutes.
~/src/mnemosyne-inference/vllm-ctl build
~/src/mnemosyne-inference/vllm-ctl start

# 6. Smoke test.
curl http://localhost:8000/health
# Then open http://localhost:8001/ui/ in a browser, log in as `admin` with the
# password you put in .env.
```

A few things to know about this flow:

- **`MNEMOSYNE_REPO_DIR`** is read by `docker-compose.yml`'s `build.context`.
  Without it set, `docker compose build` looks for the Dockerfile next to
  `docker-compose.yml` and fails. Persist it in your shell profile or in a
  `.envrc` if you use direnv.
- **`ADMIN_PASSWORD` must be in `~/vllm-manager/.env`**, not just exported in
  your shell. `vllm-ctl` reads it out of `.env` automatically for host-side
  admin commands, but only the file copy is mounted into the container. With
  `ADMIN_PASSWORD` unset, the admin app intentionally binds to `127.0.0.1`
  inside the container and the published `:8001` is unreachable from the host
  — see [Security and LAN exposure](#security-and-lan-exposure).
- **No models are installed yet.** Use the UI's *Search* tab or
  `vllm-ctl install <alias> <model>` (see [Installing gated
  models](#installing-gated-models)). The first `/v1/*` request after install
  triggers the actual GPU load.

Add `~/src/mnemosyne-inference/vllm-ctl` to your `PATH` (or symlink it into
`/usr/local/bin`) so you can drop the long path in subsequent sections.

## Configuration

Runtime configuration lives in `~/vllm-manager/config.yaml`, which is
bind-mounted read-only at `/config/config.yaml`. The shipped
[config.yaml.example](config.yaml.example) is a complete annotated reference;
this section is the reading guide.

Top-level blocks:

- **`server`** — listener ports/binds, idle eviction, swap timeouts.
  - `idle_unload_seconds: 900` evicts the resident model after 15 min of
    inactivity. Set `null` to keep the model pinned until the container stops
    or another model is loaded.
  - `swap_queue_timeout_seconds: 300` is the longest a `/v1/*` caller will
    wait while another model is loading before getting a `504`.
- **`storage`** — named locations for HF caches. `default:` is the location
  used when an install does not pick one explicitly. Each entry's `path` is a
  container path that must be backed by a host bind-mount (see [Adding a
  drive](#adding-a-drive)).
- **`defaults`** — fallback values applied to any model entry that does not
  override them: `gpu_memory_utilization`, `trust_remote_code`,
  `max_model_len`.
- **`models`** — list of profiles. Each profile is a stable alias plus the HF
  model id and per-model knobs: `gpus` (`all`, `[0]`, `[0,1]`),
  `quantization`, `max_model_len`, `storage`, and `extra_args` for raw vLLM
  flags appended verbatim.

`config.yaml` is hot-reloadable. Three equivalent triggers:

```bash
vllm-ctl reload                                            # CLI
docker kill --signal=SIGHUP vllm-manager                   # signal
ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' ~/vllm-manager/.env | head -1 | cut -d= -f2-)"
curl -u admin:"$ADMIN_PASSWORD" -X POST \
  http://localhost:8001/manager/reload                     # HTTP
```

A reload re-syncs config rows into the catalog and reconciles cache state. It
does **not** add storage mounts or change container ports — those are docker
properties. For those, edit `docker-compose.yml` and run
`docker compose up -d` (re-create).

## Adding a drive

PRD §5.12 declares multi-drive storage as config-only — no code changes — but
adding a *new* drive is a host-level workflow because Docker has to learn
about the bind-mount. The flow:

1. **Bind-mount the host path under the container** (`docker-compose.yml`):
   ```yaml
   services:
     vllm-manager:
       volumes:
         - ./config.yaml:/config/config.yaml:ro
         - ./.env:/config/.env:ro
         - ./state:/state
         - hf-cache:/hf-cache
         - /mnt/big-nvme:/storage/nvme/hf-cache    # ← new line
   ```

2. **Add a matching entry to `config.yaml`** under `storage.locations`:
   ```yaml
   storage:
     default: nvme-fast
     locations:
       - name: nvme-fast
         path: /storage/nvme/hf-cache              # ← container path
       - name: archive
         path: /storage/raid/hf-cache
   ```
   The `path:` here is the container-side path (right-hand side of the bind
   mount). The `name:` is what you'll pass to `vllm-ctl install --storage`.

3. **Re-create the container**, since `docker compose restart` will not pick
   up a new mount:
   ```bash
   cd ~/vllm-manager && docker compose up -d
   ```
   `vllm-ctl reload` is *not* enough here.

4. **Verify**:
   ```bash
   vllm-ctl storage
   ```
   You should see the new location with non-zero free space, no `READONLY`
   flag, and the right `default` flag if applicable.

> **Note about the shipped example config.** `config.yaml.example` already
> declares `nvme-fast` (`/storage/nvme/hf-cache`) and `archive`
> (`/storage/raid/hf-cache`). The matching mount lines in
> `docker-compose.example.yml` are commented out. If you copied the examples
> verbatim and skipped step 1 above, installs that target those locations
> will fail with a "location path missing" error. Either uncomment and adapt
> the two `# - /mnt/.../hf-cache:/storage/...` lines in
> `docker-compose.yml`, or trim `storage.locations` in `config.yaml` to only
> the locations you actually mount.

## Installing gated models

Some HuggingFace repos (Llama, some Gemma variants, etc.) require
authentication. The token flow:

1. **Generate a token** at <https://huggingface.co/settings/tokens> with
   `read` scope. The token only needs access to the specific gated repos
   you'll be installing.
2. **Add it to `~/vllm-manager/.env`**:
   ```
   HUGGING_FACE_HUB_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   Do **not** edit `docker-compose.yml`'s `environment:` block directly —
   `.env` is the supported location and is gitignored.
3. **Restart** so the install worker inherits the token from the manager
   environment:
   ```bash
   vllm-ctl restart
   ```
   `.env` is read once at process start; there's no live reload for secrets.
4. **Install**, either through the UI's *Search* tab or:
   ```bash
   vllm-ctl install llama-3-8b meta-llama/Meta-Llama-3-8B-Instruct \
     --storage nvme-fast
   ```

If the token is missing or wrong, the install fails with HTTP `401`/`403` from
HuggingFace, surfaced in the install row and the UI as an error message.
Set `HUGGING_FACE_HUB_TOKEN` in `.env`, restart the container, then
`vllm-ctl install-retry <alias>`.

## Recovering partial downloads

The catalog persists install state across container restarts, so a download
interrupted by `vllm-ctl restart`, a host reboot, or a crash does not have to
start over from scratch.

- **On startup**, the manager runs cache reconcile against every aliased
  install. Any row whose cache directory is missing or incomplete is moved to
  `partial` status. `vllm-ctl reload` runs the same reconcile.
- **In the UI**, `partial` rows show a **Retry** button. The CLI equivalent
  is `vllm-ctl install-retry <alias>` — it re-spawns the worker against the
  same alias / model / revision and resumes from whatever shards are already
  on disk.
- **For a corrupt cache** (rare; usually a partial download that left
  inconsistent shards), pass `--force`:
  ```bash
  vllm-ctl install-retry <alias> --force
  ```
  This wipes the cache directory before re-spawning the worker.
- **`cache-delete` on an aliased install demotes the row to `partial`**
  rather than removing the alias (PRD §9, 2026-04-27 decision). The
  user-visible effect is that the row stays in the catalog with a Retry
  button — one click to recover. To remove the alias entirely, pass
  `--remove-row`:
  ```bash
  vllm-ctl cache-delete --alias <alias> --remove-row
  ```

## Refreshing architecture support

The UI's HuggingFace Search view filters search results by the architectures
the bundled vLLM build can actually serve. The list lives in
[vllm_supported_architectures.json](vllm_supported_architectures.json) and is
generated from the running vLLM's registry. After bumping vLLM you should
regenerate it; otherwise the UI will under- or over-filter against new model
families.

The maintenance workflow:

1. **Bump the pinned vLLM** in [Dockerfile](Dockerfile) line 56 after checking
   the upstream release notes and install guidance. Update the `Last
   refreshed:` comment.
2. **Rebuild and restart** so the new vLLM is loaded:
   ```bash
   vllm-ctl build && vllm-ctl restart
   ```
3. **Regenerate the architecture list inside the container** (the script
   imports vLLM, which can only run on the CUDA host):
   ```bash
   docker exec vllm-manager \
     python /app/scripts/refresh_arch_list.py /tmp/vllm_supported_architectures.json
   ```
4. **Copy the file back to the checkout**:
   ```bash
   docker cp \
     vllm-manager:/tmp/vllm_supported_architectures.json \
     ~/src/mnemosyne-inference/vllm_supported_architectures.json
   ```
5. **Diff and commit** if the JSON changed.
6. **Restart the container** so the runtime picks up the new bundled list:
   ```bash
   vllm-ctl restart
   ```

## Security and LAN exposure

This is a single-workstation tool, but the published ports reach the LAN
unless you say otherwise. Translate PRD §5.10 / §5.13 into rules:

- **Always set `ADMIN_PASSWORD`** in `~/vllm-manager/.env` before exposing
  `:8001`. With `ADMIN_PASSWORD` unset, the admin app binds to `127.0.0.1`
  inside the container — the published `:8001` is unreachable from the host
  in that state. This is intentional fail-safe behavior, not a bug.
- **To restrict admin to the Docker host only**, change the published port
  in `docker-compose.yml`:
  ```yaml
  ports:
    - "8000:8000"
    - "127.0.0.1:8001:8001"     # admin only on host loopback
  ```
- **Set `INFERENCE_API_KEY`** if other people share your LAN. Without it,
  `/v1/*` is open to any device that can reach `:8000`. With it, callers
  must send `Authorization: Bearer <key>`.
- **`.env` is gitignored.** Don't commit it. Only the `.env.example`
  template is checked in.
- **For belt-and-suspenders**, firewall both ports at the router for hosts
  outside your subnet.

## Token usage telemetry

Every successful `/v1/{chat/completions,completions,embeddings}` call is
accounted for locally in the SQLite `request_usage` table — open
`/manager/status` or query `~/vllm-manager/state/mnemosyne.db` directly.

For multi-node fleets, the manager can additionally forward every row to a
central Postgres ledger (a "token sidecar"). The local SQLite is the
durable cache: a Postgres outage or container restart never drops data.

1. **Apply the schema once** on the central host. The writer role is
   intentionally privilege-limited and cannot DDL — apply this as a
   superuser:
   ```sql
   CREATE TABLE public.token_usage (
       event_id          text                        PRIMARY KEY,
       timestamp         timestamp with time zone    NOT NULL,
       node_id           text                        NOT NULL,
       model             text                        NOT NULL,
       prompt_tokens     integer                     NOT NULL DEFAULT 0,
       completion_tokens integer                     NOT NULL DEFAULT 0,
       total_tokens      integer                     NOT NULL DEFAULT 0,
       response_ms       double precision            NOT NULL,
       endpoint          text                        NOT NULL DEFAULT '/v1/unknown',
       status_code       integer                     NOT NULL DEFAULT 200,
       ingested_at       timestamp with time zone    NOT NULL DEFAULT now()
   );
   CREATE INDEX idx_token_usage_timestamp        ON public.token_usage(timestamp);
   CREATE INDEX idx_token_usage_node_timestamp   ON public.token_usage(node_id, timestamp);
   CREATE INDEX idx_token_usage_model_timestamp  ON public.token_usage(model, timestamp);
   ```
   To audit any host's view of the schema, run
   `TOKEN_SIDECAR_POSTGRES_DSN=... .venv/bin/python scripts/probe_token_sidecar_schema.py`
   from the manager checkout.
2. **Set the DSN** in `~/vllm-manager/.env`:
   ```
   TOKEN_SIDECAR_POSTGRES_DSN=postgresql://token_sidecar_writer:...@central-host:5432/token_sidecar
   ```
3. **Enable the sink** in `~/vllm-manager/config.yaml`:
   ```yaml
   token_sidecar:
     enabled: true
     node_id: Mnemosyne          # unique per host
     flush_interval_seconds: 30
     batch_size: 500
     max_outbox_rows: 100000
   ```
4. **Restart**: `vllm-ctl restart`. Look for
   `Token sidecar enabled (node_id=Mnemosyne, batch=500, interval=30s)` in
   the logs.
5. **Verify**:
   ```bash
   ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' ~/vllm-manager/.env | head -1 | cut -d= -f2-)"
   curl -s -u admin:"$ADMIN_PASSWORD" http://localhost:8001/manager/status \
     | jq .token_sidecar
   # { "enabled": true, "writer_ready": true, "outbox_pending": 0,
   #   "last_flush_at": 1716234567.12, "last_flush_count": 7, "last_error": null }
   ```

The DSN is secret-bearing and stays in `.env` (gitignored); everything else
is declarative config that `vllm-ctl reload` will pick up. Both the SQLite
outbox and the postgres `event_id` PK use UUIDs so a DELETE-after-success
retry is naturally idempotent — `outbox_pending` should sit at zero in
steady state, but is allowed to grow up to `max_outbox_rows` during
outages, with oldest rows dropped past the cap (logged as a warning).

## Common operations

A short tour of the CLI; mirrors `vllm-ctl help` ordering.

| Group       | Command                          | What it does                                                     |
|-------------|----------------------------------|------------------------------------------------------------------|
| Container   | `vllm-ctl build`                 | Build the image. First build is slow.                            |
|             | `vllm-ctl start`                 | `docker compose up -d` and wait for `/health`.                   |
|             | `vllm-ctl stop`                  | `docker compose down`. Unloads the model.                        |
|             | `vllm-ctl restart`               | Restart the container in place.                                  |
|             | `vllm-ctl logs [-f]`             | Tail container logs.                                             |
|             | `vllm-ctl shell`                 | Bash inside the container.                                       |
| Models      | `vllm-ctl status`                | Container + resident-model status.                               |
|             | `vllm-ctl load <model> [opts]`   | Load (or swap to) a model. `--tp N`, `--gpu-mem F`, `-- <vllm>`. |
|             | `vllm-ctl unload`                | Drop the resident model and free VRAM.                           |
|             | `vllm-ctl list`                  | Configured profiles from `config.yaml`.                          |
|             | `vllm-ctl models`                | Models present in the HF cache volume.                           |
| Config      | `vllm-ctl reload`                | Re-read `config.yaml` without restarting.                        |
|             | `vllm-ctl storage`               | Storage locations + free space.                                  |
| Installs    | `vllm-ctl install <alias> <id>`  | Install under a stable alias. `--storage`, `--quant`, etc.       |
|             | `vllm-ctl install-cancel <alias>`| Cancel an active install (resumable).                            |
|             | `vllm-ctl install-retry <alias>` | Resume; `--force` wipes the cache first.                         |
|             | `vllm-ctl install-status [a]`    | One row, or the full catalog.                                    |
|             | `vllm-ctl cache-delete ...`      | Cache-only delete (demotes to partial) or `--remove-row`.        |
| Downloads   | `vllm-ctl download <model>`      | Legacy `snapshot_download` background job.                       |
|             | `vllm-ctl download-status <m>`   | Poll a single legacy download.                                   |
|             | `vllm-ctl downloads`             | List all legacy download records.                                |
| Diagnostics | `vllm-ctl chat <prompt>`         | One-shot completion against the resident model.                  |

Direct API equivalents (Basic auth required for admin):

```bash
ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' ~/vllm-manager/.env | head -1 | cut -d= -f2-)"
curl http://localhost:8000/health
curl -u admin:"$ADMIN_PASSWORD" http://localhost:8001/manager/status
curl -u admin:"$ADMIN_PASSWORD" -X POST \
  http://localhost:8001/manager/load \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct","tp":1}'
# Browse the full admin API at http://localhost:8001/docs
```

## Troubleshooting

- **`:8001` refuses connections.** `ADMIN_PASSWORD` is unset. Add it to
  `~/vllm-manager/.env` and `vllm-ctl restart`.
- **Install fails with HF `401` or `403`.** Gated repo without a token. See
  [Installing gated models](#installing-gated-models).
- **`vllm-ctl status` shows `No model loaded` even after install.** Expected.
  Installs put the weights in the cache; the first `/v1/*` request triggers
  the actual GPU load.
- **`504` from `/v1/*` during a swap.** The caller waited longer than
  `server.swap_queue_timeout_seconds` for another model to finish loading.
  Either bump the timeout, retry, or queue your traffic differently.
- **`503` from `/v1/*` mid-load.** The vLLM child died (OOM, kernel mismatch,
  etc.). The next request retriggers a fresh load — there is no auto-restart
  loop by design (PRD §5.3). Check `vllm-ctl logs` for the underlying error.
- **`vllm-ctl reload` reports `partial` rows after editing `config.yaml`.**
  Cache reconcile has spotted aliased installs whose cache directories are
  missing — usually because a storage location is no longer mounted, or
  because the alias was newly added. See [Recovering partial
  downloads](#recovering-partial-downloads).
- **`docker compose build` cannot find the Dockerfile.** `MNEMOSYNE_REPO_DIR`
  is unset (or wrong). Export it to the absolute path of your checkout.

## Known v1 limitations

These are deliberate v1 scope cuts. The canonical decision log is
[project_docs/PRD.md §8](project_docs/PRD.md); this list is the operator-facing
summary so you don't go looking for features that aren't here.

- **No multi-model concurrent serving.** One vLLM at a time. To switch, send
  a request with the new alias — `_proxy` queues the swap and returns once
  the new model is loaded.
- **No chat playground in the UI.** The admin UI covers catalog, install,
  search, and dashboard surfaces. Use the OpenAI-compatible `/v1/*` endpoint
  from your IDE / `curl` / a separate client for actual conversations.
- **No Prometheus `/metrics` endpoint.** `/manager/status` plus the UI
  dashboard are the operational surface. `/manager/gpu` exposes live GPU
  telemetry parsed from `nvidia-smi`.
- **No startup pre-warm.** The first `/v1/*` request after container start
  triggers the lazy load; expect the usual vLLM warmup latency on that call.
- **No automatic quantization-variant discovery on install.** You pick the
  exact HF repo (e.g. `Qwen/Qwen2.5-72B-Instruct-AWQ` vs the FP16 base) at
  install time. The HF Search view surfaces compatibility flags but does not
  auto-substitute quantized variants.
- **No vLLM auto-restart on crash.** If the inner vLLM dies under a request,
  the manager fails open with a `503` and the next `/v1/*` request triggers a
  fresh load (PRD §5.3). No supervisor loop tries to keep the previous model
  resident.
- **No runtime hard-fail when `gpus='all'` finds no GPUs.** The manager logs
  a warning and falls back to `VLLM_DEFAULT_TP`. On a real CUDA host this
  only happens if the nvidia-container-toolkit is misconfigured — fix the
  toolkit setup rather than expecting a hard error from the manager.

## More

- [agents.md](agents.md) — guidance for AI coding assistants and external
  contributors.
- [CLAUDE.md](CLAUDE.md) — Claude Code repository conventions; complements
  `agents.md`.
- [project_docs/PRD.md](project_docs/PRD.md) — design intent for v1.
- [project_docs/implementation_plan.md](project_docs/implementation_plan.md) —
  phased build plan; the "current state" of features in flight.
