# Mnemosyne Fleet Security Review

Status: implementation review checklist for Fleet protocol version 1.

## Trust boundaries

Fleet has four distinct trust boundaries:

1. A client authenticates to the Nyx inference endpoint.
2. Nyx authenticates separately to each node's read-only snapshot endpoint.
3. Nyx authenticates separately to each node's inference endpoint.
4. Nyx reads the token ledger through a read-only Postgres role.

The browser talks only to Nyx. It never receives a node URL or credential and
never connects directly to a node. Nyx does not receive node administrative
credentials. Node control-plane mutation routes remain outside the fleet
protocol.

LAN and Tailscale are transport choices, not identities. An address discovered
through DNS or MagicDNS is never enrolled automatically.

## Protected assets

- Client, node-snapshot, node-inference, dashboard-admin, and ledger
  credentials.
- Model storage paths, security-scoped bookmark material, environment values,
  command lines, and arbitrary engine diagnostics.
- Prompts, responses, embeddings, generated images, and provider-specific
  request extensions.
- Accurate deployment identity, capacity, residency, and per-node token
  attribution.
- Availability of each independently managed inference workstation.

## Controls

### Enrollment and authentication

- Nodes are enrolled explicitly by stable `node_id`, private base URL, and two
  environment-backed credentials.
- `FLEET_API_KEY` protects only `GET /fleet/v1/snapshot`.
- `INFERENCE_API_KEY` protects node `/v1/*` traffic.
- A CUDA or native Mac node with Fleet discovery enabled but no
  `INFERENCE_API_KEY` fails
  snapshot discovery closed with `fleet_inference_auth_unconfigured`; it
  never advertises capacity whose enrolled inference credential is ignored.
- Nyx uses separate client and admin credentials.
- Credential values must be unique across public, admin, and every enrolled
  node role so compromise of one trust channel cannot authorize another.
- Secrets are absent from TOML/YAML, SQLite route history, snapshot documents,
  logs, dashboard payloads, and browser state returned by the service.
- Comparisons use constant-time helpers where the runtime provides them.

### Discovery and snapshots

- Snapshots use a strict versioned schema with unknown fields rejected.
- Snapshot HTTP bodies must be identity encoded and are streamed through a
  fixed 8 MiB cap before JSON/schema validation; declared oversize bodies are
  rejected without being read.
- Nyx verifies the enrolled node ID, fresh process instance, increasing
  snapshot sequence, strict deployment hash, immutable provenance, and bounded
  fields before making a node eligible.
- Once an enrollment advances to a new process instance, snapshots naming a
  retired predecessor instance are rejected and do not refresh liveness.
- Retired-instance replay state is capped without eviction. Once one
  enrollment exhausts its 1,024-transition budget, further instance churn
  fails closed rather than forgetting an older replay fence.
- Liveness expires from Nyx's monotonic receipt time. A node-controlled wall
  clock cannot extend its TTL.
- Persisted last-known snapshots are never routing authority after restart.
- Diagnostics crossing the boundary are fixed codes, not arbitrary strings.

### Data plane

- Nyx accepts only the explicitly implemented inference routes.
- Request bodies are size bounded.
- `model` is the only body field Nyx rewrites.
- Client authorization, cookies, host, hop-by-hop headers, and reserved
  `X-Mnemosyne-*` routing/proof headers are removed before forwarding; Nyx
  injects only the selected node inference credential and its own route ID.
- Node URLs come only from validated enrollment configuration. Redirect
  following remains disabled.
- Node HTTP clients ignore ambient `HTTP_PROXY`, `HTTPS_PROXY`, and related
  environment variables so prompts and bearer credentials cannot be diverted
  through an unintended host proxy.
- Fleet-owned polling and inference clients do not add an implicit active
  connection limit above scheduler admission. Explicit node capacity and
  Fleet reservations remain authoritative, while each client keeps at most 20
  idle connections.
- CUDA and native manager clients that carry requests or control probes to
  loopback engine children also ignore ambient proxy variables. External Hub
  and runtime-update clients retain their separately configured network
  behavior.
- Route history stores fixed metadata and failure codes only.
- The service never logs or persists request or response bodies.
- A response is never retried after headers or body bytes arrive. Generic
  timeouts and ambiguous connection loss are not automatic failover signals.
  Retry is limited to a `429` with the manager-owned
  `X-Mnemosyne-Error: node_busy` proof header or a proven
  connection-establishment failure. Managers strip that reserved header from
  engine responses, and Fleet does not trust body-only error codes.
- Fleet closes a proven-busy response without consuming its body. A `429`
  without that header is terminal and streamed under the same full-response
  reservation ownership as every other non-busy response. Exhausting all
  candidates with proven pre-work rejection returns bounded `429
  fleet_capacity_busy`, not an availability error that implies node failure.

### Admission and process safety

- Every node enforces its own epoch-tagged full-stream permit; Nyx reservations
  cannot override node admission.
- Reservation and lease release are cancellation shielded. Separately owned
  response/client/lease cleanup survives repeated cancellation and the outer
  ASGI response owns cleanup even if body iteration never starts. Cleanup
  attempts response close, client close, metadata completion, and capacity
  release independently so a failed close cannot strand admission.
- FIFO queues and concurrency ceilings are bounded.
- A model transition drains every lease before unloading the resident engine.
- Unknown or degraded engine state advertises zero routable capacity.
- Administrative unload, shutdown, runtime activation, and file maintenance
  close admission before draining.

### Usage data

- The serving node remains the sole writer of a token event.
- Globally unique event IDs and Postgres conflict handling preserve
  idempotency.
- Nyx uses the existing central ledger only for bounded aggregate queries.
- The ledger role should receive `SELECT` on `public.token_usage`, not schema
  mutation or write privileges.
- Fleet route metadata stays in a separate SQLite database and does not extend
  the token schema with prompts, outputs, or mutable live state.

## Residual risks and operator responsibilities

- Plain HTTP on an untrusted LAN exposes bearer tokens and inference content.
  Use Tailscale ACLs, a trusted isolated LAN, or TLS termination on both Nyx
  and nodes.
- Bearer credentials do not provide user-level attribution. Rotate them after
  suspected disclosure and use Nyx access logs outside this application when
  individual audit identity is required.
- Snapshot polling is eventually consistent. Nyx may route against a recently
  stale capacity value; node-local admission is the hard safety boundary and
  may return `node_busy`.
- A node that is already trusted can lie about its artifact identity. Version 1
  verifies self-consistency and immutable provenance, not remote attestation.
- The central usage alias is historical node output. If an alias is later
  reassigned, old rows cannot be retroactively proven to have the new
  deployment identity. Preserve alias meaning or interpret old rows by their
  event timestamp and configuration history.
- SQLite route history is metadata, not a security audit log. Operators who
  require tamper evidence should ship bounded gateway logs to an external
  append-only system.

## Release checks

- Search every snapshot/dashboard fixture for absolute paths, credential
  values, DSNs, prompts, and outputs.
- Verify snapshot access fails when `FLEET_API_KEY` is absent or wrong.
- Verify node inference access uses the distinct inference key.
- Verify public client credentials cannot access the fleet admin API.
- Verify node URLs and secret environment-variable values are absent from
  every browser-facing response.
- Verify malformed, replayed, mismatched, or stale-instance snapshots never
  refresh routable liveness.
- Verify queue-full retry occurs only for the fixed pre-work error.
- Verify a midstream disconnect produces no second node request.
- Run dependency and container/image scanning in the deployment environment.
