# Mnemosyne for Apple Silicon

This is the native macOS sibling of the CUDA deployment. It exposes one stable
API while moving the single resident model between an existing LM Studio
server, [oMLX](https://github.com/jundot/omlx), and
[DwarfStar/DS4](https://github.com/antirez/ds4), plus a manager-owned
[MFLUX](https://github.com/filipstrand/mflux) image worker. The engines remain upstream
projects; Mnemosyne coordinates and proxies them without modifying their model
runtimes.

The runtime is deliberately not a Docker image. Docker Desktop runs ordinary
containers in a Linux VM, so it is not the right boundary for arbitrary
MLX/Metal processes. Mnemosyne Core and all engines run natively.

## Current validation

The relocatable runtime, signed local app bundle, embedded service bootstrap,
and embedded MFLUX worker have been exercised on an M4 Max. A real
`krea/Krea-2-Turbo` request through `POST :17320/v1/images/generations`
returned a valid 512×512 PNG, and the request was correctly absent from token
usage. LM Studio discovery on `:1234` was also verified. Real oMLX and DS4
model loads, automatic LaunchAgent restart/login behavior, and CUDA parity
remain separate smoke gates.

## Ports and processes

| Port | Process | Role |
| ---: | --- | --- |
| `17320` | Mnemosyne Core | Unified OpenAI/Anthropic-compatible inference |
| `17321` | Mnemosyne Core | Control API used by the menu bar app |
| `1234` | LM Studio | Existing local server |
| `17322` | oMLX | Native MLX inference and admin API |
| `17323` | `ds4-server` | Mnemosyne-owned model process |
| `17324` | MFLUX worker | Mnemosyne-owned image process |

All listeners default to loopback. Ports `17325` through `17329` are reserved
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

## Requirements

- Apple Silicon and macOS 15 or newer.
- LM Studio installed separately, with its local server on port `1234`.
- Python 3.11–3.13 and `uv` for service development.
- Swift 6 for menu development. Full Xcode is required for final app signing,
  `SMAppService` integration testing, and source builds of custom Metal kernels.
- oMLX, DS4, and MFLUX are optional, but each engine and its model profiles must be set
  `enabled: false` when it is not part of the installation.

The official oMLX `.dmg` is the simplest choice for frontier model families
because it includes its native kernels. Its current source documentation notes
that GLM-5.2 and related custom-kernel builds need full Xcode; a plain source
install falls back to a much slower generic path.

## Engine preparation

LM Studio is assumed to exist already. Start its server from the Developer page
or with:

```bash
lms server start --port 1234
```

Keep it loopback-only. Disable JIT model loading for clients that bypass
Mnemosyne; a direct request to `:1234` can otherwise load a model outside the
global coordinator. If LM Studio requires an API token, place it in the Mac
environment file as `LMSTUDIO_API_KEY`.

Install oMLX from its official `.dmg` or Homebrew package, configure its server
port as `17322`, and start it. For a CLI/Homebrew installation this can be done
with the upstream `OMLX_PORT` setting:

```bash
OMLX_PORT=17322 omlx start
```

Disable model pinning and per-model TTL/LRU behavior for profiles managed by
Mnemosyne. Current oMLX releases may protect unload through an admin session
even when load accepts a bearer key. The safest integration is loopback-only
oMLX with inner admin authentication disabled and authentication enforced at
Mnemosyne. `OMLX_API_KEY` and `OMLX_ADMIN_SESSION` remain available for an
installation that requires them.

DS4 is purpose-built for its published DeepSeek V4 Flash/PRO GGUFs; it is not a
general GGUF runner. Clone and build it separately with `make` on macOS, then
put the absolute `ds4-server` and checkout paths in `config.yaml`. Mnemosyne
starts DS4 with an explicit model, context, host, and port and terminates only a
process whose recorded identity it can prove it owns.

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
curl http://127.0.0.1:17320/health
curl http://127.0.0.1:17320/v1/models
curl -X POST http://127.0.0.1:17320/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local-qwen","messages":[{"role":"user","content":"Hello"}]}'
curl http://127.0.0.1:17321/manager/status
```

With either example MFLUX profile enabled:

```bash
curl -sX POST http://127.0.0.1:17320/v1/images/generations \
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

Move the app to a stable location such as `/Applications` before enabling its
background service. Local builds use ad-hoc signing; distribution requires a
Developer ID signature, hardened runtime, notarization, and nested signing.
See [packaging/README.md](packaging/README.md) for the bundle layout and build
details.

The app is intentionally menu-bar-only: it has no Dock icon or normal app
window. On launch it installs a square AppKit status item using the
brain-profile SF Symbol, with an `M` fallback if the symbol is unavailable.
Click that icon to open the SwiftUI controller popover, then choose **Enable
Service** to register the per-user LaunchAgent. The service and menu-login
item are independent: quitting the menu does not stop an enabled service.

Choose **Configuration…** to edit `config.yaml` and `.env` in a dedicated
native window. The editor tracks unsaved changes, validates YAML against the
same runtime schema used at startup, writes both local files atomically with
mode `0600`, hot-reloads model-only changes, and offers a service restart for
engine, server, path, token-sidecar, or environment changes. If the service is
offline because a configuration needs repair, an explicit confirmed fallback
can save without validation.

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
SQLite `request_usage` table. When `token_sidecar.enabled` is true, the same
transaction adds a durable `pg_usage_outbox` row. A background writer retries
delivery to the existing `public.token_usage` Postgres ledger using stable
event IDs and `ON CONFLICT DO NOTHING`; a network outage does not discard the
local event.

Set the secret DSN only in `.env`:

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
