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
- The macOS engine surface is consolidated to llama.cpp, oMLX, DS4, and MFLUX.
  Existing mlxcel and mistral.rs settings and profiles remain upgrade-readable
  but inert; the app does not launch them, advertise them, or delete their
  external installations or model weights.
- The reusable Mac product calls its pooling coordinator the **Hub** throughout
  enrollment, invitations, approval, removal, routing, downloads, and
  credential guidance. Nyx remains only the name of this deployment's host;
  it is not baked into the operator-facing pairing experience.
- A Mac can now be promoted from **Settings → Hub Mode** without a manual Fleet
  installation. The app creates and preserves separate Hub credentials,
  bundles Fleet behind its own login service on loopback `17400`, publishes
  authoritative local model mappings, and enrolls the same Mac's independent
  inference worker as **LIMITED / overflow**. Tailscale Serve is the guided
  private-HTTPS path; an existing HTTPS proxy remains an advanced option.
- A managed Tailscale Hub now accepts OpenAI-compatible `/v1/*` requests from
  authenticated tailnet users without a client API key. New managed Hubs leave
  that key unset; Hub administration can generate, copy, or remove an optional
  bearer fallback for tagged devices and clients that need one. The gateway
  trusts Tailscale identity only from its loopback Serve peer, and the admin
  API always keeps its separate mandatory key.
- Hub Mode settings now discover pending Macs, accept the six-digit pairing
  code in the native app, wait for activation proof, and perform the separate
  enable transaction. A disabled enrollment can be enabled there after an
  interrupted ceremony. The browser dashboard remains an Advanced surface for
  service classes, catalog overrides, revocation, and manual interoperability.
- Pairing now defaults to a familiar six-digit-code flow: enter the Hub HTTPS
  address on the Mac, choose **Request to Join**, then enter the displayed code
  in the Hub Mac's native settings and choose **Pair & Enable**. The app discovers the Mac's
  Tailscale address, keeps the strong invitation and role credentials hidden,
  resumes automatically, and the Hub waits for activation proof before its
  separate enable transaction. The original manual ceremony remains under
  Advanced.
- **Refresh Status** now asks the exact Hub for the old claim's bounded
  disposition instead of rereading only local state. A conclusively rejected
  pre-claim attempt or a fully matched Hub-confirmed expired/rejected claim can
  be removed with **Discard Stale Attempt** and replaced with a fresh code. The
  recovery remains unavailable for active claims, locally assigned
  credentials, or ambiguous network outcomes; models, storage, inference,
  usage, and token accounting remain unchanged.
- The menu-bar pool participation control uses the native macOS switch style
  while retaining the same durable join, drain, pause, and rejoin behavior.
- Fresh installations request both the inference LaunchAgent and menu-app login
  registrations on first launch. Existing installations retain their choices,
  and a later explicit disable is never automatically reversed.
- Pilot upgrades now use the ordinary Finder replacement flow: quit the menu
  app, drag the new app to Applications, choose Replace, and open it. The new
  bundle refreshes previously enabled login registrations on first launch,
  while private configuration, token accounting, model paths, runtimes, and
  weights remain outside the replaced bundle.
- Replacing the app also refreshes an enabled Hub login registration. An
  explicitly disabled Hub remains disabled, while its credentials, pairings,
  inventory, local mappings, and route metadata remain in Application Support.
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
- llama.cpp updates now follow the newest complete official numbered build
  containing a macOS ARM64 asset. They no longer stop at the older build named
  by a semantic release's frozen `nightly-tag.txt`; exact asset URL, published
  size, SHA-256, runtime validation, activation barriers, and rollback remain
  enforced.
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
