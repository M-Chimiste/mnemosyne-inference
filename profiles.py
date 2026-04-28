"""Mnemosyne Inference — pure profile resolver.

Merges config + catalog + defaults into a runtime ResolvedProfile.
See project_docs/plans/phase_1_plan.md §4.5.

Lives here (not in config.py) to keep the dep graph one-way:
  config.py → (nothing)
  catalog.py → config.py
  profiles.py → config.py + catalog.py
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from catalog import Catalog
from config import Config, GpuPlan, ModelProfile


@dataclass(frozen=True)
class ResolvedProfile:
    alias: str
    model: str
    gpus: GpuPlan
    quantization: Optional[str]
    max_model_len: Optional[int]
    gpu_memory_utilization: float
    trust_remote_code: bool
    storage_name: str
    storage_path: str
    extra_args: tuple[str, ...]


def resolve_profile(
    alias: str,
    config: Config,
    catalog: Optional[Catalog] = None,
) -> ResolvedProfile:
    """Resolve an alias into a fully-merged ResolvedProfile.

    Lookup order (PRD §5.1: config wins on conflict):
      1. config.models[alias]
      2. catalog row with source='ui_install' (if catalog provided)
      3. raise KeyError(alias)
    """
    storage_locations = {loc.name: loc.path for loc in config.storage.locations}
    defaults = config.defaults

    config_profile: Optional[ModelProfile] = None
    for m in config.models:
        if m.alias == alias:
            config_profile = m
            break

    if config_profile is not None:
        storage_name = (
            config_profile.storage if config_profile.storage is not None
            else config.storage.default
        )
        storage_path = storage_locations[storage_name]
        return ResolvedProfile(
            alias=config_profile.alias,
            model=config_profile.model,
            gpus=config_profile.gpus,
            quantization=config_profile.quantization,
            max_model_len=(
                config_profile.max_model_len
                if config_profile.max_model_len is not None
                else defaults.max_model_len
            ),
            gpu_memory_utilization=defaults.gpu_memory_utilization,
            trust_remote_code=defaults.trust_remote_code,
            storage_name=storage_name,
            storage_path=storage_path,
            extra_args=tuple(config_profile.extra_args),
        )

    if catalog is not None:
        row = catalog.get_model(alias)
        if row is not None and row.source == "ui_install":
            if row.storage_location not in storage_locations:
                raise KeyError(
                    f"alias '{alias}' uses storage '{row.storage_location}' "
                    "which is no longer declared in config"
                )
            gpus = json.loads(row.gpus)
            extra_args = tuple(json.loads(row.extra_args)) if row.extra_args else ()
            return ResolvedProfile(
                alias=row.alias,
                model=row.hf_model_id,
                gpus=gpus,
                quantization=row.quantization,
                max_model_len=(
                    row.max_model_len
                    if row.max_model_len is not None
                    else defaults.max_model_len
                ),
                gpu_memory_utilization=defaults.gpu_memory_utilization,
                trust_remote_code=defaults.trust_remote_code,
                storage_name=row.storage_location,
                storage_path=storage_locations[row.storage_location],
                extra_args=extra_args,
            )

    raise KeyError(alias)
