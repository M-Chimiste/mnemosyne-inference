# Native macOS packaging

The native deployment has two long-lived processes plus an on-demand worker:

- `Unified Inference.app` is an AppKit `NSStatusItem` with a SwiftUI popover client of
  the control API. It
  reads `server.control_bind`, `server.control_port`, and
  `server.control_password_env` from the user native configuration; the
  default endpoint is `http://127.0.0.1:17321`. It shows the unified model
  catalog and can load or unload aliases without exposing individual engine
  ports. Its dedicated configuration window edits `config.yaml` and `.env`,
  validates YAML through the control API, preserves private file modes, and
  distinguishes hot-reloadable profile edits from restart-required changes.
- `MnemosyneService.app` is a background-only helper launched as a per-user
  LaunchAgent. Its bootstrap resolves the outer app's bundled Python and then
  calls `execve`; it does not daemonize or add a second supervisor.
- A private MFLUX worker is launched in its own process group only while an
  image model is resident. It uses loopback port `17324` and the independent
  `framework-mnemosyne-image` Python export layer.

The LaunchAgent owns the service lifetime, so **Quit Menu App** does not stop
inference on port `17320`. Disabling the background service unregisters it and
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

This workstation currently has Swift Command Line Tools but not a selected
full Xcode installation. `swift build` works in that setup. Some Command Line
Tools releases cannot launch Swift's test bundle because their Testing
framework runtime search path is incomplete; full Xcode is the supported path
for the final registration and login-item smoke tests.

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

Then build the relocatable Python layers and stage an ad-hoc-signed app:

```bash
python3 macos/packaging/build_runtime.py
macos/packaging/build_app.sh release
open "macos/app/build/Stage/Unified Inference.app"
```

For fast UI-only work, `build_app.sh debug --bare` omits Python. Do not enable
its background service: the bootstrap intentionally exits with a clear error
when the runtime is absent.

In a restricted build runner where SwiftPM cannot start its own sandbox, set
`MNEMOSYNE_SWIFTPM_DISABLE_SANDBOX=1`. Normal local builds should leave it
unset.

Before enabling **Background service**, move the staged app to a stable path
such as `/Applications/Unified Inference.app`. `SMAppService` tracks the containing app
and requires it to be code signed. If the helper or LaunchAgent plist changes,
disable and re-enable the service so macOS registers the new definition.
The first launch after upgrading from the former `Mnemosyne.app` filename
automatically refreshes already-enabled service and menu-login registrations;
subsequent launches do not restart either registration.

Ad-hoc signing is for local development only. Distribution still requires a
Developer ID signature, hardened runtime, nested-code signing from the inside
out, notarization, and a signed update mechanism.

## Bundle layout

```text
Unified Inference.app/Contents/
  MacOS/UnifiedInference
  Helpers/MnemosyneService.app/
    Contents/MacOS/mnemosyne-service-bootstrap
  Library/LaunchAgents/com.mnemosyne.inference.agent.plist
  Resources/
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

The menu reads `server.control_password_env` from `config.yaml`, then resolves
that named variable with the same launch-environment-over-`.env` precedence as
the Python service. The default name is `ADMIN_PASSWORD`. The secret is never
copied into SwiftUI preferences.
