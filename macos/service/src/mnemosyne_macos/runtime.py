"""Composition root for the native macOS coordinator service."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, replace
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Callable, Mapping
from uuid import uuid4

import httpx

from . import __version__
from .benchmarking import (
    BENCHMARK_ENDPOINT,
    CONTEXT_SUITE_VERSION,
    BenchmarkStore,
    BenchmarkSuite,
    ContextBenchmarkRecord,
    candidate_set_fingerprint,
    choose_target,
    context_target_fingerprint,
    system_fingerprint,
    target_fingerprint,
    target_with_context_window,
)
from .catalog_runtime import NativeCatalogRuntime
from .config import (
    ImageProfileConfig,
    MacConfig,
    ModelContextConfig,
    ModelLoadConfig,
    ModelProfile,
    StorageLocationConfig,
    load_config,
    save_config,
    suggested_model_alias,
)
from .coordinator import CoordinatorState, CoordinatorStatus, ResidencyCoordinator
from .desired_install_store import (
    DesiredInstallConflictError,
    DesiredInstallIntegrityError,
    DesiredInstallNotFoundError,
    DesiredInstallRecord,
    DesiredInstallStore,
)
from .desired_install_executor import (
    DesiredInstallExecutor,
    DesiredInstallExecutorError,
)
from .desired_install_runtime import NativeDesiredInstallAuthorities
from .engines.base import Deadline, EngineAdapter
from .engines.ds4 import DS4Adapter
from .engines.llamacpp import LlamaCppAdapter
from .engines.mflux import MFluxAdapter
from .engines.omlx import OMLXAdapter
from .filesystem import FilesystemProbe, FilesystemProbeError
from .fleet_protocol import (
    FLEET_SCHEMA_VERSION,
    deployment_identity,
    effective_capacity,
    gguf_quantization,
    immutable_revision,
    portable_load_config,
)
from .fleet_participation import (
    FleetParticipationState,
    FleetParticipationStore,
)
from .fleet_pairing import (
    FleetPairingStore,
    PairingErrorCode,
    PairingRecord,
    PairingState,
    PrivateEnvironmentInvalid,
)
from .fleet_pairing_client import (
    FleetPairingClient,
    PairingClientError,
    PairingClientErrorCode,
    PairingInvitation,
)
from .models import (
    ACTIVE_ENGINE_NAMES,
    ContextWindowHint,
    ENGINE_RELEASE_TIER,
    Endpoint,
    EngineName,
    ResolvedTarget,
    ServiceState,
)
from .performance import PerformanceTracker
from .install_provenance import (
    InstallationProvenance,
    ProvenanceProofRejected,
)
from .install_launch import (
    DS4InstallLaunch,
    InstallLaunchError,
    LlamaCppInstallLaunch,
    OMLX_TARGET_LAUNCH_KEY,
    OMLXInstallLaunch,
    omlx_target_launch,
    with_omlx_target_launch,
)
from .install_store import InstallRecord
from .installer import NativeInstaller
from .local_models import (
    LocalModel,
    LocalModelError,
    mark_omlx_id_conflicts,
)
from .mac_inventory import HardwareProbe, MacInventoryProducer
from .mac_inventory_store import (
    MacInventoryIndex,
    MacInventoryIndexError,
    StorageBinding,
    canonical_uuid,
)
from .mac_inventory_sync import MacInventorySyncClient
from .model_cleanup_journal import (
    CleanupConfigState,
    CleanupKind,
    CleanupPhase,
    CleanupRecoveryAction,
    DestinationState,
    InstallLedgerState,
    ModelCleanupJournal,
    ModelCleanupJournalError,
    decide_cleanup_recovery,
)
from .native_lifecycle import NativeLifecycleJournal, RetentionMode
from .native_lifecycle_helper_transport import (
    BundledLifecycleHelperTransport,
    LifecycleHelperTransport,
)
from .native_lifecycle_runtime import NativeLifecycleManager
from .usage import UsageEvent, normalize_usage
from .usage_delivery import UsageReservationLease, UsageService
from .runtime_updates import RuntimeUpdateManager
from .scope_process import SecurityScopeProcess
from .security_scopes import SecurityScopeRegistry
from .storage import StorageError, install_destination


logger = logging.getLogger("mnemosyne-macos.runtime")

DEFAULT_INTERACTIVE_CONTEXT_LENGTH = 32_768
MAX_INTERACTIVE_CONTEXT_LENGTH = 65_536
# llama.cpp's established manager convention for its open-ended "all layers"
# request.  Leaving ``gpu_layers`` unset is the distinct upstream automatic
# policy; the two catalog values must never collapse into one profile.
LLAMA_CPP_ALL_GPU_LAYERS = 999
# DesiredInstall is advertised only because signed launch contracts are
# persisted and materialized exactly for every supported recipe. oMLX's
# scheduler/memory-guard values remain external service globals: Mnemosyne
# observes and revalidates them but never mutates them for one model.
SIGNED_LAUNCH_MATERIALIZATION_ENABLED = True
OMLX_SIGNED_LAUNCH_RECORD_LIMIT = 1025

# In-memory index values are deliberately tri-state. A successful install
# journal read proves that an exact configured target is ordinary, signed, or
# conflicting. If a later read fails, only that last known exact state may be
# reused; a new/changed target is fenced rather than guessed ordinary.
_OMLX_CONTRACT_ORDINARY = object()
_OMLX_CONTRACT_CONFLICT = object()


def _omlx_contract_binding_key(target: ResolvedTarget) -> tuple[object, ...]:
    """Identify the configured target independently of manager authority."""

    load = dict(target.load_options)
    load.pop(OMLX_TARGET_LAUNCH_KEY, None)
    return (
        target.alias,
        target.key.engine,
        target.key.canonical_model_id,
        target.wire_model,
        tuple(sorted(item.value for item in target.capabilities)),
        json.dumps(load, sort_keys=True, separators=(",", ":")),
        target.storage_path,
        target.scope_id,
        target.storage_volume_uuid,
        target.context_mode,
        target.native_context_length,
        target.context_max_verified_age_hours,
        target.kind,
    )


def _target_with_omlx_launch(
    target: ResolvedTarget,
    launch: OMLXInstallLaunch,
) -> ResolvedTarget:
    """Project durable install authority onto an in-memory target identity."""

    load = with_omlx_target_launch(target.load_options, launch)
    digest = hashlib.sha256(
        json.dumps(
            {
                "configured_load": target.key.load_config_digest,
                "manager_load": load,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return replace(
        target,
        key=replace(target.key, load_config_digest=digest),
        load_options=load,
    )


def _target_with_omlx_launch_conflict(target: ResolvedTarget) -> ResolvedTarget:
    """Retain local catalog visibility while making every load fail closed."""

    load = dict(target.load_options)
    load[OMLX_TARGET_LAUNCH_KEY] = {"conflict": True}
    return replace(target, load_options=load)


def _desired_install_payload(
    record: DesiredInstallRecord,
    *,
    executor_available: bool = False,
) -> dict[str, object]:
    """Expose only the closed wire intent and acknowledgement shapes."""

    return {
        "job": json.loads(record.document.canonical_json),
        "acknowledgement": json.loads(
            record.acknowledgement().canonical_json
        ),
        "local_actions": {
            "refusal_available": (
                not record.terminal
                and record.document.desired_state == "run"
                and record.state == "awaiting_local_approval"
            ),
            "approval_available": (
                executor_available
                and not record.terminal
                and record.document.desired_state == "run"
                and record.state == "awaiting_local_approval"
            ),
            "cancellation_available": (
                executor_available
                and not record.terminal
                and record.document.desired_state == "run"
                and record.state
                in {
                    "accepted",
                    "downloading",
                    "verifying",
                    "downloaded_unregistered",
                    "registered",
                }
            ),
        },
    }


def recommended_interactive_context_length(detected: int | None) -> int:
    """Choose a responsive default without discarding model capability metadata.

    Model cards increasingly advertise 128K-to-1M token maxima. Passing those
    maxima straight to llama-server eagerly allocates a correspondingly large
    KV cache. New profiles therefore start at a bounded interactive size; an
    operator can still opt into the full advertised context in Settings.
    """

    if detected is None:
        return DEFAULT_INTERACTIVE_CONTEXT_LENGTH
    return max(1, min(detected, MAX_INTERACTIVE_CONTEXT_LENGTH))


class RuntimeConfigurationError(RuntimeError):
    pass


class RestartRequired(RuntimeConfigurationError):
    pass


class ConfigurationConflict(RuntimeConfigurationError):
    pass


class ModelCleanupRejected(RuntimeConfigurationError):
    pass


_SELF_TEST_VISION_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAcklEQVR42u3a"
    "oQ0AIAwAwQ6GZv9UswEaDStUkvTEL3D64+y8nQsAAAAAAAAAAAAAAACUWnN8"
    "HQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADA"
    "KAkAAAAAAAAAAAAAQHOAB/FKxe8qaK5EAAAAAElFTkSuQmCC"
)


def _redact_diagnostic(
    value: str | None,
    *,
    secret_env_keys: tuple[str, ...] = (),
) -> str | None:
    if value is None:
        return None
    redacted = value
    for key in (
        "INFERENCE_API_KEY",
        "FLEET_API_KEY",
        "FLEET_INFERENCE_API_KEY",
        "FLEET_MANAGEMENT_API_KEY",
        "ADMIN_PASSWORD",
        "OMLX_API_KEY",
        "OMLX_ADMIN_SESSION",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "TOKEN_SIDECAR_POSTGRES_DSN",
        *secret_env_keys,
    ):
        secret = os.environ.get(key, "")
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    redacted = re.sub(
        r"(?i)((?:postgres(?:ql)?|https?)://[^:/\s]+:)[^@\s]+@",
        r"\1<redacted>@",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(authorization:\s*(?:bearer|basic)\s+)\S+",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|token|password|secret|session)=([^&\s]+)",
        r"\1=<redacted>",
        redacted,
    )
    return redacted


def configuration_revision(config: MacConfig) -> str:
    payload = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _model_profile_fingerprint(profile: ModelProfile) -> str:
    """Hash one exact profile without retaining its alias or storage path."""

    payload = json.dumps(
        profile.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def _await_owned_task(task: asyncio.Task):
    """Finish a post-mutation task before replaying caller cancellation."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except Exception:
            break
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


def _restart_sensitive_configuration_changed(
    candidate: MacConfig,
    applied: MacConfig,
) -> bool:
    """Return whether ``candidate`` needs runtime components to be rebuilt."""

    return any(
        (
            candidate.server != applied.server,
            candidate.engines != applied.engines,
            candidate.paths != applied.paths,
            candidate.catalog != applied.catalog,
            candidate.storage != applied.storage,
            candidate.token_sidecar != applied.token_sidecar,
        )
    )


def _is_loopback(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_exposure(config: MacConfig) -> None:
    # Inference bearer authentication is intentionally optional on every bind.
    # The inference-plane middleware requires it whenever the configured
    # environment variable is non-empty and otherwise accepts unauthenticated
    # requests, including on an explicitly selected LAN bind.
    if not _is_loopback(config.server.control_bind):
        password = os.environ.get(config.server.control_password_env, "").strip()
        if not password:
            raise RuntimeConfigurationError(
                "a non-loopback control bind requires an admin password"
            )


def _lexical_path(value: str | Path) -> str:
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.expanduser(str(value))))
    )


def _path_is_within(root: str | Path, candidate: str | Path) -> bool:
    root_value = _lexical_path(root)
    candidate_value = _lexical_path(candidate)
    try:
        return os.path.commonpath([root_value, candidate_value]) == root_value
    except ValueError:
        return False


def _profile_uses_install_destination(
    profile: ModelProfile,
    install: InstallRecord,
) -> bool:
    destination = _lexical_path(install.destination)
    if profile.engine in {EngineName.LLAMA_CPP, EngineName.DS4}:
        selected = [
            value
            for value in (install.filename, install.projector_filename)
            if value is not None
        ]
        safe_selected = _safe_repository_files(selected)
        if (
            install.filename is None
            or safe_selected is None
            or safe_selected != sorted(selected)
        ):
            return False
        expected_model = _lexical_path(
            os.path.join(destination, install.filename)
        )
        if _lexical_path(profile.model) != expected_model:
            return False
        projector = profile.load.projector_path
        if install.projector_filename is None:
            return projector is None
        if projector is None:
            return False
        expected_projector = _lexical_path(
            os.path.join(destination, install.projector_filename)
        )
        return _lexical_path(projector) == expected_projector
    if profile.engine == EngineName.MFLUX:
        return _lexical_path(profile.model) == destination
    if profile.engine == EngineName.OMLX:
        return profile.model == Path(destination).name
    if profile.engine in {EngineName.MLXCEL, EngineName.MISTRAL_RS}:
        return _lexical_path(profile.model) == destination
    return False


def _fleet_artifact_format(engine: EngineName) -> str:
    return {
        EngineName.LLAMA_CPP: "gguf",
        EngineName.DS4: "gguf",
        EngineName.OMLX: "mlx-snapshot",
        EngineName.MFLUX: "mflux-snapshot",
        EngineName.MLXCEL: "mlx-snapshot",
        EngineName.MISTRAL_RS: "safetensors-snapshot",
    }[engine]


def _safe_repository_files(values: list[str]) -> list[str] | None:
    if len(values) > 128:
        return None
    safe: list[str] = []
    for value in values:
        if (
            not value
            or len(value) > 512
            or value.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:[\\/]", value)
        ):
            return None
        normalized = value.replace("\\", "/")
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            return None
        safe.append(normalized)
    return sorted(set(safe))


def _safe_huggingface_repo_id(value: str) -> bool:
    return bool(
        len(value) <= 512
        and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*"
            r"(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?",
            value,
        )
    )


def _fleet_deployment_identity(
    *,
    node_id: str,
    profile: ModelProfile,
    target: ResolvedTarget,
    install: InstallRecord | None,
) -> tuple[str, dict, bool]:
    """Return deployment ID, canonical identity, and strict eligibility."""

    managed = bool(
        install is not None
        and install.status == "installed"
        and profile.engine.value == install.engine
        and profile.storage == install.storage
        and _profile_uses_install_destination(profile, install)
    )
    authoritative = bool(
        managed and install is not None and immutable_revision(install.revision)
    )
    if managed and install is not None:
        if target.key.engine in {EngineName.LLAMA_CPP, EngineName.DS4}:
            # Auto-discovered shard companions are download provenance, not
            # independently selected artifacts. Strict identity contains the
            # exact primary file plus only an explicitly selected projector.
            files = [
                value
                for value in (install.filename, install.projector_filename)
                if value is not None
            ]
        else:
            install_payload = install.to_dict()
            files = [
                str(value)
                for value in install_payload.get("download_files", [])
                if isinstance(value, str) and value
            ]
        selected_files = _safe_repository_files(files)
        if selected_files is None:
            selected_files = []
            authoritative = False
        artifact: dict[str, object] = {
            "format": _fleet_artifact_format(target.key.engine),
            "selected_files": selected_files,
            "quantization": gguf_quantization(install.filename),
            "content_digest": None,
        }
        if _safe_huggingface_repo_id(install.repo_id):
            upstream_model = install.repo_id
            revision = (
                install.revision
                if install.revision is None or len(install.revision) <= 256
                else None
            )
            if immutable_revision(revision):
                revision = revision.lower()
        else:
            upstream_model = f"node-local:{node_id}:{target.alias}"
            revision = None
            authoritative = False
    else:
        # Node-scoping prevents two path-only profiles from being mistaken for
        # strict replicas while keeping local filesystem details private.
        artifact = {
            "format": _fleet_artifact_format(target.key.engine),
            "selected_files": [],
            "quantization": None,
            "content_digest": None,
        }
        upstream_model = f"node-local:{node_id}:{target.alias}"
        revision = None

    deployment_id, _load_digest, identity = deployment_identity(
        engine=target.key.engine.value,
        upstream_model=upstream_model,
        resolved_revision=revision,
        artifact=artifact,
        kind=target.kind.value,
        capabilities=(endpoint.value for endpoint in target.capabilities),
        load_config=portable_load_config(target),
    )
    return deployment_id, identity, authoritative


def _profile_filesystem_references(profile: ModelProfile) -> tuple[str, ...]:
    """Return exact absolute paths a configured profile can reference."""

    references: list[str] = []
    candidates = (
        (profile.model, profile.load.projector_path),
        *(
            (alternative.model, alternative.load.projector_path)
            for alternative in profile.alternatives
        ),
    )
    for model, projector in candidates:
        if os.path.isabs(os.path.expanduser(model)):
            references.append(model)
        if projector is not None and os.path.isabs(os.path.expanduser(projector)):
            references.append(projector)
    return tuple(references)


def _profile_references_omlx_model(
    profile: ModelProfile,
    model_id: str,
) -> bool:
    """Return whether any primary or alternative candidate names an oMLX model."""

    candidates = (
        (profile.engine, profile.model),
        *(
            (alternative.engine, alternative.model)
            for alternative in profile.alternatives
        ),
    )
    return any(
        engine == EngineName.OMLX and Path(model).name == model_id
        for engine, model in candidates
    )


def _cleanup_targets_overlap_other_profiles(
    profiles: list[ModelProfile],
    *,
    alias: str,
    targets: tuple[str, ...],
    omlx_model_id: str | None = None,
) -> str | None:
    """Return the conflicting alias when an exact cleanup target is shared."""

    for profile in profiles:
        if profile.alias == alias:
            continue
        if omlx_model_id is not None and _profile_references_omlx_model(
            profile,
            omlx_model_id,
        ):
            return profile.alias
        for reference in _profile_filesystem_references(profile):
            if any(
                _path_is_within(target, reference)
                or _path_is_within(reference, target)
                for target in targets
            ):
                return profile.alias
    return None


def _current_storage_binding_matches(
    *,
    provenance: InstallationProvenance,
    binding: StorageBinding | None,
    location: StorageLocationConfig,
) -> bool:
    """Match every path-bearing field without resolving lexical spelling."""

    return bool(
        binding is not None
        and provenance.storage_location_id == binding.storage_location_id
        and provenance.storage_binding_generation == binding.binding_generation
        and binding.local_key == location.name
        and provenance.storage_lexical_root == binding.exact_path
        and binding.exact_path == location.path
        and provenance.storage_volume_uuid == binding.volume_uuid
        and binding.volume_uuid == location.volume_uuid
        and provenance.storage_scope_id == binding.scope_id
        and binding.scope_id == location.scope_id
    )


def _managed_manifest_timeout(record: InstallRecord) -> float:
    expected_bytes = max(
        0,
        record.total_bytes or 0,
        record.bytes_downloaded,
    )
    return min(21_600.0, max(300.0, 120.0 + expected_bytes / (20 * 1024 * 1024)))


def _storage_scope_for_path(
    config: MacConfig,
    value: str | Path,
) -> tuple[str | None, str] | None:
    candidate = _lexical_path(value)
    matches: list[tuple[int, str | None, str]] = []
    for location in config.storage.locations:
        root = _lexical_path(location.path)
        try:
            contained = os.path.commonpath([root, candidate]) == root
        except ValueError:
            contained = False
        if contained:
            matches.append((len(root), location.scope_id, location.path))
    if not matches:
        return None
    _length, scope_id, scope_path = max(matches, key=lambda item: item[0])
    return scope_id, scope_path


def build_adapters(
    config: MacConfig,
    *,
    runtime_root: str | Path | None = None,
    security_scope_root: str | Path | None = None,
) -> dict[EngineName, EngineAdapter]:
    adapters: dict[EngineName, EngineAdapter] = {}
    if config.engines.llama_cpp.enabled:
        adapters[EngineName.LLAMA_CPP] = LlamaCppAdapter(
            config.engines.llama_cpp,
            runtime_root=runtime_root,
            security_scope_root=security_scope_root,
        )
    if config.engines.omlx.enabled:
        adapters[EngineName.OMLX] = OMLXAdapter(config.engines.omlx)
    if config.engines.ds4.enabled:
        adapters[EngineName.DS4] = DS4Adapter(
            config.engines.ds4,
            runtime_root=runtime_root,
            security_scope_root=security_scope_root,
        )
    if config.engines.mflux.enabled:
        adapters[EngineName.MFLUX] = MFluxAdapter(
            config.engines.mflux,
            runtime_root=runtime_root,
            security_scope_root=security_scope_root,
        )
    return adapters


class NativeRuntime:
    """Own shared state used by the inference and control HTTP planes."""

    def __init__(
        self,
        config: MacConfig,
        *,
        config_path: str | Path | None = None,
        env_path: str | Path | None = None,
        adapters: Mapping[EngineName, EngineAdapter] | None = None,
        proxy_client: httpx.AsyncClient | None = None,
        self_test_client: httpx.AsyncClient | None = None,
        pairing_transport: httpx.AsyncBaseTransport | None = None,
        inventory_transport: httpx.AsyncBaseTransport | None = None,
        inventory_hardware_probe: HardwareProbe | None = None,
        inventory_sync_interval_seconds: float = 30.0,
        catalog_transport: httpx.AsyncBaseTransport | None = None,
        catalog_clock: Callable[[], int | float] = time.time,
        catalog_environment: Mapping[str, str] | None = None,
        catalog_update_interval_seconds: float | None = None,
        desired_install_store: DesiredInstallStore | None = None,
        desired_install_executor: DesiredInstallExecutor | None = None,
        model_cleanup_journal: ModelCleanupJournal | None = None,
        native_lifecycle_journal: NativeLifecycleJournal | None = None,
        native_lifecycle_helper_transport: (
            LifecycleHelperTransport | None
        ) = None,
        usage_service: UsageService | None = None,
        update_manager: RuntimeUpdateManager | None = None,
        runtime_root: str | Path | None = None,
        security_scopes: SecurityScopeRegistry | None = None,
        security_scope_process: SecurityScopeProcess | None = None,
        filesystem_probe: FilesystemProbe | None = None,
    ) -> None:
        validate_exposure(config)
        # Bookmark persistence must not move when a user changes the SQLite
        # state-database setting.  The configuration file lives in the stable
        # per-user application-support directory in production; tests and
        # embedders without a config path retain the historical state-relative
        # fallback.
        scope_root = (
            Path(config_path).expanduser().parent / "state" / "security-scopes"
            if config_path is not None
            else Path(config.paths.state_database).expanduser().parent
            / "security-scopes"
        )
        self.security_scopes = security_scopes or SecurityScopeRegistry(scope_root)
        scope_root = getattr(self.security_scopes, "root", scope_root)
        self.security_scope_process = security_scope_process or SecurityScopeProcess(
            scope_root,
            timeout_seconds=min(30.0, config.server.swap_queue_timeout_seconds),
        )
        self.filesystem = filesystem_probe or FilesystemProbe(
            scope_root=scope_root,
            timeout_seconds=min(30.0, config.server.swap_queue_timeout_seconds),
        )
        self.config = config
        self.config_path = Path(config_path).expanduser() if config_path else None
        self.env_path = Path(env_path).expanduser() if env_path else None
        self.profiles: dict[str, ResolvedTarget] = config.profiles()
        self.profile_candidates: dict[str, tuple[ResolvedTarget, ...]] = (
            config.profile_candidates()
        )
        self.adapters = (
            dict(adapters)
            if adapters is not None
            else build_adapters(
                config,
                runtime_root=runtime_root,
                security_scope_root=scope_root,
            )
        )
        missing = sorted(
            {
                target.key.engine
                for candidates in self.profile_candidates.values()
                for target in candidates
            }
            - self.adapters.keys()
        )
        if missing:
            raise RuntimeConfigurationError(
                f"model profiles reference unconfigured engines: {missing}"
            )
        self.coordinator = ResidencyCoordinator(
            self.adapters,
            queue_timeout_seconds=config.server.swap_queue_timeout_seconds,
            transition_timeout_seconds=config.server.startup_timeout_seconds,
            cleanup_timeout_seconds=max(30, config.server.shutdown_grace_seconds),
            shutdown_grace_seconds=config.server.shutdown_grace_seconds,
            configured_max_concurrency=config.server.max_concurrency,
            max_queue_depth=config.server.max_queue_depth,
        )
        self.proxy_client = proxy_client or httpx.AsyncClient(
            timeout=None,
            trust_env=False,
        )
        self._owns_proxy_client = proxy_client is None
        self.self_test_client = self_test_client or httpx.AsyncClient(
            timeout=None,
            trust_env=False,
        )
        self._owns_self_test_client = self_test_client is None
        self.usage = usage_service or UsageService.open(
            config.paths.state_database,
            config.token_sidecar,
        )
        self.fleet_participation = FleetParticipationStore.open(
            config.paths.state_database
        )
        pairing_environment_path = (
            self.env_path
            if self.env_path is not None
            else Path(config.paths.state_database).expanduser().parent / ".env"
        )
        self.fleet_pairing = FleetPairingStore(
            config.paths.state_database,
            pairing_environment_path,
        )
        self._fleet_pairing_record: PairingRecord | None = None
        self._fleet_pairing_error_code: PairingErrorCode | None = None
        self._fleet_pairing_revocation_denied = False
        self.performance = PerformanceTracker()
        self.benchmark_store = BenchmarkStore(config.paths.state_database)
        # Routing must not touch SQLite. Durable evidence is loaded once per
        # alias and refreshed only after a benchmark, explicit invalidation,
        # or a profile addition.
        self._benchmark_records = {
            alias: tuple(self.benchmark_store.list(alias=alias))
            for alias in self.profile_candidates
        }
        self._context_records = {
            alias: tuple(self.benchmark_store.list_context(alias=alias))
            for alias in self.profile_candidates
        }
        self._context_hints: dict[str, ContextWindowHint] = {}
        self.benchmarks = BenchmarkSuite(
            self.benchmark_store,
            coordinator=self.coordinator,
            client=self.proxy_client,
        )
        self._benchmark_lock = asyncio.Lock()
        self._runtime_fingerprints: dict[EngineName, str | None] = {}
        # The private index is also the sole authority for binding a newly
        # created managed destination to the exact user-selected storage
        # path/generation. It remains optional for inference and downloads:
        # an unavailable index merely leaves cleanup ownership unknown.
        self.mac_inventory_index = MacInventoryIndex(config.paths.state_database)
        self.installer = NativeInstaller(
            config.paths.state_database,
            on_installed=self._register_installed_model,
            storage=config.storage,
            filesystem_probe=self.filesystem,
            inventory_index=self.mac_inventory_index,
        )
        # Rebuild once the install journal exists so signed oMLX contracts are
        # projected onto their exact cold targets before any request,
        # benchmark, inventory, or Fleet snapshot can observe them.
        self._apply_profiles(config)
        self.runtime_updates = update_manager or RuntimeUpdateManager(
            llama_cpp=config.engines.llama_cpp,
            omlx=config.engines.omlx,
            mflux=config.engines.mflux,
            ds4=config.engines.ds4,
            root=runtime_root,
        )
        self._started = False
        self._maintenance_task: asyncio.Task[None] | None = None
        self._reload_lock = asyncio.Lock()
        self._runtime_update_lock = asyncio.Lock()
        self._fleet_snapshot_lock = asyncio.Lock()
        self._fleet_instance_id = uuid4().hex
        self.fleet_pairing_client = FleetPairingClient(
            config.paths.state_database,
            pairing_store=self.fleet_pairing,
            reporting_node_id=self.usage.identity.node_id,
            service_version=__version__,
            service_instance_id=self._fleet_instance_id,
            transport=pairing_transport,
            on_pairing_authority_changed=self._refresh_fleet_pairing_authority,
            on_self_revoke_pending=self._deny_fleet_pairing_authority,
            on_self_revoke_aborted=(
                self._clear_fleet_pairing_revocation_denial
            ),
            on_completed_revoke_reset=(
                self._clear_fleet_pairing_revocation_denial
            ),
        )
        self.compatibility_catalog = NativeCatalogRuntime(
            config.catalog,
            config_path=self.config_path,
            environment=catalog_environment,
            transport=catalog_transport,
            clock=catalog_clock,
            update_interval_seconds=catalog_update_interval_seconds,
            on_activation=self._catalog_did_activate,
        )
        self.mac_inventory = MacInventoryProducer(
            self,
            self.mac_inventory_index,
            hardware_probe=inventory_hardware_probe,
        )
        desired_install_root = (
            self.config_path.parent / "state"
            if self.config_path is not None
            else Path(config.paths.state_database).expanduser().parent
        )
        self.desired_install_store = desired_install_store or DesiredInstallStore(
            desired_install_root / "desired-installs.sqlite3"
        )
        self.desired_install_authorities = NativeDesiredInstallAuthorities(self)
        self.desired_install_executor = (
            desired_install_executor
            if desired_install_executor is not None
            else DesiredInstallExecutor(
                store=self.desired_install_store,
                catalog=self.compatibility_catalog,
                pairing=self.desired_install_authorities,
                inventory=self.desired_install_authorities,
                storage=self.desired_install_authorities,
                runtimes=self.desired_install_authorities,
                installer=self.installer,
            )
        )
        self.model_cleanup_journal = (
            model_cleanup_journal
            if model_cleanup_journal is not None
            else (
                ModelCleanupJournal(self.config_path)
                if self.config_path is not None
                else None
            )
        )
        self.native_lifecycle_journal = (
            native_lifecycle_journal
            if native_lifecycle_journal is not None
            else (
                NativeLifecycleJournal(self.config_path)
                if self.config_path is not None
                else None
            )
        )
        if native_lifecycle_helper_transport is None:
            lifecycle_helper = os.environ.get("MNEMOSYNE_LIFECYCLE_HELPER", "")
            native_lifecycle_helper_transport = (
                BundledLifecycleHelperTransport(lifecycle_helper)
                if lifecycle_helper
                else None
            )
        self.native_lifecycle = NativeLifecycleManager(
            self,
            self.native_lifecycle_journal,
            native_lifecycle_helper_transport,
        )
        self.mac_inventory_sync = MacInventorySyncClient(
            self.mac_inventory,
            self.fleet_pairing,
            transport=inventory_transport,
            interval_seconds=inventory_sync_interval_seconds,
        )
        self._mac_inventory_available = False
        self._mac_inventory_error_code: str | None = None
        self._desired_install_available = False
        self._desired_install_executor_available = False
        self._desired_install_reconcile_task: asyncio.Task[None] | None = None
        self._desired_install_reconcile_wake = asyncio.Event()
        self._desired_install_executor_lock = asyncio.Lock()
        self._desired_install_reconcile_offset = 0
        self._desired_install_confirmed_cancellations: set[
            tuple[str, int, str]
        ] = set()
        self._model_cleanup_journal_available = False
        self._model_cleanup_recovery_error_code: str | None = None
        self._fleet_pairing_client_available = False
        self._fleet_pairing_client_error_code: PairingClientErrorCode | None = None
        self._fleet_snapshot_sequence = 0
        self._omlx_directory_sync_pending = False
        self.startup_error: str | None = None

    @property
    def filesystem(self) -> FilesystemProbe:
        probe = getattr(self, "_filesystem", None)
        if probe is None:
            scope_registry = getattr(self, "security_scopes", None)
            scope_root = getattr(scope_registry, "root", None)
            if scope_root is None:
                config_path = getattr(self, "config_path", None)
                scope_root = (
                    config_path.parent / "state" / "security-scopes"
                    if config_path is not None
                    else Path(self.config.paths.state_database).expanduser().parent
                    / "security-scopes"
                )
            probe = FilesystemProbe(
                scope_root=scope_root,
                timeout_seconds=min(
                    30.0, self.config.server.swap_queue_timeout_seconds
                ),
            )
            self._filesystem = probe
        return probe

    @filesystem.setter
    def filesystem(self, value: FilesystemProbe) -> None:
        self._filesystem = value

    async def start(self, *, raise_on_degraded: bool = False) -> None:
        if self._started:
            return
        self._started = True
        await self._initialize_fleet_pairing()
        try:
            await self._activate_configured_security_scopes()
            await self.coordinator.initialize()
            await self.installer.start()
            await self._initialize_model_cleanup_journal()
            await self._sync_omlx_model_directories()
            await self._refresh_runtime_fingerprints()
        except Exception as exc:
            self.startup_error = str(exc)
            if raise_on_degraded:
                try:
                    await self.stop()
                except Exception:
                    pass
                raise
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="mnemosyne-macos-maintenance"
        )
        await self.usage.start()
        # Signed catalog work is advisory and starts only after every local
        # inference/JIT/download/accounting dependency. Its own failures are
        # isolated and can never become ``startup_error``.
        try:
            await self.compatibility_catalog.start()
        except Exception:
            logger.error(
                "native compatibility catalog is unavailable: catalog_internal_error"
            )
        # Inventory publication is optional management-plane work. Starting
        # its loop after every inference dependency ensures that a damaged
        # index or unavailable Hub can never degrade local startup/JIT.
        await self._initialize_mac_inventory()
        # Native migration/uninstall planning is an auxiliary, non-executing
        # control-plane slice. Its journal and inventory can fail closed while
        # local inference, JIT residency, downloads, and accounting continue.
        await self.native_lifecycle.initialize()

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
        with contextlib.suppress(Exception):
            await self.compatibility_catalog.stop()
        desired_task = self._desired_install_reconcile_task
        self._desired_install_reconcile_task = None
        self._desired_install_executor_available = False
        if desired_task is not None:
            # Cancellation must be requested before waking the inner
            # ``Event.wait``.  Waking first can let ``wait_for`` consume the
            # cancellation and leave shutdown waiting on a disabled loop.
            desired_task.cancel()
            self._desired_install_reconcile_wake.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await desired_task
        self._desired_install_reconcile_wake.clear()
        with contextlib.suppress(Exception):
            await self.native_lifecycle.close()
        with contextlib.suppress(Exception):
            await self.mac_inventory_sync.stop()
        with contextlib.suppress(Exception):
            await self.mac_inventory.close()
        with contextlib.suppress(Exception):
            await self.desired_install_store.close()
        if self.model_cleanup_journal is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.model_cleanup_journal.close)
        try:
            await self.installer.stop()
            await self.coordinator.shutdown()
        finally:
            try:
                await self.usage.close()
            finally:
                try:
                    await self.runtime_updates.aclose()
                finally:
                    try:
                        if self._owns_proxy_client:
                            await self.proxy_client.aclose()
                    finally:
                        try:
                            if self._owns_self_test_client:
                                await self.self_test_client.aclose()
                        finally:
                            try:
                                await asyncio.to_thread(self.security_scopes.close)
                            finally:
                                try:
                                    await self.fleet_participation.close()
                                finally:
                                    try:
                                        await self.fleet_pairing_client.close()
                                    finally:
                                        await self.fleet_pairing.close()

    async def _initialize_fleet_pairing(self) -> None:
        """Load pairing authority without making local inference depend on Nyx."""

        try:
            self._fleet_pairing_record = await self.fleet_pairing.initialize()
            self._fleet_pairing_error_code = None
        except Exception as exc:
            self._fleet_pairing_record = None
            self._fleet_pairing_error_code = (
                PairingErrorCode.PRIVATE_ENVIRONMENT_INVALID
                if isinstance(exc, PrivateEnvironmentInvalid)
                else PairingErrorCode.STATE_INCONSISTENT
            )
            logger.error(
                "native Fleet pairing state failed closed: %s",
                self._fleet_pairing_error_code.value,
            )
            return
        try:
            await self.fleet_pairing_client.initialize()
            self._fleet_pairing_revocation_denied = (
                await self.fleet_pairing_client.self_revoke_authority_denied()
            )
            self._fleet_pairing_client_available = True
            self._fleet_pairing_client_error_code = None
        except Exception:
            if self._uses_managed_fleet_credentials():
                self._fleet_pairing_revocation_denied = True
            self._fleet_pairing_client_available = False
            self._fleet_pairing_client_error_code = (
                PairingClientErrorCode.STATE_CONFLICT
            )
            logger.error(
                "native Fleet pairing client failed closed: %s",
                self._fleet_pairing_client_error_code.value,
            )

    async def _initialize_mac_inventory(self) -> None:
        """Start optional inventory without making local service depend on it."""

        try:
            await self.desired_install_store.initialize()
            self.mac_inventory_sync.attach_desired_install_store(
                self.desired_install_store
            )
        except Exception:
            self._desired_install_available = False
            logger.error(
                "native desired-install journal is unavailable: "
                "desired_install_store_unavailable"
            )
        else:
            self._desired_install_available = True
        try:
            await self.mac_inventory.initialize()
            self._desired_install_executor_available = bool(
                self._desired_install_available
                and SIGNED_LAUNCH_MATERIALIZATION_ENABLED
            )
            if self._desired_install_executor_available:
                self._desired_install_reconcile_offset = 0
                self._desired_install_reconcile_wake.clear()
                self._desired_install_reconcile_task = asyncio.create_task(
                    self._desired_install_reconcile_loop(),
                    name="mnemosyne-desired-install-reconcile",
                )
            await self.mac_inventory_sync.start()
        except Exception:
            self._mac_inventory_available = False
            self._mac_inventory_error_code = "inventory_local_unavailable"
            logger.error("native Mac inventory is unavailable: inventory_local_unavailable")
            return
        self._mac_inventory_available = True
        self._mac_inventory_error_code = None

    async def _desired_install_reconcile_loop(self) -> None:
        """Reconcile exact approved installs without touching residency."""

        while self._desired_install_executor_available:
            try:
                await asyncio.wait_for(
                    self._desired_install_reconcile_wake.wait(),
                    timeout=1.0,
                )
            except TimeoutError:
                pass
            self._desired_install_reconcile_wake.clear()
            if not self._desired_install_executor_available:
                return
            try:
                records, total = (
                    await self.desired_install_store.list_reconcilable(
                        offset=self._desired_install_reconcile_offset,
                        limit=256,
                    )
                )
                next_offset = self._desired_install_reconcile_offset + len(
                    records
                )
                self._desired_install_reconcile_offset = (
                    0 if not records or next_offset >= total else next_offset
                )
                changed = False
                async with self._desired_install_executor_lock:
                    for record in records:
                        if record.state == "awaiting_local_approval":
                            continue
                        cancellation_key: tuple[str, int, str] | None = None
                        if (
                            record.state == "cancelled"
                            and record.installation_id is not None
                        ):
                            cancellation_key = (
                                record.document.job_id,
                                record.document.job_revision,
                                record.installation_id,
                            )
                            if (
                                cancellation_key
                                in self._desired_install_confirmed_cancellations
                            ):
                                continue
                        before = record.acknowledgement().payload_digest
                        try:
                            current = (
                                await self.desired_install_executor.reconcile(
                                    record.document.job_id
                                )
                            )
                        except (
                            DesiredInstallStoreError,
                            DesiredInstallExecutorError,
                        ):
                            # One ambiguous provider/stop observation must not
                            # hide unrelated jobs later in this bounded page.
                            # The cursor rotates and the exact row remains
                            # retryable on the next scan.
                            continue
                        if cancellation_key is not None:
                            self._desired_install_confirmed_cancellations.add(
                                cancellation_key
                            )
                        changed = changed or (
                            current.acknowledgement().payload_digest != before
                        )
                if changed:
                    self.mac_inventory_sync.trigger()
            except asyncio.CancelledError:
                raise
            except (DesiredInstallStoreError, DesiredInstallExecutorError):
                # Keep the exact journal state retryable.  Provider/executor
                # failures are fixed-code management-plane outcomes and must
                # never become inference startup or maintenance failures.
                continue
            except Exception:
                logger.error(
                    "native desired-install reconciliation failed: "
                    "desired_install_internal_error"
                )

    async def _initialize_model_cleanup_journal(self) -> None:
        """Recover cross-resource cleanup without gating local inference.

        The journal is optional management-plane state.  Corruption or an
        unresolved recovery item disables further cleanup, but never changes
        model profiles, JIT admission, or the inference/accounting startup
        result merely because this auxiliary store is unavailable.
        """

        journal = self.model_cleanup_journal
        if journal is None:
            self._model_cleanup_journal_available = False
            self._model_cleanup_recovery_error_code = (
                "model_cleanup_journal_unavailable"
            )
            return
        try:
            # Cleanup recovery resolves only opaque storage IDs. Initialize
            # that private index here before ordinary inventory publication;
            # failure remains isolated from inference startup.
            await self.mac_inventory_index.initialize()
            await asyncio.to_thread(journal.initialize)
            self._model_cleanup_journal_available = True
            self._model_cleanup_recovery_error_code = None
            await self._recover_model_cleanup_transactions()
        except ModelCleanupJournalError as exc:
            self._model_cleanup_journal_available = False
            self._model_cleanup_recovery_error_code = exc.code
            logger.error("native model cleanup recovery failed: %s", exc.code)
        except Exception:
            self._model_cleanup_journal_available = False
            self._model_cleanup_recovery_error_code = (
                "model_cleanup_recovery_observation_conflict"
            )
            logger.error(
                "native model cleanup recovery failed: "
                "model_cleanup_recovery_observation_conflict"
            )

    async def _recover_model_cleanup_transactions(self) -> None:
        journal = self.model_cleanup_journal
        if journal is None:
            return
        transactions = await asyncio.to_thread(journal.list_incomplete)
        for transaction in transactions:
            if transaction.phase is CleanupPhase.MANUAL_RECOVERY:
                continue

            async def recover_one(_deadline, *, item=transaction) -> None:
                await self._recover_model_cleanup_transaction(item)

            await self.coordinator.run_empty_maintenance(
                recover_one,
                name="recover model cleanup",
            )

    async def _recover_model_cleanup_transaction(self, transaction) -> None:
        """Reconcile one immutable cleanup plan while admission is empty."""

        journal = self.model_cleanup_journal
        if journal is None:
            raise ModelCleanupJournalError(
                "model_cleanup_journal_unavailable"
            )
        if self.config_path is None:
            await asyncio.to_thread(
                journal.mark_manual_recovery,
                transaction.transaction_id,
                "config_unavailable",
            )
            return

        fresh = load_config(self.config_path, env_path=self.env_path)
        revision = configuration_revision(fresh)
        if revision == transaction.original_config_revision:
            config_state = CleanupConfigState.ORIGINAL
        elif revision == transaction.result_config_revision:
            config_state = CleanupConfigState.RESULT
        else:
            config_state = CleanupConfigState.CONFLICT
        matching_profiles = [
            item
            for item in fresh.models
            if _model_profile_fingerprint(item)
            == transaction.alias_profile_fingerprint
        ]
        profile_present = len(matching_profiles) == 1
        if len(matching_profiles) > 1:
            config_state = CleanupConfigState.CONFLICT

        if transaction.cleanup_kind is CleanupKind.MANAGED:
            (
                destination_state,
                ledger_state,
            ) = await self._managed_cleanup_recovery_observation(
                transaction.installation_id,
                fresh,
                matching_profiles[0] if profile_present else None,
            )
        else:
            # A durable trash_confirmed phase is sufficient to finish an
            # imported cleanup without retaining paths in this private
            # journal.  A prepared-only imported transaction is intentionally
            # manual: after a crash there is no exclusive managed manifest
            # with which to distinguish Trash from an unrelated removal.
            destination_state = (
                DestinationState.UNAVAILABLE
                if transaction.phase is CleanupPhase.PREPARED
                else DestinationState.MISSING
            )
            ledger_state = InstallLedgerState.NOT_APPLICABLE

        decision = decide_cleanup_recovery(
            transaction,
            destination_state=destination_state,
            profile_fingerprint_present=profile_present,
            install_ledger_state=ledger_state,
            config_state=config_state,
        )
        if decision.action is CleanupRecoveryAction.NO_ACTION:
            return
        if decision.action is CleanupRecoveryAction.ABORT_WITHOUT_MUTATION:
            # The exact destination and every durable owner are unchanged.
            # Close this transaction so repeated pre-Trash failures cannot
            # exhaust protected journal capacity. A later explicit cleanup
            # gets a fresh ID and revalidates all authority.
            await asyncio.to_thread(
                journal.abort_without_mutation,
                transaction.transaction_id,
            )
            return
        if decision.action is CleanupRecoveryAction.MANUAL_RECOVERY:
            await asyncio.to_thread(
                journal.mark_manual_recovery,
                transaction.transaction_id,
                decision.reason_code or "recovery_observation_conflict",
            )
            return

        current = transaction
        if current.phase is CleanupPhase.PREPARED:
            current = (
                await asyncio.to_thread(
                    journal.advance,
                    current.transaction_id,
                    CleanupPhase.TRASH_CONFIRMED,
                )
            ).transaction

        if decision.action is CleanupRecoveryAction.FINISH_LEDGER_AND_CONFIG:
            assert current.installation_id is not None
            install = await self.installer.get_by_id(current.installation_id)
            if install.status == "installed":
                await self.installer.mark_trashed(current.installation_id)
            elif install.status != "trashed":
                await asyncio.to_thread(
                    journal.mark_manual_recovery,
                    current.transaction_id,
                    "install_status_conflict",
                )
                return

        if current.phase is CleanupPhase.TRASH_CONFIRMED:
            current = (
                await asyncio.to_thread(
                    journal.advance,
                    current.transaction_id,
                    CleanupPhase.LEDGER_MARKED,
                )
            ).transaction

        if current.phase is CleanupPhase.LEDGER_MARKED:
            if config_state is CleanupConfigState.ORIGINAL:
                if not profile_present:
                    await asyncio.to_thread(
                        journal.mark_manual_recovery,
                        current.transaction_id,
                        "profile_conflict",
                    )
                    return
                result = fresh.model_copy(
                    update={
                        "models": [
                            item
                            for item in fresh.models
                            if _model_profile_fingerprint(item)
                            != current.alias_profile_fingerprint
                        ]
                    }
                )
                result = MacConfig.model_validate(
                    result.model_dump(mode="json")
                )
                if configuration_revision(result) != current.result_config_revision:
                    await asyncio.to_thread(
                        journal.mark_manual_recovery,
                        current.transaction_id,
                        "config_conflict",
                    )
                    return
                await asyncio.to_thread(save_config, result, self.config_path)
                fresh = result
                self.config = result
                self._apply_profiles(result)
                self.installer.storage = result.storage
            current = (
                await asyncio.to_thread(
                    journal.advance,
                    current.transaction_id,
                    CleanupPhase.CONFIG_SAVED,
                )
            ).transaction

        if current.phase is CleanupPhase.CONFIG_SAVED:
            await asyncio.to_thread(
                journal.advance,
                current.transaction_id,
                CleanupPhase.COMPLETED,
            )

    async def _managed_cleanup_recovery_observation(
        self,
        installation_id: str | None,
        config: MacConfig,
        profile: ModelProfile | None,
    ) -> tuple[DestinationState, InstallLedgerState]:
        """Observe one exact managed destination without authorizing deletion."""

        if installation_id is None:
            return DestinationState.UNAVAILABLE, InstallLedgerState.MISSING
        try:
            install = await self.installer.get_by_id(installation_id)
        except KeyError:
            return DestinationState.UNAVAILABLE, InstallLedgerState.MISSING
        if install.status == "installed":
            ledger_state = InstallLedgerState.INSTALLED
        elif install.status == "trashed":
            ledger_state = InstallLedgerState.TRASHED
        else:
            return DestinationState.UNAVAILABLE, InstallLedgerState.CONFLICT
        try:
            provenance = await asyncio.to_thread(
                self.installer.store.get_provenance,
                installation_id,
            )
            if (
                provenance.storage_location_id is None
                or provenance.storage_binding_generation is None
                or provenance.owned_files is None
                or provenance.lexical_destination != install.destination
            ):
                return DestinationState.MISMATCH, ledger_state
            binding = await self.mac_inventory_index.resolve_storage(
                provenance.storage_location_id,
                provenance.storage_binding_generation,
            )
            location = next(
                (
                    item
                    for item in config.storage.locations
                    if binding is not None and item.name == binding.local_key
                ),
                None,
            )
            if location is None or not _current_storage_binding_matches(
                provenance=provenance,
                binding=binding,
                location=location,
            ):
                return DestinationState.UNAVAILABLE, ledger_state
            if profile is not None and (
                install.alias != profile.alias
                or install.engine != profile.engine.value
                or install.storage != profile.storage
                or not _profile_uses_install_destination(profile, install)
            ):
                return DestinationState.MISMATCH, ledger_state
            root_status = await self.filesystem.inspect(
                location.path,
                expected_volume_uuid=location.volume_uuid,
                scope_id=location.scope_id,
                scope_path=location.path,
            )
            if (
                not root_status.exists
                or not root_status.is_directory
                or not root_status.volume_matches
            ):
                return DestinationState.UNAVAILABLE, ledger_state
            target_status = await self.filesystem.inspect(
                install.destination,
                expected_volume_uuid=location.volume_uuid,
                scope_id=location.scope_id,
                scope_path=location.path,
            )
            if not target_status.exists:
                return DestinationState.MISSING, ledger_state
            if not target_status.is_directory or not target_status.volume_matches:
                return DestinationState.MISMATCH, ledger_state
            try:
                await self.filesystem.verify_exact_manifest(
                    root=location.path,
                    path=install.destination,
                    files=provenance.owned_files,
                    expected_volume_uuid=location.volume_uuid,
                    scope_id=location.scope_id,
                    expected_directory_device=provenance.directory_device,
                    expected_directory_inode=provenance.directory_inode,
                    timeout_seconds=_managed_manifest_timeout(install),
                )
            except FilesystemProbeError:
                return DestinationState.MISMATCH, ledger_state
            return DestinationState.EXACT_PRESENT, ledger_state
        except (FilesystemProbeError, MacInventoryIndexError, OSError, ValueError):
            return DestinationState.UNAVAILABLE, ledger_state

    async def native_lifecycle_status(self) -> dict[str, object]:
        """Return path-free auxiliary lifecycle availability and receipts."""

        return await self.native_lifecycle.status()

    async def preview_native_uninstall(
        self,
        retention_mode: RetentionMode | str,
    ) -> dict[str, object]:
        """Build a non-executing preview from fresh local authority."""

        return await self.native_lifecycle.preview_uninstall(retention_mode)

    async def prepare_native_uninstall(
        self,
        transaction_id: str,
        retention_mode: RetentionMode | str,
    ) -> dict[str, object]:
        """Persist one immutable plan; no lifecycle effect is performed."""

        return await self.native_lifecycle.prepare_uninstall(
            transaction_id,
            retention_mode,
        )

    async def native_lifecycle_transaction(
        self,
        transaction_id: str,
    ) -> dict[str, object]:
        return await self.native_lifecycle.read(transaction_id)

    async def native_lifecycle_authorization_status(
        self,
        transaction_id: str,
    ) -> dict[str, object]:
        return await self.native_lifecycle.authorization_status(transaction_id)

    async def issue_native_lifecycle_authorization_challenge(
        self,
        transaction_id: str,
    ) -> dict[str, object]:
        return await self.native_lifecycle.issue_authorization_challenge(
            transaction_id
        )

    async def submit_native_lifecycle_authorization_receipt(
        self,
        transaction_id: str,
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        return await self.native_lifecycle.submit_authorization_receipt(
            transaction_id, receipt
        )

    async def perform_native_lifecycle_authorization(
        self,
        transaction_id: str,
    ) -> dict[str, object]:
        """Ask the service-owned bundled helper transport to authorize."""

        return await self.native_lifecycle.perform_authorization(transaction_id)

    async def cancel_native_lifecycle_authorization_challenge(
        self,
        *,
        transaction_id: str,
        nonce: str,
        session_id: str,
    ) -> dict[str, object]:
        return await self.native_lifecycle.cancel_authorization_challenge(
            transaction_id=transaction_id,
            nonce=nonce,
            session_id=session_id,
        )

    async def preview_native_migration(self) -> dict[str, object]:
        """Fail closed until signed candidate/rollback evidence is available."""

        return await self.native_lifecycle.preview_migration()

    async def mac_inventory_status(self) -> dict[str, object]:
        """Return only the path-free observation and fixed sync metadata."""

        if not self._mac_inventory_available:
            return {
                "schema_version": 1,
                "available": False,
                "last_error_code": self._mac_inventory_error_code
                or "inventory_local_unavailable",
                "sync": None,
                "inventory": None,
            }
        try:
            payload = await self.mac_inventory_sync.inspection()
        except Exception:
            return {
                "schema_version": 1,
                "available": False,
                "last_error_code": "inventory_local_unavailable",
                "sync": None,
                "inventory": None,
            }
        return {
            "schema_version": 1,
            "available": True,
            "last_error_code": None,
            "sync": payload["sync"],
            "inventory": payload["inventory"],
        }

    async def list_desired_installs(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, object]:
        """List path-free local intents and their exact action authority."""

        self._require_desired_install_store()
        records, total = await self.desired_install_store.list(
            offset=offset,
            limit=limit,
        )
        return {
            "schema_version": 1,
            "executor_available": self._desired_install_executor_available,
            "approval_available": self._desired_install_executor_available,
            "offset": offset,
            "limit": limit,
            "total": total,
            "items": [
                _desired_install_payload(
                    record,
                    executor_available=(
                        self._desired_install_executor_available
                    ),
                )
                for record in records
            ],
        }

    async def desired_install(self, job_id: str) -> dict[str, object]:
        """Read one exact intent from the private local journal."""

        self._require_desired_install_store()
        record = await self.desired_install_store.get(job_id)
        if record is None:
            raise DesiredInstallNotFoundError("desired_install_job_unknown")
        return {
            "schema_version": 1,
            "executor_available": self._desired_install_executor_available,
            "approval_available": self._desired_install_executor_available,
            "item": _desired_install_payload(
                record,
                executor_available=self._desired_install_executor_available,
            ),
        }

    async def refuse_desired_install(
        self,
        job_id: str,
        *,
        job_revision: int,
    ) -> dict[str, object]:
        """Refuse only a job that is still awaiting local approval."""

        self._require_desired_install_store()
        record = await self.desired_install_store.get(job_id)
        if record is None:
            raise DesiredInstallNotFoundError("desired_install_job_unknown")
        if record.document.job_revision != job_revision:
            raise DesiredInstallConflictError(
                "desired_install_revision_changed"
            )
        if record.state != "awaiting_local_approval" or record.terminal:
            raise DesiredInstallConflictError(
                "desired_install_state_conflict"
            )
        transition = await self.desired_install_store.transition(
            job_id=record.document.job_id,
            job_revision=job_revision,
            installation_id=record.installation_id,
            state="refused",
            bytes_downloaded=record.bytes_downloaded,
            total_bytes=record.total_bytes,
            result_code="local_policy_refused",
        )
        self.mac_inventory_sync.trigger()
        return {
            "schema_version": 1,
            "executor_available": self._desired_install_executor_available,
            "approval_available": self._desired_install_executor_available,
            "item": _desired_install_payload(
                transition.record,
                executor_available=self._desired_install_executor_available,
            ),
        }

    async def approve_desired_install(
        self,
        job_id: str,
        *,
        job_revision: int,
    ) -> dict[str, object]:
        """Start one exact job only after this loopback local approval."""

        self._require_desired_install_executor()
        record = await self.desired_install_store.get(job_id)
        if record is None:
            raise DesiredInstallNotFoundError("desired_install_job_unknown")
        if record.document.job_revision != job_revision:
            raise DesiredInstallConflictError(
                "desired_install_revision_changed"
            )
        async with self._desired_install_executor_lock:
            current = await self.desired_install_executor.approve(job_id)
        self._desired_install_reconcile_wake.set()
        self.mac_inventory_sync.trigger()
        return {
            "schema_version": 1,
            "executor_available": True,
            "approval_available": True,
            "item": _desired_install_payload(
                current,
                executor_available=True,
            ),
        }

    async def cancel_desired_install(
        self,
        job_id: str,
        *,
        job_revision: int,
    ) -> dict[str, object]:
        """Stop one locally approved install without deleting its files."""

        self._require_desired_install_executor()
        record = await self.desired_install_store.get(job_id)
        if record is None:
            raise DesiredInstallNotFoundError("desired_install_job_unknown")
        if record.document.job_revision != job_revision:
            raise DesiredInstallConflictError(
                "desired_install_revision_changed"
            )
        if record.state == "awaiting_local_approval":
            raise DesiredInstallConflictError(
                "desired_install_state_conflict"
            )
        async with self._desired_install_executor_lock:
            current = await self.desired_install_executor.cancel_locally(job_id)
        self.mac_inventory_sync.trigger()
        return {
            "schema_version": 1,
            "executor_available": True,
            "approval_available": True,
            "item": _desired_install_payload(
                current,
                executor_available=True,
            ),
        }

    def _require_desired_install_store(self) -> None:
        if not self._desired_install_available:
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            )

    def _require_desired_install_executor(self) -> None:
        self._require_desired_install_store()
        if not self._desired_install_executor_available:
            raise DesiredInstallExecutorError(
                "desired_install_internal_error"
            )

    async def compatibility_catalog_status(self) -> dict[str, object]:
        return await self.compatibility_catalog.status()

    async def compatibility_catalog_metadata(self) -> dict[str, object]:
        return await self.compatibility_catalog.metadata()

    async def check_compatibility_catalog(self) -> dict[str, object]:
        return await self.compatibility_catalog.check_now()

    async def _catalog_did_activate(self, _snapshot: object) -> None:
        """Publish a fresh inventory basis after an atomic catalog change."""

        inventory_sync = getattr(self, "mac_inventory_sync", None)
        if inventory_sync is not None:
            inventory_sync.trigger()

    async def _refresh_fleet_pairing_authority(self) -> None:
        """Refresh only the credential-authorization cache from durable state."""

        self._fleet_pairing_record = await self.fleet_pairing.status()
        self._fleet_pairing_error_code = None
        inventory_sync = getattr(self, "mac_inventory_sync", None)
        if inventory_sync is not None:
            inventory_sync.trigger()

    def _deny_fleet_pairing_authority(self) -> None:
        """Synchronously latch Fleet admission closed for self-revocation."""

        self._fleet_pairing_revocation_denied = True

    def _clear_fleet_pairing_revocation_denial(self) -> None:
        """Open only the latch whose completed revoke was atomically reset."""

        self._fleet_pairing_revocation_denied = False

    async def _fleet_pairing_workflow_status(self) -> dict[str, object]:
        if not getattr(self, "_fleet_pairing_client_available", False):
            error_code = getattr(
                self,
                "_fleet_pairing_client_error_code",
                None,
            )
            return {
                "available": False,
                "last_error_code": (
                    error_code.value
                    if error_code
                    else PairingClientErrorCode.STATE_CONFLICT.value
                ),
            }
        try:
            record = await self.fleet_pairing_client.status()
        except PairingClientError as exc:
            self._fleet_pairing_client_error_code = exc.code
            return {
                "available": False,
                "last_error_code": exc.code.value,
            }
        self._fleet_pairing_client_error_code = None
        if record is None:
            return {
                "available": True,
                "phase": None,
                "last_error_code": None,
            }
        return {"available": True, **record.public_payload()}

    async def begin_fleet_pairing(
        self,
        invitation: PairingInvitation,
    ) -> dict[str, object]:
        if not self._fleet_pairing_client_available:
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=503,
                retryable=False,
            )
        await self.fleet_pairing_client.begin(invitation)
        await self.fleet_pairing_status()
        return await self._fleet_pairing_workflow_status()

    async def resume_fleet_pairing(
        self,
        invitation: PairingInvitation,
    ) -> dict[str, object]:
        if not self._fleet_pairing_client_available:
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=503,
                retryable=False,
            )
        await self.fleet_pairing_client.resume(invitation)
        await self.fleet_pairing_status()
        return await self._fleet_pairing_workflow_status()

    async def revoke_fleet_pairing(
        self,
        *,
        request_id: str,
    ) -> dict[str, object]:
        """Durably revoke this Mac's dynamic Nyx enrollment.

        The caller owns one canonical request ID and must replay it after an
        ambiguous response.  The pairing client writes its local denial fence
        before contacting Nyx, so this operation cannot reopen pooled routing
        while recovery is pending.  Local inference, storage, weights, usage,
        and ordinary participation preferences are deliberately untouched.
        """

        if not self._fleet_pairing_client_available:
            raise PairingClientError(
                PairingClientErrorCode.STATE_CONFLICT,
                status_code=503,
                retryable=False,
            )
        result = await self.fleet_pairing_client.self_revoke_enrollment(
            request_id=request_id,
        )
        return {
            "schema_version": 1,
            "result": result.public_payload(),
            "pairing": await self.fleet_pairing_status(),
        }

    async def fleet_pairing_status(self) -> dict[str, object]:
        """Return a bounded, credential-free view of local pairing state."""

        try:
            record = await self.fleet_pairing.status()
        except Exception as exc:
            self._fleet_pairing_record = None
            self._fleet_pairing_error_code = (
                PairingErrorCode.PRIVATE_ENVIRONMENT_INVALID
                if isinstance(exc, PrivateEnvironmentInvalid)
                else PairingErrorCode.STATE_INCONSISTENT
            )
            return {
                "schema_version": 1,
                "available": False,
                "state": PairingState.RECOVERY_REQUIRED.value,
                "last_error_code": self._fleet_pairing_error_code.value,
                "workflow": await self._fleet_pairing_workflow_status(),
            }
        self._fleet_pairing_record = record
        self._fleet_pairing_error_code = None
        self_revoke: dict[str, object] | None = None
        if getattr(self, "_fleet_pairing_client_available", False):
            try:
                self_revoke = (
                    await self.fleet_pairing_client.self_revoke_status()
                )
            except PairingClientError:
                self_revoke = None
        return {
            "schema_version": 1,
            "available": True,
            "state": record.state.value,
            "device_id": record.device_id,
            "pairing_id": record.pairing_id,
            "reporting_node_id": record.node_id,
            "credential_generation": record.credential_epoch,
            "credentials_configured": record.credentials_owned,
            "legacy_credentials_present": record.legacy_credentials_present,
            "last_error_code": (
                record.last_error_code.value if record.last_error_code else None
            ),
            "updated_at": record.updated_at,
            "paired_at": record.paired_at,
            "revoked_at": record.revoked_at,
            "self_revoke": self_revoke,
            "workflow": await self._fleet_pairing_workflow_status(),
        }

    def _uses_managed_fleet_credentials(self) -> bool:
        record = self._fleet_pairing_record
        return bool(
            (record is not None and (record.credentials_owned or record.pairing_id))
            or os.environ.get("FLEET_MANAGEMENT_API_KEY", "").strip()
        )

    def fleet_snapshot_credential_active(self) -> bool:
        """Allow static nodes or paired/staged snapshot activation probes."""

        if self._fleet_pairing_revocation_denied:
            return False
        if not self._uses_managed_fleet_credentials():
            return True
        record = self._fleet_pairing_record
        return bool(
            record is not None
            and record.credentials_owned
            and not record.credential_write_pending
            and record.state in {PairingState.PENDING, PairingState.PAIRED}
        )

    def fleet_dispatch_credential_active(self, *, activation_probe: bool) -> bool:
        """Keep staged dispatch credentials out of ordinary inference."""

        if self._fleet_pairing_revocation_denied:
            return False
        if not self._uses_managed_fleet_credentials():
            return True
        record = self._fleet_pairing_record
        if not (
            record is not None
            and record.credentials_owned
            and not record.credential_write_pending
        ):
            return False
        if record.state == PairingState.PAIRED:
            return True
        return activation_probe and record.state == PairingState.PENDING

    async def _activate_configured_security_scopes(self) -> None:
        referenced: set[str] = set()
        locations: list[StorageLocationConfig] = []
        removed_unnecessary_scope = False
        for location in self.config.storage.locations:
            effective = location
            if effective.scope_id is not None:
                # Most user-selected model folders are already available to
                # this deliberately unsandboxed per-user service. Prove that
                # in the same bounded helper used by normal storage checks and
                # remove an unnecessary bookmark instead of depending on a
                # security-scope reactivation contract the process does not
                # need. Protected paths still take the strict bookmark path.
                unscoped = None
                try:
                    unscoped = await self.filesystem.inspect(
                        effective.path,
                        name=effective.name,
                        expected_volume_uuid=effective.volume_uuid,
                    )
                except FilesystemProbeError:
                    pass
                if (
                    unscoped is not None
                    and unscoped.exists
                    and unscoped.is_directory
                    and unscoped.writable
                    and unscoped.volume_matches
                ):
                    effective = effective.model_copy(update={"scope_id": None})
                    removed_unnecessary_scope = True
                else:
                    await self.security_scope_process.activate(
                        effective.scope_id,
                        effective.path,
                    )
                    referenced.add(effective.scope_id)
            locations.append(effective)

        if removed_unnecessary_scope:
            storage = self.config.storage.model_copy(update={"locations": locations})
            migrated = MacConfig.model_validate(
                self.config.model_copy(update={"storage": storage}).model_dump(
                    mode="json"
                )
            )
            if self.config_path is not None:
                save_config(migrated, self.config_path)
            self.config = migrated
            self._apply_profiles(migrated)
            self.installer.storage = migrated.storage
            logger.info(
                "removed unnecessary selected-folder grants from accessible model storage"
            )
        self.security_scopes.prune(referenced)

    async def validate_security_scopes(self, config: MacConfig) -> None:
        """Prove every selected-folder grant before persisting configuration.

        Each receiver-owned bookmark is reactivated in a bounded helper
        process. This prevents a configuration save from recording a missing,
        stale, or path-mismatched grant without leaving an executor thread
        blocked in CoreFoundation. Obsolete grants are pruned only on startup,
        after the newly persisted configuration is loaded.
        """

        for location in config.storage.locations:
            if location.scope_id is not None:
                await self.security_scope_process.activate(
                    location.scope_id,
                    location.path,
                )

    async def configuration_snapshot(self) -> tuple[MacConfig, str, str, bool]:
        """Read the latest persisted settings under the mutation lock."""

        async with self._reload_lock:
            config = (
                load_config(self.config_path, env_path=self.env_path)
                if self.config_path is not None
                else self.config
            )
            persisted_revision = configuration_revision(config)
            applied_revision = configuration_revision(self.config)
            return (
                config,
                persisted_revision,
                applied_revision,
                _restart_sensitive_configuration_changed(config, self.config),
            )

    async def save_configuration(
        self,
        config: MacConfig,
        *,
        expected_revision: str,
    ) -> tuple[bool, str]:
        """Atomically validate, compare, persist, and optionally apply settings."""

        if self.config_path is None:
            raise RuntimeConfigurationError("runtime has no configured YAML path")
        validate_exposure(config)
        async with self._reload_lock:
            current = load_config(self.config_path, env_path=self.env_path)
            current_revision = configuration_revision(current)
            if expected_revision != current_revision:
                raise ConfigurationConflict(
                    "settings changed while this window was open; reload them before saving"
                )
            await self.validate_security_scopes(config)
            restart_required = _restart_sensitive_configuration_changed(
                config,
                self.config,
            )
            save_config(config, self.config_path)
            revision = configuration_revision(config)
            if not restart_required:
                self.config = config
                self._apply_profiles(config)
                self.installer.storage = config.storage
            return restart_required, revision

    async def delete_managed_model(
        self,
        alias: str,
        *,
        expected_revision: str,
        installation_id: str | None = None,
    ) -> tuple[MacConfig, str, bool, str]:
        """Clean up one model's exact files and remove its profile.

        An exact managed installation can move its whole destination to Trash
        only with complete exclusive-ownership provenance and a current opaque
        storage binding.  The legacy request shape remains limited to the
        independently safe local-import scan path.  The coordinator barrier
        prevents new leases while filesystem and configuration mutations run.
        """

        if self.config_path is None:
            raise RuntimeConfigurationError("runtime has no configured YAML path")

        async with self._reload_lock:
            fresh = load_config(self.config_path, env_path=self.env_path)
            current_revision = configuration_revision(fresh)
            if expected_revision != current_revision:
                raise ConfigurationConflict(
                    "settings changed while this window was open; reload them before deleting"
                )
            profile = next(
                (item for item in fresh.models if item.alias == alias),
                None,
            )
            if profile is None:
                raise KeyError(f"unknown model alias '{alias}'")
            if profile.storage is None:
                raise ModelCleanupRejected(
                    "select and save a registered model storage folder before "
                    "cleaning up this model"
                )
            location = next(
                (
                    item
                    for item in fresh.storage.locations
                    if item.name == profile.storage
                ),
                None,
            )
            if location is None:
                raise ModelCleanupRejected(
                    "the model's registered storage folder is no longer configured"
                )

            managed_install: InstallRecord | None = None
            managed_provenance: InstallationProvenance | None = None
            cleanup_paths: tuple[str, ...]
            disposition = "trashed"
            omlx_model_id = (
                profile.model if profile.engine == EngineName.OMLX else None
            )

            if installation_id is not None:
                if canonical_uuid(installation_id) is None:
                    raise ModelCleanupRejected(
                        "managed cleanup requires a canonical installation ID"
                    )
                try:
                    (
                        managed_install,
                        managed_provenance,
                    ) = await self.installer.require_cleanup_authority(
                        installation_id
                    )
                except ProvenanceProofRejected as exc:
                    raise ModelCleanupRejected(
                        f"managed cleanup refused: {exc.reason.value}"
                    ) from exc
                if (
                    managed_install.alias != alias
                    or profile.engine.value != managed_install.engine
                    or profile.storage != managed_install.storage
                    or not _profile_uses_install_destination(
                        profile, managed_install
                    )
                ):
                    raise ModelCleanupRejected(
                        "the model profile no longer matches its managed download"
                    )
                assert managed_provenance.storage_location_id is not None
                assert managed_provenance.storage_binding_generation is not None
                try:
                    binding = await self.mac_inventory_index.resolve_storage(
                        managed_provenance.storage_location_id,
                        managed_provenance.storage_binding_generation,
                    )
                except MacInventoryIndexError as exc:
                    raise ModelCleanupRejected(
                        "the managed model's storage binding is unavailable"
                    ) from exc
                if not _current_storage_binding_matches(
                    provenance=managed_provenance,
                    binding=binding,
                    location=location,
                ):
                    raise ModelCleanupRejected(
                        "the managed model's storage binding has changed"
                    )
                cleanup_paths = (managed_install.destination,)
            else:
                # This hidden-inclusive lookup is denial-only. It never grants
                # cleanup authority from a destination, alias, or lifecycle
                # status; it prevents callers from omitting the exact ID and
                # downgrading a managed download into the import workflow.
                possible_managed = await self.installer.binding_records(
                    engine=profile.engine.value,
                    storage=profile.storage,
                )
                if len(possible_managed) > 10_000:
                    raise ModelCleanupRejected(
                        "managed installation history is too large to classify safely"
                    )
                if any(
                    install.status not in {"deleted", "trashed"}
                    and _profile_uses_install_destination(profile, install)
                    for install in possible_managed
                ):
                    raise ModelCleanupRejected(
                        "this profile matches managed installation history; "
                        "select its exact installation before cleaning it up"
                    )
                if profile.engine not in {
                    EngineName.LLAMA_CPP,
                    EngineName.OMLX,
                }:
                    raise ModelCleanupRejected(
                        "only llama.cpp and oMLX models can be rediscovered for "
                        "cleanup in a registered storage folder"
                    )
                status, candidates = await self.filesystem.scan(
                    location.path,
                    scope_id=location.scope_id,
                    scope_path=location.path,
                )
                volume_matches = (
                    location.volume_uuid is None
                    or (
                        status.volume_uuid is not None
                        and status.volume_uuid.casefold()
                        == location.volume_uuid.casefold()
                    )
                )
                if (
                    not status.exists
                    or not status.is_directory
                    or not status.writable
                    or not volume_matches
                ):
                    raise ModelCleanupRejected(
                        status.diagnostic
                        or "the model's registered storage folder is unavailable"
                    )

                if profile.engine == EngineName.LLAMA_CPP:
                    matches = [
                        candidate
                        for candidate in candidates
                        if candidate.engine == EngineName.LLAMA_CPP.value
                        and _lexical_path(candidate.model_path)
                        == _lexical_path(profile.model)
                    ]
                else:
                    if profile.model != Path(profile.model).name:
                        raise ModelCleanupRejected(
                            "the oMLX profile is not a rediscoverable imported model ID"
                        )
                    matches = [
                        candidate
                        for candidate in candidates
                        if candidate.engine == EngineName.OMLX.value
                        and Path(candidate.model_path).name == profile.model
                    ]
                if len(matches) != 1:
                    raise ModelCleanupRejected(
                        "the model could not be matched to exactly one item in "
                        "its registered folder; scan and import that folder again"
                    )
                candidate = matches[0]
                if profile.engine == EngineName.LLAMA_CPP:
                    targets = list(candidate.all_paths)
                    projector = profile.load.projector_path
                    if projector is not None:
                        selected_projector = next(
                            (
                                item.path
                                for item in candidate.projector_options
                                if _lexical_path(item.path)
                                == _lexical_path(projector)
                            ),
                            None,
                        )
                        if selected_projector is None:
                            raise ModelCleanupRejected(
                                "the configured projector no longer matches the "
                                "freshly scanned model"
                            )
                        targets.append(selected_projector)
                    cleanup_paths = tuple(dict.fromkeys(targets))
                    omlx_model_id = None
                else:
                    cleanup_paths = (candidate.model_path,)
                    omlx_model_id = profile.model
                if not cleanup_paths:
                    raise ModelCleanupRejected(
                        "the fresh model scan did not identify any cleanup targets"
                    )

            conflicting_alias = _cleanup_targets_overlap_other_profiles(
                fresh.models,
                alias=alias,
                targets=cleanup_paths,
                omlx_model_id=omlx_model_id,
            )
            if conflicting_alias is not None:
                raise ModelCleanupRejected(
                    "model cleanup was refused because profile "
                    f"'{conflicting_alias}' shares the selected files"
                )

            updated_config = fresh.model_copy(
                update={
                    "models": [
                        item for item in fresh.models if item.alias != alias
                    ]
                }
            )
            updated_config = MacConfig.model_validate(
                updated_config.model_dump(mode="json")
            )
            cleanup_journal = self.model_cleanup_journal
            if (
                cleanup_journal is None
                or not self._model_cleanup_journal_available
            ):
                raise ModelCleanupRejected(
                    "model cleanup is unavailable until its private recovery "
                    "journal is healthy"
                )
            cleanup_transaction_id = str(uuid4())
            cleanup_profile_fingerprint = _model_profile_fingerprint(profile)
            cleanup_result_revision = configuration_revision(updated_config)
            removed_files = False

            async def remove(deadline) -> None:
                nonlocal removed_files
                if managed_install is not None:
                    assert installation_id is not None
                    assert managed_provenance is not None
                    try:
                        (
                            current_install,
                            current_provenance,
                        ) = await self.installer.require_cleanup_authority(
                            installation_id
                        )
                    except ProvenanceProofRejected as exc:
                        raise ModelCleanupRejected(
                            f"managed cleanup refused: {exc.reason.value}"
                        ) from exc
                    if (
                        current_install != managed_install
                        or current_provenance != managed_provenance
                    ):
                        raise ModelCleanupRejected(
                            "the managed installation changed before cleanup"
                        )
                    assert current_provenance.storage_location_id is not None
                    assert (
                        current_provenance.storage_binding_generation is not None
                    )
                    try:
                        current_binding = (
                            await self.mac_inventory_index.resolve_storage(
                                current_provenance.storage_location_id,
                                current_provenance.storage_binding_generation,
                            )
                        )
                    except MacInventoryIndexError as exc:
                        raise ModelCleanupRejected(
                            "the managed model's storage binding is unavailable"
                        ) from exc
                    if not _current_storage_binding_matches(
                        provenance=current_provenance,
                        binding=current_binding,
                        location=location,
                    ):
                        raise ModelCleanupRejected(
                            "the managed model's storage binding has changed"
                        )
                    assert current_provenance.owned_files is not None
                    await self.filesystem.verify_exact_manifest(
                        root=location.path,
                        path=current_install.destination,
                        files=current_provenance.owned_files,
                        expected_volume_uuid=location.volume_uuid,
                        scope_id=location.scope_id,
                        expected_directory_device=(
                            current_provenance.directory_device
                        ),
                        expected_directory_inode=(
                            current_provenance.directory_inode
                        ),
                        timeout_seconds=_managed_manifest_timeout(
                            current_install
                        ),
                    )
                    try:
                        await asyncio.to_thread(
                            cleanup_journal.prepare,
                            transaction_id=cleanup_transaction_id,
                            installation_id=installation_id,
                            alias_profile_fingerprint=(
                                cleanup_profile_fingerprint
                            ),
                            original_config_revision=current_revision,
                            result_config_revision=cleanup_result_revision,
                            cleanup_kind=CleanupKind.MANAGED,
                        )
                    except ModelCleanupJournalError as exc:
                        raise ModelCleanupRejected(
                            "model cleanup recovery journal refused the operation"
                        ) from exc
                    removed_files = await self.filesystem.trash_paths(
                        root=location.path,
                        paths=(current_install.destination,),
                        expected_volume_uuid=location.volume_uuid,
                        scope_id=location.scope_id,
                        exact_manifest=current_provenance.owned_files,
                        expected_directory_device=(
                            current_provenance.directory_device
                        ),
                        expected_directory_inode=(
                            current_provenance.directory_inode
                        ),
                        timeout_seconds=_managed_manifest_timeout(
                            current_install
                        ),
                    )
                    if not removed_files:
                        raise FilesystemProbeError(
                            "managed model destination changed before it could "
                            "be moved to Trash"
                        )
                else:
                    try:
                        await asyncio.to_thread(
                            cleanup_journal.prepare,
                            transaction_id=cleanup_transaction_id,
                            installation_id=None,
                            alias_profile_fingerprint=(
                                cleanup_profile_fingerprint
                            ),
                            original_config_revision=current_revision,
                            result_config_revision=cleanup_result_revision,
                            cleanup_kind=CleanupKind.IMPORTED,
                        )
                    except ModelCleanupJournalError as exc:
                        raise ModelCleanupRejected(
                            "model cleanup recovery journal refused the operation"
                        ) from exc
                    removed_files = await self.filesystem.trash_paths(
                        root=location.path,
                        paths=cleanup_paths,
                        expected_volume_uuid=location.volume_uuid,
                        scope_id=location.scope_id,
                    )

                async def finalize_after_trash() -> None:
                    def commit_durable_state() -> None:
                        cleanup_journal.advance(
                            cleanup_transaction_id,
                            CleanupPhase.TRASH_CONFIRMED,
                        )
                        if installation_id is not None:
                            self.installer.store.mark_trashed(installation_id)
                        cleanup_journal.advance(
                            cleanup_transaction_id,
                            CleanupPhase.LEDGER_MARKED,
                        )
                        assert self.config_path is not None
                        save_config(updated_config, self.config_path)
                        cleanup_journal.advance(
                            cleanup_transaction_id,
                            CleanupPhase.CONFIG_SAVED,
                        )
                        cleanup_journal.advance(
                            cleanup_transaction_id,
                            CleanupPhase.COMPLETED,
                        )

                    await asyncio.to_thread(commit_durable_state)
                    self.config = updated_config
                    self._apply_profiles(updated_config)
                    self.installer.storage = updated_config.storage

                    if profile.engine == EngineName.OMLX:
                        adapter = self.adapters.get(EngineName.OMLX)
                        if isinstance(adapter, OMLXAdapter):
                            directories: list[str] = []
                            for item in updated_config.storage.locations:
                                try:
                                    model_root = (
                                        await self.filesystem.ensure_directory(
                                            root=item.path,
                                            path=os.path.join(
                                                item.path,
                                                EngineName.OMLX.value,
                                            ),
                                            expected_volume_uuid=item.volume_uuid,
                                            scope_id=item.scope_id,
                                        )
                                    )
                                except FilesystemProbeError:
                                    continue
                                directories.append(model_root)
                            directories.extend(
                                updated_config.engines.omlx.model_directories
                            )
                            directories = list(dict.fromkeys(directories))
                            if directories:
                                self._omlx_directory_sync_pending = True
                                try:
                                    await adapter.register_model_directories(
                                        directories,
                                        deadline=deadline,
                                    )
                                finally:
                                    self._omlx_directory_sync_pending = False

                finalize_task = asyncio.create_task(
                    finalize_after_trash(),
                    name=f"finalize-model-cleanup-{cleanup_transaction_id}",
                )
                await _await_owned_task(finalize_task)

            try:
                await self.coordinator.run_empty_maintenance(
                    remove,
                    name=f"clean up model {alias}",
                )
            except asyncio.CancelledError:
                # The coordinator deliberately treats cancellation as distinct
                # from an engine failure. Before Trash confirms, cleanup has
                # not committed config or ledger state; after confirmation,
                # synchronous commits already reflect the move. Re-prove
                # emptiness before propagating cancellation in either case.
                reconcile = asyncio.create_task(self.coordinator.reconcile())
                with contextlib.suppress(Exception):
                    await asyncio.shield(reconcile)
                raise
            except (FilesystemProbeError, ModelCleanupRejected):
                # Filesystem refusal/failure does not make engine state
                # uncertain. The barrier has already failed closed, so prove
                # emptiness and reopen admission instead of leaving Setup
                # degraded after a rejected cleanup request.
                with contextlib.suppress(Exception):
                    await self.coordinator.reconcile()
                raise
            return (
                updated_config,
                configuration_revision(updated_config),
                removed_files,
                disposition,
            )

    async def register_security_scope(self, path: str, bookmark_data: str) -> str:
        return (await self.security_scope_process.register(path, bookmark_data)).id

    async def discard_security_scope(self, scope_id: str) -> None:
        await asyncio.to_thread(self.security_scopes.discard, scope_id)

    async def require_security_scope(self, scope_id: str | None, path: str) -> None:
        if scope_id is None:
            return
        process = getattr(self, "security_scope_process", None)
        if process is not None:
            await process.activate(scope_id, path)

    def storage_scope_for_path(self, path: str) -> tuple[str | None, str] | None:
        return _storage_scope_for_path(self.config, path)

    async def _maintenance_loop(self) -> None:
        last_reconcile = 0.0
        while True:
            idle_seconds = self.config.server.idle_unload_seconds
            reconcile_seconds = self.config.server.reconcile_interval_seconds
            interval = min(
                reconcile_seconds,
                30.0 if idle_seconds is None else max(1.0, idle_seconds / 2),
            )
            await asyncio.sleep(interval)
            try:
                now = time.monotonic()
                if now - last_reconcile >= reconcile_seconds:
                    await self._reconcile_maintenance()
                    last_reconcile = now
                if idle_seconds is not None:
                    await self.coordinator.evict_if_idle(float(idle_seconds))
            except Exception:
                logger.exception("native maintenance iteration failed")
                continue

    async def _reconcile_maintenance(self) -> None:
        """Audit residency and retry a failed oMLX directory registration."""

        status = await self.coordinator.status()
        if not status.initialized or self.startup_error is not None:
            # Startup can race an externally launched oMLX service. Reconcile
            # is the recovery path that both proves every adapter and restores
            # admission; a read-only audit cannot initialize a failed start.
            await self.coordinator.reconcile()
        elif self._omlx_directory_sync_pending:
            # A failed maintenance barrier deliberately leaves the coordinator
            # degraded. Reconcile first so every adapter again proves its
            # observed state before retrying the residency-neutral rescan.
            await self.coordinator.reconcile()
        else:
            await self.coordinator.audit()
        if self._omlx_directory_sync_pending:
            await self._sync_omlx_model_directories()
        await self._refresh_runtime_fingerprints()
        # Pairing reconciliation is residency-neutral. A missing or tampered
        # credential bundle closes only Hub authority; local inference stays
        # available and the fixed recovery state remains visible to Settings.
        await self.fleet_pairing_status()
        self.startup_error = None

    def resolve(self, alias: str) -> ResolvedTarget:
        try:
            return self.profiles[alias]
        except KeyError as exc:
            raise KeyError(f"unknown model alias '{alias}'") from exc

    def _profile(self, alias: str) -> ModelProfile:
        profile = next(
            (candidate for candidate in self.config.models if candidate.alias == alias),
            None,
        )
        if profile is None:
            raise KeyError(f"unknown model alias '{alias}'")
        return profile

    def _apply_profiles(self, config: MacConfig) -> None:
        self.profile_candidates = config.profile_candidates()
        self.profiles = {
            alias: candidates[0]
            for alias, candidates in self.profile_candidates.items()
            if candidates
        }
        existing_records = getattr(self, "_benchmark_records", {})
        benchmark_store = getattr(self, "benchmark_store", None)
        self._benchmark_records = {
            alias: (
                existing_records[alias]
                if alias in existing_records
                else (
                    tuple(benchmark_store.list(alias=alias))
                    if benchmark_store is not None
                    else ()
                )
            )
            for alias in self.profile_candidates
        }
        existing_context = getattr(self, "_context_records", {})
        self._context_records = {
            alias: (
                existing_context[alias]
                if alias in existing_context
                else (
                    tuple(benchmark_store.list_context(alias=alias))
                    if benchmark_store is not None
                    else ()
                )
            )
            for alias in self.profile_candidates
        }
        self._apply_context_evidence()

    def _reload_benchmark_records(self, alias: str) -> None:
        self._benchmark_records[alias] = tuple(
            self.benchmark_store.list(alias=alias)
        )

    def _reload_context_records(self, alias: str) -> None:
        self._context_records[alias] = tuple(
            self.benchmark_store.list_context(alias=alias)
        )

    def _apply_signed_omlx_contracts(
        self,
        candidates_by_alias: Mapping[str, tuple[ResolvedTarget, ...]],
    ) -> dict[str, tuple[ResolvedTarget, ...]]:
        """Bind immutable install contracts to exact in-memory oMLX targets.

        The contract remains in the hidden-inclusive install journal rather
        than YAML, so an older Settings client cannot erase it while saving an
        unrelated field. This runs only while profiles are applied; request
        routing performs no SQLite reads.
        """

        installer = getattr(self, "installer", None)
        store = getattr(installer, "store", None)
        if store is None or not hasattr(store, "signed_launch_records"):
            return dict(candidates_by_alias)

        locations = {
            location.name: location for location in self.config.storage.locations
        }
        prior_index = dict(
            getattr(self, "_omlx_signed_launch_bindings", {})
        )
        next_index = dict(prior_index)
        result: dict[str, tuple[ResolvedTarget, ...]] = {}
        for alias, candidates in candidates_by_alias.items():
            if not any(
                target.key.engine == EngineName.OMLX for target in candidates
            ):
                result[alias] = candidates
                continue
            try:
                records = store.signed_launch_records(
                    alias=alias,
                    engine=EngineName.OMLX.value,
                    limit=OMLX_SIGNED_LAUNCH_RECORD_LIMIT,
                )
            except Exception:
                # Never turn an already-signed target into an ordinary local
                # target because SQLite became unavailable. Reuse only the
                # last successful result for the exact unchanged target. A
                # new or changed target has no such proof and is fenced. This
                # leaves independently proved ordinary local profiles alone.
                recovered: list[ResolvedTarget] = []
                for target in candidates:
                    if target.key.engine != EngineName.OMLX:
                        recovered.append(target)
                        continue
                    state = prior_index.get(
                        _omlx_contract_binding_key(target),
                        _OMLX_CONTRACT_CONFLICT,
                    )
                    if isinstance(state, OMLXInstallLaunch):
                        recovered.append(_target_with_omlx_launch(target, state))
                    elif state is _OMLX_CONTRACT_ORDINARY:
                        recovered.append(target)
                    else:
                        recovered.append(_target_with_omlx_launch_conflict(target))
                result[alias] = tuple(recovered)
                continue

            overflow = len(records) >= OMLX_SIGNED_LAUNCH_RECORD_LIMIT
            bound: list[ResolvedTarget] = []
            for target in candidates:
                if target.key.engine != EngineName.OMLX:
                    bound.append(target)
                    continue
                binding_key = _omlx_contract_binding_key(target)
                matching: list[InstallRecord] = []
                for record in records:
                    location = locations.get(record.storage)
                    if location is None:
                        continue
                    try:
                        expected_destination = install_destination(
                            Path(location.path),
                            EngineName.OMLX,
                            record.repo_id,
                        )
                    except (StorageError, ValueError):
                        continue
                    if (
                        target.storage_path != location.path
                        or target.scope_id != location.scope_id
                        or target.storage_volume_uuid != location.volume_uuid
                        or _lexical_path(record.destination)
                        != _lexical_path(expected_destination)
                        or Path(record.destination).name
                        != target.key.canonical_model_id
                    ):
                        continue
                    matching.append(record)

                contracts: set[OMLXInstallLaunch] = set()
                corrupt = overflow
                for record in matching:
                    try:
                        launch = record.launch_contract
                    except InstallLaunchError:
                        corrupt = True
                        continue
                    if not isinstance(launch, OMLXInstallLaunch):
                        corrupt = True
                        continue
                    contracts.add(launch)
                if corrupt or len(contracts) > 1:
                    bound.append(_target_with_omlx_launch_conflict(target))
                    next_index[binding_key] = _OMLX_CONTRACT_CONFLICT
                elif contracts:
                    contract = next(iter(contracts))
                    bound.append(
                        _target_with_omlx_launch(target, contract)
                    )
                    next_index[binding_key] = contract
                else:
                    bound.append(target)
                    next_index[binding_key] = _OMLX_CONTRACT_ORDINARY
            result[alias] = tuple(bound)
        # Drop stale cache entries only after every target represented in the
        # current configuration received either fresh or preserved authority.
        active_keys = {
            _omlx_contract_binding_key(target)
            for candidates in candidates_by_alias.values()
            for target in candidates
            if target.key.engine == EngineName.OMLX
        }
        self._omlx_signed_launch_bindings = {
            key: value for key, value in next_index.items() if key in active_keys
        }
        return result

    def _fresh_context_record(self, target: ResolvedTarget):
        runtime = getattr(self, "_runtime_fingerprints", {}).get(target.key.engine)
        if runtime is None:
            return None
        cutoff = time.time() - target.context_max_verified_age_hours * 3600
        fingerprint = context_target_fingerprint(target)
        machine = system_fingerprint()
        return next(
            (
                record
                for record in self._context_records.get(target.alias, ())
                if record.engine == target.key.engine.value
                and record.target_fingerprint == fingerprint
                and record.runtime_fingerprint == runtime
                and record.system_fingerprint == machine
                and record.suite_version == CONTEXT_SUITE_VERSION
                and record.created_at >= cutoff
            ),
            None,
        )

    def _eligible_context_record(self, target: ResolvedTarget):
        if target.context_mode != "automatic":
            return None
        return self._fresh_context_record(target)

    def _apply_context_evidence(self) -> None:
        if not hasattr(self, "profile_candidates"):
            return
        # Always start from persisted configuration. Otherwise an automatic
        # value applied during a prior refresh could survive after its runtime
        # fingerprint or age made the evidence stale.
        baseline = (
            self.config.profile_candidates()
            if hasattr(self, "config")
            else self.profile_candidates
        )
        baseline = self._apply_signed_omlx_contracts(baseline)
        updated: dict[str, tuple[ResolvedTarget, ...]] = {}
        for alias, candidates in baseline.items():
            resolved: list[ResolvedTarget] = []
            for target in candidates:
                record = self._eligible_context_record(target)
                resolved.append(
                    target_with_context_window(target, record.verified_tokens)
                    if record is not None
                    else target
                )
            updated[alias] = tuple(resolved)
        self.profile_candidates = updated
        self.profiles = {
            alias: candidates[0]
            for alias, candidates in updated.items()
            if candidates
        }

    async def _refresh_context_hints(self) -> None:
        if not hasattr(self, "profile_candidates"):
            self._context_hints = {}
            return

        async def inspect(target: ResolvedTarget) -> tuple[str, ContextWindowHint]:
            fingerprint = target_fingerprint(target)
            adapter = self.adapters[target.key.engine]
            try:
                hint = await adapter.context_window(
                    target,
                    deadline=Deadline.after(5),
                )
            except Exception:
                hint = ContextWindowHint(
                    effective_tokens=target.requested_context_length,
                    native_tokens=target.native_context_length,
                    source=(
                        "configured-load"
                        if target.requested_context_length is not None
                        else "unavailable"
                    ),
                    confidence=(
                        "authoritative"
                        if target.requested_context_length is not None
                        else "unknown"
                    ),
                )
            return fingerprint, hint

        unique = {
            target_fingerprint(target): target
            for candidates in self.profile_candidates.values()
            for target in candidates
        }
        values = await asyncio.gather(*(inspect(target) for target in unique.values()))
        self._context_hints = dict(values)

    def context_contract(self, target: ResolvedTarget) -> dict:
        hint = self._context_hints.get(target_fingerprint(target))
        record = self._fresh_context_record(target)
        effective = (
            hint.effective_tokens
            if hint is not None and hint.effective_tokens is not None
            else target.requested_context_length
        )
        native = (
            hint.native_tokens
            if hint is not None and hint.native_tokens is not None
            else target.native_context_length
        )
        verified = record.verified_tokens if record is not None else None
        guaranteed = (
            target.requested_context_length
            if target.context_mode in {"fixed", "native"}
            and target.requested_context_length is not None
            else verified
            if target.context_mode == "automatic" and verified is not None
            else effective
        )
        return {
            "mode": target.context_mode,
            "native_tokens": native,
            "configured_tokens": target.requested_context_length,
            "effective_tokens": effective,
            "verified_tokens": verified,
            "guaranteed_tokens": guaranteed,
            "source": hint.source if hint is not None else "configuration",
            "confidence": hint.confidence if hint is not None else "unknown",
        }

    async def _refresh_runtime_fingerprints(self) -> None:
        async def fingerprint(
            engine: EngineName,
            adapter: EngineAdapter,
        ) -> tuple[EngineName, str | None]:
            try:
                value = await adapter.runtime_fingerprint(deadline=Deadline.after(5))
            except Exception:
                value = None
            return engine, value

        values = await asyncio.gather(
            *(fingerprint(engine, adapter) for engine, adapter in self.adapters.items())
        )
        self._runtime_fingerprints = dict(values)
        self._apply_context_evidence()
        await self._refresh_context_hints()

    def benchmark_decision(self, alias: str) -> tuple[ResolvedTarget, dict]:
        profile = self._profile(alias)
        candidates = self.profile_candidates.get(alias)
        if not candidates:
            raise KeyError(f"unknown model alias '{alias}'")
        primary_contract = self.context_contract(candidates[0])
        context_limits = {
            target_fingerprint(candidate): self.context_contract(candidate).get(
                "guaranteed_tokens"
            )
            for candidate in candidates
        }
        selected, decision = choose_target(
            alias=alias,
            candidates=candidates,
            policy=profile.selection,
            records=self._benchmark_records.get(alias, ()),
            runtime_fingerprints=self._runtime_fingerprints,
            config_revision=candidate_set_fingerprint(candidates),
            context_limits=context_limits,
            required_context_tokens=primary_contract.get("guaranteed_tokens"),
        )
        return selected, decision.to_dict()

    async def benchmark_model(
        self,
        alias: str,
        *,
        warmup_runs: int = 1,
        sample_runs: int = 3,
        max_tokens: int = 128,
    ) -> dict:
        """Benchmark every exact chat-capable candidate sequentially."""

        profile = self._profile(alias)
        all_candidates = self.profile_candidates.get(alias, ())
        candidates = tuple(
            target
            for target in all_candidates
            if BENCHMARK_ENDPOINT in target.capabilities
        )
        if len(candidates) < 2:
            raise RuntimeConfigurationError(
                "cross-engine benchmarking requires at least two enabled chat-capable candidates"
            )
        async with self._benchmark_lock:
            await self._refresh_runtime_fingerprints()
            revision = candidate_set_fingerprint(all_candidates)
            results: list[dict] = []
            failures: list[dict[str, str]] = []
            for target in candidates:
                try:
                    await self._validate_target_storage(target)
                    adapter = self.adapters[target.key.engine]
                    record = await self.benchmarks.run_candidate(
                        target,
                        adapter=adapter,
                        config_revision=revision,
                        warmup_runs=warmup_runs,
                        sample_runs=sample_runs,
                        max_tokens=max_tokens,
                    )
                    results.append(record.to_dict())
                    if record.successful_samples == 0:
                        failures.append(
                            {
                                "engine": target.key.engine.value,
                                "code": "benchmark_failed",
                                "detail": "warmup or every measured sample failed",
                            }
                        )
                except Exception:
                    failures.append(
                        {
                            "engine": target.key.engine.value,
                            "code": "benchmark_failed",
                            "detail": (
                                "candidate could not complete the fixed benchmark; "
                                "inspect Setup & Health before retrying"
                            ),
                        }
                    )
            self._reload_benchmark_records(alias)
            await self._refresh_runtime_fingerprints()
            selected, decision = self.benchmark_decision(alias)
            # End with the selected/fallback target warm. Loading occurs only
            # after all samples, so it cannot bias any measured candidate.
            try:
                lease = await self.coordinator.acquire(selected)
                await lease.release()
            except Exception:
                pass
            return {
                "schema_version": 1,
                "alias": alias,
                "policy": profile.selection.model_dump(mode="json"),
                "results": results,
                "failures": failures,
                "decision": decision,
            }

    async def profile_model_context(
        self,
        alias: str,
        *,
        target_tokens: int | None = None,
    ) -> dict:
        """Measure usable context for every chat candidate, sequentially."""

        profile = self._profile(alias)
        candidates = tuple(
            target
            for target in self.profile_candidates.get(alias, ())
            if BENCHMARK_ENDPOINT in target.capabilities
        )
        if not candidates:
            raise RuntimeConfigurationError(
                "context profiling requires a chat-capable language model"
            )
        async with self._benchmark_lock:
            await self._refresh_runtime_fingerprints()
            results: list[dict] = []
            failures: list[dict[str, str]] = []
            for target in candidates:
                requested = target_tokens
                if requested is None:
                    requested = (
                        target.native_context_length
                        or target.requested_context_length
                        or 262_144
                    )
                if target.native_context_length is not None:
                    requested = min(requested, target.native_context_length)
                try:
                    await self._validate_target_storage(target)
                    adapter = self.adapters[target.key.engine]
                    runtime_fingerprint = await adapter.runtime_fingerprint(
                        deadline=Deadline.after(5)
                    )
                    if runtime_fingerprint is None:
                        raise RuntimeError(
                            f"{target.key.engine.value} does not expose a stable runtime identity"
                        )
                    native_result = None
                    native_error: Exception | None = None

                    async def run_native(deadline: Deadline) -> None:
                        nonlocal native_error, native_result
                        try:
                            native_result = await adapter.profile_context_window(
                                target,
                                requested,
                                deadline=deadline,
                            )
                        except Exception as exc:
                            # Let the barrier perform its post-operation empty
                            # proof before surfacing a vendor precondition or
                            # benchmark error. A rejected native benchmark is
                            # not residency uncertainty by itself.
                            native_error = exc

                    if target.key.engine == EngineName.OMLX:
                        await self.coordinator.run_empty_maintenance(
                            run_native,
                            name=f"{target.alias} oMLX context profile",
                        )
                    if native_error is not None:
                        raise native_error
                    if native_result is not None:
                        record = ContextBenchmarkRecord(
                            created_at=time.time(),
                            alias=target.alias,
                            engine=target.key.engine.value,
                            target_fingerprint=context_target_fingerprint(target),
                            runtime_fingerprint=runtime_fingerprint,
                            system_fingerprint=system_fingerprint(),
                            suite_version=CONTEXT_SUITE_VERSION,
                            requested_tokens=native_result.requested_tokens,
                            verified_tokens=native_result.verified_tokens,
                            prompt_tokens=native_result.prompt_tokens,
                        )
                        self.benchmark_store.record_context(record)
                    else:
                        record = await self.benchmarks.profile_context_candidate(
                            target,
                            adapter=adapter,
                            requested_tokens=requested,
                        )
                    results.append(record.to_dict())
                except Exception:
                    failures.append(
                        {
                            "engine": target.key.engine.value,
                            "code": "context_profile_failed",
                            "detail": (
                                "candidate could not complete a verified long-context "
                                "prefill; its prior context contract remains active"
                            ),
                        }
                    )
            self._reload_context_records(alias)
            if results:
                # A changed automatic context becomes part of the effective
                # load identity, so short-prompt speed evidence must be rerun.
                self.benchmark_store.clear_alias(alias)
                self._reload_benchmark_records(alias)
            self._apply_profiles(self.config)
            await self._refresh_runtime_fingerprints()
            return {
                "schema_version": 1,
                "alias": alias,
                "policy": profile.context.model_dump(mode="json"),
                "results": results,
                "failures": failures,
                "contexts": self.context_snapshot(alias)["models"],
            }

    def context_snapshot(self, alias: str | None = None) -> dict:
        aliases = [alias] if alias is not None else sorted(self.profile_candidates)
        models: list[dict] = []
        for candidate_alias in aliases:
            candidates = self.profile_candidates.get(candidate_alias, ())
            if not candidates:
                continue
            models.append(
                {
                    "alias": candidate_alias,
                    "candidates": [
                        {
                            "engine": target.key.engine.value,
                            "target_fingerprint": target_fingerprint(target),
                            **self.context_contract(target),
                        }
                        for target in candidates
                    ],
                }
            )
        records = self.benchmark_store.list_context(alias=alias)
        return {
            "schema_version": 1,
            "models": models,
            "records": [record.to_dict() for record in records],
        }

    def benchmark_snapshot(self, alias: str | None = None) -> dict:
        records = self.benchmark_store.list(alias=alias)
        decisions: list[dict] = []
        aliases = [alias] if alias is not None else sorted(self.profile_candidates)
        for candidate_alias in aliases:
            try:
                _target, decision = self.benchmark_decision(candidate_alias)
                decisions.append(decision)
            except KeyError:
                continue
        return {
            "schema_version": 1,
            "records": [record.to_dict() for record in records],
            "decisions": decisions,
        }

    async def _validate_target_storage(self, target: ResolvedTarget) -> None:
        if target.storage_path is None:
            return
        if target.key.engine in {EngineName.DS4, EngineName.LLAMA_CPP}:
            await self.filesystem.validate_llama(
                root=target.storage_path,
                model=target.key.canonical_model_id,
                projector=(
                    str(target.load_options["projector_path"])
                    if target.load_options.get("projector_path") is not None
                    else None
                ),
                expected_volume_uuid=target.storage_volume_uuid,
                scope_id=target.scope_id,
            )
            return
        if target.key.engine in {EngineName.MLXCEL, EngineName.MISTRAL_RS}:
            await self.filesystem.validate_directory(
                root=target.storage_path,
                path=target.key.canonical_model_id,
                expected_volume_uuid=target.storage_volume_uuid,
                scope_id=target.scope_id,
            )
            return
        status = await self.filesystem.inspect(
            target.storage_path,
            expected_volume_uuid=target.storage_volume_uuid,
            scope_id=target.scope_id,
        )
        if not status.exists or not status.is_directory or not status.volume_matches:
            raise RuntimeConfigurationError(
                status.diagnostic or f"storage for model '{target.alias}' is unavailable"
            )

    async def _require_omlx_target_contract(
        self,
        target: ResolvedTarget,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        """Revalidate one signed oMLX target without loading or mutation."""

        if target.key.engine != EngineName.OMLX:
            return
        try:
            launch = omlx_target_launch(target.load_options)
        except InstallLaunchError as exc:
            raise RuntimeConfigurationError(
                "signed oMLX launch authority is conflicting or corrupt"
            ) from exc
        if launch is None:
            return
        adapter = self.adapters.get(EngineName.OMLX)
        require_contract = getattr(adapter, "require_launch_contract", None)
        if not callable(require_contract):
            raise RuntimeConfigurationError(
                "the configured oMLX adapter cannot prove the signed launch contract"
            )
        try:
            async with asyncio.timeout(timeout_seconds):
                await require_contract(
                    launch,
                    deadline=Deadline.after(timeout_seconds),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeConfigurationError(
                "the running oMLX service does not prove the signed launch contract"
            ) from exc

    async def _omlx_target_contract_eligible(
        self,
        target: ResolvedTarget,
    ) -> bool:
        if (
            target.key.engine == EngineName.DS4
            and target.wire_model == "glm-5.3-flash"
        ):
            adapter = self.adapters.get(EngineName.DS4)
            eligible = getattr(adapter, "target_contract_eligible", None)
            return bool(callable(eligible) and await eligible(target))
        try:
            await self._require_omlx_target_contract(target)
            return True
        except (RuntimeConfigurationError, TimeoutError):
            return False

    async def resolve_target(
        self,
        alias: str,
        endpoint: Endpoint | None = None,
    ) -> ResolvedTarget:
        """Resolve a profile and validate its storage in a killable helper."""

        target, _decision = self.benchmark_decision(alias)
        primary = self.resolve(alias)
        if endpoint is not None and endpoint not in target.capabilities:
            target = primary
        if (
            self._profile(alias).selection.mode == "benchmark"
            and self.is_engine_alternative(target)
            and target.key.engine
            in {EngineName.MLXCEL, EngineName.MISTRAL_RS}
        ):
            # These Preview binaries are vendor-owned and may be replaced
            # outside Mnemosyne. A local stat-based identity check is cheap
            # enough to protect every automatically selected request without
            # adding SQLite or an upstream HTTP round-trip to the hot path.
            adapter = self.adapters[target.key.engine]
            try:
                current_runtime = await adapter.runtime_fingerprint(
                    deadline=Deadline.after(1)
                )
            except Exception:
                current_runtime = None
            if current_runtime != self._runtime_fingerprints.get(
                target.key.engine
            ):
                self._runtime_fingerprints[target.key.engine] = current_runtime
                target = primary
        await self._validate_target_storage(target)
        await self._require_omlx_target_contract(target)
        if (
            target.key.engine == EngineName.DS4
            and target.wire_model == "glm-5.3-flash"
        ):
            adapter = self.adapters.get(EngineName.DS4)
            eligible = getattr(adapter, "target_contract_eligible", None)
            if not callable(eligible) or not eligible(target):
                raise RuntimeConfigurationError(
                    "the active DS4 runtime does not prove this model contract"
                )
        return target

    async def resolve_fixed_target(
        self,
        alias: str,
        endpoint: Endpoint | None = None,
    ) -> ResolvedTarget:
        """Resolve and validate the original profile fallback."""

        target = self.resolve(alias)
        if endpoint is not None and endpoint not in target.capabilities:
            raise RuntimeConfigurationError(
                f"model '{alias}' does not support /v1/{endpoint.value}"
            )
        await self._validate_target_storage(target)
        await self._require_omlx_target_contract(target)
        if (
            target.key.engine == EngineName.DS4
            and target.wire_model == "glm-5.3-flash"
        ):
            adapter = self.adapters.get(EngineName.DS4)
            eligible = getattr(adapter, "target_contract_eligible", None)
            if not callable(eligible) or not eligible(target):
                raise RuntimeConfigurationError(
                    "the active DS4 runtime does not prove this model contract"
                )
        return target

    def reject_benchmark_evidence(self, alias: str) -> int:
        """Fail closed after a selected alternative cannot load pre-work."""

        removed = self.benchmark_store.clear_alias(alias)
        self._benchmark_records[alias] = ()
        return removed

    def reject_context_evidence(self, alias: str) -> int:
        """Discard automatic context evidence and return to configured safety."""

        self._profile(alias)
        removed = self.benchmark_store.clear_context_alias(alias)
        self._context_records[alias] = ()
        self._apply_profiles(self.config)
        return removed

    def invalidate_automatic_selection(self, alias: str) -> int:
        """Discard a failed automatic choice without overriding a user pin."""

        if self._profile(alias).selection.mode != "benchmark":
            return 0
        return self.reject_benchmark_evidence(alias)

    def is_engine_alternative(self, target: ResolvedTarget) -> bool:
        primary = self.profiles.get(target.alias)
        return bool(
            primary is not None
            and target.key.engine != primary.key.engine
        )

    async def discover_local_models(
        self,
        root_path: str,
        *,
        config: MacConfig | None = None,
        scope_id: str | None = None,
    ) -> list[LocalModel]:
        """Scan a selected root and expose oMLX leaf-ID collisions up front."""

        await self.require_security_scope(scope_id, root_path)
        selected_status, candidates = await self.filesystem.scan(
            root_path,
            scope_id=scope_id,
            scope_path=root_path,
        )
        selected_root = selected_status.path
        effective_config = config or self.config
        registered_roots: list[tuple[str, str | None, str]] = []
        for location in effective_config.storage.locations:
            registered_roots.append(
                (
                    os.path.join(location.path, EngineName.OMLX.value),
                    location.scope_id,
                    location.path,
                )
            )
        for value in effective_config.engines.omlx.model_directories:
            scoped = _storage_scope_for_path(effective_config, value)
            registered_roots.append(
                (value, scoped[0] if scoped else None, scoped[1] if scoped else value)
            )

        other_candidates: list[LocalModel] = []
        seen_roots: set[str] = {_lexical_path(selected_root)}
        for root, root_scope_id, root_scope_path in registered_roots:
            normalized = _lexical_path(root)
            if normalized in seen_roots:
                continue
            seen_roots.add(normalized)
            try:
                _status, discovered = await self.filesystem.scan(
                    root,
                    scope_id=root_scope_id,
                    scope_path=root_scope_path,
                )
            except (LocalModelError, FilesystemProbeError):
                # oMLX likewise skips unavailable roots. Profile-level duplicate
                # validation below still prevents two adopted aliases from
                # relying on its first-root-wins behavior.
                continue
            other_candidates.extend(
                item
                for item in discovered
                if item.engine == EngineName.OMLX.value
            )
        return mark_omlx_id_conflicts(candidates, other_candidates)

    async def adopt_local_models(
        self,
        root_path: str,
        selections: list[dict[str, str | None]],
        *,
        scope_id: str | None = None,
    ) -> dict:
        """Atomically adopt selected local files without copying or loading them."""

        if self.config_path is None:
            raise RuntimeConfigurationError("runtime has no configured YAML path")
        discovered = await self.discover_local_models(root_path, scope_id=scope_id)
        by_id = {candidate.id: candidate for candidate in discovered}
        if not selections:
            raise LocalModelError("select at least one local model")
        ids = [str(item.get("candidate_id") or "") for item in selections]
        if len(ids) != len(set(ids)):
            raise LocalModelError("the same local model was selected more than once")
        missing = [candidate_id for candidate_id in ids if candidate_id not in by_id]
        if missing:
            raise LocalModelError(
                "the selected model folder changed; scan it again before importing"
            )

        storage_status = await self.filesystem.inspect(
            root_path,
            scope_id=scope_id,
        )
        if not storage_status.exists or not storage_status.is_directory:
            raise LocalModelError(
                storage_status.diagnostic or "selected model folder is unavailable"
            )

        async with self._reload_lock:
            fresh = load_config(self.config_path, env_path=self.env_path)
            pending_restart = _restart_sensitive_configuration_changed(
                fresh,
                self.config,
            )
            locations = list(fresh.storage.locations)
            existing_location = next(
                (
                    location
                    for location in locations
                    if _lexical_path(location.path) == _lexical_path(storage_status.path)
                ),
                None,
            )
            storage_name = existing_location.name if existing_location is not None else None
            if (
                existing_location is not None
                and existing_location.volume_uuid is not None
                and (
                    storage_status.volume_uuid is None
                    or storage_status.volume_uuid.casefold()
                    != existing_location.volume_uuid.casefold()
                )
            ):
                raise LocalModelError(
                    "the folder is not on the volume originally selected"
                )
            if existing_location is not None and scope_id is not None:
                existing_index = locations.index(existing_location)
                existing_location = existing_location.model_copy(
                    update={"scope_id": scope_id}
                )
                locations[existing_index] = existing_location
            if storage_name is None:
                base = re.sub(
                    r"[^a-z0-9]+", "-", Path(storage_status.path).name.casefold()
                ).strip("-") or "existing-models"
                storage_name = base
                suffix = 2
                used_names = {location.name for location in locations}
                while storage_name in used_names:
                    storage_name = f"{base}-{suffix}"
                    suffix += 1
                locations.append(
                    StorageLocationConfig(
                        name=storage_name,
                        path=storage_status.path,
                        volume_uuid=storage_status.volume_uuid,
                        scope_id=scope_id,
                    )
                )

            models = list(fresh.models)
            legacy_profiles = list(
                fresh.migration.legacy_lmstudio_profiles
            )
            imported: list[dict[str, str | bool | None]] = []
            planned_aliases = {
                profile.alias for profile in [*models, *legacy_profiles]
            }
            needs_omlx = False
            for selection, candidate_id in zip(selections, ids, strict=True):
                candidate = by_id[candidate_id]
                if candidate.compatibility == "unavailable":
                    raise LocalModelError(candidate.compatibility_reason)
                existing_index = next(
                    (
                        index
                        for index, profile in enumerate(models)
                        if (
                            profile.engine == EngineName.LLAMA_CPP
                            and _lexical_path(profile.model)
                            == _lexical_path(candidate.model_path)
                        )
                        or (
                            profile.engine == EngineName.OMLX
                            and Path(profile.model).name
                            == Path(candidate.model_path).name
                            and profile.storage == storage_name
                        )
                    ),
                    None,
                )
                existing = models[existing_index] if existing_index is not None else None
                legacy = next(
                    (
                        profile
                        for profile in legacy_profiles
                        if profile.model == candidate.source_key
                    ),
                    None,
                )
                prior = existing or legacy
                requested_alias = str(selection.get("alias") or "").strip()
                alias = requested_alias or (prior.alias if prior else "")
                if not alias:
                    alias = suggested_model_alias(
                        candidate.display_name,
                        fallback="local-model",
                    )
                    base_alias = alias
                    suffix = 2
                    while alias in planned_aliases:
                        alias = f"{base_alias}-{suffix}"
                        suffix += 1
                if alias in planned_aliases and (prior is None or alias != prior.alias):
                    raise LocalModelError(f"model alias '{alias}' is already in use")

                if candidate.engine == EngineName.LLAMA_CPP.value:
                    projector_id = selection.get("projector_id")
                    include_projector = selection.get("include_projector", True) is not False
                    projector = next(
                        (
                            item
                            for item in candidate.projector_options
                            if item.id == projector_id
                        ),
                        None,
                    )
                    if projector_id and projector is None:
                        raise LocalModelError(
                            "the selected projector changed; scan the folder again"
                        )
                    if (
                        projector is None
                        and include_projector
                        and candidate.recommended_projector_id is not None
                    ):
                        projector = next(
                            (
                                item
                                for item in candidate.projector_options
                                if item.id == candidate.recommended_projector_id
                            ),
                            None,
                        )
                    prior_load = prior.load if prior is not None else ModelLoadConfig()
                    compatible_load = prior_load.model_dump(mode="python")
                    # Retain compatible tuning from an active or migrated
                    # profile while removing options llama-server cannot use.
                    compatible_load.pop("num_experts", None)
                    compatible_load.pop("kv_disk_directory", None)
                    compatible_load.pop("kv_disk_space_mb", None)
                    prior_projector = None
                    if (
                        existing is not None
                        and existing.engine == EngineName.LLAMA_CPP
                        and prior_load.projector_path is not None
                    ):
                        previous = _lexical_path(prior_load.projector_path)
                        matching_projector = next(
                            (
                                item
                                for item in candidate.projector_options
                                if _lexical_path(item.path) == previous
                            ),
                            None,
                        )
                        if matching_projector is not None:
                            prior_projector = matching_projector.path
                    compatible_load["projector_path"] = (
                        projector.path
                        if projector is not None
                        else prior_projector
                        if include_projector
                        else None
                    )
                    if (
                        compatible_load.get("context_length") is None
                    ):
                        compatible_load["context_length"] = (
                            recommended_interactive_context_length(
                                candidate.context_length
                            )
                        )
                    load = ModelLoadConfig.model_validate(compatible_load)
                    preserved_capabilities = (
                        prior.capabilities
                        if prior is not None
                        and prior.capabilities
                        and not (
                            prior.capabilities
                            & {Endpoint.EMBEDDINGS, Endpoint.RERANK}
                            and prior.capabilities
                            & {
                                Endpoint.CHAT_COMPLETIONS,
                                Endpoint.COMPLETIONS,
                                Endpoint.RESPONSES,
                                Endpoint.MESSAGES,
                            }
                        )
                        else None
                    )
                    detected_capabilities = {
                        Endpoint(value) for value in candidate.capabilities
                    }
                    capabilities = preserved_capabilities or detected_capabilities
                    profile = ModelProfile(
                        alias=alias,
                        engine=EngineName.LLAMA_CPP,
                        model=candidate.model_path,
                        storage=storage_name,
                        served_model_name=alias,
                        capabilities=capabilities,
                        load=load,
                        context=(
                            existing.context
                            if existing is not None
                            and existing.engine == EngineName.LLAMA_CPP
                            else ModelContextConfig(
                                native_tokens=candidate.context_length
                            )
                        ),
                        enabled=prior.enabled if prior is not None else True,
                    )
                else:
                    needs_omlx = True
                    model_id = Path(candidate.model_path).name
                    duplicate_profile = next(
                        (
                            item
                            for index, item in enumerate(models)
                            if item.engine == EngineName.OMLX
                            and Path(item.model).name == model_id
                            and index != existing_index
                        ),
                        None,
                    )
                    if duplicate_profile is not None:
                        raise LocalModelError(
                            f"oMLX model ID '{model_id}' is already used by "
                            f"profile '{duplicate_profile.alias}'. oMLX exposes "
                            "leaf directory names and cannot select between duplicates."
                        )
                    profile = ModelProfile(
                        alias=alias,
                        engine=EngineName.OMLX,
                        model=model_id,
                        storage=storage_name,
                        served_model_name=(
                            existing.served_model_name
                            if existing is not None
                            and existing.engine == EngineName.OMLX
                            else None
                        ),
                        capabilities={
                            Endpoint(value) for value in candidate.capabilities
                        },
                        context=(
                            existing.context
                            if existing is not None
                            and existing.engine == EngineName.OMLX
                            else ModelContextConfig(
                                native_tokens=candidate.context_length
                            )
                        ),
                        enabled=prior.enabled if prior is not None else True,
                    )
                if existing_index is None:
                    models.append(profile)
                    planned_aliases.add(alias)
                else:
                    if alias != existing.alias:
                        planned_aliases.remove(existing.alias)
                        planned_aliases.add(alias)
                    models[existing_index] = profile
                if legacy is not None:
                    legacy_profiles.remove(legacy)
                    if alias != legacy.alias:
                        planned_aliases.discard(legacy.alias)
                        planned_aliases.add(alias)
                imported.append(
                    {
                        "candidate_id": candidate.id,
                        "alias": alias,
                        "engine": candidate.engine,
                        "model_path": candidate.model_path,
                        "projector_path": (
                            profile.load.projector_path
                            if profile.engine == EngineName.LLAMA_CPP
                            else None
                        ),
                        "migrated": prior is not None,
                    }
                )

            engines = fresh.engines
            restart_required = pending_restart
            if not engines.llama_cpp.enabled and any(
                item["engine"] == EngineName.LLAMA_CPP.value for item in imported
            ):
                engines = engines.model_copy(
                    update={"llama_cpp": engines.llama_cpp.model_copy(update={"enabled": True})}
                )
                restart_required = True
            if needs_omlx:
                directories = list(engines.omlx.model_directories)
                if storage_status.path not in directories:
                    directories.append(storage_status.path)
                omlx = engines.omlx.model_copy(
                    update={"enabled": True, "model_directories": directories}
                )
                restart_required = restart_required or not engines.omlx.enabled
                engines = engines.model_copy(update={"omlx": omlx})
            storage = fresh.storage.model_copy(update={"locations": locations})
            migration = fresh.migration.model_copy(
                update={"legacy_lmstudio_profiles": legacy_profiles}
            )
            new_config = fresh.model_copy(
                update={
                    "engines": engines,
                    "storage": storage,
                    "models": models,
                    "migration": migration,
                }
            )
            # Force a full validation pass after model_copy's intentionally
            # lightweight construction and before the atomic write.
            new_config = MacConfig.model_validate(new_config.model_dump(mode="json"))
            save_config(new_config, self.config_path)
            adapters_available = all(
                profile.engine in self.adapters
                for profile in new_config.models
                if profile.enabled and new_config.engine_enabled(profile.engine)
            )
            if adapters_available and not restart_required:
                self.config = new_config
                self._apply_profiles(new_config)
                self.installer.storage = new_config.storage

        if needs_omlx and not restart_required:
            await self._sync_omlx_model_directories()
        return {
            "schema_version": new_config.schema_version,
            "imported": imported,
            "restart_required": restart_required,
            "revision": configuration_revision(new_config),
            "config": new_config.model_dump(mode="json"),
        }

    async def status(self) -> dict:
        value: CoordinatorStatus = await self.coordinator.status()
        payload = asdict(value)
        secret_env_keys = (
            self.config.server.inference_api_key_env,
            self.config.server.fleet_api_key_env,
            self.config.server.fleet_inference_api_key_env,
            self.config.server.control_password_env,
            self.config.engines.omlx.api_key_env,
            self.config.engines.omlx.admin_session_env,
        )

        def redact(value: str | None) -> str | None:
            return _redact_diagnostic(
                value,
                secret_env_keys=secret_env_keys,
            )

        payload["diagnostic"] = redact(payload.get("diagnostic"))
        payload["status"] = payload["state"]
        payload["in_flight_requests"] = payload["inflight"]
        payload["startup_error"] = redact(self.startup_error)
        payload["omlx_model_directory_sync_pending"] = (
            self._omlx_directory_sync_pending
        )
        payload["ports"] = {
            "inference": self.config.server.inference_port,
            "control": self.config.server.control_port,
        }
        usage_status = await self.usage.status()
        usage_status["last_error"] = redact(usage_status.get("last_error"))
        payload["token_sidecar"] = usage_status
        payload["performance"] = self.performance.snapshot()
        return payload

    async def fleet_snapshot(self) -> dict:
        """Return one versioned, path-free snapshot for the enrolled gateway."""

        async with self._fleet_snapshot_lock:
            async with self._reload_lock:
                targets = list(self.profiles.values())
                profiles = {
                    profile.alias: profile.model_copy(deep=True)
                    for profile in self.config.models
                    if profile.alias in self.profiles
                }
                configured_max = self.config.server.max_concurrency
                queue_limit = self.config.server.max_queue_depth

            installs = await asyncio.gather(
                *(self.installer.latest_for_alias(target.alias) for target in targets),
                return_exceptions=True,
            )
            contract_checks = await asyncio.gather(
                *(
                    self._omlx_target_contract_eligible(target)
                    for target in targets
                ),
                return_exceptions=True,
            )
            contract_eligible_by_alias = {
                target.alias: bool(result is True)
                for target, result in zip(
                    targets,
                    contract_checks,
                    strict=True,
                )
            }
            usage = await self.usage.status()
            coordinator = await self.coordinator.status()
            participation = await self.fleet_participation.status()
            participating = participation.state == FleetParticipationState.JOINED
            accounting_available = bool(
                usage.get("recording_capacity_available", True)
            )
            node_id = self.usage.identity.node_id

            identity_by_alias: dict[str, tuple[str, dict, bool]] = {}
            for target, install_result in zip(targets, installs, strict=True):
                profile = profiles[target.alias]
                install = (
                    install_result
                    if isinstance(install_result, InstallRecord)
                    else None
                )
                identity_by_alias[target.alias] = _fleet_deployment_identity(
                    node_id=node_id,
                    profile=profile,
                    target=target,
                    install=install,
                )

            deployment_id_by_alias = {
                alias: identity[0]
                for alias, identity in identity_by_alias.items()
            }
            queued_by_deployment: dict[str, int] = {}
            for alias, count in coordinator.queued_by_deployment.items():
                deployment_id = deployment_id_by_alias.get(alias)
                if deployment_id is not None:
                    queued_by_deployment[deployment_id] = (
                        queued_by_deployment.get(deployment_id, 0) + count
                    )

            authoritative = bool(
                coordinator.initialized
                and coordinator.diagnostic is None
                and coordinator.state
                not in {CoordinatorState.DEGRADED, CoordinatorState.STOPPING}
            )
            accepting = bool(
                authoritative
                and coordinator.accepting
                and participating
                and accounting_available
            )
            deployments: list[dict] = []
            for target in targets:
                profile = profiles[target.alias]
                deployment_id, identity, eligible = (
                    identity_by_alias[target.alias]
                )
                contract_eligible = contract_eligible_by_alias[target.alias]
                selection_guarantees_primary = bool(
                    profile.selection.mode == "fixed"
                    or (
                        profile.selection.mode == "pinned"
                        and profile.selection.pinned_engine == profile.engine
                    )
                )
                warm = bool(
                    coordinator.resident_alias == target.alias
                    and coordinator.resident_engine == target.key.engine
                    and coordinator.resident_model
                    == target.key.canonical_model_id
                )
                queued = queued_by_deployment.get(deployment_id, 0)
                warm_accepting = bool(
                    warm
                    and contract_eligible
                    and participating
                    and accounting_available
                    and coordinator.state == CoordinatorState.READY
                    and coordinator.capacity is not None
                    and coordinator.capacity.available > 0
                )
                capacity = self.coordinator.capacity_for(
                    target,
                    active=coordinator.inflight if warm else 0,
                    queued=queued,
                    # Deployment capacity is a cold scheduling estimate. It
                    # remains discoverable while another model is resident;
                    # a warm resident, however, cannot advertise admission
                    # while it is draining, verifying, or fenced.
                    accepting=(
                        warm_accepting
                        if warm
                        else accepting and contract_eligible
                    ),
                )
                deployments.append(
                    {
                        "alias": target.alias,
                        "deployment_id": deployment_id,
                        "identity": identity,
                        "identity_confidence": (
                            "authoritative"
                            if eligible and contract_eligible
                            else "unverified"
                        ),
                        "fleet_eligible": bool(
                            eligible
                            and contract_eligible
                            and selection_guarantees_primary
                        ),
                        "loadable": bool(
                            authoritative
                            and participating
                            and accounting_available
                            and contract_eligible
                            and selection_guarantees_primary
                        ),
                        "warm": warm,
                        "capacity": capacity.to_dict(),
                    }
                )

            resident_primary = (
                self.profiles.get(coordinator.resident_alias)
                if coordinator.resident_alias is not None
                else None
            )
            resident_deployment = (
                deployment_id_by_alias.get(resident_primary.alias)
                if resident_primary is not None
                and coordinator.resident_engine == resident_primary.key.engine
                and coordinator.resident_model
                == resident_primary.key.canonical_model_id
                else None
            )
            transition_primary = (
                self.profiles.get(coordinator.transition_target)
                if coordinator.transition_target is not None
                else None
            )
            transition_deployment = (
                deployment_id_by_alias.get(transition_primary.alias)
                if transition_primary is not None
                and coordinator.transition_engine == transition_primary.key.engine
                and coordinator.transition_model
                == transition_primary.key.canonical_model_id
                else None
            )
            root_capacity = (
                coordinator.capacity.to_dict()
                if coordinator.capacity is not None
                else effective_capacity(
                    derived_limit=1,
                    configured_max_concurrency=configured_max,
                    active=coordinator.inflight,
                    queued=coordinator.queued,
                    source="no-resident",
                    confidence="conservative",
                    accepting=False,
                ).to_dict()
            )
            if not participating or not accounting_available:
                root_capacity["available"] = 0

            if self.startup_error is not None:
                diagnostic_code = "startup_error"
            elif coordinator.state == CoordinatorState.DEGRADED:
                diagnostic_code = "coordinator_degraded"
            elif not coordinator.initialized:
                diagnostic_code = "not_initialized"
            else:
                diagnostic_code = None
            health_state = (
                "verifying"
                if coordinator.state
                in {
                    CoordinatorState.VERIFYING_EMPTY,
                    CoordinatorState.VERIFYING_TARGET,
                }
                else coordinator.state.value
            )
            if (
                diagnostic_code is None
                and participation.state == FleetParticipationState.DRAINING
            ):
                diagnostic_code = "fleet_participation_draining"
                if coordinator.state in {
                    CoordinatorState.IDLE,
                    CoordinatorState.READY,
                    CoordinatorState.DRAINING,
                }:
                    health_state = "draining"
            elif (
                diagnostic_code is None
                and participation.state == FleetParticipationState.PAUSED
            ):
                diagnostic_code = "fleet_participation_paused"
            elif diagnostic_code is None and not accounting_available:
                diagnostic_code = "usage_outbox_full"

            self._fleet_snapshot_sequence += 1
            return {
                "schema_version": FLEET_SCHEMA_VERSION,
                "snapshot_sequence": self._fleet_snapshot_sequence,
                "observed_at": time.time(),
                "node": {
                    "node_id": node_id,
                    "instance_id": self._fleet_instance_id,
                    "platform": "macos",
                    "version": __version__,
                },
                "health": {
                    "state": health_state,
                    "accepting": accepting,
                    "authoritative": authoritative,
                    "diagnostic_code": diagnostic_code,
                },
                "residency": {
                    "alias": coordinator.resident_alias,
                    "deployment_id": resident_deployment,
                    "engine": (
                        coordinator.resident_engine.value
                        if coordinator.resident_engine is not None
                        else None
                    ),
                    "epoch": coordinator.epoch,
                    "transition_target": transition_deployment,
                },
                "admission": {
                    "queue_depth": coordinator.queued,
                    "queue_limit": queue_limit,
                    "queued_by_deployment": queued_by_deployment,
                },
                "capacity": root_capacity,
                "deployments": deployments,
                "usage_delivery": {
                    "enabled": bool(usage.get("enabled")),
                    "writer_ready": bool(usage.get("writer_ready")),
                    "outbox_pending": int(usage.get("outbox_depth") or 0),
                    "last_flush_at": usage.get("last_flush_at"),
                    "last_error_code": (
                        "outbox_full"
                        if usage.get("outbox_full")
                        else "delivery_error"
                        if usage.get("last_error")
                        else None
                    ),
                },
            }

    async def readiness(self) -> dict:
        """Return bounded, actionable setup and health state without upstream checks."""

        coordinator = await self.status()
        runtime_status = await self.runtime_updates.installed_status()
        secret_env_keys = (
            self.config.server.inference_api_key_env,
            self.config.server.fleet_api_key_env,
            self.config.server.fleet_inference_api_key_env,
            self.config.server.control_password_env,
            self.config.engines.omlx.api_key_env,
            self.config.engines.omlx.admin_session_env,
        )

        def redact(value: str | None) -> str | None:
            return _redact_diagnostic(
                value,
                secret_env_keys=secret_env_keys,
            )

        async def inspect_engine(engine: EngineName) -> dict:
            enabled = self.config.engine_enabled(engine)
            release_tier = ENGINE_RELEASE_TIER[engine]
            installed = runtime_status.get(engine.value, {})
            if not enabled:
                return {
                    "engine": engine.value,
                    "release_tier": release_tier,
                    "enabled": False,
                    "installed": bool(installed.get("installed")),
                    "installed_version": installed.get("version"),
                    "installed_path": installed.get("path"),
                    "service_state": "disabled",
                    "authoritative": True,
                    "resident_models": [],
                    "ready": False,
                    "diagnostic": None,
                }
            adapter = self.adapters.get(engine)
            if adapter is None:
                return {
                    "engine": engine.value,
                    "release_tier": release_tier,
                    "enabled": True,
                    "installed": bool(installed.get("installed")),
                    "installed_version": installed.get("version"),
                    "installed_path": installed.get("path"),
                    "service_state": "unavailable",
                    "authoritative": False,
                    "resident_models": [],
                    "ready": False,
                    "diagnostic": "enabled engine has no configured adapter",
                }
            try:
                async with asyncio.timeout(5):
                    snapshot = await adapter.inspect(deadline=Deadline.after(5))
                available_states = {ServiceState.READY}
                if engine != EngineName.OMLX:
                    # Manager-owned engines are intentionally stopped while
                    # idle. An installed runtime plus authoritative empty
                    # state means it is available to launch, not unhealthy.
                    available_states.add(ServiceState.STOPPED)
                ready = (
                    bool(installed.get("installed"))
                    and snapshot.authoritative
                    and snapshot.service_state in available_states
                )
                return {
                    "engine": engine.value,
                    "release_tier": release_tier,
                    "enabled": True,
                    "installed": bool(installed.get("installed")),
                    "installed_version": installed.get("version"),
                    "installed_path": installed.get("path"),
                    "service_state": snapshot.service_state.value,
                    "authoritative": snapshot.authoritative,
                    "resident_models": [
                        item.canonical_model_id for item in snapshot.residents
                    ],
                    "ready": ready,
                    "diagnostic": redact(snapshot.diagnostic),
                }
            except TimeoutError:
                return {
                    "engine": engine.value,
                    "release_tier": release_tier,
                    "enabled": True,
                    "installed": bool(installed.get("installed")),
                    "installed_version": installed.get("version"),
                    "installed_path": installed.get("path"),
                    "service_state": "unreachable",
                    "authoritative": False,
                    "resident_models": [],
                    "ready": False,
                    "diagnostic": "engine health inspection timed out after 5 seconds",
                }
            except Exception as exc:
                return {
                    "engine": engine.value,
                    "release_tier": release_tier,
                    "enabled": True,
                    "installed": bool(installed.get("installed")),
                    "installed_version": installed.get("version"),
                    "installed_path": installed.get("path"),
                    "service_state": "unreachable",
                    "authoritative": False,
                    "resident_models": [],
                    "ready": False,
                    "diagnostic": redact(str(exc)),
                }

        engine_rows = await asyncio.gather(
            *(inspect_engine(engine) for engine in ACTIVE_ENGINE_NAMES)
        )
        storage_results = await asyncio.gather(
            *(
                self.filesystem.inspect(
                    location.path,
                    name=location.name,
                    expected_volume_uuid=location.volume_uuid,
                    scope_id=location.scope_id,
                )
                for location in self.config.storage.locations
            ),
            return_exceptions=True,
        )
        storage_rows: list[dict] = []
        for location, result in zip(
            self.config.storage.locations,
            storage_results,
            strict=True,
        ):
            if isinstance(result, BaseException):
                storage_rows.append(
                    {
                        "name": location.name,
                        "path": location.path,
                        "available": False,
                        "writable": False,
                        "volume_matches": False,
                        "free_bytes": None,
                        "diagnostic": redact(str(result)),
                    }
                )
            else:
                storage_rows.append(
                    {
                        "name": location.name,
                        "path": result.path,
                        "available": (
                            result.exists
                            and result.is_directory
                            and result.volume_matches
                        ),
                        "writable": result.writable,
                        "volume_matches": result.volume_matches,
                        "free_bytes": result.free_bytes,
                        "diagnostic": redact(result.diagnostic),
                    }
                )

        active_installs = [
            item.to_dict()
            for item in await self.installer.list(limit=100)
            if item.status in {"queued", "downloading", "registering", "cancelling"}
        ]
        core_ready = (
            coordinator["state"] in {"idle", "ready"}
            and coordinator["diagnostic"] is None
            and coordinator["startup_error"] is None
        )
        stable_engines = [
            item
            for item in engine_rows
            if item["release_tier"] == "stable" and item["enabled"] and item["ready"]
        ]
        storage_ready = any(
            item["available"] and item["writable"] for item in storage_rows
        )
        usage_status = dict(coordinator["token_sidecar"])
        usage_status["last_error"] = redact(usage_status.get("last_error"))
        return {
            "schema_version": 1,
            "product_version": __version__,
            "core": {
                "ready": core_ready,
                "state": coordinator["state"],
                "diagnostic": redact(coordinator["diagnostic"]),
                "startup_error": redact(coordinator["startup_error"]),
                "resident_alias": coordinator["resident_alias"],
                "in_flight_requests": coordinator["in_flight_requests"],
                "queued_requests": coordinator["queued"],
                "omlx_model_directory_sync_pending": coordinator[
                    "omlx_model_directory_sync_pending"
                ],
            },
            "engines": engine_rows,
            "storage": storage_rows,
            "models": {
                "configured": len(self.profiles),
                "callable": len(self.model_list()),
            },
            "downloads": {
                "active": len(active_installs),
                "items": active_installs,
            },
            "usage": usage_status,
            "ready_for_inference": bool(
                core_ready and stable_engines and storage_ready and self.model_list()
            ),
        }

    def _self_test_base_url(self) -> str:
        bind = self.config.server.inference_bind
        if bind in {"0.0.0.0", "*"}:
            bind = "127.0.0.1"
        elif bind in {"::", "[::]"}:
            bind = "::1"
        host = f"[{bind.strip('[]')}]" if ":" in bind else bind
        return f"http://{host}:{self.config.server.inference_port}"

    async def self_test(
        self,
        alias: str,
        *,
        include_vision: bool = True,
        unload_after: bool = False,
    ) -> dict:
        """Exercise the public inference listener and verify durable usage."""

        target = await self.resolve_target(alias)
        started_monotonic = time.monotonic()
        started_wall = time.time()
        headers = {"Content-Type": "application/json"}
        inference_key = os.environ.get(
            self.config.server.inference_api_key_env,
            "",
        ).strip()
        if inference_key:
            headers["Authorization"] = f"Bearer {inference_key}"

        vision = bool(
            include_vision
            and target.key.engine == EngineName.LLAMA_CPP
            and target.load_options.get("projector_path")
        )
        if Endpoint.CHAT_COMPLETIONS in target.capabilities:
            endpoint = Endpoint.CHAT_COMPLETIONS
            content: str | list[dict[str, object]]
            if vision:
                content = [
                    {
                        "type": "text",
                        "text": "Describe the test image in one short sentence.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": _SELF_TEST_VISION_IMAGE},
                    },
                ]
            else:
                content = "Tell me about alpacas in 5 sentences or less."
            payload: dict[str, object] = {
                "model": alias,
                "messages": [{"role": "user", "content": content}],
                "stream": False,
                "max_tokens": 128,
            }
        elif Endpoint.EMBEDDINGS in target.capabilities:
            endpoint = Endpoint.EMBEDDINGS
            payload = {"model": alias, "input": "alpacas"}
        elif Endpoint.RERANK in target.capabilities:
            endpoint = Endpoint.RERANK
            payload = {
                "model": alias,
                "query": "alpacas",
                "documents": ["Alpacas are camelids.", "The ocean is salty."],
            }
        elif Endpoint.IMAGES_GENERATIONS in target.capabilities:
            endpoint = Endpoint.IMAGES_GENERATIONS
            payload = {
                "model": alias,
                "prompt": "A simple icon of an alpaca.",
                "size": "256x256",
                "num_inference_steps": 1,
                "response_format": "b64_json",
            }
        else:
            raise RuntimeConfigurationError(
                f"model '{alias}' has no endpoint supported by the self-test"
            )

        try:
            response = await self.self_test_client.post(
                f"{self._self_test_base_url()}/v1/{endpoint.value}",
                headers=headers,
                json=payload,
                timeout=max(
                    self.config.server.startup_timeout_seconds
                    + self.config.server.swap_queue_timeout_seconds,
                    self.config.server.image_request_timeout_seconds
                    if endpoint == Endpoint.IMAGES_GENERATIONS
                    else 60,
                ),
            )
            response.raise_for_status()
            decoded = response.json()
            if not isinstance(decoded, dict):
                raise RuntimeConfigurationError(
                    "self-test response was not a JSON object"
                )
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:1000]
            raise RuntimeConfigurationError(
                f"self-test returned HTTP {exc.response.status_code}: {detail}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeConfigurationError(f"self-test request failed: {exc}") from exc
        finally:
            if unload_after:
                await self.coordinator.unload()

        usage = normalize_usage(decoded, endpoint=f"/v1/{endpoint.value}")
        usage_rows = await self.usage.list_usage(limit=100)
        recorded = next(
            (
                row
                for row in usage_rows
                if row.get("alias") == alias
                and float(row.get("ts") or 0) >= started_wall
            ),
            None,
        )
        response_preview: str | None = None
        if endpoint == Endpoint.CHAT_COMPLETIONS:
            try:
                response_preview = str(
                    decoded["choices"][0]["message"]["content"]
                )[:2_000]
            except (KeyError, IndexError, TypeError):
                response_preview = None
        elif endpoint == Endpoint.EMBEDDINGS:
            response_preview = f"{len(decoded.get('data', []))} embedding result(s)"
        elif endpoint == Endpoint.RERANK:
            response_preview = f"{len(decoded.get('results', []))} rerank result(s)"
        elif endpoint == Endpoint.IMAGES_GENERATIONS:
            response_preview = f"{len(decoded.get('data', []))} image result(s)"

        performance_sample = next(
            (
                item
                for item in reversed(self.performance.snapshot().get("recent", []))
                if item.get("alias") == alias
                and item.get("endpoint") == f"/v1/{endpoint.value}"
                and isinstance(item.get("observed_at"), (int, float))
                and float(item["observed_at"]) >= started_wall
            ),
            None,
        )
        cold_start = (
            performance_sample.get("cold_start")
            if isinstance(performance_sample, dict)
            and isinstance(performance_sample.get("cold_start"), bool)
            else None
        )
        post_test_status = await self.coordinator.status()
        unloaded_after = (
            bool(
                post_test_status.resident_alias is None
                and post_test_status.resident_engine is None
                and post_test_status.inflight == 0
            )
            if unload_after
            else None
        )

        runtime_validation_recorded: bool | None = None
        record_validation = getattr(
            self.runtime_updates,
            "record_validation",
            None,
        )
        externally_versioned = {EngineName.OMLX}
        if callable(record_validation) and target.key.engine not in externally_versioned:
            try:
                validation_event = await asyncio.to_thread(
                    record_validation,
                    target.key.engine.value,
                )
                runtime_validation_recorded = validation_event is not None
            except Exception:
                # Inference already succeeded. A damaged or unwritable
                # evidence journal must be visible to strict acceptance
                # collection, but it must not turn a user request into a
                # synthetic inference failure.
                runtime_validation_recorded = False

        return {
            "schema_version": 1,
            "success": True,
            "model": alias,
            "engine": target.key.engine.value,
            "release_tier": ENGINE_RELEASE_TIER[target.key.engine],
            "endpoint": f"/v1/{endpoint.value}",
            "vision": vision,
            "response_preview": response_preview,
            "response_ms": (time.monotonic() - started_monotonic) * 1000,
            "cold_start": cold_start,
            "unloaded_after": unloaded_after,
            "usage": (
                {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                }
                if usage is not None
                else None
            ),
            "usage_recorded": recorded is not None if usage is not None else None,
            "usage_delivery": await self.usage.status(),
            "runtime_validation_recorded": runtime_validation_recorded,
        }

    async def record_usage(
        self,
        event: UsageEvent,
        *,
        reservation: UsageReservationLease | None = None,
    ) -> None:
        await self.usage.record(event, reservation=reservation)

    async def _runtime_active_version(self, engine: str) -> str | None:
        if engine.casefold() == "omlx":
            try:
                status = await self.runtime_updates.installed_status()
            except Exception:
                return None
            value = status.get("omlx", {}).get("version")
            return value if isinstance(value, str) else None
        active_version = getattr(self.runtime_updates, "active_version", None)
        if not callable(active_version):
            return None
        try:
            result = await asyncio.to_thread(active_version, engine)
        except Exception:
            return None
        return result if isinstance(result, str) else None

    async def _record_runtime_lifecycle(self, **values: object) -> bool:
        recorder = getattr(self.runtime_updates, "record_lifecycle", None)
        if not callable(recorder):
            return False
        try:
            await asyncio.to_thread(recorder, **values)
        except Exception:
            # Runtime pointers are the source of truth. Once an activation or
            # rollback has happened, an evidence-storage error must not
            # masquerade as a failed pointer transition.
            return False
        return True

    async def check_runtime_updates(self, *, refresh: bool = True) -> dict:
        result = await self.runtime_updates.check(refresh=refresh)
        # Runtime checks also refresh identities used to invalidate benchmark
        # evidence after an engine update.
        await self._refresh_runtime_fingerprints()
        return result

    async def omlx_cache_health(self) -> dict:
        adapter = self.adapters.get(EngineName.OMLX)
        if not isinstance(adapter, OMLXAdapter):
            raise RuntimeConfigurationError("oMLX is not enabled")
        deadline = Deadline.after(self.config.server.transition_timeout_seconds)
        return await adapter.cache_health(deadline=deadline)

    async def reset_omlx_cache(self) -> dict:
        """Reset only oMLX's SSD KV cache after globally draining inference."""

        adapter = self.adapters.get(EngineName.OMLX)
        if not isinstance(adapter, OMLXAdapter):
            raise RuntimeConfigurationError("oMLX is not enabled")
        deleted = 0

        async def reset(deadline: Deadline) -> None:
            nonlocal deleted
            deleted = await adapter.clear_ssd_cache(deadline=deadline)

        await self.coordinator.run_empty_maintenance(
            reset,
            name="oMLX SSD cache reset",
        )
        try:
            cache = await self.omlx_cache_health()
        except Exception:
            logger.exception(
                "oMLX cache reset succeeded but refreshed cache metrics were unavailable"
            )
            cache = None
        return {
            "status": "ok",
            "deleted_files": deleted,
            "cache": cache,
        }

    async def runtime_update_evidence(self) -> dict:
        evidence_reader = getattr(
            self.runtime_updates,
            "lifecycle_evidence",
            None,
        )
        if callable(evidence_reader):
            try:
                journal = await asyncio.to_thread(evidence_reader)
            except Exception:
                journal = {
                    "schema_version": 1,
                    "valid": False,
                    "dropped_events": 0,
                    "events": [],
                    "diagnostic": "runtime lifecycle journal could not be read",
                }
        else:
            journal = {
                "schema_version": 1,
                "valid": False,
                "dropped_events": 0,
                "events": [],
                "diagnostic": "runtime lifecycle evidence is unavailable",
            }
        return {
            "schema_version": 1,
            "journal": journal,
            "installed": await self.runtime_updates.installed_status(),
        }

    async def install_runtime_update(
        self,
        engine: str,
        *,
        version: str | None = None,
        channel: str | None = None,
    ) -> dict:
        """Stage without touching residency, then activate behind the barrier."""

        if engine.casefold() == "omlx":
            if channel not in {None, "official"}:
                raise RuntimeUpdateError(
                    "oMLX does not support managed runtime channels"
                )
            return await self._upgrade_external_omlx(version=version)

        async with self._runtime_update_lock:
            active_before = await self._runtime_active_version(engine)
            evidence_recorded = [
                await self._record_runtime_lifecycle(
                    engine=engine,
                    action="install_requested",
                    outcome="started",
                    requested_version=version,
                    active_version_before=active_before,
                    active_version_after=active_before,
                )
            ]
            try:
                if channel is None:
                    prepared = await self.runtime_updates.prepare(engine, version)
                else:
                    prepared = await self.runtime_updates.prepare(
                        engine,
                        version,
                        channel=channel,
                    )
            except Exception as exc:
                evidence_recorded.append(
                    await self._record_runtime_lifecycle(
                        engine=engine,
                        action="install_rejected",
                        outcome="failed",
                        requested_version=version,
                        active_version_before=active_before,
                        active_version_after=await self._runtime_active_version(engine),
                        error=exc,
                    )
                )
                raise
            prepared_version = prepared.runtime.version
            evidence_recorded.append(
                await self._record_runtime_lifecycle(
                    engine=engine,
                    action="prepared",
                    outcome="succeeded",
                    requested_version=version,
                    prepared_version=prepared_version,
                    active_version_before=active_before,
                    active_version_after=active_before,
                    source_revision=prepared.runtime.source_revision,
                )
            )
            activated = None

            async def activate(_deadline) -> None:
                nonlocal activated
                activated = await asyncio.to_thread(
                    self.runtime_updates.activate,
                    prepared,
                )

            try:
                await self.coordinator.run_empty_maintenance(
                    activate,
                    name=f"{engine} runtime activation",
                )
            except Exception as exc:
                evidence_recorded.append(
                    await self._record_runtime_lifecycle(
                        engine=engine,
                        action="activation_rejected",
                        outcome="failed",
                        requested_version=version,
                        prepared_version=prepared_version,
                        active_version_before=active_before,
                        active_version_after=await self._runtime_active_version(engine),
                        source_revision=prepared.runtime.source_revision,
                        error=exc,
                    )
                )
                raise
            assert activated is not None
            evidence_recorded.append(
                await self._record_runtime_lifecycle(
                    engine=engine,
                    action="activated",
                    outcome="succeeded",
                    requested_version=version,
                    prepared_version=prepared_version,
                    active_version_before=active_before,
                    active_version_after=activated.version,
                    source_revision=activated.source_revision,
                )
            )
            await self._refresh_runtime_fingerprints()
            result = await self.runtime_updates.check(refresh=False)
            result["activated"] = {
                "engine": activated.engine,
                "version": activated.version,
                "source_revision": activated.source_revision,
                "channel": getattr(activated, "channel", "official"),
                "path": str(activated.root),
            }
            result["lifecycle_evidence_recorded"] = all(evidence_recorded)
            return result

    async def _upgrade_external_omlx(self, *, version: str | None) -> dict:
        """Supervise a stable Homebrew update behind the global empty barrier."""

        async with self._runtime_update_lock:
            adapter = self.adapters.get(EngineName.OMLX)
            if adapter is None:
                raise RuntimeUpdateError(
                    "enable oMLX before updating so its control plane can be drained and validated"
                )
            active_before = await self._runtime_active_version("omlx")
            evidence_recorded = [
                await self._record_runtime_lifecycle(
                    engine="omlx",
                    action="external_update_requested",
                    outcome="started",
                    requested_version=version,
                    active_version_before=active_before,
                    active_version_after=active_before,
                )
            ]
            upgrader = getattr(self.runtime_updates, "upgrade_omlx_homebrew", None)
            if not callable(upgrader):
                raise RuntimeUpdateError(
                    "this service build cannot supervise oMLX Homebrew updates"
                )
            updated_version: str | None = None
            updated_path: str | None = None

            async def upgrade(deadline: Deadline) -> None:
                nonlocal updated_version, updated_path
                updated_version, updated_path = await upgrader(version)
                snapshot = await adapter.validate_control(deadline=deadline)
                if (
                    not snapshot.authoritative
                    or snapshot.service_state != ServiceState.READY
                    or snapshot.residents
                ):
                    raise RuntimeUpdateError(
                        "updated oMLX did not return with an authoritative empty control plane: "
                        f"{snapshot.diagnostic or snapshot.service_state}"
                    )

            try:
                await self.coordinator.run_empty_maintenance(
                    upgrade,
                    name="oMLX supervised Homebrew update",
                )
            except Exception as exc:
                evidence_recorded.append(
                    await self._record_runtime_lifecycle(
                        engine="omlx",
                        action="external_update_rejected",
                        outcome="failed",
                        requested_version=version,
                        active_version_before=active_before,
                        active_version_after=await self._runtime_active_version("omlx"),
                        error=exc,
                    )
                )
                raise

            evidence_recorded.append(
                await self._record_runtime_lifecycle(
                    engine="omlx",
                    action="external_updated",
                    outcome="succeeded",
                    requested_version=version,
                    prepared_version=updated_version,
                    active_version_before=active_before,
                    active_version_after=updated_version,
                )
            )
            await self._refresh_runtime_fingerprints()
            result = await self.runtime_updates.check(refresh=False)
            result["activated"] = {
                "engine": "omlx",
                "version": updated_version,
                "source_revision": None,
                "path": updated_path,
                "external_owner": "homebrew",
            }
            result["lifecycle_evidence_recorded"] = all(evidence_recorded)
            return result

    async def rollback_runtime_update(self, engine: str) -> dict:
        async with self._runtime_update_lock:
            active_before = await self._runtime_active_version(engine)
            evidence_recorded = [
                await self._record_runtime_lifecycle(
                    engine=engine,
                    action="rollback_requested",
                    outcome="started",
                    active_version_before=active_before,
                    active_version_after=active_before,
                )
            ]
            activated = None

            async def rollback(_deadline) -> None:
                nonlocal activated
                activated = await asyncio.to_thread(
                    self.runtime_updates.rollback,
                    engine,
                )

            try:
                await self.coordinator.run_empty_maintenance(
                    rollback,
                    name=f"{engine} runtime rollback",
                )
            except Exception as exc:
                evidence_recorded.append(
                    await self._record_runtime_lifecycle(
                        engine=engine,
                        action="rollback_rejected",
                        outcome="failed",
                        active_version_before=active_before,
                        active_version_after=await self._runtime_active_version(engine),
                        error=exc,
                    )
                )
                raise
            assert activated is not None
            evidence_recorded.append(
                await self._record_runtime_lifecycle(
                    engine=engine,
                    action="rolled_back",
                    outcome="succeeded",
                    prepared_version=activated.version,
                    active_version_before=active_before,
                    active_version_after=activated.version,
                    source_revision=activated.source_revision,
                )
            )
            await self._refresh_runtime_fingerprints()
            result = await self.runtime_updates.check(refresh=False)
            result["activated"] = {
                "engine": activated.engine,
                "version": activated.version,
                "source_revision": activated.source_revision,
                "path": str(activated.root),
                "rollback": True,
            }
            result["lifecycle_evidence_recorded"] = all(evidence_recorded)
            return result

    async def _register_installed_model(self, install: InstallRecord) -> None:
        """Persist a usable profile after bytes land; never load the model."""

        if self.config_path is None:
            raise RuntimeConfigurationError("runtime has no configured YAML path")
        engine = EngineName(install.engine)
        if engine not in ACTIVE_ENGINE_NAMES:
            raise RuntimeConfigurationError(
                f"{engine.value} downloads can no longer be registered on macOS"
            )
        try:
            launch = install.launch_contract
        except InstallLaunchError as exc:
            raise RuntimeConfigurationError(
                "install record contains an invalid launch contract"
            ) from exc
        if engine == EngineName.OMLX and launch is not None:
            if not isinstance(launch, OMLXInstallLaunch):
                raise RuntimeConfigurationError(
                    "install launch contract does not match oMLX"
                )
            await self._require_omlx_install_launch(launch)
        requested_capabilities: set[Endpoint] | None = None
        if install.capabilities is not None:
            try:
                requested_capabilities = {
                    Endpoint(value) for value in install.capabilities
                }
            except ValueError as exc:
                raise RuntimeConfigurationError(
                    "install record contains an unsupported model capability"
                ) from exc
        if engine in {EngineName.DS4, EngineName.LLAMA_CPP}:
            if not install.filename:
                raise RuntimeConfigurationError(
                    f"{engine.value} install is missing its GGUF filename"
                )
            model = str(Path(install.destination) / install.filename)
            load: dict[str, object] = {}
            load["context_length"] = recommended_interactive_context_length(
                install.context_length
            )
            if install.projector_filename:
                load["projector_path"] = str(
                    Path(install.destination) / install.projector_filename
                )
            if engine == EngineName.LLAMA_CPP and launch is not None:
                if not isinstance(launch, LlamaCppInstallLaunch):
                    raise RuntimeConfigurationError(
                        "install launch contract does not match llama.cpp"
                    )
                load["parallel"] = launch.parallel_slots
                if launch.gpu_offload == "all":
                    load["gpu_layers"] = LLAMA_CPP_ALL_GPU_LAYERS
                # ``automatic`` is exactly the absence of an explicit
                # --n-gpu-layers override in the current typed profile.
                if launch.flash_attention == "enabled":
                    load["flash_attention"] = True
                elif launch.flash_attention == "disabled":
                    load["flash_attention"] = False
                # ``automatic`` likewise leaves llama.cpp's reviewed default.
            elif engine == EngineName.DS4 and launch is not None:
                if not isinstance(launch, DS4InstallLaunch):
                    raise RuntimeConfigurationError(
                        "install launch contract does not match DS4"
                    )
                # execution_mode is closed to single-node by the durable
                # parser; DS4's typed parallel setting owns --batched-session.
                load["parallel"] = launch.batched_sessions
            profile = ModelProfile(
                alias=install.alias,
                engine=engine,
                model=model,
                storage=install.storage,
                served_model_name=(
                    install.family if engine == EngineName.DS4 else install.alias
                ),
                capabilities=requested_capabilities,
                load=load,
                context=ModelContextConfig(native_tokens=install.context_length),
            )
        elif engine == EngineName.MFLUX:
            from .model_library import image_profile_defaults, verified_model

            candidate = verified_model(
                engine=engine,
                repo_id=install.repo_id,
                filename=install.filename,
            )
            if candidate is None or candidate.family != install.family:
                raise RuntimeConfigurationError("MFLUX install is missing a supported family")
            profile = ModelProfile(
                alias=install.alias,
                engine=engine,
                model=install.destination,
                storage=install.storage,
                kind="image",
                image=ImageProfileConfig(**image_profile_defaults(candidate)),
            )
        elif engine == EngineName.OMLX:
            storage = self.installer.storage or self.config.storage
            location = next(
                (
                    candidate
                    for candidate in storage.locations
                    if candidate.name == install.storage
                ),
                None,
            )
            if location is None:
                raise RuntimeConfigurationError(
                    f"oMLX install references unknown storage location "
                    f"'{install.storage}'"
                )
            try:
                _status, candidates = await self.filesystem.scan(
                    install.destination,
                    scope_id=location.scope_id,
                    scope_path=location.path,
                )
            except (FilesystemProbeError, LocalModelError) as exc:
                raise RuntimeConfigurationError(
                    f"failed to inspect downloaded oMLX metadata: {exc}"
                ) from exc
            destination = _lexical_path(install.destination)
            matches = [
                candidate
                for candidate in candidates
                if candidate.engine == EngineName.OMLX.value
                and _lexical_path(candidate.model_path) == destination
            ]
            if len(matches) != 1:
                raise RuntimeConfigurationError(
                    "downloaded oMLX snapshot did not contain exactly one "
                    "model at its destination"
                )
            candidate = matches[0]
            if candidate.compatibility == "unavailable" or not candidate.capabilities:
                raise RuntimeConfigurationError(
                    "downloaded oMLX model cannot be registered: "
                    f"{candidate.compatibility_reason}"
                )
            try:
                detected_capabilities = {
                    Endpoint(value) for value in candidate.capabilities
                }
            except ValueError as exc:
                raise RuntimeConfigurationError(
                    "downloaded oMLX metadata advertised an unsupported capability"
                ) from exc
            if (
                requested_capabilities is not None
                and detected_capabilities != requested_capabilities
            ):
                selected = ", ".join(
                    sorted(endpoint.value for endpoint in requested_capabilities)
                )
                detected = ", ".join(
                    sorted(endpoint.value for endpoint in detected_capabilities)
                )
                raise RuntimeConfigurationError(
                    "downloaded oMLX metadata does not match the selected model "
                    f"role (selected: {selected}; detected: {detected})"
                )
            profile = ModelProfile(
                alias=install.alias,
                engine=engine,
                # oMLX's two-level discovery keeps owner/model folders on
                # disk but exposes the model directory name as its model ID.
                model=Path(candidate.model_path).name,
                storage=install.storage,
                capabilities=detected_capabilities,
                context=ModelContextConfig(
                    native_tokens=candidate.context_length
                ),
            )
        elif engine in {EngineName.MLXCEL, EngineName.MISTRAL_RS}:
            profile = ModelProfile(
                alias=install.alias,
                engine=engine,
                model=install.destination,
                storage=install.storage,
                capabilities=requested_capabilities,
                context=ModelContextConfig(native_tokens=install.context_length),
            )
        else:
            raise RuntimeConfigurationError("unsupported native download engine")

        applied = False
        async with self._reload_lock:
            fresh = load_config(self.config_path, env_path=self.env_path)
            require_binding = getattr(
                self.installer,
                "require_registration_storage_binding",
                None,
            )
            if require_binding is not None:
                await require_binding(install, fresh.storage)
            else:
                # Small direct-construction tests and embedders predating the
                # installer guard still fail closed when persisted Settings
                # rebound this storage name away from the applied snapshot.
                applied_location = next(
                    (
                        item
                        for item in self.installer.storage.locations
                        if item.name == install.storage
                    ),
                    None,
                )
                fresh_location = next(
                    (
                        item
                        for item in fresh.storage.locations
                        if item.name == install.storage
                    ),
                    None,
                )
                if (
                    applied_location is None
                    or fresh_location is None
                    or fresh_location != applied_location
                ):
                    raise RuntimeConfigurationError(
                        "registration_storage_binding_changed"
                    )
            existing = next(
                (candidate for candidate in fresh.models if candidate.alias == install.alias),
                None,
            )
            if existing is not None:
                if existing.engine == profile.engine and existing.model == profile.model:
                    if (
                        requested_capabilities is not None
                        and existing.resolve().capabilities
                        != frozenset(requested_capabilities)
                    ):
                        raise RuntimeConfigurationError(
                            f"model alias '{install.alias}' exists with a different role"
                        )
                    if existing.storage != profile.storage:
                        raise RuntimeConfigurationError(
                            f"model alias '{install.alias}' exists with a different "
                            "storage binding"
                        )
                    if launch is not None and (
                        existing.load != profile.load
                        or existing.served_model_name != profile.served_model_name
                    ):
                        raise RuntimeConfigurationError(
                            f"model alias '{install.alias}' exists with a different "
                            "signed launch contract or storage binding"
                        )
                    if engine == EngineName.OMLX:
                        # A previous attempt may have persisted the profile
                        # and then failed its residency-neutral directory
                        # refresh. Reapply the exact current config and retry
                        # that proof instead of declaring registration done.
                        if not _restart_sensitive_configuration_changed(
                            fresh,
                            self.config,
                        ):
                            self.config = fresh
                            self._apply_profiles(fresh)
                            self.installer.storage = fresh.storage
                        await self._sync_omlx_model_directories()
                        if isinstance(launch, OMLXInstallLaunch):
                            await self._require_omlx_install_launch(launch)
                    return
                raise RuntimeConfigurationError(
                    f"model alias '{install.alias}' was added while the download was running"
                )
            new_config = fresh.model_copy(update={"models": [*fresh.models, profile]})
            save_config(new_config, self.config_path)
            if not _restart_sensitive_configuration_changed(fresh, self.config):
                self.config = new_config
                self._apply_profiles(new_config)
                self.installer.storage = new_config.storage
                applied = True
        if engine == EngineName.OMLX and applied:
            await self._sync_omlx_model_directories()
            if isinstance(launch, OMLXInstallLaunch):
                await self._require_omlx_install_launch(launch)

    async def _require_omlx_install_launch(
        self,
        launch: OMLXInstallLaunch,
    ) -> None:
        """Prove a signed global contract without changing external settings."""

        adapter = self.adapters.get(EngineName.OMLX)
        require_contract = getattr(adapter, "require_launch_contract", None)
        if not callable(require_contract):
            raise RuntimeConfigurationError(
                "the configured oMLX adapter cannot prove signed launch settings"
            )
        timeout = min(
            30.0,
            self.config.server.swap_queue_timeout_seconds,
        )
        try:
            async with asyncio.timeout(timeout):
                await require_contract(
                    launch,
                    deadline=Deadline.after(timeout),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeConfigurationError(
                "the running oMLX service does not match the signed launch settings"
            ) from exc

    async def _sync_omlx_model_directories(self) -> None:
        adapter = self.adapters.get(EngineName.OMLX)
        if not isinstance(adapter, OMLXAdapter):
            self._omlx_directory_sync_pending = False
            return
        directories: list[str] = []
        for location in self.config.storage.locations:
            try:
                model_root = await self.filesystem.ensure_directory(
                    root=location.path,
                    path=os.path.join(location.path, EngineName.OMLX.value),
                    expected_volume_uuid=location.volume_uuid,
                    scope_id=location.scope_id,
                )
            except FilesystemProbeError:
                continue
            directories.append(model_root)
        directories.extend(self.config.engines.omlx.model_directories)
        directories = list(dict.fromkeys(directories))
        if not directories:
            self._omlx_directory_sync_pending = False
            return
        self._omlx_directory_sync_pending = True

        async def register(deadline) -> None:
            await adapter.register_model_directories(directories, deadline=deadline)

        await self.coordinator.run_empty_maintenance(
            register,
            name="oMLX model-library rescan",
        )
        self._omlx_directory_sync_pending = False

    def model_list(self) -> list[dict]:
        rows: list[dict] = []
        for primary in self.profiles.values():
            selected, decision = self.benchmark_decision(primary.alias)
            context = self.context_contract(selected)
            rows.append(
                {
                    "id": primary.alias,
                    "object": "model",
                    "owned_by": "mnemosyne",
                    "engine": selected.key.engine,
                    "fallback_engine": primary.key.engine,
                    "upstream_model": selected.key.canonical_model_id,
                    # Alternatives optimize a subset of the established
                    # profile contract; they never expand or remove it.
                    "capabilities": sorted(
                        endpoint.value for endpoint in primary.capabilities
                    ),
                    "model_kind": primary.kind,
                    "load_config_digest": selected.key.load_config_digest,
                    # OpenAI-compatible clients commonly recognize
                    # max_model_len. The structured companion preserves how
                    # that guaranteed value was obtained.
                    "max_model_len": context["guaranteed_tokens"],
                    "context_window": context,
                    "selection": decision,
                    "candidate_engines": [
                        item.key.engine.value
                        for item in self.profile_candidates.get(primary.alias, (primary,))
                    ],
                    "candidate_contexts": [
                        {
                            "engine": item.key.engine.value,
                            **self.context_contract(item),
                        }
                        for item in self.profile_candidates.get(primary.alias, (primary,))
                    ],
                }
            )
        return rows

    def fleet_probe_model_list(self) -> list[dict[str, str]]:
        """Return the minimal path-free catalog used only to prove dispatch auth.

        The ordinary local ``/v1/models`` response intentionally retains
        engine-facing identifiers used by existing clients and Settings. Some
        of those identifiers are absolute paths. A Hub-marked activation probe
        therefore receives only public aliases and fixed OpenAI metadata; the
        authoritative path-free deployment inventory remains snapshot-owned.
        """

        return [
            {
                "id": primary.alias,
                "object": "model",
                "owned_by": "mnemosyne",
            }
            for primary in self.profiles.values()
        ]

    async def reload_profiles(self) -> None:
        if self.config_path is None:
            raise RuntimeConfigurationError("runtime has no configured YAML path")
        async with self._reload_lock:
            new_config = load_config(self.config_path, env_path=self.env_path)
            validate_exposure(new_config)
            if new_config.server != self.config.server:
                raise RestartRequired("server settings changed; restart the LaunchAgent")
            if new_config.engines != self.config.engines:
                raise RestartRequired("engine settings changed; restart the LaunchAgent")
            if new_config.paths != self.config.paths:
                raise RestartRequired("service paths changed; restart the LaunchAgent")
            if new_config.storage != self.config.storage:
                raise RestartRequired("model storage changed; restart the LaunchAgent")
            if new_config.token_sidecar != self.config.token_sidecar:
                raise RestartRequired("token sidecar settings changed; restart the LaunchAgent")
            self.config = new_config
            self._apply_profiles(new_config)


__all__ = [
    "ConfigurationConflict",
    "NativeRuntime",
    "RestartRequired",
    "RuntimeConfigurationError",
    "build_adapters",
    "configuration_revision",
    "validate_exposure",
]
