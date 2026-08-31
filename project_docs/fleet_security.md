# Mnemosyne Fleet Security Review

Status: implementation review checklist for Fleet snapshot/routing protocol
version 1 and the implemented Hub-side pairing foundation. Production pairing
still requires the signed Mac ceremony and multi-host acceptance identified in
the pairing contract.

## Trust boundaries

Static Fleet has four distinct trust boundaries:

1. A client authenticates to the Nyx inference endpoint.
2. Nyx authenticates separately to each node's read-only snapshot endpoint.
3. Nyx authenticates separately to each node's inference endpoint.
4. Nyx reads the token ledger through a read-only Postgres role.

Optional dynamic pairing adds two more without collapsing the original four:

5. A Mac claims and manages only its own pairing through the verified Hub
   origin and its claim/management credential.
6. A Fleet administrator approves, enables/disables, and revokes enrollments
   through the existing admin-authenticated boundary.

The browser talks only to Nyx. It never receives a node URL or credential and
never connects directly to a node. Nyx does not receive node administrative
credentials. Node control-plane mutation routes remain outside the fleet
protocol.

The macOS Hub Mode pilot preserves the same boundary. It generates five
distinct random values for the public client, dashboard admin, pairing master,
local snapshot, and local Fleet-dispatch roles. Hub secrets live in a private
mode-0600 environment below Application Support; Hub TOML names only their
environment variables. Only the two local-worker values are added to the
native private `.env`, without replacing token-ledger or unrelated settings.
The Hub binds loopback `:17400`; the guided remote exposure is a separate
Tailscale Serve HTTPS listener. Copying the client or admin value requires an
explicit UI action.

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
- Pairing invitation, locator, enrollment/generation, replay, and revocation
  integrity, plus the encrypted Hub pairing store and its master key.

## Controls

### Enrollment and authentication

- Static nodes are enrolled explicitly by stable reporting `node_id`, private
  base URL, and two environment-backed credentials. Enabling pairing does not
  rewrite or disable those records.
- Paired Macs use an opaque Hub `pairing_id` for enrollment and revocation
  ownership while preserving the reporting `node_id` used by snapshot v1 and
  token history. They receive distinct snapshot, Fleet-only dispatch, and
  pairing-management bearers from a claim-bound provisioning transaction.
- `FLEET_API_KEY` protects only `GET /fleet/v1/snapshot`.
- On native Mac nodes, `FLEET_INFERENCE_API_KEY` protects Nyx-dispatched
  `/v1/*` traffic and is accepted only with Nyx's canonical Fleet route
  marker. The ordinary `INFERENCE_API_KEY` remains the local-client policy.
  Existing static Mac enrollments and the deferred CUDA worker continue to use
  the current `INFERENCE_API_KEY` contract until their explicit migration.
- A node with Fleet discovery enabled but no effective enrolled inference
  credential fails
  snapshot discovery closed with `fleet_inference_auth_unconfigured`; it
  never advertises capacity whose enrolled inference credential is ignored.
- Nyx uses separate client and admin credentials.
- Credential values must be unique across public, admin, and every enrolled
  node role, plus the pairing master key, so compromise of one trust channel
  cannot authorize another.
- Paired locator and credential material is stored under authenticated
  encryption in a private database separate from pairing metadata and Fleet
  route history. The master key is environment-backed and never appears in
  TOML or either database.
- Secrets are absent from TOML/YAML, pairing metadata, SQLite route history,
  snapshot documents, logs, dashboard/status payloads, and browser state
  returned by the service. The one-time invitation response and exact
  claim-bound provisioning response are the only intentional delivery
  surfaces and are never returned by an admin listing.
- Comparisons use constant-time helpers where the runtime provides them.

### Pairing activation and dynamic membership

- Pairing is disabled by default; when disabled its routes are not mounted and
  static scheduling is unchanged.
- Pairing payloads are strict, versioned, size-bounded, idempotent, and return
  fixed public error codes without reflecting caller-controlled secrets or
  topology.
- Invitation claims are single-use, expire without restart extension, require
  the exact admin-authorized normalized locator, and consume a bounded failed-
  secret attempt budget.
- Locator policy allows only configured transports, CIDRs, and ports and
  rejects credentials, paths, query/fragment data, loopback/link-local/
  multicast/unspecified targets, mixed allowed/denied DNS results, and changed
  resolution. Redirect and ambient-proxy use is disabled.
- Paired node clients connect only to a freshly policy-approved numeric peer.
  HTTPS retains the original hostname for SNI and certificate verification,
  and the socket peer must match the approved address before HTTP data is
  written.
- Activation uses only the candidate snapshot bearer and a reduced, path-free
  Fleet-marked `GET /v1/models` probe. It rejects any residency change and
  performs no download, load, or control-plane mutation.
- The production flow activates Hub-disabled and requires a later explicit
  admin enable. Pending, disabled, revoked, unreconciled, or stale paired
  records are absent from new scheduling.
- Dynamic registry replacement/deactivation is generation-fenced. Late polls
  cannot republish a removed enrollment, and a restart requires a fresh
  authenticated snapshot rather than trusting persisted observations.
- The Mac pairing journal and private environment fail Fleet credentials
  closed on inconsistent state without disabling local inference. Pairing-
  owned Fleet credential fields are status-only in the current Swift settings
  surface.

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
- Async batch bodies and results exist only in bounded process memory, are
  addressed by unguessable batch UUID, require the same public inference
  bearer for submit/status/cancel/results, never enter SQLite or admin/
  dashboard payloads, and disappear on expiry, bounded eviction, or restart.
  Batch completion releases the original request body immediately. This is an
  inference result surface, not a durable job or content store.
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
- The implemented pairing slice is still bearer-based. It does not yet prove a
  non-exportable device key or signed pairing transcript, and an unreachable
  Mac may continue accepting an already-provisioned bearer until it receives
  revocation or network policy blocks it. The signed app ceremony, recovery,
  rotation, and representative hardware evidence remain release gates.
- Enabling the Hub pairing API does not make the product workflow complete.
  The Swift begin/resume ceremony is implemented but has not passed signed-
  artifact and representative multi-host acceptance; keep new pairings Hub-
  disabled until activation is proven and a separate administrator explicitly
  enables routing.
- Mac inventory production/sync and Hub persistence are implemented through
  the distinct pairing-management credential. Signed-catalog update and
  advisory placement are also implemented behind separate default-off
  switches, local public-key trust anchors, strict HTTPS update coordinates,
  admin authentication, no-store responses, and path-free protocol shapes.
  They do not grant inference authority or filesystem access. The default-off
  Hub DesiredInstall journal now issues only after an administrator supplies an
  exact eligible advisory basis and Nyx recomputes every authority fence. Its
  separate private database and outbound management-sync delivery retain only
  fixed path-free identities, TTL/revision, delivery, and acknowledgement
  state. Same-key/different-intent requests conflict; changed pairing
  generation, service instance, catalog/recipe/artifact, or opaque storage
  generation never retarget. Cancellation is stop-only and cannot request
  cleanup or delete. The Hub inventory/placement UI and selected-Mac executor
  are now present, but the Mac independently revalidates every path-free fence
  and maps the opaque storage ID to its own exact local authority before using
  the existing durable downloader. Nyx still cannot approve a runtime
  mutation, observe a path/bookmark, register a profile directly, load a
  model, or clean up files; do not expose a node control credential or raw path
  as a shortcut.
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

- With pairing disabled, verify every pairing route is absent and existing
  static nodes retain byte-for-byte scheduling/authentication behavior.
- With pairing enabled, verify invitation/claim/provisioning secrecy,
  idempotency, expiry and attempt bounds, encrypted-store reconciliation,
  wrong-key/tamper failure, locator/peer pinning, Hub-disabled activation, and
  generation-fenced dynamic registry removal.
- Verify a pending credential can access only snapshot and the reduced
  Fleet-marked non-loading model probe; normal Fleet inference requires an
  active pairing, and ordinary local inference remains available in every
  pairing/participation denial state.
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
- Pairing, pause/join, disable, revoke, update, and uninstall-retention tests
  must preserve every engine and inference route plus the exact selected model
  folder spelling, nested/external volume binding, security-scope reference,
  bookmark, install destination, and managed/imported/shared ownership. No
  pairing or inventory operation may copy, move, centralize, or delete weights.
- Run dependency and container/image scanning in the deployment environment.
