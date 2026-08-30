# Mnemosyne for Apple Silicon

This is the native macOS sibling of the CUDA deployment. It exposes one stable
API while moving the single resident model between a manager-owned
[llama.cpp](https://github.com/ggml-org/llama.cpp) process for GGUF models,
[oMLX](https://github.com/jundot/omlx) for MLX models, and
[DwarfStar/DS4](https://github.com/antirez/ds4), plus a manager-owned
[MFLUX](https://github.com/filipstrand/mflux) image worker. The engines remain
upstream projects; Mnemosyne coordinates and proxies them without modifying
their model runtimes.

For a fresh workstation, begin with the
[end-user installation guide](INSTALL.md). It covers the Unified Inference
disk image, model storage, every native engine, legacy LM Studio model
adoption, and the recommended official oMLX app plus its headless Homebrew
alternative.

The runtime is deliberately not a Docker image. Docker Desktop runs ordinary
containers in a Linux VM, so it is not the right boundary for arbitrary
MLX/Metal processes. Mnemosyne Core and all engines run natively.

The [V1 scope](V1_SCOPE.md) makes llama.cpp and oMLX Stable and keeps DS4 and
MFLUX explicitly Preview. The [acceptance ledger](acceptance/v1.json) is the
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
All listeners default to loopback. Ports `17326` through `17329` remain reserved
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

Schema-v6 profiles can list exact engine alternatives for one public alias.
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
128K–1M.

Every language candidate also has an explicit context-window policy. **Automatic**
uses a fresh, content-free long-prefill profile for the exact model, runtime,
Mac, and suite; when evidence is missing or stale it keeps the configured safe
fallback. **Model native maximum** requests the detected training limit without
first proving the allocation fits, while **Explicit limit** always requests the
saved token count. For oMLX, Mnemosyne reads the effective limit from the
official model-status API and writes a requested limit through its official
per-model settings API before load, so oMLX's global 32K fallback does not
silently cap a configured model. **Profile usable context** delegates to
oMLX's memory-guard-aware native benchmark inside a global-empty maintenance
barrier; other engines test descending windows sequentially through coordinator leases. It persists only token counts,
fixed fingerprints, and timestamps—never the synthetic prompt or output. A
speed benchmark candidate cannot win if it would reduce the primary model's
guaranteed context. `GET /v1/models` exposes the selected value as
`max_model_len` plus a structured `context_window` explanation; the same
evidence is available from `GET /manager/contexts`.

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
- oMLX, DS4, and MFLUX are optional. An unavailable engine should be disabled;
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
| Exact DeepSeek V4 and GLM 5.2 GGUF layouts | DS4 Preview | Purpose-built upstream path retained only for models tested by the installed exact DS4 revision |
| GLM 5.3 Flash Q2/Q4_K GGUF layouts | DS4 Experimental Preview | An explicit opt-in resolves the official `antirez/ds4` `glm-5.3-flash` branch to an immutable commit and exposes only that source revision's exact official model contract |

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

The official DS4 repository now also has an unmerged `glm-5.3-flash` preview
branch with Q2/Q4 Metal support. Unified Inference deliberately does not infer
support from a model name or arbitrary GGUF. The explicit Experimental Preview
action first resolves that official branch to an immutable 40-character commit,
builds only that commit, and binds the managed runtime manifest to a digest of
its exact Q2/Q4 downloader contract. Only then does Model Library expose the
official `antirez/glm-5.3-flash-gguf` Q2 and Q4_K files; install rechecks a
40-character Hub revision and positive file sizes. Q2 has a conservative
128-GB floor and Q4_K a 256-GB floor. FP8 execution, vision, automatic SSD
streaming, CUDA, and cross-Mac tensor parallelism remain excluded. Local load
and Fleet advertisement fail closed if the active runtime or filename no
longer matches the recorded contract. A future signed compatibility recipe
can add narrower hardware/context evidence without weakening those checks.

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
handoff. The service first probes the exact path without a scope in a bounded
helper. When its deliberately unsandboxed process already has read/write
access, configuration stores only the exact path and volume identity; no
bookmark is retained or required on later starts. Startup applies the same
proof to older locations and atomically removes unnecessary stale `scope_id`
values, so ordinary model folders do not need to be selected again.

Only a path that fails the ordinary-access proof consumes the transferred
grant. The service explicitly starts that grant, validates the exact selected
path, creates a receiver-owned durable security-scoped bookmark, and stores
only the durable bytes as a mode-`0600` file below
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

For a location that genuinely requires a scope, every scoped filesystem helper
and manager-owned llama.cpp, DS4, MFLUX, or
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

The selected-model pane keeps runtime preparation next to the model choice. It
can install the verified managed llama.cpp or DS4 runtime, hand off to the
official oMLX DMG, stage an engine-enable setting, and show when a service
restart or health check is still required. DS4 main and the experimental GLM
5.3 source channel are never treated as interchangeable. If Apple's compiler
tools are missing, the app can open only Apple's fixed system installer after
confirmation and continues to report the prerequisite as unverified until a
later fixed toolchain/compiler probe succeeds. These actions are independent of weight download: the
chosen Download-to folder remains unchanged, downloads stay cold, and normal
lease-based JIT loading still begins with the first inference request.

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
**Delete Files** confirmation can also clean up a llama.cpp or oMLX model
previously imported from a registered folder. Cleanup drains residency and
freshly scans that exact storage grant. A unique imported match is moved to the
macOS Trash. A managed download can gain the same recoverable cleanup authority
only when the request names its exact installation UUID and complete immutable
evidence proves exclusive ownership, the current storage generation/path/
volume/scope, the absent-and-created directory identity, and the exact hashed
regular-file manifest. New managed downloads capture that proof only when the
destination was absent and this exact transaction created it; a pre-existing
destination, legacy/migrated row, unavailable proof helper, changed storage
binding, or ambiguous tree remains ownership-unknown and is retained. Bounded
helpers recheck directory identity, every file digest, and the complete tree
immediately before Trash and refuse roots, escapes, descendant symlinks,
special or extra entries, ambiguous matches, and files shared by any primary,
alternative, or projector consumer. oMLX cleanup also refreshes its
authoritative directory inventory inside the same all-engines-empty barrier.

Set `HF_TOKEN` in the private environment file for gated or private Hub repos.
The token is write-only in Settings and is inherited only by the download
worker; it is not stored in YAML or SQLite.

## Engine runtime updates

Open **Settings → Runtime Updates** to inspect installed and upstream versions
of llama.cpp, oMLX, MFLUX, and DS4. oMLX owns its own installation: Unified
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

### Qwen3.8 thinking controls

[Qwen3.8](https://huggingface.co/Qwen/Qwen3.8-27B) thinks by default. Its
model-native `reasoning_effort` values are `xhigh` (the default), `medium`, and
`low`. Unified Inference also accepts a portable `thinking_budget` token
ceiling and translates it after engine selection: oMLX receives
`thinking_budget`, while llama.cpp receives `reasoning_budget_tokens`. The
llama.cpp-native `thinking_budget_tokens` and `reasoning_budget_tokens`
spellings are accepted as input aliases, so an existing client can keep
working if the model is later pinned to oMLX.

```bash
curl -X POST http://127.0.0.1:1240/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "local-qwen38",
    "messages": [{"role": "user", "content": "Solve this carefully."}],
    "reasoning_effort": "medium",
    "thinking_budget": 8192,
    "enable_thinking": true,
    "preserve_thinking": true,
    "max_tokens": 12288,
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0
  }'
```

`reasoning_effort` and `thinking_budget` are independent: effort changes how
the Qwen chat template asks the model to reason, while the budget is a hard
engine-enforced ceiling. Keep the overall output limit above the thinking
budget so the model has room for its final answer. A very tight ceiling can
cut reasoning at an awkward boundary, so verify answer quality before making
one a client default.

The top-level `enable_thinking` and `preserve_thinking` convenience fields are
normalized into `chat_template_kwargs` for current oMLX and llama.cpp. Sending
the official self-hosted form directly is also supported:

```json
{
  "reasoning_effort": "low",
  "chat_template_kwargs": {
    "enable_thinking": true,
    "preserve_thinking": false
  }
}
```

For a direct non-thinking response, send `"enable_thinking": false`; Qwen
recommends `temperature=0.7`, `top_p=0.8`, `top_k=20`, `min_p=0.0`, and
`presence_penalty=1.5` for that mode. `/v1/responses` also maps
`reasoning.effort` into the Qwen template. Reasoning response fields are passed
through unchanged because current clients and engines may use either
`reasoning_content` or `reasoning`.

With either example MFLUX profile enabled:

```bash
curl -sX POST http://127.0.0.1:1240/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"model":"krea-2-turbo","prompt":"A glass greenhouse in snowfall","size":"1024x1024","n":1,"response_format":"b64_json"}' \
  | jq -r '.data[0].b64_json' | base64 --decode > image.png
```

The inference listener defaults to **This Mac only** (`127.0.0.1`). In
**Settings → General**, enable **Allow connections from the local network** to
persist `server.inference_bind: 0.0.0.0`; restart the background service to
apply the listener change. `0.0.0.0` means every reachable interface,
including LAN and VPN interfaces, so review the warning shown in Settings.

Inference authentication is optional on either bind. When
`INFERENCE_API_KEY` is set through **Settings → Credentials**, add
`Authorization: Bearer ...` to every `/v1/*` request. When it is absent,
`/v1/*` accepts unauthenticated requests, including from the local network.
The control listener remains on `127.0.0.1`; if it is deliberately configured
on a non-loopback address, the variable named by
`server.control_password_env` (`ADMIN_PASSWORD` by default) is still required
and clients authenticate as Basic user `admin`.

For enrollment in a Fleet Hub, set distinct `FLEET_API_KEY` and
`FLEET_INFERENCE_API_KEY` values in the private `.env`. The former protects
only snapshot reads; the latter is accepted only on `/v1/*` requests carrying
the Hub's canonical Fleet route marker. Existing static enrollments that omit the
new dispatch-only key retain their `INFERENCE_API_KEY` fallback, so upgrades
do not strand a configured Hub. The default inference bind is loopback-only
and cannot be reached from the Hub: change `server.inference_bind`
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

Then enroll `http://<trusted-mac-address>:1240` on the Hub and verify that it can
reach it. Never expose the inference listener on an untrusted LAN; bearer
credentials protect access but do not encrypt requests or responses.

Enrollment and participation are deliberately separate. Existing enrolled
Macs start joined for backward compatibility. The menu bar's **Contribute this
Mac to the pool** toggle changes a durable local preference without unpairing
the Mac, stopping local inference, unloading a model, cancelling downloads, or
changing any model-storage folder. Turning it off immediately closes new Fleet
admission, reports **Draining** while already admitted Fleet response streams
finish, and then reports **Paused**. A stale Fleet reservation is rejected
before model resolution or loading with `429`, `Retry-After: 1`, and
`X-Mnemosyne-Error: node_busy`; ordinary local requests remain callable.

Permanent removal is a different, destructive pairing action. For a dynamically
paired Mac, **Settings → Inference Pool → Remove this Mac from Hub** explicitly
confirms and revokes only the current Hub enrollment. It does not delete or
relocate model weights, change any exact configured storage folder, edit an
inference profile, stop local inference, or remove local analytics, token
history, or the durable usage outbox. A removed Mac cannot rejoin with the
participation toggle; it must complete a new Hub invitation ceremony. That
safe re-pair creates a new pairing while preserving the Mac's reporting
identity and per-device accounting continuity.

The loopback control API exposes the same state:

```bash
curl -s -u "admin:${ADMIN_PASSWORD}" \
  http://127.0.0.1:17321/manager/fleet/participation | jq

curl -s -u "admin:${ADMIN_PASSWORD}" \
  -X PUT http://127.0.0.1:17321/manager/fleet/participation \
  -H 'Content-Type: application/json' \
  -d '{"enabled":false}' | jq
```

The response contains only `enabled`, `state`, `active_requests`, and
`updated_at`. `X-Mnemosyne-Fleet-Route` is an internal canonical UUID marker
created by the Hub; local clients should not send it. Missing markers are local,
while malformed or duplicate markers fail closed.

The corresponding loopback removal route is
`POST /manager/fleet/pairing/revoke`. Its closed body is
`{"schema_version":1,"request_id":"<new-lowercase-canonical-uuid>"}`. The
service writes that exact request ID, pairing ID, and credential generation to
its durable journal before contacting the Hub, so new Fleet admission is denied
immediately and after restart. If the response is ambiguous, the optional
`self_revoke` field from `GET /manager/fleet/pairing` exposes only the secret-
free pending request ID and its fixed `pending` or `hub_committed` phase;
**Retry Removal** replays that exact ID. Once the Hub has committed, a retry
completes only local cleanup, without a second Hub call. Completion retains a
non-secret revoked tombstone and removes only the exact fingerprint-matching
pairing-owned snapshot, dispatch, and management credentials; changed or
static credentials are never guessed at or deleted.

A proven terminal Hub rejection retires that request ID before reopening the
unchanged pairing, and the old ID can never target a later generation.
Ambiguous transport, `429`, `5xx`, redirect, oversized, or malformed-success
outcomes remain fenced for exact-ID retry. After completion, a new invitation
transitions directly from the credential-free revoked tombstone; no hidden
clear operation can restore the old keys.

This uses the existing native control-plane authentication policy. When the
variable named by `server.control_password_env` (`ADMIN_PASSWORD` by default)
is set, callers must use Basic user `admin`. When it is unset on the default
loopback listener, the route relies on the existing same-user/local-process
trust boundary and has no separate pairing authorization token; the Settings
confirmation is a UI safeguard, not an authorization receipt. A non-loopback
control listener still refuses to start unless the password is configured.

The local inference and Fleet-dispatch bearers are deliberately not accepted
for the snapshot endpoint, and an unset snapshot credential makes it
unavailable. If Fleet discovery is enabled while both
`FLEET_INFERENCE_API_KEY` and the backward-compatible `INFERENCE_API_KEY`
fallback are empty, snapshot discovery fails closed with `503` and
`fleet_inference_auth_unconfigured`. The dispatch-only key never authorizes an
unmarked local request. Snapshots never expose secrets or local model paths.
Managed Hugging Face installs with an immutable resolved revision and exact
selected files are eligible for strict cross-node routing.
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

### Signed compatibility catalog

The optional `catalog` updater is disabled by default. When deliberately
enabled, it accepts only one exact canonical HTTPS origin/path and catalogs
signed by an Ed25519 public key pinned through the private `.env`. The
repository test key is never a release trust anchor. Catalog state lives in
`state/compatibility-catalog` beside the active YAML configuration; changing a
model-storage folder or the SQLite path cannot move it.

Startup first loads the still-valid signed last-known-good catalog, otherwise
it uses an empty offline catalog. Network, signature, expiry, rollback, and
private-state failures affect only advisory catalog status: local inference,
JIT loading, existing downloads, exact storage selections, and token
accounting continue unchanged. A successful activation asks the existing Mac
inventory publisher to send a fresh path-free observation. It does not edit a
profile, install or update a runtime, start a download, load a model, or move
weights. The authenticated no-store control surface is `GET /manager/catalog`,
`GET /manager/catalog/models`, and `POST /manager/catalog/check`; it never
returns the update endpoint, trust bytes, or private state path.

### Inference Pool and selected-Mac installs

Pool enrollment and local participation are separate. After this Mac is paired
once, **Settings → Inference Pool** can join or pause it without deleting its
pairing. Pause drains complete Fleet-routed streams and rejects only new
Fleet-marked work; local inference, model downloads, JIT policy, exact storage
folders, inference profiles, token history, and usage delivery continue. The
separately confirmed **Remove this Mac from Hub** action permanently revokes
that pairing and requires a new invitation; it still leaves all of those local
assets unchanged. The Hub can classify a lower-capability Mac
or its colocated worker as `overflow`, so it is considered only after primary and
opportunistic workers even when it is already warm.

The Mac publishes a path-free inventory containing hardware capability,
participation, opaque storage IDs/generations/free space, and authoritative
installed/cold/warm model identities. The Hub never receives a filesystem path,
volume mount, bookmark, scope, or credential. Its dashboard can show which Mac
has each model, explain hardware-aware placement, and let the administrator
choose one exact Mac and opaque storage location. That approval creates a
revisioned `DesiredInstall`; the chosen Mac independently revalidates its
pairing generation, service instance, signed catalog, recipe/artifact, storage
generation, capacity, and cancellation before using the ordinary durable
native downloader.

Remote selection does not change storage semantics. The destination is derived
only from the chosen Mac's existing exact lexical folder and engine-specific
layout; nested/symlink spelling, containing-volume UUID, receiver-owned scope,
per-install provenance, and ownership stay local and authoritative. There is no
fallback to a default directory, relocation, consolidation, or hidden copy.
Downloading does not load a model. Successful registration leaves it cold and
the existing coordinator performs JIT load, full-stream leasing, engine-local
batching, swaps, and idle unloading when a request arrives.

Signed GGUF artifacts can declare one exact primary file, its complete ordered
shard set, and one optional selected vision projector. The Mac downloads and
proves every declared member; missing, extra, mixed, duplicated, or ambiguous
layouts fail closed. Projectors are supplied only to llama.cpp and are never
treated as primary models; DS4 recipes cannot carry one.

For a signed managed oMLX recipe, Mnemosyne retains the immutable scheduler and
memory-guard launch contract in its hidden install journal. It uses the
authenticated official GET-only global-settings API before registration,
local/JIT load, benchmark work, and Fleet advertisement. It never changes
oMLX's service-global scheduler or memory guard as an install side effect. If
those settings drift, the model remains visible but is non-loadable,
zero-capacity, and Fleet-ineligible until the external service again proves the
signed contract; unrelated local models remain available.
The binding is recovered from hidden install history after restart. A later
install-journal read fault preserves the last exact signed/ordinary
classification, so a signed profile cannot silently become unconstrained and
an already-proved ordinary local profile is not needlessly fenced.

### Migration and removal

**Settings → Migration & Removal** offers previews for app-only removal,
state/runtime removal while retaining all weights, and full removal of only
freshly proven exclusive managed weights. The control API and UI expose only
fixed counts and component dispositions. Exact lexical paths remain solely in
a private mode-`0600` retention manifest below the native lifecycle state
directory. Imported/shared models are retained, an unavailable or changed
storage binding fails closed, and pending token-delivery rows block any mode
that would remove their durable state.

The current 0.9 surface can prepare a fresh, journal-only transaction; it does
not stop the service, unregister the LaunchAgent, remove the app, delete state,
or move weights. A primitive-free executor core models restart-safe observed
effects, durable rollback intent, a product-wide execution claim, exact Trash
authority, and manual recovery. An expired or abandoned claim is never stolen;
it blocks later lifecycle work until an authenticated recovery ceremony. The
menu now requests owner authorization only through the authenticated loopback
service, which is the direct peer allowed by the helper's sealed manifest and
launches only the bootstrap-pinned bundled helper over a one-shot socketpair.
That transport does not create proof authority: the production journal has no
per-install OS-backed verifier and the helper emits no receipt without one, so
authorization fails closed before launch and execution remains disabled.
Effects and recovery still require the signed helper/runner implementation and
the corresponding Developer ID/notarized real-Mac acceptance. This distinction
is a release boundary: a successful preview or prepare response is never
evidence that an uninstall or migration executed.
Pairing revoke is independently confirmed on the Inference Pool page and is
not triggered by migration/removal preview or preparation. Conversely, pairing
revoke cannot invoke model cleanup or lifecycle execution. Migration and every
retained-data removal mode preserve all local models, exact configured weight
paths, inference profiles, and token history unless a later executable flow
receives a separate, explicit, proven-exclusive model-deletion authorization.

## Usage delivery

Every successful response with backend-provided usage is written to the local
SQLite `request_usage` table. Reporting defaults on; when
`token_sidecar.enabled` is true, the same
transaction adds a durable `pg_usage_outbox` row. A background writer retries
delivery to the existing `public.token_usage` Postgres ledger using stable
event IDs and `ON CONFLICT DO NOTHING`; a network outage does not discard the
local event. Fleet-routed requests reuse Fleet's authenticated route UUID as
that stable event ID, so route history and the serving Mac's ledger row can be
correlated without storing request content. If the configured durable outbox
cap is reached, new language inference fails before model loading with the
fixed `usage_outbox_full` condition; Mnemosyne never makes room by deleting an
undelivered event. Image generation is unchanged because it does not emit
token-usage events.

Fleet dispatch additionally reserves its authenticated route UUID before JIT
or coordinator admission. The reservation and optional final outbox slot are
one SQLite `BEGIN IMMEDIATE` decision, so overlapping service processes cannot
both accept the last slot or execute a replayed route. Pre-work failures release
the reservation; once request bytes may have reached an engine, a content-free
replay fence remains. Accounted language success consumes the reservation into
the durable analytics/outbox event. A non-streaming Fleet 2xx response without
normalized usage is withheld and becomes a fixed 502 `usage_missing` response.
For live SSE, content events may already be visible; Mnemosyne withholds the
recognized success terminal and aborts the body with internal `usage_missing`
instead of pretending it can rewrite the already-sent HTTP status. Neither
path invents a zero-count event, and both retain the route replay fence.
Standalone missing-usage compatibility is unchanged. Completed no-usage/image/
error fences retain only the newest 10,000 rows; active work and durable
usage/outbox evidence are never pruned by that bound.

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
