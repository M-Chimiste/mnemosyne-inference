# Mnemosyne Fleet Acceptance Evidence

Copy this file for each target-host acceptance run. Keep the completed copy
outside the public repository if node identities or operational timestamps are
sensitive. Never record URLs, credentials, DSNs, prompts, outputs, model
storage paths, or arbitrary engine diagnostics.

## Candidate

- Date (UTC):
- Operator:
- Repository commit:
- Fleet protocol version:
- Nyx Fleet version:
- macOS Mnemosyne version:
- CUDA Mnemosyne version:
- Public model:
- Strict deployment ID:
- Node IDs:

## Preconditions

- [ ] Nyx reaches every enrolled inference URL over the intended private LAN
      or Tailscale path.
- [ ] Snapshot, node-inference, Fleet-client, Fleet-admin, and ledger
      credentials are distinct.
- [ ] The ledger role has `SELECT` only on `public.token_usage`.
- [ ] The configured public model has at least two online, authoritative,
      exact-capability replicas.
- [ ] Fleet has zero active routes and empty queues.
- [ ] Eligible nodes have zero active/queued work and healthy, empty usage
      outboxes.
- [ ] Unrelated traffic for the acceptance model is paused.

## Representative fan-out and usage

Acceptance command (redact environment values):

```text
uv run --directory fleet --frozen python ../scripts/fleet_acceptance.py ...
```

Paste the script's content-redacted result JSON:

```json
{}
```

- [ ] Every request completed successfully.
- [ ] The result lists both `macos` and `cuda` in `eligible_platforms` and
      `routed_platforms`.
- [ ] At least one request completed on the macOS node.
- [ ] At least one request completed on the CUDA node.
- [ ] Nyx route metadata agrees with the serving nodes.
- [ ] Exactly one central token event appeared for each successful language
      request on its serving node.
- [ ] Fleet SQLite contains only the documented metadata columns.

## Offline and rejoin

- Node stopped:
- Snapshot TTL:
- Dashboard observed offline:
- Replica request completed while node was offline:
- Node restarted:
- New instance ID:
- Rejoined without replay error:

- [ ] The expired snapshot stopped being routing authority.
- [ ] The remaining replica continued serving.
- [ ] The restarted node used a new instance ID and increasing snapshot
      sequence.
- [ ] A second fan-out probe reached both nodes.

## Concurrency and drain

- Node:
- Adapter:
- Derived limit and source:
- Configured `max_concurrency`:
- Effective limit:
- Maximum observed active:
- Maximum observed node queue:
- Maximum observed Fleet queue:

- [ ] Active work never exceeded the effective ceiling.
- [ ] Excess work used only bounded queues.
- [ ] Queue saturation was visible in the dashboard.
- [ ] A model switch waited for the complete active stream.
- [ ] Residency epoch advanced only after drain, unload, load, and readiness.
- [ ] A full node queue returned `429 node_busy`, `Retry-After`, and the
      manager-owned proof header before inference work began.

## Failure semantics

- [ ] Wrong and missing snapshot keys were rejected independently of the node
      inference key.
- [ ] A replayed or malformed snapshot did not refresh liveness.
- [ ] A body-only or unproven `429` was terminal.
- [ ] A post-header disconnect did not create a second inference request.
- [ ] A degraded or non-authoritative node advertised no immediately
      available capacity and was excluded from routing.

## Dashboard and security

- [ ] Dashboard node health, residency, capacity, queues, routes, and usage
      agreed with node-local observations.
- [ ] Browser-facing responses contained no node URLs, credentials, DSNs,
      paths, prompts, or outputs.
- [ ] Deployment dependency and container/image scans were reviewed.
- [ ] Tailscale ACLs, firewall rules, or trusted-LAN isolation were verified
      from Nyx to each node.

## Result

- Overall: `PASS` / `FAIL` / `INCONCLUSIVE`
- Failed or inconclusive checks:
- Conservative capacity fallbacks needing hardware tuning:
- Follow-up owner:
- Follow-up date:
