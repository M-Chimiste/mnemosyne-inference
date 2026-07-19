"""Configuration schema and loaders for the native macOS service."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    DEFAULT_CAPABILITIES,
    Endpoint,
    EngineName,
    ModelKind,
    ResolvedTarget,
    TargetKey,
)


_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Mnemosyne"


class ConfigError(RuntimeError):
    pass


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inference_bind: str = "127.0.0.1"
    inference_port: int = Field(default=17320, ge=1024, le=65535)
    control_bind: str = "127.0.0.1"
    control_port: int = Field(default=17321, ge=1024, le=65535)
    idle_unload_seconds: int | None = Field(default=900, ge=1)
    startup_timeout_seconds: float = Field(default=900, gt=0)
    swap_queue_timeout_seconds: float = Field(default=300, gt=0)
    shutdown_grace_seconds: float = Field(default=30, gt=0)
    reconcile_interval_seconds: float = Field(default=30, ge=5)
    image_request_timeout_seconds: float = Field(default=1800, gt=0)
    image_max_pixels: int = Field(default=4_194_304, ge=4096)
    startup_policy: str = "unload_all"
    inference_api_key_env: str = "INFERENCE_API_KEY"
    control_password_env: str = "ADMIN_PASSWORD"

    @model_validator(mode="after")
    def _validate_server(self) -> "ServerConfig":
        if self.inference_port == self.control_port:
            raise ValueError("inference and control ports must differ")
        if self.startup_policy != "unload_all":
            raise ValueError("only startup_policy='unload_all' is currently supported")
        return self


class LMStudioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    base_url: str = "http://127.0.0.1:1234"
    api_key_env: str = "LMSTUDIO_API_KEY"
    request_timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("base_url")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        return _validate_loopback_url(value, engine="LM Studio")


class OMLXConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    base_url: str = "http://127.0.0.1:17322"
    api_key_env: str = "OMLX_API_KEY"
    admin_session_env: str = "OMLX_ADMIN_SESSION"
    request_timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("base_url")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        return _validate_loopback_url(value, engine="oMLX")


class DS4Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=17323, ge=1024, le=65535)
    binary: str = "/Applications/DwarfStar/ds4-server"
    working_directory: str = "/Applications/DwarfStar"
    process_state_path: str = str(_APP_SUPPORT / "state" / "ds4-process.json")
    request_timeout_seconds: float = Field(default=30, gt=0)
    shutdown_grace_seconds: float = Field(default=30, gt=0)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        try:
            if not ipaddress.ip_address(value).is_loopback:
                raise ValueError("DS4 must bind to a loopback address")
        except ValueError as exc:
            if value != "localhost":
                raise ValueError("DS4 host must be a loopback address") from exc
        return value


class MFluxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=17324, ge=1024, le=65535)
    python: str | None = None
    python_env: str = "MNEMOSYNE_MFLUX_PYTHON"
    source_path_env: str = "MNEMOSYNE_MFLUX_PYTHONPATH"
    request_timeout_seconds: float = Field(default=30, gt=0)
    shutdown_grace_seconds: float = Field(default=30, gt=0)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        try:
            if not ipaddress.ip_address(value).is_loopback:
                raise ValueError("MFLUX must bind to a loopback address")
        except ValueError as exc:
            if value != "localhost":
                raise ValueError("MFLUX host must be a loopback address") from exc
        return value


class EnginesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lmstudio: LMStudioConfig = Field(default_factory=LMStudioConfig)
    omlx: OMLXConfig = Field(default_factory=OMLXConfig)
    ds4: DS4Config = Field(default_factory=DS4Config)
    mflux: MFluxConfig = Field(default_factory=MFluxConfig)


class ModelLoadConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_length: int | None = Field(default=None, gt=0)
    eval_batch_size: int | None = Field(default=None, gt=0)
    flash_attention: bool | None = None
    num_experts: int | None = Field(default=None, gt=0)
    offload_kv_cache_to_gpu: bool | None = None
    kv_disk_directory: str | None = None
    kv_disk_space_mb: int | None = Field(default=None, gt=0)
    extra_args: list[str] = Field(default_factory=list)


class ImageProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: Literal["qwen-image", "krea-2"]
    quantize: Literal[3, 4, 5, 6, 8] | None = 8
    width: int = Field(default=1024, ge=64, le=4096, multiple_of=16)
    height: int = Field(default=1024, ge=64, le=4096, multiple_of=16)
    num_inference_steps: int = Field(default=30, ge=1, le=200)
    guidance_scale: float = Field(default=4.0, ge=0, le=50)


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    engine: EngineName
    model: str
    served_model_name: str | None = None
    capabilities: set[Endpoint] | None = None
    load: ModelLoadConfig = Field(default_factory=ModelLoadConfig)
    kind: ModelKind = ModelKind.LANGUAGE
    image: ImageProfileConfig | None = None
    enabled: bool = True

    @field_validator("alias")
    @classmethod
    def _valid_alias(cls, value: str) -> str:
        if not _ALIAS_RE.fullmatch(value):
            raise ValueError(
                "alias must contain lowercase letters, digits, and hyphens and start with alphanumeric"
            )
        return value

    @field_validator("model")
    @classmethod
    def _model_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value

    @field_validator("served_model_name")
    @classmethod
    def _served_name_not_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("served_model_name must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_engine_options(self) -> "ModelProfile":
        if self.engine == EngineName.MFLUX:
            if self.kind != ModelKind.IMAGE or self.image is None:
                raise ValueError("MFLUX profiles require kind='image' and image settings")
            if self.load != ModelLoadConfig():
                raise ValueError("MFLUX profiles use image settings, not language load settings")
            if self.capabilities is not None and self.capabilities != {
                Endpoint.IMAGES_GENERATIONS
            }:
                raise ValueError("MFLUX profiles only support images/generations")
        elif self.kind == ModelKind.IMAGE or self.image is not None:
            raise ValueError("image profiles require engine='mflux'")
        elif self.capabilities is not None and Endpoint.IMAGES_GENERATIONS in self.capabilities:
            raise ValueError("images/generations capability requires engine='mflux'")
        if self.engine != EngineName.DS4 and (
            self.load.kv_disk_directory is not None
            or self.load.kv_disk_space_mb is not None
            or self.load.extra_args
        ):
            raise ValueError("DS4 KV and extra_args settings require engine='ds4'")
        if self.engine == EngineName.OMLX and any(
            value is not None
            for value in (
                self.load.context_length,
                self.load.eval_batch_size,
                self.load.flash_attention,
                self.load.num_experts,
                self.load.offload_kv_cache_to_gpu,
            )
        ):
            raise ValueError("oMLX load settings belong in oMLX per-model settings")
        if self.engine == EngineName.DS4 and any(
            value is not None
            for value in (
                self.load.eval_batch_size,
                self.load.flash_attention,
                self.load.num_experts,
                self.load.offload_kv_cache_to_gpu,
            )
        ):
            raise ValueError("LM Studio load settings are not supported by DS4")
        if self.capabilities is not None and not self.capabilities:
            raise ValueError("capabilities must contain at least one endpoint")
        return self

    def resolve(self) -> ResolvedTarget:
        if self.engine == EngineName.MFLUX:
            assert self.image is not None
            load_options = {
                "family": self.image.family,
                "quantize": self.image.quantize,
            }
            image_defaults = {
                "width": self.image.width,
                "height": self.image.height,
                "num_inference_steps": self.image.num_inference_steps,
                "guidance_scale": self.image.guidance_scale,
            }
        else:
            load_options = self.load.model_dump(exclude_none=True, exclude_defaults=True)
            image_defaults = {}
        canonical = str(Path(self.model).expanduser()) if self.engine == EngineName.DS4 else self.model
        payload = json.dumps(load_options, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        wire_model = self.served_model_name or (
            self.alias if self.engine in {EngineName.DS4, EngineName.MFLUX} else self.model
        )
        capabilities = (
            frozenset(self.capabilities)
            if self.capabilities is not None
            else DEFAULT_CAPABILITIES[self.engine]
        )
        return ResolvedTarget(
            alias=self.alias,
            key=TargetKey(
                engine=self.engine,
                canonical_model_id=canonical,
                load_config_digest=digest,
            ),
            wire_model=wire_model,
            capabilities=capabilities,
            load_options=load_options,
            kind=self.kind,
            image_defaults=image_defaults,
        )


class TokenSidecarConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    node_id: str = ""
    flush_interval_seconds: int = Field(default=30, ge=1)
    batch_size: int = Field(default=500, ge=1)
    max_outbox_rows: int = Field(default=100_000, ge=1)
    connect_timeout_seconds: float = Field(default=5, gt=0)

    @model_validator(mode="after")
    def _node_required(self) -> "TokenSidecarConfig":
        if self.enabled and not self.node_id.strip():
            raise ValueError("token_sidecar.node_id is required when enabled")
        return self


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_database: str = str(_APP_SUPPORT / "state" / "mnemosyne.db")
    log_directory: str = str(_APP_SUPPORT / "logs")


class MacConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: ServerConfig = Field(default_factory=ServerConfig)
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    models: list[ModelProfile] = Field(default_factory=list)
    token_sidecar: TokenSidecarConfig = Field(default_factory=TokenSidecarConfig)

    @model_validator(mode="after")
    def _validate_cross_references(self) -> "MacConfig":
        aliases = [profile.alias for profile in self.models]
        if len(aliases) != len(set(aliases)):
            duplicates = sorted({alias for alias in aliases if aliases.count(alias) > 1})
            raise ValueError(f"duplicate model aliases: {duplicates}")

        enabled_engines = {
            EngineName.LMSTUDIO: self.engines.lmstudio.enabled,
            EngineName.OMLX: self.engines.omlx.enabled,
            EngineName.DS4: self.engines.ds4.enabled,
            EngineName.MFLUX: self.engines.mflux.enabled,
        }
        disabled_references = sorted(
            profile.alias
            for profile in self.models
            if profile.enabled and not enabled_engines[profile.engine]
        )
        if disabled_references:
            raise ValueError(
                "enabled model profiles reference disabled engines: "
                f"{disabled_references}"
            )

        for profile in self.models:
            if (
                profile.image is not None
                and profile.image.width * profile.image.height
                > self.server.image_max_pixels
            ):
                raise ValueError(
                    f"model '{profile.alias}' image defaults exceed "
                    f"server.image_max_pixels={self.server.image_max_pixels}"
                )

        ports = {
            "inference": self.server.inference_port,
            "control": self.server.control_port,
        }
        if self.engines.lmstudio.enabled:
            ports["lmstudio"] = _url_port(self.engines.lmstudio.base_url)
        if self.engines.omlx.enabled:
            ports["omlx"] = _url_port(self.engines.omlx.base_url)
        if self.engines.ds4.enabled:
            ports["ds4"] = self.engines.ds4.port
        if self.engines.mflux.enabled:
            ports["mflux"] = self.engines.mflux.port
        by_port: dict[int, list[str]] = {}
        for name, port in ports.items():
            by_port.setdefault(port, []).append(name)
        conflicts = {port: names for port, names in by_port.items() if len(names) > 1}
        if conflicts:
            raise ValueError(f"configured Mnemosyne ports must be distinct: {conflicts}")
        return self

    def profiles(self) -> dict[str, ResolvedTarget]:
        return {profile.alias: profile.resolve() for profile in self.models if profile.enabled}


def _url_port(url: str) -> int:
    parsed = urlsplit(url)
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _validate_loopback_url(value: str, *, engine: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{engine} base_url must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{engine} credentials must come from the environment")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if not loopback:
        raise ValueError(f"{engine} base_url must use a loopback host")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"{engine} base_url may not include a path, query, or fragment")
    return value.rstrip("/")


def load_env(path: str | Path) -> None:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def load_config(path: str | Path, *, env_path: str | Path | None = None) -> MacConfig:
    if env_path is not None:
        load_env(env_path)
    config_path = Path(path).expanduser()
    try:
        contents = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"failed to load {config_path}: {exc}") from exc
    return parse_config(contents, source=str(config_path))


def parse_config(contents: str, *, source: str = "configuration") -> MacConfig:
    """Validate an in-memory YAML document with the runtime schema."""

    try:
        raw: Any = yaml.safe_load(contents)
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to load {source}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{source} must contain a YAML mapping")
    try:
        return MacConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"invalid config {source}: {exc}") from exc
