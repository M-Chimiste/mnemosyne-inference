"""Closed, immutable launch contracts retained with managed installs.

The signed compatibility catalog is the authority for these values.  Keeping
the parser here independent of the catalog reader makes the install journal
self-validating after restart and prevents loosely typed JSON from reaching a
profile or engine adapter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Literal, Mapping, TypeAlias


class InstallLaunchError(ValueError):
    """A persisted or caller-supplied launch contract is not exactly v1."""


@dataclass(frozen=True, slots=True)
class LlamaCppInstallLaunch:
    engine: Literal["llama.cpp"]
    parallel_slots: int
    gpu_offload: Literal["all", "automatic"]
    flash_attention: Literal["automatic", "disabled", "enabled"]


@dataclass(frozen=True, slots=True)
class OMLXInstallLaunch:
    engine: Literal["omlx"]
    scheduler_slots: int
    memory_guard: Literal["optional", "required"]


@dataclass(frozen=True, slots=True)
class DS4InstallLaunch:
    engine: Literal["ds4"]
    batched_sessions: int
    execution_mode: Literal["single-node"]


InstallLaunchContract: TypeAlias = (
    LlamaCppInstallLaunch | OMLXInstallLaunch | DS4InstallLaunch
)
InstallLaunchInput: TypeAlias = InstallLaunchContract | Mapping[str, Any]


# This key exists only on an in-memory ``ResolvedTarget.load_options`` mapping.
# It is never accepted from YAML and never sent to oMLX.  The durable source is
# the native install journal's canonical ``launch_json``; projecting it onto a
# target makes every coordinator-owned load (including benchmarks) re-check
# the external service contract before oMLX can load weights.
OMLX_TARGET_LAUNCH_KEY = "_mnemosyne_signed_omlx_launch"


_KEYS = {
    "llama.cpp": frozenset(
        {"engine", "parallel_slots", "gpu_offload", "flash_attention"}
    ),
    "omlx": frozenset({"engine", "scheduler_slots", "memory_guard"}),
    "ds4": frozenset({"engine", "batched_sessions", "execution_mode"}),
}


def validate_install_launch(
    engine: str,
    value: InstallLaunchInput,
) -> InstallLaunchContract:
    """Return one typed contract only when every key and value is exact."""

    if isinstance(
        value,
        (LlamaCppInstallLaunch, OMLXInstallLaunch, DS4InstallLaunch),
    ):
        payload: Mapping[str, Any] = asdict(value)
    elif isinstance(value, Mapping):
        payload = value
    else:
        raise InstallLaunchError("install_launch_not_an_object")

    expected_keys = _KEYS.get(engine)
    if expected_keys is None or frozenset(payload) != expected_keys:
        raise InstallLaunchError("install_launch_shape_invalid")
    if payload.get("engine") != engine:
        raise InstallLaunchError("install_launch_engine_mismatch")

    if engine == "llama.cpp":
        parallel_slots = _bounded_slots(payload["parallel_slots"])
        gpu_offload = payload["gpu_offload"]
        flash_attention = payload["flash_attention"]
        if not isinstance(gpu_offload, str) or gpu_offload not in {
            "all",
            "automatic",
        }:
            raise InstallLaunchError("install_launch_gpu_offload_invalid")
        if not isinstance(flash_attention, str) or flash_attention not in {
            "automatic",
            "disabled",
            "enabled",
        }:
            raise InstallLaunchError("install_launch_flash_attention_invalid")
        return LlamaCppInstallLaunch(
            engine="llama.cpp",
            parallel_slots=parallel_slots,
            gpu_offload=gpu_offload,
            flash_attention=flash_attention,
        )

    if engine == "omlx":
        scheduler_slots = _bounded_slots(payload["scheduler_slots"])
        memory_guard = payload["memory_guard"]
        if not isinstance(memory_guard, str) or memory_guard not in {
            "optional",
            "required",
        }:
            raise InstallLaunchError("install_launch_memory_guard_invalid")
        return OMLXInstallLaunch(
            engine="omlx",
            scheduler_slots=scheduler_slots,
            memory_guard=memory_guard,
        )

    batched_sessions = _bounded_slots(payload["batched_sessions"])
    if payload["execution_mode"] != "single-node":
        raise InstallLaunchError("install_launch_execution_mode_invalid")
    return DS4InstallLaunch(
        engine="ds4",
        batched_sessions=batched_sessions,
        execution_mode="single-node",
    )


def install_launch_json(
    engine: str,
    value: InstallLaunchInput | None,
) -> str | None:
    """Encode the canonical database representation for one contract."""

    if value is None:
        return None
    contract = validate_install_launch(engine, value)
    return json.dumps(asdict(contract), sort_keys=True, separators=(",", ":"))


def install_launch_from_json(
    engine: str,
    value: str | None,
) -> InstallLaunchContract | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise InstallLaunchError("install_launch_json_invalid")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InstallLaunchError("install_launch_json_invalid") from exc
    if not isinstance(decoded, dict):
        raise InstallLaunchError("install_launch_json_invalid")
    contract = validate_install_launch(engine, decoded)
    if install_launch_json(engine, contract) != value:
        raise InstallLaunchError("install_launch_json_not_canonical")
    return contract


def install_launch_dict(contract: InstallLaunchContract) -> dict[str, Any]:
    return asdict(contract)


def omlx_target_launch(
    load_options: Mapping[str, Any],
) -> OMLXInstallLaunch | None:
    """Read the manager-private signed oMLX contract from one target.

    Absence identifies an ordinary local oMLX profile.  A present but corrupt
    value is never treated as absence because that would silently downgrade a
    signed managed profile into the legacy unconstrained path.
    """

    if OMLX_TARGET_LAUNCH_KEY not in load_options:
        return None
    contract = validate_install_launch(
        "omlx",
        load_options[OMLX_TARGET_LAUNCH_KEY],
    )
    if not isinstance(contract, OMLXInstallLaunch):  # pragma: no cover - typed guard
        raise InstallLaunchError("install_launch_engine_mismatch")
    return contract


def with_omlx_target_launch(
    load_options: Mapping[str, Any],
    contract: OMLXInstallLaunch,
) -> dict[str, Any]:
    """Return a copy carrying one validated manager-private oMLX contract."""

    validated = validate_install_launch("omlx", contract)
    if not isinstance(validated, OMLXInstallLaunch):  # pragma: no cover - typed guard
        raise InstallLaunchError("install_launch_engine_mismatch")
    result = dict(load_options)
    result[OMLX_TARGET_LAUNCH_KEY] = install_launch_dict(validated)
    return result


def _bounded_slots(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InstallLaunchError("install_launch_slots_invalid")
    if not 1 <= value <= 1024:
        raise InstallLaunchError("install_launch_slots_invalid")
    return value


__all__ = [
    "DS4InstallLaunch",
    "InstallLaunchContract",
    "InstallLaunchError",
    "InstallLaunchInput",
    "LlamaCppInstallLaunch",
    "OMLX_TARGET_LAUNCH_KEY",
    "OMLXInstallLaunch",
    "install_launch_dict",
    "install_launch_from_json",
    "install_launch_json",
    "omlx_target_launch",
    "validate_install_launch",
    "with_omlx_target_launch",
]
