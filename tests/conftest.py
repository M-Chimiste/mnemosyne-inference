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
import contextlib
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


@pytest.fixture(autouse=True)
def _plane_auth_env(monkeypatch):
    """Exercise admin Basic by default and keep inference auth opt-in."""
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")
    monkeypatch.delenv("INFERENCE_API_KEY", raising=False)


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
    # they live on _runtime now). Phase 4 retired _downloads.
    vllm_manager.vllm_process = None
    vllm_manager.MODEL_ALIASES = {}
    # Phase 4 — reset live download handles.
    import downloader
    downloader._active.clear()
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


@contextlib.contextmanager
def _running_lifespan():
    """Drive manager_lifespan() around TestClient without FastAPI lifespan.

    Phase 3 serves two apps with lifespan disabled, so tests need process
    setup/teardown explicitly. Background tasks and signal handlers are off
    because TestClient runs requests on its own event loop.
    """
    loop = asyncio.new_event_loop()
    cm = vllm_manager.manager_lifespan(
        install_signals=False,
        spawn_background=False,
    )
    loop.run_until_complete(cm.__aenter__())
    try:
        yield
    finally:
        loop.run_until_complete(cm.__aexit__(None, None, None))
        loop.close()


@pytest.fixture
def client(tmp_config):
    """TestClient with all module globals reset and a minimal valid config."""
    _reset_globals()
    with _running_lifespan():
        with TestClient(vllm_manager.app) as c:
            c.auth = ("admin", "test-pw")
            yield c


@pytest.fixture
def admin_client_no_auth(tmp_config):
    """Admin TestClient without default Basic credentials."""
    _reset_globals()
    with _running_lifespan():
        with TestClient(vllm_manager.app) as c:
            yield c


@pytest.fixture
def inference_client(tmp_config):
    """Inference-plane TestClient; exposes only /health and /v1/*."""
    _reset_globals()
    with _running_lifespan():
        with TestClient(vllm_manager.inference_app) as c:
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
    with _running_lifespan():
        with TestClient(vllm_manager.app) as c:
            c.auth = ("admin", "test-pw")
            yield c, stub_vllm


class StubDownloader:
    """In-process replacement for downloader.start_install. Captures the
    last call's args, optionally flips catalog state directly so the
    /v1/* test paths can transition through queued → installed without
    spawning a real subprocess."""

    def __init__(self):
        self.calls: list = []
        self.auto_complete: bool = False
        self.fail_with_error: str | None = None

    def __call__(self, *, alias, model_id, revision, cache_dir, ignore_patterns,
                 hf_token, catalog, storage_location,
                 gguf_primary_filename=None):
        self.calls.append({
            "alias": alias,
            "model_id": model_id,
            "revision": revision,
            "cache_dir": cache_dir,
            "ignore_patterns": ignore_patterns,
            "hf_token": hf_token,
            "storage_location": storage_location,
            "gguf_primary_filename": gguf_primary_filename,
        })

        class Handle:
            pass
        h = Handle()
        h.alias = alias

        if self.fail_with_error:
            catalog.mark_error(alias, self.fail_with_error)
        elif self.auto_complete:
            # Pretend the worker finished cleanly. Caller-controlled cache
            # path; sha derived from a fake constant.
            sha = "b" * 40
            catalog.mark_complete(
                alias,
                cache_path=f"{cache_dir}/models--placeholder/snapshots/{sha}",
                size_bytes=0,
                resolved_sha=sha,
            )
        return h


@pytest.fixture
def stub_downloader(monkeypatch):
    """Replace downloader.start_install with a sync stub that records
    invocation args. Yields the StubDownloader so tests can inspect
    captured calls and toggle auto_complete / fail_with_error."""
    stub = StubDownloader()
    monkeypatch.setattr(vllm_manager.downloader, "start_install", stub)
    yield stub
