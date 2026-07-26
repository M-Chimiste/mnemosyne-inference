# Unified Inference 0.9

This release candidate focuses the macOS product on a reliable migration away
from LM Studio while preserving its conventional model directory as a
read-only discovery hint.

- Guided Setup & Health verifies the service, engines, storage, configured
  models, vision support, inference, token accounting, and Postgres delivery.
- llama.cpp and oMLX are the V1 Stable language engines. DS4 and MFLUX remain
  clearly labeled Preview while their remaining hardware acceptance checks are
  completed.
- Hugging Face and GGUF metadata supply richer model cards, context length,
  architecture, parameters, license, and automatic vision-projector selection.
- Downloads expose live progress, throughput, durable registration recovery,
  history removal, and explicit managed-weight deletion.
- The production pipeline supports signed Sparkle updates, enforces one
  version, and refuses release without Developer ID signing and notarization.
  Private ad-hoc candidates keep application updates disabled.

Configuration, downloaded models, engine runtimes, and token-usage history live
below Application Support and are preserved across app updates or a manual
rollback to a previous notarized DMG.
