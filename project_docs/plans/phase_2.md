# Phase 2 Plan — Runtime Lifecycle, Lazy Loading, Queueing, and Idle Eviction

**Source:** [implementation_plan.md](../implementation_plan.md) §Phase 2, [PRD.md](../PRD.md) §5.3, §5.7, §5.11
**Predecessors:** [phase_1_plan.md](../phase_1_plan.md) (config, catalog, profiles, reload) — complete.
**Successors:** Phase 3 splits the listeners and adds auth. Phase 4 wires the install/download lifecycle into the catalog.

**File location:** committed in-repo at `project_docs/plans/phase_2.md`. All relative paths below are written from that location.

## 1. Context

Phase 1 landed the declarative substrate: `Config` is parsed from YAML, the SQLite catalog is sync'd to it on startup and reload, and `resolve_profile(alias, config, catalog)` returns a fully-merged `ResolvedProfile`. None of that is wired into the runtime yet — `_start_vllm` still takes raw `(model_id, tp, gpu_mem, extra_args)` from the legacy env-driven payload, and `_maybe_swap` still 409s if a swap is racing.

Phase 2 connects the substrate to the runtime: every load funnels through a `ResolvedProfile`, `/v1/*` requests resolve aliases first and queue (not 409) during swaps, and an idle-eviction loop reclaims VRAM. **No new external surface in Phase 2** — same single FastAPI app, same ports, same auth model. Plane separation is Phase 3.

## 2. Scope

In:

- `_start_vllm(profile)` rewritten to consume a `ResolvedProfile`. Pure builders for the vLLM CLI argv and the subprocess env, extracted into `runtime.py` so they're unit-testable without asyncio/subprocess.
- `/v1/*` auto-swap routed through `resolve_profile`: alias → config → catalog `ui_install` → raw HF id fallback.
- LMStudio-style swap queue: per-target piggyback `Event` + a single `_swap_lock` for cross-target serialization. Bounded by `swap_queue_timeout_seconds`. 504 on timeout, 503 on vLLM crash during load. No auto-restart.
- Idle eviction: background task wakes every `min(idle_unload_seconds/4, 30)` s and unloads if `_inflight == 0` and idle. `null` disables.
- Buffered usage writes (`last_used_at`, `request_count`) flushed to catalog every ~30s and on shutdown — never on the hot path.
- `/manager/status` extended additively with profile alias, GPU plan, quantization, idle countdown, in-flight count. Existing keys unchanged.
- `POST /manager/load` becomes a thin shim: if the `model` value matches an alias (config or `ui_install`), resolve via profile and ignore legacy `tp`/`gpu_mem`/`extra_args` (log a warning); otherwise behave exactly as Phase 0/1.
- Tests for command/env builders, queue ordering, eviction races, in-flight counting, status shape.

Out (deferred):

- Plane separation, HTTP Basic, fail-safe bind — Phase 3. Inner-vLLM port collision with `admin_port=8001` ([vllm_manager.py:34](../../vllm_manager.py#L34)) is also Phase 3's to fix.
- Install / cancel / retry, download subprocess model — Phase 4.
- Removing the `MODEL_ALIASES` in-memory dict — kept as the lowest-priority fallback in Phase 2 (Phase 0 smoke test pins `/manager/aliases` shape). Phase 3 or 4 retires it once auth and catalog mutation routes exist.
- Multi-model concurrent serving, prewarm, Prometheus metrics — Phase 8 stretch.
- Auto-restart on vLLM crash. PRD §5.3 fails open in v1.

## 3. Files added or changed

| Path | Change | Notes |
|---|---|---|
| `runtime.py` | **new** | Fully pure helpers + a `RuntimeState` dataclass. No asyncio, no FastAPI, no subprocess, no I/O. GPU probing stays in `config.py`; the caller passes `visible_gpus` and `default_tp` in. Imports `profiles`, `config` (for type hints only). |
| `vllm_manager.py` | edit | Rewire `_start_vllm`, `_maybe_swap`, `_proxy`. New eviction + flush background tasks in `lifespan`. Extend `/manager/status`. Shim `/manager/load`. |
| `catalog.py` | edit | Add `bump_usage(alias, last_used_at, request_delta)` — single `UPDATE` per flush. |
| `tests/conftest.py` | edit | `_start_vllm` monkeypatch helper that flips state synchronously without launching a real subprocess. Reset Phase 2 globals + cancel background tasks between tests. |
| `tests/test_runtime.py` | **new** | Pure-function tests for `build_vllm_argv`, `build_vllm_env`, GPU-count derivation. |
| `tests/test_swap_queue.py` | **new** | Concurrency tests against the monkeypatched `_start_vllm`: piggyback, cross-target serialization, 504 on timeout, 503 on load failure. |
| `tests/test_eviction.py` | **new** | Idle eviction with in-flight requests, `idle_unload_seconds=null` disables, eviction never fires mid-stream. |
| `tests/test_proxy.py` | **new** | `/v1/*` alias resolution (config → ui_install → raw fallback), `/manager/load` shim with alias vs raw, `/manager/status` extended shape pin. |
| `vllm-ctl` | minor edit | `status` formatter prints the new fields when present. No new commands; `load <alias>` already works because the shim accepts any string. |

No Dockerfile, compose, or external mount changes in Phase 2. The Phase 1 mounts (`/config`, `/state`, storage drives) are sufficient.

## 4. `runtime.py` design

Fully pure module — no subprocess, no asyncio, no filesystem, no `nvidia-smi`. Two responsibilities: turn a `ResolvedProfile` into the data needed to launch vLLM, and hold the in-memory runtime state struct. All ambient state (visible GPUs, fallback TP, base env) is passed in by the caller.

```python
@dataclass
class RuntimeState:
    resident_alias: Optional[str] = None         # alias of currently-loaded profile
    resident_profile: Optional[ResolvedProfile] = None
    resident_tp_size: Optional[int] = None       # cached, since argv built it
    model_load_time: Optional[float] = None
    last_used_at: Optional[float] = None         # in-memory hot path; flushed to DB
    request_count_delta: int = 0                 # buffered count, zeroed on flush
    inflight: int = 0                            # /v1/* requests currently in proxy

def derive_tp_size(profile: ResolvedProfile, *,
                   visible_gpus: Optional[list[int]],
                   default_tp: int) -> int:
    """tp = len(profile.gpus) when explicit list.
       When 'all': len(visible_gpus) if non-empty, else default_tp (caller logs WARN).
    Pure — caller probes nvidia-smi separately."""

def build_vllm_argv(profile: ResolvedProfile, *,
                    host: str, port: int, tp_size: int) -> list[str]:
    """Translate a ResolvedProfile into the vLLM CLI invocation. tp_size is
    precomputed by the caller via derive_tp_size — keeping argv-building pure.

    Always: --model, --host, --port, --tensor-parallel-size, --gpu-memory-utilization,
            --disable-log-requests.
    Conditional:
      - --trust-remote-code  when profile.trust_remote_code is True (PRD §5.1 default
        is True; ResolvedProfile carries the resolved value so explicit False wins).
      - --quantization       when profile.quantization is set.
      - --max-model-len      when profile.max_model_len is set.
    Appended verbatim: profile.extra_args (last, so user can override our flags
    by re-stating them — current Phase 0 behavior).
    """

def build_vllm_env(profile: ResolvedProfile, *,
                   base_env: Mapping[str, str]) -> dict[str, str]:
    """Subprocess env. Starts from a copy of base_env, then:
       - When gpus is an explicit list: CUDA_VISIBLE_DEVICES = '0,1,...'.
       - When gpus == 'all': **explicitly POP** any inherited CUDA_VISIBLE_DEVICES
         from the copy. The container or the manager process may already carry
         CVD; without the explicit removal, vLLM under 'all' would silently see
         a narrowed set and tp-size would mismatch.
       - HF_HOME = profile.storage_path (per-launch, since storage is per-model).
    Returns a fresh dict — never mutates base_env.
    """
```

The caller (in `vllm_manager.py`) does the `nvidia-smi` probe via `config.gpu_indices_or_none()` (the rename is still in scope — drop the underscore on the existing helper at [config.py:160](../../config.py#L160)) and passes the result into `derive_tp_size`. Probe failures stay in `config.py` where they already are.

Why fully pure: every test in §8.1 calls these directly with synthesized inputs — no monkeypatching subprocess, no temp dirs, no env mutation.

## 5. `vllm_manager.py` integration

### 5.1 New module-level state

```python
_runtime: RuntimeState              # always present after lifespan boot
_swap_lock = asyncio.Lock()         # serializes a transition to a new target
_loading_target: Optional[str] = None
_load_event: Optional[asyncio.Event] = None
_load_error: Optional[BaseException] = None
_eviction_task: Optional[asyncio.Task] = None
_flush_task: Optional[asyncio.Task] = None
```

`loading_lock` and `_loading` from Phase 0/1 are removed — their roles fold into `_swap_lock` + `_loading_target`. The `current_model`, `model_load_time`, `_current_tp`, `_current_gpu_mem` globals also retire; their values now live in `_runtime` and are surfaced in `/manager/status`.

### 5.2 `_start_vllm(profile)` rewrite

```python
async def _start_vllm(profile: ResolvedProfile) -> None:
    global _runtime
    _kill_vllm()                                # idempotent; flushes + resets state
    visible = config.gpu_indices_or_none()      # may be None on dev hosts
    tp_size = derive_tp_size(profile, visible_gpus=visible, default_tp=DEFAULT_TP)
    if profile.gpus == "all" and not visible:
        logger.warning(
            "gpus='all' but nvidia-smi probe returned no GPUs; falling back to "
            "DEFAULT_TP=%d. Production CUDA hosts should never hit this path.",
            DEFAULT_TP,
        )
    argv = build_vllm_argv(profile, host=VLLM_INNER_HOST, port=VLLM_INNER_PORT, tp_size=tp_size)
    env  = build_vllm_env(profile, base_env=os.environ)
    logger.info("Launching vLLM (alias=%s tp=%d): %s", profile.alias, tp_size, " ".join(argv))
    vllm_process = subprocess.Popen(argv, env=env, stdout=sys.stdout, stderr=sys.stderr)
    try:
        if not await _wait_for_vllm():
            raise RuntimeError(f"vLLM failed to become ready for alias '{profile.alias}'")
    except (Exception, asyncio.CancelledError):
        # Includes wait_for-induced CancelledError when ensure_loaded times out.
        # Always clean up the half-launched subprocess.
        _kill_vllm()
        raise
    _runtime.resident_alias = profile.alias
    _runtime.resident_profile = profile
    _runtime.resident_tp_size = tp_size
    _runtime.model_load_time = time.time()
    _runtime.last_used_at = _runtime.model_load_time
```

`_kill_vllm` calls `_flush_usage()` (sync — see §5.7) before zeroing the runtime fields, so the just-evicted alias's last `request_count_delta` makes it to disk. The cleanup branch above guarantees a deadline-induced cancellation never leaves a zombie vLLM subprocess.

### 5.3 Swap queue — `ensure_loaded(target_alias, deadline)`

The PRD's "queue, don't 409" requirement (§5.3) is satisfied by piggyback + lock. PRD's "arrival order" is interpreted as ordering of *swap cycles*, not strict global FIFO across targets — see §11 risk note.

The deadline gates **every** await in the path: lock acquisition, event wait, and the `_start_vllm` call itself. Without the third, a first-ever load could run past `swap_queue_timeout_seconds` undetected.

```python
async def _run_until(coro, deadline: float):
    """Wrap any awaitable in a deadline-relative timeout. Raises asyncio.TimeoutError
    if the deadline is already past or the coro doesn't finish in time."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError()
    return await asyncio.wait_for(coro, timeout=remaining)

async def ensure_loaded(profile: ResolvedProfile, deadline: float) -> None:
    """Block until vLLM is serving `profile.alias`. Raises HTTPException(504) on
    deadline expiry, HTTPException(503) on vLLM load failure."""
    target = profile.alias
    while True:
        if _runtime.resident_alias == target and _loading_target is None:
            return                                              # warm path
        if _loading_target == target:                           # piggyback
            try:
                await _run_until(_load_event.wait(), deadline)
            except asyncio.TimeoutError:
                raise HTTPException(504, f"swap queue timeout waiting for '{target}'")
            if _load_error is not None:
                raise HTTPException(503, f"vLLM load failed: {_load_error}")
            continue                                            # re-check resident
        try:
            await _run_until(_swap_lock.acquire(), deadline)
        except asyncio.TimeoutError:
            raise HTTPException(504, f"swap queue timeout acquiring lock for '{target}'")
        try:
            if _runtime.resident_alias == target:               # raced
                return
            _loading_target = target
            _load_event = asyncio.Event()
            _load_error = None
            try:
                await _run_until(_start_vllm(profile), deadline)
            except asyncio.TimeoutError:
                # _start_vllm got cancelled by wait_for; it cleans up the
                # subprocess in its own finally (§5.2). Surface as 504.
                raise HTTPException(504, f"vLLM load did not complete in time for '{target}'")
            except asyncio.CancelledError:
                # Caller cancelled (e.g. client hung up). Don't stash as
                # _load_error — piggybackers will simply re-check resident,
                # see no model loaded, and retry on their own deadline.
                raise
            except Exception as e:
                _load_error = e
                raise HTTPException(503, f"vLLM load failed: {e}")
            finally:
                _loading_target = None
                _load_event.set()
        finally:
            _swap_lock.release()
        # loop falls through to warm path
```

Critical invariants:
- Deadline is computed at request arrival (`time.monotonic() + cfg.server.swap_queue_timeout_seconds`) and gates lock-wait, event-wait, **and** `_start_vllm` itself via `_run_until`.
- `_load_event` and `_load_error` are assigned **before** `_run_until(_start_vllm(...))` so a piggybacker that wakes on `set()` always sees a populated `_load_error` for failure cases.
- `except Exception` (not `except BaseException`) — `asyncio.CancelledError` propagates cleanly so the caller's cancellation isn't translated into a 503. The `finally` still publishes `_load_event.set()` so piggybackers don't deadlock.
- `_run_until` cancels its wrapped coroutine on timeout; `_start_vllm` must clean up any half-launched subprocess in its own `try/finally` (added to §5.2 below).

### 5.4 `_proxy` rewrite — alias resolution + inflight accounting

The inflight counter must be incremented **under `_swap_lock`**, in the same critical section that confirms the resident alias matches the request's target. Otherwise the eviction loop (§5.6, also under `_swap_lock`) could read `inflight==0` and `_kill_vllm` between `ensure_loaded` returning and the proxy's bump landing — a TOCTOU window. The locked section is intentionally tiny (a single integer increment) so it doesn't cost anything noticeable in steady state.

Structure: a single `while True:` loop with **one** deadline (computed once, at request arrival) and **one** lock release point (the `try/finally`). No manual `release()` calls, no recursion, no `.locked()` checks (which would race anyway since `.locked()` returns true regardless of holder).

```python
async def _proxy(request: Request, path: str, body: bytes):
    requested = _peek_model_field(body)
    if requested is None and _runtime.resident_alias is None:
        raise HTTPException(503, "No model loaded and no 'model' field in request.")

    # Resolve once. May KeyError → 404 (raised by route handler).
    profile = _resolve_request_model(requested) if requested is not None else None

    # One deadline for the whole request — preserved across loop iterations.
    deadline = time.monotonic() + _config.server.swap_queue_timeout_seconds

    while True:
        if profile is not None:
            await ensure_loaded(profile, deadline)              # uses same deadline

        # Bump inflight under _swap_lock with a re-check. If eviction or another
        # swap raced us between ensure_loaded returning and the lock acquire,
        # loop and try again — _run_until will eventually 504 if we starve.
        try:
            await _run_until(_swap_lock.acquire(), deadline)
        except asyncio.TimeoutError:
            raise HTTPException(504, "swap queue timeout acquiring inflight lock")
        try:
            if profile is not None and _runtime.resident_alias != profile.alias:
                continue                                        # finally releases; loop reruns
            if profile is None and _runtime.resident_alias is None:
                raise HTTPException(503, "Model evicted before request started")
            _runtime.inflight += 1
            break                                               # exit loop (finally releases)
        finally:
            _swap_lock.release()

    try:
        return await _forward_to_vllm(request, path, body)      # streaming-safe
    finally:
        # Decrement does NOT need the lock. Eviction reads inflight under
        # _swap_lock; a decrement-without-lock can only ever make eviction
        # fire sooner than it otherwise would, never spuriously.
        _runtime.inflight -= 1
        _runtime.last_used_at = time.time()
        _runtime.request_count_delta += 1
```

Why this shape:
- **Exactly one `release()` call**, in the `finally`. No risk of releasing a lock that another task acquired.
- **One deadline**, computed once. The `continue` path doesn't recompute it, so a pathological eviction-storm starves the request into a 504 instead of looping forever.
- **No recursion**, so no concern about stack growth or about the recursive call recomputing state from a fresh body parse.
- The loop's `continue` releases the lock via `finally`, then re-runs `ensure_loaded` (which has its own deadline-aware acquisition machinery from §5.3). The lock is held only for the integer-bump critical section.

Streaming bodies need the decrement to fire at end-of-stream, not end-of-handler. Wrap the `StreamingResponse`'s underlying generator in a small `async def` that does `try/finally` around the existing `aiter_bytes()` loop, decrementing inside `finally`. `ClientDisconnect` from Starlette also routes through `finally` — no extra branch needed.

### 5.5 `_resolve_request_model` — four-tier lookup with explicit raw-id gate

Tier 4 is gated by a **raw-id heuristic** so a typo'd alias (`"qwn-72b-awq"`) does NOT silently turn into a HuggingFace download attempt. A string is treated as a raw HF model ID only if it matches one of:

- `org/repo` form: exactly one `/`, both halves match `[A-Za-z0-9._-]+`. Captures every legitimate HF id (`Qwen/Qwen2.5-7B-Instruct`, `meta-llama/Llama-3.2-11B-Vision-Instruct`, `TheBloke/...`).
- Absolute path: starts with `/` and exists on disk. (Phase 0 occasionally let users point at a local checkout; preserve that.)

Anything else → `KeyError`, which `/v1/*` translates to `404 Unknown alias '<x>'` (different from "no model loaded" → 503).

```python
_RAW_HF_ID_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

def _resolve_request_model(requested: str) -> ResolvedProfile:
    # 1. Config alias (highest priority — PRD §5.1)
    if (p := next((m for m in _config.models if m.alias == requested), None)):
        return resolve_profile(requested, _config, _catalog)
    # 2. Catalog ui_install row
    row = _catalog.get_model(requested)
    if row is not None and row.source == "ui_install":
        return resolve_profile(requested, _config, _catalog)
    # 3. Legacy MODEL_ALIASES (deprecated; logged WARN once per alias)
    if requested in MODEL_ALIASES:
        _warn_legacy_alias_once(requested)
        return _synthesize_profile(MODEL_ALIASES[requested])
    # 4. Raw HF id or local path — only if the heuristic matches
    if _RAW_HF_ID_RE.match(requested) or (requested.startswith("/") and os.path.isdir(requested)):
        logger.info("Resolving '%s' as raw model id (no alias match)", requested)
        return _synthesize_profile(requested)
    raise KeyError(requested)
```

`_synthesize_profile(model_id)` returns an inline `ResolvedProfile` with `defaults.gpu_memory_utilization`, `defaults.max_model_len`, `defaults.trust_remote_code`, `gpus="all"`, no quantization, `storage.default` (resolved to its path), no `extra_args`. Used by tier 3 and tier 4 only.

Behavioral matrix:

| `"model"` value | Resolution | Status code on /v1/* (no resident model) |
|---|---|---|
| Config-defined alias | tier 1 | 200 (after load) |
| `ui_install` alias | tier 2 | 200 (after load) |
| Legacy `MODEL_ALIASES` key | tier 3 + WARN | 200 (after load) |
| `org/repo` form | tier 4 + INFO | 200 (after load) |
| Absolute existing path | tier 4 + INFO | 200 (after load) |
| Anything else (e.g. typo'd alias) | `KeyError` | **404** |
| Field absent, no resident model | n/a | 503 (existing) |

The Phase 0 smoke test that exercises raw model IDs uses `org/repo` form, so it stays green; the new `test_proxy.py` "unknown" case asserts 404, not 503 (the previous draft had this miswired).

### 5.6 Idle eviction loop

```python
async def _eviction_loop():
    if _config.server.idle_unload_seconds is None:
        return                                                  # disabled
    period = max(5, min(_config.server.idle_unload_seconds // 4, 30))
    while True:
        await asyncio.sleep(period)
        async with _swap_lock:
            if _runtime.resident_alias is None: continue
            if _runtime.inflight > 0: continue
            if _runtime.last_used_at is None: continue
            idle = time.time() - _runtime.last_used_at
            if idle > _config.server.idle_unload_seconds:
                logger.info("Idle eviction: '%s' idle %ds", _runtime.resident_alias, int(idle))
                _kill_vllm()
```

`_kill_vllm` flushes buffered usage and resets runtime fields. The lock acquisition pairs with §5.4's locked inflight bump: eviction reads `inflight` under `_swap_lock`, new requests bump `inflight` under `_swap_lock`, and the resident-alias re-check before the bump catches the rare case where eviction landed first.

Decrementing `inflight` doesn't need the lock. Eviction reads it under the lock; a decrement-without-lock can only ever make eviction fire sooner than it otherwise would, never spuriously.

### 5.7 Usage buffer flush

`_flush_usage` is **synchronous** — SQLite calls aren't awaitable and the write is fast (single UPDATE under WAL). The async loop just schedules it on the period.

```python
def _flush_usage() -> None:
    """Sync. Single UPDATE. Safe to call from any context — no-op if nothing
    to flush. Called from the periodic loop, _kill_vllm, and lifespan exit."""
    alias = _runtime.resident_alias
    if alias is None or _runtime.request_count_delta == 0:
        return
    _catalog.bump_usage(alias, _runtime.last_used_at, _runtime.request_count_delta)
    _runtime.request_count_delta = 0

async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(30)
        _flush_usage()                                  # sync; no await
```

`Catalog.bump_usage` is the single `UPDATE` (§6).

### 5.8 `/manager/status` extension (additive only)

Existing keys (`loaded_model`, `loading`, `vllm_pid`, `loaded_at`, `loaded_at_human`, `tp_size`, `gpu_mem_util`, `inner_endpoint`) stay. `loaded_model` continues to return the HF model id (now via `_runtime.resident_profile.model`). `loading` becomes `_loading_target is not None`. `tp_size` and `gpu_mem_util` come from the resident profile.

Added keys:

```json
{
  "alias": "qwen-72b-awq",
  "gpus": [0, 1],
  "quantization": "awq",
  "max_model_len": 32768,
  "storage_location": "nvme-fast",
  "last_used_at": 1714281120.5,
  "idle_seconds": 42.1,
  "seconds_until_eviction": 858,
  "inflight_requests": 0,
  "swap_target": null
}
```

`seconds_until_eviction` is `null` when `idle_unload_seconds` is `null`. `swap_target` is the alias currently being loaded (or `null` if none).

### 5.9 `POST /manager/load` shim

```python
@app.post("/manager/load")
async def load_model(request: Request):
    body = await request.json()
    requested = body.get("model")
    if not requested: raise HTTPException(400, "'model' field required")
    try:
        profile = _resolve_request_model(requested)             # tier 1-4 (§5.5)
    except KeyError:
        raise HTTPException(404, f"Unknown alias '{requested}'")

    # Aliased resolution (tiers 1-3): the profile is authoritative; legacy
    # tp/gpu_mem/extra_args from the payload are ignored with a one-time warning.
    # Raw-id resolution (tier 4): allow the legacy params to override defaults
    # in the synthesized profile so vllm-ctl load <raw-id> --gpu-mem 0.85 works.
    is_aliased = (requested in {m.alias for m in _config.models}
                  or (_catalog.get_model(requested) is not None
                      and _catalog.get_model(requested).source == "ui_install")
                  or requested in MODEL_ALIASES)
    legacy_params = {k: body[k] for k in ("tp", "gpu_mem", "extra_args") if k in body}
    if is_aliased and legacy_params:
        logger.warning(
            "Ignoring %s — profile '%s' wins (PRD §5.1)",
            sorted(legacy_params), profile.alias,
        )
    elif not is_aliased and legacy_params:
        profile = _apply_legacy_overrides(profile, legacy_params)   # raw-id only

    deadline = time.monotonic() + _config.server.swap_queue_timeout_seconds
    await ensure_loaded(profile, deadline)
    return {"status": "loaded", "alias": profile.alias, "model": profile.model}
```

`_apply_legacy_overrides` returns a new `ResolvedProfile` (frozen dataclass; use `dataclasses.replace`) with `gpu_memory_utilization=body['gpu_mem']`, `extra_args=tuple(body['extra_args'])`, and `gpus=list(range(body['tp']))` if `tp` is supplied (so legacy `--tp 1` keeps producing `--tensor-parallel-size 1`). Only used in the raw-id branch; tests pin all four code paths.

### 5.10 `lifespan` additions

`asyncio.TaskGroup` is the wrong primitive here: it waits for *all* tasks to finish before exiting, but `_eviction_loop` and `_flush_loop` are infinite. They have to be cancelled explicitly. Use plain `asyncio.create_task` + `try/finally`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _catalog, _runtime, _eviction_task, _flush_task
    load_env()
    _config = load_config()
    _catalog = open_catalog()
    _catalog.apply_config(_config.models, _config.storage.default,
                          {l.name: l.path for l in _config.storage.locations})
    _runtime = RuntimeState()
    _install_sighup_handler()

    if _config.server.idle_unload_seconds is not None:
        _eviction_task = asyncio.create_task(_eviction_loop(), name="eviction")
        logger.info("Idle eviction enabled (threshold=%ds)", _config.server.idle_unload_seconds)
    else:
        logger.info("Idle eviction disabled (idle_unload_seconds=null)")
    _flush_task = asyncio.create_task(_flush_loop(), name="usage-flush")

    try:
        yield
    finally:
        # Cancel infinite tasks first so they don't fight us during teardown.
        for t in (_eviction_task, _flush_task):
            if t is not None and not t.done():
                t.cancel()
        for t in (_eviction_task, _flush_task):
            if t is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        _flush_usage()                                  # final write before _kill_vllm wipes _runtime
        _kill_vllm()
        if _catalog is not None:
            _catalog.close()
            _catalog = None
        _config = None
```

`_kill_vllm` calls `_flush_usage` itself, but the explicit call before it is intentional belt-and-suspenders: if `_kill_vllm` ever changes shape (Phase 3 splits the listeners and may move teardown around), the lifespan code still gets the last write in.

## 6. `catalog.py` additions

Single new method:

```python
def bump_usage(self, alias: str, last_used_at: Optional[float], delta: int) -> None:
    """Buffered usage flush. UPDATE models SET last_used_at=?, request_count =
    request_count + delta WHERE alias=?. last_used_at written as INTEGER seconds."""
    if last_used_at is None or delta <= 0: return
    with self._conn:
        self._conn.execute(
            "UPDATE models SET last_used_at=?, request_count = request_count + ? WHERE alias=?",
            (int(last_used_at), delta, alias),
        )
```

No-op when alias is missing (e.g. raw-id passthrough) — `UPDATE` matches zero rows silently.

## 7. `vllm-ctl` changes (minor)

`cmd_status` already python-parses the response — extend it to print the new fields when present, falling back gracefully when absent (so a CLI built against Phase 2 still works against a Phase 1 manager and vice versa). No new subcommands. `vllm-ctl load <alias>` already works because the shim accepts any string.

## 8. Test plan

The hard part is testing concurrency without launching real vLLM. Strategy: a test fixture replaces `_start_vllm` with an async stub that synchronously sets `_runtime.resident_alias`/`resident_profile` after a configurable delay, optionally raising to simulate load failure. `_kill_vllm` is replaced with a stub that just clears state. Subprocess and `_wait_for_vllm` are not invoked.

### 8.1 `test_runtime.py` (pure)

- `build_vllm_argv` round-trips a fully-populated profile (tp_size passed in) — every always-on flag in §4 is present.
- `--quantization` omitted when profile.quantization is `None`.
- `--max-model-len` omitted when profile.max_model_len is `None`.
- `--trust-remote-code` present when `profile.trust_remote_code is True`; **absent** when explicitly `False`.
- `extra_args` appended verbatim, after our flags (so user-provided flags can re-state and override our defaults).
- `build_vllm_env` for `gpus='all'` — `CUDA_VISIBLE_DEVICES` not present in result; `HF_HOME` set to `storage_path`.
- `build_vllm_env` for `gpus='all'` with `base_env={'CUDA_VISIBLE_DEVICES': '0'}` — **CVD explicitly removed** in result. (Inherited-env trap from review.)
- `build_vllm_env` for `gpus=[1]` — `CUDA_VISIBLE_DEVICES='1'`.
- `build_vllm_env` for `gpus=[0,1]` — `CUDA_VISIBLE_DEVICES='0,1'`.
- `build_vllm_env` does not mutate `base_env` (assert by checking the input dict afterwards).
- `derive_tp_size` for explicit list `[0,1]` — `2`, regardless of `default_tp`.
- `derive_tp_size` for `'all'` with `visible_gpus=[0,1]`, `default_tp=4` — `2` (probe wins).
- `derive_tp_size` for `'all'` with `visible_gpus=None`, `default_tp=4` — `4` (fallback).
- `derive_tp_size` for `'all'` with `visible_gpus=[]`, `default_tp=4` — `4` (empty probe == no probe).

### 8.2 `test_swap_queue.py` (asyncio)

All tests use the monkeypatched `_start_vllm` with controlled delays.

- Single request for unloaded alias → triggers load, returns success.
- Two concurrent requests for the same not-yet-loaded alias → exactly one `_start_vllm` call (piggyback).
- Request for alias B arriving while A is loading → completes after A finishes loading, then B loads. (Two `_start_vllm` calls in arrival order.)
- 10 requests for currently-resident alias → zero `_start_vllm` calls.
- Request times out waiting for the lock (deadline < lock-wait + load) → `HTTPException(504)`.
- Request times out during the load itself (`_start_vllm` slower than deadline) → `HTTPException(504)` AND `_kill_vllm` is called (no zombie subprocess). Verify by asserting on the monkeypatched `_kill_vllm` call count.
- `_start_vllm` raises during load → all piggybackers get `HTTPException(503)` with the error message.
- After a failed load, a fresh request retriggers `_start_vllm` (no sticky failure state).
- Cancelled waiter (caller task cancelled mid-wait): `asyncio.CancelledError` propagates cleanly, is NOT translated into 503, and doesn't leave `_load_error` set for piggybackers.
- Cancelled loader (task running `_start_vllm` is cancelled): `_load_event` still gets set in `finally`, piggybackers wake, see `_load_error is None`, re-check resident, find no model loaded, and retry on their own deadline.

### 8.3 `test_eviction.py` (asyncio)

- `idle_unload_seconds=null` → `_eviction_loop` returns immediately; `_eviction_task` is a no-op coroutine.
- Resident model + zero inflight + idle past threshold → `_kill_vllm` called.
- Resident model + nonzero inflight + idle past threshold → no eviction.
- Resident model + zero inflight + below threshold → no eviction.
- New request arriving during eviction's lock-hold waits, then loads its profile (no race window).
- Streaming /v1/* request: simulate a slow stream; eviction task ticks during the stream and does not fire (inflight bumped).
- After eviction, `last_used_at` is flushed to catalog (`bump_usage` was called).

### 8.4 `test_proxy.py` (TestClient)

- `/v1/chat/completions` with `"model": "<config-alias>"` → resolves via config tier 1, proxies (upstream mocked at the `httpx.AsyncClient` boundary).
- `"model": "<ui_install-alias>"` → resolves via catalog tier 2.
- `"model": "<legacy-MODEL_ALIASES-key>"` → tier 3 + WARN.
- `"model": "Qwen/Qwen2.5-7B-Instruct"` (org/repo form) → tier 4 + INFO.
- `"model": "/some/abs/path"` where the path exists → tier 4 + INFO.
- `"model": "qwn-72b-awq"` (typo'd alias, no slash, no path) → **404** (`KeyError` → handler maps to 404, NOT tier 4 passthrough). This is the exact case the original draft got wrong.
- `"model": "Qwen/NonexistentModel"` (org/repo form, would 404 from HF) → tier 4 fires, then `_start_vllm` failure surfaces as 503. Fine: that's the right boundary — alias gating is the manager's job, repo existence is HF's.
- No `"model"` field, no resident model → 503 with the explanatory message (existing Phase 0 behavior).
- `POST /manager/load {"model": "<config-alias>"}` → succeeds, returns `{alias, model}` shape.
- `POST /manager/load {"model": "<config-alias>", "tp": 1}` → succeeds, warning logged that `tp` was ignored, profile values used.
- `POST /manager/load {"model": "Qwen/Qwen2.5-7B-Instruct", "tp": 1, "gpu_mem": 0.85}` → tier-4 + `_apply_legacy_overrides`, vLLM launched with `--tensor-parallel-size 1` and `--gpu-memory-utilization 0.85`.
- `POST /manager/load {"model": "qwn-72b-awq"}` (typo) → 404.
- `GET /manager/status` shape pin: every Phase 0/1 key still present; every Phase 2 key present; `seconds_until_eviction` is `null` when `idle_unload_seconds` is `null` in fixture config.

### 8.5 Existing tests

- `tests/test_smoke.py`: Phase 0 pinned keys must still appear in `/manager/status`. The shape pin is "key set is a superset" not "exact equality" — verify the existing assertion semantics; loosen to subset if needed.
- `tests/test_reload.py`, `test_config.py`, `test_catalog.py`: unchanged.

### 8.6 conftest changes

- Add `monkeypatched_vllm` fixture that swaps `vllm_manager._start_vllm` and `_kill_vllm` for stubs operating on `_runtime` directly.
- Reset Phase 2 globals in `client`: `_loading_target`, `_load_event`, `_load_error`, `_runtime`, cancel `_eviction_task` and `_flush_task` if running.
- Tests for `/v1/*` use a separate fixture (`client_with_proxy_mock`) that also patches `httpx.AsyncClient` to return canned responses.

## 9. Operational notes

- **No compose changes required** in Phase 2.
- The `_flush_loop` writes to SQLite every ~30s; with WAL mode (already enabled in Phase 1) this is harmless even under concurrent reads.
- `idle_unload_seconds=null` is the documented disable knob; the eviction task exits cleanly so there's no idle background coroutine when disabled.
- Logs on first launch will show: `"Idle eviction enabled (period=30s, threshold=900s)"` or `"Idle eviction disabled (idle_unload_seconds=null)"`.
- The `MODEL_ALIASES` dict still works but logs a one-time `WARN` on first hit per alias: `"Legacy MODEL_ALIASES used; prefer config.yaml or /manager/install (Phase 4)"`.

## 10. Exit criteria

- `POST /v1/chat/completions` with `"model": "<config-alias>"` lazy-loads the right vLLM subprocess (correct argv, correct `CUDA_VISIBLE_DEVICES`, correct `HF_HOME`) and serves the request.
- Two concurrent `/v1/*` requests against an unloaded alias cause exactly one load and both return successfully.
- A `/v1/*` request for alias B that arrives while alias A is loading waits, then triggers a fresh load of B without 409 — both requests return successfully or 504 on timeout.
- A `/v1/*` request returns `504` after `swap_queue_timeout_seconds` if no swap completes in time.
- A `/v1/*` request returns `503` with the underlying error if `_start_vllm` raises during the load.
- With `idle_unload_seconds=900`, an unused resident model is killed within ~975s (`900 + period`).
- With `idle_unload_seconds=null`, no eviction task runs and the model stays resident indefinitely.
- A streaming `/v1/chat/completions` response that takes longer than `idle_unload_seconds` does NOT trigger eviction mid-stream.
- `POST /manager/load {"model": "<alias>"}` resolves the profile and loads exactly as the `/v1/*` path would.
- `POST /manager/load {"model": "<raw-hf-id>", "tp": 1, "gpu_mem": 0.85}` still works (Phase 0 compatibility).
- `GET /manager/status` returns every Phase 0/1 key plus the new `alias`, `gpus`, `quantization`, `max_model_len`, `storage_location`, `last_used_at`, `idle_seconds`, `seconds_until_eviction`, `inflight_requests`, `swap_target` fields.
- `request_count` and `last_used_at` columns in the `models` table are updated within ~30s of activity (verify with `sqlite3` after running a `/v1/*` request in dev).
- All Phase 0/1 tests continue to pass.

## 11. Open questions / risks

- **"Arrival order" semantics.** PRD §5.3 says "the eviction-loader-serve cycle proceeds in arrival order." The piggyback model guarantees this for *swap cycles* (one cycle per distinct target reached), not for individual requests across targets — a request for B that arrives slightly after a request for C may end up served before C if B's load happens to start first. Acceptable for v1 (LMStudio behaves the same), but flag for §8 acceptance: if the user observes pathological starvation, Phase 8 can promote the lock to a strict-FIFO `Queue`. No code structure precludes this future change.
- **`_swap_lock` on every warm-path proxy.** Adds one event-loop hop per `/v1/*` request to bump `inflight`. Steady-state cost is microseconds; flagged because it's an architectural choice (vs lock-free atomic increment) traded for TOCTOU correctness.
- **Buffered flush loss on hard kill.** A `SIGKILL` of the manager loses up to ~30s of `request_count_delta`. Acceptable — `last_used_at` will rebound on next request and `request_count` is observability, not correctness. Documented; not addressed in v1.
- **`gpus='all'` vs `_gpu_indices_or_none()` returning None on macOS dev hosts.** `derive_tp_size` falls back to `DEFAULT_TP` and logs a warning. Tests run on macOS without `nvidia-smi`; production CUDA hosts always succeed the probe. Phase 8 to verify.
- **Retiring `MODEL_ALIASES`.** Kept in Phase 2 because the Phase 0 smoke test pins `/manager/aliases`. Phase 3 (when admin auth gates mutating routes) is the natural moment to drop it.
- **Inner vLLM port collision (`8001`) with `admin_port=8001`.** Not a Phase 2 problem — Phase 1 plan §4.1 already calls this out as Phase 3 work. Phase 2 does not bind the admin port.

## 12. Files to read before implementation

- [vllm_manager.py](../../vllm_manager.py) — current `_start_vllm` (line 113), `_maybe_swap` (617), `_proxy` (655), `lifespan` (204).
- [profiles.py](../../profiles.py) — `ResolvedProfile` and `resolve_profile`.
- [catalog.py](../../catalog.py) — for `bump_usage` placement (after `list_downloads`, before `schema_version`).
- [config.py](../../config.py) — `Server.idle_unload_seconds`, `swap_queue_timeout_seconds`, `_gpu_indices_or_none` (rename to public `gpu_indices_or_none`).
- [tests/conftest.py](../../tests/conftest.py) — fixture pattern for `client`.
- [phase_1_plan.md](../phase_1_plan.md) — style template.

## 13. Revision log

- **v3 (2026-04-28)** — addressed second-pass review:
  - Added `_run_until(coro, deadline)` helper and used it to wrap `_start_vllm` itself, so a first-ever load can't run past `swap_queue_timeout_seconds` undetected (§5.3).
  - `_start_vllm` gained an explicit `try/except (Exception, CancelledError): _kill_vllm(); raise` so a deadline-induced cancellation never leaves a zombie subprocess (§5.2).
  - `ensure_loaded` switched from `except BaseException` to `except Exception` — `asyncio.CancelledError` now propagates cleanly instead of being translated to 503, and `_load_error` stays `None` on cancellation so piggybackers don't see a phantom failure (§5.3).
  - `_proxy` rewritten as a single-deadline `while True:` loop with exactly one `release()` in `finally` and no recursion. Removes the manual-release + recursive-call shape that could release another task's lock and recompute the deadline (§5.4).
  - Test plan extended to pin the new cancellation and deadline-during-load invariants (§8.2).
- **v2 (2026-04-28)** — addressed first-pass review:
  - Tier-4 raw-id passthrough now gated by `org/repo` regex or absolute existing path; typo'd aliases return **404**, not silent HF lookup. Test plan and behavioral matrix in §5.5 updated.
  - `build_vllm_env` for `gpus='all'` explicitly POPs inherited `CUDA_VISIBLE_DEVICES` (§4 + new test in §8.1).
  - `_proxy` inflight bump now happens **under `_swap_lock`** with a resident-alias re-check (§5.4); the eviction TOCTOU window is closed.
  - `--trust-remote-code` now respects `profile.trust_remote_code` (§4 builder spec + §8.1 test); previously hardcoded on.
  - `build_vllm_argv` takes `tp_size: int` from the caller; no hidden GPU-probe dependency in the pure module (§4).
  - `lifespan` shutdown switched from `asyncio.TaskGroup` to plain `create_task` + explicit cancel (§5.10) — TaskGroup waits for infinite tasks to finish, which would deadlock teardown.
  - `_flush_usage` clarified as **sync**; `_flush_loop` calls it without `await` (§5.7).
  - `runtime.py` is now fully pure (no `nvidia-smi` shell-out); the probe stays in `config.py` and is passed in.
  - Doc links rewritten relative to `project_docs/plans/phase_2.md`.

## 14. Verification

After implementation, in this order:

1. `python -m pytest -v` on macOS → all old + new tests pass (no CUDA needed).
2. Workstation: rebuild container, start. Confirm `lifespan` boots cleanly with eviction enabled (log line present).
3. `vllm-ctl load <alias>` for a configured alias → profile resolves, vLLM launches, `/manager/status` shows alias + GPU plan + quantization.
4. `curl /v1/chat/completions -d '{"model":"<other-alias>", ...}'` → swap completes, response served.
5. Two concurrent `curl /v1/chat/completions` against an unloaded alias (same alias) → both succeed; manager logs show one load.
6. Idle smoke: load a model, sleep `idle_unload_seconds + 90`, verify `_kill_vllm` log line and `/manager/status` shows no resident model.
7. `sqlite3 /state/mnemosyne.db 'SELECT alias,last_used_at,request_count FROM models'` → counts incremented for the loaded alias.
8. `vllm-ctl load <raw-hf-id> --gpu-mem 0.85` → backward-compat path works.
