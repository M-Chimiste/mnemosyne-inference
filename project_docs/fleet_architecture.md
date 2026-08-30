# Mnemosyne Fleet Gateway Architecture

Status: the static protocol-v1 gateway and services are implemented; target-
host rollout evidence is tracked in [fleet acceptance](fleet_acceptance.md).
The opt-in dynamic-pairing foundation is implemented only to the boundary
described below and is not yet a production signed-app workflow.

## Purpose

Mnemosyne Fleet provides one OpenAI-compatible endpoint across independently
managed macOS and CUDA inference workstations. The gateway and fleet dashboard
run on Nyx, which already hosts the central token-usage ledger and dashboard.

The fleet layer does not own inference engines. Each workstation's Mnemosyne
manager remains the authority for:

- engine process ownership and recovery;
- model discovery and exact load configuration;
- admission and bounded local queuing;
- model residency, draining, and switching;
- complete response-stream lifetime;
- local token analytics and durable Postgres delivery.

Nyx owns:

- enrollment of trusted nodes and their credentials;
- polling and expiry of live node snapshots;
- strict logical-model to deployment mappings;
- capacity-aware request scheduling and bounded fleet queues;
- the single public inference endpoint;
- realtime fleet status and historical usage presentation.

## Deployment and trust boundary

Nodes are explicitly enrolled. Automatic inventory discovery begins only after
an operator provides a stable node identity, a private inference URL, and a
node-specific bearer credential. The transport may be a trusted local network
or Tailscale; routing and identity semantics are independent of that choice.

Anonymous LAN discovery is not an authority. DNS, including Tailscale
MagicDNS, may resolve enrolled node addresses but does not establish node
identity or permission to receive inference traffic.

The existing node control planes remain private. Each node exposes a
read-only, bearer-authenticated fleet snapshot on its inference plane. Nyx
never requires access to model deletion, configuration, runtime update, or
other administrative endpoints to route inference.

Nyx uses separate credentials for:

- public client access to the fleet inference API;
- dashboard and Fleet admin API access;
- each enrolled node's read-only snapshot endpoint;
- each enrolled node's inference plane;
- each paired node's own pairing-management operations;
- read-only token-ledger queries;

Credentials are stored only in Nyx's private secret store or environment and
are never returned by fleet APIs, written to route history, or sent to the
browser.

Existing static nodes resolve snapshot and inference credentials from the
configured environment references. A paired Mac instead receives three
independent generated bearers: snapshot, Fleet-only dispatch, and management.
The dispatch bearer is accepted by the native service for normal inference
only alongside Nyx's canonical Fleet route marker and never replaces the
Mac's ordinary local inference credential. Dynamic locator and credential
values remain encrypted outside Fleet's route database; public/admin status
exposes only opaque identity, generation, lifecycle, and fixed failure fields.

### Dynamic pairing implementation boundary

Pairing is opt-in and disabled by default. When disabled, its API routes are
absent and static enrollment behaves exactly as before. When enabled, the Hub
implements strict version-1 invitation, claim, approval/rejection,
claim-bound provisioning, activation acknowledgement, enable/disable,
revocation, encrypted secret persistence, and restart reconciliation. Its
locator policy rejects unapproved schemes, ports, networks, ambiguous DNS, and
topology changes. Activation and paired dispatch use a transport pinned to the
approved numeric peer while retaining normal TLS hostname verification,
disabling redirects, and ignoring ambient proxies.

Activation performs only authenticated snapshot and reduced path-free model
probes and checks that neither probe changed residency. The production flow
keeps a newly activated pairing Hub-disabled until a separate administrator
enable publishes it to the scheduler. Paired registry records use opaque
`pairing_id` for enrollment/revocation ownership while preserving snapshot-v1
and token-ledger `reporting_node_id`; persisted snapshots never regain routing
authority after restart without a fresh poll. Static and paired records share
the scheduler but retain explicit source and credential ownership.

The Mac service has the matching durable local pairing journal, private
credential-file transaction, staged-versus-active authentication rules,
secret-free status, and Fleet-only path-free model probe. The Swift app renders
pairing status, prevents generic editing of pairing-owned credentials, exposes
the independent participation toggle, and drives invitation claim, approval
resume, credential delivery/staging, and activation with a memory-only secret.
It also exposes authenticated permanent self-revoke and exact-request retry
recovery independently from the reversible participation toggle. Signed-
artifact acceptance, routine rotation, lifecycle integration, and the full
crash matrix remain pending. Static adoption, remote notification,
representative signed multi-host acceptance, and the broader inventory/catalog
and migration workflows remain target behavior in
[the pairing protocol](fleet_pairing_protocol.md).

### Nyx-hosted limited compute

Nyx may also run a limited inference worker, but the worker is an ordinary
enrolled node rather than part of the Fleet gateway. It must have an
independent service identity, listener, state directory, model-storage roots,
runtime lifecycle, and snapshot/inference credentials. The gateway must not
launch, adopt, signal, or share mutable engine state with that worker.

The enrollment should set `service_class = "overflow"` so primary and
opportunistic nodes remain preferred even when the Nyx worker is already warm.
The class is Hub-owned scheduling policy and is deliberately absent from
deployment identity and the node snapshot protocol. Configuration alone does
not create OS process or resource isolation; those deployment boundaries are
an operator prerequisite for enrollment.

## Versioned node protocol

Every node implements:

```text
GET /fleet/v1/snapshot
Authorization: Bearer <node FLEET_API_KEY>
```

This credential is distinct from the credential Nyx uses to dispatch
inference. Static nodes retain the current configured inference bearer; a
paired native Mac uses its dedicated `FLEET_INFERENCE_API_KEY`, with the
ordinary `INFERENCE_API_KEY` remaining local-client policy. The response is one
self-consistent document with these top-level fields:

```json
{
  "schema_version": 1,
  "snapshot_sequence": 7,
  "observed_at": 0.0,
  "node": {
    "node_id": "metis",
    "instance_id": "process-or-boot-identity",
    "platform": "macos",
    "version": "0.9.0"
  },
  "health": {
    "state": "ready",
    "accepting": true,
    "authoritative": true,
    "diagnostic_code": null
  },
  "residency": {
    "alias": "qwen-coder",
    "deployment_id": "sha256:...",
    "engine": "llama.cpp",
    "epoch": 12,
    "transition_target": null
  },
  "admission": {
    "queue_depth": 0,
    "queue_limit": 128,
    "queued_by_deployment": {}
  },
  "capacity": {
    "derived_limit": 4,
    "configured_max_concurrency": 3,
    "effective_limit": 3,
    "active": 2,
    "queued": 0,
    "available": 1,
    "source": "llama.cpp-slots",
    "confidence": "authoritative",
    "saturation": 0.666667
  },
  "deployments": [],
  "usage_delivery": {
    "enabled": true,
    "writer_ready": true,
    "outbox_pending": 0,
    "last_flush_at": null,
    "last_error_code": null
  }
}
```

The normative contract is
[`fleet_protocol/v1/snapshot.schema.json`](../fleet_protocol/v1/snapshot.schema.json);
[`snapshot.example.json`](../fleet_protocol/v1/snapshot.example.json) is a
complete conforming response. All fields are secret-redacted. Diagnostics are
fixed bounded codes. Paths, bookmark bytes, credentials, environment values,
command lines, prompts, and outputs are excluded.

The Draft 2020-12 schema defines the bounded wire shape. Nyx additionally
rejects a structurally valid document unless its semantic relationships agree:
queue totals and per-deployment counts match, capacity arithmetic and
saturation agree, queue and transition references name advertised
deployments, residency matches one distinct warm deployment identity, and
health state is consistent with admission. Multiple local aliases may
legitimately reference that one warm identity.

Nyx requests identity encoding and streams the raw snapshot through a fixed
8 MiB cap before JSON or schema validation. An oversized `Content-Length` is
rejected without reading the body; chunked input is stopped once it crosses
the same cap. This transport bound is deliberately stricter than the
theoretical aggregate of every per-field schema maximum.

Nyx treats a snapshot as live only while:

- its schema version is supported;
- its `node.node_id` matches the enrolled identity;
- its HTTP request was authenticated with that node's credential;
- a strictly increasing sequence for that process instance was received
  within the configured heartbeat TTL;
- the node reports an authoritative state suitable for the requested action.

Nyx computes TTL from its own monotonic receipt time; it does not trust node
wall clocks. The per-process `instance_id` distinguishes a restarted manager
from a delayed snapshot produced by its predecessor. Once Nyx accepts a new
instance for an enrollment, the predecessor's instance ID is retired for the
gateway process lifetime and cannot replace the new record. Nyx retains at
most 1,024 retired instance IDs per enrollment and never evicts one to admit
another: once that churn budget is exhausted, new instance IDs fail closed
until the one-process gateway restarts and receives a fresh snapshot.

## Strict deployment identity

A public fleet model is not an alias union. It is a logical name mapped to one
or more strictly equivalent node deployments.

Each node computes a `deployment_id` as lowercase SHA-256 over canonical JSON.
Canonical JSON uses UTF-8, sorted object keys, no insignificant whitespace,
and the following identity fields:

```json
{
  "protocol": 1,
  "engine": "llama.cpp",
  "upstream_model": "canonical model or exact local artifact identity",
  "resolved_revision": "immutable revision when known",
  "artifact": {
    "format": "gguf",
    "selected_files": ["model.Q4_K_M.gguf"],
    "quantization": "Q4_K_M",
    "content_digest": null
  },
  "kind": "language",
  "capabilities": ["chat/completions", "completions", "responses"],
  "load_config_digest": "sha256:..."
}
```

Runtime state and placement do not affect identity. In particular, node ID,
public alias, storage path, current residency, concurrency limits, queue
depth, and telemetry are excluded.

Immutable hexadecimal revisions are normalized to lowercase before hashing.
For GGUF, `selected_files` contains the explicitly selected primary GGUF plus
an explicitly selected projector, if any, in sorted order. Automatically
required sibling shards are not repeated in identity; the immutable repository
revision and selected primary establish their closure. A profile is
authoritative only when its actual primary and projector paths exactly match
that managed-install record.

Portable deployment identity and a node's process-load identity have different
jobs. The former deliberately excludes aliases so two nodes may serve the same
artifact under different local names. The latter must still include every
field that changes a process launch. Native llama.cpp therefore distinguishes
the served wire name and its Generation, Embeddings, or Rerank process mode
when deciding whether a resident can be reused.

If an immutable artifact digest is unavailable, the deployment remains
discoverable but reports identity confidence honestly. Nyx does not
automatically group two deployments with non-authoritative identity.
Protocol version 1 implements no heterogeneous fallback group; operators must
publish unlike deployments as distinct public models.

Capabilities use slash-separated OpenAI-compatible route names:

- `chat/completions`
- `completions`
- `responses`
- `messages`
- `embeddings`
- `rerank`
- `images/generations`

Nodes advertise only capabilities actually supported by that profile and
engine. The portable default llama.cpp Generation contract on both CUDA and
macOS is exactly `chat/completions`, `completions`, and `responses`.
`messages` remains an explicit opt-in where supported. Embeddings and Rerank
are separate profiles because they change llama.cpp launch mode.

## Node concurrency contract

Concurrency is enforced at the node and coordinated by Nyx. A request
consumes one admission permit from acceptance until the complete response
body or stream closes.

Every node reports:

- `derived_limit`: capacity established by its engine adapter;
- `configured_max_concurrency`: optional operator ceiling;
- `effective_limit`: the lower of the derived limit and configured ceiling;
- `active`: accepted permits not yet released;
- `queued`: bounded local waiters;
- `available`: `max(0, effective_limit - active)` while admission is open,
  otherwise zero during drain, transition, degraded state, or another
  admission barrier;
- `source`: the derivation mechanism;
- `confidence`: `authoritative`, `configured`, `derived`, or `conservative`;
- `saturation`: active divided by effective limit.

`configured_max_concurrency` is a ceiling. It can reduce but never raise an
authoritative engine limit.

Initial derivation sources are:

- vLLM: a valid engine `--max-num-seqs`; otherwise one conservatively. The
  pinned release logs its theoretical KV-cache concurrency at startup, while
  that value is absent from the
  [vLLM 0.22.1 production metric set](https://docs.vllm.ai/en/v0.22.1/usage/metrics/);
- CUDA llama.cpp: live server slots, with valid `--parallel` and then one as
  conservative fallbacks;
- native llama.cpp: its typed parallel setting, otherwise one;
- oMLX and DS4: one conservatively until their adapters expose and prove a
  stronger admission contract;
- MFLUX and SGLang Diffusion: one unless the isolated worker exposes and
  proves safe parallel capacity.

Nodes have a configured bounded queue. When neither an immediate permit nor a
queue slot is available, admission fails before engine work with a stable
`node_busy` error, `Retry-After`, and manager-owned
`X-Mnemosyne-Error: node_busy` proof header. Managers strip that reserved
header from engine responses. A stale Nyx snapshot can therefore cause an
inexpensive rejection but cannot overcommit the engine.

## Residency and draining

Every node implements the same full-stream lease invariant:

1. A lease is tagged with the current resident epoch.
2. Same-target requests may run concurrently up to the effective limit.
3. Once a different target reaches the head of the FIFO queue, new requests
   for the old resident target do not bypass it.
4. A transition waits for every active lease in the old epoch to release.
5. The node unloads and verifies empty state before loading the new target.
6. The target is published only after readiness verification.
7. A late finalizer from an old epoch cannot decrement the new epoch.

Manual unload, reconciliation, runtime activation, file deletion, and service
shutdown are maintenance barriers. They stop admission, drain leases, and
fail closed if empty state cannot be proved.

CUDA must satisfy this contract before it participates in fleet routing.

## Fleet scheduling and queuing

Nyx resolves the request's public `model` to a strict deployment group,
filters candidates by endpoint capability and live authoritative state, then
applies the enrollment's Hub-owned service class before residency. Classes are
strictly ordered `primary`, `opportunistic`, then `overflow`, with omitted
configuration defaulting to `primary` for existing enrollments. A lower class
is considered only when no higher-class node currently yields an admissible
candidate. A cold/loadable primary therefore precedes a warm overflow node,
while a saturated primary with no bounded queue room does not block a lower
class.

Within the selected service class, Fleet uses the following ordered tiers:

1. requested deployment already resident with an available permit;
2. requested deployment resident with the shortest bounded queue;
3. empty node able to load the deployment;
4. node able to drain and safely switch to the deployment;
5. bounded Nyx per-model queue;
6. `429` with `Retry-After` when the fleet queue is full or its deadline
   expires.

Within a class and tier, Nyx uses weighted least-outstanding selection. A node's
effective limit is the default weight; operator weights may only reduce its
share until representative performance data justifies a higher derived
capacity.

Nyx tracks requests it has routed immediately, rather than waiting for the
next node poll, so concurrent scheduler decisions see reservations made by
the same gateway process. Pending reservations count unconditionally. After
non-busy upstream headers prove admission, a reservation is treated as
reflected only by a snapshot poll that started later. This closes the race
where a poll began after reservation but sampled the node before the request
arrived. Node admission remains authoritative.

Protocol version 1 runs one Nyx gateway process. Its reservation and FIFO
state is intentionally in memory; adding multiple workers requires moving
both into a shared transactional scheduler before it is safe.

Fleet-owned snapshot and inference HTTP clients have no separate
transport-wide active-connection ceiling. Scheduler reservations plus
node-advertised limits are the authoritative concurrency controls, so admitted
work is not hidden in an HTTP connection-pool queue. Each client retains only
20 idle keepalive connections.

Queues are FIFO per public model. Queue sizes and deadlines are bounded.
Cancellation removes a waiter without consuming later capacity. A pending
model switch prevents an unbounded stream of new requests to the old resident
model from starving the queued target.

## Data-plane behavior

The fleet gateway supports only explicitly implemented OpenAI-compatible
routes. It does not blindly forward arbitrary paths.

For a routed request Nyx:

- authenticates the client;
- requires a non-empty public model identifier;
- resolves a strict deployment group and endpoint capability;
- waits for bounded fleet admission;
- rewrites only `model` to the selected node-local alias;
- strips client authorization, cookies, forwarding headers, and reserved
  proof/route headers, then injects the selected node credential and Fleet
  route ID;
- preserves the semantics of all other supported JSON fields and forwards
  non-reserved request headers;
- streams bytes without buffering the complete response;
- holds its scheduler reservation until the complete body or stream closes.

Nyx never parses prompts or outputs for persistence. Route history is limited
to fixed metadata such as request ID, timestamps, public model, deployment ID,
node ID, endpoint, queue time, response time, status, and fixed failure code.
Ownership moves from the request handler to the response iterator only after
upstream headers have been accepted. Every earlier error and every later
disconnect runs cancellation-shielded cleanup so scheduler and node leases
cannot be stranded.

Automatic failover is allowed only for:

- a `429` carrying the manager-owned `X-Mnemosyne-Error: node_busy` header,
  which proves no work started (a body-only error is terminal);
- `ConnectError`, `ConnectTimeout`, or connection-pool timeout before headers,
  which prove no HTTP response was accepted.

Fleet closes a proven-busy response without reading its body. If all attempted
candidates return that proof, the client receives bounded `429
fleet_capacity_busy` plus `Retry-After`. Every unproven `429` is terminal and
streamed without whole-body buffering. After any other response headers or
body bytes arrive, Nyx never retries or moves a stream. Read/write/protocol
timeouts and other ambiguous failures are returned to the client rather than
risk duplicate inference.

Stateful follow-up APIs that omit a model require explicit response-to-node
affinity and are out of the initial route set.

`GET /v1/models` lists logical fleet models with at least one live eligible
deployment. Rich replica state remains on the authenticated fleet admin API.
That admin view includes a path-free summary of each enrolled node's last
authenticated deployment inventory. Stale inventory remains visible only for
diagnosis and is labeled offline; it never regains routing authority. Public
model promotion remains an explicit configuration action.

This snapshot-derived deployment view remains separate from the Mac management
inventory. Native Macs now produce strict path-free `MacInventory` v1
observations, synchronize them through the pairing-management credential, and
Nyx persists them in a secret-free database with replay, generation, instance,
restart, expiry, and revocation fences. The authenticated inventory endpoints
expose summaries or the exact path-free document; persisted observations never
become inference authority.

The optional signed compatibility-catalog service is also integrated as a
management-only side path. It loads a verified last-known-good catalog or the
built-in empty offline catalog from a dedicated mode-`0700` directory, accepts
updates only from one canonical HTTPS origin/path, and verifies them against
locally pinned Ed25519 public keys. Background and admin-triggered checks are
bounded, credential-free, redirect-free, and failure-isolated. Admin-only,
no-store status, paginated model metadata, and paginated recipe metadata are
available only when `[catalog].enabled = true`. Neither startup catalog health
nor an update result mutates public model mappings, node membership, scheduler
reservations, or `/v1/*` routing.

When dynamic pairing, the signed catalog, and the independent
`placement.remote_installs_enabled` switch are all explicitly enabled, Nyx
accepts a closed placement intent at
`POST /fleet/api/v1/placement/recommendations`. The caller can state only the
exact logical-model/recipe IDs and required capability, context, concurrency,
and allowed service classes. Nyx stamps the recommendation UUID and time,
resolves every recipe fact from the verified catalog, and scores every
inventory-backed Mac/storage binding. Results are short-lived, explainable,
path-free advice and contain no selected/chosen target. Runtime installation
policy is currently `not_allowed`.

Under that same hard-default-off switch, the Hub now owns a separate private,
bounded DesiredInstall v1 journal. `POST /fleet/api/v1/desired-installs`
accepts only the original closed user contract, a canonical idempotency key,
and one exact candidate `basis` copied from the advisory response. The caller
cannot supply job identity/revision/time, artifact or engine facts, hardware or
memory facts, runtime/install policy, a selected-path value, or any desired
destination. Nyx stamps the job UUID/time, re-resolves the active signed
catalog, recomputes all candidates, and creates revision 1 only when that exact
Mac×storage basis remains eligible; it never infers or substitutes a target.
Exact-ID read, bounded list, and revision-bumping cancellation are admin-only
and `no-store`.

The journal is not the routing database, pairing database, inventory database,
or catalog store. It records fixed user contract and signed identities,
pairing/credential generation, catalog version/digest, recipe/artifact,
inventory instance/sequence, opaque storage ID/generation, TTL, delivery, and
bounded acknowledgement state. Pending documents leave Nyx only in the
authenticated response to the selected Mac's outbound inventory sync after
that inventory has been accepted. Same-instance increasing sequences are
eligible for redelivery only after current pairing/catalog/storage fences are
rechecked; instance, credential generation, catalog, recipe/artifact, or
storage-binding changes never retarget the job. Acknowledgements are
idempotent and monotonic. A delayed older-revision acknowledgement cannot
unwind a cancellation, and bounded retired terminal acknowledgements are safe
to ignore rather than wedging future inventory sync. Cancellation authorizes
only stopping the exact job and carries no cleanup/delete authority. No Mac
executor or downloader consumes this wire yet.

Frozen snapshot v1 remains the only live routing authority and must not accept
inventory or placement fields. Nyx never learns an exact model path, volume
UUID, scope/bookmark, or raw destination and cannot silently choose a default
directory or relocate existing weights.

## Usage and observability

The serving node remains the sole token-accounting authority. Existing local
SQLite analytics and durable Postgres outboxes continue to generate globally
unique event IDs and attach the stable node ID. Each completed language
response commits its local analytics row and optional delivery-outbox row in
one idempotent SQLite transaction before response completion; Postgres
delivery remains asynchronous and retry-safe.

Nyx does not create a second token-usage row. Its dashboard queries the
existing token ledger with a read-only credential and joins historical
aggregates to live fleet state by node ID and every strict local alias in the
public model mapping. If two public synonyms deliberately map to the same
node alias, the dashboard shows both names and does not invent a single
historical attribution.

Fleet route history is separate from `public.token_usage`. Fleet now carries
its canonical route UUID in the fixed `X-Mnemosyne-Fleet-Route` internal
header, and the native Mac writer reuses that UUID as the token event ID. This
gives a content-free, retry-stable join key without making Nyx a second usage
writer. Extending the central row with pairing generation, deployment ID, and
mapping version remains target work; node ID remains the authoritative serving
device attribution in the current ledger.

The realtime dashboard displays:

- node online, health, accepting, and last-seen state;
- resident engine/model, epoch, transition target, and queue;
- derived, configured, and effective capacity with its source;
- a strict model-by-node deployment matrix;
- active routed requests and bounded queues without request content;
- token totals, request counts, and latency by node/model;
- node usage-outbox depth and delivery health;
- recent bounded route metadata.

Browser updates use a Nyx-owned event stream. Browsers never connect directly
to node inference or control planes.

## Persistence on Nyx

The existing token ledger remains authoritative for historical usage. For
static nodes, Nyx's private TOML contains node URLs and environment-variable
references alongside logical model mappings, queue bounds, and routing
weights; their secret values live only in its private environment. Dynamic
pairing metadata lives in a separate private SQLite database and refers to
locator/credential records in a separate authenticated-encryption database
whose master key is environment-backed. Neither pairing database is the route
history database, and browser-facing APIs expose neither raw locator nor
secret reference. Fleet's route database persists bounded route metadata with
fixed failure codes and contains no request bodies or token rows.

High-frequency heartbeats and active reservations are in-memory state.
Persisting occasional last-known snapshots is permitted for diagnostics, but
stale persisted state is never eligible for routing after Nyx restarts.

## Failure semantics

- Missed snapshot TTL: remove the node from new scheduling; active streams
  continue until their connections terminate.
- Node restart: a changed `instance_id` prevents old-instance reservations
  from being charged against the new snapshot and requires that fresh
  authoritative snapshot for new routing. Each old route still owns and
  releases its reservation only when its upstream response terminates.
- Nyx restart: rebuild live state from fresh polls; never route from persisted
  last-known state.
- Node admission race: retry another candidate only on explicit `node_busy`.
- Every candidate proves pre-work busy: return `429 fleet_capacity_busy` with
  `Retry-After`.
- Node failure before headers: return an error unless non-acceptance is
  provable.
- Node failure after headers: terminate the client stream without failover.
- Token ledger outage: node-local outboxes retain usage; language inference
  continues only while durable capacity remains. At the configured cap, the
  native Mac closes new accounted admission with `usage_outbox_full` instead
  of pruning an undelivered row. Image inference remains outside token
  accounting.
- Uncertain engine state: capacity zero and no new routing until local
  reconciliation succeeds.

## Acceptance requirements

Implementation is not complete until automated or target-host evidence proves:

1. A CUDA request for a different model cannot unload an engine beneath an
   active non-streaming or streaming request.
2. Old-target traffic cannot starve a queued new target.
3. A cancelled or failed stream releases exactly one epoch permit.
4. Node queue and concurrency ceilings hold under concurrent arrival races.
5. Derived capacity and a lower configured ceiling produce the documented
   effective limit.
6. Strictly mismatched revisions, quantizations, capabilities, or load
   digests never enter the same automatic replica group.
7. An offline CUDA node expires and rejoins without restarting Nyx.
8. Requests fan out across warm equivalent deployments without exceeding
   either node's effective capacity.
9. A mid-stream node failure is never retried on another node.
10. Successful requests through Nyx create exactly one token event attributed
    to the serving node.
11. Node and Nyx APIs never return stored credentials, prompts, outputs,
    bookmark bytes, or unbounded diagnostics.
12. Dashboard realtime state agrees with the authoritative snapshots and
    historical usage agrees with the central token ledger.

The isolated automated suites cover the fault-injection cases. The bounded
multi-node target-host procedure and evidence format are defined in
[fleet acceptance](fleet_acceptance.md).
