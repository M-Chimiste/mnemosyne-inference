# Mac Pool Acceptance Contract

## Purpose

This document lists the evidence required before the Mac-first pool can be
called production-ready. A schema, mock, ad-hoc build, or successful local
unit test is not a substitute for the real-host gates identified below. All
reports must be bounded and secret/content redacted.

## Evidence rules

- Automated suites run from a clean checkout with locked dependencies.
- Real-host reports identify the exact app version/build, signed artifact
  digest, protocol/catalog version, macOS version, and hardware class.
- A persisted pre-run snapshot is not evidence for a transition the candidate
  did not observe.
- Routing evidence never includes prompts, responses, credentials, paths,
  bookmarks, arbitrary upstream diagnostics, or model-card text.
- Failure injection proves the final durable state, not only an HTTP result.
- Existing v1, native-engine, coordinator, storage, usage, installer, runtime,
  packaging, and Swift tests are mandatory non-regression suites.

## Current slice accounting (2026-08-30)

Implemented automated foundations currently include durable Mac-local
join/pause with full-response Fleet leases, Hub service classes and limited-
worker overflow scheduling, the opt-in Hub pairing API/store/locator/pinned-
probe/dynamic-registry slice, Mac-local pairing journal and Fleet-only
credential authority, a path-free non-loading activation catalog, and separate
strict `MacInventory`/`DesiredInstall` v1 schemas with golden fixtures. The Mac
now produces authoritative path-free inventory and synchronizes exact
observations through its management credential; Nyx persists them with
restart/replay/generation/revocation fences. The signed-catalog
verification/last-known-good core, strict HTTPS update client, Fleet lifecycle
integration, and pure explainable Mac-by-storage placement scorer are
implemented. Nyx exposes admin-authenticated, no-store, bounded catalog
status/model/recipe pages and a closed advisory placement HTTP surface behind
independent default-off switches. Pairing is off by default, static enrollments
remain compatible, and the accepted production flow activates a new pairing
Hub-disabled before a distinct admin enable.

Nyx now also implements the default-off, Hub-only DesiredInstall authority:
strict schema validation, an independent bounded private journal, explicit
candidate-basis revalidation, idempotent create/replay/conflict, exact-ID
read/list, revision-fenced cancellation, outbound-sync delivery, monotonic bounded
acknowledgements, restart recovery, expiry, and pairing/catalog/instance/
storage-generation fences. Its unified dashboard presents secret-free Mac
hardware/service class/participation, opaque storage and model inventory,
signed recipes, deterministic placement explanations, explicit target
selection, and DesiredInstall progress. Nyx still does not implement any
download, directory, registration, runtime, cleanup, delete, or inference
mutation and never receives a local path or bookmark.

The selected Mac now owns a durable DesiredInstall inbox/executor and Swift
approval/progress/refuse/cancel surface. Before the existing native downloader
runs, the Mac revalidates exact pairing/service/catalog/recipe/artifact/storage
authority, capacity, and cancellation. Downloads remain residency-neutral;
registration uses the ordinary native profile path and leaves the model cold
for existing coordinator-owned JIT loading. Exact signed GGUF primary/shard/
optional-projector roles and complete provenance are enforced. Signed oMLX
installs additionally require authenticated GET-only proof of service-global
scheduler/memory-guard settings at install, load, and Fleet-advertisement time;
drift is ineligible and zero-capacity without mutating oMLX. Restart restores
the hidden signed binding, and a later journal read fault retains the last
exact classification instead of downgrading it or fencing an already-proved
ordinary local profile.

The Swift app implements the invitation/claim/provisioning/activation
begin/resume ceremony with memory-only invitation handling. The same Inference
Pool page now exposes a confirmed permanent Mac self-revoke action and exact-
request **Retry Removal** recovery through the loopback control route. The
existing policy is unchanged: a configured `ADMIN_PASSWORD` requires Basic
authentication, while an unset password on the default loopback bind uses the
same-user/local-process trust boundary; non-loopback control fails closed
without a password. Its separate Migration & Removal page performs path-free three-mode
previews and fresh-confirmation, journal-only preparation. The service has a
private exact-path retention manifest, path-free lifecycle journal/API, and a
primitive-free observed-effect executor core; real lifecycle execution remains
disabled until a signed authenticated bundled helper is integrated and
accepted.

Those automated foundations do not clear the product gates below. No
production catalog signing key or live compatibility entry ships in this
slice; operators must supply production public trust anchors and an update
endpoint explicitly. Routine pairing rotation, static-adoption cutover,
remote administrator-to-Mac revocation notification, lifecycle integration,
the lifecycle helper, credentialed signed/notarized distribution, and
representative multi-Mac hardware acceptance remain pending. Route-correlated
accounting is implemented, but still requires multi-host acceptance. Unit,
schema, simulated-loopback, ad-hoc app, and unsigned-DMG tests must not be
reported as completed product workflows.

At every later gate, “preserve storage” means preserve the exact lexical folder
the owner selected, including nested/symlink spelling, external-volume UUID,
scope reference/bookmark, per-engine destination, and ownership/provenance.
There is no implicit default-directory fallback, relocation, consolidation, or
weight deletion during pairing, inventory, download selection, migration,
pause, revoke, rollback, or uninstall retention.

### Hermetic Phase-1 integration harness

`macos/service/tests/test_mac_pool_phase1_acceptance.py` is the bounded
loopback integration check for the first request-path slice. It composes two
distinct `NativeRuntime` instances with real `ResidencyCoordinator` objects,
real per-node SQLite analytics/outbox stores, and the real native inference
ASGI applications. The real Fleet registry, scheduler, proxy, route store, and
ASGI application consume authoritative macOS snapshots and route two
concurrent requests. One primary-class Mac and one limited overflow Mac both
start cold, each JIT-loads once, each serves one request, and each durably
records exactly one content-free event under Fleet's route ID. Only the
external oMLX adapter and its deterministic upstream response are substitutes;
no CUDA node or fake usage counter participates.

This harness is not release evidence for the signed pairing ceremony,
multi-host transport, signed-catalog compatibility, inventory/placement,
selected-storage download execution, protected-folder grants, restart
recovery, Postgres delivery, real Metal inference, or signed/notarized app
artifacts. Those remain pending gates and require their named automated and
real-host evidence below.

## Audited native compatibility baseline

The following test groups are the minimum automated evidence for behavior that
already exists. This map prevents a new pool test suite from passing while an
older native feature is omitted. It identifies test sources, not a permanent
claim that they passed: the release ledger must record a clean locked run for
the exact candidate commit and artifact.

| Existing surface | Mandatory automated evidence |
| --- | --- |
| All seven inference POST routes, `/v1/models`, language-body opacity, and the bounded image normalization surface | `macos/service/tests/test_mac_pool_nonregression.py`, `test_app.py`, `test_proxy.py`, and `test_usage.py` |
| llama.cpp generation/embedding/rerank modes and managed process/storage recovery | `test_llamacpp_adapter.py`, `test_config.py`, and `test_app.py` |
| oMLX generation, Messages, embedding/rerank classification, native capacity/context/cache control, and library rescan | `test_omlx_adapter.py`, `test_local_models.py`, `test_local_adoption.py`, and `test_app.py` |
| DS4 generation/Messages, exact upstream family registration, batching capacity, and managed process ownership | `test_ds4_adapter.py`, `test_local_adoption.py`, and `test_app.py` |
| MFLUX image generation and failure fencing; retired-engine config preservation and execution exclusion | `test_mflux_adapter.py`, `test_config.py`, `test_model_library.py`, and `test_app.py` |
| JIT loading, single global residency, same-target coalescing, FIFO switching, full-stream leases, queue bounds, idle unload, and maintenance recovery | `test_coordinator.py`, `test_app.py`, and `test_benchmarking.py` |
| Exact custom/nested/symlink folder spelling, volume identity, and engine-scoped destinations with no fallback relocation | `test_mac_pool_nonregression.py`, `test_storage.py`, `test_local_sources.py`, and `test_local_adoption.py` |
| Receiver-owned bookmark privacy/reactivation, scoped child execution, timeout/cancellation, and containment/header safety | `test_security_scopes.py`, `test_scope_process.py`, `test_filesystem.py`, `test_llamacpp_adapter.py`, `test_ds4_adapter.py`, and `test_mflux_adapter.py` |
| Managed versus imported provenance, durable exact destinations/file sets, downloaded-unregistered retry, local scan/adoption without loading or copying, and ownership-bounded cleanup | `test_install_store.py`, `test_installer.py`, `test_model_library.py`, `test_local_models.py`, `test_local_adoption.py`, and `test_app.py` |
| Content-free per-request usage normalization, local atomic persistence, streaming parsing, and durable delivery | `test_usage.py`, `test_usage_store.py`, `test_usage_delivery.py`, and `test_app.py` |

Unified-inventory round trips, selected-Mac remote installs, tiered uninstall
planning, and migration/rollback orchestration now have dedicated automated
coverage, but that evidence proves only the local protocol and inert/planned
transitions. The real-host gates in sections E, F, J, and K remain blocking
until the production signing authorities, lifecycle helper, and representative
hardware workflows complete them. They must prove semantic equivalence against
this baseline, including exact node-local storage identity and the absence of
any implicit weight relocation.

## A. Clean installation and distribution

### Automated

- The staged app contains the locked native service, all required managed
  stable-path helpers, the direct LaunchAgent bootstrap, and no undeclared
  developer checkout paths.
- App, helper, framework, runtime, package lock, appcast, tag, and DMG versions
  agree.
- Inside-out signing, hardened runtime, designated requirements, and Sparkle
  signature verification pass.
- A pristine config retains exact default internal storage behavior and every
  current inference route.

### Real Mac

- On a supported clean Apple Silicon Mac, dragging the notarized/stapled DMG
  app to Applications reaches a healthy control and inference plane without a
  terminal or source checkout.
- Gatekeeper accepts the app and helpers; Background Items approval is clear
  and recoverable.
- A managed stable recipe can install its runtime/model and pass self-test
  without Xcode Command Line Tools or Homebrew on the required stable path.
- Restart, logout/login, and in-place Sparkle update each produce a new exact
  service instance while preserving configuration, models, usage, and
  participation state.

## B. Pairing, identity, and revocation

### Automated

- Pairing codes expire, are single-use, rate-limited, and cannot be replayed.
- Issued snapshot, dispatch, management, client, admin, and ledger credentials
  are distinct and least privilege.
- Enrollment, node identity, replay fences, Hub enablement, service class, and
  local participation survive the relevant restarts.
- New pairings complete activation with Hub enablement false and remain absent
  from routing until a separate authenticated administrator action enables
  that exact enrollment.
- A node cannot self-promote its service class or override Hub disablement.
- The local `POST /manager/fleet/pairing/revoke` surface accepts
  only the closed version-1 body and one canonical request UUID. The signed UI
  requires explicit confirmation and never treats the participation toggle as
  permanent removal.
- Acceptance records whether the existing control password was configured. If
  it was, the route requires Basic user `admin`; if it was not, evidence must
  identify the default loopback same-user/local-process trust boundary rather
  than calling the route authenticated. Non-loopback control without a password
  remains a startup failure.
- Mac-initiated revocation persists an exact request fence before the Hub call,
  closes snapshot/dispatch admission immediately and after restart, replays
  only the same request after ambiguity, and never calls the Hub again after a
  committed response. The resulting tombstone retains identity/generation
  while the exact pairing-owned snapshot, dispatch, and management credentials
  are removed; changed or static credentials are preserved and old keys fail.
- Secret-free pairing status returns the exact pending request ID and only its
  fixed `pending`/`hub_committed` phase. App and service restart recover that
  identity, **Retry Removal** reuses it, and a different request conflicts.
- A proven terminal Hub rejection atomically retires only that request and
  restores the unchanged current pairing after commit. The retired ID never
  affects a later generation; ambiguous outcomes remain denied and exact-ID
  retryable, including across cancellation and restart.
- A completed revoke accepts only a new invitation ceremony. Re-pairing creates
  a new pairing identity directly from the credential-free revoked tombstone,
  without a hidden clear step, while preserving the Mac reporting identity and
  prior per-device token-attribution continuity.
- Self-revoke never changes or deletes a local model, exact configured weight
  path/volume/bookmark binding, inference profile or load setting, local
  inference state, analytics row, token history, or durable usage outbox.
- Unsupported protocol versions and downgrade attempts fail closed.

### Multi-host

- Pair two Macs from the signed UI, restart Nyx and both Macs, and observe the
  same device identities without re-pairing.
- Revoke one offline Mac, bring it back, and prove it cannot regain authority
  from persisted snapshots or credentials.
- Permanently remove one online Mac from the signed UI, interrupt it before and
  after Hub commitment, recover with the same request ID, and re-pair it only
  with a new invitation. Prove the exact models, weight paths, profiles, and
  token history are unchanged throughout.
- No node URL or credential appears in dashboard, logs, route history, crash
  reports, or exported acceptance evidence.

## C. Local participation and drain

### Automated

- Default behavior preserves existing enrolled-node routing after upgrade.
- Pausing atomically closes only Fleet admission and advertises zero available
  capacity while local `/v1/*` remains callable.
- A stale new Fleet reservation receives pre-work `429`, `Retry-After`, and the
  manager-owned `X-Mnemosyne-Error: node_busy` proof.
- An active Fleet stream retains both reservation and model lease through its
  terminal body, then releases each exactly once.
- State transitions `available -> draining -> paused`; joining requires no
  new pairing and revalidates node health before advertising capacity.
- Pausing does not unload a model unless ordinary idle policy later does so,
  cancel downloads, or stop usage delivery.
- Pause is the reversible, graceful-drain control. Permanent removal revokes
  the pairing and therefore requires a new Nyx invitation before the Mac can
  join again.
- The target-Mac acceptance collector's bounded participation exercise refuses
  active Fleet work, proves idle pause and rejoin, restores the exact prior
  preference, and requires the model/runtime/storage configuration to remain
  unchanged without exporting local paths.
- The collector's status read and participation update are separate HTTP
  operations, so this local exercise is not evidence that no request could
  enter between them. Run it only while the node is Hub-disabled or otherwise
  quiesced. A raced request that produces `draining` fails the exercise while
  retaining its request lease; closing that admission window is a separate
  service-side conditional-mutation contract.

### Multi-host

- Pause one Mac during a long stream. The stream completes, new work routes to
  another eligible Mac, and the paused Mac continues to serve an authenticated
  local request.
- Restart while paused and prove it remains paired, visible, and unroutable.
- Join again and prove a cold installed model JIT-loads without redownload.

## D. Scheduling and limited/overflow compute

### Automated

- Eligibility applies every pairing, Hub, local, liveness, identity,
  capability, installation, runtime, and admission denial.
- Service class is evaluated before warm/cold tiers and routing weight.
- A warm overflow node never preempts an eligible primary solely due to
  warmth; it becomes eligible only under the configured overflow condition.
- Within a class, warm-free, warm-queued, empty-cold, and switch-cold order is
  preserved, followed by weighted least-outstanding selection.
- Pool and node queues are bounded FIFO; cancellation and timeout release all
  reservations exactly once.
- Retries remain limited to proven connect failure or pre-work node-busy; an
  ambiguous timeout or response body error is terminal.

### Nyx host

- Hub and colocated worker have separate identities, credentials, state,
  ports, and health.
- Saturating the limited worker cannot exhaust the Hub's reserved CPU, memory,
  storage I/O, descriptors, event loop, or dashboard/API responsiveness.
- Stopping/restarting either role does not signal or adopt the other role's
  process.

## E. Unified catalog and inventory

### Automated

- One logical model can contain multiple immutable artifacts and recipes
  without confusing aliases, capabilities, or deployment identity.
- Signature, schema, expiry, monotonic version, rollback, and known-bad catalog
  tests pass; corrupt or unsigned updates leave the known-good catalog active.
- Fleet snapshot v1 remains exact and rejects inventory fields. Its existing
  schema, producers, validators, examples, and golden vectors remain unchanged.
  `MacInventory` v1 has a separate route/schema package; its schema, producer,
  validator, examples, canonical encoder, and positive/negative golden vectors
  agree without importing snapshot v1 as an extension point.
- Inventory rejects unknown fields/major versions, noncanonical ordering,
  duplicate IDs, invalid enums, oversized strings/documents, row-limit
  violations, and inconsistent cross-field state.
- Credential-role tests prove only the exact active management generation can
  sync inventory; snapshot, dispatch, client, admin, local control, and ledger
  credentials fail. Redirects and ambient proxies remain disabled.
- Canary tests populate a hostname, IP/locator, absolute and symlink path,
  nested selected-folder spelling, install destination, storage name, mount
  path, volume UUID, scope ID, bookmark bytes, secrets, model-card text,
  free-form errors, prompts, responses, and diagnostics in every current local
  source. None appears in inventory, Hub metadata/WAL, API/dashboard payloads,
  logs, or evidence.
- Source reconciliation proves `/v1/models` is only the callable view, install
  rows provide managed provenance/progress without destinations/errors,
  configuration contributes retained disabled/imported/external profiles, and
  snapshot v1 alone remains live-routing authority.
- With absolute-path profiles configured, the Fleet activation probe returns
  only its dedicated path-free shape (or uses a dedicated no-load route). Raw
  `/v1/models.upstream_model` path values never reach Hub memory, persistence,
  logs, dashboard, or acceptance evidence, and the probe does not load a model.
- Managed download, local import, legacy migration, and external reference are
  distinct from exclusive-managed, user-owned, external-owned, shared, and
  unknown ownership. Managed source without exact ownership proof cannot be
  upgraded to exclusive-managed.
- Configured, queued, downloading, partial, verifying,
  downloaded-unregistered, registered, failed, and cancelled lifecycle states
  remain separate from availability and cold/loading/warm/draining/unloading
  residency. A missing volume does not rewrite a registered row as a failed
  download.
- Authoritative catalog/revision/file-set/digest identities correlate replicas;
  alias equality and raw local paths do not. Unverified local models retain
  node-scoped IDs and cannot claim replica or Fleet eligibility.
- Runtime rows distinguish managed/bundled/external ownership and every
  available/missing/disabled/known-bad/unsupported/unhealthy intrinsic state;
  installation/recommendation rows prove recipe-specific verified/unverified/
  missing/mismatch/known-bad compatibility. Hardware inputs carry measured,
  catalog-tested, calculated, or conservative evidence labels and never
  overstate estimator output as measurement.
- Random storage IDs are not derivable from path/name/volume/scope/hardware,
  survive an unchanged restart, are never reused after deletion, and retain a
  private exact binding. Changing path, expected volume, or scope increments
  binding generation while preserving the newly selected lexical path.
- Receipt-time freshness tests cover late/duplicate sequences, new service
  instances, credential rotation, clock skew, expiry, Hub restart, and replay.
  Persisted observations remain display-only until a fresh authenticated sync.
- Paused/offline nodes and managed, imported, external, partial,
  downloaded-unregistered, registered, unavailable-volume, disabled-engine,
  and incompatible-runtime rows remain visible but ineligible where required.

### Multi-host

- The Hub shows which exact Macs hold each logical model/artifact and whether
  each copy is verified, available, cold, warm, partial, or unavailable.
- Restarting Nyx does not make persisted inventory authoritative for routing;
  a fresh increasing snapshot is required.
- Disconnect and reconnect an external model volume. Inventory changes to
  unavailable and back without forgetting or rewriting the exact local path.
- Change a registered storage folder, volume, and protected-folder grant in
  separate trials. Each advances the private binding generation, invalidates
  stale job selections, and never exposes either old or new path/grant data.
- Compare inventory against local `/v1/models`, configured disabled profiles,
  hidden install history, imports, partial jobs, and runtime status on each Mac;
  every local row is accounted for exactly once without turning an unavailable
  row into routing authority.
- Let inventory age past its bounded TTL while Fleet snapshots remain healthy,
  and vice versa. The first case preserves routing but blocks new placement and
  remote-job decisions; the second preserves catalog display but blocks
  routing. Neither protocol substitutes for the other.

## F. Placement and selected-machine downloads

### Automated

- Hard gates cover platform/OS, runtime compatibility, unified-memory reserve,
  weight/runtime/KV/context/concurrency budget, registered storage headroom,
  capabilities, consent, and node policy.
- Native-producer tests use the bounded `system_profiler` JSON contract and
  prove valid, timeout, malformed, and ambiguous GPU observations. GPU cores
  are never inferred from the SoC name: unknown cores hard-gate a signed
  minimum, and no profiler payload or arbitrary diagnostic reaches inventory.
- A cross-component test runs the actual Mac inventory producer, validates and
  persists its strict document in the Hub inventory store, resolves a signed
  recipe whose `metal`, `unified-memory`, `apple-metal`, and
  `flash-attention` requirements remain intact, and obtains an eligible
  conservative placement from that stored observation.
- For one scorer/catalog/inventory basis, candidate ordering and output are
  deterministic. Every result records scorer version, catalog digest,
  inventory instance/sequence/receipt, storage ID/generation, expiry, fixed
  hard-gate/reason codes, and measured/catalog-tested/calculated/conservative
  evidence labels.
- Placement hard gates use the node's allocatable memory after local reserve
  and account separately for weights, runtime overhead, KV/context, requested
  concurrency, and safety headroom. Boundary/overflow arithmetic and unknown
  evidence fail conservatively.
- An existing verified exact artifact and compatible runtime are recognized
  without conflating a same-named alias, different revision/file set,
  capability set, recipe, or unverified import.
- `health: unknown` on a present compatible runtime yields conservative
  `compatible_unverified` placement evidence; explicit degraded/unhealthy
  evidence remains a hard gate and never grants inference authority.
- Missing runtimes are ineligible in DesiredInstall v1. Managed and
  local-approval recipe modes expose fixed local-preparation reasons, and both
  candidate selection and delivery fencing refuse `install_managed` or
  `install_requires_approval` states until a separate runtime-preparation
  protocol is implemented.
- The Hub sends `DesiredInstall` only through the selected Mac's authenticated
  outbound sync. The payload contains job/idempotency IDs, exact inventory and
  catalog basis, recipe/artifact/contract, optional bounded alias, and storage
  ID/generation. Schema/golden-vector tests prove it cannot carry a URL,
  arbitrary repository/file selection, path/destination, volume/scope value,
  engine argument, credential, or delete operation.
- Arbitrary/unknown/retired storage IDs, stale binding generations, expired or
  stale inventory bases, changed credential generations, unsigned/changed
  catalogs, unknown recipes, mismatched artifacts/capabilities, and cross-node
  jobs are rejected before directory creation or network download.
- The Mac revalidates the exact private path/volume/scope binding, availability,
  writability, free-space headroom, runtime, recipe, and policy before creation
  and registration. Every failure leaves all alternative/default locations
  untouched; there is no fallback relocation.
- The durable idempotency tuple is recorded before work. Exact replay after
  response loss, process restart, or Hub restart returns the same local job and
  files; key/job reuse with a changed canonical payload returns fixed
  `idempotency_conflict` without work.
- `allow`, `ask`, and `local-only` policies are enforced locally. `ask` performs
  no upstream request before approval. Pausing inference participation neither
  approves nor cancels an install and does not make a local-only node remotely
  installable.
- Retry/restart reuses the same job and files. Downloaded-but-unregistered
  state retries registration without redownloading.
- Revision/file/digest verification failure never registers or loads a model.
- Signed oMLX recipes prove exact scheduler slots and any required memory guard
  through authenticated GET-only evidence before download/registration and on
  every load/Fleet snapshot. Drift and malformed/missing evidence leave the
  alias cold and visible but non-loadable, Fleet-ineligible, and zero-capacity;
  no global-settings mutation occurs. Restart and post-start journal-fault
  tests prove a signed target cannot be silently downgraded while an exact
  known-ordinary local target remains ordinary.
- Completion leaves the registered model cold. Concurrent first inference
  requests still coalesce through the existing JIT residency coordinator.
- Cancellation is not deletion; it retains exact durable partial/downloaded
  state under existing installer policy. Remote cleanup is absent from the
  initial desired-install schema.
- Remote deletion cannot remove imported, external, shared, ambiguous, root,
  escaping, symlinked, or wrong-volume data.

### Multi-host

- For representative low-, medium-, and high-memory Macs, recommendation order
  and explanations match the approved catalog evidence and memory estimator.
- Select a non-default eligible Mac and one of its registered internal or
  external storage locations. Only that Mac downloads directly from the
  upstream; Nyx never carries weight bytes.
- Select an eligible non-recommended Mac and verify the UI preserves that exact
  confirmed Mac/storage basis, shows the reasons, and does not silently switch
  either choice when a fresher inventory changes recommendation order.
- Pause, disconnect, restart, and resume during a large download. Progress is
  durable, the exact selected storage/volume is retained, and no lookalike
  folder is created on another disk.
- After job creation but before Mac receipt, change the selected storage path,
  expected volume, and grant in separate trials. The stale binding generation
  is refused on that Mac and no bytes appear on the old, new, internal-default,
  or another Mac's storage until the user confirms a fresh recommendation.
- Exercise `allow`, local `ask` approval/refusal, and `local-only` across three
  Macs while inference participation is joined and paused. Remote-install and
  inference-participation state remain independent.
- Lose each Hub response at `received`, `downloading`, `verifying`,
  `downloaded_unregistered`, and `registered`; repeated sync converges on one
  local job, one exact destination, one profile registration, and no duplicate
  download.
- After verified registration, the model remains cold until requested. Ten
  concurrent same-model requests coalesce to one load and use engine-local
  batching/capacity.

## G. Inference non-regression

### Automated

- Every current native adapter suite passes: llama.cpp, oMLX, DS4, and MFLUX.
- Every current language/image route preserves request fields outside the
  manager-owned rewrite/normalization surface.
- `/v1/models` lists all resolved callable profiles while no model is loaded.
- Disabled or unavailable engines do not prevent the control plane or Settings
  UI from starting; their profiles remain persisted but uncallable.
- Coordinator tests retain single residency, same-target load coalescing,
  FIFO anti-starvation, full-stream leases, global-empty verification, bounded
  queues/timeouts, engine-derived capacity, idle unload, and maintenance
  recovery.
- Fleet tests retain strict deployment/capability mapping, warm-first routing,
  reservation accounting, replay fencing, bounded queues, retry semantics, and
  secret-free dashboards.

### Representative Macs

- Self-tests cover every supported route/capability for the engines declared
  Stable on that release.
- DeepSeek V4 Flash and GLM 5.3 Flash run only through recipes whose exact
  artifact/runtime/hardware combinations passed their declared compatibility
  tier; unsupported Macs receive an actionable refusal, not a crash or swap
  storm.

## H. Storage non-regression

### Automated

- Exact nested selected folders and lexical symlink selections round-trip
  unchanged through save, restart, inventory, migration, rollback, and
  uninstall retention manifests.
- Volume UUID mismatch, missing volumes, stale bookmarks, containment escapes,
  unsafe headers, and blocked filesystem helpers fail closed while both HTTP
  planes remain responsive.
- Bookmark bytes stay mode-`0600` under the private mode-`0700` scope root and
  never enter YAML, APIs, logs, or evidence.
- Every scoped helper, engine, and download child reactivates the receiver-owned
  grant before `exec`.
- Existing imported-model adoption and managed/imported cleanup tests pass.

### Real Mac

- Internal storage, a nested external SSD folder, a protected folder requiring
  a grant, and a lexical symlink-selected library each survive restart and a
  signed in-place update.
- Ad-hoc-to-Developer-ID migration requests reselection when identity requires
  it and never silently replaces or deletes the old grant/path.

## I. Per-device token accounting

### Automated

- Every completed language request commits exactly one local analytics/outbox
  event before accounted success; event IDs remain stable across retries.
- Concurrent service connections atomically reserve both the Fleet route UUID
  and final outbox slot before model/JIT admission; exactly one contender wins
  and the loser performs no engine work.
- Fleet events bind route ID, paired node ID, deployment ID, public mapping
  version, endpoint, totals, latency, status, and fixed runtime identity.
- Local analytics and outbox are atomic; central insertion is idempotent; an
  ambiguous network commit plus retry produces one row.
- Stream cancellation/interruption and upstream/body failures follow the
  declared accounting policy without double writes.
- Outbox pressure never silently drops undelivered rows; it closes or degrades
  admission with an explicit fixed code.
- A 2xx Fleet language response without recognized usage cannot release a
  success body/terminal event, cannot fabricate zero counts, and leaves a
  bounded post-dispatch replay fence. Completed content-free error/image
  fences are capped without pruning active work or durable usage/outbox
  duplicate evidence.
- Pausing participation and being offline do not stop delivery retries.

### Multi-host

- Send concurrent streaming and non-streaming work across multiple Macs, force
  ledger disconnect/reconnect and Nyx restart, then reconcile one normal event
  per completed route under the actual serving device.
- Pool aggregates equal the bounded per-device rows; Nyx route metadata and
  Fleet SQLite contain no token rows or content.

## J. Migration and rollback

### Automated

- Read-only discovery produces a private versioned inventory and redacted
  summary without mutation.
- A complete sealed recovery clone and its narrow signed helper are staged and
  reverified before the predecessor app or service can become unavailable.
- The predecessor is the exact installed signed application identity. The
  candidate retains the same signing Team, has a complete exact-entry bundle
  inventory bound to its signed identity, and occupies a lexical tree
  disjoint from both the installed app and recovery clone before any effect.
- Helper authority is transaction/digest/session/nonce bound, short lived,
  obtained only after local owner authentication, carried only over an
  inherited socket pair, and denied to an unverifiable peer. There is no named
  listener, permanent helper daemon, or arbitrary path/PID/port/label/argv
  primitive.
- A consistent snapshot contains config, environment/credential references,
  SQLite/WAL/outbox identity, runtime pointers, model provenance, exact storage
  and grants, registration state, legacy evidence, pairing, and participation.
- Failure injection at every journal transition restores the exact predecessor
  and is idempotent after process/power interruption.
- Pre/post semantic comparison covers every alias, engine, artifact revision,
  projector, capability, context/load/parallel setting, alternative/pin,
  route, storage identity, ownership class, runtime pointer, usage/outbox ID,
  reporting identity, pairing, and participation preference.
- Legacy sidecar retirement cannot proceed before identity/DSN/outbox evidence
  is durably inherited and validated; rollback re-enables only the exact prior
  service identity.

### Existing and representative Macs

- Migrate supported current installations through the signed UI without a
  checkout or terminal. Validate both planes, storage grants, all retained
  models, local and Fleet inference, and usage continuity.
- Force candidate validation failure and prove automatic rollback restores the
  prior process, config, database, scopes, listeners, and inference behavior.
- Complete migration, restart/login-cycle test it, then intentionally run the
  supported application rollback and verify the matching data snapshot.

## K. Uninstall and retention

### Automated

- Dry-run manifests classify every target as app/service, Mnemosyne state,
  managed runtime, exclusively managed model, imported/external/shared model,
  or retained unknown.
- App-only uninstall unregisters exact jobs and removes no Application Support
  data.
- State/runtime uninstall keeps all weights plus a machine-readable retention
  manifest sufficient for later adoption.
- Full uninstall removes only proven exclusive managed data; recoverable model
  cleanup uses Trash where required.
- Interrupted uninstall resumes idempotently and never broadens its targets.
- The app and its LaunchAgent cannot be removed before the recovery clone is
  durable and authorized. Weight, runtime, outbox, Hub, and state dispositions
  complete before the installed app is quarantined/removed; the recovery clone
  self-removes only after a terminal receipt.
- The execution manifest enumerates exact state/runtime/scope/pairing members
  and exact exclusive-managed weight files. Changed inode, signature, storage
  binding, volume, scope, ownership, or provenance enters authenticated manual
  recovery instead of inferring success or deleting a root.
- Pairing revocation is explicit and independently confirmed; an offline
  uninstall records whether Hub revocation is still pending.
- A successful Mac self-revocation closes cached snapshot and dispatch
  authority before returning; no status poll or restart is required. If Nyx
  commits but the local tombstone/cache transition fails, the result is
  retryable outcome-unknown and only the same durable request ID may resume it.
- Pool self-revoke and lifecycle removal are independent confirmations. Neither
  the revoke route nor a migration/uninstall preview can invoke model cleanup,
  relocate an exact configured weight path, edit an inference profile, or
  delete token history.

### Real Mac

- Exercise all three levels on internal and external storage, then reinstall.
  App-only and keep-weights paths rediscover the intended data without moving
  it; full removal leaves imported/external/ambiguous data untouched.
- Verify no registered LaunchAgent, helper process, listener, or app-owned
  credential remains after the selected full-removal policy completes.

## Release condition

The Mac-pool release is ready only when the committed acceptance ledger names
the exact credentialed signed/notarized artifact and every required automated
and representative-hardware gate above is passed. Pending, stale, ad-hoc, or
different-build evidence keeps `release_ready` false.
