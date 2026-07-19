# Mnemosyne macOS Native Architecture

Status: implemented native architecture; target-Mac acceptance remains pending.

## Purpose

The macOS deployment provides one OpenAI/Anthropic-compatible endpoint for an
existing LM Studio installation, oMLX, DwarfStar (DS4), and a managed MFLUX
image worker. It preserves the
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
| 17320 | Mnemosyne Core | Unified inference API | `127.0.0.1` |
| 17321 | Mnemosyne Core | Control/admin API | `127.0.0.1` |
| 1234 | LM Studio | Existing native server | `127.0.0.1` |
| 17322 | oMLX | Native MLX engine | `127.0.0.1` |
| 17323 | DS4 | Managed native subprocess | `127.0.0.1` |
| 17324 | MFLUX | Managed native image worker | `127.0.0.1` |
| 17325-17329 | Reserved | Future local engines/diagnostics | unbound |

All ports remain configurable. Startup validates that inference and control
ports differ, all managed inner ports differ, and no configured inner port is
published beyond loopback. A port collision is reported; Mnemosyne never kills
an unknown process to reclaim a port.

The eventual application bundle has two cooperating components:

1. **Mnemosyne Core** is a per-user background service. It owns the catalog,
   global swap coordinator, proxy, usage outbox, and DS4/MFLUX subprocesses.
2. **Unified Inference.app** owns an explicit AppKit `NSStatusItem` and renders its
   controller as a SwiftUI popover. It reads status and sends commands through
   the control API. The UI may exit without interrupting an inference request.

The background service is a user LaunchAgent rather than a system daemon because
LM Studio belongs to the logged-in user's session. Distribution should bundle a
known Python runtime instead of depending on Apple's or Homebrew's Python.

## Ownership Rules

There is exactly one lifecycle owner: Mnemosyne Core.

- LM Studio remains an externally installed application. Mnemosyne never quits
  the application or its server; it loads and unloads model instances through
  LM Studio's native `/api/v1/models/*` REST API.
- oMLX remains a long-lived native service so its persistent SSD KV cache and
  model inventory survive model eviction. Mnemosyne controls its loaded engines
  through its admin API.
- DS4 is a model-specific process owned by Mnemosyne. Loading means spawning
  `ds4-server` with an explicit model and loopback port; unloading means graceful
  termination followed by a bounded forced kill if necessary.
- MFLUX is a dependency-isolated worker process owned by Mnemosyne. It exists
  only while an image profile is resident and is terminated on unload,
  timeout, or cancellation so Metal memory release follows process exit.
- Clients use Mnemosyne on port 17320. Direct client traffic to an inner engine
  can violate the single-resident invariant and is unsupported.

LM Studio JIT loading and oMLX pinning/automatic loading must not create models
outside the coordinator. Operators disable those behaviors during setup;
startup and periodic audits fail closed if an unexpected resident appears or
an enabled engine cannot report authoritative state.

## Model Profiles

Public aliases are globally unique and explicitly select an engine. Mnemosyne
does not infer the engine from a model name or file extension.

Conceptual configuration:

```yaml
server:
  inference_bind: 127.0.0.1
  inference_port: 17320
  control_bind: 127.0.0.1
  control_port: 17321
  idle_unload_seconds: 900
  startup_timeout_seconds: 900
  swap_queue_timeout_seconds: 300

engines:
  lmstudio:
    base_url: http://127.0.0.1:1234
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

models:
  - alias: local-qwen
    engine: lmstudio
    model: publisher/model-key
    load:
      context_length: 32768

  - alias: glm-5-2
    engine: omlx
    model: GLM-5.2-4bit

  - alias: deepseek-v4-flash
    engine: ds4
    model: /Volumes/Models/ds4flash.gguf
    load:
      context_length: 100000
      kv_disk_directory: /Volumes/ModelCache/ds4-kv
      kv_disk_space_mb: 8192

  - alias: krea-2-turbo
    engine: mflux
    model: krea/Krea-2-Turbo
    kind: image
    image:
      family: krea-2
      quantize: 8
      num_inference_steps: 8
      guidance_scale: 1
```

Engine-specific options live below `load`. Unknown options are rejected unless
placed in an explicit `extra_args` escape hatch. Secrets remain in the private
macOS environment file, never in YAML or SQLite; a future Keychain integration
can replace that file-backed source.

Each resolved profile exposes:

- public alias;
- engine and engine-native target;
- served model name used after request canonicalization;
- supported endpoint/capability set;
- load settings.

The control API combines profiles with coordinator state to expose availability
and the current resident alias.

LM Studio and oMLX inventories are inspected for lifecycle authority, but they
do not create implicit public aliases. DS4 profiles are always explicit because
DS4 accepts only its purpose-built GGUF layouts.

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

At startup the default recovery policy is `unload_all`: reconcile LM Studio and
oMLX, stop only previously recorded/owned child processes, and establish a clean
baseline. A future `adopt_single` policy may adopt one unambiguous loaded model.

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
- `GET /manager/usage`

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
`model`.

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
2. LM Studio adapter and lifecycle integration tests with mocked HTTP.
3. oMLX adapter, including explicit validation of programmatic unload auth.
4. DS4 subprocess adapter and readiness/termination tests using a fake server.
5. Inference proxy, endpoint capability checks, and usage normalization.
6. SQLite analytics/outbox and existing Postgres schema integration.
7. LaunchAgent templates and developer CLI.
8. AppKit status item, SwiftUI controller popover, bundled service supervision,
   and onboarding checks.
9. Native macOS smoke checklist for the remaining target-machine acceptance.

## External References

- LM Studio model management: <https://lmstudio.ai/docs/developer/rest>
- oMLX: <https://github.com/jundot/omlx>
- DwarfStar: <https://github.com/antirez/ds4>
- MLX unified memory: <https://ml-explore.github.io/mlx/build/html/usage/unified_memory.html>
- Docker Model Runner execution model: <https://docs.docker.com/ai/model-runner/#execution-environment>
- Apple `SMAppService`: <https://developer.apple.com/documentation/servicemanagement/smappservice>
- AppKit `NSStatusItem`: <https://developer.apple.com/documentation/appkit/nsstatusitem>
