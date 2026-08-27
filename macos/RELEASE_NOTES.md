# Unified Inference 0.9

This release candidate focuses the macOS product on a reliable migration away
from LM Studio while preserving its conventional model directory as a
read-only discovery hint.

- Guided Setup & Health verifies the service, engines, storage, configured
  models, vision support, inference, token accounting, and Postgres delivery.
  Clean-install evidence is scoped to the exact app version/build and records
  first presentation separately from completion, so an older preference bit
  cannot clear the release gate.
- General settings can expose only the public inference listener to the local
  network. Bearer authentication is optional in the same style as LM Studio:
  a configured Inference API key protects `/v1/*`, while an unset key permits
  unauthenticated clients. The control and inner-engine listeners remain
  loopback-only.
- llama.cpp and oMLX are the V1 Stable language engines. DS4 and MFLUX remain
  clearly labeled Preview while their remaining hardware acceptance checks are
  completed.
- Preview mlxcel and mistral.rs adapters add two isolated native Apple Silicon
  language paths without changing existing fixed profiles. They use strict
  manager-owned child-process identity while their official Homebrew/installer
  binaries remain externally owned and independently upgradeable.
- A profile can attach one exact candidate per engine and opt into durable
  cross-engine selection. Repeated streamed samples rank first-token latency,
  throughput, or a balanced objective; stale, failed, marginal, or unapproved
  Preview evidence always falls back to the original engine. Benchmark state
  retains metrics and hashed identities only.
- Each model can instead pin a declared engine, bypassing benchmark ranking
  when generation quality or compatibility matters more than measured speed.
  The Settings sidebar now shows the app version and build beside the product
  name for at-a-glance support identification.
- Native inference now uses oMLX's authoritative scheduler capacity instead of
  serializing it at one request, keeps fresh-install residents warm, bounds new
  GGUF contexts to responsive interactive defaults, and exposes content-free
  p50/p95, cold-start, admission, first-byte, and streamed throughput metrics.
- Runtime Updates distinguishes official-app, stable Homebrew, Homebrew HEAD,
  and other external oMLX ownership. Stable Homebrew updates are explicitly
  confirmed, globally drained, delegated to fixed owner commands, restarted,
  and validated. The card also reports vendor cache effectiveness and offers
  an explicit drain-safe SSD KV-cache reset that never touches model weights.
- DS4 discovery now exposes its current nine single-node targets across
  DeepSeek V4 and GLM 5.2. Exact Hub files and immutable revisions are verified
  before install, including all eleven shards of the supported Unsloth GLM Q4.
  DS4 profiles also gain typed resident-session batching and matching
  coordinator/Fleet capacity instead of remaining permanently serialized.
- A fixed-prompt, content-redacted benchmark can compare Unified Inference and
  any OpenAI-compatible local endpoint under the same concurrency.
- Hugging Face and GGUF metadata supply richer model cards, context length,
  architecture, parameters, license, and automatic vision-projector selection.
- The Model Library now searches one cross-engine catalog with explicit engine
  support badges. Hugging Face front matter is removed and safe Markdown is
  rendered in a readable scrollable card instead of clipped raw text.
- Downloads expose live progress, throughput, durable registration recovery,
  history removal, explicit managed-weight deletion, and a compact transition
  journal so release acceptance can prove cancel/retry/dismiss/delete behavior
  after the visible history is cleared.
- The private acceptance report can explicitly exercise exact-label service
  restart or KeepAlive recovery and require protected-folder reactivation,
  native LM Studio-directory adoption, oMLX reconciliation, Postgres drain,
  and the complete download lifecycle without exposing credentials.
  A private pre-logout report can also prove real login/reboot recovery through
  a changed GUI audit-session ID; an ordinary restart cannot substitute.
- Managed runtime activation, post-restart inference, rollback,
  post-rollback inference, and corrupt-runtime rejection now leave a bounded
  private lifecycle proof. Anonymous service-instance IDs distinguish a real
  restart while fixed failure codes avoid retaining exception text.
- The production pipeline supports signed Sparkle updates, enforces one
  version, and refuses release without Developer ID signing and notarization.
  Private ad-hoc candidates keep application updates disabled.
  Packaged Sparkle now has an explicit `Contents/Frameworks` executable rpath,
  and both release verification and artifact acceptance reject an app whose
  dynamic framework dependency cannot resolve at launch.

Configuration, downloaded models, engine runtimes, and token-usage history live
below Application Support and are preserved across app updates or a manual
rollback to a previous notarized DMG.
