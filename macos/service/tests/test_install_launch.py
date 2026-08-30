from __future__ import annotations

from dataclasses import asdict

import pytest

from mnemosyne_macos.install_launch import (
    DS4InstallLaunch,
    InstallLaunchError,
    LlamaCppInstallLaunch,
    OMLXInstallLaunch,
    install_launch_from_json,
    install_launch_json,
    validate_install_launch,
)


@pytest.mark.parametrize(
    ("engine", "payload", "expected_type"),
    [
        (
            "llama.cpp",
            {
                "engine": "llama.cpp",
                "parallel_slots": 2,
                "gpu_offload": "all",
                "flash_attention": "automatic",
            },
            LlamaCppInstallLaunch,
        ),
        (
            "omlx",
            {
                "engine": "omlx",
                "scheduler_slots": 3,
                "memory_guard": "required",
            },
            OMLXInstallLaunch,
        ),
        (
            "ds4",
            {
                "engine": "ds4",
                "batched_sessions": 4,
                "execution_mode": "single-node",
            },
            DS4InstallLaunch,
        ),
    ],
)
def test_launch_contract_round_trips_as_canonical_closed_json(
    engine: str,
    payload: dict[str, object],
    expected_type: type,
) -> None:
    encoded = install_launch_json(engine, payload)
    assert encoded is not None
    assert " " not in encoded
    decoded = install_launch_from_json(engine, encoded)
    assert isinstance(decoded, expected_type)
    assert asdict(decoded) == payload


@pytest.mark.parametrize(
    ("engine", "payload"),
    [
        (
            "llama.cpp",
            {
                "engine": "llama.cpp",
                "parallel_slots": True,
                "gpu_offload": "all",
                "flash_attention": "automatic",
            },
        ),
        (
            "llama.cpp",
            {
                "engine": "llama.cpp",
                "parallel_slots": 1,
                "gpu_offload": "some",
                "flash_attention": "automatic",
            },
        ),
        (
            "omlx",
            {
                "engine": "omlx",
                "scheduler_slots": 0,
                "memory_guard": "required",
            },
        ),
        (
            "ds4",
            {
                "engine": "ds4",
                "batched_sessions": 1,
                "execution_mode": "distributed",
            },
        ),
        (
            "llama.cpp",
            {
                "engine": "llama.cpp",
                "parallel_slots": 1,
                "gpu_offload": "automatic",
                "flash_attention": "enabled",
                "extra": "not-closed",
            },
        ),
        (
            "llama.cpp",
            {
                "engine": "omlx",
                "parallel_slots": 1,
                "gpu_offload": "automatic",
                "flash_attention": "enabled",
            },
        ),
    ],
)
def test_launch_contract_rejects_unsupported_or_ambiguous_values(
    engine: str,
    payload: dict[str, object],
) -> None:
    with pytest.raises(InstallLaunchError):
        validate_install_launch(engine, payload)


def test_persisted_launch_json_must_be_canonical() -> None:
    with pytest.raises(InstallLaunchError, match="not_canonical"):
        install_launch_from_json(
            "ds4",
            '{"engine": "ds4", "batched_sessions": 1, '
            '"execution_mode": "single-node"}',
        )

