# Phase 4 — Install, Download, Cache, and Multi-Drive Storage Workflows

> Draft plan; on approval promote to `project_docs/plans/phase_4.md` (the canonical home, matching phase_0/1/2/3 plans).
>
> **Revised after review v1:** restart-recovery ordering, `error` vs `partial` model status, two-tier cache/install delete, legacy `revision` field, dedup by (storage, hf_id, revision), explicit catalog `RLock`, per-worker env dict (not `os.environ` mutation).
>
> **Revised after review v2:** revision plumbed into `ResolvedProfile` + `build_vllm_argv` so the resident model matches what was downloaded; `revision` baked into the initial `CREATE TABLE` (additive ALTER runs after bootstrap, only for legacy DBs); cache wipe targets the repo cache dir (`<storage>/hub/models--org--repo`), not the snapshot path; `downloading` removed from `models.status` enum to match state-machine reality (download activity lives only on the `downloads` row); legacy `DELETE /manager/cache/{model_id:path}` now also refuses with 409 if any matching alias has an active install.
>
> **Revised after review v3:** reconcile resolves the snapshot by reading `<repo>/refs/<revision>` instead of newest-mtime, so multi-revision repos promote the right snapshot per row; cache deletes now propagate `partial` to all sibling alias rows on the same `(storage, hf_id)`; `sync_from_config` persists `revision`; legacy `GET /manager/download/{model_id}` looks up by the exact `synthetic_alias` instead of `lookup_by_hf_id` to avoid shadowing.
>
> **Revised after review v4:** concurrent-install dedup widened to **`(storage_location, hf_model_id)`** — revision dropped from the tuple, since HF stores all revisions of a repo under one shared cache dir (`refs/`, `blobs/`, `.locks`, `.incomplete` are repo-wide and two concurrent workers on different revisions corrupt each other); `_snapshot_for_revision` checks `snapshots/<revision>/` directly first (handles commit SHAs that have no `refs/` entry) before falling back to `refs/<revision>`; the helper validates revision strings (rejects `..`, absolute paths, anything that resolves outside `<repo>/refs`) so a malicious revision value can't read arbitrary files; new `resolved_sha` column on `models` records the actual snapshot SHA at `mark_complete` time so the resident vLLM is pinned to *exactly* the downloaded weights even if a moving ref like `main` advances on the Hub; legacy `/manager/download` shim restored its v0 default `ignore_patterns` (skip non-safetensor formats); section numbering tidied (3a–3f).
>
> **Revised after review v5:** `_resolve_request_model` now gates catalog `ui_install` rows on `status='installed'` so queued/partial/error installs are not accidentally routed through vLLM; all invalidation paths clear `resolved_sha` (`start_install_tx`, cancel/orphan/error, cache-delete/partial) so stale SHA pins cannot survive reinstall or deletion; raw-id and legacy synthesized profiles get `revision="main"` when `ResolvedProfile` grows the new field; legacy synthetic aliases remain keyed by HF model ID only, so repeated legacy downloads for different revisions intentionally overwrite the same v0 status slot.

## Context

Phases 1–3 landed the substrate (config + SQLite catalog + profile resolver), the runtime lifecycle (profile-driven `_start_vllm`, swap queue, idle eviction), and the security boundary (two listeners, HTTP Basic admin, fail-safe bind). Models can be loaded today only if (a) the user hand-edits `~/vllm-manager/config.yaml` and reloads, or (b) the user calls the legacy thread-based `POST /manager/download` followed by a tier-4 raw-id passthrough on `/v1/*`. Neither is durable: the in-memory `_downloads` dict at [vllm_manager.py:121](../../vllm_manager.py#L121) is wiped on restart, the legacy `/manager/download` writes to a single `HF_HOME=/hf-cache` mount and ignores the `storage.locations` declared in [config.py:65-89](../../config.py#L65-L89), and there is no way to install on a non-default drive, cancel a long download, or recover after `docker compose down && up`.

Phase 4 makes the catalog operational. New `POST /manager/install` accepts a fully-typed install request (alias, HF model, `revision`, quantization, GPU plan, `max_model_len`, storage location, `extra_args`), persists a `models` row with `source='ui_install'` and a `downloads` row in one transaction, and spawns a **killable subprocess** (per PRD §5.9) that wraps `huggingface_hub.snapshot_download` with the chosen storage location's path as `cache_dir`. Cancel sends SIGTERM; retry resumes from `.incomplete` files; `?force=true` wipes the cache directory first. Cache deletion has two flavors: alias-aware partial-recovery (`DELETE …/install/{alias}/cache`) and full removal (`DELETE …/install/{alias}`). The thread-based legacy `/manager/download` becomes a thin shim that creates a synthetic-alias row via [`catalog.synthetic_alias`](../../catalog.py#L27) and runs through the same subprocess pipeline — the in-memory `_downloads` dict retires.

After Phase 4 the catalog is the single source of truth for "what is on disk, where, and in what state," `/manager/install` is end-to-end functional from CLI and curl, downloads survive container restart, and the UI in Phase 6 has a stable surface to render against.

---

## Architectural decisions

| Decision | Choice | Why |
|---|---|---|
| Process model for downloads | **Subprocess**, not thread | PRD §5.9 step 3 explicit. SIGTERM is the only reliable way to stop `snapshot_download` mid-flight; threads can't be killed in CPython |
| Worker entrypoint | New `download_worker.py` checked into the repo, invoked as `python -m download_worker <args.json>` | Self-contained, importable for tests, no extra packaging. Keeps deps to `huggingface_hub` + stdlib |
| IPC channel | Line-delimited JSON on subprocess stdout, parsed by a daemon thread in the parent | PRD §5.9 step 3 names this option. Stderr passes through to manager logs unchanged |
| Progress fidelity | tqdm-class hook in the worker emits `{"event":"progress","bytes":N,"total":M}` per bar update | HF `snapshot_download` accepts `tqdm_class=`. Free byte-level progress for UI without polling the cache dir |
| HF token isolation | `downloader.start_install` accepts an explicit `hf_token: str \| None` and merges it into the subprocess env dict locally — **never** mutates `os.environ`. The legacy shim wires its body's `hf_token` through this same arg | Concurrent installs with different per-request tokens cannot leak across workers; main-process env stays untouched |
| Concurrent install dedup | Reject same alias **or** same `(storage_location, hf_model_id)` pair (any revision) if any existing `downloads` row is `queued` or `downloading`. 409 with the conflicting alias in the body | v4 review fix: HF stores all revisions of a repo in one cache dir — `refs/`, `blobs/`, `.locks`, and `.incomplete` are repo-wide, so two concurrent workers on the same repo (even at different revisions) race over the same files. Different drives are fine; same drive serializes |
| Catalog state machine | `models.status ∈ {queued, partial, installed, error}` — **no `downloading` value** on the model row. The active state lives only on the `downloads` row, where it walks `queued → downloading → complete\|error\|cancelled`. Hard worker failure → models=`error`, downloads=`error`; user-cancelled → models=`partial`, downloads=`cancelled`; cache-delete on aliased row → models=`partial`; cache-delete on synthetic row → row removed; manager restart mid-download → models=`partial`, downloads=`error`, with reconcile able to promote back to `installed` if the snapshot landed cleanly before crash | Distinguishes "broken — investigate" from "resumable — one click to fix". v2 review removed the redundant `downloading` model status, which previously contradicted what `mark_downloading` actually wrote |
| Revision plumbed end-to-end | `ResolvedProfile` gains `revision: str = "main"`; `CatalogRow` gains `revision`; `ModelProfile` (YAML) gains `revision: str = "main"`; `resolve_profile` propagates from config or catalog row; `build_vllm_argv` emits `--revision <value>` when set (v2 review fix — earlier draft downloaded the right ref but loaded HEAD) | Without this, an install pinned to `revision='dev'` downloads `dev` but vLLM `--model org/repo` resolves to `main` at load time. Mismatched on-disk vs in-memory weights |
| Restart-recovery ordering | Lifespan: **`reap_orphans_on_startup` runs *before* `_catalog.apply_config(...)`**. Orphans are downgraded to `partial`+`error` first; `apply_config`'s reconcile pass then promotes any whose snapshot is actually complete on disk back to `installed` | Reviewer-flagged: original draft had the inverted order, which would have undone reconcile's `installed` verdict |
| Install vs cache delete endpoints | Two separate operations, each with one route. `DELETE /manager/install/{alias}/cache` → wipe disk, mark row `partial`. `DELETE /manager/install/{alias}` → wipe disk + remove the catalog row entirely. `DELETE /manager/cache/{model_id:path}` → legacy form, behaves like `…/cache` for aliased rows and like the row-removing form for synthetic cache-only rows | Reviewer-flagged ambiguity. PRD §5.8 names "delete-from-disk" and "remove-from-catalog" as separate UI actions; one route per action |
| Subprocess parent-death linkage | Linux: worker calls `prctl(PR_SET_PDEATHSIG, SIGTERM)` on startup; macOS dev: best-effort `os.setsid()` + parent-pid poll | Stops orphaned download processes when the manager dies. Worker is Linux-only in production (Docker); macOS handling exists only so dev tests can spawn workers |
| HF token sourcing | Read `HUGGING_FACE_HUB_TOKEN` from process env at install time on `/manager/install` (no per-request token field). Legacy `/manager/download` also accepts `hf_token` in body, threaded through `downloader.start_install`'s `hf_token` arg → per-worker env, not `os.environ` | PRD §9: secrets only in `.env` for the new endpoint. Back-compat for the legacy shim is preserved without leaking tokens between concurrent workers |
| Free-space pre-check | If body has `size_estimate_gb`, refuse install when free space at the chosen location < `size_estimate_gb * 1.1`. If absent, log warning and skip (user-confirmed) | PRD §5.9 step 1. Phase 5 search results populate the field; manual callers stay ergonomic |
| Alias collision rule | `/manager/install` for an alias that exists in `config.yaml` returns 409 (config wins, PRD §5.1); for an existing `ui_install` alias, allow re-install (treat as overwrite of the request shape, not the cache_dir) only if it's not currently resident — else 409 with "alias is currently loaded; unload first" | Avoids stomping a live model's cache_path while vLLM has it mmap'd |
| Resume / force-wipe | `retry?force=true` → recursively rm the cache dir before respawning the worker (refuses to wipe paths outside `storage.locations[].path`); default retry → respawn against the same cache dir, HF resumable downloads pick up `.incomplete` | PRD §5.9 step 5 |
| Legacy `/manager/download` shim | **Migrates fully to catalog.** Body still accepts the v0 shape (`model`, `revision`, `ignore_patterns`, `hf_token`); shim creates a `models` row keyed on `synthetic_alias(model_id)` with `source='ui_install'` plus a paired `downloads` row, then enqueues a subprocess identically to `/manager/install`. **`revision` is preserved** end-to-end (catalog → worker → snapshot_download). Status route resolves by exact synthetic alias, not generic HF id, so config/UI rows for the same HF id do not shadow the legacy status slot | (User-confirmed.) Single state model; UI shows legacy downloads alongside aliased ones; survives restart |
| Catalog thread-safety | `Catalog` keeps its module-singleton connection (already `check_same_thread=False`) and gains an internal `threading.RLock` wrapping every method that opens a `with self._conn:` block. WAL stays. Reader threads from `downloader.py` go through this lock | Reviewer-flagged: WAL alone doesn't make Python's sqlite3 module thread-safe. RLock is one line, removes the ambiguity, and re-entrant so nested calls (e.g. `apply_config` during reload) keep working |
| `revision` storage | **`revision TEXT NOT NULL DEFAULT 'main'`** is part of the initial `CREATE TABLE models (...)` so a fresh DB has it built-in. The additive `ALTER TABLE` only runs *after* `_bootstrap` for legacy DBs that predate Phase 4 — `pragma table_info('models')` checks if the column exists first. SCHEMA_VERSION stays at 1 (additive). v2 review fix: original draft ran ALTER before CREATE on a fresh DB and would have failed | Backward compatible: existing rows get the default `'main'`; new DBs never need the migration step |
| `/manager/storage` | Already exists from Phase 1 ([vllm_manager.py:712](../../vllm_manager.py#L712)). Phase 4 adds a `lookup` helper for free-space resolution | No new route work |
| CLI scope | `vllm-ctl install`, `install-cancel`, `install-retry`, `install-status`, `cache-delete` land in Phase 4. Existing `download` and `download-status` keep working through the catalog-backed shim | (User-confirmed.) Operators need a non-curl path; Phase 7 polishes help text |
| Removal of `_downloads` dict | Retired in Phase 4 | Single source of truth. Migrated routes read the catalog |

---

## File-by-file changes

### 1. `download_worker.py` — **new**

A standalone module run as `python -m download_worker` from a subprocess. Imports only `huggingface_hub`, `tqdm`, and stdlib. **Does not import** `vllm`, `torch`, FastAPI, or any manager module — keeps cold-start fast.

```python
# download_worker.py — line-delimited JSON on stdout; non-zero exit on failure.
#
# Invocation: python -m download_worker <args-json-base64>
# Args: {"model_id", "revision", "cache_dir", "ignore_patterns", "alias"}
# HF token is read from HUGGING_FACE_HUB_TOKEN env on the worker side
# (parent puts it there for that one subprocess only — see start_install in §2).
# Stdout events:
#   {"event":"start","total_bytes":N|null}
#   {"event":"progress","bytes_downloaded":N,"total_bytes":M}   # ~1/sec, throttled
#   {"event":"complete","cache_path":"...","size_bytes":N}
#   {"event":"error","message":"..."}
# Exit code: 0 on complete, 1 on hard error, 130 on SIGTERM (cancel).
```

Key design points:

- **Parent-death linkage.** On Linux, `ctypes.CDLL("libc.so.6").prctl(1 /*PR_SET_PDEATHSIG*/, signal.SIGTERM)` so the worker dies when the manager process exits. On non-Linux, fall back to `os.setsid()` + a daemon thread polling `os.getppid() == 1`.
- **Throttled progress emission.** Subclass `tqdm.tqdm`, override `update()` to call a callback that writes one progress line at most every 1.0 s (wall clock).
- **Total-bytes estimate.** Sum sizes of safetensors+pytorch_model siblings via `HfApi.model_info(model_id, revision=revision).siblings` before starting; emit on the `start` event. Used by the catalog `total_bytes` column.
- **Revision propagation.** `revision` from args is passed straight to both `model_info(...)` and `snapshot_download(revision=revision)`. Defaults to `"main"` if omitted.
- **Signal handling.** Default Python SIGTERM = clean exit code 143; we override to 130 so the parent can distinguish "cancelled via SIGTERM" from "errored". The worker's outer `try/except SystemExit` finalizes by closing stdout cleanly so the parent reader thread sees EOF.

### 2. `downloader.py` — **new**

Manager-side orchestration. Owns the dict of live download subprocesses keyed by alias; spawns, cancels, reaps. **Does not** own the SQLite catalog directly — calls `catalog.mark_*` methods so catalog state is single-writer through `catalog.py`.

```python
@dataclass
class DownloadHandle:
    alias: str
    proc: subprocess.Popen
    started_at: float
    reader_thread: threading.Thread     # parses stdout JSON, calls catalog.mark_*

# Module-level state — replaces vllm_manager._downloads.
_active: dict[str, DownloadHandle] = {}
_active_lock = threading.Lock()


def start_install(
    *,
    alias: str,
    model_id: str,
    revision: str,                         # default "main" set at the route layer
    cache_dir: str,
    ignore_patterns: list[str] | None,
    hf_token: str | None,                  # explicit; threaded into subprocess env, NOT os.environ
    catalog: Catalog,
) -> DownloadHandle:
    """Spawns the worker. Builds env = os.environ.copy(); if hf_token, env['HUGGING_FACE_HUB_TOKEN']=hf_token.
    Passes env= explicitly to subprocess.Popen. Main-process os.environ is never mutated."""

def cancel_install(alias: str) -> bool: ...                    # SIGTERM + reap; idempotent
def status(alias: str) -> Optional[dict]: ...                  # reads catalog
def force_wipe_cache(cache_dir: str, *, allowed_roots: list[str]) -> None: ...
                                                               # rm -rf; refuses paths outside allowed_roots
def reap_orphans_on_startup(catalog: Catalog) -> int: ...      # see §4d / state machine below
```

The reader thread is responsible for *all* progress writes to the catalog. Wakes on each newline from stdout; one DB write per progress event (small, single UPDATE), buffered to ≥1 s intervals to match the worker's throttle. On EOF, drains the proc with `.wait()`, classifies the exit code, and calls the corresponding `catalog.mark_*`. All catalog calls go through the new `Catalog._lock`-protected methods — no raw SQL outside `catalog.py`.

**Why a reader thread, not asyncio.** Subprocess stdout is a blocking fd; an asyncio task would need `loop.connect_read_pipe`, which is platform-specific. A daemon thread is simpler, cheaper, and tests trivially.

**Concurrent install dedup.** Before spawning, `start_install` queries the catalog for any active `downloads` row (status `queued` or `downloading`) whose linked `models` row has the same `(storage_location, hf_model_id)` as the new request — **revision is excluded from the tuple** because HF's repo cache is shared across revisions (v4 review fix). If found, raise `ConflictError(other_alias)`; the route handler turns that into 409. Different drives are fine — installing the same repo to `nvme-fast` and `archive` simultaneously is allowed.

### 3. `catalog.py` (and revision plumbing into `config.py`/`profiles.py`/`runtime.py`) — edits

#### 3a. Internal locking + additive migration (ordering matters)

```python
class Catalog:
    def __init__(self, conn):
        self._conn = conn
        self._closed = False
        self._lock = threading.RLock()                 # new — guards every with-self._conn block
        ...
        self._bootstrap()                              # MUST run first — creates tables for fresh DBs
        self._migrate_revision_column()                # AFTER bootstrap — only patches legacy DBs
```

The initial `CREATE TABLE models (...)` inside `_bootstrap()` already includes the new column:

```sql
CREATE TABLE IF NOT EXISTS models (
  alias              TEXT PRIMARY KEY,
  hf_model_id        TEXT NOT NULL,
  source             TEXT NOT NULL,
  quantization       TEXT,
  gpus               TEXT NOT NULL,
  max_model_len      INTEGER,
  storage_location   TEXT NOT NULL,
  cache_path         TEXT,
  size_bytes         INTEGER,
  status             TEXT NOT NULL,
  installed_at       INTEGER,
  last_used_at       INTEGER,
  request_count      INTEGER DEFAULT 0,
  extra_args         TEXT,
  revision           TEXT NOT NULL DEFAULT 'main',     -- v2: symbolic revision pinned by user (branch/tag/SHA)
  resolved_sha       TEXT                              -- v4: actual snapshot SHA after a completed install; NULL until mark_complete
);
```

`_migrate_revision_column()` is a one-method guard for pre-Phase-4 DBs that adds **both** new columns if missing:

```python
def _migrate_revision_column(self) -> None:
    with self._lock, self._conn:
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info('models')")}
        if "revision" not in cols:
            self._conn.execute(
                "ALTER TABLE models ADD COLUMN revision TEXT NOT NULL DEFAULT 'main'"
            )
        if "resolved_sha" not in cols:
            self._conn.execute(
                "ALTER TABLE models ADD COLUMN resolved_sha TEXT"
            )
```

On a fresh DB, both columns are created by `_bootstrap` and both `if` branches are no-ops. On an existing pre-Phase-4 DB, `_bootstrap` is a no-op (`IF NOT EXISTS`) and the migration adds whichever columns are missing. v2 review fix: the original draft ran the migration first, which on a fresh DB would have hit "no such table: models".

Every public mutating method wraps its body in `with self._lock:`. Read-only `list_models`, `get_model`, etc. take the lock too — RLock is re-entrant so nested mutating calls (e.g. `apply_config` from inside a reload) still work.

#### 3b. New transition methods

| Method | Effect on `models` | Effect on `downloads` | Notes |
|---|---|---|---|
| `start_install_tx(alias, hf_id, source, revision, quantization, gpus, max_model_len, storage_location, extra_args, total_bytes_hint=None)` | INSERT/UPDATE row, status='queued', revision=…, **resolved_sha=NULL** | INSERT new row, status='queued', started_at=now | Atomic — single TX. Clears any stale SHA pin before a fresh/retry install |
| `mark_downloading(alias, pid, total_bytes)` | (no change — model row stays at `queued`) | UPDATE … SET status='downloading', pid=?, total_bytes=?, started_at=now | The model row only flips on terminal events; the active state lives on `downloads` |
| `mark_progress(alias, bytes_downloaded)` | (no change) | UPDATE … SET bytes_downloaded=? | Buffered ≥1s |
| `mark_complete(alias, cache_path, size_bytes, resolved_sha)` | UPDATE … SET status='installed', cache_path=?, size_bytes=?, installed_at=now, **resolved_sha=?** | UPDATE … SET status='complete', finished_at=now | Atomic; first place the model row leaves `queued` on the happy path. `resolved_sha` is the snapshot dir's SHA (worker derives it from `<repo>/snapshots/<sha>` after `snapshot_download` returns) so subsequent loads pin to the exact downloaded weights even if a moving ref like `main` advances on the Hub |
| `mark_error(alias, message)` | UPDATE … SET **status='error'**, resolved_sha=NULL | UPDATE … SET status='error', error=?, finished_at=now | Distinguishes hard worker failure from resumable states; clears stale SHA pins |
| `mark_cancelled(alias)` | UPDATE … SET status='partial', resolved_sha=NULL | UPDATE … SET status='cancelled', finished_at=now | User-cancelled — resumable; clears stale SHA pins |
| `mark_orphan_interrupted(alias)` | UPDATE … SET status='partial', resolved_sha=NULL | UPDATE … SET status='error', error='interrupted by manager restart', finished_at=now | Used by `recover_orphan_downloads`; subsequent reconcile may promote `models.status` back to `installed` if the snapshot is actually complete on disk |
| `mark_partial(alias)` | UPDATE … SET status='partial', cache_path=NULL, resolved_sha=NULL | (no change) | Used after cache-only delete on aliased row; clears stale SHA pins |
| `delete_install_row(alias)` | DELETE WHERE alias=? AND source='ui_install' | (CASCADE) | For full-removal endpoint and synthetic-row cache delete |
| `find_active_for(storage_location, hf_model_id)` | SELECT m.alias FROM models m JOIN downloads d USING(alias) WHERE m.storage_location=? AND m.hf_model_id=? AND d.status IN ('queued','downloading') | — | Concurrent-install dedup. **Revision dropped from the tuple** (v4) — HF repo cache is shared across revisions, so two workers on the same `(storage, hf_id)` corrupt each other regardless of revision |
| `find_active_by_hf_id(hf_model_id)` | SELECT m.alias … WHERE m.hf_model_id=? AND d.status IN ('queued','downloading') | — | Used by legacy `DELETE /manager/cache/{model_id:path}` to refuse with 409 if any matching alias has an active download (v2 review fix) |
| `lookup_by_hf_id(hf_model_id)` | SELECT * FROM models WHERE hf_model_id=? ORDER BY (source='ui_install') DESC, alias | — | Used by cache-delete and UI/catalog views that need all rows for an HF id. Legacy download status uses exact `synthetic_alias(model_id)` instead |
| `recover_orphan_downloads()` | (calls `mark_orphan_interrupted` for each row) | — | Called from lifespan startup, **before** `apply_config`. Returns the count for logging |

Status enum on `models` is `{'queued','partial','installed','error'}` (4 values — see status legend at the bottom of the state machine; `downloading` lives only on the `downloads` row). Reconcile is updated to:

- **Skip rows with `status='error'`** so a hard-failed install isn't silently promoted by a half-completed snapshot.
- **Resolve the snapshot per-revision instead of by newest-mtime** (v3 review fix; v4 hardening). New helper `_snapshot_for_revision(cache_dir, revision)`:
  1. **Path-safety guard.** Reject revisions containing `..`, leading `/`, or anything that, after `os.path.normpath` + `os.path.join`, escapes `<cache_dir>/refs/`. A malicious revision string in the catalog (or attacker-controlled YAML) cannot be used to read outside the repo cache dir.
  2. **Direct snapshot SHA path first.** If `revision` matches `^[0-9a-f]{40}$` and `<repo>/snapshots/<revision>/` exists with weights, return it. HF's cache stores commit SHAs as snapshot directories *without* a corresponding `refs/<sha>` file, so this case must be handled before the `refs/` lookup (v4 review fix).
  3. **Branch/tag ref resolution.** Otherwise read `<repo>/refs/<revision>`, get the SHA, verify `<repo>/snapshots/<sha>/` exists with weights, return it.
  4. Fall back to `_newest_snapshot` only when the row's revision is `None`/empty (defense for older catalogs that materialized before the migration).
- **Sibling-aware** — see §4b cache delete.

#### 3c. New index

```sql
CREATE INDEX IF NOT EXISTS idx_models_hf_id ON models(hf_model_id);
```

Cheap; speeds up `lookup_by_hf_id` and the dedup `find_active_for` query.

#### 3d. `sync_from_config` persists `revision`

The existing `_sync_from_config_uncommitted` ([catalog.py:243](../../catalog.py#L243)) writes a fixed column set; it has to grow `revision`. Both branches:

```python
INSERT INTO models (..., extra_args, revision)
VALUES (..., ?, ?)                                          -- new tail param
...
UPDATE models SET ..., extra_args=?, revision=? WHERE alias=?
```

`m.revision` (default `"main"` from the new `ModelProfile` field) is the source. Without this, config-aliased rows keep their pre-Phase-4 revision (whatever the migration backfilled — `'main'`) regardless of YAML edits. v3 review fix.

#### 3e. New helper: sibling rows

Cache deletes wipe the entire repo cache dir, which is shared across aliases pointing at the same `(storage_location, hf_model_id)` even if their revisions differ. `find_repo_siblings(storage_location, hf_model_id, exclude_alias=None)` returns all matching alias rows so the route handler can `mark_partial` on every sibling — otherwise an unrelated alias would still report `status='installed'` while its weights are gone.

#### 3f. `config.py`, `profiles.py`, `runtime.py` — revision plumbing

Three small edits so the resident vLLM matches what was downloaded:

- [config.py](../../config.py) — add `revision: str = "main"` to `ModelProfile` (under `extra_args`). YAML aliases can pin a revision; default matches HF.
- [profiles.py](../../profiles.py) — add `revision: str` to `ResolvedProfile`. In `resolve_profile`, **prefer `row.resolved_sha` over `row.revision`** when set — that's the v4 lock-to-actual-SHA fix. Falls back to `row.revision`, then `"main"`. Config aliases use `config_profile.revision` directly (they have no resolved_sha until they've been installed via the catalog path).
- [runtime.py](../../runtime.py) — `build_vllm_argv` emits `--revision <profile.revision>` whenever `profile.revision != "main"`. (For installed catalog rows, `profile.revision` will be the resolved SHA, so vLLM uses the exact cached snapshot.) Add a corresponding test in `tests/test_runtime.py`.
- [vllm_manager.py](../../vllm_manager.py) — update `_synthesize_profile(...)` to pass `revision="main"` when constructing raw-id and legacy `ResolvedProfile` instances. Otherwise the dataclass change would break raw HF IDs and legacy `MODEL_ALIASES`.

Without this, `revision` rides through the install/download path (PRD §5.9 step 3) but a non-`main` install would download the right ref and then load HEAD — the resident weights would mismatch the on-disk snapshot's actual SHA. The CatalogRow dataclass at [catalog.py:78](../../catalog.py#L78) gains `revision: str` and `resolved_sha: str | None` fields too so reads return both.

### 4. `vllm_manager.py` — edits

#### 4a. Imports + global retirement

- Drop `from huggingface_hub import snapshot_download` (only the worker subprocess imports it now).
- Drop `_downloads: dict[str, dict]` global at [line 121](../../vllm_manager.py#L121).
- Add `import downloader; from downloader import ConflictError`.

#### 4b. New routes (admin_router only — inference plane never sees them)

Pydantic body model:

```python
class InstallRequest(BaseModel):
    alias: str
    model: str                                    # HF id
    revision: str = "main"                        # NEW — preserved through to snapshot_download
    quantization: Optional[str] = None
    gpus: GpuPlan = "all"
    max_model_len: Optional[int] = None
    storage: Optional[str] = None                 # one of storage.locations[].name
    extra_args: list[str] = []
    size_estimate_gb: Optional[float] = None      # for free-space pre-check
    ignore_patterns: Optional[list[str]] = None
```

Routes:

```python
@admin_router.post  ("/manager/install",                    tags=["installs"])
@admin_router.post  ("/manager/install/{alias}/cancel",     tags=["installs"])
@admin_router.post  ("/manager/install/{alias}/retry",      tags=["installs"])  # ?force=true
@admin_router.get   ("/manager/install/{alias}",            tags=["installs"])  # alias-keyed status
@admin_router.delete("/manager/install/{alias}/cache",      tags=["installs"])  # cache only — row goes 'partial'
@admin_router.delete("/manager/install/{alias}",            tags=["installs"])  # full removal — row + cache
@admin_router.delete("/manager/cache/{model_id:path}",      tags=["installs"])  # legacy by-HF-id form
```

Behavior of `POST /manager/install`, in order:

1. **Validate alias shape** (`config.py`'s `_ALIAS_RE`); reject reserved-prefix synthetic aliases.
2. **Refuse if alias is in `config.yaml`** (PRD §5.1: config wins) — 409.
3. **Refuse if alias is currently resident** (`_runtime.resident_alias == alias`) — 409.
4. **Refuse if there is an active install for this alias** — 409.
5. **Refuse if (storage, hf_model_id) already has an active install** under any alias (revision-agnostic; HF's repo cache is shared across revisions) — 409 with the conflicting alias in the body.
6. **Resolve storage location** to a path; if missing or not writable, 400.
7. **Free-space pre-check** if `size_estimate_gb` provided; else log a warning and continue.
8. **`catalog.start_install_tx(...)`** — atomic insert/update of both rows.
9. **`downloader.start_install(...)`** — spawns the worker; HF token from `HUGGING_FACE_HUB_TOKEN` env passed in explicitly via the `hf_token=` arg (per-subprocess env dict).
10. **Return 202** with `{alias, status:"queued", poll: "/manager/install/<alias>"}`.

**Cache wipe target.** All deletes wipe the **repo cache dir** (`<storage_path>/hub/models--<org>--<repo>`), *not* the snapshot path. `cache_path` recorded by `mark_complete` points at `<repo>/snapshots/<sha>` (HF convention; see [catalog.py:350](../../catalog.py#L350)); deleting only that leaves `refs/`, `blobs/`, and `.incomplete` files behind. v2 review fix: a new helper computes the repo dir from `(storage_path, hf_model_id)` even when `cache_path` is null (partial downloads):

```python
# downloader.py
def repo_cache_dir(storage_path: str, hf_model_id: str) -> str:
    # mirrors catalog._hf_dir_name (HF convention).
    return os.path.join(storage_path, "hub", "models--" + hf_model_id.replace("/", "--"))
```

Behavior of `POST /manager/install/{alias}/retry`:

- 404 if catalog row missing or `source != 'ui_install'`.
- 409 if a download is already active for this alias.
- 409 if `(storage, hf_model_id)` collides with another active install (revision-agnostic).
- If `?force=true`: `force_wipe_cache(repo_cache_dir(storage_path, hf_model_id), allowed_roots=[loc.path for loc in cfg.storage.locations])`. Defense against catalog corruption pointing at `/etc`.
- Re-spawn the worker against `cache_dir=<storage_path>/hub`; HF resumable downloads pick up `.incomplete` files automatically.

Behavior of `DELETE /manager/install/{alias}/cache` (cache-only):

- 404 if not found or `source != 'ui_install'`.
- 409 if **any** sibling alias (same `storage_location, hf_model_id`) is currently resident — the wipe would yank weights out from under a running vLLM.
- 409 if **any** sibling alias has a download active.
- `force_wipe_cache(repo_cache_dir(...))` — wipes the entire repo dir even if `cache_path` is null (partial download case).
- `catalog.mark_partial(alias)` AND `catalog.mark_partial(sibling)` for every sibling row returned by `find_repo_siblings(...)` — because the wipe nuked their cache too. Row stays for each; UI shows "not downloaded — retry?". v3 review fix: original draft only updated the called-out alias and silently lied about siblings.

Behavior of `DELETE /manager/install/{alias}` (full removal):

- 404 if not found or `source != 'ui_install'`.
- 409 if **any** sibling alias is currently resident.
- 409 if **any** sibling alias has a download active.
- `force_wipe_cache(repo_cache_dir(...))`.
- `catalog.delete_install_row(alias)` — CASCADE drops the called-out alias's downloads rows.
- For every sibling: `catalog.mark_partial(sibling)` (don't delete sibling rows; the user only asked to remove this alias).

Behavior of `DELETE /manager/cache/{model_id:path}`:

- 404 if no rows match `hf_model_id == model_id`.
- **409 if any matched alias (or any sibling) is currently resident.**
- **409 if any matched alias has an active download** — `catalog.find_active_by_hf_id(model_id)` returns non-empty.
- The wipe runs once per `(storage_location, hf_model_id)` pair — multiple rows on the same drive sharing the cache dir get a single `force_wipe_cache(repo_cache_dir(...))` call.
- For each matched row, after the wipe:
  - Aliased (config or ui_install): `mark_partial`.
  - Synthetic cache-only (`is_cache_only_alias` from [catalog.py:38](../../catalog.py#L38)): `delete_install_row`.

#### 4c. `/manager/download` rewrite (legacy shim — preserves `revision` and `hf_token`)

Replace the body of `download_model` at [line 912](../../vllm_manager.py#L912) with:

```python
@admin_router.post("/manager/download", tags=["installs"])
async def download_model(request: Request):
    body = await request.json()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(400, "'model' field required")
    alias    = catalog.synthetic_alias(model_id)
    revision = body.get("revision", "main")
    hf_token = body.get("hf_token")               # may be None
    # Preserve the legacy default ignore-patterns: the v0 endpoint skipped
    # non-safetensor formats unless the caller explicitly opted in. v4 review
    # fix: previous draft passed body.get("ignore_patterns") which would be
    # None when omitted and the worker would download every format.
    ignore = body.get(
        "ignore_patterns",
        ["*.pt", "*.bin", "*.msgpack", "flax_model*", "tf_model*", "rust_model*"],
    )
    return await _install_internal(
        InstallRequest(
            alias=alias,
            model=model_id,
            revision=revision,
            gpus="all",
            ignore_patterns=ignore,
        ),
        hf_token_override=hf_token,                # threaded into start_install kwarg, NOT os.environ
    )
```

`_install_internal(request: InstallRequest, hf_token_override: str | None = None)` is the body of `install_model` factored out; it picks up `HUGGING_FACE_HUB_TOKEN` from env when `hf_token_override` is None, otherwise prefers the override. Either way, the chosen value is passed into `downloader.start_install(..., hf_token=<value>)` and never written to the manager's `os.environ`.

The synthetic alias is invisible to the user (matches the existing `__cache__:` prefix the catalog already understands). Legacy `GET /manager/download/{model_id:path}` resolves by computing `synthetic_alias(model_id)` and reading that exact row from the catalog — **not** `lookup_by_hf_id`, which could return a config or ui_install row for the same HF id and mis-shape the response. v3 review fix: original draft used a generic-by-hf-id query that would shadow the legacy synthetic record when other rows existed. The route returns the same v0 shape (`{status, model, started_at, finished_at, path, error}`) so existing CLI code keeps working. If the synthetic alias has never existed, return 404 (matches Phase 0 behavior of "no record"). `lookup_by_hf_id` is still useful — `DELETE /manager/cache/{model_id:path}` and the UI's "all rows for this model" listing both call it — but the legacy status route does not.

#### 4d. Lifespan changes (ordering matters)

In `manager_lifespan`, **between** `_catalog = open_catalog()` (currently [vllm_manager.py:535](../../vllm_manager.py#L535)) and `_catalog.apply_config(...)` (currently [line 538-542](../../vllm_manager.py#L538-L542)):

```python
recovered = downloader.reap_orphans_on_startup(_catalog)
if recovered:
    logger.warning(
        "Recovered %d interrupted download(s) from previous run — "
        "marked partial; user can retry from UI/CLI.",
        recovered,
    )
# Now run apply_config — its reconcile pass may promote a 'partial' back to
# 'installed' if the snapshot landed cleanly before the crash.
sync, rec = _catalog.apply_config(...)
```

This is the **fix to the v1 ordering bug**: `recover_orphan_downloads` runs first, sets interrupted rows to `partial`, then reconcile (inside `apply_config`) walks the disk and promotes any whose snapshot is in fact complete back to `installed`. The reverse order would let reconcile promote, then orphan recovery would clobber back to `partial`. Reconcile is also updated (§3b) to skip rows in status `error`, so a hard-failed install doesn't get accidentally promoted by a half-finished snapshot dir.

On lifespan exit (after the eviction/flush task cancellation, before `_kill_vllm`):

```python
# SIGTERM all in-flight installs so we exit cleanly. The worker's
# prctl(PDEATHSIG) is belt-and-suspenders — Linux only.
for alias in list(downloader._active.keys()):
    downloader.cancel_install(alias)
```

#### 4e. `_resolve_request_model` gates incomplete `ui_install` rows

Phase 2 already taught `_resolve_request_model` to find `source='ui_install'` rows, but Phase 4 must tighten the status gate:

```python
row = _catalog.get_model(requested)
if row is not None and row.source == "ui_install":
    if row.status != "installed":
        raise ModelNotReady(requested, row.status)  # route returns 409/423 with a clear message
    return resolve_profile(requested, _config, _catalog)
```

Queued, partial, cancelled, and error installs must not fall through to raw-HF passthrough or launch vLLM. Once an install reaches `status='installed'`, the alias is routable on `/v1/*`; `resolve_profile` prefers `resolved_sha` over the symbolic `revision` for catalog rows so the resident vLLM uses the exact downloaded snapshot.

### 5. `vllm-ctl` — additions

Five new commands. All hit the admin port through the existing `api()` helper.

```bash
cmd_install()        # vllm-ctl install <alias> <model> [--revision REF] [--quant X] \
                     #   [--gpus all|0,1] [--max-len N] [--storage NAME] \
                     #   [--size-gb F] [-- vllm extra args...]
cmd_install_cancel() # vllm-ctl install-cancel <alias>
cmd_install_retry()  # vllm-ctl install-retry <alias> [--force]
cmd_install_status() # vllm-ctl install-status <alias>     (or omit alias for full list)
cmd_cache_delete()   # vllm-ctl cache-delete <model-id>          # legacy by-HF-id (partial for aliased)
                     # vllm-ctl cache-delete --alias <alias>     # alias-cache form (partial)
                     # vllm-ctl cache-delete --alias <alias> --remove-row   # full removal
```

`cmd_install` accepts `--revision`. Argument parsing follows the existing `cmd_load` pattern at [line 160](../../vllm-ctl#L160) (the `--` extra-args separator is preserved). Output renders progress with the same `python3 -c` formatter used by `cmd_download_status` so visual consistency is free.

`cmd_download` and `cmd_download_status` continue to work — the legacy admin endpoints behind them still accept the v0 shape (§4c).

### 6. `Dockerfile` — minimal edits

Add `download_worker.py` and `downloader.py` to the COPY at [Dockerfile:59](../../Dockerfile#L59):

```dockerfile
COPY vllm_manager.py config.py catalog.py profiles.py runtime.py \
     downloader.py download_worker.py ./
```

No new pip deps — `huggingface_hub` and `tqdm` (transitive dep of HF) are already present.

### 7. External `docker-compose.yml`

Owner-managed; flag in PR description. Phase 4 expects each `storage.locations[].path` declared in `config.yaml` to be bind-mounted from the host. The example file (`docker-compose.example.yml`) gains a comment block showing how to add multiple drives:

```yaml
volumes:
  - /mnt/nvme:/storage/nvme        # storage.locations[name=nvme-fast].path
  - /mnt/raid:/storage/raid        # storage.locations[name=archive].path
  - hf-cache:/hf-cache             # legacy single-drive default; can stay
```

### 8. Tests

| File | Status | Coverage |
|---|---|---|
| `tests/test_catalog.py` | edit | Add cases for `start_install_tx`, all 9 `mark_*`/`delete_install_row` transitions, `lookup_by_hf_id`, `find_active_for`, `find_active_by_hf_id`, `recover_orphan_downloads`. Cover both DB-creation paths: (a) fresh DB has `revision` and `resolved_sha` from initial CREATE TABLE; (b) pre-Phase-4 DB seeded without the columns gets them via the post-bootstrap ALTER. Verify atomicity by raising mid-TX and asserting both tables roll back. Verify `_lock` is re-entrant by running nested mutating calls. Assert invalidation transitions clear stale `resolved_sha` and only `mark_complete` sets it |
| `tests/test_runtime.py` | edit | New cases: `build_vllm_argv` for a `ResolvedProfile` with `revision='dev'` emits `--revision dev`; default `revision='main'` does not. With both `revision='main'` and `resolved_sha='deadbeef…'` set on the catalog row, `resolve_profile` populates `ResolvedProfile.revision` with the SHA, and argv contains `--revision deadbeef…` (v4: the lock-to-actual-SHA path). Raw-id and legacy synthesized profiles still construct successfully with `revision='main'` |
| `tests/test_profiles.py` (or extend `test_config.py` / `test_catalog.py`) | edit | `resolve_profile` returns `revision='main'` for a config alias without the field, the explicit value when set; same for `ui_install` rows |
| `tests/test_downloader.py` | **new** | Spawn a fake worker (a tiny stub script under `tests/fixtures/`) that emits scripted JSON to stdout and exits with controllable codes. Assert: progress events update the catalog; SIGTERM (130) → `mark_cancelled`; non-zero exit (1) → `mark_error` (`models.status='error'`); clean exit (0) → `mark_complete`. Cover reader-thread tolerance to garbled JSON. Cover `find_active_for` dedup: same `(storage, model)` under a different alias, even with a different revision, → 409 |
| `tests/test_install.py` | **new** | Route tests over `client` (admin-authed): `install` happy path; alias collision (config) → 409; alias collision (resident) → 409; alias collision (active install) → 409; **same `(storage, model)` under a different alias and a different revision → still 409** (v4: dedup is repo-wide); same `(model)` on a different storage → 200 (different drives don't share a cache dir); bad storage name → 400; missing storage path → 400; size_estimate_gb fail → 400; size_estimate_gb absent → 200 + warning logged; cancel; retry; retry?force=true wipes the dir; install request preserves `revision='dev'` end-to-end (assert via stubbed worker invocation args) |
| `tests/test_resolve_request_model.py` (or extend `tests/test_proxy.py`) | edit | Catalog `ui_install` rows are routable only when `status='installed'`. Queued, partial, and error rows return a clear 409/423 and do not fall through to raw-HF passthrough. Installed rows resolve with `resolved_sha` when present |
| `tests/test_install_recovery.py` | **new** | Drive `manager_lifespan` open → seed a `downloads` row with `status='downloading'` and the corresponding `models` row at `status='queued'` (the actual on-the-wire shape — `models` never goes to `downloading`) → close lifespan → reopen → assert downloads went to `error`, models went to `partial`. Second case: same setup but with a complete snapshot directory on disk under `<repo>/refs/<revision>` → after reopen, assert reconcile resolved by ref (not newest mtime) and promoted `models.status` back to `installed`. Third case: a row in `status='error'` is left alone by reconcile. Fourth case: two rows for the same `(storage, hf_id)` with different revisions both have valid `refs/<rev>` snapshots → reconcile promotes the right snapshot to each. **Fifth case (v4):** a row pinned to a 40-char hex SHA with a matching `snapshots/<sha>/` directory but no `refs/<sha>` file → reconcile promotes via the direct-SHA path. **Sixth case (v4):** a row whose revision contains `..` or a leading `/` → `_snapshot_for_revision` rejects it without touching the filesystem; row stays `partial` |
| `tests/test_cache_delete.py` | **new** | `DELETE /manager/install/{alias}/cache` aliased → `partial` and the entire `<storage>/hub/models--*` dir is gone (assert blobs/refs cleaned, not just snapshots/<sha>); `DELETE /manager/install/{alias}` aliased → row deleted; synthetic alias cache delete → row removed; resident → 409; active install → 409. `DELETE /manager/cache/{model_id:path}` matches multiple rows; `force_wipe_cache` path-safety guard refuses paths outside `storage.locations[].path`. New: `DELETE /manager/cache/{model_id:path}` with an active download for that model_id → 409 with `conflict_alias` body. Cache wipe with `cache_path=NULL` (partial download) still removes the repo dir. **Sibling cleanup** (v3): two aliases A and B for the same `(storage, hf_id)` with different revisions; `DELETE /manager/install/A/cache` wipes the repo dir AND marks both A *and* B `partial`; if B is resident, the call returns 409 instead and leaves disk + both rows untouched |
| `tests/test_download_legacy.py` (additions) | edit | Legacy `GET /manager/download/{model_id}` returns 404 when only a non-synthetic alias exists for that model_id; returns the v0-shaped record when the synthetic alias exists, even if a config alias for the same model_id is also present (v3 review fix) |
| `tests/test_download_legacy.py` | **new** | `POST /manager/download {"model":"Qwen/Qwen2.5-7B","revision":"dev","hf_token":"hf_x"}` creates a synthetic-alias row with `revision='dev'`; legacy `hf_token` body field is *not* leaked to `os.environ` (assert env unchanged before/after); the (stubbed) worker subprocess receives `HUGGING_FACE_HUB_TOKEN=hf_x` via its own env. `GET /manager/download/Qwen%2FQwen2.5-7B` returns a v0-shaped status. **Default ignore_patterns** (v4): `POST /manager/download {"model":"…"}` without an `ignore_patterns` field forwards the legacy default list (`["*.pt","*.bin","*.msgpack","flax_model*","tf_model*","rust_model*"]`) to the stubbed worker; explicitly passing `[]` overrides to "download everything" |
| `tests/test_storage_routing.py` | **new** | RICH config with two locations; install to non-default location; assert the (stubbed) worker is invoked with `cache_dir=<archive>/hub`; load through `_resolve_request_model` ⟶ `_start_vllm` synthesizes argv with `HF_HOME` matching the install location |
| `tests/conftest.py` | edit | New fixture `stub_downloader` that monkey-patches `downloader.start_install` with a sync stub flipping catalog state directly (no real subprocess) and exposes the captured `cache_dir`/`hf_token`/`revision`/env-dict args to assertions. Add to `_reset_globals` clearing `downloader._active` |

Pure-Python downloader tests run on macOS without HF Hub access; the stub worker script never imports `huggingface_hub`. Two integration tests (gated by `SKIP_HF_INTEGRATION` env var) optionally hit a real tiny model (e.g. `hf-internal-testing/tiny-random-gpt2`) for end-to-end validation when the workstation builds the image.

---

## Detailed state machine

```
                  POST /manager/install
                         │
                         ▼
   ┌──────────────────────────────────────────────────┐
   │ start_install_tx (atomic, under Catalog._lock)   │
   │  models.status='queued', source='ui_install'     │
   │  downloads.status='queued', started_at=now       │
   └──────────────────────────────────────────────────┘
                         │
                         ▼ (downloader.start_install spawns subprocess
                         │   with explicit env dict — never mutates os.environ)
   ┌──────────────────────────────────────────────────┐
   │ worker emits {"event":"start", total_bytes}      │
   │  ▶ mark_downloading(alias, pid, total_bytes)     │
   │   models.status='queued' (unchanged)             │
   │   downloads.status='downloading', pid=…          │
   └──────────────────────────────────────────────────┘
                         │
                         ▼ (~1/sec progress events)
   ┌──────────────────────────────────────────────────┐
   │ worker emits progress lines                      │
   │  ▶ mark_progress(alias, bytes_downloaded)        │
   │   models row untouched; downloads.bytes_dl++     │
   └──────────────────────────────────────────────────┘
                         │
       ┌──────────┬──────┴──────┬──────────────────┐
       ▼          ▼             ▼                  ▼
   complete    SIGTERM     hard failure       manager dies
   exit 0      exit 130    exit 1             (orphan/restart)
       │          │             │                  │
       ▼          ▼             ▼                  ▼
 mark_complete  mark_       mark_error         reap_orphans:
  models=        cancelled  models=error        downloads=error
  installed     models=    downloads=error      models=partial
                partial                         (then reconcile may
                                                 promote → installed
                                                 if snapshot complete)
```

**Status legend on `models` (4 values — `downloading` lives only on the `downloads` row):**

| Status | Meaning | Reachable from |
|---|---|---|
| `queued` | Inserted by start_install_tx; not yet on disk. Stays `queued` for the entire download — the active state is on the `downloads` row | `start_install_tx` |
| `installed` | Snapshot fully on disk and verified | `mark_complete`; reconcile promotion |
| `partial` | Some bytes on disk; resumable. Cache-deleted aliased rows also land here | `mark_cancelled`, `mark_orphan_interrupted`, `mark_partial` (cache-delete), reconcile (when no full snapshot found) |
| `error` | Hard worker failure — investigate before retry. Reconcile leaves these alone | `mark_error` |

**Reconcile policy update (in §3b):** the existing `_reconcile_cache_uncommitted` walks every row and either promotes (full snapshot exists) or downgrades (no weights). Phase 4 changes it to **skip rows with `status='error'`** so a hard-failed install isn't silently promoted by a half-completed snapshot dir.

Boundary cases:

- **Worker crashes after `start` but before `complete`:** stdout EOF without a `complete` event, exit code != 0 → `mark_error`. `models.status='error'` (no auto-recovery; user-driven retry only).
- **Worker emits `complete` but exits non-zero:** trust the event (HF wrote the snapshot); reader thread calls `mark_complete` first, then logs the unclean exit at WARN.
- **Two reader threads racing on the same alias:** impossible — `_active_lock` ensures `start_install` rejects (409) if the alias is already in `_active`.
- **Catalog write fails mid-progress:** logged at WARN; in-memory state unchanged; next progress event retries the UPDATE. RLock + WAL + `check_same_thread=False` make this safe across the reader thread and the asyncio event loop.

---

## Verification

### Unit / integration (offline, dev macOS)

```bash
cd /Users/c/software_projects/mnemosyne-inference
python -m pytest -q                      # all old + new tests pass
python -m py_compile vllm_manager.py config.py catalog.py profiles.py \
                     runtime.py downloader.py download_worker.py
bash -n vllm-ctl
SKIP_HF_INTEGRATION=1 python -m pytest -q tests/test_install.py   # default
```

Expectations:

- All existing 130+ tests still pass (no public API regressions on `/manager/load`, `/manager/status`, `/v1/*`).
- New tests (~30 cases across `test_catalog.py`, `test_downloader.py`, `test_install.py`, `test_install_recovery.py`, `test_cache_delete.py`, `test_download_legacy.py`, `test_storage_routing.py`) green. The `test_install_recovery.py` ordering test specifically validates the §4d fix.

### Container smoke (workstation, after build)

```bash
./vllm-ctl build
./vllm-ctl start

# 1. Fresh install of a small model to default storage; revision pinned.
./vllm-ctl install qwen-coder-1_5b Qwen/Qwen2.5-Coder-1.5B-Instruct \
   --revision main --gpus all --size-gb 4
./vllm-ctl install-status qwen-coder-1_5b      # progresses queued → downloading → installed
./vllm-ctl chat "ping"                          # auto-loads via /v1/* tier-2 ui_install lookup

# 2. Install to a non-default storage location.
./vllm-ctl install qwen-vision Qwen/Qwen2.5-VL-7B-Instruct \
   --storage archive --gpus 1 --max-len 16384

# 3. Cancel a long install and resume.
./vllm-ctl install qwen-72b-awq Qwen/Qwen2.5-72B-Instruct-AWQ --quant awq
sleep 30 && ./vllm-ctl install-cancel qwen-72b-awq   # downloads.status=cancelled, models.status=partial
./vllm-ctl install-retry qwen-72b-awq                # resumes from .incomplete
./vllm-ctl install-retry qwen-72b-awq --force        # wipes cache, restarts

# 4. Restart recovery (the §4d ordering test in production form).
./vllm-ctl install qwen-72b-awq Qwen/Qwen2.5-72B-Instruct-AWQ --quant awq
sleep 5
docker compose -f ~/vllm-manager/docker-compose.yml restart vllm-manager
./vllm-ctl install-status qwen-72b-awq          # status=partial, error="interrupted by manager restart"
./vllm-ctl install-retry qwen-72b-awq           # picks up where it left off

# 5a. Cache-only delete (alias stays as partial).
./vllm-ctl cache-delete --alias qwen-72b-awq
./vllm-ctl install-status qwen-72b-awq          # status=partial, alias intact
./vllm-ctl install-retry qwen-72b-awq           # one-click recovery

# 5b. Full removal (row + cache).
./vllm-ctl cache-delete --alias qwen-72b-awq --remove-row
./vllm-ctl install-status qwen-72b-awq          # 404

# 6. Plane separation regression check (Phase 3 contract).
curl -sf http://localhost:8000/manager/install   # 404 — admin route absent on inference port
curl -sf -u admin:$ADMIN_PASSWORD http://localhost:8001/manager/install   # 405 (no GET) — confirms route exists

# 7. Legacy download path still works and preserves revision + token isolation.
./vllm-ctl download Qwen/Qwen2.5-3B-Instruct --revision main --token hf_xxx
./vllm-ctl download-status Qwen/Qwen2.5-3B-Instruct
sqlite3 /state/mnemosyne.db "SELECT alias, hf_model_id, revision, source FROM models WHERE source='ui_install'"
# Should show synthetic alias __cache__:<hash> with revision='main'.
docker exec vllm-manager env | grep HUGGING_FACE_HUB_TOKEN || echo "expected: not in manager env"

# 8. Concurrent-install dedup.
./vllm-ctl install foo Qwen/Qwen2.5-7B-Instruct --storage nvme-fast &
./vllm-ctl install bar Qwen/Qwen2.5-7B-Instruct --storage nvme-fast
# Second call returns 409 with body {"conflict_alias":"foo"}.
```

### PRD §7 acceptance criteria covered by Phase 4

- [x] "A model installs to a non-default drive when the user picks it from the storage dropdown" — smoke 2; `tests/test_storage_routing.py`.
- [x] "A download can be cancelled mid-flight … cache state recovers cleanly on retry." — smoke 3; `tests/test_install.py::test_cancel_then_retry_resumes`.
- [x] "After `docker compose down && up`, in-flight downloads are recoverable…" — smoke 4; `tests/test_install_recovery.py`.
- [x] "A request to `POST /manager/download` (or any other admin operation) on the inference port returns 404." — already passing in Phase 3; smoke 6 confirms.
- (Phase 5 covers the 60-second discover-to-install criterion.)

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Worker subprocess orphaned when manager dies | Linux `prctl(PR_SET_PDEATHSIG, SIGTERM)` at worker startup; manager's lifespan `finally` SIGTERMs all entries in `downloader._active` |
| Reader thread leaks on subprocess hang | `proc.wait(timeout=30)` in cancel path; SIGKILL escalation if SIGTERM doesn't take after 10 s |
| SQLite contention between reader thread and async loop | `Catalog._lock` (RLock) wraps every public mutating method; WAL stays on; `check_same_thread=False` already set in [catalog.py:477](../../catalog.py#L477) |
| HF resumable download corruption on retry | `?force=true` query param wipes the **repo cache dir** (`<storage>/hub/models--org--repo`, not just the snapshot path) before respawning; safety guard refuses to wipe paths outside declared `storage.locations[].path`. Wipe runs even when `cache_path` is null (partial-download case) |
| Legacy `DELETE /manager/cache/{model_id:path}` deleting a repo dir while a worker writes to it | New `find_active_by_hf_id` check — 409 with the conflicting alias if any matched row has `downloads.status` in `('queued','downloading')` |
| Resident vLLM serving wrong revision after a non-`main` install | `revision` plumbed through `ResolvedProfile` → `build_vllm_argv` → `--revision`; `tests/test_runtime.py::test_revision_in_argv` pins it |
| Reconcile promoting the wrong on-disk snapshot when a repo has multiple revisions | `_snapshot_for_revision` resolves via `<repo>/refs/<revision>` for branch/tag refs and via `<repo>/snapshots/<sha>/` directly for commit-SHA revisions (HF's cache stores those without a refs entry). `_newest_snapshot` is only the legacy fallback for rows without a `revision`. Path-safety guard rejects `..`/absolute revisions |
| Concurrent installs on different revisions of the same repo corrupting each other's `.incomplete`/`refs`/`blobs` | Dedup uses `(storage_location, hf_model_id)` (revision-agnostic); same drive serializes, different drives are fine. `tests/test_install.py` pins the cross-revision conflict |
| Branch/tag revision (e.g. `main`) drifting between install and load | `mark_complete` records the resolved snapshot SHA in a new `resolved_sha` column on `models`; `resolve_profile` prefers `resolved_sha` over the symbolic revision when emitting `--revision`. Locks the resident vLLM to *exactly* the snapshot we downloaded; `--revision <40hex>` makes vLLM use the cached snapshot instead of re-fetching `main` (which may have moved on the Hub since install). To follow a moving ref, the user re-runs install — explicit, not magic |
| Stale `resolved_sha` pin surviving reinstall, failure, cancel, or cache delete | Every invalidation transition clears `resolved_sha`: `start_install_tx`, `mark_error`, `mark_cancelled`, `mark_orphan_interrupted`, and `mark_partial`. Only `mark_complete` sets it. Tests assert stale SHA pins are cleared before retry/reinstall and after cache deletion |
| `/v1/*` resolving a queued/partial/error `ui_install` alias | `_resolve_request_model` gates catalog `ui_install` rows on `status='installed'`; incomplete installs return a clear not-ready response and never fall through to raw-HF passthrough |
| Sibling alias rows reporting `installed` after a cache wipe of one alias | Cache delete handlers query `find_repo_siblings` and `mark_partial` every sibling on the same `(storage, hf_id)`; `tests/test_cache_delete.py` sibling-cleanup case pins it |
| Config-aliased rows losing revision changes from YAML edits | `sync_from_config` INSERT/UPDATE include the `revision` column; `tests/test_catalog.py` sync-revision case pins it |
| Legacy `GET /manager/download/{model_id}` returning the wrong row when multiple rows share the HF id | Lookup is by exact synthetic alias (`synthetic_alias(model_id)`), not `lookup_by_hf_id`; tests pinned in `tests/test_download_legacy.py` |
| Migration fails on a brand-new DB | Initial `CREATE TABLE` includes `revision`; the additive `ALTER` runs after `_bootstrap` and only fires on legacy DBs (column-existence check) |
| User installs an alias that's currently resident | 409 with "alias is currently loaded; unload first." Avoids vLLM's mmap'd pages getting yanked |
| Legacy `/manager/download` body's `hf_token` polluting process env | Token is threaded into `downloader.start_install(..., hf_token=...)`, which writes it into a per-subprocess env dict and passes that to `subprocess.Popen(env=...)`. Manager's `os.environ` is **never** mutated; concurrent installs cannot leak tokens across each other |
| Hard-failed install accidentally promoted by a half-finished snapshot | Reconcile skips rows with `status='error'`; only the user-driven retry path reopens the cache dir |
| Restart-recovery vs reconcile ordering | Lifespan calls `reap_orphans_on_startup` **before** `apply_config` so reconcile's promotion runs last; `tests/test_install_recovery.py` pins this |
| Free-space check skipped on missing `size_estimate_gb` | Logged at WARN with the model_id; smoke check 1 verifies the warning fires; Phase 5 search auto-populates the field so UI installs always have it |
| Two distinct aliases for the same `(storage, model)` repo cache racing on one cache_dir | `find_active_for(...)` dedup at install time + retry time → 409 with `conflict_alias` in the body, regardless of revision. Documented in `vllm-ctl install` help text |
| Legacy `/manager/download` revision semantics | Legacy synthetic aliases are intentionally keyed by HF model ID only (`synthetic_alias(model_id)`), matching the v0 status route shape. Repeating legacy downloads for the same model at different revisions overwrites the same synthetic status/catalog row. Use `/manager/install` with named aliases for multiple revisions side by side |
| Catalog row for an aliased install left in `partial` after cache delete with no cache_path | `mark_partial` explicitly sets `cache_path=NULL`. Reconcile keeps it `partial` because `_newest_snapshot` returns None |
| Worker module import-time cost | Imports only `huggingface_hub` + stdlib. Cold-start ~200 ms — small enough that subprocess spawn dominates |
| Tests can't run a real subprocess on every CI run | `tests/fixtures/fake_download_worker.py` is a stub script the test suite invokes via `subprocess.Popen([sys.executable, "fixtures/fake_download_worker.py", ...])`. Real `download_worker.py` runs only behind `SKIP_HF_INTEGRATION=0` |
| Additive `revision` migration on existing DBs | Bootstrap inspects `pragma table_info('models')` and runs `ALTER TABLE … ADD COLUMN revision TEXT NOT NULL DEFAULT 'main'` if missing. Safe across reruns |
| `vllm-ctl install` argument parsing creep | Reuse the `cmd_load` pattern; one new helper `parse_install_opts()` covers shared flags |

---

## Critical files for implementation

- [vllm_manager.py](../../vllm_manager.py) — new install routes (six), retire `_downloads`, lifespan recovery hook **before** `apply_config`, lifespan teardown SIGTERM, body of `download_model` rewritten as a shim that preserves `revision` and `hf_token` without env mutation, `_resolve_request_model` gates `ui_install` aliases on `status='installed'`, `_synthesize_profile` passes `revision='main'`
- [catalog.py](../../catalog.py) — add `threading.RLock`; new columns `revision TEXT NOT NULL DEFAULT 'main'` and `resolved_sha TEXT` in initial CREATE TABLE plus post-bootstrap additive ALTERs for legacy DBs; `revision` and `resolved_sha` fields on `CatalogRow`; 9 transition methods (`start_install_tx` *(clears resolved_sha)*, `mark_downloading`, `mark_progress`, `mark_complete` *(records resolved_sha)*, `mark_error` *(clears resolved_sha)*, `mark_cancelled` *(clears resolved_sha)*, `mark_orphan_interrupted` *(clears resolved_sha)*, `mark_partial` *(clears resolved_sha)*, `delete_install_row`) + `find_active_for` (revision-agnostic) + `find_active_by_hf_id` + `find_repo_siblings` + `lookup_by_hf_id` + `recover_orphan_downloads`; **`sync_from_config` INSERT/UPDATE persist `revision`**; reconcile uses `_snapshot_for_revision` which checks `<repo>/snapshots/<sha>/` directly first (handles 40-hex commit SHAs that lack a refs entry), then falls back to `<repo>/refs/<revision>`, and refuses `..` / absolute revisions for path safety; reconcile skips `status='error'`; new index on `models(hf_model_id)`
- [config.py](../../config.py) — add `revision: str = "main"` to `ModelProfile`
- [profiles.py](../../profiles.py) — add `revision` to `ResolvedProfile`; populate from config aliases, or for catalog rows prefer `resolved_sha` then `revision` in `resolve_profile`
- [runtime.py](../../runtime.py) — `build_vllm_argv` emits `--revision` when not `"main"`; matching test in `tests/test_runtime.py`
- `downloader.py` — **new** module, ~280 LOC; explicit `hf_token=` arg + per-subprocess env dict; `find_active_for` dedup; `repo_cache_dir(storage_path, hf_model_id)` helper; allowlist-checked `force_wipe_cache`
- `download_worker.py` — **new** module, ~160 LOC; revision propagated to `model_info` and `snapshot_download`
- [vllm-ctl](../../vllm-ctl) — 5 new `cmd_*` functions + dispatch entries; `cmd_help` updated; `install` accepts `--revision`
- [tests/conftest.py](../../tests/conftest.py) — `stub_downloader` fixture (captures `hf_token`, `revision`, `cache_dir`, env dict); `_reset_globals` for `downloader._active`
- [tests/test_catalog.py](../../tests/test_catalog.py) — extend for transition methods, `find_active_for`, RLock re-entrance, additive revision migration
- `tests/test_downloader.py`, `tests/test_install.py`, `tests/test_install_recovery.py`, `tests/test_cache_delete.py`, `tests/test_download_legacy.py`, `tests/test_storage_routing.py` — **new**
- `tests/fixtures/fake_download_worker.py` — **new** stub worker for offline tests
- [Dockerfile](../../Dockerfile) — add two new modules to COPY
- [docker-compose.example.yml](../../docker-compose.example.yml) — multi-drive comment block (flag in PR description; live file lives outside repo)
- [project_docs/project_status.md](../project_status.md) — Phase 4 done row + open follow-ups update (after merge)

---

## Estimated effort

| Step | Rough size |
|---|---|
| `download_worker.py` | ~160 LOC + 2 days incl. signal handling, tqdm hook, revision plumbing |
| `downloader.py` | ~280 LOC + 2 days incl. reader thread, reaper, dedup, env-dict |
| `catalog.py` transition methods + RLock + migration | ~220 LOC + 1.5 days |
| Routes + Pydantic InstallRequest in `vllm_manager.py` | ~280 LOC + 1.5 days |
| `vllm-ctl` commands | ~150 LOC + 0.5 day |
| Tests (8 files, ~700 LOC total) | ~3 days |
| Docs + workstation smoke | 1 day |

Total: roughly 1.5–2 weeks of focused work; reviewable in two PRs (catalog + downloader engine first, then routes + CLI + UI-facing endpoints).
