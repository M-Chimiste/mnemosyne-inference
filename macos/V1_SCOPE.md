# Unified Inference macOS V1 scope

V1 is the migration-safe local inference product. It does not depend on LM
Studio as a process, API, credential owner, or inference engine. LM Studio's
configured and conventional model folders remain read-only discovery hints so
existing weights can be adopted without copying them.

## Stable in V1

- The menu app, per-user background service, public OpenAI-compatible listener
  on `127.0.0.1:1240`, and control listener on `127.0.0.1:17321`.
- Global one-model residency, lease-safe engine switching, idle unload,
  reconciliation, restart-safe process ownership, and fail-closed degraded
  state.
- llama.cpp for GGUF generation, including GGUF metadata, automatic compatible
  projector selection, explicit text-only opt-out, and manually selected
  projectors.
- oMLX for compatible MLX generation, embeddings, and rerank models. oMLX is an
  independently owned loopback service; Unified Inference can offer an
  explicitly approved stable Homebrew install but never owns or silently
  replaces its files.
- Finder-driven local model adoption and engine-aware Hugging Face downloads.
  Model-card Markdown/HTML, architecture, parameter count, context length,
  license, quantization, and vision metadata are shown when authoritative
  repository or GGUF metadata supplies them.
- Durable download state, progress, throughput, cancellation, profile
  registration retry, history removal, and explicit managed-weight deletion.
- Local token accounting and durable optional Postgres delivery with a
  secret-safe connection editor and redacted health diagnostics.
- Guided Setup & Health. First-run setup is complete only after a real request
  passes through the public listener and any token usage is durably recorded.

## Preview in V1

- DS4 language inference.
- MFLUX image generation.
- mlxcel native MLX generation and vision-language inference.
- mistral.rs Safetensors language and multimodal inference.
- Exact-model cross-engine benchmarking and opt-in automatic selection. The
  Stable profile remains the fallback whenever evidence is missing or stale.
- Explicit per-model engine pinning when answer quality or compatibility
  should override the benchmark winner; the original profile remains the
  pre-work fallback.

Preview engines remain installable and usable, but the UI and control API label
them as Preview and fresh configurations leave them disabled. A missing
Preview runtime cannot make the Stable core unavailable; enabling an absent
manager-owned runtime remains an actionable not-ready state. A port occupied
by an unknown process still fails closed because global single-residency cannot
be proven. Preview engines do not block a V1 release once their isolation,
opt-in behavior, and diagnostics pass; real DS4 model coverage and MFLUX
cancellation/Metal-release matrices remain post-V1 promotion gates.

## Deliberately out of scope

- An LM Studio adapter, server connection, model inventory bridge, credential,
  process controller, or runnable profile.
- Multiple simultaneously resident models or remote multi-user scheduling.
- Embedding upstream serving implementations in this repository.
- Automatic copying or deletion of model weights discovered outside a managed
  download root.
- A promise that arbitrary Hugging Face repositories or arbitrary GGUF/MLX
  layouts are compatible.

## V1 release gates

A `v1.0.0` release may be published only when all of these are evidenced:

1. Native service, image-worker, Swift, packaging, lock, and version checks pass
   in the macOS CI workflow.
2. A clean Apple Silicon installation completes Setup & Health, survives a
   logout/login cycle, and keeps inference alive when the menu app quits.
3. Real llama.cpp text and vision requests and a real oMLX request pass through
   `:1240`; token totals are written locally and the configured Postgres outbox
   drains idempotently.
4. A protected external model folder survives service restart and a
   manager-owned child can reactivate its bookmark.
5. Download, cancel, retry registration, remove history, and optional managed
   deletion pass without loading weights or escaping the selected storage root.
6. The exact tagged app is signed with Developer ID, hardened, notarized,
   stapled, Gatekeeper-assessed, mounted from its DMG, and reverified.
7. A notarized older build updates to the candidate through the signed appcast.
   A rejected/tampered update leaves the installed app intact, and the previous
   notarized DMG remains available for manual rollback without deleting
   Application Support data.

The machine-readable evidence ledger is
[`acceptance/v1.json`](acceptance/v1.json). A release candidate may remain at
`0.9.x`; the version source must not move to `1.0.0` while a required gate is
pending.
