# Phase 8 — Verification, Hardening, and Release Readiness

## Context

[project_docs/implementation_plan.md](/Users/c/software_projects/mnemosyne-inference/project_docs/implementation_plan.md) Phase 8 is the final pass before treating the v1 PRD as implemented. Phases 0–7 are landed (with the Phase 5 bundled-snapshot regeneration as an open follow-up). The committed test suite is at 247 tests passing; the project_status.md "Verification" checklist already covers most acceptance scenarios.

What's left is a hardening + verification pass:

1. **Acceptance scenarios from PRD §7.** Most are covered by tests already; this phase just runs them and records results, plus closes the small known gaps.
2. **Multimodal validation.** PRD §5.5 says "we need an integration test against a small vision model to prove [the OpenAI-format image content blocks proxy unchanged]." Today the test suite has zero multimodal coverage — `_proxy` doesn't inspect bodies, but we don't have an explicit test that proves it.
3. **Observability polish.** PRD §5.7 calls for "Structured JSON logs (we already log to stdout — formalize the format)." Today every module uses stdlib `logging` with a text formatter. Format only needs swapping at one site; ~50 call sites stay as-is.
4. **Error-message tightening** for the five PRD-named cases: missing HF token, bad config, insufficient disk, bad GPU index, vLLM startup failure. Most are already user-friendly; two need polish.
5. **Failure-mode review** for five PRD-named scenarios: vLLM crash, manager restart during download, missing storage mount, corrupt SQLite, ADMIN_PASSWORD missing with non-loopback bind. Four have explicit handlers + tests today; corruption detection is the one gap.
6. **Deferred-features documentation.** Phase 8 calls out specific items "remain deferred." PRD §8 already lists them as Stretch/later — surface them in the README so users know what v1 doesn't do.

This phase is intentionally narrow: no new features, no architectural changes. It's a polish pass + verification log + a small handful of new tests + documentation.

---

## Survey of current state

Inventory pass results — file:line references behind every claim.

### Logging

| File | Logger | Lines | Format |
|---|---|---|---|
| [vllm_manager.py:67-72](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L67) | `vllm-manager` | 33 calls | text via `basicConfig` |
| [downloader.py](/Users/c/software_projects/mnemosyne-inference/downloader.py) | `vllm-manager.downloader` | uses INFO/WARNING | text |
| [hf_search.py](/Users/c/software_projects/mnemosyne-inference/hf_search.py) | `vllm-manager.hf_search` | uses INFO/WARNING | text |
| [catalog.py](/Users/c/software_projects/mnemosyne-inference/catalog.py) | `vllm-manager.catalog` | uses INFO/WARNING | text |
| [config.py](/Users/c/software_projects/mnemosyne-inference/config.py) | `vllm-manager.config` | uses WARNING/DEBUG | text |
| [download_worker.py](/Users/c/software_projects/mnemosyne-inference/download_worker.py) | (subprocess JSON IPC) | already JSON-on-stdout to parent | JSON (parent-only) |

Total ~50 user-facing log lines. The download worker already emits line-delimited JSON for IPC; that's an internal channel, not a log stream.

### Error messages (PRD-named cases)

| Case | Current handler | Status |
|---|---|---|
| Missing HF token (gated repo) | [hf_search.py:584](/Users/c/software_projects/mnemosyne-inference/hf_search.py#L584) returns `"hub unauthorized — set HUGGING_FACE_HUB_TOKEN"` | ✅ search side good |
| Missing HF token (download) | [downloader.py:103](/Users/c/software_projects/mnemosyne-inference/downloader.py#L103) `_build_worker_env` passes token silently; subprocess receives an HF error → catalog `error` row with raw message | ⚠️ raw `huggingface_hub` message reaches the user; needs hinting |
| Bad config | [config.py:232,236,238,242](/Users/c/software_projects/mnemosyne-inference/config.py#L232) — five `ConfigError` raises with file path | ✅ |
| Insufficient disk | [vllm_manager.py:1043](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L1043) `"insufficient free space at '{path}': have {free} GB, need ~{needed} GB"` | ✅ |
| Bad GPU index (config) | [config.py:204](/Users/c/software_projects/mnemosyne-inference/config.py#L204) `ConfigError` with the bad index | ✅ |
| Bad GPU runtime fallback | [vllm_manager.py:189-195](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L189) — soft-fall warning when `gpus='all'` + nvidia-smi empty | ⚠️ logs warning, then launches with DEFAULT_TP — silent fallback may mask a missing nvidia-container-toolkit |
| vLLM startup failure | [vllm_manager.py:204](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L204) `RuntimeError(f"vLLM failed to become ready for alias '{alias}'")` | ⚠️ generic; vLLM stderr is inherited so users *can* see it in container logs, but the RuntimeError doesn't tell them where to look |

### Failure modes (PRD-named cases)

| Mode | Handler | Test | Status |
|---|---|---|---|
| vLLM crash during load | [vllm_manager.py:141-143,205-208](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L141) | [test_swap_queue.py::test_load_failure_raises_503](/Users/c/software_projects/mnemosyne-inference/tests/test_swap_queue.py) | ✅ |
| Manager restart during download | [downloader.py::reap_orphans_on_startup](/Users/c/software_projects/mnemosyne-inference/downloader.py), [vllm_manager.py:563-569](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L563) | [test_install_recovery.py](/Users/c/software_projects/mnemosyne-inference/tests/test_install_recovery.py) | ✅ |
| Missing storage mount | [vllm_manager.py:1021-1024](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L1021) (install-time 400), [config.py:209-216](/Users/c/software_projects/mnemosyne-inference/config.py#L209) (boot warning), [catalog.reconcile_cache](/Users/c/software_projects/mnemosyne-inference/catalog.py) | [test_install_recovery.py:195-212](/Users/c/software_projects/mnemosyne-inference/tests/test_install_recovery.py#L195) | ✅ |
| Corrupt SQLite | [catalog.py:222,226](/Users/c/software_projects/mnemosyne-inference/catalog.py#L222) (WAL + foreign_keys ON); no `PRAGMA integrity_check` on open | none | ⚠️ no proactive corruption guard |
| ADMIN_PASSWORD missing + non-loopback | [vllm_manager.py:1880-1894](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L1880) `_resolve_admin_bind` forces 127.0.0.1 + warning | [test_planes.py:58-66](/Users/c/software_projects/mnemosyne-inference/tests/test_planes.py#L58) | ✅ |

### Multimodal

Zero references to `image`, `vision`, `multimodal`, or `content_block` in tests. The `_proxy` flow is body-agnostic ([vllm_manager.py:1618-1711](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L1618)) — it streams bytes through with no JSON inspection beyond reading `model`. So passthrough already works; what's missing is an explicit test that says so.

### Acceptance scenarios

| PRD §7 criterion | Coverage |
|---|---|
| Config alias reload + no restart | [test_reload.py:32-49](/Users/c/software_projects/mnemosyne-inference/tests/test_reload.py#L32) ✅ |
| Single GPU + all-GPU launch argv | [test_runtime.py:192-207](/Users/c/software_projects/mnemosyne-inference/tests/test_runtime.py#L192) ✅ |
| Lazy load via `/v1/*` | [test_proxy.py:80-156](/Users/c/software_projects/mnemosyne-inference/tests/test_proxy.py#L80) ✅ |
| Auto-swap via `/v1/*` | [test_swap_queue.py:107-121](/Users/c/software_projects/mnemosyne-inference/tests/test_swap_queue.py#L107) ✅ |
| Swap queue timeout (504) | [test_swap_queue.py:127-155](/Users/c/software_projects/mnemosyne-inference/tests/test_swap_queue.py#L127) ✅ |
| Idle eviction | [test_eviction.py:64-133](/Users/c/software_projects/mnemosyne-inference/tests/test_eviction.py#L64) ✅ |
| Admin ops 404 on inference plane | [test_planes.py:10-13](/Users/c/software_projects/mnemosyne-inference/tests/test_planes.py#L10) ✅ |
| Install / cancel / retry / restart | [test_install.py](/Users/c/software_projects/mnemosyne-inference/tests/test_install.py) + test_install_recovery.py ✅ |
| Multi-drive install | [test_cache_delete.py:88-117](/Users/c/software_projects/mnemosyne-inference/tests/test_cache_delete.py#L88) ✅ |
| Cache delete behavior | [test_cache_delete.py:40-86](/Users/c/software_projects/mnemosyne-inference/tests/test_cache_delete.py#L40) ✅ |
| Vision `image_url` end-to-end | **none** — Phase 8 work |
| Container down/up survives partial download | test_install_recovery.py ✅ |

---

## Approach

Six concrete deliverables. Each is small and orthogonal — implementable in any order, but reviewable and verifiable independently.

### D1 — Structured JSON logging (PRD §5.7)

**Approach:** add one `JsonLogFormatter(logging.Formatter)` class in a new tiny module [logsetup.py](/Users/c/software_projects/mnemosyne-inference/logsetup.py). Install it from `vllm_manager.py` instead of the current `basicConfig` text format. **No call-site changes** — the formatter consumes existing `LogRecord` objects.

Output shape (one JSON object per line, stdout):

```json
{"ts":"2026-04-29T14:32:18.412Z","level":"INFO","logger":"vllm-manager","msg":"✓ Loaded alias='qwen-72b-awq' model='Qwen/...' tp=2 gpu_mem=0.90"}
```

Includes `exc_info` (renderered traceback string) when present. Includes any `extra` dict fields the call site passes (none today, but the door is open).

Gated on `MNEMOSYNE_LOG_FORMAT={json|text}`. Default is **`json`** unless
`MNEMOSYNE_LOG_FORMAT=text` is explicitly set. Tests that care about human
readable output should set `MNEMOSYNE_LOG_FORMAT=text`; do not auto-detect
pytest. Gate via env var sniff at logging configuration time, not by re-checking
on every call.

**Why not refactor every call to use `extra=`:** the current f-string log lines are already readable at the human level. Adding structured fields per call would touch ~50 sites for marginal value. The JSON wrapper alone gives operators what they need (greppable level/logger/msg) without churn.

**Out of scope:** request-level structured logs in `_proxy` (would need request IDs). Defer.

### D2 — Multimodal proxy passthrough test

**Approach:** add `tests/test_multimodal.py` with one test that uses the existing `rich_client` + `_patch_upstream` pattern from test_proxy.py. POSTs an OpenAI-format chat completion with a multi-part `content` array containing both text and `image_url` blocks. Asserts the body bytes reach the upstream stub byte-for-byte unchanged.

```python
def test_proxy_passes_image_content_blocks_unchanged(rich_client, monkeypatch):
    client, stub = rich_client
    captured = {}
    async def _open_upstream(request, path, body):
        captured["body"] = body
        return _FakeClient(), _FakeResponse()
    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)
    payload = {
        "model": "a-model",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "what is in this image?"},
                {"type": "image_url", "image_url": {"url": "https://..."}},
            ]},
        ],
    }
    r = client.post("/v1/chat/completions", json=payload)
    assert r.status_code == 200
    assert json.loads(captured["body"]) == payload
```

**Live workstation smoke check:** add Section 8 to [project_docs/smoke_checks.md](/Users/c/software_projects/mnemosyne-inference/project_docs/smoke_checks.md) — install a small vision model (Qwen/Qwen2.5-VL-7B-Instruct or similar), POST an `image_url` request, verify the model returns a coherent caption. This exercises the real vLLM multimodal path, but requires the GPU host so it stays manual.

### D3 — Error message polish (two cases)

**D3a. Download HF token gating** ([downloader.py:103-108](/Users/c/software_projects/mnemosyne-inference/downloader.py#L103) + worker error reporting). When the worker's `snapshot_download` raises a `GatedRepoError` or `RepositoryNotFoundError` with a 401/403, surface that as `error` with the explicit hint `"set HUGGING_FACE_HUB_TOKEN in /config/.env and restart"` rather than the raw `huggingface_hub` exception text. Touch [download_worker.py](/Users/c/software_projects/mnemosyne-inference/download_worker.py) error event emission to detect those exception classes and rewrite the message. Preserve the raw cause by appending it to the same existing error string after `"raw: ..."`. Do **not** add a new catalog column or API field for this in Phase 8.

**D3b. vLLM startup-failure error text** ([vllm_manager.py:204](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L204)). Capture the inner vLLM's exit code (when the process has exited) into the RuntimeError text. Add a one-line hint pointing at `docker compose logs` for the full stderr. Keep stderr inheritance — don't capture into a ring buffer, that's gold-plating.

```python
exit_code = vllm_process.poll() if vllm_process else None
detail = (
    f"vLLM failed to become ready for alias '{profile.alias}' "
    f"(exit_code={exit_code}; see container logs for vLLM stderr — "
    f"common causes: OOM, invalid quantization, missing weights)"
)
raise RuntimeError(detail)
```

**Out of scope:** runtime "no GPUs visible" hardening on `gpus='all'`. The existing warning is already loud, and tightening it to a hard error would break `pytest` on macOS hosts (no nvidia-smi). Documented as a Known Limitation instead.

### D4 — SQLite corruption guard

**Approach:** add an open-time corruption guard around [catalog.py::open_catalog](/Users/c/software_projects/mnemosyne-inference/catalog.py). The guard must run before returning a `Catalog` and must handle `sqlite3.DatabaseError` / `sqlite3.OperationalError` from early PRAGMAs or bootstrap, not only a successful `PRAGMA quick_check`.

Concrete policy:

1. Try to open the DB and construct `Catalog` normally.
2. During `Catalog.__init__`, after WAL setup where possible, run
   `PRAGMA quick_check;`. If it does not return exactly one row with `"ok"`,
   raise a local `CatalogCorruptionError`.
3. In `open_catalog`, catch `CatalogCorruptionError`, `sqlite3.DatabaseError`,
   and `sqlite3.OperationalError` from open/bootstrap. Close the failed
   connection if one exists.
4. Quarantine the DB files by renaming:
   - `<db>` → `<db>.corrupt-<YYYYmmddHHMMSS>`
   - `<db>-wal` → matching `.corrupt-...-wal` if present
   - `<db>-shm` → matching `.corrupt-...-shm` if present
5. Log `ERROR` with the original DB path, quarantine paths, and the exception.
6. Open a fresh DB at the original path and return a new `Catalog`; startup
   `apply_config` + reconcile will repopulate config-defined aliases and
   recover installed/partial state from storage.

Add `PRAGMA wal_checkpoint(PASSIVE)` during successful open after `quick_check`
to bound WAL growth across restarts. Ignore checkpoint failures on in-memory DBs.

Add one test in `tests/test_catalog.py` that opens a catalog, deliberately writes
garbage into the file after closing, reopens via `open_catalog`, and asserts:
fresh open succeeds, a `*.corrupt-*` quarantine file exists, and an ERROR log
mentions the quarantine. Do not assume SQLite raises at a specific operation;
the test should accept either quick_check failure or bootstrap/open failure as
the trigger.

### D5 — Deferred-features and known-limitations doc

**Approach:** add one new H2 section to [README.md](/Users/c/software_projects/mnemosyne-inference/README.md): "Known v1 limitations." Lists each PRD §8 stretch goal as a one-line entry plus the workaround for the runtime cases:

- **No multi-model concurrent serving.** One vLLM at a time. To swap, send a request with the new alias.
- **No chat playground in the UI.** Use the OpenAI endpoint from your IDE / `curl`.
- **No Prometheus metrics.** `/manager/status` is the operational surface.
- **No startup pre-warm.** First `/v1/*` request triggers lazy load.
- **No automatic quantization-variant discovery on install.**
- **No vLLM auto-restart on crash.** The next request triggers a fresh load (PRD §5.3 fail-open).
- **No runtime hard-fail when `gpus='all'` finds no GPUs.** The manager logs a warning and falls back to `DEFAULT_TP`. On a real CUDA host this only happens if the nvidia-container-toolkit is misconfigured.

Cross-reference [project_docs/PRD.md §8](/Users/c/software_projects/mnemosyne-inference/project_docs/PRD.md) so contributors see the canonical decision log.

### D6 — Acceptance verification log

**Approach:** add `project_docs/phase_8_acceptance.md` (one new file, ~120 lines) — a checklist that records for each PRD §7 criterion: the test that covers it (file:line), the smoke check that backs it on a CUDA host (smoke_checks.md section), and a pass/fail field the user can fill in once the workstation pass runs. This gives Phase 8 a concrete completion artifact rather than just "tests pass."

The existing [project_docs/smoke_checks.md](/Users/c/software_projects/mnemosyne-inference/project_docs/smoke_checks.md) already covers the runtime smokes; D6 doesn't duplicate them — it just maps PRD criteria → tests + smoke sections.

---

## Files

**New**
- `logsetup.py` — `JsonLogFormatter` + `configure_logging()` helper. ~50 lines.
- `tests/test_multimodal.py` — proxy passthrough test. ~40 lines.
- `tests/test_logsetup.py` — formatter shape test (level/logger/msg/exc_info). ~30 lines.
- `project_docs/phase_8_acceptance.md` — PRD criterion → test/smoke mapping. ~120 lines.
- `project_docs/plans/phase_8.md` — finalized plan for the archive (mirror of this file, repo-scoped).

**Modified**
- [vllm_manager.py](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py) — replace `logging.basicConfig` block at lines 67-72 with `from logsetup import configure_logging; configure_logging()`. Tighten the `vLLM failed to become ready` error text (line 204).
- [downloader.py](/Users/c/software_projects/mnemosyne-inference/downloader.py) — when worker reports a gated/auth error, rewrite the message to mention `HUGGING_FACE_HUB_TOKEN` (catalog.mark_error already accepts the message string).
- [download_worker.py](/Users/c/software_projects/mnemosyne-inference/download_worker.py) — distinguish `GatedRepoError` / 401-403 in the error event so the parent can retag.
- [catalog.py::open_catalog](/Users/c/software_projects/mnemosyne-inference/catalog.py) — add `PRAGMA quick_check` + `wal_checkpoint(PASSIVE)` after WAL pragma.
- [tests/test_catalog.py](/Users/c/software_projects/mnemosyne-inference/tests/test_catalog.py) — add corruption recovery test.
- [README.md](/Users/c/software_projects/mnemosyne-inference/README.md) — add "Known v1 limitations" section.
- [project_docs/smoke_checks.md](/Users/c/software_projects/mnemosyne-inference/project_docs/smoke_checks.md) — add Section 8 (vision-model smoke).
- [Dockerfile](/Users/c/software_projects/mnemosyne-inference/Dockerfile) — add
  `logsetup.py` to the explicit Python module `COPY` list so the container can
  import the new runtime module.
- [project_docs/project_status.md](/Users/c/software_projects/mnemosyne-inference/project_docs/project_status.md) — mark Phase 8 as "code/docs landed; workstation acceptance pending" on landing; add the acceptance log file to Quick Links.

**Not changed**
- Any of the ~50 logger call sites (D1 is formatter-only).
- The runtime/proxy/swap-queue code paths.
- The catalog schema (corruption guard runs at open-time only).
- `vllm-ctl` (no behavior change).
- Python dependencies (no new dependency; `logsetup.py` is stdlib only).

---

## Verification

Run in this order. Each step gates the next.

1. **Static checks.**
   - `python -m pytest -q` → expect 247 + 4 (multimodal + 2 logsetup + 1 corruption) = **251 passed**.
   - `python -m py_compile vllm_manager.py runtime.py config.py catalog.py profiles.py downloader.py download_worker.py hf_search.py logsetup.py scripts/refresh_arch_list.py`
   - `bash -n vllm-ctl`
   - `cd ui && npm test` — should still pass (no UI changes).
   - `cd ui && npm run build` — should still pass.

2. **Logging spot-check.** Set `MNEMOSYNE_LOG_FORMAT=json`, run a unit test that triggers a few log lines (e.g. `tests/test_reload.py`), capture stdout, and confirm each line is valid JSON with `ts`, `level`, `logger`, `msg`. Then set `MNEMOSYNE_LOG_FORMAT=text` and confirm fall-back to the existing text format.

3. **Acceptance log walkthrough.** Open `project_docs/phase_8_acceptance.md` and check off every test reference; for each PRD §7 row, verify the cited test file:line resolves (links pass `git grep`).

4. **Live workstation smoke checks** (CUDA host required — manual, deferred to user). Use the new vision smoke from `smoke_checks.md` Section 8 plus the existing pending checks in `project_status.md`:
   - `vllm-ctl install` a small text-gen model end-to-end.
   - Cancel + `install-retry` (default and `--force`).
   - Restart container during download → confirm `partial` recovery.
   - `vllm-ctl install` a small vision model; POST `/v1/chat/completions` with `image_url` content block; verify a coherent response.
   - Plane separation: `POST /manager/install` 404 on `:8000`, 200 on `:8001` with Basic auth.
   - Confirm `/ui/` works under Basic auth on `:8001`, returns 401 unauth, 404 on `:8000`.

5. **Acceptance file finalization.** After the workstation pass, fill in pass/fail for every row in `phase_8_acceptance.md`; commit. This is the v1 release artifact.

6. **project_status update.** On code/docs landing, set Phase 8 to
   "code/docs landed; workstation acceptance pending." Flip Phase 8 to ✅ only
   after the CUDA workstation acceptance pass is recorded in
   `phase_8_acceptance.md`; then remove resolved follow-ups.

---

## Out of scope (and why)

- **Per-call structured logging** — converting `logger.info("X happened with %s", thing)` to `logger.info("X happened", extra={"thing": thing})`. ~50 sites for marginal operator value; the JSON-wrap is enough for v1.
- **Request-id propagation in `_proxy`** — would let operators correlate a request to its swap and to vLLM stderr. Useful, but a structured-log enhancement that belongs after v1.
- **Prometheus `/metrics`** — PRD §8 explicit stretch.
- **Capturing vLLM stderr into a ring buffer** — adds complexity for a problem `docker compose logs` already solves.
- **Hard-fail on runtime no-GPU detection** — would break local dev hosts.
- **PRAGMA `integrity_check` (full)** instead of `quick_check` — full check scans every page; quick_check is enough for the "single bad page" case we expect, and runs in milliseconds on a multi-MB DB.
- **Phase 5 bundled-snapshot regeneration** — separate open follow-up that requires running inside the pinned vLLM container; not Phase 8.
