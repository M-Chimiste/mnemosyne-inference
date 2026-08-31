# Mnemosyne Fleet Acceptance

Run these checks after Nyx and at least two strict replicas are configured.
They complement the automated suites; they do not replace each node's existing
engine, download, release, or hardware smoke checks.

For the bundled macOS Hub pilot, also require:

- Hub-only promotion succeeds with no local models, does not enable, restart,
  credential, or enroll the native worker, and starts with zero static model
  mappings;
- optional local-worker promotion still refuses a Mac with no authoritative
  Fleet-eligible model, and the resulting Hub and worker have separate PIDs,
  listeners, state roots, and credentials with the worker fixed to `overflow`;
- every fresh authenticated node snapshot publishes all authoritative
  Fleet-eligible models into the universal catalog; exact replicas share one
  route and conflicting aliases receive stable deployment suffixes;
- removing one managed route suppresses that exact candidate across later
  polls and Hub restart, while explicit re-add restores routing without
  changing model files or node configuration;
- the Hub binds only `127.0.0.1:17400`, and its configured HTTPS origin reaches
  it through the intended private proxy;
- invitation create, Mac claim, exact-locator approval, activation, explicit
  Hub enable, local pause/rejoin, Hub disable/re-enable, and permanent revoke
  pass on the signed candidate across representative Macs;
- Finder replacement refreshes an enabled Hub login registration but leaves an
  explicitly disabled one disabled; both paths retain the exact Hub identity,
  pairing generations, inventory, catalog mappings/suppressions, and route
  history;
- the preserve-data uninstaller refuses while the Hub job is active and retains
  the Hub directory after the exact app and managed runtimes move to Trash.

## Preconditions

- Nyx can reach each enrolled inference-plane URL over the trusted LAN or
  Tailscale.
- Every node has a distinct `FLEET_API_KEY` and `INFERENCE_API_KEY`; Nyx has
  only those two node-scoped credentials, never an admin credential.
- The public model maps to one authoritative deployment ID advertised by at
  least two nodes with the exact configured capability set.
- No second public synonym maps to the same node aliases during the token
  proof; that historical ledger attribution is intentionally reported as
  ambiguous.
- For the fan-out probe, make that deployment warm on at least two nodes using
  each node's ordinary documented load workflow.
- The Fleet ledger DSN belongs to a role with `SELECT` only on
  `public.token_usage`.
- The model is suitable for a small non-sensitive acceptance prompt.
- Nyx is running one Fleet gateway process, and unrelated traffic for the
  acceptance model is paused. Before recording its baseline, the runner
  requires zero Fleet routes and queues, zero active/queued work on every
  currently eligible node, and—unless `--skip-usage` is selected—a healthy,
  empty usage-delivery outbox on each of those nodes. This makes the exact
  token-event increment meaningful.

## Automated representative probe

From the repository, export only the Nyx client and dashboard credentials:

```bash
export MNEMOSYNE_FLEET_CLIENT_KEY='...'
export MNEMOSYNE_FLEET_ADMIN_KEY='...'
uv run --directory fleet --frozen \
  python ../scripts/fleet_acceptance.py \
  --url http://nyx:17400 \
  --model qwen-coder \
  --require-node-service-class mac-primary=primary \
  --require-node-service-class nyx-worker=overflow \
  --require-platform macos
```

The script:

1. verifies the model is discoverable, has at least two live strict replicas,
   has an eligible replica on each required platform, and proves each named
   required node still has its expected `primary`, `opportunistic`, or
   `overflow` service class;
2. launches simultaneous requests through the single Fleet endpoint;
3. consumes each complete response without printing its body;
4. proves metadata-only route rows completed on every required node and
   platform, as well as on at least the configured minimum node count (a node
   named by `--require-node-service-class` is implicitly required to route);
5. waits for exactly one serving-node token event per completed language
   request to appear through Nyx's read-only aggregate view.

It does not query node control planes, print inference content, or write token
rows. Use `--request-file` for an engine-specific private payload. Images do
not produce token events by design, so image acceptance requires
`--skip-usage`. Only successful route rows are paired with token events;
metadata for a safe pre-work `node_busy` attempt remains visible but is not
mistaken for inference work. An unexpected extra successful route or token
event makes the acceptance window inconclusive instead of being treated as
success.

## Offline and rejoin

1. Stop the CUDA service using its normal lifecycle command. Do not kill a
   listener by port.
2. After `snapshot_ttl_seconds` has elapsed, allow one dashboard update
   interval (two seconds), then confirm the dashboard marks it offline and the
   model remains callable on the Mac replica.
3. Restart CUDA, wait for a new `instance_id`, and confirm its snapshot
   sequence restarts without a replay error.
4. Rerun the representative probe and require both node IDs.

An offline snapshot must never remain routing authority. Rejoining does not
require changing the public model mapping when the strict deployment ID is
unchanged.

## Concurrency and drain

For one representative language model:

1. Set a deliberately small `max_concurrency` on one node and restart or
   reload it through the documented node workflow.
2. Send more simultaneous requests than the effective limit.
3. Confirm active requests never exceed the ceiling, excess requests enter
   only the bounded node/Fleet queues, and saturation is visible in realtime.
4. While a streaming request is active, request a different model.
5. Confirm the old engine remains resident until the stream closes, then the
   node drains, unloads, loads, verifies, and advances its epoch.
6. Fill the local queue and confirm the next request receives the stable
   pre-work `429 node_busy` response with `Retry-After` and
   `X-Mnemosyne-Error: node_busy`.

Do not use prompts or outputs as acceptance evidence. The dashboard's fixed
route metadata, node snapshot fields, and node-local process observations are
the evidence surfaces.

## Failure semantics

- A wrong or missing Fleet snapshot key is rejected independently of the
  inference key.
- A replayed or malformed snapshot does not refresh liveness.
- Only a `429` carrying `X-Mnemosyne-Error: node_busy` may select a second
  node; the same JSON error without that header is returned terminally.
- A generic upstream failure after headers, a broken response body, or a
  disconnected stream produces no second inference request.
- A degraded or non-authoritative node advertises no immediately available
  capacity and is excluded from routing.
- Fleet SQLite contains no prompt, output, credential, DSN, or token-usage
  columns.

## Routing controls and batches

Using a non-sensitive payload and two eligible Macs:

1. Send an ordinary request without control headers and confirm the existing
   warm/service-class/load behavior is unchanged.
2. Send a request with an exact enrollment affinity and confirm it reaches
   that Mac when eligible. Repeat while it is unavailable with fallback
   allowed, then with fallback disabled; only the former may use another Mac.
3. Saturate one mapped model, queue `batch`, `normal`, and `interactive` work,
   and confirm the visible lane counts and admission order. Repeat with a
   shorter `X-Mnemosyne-Max-Wait-Ms` and confirm it cannot extend the TOML
   model deadline.
4. Submit a multi-item `/v1/batches` job, observe progress, retrieve ordered
   results, and confirm each completed item produced one ordinary route row
   and one serving-node token event.
5. Cancel a job with one item active and another pending. Confirm both settle
   cancelled, active Fleet reservations return to zero, and no second node
   receives ambiguous work.
6. Search the Fleet SQLite database for unique sentinel strings used only in
   the batch input/output and confirm neither appears. Restart Fleet and
   confirm the old batch ID is unavailable.
7. Open `/fleet/` and compare its first overview table with
   `/fleet/api/overview`, authenticated snapshots, Mac inventory, and the
   read-only five-minute ledger rate. No node URL, path, credential, prompt, or
   response may appear.

The automated gateway and node suites exercise these destructive/fault cases
with isolated fakes. The live check should use only ordinary service stop,
restart, concurrency, and model-switch workflows.

## Evidence to retain

Retain a redacted acceptance note containing:

- repository commit and Fleet protocol version;
- Nyx, Mac, and CUDA service versions;
- node IDs and deployment ID, but no URLs or credentials;
- effective concurrency limits and derivation sources;
- probe result JSON;
- offline/rejoin timestamps and new instance ID;
- confirmation that the token increment appeared per serving node; and
- any conservative capacity fallback that still needs hardware tuning.

Use
[fleet_acceptance_evidence.example.md](fleet_acceptance_evidence.example.md)
as the checklist and redacted evidence shape. A completed note is evidence
only when it names the actual Nyx, macOS, and CUDA candidate versions and was
produced from those target hosts; the simulated loopback suite is a rehearsal,
not a substitute.
