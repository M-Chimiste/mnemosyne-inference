# Mac Pool Architecture

## Status and intent

This document defines the target architecture for the Mac-first Mnemosyne
pool. It is a design contract, not a statement that every component below is
already implemented. Existing native inference, storage, download, runtime,
and accounting behavior remains authoritative until a versioned replacement
has passed the acceptance gates in `mac_pool_acceptance.md`.

The product goal is an LM Studio-like installation and operating experience:
install one signed app, pair a Mac with Nyx, choose models and storage, and use
one OpenAI-compatible API. The implementation must retain Mnemosyne's stricter
residency, provenance, filesystem, credential, and accounting guarantees.

## Scope

The first production pool consists of Apple Silicon Macs. Nyx hosts the Hub
and may also host an independently isolated, limited worker when its platform
is supported by the current worker release. CUDA, vLLM, SGLang, and production
cross-machine model parallelism are deferred. Protocol fields may describe
future platforms, but a deferred platform must never become routable merely
because it can publish a snapshot.

The following existing native engines and inference routes are non-regression
surfaces even when the initial managed model recipes focus on llama.cpp, oMLX,
and DS4:

- Engines: llama.cpp, oMLX, DS4, and MFLUX. Legacy mlxcel and mistral.rs
  configuration is upgrade-readable but inert; its weights are retained.
- Routes: chat completions, completions, Responses, Messages, embeddings,
  rerank, image generations, and the local model catalog.
- Request behavior: opaque forwarding of fields outside the manager-owned
  model, image-normalization, authentication, and usage layers.

## Release-blocking compatibility floor

Mac Pool features are additive to the native Mac product. Pairing, a Hub
outage, local pause, an unavailable remote catalog, or a node being omitted
from a placement recommendation must not reduce the Mac's existing local
inference, model-library, runtime, download, storage, or accounting surface.
In particular:

- every retained-engine profile, alias, engine choice/alternative, capability,
  context/load/parallel setting, image default, JIT residency behavior, and
  token-accounting path remains intact unless the owner explicitly edits it;
  retired-engine profile metadata remains persisted but uncallable;
- a registered storage location continues to mean the exact lexical folder
  selected on that Mac, together with its expected volume identity and any
  receiver-owned scope; a nested folder is not replaced by its mount root and
  a symlink selection is not rewritten to its resolved target;
- the opaque storage IDs reported to Nyx are references to those existing
  node-local records, not permission for Nyx to nominate paths or create a
  global model directory;
- every local or Hub-requested install names a user-registered location. A
  missing, disconnected, wrong-volume, stale-scope, or unknown location fails
  closed instead of falling back to another disk or the default location;
- catalog reconciliation, pairing, participation, migration discovery, and
  inventory publication never copy, move, consolidate, or delete weights;
  model-file movement remains a separate, explicit, ownership-checked local
  operation; and
- managed, imported, external, shared, partial, and downloaded-unregistered
  provenance stays distinguishable so later migration, retention, and cleanup
  cannot silently broaden ownership.

These are release gates, not best-effort compatibility goals. The audited
automated baseline and the remaining real-host/lifecycle evidence are listed
in `mac_pool_acceptance.md`.

## Roles and authority

### Nyx Hub

Nyx owns:

- the single client-facing API and public model aliases;
- pairing approval, credential revocation, and Hub-side node enablement;
- each node's Hub-owned service class;
- logical model and deployment-recipe policy;
- desired installation jobs and placement recommendations;
- warm-first pool scheduling and bounded per-model queues;
- content-free route metadata and read-only usage aggregates.

Nyx never owns an inference engine, model path, bookmark, prompt, response, or
node-local engine process. It does not proxy model weight bytes. A node pulls
its own artifacts from an approved upstream and verifies them locally.

### Mac worker

Each Mac owns:

- its inference engines and the single-resident coordinator;
- exact local storage paths, volume identities, and protected-folder grants;
- actual model, runtime, install, verification, residency, and capacity state;
- local remote-install policy and consent;
- the authoritative local join/pause preference;
- final admission and full-stream model leases;
- local token-event persistence and durable delivery.

The node reports sanitized facts. Nyx may request a desired change, but only
the node can validate and apply it. Nyx cannot force a paused node to join,
nominate an arbitrary filesystem path, bypass a storage policy, or delete an
imported or externally owned artifact.

### Nyx limited worker

Hub and worker are separate roles even when colocated. A Nyx worker must have
independent process identity, credentials, ports, state, health, and resource
limits. It is assigned the Hub-owned `overflow` service class by default. Hub
health and queue handling must retain reserved CPU, memory, storage I/O, and
file descriptors; worker pressure must not make the API or dashboard fail.

## Device lifecycle

Pairing and participation are independent state machines.

### Pairing

Pairing is durable until an administrator or the paired Mac permanently
revokes it:

```text
unpaired -> pairing-pending -> paired -> revoked
                         \-> rejected/expired
```

A pairing code is short-lived, single-use, rate-limited, and created through
an authenticated Hub-admin action. Pairing establishes a stable device ID and
distinct least-privilege credentials for snapshot, dispatch, and management
traffic. Credentials are never reused as client, dashboard-admin, control, or
usage-ledger credentials. Revocation invalidates all node authority without
requiring the node to be reachable.

The Mac's permanent removal action is independent of participation. It writes
an exact durable request fence before contacting Nyx, denies Fleet admission
across restart, and replays only that request after an ambiguous outcome. A
committed revoke retains a non-secret tombstone and removes only the exact
pairing-owned credential generation. Re-enrollment requires a new invitation.
None of those transitions change or delete local models, exact configured
weight paths, inference profiles, local inference state, or token history.

The Hub persists enrollment and replay fences. The Mac persists its pairing
identity and credential references in private state appropriate to the signed
menu app and LaunchAgent architecture. Reinstall and migration must preserve
or deliberately rotate this identity; a reporting hostname is not a pairing
identity.

The complete versioned ceremony, exact credential roles, transport/locator
policy, crash recovery, static-enrollment coexistence, and deferred hardening
are defined in `fleet_pairing_protocol.md`. That target document does not make
an unimplemented pairing route current behavior.

### Participation

A paired node has a durable local preference:

```text
paused --join--> joining --> available
available --pause--> draining --> paused
```

Turning participation off:

1. closes node-local Fleet admission atomically;
2. advertises no routable capacity;
3. rejects stale new Hub reservations before model work with the existing
   authenticated `node_busy` proof;
4. lets admitted Fleet response bodies and streams retain their leases until
   completion or cancellation;
5. reaches `paused` when Fleet in-flight work is zero;
6. keeps pairing, local inference, model residency policy, downloads, and
   usage-outbox delivery intact.

Turning participation on does not re-pair. It revalidates the coordinator,
runtime, storage, and inventory facts before advertising availability.

Hub-side disablement and revocation are additional denials. Effective routing
eligibility is:

```text
paired
and not revoked
and hub_enabled
and locally_joined
and snapshot_fresh
and health_authoritative
and exact_deployment_match
and required_capability_present
and installation_verified_and_available
and runtime_compatible
and node_admission_open
```

No positive state on one side overrides a denial on the other.

## Scheduling and JIT residency

The existing node coordinator remains the only engine-residency authority.
The pool must preserve:

- JIT loading of installed cold models, never JIT downloading;
- exactly one globally verified resident target per node;
- warm-first routing;
- bounded FIFO pool and node queues;
- transition barriers that stop old-target admission and drain leases;
- a lease held through the complete response body or stream;
- engine-derived concurrency capped by local configuration;
- engine-local batching and parallel slots;
- bounded idle unloading when configured;
- cancellation-safe, exactly-once reservation release;
- no replay after ambiguous upstream work.

Service class is an outer scheduling policy, evaluated before warmth or
weight:

1. eligible `primary` nodes;
2. eligible `opportunistic` nodes under their local policy;
3. eligible `overflow` nodes only when the configured overflow condition is
   met.

Within a class, preserve warm-free, warm-queued, empty-cold-load, and
drain/switch tiers, then weighted least-outstanding selection. A warm overflow
node must not preempt an eligible primary solely because it is warm.

## Unified model catalog

The catalog separates five concepts that must not be collapsed into one row:

```text
logical model
  -> artifact variant
      -> deployment recipe
          -> per-node installation
              -> current residency
```

- A logical model is the user-facing identity and capabilities.
- An artifact variant is an immutable revision and exact file set, format,
  quantization, size, digest evidence, and upstream provenance.
- A deployment recipe binds an artifact to a compatible engine runtime, load
  policy, guaranteed context contract, capabilities, and compatibility tier.
- A per-node installation is actual state: managed/imported/external origin,
  opaque storage ID, progress, verification, availability, runtime match, and
  self-test evidence.
- Residency is transient JIT state and capacity.

Aliases and storage paths are not deployment identity. Two nodes are replicas
only when their authoritative immutable identity and capability set match.

### Current-source boundaries

The first inventory implementation must reconcile existing sources without
mistaking any one of them for the unified catalog:

- local `GET /v1/models` is the callable client view. It contains only
  currently resolved profiles, reports the selected candidate plus fallback
  and context policy, and may internally derive a canonical model ID from an
  absolute local path. It is not safe to forward or sufficient as inventory;
- the native install ledger is authoritative for managed job ID, storage key,
  exact destination, revision/file selection, progress, and registration
  transition. Its destination and free-form error remain local and are never
  copied into an inventory payload;
- configured profiles are authoritative for aliases, alternatives,
  capabilities, load/context policy, and imported or externally owned model
  references, including profiles retained while an engine is disabled;
- storage configuration is authoritative for the exact lexical selected path,
  expected volume UUID, and private scope reference. None of those three
  values leaves the Mac; and
- Fleet snapshot v1 is the sole existing live-routing snapshot. It carries
  path-neutral deployment identity, residency, admission, capacity, and usage
  delivery for resolved profiles, but intentionally omits disabled profiles,
  partial installs, storage, runtimes, and hardware.

Consequently, neither inventory nor pairing activation may forward the raw
local `/v1/models` body. Before the paired release, its Fleet-marked activation
probe must use a dedicated path-free response (or a dedicated no-load probe)
that proves only dispatch credential/marker scope and callable alias identity.
The Hub must not receive or persist path-derived `upstream_model` values.

Model-library search results are discovery candidates, not proof that weights
exist on a Mac. A candidate becomes a verified managed recipe only through the
signed compatibility catalog, and becomes an installation only after the Mac
reports local durable state.

### Signed compatibility catalog

Managed recommendations come from a versioned, signed catalog. The app ships
with a known-good baseline and may atomically activate a newer catalog only
after signature, schema, expiry, monotonic-version, and content validation.
Rollback retains the prior known-good catalog. Revocations and known-bad
runtime combinations fail closed. A catalog update never rewrites an existing
profile or changes a running deployment without an explicit operation.

Live discovery may show experimental candidates, but it cannot promote them
to a verified managed recipe or silently install a runtime.

## Node inventory

Actual inventory is reported by the Mac and remains visible while a node is
paused, offline, or a storage volume is disconnected. It is a separate
protocol family, not fields added to frozen Fleet snapshot v1.

### Transport and versioning

The initial protocol is `MacInventory` schema version 1, carried by an
outbound Mac-to-Hub sync over the pairing protocol's verified HTTPS origin:

```http
POST /fleet/management/v1/pairings/{pairing_id}/inventory-sync
Authorization: Bearer <management bearer>
Content-Type: application/json
```

The Mac control listener remains loopback-only and Nyx receives no control
password. Snapshot, dispatch, client, admin, and ledger credentials cannot use
this route. The path `v1` names the first inventory schema family; it does not
mean or extend `GET /fleet/v1/snapshot`. The two schemas, fixtures, validators,
sequences, freshness rules, and authority remain independent.

The bounded request contains one inventory observation and acknowledgements
for previously received desired jobs. The response acknowledges the exact
inventory instance/sequence and returns zero or more desired jobs. Both sides
negotiate supported inventory/job versions during pairing activation. Unknown
major versions fail closed while prior observations remain displayable as
stale. The wire document is at most 2 MiB, has at most 128 storage locations,
16 runtimes, 10,000 installations, and 256 job acknowledgements; arrays are
canonically sorted and all objects reject unknown fields.

### MacInventory v1 envelope

The normative top-level fields are:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1`. This is the inventory-family version. |
| `inventory_instance_id` | Random UUID created for each service process; not a hostname or hardware ID. |
| `inventory_sequence` | Strictly increasing unsigned integer within the instance. |
| `observed_at` | Node wall-clock timestamp for display only; Nyx receipt monotonic time controls freshness. |
| `pairing_id` | Stable opaque enrolled-device ID; must equal the management credential's enrollment. |
| `credential_generation` | Active pairing generation, used only as a replay/rotation fence. |
| `service` | Bounded version, Apple Silicon platform/architecture, supported inventory/job versions, and active signed-catalog version/digest. |
| `hardware` | Sanitized placement inputs and their evidence metadata. |
| `participation` | `joined`, `draining`, or `paused`, plus Hub-independent remote-install policy. |
| `storage_locations` | Path-free, node-authoritative storage observations. |
| `runtimes` | One bounded row per supported native engine/runtime. |
| `installations` | One row per durable node-local installation/configuration candidate, including non-callable rows. |
| `usage_delivery` | Bounded health counters/codes already safe for Fleet display. |
| `job_acknowledgements` | Idempotent state/version acknowledgements; never arbitrary errors or logs. |

The inventory never contains a hostname, serial number, MAC address, IP or
locator, model path, install destination, storage name/path, selected folder
spelling, mount path, volume UUID, scope ID, bookmark bytes, credential,
prompt, response, model-card text, arbitrary diagnostic, or upstream response
text. Repository-relative selected filenames may appear only inside an
authoritative artifact identity already permitted by the signed catalog or
Fleet deployment identity; otherwise only an opaque/digest identity is sent.

### Hardware and runtime observations

Hardware reports only fields needed for compatibility and placement:

- `soc_family`, `architecture`, performance/efficiency CPU core counts, GPU
  core count, installed unified-memory bytes, and a node-policy
  `allocatable_memory_bytes` after the owner's system/Hub reserve;
- macOS major/minor and a closed interpretation of fixed Apple capability
  facts required by managed recipes;
- current `power_source` (`ac`, `battery`, or `unknown`), low-power state, and
  a fixed thermal/pressure class rather than raw sensor data; and
- a `hardware_probe_version`, observation time, and evidence class.

The hardware object uses the following fixed fields; absent optional facts are
`null`, never guessed:

| Field | Type/meaning |
| --- | --- |
| `probe_version` | Bounded probe-contract version. Version 2 means one unambiguous built-in Apple GPU reported Metal support through the fixed `system_profiler` JSON contract. |
| `soc_family`, `architecture` | Sanitized Apple SoC family and `arm64`. |
| `performance_cores`, `efficiency_cores`, `gpu_cores` | Non-negative bounded integers or `null`. |
| `unified_memory_bytes` | Installed physical unified memory from the host probe. |
| `allocatable_memory_bytes` | Node-policy budget after fixed local/Hub reserve; never greater than installed memory. |
| `os_major`, `os_minor` | Compatibility-relevant macOS version only; no username/build/serial. |
| `power_source`, `low_power_mode`, `pressure_class` | Fixed enums used as soft evidence, not a hardware identity. |
| `observed_at`, `evidence_class` | Observation time and provenance label. |

Inventory v1 is not expanded with an arbitrary feature array. The native probe
runs the system profiler with a strict timeout and bounded accepted output,
accepts exactly one built-in Apple GPU row, and retains only its fixed Metal
support result, allowlisted `Apple M…` SoC family, and bounded numeric core
count. Malformed, oversized, timed-out, non-Apple, or ambiguous output produces
no Metal proof; an absent/malformed GPU core count remains `null` and hard-gates
any recipe with a GPU-core minimum.
The profiler payload, model/display names, and diagnostics are discarded.

Fleet recognizes only a closed vocabulary from those already-bound facts:
`unified-memory` requires an exact Apple-M-series `arm64` SoC plus non-zero
physical memory, while `metal` additionally requires native probe contract v2.
Runtime `apple-metal` and llama.cpp `flash-attention` require those hardware
facts plus an enabled, present runtime inside the signed recipe's exact release
tier/version-or-fingerprint bounds and the typed launch contract. Every unknown
feature string fails closed. This preserves the strict v1 schema instead of
creating an unauthenticated feature side channel.

Each recommendation input is labeled `measured`, `catalog_tested`,
`calculated`, or `conservative`; it must not be presented as measured when it
came from a catalog or estimator. Runtime rows carry `engine`, release tier,
enabled state, ownership (`managed`, `bundled`, or `external`), bounded version
and path-free runtime fingerprint, health, and one intrinsic catalog status:

```text
available | missing | disabled | known_bad | unsupported_os | unhealthy | unknown
```

Exact compatibility is a relation between this runtime evidence and one signed
recipe, so it is recorded on each installation/recommendation rather than
claiming the runtime works for every recipe. An external runtime being missing
or incompatible leaves its configured installations visible and non-callable;
it does not prevent inventory or the control plane from starting.

Each runtime row contains exactly `engine`, `release_tier`, `enabled`,
`ownership`, `version`, `runtime_fingerprint`, `health`, `catalog_status`,
`catalog_digest`, `observed_at`, and a nullable fixed `diagnostic_code`.
Versions and fingerprints are path-free; missing or externally opaque versions
remain `null` and conservative rather than synthesized.

Runtime `health: unknown` is conservative but does not by itself block an
otherwise compatible advisory model-download placement: the result is
`compatible_unverified`, and the selected Mac still revalidates runtime health
before approval or work. Explicit `degraded`/`unhealthy` health or intrinsic
`catalog_status: unhealthy` is a hard gate. A missing runtime is also a hard
gate in DesiredInstall v1 even when the signed recipe describes a future
managed/local-approval installation mode. The recommendation exposes the fixed
local-preparation reason, but the Hub cannot create or deliver a model-only job
until a later runtime-preparation protocol exists and a fresh inventory proves
the runtime present.

### Opaque storage identity

On first inventory migration, the Mac assigns each configured storage record a
random, non-reused `storage_location_id`; it is never a hash of the path,
volume UUID, scope ID, storage name, or hardware identifiers. Private state
maps that ID back to the existing exact lexical path, expected volume identity,
and scope. Inventory publishes only:

- `storage_location_id` and integer `binding_generation`;
- `kind` (`internal`, `external`, or `other`) and an optional explicitly
  shareable display label that is never initialized from a path/volume name;
- availability (`available`, `missing`, `wrong_volume`, `permission_required`,
  `read_only`, or `unhealthy`) and one fixed diagnostic code;
- total/free bytes, a conservative write-speed class, remote-install policy,
  and observation/evidence timestamps.

A storage row contains exactly `storage_location_id`, `binding_generation`,
`kind`, optional `share_label`, `availability`, `writable`, `total_bytes`,
`free_bytes`, `write_speed_class`, `remote_install_policy`, `observed_at`,
`evidence_class`, and nullable fixed `diagnostic_code`. Capacity arithmetic
must satisfy `0 <= free_bytes <= total_bytes` when both are known.

Any change to the exact path, expected volume UUID, or scope reference advances
`binding_generation`. Reselecting or repairing a grant is therefore visible as
a new binding even if `storage_location_id` remains associated with the same UI
slot. Deleting a location retires its ID permanently. Desired jobs bind both
ID and generation; a stale generation is rejected before directory creation or
download. This prevents an old Hub observation from redirecting weights after
a local storage edit. Renaming only the explicitly shareable label does not
change the binding.

The migration registry initially associates each current config storage key
with its random ID without rewriting that config's path. Later native-UI edits
address the ID, so a deliberate rebind can advance its generation. If an
out-of-band YAML edit makes that association ambiguous, reconciliation retires
the old binding and creates a new ID rather than guessing from a similar path,
name, or volume. The local config key remains local and may still select the
exact destination for existing profiles.

The Hub can recommend and display a storage location, but cannot resolve the
ID, choose an arbitrary path, or replace the selection with a default. Local
UI may show the real path because it is on the owning Mac; Hub UI must use only
the shareable label/kind and availability.

### Installation identity, source, and state

Every installation has a stable, node-scoped `installation_id`. Managed rows
reuse their durable installer identity; config/import/external rows receive a
random ID from a private inventory index. IDs are never derived from aliases or
paths, and ambiguous out-of-band profile replacement retires/recreates a row
rather than guessing ownership. An installation may carry
globally correlatable `logical_model_id`, `artifact_id`, `recipe_id`, and
Fleet `deployment_id` only when backed by authoritative catalog, revision,
file-set, digest, or local verification evidence. Alias equality alone never
correlates two Macs. An unverified local path receives only its node-scoped ID
and bounded alias until verification establishes a path-neutral identity.

Source and ownership are separate fields:

| `source_kind` | Meaning |
| --- | --- |
| `managed_download` | Created through the native durable installer from an approved immutable recipe/artifact. |
| `local_import` | Finder-selected weights adopted in place without copy or load. |
| `legacy_migration` | Retained/adopted from an earlier Mnemosyne or LM Studio configuration. |
| `external_reference` | Configuration points to weights/runtime state owned by another product or operator. |

| `ownership_class` | Deletion implication |
| --- | --- |
| `exclusive_managed` | May become eligible for a separately confirmed exact-ledger cleanup. |
| `user_owned` | Never permanently deleted as a managed install. |
| `external_owned` | Never deleted or relocated by Mnemosyne. |
| `shared` | Refuse automated deletion. |
| `unknown` | Refuse automated deletion until authoritative local proof changes the class. |

`managed_download` does not imply `exclusive_managed` without exact durable
destination/provenance proof. Inventory reports lifecycle, availability, and
residency independently so a disconnected registered model is not confused
with a failed download:

The native install ledger implements the first local-only part of this
boundary. It gives every existing and new installer row a companion provenance
record keyed only by the immutable installation ID. Migrated, hidden, partial,
downloaded, and installed history starts as `managed_download` / `unknown`;
alias, destination, status, and the legacy selected-file list never widen that
class. A separate exact-proof API accepts only canonical bounded file digests,
signed-catalog identities/digests, lexical storage binding and generation,
absent-and-created destination evidence, and an immutable creation transaction.
The native model-cleanup path consumes that decision only when its control
request names the exact canonical installation ID. It rechecks profile and
engine identity, the current opaque storage ID/generation and exact lexical
path/volume/scope, every primary/alternative/projector consumer, and the exact
regular-file manifest inside the empty-engine barrier. The bounded Trash
helper repeats the manifest check immediately before moving the whole exact
destination to macOS Trash; it never permanently deletes the directory.
Omitting the ID retains only the existing fresh-scan local-import path and is
refused when hidden-inclusive ledger history matches the profile, preventing a
managed-to-import authority downgrade. Successful moves preserve provenance
and enter the fixed `trashed` ledger state. Capturing exclusive proof during a
production install and a cross-resource crash-resume journal for the remaining
Trash/SQLite/config commit window are still required before this becomes
general uninstall authority.

```text
lifecycle: configured | queued | downloading | partial | verifying
           | downloaded_unregistered | registered | failed | cancelled
           | trashed
availability: available | storage_missing | wrong_volume
              | permission_required | corrupt | engine_disabled
              | runtime_incompatible | unknown
residency: cold | loading | warm | draining | unloading | unknown
```

Rows also contain bounded byte progress, artifact/recipe/catalog identifiers,
engine, model kind, exact advertised capabilities, guaranteed context,
verification state/time, runtime-compatibility result, storage ID/generation,
and fixed failure code. They never contain the install ledger's destination or
free-form error. Hidden install history still contributes provenance; hiding a
row in local UI does not erase inventory ownership evidence.

The fixed installation row is:

| Field | Contract |
| --- | --- |
| `installation_id` | Stable node-scoped opaque ID, never a path digest. |
| `aliases` | Canonically sorted bounded local aliases associated with this exact candidate. |
| `logical_model_id`, `artifact_id`, `recipe_id`, `deployment_id` | Nullable path-neutral global IDs, populated only at their declared confidence. |
| `identity_confidence` | `authoritative` or `unverified`; authoritative requires immutable provenance compatible with Fleet identity rules. |
| `engine`, `model_kind`, `capabilities`, `guaranteed_context_tokens` | Exact configured/callable contract; capabilities are sorted unique endpoint names. |
| `source_kind`, `ownership_class` | Independent enums defined above. |
| `lifecycle`, `availability`, `residency` | Independent state axes defined above. |
| `storage_location_id`, `storage_binding_generation` | Nullable together; required for registered managed/local storage. |
| `runtime_compatibility` | `compatible_verified`, `compatible_unverified`, `runtime_missing`, `engine_disabled`, `version_mismatch`, `known_bad`, `unsupported_os`, or `unhealthy`, evaluated against the exact recipe/catalog. |
| `verification` | Fixed state (`unverified`, `revision_verified`, `digest_verified`, or `self_tested`), evidence class, and nullable timestamp. |
| `bytes_downloaded`, `total_bytes` | Bounded progress; downloaded cannot exceed a known total. |
| `observed_at`, `diagnostic_code` | Row observation time and nullable fixed code only. |

### Freshness and authority

Nyx keys replay protection by pairing ID, credential generation, inventory
instance, and sequence, and timestamps receipt with its monotonic clock. A new
service instance starts a new sequence domain. Inventory freshness defaults to
60 seconds and is configurable only within a bounded 15-to-300-second range.
A late or duplicated observation cannot replace a newer one. Node wall time is
never liveness authority.

Fresh inventory is sufficient for recommendations and desired-job selection,
not inference routing. Stale inventory remains visible with its last receipt
time, becomes ineligible for new remote jobs/recommendations, and never regains
authority after Nyx restart until a fresh authenticated sync arrives. Fleet
snapshot v1 independently remains the only live routing/admission authority;
an inventory row cannot make a deployment routable.

## Placement recommendations

Placement first applies hard gates:

- platform and OS support;
- compatible managed runtime or an allowed runtime-install path;
- model weight, runtime, KV/context, concurrency, and host memory reserve;
- available registered storage and download headroom;
- required capabilities and context guarantee;
- node remote-install consent and allowlist;
- participation/service policy where relevant.

Eligible Macs are then ranked with a deterministic, versioned scorer using
memory headroom, compute evidence, storage headroom/speed class, compatible
warm runtime, existing artifact reuse, power policy, recent bounded benchmark
evidence, and service class. The UI shows the hard-gate result and a concise
explanation for every recommendation. It must distinguish measured,
catalog-tested, calculated, and conservative estimates.

A recommendation result is immutable and records `scorer_version`, signed
catalog version/digest, required capability/context/concurrency, and a basis
reference for every Mac: pairing ID, credential generation, inventory instance
and sequence, Hub receipt time, and relevant storage ID/generation. It expires
with its inventory basis.

`PlacementRecommendation` schema version 1 contains `recommendation_id`,
`scorer_version`, `created_at`, `expires_at`, catalog version/digest, requested
logical model/recipe/capability/context/concurrency, and canonically ordered
candidate rows. Each candidate contains only pairing ID and the
administrator-approved pairing display name (never a discovered hostname),
storage ID/generation, `eligible`, nullable order, fixed hard-gate codes,
estimated download/peak-memory/headroom bytes, evidence labels, and bounded
reason objects. The chosen Mac and storage are a separate explicit selection
that references this recommendation ID and exact candidate basis; viewing a
new recommendation cannot mutate an existing selection.

Hard-gate results use fixed reason codes such as `stale_inventory`,
`platform_unsupported`, `os_unsupported`, `recipe_known_bad`,
`runtime_unavailable`, `insufficient_memory_budget`, `storage_unavailable`,
`insufficient_storage`, `storage_binding_changed`, and
`remote_installs_local_only`. Install eligibility is independent of inference
participation: a paused Mac may continue an already approved download, while a
new remote job still requires an active pairing/management channel and its
local install policy. Routing eligibility continues to require participation.

For eligible choices, the versioned deterministic order considers:

1. an already verified exact artifact (no download);
2. an already compatible verified runtime;
3. calculated memory headroom after model weights, runtime overhead, declared
   context/KV/concurrency budget, and local reserve;
4. selected-storage free/headroom and conservative speed class;
5. catalog-tested or recent content-free local benchmark evidence;
6. AC/low-power policy and the Hub-owned service class where relevant.

Each candidate returns concise reason objects containing a fixed code, evidence
class, input observation time, and bounded human explanation. The UI shows
required/estimated bytes and headroom without claiming benchmark precision.
The scorer never uses hostnames, paths, volume identities, or arbitrary
diagnostics.

A recommendation is advisory. The user may select another eligible Mac and one
of that Mac's opaque registered storage locations. The UI shows why an
alternative is eligible/ineligible. A selection based on stale inventory is
not silently refreshed into a different Mac or disk: the user confirms the new
basis, and the Mac performs final validation regardless.

## Remote model installation

Current implementation includes both sides of the default-off path. Nyx owns
durable explicit-basis creation/read/list/cancel, outbound-sync delivery, and
acknowledgement ingestion. The selected Mac owns the private durable inbox,
local approval/refusal/cancel policy, revalidation, and execution through the
existing native installer. Nyx still has no filesystem, downloader,
registration, runtime-mutation, or inference authority.

Nyx returns an idempotent desired job only to the selected paired Mac's outbound
inventory sync. The initial `DesiredInstall` schema version 1 contains only:

- `job_id`, immutable `idempotency_key`, creation/expiry times, pairing ID, and
  credential generation;
- the exact recommendation basis (inventory instance/sequence), signed catalog
  version/digest, logical model ID, recipe ID, and immutable artifact ID;
- required engine/capability/context contract and optional bounded alias; and
- the selected `storage_location_id` plus exact `binding_generation`.

The wire object has fixed fields `schema_version`, `job_id`, `job_revision`,
`idempotency_key`, `desired_state` (`run` or `cancel`), `created_at`,
`expires_at`, bounded `valid_for_seconds`, `pairing_id`,
`credential_generation`, `recommendation_basis`, `catalog_version`,
`catalog_digest`, `logical_model_id`, `recipe_id`, `artifact_id`, `engine`,
`capabilities`, `guaranteed_context_tokens`, optional `alias`,
`storage_location_id`, and `storage_binding_generation`. The Mac uses monotonic
time from authenticated receipt plus `valid_for_seconds` for execution; Hub
`expires_at` is display/audit evidence and node clock skew cannot extend a job.
`cancel` stops only the matching durable job and never means delete weights.

It contains no repository URL supplied by the browser, arbitrary Hugging Face
ID, file/path override, destination, volume/scope value, engine arguments, or
cleanup request. The Mac resolves the signed recipe and storage binding from
its own current state, then reuses the existing native installer.

The Mac persists `(pairing_id, job_id, idempotency_key, canonical_payload_hash)`
before work. An exact replay returns the existing local install/job state; a
reused job/key with different fields fails with `idempotency_conflict`. Status
acknowledgements monotonically report:

```text
received -> awaiting_local_approval -> accepted -> downloading
         -> verifying -> downloaded_unregistered -> registered
         -> completed
         \-> refused | cancelled | failed
```

Each acknowledgement contains only `schema_version`, `job_id`,
`job_revision`, `installation_id` when assigned, state, bounded byte progress,
`updated_at`, and a nullable fixed result code. Nyx accepts only increasing job
revisions and keeps desired state separate from the last node observation.

`ask` jobs do no download before local approval. `local-only` jobs are refused.
Cancellation stops only the job/download and preserves verified partial or
downloaded weights according to the existing durable installer; it is never a
delete command. Completion means registered and self-tested as required, not
resident. The model stays cold until an ordinary request JIT-loads it.

Node policy is one of:

- `allow`: an approved Hub may start matching jobs;
- `ask`: queue the request until a local user approves it;
- `local-only`: reject remote installs.

The node pulls directly from the authoritative upstream, resumes safely,
persists progress, verifies the resolved revision and exact file set, records
content evidence when available, registers without loading, and self-tests
according to the recipe. Download completion and profile registration are
separate retryable states. A disconnected or wrong volume fails closed and
must not cause creation of a lookalike path on another disk.

Before creating any directory and again before registration, the Mac rechecks
pairing generation, job identity, catalog digest, recipe/artifact identity,
storage ID/generation, exact path binding, expected volume, scope activation,
free-space headroom, runtime compatibility, and local policy. Any mismatch is
a fixed refusal/retry state; it never switches to default/internal storage or
another Mac. Nyx never carries weight bytes. Only the selected Mac downloads
directly from the recipe's approved upstream.

An oMLX recipe additionally binds exact scheduler slots and its declared
memory-guard requirement. The Mac uses the authenticated official GET-only
global-settings response before download approval and registration, then
retains the canonical contract in the hidden-inclusive install journal. Every
local/JIT load, benchmark load, and fresh Fleet snapshot revalidates it. Drift
keeps the cold alias visible but makes it unverified, non-loadable, and
zero-capacity for Fleet; the installer never changes those service-global
settings. A post-start journal read fault preserves only a prior exact target
classification, so it cannot silently strip a signed guard or contaminate an
unrelated ordinary local profile.

Remote cleanup is a separate, explicitly authorized operation behind the
global empty-residency barrier. It may delete only exact ledger-owned managed
destinations. Imported, external, shared, ambiguous, root, escaping, and
symlinked targets retain the existing Trash/refusal rules.

## Token accounting

The serving node remains the sole writer of normal token events. For every
completed language request, it durably commits one content-free event before
claiming accounted success. The event binds:

- a stable event ID;
- the authenticated Fleet route ID when present;
- paired device ID;
- exact deployment ID and public model mapping version;
- endpoint, token totals, latency, status, and fixed runtime identity.

Local analytics and the delivery outbox commit atomically. Delivery is
at-least-once with central uniqueness on event ID, yielding exactly-once
ledger insertion. Ambiguous retries reuse the same event ID. A bounded policy
must not silently discard undelivered events; pressure becomes an explicit
health/admission condition. Pausing inference participation does not pause
outbox delivery. Nyx reads aggregates through a read-only role and never
writes synthetic normal-completion events.

Prompts, responses, raw usage envelopes, credentials, paths, and arbitrary
diagnostics never enter Fleet metadata or the central ledger.

Before Fleet work can acquire or JIT-load a model, the serving Mac atomically
reserves the route UUID and, when reporting is enabled, one durable outbox
slot under `BEGIN IMMEDIATE`. The reservation moves from `reserved` to
`started` immediately before request bytes may reach an engine, then is
consumed by the same transaction that writes analytics and the outbox. A
pre-work failure releases it; a post-dispatch failure retains a replay fence.
Concurrent or restarted copies therefore cannot both consume the last outbox
slot or execute the same Fleet route. Completed language routes use the
durable analytics/outbox row as their permanent duplicate fence. Completed
no-usage/image/error fences are content-free and bounded to the newest 10,000;
active reservations are never pruning candidates.

An accounted Fleet language response with a 2xx upstream status but no
recognized usage block is not exposed as a completed success. For a
non-streaming response, the Mac withholds the body and returns a fixed 502
`usage_missing` response. For live SSE, ordinary content events may already
have crossed the response boundary, so the Mac withholds the recognized
success terminal (`[DONE]`, `response.completed`, or `message_stop`) and aborts
the stream with the internal `usage_missing` failure; it cannot rewrite the
already-sent HTTP status. Both paths retain the route replay fence without
inventing zero token counts. Accounting pressure similarly advertises zero
capacity and rejects stale-snapshot work before model admission with the
proven `node_busy` response. Existing standalone missing-usage behavior
remains unchanged.

## Migration, rollback, and uninstall

Lifecycle operations share one private, versioned inventory and transaction
journal. Before cutover, migration captures:

- raw and semantic configuration;
- a consistent SQLite backup including WAL and outbox identity;
- private environment and credential references;
- runtime pointers and ownership;
- model provenance and partial jobs;
- exact storage paths, volume identities, scope references, and bookmark
  material in the private rollback snapshot;
- LaunchAgent/menu registration state and legacy-sidecar evidence;
- pairing identity and participation preference.

The production journal is idempotent and deliberately stages a complete,
signed recovery clone before it makes the installed application or its agent
unavailable:

```text
discovered -> helper-staged -> authorized -> preflighted -> drained
  -> snapshotted -> predecessor-stopped -> candidate-installed
  -> candidate-started -> validated -> committed
                                   \-> restored
```

The clone is transaction-scoped and contains the complete sealed app plus its
narrow lifecycle helper. The helper accepts no arbitrary path, PID, port,
LaunchAgent label, command, or argument vector. It communicates only through
an inherited socket pair, verifies the peer audit token/code requirement and
the clone's complete signature/build identity, and consumes a nonce-bound,
short-lived authorization challenge issued by the bundled service after the
visible app requests the authenticated loopback `/authorization/perform`
operation. The menu never creates the helper socketpair because the sealed
peer manifest authorizes only the bundled service Python. A separate
per-install OS-backed proof authority must bind successful local owner
authentication to the returned receipt; transport alone never authorizes a
transaction. The helper never installs a permanent privileged daemon or opens
a named socket/TCP listener. Resume, rollback, and manual recovery revalidate
the same immutable execution manifest rather than rediscovering targets.

Validation includes both HTTP planes, exact service identities, storage grant
reactivation, semantic feature equivalence, usage/outbox continuity, and an
appropriate inference self-test. Any pre-commit failure restores the prior
state and registration automatically. Existing profiles are preserved
verbatim and may receive catalog metadata overlays; they are never rebuilt
from recommendations.

Uninstall offers three explicit levels:

1. Remove the app and service registration; keep all Application Support data.
2. Also remove Mnemosyne state and managed runtimes; keep all model weights and
   produce a retention manifest.
3. Full removal of data proven exclusively Mnemosyne-managed; imported,
   external, shared, ambiguous, and unowned weights are retained or moved to
   Trash under existing safety rules.

Every material deletion is previewed from authoritative ownership evidence.
Pairing revocation is an explicit network action, not an accidental side
effect of deleting a local app.

Uninstall uses the recovery clone through the entire destructive tail so the
executor cannot remove itself before it has resolved the owner's retention
choices:

```text
prepared -> helper-staged -> authorized -> service-quiesced
  -> outbox-resolved -> hub-resolved -> weights-resolved
  -> runtimes-resolved -> agent-unregistered -> menu-login-unregistered
  -> state-resolved -> application-quarantined -> application-removed
  -> completed
```

The private execution manifest enumerates exact members and binds their
filesystem identity and signing/provenance evidence. State and runtime cleanup
never becomes recursive authority over a root. Exclusive model cleanup is
still limited to the exact locally proven files at the owner's original
lexical storage location and uses recoverable Trash semantics. The recovery
clone removes itself only after a terminal receipt is durable.

## Protocol evolution

Fleet snapshot v1 is frozen. Its producers, packaged schema, validators,
examples, and golden vectors remain byte-shape compatible. Mac Pool v2 is a
suite of separate versioned protocol families; the first inventory and desired
install schemas are each version 1 within that suite. Unsupported inventory/job
versions fail closed for recommendations and remote work without adding fields
to or deriving routing authority from snapshot v1. Any deployment-identity
change still updates every snapshot schema copy, producer, validator, packaged
resource, and golden vector together.

Snapshot, management, and dispatch credentials are distinct. The paired
dispatch credential is accepted only on a request carrying the Hub's canonical
route marker and cannot authenticate an ordinary local request. Existing
static enrollments retain an inference-key fallback during migration.
Redirects and ambient proxies remain disabled. Node URLs and credentials never
appear in dashboard payloads. Dynamic enrollment must preserve these existing
boundaries.

## Delivery sequence

1. Durable Mac-local participation and Fleet-only drain/gating.
2. Versioned pairing identity, revocation, and Hub-owned service class.
3. Separate `MacInventory` v1 hardware, storage, runtime, installation, and
   participation inventory while frozen Fleet snapshot v1 remains unchanged.
4. Signed compatibility catalog and explainable placement.
5. Idempotent selected-Mac installation through opaque storage IDs.
6. Route/deployment-correlated durable accounting.
7. Transactional migration/restore, guided cutover, and tiered uninstall.
8. Credentialed signed release and representative-hardware acceptance.

Each step must keep the existing regression surface green and must not claim a
later step merely because its schema or UI placeholder exists.
