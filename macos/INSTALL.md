# Install Unified Inference on Apple Silicon

This is the normal end-user installation path for a Mac. It installs
`Unified Inference.app`, prepares every supported native engine, and leaves
clients with one OpenAI-compatible endpoint:

```text
http://127.0.0.1:1240/v1
```

Docker and LM Studio are not required. Only oMLX is installed separately,
through its official app or Homebrew.
llama.cpp, DS4, and MFLUX are installed or updated from Unified Inference.

The current 0.9 line is a release candidate. Use the private DMG for local
testing, but do not treat it as the V1 distribution until the
[acceptance ledger](acceptance/v1.json) is clear and a Developer ID-notarized
artifact is published. The exact Stable/Preview contract is in
[V1_SCOPE.md](V1_SCOPE.md).

## What gets installed

| Engine | V1 tier | Model format or role | Normal installation | Private port |
| --- | --- | --- | --- | ---: |
| llama.cpp | Stable | GGUF language and vision models | **Settings → Runtime Updates** | `17325` |
| oMLX | Stable | MLX generation, embedding, and rerank models | Official oMLX app (recommended) | `17322` |
| DS4 | Preview | Supported DeepSeek V4 and GLM 5.2 layouts | **Settings → Runtime Updates** | `17323` |
| MFLUX | Preview | Apple Silicon image generation | Bundled; updates in **Runtime Updates** | `17324` |

Clients never call those private ports. Unified Inference owns model selection,
global residency, proxying, and language-token accounting on port `1240`.

## 1. Prepare the Mac

You need:

- An Apple Silicon Mac running macOS 15 or newer.
- Apple Command Line Tools for DS4. The recommended official oMLX app includes
  its custom Metal kernels and does not require a local kernel build.
- Optional: [Homebrew](https://brew.sh/) for a headless oMLX installation.
  Full Xcode is needed only for the advanced Homebrew HEAD custom-kernel build.
- Enough internal or external storage for model weights.

Install DS4's command-line build prerequisite with:

```bash
xcode-select --install
xcode-select --print-path
```

## 2. Install Unified Inference

Open the Unified Inference disk image, drag **Unified Inference** to the
**Applications** shortcut, eject the image, and launch:

```bash
open "/Applications/Unified Inference.app"
```

The same signed arm64 disk image can be copied to every Apple Silicon Mac; it
does not need to be rebuilt on Metis, Athena, or another workstation. Engine
runtimes and model folders are prepared separately on each machine.

For a private, non-notarized local build, use Finder's **Control-click →
Open** once if Gatekeeper asks. Do not disable Gatekeeper system-wide.

Unified Inference is a menu-bar app, so it intentionally has no Dock icon.
Open its menu-bar item and choose **Settings → Setup & Health**. Enable the
background service, then approve the background item in **System Settings →
General → Login Items & Extensions** if macOS requests it. Setup & Health
shows actionable core, engine, storage, model, download, and reporting state;
use **Reconcile Engines**, **Restart Service**, or **Open Logs** when a check
is degraded.

The first-run flow remains incomplete until a configured model passes **Run
Self-Test**. That sends a real request through the public listener, uses a
matching vision projector automatically when the selected llama.cpp profile
has one, and verifies the durable local usage row. Before installing a model,
these commands provide a basic core check:

```bash
curl --fail http://127.0.0.1:1240/health
curl --fail http://127.0.0.1:17321/manager/status
```

If this Mac still has the old token sidecar, retire it before enabling the new
service on port `1240`:

```bash
macos/retire_legacy_sidecar.sh
```

That command is run from a checkout of this repository. It migrates the
reporting identity and ledger configuration before persistently disabling the
old LaunchAgent; it does not kill an arbitrary process using the port.

## 3. Choose model storage and credentials

In **Settings → Storage**, choose **Add Model Folder…** and select the exact
folder to use. Nested external paths such as `/Volumes/Metis/models` or
`/Volumes/Athena/models` are supported; do not select the volume root unless
that is genuinely where the models belong.

In **Settings → Credentials**, add a Hugging Face token when you want faster
downloads or need a gated model. Credential values are write-only and are not
stored in `config.yaml`.

In **Settings → Usage → Postgres connection**, add or replace the central
ledger connection URL. The host, port, database, username, and password are
carried by this write-only DSN:

```text
postgresql://user:password@host:5432/database
```

The saved value is never displayed again. Leaving the secure field blank keeps
the current connection; only the explicit **Clear** action removes it. Save the
settings and restart the background service before testing a replacement.

Model weights are separate from engine runtimes. Install or import weights
through **Settings → Model Library** after preparing the corresponding engine.

## 4. Install llama.cpp

No Homebrew llama.cpp installation is needed.

1. Open **Settings → Runtime Updates**.
2. Choose **Check Now**.
3. Install the available llama.cpp runtime.
4. In **Settings → Engines**, leave llama.cpp enabled.
5. Use **Model Library → llama.cpp** to choose a GGUF repository and exact
   quant/shard set. A detected vision projector is selected automatically; you
   can choose another or opt out for text-only use.

Unified Inference downloads the official
[ggml-org/llama.cpp release](https://github.com/ggml-org/llama.cpp/releases/latest),
verifies its published metadata and server contract, and owns the resulting
`llama-server` process. Do not start a second `llama-server` on port `17325`.

## 5. Install the official oMLX app

The official oMLX app is the normal installation. It ships precompiled custom
kernels, avoids a local CMake/Xcode build, and owns its own one-click updates.

1. Open **Settings → Runtime Updates** and choose **Check Now**.
2. In the oMLX card, choose **Download oMLX**. Unified Inference selects the
   official DMG for this Mac's macOS major version.
3. Open the downloaded DMG, drag **oMLX** to **Applications**, and launch it.
4. Complete oMLX's welcome flow. Configure the server for host `127.0.0.1`
   and port `17322`, choose the model folder you want oMLX to scan, and start
   its server.
5. Return to Unified Inference, choose **Check Again**, then enable oMLX under
   **Settings → Engines**.

Leave oMLX model pinning and per-model TTL/LRU behavior disabled because
Unified Inference owns the one-model residency policy. The oMLX app remains an
independently installed engine; Unified Inference does not overwrite or
silently update it. Do not also start a Homebrew oMLX service.

The official app installs a lightweight CLI shim at
`~/.omlx/bin/omlx`. This is useful for diagnostics:

```bash
"$HOME/.omlx/bin/omlx" restart
curl --fail http://127.0.0.1:17322/v1/models
```

No oMLX API key or admin session is needed for this loopback-only default. If
you deliberately enable oMLX authentication, save the corresponding
`OMLX_API_KEY` and `OMLX_ADMIN_SESSION` values through Unified Inference's
Credentials page.

### Headless Homebrew alternative

For a Mac that should not run the oMLX menu app, install the current stable
formula—not the source-tracking HEAD build:

1. In the oMLX Runtime Updates card, choose **Install with Homebrew…**.
2. Review the exact `brew tap` and `brew install` commands in the confirmation
   dialog, then approve them.
3. Wait for the app to re-check the detected oMLX version.

Unified Inference delegates these fixed commands to the user's existing
Homebrew installation. It does not request `sudo`, use `--HEAD`, update an
existing oMLX installation, or accept arbitrary formula arguments. The same
steps can be run manually:

```bash
brew tap jundot/omlx https://github.com/jundot/omlx
brew install omlx
```

Configure oMLX once from the CLI. Use the same exact model-library folder you
selected in Unified Inference; the example is intentionally a nested
external-drive path:

```bash
omlx serve \
  --host 127.0.0.1 \
  --port 17322 \
  --model-dir "/Volumes/Metis/models"
```

The explicit arguments are validated and saved to `~/.omlx/settings.json`.
After the server reports that it is listening, press **Control-C**, then start
the persistent Homebrew service:

```bash
omlx start
brew services info omlx
curl --fail http://127.0.0.1:17322/v1/models
```

On another Mac, replace only the model folder; keep the host and port
unchanged. A fresh CLI installation has no pinned models.

Finally open **Settings → Engines**, enable oMLX, and leave its local API
address at `http://127.0.0.1:17322`.

### Advanced source-built custom kernels

Only use this path when you deliberately require a headless Homebrew service
for GLM-5.2, MiniMax M3, or Qwen3.5/3.6 and cannot use the recommended app.
It follows oMLX main, requires full Xcode and its Metal toolchain, and can fail
when an upstream native extension does not build on a new macOS/Xcode version:

```bash
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
xcrun --find metal
brew install omlx --HEAD --with-custom-kernel
```

Verify that the resulting Homebrew runtime has its native kernels:

```bash
"$(brew --prefix omlx)/libexec/bin/python" -c \
  'from omlx.custom_kernels import native_kernel_status; print(native_kernel_status())'
```

If this build fails in CMake, use the official oMLX app instead. Its release
DMG already contains the compiled kernels and does not depend on the local
source-build toolchain.

Official references:

- [Official oMLX app and Homebrew installation](https://github.com/jundot/omlx#install)
- [oMLX CLI configuration](https://github.com/jundot/omlx#cli-configuration)
- [Official oMLX releases](https://github.com/jundot/omlx/releases)

## 6. Install DS4

DS4 is specialized; it is not a second general-purpose GGUF engine.

1. Confirm `xcode-select --print-path` succeeds.
2. Open **Settings → Runtime Updates** and choose **Check Now**.
3. Install DS4, then leave it enabled under **Settings → Engines**.
4. Use **Model Library → DS4** to install one of the exact supported model
   layouts.

Unified Inference downloads an exact commit from the official
[antirez/ds4 repository](https://github.com/antirez/ds4), builds
`ds4-server` locally with Apple's toolchain, validates it, and owns its process
on port `17323`. Do not manually clone DS4 or place a second server in
`/Applications/DwarfStar` for a normal installation.

Large DS4 targets have substantial unified-memory and SSD-streaming
requirements. Check the selected model's size and the upstream DS4 notes
before downloading it.

## 7. Prepare MFLUX

Do not install MFLUX globally with `pip` or `uv tool` for the packaged app.
Unified Inference includes an isolated MFLUX worker and can update its upstream
package independently:

1. Open **Settings → Runtime Updates**, choose **Check Now**, and install an
   available MFLUX update.
2. Leave MFLUX enabled under **Settings → Engines**.
3. Choose a verified image checkpoint and storage location under
   **Settings → Model Library → MFLUX**.

Supported image profiles are exposed through
`POST /v1/images/generations`. Image requests deliberately do not create
token-usage records.

The standalone upstream project is
[filipstrand/mflux](https://github.com/filipstrand/mflux), but its global CLI
environment is not used by Unified Inference.

## 8. Import an existing LM Studio library

LM Studio is not required and is never used as an inference engine. To reuse
weights from an older installation:

1. Open **Settings → Models → Add Existing Models…**.
2. Choose the exact LM Studio model directory offered by the source hint, or
   select it in Finder.
3. Review the detected GGUF and MLX candidates and import only the wanted
   models.
4. Test every migrated alias through Unified Inference before deleting the
   old LM Studio installation.

The source hint reads only LM Studio's on-disk settings and conventional model
directory. The scan does not contact LM Studio, load a model, copy weights, or
treat a multimodal projector as a primary model.

## 9. Verify the complete installation

The catalog should list only models whose engines are enabled and whose
profiles are usable:

```bash
curl --fail http://127.0.0.1:1240/v1/models
```

Send a language request through the unified endpoint:

```bash
curl --fail http://127.0.0.1:1240/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "your-model-alias",
    "messages": [{"role": "user", "content": "Reply with one short sentence."}]
  }'
```

Every client—including Hermes Agent and applications that call the endpoint
“LM Studio” or “OpenAI compatible”—should use:

```text
Base URL: http://127.0.0.1:1240/v1
API key:  any non-empty placeholder, unless INFERENCE_API_KEY is configured
Model:    an alias returned by GET /v1/models
```

Language usage is recorded locally and central Postgres reporting defaults on.
The workstation identifier is inherited from the previous token sidecar when
available, then falls back to the normalized macOS Computer Name.

Return to **Settings → Setup & Health**, select the newly configured alias, and
run the real self-test. Confirm the response preview, route, engine tier,
latency, token counts, local usage result, and Postgres writer/outbox status.
The test does not claim central delivery until the outbox has actually drained.

For a release-candidate acceptance pass from a source checkout, capture the
same service/version/durable-usage proof in a private report:

```bash
python3 macos/packaging/collect_acceptance.py \
  --app "/Applications/Unified Inference.app" \
  --live --require-live --self-test your-model-alias \
  --output "$HOME/Desktop/unified-inference-live-acceptance.json"
```

The report is written with mode `0600` and redacts credentials and
credential-bearing URLs. A running Login Item alone is not accepted when the
public or control listener, readiness contract, catalog, usage store, product
version, or requested self-test fails.
Release operators should then use the opt-in restart/KeepAlive, protected
folder, LM Studio-directory adoption, oMLX recovery, Postgres drain, and
download-lifecycle flags documented in [RELEASE.md](RELEASE.md). Those flags
produce durable machine-readable evidence and signal only the exact registered
LaunchAgent; ordinary users do not need to run them.

## Updating Unified Inference

A production build exposes **Check for Updates…** and accepts only updates from
the HTTPS appcast that pass Sparkle's EdDSA signature and Apple code-identity
checks. Private ad-hoc builds intentionally disable application updates because
they contain no production public key.

If an application update regresses, install the previous notarized DMG without
deleting `~/Library/Application Support/Mnemosyne`. Configuration, model
weights, managed engine runtimes, bookmarks, local usage, and the Postgres
outbox remain in Application Support. Follow the exact recovery sequence in
[RELEASE.md](RELEASE.md).

## Updating engines

- llama.cpp, DS4, and MFLUX: use **Settings → Runtime Updates**. Activation
  drains active requests and unloads the resident model first.
- Official oMLX app: use its in-app updater. Unified Inference's oMLX card
  opens the matching official release and detects the new version afterward.
- oMLX stable installations: `brew update` followed by
  `brew upgrade omlx`.
- Advanced oMLX custom-kernel HEAD installations:

  ```bash
  brew update
  brew reinstall omlx --HEAD --with-custom-kernel
  omlx restart
  ```

Re-run the native-kernel verification after every advanced Homebrew
custom-kernel replacement.

## Troubleshooting

### Unified Inference says it cannot connect to the server

Confirm the background item is enabled, then inspect the control plane and
logs from the menu:

```bash
curl --fail http://127.0.0.1:17321/manager/status
```

Use **Open Logs** in the menu-bar popover for the service diagnostic.

### The status is degraded because oMLX is unavailable

For the recommended app, open oMLX and confirm its server is running on
`127.0.0.1:17322`. For the headless Homebrew alternative:

```bash
brew services info omlx
omlx restart
curl --fail http://127.0.0.1:17322/v1/models
```

If oMLX is listening on its default port `8000`, rerun the one-time
`omlx serve --host 127.0.0.1 --port 17322 --model-dir ...` command, stop it
with Control-C, and run `omlx restart`.

### An engine is not installed yet

Disable that engine under **Settings → Engines** while repairing it. Its model
profiles are retained but omitted from `/v1/models` until the engine is
enabled again.

### Port `1240` is already in use

Retire the legacy token sidecar with the repository script above. Do not kill
an unknown process merely because it owns the port.

To build the disk image itself from a checkout, use the
[native packaging guide](packaging/README.md).
