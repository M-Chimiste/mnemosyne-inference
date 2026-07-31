"""Configuration schema and loaders for the native macOS service."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import tempfile
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
from .sidecar_discovery import LEGACY_AUTOMATIC_NODE_IDS


_ALIAS_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_IMAGE_FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_MODEL_FORMAT_SUFFIX_RE = re.compile(
    r"(?:[._\s-]+(?:gguf|mlx|safetensors))+$",
    re.IGNORECASE,
)
_STORAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Mnemosyne"


class ConfigError(RuntimeError):
    pass


def suggested_model_alias(
    name: str,
    *,
    fallback: str = "model",
    max_length: int = 64,
) -> str:
    """Build a client-facing alias without leaking the weight format."""

    without_format = _MODEL_FORMAT_SUFFIX_RE.sub("", name.strip())
    alias = re.sub(r"[^a-z0-9.]+", "-", without_format.casefold())
    alias = re.sub(r"-+", "-", alias)
    alias = re.sub(r"\.+", ".", alias)
    alias = re.sub(r"(?:\.-|-\.)+", "-", alias).strip(".-")
    alias = alias[:max_length].rstrip(".-")
    return alias if alias and _ALIAS_RE.fullmatch(alias) else fallback


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inference_bind: str = "127.0.0.1"
    inference_port: int = Field(default=1240, ge=1024, le=65535)
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


class LlamaCppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=17325, ge=1024, le=65535)
    # A managed active runtime overrides this path. The deliberately missing
    # fallback prevents a fresh install from silently depending on Homebrew.
    binary: str = str(
        _APP_SUPPORT / "runtimes" / "llama.cpp" / "not-installed" / "llama-server"
    )
    working_directory: str = str(_APP_SUPPORT)
    process_state_path: str = str(_APP_SUPPORT / "state" / "llama-cpp-process.json")
    request_timeout_seconds: float = Field(default=30, gt=0)
    shutdown_grace_seconds: float = Field(default=30, gt=0)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        try:
            if not ipaddress.ip_address(value).is_loopback:
                raise ValueError("llama.cpp must bind to a loopback address")
        except ValueError as exc:
            if value != "localhost":
                raise ValueError("llama.cpp host must be a loopback address") from exc
        return value


class OMLXConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = "http://127.0.0.1:17322"
    api_key_env: str = "OMLX_API_KEY"
    admin_session_env: str = "OMLX_ADMIN_SESSION"
    request_timeout_seconds: float = Field(default=30, gt=0)
    model_directories: list[str] = Field(default_factory=list)

    @field_validator("base_url")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        return _validate_loopback_url(value, engine="oMLX")

    @field_validator("model_directories")
    @classmethod
    def _model_directories_are_unique(cls, value: list[str]) -> list[str]:
        normalized = [
            str(Path(item).expanduser().resolve(strict=False)) for item in value
        ]
        if any(not item.strip() for item in value):
            raise ValueError("oMLX model directories must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("oMLX model directories must be unique")
        return normalized


class DS4Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
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

    llama_cpp: LlamaCppConfig = Field(default_factory=LlamaCppConfig)
    omlx: OMLXConfig = Field(default_factory=OMLXConfig)
    ds4: DS4Config = Field(default_factory=DS4Config)
    mflux: MFluxConfig = Field(default_factory=MFluxConfig)


class ModelLoadConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_length: int | None = Field(default=None, gt=0)
    eval_batch_size: int | None = Field(default=None, gt=0)
    flash_attention: bool | None = None
    offload_kv_cache_to_gpu: bool | None = None
    projector_path: str | None = None
    gpu_layers: int | None = Field(default=None, ge=0)
    ubatch_size: int | None = Field(default=None, gt=0)
    threads: int | None = Field(default=None, gt=0)
    parallel: int | None = Field(default=None, gt=0)
    pooling: Literal["none", "mean", "cls", "last", "rank"] | None = None
    kv_disk_directory: str | None = None
    kv_disk_space_mb: int | None = Field(default=None, gt=0)
    extra_args: list[str] = Field(default_factory=list)


class LegacyLMStudioLoadConfig(ModelLoadConfig):
    """Settings retained only until a v1 profile is adopted from local files."""

    num_experts: int | None = Field(default=None, gt=0)


class LegacyLMStudioProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    alias: str
    model: str
    served_model_name: str | None = None
    capabilities: set[Endpoint] | None = None
    load: LegacyLMStudioLoadConfig = Field(default_factory=LegacyLMStudioLoadConfig)
    enabled: bool = True


class MigrationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    legacy_lmstudio_profiles: list[LegacyLMStudioProfile] = Field(
        default_factory=list
    )


class ImageProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: str = Field(min_length=1, max_length=80)
    quantize: Literal[3, 4, 5, 6, 8] | None = 8
    width: int = Field(default=1024, ge=64, le=4096, multiple_of=16)
    height: int = Field(default=1024, ge=64, le=4096, multiple_of=16)
    num_inference_steps: int = Field(default=30, ge=1, le=200)
    guidance_scale: float = Field(default=4.0, ge=0, le=50)

    @field_validator("family")
    @classmethod
    def _valid_family(cls, value: str) -> str:
        if not _IMAGE_FAMILY_RE.fullmatch(value):
            raise ValueError(
                "image family must contain lowercase letters, digits, and hyphens"
            )
        return value


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    engine: EngineName
    model: str
    storage: str | None = None
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
                "alias must contain lowercase letters, digits, dots, and hyphens in alphanumeric segments"
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
        ):
            raise ValueError("KV disk settings require engine='ds4'")
        if self.engine not in {EngineName.DS4, EngineName.LLAMA_CPP} and self.load.extra_args:
            raise ValueError(
                "extra_args settings require engine='ds4' or 'llama.cpp'"
            )
        if self.engine == EngineName.OMLX and any(
            value is not None
            for value in (
                self.load.context_length,
                self.load.eval_batch_size,
                self.load.flash_attention,
                self.load.offload_kv_cache_to_gpu,
                self.load.projector_path,
                self.load.gpu_layers,
                self.load.ubatch_size,
                self.load.threads,
                self.load.parallel,
                self.load.pooling,
            )
        ):
            raise ValueError("oMLX load settings belong in oMLX per-model settings")
        if self.engine == EngineName.DS4 and any(
            value is not None
            for value in (
                self.load.eval_batch_size,
                self.load.flash_attention,
                self.load.offload_kv_cache_to_gpu,
                self.load.projector_path,
                self.load.gpu_layers,
                self.load.ubatch_size,
                self.load.threads,
                self.load.parallel,
                self.load.pooling,
            )
        ):
            raise ValueError("llama.cpp load settings are not supported by DS4")
        if self.engine == EngineName.LLAMA_CPP:
            capabilities = self.capabilities or set(DEFAULT_CAPABILITIES[EngineName.LLAMA_CPP])
            generation = capabilities & {
                Endpoint.CHAT_COMPLETIONS,
                Endpoint.COMPLETIONS,
                Endpoint.RESPONSES,
                Endpoint.MESSAGES,
            }
            specialized = capabilities & {Endpoint.EMBEDDINGS, Endpoint.RERANK}
            if generation and specialized:
                raise ValueError(
                    "llama.cpp generation profiles cannot also advertise embeddings or rerank"
                )
            if Endpoint.RERANK in capabilities and capabilities != {Endpoint.RERANK}:
                raise ValueError("llama.cpp rerank profiles must use only the rerank capability")
            if self.load.projector_path is not None and not generation:
                raise ValueError(
                    "llama.cpp projectors require a generation-capable profile"
                )
        if self.capabilities is not None and not self.capabilities:
            raise ValueError("capabilities must contain at least one endpoint")
        return self

    def resolve(
        self,
        *,
        storage_path: str | None = None,
        scope_id: str | None = None,
        storage_volume_uuid: str | None = None,
    ) -> ResolvedTarget:
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
        canonical = (
            os.path.abspath(os.path.expanduser(self.model))
            if self.engine in {EngineName.DS4, EngineName.LLAMA_CPP}
            else self.model
        )
        payload = json.dumps(load_options, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        wire_model = self.served_model_name or (
            self.alias
            if self.engine in {EngineName.DS4, EngineName.LLAMA_CPP, EngineName.MFLUX}
            else self.model
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
            storage_path=storage_path,
            scope_id=scope_id,
            storage_volume_uuid=storage_volume_uuid,
        )


class TokenSidecarConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # Empty means: migrate node.id from the previous local token sidecar,
    # falling back to the normalized Mac hostname. ``mnemosyne-mac`` remains
    # an automatic sentinel so older generated configs migrate safely.
    node_id: str = Field(default="", max_length=128)
    flush_interval_seconds: int = Field(default=30, ge=1)
    batch_size: int = Field(default=500, ge=1)
    max_outbox_rows: int = Field(default=100_000, ge=1)
    connect_timeout_seconds: float = Field(default=5, gt=0)

    @field_validator("node_id")
    @classmethod
    def _clean_node_id(cls, value: str) -> str:
        cleaned = value.strip()
        return "" if cleaned.casefold() in LEGACY_AUTOMATIC_NODE_IDS else cleaned


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_database: str = str(_APP_SUPPORT / "state" / "mnemosyne.db")
    log_directory: str = str(_APP_SUPPORT / "logs")


class StorageLocationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    volume_uuid: str | None = None
    # Opaque SHA-256 identifier for bookmark bytes stored in the private state
    # directory. Never place the transferable bookmark itself in YAML.
    scope_id: str | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _STORAGE_NAME_RE.fullmatch(value):
            raise ValueError(
                "storage location name must contain lowercase letters, digits, "
                "and hyphens and start with alphanumeric"
            )
        return value

    @field_validator("path")
    @classmethod
    def _path_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("storage location path must not be empty")
        return value

    @field_validator("volume_uuid")
    @classmethod
    def _volume_uuid_not_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value

    @field_validator("scope_id")
    @classmethod
    def _valid_scope_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("storage scope_id must be a SHA-256 identifier")
        return normalized


def _default_storage_locations() -> list[StorageLocationConfig]:
    return [
        StorageLocationConfig(
            name="internal",
            path=str(_APP_SUPPORT / "models"),
        )
    ]


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str = "internal"
    locations: list[StorageLocationConfig] = Field(
        default_factory=_default_storage_locations
    )

    @model_validator(mode="after")
    def _validate_locations(self) -> "StorageConfig":
        if not self.locations:
            raise ValueError("storage.locations must contain at least one location")
        names = [location.name for location in self.locations]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate storage location names: {duplicates}")
        if self.default not in names:
            raise ValueError(
                f"storage.default '{self.default}' is not a declared location"
            )
        return self


class MacConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    server: ServerConfig = Field(default_factory=ServerConfig)
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    models: list[ModelProfile] = Field(default_factory=list)
    migration: MigrationConfig = Field(default_factory=MigrationConfig)
    token_sidecar: TokenSidecarConfig = Field(default_factory=TokenSidecarConfig)

    @model_validator(mode="before")
    @classmethod
    def _migrate_v1_lmstudio_configuration(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        raw = copy.deepcopy(value)
        version = raw.get("schema_version", 1)
        if version != 1:
            return raw

        engines = raw.get("engines")
        if isinstance(engines, dict):
            engines.pop("lmstudio", None)

        migrated_profiles: list[dict[str, Any]] = []
        retained_profiles: list[Any] = []
        models = raw.get("models", [])
        if isinstance(models, list):
            for profile in models:
                if isinstance(profile, dict) and profile.get("engine") == "lmstudio":
                    migrated_profiles.append(
                        {
                            key: item
                            for key, item in profile.items()
                            if key
                            in {
                                "alias",
                                "model",
                                "served_model_name",
                                "capabilities",
                                "load",
                                "enabled",
                            }
                        }
                    )
                else:
                    if isinstance(profile, dict):
                        load = profile.get("load")
                        if (
                            isinstance(load, dict)
                            and load.get("num_experts") is None
                        ):
                            # V1 serialized this LM Studio-only option as null
                            # on every profile. It is not part of the V2 load
                            # schema for retained engines.
                            load.pop("num_experts", None)
                    retained_profiles.append(profile)
            raw["models"] = retained_profiles

        migration = raw.get("migration")
        if not isinstance(migration, dict):
            migration = {}
        previous = migration.get("legacy_lmstudio_profiles")
        if isinstance(previous, list):
            migrated_profiles = [*previous, *migrated_profiles]
        migration["legacy_lmstudio_profiles"] = migrated_profiles
        raw["migration"] = migration
        raw["schema_version"] = 2
        return raw

    @model_validator(mode="after")
    def _validate_cross_references(self) -> "MacConfig":
        aliases = [profile.alias for profile in self.models]
        if len(aliases) != len(set(aliases)):
            duplicates = sorted({alias for alias in aliases if aliases.count(alias) > 1})
            raise ValueError(f"duplicate model aliases: {duplicates}")

        storage_names = {location.name for location in self.storage.locations}
        invalid_storage = sorted(
            profile.alias
            for profile in self.models
            if profile.storage is not None and profile.storage not in storage_names
        )
        if invalid_storage:
            raise ValueError(
                "model profiles reference unknown storage locations: "
                f"{invalid_storage}"
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
        if self.engines.llama_cpp.enabled:
            ports["llama.cpp"] = self.engines.llama_cpp.port
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

    def engine_enabled(self, engine: EngineName) -> bool:
        return {
            EngineName.LLAMA_CPP: self.engines.llama_cpp.enabled,
            EngineName.OMLX: self.engines.omlx.enabled,
            EngineName.DS4: self.engines.ds4.enabled,
            EngineName.MFLUX: self.engines.mflux.enabled,
        }[engine]

    def profiles(self) -> dict[str, ResolvedTarget]:
        locations = {location.name: location for location in self.storage.locations}
        profiles: dict[str, ResolvedTarget] = {}
        for profile in self.models:
            if not profile.enabled or not self.engine_enabled(profile.engine):
                continue
            location = locations.get(profile.storage) if profile.storage else None
            profiles[profile.alias] = profile.resolve(
                storage_path=location.path if location is not None else None,
                scope_id=location.scope_id if location is not None else None,
                storage_volume_uuid=(
                    location.volume_uuid if location is not None else None
                ),
            )
        return profiles


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


def save_config(config: MacConfig, path: str | Path) -> None:
    """Atomically persist a validated configuration with private permissions."""

    config_path = Path(path).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    contents = yaml.safe_dump(
        config.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        dir=config_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, config_path)
        os.chmod(config_path, 0o600)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise
