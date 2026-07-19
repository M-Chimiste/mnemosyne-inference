from __future__ import annotations

import pytest

from mnemosyne_macos.config import MacConfig
from mnemosyne_macos.models import Endpoint, EngineName


def test_defaults_use_dedicated_port_block() -> None:
    config = MacConfig()
    assert config.server.inference_port == 17320
    assert config.server.control_port == 17321
    assert config.engines.omlx.base_url.endswith(":17322")
    assert config.engines.ds4.port == 17323
    assert config.engines.ds4.process_state_path.endswith("/state/ds4-process.json")


def test_ds4_process_state_path_is_configurable(tmp_path) -> None:
    state_path = tmp_path / "owned-ds4.json"
    config = MacConfig.model_validate(
        {"engines": {"ds4": {"process_state_path": str(state_path)}}}
    )
    assert config.engines.ds4.process_state_path == str(state_path)


def test_profiles_resolve_engine_specific_wire_names() -> None:
    config = MacConfig.model_validate(
        {
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
            {"engines": {"omlx": {"base_url": "http://127.0.0.1:17320"}}}
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
