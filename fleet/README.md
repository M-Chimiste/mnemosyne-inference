# Mnemosyne Fleet on Nyx

This directory is a standalone routing and observability service for Nyx. It
provides one OpenAI-compatible endpoint while leaving model loading, process
ownership, concurrency enforcement, and token accounting on each Mnemosyne
node.

## Safety boundary

Fleet enrolls nodes explicitly; it does not discover or trust anonymous LAN
listeners. Existing static enrollments keep two environment-backed secrets:

- `fleet_token_env` reads the node's dedicated `FLEET_API_KEY` and is used
  only for `GET /fleet/v1/snapshot`;
- `inference_token_env` reads the node's current inference bearer and is used
  only for proxied inference. For a legacy/static Mac this remains its existing
  `INFERENCE_API_KEY` contract.

The opt-in pairing foundation instead creates three independent per-Mac
credentials for snapshot, Fleet-only dispatch, and pairing management. Nyx
stores those values only in its separate encrypted pairing store. The Mac's
dispatch bearer occupies `FLEET_INFERENCE_API_KEY` and is accepted for normal
inference only with Nyx's canonical Fleet route marker; pairing never replaces
the ordinary local `INFERENCE_API_KEY`. The management bearer is not a Mac
control-plane or Fleet-admin credential.

Fleet refuses to start unless the public-client key, admin key, pairing master
key when enabled, and every known node credential are distinct. Secret values
never enter Fleet metadata SQLite, API/status responses, or route history. The
one-time invitation response and claim-bound provisioning response are the
deliberate exceptions needed to deliver their respective secret material; both
are `no-store` and never enter an admin/dashboard listing. Node and ledger
secrets never enter the browser; the operator-supplied dashboard key is kept
only in that tab's session storage. Node URLs are omitted from dashboard APIs.
Production node HTTP clients ignore ambient proxy environment variables and
never follow redirects. Paired-node activation and dispatch additionally pin
the approved DNS result and prove the connected peer. Clients do not impose a
hidden transport-wide active-connection ceiling: explicit scheduler
reservations and node-advertised capacity remain authoritative. Idle
connection reuse is still bounded to 20 connections per client.

Pairing is disabled by default and its routes are absent while disabled, so an
existing static Fleet deployment keeps its current behavior. When enabled,
the implemented Hub foundation covers bounded version-1 invitation, claim,
approval/rejection, claim-bound provisioning, non-loading activation probes,
Hub enable/disable, revocation, encrypted secret storage, restart
reconciliation, and dynamic scheduler membership. A new production pairing
must complete activation while Hub-disabled and requires a separate admin
enable before it can route. The native Swift UI drives begin/resume with a
memory-only invitation secret and exposes secret-free status plus local
join/pause. Signed-artifact evidence, rotation/forget/recovery, and
representative multi-host gates in the pairing contract still block a
production claim.

An inference worker colocated on Nyx remains a separate enrolled node. Run it
under an independently isolated service identity with its own listener, state,
model-storage roots, and snapshot/inference credentials; never place an engine
inside the Fleet gateway process. Configure that limited worker with
`service_class = "overflow"`. This Hub policy does not create process or
resource isolation by itself, so the operator must establish those boundaries
before enrolling the worker.

A logical model maps to one exact `sha256:` deployment ID and an exact
capability set. A node is eligible only when its authenticated, fresh protocol
v1 snapshot reports the same ID, an authoritative identity, an immutable
revision or content digest, and `fleet_eligible=true`. Symbolic revisions and
unverified local artifacts remain visible at the node but cannot enter an
automatic replica group.

Nyx liveness uses the monotonic time at which a new snapshot sequence was
received. Replayed snapshots do not extend a node's TTL, and node wall-clock
skew does not decide eligibility. Snapshot responses must be identity-encoded
JSON and are capped at 8 MiB before schema validation. Replay history is also
bounded: after 1,024 accepted process-instance transitions for one enrollment,
that enrollment fails closed until Fleet restarts and obtains a fresh
snapshot.

## Routing behavior

Each enrollment has a Hub-owned `service_class`: `primary`, `opportunistic`,
or `overflow` (the omitted default is `primary`). The scheduler first chooses
the highest class with any currently admissible candidate. Only then does it
prefer, in order:

1. a warm deployment with a free permit;
2. a warm deployment with room in its bounded node queue;
3. an empty node that can load the deployment;
4. a node that can safely drain and switch;
5. the model's bounded FIFO queue.

Thus a cold/loadable primary node precedes a warm overflow node; a lower class
becomes available only after every higher-class node is ineligible, stale, or
has neither a free permit nor bounded queue room. Within a class and residency
tier Fleet chooses weighted least-outstanding capacity. A reservation
that has not received upstream headers is counted regardless of when the
latest poll began. Once non-busy headers prove node admission, only a poll
started after that admission may account for the request in node-local state.
The node remains the final admission authority.

Run one Fleet process on Nyx. Protocol-v1 FIFO and reservation state is
in-memory by design; multiple Uvicorn workers are unsafe until that state is
moved to a shared transactional scheduler.

Only the listed POST routes are exposed: Chat Completions, Completions,
Responses, Messages, Embeddings, Rerank, and Images Generations. Fleet parses
and reserializes the JSON object but changes only its `model` field. It removes
client credentials, cookies, forwarding headers, and reserved proof/route
headers, then injects the selected node authorization and Fleet route ID. A
proven `node_busy` response means `429` plus the manager-owned
`X-Mnemosyne-Error: node_busy` header; a body field alone is never retryable.
That proof, `ConnectError`, `ConnectTimeout`, or a connection-pool timeout
before headers may select another node. Other transport failures are
ambiguous and terminal. After non-busy headers arrive, the response is
streamed to completion and is never failed over. If every attempted node
returns the pre-work proof, Fleet returns bounded `429 fleet_capacity_busy`
with `Retry-After`.

Fleet SQLite stores only fixed route metadata. It never stores request bodies,
prompts, generated output, node credentials, or ledger credentials. Token
usage continues to be emitted exactly once by the serving node. The optional
ledger integration uses a read-only Postgres credential to show aggregates.

## Nyx install

Python 3.11 or newer and `uv` are required:

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin \
  mnemosyne-fleet
sudo install -d -o root -g root -m 0755 /opt/mnemosyne-fleet
sudo cp -R fleet/src /opt/mnemosyne-fleet/src
sudo install -o root -g root -m 0644 fleet/pyproject.toml fleet/uv.lock \
  /opt/mnemosyne-fleet/
sudo env UV_CACHE_DIR=/var/cache/mnemosyne-fleet-uv \
  uv sync --directory /opt/mnemosyne-fleet --frozen --no-dev
sudo install -d -o mnemosyne-fleet -g mnemosyne-fleet -m 0700 \
  /var/lib/mnemosyne-fleet
sudo install -d -o root -g mnemosyne-fleet -m 0750 /etc/mnemosyne-fleet
sudo install -o root -g mnemosyne-fleet -m 0640 fleet/config.example.toml \
  /etc/mnemosyne-fleet/config.toml
sudo install -o root -g root -m 0644 fleet/mnemosyne-fleet.service.example \
  /etc/systemd/system/mnemosyne-fleet.service
```

Create `/etc/mnemosyne-fleet/secrets.env` owned by
`root:mnemosyne-fleet` as mode `0640`. Set the client and admin keys, both
credentials for every static node, and optionally a DSN belonging to a
Postgres role with `SELECT` access only to `public.token_usage`. If pairing is
enabled, also set a distinct canonical 32-byte base64url master key through the
environment variable named by `pairing.master_key_env`; never put that key or
node credentials in `config.toml`. The pairing metadata and encrypted-secret
databases belong in the private service-owned directory shown in the example.
Replace the example deployment ID with the authoritative ID shown by a node
snapshot.

The Apple Silicon compatibility catalog is a separate, optional management
surface. It remains disabled unless `[catalog].enabled = true` and requires a
dedicated private state directory, one canonical HTTPS update origin/path, and
one or more locally pinned production Ed25519 public keys. The repository's
golden test key is not a production trust anchor. Catalog checks send no
credentials, ignore ambient proxies, reject redirects, and never rewrite Fleet
model mappings or scheduler state. When enabled, administrators can use:

- `GET /fleet/api/v1/catalog/status`;
- paginated `GET /fleet/api/v1/catalog/models` and `/recipes`;
- `POST /fleet/api/v1/catalog/check` for a bounded manual refresh.

All are admin-authenticated and `no-store`. Remote-install placement has its
own `placement.remote_installs_enabled = false` default. When explicitly
enabled with pairing and the catalog, the closed
`POST /fleet/api/v1/placement/recommendations` endpoint returns path-free,
short-lived advice for every inventory-backed Mac/storage binding. It never
chooses a target. An administrator may submit the exact user intent plus one
unchanged eligible candidate basis to `POST /fleet/api/v1/desired-installs`.
Nyx re-resolves the active signed recipe, recomputes placement, and journals a
path-free `DesiredInstall` only for that explicit Mac/storage identity. The
bounded admin list, exact-ID read, and cancellation endpoints share the
`/fleet/api/v1/desired-installs` prefix. Jobs are returned only through that
Mac's authenticated outbound inventory sync, are redelivered until an exact
revision acknowledgement, and remain fenced by pairing generation, service
instance, catalog digest, inventory sequence, and storage-binding generation.
The separate private SQLite journal defaults to
`private/desired-installs.db`; TTL and active/history bounds are configurable
under `[placement]`. Cancellation is a revisioned stop intent, never cleanup or
deletion. The cancellation endpoint requires a strong `If-Match: "<revision>"`
precondition and returns a conflict if that observed revision is no longer
current, so a stale dashboard cannot race a newer acknowledgement or stop
intent. This Hub slice does not execute jobs, download weights, install
runtimes, choose local filesystem paths, create live model claims, or change
the existing inference path. The selected Mac has a separate executor that
revalidates this path-free intent and maps the opaque storage binding through
its own local authority before invoking its existing durable downloader.

The example unit assumes the environment was synced at
`/opt/mnemosyne-fleet/.venv`. After reviewing private bind/Tailscale or reverse
proxy settings:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mnemosyne-fleet
curl http://127.0.0.1:17400/health
```

Fleet does not hot-reload TOML or secret environment changes. Restart the
service after either changes; the new process routes only after it receives
fresh authenticated node snapshots.

Client inference uses `Authorization: Bearer <MNEMOSYNE_FLEET_CLIENT_KEY>`.
The responsive dashboard is at `/fleet/`; it keeps the admin key in browser
session storage, consumes Nyx's authenticated realtime event stream, shows the
sanitized model inventory advertised by every enrolled node, explains strict
replica eligibility without exposing node URLs or artifact paths, and queries
24-hour or 7-day token aggregates from the read-only ledger. When the optional
Mac-pool switches are enabled, the same page shows each paired Mac's hardware,
participation/service class, exact opaque storage bindings, installed/cold/
resident models, signed catalog models and recipes, explainable placement
candidates, and DesiredInstall delivery/acknowledgement progress. Creating a
job requires an explicit Mac/storage selection plus browser confirmation;
cancellation is revision-preconditioned and stop-only. With those switches
off, these controls report disabled and static enrollment behavior is
unchanged. Promotion into a public Fleet model remains an explicit TOML
mapping.

The complete protocol and threat boundary are documented in
[Fleet architecture](../project_docs/fleet_architecture.md) and
[Fleet security](../project_docs/fleet_security.md). The implemented and
deferred pairing boundaries are tracked separately in the
[pairing protocol](../project_docs/fleet_pairing_protocol.md), and the broader
catalog, placement, migration, and storage non-regression gates are in the
[Mac pool acceptance contract](../project_docs/mac_pool_acceptance.md). After isolated tests,
use the content-redacted [multi-node acceptance procedure](../project_docs/fleet_acceptance.md)
before treating a Mac/CUDA rollout as complete.

## Verification

```bash
uv run --directory fleet --frozen --extra dev python -m pytest -q
uv run --directory fleet --frozen python -m compileall -q src
```
