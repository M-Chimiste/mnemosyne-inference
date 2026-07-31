"""Phase 1 — config schema, loaders, profile resolver."""
from __future__ import annotations

import os
from textwrap import dedent
from unittest import mock

import pytest

from config import (
    Config,
    ConfigError,
    Defaults,
    ModelProfile,
    Server,
    Storage,
    StorageLocation,
    load_config,
    load_env,
)
from profiles import ProfileNotReady, resolve_profile


# ── helpers ──────────────────────────────────────────────────────────

def _write(path, text: str) -> str:
    path.write_text(dedent(text))
    return str(path)


def _minimal_yaml(tmp_path) -> str:
    return dedent(f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
    """)


@pytest.fixture
def cfg_path(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(_minimal_yaml(tmp_path))
    return p


@pytest.fixture(autouse=True)
def _no_real_nvidia_smi(monkeypatch):
    """Default to 'no GPUs visible, probe failed' so list-gpu validators
    don't reach for real hardware. Individual tests override this."""
    import config as config_mod
    monkeypatch.setattr(config_mod, "gpu_indices_or_none", lambda: None)


# ── parsing ──────────────────────────────────────────────────────────

def test_minimal_config_parses(cfg_path):
    cfg = load_config(str(cfg_path))
    assert cfg.storage.default == "tmp"
    assert len(cfg.storage.locations) == 1
    assert cfg.models == []
    assert cfg.server.inference_port == 8000
    assert cfg.server.max_concurrency is None
    assert cfg.server.max_queue_depth == 128
    assert cfg.fleet.node_id == ""
    assert cfg.defaults.gpu_memory_utilization == 0.90


def test_full_config_parses(tmp_path):
    p = _write(tmp_path / "config.yaml", f"""\
        server:
          idle_unload_seconds: null
        storage:
          default: nvme
          locations:
            - name: nvme
              path: {tmp_path}
            - name: archive
              path: {tmp_path}
        defaults:
          max_model_len: 16384
        models:
          - alias: qwen-72b-awq
            model: Qwen/Qwen2.5-72B-Instruct-AWQ
            quantization: awq
            gpus: all
            max_model_len: 32768
            storage: nvme
          - alias: qwen-coder-7b
            model: Qwen/Qwen2.5-Coder-7B-Instruct
            gpus: [0]
          - alias: llama-vision
            model: meta-llama/Llama-3.2-11B-Vision-Instruct
            gpus: [1]
            storage: archive
            extra_args:
              - --limit-mm-per-prompt
              - image=4
    """)
    cfg = load_config(p)
    assert cfg.server.idle_unload_seconds is None
    assert cfg.defaults.max_model_len == 16384
    aliases = [m.alias for m in cfg.models]
    assert aliases == ["qwen-72b-awq", "qwen-coder-7b", "llama-vision"]
    assert cfg.models[0].gpus == "all"
    assert cfg.models[1].gpus == [0]
    assert cfg.models[2].extra_args == ["--limit-mm-per-prompt", "image=4"]


def test_model_capability_defaults_are_engine_aware():
    vllm = ModelProfile(alias="vllm-model", model="org/model")
    llama = ModelProfile(
        alias="llama-model",
        model="org/model-gguf",
        backend="llama.cpp",
        gguf_filename="model-Q4_K_M.gguf",
    )
    image = ModelProfile(
        alias="image-model",
        model="org/image",
        backend="sglang-diffusion",
        kind="image",
    )

    assert vllm.capabilities == (
        "chat.completions",
        "completions",
        "embeddings",
        "responses",
    )
    assert llama.capabilities == (
        "chat.completions",
        "completions",
        "responses",
    )
    assert image.capabilities == ("images.generations",)


def test_llamacpp_generation_contract_and_projector_are_configurable():
    profile = ModelProfile(
        alias="vision-model",
        model="org/model-gguf",
        backend="llama.cpp",
        gguf_filename="model-Q4_K_M.gguf",
        projector_filename="vision/mmproj-F16.gguf",
        capabilities=[
            "/v1/responses",
            "completions",
            "chat/completions",
        ],
    )

    assert profile.capabilities == (
        "chat.completions",
        "completions",
        "responses",
    )
    assert profile.projector_filename == "vision/mmproj-F16.gguf"


def test_llamacpp_projector_rejects_specialized_or_unsafe_profile():
    with pytest.raises(ValueError, match="generation-capable"):
        ModelProfile(
            alias="embed-model",
            model="org/model-gguf",
            backend="llama.cpp",
            gguf_filename="model-Q4_K_M.gguf",
            projector_filename="mmproj-F16.gguf",
            capabilities=["embeddings"],
        )
    with pytest.raises(ValueError, match="repository-relative"):
        ModelProfile(
            alias="unsafe-projector",
            model="org/model-gguf",
            backend="llama.cpp",
            gguf_filename="model-Q4_K_M.gguf",
            projector_filename="../mmproj-F16.gguf",
        )


def test_idle_unload_null_parses_to_none(cfg_path):
    cfg_path.write_text(cfg_path.read_text() + "\nserver:\n  idle_unload_seconds: null\n")
    cfg = load_config(str(cfg_path))
    assert cfg.server.idle_unload_seconds is None


def test_empty_models_allowed(cfg_path):
    cfg = load_config(str(cfg_path))
    assert cfg.models == []


def test_concurrency_queue_must_have_at_least_one_slot(tmp_path):
    p = _write(tmp_path / "config.yaml", f"""\
        server:
          max_queue_depth: 0
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
    """)
    with pytest.raises(ConfigError, match="max_queue_depth"):
        load_config(p)


def test_fleet_and_usage_node_ids_must_match_when_reporting_enabled(tmp_path):
    p = _write(tmp_path / "config.yaml", f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
        fleet:
          node_id: cuda-a
        token_sidecar:
          enabled: true
          node_id: cuda-b
    """)
    with pytest.raises(ConfigError, match="must match token_sidecar.node_id"):
        load_config(p)


# ── validation failures ─────────────────────────────────────────────

def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "does-not-exist.yaml"))


def test_malformed_yaml_raises(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("storage:\n  default: tmp\n  locations:\n    - name: tmp\n      path: {bad")
    with pytest.raises(ConfigError, match="malformed YAML"):
        load_config(str(p))


def test_top_level_must_be_mapping(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(str(p))


def test_unknown_top_level_key_rejected(tmp_path):
    p = _write(tmp_path / "config.yaml", f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
        wat: 1
    """)
    with pytest.raises(ConfigError, match="invalid config"):
        load_config(p)


def test_storage_default_unknown_rejected(tmp_path):
    p = _write(tmp_path / "config.yaml", f"""\
        storage:
          default: missing
          locations:
            - name: foo
              path: {tmp_path}
    """)
    with pytest.raises(ConfigError):
        load_config(p)


def test_storage_locations_empty_rejected(tmp_path):
    p = _write(tmp_path / "config.yaml", """\
        storage:
          default: tmp
          locations: []
    """)
    with pytest.raises(ConfigError):
        load_config(p)


def test_model_storage_unknown_rejected(tmp_path):
    p = _write(tmp_path / "config.yaml", f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
        models:
          - alias: foo
            model: org/foo
            storage: ghost
    """)
    with pytest.raises(ConfigError, match="ghost"):
        load_config(p)


def test_duplicate_alias_rejected(tmp_path):
    p = _write(tmp_path / "config.yaml", f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
        models:
          - alias: dup
            model: org/a
          - alias: dup
            model: org/b
    """)
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(p)


def test_alias_must_be_lowercase_hyphenated(tmp_path):
    p = _write(tmp_path / "config.yaml", f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
        models:
          - alias: Bad_Alias
            model: org/m
    """)
    with pytest.raises(ConfigError):
        load_config(p)


@pytest.mark.parametrize("alias", ["__cache__:abcd", "__cache__/foo"])
def test_reserved_synthetic_prefix_rejected(tmp_path, alias):
    p = _write(tmp_path / "config.yaml", f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
        models:
          - alias: "{alias}"
            model: org/m
    """)
    with pytest.raises(ConfigError, match="reserved prefix"):
        load_config(p)


def test_gpus_empty_list_rejected(tmp_path):
    p = _write(tmp_path / "config.yaml", f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
        models:
          - alias: foo
            model: org/m
            gpus: []
    """)
    with pytest.raises(ConfigError):
        load_config(p)


# ── runtime validation: GPU probe ────────────────────────────────────

def test_gpu_index_missing_hard_fails(tmp_path, monkeypatch):
    import config as config_mod
    monkeypatch.setattr(config_mod, "gpu_indices_or_none", lambda: [0])
    p = _write(tmp_path / "config.yaml", f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
        models:
          - alias: foo
            model: org/m
            gpus: [3]
    """)
    with pytest.raises(ConfigError, match="GPU 3"):
        load_config(p)


def test_gpu_probe_unavailable_does_not_fail(tmp_path, monkeypatch):
    import config as config_mod
    monkeypatch.setattr(config_mod, "gpu_indices_or_none", lambda: None)
    p = _write(tmp_path / "config.yaml", f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
        models:
          - alias: foo
            model: org/m
            gpus: [42]
    """)
    cfg = load_config(p)
    assert cfg.models[0].gpus == [42]


# ── .env loader ──────────────────────────────────────────────────────

def test_load_env_populates_unset_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("MNEMOSYNE_TEST_KEY_A", raising=False)
    monkeypatch.delenv("MNEMOSYNE_TEST_KEY_B", raising=False)
    p = tmp_path / ".env"
    p.write_text(
        "MNEMOSYNE_TEST_KEY_A=alpha\n"
        "# a comment\n"
        "\n"
        "MNEMOSYNE_TEST_KEY_B=\"with spaces\"\n"
    )
    parsed = load_env(str(p))
    assert parsed == {"MNEMOSYNE_TEST_KEY_A": "alpha", "MNEMOSYNE_TEST_KEY_B": "with spaces"}
    assert os.environ["MNEMOSYNE_TEST_KEY_A"] == "alpha"
    assert os.environ["MNEMOSYNE_TEST_KEY_B"] == "with spaces"


def test_load_env_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_TEST_PRESET", "preset")
    p = tmp_path / ".env"
    p.write_text("MNEMOSYNE_TEST_PRESET=overwritten\n")
    load_env(str(p))
    assert os.environ["MNEMOSYNE_TEST_PRESET"] == "preset"


def test_load_env_missing_file_ok(tmp_path):
    parsed = load_env(str(tmp_path / "absent.env"))
    assert parsed == {}


# ── profile resolver ────────────────────────────────────────────────

def _make_config(tmp_path) -> Config:
    return Config.model_validate({
        "storage": {
            "default": "tmp",
            "locations": [
                {"name": "tmp", "path": str(tmp_path)},
                {"name": "alt", "path": str(tmp_path / "alt")},
            ],
        },
        "defaults": {
            "gpu_memory_utilization": 0.85,
            "max_model_len": 8192,
        },
        "models": [
            {"alias": "qw7b", "model": "Qwen/Qwen2.5-7B"},
            {"alias": "qw72b-awq", "model": "Qwen/Qwen2.5-72B-AWQ",
             "quantization": "awq", "gpus": [0, 1],
             "max_model_len": 32768, "storage": "alt",
             "extra_args": ["--foo", "bar"]},
        ],
    })


def test_resolve_profile_applies_defaults(tmp_path):
    cfg = _make_config(tmp_path)
    rp = resolve_profile("qw7b", cfg)
    assert rp.alias == "qw7b"
    assert rp.model == "Qwen/Qwen2.5-7B"
    assert rp.gpus == "all"
    assert rp.max_model_len == 8192
    assert rp.gpu_memory_utilization == 0.85
    assert rp.storage_name == "tmp"
    assert rp.storage_path == str(tmp_path)
    assert rp.extra_args == ()


def test_resolve_profile_overrides_default(tmp_path):
    cfg = _make_config(tmp_path)
    rp = resolve_profile("qw72b-awq", cfg)
    assert rp.max_model_len == 32768
    assert rp.gpus == [0, 1]
    assert rp.quantization == "awq"
    assert rp.storage_name == "alt"
    assert rp.storage_path == str(tmp_path / "alt")
    assert rp.extra_args == ("--foo", "bar")


def test_resolve_profile_unknown_alias(tmp_path):
    cfg = _make_config(tmp_path)
    with pytest.raises(KeyError):
        resolve_profile("nope", cfg)


def test_resolve_profile_falls_back_to_ui_install(tmp_path):
    """No config row, but a ui_install catalog row exists."""
    from catalog import open_catalog
    cfg = _make_config(tmp_path)
    cat = open_catalog(":memory:")
    try:
        cat._raw_insert_model(
            alias="ui-only",
            hf_model_id="org/ui",
            source="ui_install",
            quantization="gptq",
            gpus='[1]',
            storage_location="tmp",
            extra_args='["--x"]',
        )
        rp = resolve_profile("ui-only", cfg, catalog=cat)
        assert rp.model == "org/ui"
        assert rp.gpus == [1]
        assert rp.quantization == "gptq"
        assert rp.extra_args == ("--x",)
    finally:
        cat.close()


def test_resolve_profile_config_wins_over_ui_install(tmp_path):
    from catalog import open_catalog
    cfg = _make_config(tmp_path)
    cat = open_catalog(":memory:")
    try:
        cat._raw_insert_model(
            alias="qw7b",
            hf_model_id="evil/shadow",
            source="ui_install",
            gpus='"all"',
            storage_location="tmp",
        )
        rp = resolve_profile("qw7b", cfg, catalog=cat)
        # Config alias still wins
        assert rp.model == "Qwen/Qwen2.5-7B"
    finally:
        cat.close()


def test_resolve_config_profile_uses_matching_installed_sha(tmp_path):
    from catalog import open_catalog

    cfg = _make_config(tmp_path)
    cat = open_catalog(":memory:")
    try:
        cat._raw_insert_model(
            alias="qw7b",
            hf_model_id="Qwen/Qwen2.5-7B",
            source="config",
            gpus='"all"',
            storage_location="tmp",
            status="installed",
            resolved_sha="a" * 40,
            backend="vllm",
        )
        rp = resolve_profile("qw7b", cfg, catalog=cat)
        assert rp.revision == "a" * 40
        assert rp.upstream_model == "Qwen/Qwen2.5-7B"
    finally:
        cat.close()


def test_resolve_profile_config_llamacpp_uses_reconciled_cache_path(tmp_path):
    from catalog import open_catalog
    cfg = Config.model_validate({
        "storage": {
            "default": "tmp",
            "locations": [{"name": "tmp", "path": str(tmp_path)}],
        },
        "models": [{
            "alias": "gguf",
            "model": "org/gguf-repo",
            "backend": "llama.cpp",
            "gguf_filename": "model-Q4_K_M.gguf",
        }],
    })
    snap = tmp_path / "hub" / "models--org--gguf-repo" / "snapshots" / ("a" * 40)
    snap.mkdir(parents=True)
    gguf = snap / "model-Q4_K_M.gguf"
    gguf.write_text("fake")
    cat = open_catalog(":memory:")
    try:
        cat._raw_insert_model(
            alias="gguf",
            hf_model_id="org/gguf-repo",
            source="config",
            gpus='"all"',
            storage_location="tmp",
            status="installed",
            cache_path=str(snap),
            resolved_sha="a" * 40,
            backend="llama.cpp",
            gguf_filename="model-Q4_K_M.gguf",
        )
        rp = resolve_profile("gguf", cfg, catalog=cat)
        assert rp.served_model_name == "gguf"
        assert rp.engine_model_path == str(gguf)
        assert "snapshots/main" not in rp.engine_model_path
    finally:
        cat.close()


def test_resolve_profile_config_llamacpp_resolves_explicit_projector(tmp_path):
    from catalog import open_catalog

    cfg = Config.model_validate({
        "storage": {
            "default": "tmp",
            "locations": [{"name": "tmp", "path": str(tmp_path)}],
        },
        "models": [{
            "alias": "gguf",
            "model": "org/gguf-repo",
            "backend": "llama.cpp",
            "gguf_filename": "model-Q4_K_M.gguf",
            "projector_filename": "vision/mmproj-F16.gguf",
        }],
    })
    snap = tmp_path / "hub" / "models--org--gguf-repo" / "snapshots" / ("a" * 40)
    (snap / "vision").mkdir(parents=True)
    gguf = snap / "model-Q4_K_M.gguf"
    projector = snap / "vision" / "mmproj-F16.gguf"
    gguf.write_text("fake")
    projector.write_text("fake-projector")
    cat = open_catalog(":memory:")
    try:
        cat._raw_insert_model(
            alias="gguf",
            hf_model_id="org/gguf-repo",
            source="config",
            gpus='"all"',
            storage_location="tmp",
            status="installed",
            cache_path=str(snap),
            resolved_sha="a" * 40,
            backend="llama.cpp",
            gguf_filename="model-Q4_K_M.gguf",
        )
        rp = resolve_profile("gguf", cfg, catalog=cat)
        assert rp.engine_model_path == str(gguf)
        assert rp.projector_filename == "vision/mmproj-F16.gguf"
        assert rp.projector_model_path == str(projector)
    finally:
        cat.close()


def test_resolve_legacy_mixed_llamacpp_capabilities_matches_launch_mode(
    tmp_path,
):
    from catalog import open_catalog
    from runtime import build_llama_argv
    import vllm_manager

    cfg = Config.model_validate({
        "storage": {
            "default": "tmp",
            "locations": [{"name": "tmp", "path": str(tmp_path)}],
        },
    })
    cat = open_catalog(":memory:")
    try:
        cat._raw_insert_model(
            alias="legacy-gguf",
            hf_model_id="org/legacy-gguf",
            source="ui_install",
            gpus='"all"',
            storage_location="tmp",
            status="installed",
            cache_path=str(tmp_path / "snapshot"),
            resolved_sha="a" * 40,
            backend="llama.cpp",
            gguf_filename="model-Q4_K_M.gguf",
            capabilities=(
                '["chat.completions","completions","embeddings","responses"]'
            ),
        )
        profile = resolve_profile("legacy-gguf", cfg, catalog=cat)
    finally:
        cat.close()

    assert profile.capabilities == (
        "chat.completions",
        "completions",
        "responses",
    )
    argv = build_llama_argv(profile, host="127.0.0.1", port=8002)
    assert "--embedding" not in argv
    _deployment_id, identity, authoritative = (
        vllm_manager._profile_deployment(profile)
    )
    assert identity["capabilities"] == [
        "chat/completions",
        "completions",
        "responses",
    ]
    assert authoritative is True


def test_resolve_profile_config_llamacpp_requires_installed_cache_row(tmp_path):
    from catalog import open_catalog
    cfg = Config.model_validate({
        "storage": {
            "default": "tmp",
            "locations": [{"name": "tmp", "path": str(tmp_path)}],
        },
        "models": [{
            "alias": "gguf",
            "model": "org/gguf-repo",
            "backend": "llama.cpp",
            "gguf_filename": "model-Q4_K_M.gguf",
        }],
    })
    cat = open_catalog(":memory:")
    try:
        cat._raw_insert_model(
            alias="gguf",
            hf_model_id="org/gguf-repo",
            source="config",
            gpus='"all"',
            storage_location="tmp",
            status="partial",
            cache_path=None,
            backend="llama.cpp",
            gguf_filename="model-Q4_K_M.gguf",
        )
        with pytest.raises(ProfileNotReady):
            resolve_profile("gguf", cfg, catalog=cat)
    finally:
        cat.close()
