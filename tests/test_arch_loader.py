"""Phase 5 — load_supported_architectures fallback chain.

Runs on macOS dev hosts; vLLM is not installed so the runtime path is
exercised via a stubbed module rather than the real import.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

import hf_search


REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_ARCH_PATH = REPO_ROOT / "vllm_supported_architectures.json"


@pytest.fixture(autouse=True)
def _isolate_vllm_module(monkeypatch):
    """Make sure no leaked import of `vllm` from a prior test affects this one."""
    for mod in list(sys.modules):
        if mod == "vllm" or mod.startswith("vllm."):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    yield


def _install_fake_vllm_registry(monkeypatch, archs: list[str]) -> None:
    """Materialize a fake `vllm.model_executor.models.registry.ModelRegistry`
    in sys.modules so hf_search's runtime introspection finds it."""
    class FakeRegistry:
        @staticmethod
        def get_supported_archs():
            return list(archs)

    pkg_vllm = types.ModuleType("vllm")
    pkg_me = types.ModuleType("vllm.model_executor")
    pkg_models = types.ModuleType("vllm.model_executor.models")
    pkg_registry = types.ModuleType("vllm.model_executor.models.registry")
    pkg_registry.ModelRegistry = FakeRegistry
    pkg_models.ModelRegistry = FakeRegistry  # legacy import path
    monkeypatch.setitem(sys.modules, "vllm", pkg_vllm)
    monkeypatch.setitem(sys.modules, "vllm.model_executor", pkg_me)
    monkeypatch.setitem(sys.modules, "vllm.model_executor.models", pkg_models)
    monkeypatch.setitem(sys.modules, "vllm.model_executor.models.registry", pkg_registry)


def test_runtime_registry_succeeds(monkeypatch, tmp_path):
    """When ModelRegistry is importable, source = 'vllm-registry'."""
    _install_fake_vllm_registry(monkeypatch, ["LlamaForCausalLM", "Qwen2ForCausalLM"])
    archs, source = hf_search.load_supported_architectures(
        tmp_path / "missing.json",
    )
    assert source == "vllm-registry"
    assert archs == frozenset({"LlamaForCausalLM", "Qwen2ForCausalLM"})


def test_runtime_registry_falls_through_to_bundled(monkeypatch, tmp_path):
    """ImportError on both registry paths → bundled JSON."""
    # Don't install fake vllm — the import will raise ImportError. We do
    # need the bundled JSON to be present though, so write one.
    bundled = tmp_path / "fallback.json"
    bundled.write_text(json.dumps({
        "vllm_version": "test-0.0.0",
        "generated_at": "2026-04-28T00:00:00Z",
        "architectures": ["LlamaForCausalLM", "Qwen2ForCausalLM"],
    }))
    archs, source = hf_search.load_supported_architectures(bundled)
    assert source == "bundled-json"
    assert archs == frozenset({"LlamaForCausalLM", "Qwen2ForCausalLM"})


def test_missing_json_yields_empty(monkeypatch, tmp_path, caplog):
    """No registry, no JSON → empty set, ERROR-level log."""
    archs, source = hf_search.load_supported_architectures(
        tmp_path / "does-not-exist.json",
    )
    assert source == "empty"
    assert archs == frozenset()
    # ERROR record exists
    assert any(rec.levelname == "ERROR" for rec in caplog.records)


def test_malformed_json_yields_empty(monkeypatch, tmp_path, caplog):
    bad = tmp_path / "bad.json"
    bad.write_text("not json {{{")
    archs, source = hf_search.load_supported_architectures(bad)
    assert source == "empty"
    assert archs == frozenset()
    assert any(rec.levelname == "ERROR" for rec in caplog.records)


def test_bundled_repo_file_parses_and_has_common_archs():
    """The committed snapshot must parse and include common architectures.

    Per Phase 5 plan: 'replacing the current placeholder JSON with a
    generated snapshot' — we use a hand-curated list as the bundled
    fallback; runtime introspection is the primary source.
    """
    assert BUNDLED_ARCH_PATH.exists()
    data = json.loads(BUNDLED_ARCH_PATH.read_text())
    archs = set(data.get("architectures") or [])
    assert "LlamaForCausalLM" in archs
    assert "Qwen2ForCausalLM" in archs
    assert len(archs) > 50, f"only {len(archs)} architectures bundled"


def test_empty_architectures_in_json_yields_empty(monkeypatch, tmp_path, caplog):
    """Bundled JSON exists but architectures list is empty (placeholder
    state): treat as empty and log loudly."""
    bundled = tmp_path / "placeholder.json"
    bundled.write_text(json.dumps({
        "vllm_version": None,
        "generated_at": None,
        "architectures": [],
    }))
    archs, source = hf_search.load_supported_architectures(bundled)
    assert source == "empty"
    assert archs == frozenset()
    assert any(rec.levelname == "ERROR" for rec in caplog.records)
