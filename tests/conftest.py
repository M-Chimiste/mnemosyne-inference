"""Test fixtures shared across the Phase 0 smoke harness.

Tests import `vllm_manager` directly — it has no CUDA-dependent imports
at module load (vLLM is launched as a subprocess, not imported), so this
runs on a stock macOS/Linux dev host.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import vllm_manager  # noqa: E402


@pytest.fixture
def client():
    """Yield a TestClient with all module globals reset to their defaults.

    Reset list mirrors the globals declared in vllm_manager.py. New globals
    added in later phases must be reset here too.
    """
    vllm_manager.current_model = None
    vllm_manager.vllm_process = None
    vllm_manager.model_load_time = None
    vllm_manager._loading = False
    vllm_manager._current_tp = vllm_manager.DEFAULT_TP
    vllm_manager._current_gpu_mem = vllm_manager.DEFAULT_GPU_MEM
    vllm_manager._downloads = {}
    vllm_manager.MODEL_ALIASES = {}

    with TestClient(vllm_manager.app) as c:
        yield c
