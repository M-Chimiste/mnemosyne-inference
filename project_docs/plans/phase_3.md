# Phase 3 — Inference/Admin Plane Separation and Auth

## Context

The current manager is a single `FastAPI` app at [vllm_manager.py:534](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L534) bound to one port (`MANAGER_HOST:MANAGER_PORT`, defaults `0.0.0.0:8000`). All 18 routes — `/v1/{path:path}`, `/health`, and 15 `/manager/*` endpoints — sit on that single listener with no auth. Phases 0–2 already shipped the scaffolding: `config.py:Server` carries `inference_port`, `admin_port`, `inference_bind`, `admin_bind` (parsed but unused — Phase 1 startup logs "parsed but not bound"), and `config.py:load_env` reads `/config/.env`. `ADMIN_PASSWORD` and `INFERENCE_API_KEY` are documented in `.env.example` but never read in Python.

Phase 3 enforces the PRD §5.10 boundary at the network layer: two listeners in one process running both uvicorn servers as `asyncio` tasks under `asyncio.wait(..., FIRST_EXCEPTION)` (so a bind failure on either side cleanly tears down the other), sharing module state. Inference (`:8000`) keeps `/v1/*` and `/health` open by default with optional bearer auth. Admin (`:8001`) gates `/manager/*` (and `/v1/*`, since admin is a superset) behind HTTP Basic. If `ADMIN_PASSWORD` is unset, the admin port refuses to bind to anything other than `127.0.0.1` (fail-safe).

---

## Architectural decisions

| Decision | Choice | Why |
|---|---|---|
| App split | Two `FastAPI` instances built at file bottom; route definitions on `APIRouter()` objects declared at file top. `include_router()` snapshots routes, so the apps must be constructed *after* all decorators run | Per-router `dependencies=[...]` is the cleanest way to scope auth |
| Lifespan | Extracted from FastAPI; one top-level `async with manager_lifespan(cfg)` wraps the `asyncio.wait(..., FIRST_EXCEPTION) → signal both → drain` server loop | Symmetric, decoupled from listener readiness, makes test setup easier; gives clean shutdown when one server fails |
| Auth mechanism | FastAPI dependencies (`Depends(...)`), not Starlette middleware | OpenAPI-aware, override-friendly in tests, scopes per-router cleanly |
| Admin `/v1/*` superset | Include in Phase 3 (PRD §5.10 explicit) | Avoids retesting churn later; tests keep using one client |
| `vllm_manager.app` alias | Keep `app = admin_app` | Zero churn for tests using `TestClient(vllm_manager.app)` |
| `MANAGER_HOST` / `MANAGER_PORT` | **Drop.** Config drives binds | Phase 1 already labeled them legacy; removes overlapping override paths |
| `VLLM_INNER_PORT` default | **Move from 8001 → 8002.** Add a startup guard that fails if it equals either external port | Inner vLLM and admin app would both want 8001 in the container — same network namespace, hard conflict |
| Port collision check | Three-way: config Pydantic validator (`inference_port != admin_port`) **plus** `main()` runtime guard against `VLLM_INNER_PORT` | Pydantic can't see env vars; the env-var port needs a runtime check |
| Fail-safe bind | Computed in `main()` after env load, before uvicorn config build | Simplest place; logged warning |
| Fail-safe + Docker reachability | Document explicitly: loopback admin **inside** the container is unreachable through `-p 8001:8001`. Set `ADMIN_PASSWORD` if you want host-side `vllm-ctl` admin commands to work | Docker bridge networking forwards published ports to container `0.0.0.0` binds, not loopback |
| Signal handling | Subclass `uvicorn.Server` overriding **both** `capture_signals()` (modern uvicorn ≥0.30) and `install_signal_handlers()` (legacy) to no-op; install our own SIGTERM/SIGINT handler at the gather level | Modern uvicorn wraps `serve()` in `with self.capture_signals():` — overriding only the legacy method does nothing. Override both for forward/backward compat |
| `/docs` | Disabled on inference (`docs_url=None, redoc_url=None, openapi_url=None`); kept on admin | Don't leak admin route surface to LAN |
| `/health` | Own router, no auth dep, included on both apps | Container healthcheck must work without creds |
| `vllm-ctl` env load | Targeted `grep` parse of `${VLLM_COMPOSE_DIR}/.env` for `ADMIN_PASSWORD`, not `source` | Avoids polluting every spawned subprocess with all `.env` keys |

---

## File-by-file changes

### 1. [config.py](/Users/c/software_projects/mnemosyne-inference/config.py) — port-collision validator

Add to the `Server` model (after the existing fields, around [config.py:47](/Users/c/software_projects/mnemosyne-inference/config.py#L47)):

```python
@model_validator(mode="after")
def _ports_distinct(self):
    if self.inference_port == self.admin_port:
        raise ValueError(
            f"server.inference_port and server.admin_port must differ "
            f"(both = {self.inference_port})"
        )
    return self
```

No other changes here — the four port/bind fields already exist with the right defaults. The third port (`VLLM_INNER_PORT`, an env var, not a config field) is checked at runtime in `main()` — see §2g.

### 2. [vllm_manager.py](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py) — the bulk of the work

**2a. Imports + drop legacy env reads + bump inner-vLLM default port.**

The current FastAPI import at [vllm_manager.py:32](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L32) brings in only `FastAPI, HTTPException, Query, Request`. The refactor adds:
- `APIRouter, Depends` from `fastapi`
- `HTTPBasic, HTTPBasicCredentials` from `fastapi.security`
- `secrets`, `signal`, `contextlib` from stdlib (verify which are already imported — `contextlib` is likely already in)

Remove lines 47–48 (`MANAGER_HOST`, `MANAGER_PORT`). Change `VLLM_INNER_PORT` default at line 46 from `"8001"` to `"8002"` — inside the container the admin app now wants `8001`, and `0.0.0.0:8001` (admin) collides with `127.0.0.1:8001` (inner vLLM) since they share the same network namespace. Also bump any matching reference inside `_start_vllm` if the inner port flows through there. The runtime guard in §2g catches the case where a user has set `VLLM_INNER_PORT` explicitly to one of the external ports.

**2b. Add an auth section** (new block near the top, after the existing Configuration block):

```python
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

_basic = HTTPBasic(auto_error=False)

def require_admin_basic(creds: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    expected = os.environ.get("ADMIN_PASSWORD")
    if not expected:
        # Loopback-only mode (fail-safe bind enforced at startup); allow.
        return "admin"
    if creds is None:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    user_ok = secrets.compare_digest(creds.username, "admin")
    pw_ok = secrets.compare_digest(creds.password, expected)
    if not (user_ok and pw_ok):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    return creds.username

async def require_inference_bearer(request: Request) -> None:
    expected = os.environ.get("INFERENCE_API_KEY")
    if not expected:
        return
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401)
    if not secrets.compare_digest(auth[len("Bearer "):], expected):
        raise HTTPException(401)
```

**2c. Refactor route decorators to use three `APIRouter` objects:**

| Router | Routes (decorator changes only — bodies unchanged) |
|---|---|
| `health_router` | `/health` (currently [vllm_manager.py:932](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L932)) |
| `inference_router` | `/v1/{path:path}` ([line 1104](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L1104)) |
| `admin_router` | All 15 `/manager/*` routes (lines 546, 592, 605, 626, 665, 677, 729, 749, 826, 891, 912, 918, 1121, 1127, 1147) |

Mechanical: change `@app.get(...)` / `@app.post(...)` / `@app.api_route(...)` / `@app.delete(...)` to `@admin_router.<verb>(...)` etc. Tags preserved.

**2d. Extract lifespan; accept pre-loaded config + opt-out flags for signals and background tasks.** Rename `lifespan(app)` ([vllm_manager.py:465](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L465)) to `manager_lifespan(cfg=None, *, install_signals=True, spawn_background=True)`. The two flags exist so test fixtures can run setup/teardown without (a) attaching a SIGHUP handler to a short-lived test event loop that's about to be closed, and (b) spawning `_eviction_task` / `_flush_task` onto that test loop where they'd never run because TestClient serves on a different loop.

```python
@asynccontextmanager
async def manager_lifespan(
    cfg: "Config | None" = None,
    *,
    install_signals: bool = True,
    spawn_background: bool = True,
):
    global _config, _catalog
    if cfg is None:
        load_env()
        cfg = load_config()
    _config = cfg                                # ← explicit; downstream reads _config.models
    _catalog = open_catalog()
    _catalog.apply_config(...)
    if install_signals:
        _install_sighup_handler()
    if spawn_background:
        if _config.server.idle_unload_seconds is not None:   # 0 is "disabled" via None only
            ...spawn _eviction_task...
        ...spawn _flush_task...
    try:
        yield
    finally:
        if spawn_background:
            ...cancel tasks...
        _flush_usage_best_effort("lifespan shutdown")
        _kill_vllm()
        _catalog.close()
```

- Production (`_serve_both`): `manager_lifespan(cfg)` — defaults give SIGHUP and background tasks.
- Test fixtures (`_running_lifespan`): `manager_lifespan(install_signals=False, spawn_background=False)` — synchronous, ephemeral, no signal/task leaks.
- Tests that specifically exercise eviction (e.g., `test_eviction.py`) drive `_eviction_loop` directly today; that pattern continues to work since the loop function is independent of whether the lifespan spawned the task.

This fixes (a) the v1 bug that left `_config = None` in the production path, and (b) the v2 oversight where the test helper's private loop closed with a SIGHUP handler and a half-spawned eviction task still attached.

Update the Phase 1 "parsed but not bound" log line ([vllm_manager.py:483-486](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L483-L486)) to a Phase 3 banner that prints both bound endpoints plus inner vLLM port.

**2e. Build the two apps — file ordering is load-bearing.**

> ⚠️ **`FastAPI.include_router()` snapshots the router's route table at the moment it is called. Routes registered on the router *after* `include_router()` are silently ignored.** Get the order wrong here and every test will 404.

The refactor splits the current monolithic `app = FastAPI(...)` block at lines 534–539 across **two separate file positions**:

**Position A — top of the module, immediately after the auth section §2c, replacing the deleted `app = FastAPI(...)` block:**

```python
# Routers — populated by the @router.<verb>(...) decorators below. Apps
# that use them are built at the BOTTOM of this file (see Position B),
# AFTER all route decorators have run, because include_router() snapshots.
health_router = APIRouter()
inference_router = APIRouter()
admin_router = APIRouter()
```

(Nothing else at this position. No `FastAPI(...)`. No `include_router(...)`.)

**Position B — bottom of the module, immediately before `if __name__ == "__main__":`, AFTER the last route decorator (currently `@app.delete("/manager/aliases/{alias}")` at line 1147):**

```python
# ── App construction ────────────────────────────────────────────────
# Must run after every @<router>.<verb>(...) decorator above, because
# FastAPI.include_router() copies routes by value at call time.

inference_app = FastAPI(
    title="Mnemosyne Inference",
    version="1.0.0",
    docs_url=None, redoc_url=None, openapi_url=None,
)
inference_app.include_router(health_router)
inference_app.include_router(
    inference_router,
    dependencies=[Depends(require_inference_bearer)],
)

admin_app = FastAPI(
    title="Mnemosyne Admin",
    version="1.0.0",
    description="Admin plane: /manager/*, /ui/* (Phase 6), and /v1/* superset.",
)
admin_app.include_router(health_router)
admin_app.include_router(
    admin_router,
    dependencies=[Depends(require_admin_basic)],
)
admin_app.include_router(
    inference_router,
    dependencies=[Depends(require_admin_basic)],
)

# Back-compat alias — tests and importers using `from vllm_manager
# import app` keep working. Admin is the superset.
app = admin_app
```

Implementation order to avoid breaking the file mid-refactor:
1. Add the auth section §2c.
2. Add Position A (router declarations) and remove the old `app = FastAPI(...)` block.
3. Sweep route decorators §2d (`@app.<verb>` → `@<router>.<verb>`).
4. Add Position B at the bottom of the file (this is the moment `app = admin_app` reappears).
5. Switch the entrypoint §2g.

Between steps 2 and 4 the module won't import — that's fine; commit at step 4 or later.

**2f. Strip auth headers from `_proxy` upstream forwarding.** [vllm_manager.py:971-974](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L971-L974) currently forwards every header except `host`/`content-length`, which would leak the admin's Basic credentials and the inference port's bearer token into vLLM's logs. Add `authorization` and `cookie` to the exclusion set:

```python
headers = {
    k: v for k, v in request.headers.items()
    if k.lower() not in ("host", "content-length", "authorization", "cookie")
}
```

**2g. Replace the entrypoint** ([vllm_manager.py:1160-1166](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py#L1160-L1166)):

```python
class _ManagedServer(uvicorn.Server):
    """uvicorn.Server with signal-handler installation suppressed.
    We install one handler at the gather level so a single SIGTERM
    sets `should_exit` on both server instances atomically.

    Modern uvicorn (≥0.30) wraps `serve()` in `with self.capture_signals():`.
    Older versions called `self.install_signal_handlers()` directly. We
    override both so this works regardless of which uvicorn the Dockerfile
    pulls in."""
    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self) -> None:  # legacy uvicorn
        return

def _resolve_admin_bind(cfg_bind: str) -> str:
    if not os.environ.get("ADMIN_PASSWORD") and cfg_bind != "127.0.0.1":
        logger.warning(
            "ADMIN_PASSWORD unset; forcing admin bind from %s to 127.0.0.1 "
            "(fail-safe). Admin port will be unreachable from outside the "
            "container — set ADMIN_PASSWORD in /config/.env for LAN admin.",
            cfg_bind,
        )
        return "127.0.0.1"
    return cfg_bind

def _check_inner_port_clash(cfg) -> None:
    inner = int(os.environ.get("VLLM_INNER_PORT", "8002"))
    if inner in (cfg.server.inference_port, cfg.server.admin_port):
        raise SystemExit(
            f"VLLM_INNER_PORT={inner} collides with "
            f"server.inference_port={cfg.server.inference_port} or "
            f"server.admin_port={cfg.server.admin_port}. "
            f"Pick an unused port (default 8002)."
        )

async def _serve_both(cfg) -> None:
    inf_cfg = uvicorn.Config(
        inference_app,
        host=cfg.server.inference_bind,
        port=cfg.server.inference_port,
        log_level="info",
        lifespan="off",
    )
    adm_cfg = uvicorn.Config(
        admin_app,
        host=_resolve_admin_bind(cfg.server.admin_bind),
        port=cfg.server.admin_port,
        log_level="info",
        lifespan="off",
    )
    inf_server = _ManagedServer(inf_cfg)
    adm_server = _ManagedServer(adm_cfg)

    loop = asyncio.get_running_loop()
    def _shutdown():
        inf_server.should_exit = True
        adm_server.should_exit = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown)

    async with manager_lifespan(cfg):           # ← passes cfg explicitly
        # If one server fails to bind (or any other exception), we want
        # the other to wind down too — not be torn from under itself by
        # gather() raising. Wait for FIRST_EXCEPTION (or completion),
        # signal both, then drain with return_exceptions=True so we can
        # log per-server errors without losing the first one.
        inf_task = asyncio.create_task(inf_server.serve(), name="inference-uvicorn")
        adm_task = asyncio.create_task(adm_server.serve(), name="admin-uvicorn")
        done, pending = await asyncio.wait(
            {inf_task, adm_task}, return_when=asyncio.FIRST_EXCEPTION
        )
        _shutdown()                              # set should_exit on both
        results = await asyncio.gather(inf_task, adm_task, return_exceptions=True)
        for name, result in zip(("inference", "admin"), results):
            if isinstance(result, BaseException):
                logger.error("%s uvicorn exited with error: %r", name, result)
        # Re-raise the first exception so the process exits non-zero.
        for result in results:
            if isinstance(result, BaseException):
                raise result

if __name__ == "__main__":
    load_env()
    cfg_at_boot = load_config()
    _check_inner_port_clash(cfg_at_boot)
    asyncio.run(_serve_both(cfg_at_boot))
```

(`signal` and `contextlib` imports already present or trivially added.)

### 3. [vllm-ctl](/Users/c/software_projects/mnemosyne-inference/vllm-ctl) — auth + URL split

**3a. URL resolution** (top of script, replacing line 20):

```bash
# Admin URL has NO fallback to VLLM_MANAGER_URL: existing users have that
# pointed at :8000 (inference), and falling back would silently route admin
# commands through the inference port and 404. Users who customized the
# admin host must set VLLM_ADMIN_URL explicitly. This is called out in the
# Phase 3 release note in CLAUDE.md.
ADMIN_URL="${VLLM_ADMIN_URL:-http://localhost:8001}"
INFERENCE_URL="${VLLM_INFERENCE_URL:-${VLLM_MANAGER_URL:-http://localhost:8000}}"
```

Inference URL keeps the `VLLM_MANAGER_URL` fallback because the legacy default (`:8000`) is the new inference port — same value, no semantic change.

**3b. Targeted password loader** (one-time at script entry):

```bash
load_admin_password() {
  [[ -n "${ADMIN_PASSWORD:-}" ]] && return
  local env_file="${VLLM_COMPOSE_DIR:-$HOME/vllm-manager}/.env"
  [[ -r "$env_file" ]] || return
  ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' "$env_file" \
    | head -1 | cut -d= -f2- \
    | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//')"
  export ADMIN_PASSWORD
}
load_admin_password
```

**3c. Update `api()` helper** ([vllm-ctl:30-35](/Users/c/software_projects/mnemosyne-inference/vllm-ctl#L30-L35)):

```bash
api() {
  local method="$1"; shift
  local path="$1";   shift
  local auth_args=()
  [[ -n "${ADMIN_PASSWORD:-}" ]] && auth_args=(-u "admin:${ADMIN_PASSWORD}")
  curl -sf -X "$method" "${ADMIN_URL}${path}" \
    -H "Content-Type: application/json" \
    "${auth_args[@]}" "$@"
}
```

**3d. Refactor the three inline curls to use `api()`:**
- `cmd_load` ([line 189](/Users/c/software_projects/mnemosyne-inference/vllm-ctl#L189))
- `cmd_reload` ([line 230](/Users/c/software_projects/mnemosyne-inference/vllm-ctl#L230))
- `cmd_download` ([line 400](/Users/c/software_projects/mnemosyne-inference/vllm-ctl#L400))

**3e. `cmd_chat`** ([line 350](/Users/c/software_projects/mnemosyne-inference/vllm-ctl#L350)) — keep using `${INFERENCE_URL}/v1/chat/completions`. Add an `Authorization: Bearer ${INFERENCE_API_KEY}` header only if `INFERENCE_API_KEY` is set in the script's env (don't auto-load this from `.env`; if needed it's already exportable).

**3f. `cmd_start`** ([line 52](/Users/c/software_projects/mnemosyne-inference/vllm-ctl#L52)) — two changes:

1. Wait-loop polls `${INFERENCE_URL}/health`, **not** admin's `/health`. Reason: when `ADMIN_PASSWORD` is unset, the admin port binds to container loopback, which is unreachable through Docker's `-p 8001:8001` port publish (the bridge forwards to container `0.0.0.0` only). Inference port is always reachable; both apps share the same process, so inference `/health` is a sufficient process-liveness signal.

2. The post-start `cmd_status` call at [line 55](/Users/c/software_projects/mnemosyne-inference/vllm-ctl#L55) hits the admin port, so in fail-safe mode (no `ADMIN_PASSWORD`) it would print a confusing "admin unreachable" error immediately after a successful start. Wrap it: only call `cmd_status` if `[[ -n "${ADMIN_PASSWORD:-}" ]]`. Otherwise print a friendlier hint:

```bash
if [[ -n "${ADMIN_PASSWORD:-}" ]]; then
  cmd_status
else
  yellow "Admin port is loopback-only (ADMIN_PASSWORD unset). Use 'docker exec vllm-manager curl http://127.0.0.1:8001/manager/status' or set ADMIN_PASSWORD in ~/vllm-manager/.env."
fi
```

### 4. [tests/conftest.py](/Users/c/software_projects/mnemosyne-inference/tests/conftest.py) — auth fixtures + lifespan helper

Lifespan extraction means **none of the existing fixtures will set up `_config`/`_catalog` anymore** — `TestClient(vllm_manager.app)` no longer triggers `manager_lifespan()` because both apps have no FastAPI lifespan. Every fixture that yields a TestClient must drive `manager_lifespan()` itself.

**4a. Autouse `ADMIN_PASSWORD` fixture** (so admin Basic is exercised, not bypassed by the loopback fallback in `require_admin_basic`):

```python
@pytest.fixture(autouse=True)
def _admin_password(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")
```

**4b. Lifespan helper** (used by every client fixture):

```python
@contextlib.contextmanager
def _running_lifespan():
    """Drive manager_lifespan() across a sync test. Uses a private loop
    so test code can stay synchronous (TestClient is sync).

    Passes install_signals=False and spawn_background=False so:
      - SIGHUP isn't attached to a loop that's about to close.
      - _eviction_task / _flush_task aren't scheduled onto a loop that
        won't run after this contextmanager exits.

    Tests that exercise eviction call _eviction_loop directly (existing
    pattern in tests/test_eviction.py)."""
    loop = asyncio.new_event_loop()
    cm = vllm_manager.manager_lifespan(install_signals=False, spawn_background=False)
    loop.run_until_complete(cm.__aenter__())
    try:
        yield
    finally:
        loop.run_until_complete(cm.__aexit__(None, None, None))
        loop.close()
```

**4c. Update `client` to wrap the lifespan and use `with TestClient(...)`** for proper close:

```python
@pytest.fixture
def client(tmp_config):
    _reset_globals()
    with _running_lifespan():
        with TestClient(vllm_manager.app) as c:
            c.auth = ("admin", "test-pw")
            yield c
```

**4d. Update `rich_client` similarly:**

```python
@pytest.fixture
def rich_client(rich_config, stub_vllm):
    _reset_globals()
    with _running_lifespan():
        with TestClient(vllm_manager.app) as c:
            c.auth = ("admin", "test-pw")
            yield c, stub_vllm
```

**4e. Add `inference_client` fixture** for plane-separation tests:

```python
@pytest.fixture
def inference_client(tmp_config):
    _reset_globals()
    with _running_lifespan():
        with TestClient(vllm_manager.inference_app) as c:
            yield c   # no auth
```

Note: with the apps' FastAPI `lifespan` set to no-op (or `None`), `with TestClient(...) as c:` becomes a cheap nested context that just closes the underlying httpx client. The real startup/teardown is done by `_running_lifespan`.

With `install_signals=False`, `manager_lifespan` does not touch any signal handlers — safe to drive from a test loop that's about to close. With `spawn_background=False`, `_eviction_task` and `_flush_task` are never created, so there's nothing to cancel awkwardly. Verify the pattern with one trial test (`test_smoke.py`) before scaling to all fixtures.

### 5. New [tests/test_planes.py](/Users/c/software_projects/mnemosyne-inference/tests/test_planes.py)

Tests to cover Phase 3 acceptance criteria:

1. `test_inference_port_404s_manager_routes` — `inference_client.get("/manager/status")` returns 404; same for `POST /manager/load`.
2. `test_admin_requires_basic_when_password_set` — TestClient without `c.auth` → 401 on `/manager/status`.
3. `test_admin_basic_succeeds_with_correct_creds` — with `c.auth = ("admin", "test-pw")` → 200.
4. `test_admin_basic_fails_with_wrong_password` — wrong password → 401.
5. `test_admin_v1_proxy_under_basic` — admin `/v1/...` works when Basic creds provided (superset rule).
6. `test_inference_bearer_required_when_set` — set `INFERENCE_API_KEY=k`, no header → 401; `Bearer k` → not 401.
7. `test_inference_open_when_bearer_unset` — unset `INFERENCE_API_KEY`, no header → not 401.
8. `test_health_no_auth_either_plane` — `/health` returns 200 on both clients without creds.
9. `test_resolve_admin_bind_forces_loopback_unset_password` — pure unit test on `_resolve_admin_bind`.
10. `test_resolve_admin_bind_respects_config_when_password_set` — pure unit test.
11. `test_ports_distinct_validator` — config with equal ports → `ConfigError` from `load_config()`.
12. `test_proxy_strips_authorization_header` — assert vLLM upstream call (mock or capture) does not see `Authorization`.

### 6. [Dockerfile](/Users/c/software_projects/mnemosyne-inference/Dockerfile)

Three edits:

**6a. Add `runtime.py` to the COPY** at [Dockerfile:59](/Users/c/software_projects/mnemosyne-inference/Dockerfile#L59). Current line is:

```dockerfile
COPY vllm_manager.py config.py catalog.py profiles.py ./
```

`vllm_manager.py:40` does `from runtime import RuntimeState, build_vllm_argv, build_vllm_env, derive_tp_size`, so the existing image is already broken — Phase 3 just happens to be the place we discovered it. Change to:

```dockerfile
COPY vllm_manager.py config.py catalog.py profiles.py runtime.py ./
```

**6b. Expose admin port:** `EXPOSE 8000` (line 67) → `EXPOSE 8000 8001`.

**6c. Update the inline port comment** (line 69) to: "manager: inference :8000, admin :8001 (LAN-gated by ADMIN_PASSWORD); vLLM inner on 127.0.0.1:8002 inside container (was 8001 in Phase 0–2)".

The inner vLLM port move from 8001 → 8002 is critical: 0.0.0.0:8001 (admin) and 127.0.0.1:8001 (inner) **share the container's network namespace** and would conflict on bind. The new default puts inner vLLM on a different port; the runtime guard `_check_inner_port_clash` (§2g) catches the case where someone overrides `VLLM_INNER_PORT` to a colliding value.

### 7. External `docker-compose.yml`

`docker-compose.yml` lives outside the repo at `${VLLM_COMPOSE_DIR}/docker-compose.yml` (default `~/vllm-manager`). The user must update it to:
- Map both `8000:8000` and `8001:8001`.
- Optionally bind `8001` to `127.0.0.1` at the host level if the user wants to firewall the admin port without relying on the in-process fail-safe.

This needs to be **flagged in the PR description** (per CLAUDE.md guidance about the external compose file). I will not edit a file that's not in this repo.

### 8. Docs

- [CLAUDE.md](/Users/c/software_projects/mnemosyne-inference/CLAUDE.md) — update the curl example (line 52) to use the admin URL with `-u admin:...`, and note the plane separation in the architecture section.
- [.env.example](/Users/c/software_projects/mnemosyne-inference/.env.example) — already correct; comments already describe the fail-safe bind.
- [config.yaml.example](/Users/c/software_projects/mnemosyne-inference/config.yaml.example) — already correct.
- Append a Phase 3 section to existing project_status notes if that file exists.

---

## Verification

### Unit / integration (offline, on dev macOS)

```bash
cd /Users/c/software_projects/mnemosyne-inference
python -m pytest -q
```

Expectations:
- All existing tests pass (auth header pre-set on `client`/`rich_client`).
- New `tests/test_planes.py` passes (~12 cases).
- `python -m py_compile vllm_manager.py config.py runtime.py catalog.py profiles.py` clean.
- `bash -n vllm-ctl` clean.

### Container smoke test (on the workstation)

```bash
# Build with the multi-port EXPOSE and updated inner-vLLM port.
./vllm-ctl build

# Test 1: ADMIN_PASSWORD unset → admin port container-loopback only.
# Loopback inside the container is NOT reachable via `-p 8001:8001` from the host —
# Docker's bridge forwards to container 0.0.0.0, not 127.0.0.1. So the host cannot
# reach :8001 at all in this mode. This is the documented fail-safe behavior.
rm -f ~/vllm-manager/.env
./vllm-ctl start                               # waits on INFERENCE /health, succeeds
curl -sf http://localhost:8000/health          # 200
curl -sf --max-time 3 http://localhost:8001/health || echo "expected: unreachable"
docker exec vllm-manager curl -sf http://127.0.0.1:8001/health  # 200 (in-container)
./vllm-ctl status                              # FAILS (admin unreachable from host) — expected
./vllm-ctl stop

# Test 2: ADMIN_PASSWORD set → admin port LAN-bound, Basic enforced.
echo 'ADMIN_PASSWORD=correct-horse' > ~/vllm-manager/.env
./vllm-ctl start
curl -sf http://localhost:8001/health                            # 200 (no auth on /health)
curl -sf http://localhost:8001/manager/status                    # 401
curl -sf -u admin:correct-horse http://localhost:8001/manager/status  # 200
curl -sf http://localhost:8000/manager/status                    # 404 (plane separation)
curl -sf http://localhost:8000/health                            # 200

# Test 3: vllm-ctl picks up password from .env automatically.
./vllm-ctl status                              # 200 (auto-loaded from ~/vllm-manager/.env)
./vllm-ctl chat "ping"                         # 503 if no model loaded; load one first
./vllm-ctl load Qwen/Qwen2.5-Coder-7B-Instruct # admin op; uses Basic
./vllm-ctl chat "ping"                         # now 200
./vllm-ctl stop
```

Note that `curl http://localhost:8000/v1/models` returns 503 with no model loaded — `_proxy` requires either a resident model or a `model` field in the request body. Phase 3 doesn't change this behavior; it's listed here only so the smoke test doesn't depend on it.

### Acceptance criteria from `implementation_plan.md` Phase 3

- [x] `POST /manager/*` on inference port returns 404 — covered by `test_inference_port_404s_manager_routes` and the smoke test.
- [x] Admin routes require HTTP Basic when exposed beyond loopback — covered by `test_admin_requires_basic_when_password_set` + smoke Test 2.
- [x] CLI admin commands work when `ADMIN_PASSWORD` is set and admin is host-reachable — covered by smoke Test 3. (When `ADMIN_PASSWORD` is unset, host-side admin is intentionally unreachable per the fail-safe; container-internal `docker exec` is the supported path. This is documented behavior, not a regression.)

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Two uvicorn servers fighting over SIGTERM | Subclass `uvicorn.Server` overriding **both** `capture_signals()` (modern uvicorn ≥0.30, used as `with self.capture_signals():` inside `serve()`) and `install_signal_handlers()` (legacy) to no-op; install one handler at the gather level setting `should_exit=True` on both. `lifespan="off"` alone is **not** enough |
| One uvicorn server failing to bind tearing the other from under itself | Use `asyncio.wait(..., return_when=FIRST_EXCEPTION)`, then signal `should_exit` on both, then `gather(..., return_exceptions=True)` to drain. Re-raise the first exception so the process exits non-zero |
| Test fixture loop closing with SIGHUP attached / background tasks pending | `manager_lifespan(install_signals=False, spawn_background=False)` keyword args used by `_running_lifespan`; production path uses defaults (both True) |
| Inner vLLM and admin port both want 8001 inside the container (same network namespace) | Bump `VLLM_INNER_PORT` default 8001 → 8002; runtime guard `_check_inner_port_clash` in `main()` rejects user overrides that re-collide |
| Admin Basic creds leaking into vLLM logs via `_proxy` | Strip `authorization` and `cookie` in `_open_upstream` headers (line 971); add `test_proxy_strips_authorization_header` |
| Lifespan body references `_config` but extracted `load_config()` runs only in `main()` | `manager_lifespan(cfg)` accepts a pre-loaded config and **explicitly assigns** `_config = cfg` before any catalog work; tests pass `cfg=None` and lifespan loads it as before |
| Loopback admin in Docker is unreachable from host via `-p 8001:8001` | Document explicitly in `vllm-ctl` comments, plan smoke test, and CLAUDE.md. `vllm-ctl start` polls inference `/health`, not admin's, so the wait loop still works in fail-safe mode |
| `VLLM_MANAGER_URL` falling back to admin would route to inference port → 404 | Drop the fallback for `ADMIN_URL`; require explicit `VLLM_ADMIN_URL` if not localhost. Keep the fallback only for `INFERENCE_URL` (legacy default 8000 == new inference port) |
| Tests using `inference_client` need lifespan setup outside FastAPI | `manager_lifespan(cfg=None)` runs standalone; `inference_client` fixture wraps it via a small loop helper |
| Users with `MANAGER_HOST`/`MANAGER_PORT` set in their compose file will silently be ignored | Single line in Phase 3 PR description + CLAUDE.md noting env vars are gone |
| `vllm-ctl` parsing `.env` chokes on quoted values or `=` in the password | The `sed` strip handles both single and double quotes; `cut -d= -f2-` preserves embedded `=` characters |
| Smoke test for `/v1/models` confused — returns 503 not 200 with no model | Drop the assertion; `_proxy` 503 behavior is by design and unchanged in Phase 3 |

---

## Critical files for implementation

- [vllm_manager.py](/Users/c/software_projects/mnemosyne-inference/vllm_manager.py) — most changes (routes, auth deps, two apps, lifespan extraction, `main()` entrypoint, `_open_upstream` header strip)
- [config.py](/Users/c/software_projects/mnemosyne-inference/config.py) — port-distinct validator
- [vllm-ctl](/Users/c/software_projects/mnemosyne-inference/vllm-ctl) — `ADMIN_URL`/`INFERENCE_URL`, password loader, `api()` Basic header, refactor 3 inline curls
- [tests/conftest.py](/Users/c/software_projects/mnemosyne-inference/tests/conftest.py) — autouse `ADMIN_PASSWORD` fixture, `client.auth`, new `inference_client` fixture
- [tests/test_planes.py](/Users/c/software_projects/mnemosyne-inference/tests/test_planes.py) — new file, ~12 tests
- [Dockerfile](/Users/c/software_projects/mnemosyne-inference/Dockerfile) — `EXPOSE 8000 8001`
- [CLAUDE.md](/Users/c/software_projects/mnemosyne-inference/CLAUDE.md) — curl example, plane note
