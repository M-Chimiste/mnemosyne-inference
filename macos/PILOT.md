# Unified Inference 0.9 Mac pilot

This pilot is for **non-critical Apple Silicon Macs running macOS 15 or
newer**, including Macs with an earlier Unified Inference pilot installed. It
validates fresh install and in-place upgrade, exact model-storage selection,
cold/JIT model loading, per-serving-Mac usage accounting, Hub
join/pause/rejoin behavior, and a recoverable uninstall that retains private
accounting data.

## Candidate artifact

- Product: Unified Inference 0.9.0, build 70, arm64
- DMG: `Unified-Inference-0.9.0-macos-arm64.dmg`
- SHA-256: `022cb66aeb3d2f36af1a2f52e09f259e8b3cb42192b714c2b320e435576a9729`
- Signing: ad-hoc development signature
- Artifact acceptance: passed

The DMG is structurally verified but is not Developer ID signed, notarized, or
stapled. On each pilot Mac, use Finder's **Control-click → Open** once if
Gatekeeper asks. Never disable Gatekeeper system-wide.

## Deliberate pilot limits

- Upgrade an earlier Unified Inference pilot only with **Install or Upgrade
  Unified Inference.command** from the DMG. It verifies the candidate, retains
  all Application Support data and model locations, checks that `.env` is
  byte-identical, keeps the prior app in Trash for rollback, and waits for an
  already-enabled exact service registration to restart. A Mac that still
  runs the legacy token sidecar must complete the documented identity/DSN
  inheritance and retirement path first.
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

2. For a fresh install, drag **Unified Inference** to **Applications**. For an
   existing pilot, quit only the menu app and double-click **Install or
   Upgrade Unified Inference.command**; the inference service may remain
   enabled. Launch the installed app and enable or approve the background
   service in **Setup & Health** if macOS requests it.
3. In **Storage**, select the exact intended model folder. For an external
   drive, select the nested folder such as `/Volumes/Metis/models`, not the
   volume root. Do not move or copy existing weights for the pilot.
4. Prepare one engine in **Runtime Updates**, then install or explicitly import
   one known-compatible model through **Model Library** onto the selected Mac
   and storage entry. Confirm the UI shows that exact machine and destination.
5. With no model resident, run **Setup & Health → Run Self-Test**. Confirm the
   request cold-loads the selected model, completes, records usage for this
   Mac, and returns to the configured idle policy.
6. Pair the Mac with the pilot Hub. Toggle **Contribute this Mac to
   the pool** on, route one request through the Hub, pause contribution, confirm
   local inference still works, then rejoin. A limited-compute Hub worker must
   remain in the Hub's `overflow` service class.
7. Restart the background service and repeat one self-test. Record any
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

If a pilot must be removed, disable its exact background service, turn off the
menu login item, and quit the menu app. Then run **Uninstall Unified Inference
(Preserve Data).command** from the DMG. It moves the exact app and the default
manager-owned runtime directory to Trash. It retains `.env`, `config.yaml`,
the SQLite usage ledger and durable token outbox, node identity, lifecycle
receipts, security scopes, Hub pairing, the default model directory, and every
configured external model location. Reinstalling resumes the same accounting
identity and queued usage. Externally owned engines such as oMLX are not
modified.

These two pilot assistants are deliberately narrower than the production
signed lifecycle executor: they do not migrate a legacy sidecar, revoke an
offline Hub pairing, delete private/accounting state, or delete arbitrary
model payloads. Signed migration, rollback, and destructive privacy-reset
modes remain production gates.
