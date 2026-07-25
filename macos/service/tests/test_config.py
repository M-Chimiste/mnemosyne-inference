from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mnemosyne_macos.config import (
    ConfigError,
    ImageProfileConfig,
    MacConfig,
    load_config,
    parse_config,
    save_config,
    suggested_model_alias,
)


EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml.example"


def test_shipped_example_config_is_valid() -> None:
    payload = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))

    config = MacConfig.model_validate(payload)

    assert config.engines.omlx.enabled is False
    assert next(model for model in config.models if model.alias == "glm-5-2").enabled
    assert "glm-5-2" not in config.profiles()
from mnemosyne_macos.models import Endpoint, EngineName, ModelKind


def test_defaults_replace_the_legacy_sidecar_port() -> None:
    config = MacConfig()
    assert config.server.inference_port == 1240
    assert config.server.control_port == 17321
    assert config.engines.lmstudio.base_url.endswith(":1234")
    assert config.engines.lmstudio.enabled is False
    assert config.engines.llama_cpp.port == 17325
    assert config.engines.llama_cpp.enabled is True
    assert config.engines.omlx.base_url.endswith(":17322")
    assert config.engines.ds4.port == 17323
    assert config.engines.mflux.port == 17324
    assert config.engines.ds4.process_state_path.endswith("/state/ds4-process.json")
    assert config.token_sidecar.enabled is True
    assert config.token_sidecar.node_id == ""


def test_legacy_mac_node_placeholder_migrates_to_automatic_identity() -> None:
    config = MacConfig.model_validate(
        {"token_sidecar": {"enabled": True, "node_id": "mnemosyne-mac"}}
    )
    assert config.token_sidecar.node_id == ""


def test_parse_config_validates_in_memory_yaml_without_a_file() -> None:
    config = parse_config(
        """
        engines:
          lmstudio:
            enabled: true
          omlx:
            enabled: false
          ds4:
            enabled: false
        models:
          - alias: local-model
            engine: lmstudio
            model: publisher/model
        """,
        source="in-memory configuration",
    )

    assert [model.alias for model in config.models] == ["local-model"]


def test_parse_config_reports_source_for_invalid_yaml() -> None:
    with pytest.raises(ConfigError, match="in-memory configuration"):
        parse_config("models: [", source="in-memory configuration")


def test_save_config_is_atomic_private_and_round_trips(tmp_path) -> None:
    path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {"mflux": {"enabled": True}},
            "models": [
                {
                    "alias": "qwen-image",
                    "engine": "mflux",
                    "model": "Qwen/Qwen-Image",
                    "kind": "image",
                    "image": {"family": "qwen-image"},
                }
            ],
        }
    )

    save_config(config, path)

    assert load_config(path) == config
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".*.tmp"))


def test_ds4_process_state_path_is_configurable(tmp_path) -> None:
    state_path = tmp_path / "owned-ds4.json"
    config = MacConfig.model_validate(
        {"engines": {"ds4": {"process_state_path": str(state_path)}}}
    )
    assert config.engines.ds4.process_state_path == str(state_path)


def test_profiles_resolve_engine_specific_wire_names() -> None:
    config = MacConfig.model_validate(
        {
            "engines": {"lmstudio": {"enabled": True}},
            "models": [
                {"alias": "studio-model", "engine": "lmstudio", "model": "org/model"},
                {"alias": "deepseek-v4", "engine": "ds4", "model": "~/ds4.gguf"},
            ]
        }
    )
    profiles = config.profiles()
    assert profiles["studio-model"].wire_model == "org/model"
    assert profiles["deepseek-v4"].wire_model == "deepseek-v4"
    assert profiles["deepseek-v4"].key.engine == EngineName.DS4
    assert Endpoint.RESPONSES in profiles["deepseek-v4"].capabilities


def test_profiles_allow_safe_dotted_legacy_aliases() -> None:
    config = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "lfm2.5-8b-a1b",
                    "engine": "llama.cpp",
                    "model": "/models/lfm.gguf",
                    "served_model_name": "lfm2.5-8b-a1b",
                },
            ]
        }
    )
    profiles = config.profiles()

    assert profiles["lfm2.5-8b-a1b"].wire_model == "lfm2.5-8b-a1b"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("LFM2.5-8B-A1B-GGUF", "lfm2.5-8b-a1b"),
        ("Qwen3-Coder-Next_MLX", "qwen3-coder-next"),
        ("gemma-4-26B-A4B-it.safetensors", "gemma-4-26b-a4b-it"),
    ],
)
def test_suggested_alias_omits_weight_format(name: str, expected: str) -> None:
    assert suggested_model_alias(name) == expected


def test_load_digest_changes_with_effective_options() -> None:
    base = {
        "alias": "deepseek-v4",
        "engine": "ds4",
        "model": "/models/ds4.gguf",
    }
    first = MacConfig.model_validate(
        {"models": [{**base, "load": {"context_length": 100_000}}]}
    ).profiles()["deepseek-v4"]
    second = MacConfig.model_validate(
        {"models": [{**base, "load": {"context_length": 200_000}}]}
    ).profiles()["deepseek-v4"]
    assert first.key.load_config_digest != second.key.load_config_digest


def test_duplicate_or_conflicting_ports_are_rejected() -> None:
    with pytest.raises(ValueError, match="ports must be distinct"):
        MacConfig.model_validate(
            {"engines": {"omlx": {"base_url": "http://127.0.0.1:1240"}}}
        )


def test_omlx_rejects_process_load_options() -> None:
    with pytest.raises(ValueError, match="oMLX load settings"):
        MacConfig.model_validate(
            {
                "models": [
                    {
                        "alias": "glm",
                        "engine": "omlx",
                        "model": "GLM",
                        "load": {"context_length": 4096},
                    }
                ]
            }
        )


def test_inner_engine_urls_must_be_loopback_and_secret_free() -> None:
    with pytest.raises(ValueError, match="loopback"):
        MacConfig.model_validate(
            {"engines": {"omlx": {"base_url": "http://192.168.1.2:17322"}}}
        )
    with pytest.raises(ValueError, match="credentials"):
        MacConfig.model_validate(
            {
                "engines": {
                    "lmstudio": {
                        "base_url": "http://token:secret@127.0.0.1:1234"
                    }
                }
            }
        )


def test_ds4_rejects_lmstudio_only_load_options() -> None:
    with pytest.raises(ValueError, match="not supported by DS4"):
        MacConfig.model_validate(
            {
                "models": [
                    {
                        "alias": "deepseek",
                        "engine": "ds4",
                        "model": "/models/ds4.gguf",
                        "load": {"flash_attention": True},
                    }
                ]
            }
        )


def test_mflux_image_profile_resolves_load_identity_and_defaults() -> None:
    config = MacConfig.model_validate(
        {
            "engines": {"mflux": {"enabled": True}},
            "models": [
                {
                    "alias": "krea-2-turbo",
                    "engine": "mflux",
                    "model": "krea/Krea-2-Turbo",
                    "kind": "image",
                    "image": {
                        "family": "krea-2",
                        "quantize": 8,
                        "num_inference_steps": 8,
                        "guidance_scale": 1,
                    },
                }
            ],
        }
    )
    target = config.profiles()["krea-2-turbo"]
    assert target.key.engine == EngineName.MFLUX
    assert target.kind == ModelKind.IMAGE
    assert target.wire_model == "krea-2-turbo"
    assert target.load_options == {"family": "krea-2", "quantize": 8}
    assert target.image_defaults["num_inference_steps"] == 8
    assert target.capabilities == frozenset({Endpoint.IMAGES_GENERATIONS})


def test_mflux_accepts_each_bundled_text_to_image_family() -> None:
    families = {
        "schnell",
        "dev",
        "krea-dev",
        "flux2-klein-4b",
        "flux2-klein-9b",
        "flux2-klein-9b-kv",
        "flux2-klein-base-4b",
        "flux2-klein-base-9b",
        "qwen-image",
        "krea-2",
        "fibo",
        "fibo-lite",
        "z-image",
        "z-image-turbo",
        "ernie-image",
        "ernie-image-turbo",
        "ideogram-4-fp8",
    }
    for family in families:
        profile = ImageProfileConfig(family=family)
        assert profile.family == family


def test_disabled_engine_profile_is_retained_but_not_resolved() -> None:
    config = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "qwen-image",
                    "engine": "mflux",
                    "model": "Qwen/Qwen-Image",
                    "kind": "image",
                    "image": {"family": "qwen-image"},
                }
            ]
        }
    )

    assert config.models[0].enabled is True
    assert config.profiles() == {}


def test_storage_scope_uses_only_an_opaque_sha256_identifier() -> None:
    config = MacConfig.model_validate(
        {
            "storage": {
                "default": "athena-models",
                "locations": [
                    {
                        "name": "athena-models",
                        "path": "/Volumes/Athena/models",
                        "scope_id": "A" * 64,
                    }
                ],
            }
        }
    )
    assert config.storage.locations[0].scope_id == "a" * 64

    with pytest.raises(ValueError, match="scope_id"):
        MacConfig.model_validate(
            {
                "storage": {
                    "default": "models",
                    "locations": [
                        {
                            "name": "models",
                            "path": "/Volumes/Athena/models",
                            "scope_id": "bookmark-bytes-do-not-belong-in-yaml",
                        }
                    ],
                }
            }
        )
