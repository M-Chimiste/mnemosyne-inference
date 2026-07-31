"""Mnemosyne Inference — config schema, YAML/.env loaders, runtime validation.

Single source of truth for the declarative model registry. See
project_docs/phase_1_plan.md §4.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger("vllm-manager.config")

DEFAULT_CONFIG_PATH = "/config/config.yaml"
DEFAULT_ENV_PATH = "/config/.env"

# Lowercase letters, digits, hyphens. Must start with alnum.
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Both forms reserved — '__cache__:' is the canonical synthetic prefix
# (catalog.synthetic_alias); '__cache__/' is rejected as defense-in-depth
# against earlier drafts of this design leaking into user configs.
_RESERVED_PREFIXES = ("__cache__:", "__cache__/")

_CAPABILITY_ALIASES = {
    "chat.completions": "chat.completions",
    "chat/completions": "chat.completions",
    "completions": "completions",
    "embeddings": "embeddings",
    "images.generations": "images.generations",
    "images/generations": "images.generations",
    "messages": "messages",
    "rerank": "rerank",
    "responses": "responses",
}
SUPPORTED_MODEL_CAPABILITIES = frozenset(_CAPABILITY_ALIASES.values())


class ConfigError(Exception):
    """Any non-recoverable problem loading or validating config."""


GpuPlan = Union[Literal["all"], list[int]]


class Server(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inference_port: int = 8000
    admin_port: int = 8001
    inference_bind: str = "0.0.0.0"
    admin_bind: str = "0.0.0.0"
    idle_unload_seconds: int | None = 900
    startup_timeout_seconds: int = 600
    swap_queue_timeout_seconds: int = 300
    # Optional operator ceiling over adapter-derived concurrent request
    # capacity. The engine-derived limit remains authoritative when lower.
    max_concurrency: int | None = Field(default=None, ge=1)
    # Requests beyond active capacity may wait locally only within this
    # bounded FIFO. Nyx maintains its own bounded per-model queues as well.
    max_queue_depth: int = Field(default=128, ge=1, le=100_000)
    image_request_timeout_seconds: int = 1800
    image_max_pixels: int = 4_194_304

    @model_validator(mode="after")
    def _ports_distinct(self) -> "Server":
        if self.inference_port == self.admin_port:
            raise ValueError(
                f"server.inference_port and server.admin_port must differ "
                f"(both = {self.inference_port})"
            )
        if self.image_request_timeout_seconds <= 0:
            raise ValueError("server.image_request_timeout_seconds must be positive")
        if self.image_max_pixels < 4096:
            raise ValueError("server.image_max_pixels must be at least 4096")
        return self


class StorageLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    path: str


class Storage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: str
    locations: list[StorageLocation]

    @field_validator("locations")
    @classmethod
    def _names_unique_and_nonempty(cls, v: list[StorageLocation]) -> list[StorageLocation]:
        if not v:
            raise ValueError("storage.locations must have at least one entry")
        names = [loc.name for loc in v]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate storage location names: {dupes}")
        return v

    @model_validator(mode="after")
    def _default_must_exist(self) -> "Storage":
        names = {loc.name for loc in self.locations}
        if self.default not in names:
            raise ValueError(
                f"storage.default '{self.default}' is not a declared location "
                f"(known: {sorted(names)})"
            )
        return self


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gpu_memory_utilization: float = 0.90
    trust_remote_code: bool = True
    max_model_len: int | None = None


class ImageDefaults(BaseModel):
    """Engine-neutral defaults applied to image-generation requests."""

    model_config = ConfigDict(extra="forbid")
    width: int = Field(default=1024, ge=64, le=4096)
    height: int = Field(default=1024, ge=64, le=4096)
    num_inference_steps: int = Field(default=30, ge=1, le=200)
    guidance_scale: float = Field(default=4.0, ge=0, le=50)
    guidance_parameter: Literal["guidance_scale", "true_cfg_scale"] = "guidance_scale"

    @model_validator(mode="after")
    def _dimensions_are_pipeline_safe(self) -> "ImageDefaults":
        if self.width % 16 or self.height % 16:
            raise ValueError("image width and height must be multiples of 16")
        return self


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str
    model: str
    quantization: str | None = None
    gpus: GpuPlan = "all"
    max_model_len: int | None = None
    storage: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    revision: str = "main"
    # Backend dispatch. Defaults to vLLM so existing configs round-trip.
    # llama.cpp requires gguf_filename to be set (the primary shard for
    # sharded models, the lone file otherwise).
    backend: Literal["vllm", "llama.cpp", "sglang-diffusion"] = "vllm"
    kind: Literal["language", "image"] = "language"
    image: ImageDefaults | None = None
    gguf_filename: str | None = None
    # Optional repository-relative multimodal projector. The resolved profile
    # turns this into a private cache path for llama-server while fleet
    # identity records only this explicit selected file.
    projector_filename: str | None = None
    # Endpoint contract is configurable, but omitted values retain safe
    # engine-specific defaults. Internally the historic dotted spellings are
    # preserved for compatibility; the fleet protocol canonicalizes them.
    capabilities: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _backend_consistency(self) -> "ModelProfile":
        if self.capabilities is None:
            self.capabilities = default_model_capabilities(
                backend=self.backend,
                kind=self.kind,
            )
        capabilities = set(self.capabilities)
        if self.backend == "llama.cpp" and not self.gguf_filename:
            raise ValueError(
                f"model '{self.alias}' has backend='llama.cpp' but no gguf_filename"
            )
        if self.backend != "llama.cpp" and self.gguf_filename:
            raise ValueError(
                f"model '{self.alias}' has gguf_filename set but backend is '{self.backend}'"
            )
        if self.backend != "llama.cpp" and self.projector_filename:
            raise ValueError(
                f"model '{self.alias}' has projector_filename set but backend "
                f"is '{self.backend}'"
            )
        if (
            self.projector_filename is not None
            and self.projector_filename == self.gguf_filename
        ):
            raise ValueError(
                f"model '{self.alias}' projector_filename must differ from "
                "gguf_filename"
            )
        if self.kind == "image" and self.backend != "sglang-diffusion":
            raise ValueError(
                f"image model '{self.alias}' requires backend='sglang-diffusion'"
            )
        if self.backend == "sglang-diffusion" and self.kind != "image":
            raise ValueError(
                f"model '{self.alias}' uses sglang-diffusion but kind is not 'image'"
            )
        if self.kind == "image" and self.image is None:
            self.image = ImageDefaults()
        if self.kind != "image" and self.image is not None:
            raise ValueError(
                f"model '{self.alias}' has image defaults but kind is not 'image'"
            )
        if self.kind == "image":
            if capabilities != {"images.generations"}:
                raise ValueError(
                    f"image model '{self.alias}' may only advertise "
                    "images/generations"
                )
        elif "images.generations" in capabilities:
            raise ValueError(
                f"language model '{self.alias}' cannot advertise "
                "images/generations"
            )
        if self.backend == "llama.cpp":
            generation = capabilities & {
                "chat.completions",
                "completions",
                "responses",
                "messages",
            }
            specialized = capabilities & {"embeddings", "rerank"}
            if generation and specialized:
                raise ValueError(
                    f"llama.cpp generation model '{self.alias}' cannot also "
                    "advertise embeddings or rerank"
                )
            if "rerank" in capabilities and capabilities != {"rerank"}:
                raise ValueError(
                    f"llama.cpp rerank model '{self.alias}' may only "
                    "advertise rerank"
                )
            if self.projector_filename and not generation:
                raise ValueError(
                    f"llama.cpp projector for '{self.alias}' requires a "
                    "generation-capable profile"
                )
        return self

    @field_validator("capabilities", mode="before")
    @classmethod
    def _capabilities_shape(cls, value):
        if value is None:
            return None
        if isinstance(value, (str, bytes)) or not isinstance(
            value, (list, tuple, set, frozenset)
        ):
            raise ValueError("capabilities must be a non-empty list")
        normalized: set[str] = set()
        for raw in value:
            item = str(raw).strip().lower().removeprefix("/v1/")
            mapped = _CAPABILITY_ALIASES.get(item)
            if mapped is None:
                raise ValueError(f"unsupported model capability '{raw}'")
            normalized.add(mapped)
        if not normalized:
            raise ValueError("capabilities must contain at least one endpoint")
        return tuple(sorted(normalized))

    @field_validator("projector_filename")
    @classmethod
    def _projector_filename_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value
            or not value.endswith(".gguf")
            or value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError(
                "projector_filename must be a repository-relative .gguf file"
            )
        return value

    @field_validator("alias")
    @classmethod
    def _alias_shape(cls, v: str) -> str:
        for prefix in _RESERVED_PREFIXES:
            if v.startswith(prefix):
                raise ValueError(
                    f"alias '{v}' uses reserved prefix '{prefix}' "
                    "(synthetic cache-only namespace)"
                )
        if not _ALIAS_RE.match(v):
            raise ValueError(
                f"alias '{v}' must match {_ALIAS_RE.pattern} "
                "(lowercase letters, digits, hyphens; must start with alnum)"
            )
        return v

    @field_validator("gpus")
    @classmethod
    def _gpus_shape(cls, v):
        if v == "all":
            return v
        if isinstance(v, list):
            if not v:
                raise ValueError("gpus list must not be empty (use 'all' for all GPUs)")
            for idx in v:
                if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
                    raise ValueError(f"gpus list contains invalid index: {idx!r}")
            return v
        raise ValueError(f"gpus must be 'all' or a list of non-negative ints, got {v!r}")


def default_model_capabilities(
    *,
    backend: str,
    kind: str,
) -> tuple[str, ...]:
    """Return the backward-compatible, engine-aware endpoint contract."""

    if kind == "image" or backend == "sglang-diffusion":
        return ("images.generations",)
    if backend == "llama.cpp":
        # Common portable Generation contract shared with native llama.cpp.
        return ("chat.completions", "completions", "responses")
    # Preserve the established CUDA vLLM behavior.
    return (
        "chat.completions",
        "completions",
        "embeddings",
        "responses",
    )


class TokenSidecar(BaseModel):
    """Forward per-request token usage to a central Postgres ledger.

    DSN lives in `.env` as `TOKEN_SIDECAR_POSTGRES_DSN` (it embeds a password).
    Everything else is declarative config so `vllm-ctl reload` can pick it up.
    Disabled by default; node_id is required when enabled.
    """
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    node_id: str = ""
    flush_interval_seconds: int = 30
    batch_size: int = 500
    # Cap on rows held locally in the SQLite outbox. Past this, the oldest
    # rows are dropped (with a WARN log) so a long Postgres outage can't OOM
    # the catalog. At default batch_size=500 / interval=30s, 100k rows is
    # ~17 hours of headroom for a heavily-used box.
    max_outbox_rows: int = 100_000
    connect_timeout_seconds: float = 5.0

    @model_validator(mode="after")
    def _node_id_required_when_enabled(self) -> "TokenSidecar":
        if self.enabled and not self.node_id.strip():
            raise ValueError(
                "token_sidecar.node_id is required when token_sidecar.enabled=true"
            )
        return self


class FleetNode(BaseModel):
    """Stable, non-secret identity advertised to the Nyx fleet gateway."""

    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(default="", max_length=128)

    @field_validator("node_id")
    @classmethod
    def _node_id_is_bounded(cls, value: str) -> str:
        value = value.strip()
        if value and any(ord(char) < 0x20 for char in value):
            raise ValueError("fleet.node_id must not contain control characters")
        return value


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: Server = Field(default_factory=Server)
    storage: Storage
    defaults: Defaults = Field(default_factory=Defaults)
    models: list[ModelProfile] = Field(default_factory=list)
    token_sidecar: TokenSidecar = Field(default_factory=TokenSidecar)
    fleet: FleetNode = Field(default_factory=FleetNode)

    @model_validator(mode="after")
    def _cross_refs(self) -> "Config":
        aliases = [m.alias for m in self.models]
        if len(aliases) != len(set(aliases)):
            dupes = sorted({a for a in aliases if aliases.count(a) > 1})
            raise ValueError(f"duplicate model aliases: {dupes}")
        loc_names = {loc.name for loc in self.storage.locations}
        for m in self.models:
            if m.storage is not None and m.storage not in loc_names:
                raise ValueError(
                    f"model '{m.alias}' references unknown storage '{m.storage}' "
                    f"(known: {sorted(loc_names)})"
                )
            if (
                m.image is not None
                and m.image.width * m.image.height > self.server.image_max_pixels
            ):
                raise ValueError(
                    f"model '{m.alias}' image defaults exceed "
                    f"server.image_max_pixels={self.server.image_max_pixels}"
                )
        if (
            self.token_sidecar.enabled
            and self.fleet.node_id
            and self.fleet.node_id != self.token_sidecar.node_id
        ):
            raise ValueError(
                "fleet.node_id must match token_sidecar.node_id when token "
                "reporting is enabled"
            )
        return self


@dataclass
class ConfigDiagnostics:
    gpu_warnings: list[str] = field(default_factory=list)
    storage_warnings: list[str] = field(default_factory=list)


def gpu_indices_or_none() -> list[int] | None:
    """Return visible GPU indices via `nvidia-smi -L`, or None if probe fails."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    indices: list[int] = []
    for line in proc.stdout.splitlines():
        m = re.match(r"GPU (\d+):", line)
        if m:
            indices.append(int(m.group(1)))
    return indices


def _validate_runtime(cfg: Config) -> ConfigDiagnostics:
    """Hard-fail on missing GPU indices when the probe succeeds. Soft-warn on
    storage path issues (PRD §5.12 — drives can be temporarily unmounted)."""
    diag = ConfigDiagnostics()
    gpus = gpu_indices_or_none()
    if gpus is None:
        diag.gpu_warnings.append(
            "nvidia-smi unavailable; skipping GPU index validation"
        )
    else:
        present = set(gpus)
        for m in cfg.models:
            if isinstance(m.gpus, list):
                for idx in m.gpus:
                    if idx not in present:
                        raise ConfigError(
                            f"model '{m.alias}' references GPU {idx} "
                            f"but only {sorted(present)} are visible"
                        )

    for loc in cfg.storage.locations:
        if not os.path.isdir(loc.path):
            diag.storage_warnings.append(
                f"storage '{loc.name}' path '{loc.path}' does not exist"
            )
        elif not os.access(loc.path, os.W_OK):
            diag.storage_warnings.append(
                f"storage '{loc.name}' path '{loc.path}' is not writable"
            )

    for w in diag.gpu_warnings:
        logger.warning(w)
    for w in diag.storage_warnings:
        logger.warning(w)
    return diag


def load_config(path: str | None = None) -> Config:
    """Read YAML, validate via Pydantic, run runtime checks. Path resolved at
    call time (env var → fallback) so tests can monkeypatch the env."""
    cfg_path = path or os.environ.get("MNEMOSYNE_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    p = Path(cfg_path)
    if not p.exists():
        raise ConfigError(f"config file not found: {cfg_path}")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"malformed YAML in {cfg_path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {cfg_path} must contain a mapping at the top level")
    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid config in {cfg_path}: {e}") from e
    _validate_runtime(cfg)
    return cfg


def load_env(path: str | None = None, override: bool = False) -> dict[str, str]:
    """Read /config/.env if present. Populate os.environ for keys not already
    set (override=False). Path resolved at call time. Missing file is OK."""
    env_path = path or os.environ.get("MNEMOSYNE_ENV_PATH", DEFAULT_ENV_PATH)
    p = Path(env_path)
    parsed: dict[str, str] = {}
    if not p.exists():
        logger.debug("no .env file at %s; skipping", env_path)
        return parsed
    for raw_line in p.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        parsed[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return parsed
