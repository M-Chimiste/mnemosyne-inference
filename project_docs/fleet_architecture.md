# Mnemosyne Fleet Gateway Architecture

Status: protocol and services implemented; target-host rollout evidence is
tracked in [fleet acceptance](fleet_acceptance.md).

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
- read-only token-ledger queries;

Credentials are stored only in Nyx's private secret store or environment and
are never returned by fleet APIs, written to route history, or sent to the
browser.

## Versioned node protocol

Every node implements:

```text
GET /fleet/v1/snapshot
Authorization: Bearer <node FLEET_API_KEY>
```

This credential is distinct from the node's `INFERENCE_API_KEY`. The response
is one self-consistent document with these top-level fields:

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
uses the following ordered tiers:

1. requested deployment already resident with an available permit;
2. requested deployment resident with the shortest bounded queue;
3. empty node able to load the deployment;
4. node able to drain and safely switch to the deployment;
5. bounded Nyx per-model queue;
6. `429` with `Retry-After` when the fleet queue is full or its deadline
   expires.

Within a tier, Nyx uses weighted least-outstanding selection. A node's
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

Fleet route history is separate from `public.token_usage`. A future optional
request/route ID may be carried in a fixed internal header and stored in both
systems for correlation, but lack of that correlation does not weaken
per-node token attribution.

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

The existing token ledger remains authoritative for historical usage. Nyx's
private TOML contains node URLs, environment-variable references, logical
model mappings, queue bounds, and routing weights. Secret values live only in
its private environment. Fleet's separate SQLite database persists enrolled
node IDs, logical model/deployment mappings, and bounded route metadata with
fixed failure codes. It contains no request bodies or token rows.

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
- Token ledger outage: node-local outboxes retain usage; inference continues
  within the configured outbox cap.
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
