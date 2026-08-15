# Native macOS smoke checks

Run these checks on the target Apple Silicon workstation. Automated tests use
fake engines and cannot validate Metal memory release, upstream API drift,
LaunchAgent behavior, or model quality.

Start every candidate pass with the evidence collector. It is read-only unless
`--self-test` is supplied, and it writes the report atomically with mode
`0600`. Credential-bearing fields and URLs are redacted; token counts remain
visible:

For the clean-install pass, quit the menu app and reset its preferences domain
before launching the newly installed candidate:

```bash
defaults delete com.mnemosyne.inference.menu 2>/dev/null || true
```

This clears menu/login-item preferences only; it does not delete Application
Support configuration, weights, bookmarks, usage, or the Postgres outbox.
Launch the candidate, confirm Setup & Health appears automatically, complete
its real model self-test, and add `--require-guided-setup` to the first report:

```bash
python3 macos/packaging/collect_acceptance.py \
  --app "/Applications/Unified Inference.app" \
  --dmg "/path/to/Unified-Inference-0.9.0-macos-arm64.dmg" \
  --live \
  --require-live \
  --self-test your-model-alias \
  --require-guided-setup \
  --require-postgres-drain \
  --output "$HOME/Desktop/unified-inference-live-acceptance.json"
```

When control authentication is enabled, export its password as
`MNEMOSYNE_ADMIN_PASSWORD` for this command only. Do not pass the value on the
command line. Attach the resulting report to the acceptance ledger evidence;
a registered/running LaunchAgent does not pass when either HTTP plane,
readiness, version matching, catalog, usage, or the requested durable self-test
fails. The Postgres option also requires a new successful flush and an empty
outbox; verify the corresponding `event_id` exists exactly once in the central
ledger before clearing the remote-delivery gate.
The guided-setup check rejects an older preference bit: the exact installed
version and build must have recorded first presentation before completion, and
the same report must pass the durable-usage self-test.

After the ordinary UI pass, rerun the collector with the relevant strict
scenario flags instead of translating screenshots into release claims:

```bash
python3 macos/packaging/collect_acceptance.py \
  --app "/Applications/Unified Inference.app" \
  --dmg "/path/to/Unified-Inference-0.9.0-macos-arm64.dmg" \
  --require-live \
  --exercise-service-restart \
  --exercise-reconcile \
  --self-test protected-vision-alias \
  --expected-engine llama.cpp \
  --require-vision \
  --require-protected-model \
  --require-download-lifecycle \
  --require-postgres-drain \
  --output "$HOME/Desktop/unified-inference-restart-acceptance.json"
```

Run a second pass with `--exercise-keepalive` to prove launchd restarted the
exact registered job after SIGTERM. The report requires a different PID and
both HTTP planes healthy before continuing to reconciliation and inference.
To prove a real logout/login or reboot rather than another process restart,
keep the accepted first report as the private baseline, complete the login
cycle, and run:

```bash
python3 macos/packaging/collect_acceptance.py \
  --app "/Applications/Unified Inference.app" \
  --live --require-live \
  --self-test your-model-alias \
  --require-login-cycle-baseline \
    "$HOME/Desktop/unified-inference-live-acceptance.json" \
  --output "$HOME/Desktop/unified-inference-after-login.json"
```

The baseline must be mode `0600`, accepted, from the same host and exact app
version/build. The registered LaunchAgent must return under a different GUI
audit-session ID and PID, both HTTP planes must be healthy, and the current
report must complete another durable self-test. An ordinary restart cannot
satisfy the audit-session check.
For the migrated-library pass add
`--require-lmstudio-adoption <the-same-self-test-alias>` while LM Studio is
stopped. For the oMLX pass select an oMLX alias and add
`--require-omlx-recovery --expected-engine omlx`; this combination requires a
service restart/KeepAlive exercise and `--exercise-reconcile`.

The download check reads the durable install transition journal. It needs
real target-Mac records proving cancellation followed by retry and completion,
downloaded-weight registration retry without another download, completed
history dismissal, exact revision pinning, and managed deletion. An upgraded
database receives only a `snapshot` event, which is deliberately insufficient
to clear transitions that were not observed by this candidate.

## 1. Configuration and listeners

```bash
uv run --project macos/service mnemosyne-macos --check-config \
  --config "$HOME/Library/Application Support/Mnemosyne/config.yaml" \
  --env "$HOME/Library/Application Support/Mnemosyne/.env"
lsof -nP \
  -iTCP:1240 -iTCP:17321 -iTCP:17322 -iTCP:17323 \
  -iTCP:17324 -iTCP:17325 \
  -sTCP:LISTEN
```

Confirm Unified Inference owns `1240`/`17321` and every inner listener is
loopback-only. oMLX owns `17322` when that optional engine is enabled.
Manager-owned DS4, MFLUX, and llama.cpp should be absent from `17323`,
`17324`, and `17325` while unloaded. LM Studio is not part of the inference
topology. The previous token sidecar is not required in the inference path.

With every configured engine empty, confirm both status and the aggregate model
catalog remain available:

```bash
curl -s http://127.0.0.1:17321/manager/status | jq
curl -s http://127.0.0.1:17321/manager/readiness | jq
curl -s http://127.0.0.1:1240/v1/models | jq
```

Open **Settings → Setup & Health** and compare it with the readiness payload.
Confirm the product version matches `macos/VERSION`, diagnostics contain no
credential or URI password, manager-owned stopped engines are described as
available when their runtime is installed, external oMLX is not described as
ready unless its authoritative service responds, and DS4/MFLUX are labeled
Preview rather than Stable.

### Fleet snapshot and bounded admission

Set different values for `INFERENCE_API_KEY` and `FLEET_API_KEY` in the private
`.env`, restart the service, and verify the fleet credential boundary:

```bash
curl -si http://127.0.0.1:1240/fleet/v1/snapshot
curl -si http://127.0.0.1:1240/fleet/v1/snapshot \
  -H "Authorization: Bearer $INFERENCE_API_KEY"
curl -s http://127.0.0.1:1240/fleet/v1/snapshot \
  -H "Authorization: Bearer $FLEET_API_KEY" | jq
```

The first two calls must return `401`; the third must return schema version 1
without a credential, DSN, absolute model path, storage root, or bookmark.
Successive snapshots must retain `node.node_id` and `node.instance_id` while
increasing `snapshot_sequence`. Restarting the service must change only the
instance ID. A managed install pinned to a 40–64 hex Hub revision must be
`fleet_eligible`; a Finder/manual profile or symbolic revision must be
`unverified` and ineligible.

For the Nyx rollout, change only `server.inference_bind` from loopback to the
Mac's trusted LAN or Tailscale address, leave `server.control_bind` on
loopback, restart, and restrict `:1240` with the host firewall or Tailscale
ACLs. From Nyx, repeat the authenticated snapshot request against that private
address before enrolling it in Fleet. Do not expose this bearer-authenticated
plain-HTTP listener on an untrusted network.

Set `server.max_concurrency` below a llama.cpp profile's `load.parallel`, set
`server.max_queue_depth: 1`, and issue enough overlapping long requests to
occupy every permit and the one waiter. Confirm the next request receives
`429` with `detail.code=node_busy` and `Retry-After: 1` before the inner engine
sees it. During the run, snapshot `capacity.active`, `capacity.available`,
`admission.queue_depth`, and `queued_by_deployment` must agree. Queue a
different model and verify no later request to the old model bypasses it; the
snapshot must show `draining` and the target deployment ID until every old
stream closes.

## 2. Local-library adoption

In **Settings → Models**, choose **Add Existing Models…** and select the exact
existing library directory, including a nested external path such as
`/Volumes/Athena/models`.

Confirm:

- no model is selected for import automatically;
- complete split-GGUF sets appear as one candidate and incomplete sets are
  unavailable;
- `mmproj` files appear only as projector choices, never as primary models;
- MLX directories and GGUF models are assigned to oMLX and llama.cpp
  respectively;
- every selected model has an editable, unique alias; a vision GGUF
  preselects the highest-fidelity same-directory projector and offers another
  projector or **Text only (opt out)**;
- architecture, context length, parameter count, and summary are shown when
  discoverable from GGUF/config/model-card metadata;
- importing does not copy model files, contact an engine load/unload endpoint,
  or make a process resident;
- the exact selected directory and containing-volume UUID are persisted;
- an LM Studio model root that is a symlink to a nested external-SSD folder
  remains recorded under the selected symlink path while volume inspection,
  GGUF validation, and model loading operate on its authorized target;
- `config.yaml` contains a 64-character SHA-256 `scope_id` for a
  Finder-authorized protected folder but contains no raw/base64 bookmark;
- the matching receiver-owned durable bookmark exists only below the
  mode-`0700` private `state/security-scopes/` directory with file mode `0600`;
- that scope directory remains beside `config.yaml` when
  `paths.state_database` is changed to another location;
- saving a location with a missing, stale, or path-mismatched `scope_id` is
  rejected before `config.yaml` changes; and
- an alias matching a legacy LM Studio profile retains that alias and its
  compatible load settings while changing to the selected native engine.

Rescan before importing and confirm stale opaque candidate/projector IDs are
rejected. Unmount an external library, or mount a different volume at the same
path, and confirm a request fails before the currently resident model is
drained.

Quit the menu app, restart the LaunchAgent, and request a model in the
Finder-authorized folder. Confirm the service reactivates its receiver-owned
bookmark and that the scoped launcher reactivates it again before `exec` starts
the managed child. Remove a storage location, restart, and confirm its
unreferenced private bookmark is pruned while bookmarks still referenced by
the persisted configuration remain.

Also test a protected path whose authorization is unavailable: poll
`/manager/status` concurrently and confirm the control plane remains responsive
while bookmark registration/reactivation, model resolution, or GGUF validation
ends at its bounded deadline with a permission/volume diagnostic. Repeat by
cancelling the caller and by stopping the service; confirm the complete
bookmark/filesystem-helper process group is gone in all three cases rather
than lingering behind the service.

These protected-folder checks are an outstanding packaged-Mac gate. Confirm
the app is using either the intended stable `CODESIGN_IDENTITY` or a known
ad-hoc build. Theseus now has a valid Developer ID Application identity and its
installed stable-signed bundle should retain that identity across updates;
switching to an ad-hoc or different signing identity may require folder
re-selection. Do not expect or claim App Sandbox bookmark entitlements: verify
the ordinary bookmark's implicit interprocess handoff, receiver-owned bookmark
creation, LaunchAgent restart, helper restart, and child `exec` directly.

## 3. Managed llama.cpp lifecycle

Install llama.cpp from **Settings → Runtime Updates**, then request an adopted
GGUF alias through the unified API:

```bash
curl -s http://127.0.0.1:1240/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local-qwen","messages":[{"role":"user","content":"Reply with one short sentence."}]}' \
  | jq
```

Verify the manager starts its recorded `llama-server` process group on
loopback `:17325`, rewrites the upstream model name to the public alias, and
returns authoritative backend token usage. A second request for the same alias
must reuse the process. For a profile with an explicit projector, confirm the
server starts with that exact `--mmproj` path and a supported multimodal request
works; a text-only profile must omit the flag.

While a protected-folder llama.cpp process is resident, restart only the core
so `llama-server` briefly survives. Confirm its ownership record retains the
storage root, scope ID, and volume UUID; the new core must reactivate the grant,
revalidate the volume/model through that metadata, prove the live process
identity, and then follow the configured startup recovery policy. It must not
signal a survivor whose scope, volume, argv, or process identity fails.

Unload through `POST :17321/manager/unload`. Confirm the owned process exits,
Metal memory returns, and the GGUF/projector files remain untouched. Place an
unrelated listener on `17325` and confirm Unified Inference reports a
fail-closed ownership/port error and never signals that process.

## 4. Managed Hugging Face downloads

In **Settings → Model Library**, search once and confirm llama.cpp, oMLX, DS4,
and MFLUX candidates share one result list with explicit engine-support badges;
there must be no engine tabs or picker. Choose a llama.cpp GGUF repository and
confirm Download remains disabled until an exact quant/shard set is selected.
Confirm Hugging Face YAML front matter is absent, Markdown headings/lists/links
are rendered, the card and full detail pane scroll without truncating install
controls, and detected architecture, context length, parameter count, and
license match the repository metadata. When the
repository publishes a vision projector beside the selected GGUF, confirm the
highest-fidelity option is selected automatically; then exercise both a manual
choice and **Text only (opt out)**. Choose a GUI-configured storage folder and
start a small download.

Confirm the install record pins the resolved Hub revision and exact primary
shards/projector, survives closing and reopening Settings, and creates a
profile without loading it. While it runs, confirm transferred/total bytes,
percentage, the progress bar, and transfer speed update without reopening the
window. Exercise cancel and retry, then clear the completed history row and
verify the model profile and files remain. Remove that profile with
**Keep Files** and verify only configuration changes; separately use
**Delete Files** on an app-managed test download and verify its exact directory
and profile disappear. Confirm Finder-imported profiles cannot delete files,
and no unrelated files are touched. Repeat with a gated repository to prove
the write-only `HF_TOKEN` reaches only the download worker.

Use an empty unified search and identify results with the DS4 support badge.
Confirm nine current single-node choices appear: five DeepSeek V4 and four GLM
5.2. DSpark support weights and
distributed-only Pro halves must not appear as standalone models. Select the
Unsloth GLM Q4 choice and confirm its displayed size covers eleven shards; its
durable install record must retain all eleven exact paths and one immutable
revision.

For a DeepSeek Flash profile with enough memory headroom, set **Resident request
sessions** to `2`, restart/reload the profile, and issue two overlapping
requests. Confirm the DS4 argv contains exactly `--batched-session 2`, status
reports capacity two, neither request is rejected by manager admission, and
both leases drain before unload. Repeat with the setting unset and confirm
authoritative capacity returns to one.

Afterward, `GET /manager/model-library/install-evidence` must show the
candidate-observed state transitions, including hidden/deleted rows, without
credentials or arbitrary worker output. The ordinary installs endpoint remains
the dismissible UI view.

## 5. oMLX lifecycle

Request the configured oMLX alias. Confirm Mnemosyne unloads llama.cpp first,
oMLX reports exactly one loaded pool model, and a second request reuses it.
Exercise non-streaming and streaming chat, Responses, embeddings, or rerank as
allowed by that profile. Explicit unload must converge without an admin-auth
error.

Set oMLX `scheduler.max_concurrent_requests` above one, leave Unified
Inference's concurrency ceiling blank, and issue that many overlapping warm
requests. `GET /manager/status` and the fleet snapshot must report the oMLX
admin setting as the authoritative capacity while all leases remain on the
same resident epoch. Then set a lower global ceiling and verify it caps, but
never raises, the engine limit.

After several requests, inspect `GET /manager/performance` and the menu-bar
popover. Confirm p50/p95, cold-start count, and streamed tokens/second are
present and that no prompt or response content appears. Compare the same alias
against a direct compatible endpoint with `macos/scripts/benchmark_native.py`.

Inspect the oMLX Runtime Updates card's SSD-cache metrics. Exercise **Reset SSD
Cache…** only with disposable cache state: admission must close, active work
must drain, all engines must unload, the official oMLX cache-clear API must
complete, and model weights must remain untouched.

If oMLX rejects unload, do not weaken strict residency. Keep it loopback-only
and correct its admin authentication/session configuration.

For an MLX library in a protected folder, separately authorize that folder for
the external oMLX installation. The receiver-owned Unified Inference bookmark
is intentionally available only to its own scoped helpers and children.

## 6. DS4 lifecycle

Request the DS4 alias and confirm:

- `ds4-server` starts with the configured GGUF, context, `127.0.0.1:17323`, and
  KV-cache arguments;
- `/v1/models` becomes ready before the client request is proxied;
- OpenAI Chat/Completions, Responses, and Anthropic Messages behave as expected;
- unloading sends TERM to the owned process group, escalates only after the
  grace period, and leaves GGUF/KV files intact;
- an unrelated process occupying `17323` is reported and never signaled.

## 7. Cross-engine drain and strict residency

Begin a long streaming request on each engine. While it is active, request an
alias on another engine. The old stream must finish (or disconnect) before its
engine unloads. Continuous new traffic for the old target must not starve the
queued switch. Sample Activity Monitor or `ps` throughout and confirm two model
engines are never resident together.

Directly load a second model through oMLX, then call:

```bash
curl -X POST http://127.0.0.1:17321/manager/reconcile | jq
```

Mnemosyne must detect the drift, unload it, and never load another target while
any enabled adapter has uncertain state.

## 8. MFLUX image lifecycle

Request each configured MFLUX alias through `POST :1240/v1/images/generations`.
Confirm the worker appears only on loopback `:17324`, produces a valid base64
PNG, and exactly one model process is resident. While a long image is running,
cancel the client and verify the worker exits and Metal memory returns. Repeat
for timeout, explicit unload, and a switch to llama.cpp/oMLX/DS4. An unrelated
listener on `17324` must cause fail-closed degraded state and must never be
signaled. Confirm image calls do not appear in `/manager/usage`.

For the installed app, inspect the saved engine settings and effective worker
location. `engines.mflux.python` must be unset and neither
`MNEMOSYNE_MFLUX_PYTHON` nor `MNEMOSYNE_MFLUX_PYTHONPATH` may point into the
source checkout. Confirm the worker comes from the packaged image layer or an
activated managed MFLUX runtime.

## 9. Usage outbox

With the Postgres DSN intentionally unreachable, complete streaming and
non-streaming requests through each engine. Confirm local rows and a growing
outbox through `GET /manager/usage`. Restore Postgres, wait one flush interval,
and verify the outbox drains once with no duplicate `event_id` rows centrally.
Confirm Settings displays the identifier inherited from the previous token
sidecar as read-only, central reporting defaults on for a new configuration,
and the Postgres DSN is never returned by the API.

## 10. LaunchAgent and menu app

Stage and sign the app, move it to `/Applications`, enable **Background
service**, and approve it in Login Items if requested. Confirm:

```bash
codesign --verify --deep --strict --verbose=4 \
  "/Applications/Unified Inference.app"
codesign -dvvv "/Applications/Unified Inference.app"
codesign -dvvv \
  "/Applications/Unified Inference.app/Contents/MacOS/mnemosyne-service-bootstrap"
launchctl print "gui/$(id -u)/com.mnemosyne.inference.agent"
curl -fsS http://127.0.0.1:17321/manager/status | jq
```

The app and helper must have the same intended Team ID; the helper identifier
must be `com.mnemosyne.inference.service`. The LaunchAgent must resolve
`Contents/MacOS/mnemosyne-service-bootstrap` directly and reach `state =
running`. Treat the control-plane response as the readiness proof:
`SMAppService` reporting enabled is not sufficient if `:17321` is unavailable.
The direct helper must not carry an unexpected embedded launch-constraint
section.

- launching the installed app creates exactly one visible brain-profile status
  item and logs `Unified Inference menu bar status item installed`;
- **Settings…** opens a separate resizable window with Setup & Health, General,
  Engines, Runtime Updates, Storage, Model Library, Models, Usage, and
  Credentials pages; the pages use native controls rather than a raw file editor,
  add profiles only through Model Library or Finder discovery, remove profiles
  correctly, and warn before discarding unsaved edits;
- **Storage → Add Model Folder…** opens the native directory chooser; selecting
  an exact nested folder such as `/Volumes/Athena/models` displays that path,
  its containing mount and free space, and never reduces it to `/Volumes/Athena`;
  the menu transfers its Finder bookmark to the service, the service creates a
  receiver-owned durable bookmark, only the resulting SHA-256 `scope_id`
  appears in YAML, and selecting the folder again repairs a missing or stale
  private bookmark;
  unmount the drive and confirm the location becomes unavailable, then mount a
  different volume at the same path and confirm the UUID mismatch fails closed;
- **Model Library** exposes one cross-engine list with engine-support badges,
  never engine tabs or a raw repository/storage-path field; start a small compatible test
  download, confirm bytes/total, percentage, progress, and speed update live,
  then close/reopen Settings and confirm progress persists and cancel/retry
  work without making any model resident;
- **Models → Add Existing Models…** always opens the directory picker and uses
  the explicit, initially-unselected workflow from section 2;
- **Models → Detected model folders** lists LM Studio's configured
  `downloadsFolder` before `~/.lmstudio/models`, without contacting an LM
  Studio process; selecting either is still a Finder-confirmed scan, not an
  automatic model load;
- existing model engine/source/storage/served-name/projector/family fields are
  read-only; there is no raw model or projector path editor and endpoint
  routing uses only the typed Generation, Embeddings, Rerank, or Image role
  choices valid for that engine;
- there is no LM Studio engine toggle, credential, inventory action, callable
  profile, or request to `:1234`;
- a deliberately invalid field combination is rejected without overwriting
  `config.yaml`; an older app refuses to save a newer `schema_version`;
  model-only changes apply without restart, while an engine, storage, or port
  change offers **Restart Service**;
- open Settings, complete a download or local import from another window, then
  save the stale first window; confirm its old revision receives `409 Conflict`
  and cannot erase the newly added profile;
- configured credentials show only a configured indicator, never the saved
  value; replacement and explicit removal preserve unrelated `.env` lines;
- service restart and signed-bundle refresh wait for asynchronous
  `SMAppService` unregister completion and a terminal disabled state before
  registering again; an approval-required or failed refresh remains visible
  and retryable instead of being reported as applied;
- clicking the item opens the controller popover while the app remains absent
  from the Dock;
- reopening the already-running app brings the Settings window forward;
- a fresh preferences domain presents Setup & Health on first launch and does
  not mark setup complete merely because the window opened or the service
  registered;
- select a configured alias and run the Setup & Health self-test. Confirm it
  calls the public `:1240` listener with the alpaca prompt, selects the profile's
  vision projector when present, shows the response/route/timing/token result,
  and finds the matching durable local usage row. Its Postgres status must
  distinguish writer readiness and outbox depth from an actually drained
  central event;
- only after that successful self-test does an ordinary login launch stop
  presenting the setup window automatically;
- the service survives **Quit Menu App**;
- the menu app can list/load/unload configured aliases after reopening;
- disabling the background service unregisters the LaunchAgent;
- an unexpected service exit is restarted by `KeepAlive`;
- login starts the service without a terminal or Docker Desktop;
- logs and private config live below
  `~/Library/Application Support/Mnemosyne/`.

## 11. Engine runtime updates

In **Settings → Runtime Updates**, confirm llama.cpp reports the official
`ggml-org/llama.cpp` Apple Silicon release, oMLX links to its official release,
MFLUX matches the official PyPI version, and DS4 shows the current official
`antirez/ds4` commit. No dependency metadata should be requested from the
Unified Inference GitHub repository.

For a stable Homebrew-owned oMLX installation with an update available,
confirm the UI displays the exact stop/update/upgrade/start sequence. Begin a
long request and approve the update: it must wait for the lease, invoke only
those fixed owner commands, restart oMLX, validate an authoritative empty
inventory, and then reopen admission. A Homebrew HEAD build must refuse this
path and present stable migration guidance; an official app must continue to
delegate updates to its own updater.

Start a long request, then install an available official update. The download
and validation phase must not disturb the resident model. For llama.cpp,
confirm the asset name/URL, GitHub-published size and SHA-256, safe archive
layout, executable, and required CLI flags are checked before activation.
Activation must wait for the lease, unload every engine through the maintenance
barrier, atomically select the new runtime below Application Support, and
leave all model folders untouched. Load a model with the new runtime, restart
the service, and confirm the same runtime is selected. Finally choose
**Roll Back**, verify the previous runtime is restored, and confirm a bad
checksum, unsafe archive, failed DS4 build, or failed MFLUX import never changes
`current.json`.

For the required managed llama.cpp lifecycle proof, use two installed official
versions: call the original version `A` and the update `B`.

1. Activate `B`, then run the acceptance collector with
   `--exercise-service-restart --self-test <alias> --expected-engine llama.cpp`.
   This records successful inference from `B` under a different anonymous
   service-instance ID than the activation.
2. Choose **Roll Back** to return to `A`, then repeat that restart/self-test
   pass. This records successful inference from `A` under a different
   service-instance ID than the rollback.
3. Make a private backup of inactive
   `~/Library/Application Support/Mnemosyne/runtimes/llama.cpp/B/runtime.json`.
   In the inactive copy only, temporarily change `entrypoint.binary` to an
   escaping path such as `../acceptance-invalid/llama-server`. Request the
   official `B` install again. It must be rejected before activation, the
   lifecycle event must use the fixed `unsafe_archive` failure code, and
   `A` must remain current. Restore the exact backed-up manifest immediately.
   Never alter the active `A` directory.
4. Capture the composed proof:

   ```bash
   python3 macos/packaging/collect_acceptance.py \
     --app "/Applications/Unified Inference.app" \
     --live --require-live \
     --exercise-service-restart \
     --self-test your-llamacpp-alias \
     --expected-engine llama.cpp \
     --require-runtime-lifecycle llama.cpp \
     --output "$HOME/Desktop/unified-inference-runtime-acceptance.json"
   ```

The final report is accepted only if the ordered `A → B → A` transition,
both post-restart inference events, the path-safety rejection, and the fresh
installed-runtime snapshot all agree.
`GET /manager/runtime-updates/evidence` is local and read-only; its bounded
mode-`0600` journal contains fixed fields and failure codes, never raw
exceptions or credentials.

## 12. LM Studio directory migration

This gate applies only to a machine with an older LM Studio-backed
configuration or model library.

1. Stop LM Studio before starting Unified Inference.
2. Open the upgraded schema-version-3 configuration and confirm there is no
   `engines.lmstudio` block. Old LM Studio profiles should appear only under
   `migration.legacy_lmstudio_profiles` and must not appear in `/v1/models`.
3. Confirm **Detected model folders** offers the configured
   `downloadsFolder`, then `~/.lmstudio/models`, without opening either path
   until Finder selection.
4. Adopt the library with **Add Existing Models…** and verify matching aliases
   and compatible load settings are preserved. Imported records must disappear
   from the inert migration list.
5. Exercise each imported alias through native llama.cpp or oMLX, including
   non-streaming, streaming usage, explicit unload, service restart, and a
   login cycle.

Throughout the check, confirm logs and network inspection show no request to
`:1234`, and that stopping or uninstalling LM Studio has no effect on the
catalog, residency, or token accounting.

## 13. Signed application update and rollback

This gate requires two Developer ID-notarized builds with the production
Sparkle public key and signed HTTPS appcast. It cannot be cleared with ad-hoc
artifacts.

1. Install the older notarized DMG and complete Setup & Health.
2. Publish the candidate to a non-public test feed signed by the matching
   Sparkle private key. **Check for Updates…** must validate its EdDSA signature
   and Apple code identity, replace the app, refresh service registration, and
   preserve Application Support data.
3. Serve an altered archive and an invalidly signed appcast in turn. Both must
   be rejected with the older installed app left intact.
4. Install the previous immutable notarized DMG over the app bundle without
   deleting `~/Library/Application Support/Mnemosyne`, then rerun Setup &
   Health. Configuration, weights, runtimes, bookmarks, usage, and outbox state
   must remain available.
