# Native macOS smoke checks

Run these checks on the target Apple Silicon workstation. Automated tests use
fake engines and cannot validate Metal memory release, upstream API drift,
LaunchAgent behavior, or model quality.

## 1. Configuration and listeners

```bash
uv run --project macos/service mnemosyne-macos --check-config \
  --config "$HOME/Library/Application Support/Mnemosyne/config.yaml" \
  --env "$HOME/Library/Application Support/Mnemosyne/.env"
lsof -nP \
  -iTCP:1234 \
  -iTCP:1240 -iTCP:17321 -iTCP:17322 -iTCP:17323 \
  -iTCP:17324 -iTCP:17325 \
  -sTCP:LISTEN
```

Confirm Unified Inference owns `1240`/`17321` and every inner listener is
loopback-only. oMLX owns `17322` when that optional engine is enabled.
Manager-owned DS4, MFLUX, and llama.cpp should be absent from `17323`,
`17324`, and `17325` while unloaded. An older migration installation may still
have LM Studio on `1234`, but a fresh configuration does not require it. The
previous token sidecar is not required in the inference path.

With every configured engine empty, confirm both status and the aggregate model
catalog remain available:

```bash
curl -s http://127.0.0.1:17321/manager/status | jq
curl -s http://127.0.0.1:1240/v1/models | jq
```

## 2. Local-library adoption

In **Settings → Models**, choose **Add Existing Models…** and select the exact
existing library directory, including a nested external path such as
`/Volumes/Athena/models`.

Confirm:

- no candidate is selected automatically;
- complete split-GGUF sets appear as one candidate and incomplete sets are
  unavailable;
- `mmproj` files appear only as projector choices, never as primary models;
- MLX directories and GGUF models are assigned to oMLX and llama.cpp
  respectively;
- every selected model has an editable, unique alias and every GGUF has an
  explicit **Text only (no projector)** or same-directory projector choice;
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

In **Settings → Model Library**, choose llama.cpp and search for a GGUF
repository. Confirm Download remains disabled until an exact quant/shard set is
selected. Select an optional projector only from the same repository directory,
choose a GUI-configured storage folder, and start a small download.

Confirm the install record pins the resolved Hub revision and exact primary
shards/projector, survives closing and reopening Settings, and creates a
profile without loading it. Exercise cancel and retry, then verify no unrelated
files are downloaded and no partial install is advertised as available. Repeat
with a gated repository to prove the write-only `HF_TOKEN` reaches only the
download worker.

## 5. oMLX lifecycle

Request the configured oMLX alias. Confirm Mnemosyne unloads llama.cpp first,
oMLX reports exactly one loaded pool model, and a second request reuses it.
Exercise non-streaming and streaming chat, Responses, embeddings, or rerank as
allowed by that profile. Explicit unload must converge without an admin-auth
error.

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
- **Settings…** opens a separate resizable window with General, Engines,
  Runtime Updates, Storage, Model Library, Models, Usage, and Credentials
  pages; the pages use native controls rather than a raw file editor,
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
- **Model Library** exposes engine and model choices through pickers/lists,
  never a raw repository or storage-path field; start a small compatible test
  download, close/reopen Settings, then confirm progress persists and
  cancel/retry work without making any model resident;
- **Models → Add Existing Models…** always opens the directory picker and uses
  the explicit, initially-unselected workflow from section 2;
- **Models → Detected model folders** lists LM Studio's configured
  `downloadsFolder` before `~/.lmstudio/models`, even while the LM Studio
  migration engine is disabled and its server is stopped; selecting either is
  still a Finder-confirmed scan, not an automatic model load;
- existing model engine/source/storage/served-name/projector/family fields are
  read-only; there is no raw model or projector path editor and endpoint
  routing uses only the typed Generation, Embeddings, Rerank, or Image role
  choices valid for that engine;
- **Legacy LM Studio Inventory…** appears only as the temporary migration
  bridge and remains read-only/residency-neutral;
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
- a fresh preferences domain presents Settings once on first launch and does
  not present it again on an ordinary login launch;
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

## 12. LM Studio migration soak

This gate applies only to a machine migrating an older LM Studio-backed
configuration. Before the soak, keep the legacy adapter explicitly enabled and
confirm its read-only inventory lists downloaded models without loading any:

```bash
curl -s http://127.0.0.1:17321/manager/engines/lmstudio/models \
  | jq '{count: (.models | length), loaded: [.models[] | select(.loaded)]}'
```

Create only the representative temporary profiles needed for the soak; do not
profile or download every model merely to test the bridge. Exercise at least
one existing language model and, when present, one embeddings model. Confirm
non-streaming, streaming with `stream_options.include_usage=true`, repeated
load, same-engine swap, and explicit `POST :17321/manager/unload`. After
unload, both `/manager/status` and the LM Studio inventory must report no
resident model. Verify the resulting local usage rows identify backend
`lmstudio` and the durable reporting outbox drains normally.

Before switching Unified Inference to `:1240`, boot out the previous token
sidecar LaunchAgent, persistently disable its launchd label, and prove the port
is free. Merely unloading it is insufficient because it can return at the next
login. Unified Inference then owns the same client-facing endpoint and must
account for requests itself; the previous sidecar is never chained in front of
or behind the new service.

Adopt the existing model library with
**Add Existing Models…**, verify matching aliases and compatible settings were
preserved, and test each migrated alias through its native llama.cpp or oMLX
engine.

Then disable **Keep LM Studio available during migration**, save, and restart
the background service. Confirm:

- every migrated alias works after repeated unloads, service restarts, and a
  login cycle;
- status and logs show no Unified Inference traffic to `:1234`;
- no model is missing merely because LM Studio itself is stopped; and
- token accounting, central delivery, and strict cross-engine residency remain
  correct for the entire soak period.

LM Studio adapter/configuration/credential/inventory removal is a later,
operator-accepted cleanup. Do not remove that fallback merely because the
short smoke passes.
