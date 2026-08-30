from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest
import yaml

import mnemosyne_macos.config as config_module
from mnemosyne_macos.config import (
    ConfigError,
    ImageProfileConfig,
    MacConfig,
    ModelProfile,
    StorageLocationConfig,
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
    assert config.models == []
    assert config.profiles() == {}
from mnemosyne_macos.models import (
    Endpoint,
    EngineName,
    ModelKind,
    effective_load_identity,
)
from mnemosyne_macos.runtime import recommended_interactive_context_length


def test_defaults_replace_the_legacy_sidecar_port() -> None:
    config = MacConfig()
    assert config.schema_version == 6
    assert config.server.inference_port == 1240
    assert config.server.control_port == 17321
    assert config.engines.llama_cpp.port == 17325
    assert config.engines.llama_cpp.enabled is True
    assert config.engines.omlx.base_url.endswith(":17322")
    assert config.engines.omlx.enabled is False
    assert config.engines.ds4.port == 17323
    assert config.engines.ds4.enabled is False
    assert config.engines.mflux.port == 17324
    assert config.engines.mflux.enabled is False
    assert config.engines.ds4.process_state_path.endswith("/state/ds4-process.json")
    assert config.token_sidecar.enabled is True
    assert config.token_sidecar.node_id == ""
    assert config.server.max_concurrency is None
    assert config.server.max_queue_depth == 128
    assert config.server.idle_unload_seconds is None
    assert config.catalog.enabled is False
    assert config.catalog.trusted_keys == []
    assert config.server.fleet_api_key_env == "FLEET_API_KEY"
    assert (
        config.server.fleet_inference_api_key_env
        == "FLEET_INFERENCE_API_KEY"
    )


def test_catalog_policy_is_strict_complete_and_bounded() -> None:
    key = {
        "key_id": "release-2026-a",
        "public_key_env": "MNEMOSYNE_CATALOG_PUBLIC_KEY_2026_A",
    }
    enabled = MacConfig.model_validate(
        {
            "catalog": {
                "enabled": True,
                "update_origin": "https://catalog.mnemosyne.example",
                "update_path": "/v1/apple-silicon/catalog.json",
                "trusted_keys": [key],
            }
        }
    )
    assert enabled.catalog.enabled is True
    assert enabled.catalog.update_interval_seconds == 3600

    invalid_policies = [
        {"enabled": True},
        {
            "enabled": True,
            "update_origin": "http://catalog.mnemosyne.example",
            "update_path": "/v1/catalog.json",
            "trusted_keys": [key],
        },
        {
            "enabled": True,
            "update_origin": "https://Catalog.mnemosyne.example",
            "update_path": "/v1/catalog.json",
            "trusted_keys": [key],
        },
        {
            "enabled": True,
            "update_origin": "https://catalog.mnemosyne.example",
            "update_path": "/v1/../catalog.json",
            "trusted_keys": [key],
        },
        {
            "enabled": True,
            "update_origin": "https://catalog.mnemosyne.example",
            "update_path": "/v1/catalog.json",
            "update_interval_seconds": 299,
            "trusted_keys": [key],
        },
        {
            "enabled": True,
            "update_origin": "https://catalog.mnemosyne.example",
            "update_path": "/v1/catalog.json",
            "trusted_keys": [key, key],
        },
    ]
    for policy in invalid_policies:
        with pytest.raises(ValueError):
            MacConfig.model_validate({"catalog": policy})


def test_interactive_context_defaults_bound_extreme_model_metadata() -> None:
    assert recommended_interactive_context_length(None) == 32_768
    assert recommended_interactive_context_length(8_192) == 8_192
    assert recommended_interactive_context_length(131_072) == 65_536
    assert recommended_interactive_context_length(1_048_576) == 65_536


def test_packaged_example_has_the_intentional_v1_runtime_topology() -> None:
    payload = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    packaged = MacConfig.model_validate(payload)

    assert packaged.server.inference_port == 1240
    assert packaged.server.control_port == 17321
    assert packaged.engines.llama_cpp.enabled is True
    assert packaged.engines.omlx.enabled is False
    assert packaged.engines.ds4.enabled is False
    assert packaged.engines.mflux.enabled is False


def test_legacy_mac_node_placeholder_migrates_to_automatic_identity() -> None:
    config = MacConfig.model_validate(
        {"token_sidecar": {"enabled": True, "node_id": "mnemosyne-mac"}}
    )
    assert config.token_sidecar.node_id == ""


def test_parse_config_migrates_v1_lmstudio_profiles_to_inert_import_records() -> None:
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

    assert config.schema_version == 6
    assert config.models == []
    assert [
        model.alias for model in config.migration.legacy_lmstudio_profiles
    ] == ["local-model"]
    assert not hasattr(config.engines, "lmstudio")


def test_v2_configuration_migrates_to_v6_concurrency_defaults() -> None:
    config = MacConfig.model_validate({"schema_version": 2})

    assert config.schema_version == 6
    assert config.server.max_concurrency is None
    assert config.server.max_queue_depth == 128


def test_v5_context_length_migrates_to_an_explicit_context_policy() -> None:
    config = MacConfig.model_validate(
        {
            "schema_version": 5,
            "engines": {"llama_cpp": {"enabled": True}},
            "models": [
                {
                    "alias": "qwen",
                    "engine": "llama.cpp",
                    "model": "/models/qwen.gguf",
                    "load": {"context_length": 65_536},
                }
            ],
        }
    )

    profile = config.models[0]
    assert config.schema_version == 6
    assert profile.context.mode == "fixed"
    assert profile.context.fixed_tokens == 65_536
    assert config.profiles()["qwen"].requested_context_length == 65_536


def test_omlx_context_policy_uses_the_manager_contract_not_load_options() -> None:
    config = MacConfig.model_validate(
        {
            "engines": {"omlx": {"enabled": True}},
            "models": [
                {
                    "alias": "qwen",
                    "engine": "omlx",
                    "model": "owner/qwen",
                    "context": {
                        "mode": "native",
                        "native_tokens": 262_144,
                    },
                }
            ],
        }
    )

    target = config.profiles()["qwen"]
    assert target.requested_context_length == 262_144
    assert target.native_context_length == 262_144
    assert "context_length" not in target.load_options
    assert config.server.fleet_api_key_env == "FLEET_API_KEY"
    assert (
        config.server.fleet_inference_api_key_env
        == "FLEET_INFERENCE_API_KEY"
    )


@pytest.mark.parametrize(
    ("server", "field"),
    [
        ({"max_concurrency": 0}, "max_concurrency"),
        ({"max_queue_depth": 0}, "max_queue_depth"),
    ],
)
def test_concurrency_limits_must_be_positive(server, field: str) -> None:
    with pytest.raises(ValueError, match=field):
        MacConfig.model_validate({"server": server})


def test_parse_config_removes_obsolete_v1_num_experts_from_retained_profiles(
) -> None:
    config = parse_config(
        """
        schema_version: 1
        engines:
          lmstudio:
            enabled: false
          omlx:
            enabled: true
        models:
          - alias: omlx-model
            engine: omlx
            model: publisher/omlx-model
            load:
              num_experts: null
          - alias: llama-model
            engine: llama.cpp
            model: /models/llama-model.gguf
            load:
              num_experts: null
              threads: 8
        """,
        source="v1 configuration",
    )

    assert config.schema_version == 6
    assert [profile.alias for profile in config.models] == [
        "omlx-model",
        "llama-model",
    ]
    assert config.models[1].load.threads == 8


def test_parse_config_reports_source_for_invalid_yaml() -> None:
    with pytest.raises(ConfigError, match="in-memory configuration"):
        parse_config("models: [", source="in-memory configuration")


def test_omlx_model_directory_round_trip_preserves_nested_symlink_spelling(
    tmp_path,
) -> None:
    physical_root = tmp_path / "physical-volume"
    (physical_root / "deep" / "models").mkdir(parents=True)
    selected_alias = tmp_path / "selected-volume"
    selected_alias.symlink_to(physical_root, target_is_directory=True)
    lexical_directory = str(selected_alias / "deep" / "models")
    path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "engines": {
                "omlx": {
                    "enabled": True,
                    "model_directories": [lexical_directory],
                }
            }
        }
    )

    assert config.engines.omlx.model_directories == [lexical_directory]
    assert lexical_directory != str(Path(lexical_directory).resolve())
    save_config(config, path)
    restored = load_config(path)
    assert restored.engines.omlx.model_directories == [lexical_directory]


def test_omlx_model_directories_reject_distinct_lexical_aliases_of_one_target(
    tmp_path,
) -> None:
    physical_root = tmp_path / "physical-volume"
    target = physical_root / "deep" / "models"
    target.mkdir(parents=True)
    selected_alias = tmp_path / "selected-volume"
    selected_alias.symlink_to(physical_root, target_is_directory=True)
    lexical_alias = str(selected_alias / "deep" / "models")

    with pytest.raises(ValueError, match="model directories must be unique"):
        MacConfig.model_validate(
            {
                "engines": {
                    "omlx": {
                        "enabled": True,
                        "model_directories": [str(target), lexical_alias],
                    }
                }
            }
        )


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


def test_save_config_fsyncs_file_then_replaces_then_fsyncs_parent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "settings" / "config.yaml"
    events: list[str] = []
    real_fsync = config_module.os.fsync
    real_replace = config_module.os.replace

    def tracked_fsync(descriptor: int) -> None:
        kind = (
            "directory_fsync"
            if stat.S_ISDIR(os.fstat(descriptor).st_mode)
            else "file_fsync"
        )
        events.append(kind)
        real_fsync(descriptor)

    def tracked_replace(source, destination) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(config_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(config_module.os, "replace", tracked_replace)

    config = MacConfig()
    save_config(config, path)

    assert events == ["file_fsync", "replace", "directory_fsync"]
    assert load_config(path) == config


def test_save_config_closes_parent_descriptor_and_reports_directory_fsync_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "settings" / "config.yaml"
    real_fsync = config_module.os.fsync
    failed_descriptor: int | None = None

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal failed_descriptor
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed_descriptor = descriptor
            raise OSError("parent directory fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(config_module.os, "fsync", fail_directory_fsync)

    config = MacConfig()
    with pytest.raises(OSError, match="parent directory fsync failed"):
        save_config(config, path)

    assert failed_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(failed_descriptor)
    # The atomic replace already completed, but the caller correctly receives
    # the durability failure and no temporary file is stranded.
    assert load_config(path) == config
    assert not list(path.parent.glob(".*.tmp"))


def test_ds4_process_state_path_is_configurable(tmp_path) -> None:
    state_path = tmp_path / "owned-ds4.json"
    config = MacConfig.model_validate(
        {"engines": {"ds4": {"process_state_path": str(state_path)}}}
    )
    assert config.engines.ds4.process_state_path == str(state_path)


@pytest.mark.parametrize(
    ("filename", "wire_model"),
    [
        ("DeepSeek-V4-Flash-IQ2XXS-0731.gguf", "deepseek-v4-flash"),
        ("DeepSeek-V4-Pro-IQ2XXS-imatrix.gguf", "deepseek-v4-pro"),
        ("GLM-5.2-UD-Q2_K_RoutedQ2K.gguf", "glm-5.2"),
        ("GLM-5.3-Flash-Q2.gguf", "glm-5.3-flash"),
    ],
)
def test_ds4_uses_upstream_canonical_wire_model(
    filename: str, wire_model: str
) -> None:
    profile = ModelProfile(
        alias="local-ds4",
        engine=EngineName.DS4,
        model=f"/models/{filename}",
        # Older app-generated profiles persisted the public alias here. DS4
        # accepts only its family aliases, so resolution must repair it.
        served_model_name="local-ds4",
    )

    assert profile.resolve().wire_model == wire_model


def test_profiles_resolve_engine_specific_wire_names() -> None:
    config = MacConfig.model_validate(
        {
            "engines": {"ds4": {"enabled": True}},
            "models": [
                {"alias": "deepseek-v4", "engine": "ds4", "model": "~/ds4.gguf"},
            ]
        }
    )
    profiles = config.profiles()
    assert profiles["deepseek-v4"].wire_model == "deepseek-v4-flash"
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


def test_llama_generation_contract_defaults_without_messages_but_allows_opt_in() -> None:
    config = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "portable",
                    "engine": "llama.cpp",
                    "model": "/models/portable.gguf",
                },
                {
                    "alias": "anthropic",
                    "engine": "llama.cpp",
                    "model": "/models/anthropic.gguf",
                    "capabilities": [
                        "chat/completions",
                        "completions",
                        "responses",
                        "messages",
                    ],
                },
            ]
        }
    )
    profiles = config.profiles()

    assert profiles["portable"].capabilities == frozenset(
        {
            Endpoint.CHAT_COMPLETIONS,
            Endpoint.COMPLETIONS,
            Endpoint.RESPONSES,
        }
    )
    assert Endpoint.MESSAGES in profiles["anthropic"].capabilities


def test_request_only_image_defaults_do_not_change_resident_load_identity() -> None:
    def target(width: int):
        return MacConfig.model_validate(
            {
                "engines": {"mflux": {"enabled": True}},
                "models": [
                    {
                        "alias": "image",
                        "engine": "mflux",
                        "model": "publisher/image",
                        "kind": "image",
                        "served_model_name": "image-wire",
                        "image": {
                            "family": "image-family",
                            "quantize": 8,
                            "width": width,
                        },
                    }
                ],
            }
        ).profiles()["image"]

    assert effective_load_identity(target(1024)) == effective_load_identity(
        target(768)
    )


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
        {
            "engines": {"ds4": {"enabled": True}},
            "models": [{**base, "load": {"context_length": 100_000}}],
        }
    ).profiles()["deepseek-v4"]
    second = MacConfig.model_validate(
        {
            "engines": {"ds4": {"enabled": True}},
            "models": [{**base, "load": {"context_length": 200_000}}],
        }
    ).profiles()["deepseek-v4"]
    assert first.key.load_config_digest != second.key.load_config_digest


def test_duplicate_or_conflicting_ports_are_rejected() -> None:
    with pytest.raises(ValueError, match="ports must be distinct"):
        MacConfig.model_validate(
            {
                "engines": {
                    "omlx": {
                        "enabled": True,
                        "base_url": "http://127.0.0.1:1240",
                    }
                }
            }
        )


@pytest.mark.parametrize("engine", ["mlxcel", "mistral_rs"])
def test_retired_engine_settings_are_parseable_but_inert(engine: str) -> None:
    config = MacConfig.model_validate(
        {
            "engines": {
                engine: {
                    "enabled": True,
                    "port": 17321,
                }
            }
        }
    )

    parsed = EngineName.MLXCEL if engine == "mlxcel" else EngineName.MISTRAL_RS
    assert config.engine_enabled(parsed) is False
    legacy = config.engines.mlxcel if engine == "mlxcel" else config.engines.mistral_rs
    assert legacy.enabled is False
    dumped_engines = config.model_dump(mode="json")["engines"]
    assert "mlxcel" not in dumped_engines
    assert "mistral_rs" not in dumped_engines


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
                    "omlx": {
                        "base_url": "http://token:secret@127.0.0.1:17322"
                    }
                }
            }
        )


def test_ds4_rejects_llama_cpp_only_load_options() -> None:
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


def test_ds4_parallel_slots_are_typed() -> None:
    config = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "deepseek",
                    "engine": "ds4",
                    "model": "/models/ds4.gguf",
                    "load": {"parallel": 4},
                }
            ]
        }
    )
    assert config.models[0].resolve().load_options["parallel"] == 4


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


def test_v4_profile_candidates_migrate_to_v6_and_remain_fixed_by_default(
    tmp_path,
) -> None:
    config = MacConfig.model_validate(
        {
            "schema_version": 4,
            "engines": {
                "omlx": {"enabled": True},
                "mlxcel": {"enabled": True},
                "mistral_rs": {"enabled": True},
            },
            "models": [
                {
                    "alias": "qwen",
                    "engine": "omlx",
                    "model": "qwen",
                    "alternatives": [
                        {
                            "engine": "mlxcel",
                            "model": str(tmp_path / "mlx"),
                            "load": {"parallel": 4},
                        },
                        {
                            "engine": "mistral.rs",
                            "model": str(tmp_path / "safetensors"),
                        },
                    ],
                }
            ],
        }
    )

    assert config.models[0].selection.mode == "fixed"
    assert config.schema_version == 6
    assert config.profiles()["qwen"].key.engine == EngineName.OMLX
    candidates = config.profile_candidates()["qwen"]
    assert [candidate.key.engine for candidate in candidates] == [EngineName.OMLX]
    assert [alternative.engine for alternative in config.models[0].alternatives] == [
        EngineName.MLXCEL,
        EngineName.MISTRAL_RS,
    ]


def test_benchmark_selection_requires_an_alternative() -> None:
    with pytest.raises(ValueError, match="requires at least one engine alternative"):
        MacConfig.model_validate(
            {
                "models": [
                    {
                        "alias": "solo",
                        "engine": "llama.cpp",
                        "model": "/models/solo.gguf",
                        "selection": {"mode": "benchmark"},
                    }
                ]
            }
        )


def test_pinned_selection_accepts_a_declared_engine_without_benchmark_evidence(
    tmp_path,
) -> None:
    config = MacConfig.model_validate(
        {
            "engines": {
                "omlx": {"enabled": True},
                "mlxcel": {"enabled": True},
            },
            "models": [
                {
                    "alias": "qwen",
                    "engine": "omlx",
                    "model": "qwen",
                    "alternatives": [
                        {
                            "engine": "mlxcel",
                            "model": str(tmp_path / "mlx"),
                        }
                    ],
                    "selection": {
                        "mode": "pinned",
                        "pinned_engine": "mlxcel",
                    },
                }
            ],
        }
    )

    assert config.models[0].selection.pinned_engine == EngineName.MLXCEL


def test_pinned_selection_rejects_an_engine_outside_the_candidate_set() -> None:
    with pytest.raises(ValueError, match="primary engine or a declared alternative"):
        MacConfig.model_validate(
            {
                "models": [
                    {
                        "alias": "qwen",
                        "engine": "omlx",
                        "model": "qwen",
                        "selection": {
                            "mode": "pinned",
                            "pinned_engine": "mlxcel",
                        },
                    }
                ]
            }
        )


def test_benchmark_selection_requires_two_chat_capable_candidates() -> None:
    with pytest.raises(ValueError, match="two enabled chat-capable"):
        MacConfig.model_validate(
            {
                "engines": {
                    "omlx": {"enabled": True},
                    "llama_cpp": {"enabled": True},
                },
                "models": [
                    {
                        "alias": "embed",
                        "engine": "omlx",
                        "model": "embed-mlx",
                        "capabilities": ["embeddings"],
                        "alternatives": [
                            {
                                "engine": "llama.cpp",
                                "model": "/models/embed.gguf",
                                "capabilities": ["embeddings"],
                            }
                        ],
                        "selection": {"mode": "benchmark"},
                    }
                ],
            }
        )


def test_engine_alternatives_require_one_candidate_per_engine() -> None:
    with pytest.raises(ValueError, match="at most one candidate per engine"):
        MacConfig.model_validate(
            {
                "models": [
                    {
                        "alias": "qwen",
                        "engine": "mlxcel",
                        "model": "/models/qwen-primary",
                        "alternatives": [
                            {
                                "engine": "mlxcel",
                                "model": "/models/qwen-second",
                            }
                        ],
                    }
                ]
            }
        )


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


def test_storage_path_expands_to_absolute_without_resolving_selected_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    physical = tmp_path / "Volumes" / "Athena" / "models"
    physical.mkdir(parents=True)
    home.mkdir()
    selected = home / "selected-models"
    selected.symlink_to(physical, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))

    location = StorageLocationConfig(
        name="athena-models",
        path="~/selected-models/nested",
        volume_uuid="volume-uuid-exact",
        scope_id="A" * 64,
    )

    assert location.path == str(selected / "nested")
    assert location.path != str((physical / "nested").resolve())
    assert location.volume_uuid == "volume-uuid-exact"
    assert location.scope_id == "a" * 64


def test_app_owned_internal_storage_discards_an_obsolete_bookmark() -> None:
    internal = Path.home() / "Library" / "Application Support" / "Mnemosyne" / "models"
    config = MacConfig.model_validate(
        {
            "storage": {
                "default": "internal",
                "locations": [
                    {
                        "name": "internal",
                        "path": str(internal),
                        "scope_id": "a" * 64,
                    }
                ],
            }
        }
    )

    assert config.storage.locations[0].scope_id is None
