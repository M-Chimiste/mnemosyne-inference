# Mnemosyne for Apple Silicon

This is the native macOS sibling of the CUDA deployment. It exposes one stable
API while moving the single resident model between a manager-owned
[llama.cpp](https://github.com/ggml-org/llama.cpp) process for GGUF models,
[oMLX](https://github.com/jundot/omlx) for MLX models, and
[DwarfStar/DS4](https://github.com/antirez/ds4), plus a manager-owned
[MFLUX](https://github.com/filipstrand/mflux) image worker. The engines remain upstream
projects; Mnemosyne coordinates and proxies them without modifying their model
runtimes.

For a fresh workstation, begin with the
[end-user installation guide](INSTALL.md). It covers the Unified Inference
disk image, model storage, every native engine, legacy LM Studio model
adoption, and the canonical headless Homebrew/CLI installation of oMLX.

The runtime is deliberately not a Docker image. Docker Desktop runs ordinary
containers in a Linux VM, so it is not the right boundary for arbitrary
MLX/Metal processes. Mnemosyne Core and all engines run natively.

## Current validation

The relocatable runtime, signed local app bundle, embedded service bootstrap,
and embedded MFLUX worker have been exercised on an M4 Max. A real
`krea/Krea-2-Turbo` request through `POST :1240/v1/images/generations`
returned a valid 512×512 PNG, and the request was correctly absent from token
usage. The official llama.cpp `b10091` arm64 asset passed published-size and
SHA-256 verification, its runtime contract was validated, and it generated a
real response from an existing 4.8 GB LFM2.5 GGUF on Theseus. An externally
launched official oMLX 0.5.3 service also generated from an existing LFM2 1B
MLX model and returned usage that drained to the central ledger under
`theseus`. Packaged-service migration/soak, a real protected-folder transfer
followed by helper restart and managed-child `exec`, durable oMLX login
startup, DS4 model loading, automatic LaunchAgent restart/login behavior, and
CUDA parity remain separate smoke gates. A Developer ID-signed bundle is now
installed on Theseus: its direct `Contents/MacOS/mnemosyne-service-bootstrap`
LaunchAgent is running through `SMAppService`, and both native HTTP planes
answer from the packaged runtime. LM Studio is still available only as an
explicitly enabled migration fallback until the native-model soak is accepted.

## Ports and processes

| Port | Process | Role |
| ---: | --- | --- |
| `1240` | Mnemosyne Core | Unified OpenAI/Anthropic-compatible inference; drop-in replacement for the previous token sidecar |
| `17321` | Mnemosyne Core | Control API used by the menu bar app |
| `17322` | oMLX | Native MLX inference and admin API |
| `17323` | `ds4-server` | Mnemosyne-owned model process |
| `17324` | MFLUX worker | Mnemosyne-owned image process |
| `17325` | `llama-server` | Unified Inference-owned GGUF process |

All listeners default to loopback. Ports `17326` through `17329` are reserved
for future local engines and diagnostics. Legacy migration configurations may
also enable LM Studio on `1234`; fresh configurations do not.

Mnemosyne Core is a per-user LaunchAgent. `Unified Inference.app` is only a controller,
so **Quit Menu App** does not interrupt inference. The coordinator holds a model
lease for the complete upstream response stream, drains existing leases before
a swap, verifies every enabled engine is empty, loads one target, and verifies
that exactly one manager-owned target became ready.

The menu controller uses the friendly macOS Computer Name as its runtime title
and status-item label, so one unchanged app bundle identifies itself as Theseus,
Metis, Athena, or the name configured for that Mac. The installed bundle and
portable app remains `Unified Inference.app`; internal service identifiers and
the `Mnemosyne` application-support path stay stable so existing settings and
LaunchAgent registration survive upgrades. For diagnostic launches, the process
environment variable `MNEMOSYNE_WORKSTATION_NAME` overrides auto-detection.

## Requirements

- Apple Silicon and macOS 15 or newer.
- Python 3.11–3.13 and `uv` for service development.
- Swift 6 for menu development. Full Xcode is required for final app signing,
  `SMAppService` integration testing, and source builds of custom Metal kernels.
- oMLX, DS4, MFLUX, and the temporary LM Studio migration adapter are optional,
  and an unavailable engine should be disabled. Its profiles are retained but
  omitted from the callable model catalog until the engine is enabled again.

The supported operator path for oMLX is its official Homebrew formula and
headless CLI, not the separate oMLX menu-bar app. GLM-5.2 and related
custom-kernel builds require full Xcode; a plain installation falls back to a
much slower, more memory-hungry generic path. See
[Install oMLX with Homebrew and the CLI](INSTALL.md#5-install-omlx-with-homebrew-and-the-cli)
for the exact upstream commands and verification.

## Engine preparation

Open **Settings → Runtime Updates**, refresh official sources, and install the
available llama.cpp runtime. Unified Inference downloads the official
`ggml-org/llama.cpp` Apple Silicon release, verifies GitHub's published size
and SHA-256, checks the executable and required server flags, and activates it
only after the coordinator proves every engine empty. The private server binds
`127.0.0.1:17325`; clients continue to use the unified API on `1240`.

Unified Inference records usage for all language engines and central reporting
defaults on. During migration, it reads the previous sidecar's stable `node.id`
and Postgres DSN from
`~/Library/LaunchAgents/com.athena.token-sidecar.plist`; native `.env` values
take precedence. On the first normal service start it atomically copies missing
legacy values into Unified Inference's private mode-`0600` `.env`, making the
old sidecar a one-time migration source rather than a permanent dependency. If
neither a migrated identity nor the old sidecar is available, identity falls
back to the normalized macOS Computer Name. The identifier is displayed
read-only in Settings, while the DSN remains secret and is never returned by
the API.

After installing Unified Inference, run the one-shot migration script before
enabling clients on `:1240`:

```bash
macos/retire_legacy_sidecar.sh
```

It validates the exact `com.athena.token-sidecar` user plist, persistently
disables and boots out that job, restarts Unified Inference, and waits for both
native HTTP planes. It never kills an arbitrary listener on the port. The
script retains the old plist while the new service starts so it can migrate
the reporting identity and ledger DSN, then archives the inactive plist only
after both APIs are reachable.

Install oMLX through Homebrew and persist its loopback host, port `17322`, and
exact model directory with the upstream `omlx serve` CLI before starting its
Homebrew service. The complete copy-and-paste procedure, including the
GLM-5.2 native-kernel build and verification, is in
[INSTALL.md](INSTALL.md#5-install-omlx-with-homebrew-and-the-cli). No oMLX GUI
configuration is required.

Disable model pinning and per-model TTL/LRU behavior for profiles managed by
Mnemosyne. Current oMLX releases may protect unload through an admin session
even when load accepts a bearer key. The safest integration is loopback-only
oMLX with inner admin authentication disabled and authentication enforced at
Mnemosyne. `OMLX_API_KEY` and `OMLX_ADMIN_SESSION` remain available for an
installation that requires them.

Model profiles are retained when their engine is disabled, but omitted from
the callable `/v1/models` catalog until that engine is enabled. This keeps the
Settings UI available while an external engine such as oMLX is being installed
or repaired. Older app builds rejected that state at startup; use
`macos/enable_omlx_engine.sh` as a one-time recovery without disabling the
configured GLM profile.

oMLX remains a separately owned process. Unified Inference can register a
configured model directory through oMLX's admin API, but its Finder bookmark
cannot grant filesystem access to an already-running external oMLX process.
When an MLX library is in a macOS-protected folder, authorize that folder for
the oMLX installation as well.

DS4 is purpose-built for its published DeepSeek V4 Flash/PRO GGUFs; it is not a
general GGUF runner. Runtime Updates can fetch an exact official `antirez/ds4`
commit and build `ds4-server` locally with Apple's toolchain. An explicitly
configured external checkout/binary remains available as a development or
recovery fallback. Mnemosyne starts DS4 with an explicit model, context, host,
and port and terminates only a process whose recorded identity it can prove it
owns.

An existing LM Studio installation is not required for a fresh setup. During
the staged migration only, an older configuration may keep `engines.lmstudio`
enabled on loopback `:1234` so unconverted aliases remain usable. Keep it
loopback-only and disable JIT loading for direct clients. Do not remove that
explicit fallback or its credential until the local-library import and
packaged-service soak are accepted.

## Model storage and Hugging Face library

Use **Settings → Storage → Add Model Folder…** to choose the exact directory
that should hold managed downloads. This is a native directory picker, not a
path text field. A library may be the default internal directory or any nested
folder such as `/Volumes/Athena/models`; it does not have to be the root of an
external volume. Mnemosyne records both that exact directory and the containing
volume's UUID. A missing volume, non-writable directory, or different disk at
the same mount path is reported as unavailable and downloads fail closed.

While `NSOpenPanel` still owns the user's selection grant, the menu app creates
an ordinary bookmark and sends it to the loopback control API. Foundation's
implicit extension on that bookmark is Apple's supported single interprocess
handoff. The service explicitly starts the transferred grant, validates the
exact selected path, creates a receiver-owned durable security-scoped
bookmark, and stores only the durable bytes as a mode-`0600` file below
`state/security-scopes/` next to the active `config.yaml` (normally
`~/Library/Application Support/Mnemosyne/state/security-scopes/`). This stable
location does not move if `paths.state_database` changes. The durable
bookmark's SHA-256 becomes the `scope_id` written to YAML. Saving configuration
proves every referenced scope is present, current, path-matched, and capable of
fresh receiver-side activation before YAML is replaced. Bookmark receipt and
reactivation run in bounded, killable subprocess groups. Startup revalidates
configured bookmarks before coordinator initialization and prunes private
bookmarks no longer referenced by the persisted configuration.

The current app does not ship App Sandbox bookmark entitlements; do not treat
this handoff as entitlement-backed. A real protected-folder selection,
LaunchAgent restart, helper restart, and managed-child `exec` still need
acceptance smoke from the final installed bundle and signature.

Every scoped filesystem helper and manager-owned llama.cpp, DS4, MFLUX, or
download child reactivates the persisted bookmark in its own process before
`exec` preserves that process identity for the upstream command. Raw bookmark
data is never placed in YAML, SQLite, logs, or a control response.

Protected-folder and removable-volume operations can block inside macOS while
authorization or media is unavailable. Bookmark receipt/reactivation, storage
inspection, local scans, profile/path resolution, GGUF/projector header checks,
destination creation, and directory-size measurement therefore run in
separate, killable process groups off the asyncio event loop with bounded
deadlines. On timeout the service terminates the helper group and returns a
permission/volume diagnostic. Client cancellation and service shutdown use the
same process-group termination, so no blocked helper is stranded while the
inference and control planes remain responsive.

After saving a storage change, restart the background service when prompted.
The selected root is then organized by engine:

```text
<selected folder>/
  llama.cpp/<owner>/<repository>/
  omlx/<owner>/<repository>/
  ds4/<owner>/<repository>/
  mflux/<owner>/<repository>/
```

Use **Settings → Model Library** to choose llama.cpp, oMLX, DS4, or MFLUX,
search or pick a curated recommendation, choose one of the configured folders,
and download. llama.cpp search first returns GGUF repositories; a second GUI
step requires an exact quant/shard set and optionally an explicit
same-directory multimodal projector before Download is enabled. The resolved
Hub revision and exact file list are persisted so retries cannot silently
change weights.
DS4 results are restricted to the GGUF files published for DS4. MFLUX offers
the text-to-image configurations present in the pinned runtime: FLUX.1,
FLUX.2 Klein, Qwen Image, Krea 2 Turbo, FIBO, Z-Image, ERNIE Image, and
Ideogram 4. Krea 2 Raw is shown for completeness but cannot be installed: the
pinned upstream MFLUX loader currently accepts only Turbo's weight layout.
Edit, fill, depth, ControlNet, Redux, and restoration models stay hidden until
the unified API has request types for them. oMLX search is limited to MLX
repositories and labels Hub metadata checks as likely compatibility rather
than claiming runtime verification.

Use **Settings → Models → Add Existing Models…** to adopt an existing local
library without copying weights. The action always opens `NSOpenPanel`; the
user may select an exact nested folder such as `/Volumes/Athena/models`.
Unified Inference rescans the folder server-side, groups complete split GGUF
sets, excludes `mmproj` files as primary models, discovers MLX folders, and
returns opaque candidate IDs. Nothing is selected automatically. The user
chooses aliases and an explicit projector (or text-only) before import; the
service rescans and validates those IDs again, records the exact folder and
volume UUID, and atomically migrates matching legacy aliases and compatible
load settings. Discovery and import never load a model.

The ordinary Models editor does not accept raw model IDs or projector paths.
Engine, model source, storage, served name, projector, and image family are
read-only facts established by the library/import workflow. Routing is exposed
as a typed Generation, Embeddings, Rerank, or Image role, limited to the roles
the selected engine can serve.

Downloads run in killable child processes and persist queued, downloading,
registering, downloaded-but-not-registered, partial, cancelled, failed, and
installed states in SQLite. They can continue while the Settings window is
closed, be cancelled or retried from the GUI, and never make a model resident.
If profile registration fails after the weights land, Retry resumes only
registration without downloading the weights again. On completion Mnemosyne
creates the profile and, for oMLX, safely adds the selected library directory
through oMLX's admin API while the global residency coordinator has every
engine empty. Downloaded oMLX metadata is classified before registration so
the profile advertises only its detected generation, embeddings, or rerank
routes.

Set `HF_TOKEN` in the private environment file for gated or private Hub repos.
The token is write-only in Settings and is inherited only by the download
worker; it is not stored in YAML or SQLite.

## Engine runtime updates

Open **Settings → Runtime Updates** to inspect installed and upstream versions
of llama.cpp, oMLX, MFLUX, and DS4. oMLX owns its own installation: Unified
Inference detects the Homebrew CLI or running server version and links to the
official stable release, but never overwrites it. A legacy oMLX app
installation is still detected, although the documented setup uses Homebrew.

llama.cpp, MFLUX, and DS4 are resolved directly from their official upstreams;
there is no Unified Inference release manifest to maintain. MFLUX versions come from the
official PyPI project and are installed into an isolated managed package
directory. DS4 tracks the official `antirez/ds4` main commit, downloads that
exact source archive from GitHub, rejects unsafe archive paths, and builds
`ds4-server` locally with Apple's command-line toolchain. llama.cpp uses the
official macOS arm64 archive and rejects it unless its name, URL, size, SHA-256,
safe archive layout, executable, and required flags match the reported release.
MFLUX imports and the DS4 binary are also validated before activation. Download
and validation do not affect residency. The final switch runs inside the
coordinator's all-engines-empty maintenance barrier and atomically updates a
small pointer below:

```text
~/Library/Application Support/Mnemosyne/runtimes/
  llama.cpp/<build>/
  llama.cpp/current.json
  mflux/<version>/
  mflux/current.json
  ds4/<version>/
  ds4/current.json
```

The bundled MFLUX layer and configured external DS4 paths remain fallbacks.
The previous managed version is retained for rollback; model weights and model
storage are never moved or deleted. The managed MFLUX environment carries the
bundled unified worker and capability catalog; upstream dependency updates do
not require rebuilding the app. Unsupported checkpoints remain unavailable
until the unified worker and catalog both declare them.

For an installed app, leave `engines.mflux.python` unset. The service bootstrap
supplies the bundled image-layer Python and worker source unless an activated
managed MFLUX runtime supersedes them. A checkout `.venv` in that field, or
checkout-valued `MNEMOSYNE_MFLUX_PYTHON` /
`MNEMOSYNE_MFLUX_PYTHONPATH`, is a development override and must not be carried
into the packaged workstation configuration.

For development, `MNEMOSYNE_RUNTIME_ROOT` selects another managed-runtime
directory. Update metadata and code still come from the official projects.

## Developer setup

Create the user configuration without touching the CUDA compose directory:

```bash
mkdir -p "$HOME/Library/Application Support/Mnemosyne/state"
cp macos/config.yaml.example \
  "$HOME/Library/Application Support/Mnemosyne/config.yaml"
cp macos/.env.example \
  "$HOME/Library/Application Support/Mnemosyne/.env"
chmod 600 \
  "$HOME/Library/Application Support/Mnemosyne/config.yaml" \
  "$HOME/Library/Application Support/Mnemosyne/.env"
```

Edit the engine paths, model aliases, and enabled flags. Then install and run
the independently locked service environment:

```bash
uv sync --project macos/service --extra dev
uv run --project macos/service mnemosyne-macos serve \
  --config "$HOME/Library/Application Support/Mnemosyne/config.yaml" \
  --env "$HOME/Library/Application Support/Mnemosyne/.env"
```

For source-mode image development, sync the separately locked worker and point
the service at it before starting:

```bash
uv sync --project macos/image-worker --extra dev
export MNEMOSYNE_MFLUX_PYTHON="$PWD/macos/image-worker/.venv/bin/python"
export MNEMOSYNE_MFLUX_PYTHONPATH="$PWD/macos/image-worker/src"
```

The first startup applies the fail-closed `unload_all` policy. If an enabled
engine cannot report authoritative state, inference remains disabled while the
control plane reports a degraded diagnostic. Correct the engine configuration
and call `POST /manager/reconcile`.

Smoke the API with a configured alias:

```bash
curl http://127.0.0.1:1240/health
curl http://127.0.0.1:1240/v1/models
curl -X POST http://127.0.0.1:1240/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local-qwen","messages":[{"role":"user","content":"Hello"}]}'
curl http://127.0.0.1:17321/manager/status
```

With either example MFLUX profile enabled:

```bash
curl -sX POST http://127.0.0.1:1240/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"model":"krea-2-turbo","prompt":"A glass greenhouse in snowfall","size":"1024x1024","n":1,"response_format":"b64_json"}' \
  | jq -r '.data[0].b64_json' | base64 --decode > image.png
```

When `INFERENCE_API_KEY` is set, add `Authorization: Bearer ...` to `/v1/*`.
When the variable named by `server.control_password_env` (`ADMIN_PASSWORD` by
default) is set, authenticate to the control API as Basic user `admin`. A
non-loopback bind is rejected unless the corresponding credential exists.

## Menu bar app

For UI development:

```bash
cd macos/app
swift build
swift test
```

For a local staged application with its own relocatable Python runtime:

```bash
python3 macos/packaging/build_runtime.py
macos/packaging/build_app.sh release
open "macos/app/build/Stage/Unified Inference.app"
```

The build signs ad hoc when `CODESIGN_IDENTITY` is unset. To preserve a stable
code identity across local rebuilds, choose an identity visible in the login
keychain:

```bash
CODESIGN_IDENTITY="Apple Development: Example Name (TEAMID)" \
  macos/packaging/build_app.sh release
```

Move the app to a stable location such as `/Applications` before enabling its
background service. Theseus currently runs a Developer ID-signed installation
whose menu app and direct LaunchAgent helper share the same Team ID. Stable
builds use hardened runtime, secure timestamps, and inside-out nested signing.
Ad-hoc development builds remain supported, but rebuilding under a different
code identity may require protected model folders to be selected again. Use
`macos/packaging/build_dmg.sh` for a verified drag-to-install artifact; setting
`NOTARYTOOL_PROFILE` submits it to Apple and staples the accepted ticket.
See [packaging/README.md](packaging/README.md) for the bundle layout and build
details.

The app is intentionally menu-bar-only: it has no Dock icon or normal app
window. On launch it installs a square AppKit status item using the
brain-profile SF Symbol, with an `M` fallback if the symbol is unavailable.
Click that icon to open the SwiftUI controller popover, then choose **Enable
Service** to register the per-user LaunchAgent. The service and menu-login
item are independent: quitting the menu does not stop an enabled service.
Reopening an already-running `Unified Inference.app` brings the Settings window
forward, which provides a visible entry point even when the menu-bar icon is
hidden by a crowded display. The same window is shown once on first launch for
discoverability; subsequent login launches remain menu-bar-only.

Choose **Settings…** to open a dedicated native settings window. Its General,
Engines, Runtime Updates, Storage, Model Library, Models, Usage, and
Credentials pages expose ordinary toggles, folder/model pickers, and labeled
fields instead of YAML. The control service remains the schema authority:
`GET /manager/config` supplies normalized settings and
`PUT /manager/config` validates and atomically writes `config.yaml` with mode
`0600`. Each snapshot includes an optimistic content revision that the UI
returns on save. Downloads and imports mutate configuration under the same
lock, so a Settings window opened before one completes receives `409 Conflict`
instead of overwriting the new profile. The UI preserves `schema_version` and
refuses to save a schema newer than it understands. Model-only edits hot-apply;
engine, server, storage, token-sidecar, or credential edits offer a
background-service restart.

The Models page's **Add Existing Models…** action opens a directory picker and
uses `POST /manager/model-library/local-scan` plus
`POST /manager/model-library/imports`. `GET
/manager/model-library/local-sources` also reads LM Studio's
`~/.lmstudio/settings.json` `downloadsFolder` and offers that exact path, then
the documented `~/.lmstudio/models` default, as convenient Finder-backed scan
shortcuts. Source discovery neither requires the LM Studio engine nor contacts
its server. It does not inspect model weights: Finder still confirms the
folder and the bounded filesystem helper performs the scan. This preserves
nested and symlink paths used for external SSD libraries. Results are initially
unselected, show engine, quant, size, compatibility, exact path, and whether an
alias will be migrated, and offer explicit projector selection for multimodal
use. A newly adopted profile does not become resident until a client requests
its alias. The legacy LM Studio inventory endpoint remains only during the
migration soak and is not the primary import workflow.

The Storage page displays the exact selected directory, containing mount,
free space, and availability. Add/change actions always use `NSOpenPanel`, so
external libraries such as `/Volumes/Athena/models` are selected visually.
The Model Library page similarly presents compatible model results and storage
locations as GUI selections; raw repository or filesystem fields are not
required for managed installs.

Credential fields are write-only. The window shows only whether each supported
secret is configured, leaves its secure field blank, and allows explicit
replacement or removal. Unknown `.env` entries and comments are preserved and
the file remains mode `0600`; existing secret values are never loaded into
SwiftUI state or returned by the control API.

For an installed local build:

```bash
ditto "macos/app/build/Stage/Unified Inference.app" \
  "/Applications/Unified Inference.app"
open "/Applications/Unified Inference.app"
```

If the icon does not appear, confirm the installed process is running with
`pgrep -fl UnifiedInference`, look on the active display's menu bar, and check whether
a crowded MacBook menu bar has hidden status items around the notch. A
diagnostic terminal launch prints `Unified Inference menu bar status item installed`
after the explicit `NSStatusItem` is created:

```bash
"/Applications/Unified Inference.app/Contents/MacOS/UnifiedInference"
```

When launched from Finder, the menu reads the control bind, port, and password
environment-variable name from
`~/Library/Application Support/Mnemosyne/config.yaml`, then resolves the secret
from the private `.env`. Process environment values take precedence for
command-line development. `MNEMOSYNE_CONTROL_URL` is an explicit fixture
override; wildcard service binds are translated to a loopback connect address.

## Usage delivery

Every successful response with backend-provided usage is written to the local
SQLite `request_usage` table. Reporting defaults on; when
`token_sidecar.enabled` is true, the same
transaction adds a durable `pg_usage_outbox` row. A background writer retries
delivery to the existing `public.token_usage` Postgres ledger using stable
event IDs and `ON CONFLICT DO NOTHING`; a network outage does not discard the
local event.

The Postgres writer migrates the previous token sidecar's stable machine
identity when available and persists that identity plus the DSN into Unified
Inference's private `.env` before the legacy LaunchAgent is retired.
`token_sidecar.node_id` is only an explicit override; leave it empty to keep
Theseus, Metis, Athena, and other machines aligned during the transition.

Set or replace the secret DSN through **Settings → Usage → Postgres
connection**. The field is write-only: the app stores it in Unified
Inference's private mode-`0600` `.env`, reports only whether it is configured,
and requires a service restart to apply a replacement. Existing installations
inherit and persist it from the previous sidecar's LaunchAgent during
migration. The equivalent private-file form is:

```dotenv
TOKEN_SIDECAR_POSTGRES_DSN=postgresql://writer:password@server/token_sidecar
```

Inspect local delivery status through `/manager/status` or request rows through
`GET /manager/usage` on port `17321`.

Image-generation requests are deliberately not token-counted. The MFLUX worker
is process-isolated and terminated on model switches, explicit unload, request
timeout, or cancellation so Metal allocations are released with the process.

## Verification and design

Run the service suite independently of the CUDA tests:

```bash
uv run --project macos/service --extra dev python -m pytest macos/service/tests
uv run --project macos/image-worker --extra dev python -m pytest macos/image-worker/tests
cd macos/app && swift test
```

Native engine and LaunchAgent validation requires the target Mac. Follow
[smoke_checks.md](smoke_checks.md). The detailed ownership, adapter, lease,
security, and accounting decisions are recorded in
[../project_docs/macos_native_architecture.md](../project_docs/macos_native_architecture.md).
