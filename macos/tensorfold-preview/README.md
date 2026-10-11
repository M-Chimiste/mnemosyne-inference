# TensorFold Preview

This is an explicitly authorized, isolated upstream Preview development build,
not an installed Unified Inference engine or a shipping release. No serving
implementation or TensorFold dependency is imported by the native coordinator.
Existing EXO processes, installed apps, active runtimes, and model files are
untouched.

## Source and patch

The Python/MLX source is official TensorFold `v0.6.6`, pinned to
`cb2ebf0540f42604e2759b2ddef497861e928248`. `pin.json` binds the patch by
SHA-256. The patch extends TensorFold's existing vision frontend and model
worker; it does not launch oMLX or load a second language model.

- GLM 5.3 Flash uses the existing upstream Mac image path.
- Qwen 3.8 Flash Next uses its own upstream MLX-VLM image tower, sharing the
  already loaded TensorFold language embedding table.
- Qwen image positions cover attention Q/K, sparse-indexer queries, pooled
  indexer keys, chunked prefill, and subsequent generated tokens.
- Qwen vision is explicitly serial (`--parallel 1`), without fused decoding
  or MTP. Those kernels are not image-qualified. No speed claim is made.
- Image prompts keep the upstream fresh-cache, bounded workspace, and URL
  opt-in policy. Remote image fetching is off by default.

## Reproduce

Use an Apple Silicon Mac with the macOS version required by upstream MLX and
a Python 3.11+ interpreter. Choose a fresh, private build directory outside
the installed app and coordinator environment:

```sh
python3 macos/tensorfold-preview/build_preview.py \
  --output /private/tmp/tensorfold-preview-candidate
```

An existing upstream Git checkout can supply `--local-source PATH`; only the
pinned commit is cloned, never its uncommitted edits. The script applies only
the integrity-checked patch, creates a separate virtual environment, installs
the exact upstream project's vision/test extras, and runs the selected tests.
It never updates or activates an installed runtime. Dependency versions are
recorded as observed build evidence, not an additional dependency manifest.

On October 10, 2026, the selected suite passed **214 tests** on an M4 Max,
including CPU and Metal tiny-model tests, real Qwen tower output affecting
language logits, sparse selection against official MLX-VLM, existing GLM
vision, and server image isolation/limits. This is not full-model acceptance.
The final patch also passed 108 additional upstream text/prefill regression
tests with four skipped tests. The isolated builder's seven local integrity
and non-activation tests passed. A clean checkout and non-editable wheel
installation reproduced the 214-test vision result.

The additional regression command is:

```sh
PATH_TO_CANDIDATE/venv/bin/python -m pytest \
  tests/test_qwen4_exp_prefill.py tests/test_qwen4_exp_prefill_kernels.py \
  tests/test_qwen4_exp_table_scale.py tests/test_flash_next_prefill_rows_env.py \
  tests/test_cuda_cli.py -q
```

Run it from the candidate's `source` directory, not the coordinator environment.

## Required Delivery Gates

Delivery remains blocked until all of the following are implemented and pass:

1. Default-off native engine integration, managed ownership, configuration,
   settings, installer, and runtime activation/rollback.
2. Real GLM and Qwen image understanding on the intended 512 GB M3 Ultras,
   including changing-image, multi-image, streaming, cancellation, context
   limits, reload, and exactly-once accounting tests.
3. Matched Thunderbolt/MCDMA runtime builds, explicit device ordering, peer
   authentication and leases, follower-first startup, both-host draining,
   link-failure recovery, and a demonstrated performance benefit.
4. Vision in the intended cluster workflow without ignoring image input or
   keeping duplicate models resident through another engine.
5. Developer ID signing, notarization/stapling, Gatekeeper assessment, and
   signed-artifact acceptance before Desktop delivery.

The native TensorFold `v1.0.4` paired text runtime and this Python vision
candidate are different upstream implementations. This patch does **not**
add Thunderbolt vision to the native runtime, pool the Studios' RAM, or
prove that transport qualification is full-model inference qualification.
The intended product is a single, explicitly selected TensorFold engine;
no automatic oMLX fallback is authorized.

Upstream's Apache-2.0 license and NOTICE are retained here and in the cloned
candidate. Patched source files explicitly identify the Preview modifications;
upstream and third-party notices in the checkout are not removed.
