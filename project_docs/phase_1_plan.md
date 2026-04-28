# Phase 1 Plan — Config, Profiles, Storage, and Catalog Core

**Source:** [implementation_plan.md](implementation_plan.md) §Phase 1, [PRD.md](PRD.md) §5.1, §5.2, §5.11, §5.12, §5.13
**Predecessors:** Phase 0 (foundation, smoke tests, vLLM pin, examples) — complete.
**Successors:** Phase 2 wires the runtime profile object into `_start_vllm` and adds lazy/queue/eviction. Phase 3 splits the listeners.

## 1. Goal

Replace ad-hoc env-driven configuration with a durable, declarative registry: YAML config drives a typed profile model, `.env` provides secrets, and SQLite holds the persistent catalog. **No runtime serving behavior changes in this phase** — `/v1/*` proxy, `/manager/load`, and download flows keep working exactly as today. The deliverable is the substrate Phase 2 will plug into.

## 2. Scope

In:

- A `Config` model loaded from `/config/config.yaml` covering `server`, `storage`, `defaults`, `models`.
- `.env` loading from `/config/.env` into the process environment.
- A SQLite catalog at `/state/mnemosyne.db` with `models` and `downloads` tables.
- Sync from config aliases into the catalog with `source='config'` precedence rules.
- Config reload via `POST /manager/reload` and `SIGHUP`.
- Read-only endpoints exposing the new state for early CLI and debugging.
- Tests for config validation, catalog CRUD, sync, reload, and new endpoints.

Out (deferred):

- Wiring a runtime profile object into `_start_vllm` — Phase 2. (The pure `resolve_profile(alias)` helper described in §4.5 IS in scope; Phase 2 only consumes its return value.)
- Lazy loading, queueing, idle eviction — Phase 2.
- Plane separation, auth, fail-safe bind — Phase 3.
- Install / cancel / retry endpoints, the download subprocess model — Phase 4.
- HF search and architecture compatibility — Phase 5.
- Admin UI — Phase 6.
- Modifying the response shape of `/manager/status` and the existing `/manager/aliases` endpoints (pinned by [Phase 0 smoke tests](../tests/test_smoke.py); we add new endpoints instead).

## 3. Files added or changed

| Path | Change | Notes |
|---|---|---|
| `config.py` | **new** | Pydantic schema, YAML loader, `.env` loader, validators, GPU/storage probes. No imports from `catalog.py`. |
| `catalog.py` | **new** | SQLite layer: schema bootstrap, CRUD, sync, reconciliation. Imports `ModelProfile` from `config.py`. |
| `profiles.py` | **new** | `ResolvedProfile` dataclass + pure `resolve_profile(alias, config, catalog)`. Imports both `config.py` and `catalog.py`; nothing imports it back. Keeps the `config ↔ catalog` dep one-way. |
| `vllm_manager.py` | edit | Wire config + catalog into `lifespan`. Add reload endpoint, `SIGHUP` handler, three new read-only endpoints. No changes to existing routes' shapes. |
| `requirements-dev.txt` | edit | Add `pydantic>=2`. (`pyyaml` is already there.) |
| `Dockerfile` | edit | Add `pydantic` and `pyyaml` to the runtime pip install. |
| `tests/conftest.py` | edit | Reset new module globals (loaded `Config`, catalog connection) between tests. Provide a tmp-path fixture for config + DB. |
| `tests/test_config.py` | **new** | Unit tests for parsing, validation, error paths. |
| `tests/test_catalog.py` | **new** | Unit tests for schema bootstrap, sync, CRUD, reconciliation. |
| `tests/test_reload.py` | **new** | Route tests for `/manager/reload`, `/manager/profiles`, `/manager/storage`, `/manager/catalog`. |
| `project_docs/smoke_checks.md` | edit (if present) or skip | Note Phase 0 contracts unchanged; new endpoints are additive. |

The `docker-compose.yml` lives outside the repo. Phase 1 needs **three additional bind mounts** the user must add (call this out in the PR description):

- `~/vllm-manager/config.yaml:/config/config.yaml:ro`
- `~/vllm-manager/.env:/config/.env:ro`
- `~/vllm-manager/state:/state` (writable; for the SQLite file).

Note: Phase 4 also lists `GET /manager/storage` as work; treat it there as **extend**, not implement — Phase 1 ships the endpoint and Phase 4 adds install/free-space-check semantics on top. (Worth a one-line correction in [implementation_plan.md](implementation_plan.md) §Phase 4.)

## 4. `config.py` design

### 4.1 Pydantic schema

Use Pydantic v2. One module-level `Config` model with nested submodels.

```python
class Server(BaseModel):
    inference_port: int = 8000
    admin_port: int = 8001                  # parsed, NOT bound in Phase 1 (see note below)
    inference_bind: str = "0.0.0.0"
    admin_bind: str = "0.0.0.0"
    idle_unload_seconds: int | None = 900   # null disables idle eviction
    startup_timeout_seconds: int = 600
    swap_queue_timeout_seconds: int = 300

class StorageLocation(BaseModel):
    name: str
    path: str

class Storage(BaseModel):
    default: str
    locations: list[StorageLocation]

    @field_validator("locations")
    def names_unique(...): ...

    @model_validator(mode="after")
    def default_must_exist(self): ...

class Defaults(BaseModel):
    gpu_memory_utilization: float = 0.90
    trust_remote_code: bool = True
    max_model_len: int | None = None

GpuPlan = Literal["all"] | list[int]    # validated separately

class ModelProfile(BaseModel):
    alias: str
    model: str                          # HF model ID
    quantization: str | None = None     # passed through to --quantization
    gpus: GpuPlan = "all"
    max_model_len: int | None = None
    storage: str | None = None          # references Storage.locations[].name; None → Storage.default
    extra_args: list[str] = Field(default_factory=list)

    @field_validator("alias")
    def alias_shape(cls, v):
        # [a-z0-9][a-z0-9-]* — lowercase, hyphenated. Used as PRIMARY KEY in SQLite.
        ...

class Config(BaseModel):
    server: Server = Field(default_factory=Server)
    storage: Storage
    defaults: Defaults = Field(default_factory=Defaults)
    models: list[ModelProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def cross_refs(self):
        # 1. alias uniqueness
        # 2. each model.storage references a known location (or is None)
        ...
```

Notes:

- `gpus: all` is the default. Empty list `[]` is rejected.
- `quantization` accepts any string (PRD §5.4 — forward-compatible with vLLM).
- `dtype` and `modality` are **not** schema fields (PRD §9 resolved decisions).
- `extra_args` is opaque list-of-strings; not parsed.
- **`server.inference_port`, `server.inference_bind`, `server.admin_port`, `server.admin_bind` are all parsed but not bound in Phase 1.** Process startup at [vllm_manager.py:596](../vllm_manager.py#L596) keeps using `MANAGER_HOST` / `MANAGER_PORT` env vars exactly as today; the single FastAPI app stays on whatever uvicorn was already binding. Phase 1 deliberately makes **zero** runtime/compose behavior changes — the four `server.*` fields are parsed, stored in `_config`, logged at startup with a one-line "Phase 1 parses these but does not bind; Phase 3 will" note, and otherwise inert. Phase 3 wires `server.inference_port` into uvicorn startup, splits the admin listener onto `server.admin_port`, and at the same time relocates the inner vLLM port off `8001` to resolve the collision noted at [vllm_manager.py:34](../vllm_manager.py#L34). Keeping Phase 1 inert here means existing compose files keep working without edits beyond the three new mounts in §3.

### 4.2 Loader API

```python
DEFAULT_CONFIG_PATH = "/config/config.yaml"
DEFAULT_ENV_PATH = "/config/.env"

def load_config(path: str | None = None) -> Config:
    """Read YAML, parse via Pydantic. Raises ConfigError on any problem."""

def load_env(path: str | None = None, override: bool = False) -> dict[str, str]:
    """
    Read /config/.env if present. Populate os.environ for keys not already set
    (override=False). Return the parsed dict for logging/debugging.
    Missing file is OK (logs at DEBUG, not WARN).
    """

class ConfigError(Exception): ...
```

Both paths are overridable via env var so tests can point at tmp paths:

- `MNEMOSYNE_CONFIG_PATH` (default `/config/config.yaml`)
- `MNEMOSYNE_ENV_PATH` (default `/config/.env`)

### 4.3 GPU and storage validation

At load time, after Pydantic parse, run runtime checks:

- **GPU probe.** Run `nvidia-smi -L` once; collect available GPU indices. For every `ModelProfile.gpus` that is a list, every index must exist. `all` is always accepted. If `nvidia-smi` is missing (dev host), log a WARNING and **skip** the index check (don't fail boot).
- **Storage paths.** For each `StorageLocation`, check `os.path.isdir(path)` and `os.access(path, os.W_OK)`. Missing or read-only paths are **WARN, not fail** — the user may have a drive temporarily unmounted (PRD §5.12).

Both checks accumulate findings into a structured `ConfigDiagnostics` value that's logged once at startup and surfaced via the new `/manager/storage` endpoint.

### 4.4 Hard-fail policy

Any of: malformed YAML, schema violation, duplicate alias, unknown storage reference, missing required field. → raise `ConfigError`, log the full message, exit non-zero. Must abort container start (PRD §9, "Malformed config: hard fail on container start").

### 4.5 Profile resolver (Phase 1 — unwired, lives in `profiles.py`)

A pure helper that satisfies the implementation-plan exit criterion ([implementation_plan.md:75](implementation_plan.md#L75)) without touching `_start_vllm` (which stays Phase 2 work). Lives in `profiles.py` rather than `config.py` to keep `config → catalog` from going circular: `catalog.py` already imports `ModelProfile` from `config.py`, so the resolver — which needs both — moves up a layer.

```python
@dataclass(frozen=True)
class ResolvedProfile:
    alias: str
    model: str                          # HF model ID
    gpus: GpuPlan
    quantization: str | None
    max_model_len: int | None           # profile override → defaults.max_model_len
    gpu_memory_utilization: float       # from defaults
    trust_remote_code: bool             # from defaults
    storage_name: str                   # location name
    storage_path: str                   # absolute container path used as HF_HOME
    extra_args: tuple[str, ...]

def resolve_profile(
    alias: str,
    config: Config,
    catalog: Catalog | None = None,
) -> ResolvedProfile:
    """
    Merge profile + defaults + storage into a single immutable view.

    Lookup order (PRD §5.1: config wins on conflict):
      1. config.models[alias]
      2. catalog row with source='ui_install'
      3. raise KeyError(alias)

    Resolution rules:
      - max_model_len: profile value if set, else defaults.max_model_len.
      - storage_name:  profile.storage if set, else config.storage.default.
      - storage_path:  config.storage.locations[storage_name].path.
      - gpu_memory_utilization, trust_remote_code: from defaults.
      - extra_args:    profile.extra_args (frozen as tuple).
    """
```

Pure function — no I/O, no subprocess. Phase 2 will call it from inside `_maybe_swap` / `_start_vllm` and translate the result into vLLM CLI args + subprocess env. Phase 1 just exposes it for unit testing and later reuse.

Tests (in `test_config.py`):

- Resolves a config-only alias with all defaults applied.
- Resolves a profile that overrides `max_model_len` (override beats default).
- Resolves a profile with `storage` omitted (falls back to `storage.default`).
- Resolves a `ui_install` row when no config entry exists.
- Config alias shadows a `ui_install` row of the same name (config wins).
- Unknown alias → `KeyError`.

## 5. `catalog.py` design

### 5.1 Connection lifecycle

One `sqlite3.Connection` per process, opened in `lifespan`, closed at shutdown. WAL mode enabled (`PRAGMA journal_mode=WAL`) for safety when later phases add concurrent reads.

```python
DEFAULT_DB_PATH = "/state/mnemosyne.db"

def open_catalog(path: str | None = None) -> Catalog: ...
class Catalog:
    def __init__(self, conn: sqlite3.Connection): ...
    def close(self) -> None: ...
```

`MNEMOSYNE_DB_PATH` overrides the default (tests use `:memory:` or `tmp_path`).

### 5.2 Schema (verbatim from PRD §5.11)

```sql
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS models (
  alias              TEXT PRIMARY KEY,
  hf_model_id        TEXT NOT NULL,
  source             TEXT NOT NULL,    -- 'config' | 'ui_install'
  quantization       TEXT,
  gpus               TEXT NOT NULL,    -- JSON: "all" or [0,1]
  max_model_len      INTEGER,
  storage_location   TEXT NOT NULL,
  cache_path         TEXT,
  size_bytes         INTEGER,
  status             TEXT NOT NULL,    -- 'queued' | 'downloading' | 'installed' | 'partial' | 'error'
  installed_at       INTEGER,
  last_used_at       INTEGER,
  request_count      INTEGER DEFAULT 0,
  extra_args         TEXT              -- JSON array
);

CREATE TABLE IF NOT EXISTS downloads (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  alias              TEXT NOT NULL REFERENCES models(alias) ON DELETE CASCADE,
  pid                INTEGER,
  status             TEXT NOT NULL,
  started_at         INTEGER NOT NULL,
  finished_at        INTEGER,
  bytes_downloaded   INTEGER DEFAULT 0,
  total_bytes        INTEGER,
  error              TEXT
);

CREATE INDEX IF NOT EXISTS idx_downloads_alias  ON downloads(alias);
CREATE INDEX IF NOT EXISTS idx_models_last_used ON models(last_used_at);
```

A bare `schema_version` row tracks future migrations. v1 inserts `(version=1)` on bootstrap. Migrations are hand-written `if current < N: ...` blocks — no Alembic.

**Cache-only download convention (Phase 1 schema decision).** PRD §5.1 / §5.11 has `models.alias TEXT PRIMARY KEY` and `downloads.alias TEXT NOT NULL`, but PRD §5.9 line 229 also says the legacy `POST /manager/download` shim "calls /manager/install with no alias (cache-only download)". To reconcile: cache-only entries get a **synthetic alias** that is URL-safe and deterministic:

```
__cache__:<first-16-hex-of-sha256(hf_model_id)>
```

e.g. `__cache__:9b1a4f2e7c3d5a08`. Properties:

- URL-safe ASCII — no slashes, no case-folding ambiguity, fits cleanly in route paths and JSON keys without escaping.
- Deterministic — same `hf_model_id` always maps to the same synthetic alias, so the legacy shim is idempotent without an extra "does a cache row already exist" lookup.
- The `__cache__:` prefix is reserved — `ModelProfile.alias` validation in §4.1 rejects any user-supplied alias that starts with it (or with `__cache__/` for defense-in-depth against earlier drafts of this design leaking into configs).
- The original `hf_model_id` is preserved in the `models.hf_model_id` column, so the UI can display it without reversing the hash.
- `/manager/profiles` and `/manager/catalog` filter these rows out by default; an explicit `?include_cache_only=true` query param exposes them. (Phase 1 wires the filter; Phase 6 UI uses it for the "on-disk but unaliased" rows of the unified catalog.)
- Phase 4 implements creation of these rows from the legacy shim. Phase 1 only commits to the schema accommodation and the encoding contract.

This keeps `alias NOT NULL` everywhere (faithful to PRD §5.11) while letting Phase 4 ship cache-only downloads without a schema migration or URL-encoding gymnastics.

### 5.3 CRUD surface (Phase 1 only)

Phase 1 only needs the read paths and the config-sync write path. Install/download writes come in Phase 4.

```python
class Catalog:
    # writes
    def sync_from_config(self, models: list[ModelProfile], default_storage: str) -> SyncResult: ...
    def reconcile_cache(self, storage_paths: dict[str, str]) -> ReconcileResult: ...

    # reads
    def list_models(self) -> list[CatalogRow]: ...
    def get_model(self, alias: str) -> CatalogRow | None: ...
    def list_downloads(self) -> list[DownloadRow]: ...   # returns [] in Phase 1
```

`SyncResult` carries counts for logging: `{added, updated, removed_config_orphans, ui_preserved}`.

### 5.4 Sync semantics (PRD §5.11)

On every config load — startup AND `POST /manager/reload`:

1. **UPSERT** each `ModelProfile` into `models` with `source='config'`. Sync writes only the **declarative** columns (those derived from YAML); **durable metadata is preserved** on existing rows.
2. `DELETE FROM models WHERE source='config' AND alias NOT IN (<current config aliases>)` — config rows that disappeared from YAML are dropped. Cascade drops their `downloads` rows (FK with `ON DELETE CASCADE`).
3. `source='ui_install'` rows are **never** touched by sync. Conflicts (same alias in both YAML and DB-as-ui_install) — config wins, UI row is overwritten with a logged WARNING. (PRD §5.1: "Config-defined aliases take precedence on conflict.")

All inside one transaction.

**Column write rules for the UPSERT** — explicit so reload-on-running-system doesn't lose history (PRD §5.11 mandates persistent usage data):

| Column | On insert (new alias) | On update (existing alias) |
|---|---|---|
| `alias` | from YAML | (PK — unchanged) |
| `hf_model_id` | from YAML | overwrite from YAML |
| `source` | `'config'` | overwrite to `'config'` (handles `ui_install` → `config` conflict per rule 3) |
| `quantization` | from YAML or null | overwrite from YAML |
| `gpus` | from YAML | overwrite from YAML |
| `max_model_len` | from YAML or null | overwrite from YAML |
| `storage_location` | from YAML or `defaults.storage` | overwrite from YAML |
| `extra_args` | from YAML JSON | overwrite from YAML |
| `status` | `'partial'` (reconciliation may upgrade) | **preserved** if reconciliation later confirms cache; reconciliation is the only path that writes this column |
| `cache_path` | null | **preserved** — only reconciliation writes this column |
| `size_bytes` | null | **preserved** — Phase 4 install/reconciliation writes this |
| `installed_at` | null | **preserved** — Phase 4 install writes this |
| `last_used_at` | null | **preserved** (PRD §5.11 usage history) |
| `request_count` | `0` | **preserved** (PRD §5.11 usage history) |

Implementation: SQL `INSERT ... ON CONFLICT(alias) DO UPDATE SET <only the declarative columns>` — the preserved columns are simply not in the `SET` list. Reconciliation runs as a separate step (§5.5) and only touches `status`, `cache_path`.

This is testable: a row whose `last_used_at=12345`, `request_count=42`, `installed_at=99000`, `size_bytes=8e9` must round-trip through a reload unchanged on those four columns, even when other declarative columns shift.

### 5.5 Cache reconciliation

For each row in `models`, derive the expected on-disk location and check it. The HF cache layout under any `HF_HOME` is:

```
<storage_path>/hub/models--<org>--<name>/snapshots/<rev>/
```

Algorithm (idempotent; runs after sync on startup AND reload):

```python
def reconcile_cache(self, storage_paths: dict[str, str]) -> ReconcileResult:
    for row in self.list_models():
        loc = storage_paths.get(row.storage_location)
        if not loc:
            # Storage location vanished from config. Leave row as-is, flag it.
            mark_partial(row, reason="storage_location_missing")
            continue

        # "Qwen/Qwen2.5-7B-Instruct" → "models--Qwen--Qwen2.5-7B-Instruct"
        cache_dir = os.path.join(loc, "hub", _hf_dir_name(row.hf_model_id))
        snapshot_dir = _newest_snapshot(cache_dir)   # None if no snapshots

        if snapshot_dir and _has_weights(snapshot_dir):
            update(row, cache_path=snapshot_dir, status="installed")
            # Reconciliation writes ONLY status + cache_path.
            # last_used_at, request_count, installed_at, size_bytes are
            # preserved per §5.4. size_bytes specifically defers to Phase 4.
        else:
            update(row, cache_path=None, status="partial")
```

Where:

- `_hf_dir_name("Qwen/Qwen2.5-7B")` returns `"models--Qwen--Qwen2.5-7B"` (HF cache convention).
- `_newest_snapshot` picks the most recently mtime'd subdir under `snapshots/`; returns `None` if the dir is missing or empty.
- `_has_weights` is a cheap heuristic: at least one `.safetensors`, `.bin`, or `.gguf` file under the snapshot. (Avoids treating an interrupted-mid-config-fetch dir as `installed`.)

This closes the gap the previous draft had: config rows whose cache already exists from before Phase 1 will correctly land as `'installed'` with a populated `cache_path` on the very first reconciliation pass. Full byte-accurate size accounting still defers to Phase 4.

## 6. `vllm_manager.py` integration

### 6.1 Module-level state additions

```python
_config: Config | None = None
_catalog: Catalog | None = None
```

That's it. Path env vars (`MNEMOSYNE_CONFIG_PATH`, `MNEMOSYNE_DB_PATH`, `MNEMOSYNE_ENV_PATH`) are read **inside** `load_config()` / `load_env()` / `open_catalog()` at call time, not at module import. This matters because [tests/conftest.py:16](../tests/conftest.py#L16) imports `vllm_manager` once before any test runs; if path globals were resolved at import, `monkeypatch.setenv(...)` from a fixture would have no effect. Reading at call time keeps the loaders test-friendly without the fixture having to also reset module globals.

`_config` and `_catalog` are reset by `tests/conftest.py::client` between tests (see §8).

### 6.2 `lifespan` changes

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _catalog
    load_env()                                  # populate os.environ
    _config = load_config()                     # reads MNEMOSYNE_CONFIG_PATH at call time
    _catalog = open_catalog()                   # reads MNEMOSYNE_DB_PATH at call time
    sync = _catalog.sync_from_config(_config.models, _config.storage.default)
    rec = _catalog.reconcile_cache({l.name: l.path for l in _config.storage.locations})
    logger.info("Catalog ready: %s, %s", sync, rec)

    _install_sighup_handler()                   # safe-skips when not on main thread
    logger.info(<existing banner>)
    yield
    _kill_vllm()
    if _catalog: _catalog.close()
```

Existing `_kill_vllm` and the banner are preserved.

### 6.3 SIGHUP handler (main-thread / platform guarded)

`loop.add_signal_handler` only works on Unix and only when the loop is running on the main thread. Both conditions fail under `fastapi.testclient.TestClient`, which spins up its own thread. Guard accordingly:

```python
import threading, signal, sys

def _install_sighup_handler() -> None:
    if sys.platform == "win32":
        logger.debug("SIGHUP not available on Windows; skipping.")
        return
    if threading.current_thread() is not threading.main_thread():
        logger.debug("SIGHUP handler skipped: not on main thread (likely TestClient).")
        return
    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(
            signal.SIGHUP,
            lambda: asyncio.create_task(_reload_config()),
        )
    except NotImplementedError:
        logger.warning("Event loop does not support add_signal_handler; SIGHUP disabled.")
```

The `POST /manager/reload` route is the always-available fallback; SIGHUP is the nicety that gets skipped under tests.

### 6.4 Reload — sync first, swap last

Order matters: if catalog sync raises against the new config, we must **not** have already replaced `_config`, otherwise the "old config still loaded" guarantee in §10 breaks.

```python
async def _reload_config() -> ReloadResult:
    """Reread config.yaml, re-sync catalog. Resident model untouched.

    On any failure, _config and _catalog state are unchanged. The caller
    sees the exception (POST /manager/reload turns it into a 400).
    """
    global _config
    new = load_config()                         # 1. parse new YAML; raises on error
    # 2. apply DB changes against `new` BEFORE swapping _config.
    sync = _catalog.sync_from_config(new.models, new.storage.default)
    rec = _catalog.reconcile_cache({l.name: l.path for l in new.storage.locations})
    # 3. only after sync+reconcile succeed do we publish the new config.
    _config = new
    return ReloadResult(sync=sync, reconcile=rec)
```

`Catalog.sync_from_config` already runs in a single transaction (§5.4), so a mid-sync failure rolls back DB state cleanly. Combined with the deferred `_config` swap, a failed reload leaves both DB and in-memory state untouched.

Failures during reload **do not** crash the process — the existing config stays loaded, the error is logged, and `/manager/reload` returns `400` with the message. (Startup is hard-fail; reload is soft-fail by intent: the user should be able to fix a typo without losing the running session.)

### 6.5 New endpoints (additive — Phase 0 contracts unchanged)

All under `/manager/*`, all `tags=["manager"]`. These will move to the admin plane in Phase 3; Phase 1 puts them on the single existing app.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/manager/reload` | Trigger `_reload_config()`. Returns `ReloadResult` JSON. 400 on failure. |
| `GET` | `/manager/profiles` | List **config-defined** aliases as full profile dicts (alias, model, quantization, gpus, storage, max_model_len, extra_args). Source: `_config.models`. Cache-only synthetic aliases are not included here regardless of any query param — this endpoint reflects YAML state. |
| `GET` | `/manager/storage` | List storage locations with `name`, `path`, `free_bytes`, `total_bytes`, `writable`, `is_default`. Free space via `shutil.disk_usage`. |
| `GET` | `/manager/catalog?include_cache_only=<bool>` | List catalog rows: alias, hf_model_id, source, status, storage_location, cache_path, size_bytes, last_used_at, installed_at, request_count. **Default excludes** rows whose alias starts with `__cache__:`. Pass `?include_cache_only=true` to include them. The query param is parsed as a boolean (`true`/`false`/`1`/`0`); any other value → 422. |

Response shapes are pinned by the new tests so future phases don't accidentally reshape them.

### 6.6 What does NOT change in Phase 1

- `/manager/status` — body keys stay exactly as Phase 0 pinned them.
- `/manager/load` — still takes `{model, tp, gpu_mem, extra_args}`. Phase 2 will add alias resolution against `_config`.
- `/manager/aliases` — the in-memory `MODEL_ALIASES` dict and its CRUD endpoints stay untouched. (The catalog and `MODEL_ALIASES` coexist for one phase. Phase 2 will deprecate `MODEL_ALIASES` in favor of catalog lookups; for now they're independent.)
- `/manager/download` — unchanged. Phase 4 routes it through the catalog.
- `_start_vllm`, `_proxy`, auto-swap — unchanged.
- `/health` — unchanged.

## 7. `vllm-ctl` changes (minimal)

Phase 1 keeps the CLI working. Add three thin wrappers; defer auth/URL changes to Phase 3.

- `vllm-ctl reload` → `POST /manager/reload`, print sync/reconcile counts.
- `vllm-ctl list` → `GET /manager/profiles`, print alias / model / GPU plan / storage. (Distinct from `vllm-ctl models`, which still walks the cache.)
- `vllm-ctl storage` → `GET /manager/storage`, print each location's name, path, free GB, writable status.

Help text updated; existing commands untouched.

## 8. Test plan

Build on the existing pytest harness. The fixture story has to handle one new constraint: Phase 1's `lifespan` hard-fails when `/config/config.yaml` is missing, and [tests/conftest.py:35](../tests/conftest.py#L35)'s `with TestClient(...) as c:` triggers `lifespan`. Without intervention every existing Phase 0 test breaks on import.

Two new fixtures in `tests/conftest.py`, and `client` becomes a dependent of the first:

```python
MINIMAL_CONFIG_YAML = """\
storage:
  default: tmp
  locations:
    - name: tmp
      path: {tmp_path}
"""

@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    """Redirect Phase 1 path env vars to tmp. Doesn't write any files."""
    monkeypatch.setenv("MNEMOSYNE_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("MNEMOSYNE_DB_PATH",     str(tmp_path / "mnemosyne.db"))
    monkeypatch.setenv("MNEMOSYNE_ENV_PATH",    str(tmp_path / ".env"))
    return tmp_path

@pytest.fixture
def tmp_config(tmp_paths):
    """Write a minimal valid config — enough for lifespan to start cleanly."""
    cfg = tmp_paths / "config.yaml"
    cfg.write_text(MINIMAL_CONFIG_YAML.format(tmp_path=tmp_paths))
    return cfg

@pytest.fixture
def client(tmp_config):                         # ← now depends on tmp_config
    # reset module globals
    vllm_manager.current_model = None
    vllm_manager.vllm_process = None
    vllm_manager.model_load_time = None
    vllm_manager._loading = False
    vllm_manager._current_tp = vllm_manager.DEFAULT_TP
    vllm_manager._current_gpu_mem = vllm_manager.DEFAULT_GPU_MEM
    vllm_manager._downloads = {}
    vllm_manager.MODEL_ALIASES = {}
    # reset Phase 1 globals
    if vllm_manager._catalog:
        vllm_manager._catalog.close()
    vllm_manager._config = None
    vllm_manager._catalog = None

    with TestClient(vllm_manager.app) as c:
        yield c
```

Why this shape:

- `tmp_paths` always runs and is cheap. It sets the env *before* `lifespan` runs (TestClient starts the lifespan inside the `with` block, which is **after** the fixture has set env vars). Combined with §6.1 reading paths at call time, this is enough for the loaders to find tmp files.
- `tmp_config` writes a minimal valid YAML — empty `models:` is allowed by the schema (§4.1), so the file is just `storage:` plus its location. `lifespan` succeeds, the catalog initializes empty, and Phase 0 smoke tests run unchanged.
- Tests that need richer config (e.g. `test_reload.py` writing a config with three aliases) overwrite the file before invoking the client or call `_reload_config()` directly.

`tests/test_smoke.py` does **not** need to be modified — it depends on `client`, which now transparently provides a writable config.

### 8.1 `test_config.py`

- Minimal valid YAML parses cleanly.
- `quantization`, `gpus: all`, `gpus: [0,1]`, single-GPU all parse.
- `extra_args` round-trips as list-of-strings.
- Unknown top-level key → fails (Pydantic strict).
- `storage.default` references missing location → `ConfigError`.
- `models[].storage` references missing location → `ConfigError`.
- Duplicate aliases → `ConfigError`.
- Malformed YAML (syntax error) → `ConfigError`.
- Empty `models: []` is allowed (a fresh deployment).
- `idle_unload_seconds: null` parses to `None`.
- `.env` populates `os.environ` for unset keys; doesn't override pre-set ones.
- `.env` missing → no error.
- GPU probe is mocked to avoid `nvidia-smi` dependency. Mocked-missing-gpu indices in `models[].gpus` → `ConfigError`. `nvidia-smi` returning non-zero (dev host) → log warning, no error.

### 8.2 `test_catalog.py`

- Schema bootstrap on a fresh DB creates both tables and `schema_version=1`.
- Re-running bootstrap is idempotent.
- `sync_from_config` upserts new aliases, updates changed ones, removes orphaned config rows.
- `source='ui_install'` rows survive sync.
- Conflict (config alias matches existing `ui_install` alias) → config wins, warning logged, status reflects config row.
- Cache reconciliation:
  - Pre-existing `models--<org>--<name>/snapshots/<rev>/<weights>.safetensors` under the storage path → row flips to `installed` with `cache_path` populated.
  - Empty `snapshots/` dir → stays `partial`, `cache_path` null.
  - Missing `models--*` dir → stays `partial`.
  - Previously `installed` row whose dir vanished → flips back to `partial`, `cache_path` cleared.
  - Storage location dropped from config → row flagged `partial` with the documented reason.
- `list_models`, `get_model` return the expected shapes.
- **Reload preserves durable metadata** (§5.4 column rules): seed a row with `last_used_at=12345`, `request_count=42`, `installed_at=99000`, `size_bytes=8_000_000_000`; run `sync_from_config` again with the same alias; assert all four columns are unchanged. Repeat with a slightly mutated YAML (different `quantization`) and assert declarative columns DID change while durable ones did NOT.
- Synthetic-alias prefix (`__cache__:`) is rejected by `ModelProfile.alias` validation. The defense-in-depth `__cache__/` prefix is also rejected. Both are accepted as direct catalog inserts (Phase 4 will exercise the insert path; Phase 1 only proves the validation gate).

### 8.3 `test_reload.py`

- `POST /manager/reload` with valid config → 200, sync counts in body.
- `POST /manager/reload` with malformed config on disk → 400, old config still loaded (a follow-up `GET /manager/profiles` returns the previous aliases).
- `POST /manager/reload` where `Catalog.sync_from_config` raises (monkeypatched) → 400, `_config` unchanged (proves the §6.4 sync-first/swap-last ordering).
- `GET /manager/profiles` shape pin. Even with synthetic `__cache__:...` rows present in the catalog, the response only contains config-defined aliases.
- `GET /manager/storage` shape pin (free/total bytes are positive ints; `is_default` flag set on exactly one row).
- `GET /manager/catalog` cache-only filter coverage:
  - Seed the DB with one config alias (sync) and one synthetic `__cache__:abcdef0123456789` row inserted directly.
  - `GET /manager/catalog` → only the config alias (default exclusion).
  - `GET /manager/catalog?include_cache_only=true` → both rows.
  - `GET /manager/catalog?include_cache_only=false` → only the config alias (explicit form).
  - `GET /manager/catalog?include_cache_only=banana` → 422.
- `GET /manager/catalog` shape pin (key set per row).

### 8.4 Phase 0 contracts hold

`tests/test_smoke.py` runs unchanged. CI fails if any pinned key in `/manager/status` or `/manager/aliases` shifts.

## 9. Operational notes

- Volume mounts: communicate clearly in the PR description that the user must add the three mounts (`/config/config.yaml`, `/config/.env`, `/state`) to their external compose file. Without them the container will hard-fail on first boot looking for `/config/config.yaml`. Provide a one-liner snippet.
- The `pyyaml` and `pydantic` Dockerfile additions force a layer rebuild — flag in the PR.
- `requirements-dev.txt` already has `pyyaml`; just add `pydantic>=2`.

## 10. Exit criteria

- Editing `~/vllm-manager/config.yaml` and running `vllm-ctl reload` makes new aliases visible via `GET /manager/profiles` and the `models` table. No container restart.
- `source='config'` rows track YAML; `source='ui_install'` rows are preserved across reload (verifiable by manual `INSERT` via `sqlite3` and a reload).
- `GET /manager/storage` reports free space and writability for each declared location.
- `GET /manager/catalog` reflects the current sync + reconcile state.
- Hard-fail on container start when YAML is malformed; soft-fail with 400 + retained old config on reload.
- `resolve_profile(alias, config, catalog)` returns a fully-merged `ResolvedProfile` for any config-defined alias (defaults applied, storage path resolved). Phase 2 will consume this directly. (Satisfies [implementation_plan.md:75](implementation_plan.md#L75).)
- A pre-existing HF cache for a config-defined model is detected on first boot: the row lands as `installed` with `cache_path` populated, without needing a re-download.
- All Phase 0 smoke tests still pass.
- New tests cover the validation matrix above and run in CI alongside the existing suite.

## 11. Open questions / risks

- **Pydantic strict mode.** Forbid extra fields at every level vs. allow-with-warning to ease the path for future schema growth? Recommendation: strict for now — it's a single-user config file, surprises are worse than friction. Easy to relax later.
- **Schema-version migrations on a hot DB.** Phase 1 is v1 only; no migration logic exercised. Worth writing the `if version < N:` skeleton anyway so Phase 4 isn't the first to touch it.
- **`MODEL_ALIASES` dual-life.** Keeping the in-memory alias dict alongside the catalog for one phase is mildly confusing. The alternative — merging immediately — would force `/manager/aliases` shape changes that the Phase 0 test pins. Cost of one phase of overlap is acceptable; flag for cleanup in Phase 2.
- **GPU probe on macOS dev hosts.** `nvidia-smi` is missing. Plan is "log warning, skip index check." Keeps `pytest` green on macOS. Verify in Phase 8 that production CUDA hosts do enforce the check.
- **`/state` permissions inside the container.** SQLite needs write access. Document the host dir ownership requirement (`chown` to the container user, or `:rw` mount) in the PR notes.
