# Mnemosyne Fleet Pairing Protocol

Status: partially implemented design contract. The opt-in Hub-side version-1
API foundation, encrypted Hub secret store, locator policy, peer-pinned
activation transport, non-loading probes, dynamic enrollment registry, and
Mac-local pairing state/authentication foundation are implemented. Pairing is
disabled by default, and existing static Fleet enrollment plus native macOS
inference remain authoritative and compatible.

This is not yet signed-release production pairing. The Mac service and Swift UI
now request a hidden invitation from one entered Hub URL, display a six-digit
presence code, claim the invitation, poll after approval, stage the provisioned
bundle, and acknowledge activation while retaining the invitation secret only
in view-model memory. The original manual invitation flow remains available.
The same **Inference Pool** page now exposes an explicitly confirmed permanent
**Remove this Mac from Nyx** action backed by the loopback
`POST /manager/fleet/pairing/revoke` route. The existing native control-plane
policy applies: a configured `ADMIN_PASSWORD` requires Basic authentication;
when it is unset on the default loopback bind, the route uses the product's
same-user/local-process trust boundary and has no separate authorization token.
Non-loopback control remains unable to start without that password. A dynamic Mac can revoke only its
own enrollment; successful self-revocation durably closes the live Mac
authority cache before the local operation returns, and a Hub-success/local-
failure outcome requires replaying the exact request ID. The signed release
artifact and representative crash/recovery ceremony are not yet accepted.
Routine rotation, adopt-static finalization/rollback, remote Mac notification,
the authenticated
lifecycle helper, and representative multi-host signed-artifact acceptance
remain unimplemented or incomplete. Runtime Mac-inventory sync, the signed-
catalog last-known-good/update substrate, path-free selected-Mac installation,
and journaled lifecycle planning/executor core are implemented but have not
cleared those production gates. The current UI
shows secret-free pairing state, protects pairing-owned credential fields,
provides local join/pause, drives the bounded begin/resume ceremony, and resumes
an outcome-unknown removal with the service-owned request ID; those controls
are not evidence of signed multi-host acceptance.

This document defines the Mac-first pairing boundary between one Mnemosyne Mac
worker and the Nyx Hub. It deliberately separates an initial bearer-based slice
that can be implemented on the current architecture from later device-key and
short-lived-token hardening. The six-digit presence code is now an implemented
authenticated-admin wrapper around the high-entropy invitation; it is not a
credential or a substitute for those later layers. A schema or UI placeholder
must never be reported as implemented behavior.

## Goals and non-regression boundary

Pairing should let an administrator enroll a signed Mac app without copying
long-lived credentials by hand. It establishes durable Hub enrollment; it does
not transfer control of the Mac to Nyx.

Pairing and every subsequent lifecycle operation must preserve:

- all current macOS inference engines and routes, including llama.cpp, oMLX,
  DS4, MFLUX, chat completions, completions, Responses,
  Messages, embeddings, rerank, image generations, and the local model catalog;
- opaque forwarding of request fields outside manager-owned authentication,
  model selection, image normalization, and usage handling;
- JIT loading of already installed models, single-resident coordination,
  engine-derived capacity, batching/parallel slots, bounded queues, complete
  response-stream leases, and configured idle unloading;
- local inference while the Mac is paused or Nyx is unavailable;
- every exact user-selected model folder, nested external-volume path, volume
  UUID, security-scope reference, and bookmark;
- imported, shared, externally owned, and managed model ownership distinctions;
- model profiles, pinned revisions, projectors, load/context settings, engine
  alternatives, managed runtimes, partial downloads, and install provenance;
- local usage analytics, stable event IDs, per-device accounting, and the
  durable delivery outbox;
- the existing rule that Fleet route metadata and browser payloads contain no
  prompts, responses, credentials, model paths, bookmarks, or arbitrary engine
  diagnostics.

Pairing must never overwrite an existing ordinary `INFERENCE_API_KEY`, disclose
the Mac control password, expose the control plane to Nyx, download a model,
load a model, change a storage location, or make the Mac join the pool.

## Terms and identities

The following identities are intentionally different:

- `pairing_id`: an opaque Hub-assigned UUID that identifies one durable Mac/Hub
  enrollment. It is the primary key for pairing, credential generations,
  revocation, and dynamic registry ownership.
- `reporting_node_id`: the stable node ID carried by snapshot v1 and existing
  token-ledger events. Existing explicit or legacy-migrated values are retained
  so pairing does not split historical per-device accounting.
- `display_name`: a mutable operator-facing label. It is never an identity or
  authorization input.
- `instance_id`: the manager process identity already carried by each snapshot.
  It changes after a service restart and remains part of snapshot replay
  protection.
- `invitation_id`: one pairing ceremony identifier. It expires and cannot be
  reused to create another pairing.
- `claim_id`: one idempotent claim of an invitation.
- `request_id`: a caller-generated canonical UUID that makes one mutating API
  operation idempotent.

For a previously configured Mac, the Hub binds the new `pairing_id` to its
existing `reporting_node_id`. If a Mac still derives that ID from its hostname,
the first successful pairing must persist the resolved value so a later host
rename cannot silently create another ledger identity. New installations may
receive a random reporting ID. Hardware serial numbers, MAC addresses,
hostnames, DNS names, and Tailscale addresses are not pairing identities.

Snapshot protocol v1 remains unchanged in the initial slice. Its
`node.node_id` continues to carry `reporting_node_id`; the unique snapshot
credential and Hub enrollment bind that value to `pairing_id`. A later protocol
version may carry both fields explicitly, but a future field must not be added
to snapshot v1.

## Threat model

The protocol must defend against:

- an unpaired host guessing, stealing, replaying, or racing a pairing code;
- passive traffic capture and active interception on a LAN;
- DNS rebinding, redirects, ambient proxy variables, or a malicious locator
  causing Nyx to contact an unintended service;
- a public Fleet client, paired node, or browser session attempting to gain
  Fleet-admin or another node's authority;
- replay or reordering of claims, approvals, activation acknowledgements,
  credential rotations, and stale node snapshots;
- a copied application-state directory producing two active enrollments;
- credentials appearing in TOML/YAML, `fleet.db`, WAL files, route history,
  logs, diagnostics, browser state, crash reports, or acceptance artifacts;
- process or machine failure after any durable transition;
- compromise of one node credential becoming authority for another node or
  another trust channel.

The initial protocol does not claim to defend against a fully compromised live
Nyx process or a fully compromised paired Mac. Nyx necessarily sees routed
request content and active node credentials; a trusted node can lie about its
own model state. Per-device, per-role credentials and strict scheduling limits
their blast radius. Disk encryption of Hub secrets protects offline media and
backups, not a live process with access to its decryption key.

## Credential roles

Every value below is independently generated and must be unequal to every
other configured credential known to the Hub.

| Credential | Direction and authority | Initial storage |
| --- | --- | --- |
| Pairing secret | Mac to Hub; claims one invitation only | Returned once to the admin, held transiently by the Mac, verifier/material only in the encrypted Hub secret store |
| Snapshot bearer | Hub to one Mac; `GET /fleet/v1/snapshot` only | Mac private service state and encrypted Hub secret store |
| Dispatch bearer | Hub to one Mac; Fleet-tagged inference routes and a safe activation probe only | Mac private service state and encrypted Hub secret store |
| Management bearer | One Mac to Hub; its own activation, status, routine rotation, and permanent self-revoke only | Mac private pairing state and encrypted Hub secret store |
| Fleet client key | Client to Nyx inference | Existing Hub secret/environment boundary; never exchanged during pairing |
| Fleet admin key | Administrator to pairing, enrollment, route, and policy APIs | Existing Hub secret/environment boundary; never sent to a Mac |
| Local inference key | Ordinary clients to the Mac | Existing Mac configuration; never changed by pairing |
| Local control password | Menu/local operator to the Mac control plane | Existing Mac configuration; never sent to Nyx |
| Ledger DSN and Hub/download credentials | Existing narrowly scoped roles | Never derived from or exchanged during pairing |

The initial dispatch bearer requires a new Fleet-only authentication slot on the
Mac. For inference POSTs, it is accepted only with the canonical
`X-Mnemosyne-Fleet-Route` proof inserted by Nyx. It must not replace the
ordinary `INFERENCE_API_KEY` or become a general LAN client key. A safe
`GET /v1/models` activation probe may use the dispatch bearer without a route
reservation; it performs no model load or download.

The management bearer is not a Mac control-plane credential. It cannot mutate
another pairing, public model mappings, service class, Hub enablement, or Hub
policy, and it cannot call the Mac's loopback administrative API through Nyx.
An administrator may revoke any authorized enrollment through the Hub-admin
boundary; possession of one management bearer may only self-revoke or request
rotation of that exact pairing.

Initial Mac persistence may reuse the service's private mode-`0600` environment
and pairing-state pattern for the snapshot and dispatch bearers. The app must
show only configured state and credential generation, never the generic
prefix/suffix secret preview. Device private keys and shared Keychain access are
later hardening and require signed-app/LaunchAgent acceptance before use.

## Protocol versions and state machines

All pairing and management JSON payloads carry `schema_version: 1`. Unknown
fields are rejected. Bounded strings use UTF-8 and fixed maximum lengths;
timestamps are finite UTC epoch seconds; IDs are canonical lower-case UUIDs.
Bodies, headers, and response sizes are bounded before parsing.

An implementation advertises its minimum and maximum pairing protocol versions.
The Hub selects one exact mutually supported version. Unsupported versions,
unknown fields, and downgrade attempts fail closed before an invitation is
claimed.

### Pairing transaction

```text
issued -> claimed -> approved -> provisioning -> activating -> completed
   |         |          |             |              |
   +------> expired      +----------> failed <--------+
             |          \\-> rejected
             \\-> rejected
```

- `issued`: an administrator created an unexpired invitation.
- `claimed`: its secret is bound to exactly one `claim_id`, request digest,
  reporting identity, and proposed locator.
- `approved`: an authenticated administrator approved that exact claim and
  locator.
- `provisioning`: one credential generation exists in the encrypted secret
  store and may be delivered only to that claim.
- `activating`: the Mac durably staged the generation and Nyx is running
  non-loading probes.
- `completed`: the enrollment is active or deliberately Hub-disabled, and the
  invitation secret has been destroyed.
- `expired`, `rejected`, and `failed` are non-routable. A failed transaction may
  resume only where its journal explicitly permits; it never starts a new claim
  with the same invitation.

An invitation is single-use when the first valid claim atomically binds it to
one `claim_id`. Repeating the exact request from the same `request_id` resumes
that transaction; it does not permit another device or different payload to
claim the invitation.

### Enrollment

```text
pending -> active -> revoked
             |
             +-- hub_enabled = false/true
```

`hub_enabled` is Hub policy orthogonal to pairing state. A paired but disabled
node retains its credentials and inventory but receives no new routes. A
revoked pairing cannot be re-enabled; it requires a new ceremony.

Local participation is a separate Mac-owned state machine:

```text
joined -> draining -> paused -> joined
```

No positive state overrides a denial on the other side. Effective routing
requires active pairing, non-revocation, Hub enablement, local join, a fresh
authoritative snapshot, an exact deployment/capability match, and open local
admission.

### Credential generation

```text
candidate -> active -> retiring -> retired
     |          |
     +------> revoked <------+
```

Only one generation is active for new Hub requests. During a bounded rotation
overlap, the Mac may accept the active and candidate/retiring values while Nyx
uses only the generation selected by its durable enrollment record.

## Initial version-1 ceremony

The Hub routes in steps 1-4 and 7 are implemented only when `[pairing]` is
explicitly enabled, as is the management activation-ack route in step 5. The
Hub then performs the pinned probes in step 6 and can publish a separately
enabled paired enrollment into the dynamic registry. The Mac service and
signed app drive both the default presence-code wrapper and the manual
begin/resume ceremony, durably stage/activate credentials, and enforce
pending-versus-active authority. Later lifecycle behavior in this document
remains target contract unless the implementation-status text above says
otherwise.

### 1. Administrator creates an invitation

```http
POST /fleet/api/v1/pairing/invitations
Authorization: Bearer <Fleet admin key>
Content-Type: application/json
```

```json
{
  "schema_version": 1,
  "request_id": "b74a0867-2c7d-420d-b8e2-f88a99cf2224",
  "intent": "new",
  "expected": {
    "platform": "macos",
    "reporting_node_id": null,
    "locator": "https://mac-a.example.internal:1240",
    "transport": "https",
    "service_class": "primary"
  },
  "expires_in_seconds": 300
}
```

`intent` is `new` or `adopt-static`. Static adoption additionally names the
existing enrollment in authenticated admin state and must bind its exact
reporting identity and locator.

The request supplies the exact locator the administrator authorizes. The Hub
normalizes and validates it before issuing an invitation. APIs never echo the
raw locator to a dashboard response; the browser may retain the value it just
submitted only in its current in-memory form.

The response returns the following once and uses `Cache-Control: no-store`:

```json
{
  "schema_version": 1,
  "invitation_id": "3610d060-aa50-4fd5-8454-d15a17e88a53",
  "pairing_secret": "<at-least-256-bits-of-url-safe-randomness>",
  "hub_origin": "https://nyx.example.internal",
  "expires_at": 1788019500.0
}
```

The full secret is suitable for a QR/deep link or explicit copy. It is not a
six-digit code. The Hub stores no raw value in Fleet metadata. Invitations have
a maximum five-minute lifetime, a small fixed failed-attempt budget, per-
invitation and global rate limits, and a bounded total pending count. Rate
limiting cannot rely solely on a forwarded source IP unless the exact trusted
reverse proxy is configured.

#### Default client-initiated presence-code wrapper

The signed Mac app normally asks its loopback service to start with only one
administrator-supplied HTTPS Hub origin. The app obtains the Mac's exact
MagicDNS name from the installed Tailscale CLI and derives the private worker
locator as `http://<magic-dns-name>:1240`. The service submits its bounded
identity, locator, transport, and one idempotent request ID to:

```http
POST /fleet/pairing/v1/requests
Content-Type: application/json
```

The Hub applies the same locator policy and creates an ordinary five-minute,
high-entropy invitation. Its response is `Cache-Control: no-store` and is
accepted only by the local loopback service. A six-digit display code is
derived from the first eight big-endian bytes of
`HMAC-SHA256(invitation_secret,
"mnemosyne-fleet-presence-pin-v1") mod 1_000_000`; it shares the invitation's
expiry and fixed attempt budget and is never a credential. The strong secret
continues through the ordinary claim/provisioning path only in bounded memory.
The browser never receives it.

After the claim appears, an authenticated Hub administrator submits the code
to `POST /fleet/api/v1/pairing/claims/{claim_id}/approve-presence`. Comparison
is constant-time. A mismatch consumes the ordinary invitation attempt budget.
Successful presence approval still commits `hub_enabled=false`. The Mac polls
and completes provisioning and activation, after which the Hub Mac's native
**Pair & Enable** action issues the existing separate authenticated enable
transaction. The native UI reads the private admin bearer just in time from
Hub private state and sends it only to the fixed loopback origin with proxies
and redirects disabled. The bearer never enters view state, while the PIN
exists only in ephemeral view-model memory and never enters preferences or
disk. A timeout leaves the activated enrollment disabled and visible with a native
**Enable** recovery action; it never enables speculatively. Manual invitation
creation and exact-locator approval remain available in the dashboard as an
Advanced recovery/interoperability surface.

### 2. The Mac claims the invitation

The signed menu app connects to the exact HTTPS `hub_origin` and verifies the
normal platform trust chain. It does not offer a skip-verification switch.

```http
POST /fleet/pairing/v1/claims
Content-Type: application/json
```

```json
{
  "schema_version": 1,
  "request_id": "e65c3b08-e476-4057-a0be-f57de2fbfbec",
  "invitation_id": "3610d060-aa50-4fd5-8454-d15a17e88a53",
  "pairing_secret": "<secret>",
  "mac": {
    "platform": "macos",
    "service_version": "0.9.0",
    "display_name": "Studio Mac",
    "reporting_node_id": "metis"
  },
  "locator": "https://mac-a.example.internal:1240",
  "supported_protocol": {"minimum": 1, "maximum": 1}
}
```

The Hub compares the invitation secret in constant time, checks expiry and
attempt limits, negotiates one version, and requires the normalized locator to
equal the administrator-authorized locator exactly. For an adoption, the
reporting identity must also equal the static enrollment. It then atomically
binds the invitation to this request and assigns `claim_id` and `pairing_id`.

Before the first outbound claim, the Mac durably records the exact invitation,
Hub origin, and normalized locator but never the invitation secret. A restart
therefore resumes only that exact attempt and refuses different invitation
data. When the Hub conclusively rejects the claim and the Mac has received no
claim ID, pairing ID, credential generation, or pairing-owned credential, the
signed Settings UI may explicitly discard that failed attempt. The local
pairing store and client journal must clear atomically, and the private
credential environment must be proven unchanged. Ambiguous network or Hub
outcomes remain fenced to the exact recorded attempt and are never discardable.

The response contains only bounded non-secret status:

```json
{
  "schema_version": 1,
  "claim_id": "71a1ca20-2cd3-4e8c-b784-32bb0bc0299a",
  "pairing_id": "28bfef6e-ce8d-4cd7-828e-79a3c99642eb",
  "state": "claimed",
  "expires_at": 1788019500.0,
  "locator_accepted": true
}
```

The pairing secret must be stripped from access logs before request logging,
never included in exception text, and never included in an idempotency digest
stored in `fleet.db`.

### 3. Administrator approves or rejects the exact claim

The admin UI shows the pending `pairing_id`, display name, platform, reporting
ID, version, claim time, and locator transport classification. It does not
receive the raw locator or any credential. The value the administrator entered
when creating the invitation remains the only browser-side raw locator.

```http
POST /fleet/api/v1/pairing/claims/{claim_id}/approve
Authorization: Bearer <Fleet admin key>
```

```json
{
  "schema_version": 1,
  "request_id": "45015770-caf5-42a4-b4f7-6849e94568ca",
  "locator": "https://mac-a.example.internal:1240",
  "service_class": "primary",
  "hub_enabled": false
}
```

The approval resubmits the exact locator and must match the issued invitation
and claim after normalization. The production ceremony starts
`hub_enabled=false`: activation succeeds without making the node routable, and
the administrator explicitly enables it afterward. The low-level Hub payload
currently carries the boolean, but setting it true during approval is not an
accepted production flow until a separate-enable invariant is enforced and
covered end to end. Service class is Hub-owned and cannot be supplied or
changed by the Mac.

Rejecting a claim destroys its invitation secret reference and makes every
later claim/provision request for it fail with a fixed terminal code.

### 4. Hub creates and delivers one credential generation

After approval, Nyx generates three independent 256-bit random values for
snapshot, dispatch, and management roles. It first commits an encrypted
candidate generation to the secret store and then commits only its opaque
references and generation number to Fleet metadata.

The Mac polls the claim with the invitation secret using a POST body, never a
query parameter. Before approval it receives only state. After approval it may
retrieve this exact bundle during a short provisioning window:

```json
{
  "schema_version": 1,
  "claim_id": "71a1ca20-2cd3-4e8c-b784-32bb0bc0299a",
  "pairing_id": "28bfef6e-ce8d-4cd7-828e-79a3c99642eb",
  "reporting_node_id": "metis",
  "credential_generation": 1,
  "credentials": {
    "snapshot_bearer": "<secret>",
    "dispatch_bearer": "<secret>",
    "management_bearer": "<secret>"
  },
  "state": "provisioning"
}
```

This is the only API response that contains durable node credentials. It is
never returned to an admin/browser API. If delivery is ambiguous, the same
claim may retrieve the same already-created bundle—not a regenerated bundle—
until the Mac acknowledges durable staging or the provisioning window expires.
The invitation remains bound to that claim, so this recovery behavior does not
allow a second pairing.

### 5. The Mac stages without disturbing local inference

The Mac atomically persists:

- `pairing_id`, Hub origin, reporting identity, generation, and pairing state;
- the new snapshot and Fleet-only dispatch bearers in private service state;
- the management bearer in private pairing state;
- the existing local join/pause preference unchanged.

It does not alter ordinary inference credentials, control credentials, model or
runtime configuration, storage paths, grants, profiles, downloads, resident
state, or usage state. A restart loads the staged generation in activation-only
mode: the snapshot route, safe dispatch probe, and management acknowledgement
are available, but candidate credentials do not authorize normal Fleet
inference until activation commits.

The Mac then calls a management endpoint with the candidate management bearer:

```http
POST /fleet/management/v1/pairings/{pairing_id}/activation-ack
Authorization: Bearer <candidate management bearer>
```

```json
{
  "schema_version": 1,
  "request_id": "e8719a56-4195-4757-ad9b-aab75ef533f4",
  "credential_generation": 1,
  "reporting_node_id": "metis",
  "service_instance_id": "<bounded-instance-id>"
}
```

This proves only possession of the management bearer and durable Mac staging;
it is not device-key proof. The residual risk and later replacement are stated
below.

### 6. Nyx runs activation probes

Nyx performs no model load, inference, download, or control-plane mutation.

1. It polls `GET /fleet/v1/snapshot` with the candidate snapshot bearer.
2. It validates the frozen snapshot-v1 schema, exact
   `node.node_id == reporting_node_id`, `platform == macos`, fresh process
   instance and sequence, bounded fields, and configured dispatch
   authentication.
3. It requests `GET /v1/models` with the candidate Fleet dispatch bearer and a
   fresh canonical `X-Mnemosyne-Fleet-Route` UUID. This explicitly safe probe
   proves the dispatch-only credential and marker boundary while bypassing
   ordinary inference admission; the Fleet-marked response is a deliberately
   reduced, path-free catalog and does not change the richer local response
   used by existing clients. The probe never loads a model, even when local
   pool participation is paused.
4. It rechecks that the connected destination is the approved locator under the
   locator policy below.

Both HTTP clients keep `trust_env=false` and `follow_redirects=false`. A
response from any other locator, a redirect, identity mismatch, credential
failure, malformed snapshot, or probe timeout leaves the pairing non-routable.

### 7. Activation commits

After both probes and the Mac acknowledgement succeed, Nyx marks generation 1
active and sends an idempotent activation-complete acknowledgement to the Mac.
The Mac marks its staged dispatch credential active and returns confirmation.
Only then may Nyx publish the enrollment to the dynamic registry. If either
side is uncertain, the enrollment remains `pending` and normal dispatch stays
closed.

The Hub destroys the invitation secret after completion. Later status APIs
return pairing state, IDs, generation, fixed failure codes, and timestamps only.

## Locator and transport policy

LAN and Tailscale addresses are transport locators, never identity.

### Pairing and management transport

The initial production slice requires verified HTTPS from the Mac to Nyx for
pairing and management. The Mac follows no redirect from the invitation's exact
Hub origin and never disables certificate or hostname verification. A reverse
proxy is trusted only when explicitly configured; forwarded client addresses
from any other peer are ignored.

### Hub-to-Mac transport

Supported secure-default modes are:

- `https`: end-to-end HTTPS with hostname verification; or
- `tailscale`: a locator reached over a verified Tailscale route and restricted
  by explicit tailnet ACLs/firewall policy. HTTP inside that WireGuard-protected
  route may be allowed, but the route/interface and allowed destination range
  must be configured rather than inferred from a hostname.

Generic plain HTTP on a LAN exposes bearer credentials and inference content.
If retained for backward compatibility, it is an explicit advanced
`trusted_lan_http` mode with a persistent warning and is not sufficient for the
secure-default multi-host acceptance gate.

### Locator normalization and SSRF controls

An approved locator:

- uses only `http` or `https` as permitted by its declared transport mode;
- has one canonical host and explicit allowed port;
- contains no userinfo, query, fragment, non-root path, encoded authority,
  ambiguous IPv4 form, IPv6 zone identifier, or control character;
- resolves only to addresses in configured per-transport allowlists;
- rejects unspecified, multicast, broadcast, link-local, metadata-service,
  IPv4-mapped bypass, and mixed allowed/disallowed address sets;
- rejects loopback for a remote Mac. Loopback is allowed only for a separately
  configured Nyx-local worker identity, never from a Mac pairing claim;
- is resolved and revalidated at claim, approval, activation, and connection
  time. The actual peer must match an allowed resolution so DNS rebinding cannot
  move a credential-bearing request to another destination;
- never follows redirects and never uses `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`,
  or a netrc credential.

Changing the locator after pairing is a new admin-approved transaction. A Mac
cannot update it with only its management bearer. The dynamic registry keeps
the active locator immutable until the replacement has passed the same probes;
failure retains the prior locator.

## Hub persistence and encrypted secret boundary

`fleet.db` remains secret-free. In particular it stores no plaintext or
ciphertext credential, invitation verifier, DSN, raw locator, request/response
body, prompt, output, or model path.

Fleet metadata may store:

- pairing, claim, request, and reporting IDs;
- protocol version, platform, display name, lifecycle state, service class,
  Hub enablement, credential generation, timestamps, and fixed failure codes;
- opaque `secret_ref` and `locator_ref` identifiers;
- idempotency digests computed only over secret-free canonical fields;
- snapshot replay fences and bounded path-free diagnostic metadata.

The exact locator is topology-sensitive and lives beside the credentials in a
separate private encrypted enrollment store. The store uses authenticated
encryption per record with a fresh nonce and associated data binding Hub ID,
`pairing_id`, role, generation, and schema version. Its master key comes from a
systemd encrypted credential, OS keyring, TPM/KMS, or equivalent deployment
secret that is not in the repository, TOML, `fleet.db`, or the same backup.

Secret-store requirements:

- directory mode `0700`, files `0600`, atomic replace, authenticated format
  version, and bounded record sizes;
- wrong key, corrupt tag, missing record, role mismatch, or reference mismatch
  fails the affected enrollment closed without plaintext fallback;
- logs and errors expose only fixed codes and opaque pairing/generation IDs;
- orphaned staged records are recoverable or garbage-collected only after
  comparing the metadata journal;
- metadata and encrypted-store backups are coordinated, while the master key is
  backed up separately under operator control;
- master-key rotation rewraps records transactionally and retains a tested
  rollback until validation completes.

The current global Fleet client/admin keys and ledger DSN may remain supplied by
the existing deployment secret mechanism during the first slice. An environment
file stored in plaintext does not satisfy an encrypted-at-rest claim; production
packaging should use systemd encrypted credentials or an equivalent secret
source before making that claim for global credentials.

## Dynamic registry semantics

Static configuration and paired enrollments feed one scheduler, but their
ownership remains explicit.

Each in-memory enrollment has `source = static` or `source = paired`:

- Static nodes continue to resolve their locator and two current credentials
  from private TOML environment references. Existing behavior remains the
  compatibility fallback.
- Paired nodes resolve their active locator and credential generation from the
  encrypted store through opaque references.
- Duplicate active `pairing_id`, reporting identity, or exact locator across
  either source is rejected. A deliberate static adoption is the only atomic
  exception and never exposes two routable copies at once.
- Only active, non-revoked, Hub-enabled enrollments get pollers and scheduler
  candidates. Local pause is learned from the authenticated snapshot and
  advertises zero availability.
- Registry add, replace, disable, and revoke operations are serialized with
  scheduler wake-up and reservation ownership. A replacement does not mutate an
  enrollment object already owned by an active route.
- Revocation or hard failure removes the node from new scheduling immediately.
  Existing route ownership is handled according to the revoke policy below and
  never transferred or retried after ambiguous work.
- A Hub restart reconstructs configured enrollments but grants no routing
  authority from persisted snapshots. Every active pairing must produce a fresh
  authenticated snapshot before becoming eligible.
- Persisted replay fences may reject retired instance/sequence rollback, but a
  persisted last-known snapshot is diagnostic only.

The scheduler and route history use `pairing_id` as the enrollment key for new
paired nodes while retaining `reporting_node_id` for usage joins and legacy
views. Browser responses expose neither locator nor credential reference.

## Pause, Hub disable, and permanent revoke

These operations are intentionally different.

### Local pause

Pause is Mac-owned and already conceptually independent of enrollment. It
closes new Fleet admission, advertises no routable capacity, allows admitted
streams to release normally, and retains pairing, local inference, models,
downloads, residency policy, credentials, storage, and outbox delivery. Joining
again requires no pairing or credential change and becomes routable only after
a fresh healthy snapshot.

### Hub disable

Hub disable is a reversible administrator policy. Nyx stops new routing and
allows existing Hub-owned streams to finish under their existing reservations.
Credentials and pairing remain. A management bearer cannot re-enable its own
pairing.

### Revoke

Revocation is irreversible for that `pairing_id`:

1. An authenticated Hub administrator, or the paired Mac's management bearer
   acting only on that same pairing, durably sets `revoked` before Hub secret
   deletion.
2. Nyx removes the enrollment from new scheduling immediately.
3. Hard revocation closes its active Hub-to-node connections without retrying
   the request elsewhere. Work may already have started at the node; the route
   receives a fixed revoked/terminated outcome rather than duplicate inference.
4. Nyx revokes encrypted credential references and best-effort notifies an
   online Mac.
5. The Mac deletes its local snapshot, dispatch, and management generation,
   retains a non-secret revoked tombstone, and requires a new pairing.

Hub-side routing authority is revoked even when the Mac is offline. The initial
static bearer design cannot make an unreachable Mac instantly stop accepting a
bearer already stored on that Mac. Direct-use exposure ends when the Mac
receives revocation, the credential is locally removed/rotated, or network ACLs
block it. Short-lived scoped credentials that bound this offline window are
later hardening; the initial implementation and acceptance report must state
this limitation rather than claim immediate remote invalidation at an offline
endpoint.

### Mac-initiated permanent removal

**Remove this Mac from Nyx** is an explicit pairing action, not uninstall and
not pause. The Swift client calls the loopback
`POST /manager/fleet/pairing/revoke` route with `schema_version: 1` and one
canonical caller-generated `request_id`. The service then:

1. durably binds that request to the exact pairing ID, reporting identity, and
   credential generation before contacting Nyx, and closes snapshot/dispatch
   admission immediately and across service or app restart;
2. requires the exact same request ID after an unavailable or otherwise
   ambiguous Hub response; secret-free pairing status exposes only that retry
   ID and the `pending` or `hub_committed` phase so the UI can offer
   **Retry Removal** without inventing a second intent;
3. after a proven terminal, non-ambiguous Hub rejection, atomically retires that
   request ID before reopening the unchanged prior pairing. The rejected ID can
   never be reused against that or a later credential generation; ambiguous
   transport, `429`, `5xx`, redirect, oversized, or malformed-success outcomes
   remain fenced;
4. after Nyx commits, marks the local pairing revoked, removes only the exact
   fingerprint-matching pairing-owned snapshot, dispatch, and management
   generation, and retains a non-secret revoked tombstone;
5. treats a `hub_committed` retry as local-only cleanup and never calls Nyx a
   second time; changed, mismatched, or static credentials are preserved rather
   than guessed at or deleted; and
6. permits re-enrollment only through a new Nyx invitation. Beginning that new
   ceremony atomically retires the completed removal fence, creates a new
   pairing, and preserves the Mac's reporting identity and token-attribution
   continuity.

This route does not invent a second authentication scheme. If the environment
variable named by `server.control_password_env` is non-empty, the existing
control middleware requires Basic user `admin`; if it is empty on the default
loopback-only listener, local processes in the Mac's existing trust boundary can
call the route without a password. The explicit Swift confirmation is a product
interaction safeguard, not a cryptographic authorization receipt. A
non-loopback control bind still fails configuration validation unless the
password is set.

The permanent revoke path never invokes model cleanup, migration, or uninstall.
It retains every local model and runtime, exact configured weight path and
volume/bookmark binding, inference profile and load setting, local inference
credential, download record, local analytics row, token history, and durable
usage outbox. If the user only wants to stop contributing temporarily and let
current Fleet streams drain, the participation toggle is the correct action;
joining later then requires no new invitation.

Deleting the app or service registration must not silently revoke the Hub
pairing. Retention-level uninstall preserves pairing state unless the user
independently confirms permanent Nyx removal; lifecycle integration that
coordinates that choice with an executable full-privacy uninstall remains a
separate release gate.

## Credential rotation

Routine rotation is an administrator-approved, crash-recoverable two-phase
operation. It rotates snapshot, dispatch, and management credentials as one
generation so a mixed generation never becomes the durable active state.

1. Hub admin creates a rotation transaction with a unique `request_id`.
2. Nyx generates and encrypts one candidate generation before exposing it.
3. The Mac authenticates with the current management bearer, retrieves the exact
   candidate bundle, and durably stages both old and new generations.
4. The Mac acknowledges the candidate using the candidate management bearer.
5. Nyx proves the candidate snapshot and dispatch credentials using the same
   non-loading activation probes.
6. Nyx atomically selects the candidate generation and sends activation-complete.
7. The Mac marks it active, retains the old values for a short fixed grace, and
   acknowledges.
8. Nyx and Mac retire/delete the old generation after acknowledgement or grace.

Failure before step 6 retains the old active generation. Failure after step 6
is recovered from the durable transaction; Nyx does not generate another
candidate for the same rotation request. During overlap, only Nyx's durable
active generation is used for new routes.

Routine rotation authenticated only by the current management bearer is not a
safe recovery from suspected management-bearer compromise. That case requires
Hub-admin revocation and a new pairing in the initial slice. Device-key proof
later permits stronger recovery and independent bearer rotation.

## Idempotency and crash recovery

Every mutation has one `request_id`. The receiver persists the operation kind,
secret-free canonical request digest, state, and fixed result metadata. Reusing
the ID with the same secret-free payload returns the same result; reusing it
with different fields returns `409 idempotency_conflict`. Secrets themselves
are compared through their secret-store references and never included in
metadata digests.

Recovery rules by transition:

| Failure point | Required durable outcome |
| --- | --- |
| Before invitation commit | No invitation exists |
| After invitation commit | Same unexpired invitation may be claimed; restart never extends expiry |
| During concurrent claim | Exactly one claim wins; exact retry resumes it |
| After claim, before approval | Claim remains pending or expires; never routable |
| After approval, before secret creation | Resume generation creation once |
| After encrypted generation, before metadata reference | Reconcile a bounded orphan; never expose it as active |
| After metadata reference, before Mac receipt | Redeliver the same bundle to the bound claim during the provisioning window |
| After Mac staging, before acknowledgement | Mac restart retains activation-only staged state; Hub retry is idempotent |
| During activation probes | Repeat safe probes; never dispatch inference |
| After one side commits activation | Reconcile to one generation; keep routing closed until both confirmations exist |
| During rotation | Old generation stays active until the candidate commit point; then journal recovery finishes the declared switch |
| During revoke | Revoked metadata wins over every credential, snapshot, or stale registry object |

If metadata and the encrypted secret store cannot be reconciled, the enrollment
is non-routable with a fixed failure code. Startup never reconstructs authority
from a snapshot, partial transaction, browser cache, or Mac-supplied claim.

## Legacy static enrollment coexistence and migration

The initial release must support existing static nodes while paired nodes are
introduced. Static configuration remains readable and routable without an
automatic rewrite.

### Coexistence

- Static enrollments retain their current `node_id`, locator, snapshot bearer,
  and inference bearer from environment references.
- Paired enrollments use `pairing_id`, encrypted secret references, a dedicated
  dispatch bearer, and dynamic lifecycle state.
- The scheduler receives a uniform in-memory view but status identifies each
  enrollment source.
- Pairing cannot claim a static reporting ID or locator unless the invitation
  explicitly has `intent: adopt-static` for that exact enrollment.
- A static node cannot be revoked or rotated through the dynamic API until it
  is adopted; its operator continues using the current configuration workflow.

### Adopt-static transaction

1. Hub admin creates an adoption invitation bound to the exact static node ID,
   locator, service class, and current enrollment record.
2. The Mac claims it without changing the existing inference, snapshot,
   storage, model, or usage configuration.
3. Nyx provisions the dedicated dynamic credentials; the Mac stages them in
   addition to the current values.
4. Activation probes validate the new snapshot and Fleet dispatch channels.
5. Nyx atomically swaps scheduler ownership from the static record to the
   paired record. There is never a window with two routable enrollments for the
   same Mac.
6. The previous static credentials and stanza are retained as private rollback
   evidence until the administrator commits migration and completes a restart,
   pause/join, inference, and usage-continuity check.
7. Finalization gives exact instructions for removing obsolete environment
   values; it does not silently edit an external machine-specific deployment.

Rollback before finalization reselects the exact static enrollment and removes
only the staged pairing generation. It does not replace the Mac's ordinary
inference key or reporting ID. Historical token rows remain attributable to the
same `reporting_node_id` before and after adoption.

### Mac application migration and uninstall

Same-Mac app updates and guided migration retain `pairing_id`, credential
generation, Hub origin, reporting ID, and participation preference along with
the existing config, database/WAL/outbox, scopes, and storage inventory. Moving
an installation to another physical Mac is not an implicit identity transfer;
the initial slice requires a new pairing and revocation of the predecessor.

Uninstall follows the product retention levels. Keeping Application Support
retains pairing; deleting Mnemosyne state requires an explicit choice to
permanently revoke the pairing or preserve a recovery export. No uninstall
level deletes user-selected model folders, imported weights, external volumes,
or bookmarks merely because pairing is removed.

## Initial implementable vertical slice

The first pairing release is intentionally bounded to the current bearer-based
architecture:

1. Version-1 invitation, claim, admin approval, provisioning, activation, and
   status state machines with fixed payloads and idempotency.
2. Verified HTTPS Mac-to-Hub pairing/management and HTTPS or explicitly
   verified Tailscale Hub-to-Mac transport.
3. Exact admin-authorized locator binding and the SSRF controls above.
4. Opaque stable `pairing_id` separate from preserved `reporting_node_id`.
5. Three independent per-device bearer credentials: snapshot, dedicated
   Fleet dispatch, and management.
6. A private authenticated-encryption store outside `fleet.db`, with opaque
   metadata references and no browser exposure.
7. Non-loading snapshot and dispatch activation probes.
8. Dynamic active/disabled/revoked registry semantics while static enrollments
   continue to work.
9. Existing local pause/join behavior, explicit Hub disable/admin revoke,
   Mac-initiated permanent revoke, routine credential rotation, crash recovery,
   and static adoption.
10. Full native inference, storage, download, accounting, packaging, migration,
    and signed multi-host non-regression acceptance.

This slice does not provide cryptographic device possession beyond the
high-entropy invitation and provisioned management bearer. That limitation is
acceptable only with verified TLS, admin approval, short invitation lifetime,
strict rate limits, private node transport, and the stated compromise-recovery
rule of revoke and re-pair.

### Current implementation boundary

As of 2026-08-30, items 1-8 have a Hub-side foundation: strict bounded API
models and opt-in routes, durable idempotent invitation/claim/enrollment
metadata, authenticated-encryption storage for locator and credential
material, CIDR/port locator policy, DNS-result and TLS peer pinning, non-loading
snapshot/path-free-model probes, separate `pairing_id` and
`reporting_node_id`, Hub enable/disable/revoke, and dynamic registry publication
without granting persisted snapshots fresh authority. Static enrollments still
route through their original environment-backed configuration.

The Mac service has the complementary durable local journal, atomic private
credential-file ownership, staged-versus-active snapshot/dispatch checks,
secret-free status, reduced Fleet-marked `/v1/models` activation output, and
the independent join/pause lease. Pairing-state failure closes only Fleet
authority; it does not make the local inference or control plane depend on Nyx.
Mac-initiated revoke persists an exact, secret-free request fence before its
Hub call, denies Fleet admission immediately and across restart, and permits
only that request to resolve an ambiguous outcome. After Hub commitment it
retains a non-secret revoked tombstone and idempotently removes only the exact
fingerprint-matching pairing-owned credential generation. A cleanup retry is
local-only; mismatched or static credentials are never removed.

Items 9-10 are not complete as product workflows. Pause/join, Hub disable/admin
revoke, and Mac-initiated permanent revoke now have automated foundations;
routine rotation, remote administrator-to-Mac notification, adopt-static
cutover/rollback, lifecycle integration, and the full signed-artifact crash
matrix remain target behavior. The Swift ceremony now moves an invitation
through claim, approval resume, provisioning, Mac staging, activation, and
completion, and the same page can permanently remove and safely re-pair this
Mac, but neither flow has passed the signed-artifact crash matrix. No release
claim is valid until signed-app and representative
multi-host evidence also proves every existing engine/route, JIT and batching
behavior, token path, exact nested/external storage path, volume identity,
bookmark, and model ownership/provenance survives the lifecycle unchanged.

## Deferred security hardening

The following are deliberate later protocol versions, not requirements that
the initial bearer slice should pretend to satisfy:

- A non-exportable per-installation Ed25519 or platform-backed device key. The
  claim signs a canonical transcript binding Hub origin, protocol version,
  invitation, nonces, locator, reporting identity, and device public key.
- A Hub signing identity pinned during pairing and signed pairing receipts.
- A short authentication string shown independently by Mac and Hub and compared
  by the administrator before approval, closing an invitation race or endpoint
  substitution more strongly than labels alone.
- Challenge-response management authentication, replacing the long-lived
  management bearer and enabling safer compromise recovery.
- Short-lived, audience- and route-scoped snapshot/dispatch tokens or mTLS,
  automatic renewal, and bounded offline revocation exposure.
- Secure Enclave attestation where available, without making one hardware
  feature a false portability requirement.
- Automatic scheduled credential rotation, separate role rotation, recovery
  codes, and deliberate same-owner device transfer.
- KMS/HSM-backed Hub master keys, tamper-evident external audit logs, multi-admin
  approval, and formal key-compromise recovery.
- A device-authenticated outbound management channel for later remote install
  jobs. Nyx still never receives the Mac control credential or direct arbitrary
  filesystem authority.
- Tailscale node-identity binding as defense in depth. Tailnet identity remains
  separate from the durable Mnemosyne pairing identity.

Introducing these features requires a new negotiated protocol version or an
explicit backward-compatible capability. A Hub must not silently downgrade a
device-key pairing to bearer-only after it has required the stronger mode.

## Acceptance matrix

All reports are secret/content redacted and identify exact app, Hub, protocol,
catalog, macOS, and signed-artifact versions.

| Area | Automated evidence | Signed multi-host / operator evidence |
| --- | --- | --- |
| Invitation lifecycle | Expiry, attempt cap, global cap, constant-time comparison, exactly one concurrent claimant, exact retry, mismatched retry conflict, reject and replay failure | Create, claim, approve, reject, and expire invitations from the signed UI without a terminal or manual secret file edit |
| Versioning | Unknown fields, unsupported versions, non-canonical IDs, oversized bodies, non-finite values, and downgrade attempts fail closed | Old static gateway remains usable while an unsupported paired client is visible only as non-routable diagnostic state |
| Authorization separation | Full negative matrix proves client, admin, snapshot, dispatch, management, local inference, control, and ledger credentials cannot cross roles or nodes | Captured redacted access evidence shows Nyx has no Mac admin credential and Mac receives no Hub admin/client/ledger secret |
| Locator/SSRF | Reject userinfo, path/query/fragment, redirects, proxies, loopback, link-local, metadata, multicast, mixed DNS answers, rebinding, IPv4-mapped and IPv6 bypasses; accept only configured HTTPS/Tailscale cases | Verify the exact approved path, certificate/ACL/firewall policy, and that changing DNS or locator removes eligibility rather than contacting a new service |
| Secret boundary | Schema scan of `fleet.db` and WAL; API/log/crash/export scans; encrypted-store wrong-key, tag-tamper, role/reference swap, permissions, atomic-write, backup/restore, and master-key rotation tests | Inspect deployed permissions and redacted backups; prove no URL or credential appears in dashboard, route history, process arguments, or retained evidence |
| Stable identity | Pairing ID survives Hub/Mac/service restarts and hostname change; reporting ID and existing token attribution remain stable; copied partial state fails closed | Pair two Macs, restart/login-cycle all hosts, rename one Mac, and observe the same two pairings and correct per-device token rows |
| Activation | Failure injection before/after every claim, approval, secret, staging, ack, probe, and commit transition leaves no partially routable node; probes cause no load/download/residency change | Pair an idle Mac and prove model residency, installed files, downloads, and local API behavior are unchanged until an ordinary inference request JIT-loads a model |
| Dynamic registry | Add/disable/replace/revoke serialized with scheduler; no stale snapshot authority after restart; no duplicate pairing/reporting/locator; active route keeps immutable owner | Enable two paired Macs, restart Nyx, require fresh snapshots, and route only after both independently regain eligibility |
| Participation | Pause closes Fleet-only admission, drains complete streams, preserves local inference/downloads/residency/outbox, and joins without re-pairing | Pause during a long stream, route new work elsewhere, run local inference on the paused Mac, restart paused, then join and JIT-load without redownload |
| Disable/revoke/self-removal | Hub disable is reversible and drains; revoke wins over secrets/snapshots/registry and never retries ambiguous inference; Mac removal fences authority before its Hub call, replays only the exact request ID, retains a tombstone, and removes only its exact pairing credentials | Disable and re-enable one Mac; permanently remove another from the signed Mac UI, restart during pending and post-Hub-commit phases, finish with the same request ID, prove the old credentials fail, then re-pair with a new invitation |
| Rotation | Failure at every generation transition retains one declared active generation; candidate probes are non-loading; old credentials fail after grace; duplicate request never creates another candidate | Rotate all three credentials during service/Hub restarts, retain in-flight request semantics, and prove new snapshot/dispatch plus local inference afterward |
| Static coexistence/adoption | Static and paired sources share scheduling without duplicate authority; adoption switch and rollback are atomic; ordinary inference key is never overwritten | Adopt a current static Mac, verify route/token continuity, exercise rollback, then finalize removal of only obsolete Fleet environment values |
| Storage/inference non-regression | Existing engine, route, coordinator, storage, bookmark, installer, runtime, usage, packaging, migration, and Swift suites remain mandatory | Validate internal storage, a nested external volume, and a protected-folder grant; all models, exact weight paths, profiles, routes, token history, and settings survive pairing, rotation, update, pause, permanent revoke/re-pair, migration, and retained-data uninstall |

No release may claim production pairing from unit mocks alone. At minimum, the
signed multi-host run pairs two representative Apple Silicon Macs with Nyx,
restarts every role, pauses and rejoins one Mac, rotates credentials, revokes
the other while offline, validates exact per-device token accounting, and
proves that no pairing operation changed or deleted weights, exact configured
storage paths, inference profiles, token history, JIT residency behavior, or
ordinary local inference.
