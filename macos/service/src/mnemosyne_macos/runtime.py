"""Composition root for the native macOS coordinator service."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import time
from typing import Mapping

import httpx

from .config import (
    ImageProfileConfig,
    MacConfig,
    ModelLoadConfig,
    ModelProfile,
    StorageLocationConfig,
    load_config,
    save_config,
    suggested_model_alias,
)
from .coordinator import CoordinatorStatus, ResidencyCoordinator
from .engines.base import EngineAdapter
from .engines.ds4 import DS4Adapter
from .engines.lmstudio import LMStudioAdapter
from .engines.llamacpp import LlamaCppAdapter
from .engines.mflux import MFluxAdapter
from .engines.omlx import OMLXAdapter
from .filesystem import FilesystemProbe, FilesystemProbeError
from .models import Endpoint, EngineName, ResolvedTarget
from .install_store import InstallRecord
from .installer import NativeInstaller
from .local_models import (
    LocalModel,
    LocalModelError,
    mark_omlx_id_conflicts,
)
from .usage import UsageEvent
from .usage_delivery import UsageService
from .runtime_updates import RuntimeUpdateManager
from .scope_process import SecurityScopeProcess
from .security_scopes import SecurityScopeRegistry


class RuntimeConfigurationError(RuntimeError):
    pass


class RestartRequired(RuntimeConfigurationError):
    pass


class ConfigurationConflict(RuntimeConfigurationError):
    pass


def configuration_revision(config: MacConfig) -> str:
    payload = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    if not _is_loopback(config.server.inference_bind):
        key = os.environ.get(config.server.inference_api_key_env, "").strip()
        if not key:
            raise RuntimeConfigurationError(
                "a non-loopback inference bind requires an inference API key"
            )
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
    if config.engines.lmstudio.enabled:
        adapters[EngineName.LMSTUDIO] = LMStudioAdapter(config.engines.lmstudio)
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
            {profile.key.engine for profile in self.profiles.values()} - self.adapters.keys()
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
        )
        self.proxy_client = proxy_client or httpx.AsyncClient(timeout=None)
        self._owns_proxy_client = proxy_client is None
        self.usage = usage_service or UsageService.open(
            config.paths.state_database,
            config.token_sidecar,
        )
        self.installer = NativeInstaller(
            config.paths.state_database,
            on_installed=self._register_installed_model,
            storage=config.storage,
            filesystem_probe=self.filesystem,
        )
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
        try:
            await self._activate_configured_security_scopes()
            await self.coordinator.initialize()
            await self._sync_omlx_model_directories()
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
        await self.installer.start()

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
                        await asyncio.to_thread(self.security_scopes.close)

    async def _activate_configured_security_scopes(self) -> None:
        referenced: set[str] = set()
        for location in self.config.storage.locations:
            if location.scope_id is not None:
                await self.security_scope_process.activate(
                    location.scope_id,
                    location.path,
                )
                referenced.add(location.scope_id)
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
                self.profiles = config.profiles()
                self.installer.storage = config.storage
            return restart_required, revision

    async def register_security_scope(self, path: str, bookmark_data: str) -> str:
        return (await self.security_scope_process.register(path, bookmark_data)).id

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
                # Status exposes coordinator diagnostics. A future logging
                # integration records the traceback without killing upkeep.
                continue

    async def _reconcile_maintenance(self) -> None:
        """Audit residency and retry a failed oMLX directory registration."""

        if self._omlx_directory_sync_pending:
            # A failed maintenance barrier deliberately leaves the coordinator
            # degraded. Reconcile first so every adapter again proves its
            # observed state before retrying the residency-neutral rescan.
            await self.coordinator.reconcile()
            await self._sync_omlx_model_directories()
        else:
            await self.coordinator.audit()
        self.startup_error = None

    def resolve(self, alias: str) -> ResolvedTarget:
        try:
            return self.profiles[alias]
        except KeyError as exc:
            raise KeyError(f"unknown model alias '{alias}'") from exc

    async def resolve_target(self, alias: str) -> ResolvedTarget:
        """Resolve a profile and validate its storage in a killable helper."""

        target = self.resolve(alias)
        profile = next(
            (candidate for candidate in self.config.models if candidate.alias == alias),
            None,
        )
        if profile is not None and profile.storage is not None:
            location = next(
                (
                    candidate
                    for candidate in self.config.storage.locations
                    if candidate.name == profile.storage
                ),
                None,
            )
            if location is None:
                raise RuntimeConfigurationError(
                    f"model '{alias}' references missing storage '{profile.storage}'"
                )
            if target.key.engine in {EngineName.DS4, EngineName.LLAMA_CPP}:
                await self.filesystem.validate_llama(
                    root=location.path,
                    model=target.key.canonical_model_id,
                    projector=(
                        str(target.load_options["projector_path"])
                        if target.load_options.get("projector_path") is not None
                        else None
                    ),
                    expected_volume_uuid=location.volume_uuid,
                    scope_id=location.scope_id,
                )
                return target
            status = await self.filesystem.inspect(
                location.path,
                name=location.name,
                expected_volume_uuid=location.volume_uuid,
                scope_id=location.scope_id,
            )
            if not status.exists or not status.is_directory or not status.volume_matches:
                raise RuntimeConfigurationError(
                    status.diagnostic or f"storage for model '{alias}' is unavailable"
                )
        return target

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
            imported: list[dict[str, str | bool | None]] = []
            planned_aliases = {profile.alias for profile in models}
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
                            profile.engine == EngineName.LMSTUDIO
                            and profile.model == candidate.source_key
                        )
                        or (
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
                requested_alias = str(selection.get("alias") or "").strip()
                alias = requested_alias or (existing.alias if existing else "")
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
                if alias in planned_aliases and (existing is None or alias != existing.alias):
                    raise LocalModelError(f"model alias '{alias}' is already in use")

                if candidate.engine == EngineName.LLAMA_CPP.value:
                    projector_id = selection.get("projector_id")
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
                    prior = existing.load if existing is not None else ModelLoadConfig()
                    compatible_load = prior.model_dump(mode="python")
                    # These settings belong to LM Studio/DS4 and have no
                    # llama-server equivalent. Preserve every other typed
                    # option, including advanced llama.cpp tuning and
                    # extra_args, when re-adopting an existing profile.
                    compatible_load.pop("num_experts", None)
                    compatible_load.pop("kv_disk_directory", None)
                    compatible_load.pop("kv_disk_space_mb", None)
                    prior_projector = None
                    if (
                        existing is not None
                        and existing.engine == EngineName.LLAMA_CPP
                        and prior.projector_path is not None
                    ):
                        previous = _lexical_path(prior.projector_path)
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
                        projector.path if projector is not None else prior_projector
                    )
                    load = ModelLoadConfig.model_validate(compatible_load)
                    capabilities = (
                        existing.capabilities
                        if existing is not None
                        and existing.capabilities
                        and not (
                            existing.capabilities
                            & {Endpoint.EMBEDDINGS, Endpoint.RERANK}
                            and existing.capabilities
                            & {
                                Endpoint.CHAT_COMPLETIONS,
                                Endpoint.COMPLETIONS,
                                Endpoint.RESPONSES,
                                Endpoint.MESSAGES,
                            }
                        )
                        else None
                    )
                    profile = ModelProfile(
                        alias=alias,
                        engine=EngineName.LLAMA_CPP,
                        model=candidate.model_path,
                        storage=storage_name,
                        served_model_name=alias,
                        capabilities=capabilities,
                        load=load,
                        enabled=existing.enabled if existing is not None else True,
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
                        enabled=existing.enabled if existing is not None else True,
                    )
                if existing_index is None:
                    models.append(profile)
                    planned_aliases.add(alias)
                else:
                    if alias != existing.alias:
                        planned_aliases.remove(existing.alias)
                        planned_aliases.add(alias)
                    models[existing_index] = profile
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
                        "migrated": existing is not None,
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
            new_config = fresh.model_copy(
                update={"engines": engines, "storage": storage, "models": models}
            )
            # Force a full validation pass after model_copy's intentionally
            # lightweight construction and before the atomic write.
            new_config = MacConfig.model_validate(new_config.model_dump(mode="json"))
            save_config(new_config, self.config_path)
            adapters_available = all(
                profile.engine in self.adapters for profile in new_config.models if profile.enabled
            )
            if adapters_available and not restart_required:
                self.config = new_config
                self.profiles = new_config.profiles()
                self.installer.storage = new_config.storage

        if needs_omlx and not restart_required:
            await self._sync_omlx_model_directories()
        return {
            "schema_version": 1,
            "imported": imported,
            "restart_required": restart_required,
            "revision": configuration_revision(new_config),
            "config": new_config.model_dump(mode="json"),
        }

    async def status(self) -> dict:
        value: CoordinatorStatus = await self.coordinator.status()
        payload = asdict(value)
        payload["status"] = payload["state"]
        payload["in_flight_requests"] = payload["inflight"]
        payload["startup_error"] = self.startup_error
        payload["omlx_model_directory_sync_pending"] = (
            self._omlx_directory_sync_pending
        )
        payload["ports"] = {
            "inference": self.config.server.inference_port,
            "control": self.config.server.control_port,
        }
        payload["token_sidecar"] = await self.usage.status()
        return payload

    async def record_usage(self, event: UsageEvent) -> None:
        await self.usage.record(event)

    async def check_runtime_updates(self, *, refresh: bool = True) -> dict:
        return await self.runtime_updates.check(refresh=refresh)

    async def install_runtime_update(
        self, engine: str, *, version: str | None = None
    ) -> dict:
        """Stage without touching residency, then activate behind the barrier."""

        async with self._runtime_update_lock:
            prepared = await self.runtime_updates.prepare(engine, version)
            activated = None

            async def activate(_deadline) -> None:
                nonlocal activated
                activated = self.runtime_updates.activate(prepared)

            await self.coordinator.run_empty_maintenance(
                activate,
                name=f"{engine} runtime activation",
            )
            assert activated is not None
            result = await self.runtime_updates.check(refresh=False)
            result["activated"] = {
                "engine": activated.engine,
                "version": activated.version,
                "source_revision": activated.source_revision,
                "path": str(activated.root),
            }
            return result

    async def rollback_runtime_update(self, engine: str) -> dict:
        async with self._runtime_update_lock:
            activated = None

            async def rollback(_deadline) -> None:
                nonlocal activated
                activated = self.runtime_updates.rollback(engine)

            await self.coordinator.run_empty_maintenance(
                rollback,
                name=f"{engine} runtime rollback",
            )
            assert activated is not None
            result = await self.runtime_updates.check(refresh=False)
            result["activated"] = {
                "engine": activated.engine,
                "version": activated.version,
                "source_revision": activated.source_revision,
                "path": str(activated.root),
                "rollback": True,
            }
            return result

    async def _register_installed_model(self, install: InstallRecord) -> None:
        """Persist a usable profile after bytes land; never load the model."""

        if self.config_path is None:
            raise RuntimeConfigurationError("runtime has no configured YAML path")
        engine = EngineName(install.engine)
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
            load = {}
            if install.projector_filename:
                load["projector_path"] = str(
                    Path(install.destination) / install.projector_filename
                )
            profile = ModelProfile(
                alias=install.alias,
                engine=engine,
                model=model,
                storage=install.storage,
                served_model_name=install.alias,
                capabilities=requested_capabilities,
                load=load,
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
            )
        else:
            raise RuntimeConfigurationError("unsupported native download engine")

        applied = False
        async with self._reload_lock:
            fresh = load_config(self.config_path, env_path=self.env_path)
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
                    return
                raise RuntimeConfigurationError(
                    f"model alias '{install.alias}' was added while the download was running"
                )
            new_config = fresh.model_copy(update={"models": [*fresh.models, profile]})
            save_config(new_config, self.config_path)
            if not _restart_sensitive_configuration_changed(fresh, self.config):
                self.config = new_config
                self.profiles = new_config.profiles()
                self.installer.storage = new_config.storage
                applied = True
        if engine == EngineName.OMLX and applied:
            await self._sync_omlx_model_directories()

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
        return [
            {
                "id": target.alias,
                "object": "model",
                "owned_by": "mnemosyne",
                "engine": target.key.engine,
                "upstream_model": target.key.canonical_model_id,
                "capabilities": sorted(endpoint.value for endpoint in target.capabilities),
                "model_kind": target.kind,
                "load_config_digest": target.key.load_config_digest,
            }
            for target in self.profiles.values()
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
            self.profiles = new_config.profiles()


__all__ = [
    "ConfigurationConflict",
    "NativeRuntime",
    "RestartRequired",
    "RuntimeConfigurationError",
    "build_adapters",
    "configuration_revision",
    "validate_exposure",
]
