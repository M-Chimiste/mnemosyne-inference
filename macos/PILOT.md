# Unified Inference 0.9 Mac pilot

This pilot is for **non-critical Apple Silicon Macs running macOS 15 or
newer**, including Macs with an earlier Unified Inference pilot installed. It
validates fresh install and in-place upgrade, exact model-storage selection,
cold/JIT model loading, per-serving-Mac usage accounting, in-app Hub promotion
and invitation management, Hub join/pause/rejoin behavior, and a recoverable
uninstall that retains private accounting and Hub state.

## Candidate artifact

- Product: Unified Inference 0.9.0, build 73, arm64
- DMG: `Unified-Inference-0.9.0-macos-arm64.dmg`
- SHA-256: `d5bdc87b06629d87c7a05d53e010f4cb0efac8401edc65bf26e968f6a4bc3fc3`
- Signing: ad-hoc development signature
- Artifact acceptance: passed

The DMG is structurally verified but is not Developer ID signed, notarized, or
stapled. On each pilot Mac, use Finder's **Control-click → Open** once if
Gatekeeper asks. Never disable Gatekeeper system-wide.

## Deliberate pilot limits

- Upgrade an earlier Unified Inference pilot with the ordinary Finder flow:
  quit the menu app, drag the new **Unified Inference** onto **Applications**,
  choose **Replace**, then open it. Private Application Support data and model
  locations are outside the app bundle and remain untouched. On first launch,
  the changed bundle refreshes any previously enabled exact background-service
  and menu-login registrations; an explicitly disabled registration stays
  disabled. A Mac that still runs the legacy token sidecar must complete the
  documented identity/DSN inheritance and retirement path first.
- Do not use **Migration & Removal** to change the host. Its preview and
  journal are testable, but the authenticated OS-effects runner is deliberately
  unavailable in this candidate.
- Do not treat this artifact as a public release or rely on Sparkle updates.
  Developer ID signing, the lifecycle-helper provisioning profile,
  notarization, stapling, and Gatekeeper distribution acceptance are still
  required.
- This pilot is Mac-only. CUDA nodes are outside this run.
- The active Mac engine set is llama.cpp, oMLX, DS4, and MFLUX. An upgrade
  accepts old mlxcel or mistral.rs configuration but keeps it disabled and
  omits it on the next save. Existing profile metadata, external binaries, and
  model weights are retained; no retired engine is launched or advertised.
- Frontier model names do not imply a supported recipe. DeepSeek V4/Flash and
  GLM Flash candidates may be exposed only after their exact weights, engine
  revision, context/capacity contract, and inference result are verified on
  the target hardware. The production signed catalog and trust anchors are
  not part of this artifact.
- Hub Mode is a pilot path. It bundles the Fleet gateway as a separate
  login service, creates private Hub credentials, publishes only authoritative
  local deployments, and enrolls the Hub Mac's worker as `overflow`. The
  recommended exposure is Tailscale Serve; pairing still requires
  representative signed multi-host acceptance before a production claim.

These limits do not weaken ordinary inference. The candidate retains all
current language, embedding, rerank, image, and multimodal routes supported by
its enabled engines; JIT single-model residency and full-stream leases;
bounded queues and engine capacity; idle unloading; exactly-once local usage
events with optional durable central delivery; and the user's exact selected
lexical model folder, including nested, symlink-spelled, and external-volume
locations.

## Install and exercise one pilot Mac

1. Confirm the Mac is non-critical, then copy the candidate DMG to it and
   verify the checksum:

   ```bash
   shasum -a 256 Unified-Inference-0.9.0-macos-arm64.dmg
   ```

2. Quit only the existing menu app if present. Drag **Unified Inference** to
   **Applications** and choose **Replace** when Finder asks, then open the
   installed app. The inference service may remain enabled while the bundle is
   copied. The new app refreshes its exact registration on launch; approve the
   background item in **Setup & Health** only if macOS requests it.
3. In **Storage**, select the exact intended model folder. For an external
   drive, select the nested folder such as `/Volumes/Metis/models`, not the
   volume root. Do not move or copy existing weights for the pilot.
4. Prepare one engine in **Runtime Updates**, then install or explicitly import
   one known-compatible model through **Model Library** onto the selected Mac
   and storage entry. Confirm the UI shows that exact machine and destination.
5. With no model resident, run **Setup & Health → Run Self-Test**. Confirm the
   request cold-loads the selected model, completes, records usage for this
   Mac, and returns to the configured idle policy.
6. On the designated Hub Mac, open **Hub Mode**, configure Tailscale HTTPS (or
   an already-secured HTTPS proxy), and enable the Hub. Confirm the dashboard
   opens and the local worker appears as **LIMITED / overflow**. Create an
   invitation from **Invite and manage Macs**.
7. Pair another Mac with the pilot Hub. Toggle **Contribute this Mac to
   the pool** on, route one request through the Hub, pause contribution, confirm
   local inference still works, then rejoin. A limited-compute Hub worker must
   remain in the Hub's `overflow` service class.
8. Restart the background service and repeat one self-test. Record any
   permission prompt, runtime preparation failure, model-path change, or
   unexpected reload as a pilot failure.

The detailed engine installation path is in [INSTALL.md](INSTALL.md). The
complete evidence commands and opt-in restart/Fleet exercises are in
[smoke_checks.md](smoke_checks.md).

## Evidence required before expanding the pilot

For each Mac, retain the secret-redacted acceptance report for the exact
candidate build. The acceptance run must prove both HTTP planes, a durable
self-test usage row, service restart, exact storage health, and an idle
pause/rejoin cycle while restoring the original preference. Where central
usage reporting is configured, require the outbox to drain and verify the
single route-correlated ledger row.

After the UI install finishes, run the collector from a checkout with the
self-tested alias and the exact Storage name selected in the UI:

```bash
python3 macos/packaging/collect_acceptance.py \
  --app "/Applications/Unified Inference.app" \
  --live --require-live \
  --exercise-service-restart \
  --exercise-fleet-participation \
  --self-test your-pilot-alias \
  --expected-engine llama.cpp \
  --require-pilot-install-storage your-storage-name \
  --require-cold-jit \
  --require-guided-setup \
  --require-postgres-drain \
  --output "$HOME/Desktop/unified-inference-pilot-acceptance.json"
```

Use the engine actually selected for the pilot. The storage check follows the
configured lexical string without resolving symlinks, requires the latest
install for that alias to be complete and revision-pinned, proves its byte
count and registration transition, and rejects a destination outside the
selected root. The cold-JIT check additionally requires empty residency before
the self-test, the request's authoritative coordinator cold-start marker, and
empty residency after the requested unload. Run the participation exercise
only while Hub dispatch is quiesced.

If a pilot must be removed, disable its exact background service and Hub
service when configured, turn off the menu login item, and quit the menu app.
Then run **Uninstall Unified Inference (Preserve Data).command** from the DMG.
It moves the exact app and the default manager-owned runtime directory to
Trash. It retains `.env`, `config.yaml`, the SQLite usage ledger and durable
token outbox, node identity, lifecycle receipts, security scopes, Hub pairing,
Hub credentials/enrollments/routing metadata, the default model directory, and
every configured external model location. Reinstalling resumes the same
accounting and Hub identities. Externally owned engines such as oMLX are not
modified.

These two pilot assistants are deliberately narrower than the production
signed lifecycle executor: they do not migrate a legacy sidecar, revoke an
offline Hub pairing, delete private/accounting state, or delete arbitrary
model payloads. Signed migration, rollback, and destructive privacy-reset
modes remain production gates.
