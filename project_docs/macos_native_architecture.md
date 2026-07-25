# Mnemosyne macOS Native Architecture

Status: implemented native architecture; target-Mac acceptance remains pending.

## Purpose

The macOS deployment provides one OpenAI/Anthropic-compatible endpoint for a
manager-owned llama.cpp runtime, oMLX, DwarfStar (DS4), and a managed MFLUX
image worker. LM Studio is retained only as an explicitly enabled migration
fallback until its soak is accepted. The deployment preserves the
single-resident-model behavior of the CUDA deployment while running natively so
Apple Metal and unified memory remain available to the inference engines.

This is a sibling deployment, not a platform mode inside `vllm_manager.py`.
The CUDA container remains independently buildable and keeps its existing
configuration, ports, dependencies, and CLI behavior.

## Runtime Topology

The production Mac runtime is native. Ordinary Docker Desktop containers run
inside a Linux VM and cannot host arbitrary MLX/Metal processes. Docker Model
Runner's Metal support is a host-side sandboxed runtime and does not provide a
general container environment for oMLX, DS4, or MFLUX.

Default ports are deliberately outside common development defaults:

| Port | Owner | Purpose | Default bind |
| ---: | --- | --- | --- |
| 1240 | Mnemosyne Core | Unified inference API and legacy-sidecar replacement | `127.0.0.1` |
| 17321 | Mnemosyne Core | Control/admin API | `127.0.0.1` |
| 17322 | oMLX | Native MLX engine | `127.0.0.1` |
| 17323 | DS4 | Managed native subprocess | `127.0.0.1` |
| 17324 | MFLUX | Managed native image worker | `127.0.0.1` |
| 17325 | llama.cpp | Managed native GGUF subprocess | `127.0.0.1` |
| 17326-17329 | Reserved | Future local engines/diagnostics | unbound |

All ports remain configurable. Startup validates that inference and control
ports differ, all managed inner ports differ, and no configured inner port is
published beyond loopback. A port collision is reported; Mnemosyne never kills
an unknown process to reclaim a port.

The application bundle has two cooperating components:

1. **Mnemosyne Core** is a per-user background service. It owns the catalog,
   global swap coordinator, proxy, usage outbox, and manager-owned
   llama.cpp/DS4/MFLUX subprocesses.
2. **Unified Inference.app** owns an explicit AppKit `NSStatusItem` and renders its
   controller as a SwiftUI popover. It reads status and sends commands through
   the control API. The UI may exit without interrupting an inference request.

The background service is a user LaunchAgent rather than a system daemon
because every engine and the menu controller belong to the logged-in user's
session. Distribution bundles a known Python runtime instead of depending on
Apple's or Homebrew's Python.

## Ownership Rules

There is exactly one lifecycle owner: Mnemosyne Core.

- llama.cpp is a model-specific process owned by Mnemosyne. Its executable
  comes from the official `ggml-org/llama.cpp` macOS arm64 release, and its
  PID, process group, start identity, executable, and complete argv must match
  persisted ownership before recovery or signaling. Survivor metadata also
  carries storage root, security-scope ID, and volume UUID so a replacement
  service can reconstruct and revalidate a protected target.
- Unified Inference owns local usage accounting and defaults central delivery
  on for every language engine. It may migrate identity and the ledger DSN from
  the previous sidecar's LaunchAgent, then persists missing values into its own
  private `.env` so that LaunchAgent can be retired. The legacy process is not
  a request proxy.
- oMLX remains a long-lived native service so its persistent SSD KV cache and
  model inventory survive model eviction. Mnemosyne controls its loaded engines
  through its admin API.
- DS4 is a model-specific process owned by Mnemosyne. Loading means spawning
  `ds4-server` with an explicit model and loopback port; unloading means graceful
  termination followed by a bounded forced kill if necessary.
- MFLUX is a dependency-isolated worker process owned by Mnemosyne. It exists
  only while an image profile is resident and is terminated on unload,
  timeout, or cancellation so Metal memory release follows process exit.
- Routine llama.cpp, MFLUX, and DS4 engine updates come directly from their
  official upstreams below Application Support. Mnemosyne verifies the
  official llama.cpp asset name, URL, published size and SHA-256, safe archive
  layout, executable, and required CLI contract; installs the official MFLUX
  PyPI package; or builds an exact `antirez/ds4` commit while normal requests
  continue, then activates it only through the coordinator's all-engines-empty
  maintenance barrier. The signed app's MFLUX layer and configured DS4 paths
  remain fallbacks. oMLX remains externally installed and uses its official
  update mechanism.
- Clients use Mnemosyne on port 1240. Direct client traffic to an inner engine
  can violate the single-resident invariant and is unsupported.

During the migration soak, LM Studio JIT loading and oMLX pinning/automatic
loading must not create models outside the coordinator. Operators disable
those behaviors during setup. Startup and periodic audits fail closed if an
unexpected resident appears or an enabled engine cannot report authoritative
state.

## Model Profiles

Public aliases are globally unique and explicitly select an engine. Mnemosyne
does not infer the engine from a model name or file extension.

Conceptual configuration (fresh installs disable the legacy LM Studio adapter):

```yaml
schema_version: 1

server:
  inference_bind: 127.0.0.1
  inference_port: 1240
  control_bind: 127.0.0.1
  control_port: 17321
  idle_unload_seconds: 900
  startup_timeout_seconds: 900
  swap_queue_timeout_seconds: 300

engines:
  lmstudio:
    enabled: false
    base_url: http://127.0.0.1:1234
  llama_cpp:
    enabled: true
    host: 127.0.0.1
    port: 17325
    process_state_path: ~/Library/Application Support/Mnemosyne/state/llama-cpp-process.json
  omlx:
    base_url: http://127.0.0.1:17322
  ds4:
    host: 127.0.0.1
    port: 17323
    binary: /Applications/DwarfStar/ds4-server
    working_directory: /Applications/DwarfStar
    process_state_path: ~/Library/Application Support/Mnemosyne/state/ds4-process.json
  mflux:
    enabled: true
    host: 127.0.0.1
    port: 17324

# Fresh installs have no model profiles. Model Library and Finder discovery
# create profiles only after the user selects the exact model and destination;
# the service preserves those paths instead of assuming a volume or cache root.
models: []
```

Engine-specific options live below `load`. Unknown options are rejected unless
placed in an explicit `extra_args` escape hatch. Secrets remain in the private
macOS environment file, never in YAML or SQLite. The UI treats values as
write-only and preserves unrelated environment lines.

Each resolved profile exposes:

- public alias;
- engine and engine-native target;
- served model name used after request canonicalization;
- supported endpoint/capability set;
- load settings.

The control API combines profiles with coordinator state to expose availability
and the current resident alias.

oMLX inventory is inspected for lifecycle authority, but it does not create
implicit public aliases. Local GGUF/MLX discovery uses a Finder-selected root,
opaque candidate IDs, explicit selection, and a server-side rescan before an
atomic profile migration. Projector GGUFs never become primary models. DS4
profiles are always explicit because DS4 accepts only its purpose-built GGUF
layouts. A read-only local-source route parses LM Studio's configured
`downloadsFolder` and advertises that exact path before its documented
`~/.lmstudio/models` default, without depending on the LM Studio adapter or
daemon. These are only Finder preselection hints; the picker grant and bounded
filesystem helper remain the access and validation boundary. Symlink and
nested paths are retained rather than collapsed to a volume root. The LM
Studio inventory remains only for the temporary soak fallback.

## Finder Access and Protected Paths

The menu app creates ordinary bookmark data while the `NSOpenPanel` selection
grant is live and sends it only to the control plane. The ordinary bookmark's
implicit extension is Apple's supported single interprocess handoff.
Mnemosyne resolves it without presenting UI, explicitly starts the transferred
grant, rejects stale or path-mismatched access, and creates a new
receiver-owned security-scoped bookmark. The durable bookmark's SHA-256 is the
opaque `scope_id`. Only those receiver-owned bytes are stored as mode-`0600`
files below the mode-`0700` `state/security-scopes` directory beside the
active config; this root does not follow the configurable SQLite path. YAML
contains only `scope_id`, and neither SQLite, logs, nor API responses expose
bookmark data. The current bundle declares no App Sandbox bookmark
entitlements; this design must not be described as entitlement-backed.

Before replacing YAML, the configuration endpoint freshly reactivates every
referenced bookmark and rejects a missing, stale, path-mismatched, or
non-reactivatable grant. At startup Mnemosyne revalidates every configured
bookmark before coordinator initialization, then removes private bookmarks no
longer referenced by the loaded configuration. Bookmark receipt and
reactivation run in bounded, killable helper process groups. Scoped filesystem
helpers and manager-owned engine/download children reactivate the durable
bookmark in their own process, then `exec` the real upstream command without
changing process identity. The background LaunchAgent can therefore use the
exact Finder-approved folder after the menu controller exits. External oMLX
remains responsible for obtaining its own macOS access to protected MLX
directories.

macOS may hold a bookmark or `open(2)` call while a protected-folder decision
is pending. Bookmark receipt/reactivation, storage probes, local scans, target
resolution, GGUF/projector header validation, directory creation, and
directory-size measurement therefore run in separate process groups rather
than on either HTTP plane's asyncio event loop. Profile resolution has a
maximum 30-second wait; llama.cpp argv validation is bounded by the remaining
transition and engine-request deadline. A timeout, client cancellation, or
service shutdown terminates the complete helper process group. The operation
fails closed with a permission/volume diagnostic and never freezes
control/status while a request waits.

## Engine Adapter Contract

Every adapter implements the same concrete lifecycle surface:

```text
validate_control()
inspect()
load(profile)
unload(instance)
unload_all()
route(handle, endpoint)
aclose()
```

`inspect`, `load`, and `unload` report engine-observed state rather than
trusting the coordinator's cache. `load` is idempotent for the exact target.
`unload_all` is idempotent when nothing is resident. Adapter failures carry an
engine name, operation, retryability, and actionable detail suitable for the
control API and menu bar.

Language inference is proxied without translating between wire protocols. The gateway
only canonicalizes the `model` field, injects usage opt-in where supported, and
removes outer authorization/cookie headers before forwarding.
Image generation uses a deliberately narrow OpenAI-compatible contract:
`POST /v1/images/generations`, one base64 PNG, bounded dimensions, seed,
steps, guidance, and negative prompt. It does not emit token-usage events.

## Global Residency State Machine

A FIFO coordinator plus one engine-operation lock serializes transitions.
Every proxied request owns an epoch-tagged lease through the complete response
body or streaming generator. State is one of:

```text
idle -> unloading -> verifying_empty -> loading -> verifying_target -> ready
  ^                                                                    |
  +------------------------ draining <---------------------------------+
                               |
                            degraded
```

For each inference request:

1. Resolve the public alias and validate endpoint capabilities.
2. Enter a FIFO waiter queue with the configured timeout.
3. If a different target is at the queue head, stop admitting new leases for
   the old target and drain its existing leases.
4. Inspect every enabled adapter and require authoritative state.
5. Unload every observed resident, then inspect again and prove global empty.
6. Load the selected target under one absolute transition deadline.
7. Inspect all adapters again and require exactly one ready, manager-owned
   matching resident.
8. Publish a new resident epoch and grant the adjacent same-target waiter group.
9. Proxy the request. Release its epoch-tagged lease in a guaranteed finalizer
   after the full response stream closes.

Same-target callers piggyback on a single load. Different-target callers queue
without starving a pending switch. A swap-queue timeout never tears down an
active generation; an image request timeout deliberately terminates its owned
MFLUX worker. Manual unload, reconciliation, shutdown, and idle eviction use the
same drain-and-verify lifecycle primitives. Any non-authoritative adapter state
fails closed and leaves the coordinator degraded.

At startup the default recovery policy is `unload_all`: reconcile every enabled
external adapter, stop only previously recorded/owned child processes, and
establish a clean baseline. A future `adopt_single` policy may adopt one
unambiguous loaded model.

## API Contract

The inference plane supports capability-gated pass-through routes:

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `POST /v1/embeddings`
- `POST /v1/rerank`
- `POST /v1/images/generations`
- `GET /health`

The aggregate model listing exposes stable aliases. Extended engine,
availability, capability, and residency metadata belongs on the control plane so
strict OpenAI clients continue to accept `/v1/models`.

The control plane starts with:

- `GET /manager/status`
- `GET /manager/models`
- `POST /manager/load`
- `POST /manager/unload`
- `POST /manager/reconcile`
- `POST /manager/reload`
- `GET /manager/config`
- `PUT /manager/config`
- `GET /manager/storage`
- `GET /manager/storage/inspect`
- `POST /manager/storage/inspect`
- `GET /manager/model-library/recommendations`
- `GET /manager/model-library/local-sources`
- `POST /manager/model-library/local-scan`
- `POST /manager/model-library/imports`
- `GET /manager/model-library/search`
- `GET /manager/model-library/files`
- `GET /manager/model-library/installs`
- `POST /manager/model-library/installs`
- `POST /manager/model-library/installs/{id}/cancel`
- `POST /manager/model-library/installs/{id}/retry`
- `GET /manager/usage`
- `GET /manager/runtime-updates`
- `POST /manager/runtime-updates/check`
- `POST /manager/runtime-updates/{engine}/install`
- `POST /manager/runtime-updates/{engine}/rollback`

`GET /manager/config` returns a content-derived optimistic revision.
`PUT /manager/config` must echo that revision and returns `409 Conflict` when
the persisted file has changed. Settings saves, completed-download profile
creation, and local imports share one mutation lock and always reload the
latest YAML before writing, so a stale window cannot erase a concurrently
added model.

Inference bearer auth and control auth follow the CUDA manager's fail-safe
behavior. Inner engine credentials are never forwarded to clients, and outer
authorization/cookie headers are never forwarded to inner engines.

## Usage Accounting

Usage accounting preserves the existing delivery guarantees:

```text
response/stream
  -> endpoint-specific usage normalizer
  -> local request_usage row
  -> durable SQLite pg_usage_outbox
  -> retry-safe Postgres batch writer
```

Image generation is outside this accounting path and creates no token-usage
or Postgres-outbox row.

The normalized local record retains event ID, timestamp, public alias, engine,
endpoint, streamed flag, prompt/input tokens, completion/output tokens, total
tokens, response time, status, and the unmodified backend usage object.

Normalizers understand:

- OpenAI Chat/Completions terminal `usage` objects;
- OpenAI Responses `response.completed` usage;
- Anthropic Messages non-streaming and terminal SSE usage;
- oMLX/DS4 cache token details when present.

Backend counts are authoritative. Mnemosyne does not silently substitute a
different tokenizer. Any future fallback estimate must be explicitly marked as
estimated in local metadata. The initial Postgres writer remains compatible with
the current `public.token_usage` schema and sends the stable public alias as
`model`. Its node ID can be inherited from the legacy standalone sidecar
LaunchAgent registered in `com.athena.token-sidecar.plist`; the normalized Mac
Computer Name is only a fallback and an explicit native `node_id` remains an
override.

## Repository Boundary

The initial implementation lives below `macos/` with independent service and
native-app build boundaries:

```text
macos/
  service/
    pyproject.toml
    src/mnemosyne_macos/
    tests/
  app/
    Package.swift
    Sources/
    Tests/
  packaging/
  config.yaml.example
  smoke_checks.md
```

The current root CUDA files are not moved. Proven platform-neutral components,
starting with usage records/outbox delivery, may later move into a small shared
package under compatibility imports. The macOS package must never import
`vllm_manager.py` or acquire CUDA/vLLM runtime dependencies.

## Implemented Scope

1. Configuration, adapter protocol, coordinator, and deterministic fake-engine
   tests.
2. Manager-owned llama.cpp adapter, official runtime integrity checks, GGUF
   quant/shard/projector selection, and local-library adoption tests. The LM
   Studio adapter remains covered only for migration/soak compatibility.
3. oMLX adapter, including explicit validation of programmatic unload auth.
4. DS4 subprocess adapter and readiness/termination tests using a fake server.
5. Inference proxy, endpoint capability checks, and usage normalization.
6. SQLite analytics/outbox and existing Postgres schema integration.
7. LaunchAgent templates and developer CLI.
8. AppKit status item, SwiftUI controller popover, bundled service supervision,
   typed settings, model-library discovery, and onboarding checks.
9. Official-source, rollback-safe llama.cpp, MFLUX, and DS4 managed runtimes
   plus external oMLX update discovery.
10. Native macOS smoke checklist for the remaining target-machine acceptance.

## External References

- llama.cpp: <https://github.com/ggml-org/llama.cpp>
- LM Studio model management (temporary migration fallback): <https://lmstudio.ai/docs/developer/rest>
- oMLX: <https://github.com/jundot/omlx>
- DwarfStar: <https://github.com/antirez/ds4>
- MLX unified memory: <https://ml-explore.github.io/mlx/build/html/usage/unified_memory.html>
- Docker Model Runner execution model: <https://docs.docker.com/ai/model-runner/#execution-environment>
- Apple `SMAppService`: <https://developer.apple.com/documentation/servicemanagement/smappservice>
- Apple bookmark implicit-scope option:
  <https://developer.apple.com/documentation/foundation/nsurl/bookmarkcreationoptions/withoutimplicitsecurityscope>
- AppKit `NSStatusItem`: <https://developer.apple.com/documentation/appkit/nsstatusitem>
