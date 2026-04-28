"""Shared test fixtures.

Tests import `vllm_manager` directly — it has no CUDA-dependent imports
at module load (vLLM is launched as a subprocess, not imported), so this
runs on a stock macOS/Linux dev host.

Phase 1 lifespan hard-fails if /config/config.yaml is missing, so the
default `client` fixture transparently writes a minimal valid config to
a tmp path and points the relevant env vars at tmp before TestClient
enters lifespan.

Phase 2 adds `stub_vllm` and `rich_config` for tests that need to
exercise the swap queue, eviction, and proxy without launching a real
vLLM subprocess. See plans/phase_2.md §8.6.
"""
import asyncio
import sys
import time
from pathlib import Path
from textwrap import dedent

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import vllm_manager  # noqa: E402

MINIMAL_CONFIG_YAML = """\
storage:
  default: tmp
  locations:
    - name: tmp
      path: {tmp_path}
"""

RICH_CONFIG_YAML = """\
server:
  idle_unload_seconds: 900
  swap_queue_timeout_seconds: 5

storage:
  default: tmp
  locations:
    - name: tmp
      path: {tmp_path}

defaults:
  gpu_memory_utilization: 0.85
  trust_remote_code: true
  max_model_len: null

models:
  - alias: a-model
    model: org/a-model
    gpus: all

  - alias: b-model
    model: org/b-model
    gpus: all
    quantization: awq
    max_model_len: 32768
"""


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    """Redirect Phase 1 path env vars to tmp. No files written."""
    monkeypatch.setenv("MNEMOSYNE_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("MNEMOSYNE_DB_PATH",     str(tmp_path / "mnemosyne.db"))
    monkeypatch.setenv("MNEMOSYNE_ENV_PATH",    str(tmp_path / ".env"))
    return tmp_path


@pytest.fixture
def tmp_config(tmp_paths):
    """Write a minimal valid YAML — empty models, single tmp storage location."""
    cfg = tmp_paths / "config.yaml"
    cfg.write_text(MINIMAL_CONFIG_YAML.format(tmp_path=tmp_paths))
    return cfg


def _reset_globals():
    """Reset every module global the test suite touches. Called by every
    Phase 1+ fixture before TestClient enters lifespan."""
    from runtime import RuntimeState

    # Phase 0 globals (Phase 2 retired the {current_model, _loading,
    # _current_tp, _current_gpu_mem, model_load_time, loading_lock} set —
    # they live on _runtime now).
    vllm_manager.vllm_process = None
    vllm_manager._downloads = {}
    vllm_manager.MODEL_ALIASES = {}
    # Phase 1 globals
    if vllm_manager._catalog is not None:
        vllm_manager._catalog.close()
    vllm_manager._config = None
    vllm_manager._catalog = None
    # Phase 2 globals
    vllm_manager._runtime = RuntimeState()
    vllm_manager._loading_target = None
    vllm_manager._load_event = None
    vllm_manager._load_error = None
    vllm_manager._eviction_task = None
    vllm_manager._flush_task = None
    vllm_manager._legacy_alias_warned.clear()
    # Reset the lock so a new event loop's primitives don't tangle with
    # one held over from a prior test.
    vllm_manager._swap_lock = asyncio.Lock()


@pytest.fixture
def client(tmp_config):
    """TestClient with all module globals reset and a minimal valid config."""
    _reset_globals()
    with TestClient(vllm_manager.app) as c:
        yield c


@pytest.fixture
def rich_config(tmp_paths):
    """Config with two configured aliases (a-model, b-model) and a 5s swap
    timeout for fast tests. Used by proxy/swap-queue/eviction suites."""
    cfg = tmp_paths / "config.yaml"
    cfg.write_text(RICH_CONFIG_YAML.format(tmp_path=tmp_paths))
    return cfg


class StubLauncher:
    """Replacement for _start_vllm that flips _runtime state in-process,
    optionally sleeping or raising to simulate slow loads / failures.
    Tracks call history so tests can assert on launch counts and order."""

    def __init__(self):
        self.calls: list = []           # ResolvedProfile per launch
        self.delay: float = 0.0         # seconds to sleep before flipping state
        self.fail_with: Exception | None = None
        self.kill_calls: int = 0

    async def start(self, profile):
        self.calls.append(profile)
        # Mirror real _start_vllm: kill (and flush) before launching.
        self.kill()
        # Mirror the try/except (Exception, CancelledError): _kill_vllm(); raise
        # cleanup that wraps _wait_for_vllm in the real implementation. Tests
        # rely on this to verify the no-zombie-subprocess invariant.
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_with is not None:
                raise self.fail_with
        except (Exception, asyncio.CancelledError):
            self.kill()
            raise
        rt = vllm_manager._runtime
        rt.resident_alias = profile.alias
        rt.resident_profile = profile
        rt.resident_tp_size = 1
        now = time.time()
        rt.model_load_time = now
        rt.last_used_at = now

    def kill(self):
        self.kill_calls += 1
        # Flush in-memory usage to catalog so eviction tests see the write,
        # then clear the resident view — same contract as the real _kill_vllm.
        vllm_manager._flush_usage()
        rt = vllm_manager._runtime
        rt.resident_alias = None
        rt.resident_profile = None
        rt.resident_tp_size = None
        rt.model_load_time = None
        rt.last_used_at = None


@pytest.fixture
def stub_vllm(monkeypatch):
    """Replace _start_vllm and _kill_vllm with in-process stubs that don't
    spawn subprocesses. Returns the StubLauncher so tests can configure
    delay/failure and assert on call history."""
    stub = StubLauncher()
    monkeypatch.setattr(vllm_manager, "_start_vllm", stub.start)
    monkeypatch.setattr(vllm_manager, "_kill_vllm", stub.kill)
    return stub


@pytest.fixture
def rich_client(rich_config, stub_vllm):
    """TestClient backed by RICH_CONFIG_YAML with subprocess launches stubbed.
    Yields (client, stub) so tests can drive both."""
    _reset_globals()
    with TestClient(vllm_manager.app) as c:
        yield c, stub_vllm
