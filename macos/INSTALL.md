# Install Unified Inference on Apple Silicon

This is the normal end-user installation path for a Mac. It installs
`Unified Inference.app`, prepares every supported native engine, and leaves
clients with one OpenAI-compatible endpoint:

```text
http://127.0.0.1:1240/v1
```

Docker and LM Studio are not required. Only oMLX is installed separately.
llama.cpp, DS4, and MFLUX are installed or updated from Unified Inference.

## What gets installed

| Engine | Model format or role | Normal installation | Private port |
| --- | --- | --- | ---: |
| llama.cpp | GGUF language and vision models | **Settings → Runtime Updates** | `17325` |
| oMLX | MLX language, vision, embedding, and rerank models | Homebrew CLI | `17322` |
| DS4 | Supported DeepSeek V4 and GLM 5.2 layouts | **Settings → Runtime Updates** | `17323` |
| MFLUX | Apple Silicon image generation | Bundled; updates in **Runtime Updates** | `17324` |

Clients never call those private ports. Unified Inference owns model selection,
global residency, proxying, and language-token accounting on port `1240`.

## 1. Prepare the Mac

You need:

- An Apple Silicon Mac running macOS 15 or newer.
- Full Xcode for oMLX's GLM-5.2 custom Metal kernels. Apple Command Line Tools
  alone are enough for DS4, but not for these oMLX kernels.
- [Homebrew](https://brew.sh/) for the headless oMLX installation.
- Enough internal or external storage for model weights.

After installing Xcode, select it and confirm that the Metal compiler is
available:

```bash
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
xcrun --find metal
```

If you will not use oMLX custom kernels, DS4's smaller prerequisite can be
installed with:

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
Open its menu-bar item and choose **Enable Service**. Approve the background
item in **System Settings → General → Login Items & Extensions** if macOS
requests it.

Verify the core before installing models:

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

## 5. Install oMLX with Homebrew and the CLI

The oMLX GUI/DMG is not needed. The normal Unified Inference setup uses the
official Homebrew formula and its headless `omlx` command.

First add the official tap:

```bash
brew tap jundot/omlx https://github.com/jundot/omlx
```

For GLM-5.2, MiniMax M3, and the other frontier families that use oMLX's
custom Metal kernels, install the upstream HEAD build with those kernels:

```bash
brew install omlx --HEAD --with-custom-kernel
```

`--HEAD` follows oMLX's current main branch; it is used here because the
upstream project currently requires that build for the Homebrew custom-kernel
option. A Mac that will not serve those model families can instead use the
stable `brew install omlx`.

If oMLX is already installed without them, replace that installation:

```bash
brew reinstall omlx --HEAD --with-custom-kernel
```

The official oMLX documentation warns that a plain install silently uses a
substantially slower, more memory-hungry fallback for GLM-5.2. The custom
kernel build requires full Xcode. Verify the Homebrew runtime itself:

```bash
"$(brew --prefix omlx)/libexec/bin/python" -c \
  'from omlx.custom_kernels import native_kernel_status; print(native_kernel_status())'
```

The `glm_moe_dsa` entry must report that its native kernel is available.

Configure oMLX once from the CLI. Use the same exact model-library folder you
selected in Unified Inference; the example below is intentionally a nested
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

On Athena or another Mac, replace only the model folder; keep the host and
port unchanged. A fresh CLI installation has no pinned models. Do not add
oMLX pins or per-model TTL rules, because Unified Inference owns the one-model
residency policy.

Finally open **Settings → Engines**, enable oMLX, and leave its local API
address at the supplied default:

```text
http://127.0.0.1:17322
```

No oMLX API key or admin session is needed for this loopback-only default. If
you deliberately enable oMLX authentication, save the corresponding
`OMLX_API_KEY` and `OMLX_ADMIN_SESSION` values through Unified Inference's
Credentials page.

Official references:

- [oMLX Homebrew and custom-kernel installation](https://github.com/jundot/omlx#homebrew)
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

## Updating engines

- llama.cpp, DS4, and MFLUX: use **Settings → Runtime Updates**. Activation
  drains active requests and unloads the resident model first.
- oMLX stable installations: `brew update` followed by
  `brew upgrade omlx`.
- oMLX custom-kernel HEAD installations:

  ```bash
  brew update
  brew reinstall omlx --HEAD --with-custom-kernel
  omlx restart
  ```

Re-run the native-kernel verification after every oMLX replacement.

## Troubleshooting

### Unified Inference says it cannot connect to the server

Confirm the background item is enabled, then inspect the control plane and
logs from the menu:

```bash
curl --fail http://127.0.0.1:17321/manager/status
```

Use **Open Logs** in the menu-bar popover for the service diagnostic.

### The status is degraded because oMLX is unavailable

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
