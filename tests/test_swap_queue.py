"""Phase 2 — swap queue concurrency.

Verifies plans/phase_2.md §5.3, §8.2: piggyback on in-flight loads,
cross-target serialization, deadline gating (lock-wait + load), 503 on
load failure, 504 on timeout, cancellation hygiene.

Drives `ensure_loaded` directly with the StubLauncher so timing is
deterministic. No HTTP layer involved.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import vllm_manager
from profiles import resolve_profile
from runtime import RuntimeState


@pytest.fixture
def boot(rich_config, stub_vllm):
    """Initialize Phase 2 globals against the rich config without spinning
    up a TestClient (we don't need HTTP for these tests)."""
    from config import load_config
    from catalog import open_catalog

    # Reset module globals.
    vllm_manager.vllm_process = None
    vllm_manager._downloads = {}
    vllm_manager.MODEL_ALIASES = {}
    if vllm_manager._catalog is not None:
        vllm_manager._catalog.close()
    vllm_manager._config = load_config()
    vllm_manager._catalog = open_catalog()
    vllm_manager._catalog.apply_config(
        vllm_manager._config.models,
        vllm_manager._config.storage.default,
        {l.name: l.path for l in vllm_manager._config.storage.locations},
    )
    vllm_manager._runtime = RuntimeState()
    vllm_manager._loading_target = None
    vllm_manager._load_event = None
    vllm_manager._load_error = None
    vllm_manager._eviction_task = None
    vllm_manager._flush_task = None
    vllm_manager._legacy_alias_warned.clear()
    vllm_manager._swap_lock = asyncio.Lock()
    yield stub_vllm
    if vllm_manager._catalog is not None:
        vllm_manager._catalog.close()


def _profile(alias: str):
    return resolve_profile(alias, vllm_manager._config, vllm_manager._catalog)


def _deadline(seconds: float = 5.0) -> float:
    import time as _t
    return _t.monotonic() + seconds


# ── happy paths ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_request_loads_alias(boot):
    await vllm_manager.ensure_loaded(_profile("a-model"), _deadline())
    assert vllm_manager._runtime.resident_alias == "a-model"
    assert len(boot.calls) == 1


@pytest.mark.asyncio
async def test_warm_path_no_extra_load(boot):
    await vllm_manager.ensure_loaded(_profile("a-model"), _deadline())
    await vllm_manager.ensure_loaded(_profile("a-model"), _deadline())
    assert len(boot.calls) == 1


@pytest.mark.asyncio
async def test_ten_concurrent_warm_path_zero_loads(boot):
    await vllm_manager.ensure_loaded(_profile("a-model"), _deadline())
    boot.calls.clear()
    await asyncio.gather(*[
        vllm_manager.ensure_loaded(_profile("a-model"), _deadline()) for _ in range(10)
    ])
    assert boot.calls == []


# ── piggyback ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_same_target_only_one_load(boot):
    boot.delay = 0.05
    await asyncio.gather(*[
        vllm_manager.ensure_loaded(_profile("a-model"), _deadline()) for _ in range(5)
    ])
    assert len(boot.calls) == 1
    assert vllm_manager._runtime.resident_alias == "a-model"


# ── cross-target serialization ────────────────────────────────────────


@pytest.mark.asyncio
async def test_different_targets_serialize(boot):
    boot.delay = 0.05
    # First request kicks off A; second arrives a tick later for B.
    a_task = asyncio.create_task(
        vllm_manager.ensure_loaded(_profile("a-model"), _deadline())
    )
    await asyncio.sleep(0.005)  # let A grab the lock
    b_task = asyncio.create_task(
        vllm_manager.ensure_loaded(_profile("b-model"), _deadline())
    )
    await asyncio.gather(a_task, b_task)
    aliases = [p.alias for p in boot.calls]
    assert aliases == ["a-model", "b-model"]
    assert vllm_manager._runtime.resident_alias == "b-model"


# ── timeouts ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deadline_during_load_raises_504(boot):
    boot.delay = 0.5
    short = _deadline(0.05)
    with pytest.raises(HTTPException) as excinfo:
        await vllm_manager.ensure_loaded(_profile("a-model"), short)
    assert excinfo.value.status_code == 504
    # _start_vllm cancellation triggered the kill cleanup path.
    # StubLauncher.kill is called once for the initial _kill_vllm-before-launch
    # (mirroring real _start_vllm), and again on the cancellation cleanup.
    assert boot.kill_calls >= 2


@pytest.mark.asyncio
async def test_deadline_for_lock_wait_raises_504(boot):
    boot.delay = 0.5
    # First task holds the lock; second task gets a deadline shorter than
    # the first task's load duration.
    long_deadline = _deadline(2.0)
    short_deadline = _deadline(0.05)
    a_task = asyncio.create_task(
        vllm_manager.ensure_loaded(_profile("a-model"), long_deadline)
    )
    await asyncio.sleep(0.005)
    with pytest.raises(HTTPException) as excinfo:
        await vllm_manager.ensure_loaded(_profile("b-model"), short_deadline)
    assert excinfo.value.status_code == 504
    # Don't leak the long load.
    await a_task


@pytest.mark.asyncio
async def test_run_until_expired_deadline_does_not_create_coroutine(boot, recwarn):
    called = False

    async def should_not_be_created():
        nonlocal called
        called = True

    with pytest.raises(asyncio.TimeoutError):
        await vllm_manager._run_until(should_not_be_created, _deadline(-1))

    assert called is False
    assert not [w for w in recwarn if "was never awaited" in str(w.message)]


# ── load failure → 503 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_failure_raises_503(boot):
    boot.fail_with = RuntimeError("vLLM exploded")
    with pytest.raises(HTTPException) as excinfo:
        await vllm_manager.ensure_loaded(_profile("a-model"), _deadline())
    assert excinfo.value.status_code == 503
    assert "vLLM exploded" in excinfo.value.detail


@pytest.mark.asyncio
async def test_piggybackers_get_503_when_load_fails(boot):
    boot.delay = 0.05
    boot.fail_with = RuntimeError("upstream down")
    with pytest.raises(HTTPException):
        results = await asyncio.gather(
            vllm_manager.ensure_loaded(_profile("a-model"), _deadline()),
            vllm_manager.ensure_loaded(_profile("a-model"), _deadline()),
            return_exceptions=False,
        )
    # gather raises the first one; verify both individually instead.


@pytest.mark.asyncio
async def test_piggybackers_get_503_individually(boot):
    boot.delay = 0.05
    boot.fail_with = RuntimeError("upstream down")
    results = await asyncio.gather(
        vllm_manager.ensure_loaded(_profile("a-model"), _deadline()),
        vllm_manager.ensure_loaded(_profile("a-model"), _deadline()),
        vllm_manager.ensure_loaded(_profile("a-model"), _deadline()),
        return_exceptions=True,
    )
    assert all(isinstance(r, HTTPException) and r.status_code == 503 for r in results)


@pytest.mark.asyncio
async def test_fresh_request_after_failure_retries(boot):
    boot.fail_with = RuntimeError("transient")
    with pytest.raises(HTTPException):
        await vllm_manager.ensure_loaded(_profile("a-model"), _deadline())
    boot.fail_with = None
    boot.calls.clear()
    await vllm_manager.ensure_loaded(_profile("a-model"), _deadline())
    assert len(boot.calls) == 1
    assert vllm_manager._runtime.resident_alias == "a-model"


# ── cancellation hygiene ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_block_others(boot):
    """If a caller is cancelled mid-wait, subsequent requests must not
    deadlock. Plan §5.3: CancelledError propagates cleanly, _load_event
    is still set in finally."""
    boot.delay = 0.2

    async def cancelled_caller():
        await vllm_manager.ensure_loaded(_profile("a-model"), _deadline())

    task = asyncio.create_task(cancelled_caller())
    await asyncio.sleep(0.02)  # let it grab the lock and start loading
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # State: _start_vllm cancellation triggered cleanup; resident is None.
    assert vllm_manager._runtime.resident_alias is None
    # A fresh request must succeed without hanging.
    await asyncio.wait_for(
        vllm_manager.ensure_loaded(_profile("a-model"), _deadline()),
        timeout=2.0,
    )
    assert vllm_manager._runtime.resident_alias == "a-model"


@pytest.mark.asyncio
async def test_cancelled_loader_does_not_set_load_error(boot):
    """A cancelled loader leaves _load_error as None. Piggybackers wake on
    the event, see no error, re-check resident, find no model, and retry."""
    boot.delay = 0.2

    async def loader():
        await vllm_manager.ensure_loaded(_profile("a-model"), _deadline())

    task = asyncio.create_task(loader())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # _load_error must NOT be left set — confirms the except Exception (not
    # BaseException) discipline from §5.3.
    assert vllm_manager._load_error is None


# ── manual unload while a load is in progress ─────────────────────────


@pytest.mark.asyncio
async def test_unload_waits_for_in_progress_load_then_unloads(boot):
    boot.delay = 0.05

    load_task = asyncio.create_task(
        vllm_manager.ensure_loaded(_profile("a-model"), _deadline())
    )
    await asyncio.sleep(0.005)  # let the load acquire _swap_lock

    unload_task = asyncio.create_task(vllm_manager.unload_model())
    await asyncio.sleep(0.005)
    assert not unload_task.done(), "unload should wait for active load to finish"

    await load_task
    result = await asyncio.wait_for(unload_task, timeout=2.0)

    assert result == {"status": "unloaded", "was": "a-model"}
    assert vllm_manager._runtime.resident_alias is None
