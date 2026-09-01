# Mnemosyne Inference — Project Status

**Last updated:** 2026-08-31

## Current state

**Active milestone:** M5 — Workstation-ready release

The major v1 feature set is implemented: persistent installs and catalog
state, two authenticated HTTP planes, lazy single-model swapping, vLLM,
llama.cpp, and SGLang Diffusion backends, the admin SPA, HuggingFace/GGUF discovery, token-usage
analytics, and the optional durable Postgres sidecar. `GET /v1/models` is
served locally from the catalog so clients can discover every installed alias
while no engine is resident. Known slow CUDA-graph-capture SSM/hybrid vLLM
families now default to eager mode to keep cold swaps interactive.

The image currently pins CUDA 13.0.2, PyTorch cu129, vLLM 0.22.1,
SGLang Diffusion 0.5.13 in a separate virtualenv, and llama.cpp b9548. The bundled vLLM architecture snapshot was refreshed with
the engine-pin update. The remaining release work is workstation/CUDA smoke
validation of the current pins and end-to-end backend swaps; see
[smoke_checks.md](smoke_checks.md).

The optional Nyx-hosted Fleet gateway is implemented as a separately locked
service under `fleet/`. Enrolled CUDA and native nodes expose a versioned,
secret-free snapshot through a dedicated credential. Nyx performs strict
deployment and capability matching, warm-first capacity-aware scheduling,
bounded FIFO queuing, and cancellation-safe full-response reservations behind
one public endpoint. Its realtime dashboard joins live node state and
metadata-only route history with bounded read-only aggregates from the
existing token ledger; nodes remain the only token-event writers. Isolated
fault-injection coverage and a content-redacted multi-node acceptance runner
are present. LAN/Tailscale deployment on Nyx and representative simultaneous
Mac/CUDA hardware evidence remain release gates; see
[fleet acceptance](fleet_acceptance.md).

Fleet now also has a single joined Mac overview for online/joined state,
hardware, path-free free storage, installed/resident models, active work,
queues, recent fixed-code errors, and a read-only five-minute token rate.
Optional request headers add bounded priority, exact enrollment affinity,
shorter maximum wait, and safe pre-work fallback policy without changing
ordinary clients. Bounded `/v1/batches` execution groups compatible
non-streaming requests through the same scheduler/proxy/accounting path in the
lowest-priority lane, with progress, cancellation, and ordered results.
Batch bodies/results are process-memory-only with per-item, aggregate, job,
concurrency, and retention caps; restart expires them and Fleet SQLite remains
metadata-only.

The macOS pilot now bundles that Fleet gateway behind a separate, explicitly
enabled **Hub Mode** login service. Fresh promotion is Hub-only by default:
the gateway stays on loopback `:17400` and does not enable, restart,
credential, or enroll the native worker. An explicit option enrolls that
separate worker on `:1240` as `overflow`; gateway and engine ownership remain
separate. Fresh authenticated snapshots automatically publish every
authoritative Fleet-eligible deployment into a durable universal routing
catalog. Exact replicas collapse, alias conflicts receive stable deployment
suffixes, and the dashboard supports durable remove/suppress plus explicit
re-add. The guided remote path configures Tailscale Serve HTTPS, while an
existing HTTPS proxy is an advanced alternative. Finder replacement and
disable/re-enable retain Hub credentials, pairings, inventory, catalog
overrides, and route metadata.

Mac-first pool hardening is now an active follow-on milestone. Several
non-breaking foundations are implemented:

- each Mac has a durable local **Contribute this Mac to the pool** join/pause
  control whose Fleet-only leases drain complete streams while local inference,
  downloads, JIT residency policy, usage delivery, and exact storage choices
  remain unchanged;
- Nyx enrollments have Hub-owned `primary`, `opportunistic`, and `overflow`
  service classes evaluated before warm/cold tiers, so an independently
  isolated limited Nyx worker can remain overflow-only instead of winning
  merely because it is warm;
- optional Hub-side pairing APIs now cover bounded invitation, claim,
  approval/rejection, claim-bound credential provisioning, pinned non-loading
  activation, explicit enable/disable, revocation, encrypted private secrets,
  restart reconciliation, and paired dynamic registry membership while
  preserving static-node compatibility; and
- the Mac service has a durable pairing journal, atomic private credential
  ownership, staged-versus-active Fleet authentication, a secret-free status
  route, and a path-free Fleet-marked activation catalog. Pairing-state failure
  leaves ordinary local inference available.

Pairing stays disabled by default and its routes are absent in that mode. The
production ceremony starts a new enrollment Hub-disabled and requires a
separate admin enable after activation. The current Swift app drives the
invitation/claim/provisioning/activation begin/resume ceremony with a
memory-only invitation secret, shows secret-free status, protects pairing-owned
credential fields, controls temporary participation, and now provides a
separately confirmed **Remove this Mac from Hub** action plus exact-request
**Retry Removal** recovery. Routine rotation, remote administrator-to-Mac
notification, adopt-static cutover, signed-artifact evidence, and multi-host
acceptance remain open.

The default interactive ceremony now wraps that boundary in a six-digit
presence-code flow. The Mac owner enters only the Hub's HTTPS origin; the app
discovers the Mac's Tailscale DNS name, requests a bounded five-minute hidden
invitation, submits the existing claim, displays the derived code, and polls
the exact attempt. An authenticated Hub operator enters the code in the Hub
Mac's native settings and chooses **Pair & Enable**. A wrong code consumes the
existing attempt budget, the code is never a credential, and the three role
credentials remain hidden from the browser and Swift diagnostics. The native
UI uses the private admin bearer only for loopback, redirect-free requests and
waits for the existing pinned activation probe to leave the enrollment
disabled before it issues the separate enable transaction. Manual invitation
and locator entry remain under the Advanced dashboard. Pairing does not change
model/storage state, token accounting, or
the default universal-catalog publication path.

Pairing status refresh now reconciles a waiting Mac with a read-only Hub claim
disposition using the exact durable claim request correlation. A restarted app
can therefore recognize an expired or rejected old claim even though the
invitation secret was correctly discarded from memory. Only a fully matching
terminal response enables the atomic **Discard Stale Attempt** recovery; Hub
unavailability, response ambiguity, active claims, and any locally assigned
credential remain fail-closed. The recovery path does not touch inference,
models, storage, participation, usage, or the token outbox.

Mac-initiated self-revocation now commits a secret-free fence for the exact
client attempt, invitation, pairing, generation, credential-bundle fingerprint,
and request ID before contacting Nyx. That fence closes snapshot and dispatch
admission immediately and after restart. Once Nyx has committed the revoke,
retries finish the local tombstone and removal of only the exact pairing-owned
snapshot, dispatch, and management credentials without contacting Nyx again;
changed or static credentials are refused rather than deleted. Malformed,
oversized, redirected, and otherwise ambiguous post-dispatch responses retain
the exact retry ID and denial fence. A proven terminal rejection retires that
request before reopening unchanged authority.

The completed reset accepts only the exact credential-free `REVOKED` tombstone
bound to the prior `COMPLETE` client attempt and endpoint fingerprints; missing
or corrupt tombstone fields and the impossible combination of a pending revoke
journal with local `REVOKED` state fail closed. A bounded retired-authority
history prevents stale request IDs, the old invitation or pairing ID, and the
exact old credential bundle from being restored by a later ceremony. It stores
only the credential fingerprint, never bearer values, and checks capacity
before latching or sending. The loopback control route and Swift confirmation
never invoke model cleanup or lifecycle execution: local models, exact
configured weight paths, inference profiles, local inference, token history,
and the durable usage outbox remain unchanged. A completed revoke can be
re-enrolled only through a different invitation; the reset preserves the
stable reporting identity and prior per-device accounting continuity.

The separate `MacInventory`/`DesiredInstall` v1 schemas and cross-component
golden fixtures have landed without changing frozen snapshot v1. Native Macs
now produce path-free inventory with durable opaque storage/installation IDs
and send exact retryable observations through the pairing-management role;
Nyx persists them behind restart/replay/generation/revocation fences and
exposes an authenticated administrative inventory view. A signed catalog
verifier/last-known-good store, strict HTTPS update client, failure-isolated
Fleet lifecycle, bounded admin metadata API, and deterministic explainable
Mac-by-storage placement API are implemented behind independent default-off
switches. The placement caller supplies only intent; Nyx stamps identity/time,
resolves signed recipe facts, returns every inventory-backed Mac/storage
candidate, and never selects a target or issues a job. Route-correlated
exactly-once accounting reuses Fleet's route UUID as the serving Mac's durable
event ID.

The Hub-side DesiredInstall v1 authority is implemented behind that same
hard-default-off switch. An administrator must submit one exact advisory
Mac×storage basis; Nyx re-resolves the signed recipe and recomputes current
placement before creating a path-free job in a separate bounded private SQLite
journal. Exact idempotency replay/conflict, restart recovery, expiry,
revision-fenced cancellation, authenticated outbound-sync delivery, monotonic
acknowledgements, and pairing/catalog/service-instance/storage-generation
fences are covered by Fleet tests. The unified Fleet dashboard now exposes
bounded, escaped, secret-free Mac hardware, participation/service class,
opaque storage inventory, installed/cold/warm model state, signed recipes,
placement explanations, explicit Mac/storage selection, and job progress.
Nyx still has no path, bookmark, downloader, runtime, cleanup, or inference
authority.

The selected-Mac executor is integrated with the existing native installer. It
independently revalidates the current pairing generation, service instance,
catalog digest and signature, recipe/artifact contract, opaque storage binding,
free space, cancellation, and exact destination before downloading. It
preserves the owner's lexical nested/symlink storage folder, volume UUID,
scope/bookmark, engine-specific destination, and provenance; downloads remain
residency-neutral and completed registrations enter the existing cold-model/JIT
path. Hidden-inclusive install rows retain exact signed launch contracts.
Catalog artifact roles support one exact GGUF primary, a complete shard set,
and one optional selected vision projector; missing, extra, mixed, duplicated,
or ambiguous layouts fail closed. DS4 remains projector-free and single-node.

Exact lexical weight location is also a tested registration invariant, not
just a placement preference. Automated coverage carries a user-selected nested
symlink spelling through opaque storage resolution, the download destination,
cold registration, JIT inference/accounting, and in-place legacy adoption
without resolving it to the physical target, copying weights, or falling back
to the default model folder. Registration reloads current Settings and requires
the same storage name, lexical root and destination, volume UUID, scope ID, and
revision recorded by the durable creation authority or current download
attempt. Rebinding any of them leaves the bytes at the original lexical
location, registers no profile, launches no fallback download, and remains
fail-closed across restart. A legacy post-download row with neither a durable
creation claim nor the originating process's binding snapshot also fails
closed on registration retry; it cannot infer authority from current Settings
or adopt a replacement volume/scope, and its downloaded weights are retained.

For signed managed oMLX recipes, the native manager uses authenticated GET-only
inspection to prove the externally owned service's exact scheduler slots and
required prefill memory guard before registration, request resolution, adapter
load/benchmark work, and every fresh Fleet snapshot. It never changes those
service-global settings as an install side effect. Post-install drift leaves
the alias visible but unverified, non-loadable, zero-capacity, and Fleet-
ineligible while unrelated models and node health remain available.
The binding is recovered from hidden install history on restart. If a later
install-journal index read fails, the runtime reuses only its last successful
classification for each exact unchanged oMLX target; unknown/changed targets
fail closed, and a signed target cannot become an unconstrained ordinary one.

The native installer also has additive local-only asset provenance keyed by
its immutable installation UUID. Historical rows and downloads into a
pre-existing unowned destination remain `managed_download` / `unknown`. A new
download into an absent app-created exact destination can become
`exclusive_managed` only after a separate complete canonical manifest proves
the creation claim, selected payload, directory identity, and live storage
binding. Native model cleanup now accepts an optional canonical installation
ID and grants managed cleanup only to the exact eligible row after profile,
live opaque storage binding, overlap, and exact regular-file manifest checks.
It revalidates that manifest in the bundled helper immediately before moving
the exact destination to macOS Trash, preserves provenance, and marks only that
row `trashed`; it never permanently removes the directory. Legacy payloads
retain fresh-scan imported llama.cpp/oMLX cleanup but cannot downgrade matching
hidden-inclusive managed history into that path. Per-model cleanup now uses a
crash-resume journal across Trash, SQLite, and config commits.

The native **Migration & Removal** page provides all three retention previews
and fresh-confirmation journal-only preparation. Exact paths exist only in a
private mode-0600 manifest; API/UI responses expose fixed component
dispositions and counts. Pending usage outbox state blocks state-removing
modes. A separate primitive-free lifecycle executor core models observed,
restart-safe uninstall/migration/rollback phases, exact exclusive-weight Trash
proofs, a product-wide execution claim, monotonic rollback intent, and durable
manual recovery. An expired or abandoned claim is never stolen: it blocks all
lifecycle work pending authenticated recovery. Production execution remains
inert until a signed authenticated bundled helper implements the closed effect
and recovery protocol and passes credentialed Mac acceptance.

Production catalog signing/publication, live model entries and trust anchors,
the authenticated lifecycle helper and execution ceremony, routine pairing
rotation/static-adoption cutover, remote revocation notification, Developer
ID/notarized distribution, and representative multi-Mac Apple Silicon
acceptance are not release-complete.
No catalog entry for a frontier model may ship until its exact weights, runtime
fingerprint, hardware class, context/capacity contract, and post-install
inference evidence pass. None of those remaining paths may centralize or
relocate weights:
existing exact lexical folders, nested external volumes, volume identities,
bookmarks, imported/shared ownership, and explicit per-install destinations
remain release-blocking non-regression surfaces. The complete target and gates
are tracked in [Mac pool architecture](mac_pool_architecture.md),
[pairing protocol](fleet_pairing_protocol.md), and
[Mac pool acceptance](mac_pool_acceptance.md).

The Mac-first setup and release-evidence surface now has four additional
bounded pieces. Model Library derives an advisory runtime preparation plan
from the selected model and exact DS4 channel, can hand off only to the
official oMLX DMG or the fixed Apple Command Line Tools dialog, and requires a
draft engine-enable change to be saved before offering restart. Those Apple
tool probes have a 15-second deadline and terminate/reap only their exact child.
Weight downloads remain separate, cold, residency-neutral, and bound to the
unchanged Download-to storage key. The compatibility catalog has an offline
encrypted-Ed25519 key/sign/assemble/verify ceremony with multi-signer rotation
and prior-sequence fencing, but no production key, anchor, endpoint, or live
recipe was added. Lifecycle owner authorization is now requested only through
the authenticated service `/authorization/perform` route; the obsolete menu
socketpair launcher is removed and the bootstrap pins the bundled helper path.
Because no OS-backed proof authority or effects runner exists, production
fails before helper spawn and execution remains unavailable. Finally, the live
acceptance collector can exercise an idle participation pause/rejoin, restore
the exact prior joined/paused preference, and prove the model/runtime/storage
configuration is unchanged without exporting local paths. It must run while
Hub dispatch is quiesced because the baseline read and update are separate
requests.

The lifecycle preparation audit now also binds a migration candidate to the
exact installed predecessor identity, signing Team, independently enumerated
candidate bundle inventory, and a non-overlapping lexical tree. The durable
effect journal validates one closed forward/rollback graph in order across
restart, but this is only a fail-closed validation fence: the production
helper cannot emit an owner proof and the runner implements no OS effect. The
credentialed design therefore requires a nested app-like helper with its own
profile-authorized Keychain identity and a non-exportable Secure Enclave P-256
key gated by macOS user presence. No migration/uninstall authority may move,
rewrite, or delete a retained model path; full removal may eventually Trash
only an explicitly selected, freshly re-proved exclusive-managed payload.

An isolated native macOS track is implemented, with frontier text-engine,
protected-storage, and migration-soak acceptance still pending. It lives
entirely below `macos/`, replaces the previous sidecar on `1240`, uses
`17321-17327` for control and inner engines, and does not alter
the CUDA image or its `8000-8002` topology. The first implementation includes:

- a separately locked FastAPI service with inference and control planes;
- a FIFO, epoch-lease coordinator that drains streams and verifies strict
  single-model residency across manager-owned llama.cpp/DS4/MFLUX and external oMLX;
- official-source managed llama.cpp plus native oMLX lifecycle integration and
  manager-owned DS4/MFLUX subprocesses; LM Studio engine, credential, and
  inventory support has been removed, leaving only read-only model-directory
  migration hints;
- packaged MFLUX discovery, installation, smoke checks, and worker launch use
  the exact relocatable image interpreter. The production bootstrap discards
  all ambient `PYTHON*` controls, installs only its bundled `PYTHONHOME` and
  closed source paths, disables user-site/unsafe-path loading, and ignores the
  development interpreter override whenever an embedded runtime exists;
- fresh native configuration starts with an empty model catalog so profiles
  come only from Model Library downloads or Finder-confirmed exact locations;
- OpenAI/Responses/Anthropic usage normalization plus an atomic SQLite
  analytics/Postgres-outbox path;
- a per-user LaunchAgent bundle, native Python bootstrap, and explicit AppKit
  status item with a SwiftUI controller popover;
- a structured native settings window for server, engine, exact GUI-selected
  model storage, Hugging Face discovery/downloads, model, usage, and write-only
  credential settings, including Finder-driven in-place GGUF/MLX discovery,
  LM Studio settings/default-folder hints that never contact its engine,
  model-card and GGUF/config metadata, exact quant selection, automatic vision
  projector selection with opt-out, and alias-preserving migration;
- optional local-network exposure for the public native inference listener,
  with a guarded Settings toggle, optional bearer authentication, an explicit
  unauthenticated warning, and loopback-only control/inner-engine listeners;
- receiver-owned durable Finder bookmarks stored privately across LaunchAgent
  restarts and scoped child `exec`, with bounded killable receipt/reactivation,
  configuration-save preflight, startup revalidation/pruning, only SHA-256
  `scope_id` values in YAML, and bounded killable filesystem helpers for
  protected paths; scope storage is anchored to the configuration directory
  rather than the configurable SQLite path;
- optimistic configuration revisions and a shared mutation lock prevent stale
  Settings windows from overwriting profiles created by downloads/imports;
- process-isolated, durable, cancellable native downloads with DS4/MFLUX
  verified recommendations, exact llama.cpp GGUF shard/projector downloads,
  oMLX compatibility signals, nested external-folder support, containing-volume
  UUID validation, live byte/percentage/speed progress, dismissible completed
  history, a compact persistent transition journal for target-Mac lifecycle
  evidence, residency-neutral profile creation, and exact manager-owned
  directory deletion behind the global empty-residency barrier;
- current DS4 discovery for its five DeepSeek V4 and four GLM 5.2 single-node
  targets, with exact Hub-file/revision validation and atomic eleven-shard GLM
  installation rather than broad unsupported GGUF claims, plus typed
  resident-session capacity for upstream scheduling/batching;
- a bounded private managed-runtime lifecycle journal that distinguishes
  service instances and proves activation, post-restart inference, rollback,
  post-rollback inference, and fixed-code corrupt-runtime rejection without
  retaining exception text; and
- independent setup, architecture, and Apple Silicon smoke documentation.

The native track previously added Preview adapters for mlxcel and mistral.rs.
They are now retired from the product surface: legacy schema-v6 engine and
profile values remain readable but are always inert, and upgrades retain their
external installations and model weights. Schema-v5 model profiles retain their original
engine as the fixed fallback, may attach one exact candidate per additional
engine, can pin a user-selected engine when quality should override speed, and
can opt into durable content-free benchmark selection. Automatic routing
requires fresh successful evidence for the exact ordered candidate set,
runtime, system, and suite; Preview winners require explicit consent. Fleet
excludes aliases whose local policy can select a non-primary engine, preserving
immutable deployment identity. The Settings sidebar exposes the packaged app
version and build beside the product name.

- Schema-v6 language candidates now carry detected-native, configured,
  effective, and locally verified context-window contracts. Settings can apply
  fresh exact-Mac profiling automatically, request the model-native maximum, or
  pin an explicit token limit. oMLX is inspected and configured through its
  official APIs, `/v1/models` advertises `max_model_len`, and an otherwise faster
  engine cannot win automatic selection by silently reducing usable context.
- Qwen3.8 request-level thinking controls are portable across stable native
  language engines. Unified Inference retains the model-native `low`, `medium`,
  and `xhigh` reasoning effort, normalizes thinking/preservation template
  controls, and translates one token-budget surface to the selected oMLX or
  llama.cpp dialect without persisting prompts or reasoning content.

The native product is now a 0.9.0 release candidate with one enforced version,
a guided Setup & Health flow, bounded secret-redacted readiness, real public
listener self-tests with durable usage verification, Stable/Preview engine
tiers, Sparkle integration, and separate CI/signed-release workflows. The
Python schema, packaged YAML, and Swift defaults now agree: llama.cpp is
enabled, external oMLX remains off until configured, and Preview DS4 and MFLUX
are opt-in. The build-47 private DMG was rejected after
installation because dyld
could not resolve its packaged Sparkle framework. Corrected build 50 adds the
`Contents/Frameworks` rpath and is independently verified against the
dependency, embedded framework, and load command
(`Unified-Inference-0.9.0-macos-arm64.dmg`, SHA-256
`c830fafaf08650eef055a7bdcff296481ff900ce2b329f6874c171ff884837ad`).
The previous hash must not be used. Build 50 is still not the Developer
ID-notarized V1 release. The precise contract and open gates are
[the native V1 scope](../macos/V1_SCOPE.md) and
[acceptance ledger](../macos/acceptance/v1.json).

The Python suites, Swift production build, relocatable runtime export, staged
app signing, embedded Python bootstrap, and Krea 2 Turbo MFLUX generation have
run on the development M4 Max. The official llama.cpp b10091 arm64 artifact
also passed its published size/SHA-256 and CLI contract checks and generated a
real response with an existing 4.8 GB LFM2.5 GGUF on Theseus. The managed
runtime then updated to official b10099, served a real request, rolled back to
b10091, and served again. An external official oMLX 0.5.3 service generated
from an existing LFM2 1B MLX model and its usage drained to the central ledger
under `theseus`. An earlier Developer ID-signed packaged service was installed
on Theseus and proved the direct
`Contents/MacOS/mnemosyne-service-bootstrap` LaunchAgent through
`SMAppService`; that historical smoke does not clear the current candidate's
signing, notarization, clean-install, or update gates.

The former LM Studio migration bridge passed a historical Theseus fallback
check before removal. The production design no longer contacts that server:
schema-version-1 LM Studio profiles become inert migration records, and only
Finder-confirmed adoption into llama.cpp or oMLX can make them callable.

The native handoff is live on Theseus using an installed bundle that predates
the current 0.9.0 candidate. The previous
`com.athena.token-sidecar` job is unloaded, Unified Inference owns the public
loopback API on `:1240`, and the unchanged TheseusInsight client successfully
returned content through both `lfm2.5-8b-a1b` and
`gemma-4-26b-a4b-qat`. Imported GGUF aliases no longer expose a `-gguf`
format suffix, central reporting is healthy under node `theseus`, and the
historical Developer ID-signed menu app is installed in `/Applications`. The unavailable
external oMLX service and its profiles are disabled on this host; oMLX is also
disabled in fresh defaults until its separately installed service is running.
The registered background service also passed a direct restart check while
idle: `launchctl` advanced from run 2/PID 87693 to run 3/PID 94187, both native
HTTP planes returned, `/health` reported `ok`, residency remained empty, and
central reporting resumed as `theseus` with an empty outbox. After that
restart, the existing `lfm2-1b-mlx` alias streamed `restart-ok` through the
native proxy with 17 prompt and 4 completion tokens, flushed one usage row,
then explicitly unloaded; the coordinator and authoritative oMLX inventory
both returned empty.
GUI Finder-confirmed migration, durable oMLX login startup, and login-cycle
validation remain in progress. The secret-redacted acceptance collector now
has opt-in exact-label restart and KeepAlive exercises plus strict
protected-model, oMLX recovery, LM Studio-directory adoption, Postgres drain,
durable download-lifecycle, candidate-scoped guided-setup, and real
login-session checks. Guided setup requires this exact version/build to record
first presentation before durable self-test completion. Login recovery
requires a private accepted same-host/build baseline and a changed GUI audit
session plus PID; an ordinary restart cannot pass. Those checks do not clear a
gate until the candidate actually produces the required state transitions.
Managed runtime acceptance now has the same durable treatment: the strict
collector requires an ordered update/restart/rollback/restart/rejection chain
and confirms the original managed version remains active.
The current CUDA/manager suite passes 478 tests, and the independently locked
Fleet suite passes 79 tests. The native service suite passes 402 tests on the
development Mac; two real-bookmark tests skip in restricted runners.
The isolated MFLUX worker passes 23 tests with real Metal access, the packaging
suite passes 33 tests, and all 73 current Swift tests pass. The Fleet wheel
contains its protocol schema and passed a clean build. The full relocatable
build reports version 0.9.0 and its
embedded service passes configuration validation without adding bytecode to or
invalidating the signed app. GitHub Actions macOS CI run
[30265133377](https://github.com/M-Chimiste/mnemosyne-inference/actions/runs/30265133377)
passed the complete native matrix for implementation commit `8fd50e9`,
including staged bare-app and acceptance-report verification. The DMG also
passes image verification, read-only
mount/layout inspection, and deep signature verification from the mounted
copy. Real DS4 model loading,
cancellation-driven Metal release,
protected-folder bookmark transfer/restart/child-`exec`, and packaged
`SMAppService` KeepAlive/login behavior remain target-Mac smoke gates;
see
[../macos/smoke_checks.md](../macos/smoke_checks.md).

## Phase status

| Phase | Goal | Status |
|---|---|---|
| 0 — Foundation & safety rails | Examples, test harness, vLLM pin | ✅ Done; download baseline fixed (2026-04-28) |
| 1 — Config, profiles, storage, catalog core | Declarative YAML config + SQLite catalog + profile resolver | ✅ Done (2026-04-28) |
| 2 — Runtime lifecycle, lazy load, queue, idle eviction | Profile-driven `_start_vllm`, swap queue, idle eviction | ✅ Done (2026-04-28) |
| 3 — Plane separation & auth | Two FastAPI apps (inference :8000, admin :8001), HTTP Basic | ✅ Done (2026-04-28) |
| 4 — Install, download, cache, multi-drive | `/manager/install` + cancellable subprocess downloads | ✅ Done; review fixes landed (2026-04-28) |
| 5 — HF search & vLLM compatibility filter | `GET /manager/hf/search`, runtime registry introspection | ✅ Done; bundled snapshot refreshed (2026-06-07) |
| 6 — Admin UI | React + Vite SPA on the admin port | ✅ Done; host verification complete (2026-04-29) |
| 7 — Packaging, compose, docs | Multi-stage Dockerfile, compose mounts, ops docs | ✅ Docs/CLI landed; CUDA quickstart smoke pending |
| 8 — Verification & hardening | Automated coverage and workstation acceptance | ⚠️ Code/docs landed; workstation acceptance pending |
| 9 — llama.cpp backend for GGUF | Auto-detected llama-server dispatch alongside vLLM | ⚠️ Code landed; CUDA workstation smoke pending |
| 10 — Local image generation | Unified Images API via CUDA SGLang Diffusion and macOS MFLUX | ⚠️ Mac Krea 2 smoke passed; CUDA model smoke pending |
| 11 — Native GGUF migration | Replace the Mac LM Studio dependency with managed llama.cpp and adopt existing libraries in place | ⚠️ LM Studio runtime dependency removed; service/runtime, direct GGUF, historical signed-package, and live LaunchAgent smokes passed; current 0.9 candidate migration/durability/signing gates remain |
| 12 — Nyx Fleet gateway | Strict multi-node discovery, capacity routing, concurrency, usage view, and realtime dashboard | ⚠️ Implementation and isolated coverage landed; Nyx + Mac + CUDA target-host acceptance pending |

## What has landed

**Phase 12**

- CUDA and native managers expose separately authenticated protocol-v1
  snapshots containing strict immutable deployment identity, residency epoch,
  bounded admission, derived/configured/effective concurrency, and redacted
  usage-delivery health.
- Both nodes use FIFO epoch leases through complete response streams. Different
  targets drain before unload, while unload, eviction, reconciliation,
  maintenance, and shutdown share fail-closed barriers.
- The Nyx service explicitly enrolls nodes, expires stale instances from its
  own monotonic receipt clock, and routes exact public model/deployment and
  capability mappings with warm-first weighted least-outstanding selection.
- Gateway retries are limited to proven pre-work failures. Cancellation-safe
  reservations, fixed metadata-only route history, separate client/admin/node
  credentials, ambient-proxy isolation, and a read-only ledger role preserve
  the data-plane trust boundary.
- `/fleet/` presents realtime node, resident, queue, capacity, route, and
  usage state without exposing node URLs or secrets. The serving node remains
  the sole durable token-accounting authority.
- CUDA catalog schema v5 gives local analytics the same event UUID as the
  delivery outbox and commits both atomically before response completion;
  ambiguous commits and restart retries cannot double-count model totals.
- The canonical schema, cross-platform identity vectors, security review,
  automated suites, and `scripts/fleet_acceptance.py` define the remaining
  target-host evidence rather than treating simulated engines as hardware
  acceptance. A bounded wire-level rehearsal now runs that real CLI against
  the real Fleet HTTP service and two separately authenticated macOS- and
  CUDA-labelled protocol nodes, proving deterministic fan-out, exact simulated
  per-node usage increments, and metadata-only persistence; it remains
  explicitly distinct from Nyx, engine, outbox/Postgres, and transport
  acceptance.

**Phase 10**

- `POST /v1/images/generations` is capability-gated on both deployments and
  initially supports `n=1`, base64 PNG, bounded width/height, seed, inference
  steps, guidance, and negative prompts. Image calls do not enter token usage
  analytics or the Postgres outbox.
- CUDA image profiles use `kind: image` with `backend: sglang-diffusion`.
  Qwen/Qwen-Image and krea/Krea-2-Turbo share the existing inner `:8002`
  lifecycle, so loading an image model unloads vLLM/llama.cpp and vice versa.
- SGLang Diffusion 0.5.13 is installed under `/opt/sglang`, isolated from the
  vLLM environment. Catalog schema v4 introduced model kind, capabilities, and
  image defaults; the CLI, UI, and HF `text-to-image` search path can create
  image installs.
- Apple Silicon image profiles use a separately locked MFLUX 0.18.0 worker on
  loopback `:17324`. Mnemosyne owns its process group and terminates it on
  swaps, unload, timeout, and cancellation to release Metal memory.
- The native Model Library mirrors the pinned MFLUX text-to-image catalog and
  supplies per-model generation defaults for FLUX.1, FLUX.2 Klein, Qwen Image,
  Krea 2 Turbo, FIBO, Z-Image, ERNIE Image, and Ideogram 4. Krea 2 Raw remains
  visible but non-installable because the pinned upstream loader supports only
  the Turbo weight layout.
- The native app bundle contains separate `framework-mnemosyne-base` and
  `framework-mnemosyne-image` export layers plus separate service/worker
  source trees. No MLX runtime is added to Docker.
- The native Runtime Updates page detects llama.cpp, oMLX, MFLUX, and DS4
  versions. For oMLX it selects the official DMG matching the host macOS
  version, detects the app, CLI shim, conventional Homebrew paths, or running
  server, and delegates updates to oMLX. A missing oMLX runtime can instead be
  installed through an approval-gated, argument-bounded stable Homebrew
  action. llama.cpp comes from its official
  GitHub arm64 artifact, MFLUX installs from its official PyPI project, and DS4
  builds from an exact official GitHub commit. Managed updates stage
  independently, activate through the global-empty maintenance barrier, and
  retain the previous version for rollback without a repository-owned feed.
- The native coordinator now reads oMLX's authoritative concurrent-request
  limit from its admin settings, applies only an optional global ceiling, and
  keeps a bounded metadata-only performance window with cold-start, admission,
  first-byte, total-latency, and streamed token-rate aggregates. Fresh configs
  retain one verified warm model and new GGUF profiles use bounded interactive
  context defaults.
- oMLX runtime ownership is classified as official app, stable Homebrew,
  Homebrew HEAD, or other external service. Stable Homebrew updates use an
  explicit globally drained fixed-command workflow; HEAD builds migrate rather
  than rebuild. Vendor cache metrics and a confirmed, drain-safe official SSD
  cache reset are available without exposing paths or credentials. A
  content-redacted benchmark supports direct compatible-endpoint comparisons.
- Unified Inference is now the native token sidecar: central reporting defaults
  on for every language engine. Existing machines can migrate the previous
  sidecar's canonical `node.id` and ledger DSN from its LaunchAgent; Settings
  displays only the effective identity. LM Studio is not in the reporting or
  request path.
- Automated request, routing, catalog, runtime, macOS adapter, worker,
  CUDA-web-UI, and packaging checks pass. The native Swift production build
  passes; its Swift Testing target still requires full Xcode. A bundled-runtime
  Krea 2 Turbo generation passed on
  an M4 Max; real Qwen/Krea CUDA generation remains a workstation smoke gate.

**Phase 11**

- Native configurations contain no LM Studio engine and enable a manager-owned
  llama.cpp adapter on loopback `:17325`. Schema-version-1 LM Studio profiles
  are retained only as inert migration metadata until Finder adoption. The
  adapter uses the hardened
  PID/process-group/start-identity/argv ownership proof and preserves the
  global lease-based residency invariant. Survivor records now retain storage
  root, scope ID, and volume UUID so restart validation can reconstruct the
  protected target.
- Runtime Updates discovers the official `ggml-org/llama.cpp` macOS arm64
  release, requires the expected asset URL/name plus the GitHub-published size
  and SHA-256, safely extracts it, validates the executable and required flags,
  and activates or rolls back only behind the all-engines-empty barrier.
- Finder-driven local discovery recognizes complete GGUF shard sets and MLX
  folders, excludes projector files as primaries, revalidates opaque candidate
  IDs and external-volume identity, and migrates matching aliases/configuration
  atomically without loading or copying weights.
- The read-only local-source route discovers LM Studio's configured download
  folder plus its documented default without enabling or contacting LM Studio,
  statting a potentially offline model volume, or changing residency. The
  Models GUI exposes those paths as Finder-confirmed suggestions. A selected
  symlink/nested path remains exact through scan, bookmark activation, import,
  and persisted storage while its resolved mount and volume UUID remain
  separate.
- Finder-created ordinary bookmarks use Apple's implicit single-transfer
  extension and are consumed while their grant is live, then
  converted into receiver-owned durable bookmarks after exact-path
  validation; YAML retains only their SHA-256 `scope_id`. Configuration saves
  preflight every referenced grant, startup prunes unreferenced private
  bookmarks, and scoped helpers/managed children reactivate a grant before
  `exec`. Protected filesystem/model-header work executes in killable process
  groups off the asyncio event loop behind bounded deadlines; timeout and
  cancellation both terminate the group. The bundle does not currently carry
  App Sandbox bookmark entitlements.
- Configuration snapshots include an optimistic revision. Settings saves,
  completed downloads, and imports share one mutation lock, preventing a stale
  window from overwriting a concurrently added profile. After a LaunchAgent
  restart, the menu app now keeps the restart warning visible until the
  control service reports the exact saved revision as applied. Service updates
  await asynchronous `SMAppService` unregister completion before re-registering
  and retain retry intent across failure or approval-required states.
- The ordinary Models page creates profiles only through the engine-aware
  library or Finder discovery. Engine/source/storage/served-name/projector
  facts are read-only, and API routing is selected through typed,
  engine-constrained Generation, Embeddings, Rerank, or Image roles rather
  than raw paths and arbitrary endpoint checkboxes.
- Hugging Face discovery now uses one cross-engine catalog with explicit
  engine-support badges while retaining exact per-engine install validation.
  Model cards strip Hub YAML front matter and render safe Markdown blocks in a
  scrollable detail pane rather than compressing or truncating the install UI.
  Discovery also shows bounded model-card prose and architecture,
  context length, parameter count, and license when Hub/config/GGUF metadata
  provides them. GGUF search requires an exact quant/shard selection and
  automatically selects the highest-fidelity same-directory vision projector,
  with manual selection and text-only opt-out. Detected context length and the
  selected projector persist into the llama.cpp profile. Downloads persist the
  resolved revision and exact file list before profile creation. oMLX
  downloads are scanned before registration and receive only metadata-derived
  generation, embeddings, or rerank capabilities. Completed weights enter a
  durable registration state, so a failed/interrupted profile write can be
  retried without downloading the model again. The native GUI renders
  transferred/total bytes, percentage, progress, and smoothed transfer speed.
  Hiding completed history preserves internal managed-download provenance.
  Managed cleanup requires the exact installation UUID plus immutable
  exclusive-ownership and current storage-binding proof, revalidates the exact
  regular-file manifest, and moves the whole destination to Trash. A
  llama.cpp or oMLX import can be cleaned up only after a fresh bounded scan
  uniquely rediscovers its payload under the registered storage, at which
  point the exact imported paths move to the macOS Trash. Root, escape,
  symlink, ambiguous, and shared targets remain refused.
- Runtime version probes, installs, and activation helpers all use bounded
  process-group cleanup; timeout or cancellation cannot leave an updater child
  running.
- Migration consumes the inert legacy record and clears LM Studio-specific wire
  names when an alias becomes a native profile. oMLX directory rescans
  authoritatively unload any
  pinned models preloaded by the official reload API before the maintenance
  barrier reopens, and a maintenance drain timeout enters an explicit
  recoverable degraded state rather than silently wedging admission.
- Missing legacy reporting identity and DSN values are atomically copied into
  Unified Inference's private `.env`, so retiring the previous token-sidecar
  LaunchAgent does not break future reporting starts.
- The native service suite passes (`278 passed, 2 skipped` plus both skipped
  real-bookmark tests with host access), all 60 Swift tests, all 23
  image-worker tests, and all 31 packaging tests pass, and the Swift
  production build completes.
  A direct official-runtime LFM2.5 GGUF inference and an external oMLX LFM2 1B
  inference both produced backend token usage on Theseus. An earlier Developer
  ID-signed Swift package and direct `SMAppService` helper ran successfully
  from `/Applications`; the current 0.9 candidate still requires its own signed
  and notarized acceptance. A direct registered-service restart also returned both
  HTTP planes with empty residency and a ready `theseus` reporting sink.
  Finder-confirmed GUI import, durable oMLX login startup, login-cycle behavior,
  DS4 model loading, real protected-folder
  helper/restart/child-`exec` validation are the remaining acceptance steps.

**Phase 0**

- Baseline test harness, smoke checklist, example config/env files, and pinned
  Docker vLLM dependency.

**Phase 1**

- `config.py` loads YAML config and `/config/.env`, validates storage/model
  references, and probes GPU indices when available.
- `catalog.py` manages SQLite state at `/state/mnemosyne.db`, syncs config
  aliases, preserves `ui_install` rows, and reconciles cache status.
- `profiles.py` resolves config/catalog aliases into `ResolvedProfile`.
- Manager endpoints now expose reload, configured profiles, storage locations,
  and catalog rows.

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
- Idle eviction retains buffered request-count updates. Token events now use
  an immediate idempotent SQLite transaction so local analytics and the
  optional delivery outbox become durable together before response completion.
- `/manager/status` is additive: legacy keys remain, with alias, GPU plan,
  quantization, idle countdown, in-flight count, and swap target added.
- `vllm-ctl status` prints the new fields when present.

**Phase 3**

- Split inference (`:8000`) and admin (`:8001`) FastAPI apps; admin is a
  superset that includes `/v1/*` for back-compat plus `/manager/*` and
  `/docs`/`/openapi.json`/`/redoc`.
- HTTP Basic auth on admin (`admin:$ADMIN_PASSWORD`); fail-safe bind to
  `127.0.0.1` inside the container when `ADMIN_PASSWORD` is unset.
- Optional bearer auth on inference (`INFERENCE_API_KEY`); admin Basic and
  inference Bearer headers are stripped before proxying to the inner vLLM.
- Inner vLLM moved from loopback `:8001` → `:8002` so the admin app and inner
  server don't collide in the container's network namespace; startup checks
  reject overrides that re-collide.
- Single SIGTERM handler at the asyncio gather level shuts down both
  uvicorn instances atomically.

**Phase 4**

- `POST /manager/install` accepts a fully-typed install request (alias,
  HF model, revision, quantization, GPU plan, `max_model_len`, storage,
  `extra_args`) and persists `models` + `downloads` rows in one
  transaction, then spawns a killable subprocess download. It now returns
  `202 Accepted` for queued async work.
- New `download_worker.py` (subprocess) wraps `huggingface_hub.snapshot_download`,
  emits line-delimited JSON progress on stdout, exits 130 on SIGTERM, and
  links its lifetime to the manager via `prctl(PR_SET_PDEATHSIG)` on Linux.
- New `downloader.py` (manager-side) owns the live subprocess registry,
  parses worker stdout in a daemon thread, and writes catalog state through
  `mark_*` methods. HF token is threaded explicitly into a per-subprocess
  env dict — `os.environ` is never mutated.
- Catalog gains a `threading.RLock`, `revision TEXT NOT NULL DEFAULT 'main'`
  + `resolved_sha TEXT` columns (with additive ALTERs for legacy DBs), and
  9 transition methods (`start_install_tx`, `mark_downloading`,
  `mark_progress`, `mark_complete`, `mark_error`, `mark_cancelled`,
  `mark_orphan_interrupted`, `mark_partial`, `delete_install_row`) plus
  `find_active_for` (revision-agnostic), `find_active_by_hf_id`,
  `find_repo_siblings`, `lookup_by_hf_id`, and `recover_orphan_downloads`.
- Reconcile resolves snapshots per-revision via `<repo>/refs/<revision>` (or
  the direct `snapshots/<sha>/` path for 40-hex commit SHAs), refuses
  `..`/absolute paths, and skips `status='error'` rows so a hard-failed
  install isn't silently promoted by a half-finished snapshot.
- Resident vLLM is pinned to the exact downloaded snapshot: `mark_complete`
  records `resolved_sha`, and `resolve_profile` prefers it over the symbolic
  revision when emitting `--revision`. Every invalidation path clears stale
  SHA pins, including start/retry, error, cancel, orphan recovery, partial
  transitions, config-sync cache invalidation, and reconcile downgrades.
- Restart recovery: lifespan calls `reap_orphans_on_startup` **before**
  `apply_config` so reconcile may promote any whose snapshot landed cleanly
  before the crash; lifespan teardown SIGTERMs all in-flight installs.
- Cache delete has two flavors with sibling-aware cleanup:
  `DELETE /manager/install/{alias}/cache` (wipe disk + mark every sibling
  `partial`) and `DELETE /manager/install/{alias}` (wipe + remove this row);
  `DELETE /manager/cache/{model_id:path}` is the legacy by-HF-id form.
  All paths are gated on residency + active-download checks; wipes refuse
  paths outside `storage.locations[].path` and fail closed without mutating
  catalog rows when a wipe is refused or fails.
- `_resolve_request_model` gates `ui_install` rows on `status='installed'`
  so queued/partial/error installs return a 409 instead of falling through
  to raw-HF passthrough or launching vLLM with incomplete weights.
- Public `/manager/install` rejects reserved synthetic cache aliases
  (`__cache__:` / `__cache__/`). Only the legacy `/manager/download` shim may
  create synthetic cache rows internally.
- Worker-emitted error messages are preserved in the catalog instead of
  being collapsed to a generic subprocess exit code.
- Legacy `POST /manager/download` is now a catalog-backed shim that
  preserves the v0 body shape (including the default `ignore_patterns`
  list), creates a synthetic-alias `ui_install` row, and runs through the
  same subprocess pipeline. Status route resolves by exact synthetic alias
  so config/UI rows for the same HF id don't shadow it. Per-request
  `hf_token` is threaded into the worker env without polluting
  `os.environ`. The in-memory `_downloads` dict is retired.
- `vllm-ctl` adds `install`, `install-cancel`, `install-retry`,
  `install-status`, and `cache-delete` commands; `download` and
  `download-status` continue to work through the catalog-backed shim.

**Phase 5**

- New `hf_search.py` wraps `HfApi.list_models` with a `filter="transformers"`
  + `pipeline_tag` pre-filter, fetches each candidate's `config.json`,
  and decides vLLM compatibility against the loaded architecture set.
  Per-row failures stay row-level (gated/missing/error reasons surface in
  `compat_reason`), endpoint-level auth/timeout failures map to 502/504.
- Architecture set sourced primarily by introspecting
  `vllm.model_executor.models.registry.ModelRegistry.get_supported_archs()`
  during `manager_lifespan` startup; falls back to the bundled
  `vllm_supported_architectures.json` snapshot when the import path moves
  on a vLLM bump, and to an empty set as a last resort (search still
  returns rows, all flagged `vllm registry unavailable`).
- `scripts/refresh_arch_list.py` regenerates the bundled snapshot from a
  live vLLM install (`docker exec vllm-manager python scripts/refresh_arch_list.py`);
  exits non-zero if vLLM cannot be imported or the registry API has shifted.
- New admin route `GET /manager/hf/search?q=...&limit=...&filter_compat=...&include_vision=...`
  returns the pinned envelope `{query, limit, include_vision,
  vllm_arch_source, vllm_arch_count, results}`. Each result row carries
  `model_id, architectures, is_compatible, compat_reason, size_estimate_gb,
  downloads, likes, last_modified, tags, pipeline_tag`. `include_vision`
  defaults `false` but exposes a flag the UI can flip on for
  vision-LLM searches (Qwen-VL, Llava, etc.).
- Bounded daemon search workers cap `huggingface_hub` thread pile-up without
  blocking process exit; outer `asyncio.wait_for(timeout=30)` raises 504 on
  the response side. Lifespan teardown cancels queued search jobs.
- Config lookups are cached by `(repo_id, sha_or_last_modified)` with a
  10-minute TTL for unversioned rows, and `hf_hub_download` is pinned to the
  Hub sha when available.
- The Dockerfile sets `HF_HUB_ETAG_TIMEOUT` and `HF_HUB_DOWNLOAD_TIMEOUT`
  defaults that cover the per-row `hf_hub_download` HTTP path; `model_info`
  gets an explicit 15s timeout.
- Size estimate reuses the proven siblings approach from
  `download_worker._safetensor_total`; failures yield
  `size_estimate_gb: null` without flipping `is_compatible`.
- `/manager/status` gains `vllm_arch_count` and `vllm_arch_source` so
  operators can see when the bundled fallback is active.

**Phase 6**

- New React/Vite/TypeScript/Tailwind admin UI under `ui/`, with TanStack
  Query data hooks, react-router routes, lucide icon buttons, and dense
  operational views for Dashboard, Catalog, HuggingFace Search, and Downloads.
- Dockerfile now builds the UI in a Node 22 stage and copies `ui/dist` into
  `/app/static` in the CUDA runtime image. `.dockerignore` keeps local
  `node_modules`, build output, caches, and git metadata out of the build
  context.
- `vllm_manager.py` registers a dedicated admin-only `ui_router` before app
  construction. `GET /` redirects to `/ui/`; `/ui` and `/ui/` serve
  `index.html`; `/ui/{full_path:path}` serves contained assets or falls back
  to the SPA for internal routes. Static root resolution happens per request
  from `MNEMOSYNE_UI_DIR` or `/app/static`, and traversal attempts return 404.
- New read-only `GET /manager/gpu` endpoint parses `nvidia-smi` output into
  `{available, gpus}` for live dashboard telemetry. Missing/failing
  `nvidia-smi` returns `available:false` instead of erroring, so macOS and
  no-GPU dev hosts remain usable.
- Dashboard shows live GPU metrics when available, GPU plan/utilization cap
  from `/manager/status`, and the resident alias's persisted catalog request
  count without presenting it as a recent traffic metric.
- Catalog UI derives cache-only rows from reserved alias prefixes
  (`__cache__:` / `__cache__/`) and mirrors backend action rules: cache-only
  rows hide Load and offer Create alias; config rows delete cache by HF ID;
  non-cache UI installs use alias-scoped cache deletion; removable UI/synthetic
  rows use `DELETE /manager/install/{alias}`.
- Search keeps incompatible HF rows visible with `compat_reason`, disables
  Install for them, and carries `size_estimate_gb` into `POST /manager/install`
  when present.
- Downloads view polls `/manager/downloads` plus selected
  `/manager/install/{alias}` detail and exposes cancel/retry/clear actions.

**Phase 7**

- Added top-level [README](../README.md) covering quickstart, config reload,
  multi-drive storage, gated HF tokens, partial-download recovery,
  architecture-list refresh, LAN exposure, common CLI operations, and
  troubleshooting.
- `vllm-ctl` help/env handling now reflects the two-plane world:
  `VLLM_ADMIN_URL` defaults to `:8001`, legacy `VLLM_MANAGER_URL` is an admin
  fallback, and `VLLM_INFERENCE_URL` steers `/v1/*` chat requests.
- `vllm-ctl` is executable in git (`100755`) so README quickstart commands and
  PATH/symlink usage work without `bash vllm-ctl`.
- README raw admin `curl` examples load `ADMIN_PASSWORD` from
  `~/vllm-manager/.env`, preserving `.env` as the container secret source while
  making copy-paste API examples authenticate correctly.
- README terminology now matches the shipped UI navigation (`Search`, not
  `Discover`).

**Phase 8**

- New `logsetup.py` installs a JSON formatter on the root logger; controlled
  by `MNEMOSYNE_LOG_FORMAT={json|text}` (default JSON). One-line JSON objects
  per record carry `ts`/`level`/`logger`/`msg`, fold `extra=` fields, and
  render `exc_info` tracebacks. No call sites changed.
- vLLM startup-failure error text now reports the inner subprocess exit code
  and points operators at container logs for vLLM stderr.
- Download worker tags HF errors as `auth` / `not_found`; the manager rewrites
  the catalog message to `"set HUGGING_FACE_HUB_TOKEN in /config/.env and
  restart"` (preserving the raw cause) for gated/private repos.
- `catalog.open_catalog` now runs `PRAGMA quick_check` + a passive WAL
  checkpoint at open time. Corrupt DBs are quarantined to `*.corrupt-<ts>`
  and a fresh DB is opened at the original path; startup `apply_config` +
  reconcile then repopulate config rows and recover storage state.
- New tests: multimodal proxy passthrough, JSON log formatter shape + text
  fallback, SQLite corruption quarantine.
- Docs: README "Known v1 limitations" section and smoke checks Section 9
  (vision-model multimodal smoke).
- Workstation acceptance pass remains pending because it requires a CUDA host.

**Phase 9**

- Second supervised inference backend: `llama-server` is now baked into
  the Docker image (CUDA build from a pinned llama.cpp tag) and runs as
  the resident subprocess for GGUF-only repos. The same `vllm_process`
  global, `_swap_lock`, `ensure_loaded`, eviction loop, and `_proxy` are
  shared with vLLM — only one model is resident at a time, so both
  backends bind `127.0.0.1:8002` sequentially.
- New pure module `repo_probe.py` (stdlib only) holds the GGUF grouping
  rules and the `probe_repo_format` decision: `has_transformer_weights`
  wins → vLLM (mixed-format included); GGUF-only → llama.cpp; neither →
  rejected at install time. Imported by the catalog, `hf_search`, manager,
  and the standalone `download_worker` (the latter only takes the pure
  shard expander to keep its cold-start surface unchanged).
- `runtime.py` gains pure `build_llama_argv` / `build_llama_env` mirroring
  the vLLM builders. `vllm_manager._start_engine(profile)` dispatches to
  `_start_vllm` or `_start_llama_cpp` based on `profile.backend`.
- `ResolvedProfile` now decouples `served_model_name` (forwarded as the
  upstream `"model"` field) from `engine_model_path` (the engine's
  `--model` / `-m` argument). For llama.cpp rows the served name is the
  user-facing alias and the engine is launched with `--alias <alias>`,
  so the GGUF path stays inside the engine and the proxy keeps a stable
  short name on the wire. A `model` back-compat property preserves
  existing callers.
- `config.ModelProfile`, `catalog.CatalogRow`, and the install request all
  carry `backend` + `gguf_filename`. Catalog migration is additive
  (`ALTER TABLE … ADD COLUMN`). Reconcile reads both columns and uses a new
  backend-aware `_has_expected_weights` so a llama.cpp row only goes
  `installed` when *its* specific GGUF (and all canonical
  `*-NNNNN-of-NNNNN.gguf` shards) are present — even when other quants in
  the same shared snapshot exist.
- New admin route `GET /manager/hf/files?model_id=…&revision=…` returns
  `{has_gguf, has_transformer_weights, recommended_backend,
  gguf_candidates: [{label, primary_filename, all_filenames, shard_count,
  size_bytes}, …]}` for the install-form dropdown. `hf_search.py` now
  consolidates `model_info(files_metadata=True)` into a single cached
  fetch shared by the size estimate, the GGUF probe, and the new files
  endpoint, so search-row enrichment doesn't pay extra round-trips.
- `_decide_compat` short-circuits to `is_compatible=true` when the repo
  has GGUF siblings (covers GGUF-only and mixed-format repos with
  unsupported architectures), so `filter_compat=true` no longer hides
  installable llama.cpp models. Search rows now carry `has_gguf` and
  `recommended_backend`.
- Install endpoint validates backend + gguf_filename consistency:
  llama.cpp without `gguf_filename` → 400; vLLM with `gguf_filename` →
  400; explicit backend on a no-weight repo → 400 (`"no supported weight
  files"`). When neither is supplied the install defaults to vLLM and
  skips the Hub probe (preserves legacy clients and offline tests).
  Retry trusts the row and skips the probe.
- `download_worker.py` accepts `gguf_primary_filename` and switches to a
  selected-only download: shards are expanded from the canonical filename
  pattern, passed to `snapshot_download` as `allow_patterns`, and
  `total_bytes` is summed across only the chosen shard set so progress
  bars and free-space estimates stay honest on multi-quant repos.
- `/manager/status` surfaces `backend`, `gguf_filename`, and `engine_pid`
  alongside the existing keys; `vllm_pid` stays as a deprecated alias for
  one release. `vllm-ctl status` prints the new fields. `vllm-ctl install`
  learns `--backend`, `--gguf-filename`, and a `--list-gguf` mode that
  prints the candidates a user can pick from before submitting.
- UI wiring: backend selector + required GGUF dropdown in `InstallForm`,
  per-row backend badge in `Catalog` and `Search`, backend + filename on
  the Dashboard's resident card. New `useHfFiles` query is cached.
- 17 new pure tests in `tests/test_repo_probe.py`. Existing suites
  extended with engine-dispatch tests, llama.cpp argv/env coverage,
  GGUF-vs-vLLM compat tests, install-validation rejections, and
  reconcile assertions for sharded / mixed-quant repos.
- Workstation smoke (build + install + load + swap a real GGUF repo) remains
  pending.

## Post-phase maintenance

- Added per-request token usage rows for streaming and non-streaming chat,
  completions, and embeddings. SQLite stores local analytics; an optional
  SQLite outbox drains idempotently to `public.token_usage` through
  `pg_writer.py` when the Postgres sidecar is enabled.
- Added local, OpenAI-compatible `GET /v1/models` synthesis from installed
  catalog rows across both backends, with a resident raw-model fallback.
- Added automatic `--enforce-eager` defaults for known slow graph-capture
  SSM/hybrid vLLM families by inspecting cached `config.json` metadata.
- Updated the container pins to CUDA 13.0.2, PyTorch cu129, vLLM 0.22.1, and
  llama.cpp b9548; refreshed `vllm_supported_architectures.json`.

## Verification

Latest Mac-first host verification on 2026-08-31:

- Native service suite: `1131 passed, 2 skipped`, including all adapters, routes, JIT
  residency, full-stream leasing, usage accounting, pairing, inventory,
  selected-node installs, exact storage bindings, and inert lifecycle work.
- Dedicated inference/storage non-regression slice: `29 passed`, including all
  six current Mac engines and nested external/symlink lexical destinations
  with no default-location fallback, storage-rebind laundering, or stale retry
  transition.
- Nyx Fleet suite: `396 passed`; cross-component catalog, inventory,
  DesiredInstall, catalog-ceremony, and live-acceptance contracts: `127 passed`.
- Isolated MFLUX worker suite: `23 passed`; Swift app suite: `170 passed`;
  packaging logic: `73 passed`.
- The complete ad-hoc `Unified Inference 0.9.0 (77)` app was rebuilt from the
  final sources. The isolated packaged verifier and secret-redacted acceptance
  collector passed and it contains zero `.pyc` or `.pyo` files. The canonical
  arm64 pilot DMG passed disk-image and artifact acceptance with SHA-256
  `625129d7651edf9e0197d950137807ed1339b0a8bd56aef3008dbf23b0d6712c`.
  The mounted image also proves the preserve-data uninstall assistant, native
  service helper, and separate Hub helper/LaunchAgent are present and valid.
  The nested lifecycle-helper wrapper is present, but no provisioning profile,
  OS-backed proof authority, or effects runner is enabled. This is development
  evidence only: it has no Team Identifier, hardened runtime, or timestamp and
  does not satisfy Developer ID, notarization, LocalAuthentication, update, or
  representative-hardware gates. The bounded Mac pilot scope is documented in
  [macOS pilot](../macos/PILOT.md).
- Release version/lock checks passed for 35 exact service pins and 107 exact
  image-worker pins; `git diff --check` passed.
- A real existing-Mac upgrade on Theseus moved the ad-hoc build 61 app to
  Trash, installed build 66 at the canonical path, refreshed the exact
  Service Management registration to build 66 on a new PID, and retained the
  exact `.env` inode and SHA-256 plus the existing config, SQLite database,
  storage/model locations, and usage history. The first attempt exposed an
  incompatible incomplete lifecycle-v2 journal; build 66 now reads that
  legacy recovery record without allowing the current planner to emit its old
  destructive retention contract. The accepted post-upgrade live report then
  cold-loaded oMLX, returned 110 tokens, unloaded, committed exactly one new
  local usage row under node `theseus`, and drained the Postgres outbox to
  zero. The recoverable uninstall assistant was also proved to refuse while
  the exact service remains registered; no uninstall or weight removal was
  performed on Theseus.
- The acceptance collector now has a separate pilot install/storage gate. It
  proves that the self-tested alias's newest install completed at the named
  configured storage, stayed beneath the exact lexical root without resolving
  symlinks, pinned a revision, recorded complete bytes and the registration
  transition, and rejects a rebound destination. It does not require the
  destructive download-lifecycle/deletion exercise.
- Strict pilot evidence also requires a genuinely cold coordinator admission:
  the self-test reports the actual bounded performance sample's `cold_start`
  classification, while the collector independently proves empty residency
  before the request and again after the requested unload.
- The earlier staged build-64 embedded Python/service runtime was also launched
  directly on Theseus with isolated temporary state, every engine disabled,
  and alternate loopback ports. Both HTTP planes returned version 0.9.0, the
  coordinator was initialized, idle, and accepting with zero leases/queue, and
  the temporary process and state were removed afterward. This proves packaged
  runtime bootability without modifying the installed build 61, but it is not
  a substitute for build-64 clean-install or real-model evidence.

The most recent CUDA-only host verification remains the 2026-07-23 baseline:
the manager suite had `393 passed` with one dependency deprecation warning, the
CUDA admin UI had `11 passed`, and its production Vite build completed. CUDA
workstation validation is deliberately outside the current Mac-first scope.

Workstation/GPU smoke validation is still outstanding:

- Rebuild/start the container.
- Install a small model end-to-end (`vllm-ctl install qwen-coder-1_5b ...`)
  and watch progress through `install-status`.
- Install to a non-default storage location and confirm the cache lands on
  the right drive.
- Cancel a long install mid-flight, then `install-retry` (default and
  `--force`) to confirm resumable / wipe semantics.
- Restart the container during a download and confirm `partial` →
  `install-retry` recovery.
- Cache-delete (`--alias` cache-only, `--alias --remove-row`, and legacy
  by-HF-id) with the existing safety gates.
- Plane-separation regression: `POST /manager/install` is 404 on `:8000`,
  reachable on `:8001` behind Basic auth.
- Confirm `HUGGING_FACE_HUB_TOKEN` from the legacy `/manager/download` body
  does not appear in `docker exec vllm-manager env`.
- Admin UI smoke: authenticated admin `/ui/` returns 200,
  unauthenticated admin `/ui/` returns 401, inference `/ui/` returns 404, and
  refreshing `/ui/catalog` serves the SPA.
- CUDA quickstart smoke: copy examples into a clean compose dir,
  set `ADMIN_PASSWORD`, build/start on the workstation, confirm `/health`,
  authenticated `/manager/status`, and authenticated `/ui/`.
- GGUF / llama.cpp smoke: rebuild the image and confirm
  `docker run --rm --entrypoint which <img> llama-server`. Search a known GGUF repo
  (e.g. `bartowski/Qwen2.5-7B-Instruct-GGUF`) and verify
  `recommended_backend == "llama.cpp"` and `has_gguf == true`. Install via
  the UI dropdown, confirm `/manager/status` shows `backend: llama.cpp`
  with the chosen `gguf_filename`, run a `/v1/chat/completions` call, and
  inspect logs for `Launching llama-server`. Repeat with a sharded quant
  (Q8_0 split into 3 files) to confirm only the shard set downloads and
  reconcile flips to `partial` when a shard is manually deleted. Swap from
  a vLLM model to a llama.cpp model and back to confirm clean teardown
  and no port collision on `127.0.0.1:8002`.

## Open follow-ups

- **External `docker-compose.yml`.** Lives outside the repo at
  `~/vllm-manager/`. Every `storage.locations[].path` from `config.yaml` must
  be bind-mounted there; `docker-compose.example.yml` documents the canonical
  layout.
- **vLLM pin staleness.** Refresh the pinned release deliberately after
  checking upstream release notes. After bumping vLLM, rerun
  `scripts/refresh_arch_list.py` to keep the bundled fallback aligned.
- **Free-space pre-check absent on manual installs.** `vllm-ctl install`
  warns when `--size-gb` is not supplied; the Phase 6 UI sets it from search
  results when available, so the warning should primarily appear on
  hand-crafted curl/CLI calls or search rows without a size estimate.
- **llama.cpp tag pin.** The Dockerfile pins `LLAMA_CPP_TAG=b9548`. Refresh
  deliberately after checking llama.cpp release notes (CLI flags can shift
  on minor bumps). The argv builders cover the documented stable flags;
  rare additions land via `extra_args` in the catalog row.
- **llama.cpp workstation smoke.** Run the GGUF / llama.cpp checks above on a
  CUDA host and record a real install + chat round-trip before release.

## Quick links

- [README](../README.md)
- [Contributor guide](../agents.md)
- [Workstation smoke checks](smoke_checks.md)
