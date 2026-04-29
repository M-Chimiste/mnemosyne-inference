# Phase 8 — Acceptance verification log

This file is the v1 release artifact. Each row maps a PRD §7 acceptance
criterion to (a) the test that covers it on a non-CUDA host and (b) the
manual smoke section that backs it on a workstation. The `Workstation`
column is filled in by the operator after the live pass.

- **Tests:** `python -m pytest -q` reports **253 passed** today
  (247 baseline + 1 multimodal + 4 logsetup + 1 corruption recovery).
- **Smoke checks:** [smoke_checks.md](smoke_checks.md). Section 9 (vision)
  is new in Phase 8.

## PRD §7 acceptance scenarios

| # | PRD criterion | Test reference | Smoke section | Workstation |
|---|---|---|---|---|
| 1 | Editing `config.yaml` adds/removes/renames an alias and reloads without restart. | [tests/test_reload.py:32-49](../tests/test_reload.py) | §1 (Cold load) + reload via SIGHUP | ☐ |
| 2 | Single-GPU profile launches with `tp=1`; all-GPU profile derives `tp` from visible GPUs. | [tests/test_runtime.py:192-207](../tests/test_runtime.py) | §1 (Cold load) | ☐ |
| 3 | First `/v1/*` request lazy-loads the resident vLLM. | [tests/test_proxy.py:80-156](../tests/test_proxy.py) | §2 (Direct proxy) | ☐ |
| 4 | `/v1/*` for a different alias triggers an auto-swap. | [tests/test_swap_queue.py:107-121](../tests/test_swap_queue.py) | §4 (Auto-swap) | ☐ |
| 5 | Swap-queue timeout returns `504`. | [tests/test_swap_queue.py:127-155](../tests/test_swap_queue.py) | §4 (with timeout reduced via config) | ☐ |
| 6 | Idle eviction fires after `idle_unload_seconds` of inactivity. | [tests/test_eviction.py:64-133](../tests/test_eviction.py) | §8 (Unload) + idle observation | ☐ |
| 7 | Admin endpoints 404 on the inference plane. | [tests/test_planes.py:10-13](../tests/test_planes.py) | §7 (Plane separation) | ☐ |
| 8 | `install` / `install-cancel` / `install-retry` / restart recovery. | [tests/test_install.py](../tests/test_install.py) + [test_install_recovery.py](../tests/test_install_recovery.py) | §6 (Download lifecycle) | ☐ |
| 9 | Multi-drive install routes weights to the named storage. | [tests/test_cache_delete.py:88-117](../tests/test_cache_delete.py) | §6 with `--storage <name>` | ☐ |
| 10 | Cache-only delete demotes to `partial`; `--remove-row` cleans the alias. | [tests/test_cache_delete.py:40-86](../tests/test_cache_delete.py) | §6 + `vllm-ctl cache-delete` | ☐ |
| 11 | Vision `image_url` content blocks reach the model end-to-end. | [tests/test_multimodal.py](../tests/test_multimodal.py) (proxy passthrough only) | §9 (Vision smoke — new) | ☐ |
| 12 | Container down/up survives a partial download. | [tests/test_install_recovery.py](../tests/test_install_recovery.py) | §6 + `vllm-ctl restart` mid-download | ☐ |

## Hardening / failure-mode scenarios (PRD §5.3 + §5.13)

| Mode | Test reference | Smoke check | Workstation |
|---|---|---|---|
| vLLM crash during load | [tests/test_swap_queue.py — test_load_failure_raises_503](../tests/test_swap_queue.py) | §5 (Bad model id) | ☐ |
| Manager restart during download | [tests/test_install_recovery.py](../tests/test_install_recovery.py) | `vllm-ctl restart` mid `vllm-ctl install` | ☐ |
| Missing storage mount | [tests/test_install_recovery.py:195-212](../tests/test_install_recovery.py) | `vllm-ctl storage` after removing a bind-mount | ☐ |
| Corrupt SQLite | [tests/test_catalog.py — test_corrupt_db_is_quarantined_and_replaced](../tests/test_catalog.py) | Inject garbage into `/state/mnemosyne.db`; restart container; confirm fresh DB + `*.corrupt-*` sibling | ☐ |
| `ADMIN_PASSWORD` missing + non-loopback bind | [tests/test_planes.py:58-66](../tests/test_planes.py) | Unset `ADMIN_PASSWORD`, restart, confirm `:8001` published port refuses host-side connections | ☐ |

## Error-message polish (PRD §5.7)

| Case | Source | Expected message contains | Workstation verification |
|---|---|---|---|
| Missing HF token (search) | [hf_search.py:584](../hf_search.py) | `set HUGGING_FACE_HUB_TOKEN` | Search a gated repo without a token. |
| Missing HF token (download) | [downloader.py](../downloader.py) | `HuggingFace authentication failed`, `set HUGGING_FACE_HUB_TOKEN`, `raw:` | Install a gated repo with no token; check the install row error. |
| Bad config | [config.py:232,236,238,242](../config.py) | `ConfigError` with file path | Break `config.yaml`; container start logs the error. |
| Insufficient disk | [vllm_manager.py:1043](../vllm_manager.py) | `insufficient free space` with required GB | Try installing into an over-full mount. |
| Bad GPU index (config) | [config.py:204](../config.py) | `ConfigError` with the bad index | Set `gpus: [9]` in `config.yaml`. |
| vLLM startup failure | [vllm_manager.py:204](../vllm_manager.py) | `exit_code=`, `see container logs` | Force an OOM with an oversized `gpu_memory_utilization`. |

## Observability / logging

| Item | Test reference | Workstation verification |
|---|---|---|
| JSON formatter shape | [tests/test_logsetup.py](../tests/test_logsetup.py) | Manager log lines are JSON; vLLM child stdout/stderr may appear as raw text in the same container log stream. Before launching a model, confirm startup/status manager lines parse with `jq`; after launch, filter JSON lines before piping to `jq`. |
| Text fallback when `MNEMOSYNE_LOG_FORMAT=text` | [tests/test_logsetup.py](../tests/test_logsetup.py) | Set the env var in `docker-compose.yml`, restart, confirm the legacy format. |

## Sign-off

- Static checks: pytest 253 passed, `npm test` 9 passed, `npm run build` clean,
  `py_compile` clean, `bash -n vllm-ctl` clean.
- Workstation pass: ☐ — record the date and operator.
- Open follow-ups closed: ☐ — Phase 5 bundled snapshot regenerated.

When every checkbox above is filled in, Phase 8 may be marked ✅ in
`project_status.md` and v1 is shipped.
