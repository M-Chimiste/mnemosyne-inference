# Mnemosyne Fleet on Nyx

This directory is a standalone routing and observability service for Nyx. It
provides one OpenAI-compatible endpoint while leaving model loading, process
ownership, concurrency enforcement, and token accounting on each Mnemosyne
node.

## Safety boundary

Fleet enrolls nodes explicitly; it does not discover or trust anonymous LAN
listeners. Each enrollment has two environment-backed secrets:

- `fleet_token_env` reads the node's dedicated `FLEET_API_KEY` and is used
  only for `GET /fleet/v1/snapshot`;
- `inference_token_env` reads the node's `INFERENCE_API_KEY` and is used only
  for proxied inference.

Fleet refuses to start unless the public-client key, admin key, and every
node snapshot/inference credential are all distinct. Secret values never
enter SQLite, API responses, or route history. Node and ledger secrets never
enter the browser; the operator-supplied dashboard key is kept only in that
tab's session storage. Node URLs are omitted from dashboard APIs. Production
node HTTP clients ignore ambient proxy environment variables and never follow
redirects. They do not impose a hidden transport-wide active-connection
ceiling: explicit scheduler reservations and node-advertised capacity remain
authoritative. Idle connection reuse is still bounded to 20 connections per
client.

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

The scheduler prefers, in order:

1. a warm deployment with a free permit;
2. a warm deployment with room in its bounded node queue;
3. an empty node that can load the deployment;
4. a node that can safely drain and switch;
5. the model's bounded FIFO queue.

Within a tier it chooses weighted least-outstanding capacity. A reservation
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
credentials for every node, and optionally a DSN belonging to a Postgres role
with `SELECT` access only to `public.token_usage`. Replace the example
deployment ID with the authoritative ID shown by a node snapshot. Do not put
secret values in `config.toml`.

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
24-hour or 7-day token aggregates from the read-only ledger. Promotion into a
public Fleet model remains an explicit TOML mapping.

The complete protocol and threat boundary are documented in
[Fleet architecture](../project_docs/fleet_architecture.md) and
[Fleet security](../project_docs/fleet_security.md). After isolated tests,
use the content-redacted [multi-node acceptance procedure](../project_docs/fleet_acceptance.md)
before treating a Mac/CUDA rollout as complete.

## Verification

```bash
uv run --directory fleet --frozen --extra dev python -m pytest -q
uv run --directory fleet --frozen python -m compileall -q src
```
