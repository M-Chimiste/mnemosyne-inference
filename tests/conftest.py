"""Shared test fixtures.

Tests import `vllm_manager` directly — it has no CUDA-dependent imports
at module load (vLLM is launched as a subprocess, not imported), so this
runs on a stock macOS/Linux dev host.

Phase 1 lifespan hard-fails if /config/config.yaml is missing, so the
default `client` fixture transparently writes a minimal valid config to
a tmp path and points the relevant env vars at tmp before TestClient
enters lifespan.
"""
import sys
from pathlib import Path

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


@pytest.fixture
def client(tmp_config):
    """TestClient with all module globals reset and a minimal valid config."""
    # Phase 0 globals
    vllm_manager.current_model = None
    vllm_manager.vllm_process = None
    vllm_manager.model_load_time = None
    vllm_manager._loading = False
    vllm_manager._current_tp = vllm_manager.DEFAULT_TP
    vllm_manager._current_gpu_mem = vllm_manager.DEFAULT_GPU_MEM
    vllm_manager._downloads = {}
    vllm_manager.MODEL_ALIASES = {}
    # Phase 1 globals
    if vllm_manager._catalog is not None:
        vllm_manager._catalog.close()
    vllm_manager._config = None
    vllm_manager._catalog = None

    with TestClient(vllm_manager.app) as c:
        yield c
