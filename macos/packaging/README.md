# Native macOS packaging

This file is for people building the application. To install an existing disk
image and prepare all native engines, use the
[Mac installation guide](../INSTALL.md).

The native deployment has a menu controller and a long-lived background
service plus on-demand manager-owned engine processes:

- `Unified Inference.app` is an AppKit `NSStatusItem` with a SwiftUI popover client of
  the control API. It
  reads `server.control_bind`, `server.control_port`, and
  `server.control_password_env` from the user native configuration; the
  default endpoint is `http://127.0.0.1:17321`. It shows the unified model
  catalog and can load or unload aliases without exposing individual engine
  ports. Its dedicated settings window presents native forms for general,
  engine, storage, Hugging Face library, model, usage, and credential settings.
  Exact internal or nested external model directories are selected with a
  native folder chooser and tied to their containing volume UUID. The Model
  Library page provides engine-aware search and durable download controls.
  The Models page uses a Finder-selected directory to discover GGUF shard sets,
  multimodal projectors, and MLX folders in place without loading or copying
  them. While the picker grant is live, it transfers an ordinary bookmark to
  the service, which consumes it and creates a receiver-owned durable bookmark;
  only the durable bytes stay in private state and only their SHA-256
  `scope_id` is persisted in YAML. The service preflights referenced grants on
  save, revalidates them and prunes unreferenced bookmarks at startup, and
  scoped helpers/model/download children reactivate a grant before `exec`.
  Model import is explicit, while compatible vision projectors are selected
  automatically with manual and text-only choices. LM Studio model-folder
  settings are read only to offer a migration shortcut; there is no LM Studio
  engine or inventory bridge. The control API
  validates and atomically persists versioned structured configuration,
  private credentials stay write-only, and the UI distinguishes
  hot-reloadable profile edits from restart-required changes.
- `Contents/MacOS/mnemosyne-service-bootstrap` is the directly embedded
  helper executable launched as a per-user LaunchAgent. This follows
  `SMAppService`'s bundle-relative `BundleProgram` layout instead of nesting a
  second app bundle inside the main app. The bootstrap resolves the outer
  app's bundled Python and then calls `execve`; it does not daemonize or add a
  second supervisor.
- A managed `llama-server` is launched in a proven-owned process group on
  loopback `17325` only while a GGUF profile is resident. Its binary is
  installed from the official upstream release below Application Support,
  not embedded in the signed app or stored beside model weights.
- DS4 and the private MFLUX worker are also manager-owned, on-demand process
  groups. MFLUX uses loopback port `17324` and the independent
  `framework-mnemosyne-image` Python export layer.

The LaunchAgent owns the service lifetime, so **Quit Menu App** does not stop
inference on port `1240`. Disabling the background service unregisters it and
causes macOS to terminate the job. `KeepAlive=true` handles unexpected exits.

## Development

The Python service and menu UI can be developed independently:

```bash
uv run --project macos/service --extra dev python -m pytest macos/service/tests
uv run --project macos/service mnemosyne-macos serve \
  --config "$HOME/Library/Application Support/Mnemosyne/config.yaml" \
  --env "$HOME/Library/Application Support/Mnemosyne/.env"

cd macos/app
swift build
swift test
```

Set `MNEMOSYNE_CONTROL_URL` when the UI should target a fixture service.
`MNEMOSYNE_MACOS_CONFIG_PATH` and `MNEMOSYNE_MACOS_ENV_PATH` can point a
command-line development launch at alternate files. A plain
`swift run MnemosyneMenu` can exercise the menu and HTTP client, but
`SMAppService` registration must be tested from a signed `.app` bundle.

`swift build` and the Swift unit tests can run with compatible Command Line
Tools. Full Xcode remains required for the final packaged `SMAppService`,
login-item, signing, and notarization acceptance work. Restricted runners may
also need access to SwiftPM's cache and plugin directories even when the source
itself compiles normally.

## Staging a local app

The runtime builder requires `uv`. It exports the production dependency graph
from the committed `macos/service/uv.lock` and
`macos/image-worker/uv.lock` with cache-free, offline `uv export --locked`,
rejects non-exact requirements, and passes each graph to its own venvstacks
framework layer.
An HTTPS GitHub dependency is accepted only when pinned to a full commit SHA;
the builder turns it into a commit-keyed wheel under `packaging/_wheels` before
the hash-locked, binary-only venvstacks install. This currently covers the
post-0.18.0 MFLUX Krea 2 implementation while preserving immutable provenance.
The image layer also resolves Pillow/OpenCV duplicate dylib names by excluding
OpenCV's private copies from venvstacks' shared-link scan; cv2 continues to load
those files directly through its package-local `@loader_path/.dylibs` paths.
This keeps the packaged service on the same dependency versions as the tested
Mac service environment; venvstacks still creates its target-specific layer
lock and relocatable Python runtime.

Validate the lock handoff without downloading or building anything:

```bash
python3 macos/packaging/build_runtime.py --check-lock
python3 macos/packaging/build_runtime.py --print-resolved
```

If either command reports that the lock is stale, regenerate and review it
before packaging:

```bash
uv lock --project macos/service
uv lock --project macos/image-worker
```

Then build the relocatable Python layers and stage an app. The default identity
is `-` (ad hoc):

```bash
python3 macos/packaging/build_runtime.py
macos/packaging/build_app.sh release
open "macos/app/build/Stage/Unified Inference.app"
```

The source artwork for the Finder, Settings, and Login Items icon is
`AppIcon.png`; `AppIcon.icns` is the generated multi-resolution bundle asset
that `build_app.sh` stages under `Contents/Resources`. The menu-bar status item
remains a monochrome template symbol so it follows the current macOS tint and
contrast settings.

For a stable signature, set an identity available in the login keychain:

```bash
CODESIGN_IDENTITY="Apple Development: Example Name (TEAMID)" \
  macos/packaging/build_app.sh release
```

The script uses that identity for nested Mach-O files, the direct helper, and
the outer app, applies hardened runtime and secure timestamps, then performs a
deep strict verification. If a target Mac has no valid code-signing identity,
its local build remains ad hoc. Rebuilding an ad-hoc app changes its code
identity and macOS may require each protected model folder to be selected
again.

## Building a disk image

After staging the app, create the installable disk image with:

```bash
CODESIGN_IDENTITY="Developer ID Application: Example Name (TEAMID)" \
  macos/packaging/build_dmg.sh
```

The default output is
`macos/app/build/Distribution/Unified-Inference-<version>-macos-<architecture>.dmg`.
The disk image contains the signed app and an Applications shortcut for the
usual drag-to-install workflow. The builder validates the source app, creates
and optionally signs the compressed image, verifies it with `hdiutil`, mounts
it read-only, and revalidates the app and shortcut before replacing the final
artifact.

Notarization credentials stay in the login Keychain, not the repository. Set
up a profile once; leaving out `--password` makes `notarytool` prompt securely
for an Apple app-specific password:

```bash
xcrun notarytool store-credentials unified-inference-notary \
  --apple-id "developer@example.com" \
  --team-id "TEAMID"
```

Then create, submit, wait for Apple, staple the approval ticket, and validate
the result in one command:

```bash
CODESIGN_IDENTITY="Developer ID Application: Example Name (TEAMID)" \
NOTARYTOOL_PROFILE="unified-inference-notary" \
  macos/packaging/build_dmg.sh
```

An App Store Connect API-key-backed profile works as well. Use `--app`,
`--output`, or `--volume-name` to override the artifact defaults, and
`--notary-profile` instead of the environment variable when desired.

For fast UI-only work, `build_app.sh debug --bare` omits Python. Do not enable
its background service: the bootstrap intentionally exits with a clear error
when the runtime is absent.

In a restricted build runner where SwiftPM cannot start its own sandbox, set
`MNEMOSYNE_SWIFTPM_DISABLE_SANDBOX=1`. Normal local builds should leave it
unset.

Before enabling **Background service**, move the staged app to a stable path
such as `/Applications/Unified Inference.app`. `SMAppService` tracks the containing app
and requires it to be code signed. On launch, the menu app fingerprints its
installed signed bundle and refreshes already-enabled service and menu-login
registrations only when that bundle has changed. Refresh uses
`SMAppService`'s asynchronous unregister completion, waits for the terminal
disabled state, then registers and waits for enabled or approval-required; it
never immediately re-registers a still-running old helper. Pending refresh
intent survives failure or cancellation and is retried on the next launch.
This covers the former `Mnemosyne.app` filename migration and local
ad-hoc-signed updates without restarting either registration on ordinary
launches.

For an ad-hoc-signed update, do not merge the staged directory over a running
installation. In the old app, first disable the background service (and menu
login item if enabled), wait for the LaunchAgent and ports to disappear, then
quit the menu app. Copy the staged bundle to a new sibling under
`/Applications` and verify that copy. Move the old canonical bundle to an
explicit sibling backup such as
`/Applications/Unified Inference.previous.app`, then atomically move the
verified candidate into the exact `/Applications/Unified Inference.app` path.
If that second move fails, restore the backup to the canonical path before
continuing. Launch the new app and explicitly enable the service again;
approve it in Login Items if macOS reports approval-required. Keep the backup
until the protected-folder and engine-swap smokes pass.

Ad-hoc signing is for local development only. `CODESIGN_IDENTITY` provides
signature stability but does not by itself implement distribution.
Distribution still requires a Developer ID signature, hardened runtime,
nested-code signing from the inside out, notarization, and a signed update
mechanism.

## Bundle layout

```text
Unified Inference.app/Contents/
  MacOS/UnifiedInference
  MacOS/mnemosyne-service-bootstrap
  Library/LaunchAgents/com.mnemosyne.inference.agent.plist
  Resources/
    AppIcon.icns
    Python/                 # venvstacks export
    Service/mnemosyne_macos/
    ImageWorker/mnemosyne_mflux_worker/
    config.yaml.example
    .env.example
```

At first launch the bootstrap copies missing examples to
`~/Library/Application Support/Mnemosyne/`, creates `logs/` and `state/` with
private permissions, and exports `MNEMOSYNE_MACOS_CONFIG_PATH` and
`MNEMOSYNE_MACOS_ENV_PATH` for the service.

The examples are copied only when the user files are absent. Upgrading an
existing installation therefore preserves its aliases, storage roots, and
secrets. Schema-version migration converts old LM Studio profiles into inert
alias/load-setting records for later Finder import and removes the engine
configuration. Fresh examples enable manager-owned llama.cpp. Runtime downloads live separately under
`~/Library/Application Support/Mnemosyne/runtimes/`; installing or replacing
the app neither deletes them nor touches model libraries on internal or
external drives.

The menu's ordinary bookmark carries Apple's implicit single interprocess
extension and is converted by the receiving service into a durable
security-scoped bookmark. The bundle currently declares no App Sandbox
bookmark entitlements. Receiver-owned bytes are stored separately below the
private `state/security-scopes/` directory beside the active config, with mode
`0600`; that root does not follow `paths.state_database`, and only the
bookmark's SHA-256 `scope_id` appears in the copied/user-edited YAML.
Configuration saves reject a missing, stale, path-mismatched, or
non-reactivatable referenced grant. Bookmark receipt and reactivation run in
bounded, killable process groups. Startup revalidates persisted references and
prunes unreferenced private bookmark files. Scoped helpers and managed
engine/download children reactivate the durable bookmark before `exec`.

Potentially blocking bookmark receipt/reactivation and protected-filesystem
inspection, scanning, path resolution, destination creation/measurement, and
GGUF validation run in bounded, killable subprocess groups off the asyncio
event loop. Timeout, request cancellation, and shutdown terminate the complete
group so a missing grant cannot stall the control service or leave a child
behind.

Config snapshots include a content revision. Settings saves must echo it, and
download/import profile writes use the same mutation lock; stale Settings
windows receive a conflict instead of overwriting a newly created profile.

The menu reads `server.control_password_env` from `config.yaml`, then resolves
that named variable with the same launch-environment-over-`.env` precedence as
the Python service. The default name is `ADMIN_PASSWORD`. The secret is never
copied into SwiftUI preferences.

For packaged operation, `engines.mflux.python` must remain unset. The bootstrap
exports the bundled image-layer Python and worker source, and an activated
managed MFLUX runtime may supersede that fallback. Checkout `.venv` paths and
checkout-valued `MNEMOSYNE_MFLUX_PYTHON` /
`MNEMOSYNE_MFLUX_PYTHONPATH` are development-only overrides and must not be
persisted into an installed workstation's configuration.

## Engine dependency updates

Routine llama.cpp, MFLUX, and DS4 updates do not require a new app bundle or a
separately published Unified Inference artifact. The running service checks
the official `ggml-org/llama.cpp` releases, MFLUX PyPI project, and
`antirez/ds4` GitHub repository directly; it never relies on a
repository-owned dependency manifest. oMLX remains externally installed, so
the app reports its version and official update link without replacing an app
bundle or Homebrew files.

For llama.cpp, the service selects the official macOS arm64 archive and checks
its upstream asset name, URL, published size and SHA-256, safe extraction,
executable, and required CLI flags. It stages MFLUX in an isolated package
directory or builds `ds4-server` from the exact reported commit and validates
the result. Staging may run while a model is resident; pointer activation and
rollback use the coordinator's all-engines-empty maintenance barrier. Previous
managed runtimes are retained for recovery, and model weights never live
inside a runtime directory.
