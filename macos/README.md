# Mnemosyne for Apple Silicon

This is the native macOS sibling of the CUDA deployment. It exposes one stable
API while moving the single resident model between a manager-owned
[llama.cpp](https://github.com/ggml-org/llama.cpp) process for GGUF models,
[oMLX](https://github.com/jundot/omlx) for MLX models, and
[DwarfStar/DS4](https://github.com/antirez/ds4), plus a manager-owned
[MFLUX](https://github.com/filipstrand/mflux) image worker. Preview adapters
also support [mlxcel](https://github.com/lablup/mlxcel) for native MLX
generation/VLM serving and [mistral.rs](https://github.com/EricLBuehler/mistral.rs)
for pinned Safetensors models. The engines remain upstream projects; Mnemosyne
coordinates and proxies them without modifying their model runtimes.

For a fresh workstation, begin with the
[end-user installation guide](INSTALL.md). It covers the Unified Inference
disk image, model storage, every native engine, legacy LM Studio model
adoption, and the recommended official oMLX app plus its headless Homebrew
alternative.

The runtime is deliberately not a Docker image. Docker Desktop runs ordinary
containers in a Linux VM, so it is not the right boundary for arbitrary
MLX/Metal processes. Mnemosyne Core and all engines run natively.

The [V1 scope](V1_SCOPE.md) makes llama.cpp and oMLX Stable and keeps DS4,
MFLUX, mlxcel, and mistral.rs explicitly Preview. The [acceptance ledger](acceptance/v1.json) is the
release truth; a 0.9 candidate is not V1 while any required gate remains
pending. See [release and recovery](RELEASE.md) for versioning, signing,
notarization, signed updates, and rollback.

## Current validation

The current 0.9.0 source passes the native service, image-worker, Swift, and
packaging suites. A full relocatable ad-hoc app and private DMG have also been
built and structurally reverified. They are development artifacts, not the
Developer ID/notarized V1 distribution.

Earlier hardware and packaging smokes were exercised on an M4 Max. A real
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
CUDA parity remain separate smoke gates. An earlier Developer ID-signed bundle
on Theseus proved that its direct
`Contents/MacOS/mnemosyne-service-bootstrap` LaunchAgent could run through
`SMAppService` and answer both native HTTP planes. That historical smoke does
not substitute for signing, notarizing, and exercising the exact current
candidate. LM Studio is not an inference engine or API dependency; only its
on-disk model directory remains as a migration hint.

## Ports and processes

| Port | Process | Role |
| ---: | --- | --- |
| `1240` | Mnemosyne Core | Unified OpenAI/Anthropic-compatible inference; drop-in replacement for the previous token sidecar |
| `17321` | Mnemosyne Core | Control API used by the menu bar app |
| `17322` | oMLX | Native MLX inference and admin API |
| `17323` | `ds4-server` | Mnemosyne-owned model process |
| `17324` | MFLUX worker | Mnemosyne-owned image process |
| `17325` | `llama-server` | Unified Inference-owned GGUF process |
| `17326` | `mlxcel-server` | Unified Inference-owned Preview MLX model process |
| `17327` | `mistralrs serve` | Unified Inference-owned Preview Safetensors model process |

All listeners default to loopback. Ports `17328` and `17329` remain reserved
for future local engines and diagnostics.

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

Same-model requests may run concurrently while holding leases on the same
resident epoch. The service derives a per-profile limit from the engine
contract (including oMLX's authoritative
`scheduler.max_concurrent_requests`, llama.cpp `load.parallel`, and MFLUX's
serial worker), then applies the optional global `server.max_concurrency`
ceiling. If an external scheduler does not expose a valid limit, admission
falls back to one request instead of guessing. Once those
permits are occupied, up to `server.max_queue_depth` waiters are retained in
FIFO order; another request is rejected before upstream inference with
`429 node_busy`, `Retry-After`, and the manager-owned
`X-Mnemosyne-Error: node_busy` proof header. A queued model switch closes
old-target admission, drains all active streams, and preserves the one-resident
invariant.

Schema-v5 profiles can list exact engine alternatives for one public alias.
The existing profile is always the fallback. The user can choose **Pinned
engine** to prefer a declared engine regardless of benchmark rank, which is
useful when the fastest candidate has quality or compatibility problems. If
the pin is unavailable or cannot load before inference begins, the original
profile handles the untouched request. Choosing **Best fresh benchmark** makes
selection automatic; **Benchmark compatible engines** then runs each
candidate sequentially through ordinary coordinator leases, including one
warmup and repeated streamed samples. It records only success counts, TTFT,
end-to-end latency, output tokens/sec, and hashed model/runtime/system/config
identities—never prompts, generated text, credentials, arbitrary diagnostics,
or unhashed local paths. Evidence becomes ineligible after this alias's
candidate/model/load change, engine binary/version change, Mac/OS change, suite change, or configured
age limit. Preview engines cannot win without explicit consent, and an absent,
stale, failed, or marginal result keeps the fallback engine. If a selected
alternative cannot load before upstream work begins, that request recovers
through the fixed engine and invalidates the evidence. An ambiguous transport
or upstream 5xx is never replayed, but invalidates an automatically selected
alternative for the next request. A user pin persists until changed. The suite
ranks stability and performance, not answer quality;
attaching an alternative explicitly asserts that both profiles represent the
same logical model and role.

Fresh configurations keep the verified resident model warm. Settings provides
Performance, Balanced, and Memory Saver residency presets; custom values may
cap concurrency or unload after a chosen idle interval. Newly discovered GGUF
profiles start with an interactive context no larger than 64K (32K when no
metadata is available) rather than blindly allocating a model-card maximum of
128K–1M. An explicitly saved context remains authoritative.

The service keeps a bounded, in-memory, content-free performance window. The
menu shows rolling p50/p95 latency and streamed output tokens/second for the
resident alias; `GET /manager/performance` exposes admission, upstream-header,
first-byte, total-latency, cold-start, and error aggregates. No prompt,
response, API key, or arbitrary diagnostic text is retained in this window.

For a repeatable comparison against another OpenAI-compatible Mac endpoint:

```bash
uv run --project macos/service python macos/scripts/benchmark_native.py \
  --model your-alias --requests 8 --concurrency 4 \
  --compare-base-url http://127.0.0.1:1234 --compare-label lm-studio
```

The benchmark uses one fixed prompt and records only status, latency, usage,
and throughput. Omit `--compare-base-url` to measure Unified Inference alone.

## Requirements

- Apple Silicon and macOS 15 or newer.
- Python 3.11–3.13 and `uv` for service development.
- Swift 6 for menu development. Full Xcode is required for final app signing,
  `SMAppService` integration testing, and source builds of custom Metal kernels.
- oMLX, DS4, MFLUX, mlxcel, and mistral.rs are optional. An unavailable engine should be disabled;
  its profiles are retained but omitted from the callable model catalog until
  the engine is enabled again.

The supported default path for oMLX is its official macOS app, selected from
**Runtime Updates**. Its DMG includes precompiled custom kernels and its own
one-click updater. A stable headless Homebrew installation is available
through an approval-gated button that shows and runs only the official tap and
stable install commands; the fragile `--HEAD --with-custom-kernel` source
build is advanced-only. See
[Install the official oMLX app](INSTALL.md#5-install-the-official-omlx-app)
for the guided setup and headless alternative.

## Engine strategy

Unified Inference intentionally remains an adapter layer, not a fifth model
runtime. The current upstream split is:

| Model artifact | Default engine | Reason |
| --- | --- | --- |
| MLX directories | oMLX | Continuous batching, tiered persistent KV cache, multi-model admin lifecycle, native app, and a stable Homebrew service |
| GGUF | llama.cpp | Official Metal-enabled server, broad quant/model support, continuous batching, and explicit parallel slots |
| Curated image checkpoints | MFLUX | Native Apple Silicon image pipeline isolated from language dependencies |
| Exact DeepSeek V4 and GLM 5.2 GGUF layouts | DS4 Preview | Purpose-built upstream path retained only for models tested by DS4 |

The official
[`mlx_lm.server`](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/server.py)
now includes batched generation and prompt-cache
support, but adopting it directly would make Unified Inference own another
Python runtime, server lifecycle, and upgrade surface while giving up oMLX's
admin contract and vendor app. The current evidence does not justify that
trade. The deliberate choice is therefore to keep oMLX replaceable behind its
adapter, fix admission and ownership friction, and use the included benchmark
before changing engines on measured hardware.

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

Install the official oMLX app from **Runtime Updates**, configure its loopback
server on port `17322`, and let its app own future updates. The release DMG
includes the native kernels used by GLM-5.2 and related families. A stable
headless Homebrew path and the advanced source-build verification are in
[INSTALL.md](INSTALL.md#5-install-the-official-omlx-app).

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

DS4 is purpose-built for the exact DeepSeek V4 and GLM 5.2 GGUF layouts listed
by its official downloader; it is not a general GGUF runner. The current
library exposes four DeepSeek V4 Flash variants, one DeepSeek V4 Pro variant,
and four GLM 5.2 variants. The Unsloth GLM Q4 choice is one atomic eleven-shard
install. Search verifies every expected file and pins the Hugging Face revision
before a download can start, and a managed runtime must declare the selected
target. Runtime Updates can fetch an exact official `antirez/ds4` commit and
build `ds4-server` locally with Apple's toolchain. An
explicitly configured external checkout/binary remains available as a
development or recovery fallback. Mnemosyne starts DS4 with an explicit model,
context, host, and port and terminates only a process whose recorded identity
it can prove it owns. DS4 profiles can set **Resident request sessions** to
translate into the upstream `--batched-session` setting. Admission uses that
exact slot count; leaving it unset preserves the safest one-session memory
profile because every extra session allocates another KV state.

An existing LM Studio installation is not required. Version-1 configurations
are upgraded without starting or contacting LM Studio: legacy LM Studio
profiles become inert alias/load-setting records and disappear from the
callable catalog until their weights are imported into llama.cpp or oMLX.

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
  mlxcel/<owner>/<repository>/
  mistral.rs/<owner>/<repository>/
```

Use **Settings → Model Library** to search one unified catalog across all
enabled and Preview-capable engines, choose one of the configured folders, and download. Every
result carries an engine-support badge; selecting it retains the exact
engine-specific validation and install path. Search results and details show
bounded model-card prose plus
detected architecture, context length, parameter count, and license when the
Hub repository, config, or GGUF provides them. Model cards remove Hugging Face
YAML front matter and render safe Markdown headings, lists, quotes, code, links,
and paragraphs in a dedicated scrollable surface. llama.cpp results first return
GGUF repositories; a second GUI step requires an exact quant/shard set. When a
same-directory vision projector exists, the highest-fidelity option is selected
automatically; the user can choose another or opt out for text-only use. The
resolved Hub revision and exact file list are persisted so retries cannot
silently change weights.
DS4 results are restricted to the exact current DeepSeek V4 and GLM 5.2 GGUF
files published and tested for DS4. They are hydrated from Hugging Face file
metadata so missing files, incomplete shard groups, or an unresolvable revision
are unavailable rather than installable. Auxiliary DSpark weights and
distributed-only DeepSeek Pro halves are intentionally not presented as
standalone models. MFLUX offers the text-to-image configurations present in
the pinned runtime: FLUX.1,
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
returns opaque candidate IDs. Models remain an explicit import choice, while
the highest-fidelity same-directory projector is preselected for each vision
candidate. The user chooses aliases and can select another projector or opt
out for text-only use; the service rescans and validates those IDs again,
records detected metadata plus the exact folder and volume UUID, and atomically
migrates matching legacy aliases and compatible load settings. Discovery and
import never load a model.

The ordinary Models editor does not accept raw model IDs or projector paths.
Engine, model source, storage, served name, projector, and image family are
read-only facts established by the library/import workflow. Routing is exposed
as a typed Generation, Embeddings, Rerank, or Image role, limited to the roles
the selected engine can serve.

New llama.cpp Generation profiles use the portable fleet contract:
Chat Completions, Completions, and Responses. The native Messages route remains
available when a hand-authored llama.cpp profile explicitly includes the
`messages` capability. oMLX and DS4 keep their existing Generation-with-Messages
defaults.

Downloads run in killable child processes and persist queued, downloading,
registering, downloaded-but-not-registered, partial, cancelled, failed, and
installed states in SQLite. They can continue while the Settings window is
closed, be cancelled or retried from the GUI, and never make a model resident.
The GUI shows transferred/total bytes, percentage, a progress bar, and smoothed
live transfer speed. Completed entries can be hidden from recent history
without discarding the managed-download identity used for safe maintenance.
If profile registration fails after the weights land, Retry resumes only
registration without downloading the weights again. On completion Mnemosyne
creates the profile and, for oMLX, safely adds the selected library directory
through oMLX's admin API while the global residency coordinator has every
engine empty. Downloaded oMLX metadata is classified before registration so
the profile advertises only its detected generation, embeddings, or rerank
routes.

Removing a model profile keeps its files by default. The separate
**Delete Files** confirmation is available only for a completed download owned
by Unified Inference; Finder imports and hand-authored paths are never deleted.
Deletion drains residency, revalidates the exact configured storage and managed
destination, removes it in a bounded helper that refuses roots, escapes, and
symlinks, then atomically removes the profile. oMLX deletion also refreshes its
authoritative directory inventory inside the same all-engines-empty barrier.

Set `HF_TOKEN` in the private environment file for gated or private Hub repos.
The token is write-only in Settings and is inherited only by the download
worker; it is not stored in YAML or SQLite.

## Engine runtime updates

Open **Settings → Runtime Updates** to inspect installed and upstream versions
of llama.cpp, oMLX, MFLUX, DS4, mlxcel, and mistral.rs. oMLX owns its own installation: Unified
Inference selects the official DMG matching this Mac, detects the installed
app, CLI shim, conventional Homebrew paths, or running server, and links to
the official stable release without overwriting it. For a missing runtime, an
explicitly confirmed action may delegate the initial stable installation to
Homebrew. A detected stable Homebrew installation also gets a supervised
update action: Unified Inference drains inference and then delegates the fixed
`omlx stop`, `brew update`, `brew upgrade omlx`, and `omlx start` sequence to
the owner before validating an authoritative empty control plane. Official-app
updates stay in the app, and Homebrew HEAD builds are offered a one-time stable
migration instead of an unreproducible rebuild.

The same card reports oMLX's vendor-provided SSD prompt-cache size and reuse
metrics. A reset is never automatic. The explicitly confirmed reset drains all
engines and calls oMLX's official cache-clear API; model weights and settings
are untouched.

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

mlxcel and mistral.rs remain externally installed Preview binaries. The menu
app detects their configured executables without claiming ownership of the
installation: use the official `lablup/tap` Homebrew formula for mlxcel and
the official mistral.rs installer plus `mistralrs update` for mistral.rs.
Unified Inference owns only the exact child server process and requires Model
Library to pin and download weights before load, so neither engine performs an
implicit Hub download in the inference path.

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

For enrollment in the Nyx fleet gateway, set distinct `FLEET_API_KEY` and
`INFERENCE_API_KEY` values in the private `.env`. The default inference bind
is loopback-only and cannot be reached from Nyx: change `server.inference_bind`
to this Mac's trusted LAN or Tailscale address, restrict that address with the
host firewall or Tailscale ACLs, keep `server.control_bind` on loopback, and
restart Mnemosyne Core. The read-only `GET /fleet/v1/snapshot` endpoint then
exposes the versioned node identity, health, residency transition, bounded
queue, derived/configured concurrency, strict deployments, and usage-delivery
health. Verify locally first:

```bash
curl -s http://127.0.0.1:1240/fleet/v1/snapshot \
  -H "Authorization: Bearer $FLEET_API_KEY" | jq
```

Then enroll `http://<trusted-mac-address>:1240` on Nyx and verify that Nyx can
reach it. Never expose the inference listener on an untrusted LAN; bearer
credentials protect access but do not encrypt requests or responses.

The inference bearer is deliberately not accepted for this endpoint, and an
unset fleet credential makes it unavailable. If Fleet discovery is enabled
while `INFERENCE_API_KEY` is empty, snapshot discovery fails closed with `503`
and `fleet_inference_auth_unconfigured`. Snapshots never expose secrets or
local model paths. Managed Hugging Face installs with an immutable resolved
revision and exact selected files are eligible for strict cross-node routing.
Finder imports, hand-authored paths, symbolic revisions, and unverifiable
legacy installs remain visible only as node-scoped `unverified` deployments
and cannot be grouped automatically.

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
background service. An earlier Theseus installation proved that the menu app
and direct LaunchAgent helper can run with the same Developer ID Team ID.
Production builds use hardened runtime, secure timestamps, and inside-out
nested signing.
Ad-hoc development builds remain supported, but rebuilding under a different
code identity may require protected model folders to be selected again. Use
`macos/packaging/build_dmg.sh` for a verified drag-to-install artifact; setting
`NOTARYTOOL_PROFILE` submits it to Apple and staples the accepted ticket.
See [packaging/README.md](packaging/README.md) for the bundle layout and local
build details, and [RELEASE.md](RELEASE.md) for the production pipeline.

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

Choose **Settings…** to open a dedicated native settings window. **Setup &
Health** guides service registration, Stable and Preview engine readiness,
storage, model setup, and reporting. Its real self-test uses the public
listener and verifies the durable local usage row before first-run setup is
marked complete. General, Engines, Runtime Updates, Storage, Model Library,
Models, Usage, and Credentials expose ordinary toggles, folder/model pickers,
and labeled fields instead of YAML. The control service remains the schema
authority:
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
its server, load an adapter, or require an API credential. It does not inspect model weights: Finder still confirms the
folder and the bounded filesystem helper performs the scan. This preserves
nested and symlink paths used for external SSD libraries. Results are initially
unselected, show engine, quant, size, compatibility, detected model metadata,
exact path, and whether an alias will be migrated. Vision candidates
automatically select a projector while retaining manual and text-only choices.
A newly adopted profile does not become resident until a client requests its
alias.

The Storage page displays the exact selected directory, containing mount,
free space, and availability. Add/change actions always use `NSOpenPanel`, so
external libraries such as `/Volumes/Athena/models` are selected visually.
The Model Library page similarly presents a unified cross-engine result list
with explicit engine-support badges and storage locations as GUI selections;
raw repository or filesystem fields are not required for managed installs.

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
leaves it unchanged when the editor is blank, and requires an explicit
**Clear** action to remove it. A service restart applies a replacement.
Existing installations inherit and persist it from the previous sidecar's
LaunchAgent during migration. The equivalent private-file form is:

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
