# Native macOS packaging

This file is for people building the application. To install an existing disk
image and prepare all native engines, use the
[Mac installation guide](../INSTALL.md).

`macos/VERSION` is the single native product version. Run
`python3 macos/packaging/verify_release.py` before staging; the app plist,
service, image worker, lock files, tag, and staged bundle must agree. Local
ad-hoc artifacts are deliberately not distribution releases. The credentialed
CI process, signed appcast, and recovery contract are documented in
[release and recovery](../RELEASE.md).
When a staged app is supplied, the verifier also inspects the menu
executable's Mach-O dependencies and `LC_RPATH`. The bundled Sparkle framework
must exist and resolve through `@executable_path/../Frameworks`; a valid deep
code signature is not sufficient if dyld cannot launch the app.

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
  hot-reloadable profile edits from restart-required changes. A missing oMLX
  runtime can be delegated to Homebrew only after an explicit confirmation
  displays the fixed official tap and stable install commands; arbitrary
  formulas, `--HEAD`, replacement, and update commands are not accepted.
- `Contents/MacOS/mnemosyne-service-bootstrap` is the directly embedded
  helper executable launched as a per-user LaunchAgent. This follows
  `SMAppService`'s bundle-relative `BundleProgram` layout instead of nesting a
  second app bundle inside the main app. The bootstrap resolves the outer
  app's bundled Python and then calls `execve`; it does not daemonize or add a
  second supervisor.
- `Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper`
  is a separately identified, on-demand
  authentication transport inside a fixed app-like wrapper. It accepts
  exactly one inherited, unnamed Unix socket session, verifies the complete
  enclosing outer-app seal and the connected service Python transport through
  its audit token and the sealed `lifecycle-helper-peer-v2.json` manifest. The
  wrapper gives a future credentialed build the supported location for its
  provisioning profile, but the helper deliberately cannot emit a production
  authorization receipt because no private helper proof key is provisioned.
  It exposes no named listener, path/PID/port/LaunchAgent/argv operation, or
  lifecycle effect. An ad-hoc build includes the binary for packaging
  validation but remains explicitly unavailable as authorization authority.
  The credentialed profile/entitlement contract, Keychain/Secure Enclave proof
  design, mutual peer-pinning ceremony, and release gates required to activate
  this transport are specified in
  [`../../project_docs/native_lifecycle_authorization.md`](../../project_docs/native_lifecycle_authorization.md).
- `Contents/MacOS/mnemosyne-lifecycle-runner` is a separately identified,
  one-shot inert adapter. It accepts only one bounded registration frame over
  an inherited unnamed Unix stream socket, checks the sealed app/runner and
  connected service-Python role identities, and returns only the fixed
  `runner_adapter_unavailable` refusal before exiting nonzero. The Python role
  is explicitly non-authoritative. This runner has no journal, lifecycle
  command, effect implementation, peer-supplied path, or process operation;
  signed and ad-hoc builds are both execution-disabled in this milestone.
- A managed `llama-server` is launched in a proven-owned process group on
  loopback `17325` only while a GGUF profile is resident. Its binary is
  installed from the official upstream release below Application Support,
  not embedded in the signed app or stored beside model weights.
- DS4 and the private MFLUX worker are also manager-owned, on-demand process
  groups. Retired mlxcel and mistral.rs configuration remains parseable but
  inert so an existing pilot can upgrade without losing profile metadata or
  model weights. oMLX retains its externally installed official runtime.
  MFLUX uses loopback port `17324` and the independent
  `framework-mnemosyne-image` Python export layer.

The LaunchAgent owns the service lifetime, so **Quit Menu App** does not stop
inference on port `1240`. Disabling the background service unregisters it and
causes macOS to terminate the job. `KeepAlive=true` handles unexpected exits.
On a genuinely fresh setup, the first app launch requests both the LaunchAgent
and main-app login registrations. Existing configured installations retain
their current choices, and the one-time default never restores a registration
that the user later disables. Approval-required registrations direct the user
to macOS Login Items rather than bypassing Service Management consent.

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
framework layer through the exact `venvstacks==0.7.0` builder.
Every registry dependency is rewritten to the one CPython 3.12 / Apple Silicon
wheel selected from its committed lock record for the app's macOS 15 deployment
target, including the lock's SHA-256. Generated layer environments are rebuilt
cleanly so an older same-version host wheel cannot survive. Export and release
verification then parse every slice of every bundled Mach-O and reject a
declared minimum newer than macOS 15; wheel tags alone are not accepted as
proof.
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
python3 macos/packaging/verify_release.py
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

The script uses that identity for nested Mach-O files, the direct service and
Trash helpers, the nested lifecycle-helper wrapper, the inert lifecycle
runner, and the outer app. After nested Python/helper/runner signing it
generates a closed role-identity manifest
containing their exact bundle-relative locations, identifiers, Team
Identifiers, CDHashes, requirement digests, and app version/build binding. The
service Python entry is marked non-authoritative, and the final outer signature
seals the manifest. Developer ID builds apply the hardened runtime and secure
timestamps throughout, then perform a deep strict verification. If a target
Mac has no valid code-signing identity,
its local build remains ad hoc. Rebuilding an ad-hoc app changes its code
identity and macOS may require each protected model folder to be selected
again.

A credentialed lifecycle-helper wrapper additionally requires an externally
managed Developer ID Application provisioning profile. Keep it outside the
repository and pass only its exact path:

```bash
CODESIGN_IDENTITY="Developer ID Application: Example Name (TEAMID)" \
SPARKLE_PUBLIC_ED_KEY="<public EdDSA key>" \
MNEMOSYNE_LIFECYCLE_HELPER_PROVISIONING_PROFILE="/secure/path/helper.provisionprofile" \
  macos/packaging/build_app.sh release
```

The build accepts only a current macOS Developer ID distribution profile for
the fixed helper App ID and Team, extracts exactly the application identifier,
Team identifier, and dedicated Keychain access group entitlements, and rejects
debugging or mismatched profiles. Embedding a valid profile does not enable
owner authorization or lifecycle effects; the OS-backed proof authority,
mutual service-side peer verification, and effects runner remain separate
fail-closed gates.

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

The production service bootstrap also removes ambient `PYTHON*` controls,
sets only the bundled runtime's `PYTHONHOME` and closed source paths, disables
user-site and unsafe-path loading, and launches with `-B -P -s`. The explicit
`MNEMOSYNE_PYTHON_OVERRIDE` path is consulted only when the bundle has no
embedded runtime, so it remains a bare source-development escape hatch and
cannot replace Python in a complete packaged release. Full-app verification
executes the sealed bootstrap's configuration check with hostile `PYTHON*`
values and a false override, then rechecks the complete code seal to prove the
inspection itself did not mutate the bundle.

Every build also writes a mode-`0600`, secret-redacted
`Unified-Inference-<version>-macos-<architecture>-acceptance.json` beside the
image. The local report proves app/runtime/default/signature and DMG integrity.
The notarized path additionally requires Developer ID, hardened runtime,
secure timestamp, Sparkle signing configuration, staple validation, and
Gatekeeper acceptance. After installation, run
`collect_acceptance.py --live --require-live --self-test <alias>` to add
LaunchAgent, listener, readiness, catalog, usage, and durable self-test
evidence without putting credentials on the command line. The live report also
captures bounded configuration, storage, installed-runtime readiness, LM Studio
directory-hint, durable download-transition evidence, and the bounded
managed-runtime lifecycle journal. Explicit
`--exercise-service-restart` and `--exercise-keepalive` modes operate through
the exact registered LaunchAgent label and require a new PID plus both healthy
HTTP planes. Strict options can require protected-model reactivation, oMLX
reconcile/auth recovery, native LM Studio-directory adoption, and the complete
download cancel/retry/registration-retry/dismiss/delete history.
`--require-runtime-lifecycle <engine>` additionally requires an ordered
activation/restarted-inference/rollback/restarted-inference/corrupt-rejection
chain and the original active version. Service-instance UUIDs prove the two
restart boundaries without exposing a PID history or credentials. See
[`../RELEASE.md`](../RELEASE.md) for the composed target-Mac commands.
`--exercise-fleet-participation` runs only when the Mac has no active Fleet
requests, pauses and rejoins the node, restores the exact prior participation
preference, and rejects the evidence if model/runtime/storage configuration
changes. The report retains only fixed states and equality checks, not local
model paths. Run this local exercise only while Hub dispatch to the Mac is
disabled or otherwise quiesced: its status read and update are separate HTTP
operations, and a request entering between them correctly makes the exercise
fail rather than weakening that request's drain lease.
For a clean-install pass, `--require-guided-setup` requires candidate-scoped
first-presentation and completion timestamps from the app preferences plus the
same report's durable-usage self-test. The operator must reset the menu app's
preferences domain before the first candidate launch; Application Support
state is separate and remains intact.
For logout/login or reboot acceptance, preserve an accepted private report
from before the cycle and pass it to `--require-login-cycle-baseline`.
The report must belong to the same host and candidate build; the exact
LaunchAgent must return under a new GUI audit-session ID and PID before the
current listeners and durable self-test pass. Ordinary restart exercises keep
the same audit session and cannot satisfy this gate.

Notarization credentials stay in the login Keychain, not the repository. Set
up a profile once; leaving out `--password` makes `notarytool` prompt securely
for an Apple app-specific password:

```bash
xcrun notarytool store-credentials unified-inference-notary \
  --apple-id "developer@example.com" \
  --team-id "TEAMID"
```

Then notarize and staple the signed app, create the disk image, notarize and
staple the image, and Gatekeeper-assess both mounted artifacts in one command:

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
disabled state, allows Background Task Management's launch-requirement
invalidation to settle, then registers and waits for enabled or
approval-required; it never immediately re-registers a still-running old
helper. The settling interval works around a macOS Service Management race
where the unregister completion can arrive before the old launch requirement
has been invalidated. Pending refresh intent survives failure or cancellation
and is retried on the next launch. This covers the former `Mnemosyne.app`
filename migration and local ad-hoc-signed updates without restarting either
registration on ordinary launches.

For an ad-hoc-signed pilot update, do not merge the staged directory over a
running installation. Build the DMG, quit the menu app, and run its **Install
or Upgrade Unified Inference.command**. The assistant verifies the exact
candidate identity and signature, refuses a downgrade, stages a complete
bundle on the Applications filesystem, moves the old app to Trash, atomically
activates the candidate, and launches it. An already-enabled service may keep
serving during bundle staging; the new menu app then refreshes that exact
Service Management registration and the assistant requires a changed healthy
agent PID. The assistant hashes `.env` before and after and never writes to
Application Support or model storage.

The DMG also carries **Uninstall Unified Inference (Preserve Data).command**.
It requires the exact service to be disabled and the menu app to be quit, then
moves the canonical app and default manager-owned runtime directory to Trash.
It deliberately retains the entire accounting recovery surface: `.env`,
config, SQLite usage/outbox state, identity, scopes, pairing, and weights.

Ad-hoc signing is for local development only. `CODESIGN_IDENTITY` provides
signature stability but does not by itself implement distribution.
Distribution still requires a Developer ID signature, hardened runtime,
nested-code signing from the inside out, notarization, and a signed update
mechanism. The `macOS signed release` workflow enforces a GitHub-verified
signed annotated tag, exact version agreement, the complete native suites,
inside-out signing, notarization/stapling, Gatekeeper assessment, and an
EdDSA-signed Sparkle appcast. It retains three feed versions; complete
notarized DMGs remain immutable GitHub release assets for manual rollback.
Version 0.x builds are GitHub prereleases. A 1.x build additionally fails
closed unless every required gate in `macos/acceptance/v1.json` is passed and
the ledger declares the release ready.

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
the app selects the official DMG matching the host macOS version, reports the
detected app/CLI/server version, and opens the official install or update path
without replacing an app bundle or Homebrew files. The menu app may delegate
an initial missing-runtime installation to the user's Homebrew after explicit
approval; Homebrew retains ownership and later updates stay external.

For llama.cpp, the service selects the official macOS arm64 archive and checks
its upstream asset name, URL, published size and SHA-256, safe extraction,
executable, and required CLI flags. It stages MFLUX in an isolated package
directory or builds `ds4-server` from the exact reported commit and validates
the result. Staging may run while a model is resident; pointer activation and
rollback use the coordinator's all-engines-empty maintenance barrier. Previous
managed runtimes are retained for recovery, and model weights never live
inside a runtime directory.
