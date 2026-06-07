"""Mnemosyne Inference — pure profile resolver.

Merges config + catalog + defaults into a runtime ResolvedProfile.
See project_docs/plans/phase_1_plan.md §4.5.

Lives here (not in config.py) to keep the dep graph one-way:
  config.py → (nothing)
  catalog.py → config.py
  profiles.py → config.py + catalog.py

Naming rule (deterministic, see plan §"Decoupling 'served name' from
'engine target'"):
  - vLLM rows:      served_model_name = engine_model_path = HF id (or path).
  - llama.cpp rows: served_model_name = alias (stable, user-facing handle);
                    engine_model_path = absolute path to chosen GGUF shard.

The proxy rewrites request bodies' `"model"` field to `served_model_name`
before forwarding upstream, so for llama.cpp we launch llama-server with
`--alias <alias>` and the path stays inside the engine.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from catalog import Catalog
from config import Config, GpuPlan, ModelProfile


class ProfileNotReady(Exception):
    """A managed profile exists but its expected local weights are not ready."""


@dataclass(frozen=True)
class ResolvedProfile:
    alias: str
    served_model_name: str   # forwarded as the `"model"` field upstream
    engine_model_path: str   # passed to vLLM `--model` / llama-server `-m`
    gpus: GpuPlan
    quantization: Optional[str]
    max_model_len: Optional[int]
    gpu_memory_utilization: float
    trust_remote_code: bool
    storage_name: str
    storage_path: str
    extra_args: tuple[str, ...]
    revision: str = "main"
    backend: str = "vllm"   # "vllm" | "llama.cpp"
    gguf_filename: Optional[str] = None

    @property
    def model(self) -> str:
        """Back-compat alias for the public-facing model name. Prefer
        `served_model_name` in new code."""
        return self.served_model_name


def _gguf_engine_path(
    storage_path: str,
    hf_model_id: str,
    resolved_sha: Optional[str],
    revision: str,
    gguf_filename: str,
) -> str:
    """Compute the absolute path llama-server should be pointed at.

    The HF cache layout puts the chosen file at:
      <storage_path>/hub/models--<org>--<repo>/snapshots/<sha>/<filename>
    We prefer the resolved snapshot SHA (pinned at install time); when only
    a symbolic revision is available the path still works because hf cache
    derefs refs/<rev> to the same snapshot dir.
    """
    safe = "models--" + hf_model_id.replace("/", "--")
    sha_or_ref = resolved_sha or revision or "main"
    return os.path.join(
        storage_path, "hub", safe, "snapshots", sha_or_ref, gguf_filename,
    )


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
        backend = config_profile.backend
        if backend == "llama.cpp":
            if not config_profile.gguf_filename:
                # Pydantic validator catches this, but guard defensively.
                raise ValueError(
                    f"alias '{alias}' has backend=llama.cpp but no gguf_filename"
                )
            row = catalog.get_model(alias) if catalog is not None else None
            if row is None or row.cache_path is None or row.status != "installed":
                raise ProfileNotReady(
                    f"alias '{alias}' is not ready; install/reconcile the selected GGUF first"
                )
            served = alias
            engine_path = os.path.join(row.cache_path, config_profile.gguf_filename)
            if not os.path.isfile(engine_path):
                raise ProfileNotReady(
                    f"alias '{alias}' is not ready; missing GGUF file '{config_profile.gguf_filename}'"
                )
        else:
            served = config_profile.model
            engine_path = config_profile.model
        return ResolvedProfile(
            alias=config_profile.alias,
            served_model_name=served,
            engine_model_path=engine_path,
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
            revision=config_profile.revision,
            backend=backend,
            gguf_filename=config_profile.gguf_filename,
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
            # Prefer the resolved snapshot SHA when set so the resident engine
            # pins to the exact downloaded weights even if a moving ref like
            # 'main' has advanced on the Hub. Falls back to the symbolic
            # revision (default "main") for rows that have not been installed.
            revision = (
                row.resolved_sha
                if row.resolved_sha
                else (row.revision or "main")
            )
            backend = row.backend or "vllm"
            if backend == "llama.cpp":
                if not row.gguf_filename:
                    raise ValueError(
                        f"alias '{alias}' has backend=llama.cpp but no gguf_filename"
                    )
                served = row.alias
                engine_path = _gguf_engine_path(
                    storage_locations[row.storage_location],
                    row.hf_model_id,
                    resolved_sha=row.resolved_sha,
                    revision=row.revision or "main",
                    gguf_filename=row.gguf_filename,
                )
            else:
                served = row.hf_model_id
                engine_path = row.hf_model_id
            return ResolvedProfile(
                alias=row.alias,
                served_model_name=served,
                engine_model_path=engine_path,
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
                revision=revision,
                backend=backend,
                gguf_filename=row.gguf_filename,
            )

    raise KeyError(alias)
