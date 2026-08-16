from pathlib import Path

import pytest

from mnemosyne_macos.config import MLXcelConfig, MistralRSConfig, ModelProfile
from mnemosyne_macos.engines.base import AdapterError
from mnemosyne_macos.engines.managed_apple import MLXcelAdapter, MistralRSAdapter
from mnemosyne_macos.models import Endpoint, EngineName, ResidentInstance


def test_mlxcel_argv_owns_model_loopback_port_context_and_parallel(tmp_path: Path) -> None:
    binary = tmp_path / "mlxcel-server"
    config = MLXcelConfig(
        binary=str(binary),
        working_directory=str(tmp_path),
        process_state_path=str(tmp_path / "state.json"),
    )
    target = ModelProfile(
        alias="qwen",
        engine=EngineName.MLXCEL,
        model=str(tmp_path / "model"),
        load={"context_length": 32768, "parallel": 3},
    ).resolve()
    adapter = MLXcelAdapter(config)
    try:
        assert adapter._build_argv(config, target) == [  # noqa: SLF001
            str(binary),
            "-m",
            str(tmp_path / "model"),
            "--host",
            "127.0.0.1",
            "--port",
            "17326",
            "--parallel",
            "3",
            "--ctx-size",
            "32768",
        ]
        assert adapter.capacity_hint(target).limit == 3
    finally:
        import asyncio

        asyncio.run(adapter._client.aclose())  # noqa: SLF001


def test_mlxcel_uses_reported_wire_model(tmp_path: Path) -> None:
    config = MLXcelConfig(
        working_directory=str(tmp_path),
        process_state_path=str(tmp_path / "state.json"),
    )
    target = ModelProfile(
        alias="public",
        engine=EngineName.MLXCEL,
        model=str(tmp_path / "model"),
    ).resolve()
    adapter = MLXcelAdapter(config)
    try:
        handle = adapter._handle(  # noqa: SLF001
            target,
            ResidentInstance(
                engine=EngineName.MLXCEL,
                canonical_model_id=target.key.canonical_model_id,
                raw={"reported_model": "mlx-community/Qwen"},
            ),
        )
        assert handle.wire_model == "mlx-community/Qwen"
    finally:
        import asyncio

        asyncio.run(adapter._client.aclose())  # noqa: SLF001


def test_mistral_rs_argv_is_offline_loopback_and_ui_free(tmp_path: Path) -> None:
    binary = tmp_path / "mistralrs"
    config = MistralRSConfig(
        binary=str(binary),
        working_directory=str(tmp_path),
        process_state_path=str(tmp_path / "state.json"),
    )
    target = ModelProfile(
        alias="qwen",
        engine=EngineName.MISTRAL_RS,
        model=str(tmp_path / "model"),
    ).resolve()
    adapter = MistralRSAdapter(config)
    try:
        assert adapter._build_argv(config, target) == [  # noqa: SLF001
            str(binary),
            "--token-source",
            "none",
            "serve",
            "-m",
            str(tmp_path / "model"),
            "--host",
            "127.0.0.1",
            "--port",
            "17327",
            "--no-ui",
        ]
        route = adapter.route(
            adapter._handle(  # noqa: SLF001
                target,
                ResidentInstance(
                    engine=EngineName.MISTRAL_RS,
                    canonical_model_id=target.key.canonical_model_id,
                ),
            ),
            Endpoint.MESSAGES,
        )
        assert route.wire_model == "default"
        assert route.usage_dialect == "anthropic"
    finally:
        import asyncio

        asyncio.run(adapter._client.aclose())  # noqa: SLF001


def test_preview_adapters_reject_manager_owned_extra_args(tmp_path: Path) -> None:
    target = ModelProfile(
        alias="qwen",
        engine=EngineName.MLXCEL,
        model=str(tmp_path / "model"),
        load={"extra_args": ["--parallel=99"]},
    ).resolve()
    config = MLXcelConfig(working_directory=str(tmp_path))
    adapter = MLXcelAdapter(config)
    try:
        with pytest.raises(AdapterError, match="manager-owned option --parallel"):
            adapter._build_argv(config, target)  # noqa: SLF001
    finally:
        import asyncio

        asyncio.run(adapter._client.aclose())  # noqa: SLF001
