# Agents Guide

This repository contains **Mnemosyne Inference**, with two isolated inference
workstation deployments and an optional Nyx-hosted Fleet gateway. The CUDA
deployment runs vLLM, llama.cpp, or SGLang Diffusion in a container; the native
Apple Silicon deployment owns an official llama.cpp server for GGUF,
coordinates oMLX and DS4, and uses a process-isolated MFLUX worker without
Docker. LM Studio is not an inference engine; its configured and conventional
model folders remain read-only migration hints. Both inference deployments
remain thin managers around upstream engines and must not fork or embed their
serving implementations. Fleet routes to those managers; it never owns an
engine process.

## Repository Shape

- `vllm_manager.py` is the FastAPI service entrypoint. It starts two uvicorn servers, owns manager state, launches the active inference engine, proxies `/v1/*`, serves the admin UI, and wires all HTTP routes.
- `config.py`, `catalog.py`, `profiles.py`, `runtime.py`, and `image_api.py` hold the core substrate: YAML/.env loading, SQLite catalog state, profile resolution, pure engine argv/env builders, and bounded Images API normalization.
- `cuda_residency.py` owns CUDA FIFO admission, strict-deployment transitions,
  epoch leases, full-stream draining, and maintenance barriers.
- `fleet_protocol.py` and `fleet_protocol/v1/` define shared, secret-free
  deployment identity, capacity helpers, the node snapshot schema, and golden
  cross-platform vectors.
- `mac_pool_protocol/v1/` defines the separate, strict, path-free Mac inventory,
  advisory-placement, and desired-install schemas and complete cross-component
  golden fixtures. It does not extend or grant authority to frozen Fleet
  snapshot v1.
- `fleet/src/mnemosyne_fleet/placement.py` consumes fresh authenticated Mac
  inventory plus one normalized signed-catalog recipe and produces an
  expiring, path-free advisory ranking for explicit Mac/storage selection. It
  never chooses a target, creates a desired job, or grants routing authority.
- `fleet/src/mnemosyne_fleet/catalog_service.py` owns Fleet's optional,
  failure-isolated last-known-good/update lifecycle, while
  `placement_api.py` stamps closed caller intent and adapts paired enrollment
  plus Mac inventory into scorer inputs. Both are default-off management side
  paths and must never mutate the registry, scheduler, public model mappings,
  node storage, or downloads.
- `fleet/src/mnemosyne_fleet/desired_install_protocol.py`,
  `desired_install_store.py`, and `desired_install_api.py` implement the
  default-off Hub DesiredInstall v1 authority. The administrator must submit
  one exact current advisory Mac/storage basis; Nyx re-resolves the signed
  recipe and recomputes placement before journaling a path-free job in its own
  private bounded SQLite database. Delivery occurs only through the selected
  Mac's authenticated outbound inventory sync and is fenced by exact pairing,
  credential generation, service instance, catalog, inventory sequence, and
  opaque storage generation. Cancellation is a revisioned stop intent, never
  cleanup/delete. This Hub layer has no filesystem path authority, downloader,
  runtime installer, live model claim, or inference-routing authority; the
  selected Mac independently validates and executes the path-free intent.
- `compatibility_catalog/v1/` defines the closed signed Apple Silicon catalog
  envelope for logical models, immutable artifacts, and typed `llama.cpp`,
  `omlx`, and `ds4` recipes. `compatibility_catalog/catalog.py` is vendored
  byte-for-byte into Fleet and the native service; both wheels embed the same
  normative schema. This trust substrate is read/verify/activate only and does
  not authorize downloads, runtime mutation, placement, or local storage.
  `macos/service/catalog_runtime.py` owns the native optional default-off LKG
  and update lifecycle. Its private state is anchored beside the active Mac
  config, and failures must remain isolated from inference, JIT residency,
  downloads, exact storage bindings, Settings, and accounting.
  `compatibility_catalog/ceremony.py` is the offline-only encrypted Ed25519
  key generation, detached-signature, multi-signer assembly, and exact-byte
  publication verifier. It must never create or consume a private key below
  the repository, accept the golden test key as publication authority, contact
  a network, or treat a valid signature as model/runtime/hardware evidence.
- `fleet/` is the independently locked Nyx service. It owns explicit node
  enrollment, authenticated snapshot polling, strict model mappings,
  capacity-aware routing, bounded priority/affinity controls, process-local
  async batches, metadata-only route history, the joined fleet overview and
  realtime dashboard, and read-only token-ledger aggregates. Batch request and
  response content must remain bounded process memory and must never enter
  Fleet SQLite or browser-facing admin APIs. It must run as one process unless
  reservations and batch ownership are moved to a shared transactional
  scheduler.
- `fleet/src/mnemosyne_fleet/pairing_store.py`, `secret_store.py`,
  `locator_policy.py`, `paired_transport.py`, `pairing_probe.py`,
  `pairing_coordinator.py`, and `pairing_api.py` implement the opt-in Hub-side
  bearer-pairing foundation: strict/idempotent lifecycle metadata, encrypted
  secrets, bounded locator policy, peer-pinned non-loading activation, and
  dynamic registry publication. This is not the signed Mac ceremony; pairing
  remains disabled by default and static enrollment must remain compatible.
- `downloader.py` and `download_worker.py` implement install/download orchestration. Installs run as killable subprocesses and persist state in SQLite.
- `hf_search.py`, `repo_probe.py`, `vllm_supported_architectures.json`, and `scripts/refresh_arch_list.py` support HuggingFace discovery, vLLM architecture filtering, and GGUF probing.
- `ui/` contains the React/Vite/TypeScript/Tailwind admin UI that is built into `/app/static` by the Dockerfile and served from the admin plane.
- `vllm-ctl` is the Bash CLI for Docker lifecycle, admin API calls, model loading, installs, cache deletion, status, logs, and one-shot chat.
- `Dockerfile` defines the CUDA/Python runtime, builds the UI, builds a pinned `llama-server`, installs PyTorch cu129, and installs pinned vLLM plus manager dependencies. Runtime dependencies live here, not in a runtime `requirements.txt` or `pyproject.toml`.
- `requirements-dev.txt`, `pytest.ini`, `tests/`, and `ui/package.json` define the host-side Python and UI test/build workflows.
- `pg_writer.py` and `scripts/probe_token_sidecar_schema.py` implement and inspect the optional Postgres token-usage sink; SQLite remains the local system of record and durable outbox.
- `project_docs/project_status.md` records current release status and feature
  history; `project_docs/smoke_checks.md` is the manual GPU-host checklist for
  behavior pytest cannot exercise. `project_docs/fleet_architecture.md`,
  `fleet_security.md`, and `fleet_acceptance.md` define the cross-node
  protocol, threat boundary, and target-host rollout evidence.
  `project_docs/mac_pool_architecture.md` and
  `project_docs/mac_pool_acceptance.md` define the Mac-first pooled-product
  target and its non-regression/release gates. They describe target state;
  never treat an unimplemented section as current behavior.
  `project_docs/fleet_pairing_protocol.md` defines the versioned dynamic
  enrollment, credential, activation, revocation, and recovery target; its
  initial and deferred security layers must remain explicitly distinguished.
- `scripts/fleet_acceptance.py` runs a bounded, content-redacted multi-node
  probe through Nyx and checks metadata fan-out plus exactly one normal token
  event per completed language request.
- `macos/service/` is an independent Python package for the native inference/control planes, engine adapters, lease-based global residency coordinator, and durable usage outbox. Its dependencies and lock file stay below that directory.
- `macos/service/desired_install_runtime.py`, `desired_install_store.py`, and
  `desired_install_executor.py` own the selected Mac's bounded DesiredInstall
  inbox and execution path. They revalidate pairing, service instance, signed
  catalog, exact recipe/artifact, opaque storage generation, free space, and
  cancellation before launching the existing durable native installer. The
  Hub never receives a path or bookmark, downloads remain residency-neutral,
  and profile registration/JIT loading continue through the ordinary native
  coordinator. Exact GGUF shard sets and an optional selected projector are
  catalog-bound artifact roles, not inferred after approval.
- `macos/service/mac_inventory.py`, `mac_inventory_store.py`, and
  `mac_inventory_sync.py` produce and deliver the path-free authoritative Mac
  hardware/storage/model inventory. Local lexical paths, bookmark/scope data,
  credentials, and arbitrary diagnostics must never enter this protocol.
- `macos/service/fleet_participation.py` persists the Mac owner's independent
  join/pause preference and counts only canonical Fleet-routed requests through
  complete response cleanup. Pausing must close Fleet admission while leaving
  local inference, downloads, storage, residency policy, and usage delivery
  intact.
- `macos/service/fleet_pairing.py` owns the Mac-local pairing journal and the
  atomic private-environment lifecycle for snapshot, Fleet-only dispatch, and
  management credentials. The service exposes only secret-free status and
  staged/active authentication gates. Its outbound client and the Swift
  Inference Pool page drive invitation claim, approval resume, provisioning,
  staging, and activation with a memory-only invitation secret. They also own
  an explicitly confirmed permanent self-revoke path with a durable exact-
  request recovery fence, pairing-only credential retirement, and safe new-
  invitation re-pair. Revoke must never alter models, exact weight paths,
  inference profiles, or token history; signed-artifact and multi-host
  acceptance remain release gates.
- `macos/service/storage.py`, `model_library.py`, `install_store.py`, `installer.py`, and `download_worker.py` implement exact nested-folder/volume validation, engine-aware Hugging Face discovery, and process-isolated durable native downloads. The install store retains a compact transition journal so target-Mac cancel/retry/registration/dismiss/delete acceptance remains provable after UI history is hidden. Managed downloads must remain residency-neutral.
- `macos/service/local_models.py` scans Finder-selected GGUF/MLX libraries without loading or copying weights. `macos/service/engines/llamacpp.py` translates typed profiles into a manager-owned upstream `llama-server` process while reusing the hardened managed-process ownership proof.
- `macos/service/security_scopes.py` consumes Finder-created transfer
  bookmarks and creates receiver-owned durable bookmarks for protected model
  folders. `scope_process.py` and `scope_worker.py` perform receipt and
  reactivation in bounded, killable process groups; startup revalidates
  configured grants and prunes unreferenced private bookmarks. Bookmark bytes
  remain private state; configuration carries only their SHA-256 `scope_id`.
  Production scope storage is anchored beside the active config under
  `state/security-scopes`, never derived from the user-configurable SQLite
  path.
- `macos/service/filesystem.py` and `fs_worker.py` keep protected-path
  inspection, scans, containment/header validation, directory creation, and
  size measurement in bounded, killable subprocess groups.
  `scoped_process.py` and `scope_exec.py` reactivate a persisted grant in each
  scoped helper/engine/download process and then `exec` the upstream command.
- `macos/service/sidecar_discovery.py` migrates the previous token sidecar's
  identity and ledger DSN through its user LaunchAgent, then atomically
  persists missing values into Unified Inference's private `.env`. Unified
  Inference is the native token sidecar; the legacy process is not in the
  inference path and must not remain a permanent reporting dependency.
- `macos/service/native_lifecycle.py` and `native_lifecycle_runtime.py` own the
  path-free migration/uninstall preview, private exact-path retention manifest,
  journal-only prepare API, and authenticated loopback authorization trigger.
  `native_lifecycle_helper_transport.py` is the bounded service-owned direct
  peer for the bootstrap-pinned bundled helper; the menu must never spawn that
  helper because the sealed peer manifest authorizes only the bundled service
  Python. Transport availability is not proof authority: production remains
  fail-closed before helper launch until a real per-install OS-backed proof
  verifier is provisioned. Migration staging must bind the predecessor to the
  exact installed application identity, require candidate signing-Team
  continuity, and enumerate the candidate's complete retained bundle-member
  inventory under a distinct lexical tree; a signed identity or build digest
  alone is never candidate authority. `native_lifecycle_executor.py` is a closed,
  restart-safe orchestration core with inert production defaults. Its durable
  product-wide claim, monotonic rollback intent, and prior-effect observations
  prevent concurrent or forward-after-rollback replay; an expired/abandoned
  claim enters manual recovery and blocks every other lifecycle transaction.
  The separate v2 runner receipt ledger enforces the immutable direction's
  exact effect order across grants and restart: a later opaque target cannot
  begin until the prior target has a conclusive finalized receipt. This is a
  validation fence, not execution or phase-advance authority. Manual-recovery
  effects, terminal recovery-clone cleanup, and the receipt-to-phase bridge
  remain deliberately unavailable.
  It contains no launchctl, process, filesystem-removal, Trash, or signing
  primitive. Do not enable execution until an authenticated signed bundled
  helper implements every named effect plus manual recovery and the
  credentialed release/hardware gates pass.
- Keep configured model profiles when an engine is disabled, but exclude them
  from the resolved/callable catalog until that engine is enabled. An external
  engine being unavailable must not make the control plane or Settings UI
  unable to start.
- `macos/retire_legacy_sidecar.sh` validates and persistently disables the
  exact `com.athena.token-sidecar` user job, boots it out, restarts Unified
  Inference, and archives the inactive plist only after both native HTTP
  planes are reachable. Preserve the plist until the service has inherited
  and persisted its node identity and ledger DSN; never kill an arbitrary
  listener on `:1240`.
- `macos/service/runtime_updates.py` discovers releases directly from the
  official upstreams, verifies and installs official `ggml-org/llama.cpp`
  Apple Silicon assets, installs MFLUX from PyPI, builds an exact DS4 GitHub
  commit, and provides atomic activation/rollback. Its private bounded
  lifecycle journal records only fixed transition fields, anonymous service
  instance IDs, and fixed failure codes so acceptance can prove activation,
  post-restart inference, rollback, post-rollback restart inference, and
  corrupt-runtime rejection without persisting arbitrary diagnostics.
  Runtime activation must use the coordinator's all-engines-empty maintenance
  barrier; never introduce a repository-owned dependency manifest.
- `macos/image-worker/` is the separately locked MFLUX runtime. It is launched only as a manager-owned child, binds loopback `:17324`, and must remain dependency-isolated from the macOS coordinator service.
- `macos/app/` is the SwiftPM menu bar controller, typed native settings UI, secret-safe credential store, and native service bootstrap. `macos/packaging/` stages the signed app, embedded LaunchAgent plist, direct `Contents/MacOS/mnemosyne-service-bootstrap` executable, relocatable Python runtime, and verified drag-to-Applications DMG. Keep this unsandboxed `SMAppService` LaunchAgent's `BundleProgram` pointed at that direct helper; introducing a second bundle identity is unnecessary here and broke launch-requirement refresh during in-place updates. A future sandboxed or restricted-entitlement job would require its own deliberate wrapper architecture.
- The Mac app also bundles the exact Fleet source and a direct
  `Contents/MacOS/mnemosyne-hub-bootstrap` executable behind the independently
  opt-in `com.mnemosyne.inference.hub` LaunchAgent. Hub Mode writes its secrets,
  configuration, pairing stores, inventory, and route database below the
  private Application Support `hub` tree; the gateway remains a separate
  process on loopback `:17400` and never owns the native worker on `:1240`.
  Promotion publishes only authoritative local snapshot deployments and
  enrolls that independent worker as `overflow`. Fresh installs must not enable
  the Hub automatically, while Finder replacement must refresh an enabled Hub
  registration and preserve an explicitly disabled one. Disable/uninstall must
  retain Hub identity and state unless the user separately requests a privacy
  reset.
- `macos/VERSION` is the only native product-version source.
  `macos/packaging/verify_release.py`, the native packages/locks, staged app,
  release tag, DMG name, and Sparkle appcast must agree. CI may stage ad-hoc
  candidates, but distribution requires the credentialed signed-release
  workflow, a GitHub-verified signed annotated tag, Developer ID hardened
  inside-out signing, notarization/stapling, Gatekeeper assessment, and an
  EdDSA-signed HTTPS appcast. Never put a private update key in the repository.
- `macos/V1_SCOPE.md`, `macos/acceptance/v1.json`, `macos/RELEASE.md`, and
  `macos/RELEASE_NOTES.md` define the Stable/Preview contract, release gates,
  credentials, update behavior, and rollback. Do not advance
  `macos/VERSION` to `1.0.0` while a required acceptance gate is pending.
  `macos/packaging/collect_acceptance.py` is the secret-redacted artifact/live
  evidence collector. Its opt-in restart and KeepAlive exercises must address
  only the exact registered LaunchAgent label, require a new PID and both HTTP
  planes, and never discover or signal a process by port. A migrated install
  journal `snapshot` is not evidence for transitions this candidate did not
  observe. Managed-runtime acceptance must use the runtime lifecycle journal,
  require service-instance changes before validating both activated and
  rolled-back versions, and prove a rejected integrity/path-safety failure
  left the baseline active.
  Login-cycle acceptance must compare a private accepted pre-cycle report
  against the exact candidate on the same host and require a changed
  `launchctl` GUI audit-session ID plus PID, healthy listeners, and a fresh
  durable self-test. A kickstart or KeepAlive restart is not login evidence.
  Release verification must reject every 1.x build unless the ledger version
  matches, `release_ready` is true, and every required gate is passed; 0.x
  candidate artifacts remain prereleases.
- `macos/INSTALL.md` is the end-user disk-image and all-engine setup path.
  `macos/config.yaml.example`, `macos/.env.example`, `macos/README.md`, and
  `macos/smoke_checks.md` are the native deployment's configuration, operator,
  development, and validation surface. Mac settings must not be added to the
  external CUDA compose file.
- `agents.md` is the single repository guide for coding assistants and contributors. Keep it aligned with code, examples, and verification commands when architecture or workflows change.

The live `docker-compose.yml` is intentionally machine-specific and may live outside this repo. The CLI expects it under `$VLLM_COMPOSE_DIR`, defaulting to `~/vllm-manager`. Use `docker-compose.example.yml` as the maintained template. If a change affects ports, env vars, volumes, container names, build args, or mounts, call out the required external compose changes.

## Current Architecture

### CUDA deployment

- The container runs **two HTTP planes** in one Python process:
  - Inference plane on `:8000`: `/v1/*`, `/health`, and the separately
    authenticated read-only `/fleet/v1/snapshot`.
  - Admin plane on `:8001`: `/manager/*`, `/ui/`, `/docs`, `/openapi.json`, `/redoc`, and admin-authenticated `/v1/*`.
- The active engine runs behind the manager on loopback, default `127.0.0.1:8002` via `VLLM_INNER_PORT`. Do not collide this with the external inference or admin ports.
- Admin uses HTTP Basic as `admin:$ADMIN_PASSWORD`. If `ADMIN_PASSWORD` is unset, admin bind is forced to `127.0.0.1` inside the container, which makes the published Docker admin port unreachable from the host.
- Inference bearer auth is optional for ordinary standalone use. If `INFERENCE_API_KEY` is set, `/v1/*` on the inference plane requires `Authorization: Bearer <key>`. The independent `FLEET_API_KEY` enables bearer-authenticated, read-only `GET /fleet/v1/snapshot`; if unset that route returns 404 and it is never mounted on the admin plane. Fleet discovery fails closed with `fleet_inference_auth_unconfigured` unless `INFERENCE_API_KEY` is also non-empty.
- Only one model is resident at a time. `CudaResidencyCoordinator` compares
  strict deployment IDs rather than aliases, bounds FIFO waiters with
  `server.max_queue_depth`, enforces engine-derived capacity under the optional
  `server.max_concurrency` ceiling, and holds epoch permits through complete
  response streams. A different-target head drains the old epoch before
  unload/load; manual unload, eviction, and shutdown use the same barrier.
- Proxied `/v1/*` requests peek at the JSON `model` field, resolve it through config aliases, UI-installed catalog aliases, legacy aliases, installed HF IDs, raw HF IDs, or absolute paths, then rewrite the model field to the engine-served name. Streaming requests may also receive `stream_options.include_usage=true` for accounting; synthetic usage events are hidden unless the client requested them. All other request fields, including multimodal content and backend-specific extensions, stay opaque.
- `GET /v1/models` is the deliberate proxy exception: the manager serves it locally from the catalog so it lists every installed alias across all backends even while idle. A raw/resident model without an installed catalog row is included as a fallback.
- Supported backends are `vllm`, `llama.cpp`, and `sglang-diffusion`.
  - vLLM launches `vllm.entrypoints.openai.api_server`.
  - llama.cpp launches `llama-server` with a selected GGUF file and serves under the alias.
  - SGLang Diffusion launches the separately pinned `/opt/sglang` runtime and serves image profiles through `POST /v1/images/generations` on the same sequential inner port.
  - vLLM profiles in known slow CUDA-graph-capture SSM/hybrid families default to `--enforce-eager`, based on the cached model `config.json`. Keep the detection in `runtime.vllm_wants_eager_default`; profile `extra_args` remain the final override layer.
- Runtime configuration lives in YAML, default `/config/config.yaml`, with secrets in `/config/.env`.
- Persistent catalog state lives in SQLite, default `/state/mnemosyne.db`. It stores config-synced rows, UI-installed rows, download rows, resolved revisions, usage counters, backend, selected GGUF filename, model kind, capabilities, and image defaults.
- Config reload is supported by `POST /manager/reload`, `vllm-ctl reload`, or SIGHUP. Reload re-syncs config into the catalog and reconciles caches; it does not change Docker mounts or published ports.
- Idle eviction is enabled by `server.idle_unload_seconds` unless set to `null`. Usage deltas are flushed periodically and during unload/shutdown.
- Installs use a subprocess worker (`python -m download_worker`) rather than in-process HuggingFace downloads. Interrupted installs are recovered as `partial` on startup and can be retried.
- Legacy `/manager/download*` endpoints are preserved as v0 shims using synthetic cache-only aliases and the same persistent install pipeline.
- HuggingFace search and `/manager/hf/files` run on the admin plane, include compatibility signals, and detect GGUF candidates for llama.cpp installs.
- Token usage tracking: every successful `/v1/{chat/completions,completions,responses,messages,embeddings,rerank}` response with a recognized usage block produces one normalized event; OpenAI Responses input/output fields, Anthropic Messages cache counters, nested envelopes, and completed streams normalize into the common prompt/completion/total shape. Before a non-streaming response completes or a terminal stream event is forwarded, the manager atomically commits idempotent local analytics and, when `token_sidecar.enabled`, the SQLite `pg_usage_outbox`. `_runtime.usage_rows` is only an in-memory retry queue for a failed immediate SQLite transaction, and `_flush_loop` removes rows only after a later commit. `_pg_flush_loop` (via `pg_writer.PgWriter`) drains the durable outbox to `public.token_usage`; the shared event UUID plus `ON CONFLICT DO NOTHING` makes ambiguous local commits and DELETE-after-success delivery retries safe. Respect `max_outbox_rows`, which intentionally drops the oldest rows at the cap. `/manager/status.token_sidecar` exposes outbox depth and last-flush metadata.

### Nyx Fleet gateway

- Fleet exposes one authenticated OpenAI-compatible `/v1/*` endpoint and a
  `/fleet/` dashboard. It supports only its explicit route allowlist and
  rewrites only the model alias and authorization header.
- Node enrollment is explicit. Each node has distinct snapshot and inference
  credentials, and neither credential is an admin credential. Fleet's public
  client key, dashboard-admin key, all node credentials, and read-only ledger
  DSN must remain distinct environment-backed secrets.
- Dynamic Mac pairing is an opt-in, partially implemented layer over that
  boundary. With pairing disabled its routes are absent and static nodes retain
  their current environment-backed behavior. The implemented Hub foundation
  covers bounded version-1 invitation/claim/approval/provisioning, encrypted
  per-role credentials, pinned non-loading activation probes, explicit
  enable/disable/revoke, restart reconciliation, and paired registry
  membership. A production pairing starts Hub-disabled and requires a separate
  admin enable after activation. Do not claim a complete product workflow
  until the implemented Swift ceremony passes signed-artifact acceptance and
  lifecycle integration, routine rotation, static adoption, and multi-host
  artifact acceptance exist.
- Snapshot liveness is based on Nyx monotonic receipt time, instance identity,
  and increasing sequence. Persisted snapshots never regain routing authority
  after restart without a fresh poll.
- A public model maps to one exact deployment ID and exact capability set.
  Only authoritative immutable provenance is eligible; aliases, node IDs,
  storage paths, capacity, and live residency are excluded from deployment
  identity. The schema, packaged copy, producers, validators, and golden
  vectors must change together.
- Scheduling is warm-first and weighted least-outstanding within a tier.
  Requests without routing controls retain the normal FIFO lane; closed
  interactive/normal/batch lanes age lower priority toward admission. Exact
  enrollment affinity, shortened maximum wait, and disabled fallback may only
  narrow/rank existing eligibility and must never create routing authority.
  In-memory reservations cover the stale-poll window, while the selected node
  remains final admission authority.
- A reservation lasts through the complete response body or stream.
  Cancellation-safe cleanup must return it exactly once. Retry is permitted
  only for proven connection establishment failure or a pre-work `429` with
  the manager-owned `X-Mnemosyne-Error: node_busy` proof header; body-only
  errors are terminal, and ambiguous timeouts are never retried.
- Fleet SQLite stores fixed route metadata only. It never stores request or
  response bodies, secrets, or token rows. Nodes remain the sole token-event
  writers; Nyx reads bounded aggregates from `public.token_usage` through a
  read-only role.
- Default node HTTP clients must ignore ambient proxy variables, must not
  follow redirects, and must never expose node URLs or credentials to the
  dashboard.

### Native macOS deployment

- Inference defaults to `127.0.0.1:1240` so Unified Inference is a drop-in replacement for the previous token sidecar; the native Settings UI may deliberately switch only that public inference listener to `0.0.0.0:1240` for LAN/VPN clients. Control stays on `127.0.0.1:17321`, oMLX uses `:17322`, the manager-owned DS4 child uses `:17323`, the manager-owned MFLUX worker uses `:17324`, and manager-owned llama.cpp uses `:17325`. Reserve `17320` and `17326-17329` for later native services. The legacy sidecar must be booted out and persistently disabled or removed before Unified Inference binds `:1240`; merely unloading it lets it return at the next login.
- The inference plane also exposes read-only `GET /fleet/v1/snapshot` only
  when its independent `FLEET_API_KEY` is configured. Discovery fails closed
  with `fleet_inference_auth_unconfigured` unless the inference key is also
  configured. Never reuse the inference key, control password, or another
  node's credential.
- Fleet pairing/enrollment and Mac-local participation are separate. Existing
  manually enrolled Macs default joined for backward compatibility. The
  authenticated control route `/manager/fleet/participation` changes only the
  durable local preference. A valid canonical `X-Mnemosyne-Fleet-Route`
  request holds a participation lease through the complete response; pause
  rejects later Fleet work with the proven pre-work `node_busy` response while
  local requests without that internal marker remain available. Snapshot v1
  stays shape-compatible and closes its advertised capacity/loadability while
  paused or draining. Permanent self-revoke is a separate control-plane action:
  it requires a new invitation to re-pair and may retire only the exact pairing-
  owned credentials, never models, exact storage bindings, profiles, local
  inference, token history, or the usage outbox.
- A per-user LaunchAgent owns Mnemosyne Core. The controller uses an explicit AppKit `NSStatusItem` with a SwiftUI popover; quitting it must not terminate inference. `SMAppService.agent` registers the embedded plist and the bootstrap must `execve` the bundled Python without daemonizing.
- `ResidencyCoordinator` owns the cross-engine invariant. A request holds an epoch-tagged model lease through its complete stream. FIFO queuing stops old-target admission once a switch is pending, drains active leases, proves all enabled adapters empty, loads one target, and proves exactly one ready manager-owned resident. Engine-derived capacity is capped by optional `server.max_concurrency`; `server.max_queue_depth` and `server.queue_timeout_seconds` bound admission.
- oMLX capacity comes from its authoritative admin
  `scheduler.max_concurrent_requests`; a missing or incompatible scheduler
  contract falls back to one request. llama.cpp capacity comes from the exact
  managed `parallel` setting. Fresh configs keep the verified resident warm,
  while the Settings presets may opt into bounded idle unloading and a lower
  global ceiling. Performance telemetry must remain bounded, in-memory, and
  metadata-only: never retain prompts, responses, credentials, or arbitrary
  upstream diagnostics.
- A signed managed oMLX install retains its exact catalog launch contract in
  the hidden-inclusive install journal. Before registration, every local/JIT
  load, benchmarks, and every fresh Fleet snapshot, Mnemosyne must prove by an
  authenticated GET that the externally owned oMLX service already has the
  exact scheduler slot count and any required prefill memory guard. Never
  mutate those service-global settings as an install/load side effect. Drift,
  malformed state, timeout, or authorization failure keeps the alias visible
  but unverified, non-loadable, and Fleet-ineligible without hiding unrelated
  local profiles or taking down the node. A later install-journal read fault
  must preserve the last successful signed/ordinary/conflict classification
  for each exact unchanged oMLX target; an unknown or changed target fails
  closed and a previously signed target must never be downgraded to ordinary.
- oMLX is an external loopback service controlled through its native lifecycle APIs. llama.cpp and DS4 are model-specific process groups started by Mnemosyne. Never kill an unknown PID or listener; persisted managed-process identity must match executable, argv, start identity, and process group before recovery or signaling.
- The supported macOS engine set is llama.cpp, oMLX, DS4, and MFLUX. Keep
  legacy mlxcel and mistral.rs configuration parseable for upgrade safety, but
  always treat it as disabled and exclude it from adapters, callable profiles,
  Model Library, runtime updates, readiness, inventory, Fleet placement, and
  DesiredInstall. Never delete its external binaries, profile records, or
  model weights as an upgrade side effect.
- Configuration schema v6 may attach exact engine alternatives to one public model alias. The original profile remains the unconditional fallback and `selection.mode` defaults to `fixed`. A user may explicitly set `selection.mode: pinned` plus `pinned_engine` to bypass benchmark ranking and prefer that declared engine; if it is disabled or cannot load before inference starts, use the original fallback without replaying ambiguous upstream work. Benchmark selection is opt-in, applies only to capabilities the selected alternative actually supports, requires a context guarantee at least as large as the primary, and requires fresh content-free evidence matching the exact ordered candidate/load fingerprint, runtime fingerprint, benchmark suite, and local Mac. Unrelated settings or other-model edits must not invalidate that evidence. A missing/stale/failed result must select the fallback; it must never make a profile disappear. Fleet must exclude any alias whose local policy can route away from the exact advertised primary deployment.
- Every schema-v6 language candidate owns a context policy: `automatic` applies fresh long-prefill evidence for the exact model/runtime/system and otherwise retains the configured safe fallback; `native` requests detected model metadata without claiming it was profiled; and `fixed` applies the explicit user limit. oMLX context inspection and changes must use its official status and per-model settings APIs; when available, its native memory-guard context benchmark runs only inside the coordinator's global-empty maintenance barrier. Other context profiling must use sequential coordinator leases. Persist only fixed fingerprints, requested/verified/prompt token counts, suite/runtime/system identities, and timestamps. Never persist the synthetic probe prompt, response, arbitrary diagnostics, credentials, or unhashed paths. `/v1/models.max_model_len` must equal the selected candidate's guaranteed contract.
- Cross-engine benchmark rows may retain only fixed engine/model fingerprints, sample counts, success rate, TTFT, total latency, output throughput, suite/config/runtime/system fingerprints, and timestamps. Never persist prompts, generated text, arbitrary upstream diagnostics, credentials, or unhashed local model paths. Benchmarks must use coordinator leases sequentially and hold each lease through the complete response stream.
- Persisted llama.cpp survivor metadata must also retain the exact storage
  root, scope ID, and volume UUID so restart recovery can reconstruct and
  revalidate the protected-path target before adopting or signaling a child.
- Official oMLX model-directory reloads may synchronously preload pinned
  models. Any directory rescan inside the all-engines-empty maintenance
  barrier must therefore unload through oMLX's authoritative admin inventory
  and prove emptiness before admission reopens. A maintenance drain timeout
  must enter an explicit recoverable degraded state, never leave admission
  silently wedged.
- Unified Inference is the token sidecar for every language engine and central
  reporting defaults on. During migration, it may inherit `node.id` and the
  ledger DSN from the previous sidecar's LaunchAgent; explicit native values
  win. Persist missing inherited values into the private native `.env` before
  retiring that LaunchAgent. Keep the Settings identity read-only. The Usage
  page may replace or clear the Postgres DSN only through a write-only secure
  field backed by the private `.env`; never return, prefill, or log the DSN.
- The primary local migration surface is the read-only
  `GET /manager/model-library/local-sources` hint list, followed by
  Finder-driven `POST /manager/model-library/local-scan` and explicit
  `POST /manager/model-library/imports`. Source hints read LM Studio's
  configured download root and documented default without contacting its
  server or probing model paths. There is no LM Studio adapter, credential,
  control endpoint, or runnable profile. Version-1 LM Studio profiles migrate
  into inert alias/load-setting records that the Finder import consumes.
  Finder must still confirm the exact folder before bookmark creation or
  scanning. Scan again before persisting opaque candidate/projector IDs; never
  preselect every candidate, load a model, copy weights, treat `mmproj` as a
  primary model, or replace an exact nested/symlink path with its resolved
  target or mount root. Preserve matching aliases and compatible load
  settings.
- The native Storage UI always uses the macOS directory picker. Preserve the exact selected folder (including paths such as `/Volumes/Athena/models`) separately from the containing mount and its UUID; never replace it with the volume root or make users type it.
- The menu app must create an ordinary bookmark while its `NSOpenPanel` grant
  is live. Its implicit extension is the Apple-supported single interprocess
  handoff. The service must first use a bounded unscoped helper to determine
  whether its deliberately unsandboxed process already has durable read/write
  access. Persist only the exact path when that succeeds, automatically remove
  an obsolete scope on startup after proving the same access, and create a
  receiver-owned security-scoped bookmark only when the path genuinely needs
  the transferred grant. A newly created scope must reactivate in a fresh
  helper before configuration can reference it. Do not claim or add App Sandbox bookmark
  entitlements without a deliberate signing/sandbox architecture change.
  Store only the receiver-owned bookmark below the mode-`0700` private
  `state/security-scopes` directory beside the active config, with mode-`0600`
  files. Store only its SHA-256 `scope_id` in YAML and never return or log
  bookmark bytes. Preflight every referenced grant before saving config;
  revalidate configured scopes before coordinator startup and prune
  unreferenced private bookmarks only after loading persisted config. Receipt
  and preflight must run in killable subprocess groups with deadlines. Scoped
  helpers and manager-owned model/download children must reactivate the durable
  bookmark in-process before `exec`.
- macOS can block bookmark and filesystem calls while a protected-folder
  decision is pending. Keep bookmark receipt/reactivation, storage inspection,
  local scans, profile/path resolution, GGUF/projector header validation,
  directory creation, and directory sizing in killable subprocess groups off
  the asyncio event loop and behind bounded deadlines. Timeout, request
  cancellation, and service shutdown must terminate the complete helper
  process group and fail closed with an actionable permission/volume diagnostic
  while both HTTP planes remain responsive.
- Native Hugging Face installs are engine-aware and durable. The Model Library
  presents one cross-engine result list; each candidate retains and displays
  its authoritative engine compatibility, and selecting it drives the exact
  engine-specific validation/install flow. The selected-model card also
  presents an advisory runtime-preparation plan for the exact engine and, for
  DS4, the exact managed source channel. It must never start a weight download,
  replace the selected Download-to key, move files, or imply readiness before
  the runtime and enabled-engine health are proved. A missing DS4 Apple
  toolchain may open only the explicit, bounded `/usr/bin/xcode-select
  --install` system dialog after confirmation; requesting that dialog remains
  unverified until later fixed `xcode-select --print-path` and `xcrun --find
  clang` probes succeed and both the selected toolchain and compiler exist.
  Model-card rendering must remove
  Hub YAML front matter, preserve safe Markdown structure, and remain readable
  and scrollable without compressing the rest of the install controls. llama.cpp search
  requires explicit GGUF quant/shard selection, automatically selects the
  highest-fidelity same-directory vision projector when present, and retains
  explicit text-only opt-out/manual projector selection. Discovery shows a
  bounded model-card preview plus architecture, context length, parameter
  count, and license when Hub/config/GGUF metadata provides them. Detected GGUF
  context length and selected projector persist into the created profile.
  Exact revisions and file sets remain pinned. DS4 and MFLUX use verified
  curated candidates; DS4 discovery must mirror the exact single-node main
  model targets in the official `antirez/ds4` downloader, verify every Hub
  file at the resolved revision, retain complete shard groups as one install,
  and exclude auxiliary DSpark weights and distributed-only Pro halves. oMLX
  search exposes metadata-derived compatibility honestly and downloaded
  snapshots must be
  classified before profile registration so they advertise only detected
  generation, embeddings, or rerank routes. Downloads run out of process,
  never load a model, and oMLX directory changes run only through the
  coordinator's all-engines-empty maintenance barrier. Treat
  downloaded-but-not-registered weights as a durable retryable state; retry
  profile registration without redownloading them. Report durable byte/total
  progress and smoothed transfer speed. Hiding completed history must retain
  internal managed-download provenance. File cleanup is an explicit separate
  action behind the global empty-residency barrier. Managed downloads may
  permanently delete only their exact ledger-owned destination. A llama.cpp
  or oMLX profile without managed provenance may clean up only when a fresh
  bounded scan uniquely rediscovers its exact payload inside its registered
  storage; move those imported paths to the macOS Trash. Refuse roots,
  escapes, symlinks, ambiguous matches, and paths shared by another profile.
- DS4's typed `load.parallel` maps to upstream `--batched-session` and owns
  coordinator/Fleet admission capacity; for llama.cpp the same typed setting
  maps to its parallel slots. Never permit `extra_args` to override either
  manager-owned slot count. The unset DS4 default is one authoritative session
  because every additional slot allocates another full KV state.
- Runtime update checks are read-only. For oMLX, select the official DMG that
  matches the host macOS major version and detect its app, CLI shim,
  conventional Homebrew locations, or running server. oMLX remains externally
  owned; never replace its app or Homebrew files. The menu app may delegate
  an initial missing-runtime installation to an existing Homebrew only after
  explicit user confirmation displays fixed official tap and stable install
  commands. Do not accept arbitrary formulas/arguments, use `--HEAD`, update,
  or reinstall through that initial-install action. A separately confirmed
  update action may operate only on a detected stable Homebrew-owned oMLX,
  behind the global empty-residency barrier, using the fixed `omlx stop`,
  `brew update`, `brew upgrade omlx`, and `omlx start` sequence. It must reject
  Homebrew HEAD, official-app, and unknown external ownership and validate an
  authoritative empty control plane before admission reopens. Cache metrics
  must come from oMLX's official admin API; an explicit cache reset must drain
  globally and call the official cache-clear API, never infer or delete an
  arbitrary filesystem path. llama.cpp must come from the official
  `ggml-org/llama.cpp` macOS arm64 release asset and pass published size,
  SHA-256, safe-extraction, executable, and CLI-contract checks. MFLUX must
  come from its official PyPI project and DS4 from an exact commit in
  `antirez/ds4`. Staging may happen
  while a model is resident, but pointer activation and rollback must drain
  leases, unload every engine, and prove global emptiness. Keep old runtimes
  for recovery and never place model weights inside them.
- Keep the native MFLUX catalog limited to text-to-image configurations the pinned worker can dispatch through `/v1/images/generations`, and persist each candidate's model-specific defaults when an install becomes a profile. An official Hub checkpoint is not installable merely because it shares a family name: keep unsupported layouts, such as Krea 2 Raw under the current Turbo-only loader, visibly unavailable rather than creating a broken profile.
- A packaged installation must not persist a checkout virtualenv in
  `engines.mflux.python`. Leave that override unset so the app bootstrap or
  active managed runtime supplies the packaged MFLUX Python and worker source;
  checkout paths and `MNEMOSYNE_MFLUX_*` overrides are development-only.
- Adapter-observed state is authoritative. An unreachable/unauthorized/incompatible adapter is not implicitly empty. Startup defaults to `unload_all`; uncertain state fails closed until `/manager/reconcile` succeeds.
- The Mac proxy supports capability-gated Chat/Completions, Responses, Messages, Embeddings, Rerank, and Images Generations routes. For oMLX and llama.cpp generation requests only, it normalizes the portable `thinking_budget` aliases after engine selection and maps top-level `enable_thinking`/`preserve_thinking` conveniences into chat-template kwargs. Qwen3.8 `reasoning_effort` remains request-scoped and is mirrored into its template; never persist reasoning inputs or content. MFLUX is terminated on unload/abort so Metal memory is released at the process boundary.
- The menu app reads and writes structured configuration through the control plane. The service remains the schema authority and atomically persists validated YAML; credential values are write-only in the UI and never returned by the API. Preserve `schema_version`; an older app must refuse to save a newer schema instead of dropping unknown fields. Config snapshots carry an optimistic revision that every save must echo. Serialize saves, download-completion profile creation, and local imports through the same mutation lock; reject a stale Settings save instead of overwriting a concurrent model addition.
- Setup & Health is the native first-run authority. It must remain usable when
  the service is disabled or degraded, present bounded and secret-redacted
  readiness, distinguish Stable llama.cpp/oMLX from Preview DS4/MFLUX, and
  provide recovery actions. First-run setup completes only after its self-test
  sends a real request through the public listener and verifies the matching
  durable local usage row; Postgres delivery is separately authoritative from
  writer/outbox state. Keep first-presentation and completion evidence scoped
  to the exact app version/build, record presentation only after the
  first-run window is shown, and never let a stale completion preference clear
  clean-install acceptance.
- The ordinary Mac Models page must not create profiles from raw model or
  projector text fields. New profiles come from the engine-aware Model Library
  or Finder discovery; imported engine/source/storage/served-name/projector
  facts remain read-only, and users choose only engine-valid typed model roles
  (Generation, Embeddings, Rerank, or Image).
- `macos/packaging/build_app.sh` signs ad hoc by default and accepts
  `CODESIGN_IDENTITY` for a stable signing identity. Do not imply
  durable protected-folder grants survive arbitrary ad-hoc rebuilds; after a
  code-identity change, the user may need to reselect the folder.
  Every non-system dynamic framework copied into `Contents/Frameworks` must
  have a matching bundle-relative executable rpath. Release verification must
  inspect both the dependency and `LC_RPATH`; deep code-signature validation
  alone does not prove that the app can reach
  `applicationDidFinishLaunching`.
  A full stage must also reject any relocatable Python export whose closed
  provenance does not match both committed native lock files, and must embed
  byte-identical compatibility-catalog and DesiredInstall schemas. Release
  builds must use the pinned venvstacks version, bind registry installs to the
  exact target-compatible wheel URL and SHA-256 in the committed locks, rebuild
  generated layers cleanly, and reject every bundled Mach-O slice whose
  deployment target exceeds the app's declared macOS minimum. The production
  bootstrap must scrub ambient `PYTHON*` state and use only its closed bundled
  Python/source paths. A complete bundle must ignore
  `MNEMOSYNE_PYTHON_OVERRIDE`; consult it only when an intentionally bare
  development bundle has no embedded runtime. Release verification compares the complete bundled Python source inventories and
  critical runtime dependencies; `--allow-bare` is development-only and must
  never weaken a DMG/release verification.
- Mac usage events normalize OpenAI, Responses, and Anthropic token shapes and
  atomically write local analytics plus the SQLite Postgres outbox. A
  Fleet-routed request reuses the authenticated route UUID as its stable event
  ID. Reserve that UUID and any required outbox slot atomically before JIT or
  coordinator admission; concurrent/restarted service connections must not
  both accept the final slot or execute a duplicate route. Mark the durable
  reservation started immediately before request bytes may reach an engine.
  Pre-work failures may release it, but post-dispatch Fleet failures retain a
  bounded replay fence. The native service must commit accounting before
  non-stream success or a recognized terminal SSE event. A Fleet 2xx language
  response without normalized usage must fail closed with `usage_missing`
  without fabricating counts; preserve standalone missing-usage compatibility.
  A full durable outbox must close new language admission with
  `usage_outbox_full` and zero Fleet capacity; never prune an undelivered row
  to make space. Image requests remain outside token accounting.
- Image requests intentionally do not emit token-usage records. Do not add image prompt/output policy hooks; this repository is a local homelab tool.

## Configuration

The CUDA and Mac configurations are intentionally separate. The following
section describes CUDA; native settings live in `macos/config.yaml.example` and
are copied to `~/Library/Application Support/Mnemosyne/config.yaml`.

The canonical host setup is a workstation directory such as `~/vllm-manager` containing:

- `docker-compose.yml` copied from `docker-compose.example.yml`.
- `config.yaml` copied from `config.yaml.example`.
- `.env` copied from `.env.example`.
- `state/` for the SQLite database.

Important environment variables:

- `MNEMOSYNE_REPO_DIR`: lets the external compose file find this repo's Dockerfile.
- `MNEMOSYNE_CONFIG_PATH`: defaults to `/config/config.yaml`.
- `MNEMOSYNE_ENV_PATH`: defaults to `/config/.env`.
- `MNEMOSYNE_DB_PATH`: defaults to `/state/mnemosyne.db`.
- `VLLM_INNER_PORT`: defaults to `8002`.
- `ADMIN_PASSWORD`: required for host/LAN access to the admin plane.
- `INFERENCE_API_KEY`: optional bearer key for inference-plane `/v1/*`.
- `FLEET_API_KEY`: optional node-specific bearer key enabling the read-only
  fleet snapshot on the inference plane.
- `HUGGING_FACE_HUB_TOKEN`: optional token for gated HuggingFace repos, read by install workers after restart.
- `TOKEN_SIDECAR_POSTGRES_DSN`: optional secret-bearing DSN used only when `token_sidecar.enabled` is true.
- `MNEMOSYNE_LOG_FORMAT`: `json` by default; set `text` for locally readable logs.
- `LLAMA_SERVER_BIN` and `MNEMOSYNE_UI_DIR`: development overrides for the llama.cpp binary and built UI directory.

Model profiles support aliases, HF model IDs, revision, quantization, GPU plan, max context, storage location, backend, GGUF filename, and raw `extra_args`. Aliases must be lowercase alphanumeric/hyphen and cannot use the reserved `__cache__:` namespace.

## Development Constraints

- Preserve the thin-wrapper design. Do not embed custom serving logic or fork vLLM/llama.cpp behavior into the manager.
- Keep OpenAI-compatible request bodies as pass-through as possible. Intentional mutations are limited to model-name canonicalization, streaming usage opt-in for accounting, and the native oMLX/llama.cpp reasoning-control translation described above; preserve all remaining JSON fields verbatim.
- Maintain backward-compatible shims where they exist, especially `POST /manager/load`, `POST /manager/download`, `/manager/download*`, legacy aliases, and existing `vllm-ctl` workflows.
- Treat the external compose file as user-managed. Do not assume it can be edited from this repo.
- Be careful with cache deletion. Deletion must stay under configured storage roots, refuse active installs/downloads, and refuse resident models.
- Keep admin-only mutation endpoints off the inference plane.
- Do not store secrets in `config.yaml`, committed examples, logs, catalog rows, or UI state.
- Preserve `extra_args` as the escape hatch for new engine flags and append them last.
- Prefer the existing module boundaries:
  - `config.py` for config/env loading and validation.
  - `catalog.py` for SQLite schema, migrations, reconcile, and durable state.
  - `profiles.py` for alias/profile resolution.
  - `runtime.py` for pure backend argv/env construction and runtime state shape.
  - `downloader.py` and `download_worker.py` for install subprocess lifecycle.
  - `hf_search.py` and `repo_probe.py` for Hub metadata and format compatibility.
  - `vllm_manager.py` for app wiring, auth, proxying, and engine lifecycle.
- Preserve the native boundary: Mac code may share protocol/usage concepts but
  must not import `vllm_manager.py`, CUDA profiles, or Docker runtime modules.
- In Mac code, engine mutation belongs to adapters and cross-engine ordering
  belongs to `ResidencyCoordinator`. The HTTP layer must acquire a lease before
  opening upstream and release it only after the complete body/stream closes.
- On CUDA, all inference, explicit load, unload, eviction, and shutdown paths
  must go through `CudaResidencyCoordinator`; never call the process teardown
  hook beneath an active epoch lease. Queue keys and transition targets are
  strict deployment IDs, not aliases.
- Keep inner Mac engines on loopback. The public Mnemosyne inference listener
  may bind non-loopback with optional authentication: when `INFERENCE_API_KEY`
  is set, `/v1/*` requires its bearer token; when it is absent, inference is
  deliberately unauthenticated. A non-loopback control bind still requires
  `ADMIN_PASSWORD`.

## Common Commands

Most CUDA runtime workflows happen through Docker because vLLM and the
CUDA-linked llama.cpp build are container-host concerns.

```bash
./vllm-ctl build
./vllm-ctl start
./vllm-ctl stop
./vllm-ctl restart
./vllm-ctl status
./vllm-ctl logs -f
```

Model, config, and storage operations:

```bash
./vllm-ctl load qwen-72b-awq
./vllm-ctl load Qwen/Qwen3-8B --tp 1 -- --max-model-len 32768
./vllm-ctl unload
./vllm-ctl list
./vllm-ctl models
./vllm-ctl reload
./vllm-ctl storage
./vllm-ctl chat "What model are you?"
```

Install and cache operations:

```bash
./vllm-ctl install qwen-coder Qwen/Qwen2.5-Coder-7B-Instruct --storage nvme-fast
./vllm-ctl install TheBloke/Some-GGUF --list-gguf
./vllm-ctl install local-model org/repo-gguf --backend llama.cpp --gguf-filename model.Q4_K_M.gguf
./vllm-ctl install qwen-image Qwen/Qwen-Image --backend sglang-diffusion --gpus 0
./vllm-ctl install-status
./vllm-ctl install-status qwen-coder
./vllm-ctl install-cancel qwen-coder
./vllm-ctl install-retry qwen-coder --force
./vllm-ctl cache-delete --alias qwen-coder
./vllm-ctl cache-delete --alias qwen-coder --remove-row
```

Legacy download shims:

```bash
./vllm-ctl download <model-id>
./vllm-ctl download-status <model-id>
./vllm-ctl downloads
```

Direct API examples:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/v1/models
curl -u admin:"$ADMIN_PASSWORD" http://localhost:8001/manager/status
curl -u admin:"$ADMIN_PASSWORD" -X POST http://localhost:8001/manager/load \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen-coder"}'
```

Native macOS development and verification:

```bash
uv sync --project macos/service --extra dev
uv run --project macos/service --extra dev python -m pytest macos/service/tests
uv sync --project macos/image-worker --extra dev
uv run --project macos/image-worker --extra dev python -m pytest macos/image-worker/tests
uv run --project macos/service mnemosyne-macos --check-config \
  --config "$HOME/Library/Application Support/Mnemosyne/config.yaml" \
  --env "$HOME/Library/Application Support/Mnemosyne/.env"
cd macos/app && swift build && swift test
```

Nyx Fleet development and representative acceptance:

```bash
uv sync --directory fleet --frozen --extra dev
uv run --directory fleet --frozen --extra dev python -m pytest -q
uv run --directory fleet --frozen python -m compileall -q \
  src/mnemosyne_fleet
uv run --directory fleet --frozen \
  python ../scripts/fleet_acceptance.py --url http://nyx:17400 \
  --model <public-model> \
  --require-node-service-class <primary-mac>=primary \
  --require-node-service-class <limited-nyx-worker>=overflow \
  --require-platform macos
```

## Verification Expectations

For docs-only changes, a readback, relative-link audit, and `git diff --check` are enough. When deleting or renaming documentation, remove references from the README, examples, and this guide in the same change.

For Python or CLI changes, prefer at least:

- `python -m py_compile vllm_manager.py cuda_residency.py fleet_protocol.py usage_normalization.py config.py catalog.py profiles.py runtime.py downloader.py download_worker.py hf_search.py repo_probe.py pg_writer.py logsetup.py`
- `bash -n vllm-ctl`
- `python -m pytest -q`

For UI changes, run from `ui/`:

```bash
npm test
npm run build
```

For native service changes, run
`uv run --project macos/service --extra dev python -m pytest macos/service/tests`.
For MFLUX worker changes, run its independent suite under `macos/image-worker`.
For menu/bootstrap changes, run `swift build` and `swift test` from `macos/app`.
LaunchAgent registration, Metal memory release, and real engine swapping still
require the target Mac and `macos/smoke_checks.md`; full Xcode is required for
the packaged `SMAppService` smoke.

For Fleet changes, run its independent locked suite, build a wheel, verify the
canonical and packaged snapshot schemas are semantically identical, and run
the target-host procedure in `project_docs/fleet_acceptance.md` before calling
the multi-node rollout complete.

When behavior touches process launch, ports, engine argv construction, Docker mounts, or GPU behavior, add or run targeted tests and call out any manual Docker smoke checks that still need a CUDA host.

## Safety Notes

- Never discard user changes in this repository.
- Avoid destructive git commands unless the user explicitly requests them.
- Cache wiping must remain path-safe and catalog-aware.
- Do not let admin auth, inference bearer handling, cookies, or authorization headers leak to the inner engine.
- If `ADMIN_PASSWORD` is unset, admin bind must continue to fail safe to container loopback.
- Keep multimodal request payloads opaque through the proxy.
- If vLLM is bumped, rebuild the image and regenerate `vllm_supported_architectures.json` from the new runtime registry.
- If the CUDA llama.cpp pin is bumped, rebuild the image, check `llama-server`
  CLI compatibility, and verify the CUDA-linked binary with GPU passthrough.
  Native llama.cpp updates instead use the official-source integrity and
  activation checks described above.

## Useful Reading Order

1. `README.md`
2. `project_docs/project_status.md`
3. `vllm_manager.py`
4. `config.py`, `catalog.py`, `profiles.py`, `runtime.py`
5. `downloader.py`, `download_worker.py`
6. `hf_search.py`, `repo_probe.py`
7. `pg_writer.py`
8. `vllm-ctl`
9. `Dockerfile`, `config.yaml.example`, `.env.example`, and `docker-compose.example.yml`
10. `ui/src/`
11. `project_docs/smoke_checks.md` for CUDA-host validation
12. `macos/INSTALL.md`, `macos/README.md`, then
    `project_docs/macos_native_architecture.md`, for the native sibling
13. `project_docs/mac_pool_architecture.md` and
    `project_docs/mac_pool_acceptance.md` for the Mac-first pool target
