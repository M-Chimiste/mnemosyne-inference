"""Node-local authority adapters for the DesiredInstall executor.

The Hub job is intentionally path-free and carries no authority to choose a
directory, runtime, or pairing generation.  This module translates the
current :class:`NativeRuntime`-owned state into the narrow provider records
consumed by :mod:`mnemosyne_macos.desired_install_executor`.

Every operation is read-only and bounded.  It never creates a directory,
starts or loads an engine, mutates configuration, or changes residency.  The
path-bearing storage record is local-only; failures are represented by fixed
state or ``None`` and never raise source diagnostics containing a path.
"""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any, Mapping

from .desired_install_executor import (
    InventoryAuthority,
    LocalStorageAuthority,
    PairingAuthority,
    RuntimeAuthority,
)
from .engines.base import Deadline
from .fleet_pairing import PairingState
from .models import ENGINE_RELEASE_TIER, EngineName, ServiceState


DEFAULT_STORAGE_RESERVE_BYTES = 5 * 1024**3
DEFAULT_AUTHORITY_TIMEOUT_SECONDS = 5.0
DEFAULT_INSTALLED_STATUS_TIMEOUT_SECONDS = 20.0

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_FEATURE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_DESIRED_ENGINES = frozenset(
    {EngineName.LLAMA_CPP, EngineName.OMLX, EngineName.DS4}
)
_MANAGER_OWNED_ENGINES = frozenset({EngineName.LLAMA_CPP, EngineName.DS4})


class NativeDesiredInstallAuthorities:
    """Implement the executor's four local provider protocols.

    ``remote_installs_allowed`` in the returned storage record means only
    that this provider is used by the executor *after* its explicit local
    approval transition.  It is not a persisted Hub permission and this
    adapter deliberately implements neither an automatic ``allow`` policy nor
    a remote approval surface.
    """

    def __init__(
        self,
        runtime: Any,
        *,
        authority_timeout_seconds: float = DEFAULT_AUTHORITY_TIMEOUT_SECONDS,
        installed_status_timeout_seconds: float = (
            DEFAULT_INSTALLED_STATUS_TIMEOUT_SECONDS
        ),
        storage_reserve_bytes: int = DEFAULT_STORAGE_RESERVE_BYTES,
    ) -> None:
        if (
            isinstance(authority_timeout_seconds, bool)
            or not isinstance(authority_timeout_seconds, (int, float))
            or not math.isfinite(float(authority_timeout_seconds))
            or authority_timeout_seconds <= 0
        ):
            raise ValueError("desired_install_authority_timeout_invalid")
        if (
            isinstance(installed_status_timeout_seconds, bool)
            or not isinstance(installed_status_timeout_seconds, (int, float))
            or not math.isfinite(float(installed_status_timeout_seconds))
            or installed_status_timeout_seconds <= 0
        ):
            raise ValueError("desired_install_installed_timeout_invalid")
        if (
            isinstance(storage_reserve_bytes, bool)
            or not isinstance(storage_reserve_bytes, int)
            or storage_reserve_bytes < 0
        ):
            raise ValueError("desired_install_storage_reserve_invalid")
        self._runtime = runtime
        self._authority_timeout_seconds = float(authority_timeout_seconds)
        self._installed_status_timeout_seconds = float(
            installed_status_timeout_seconds
        )
        self._storage_reserve_bytes = storage_reserve_bytes

    async def current_pairing(self) -> PairingAuthority:
        """Return only the exact active pairing and credential generation."""

        try:
            record = await asyncio.wait_for(
                self._runtime.fleet_pairing.status(),
                timeout=self._authority_timeout_seconds,
            )
            pairing_id = record.pairing_id
            generation = record.credential_epoch
            active = bool(
                record.state == PairingState.PAIRED
                and isinstance(pairing_id, str)
                and pairing_id
                and isinstance(generation, int)
                and not isinstance(generation, bool)
                and generation > 0
                and record.credentials_owned
                and not record.credential_write_pending
            )
            return PairingAuthority(
                active=active,
                pairing_id=pairing_id if isinstance(pairing_id, str) else None,
                credential_generation=(
                    generation
                    if isinstance(generation, int)
                    and not isinstance(generation, bool)
                    else None
                ),
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException:
            # Provider exceptions can otherwise become path-bearing runtime
            # diagnostics.  Inactive authority makes the executor refuse the
            # job through its fixed pairing-generation result code.
            return PairingAuthority(
                active=False,
                pairing_id=None,
                credential_generation=None,
            )

    async def current_inventory(self) -> InventoryAuthority:
        """Return the latest produced identity/sequence, never a Hub value.

        A sequence newer than the desired job basis remains valid.  The
        executor performs the ``current >= basis`` comparison.  A pairing-
        generation mismatch makes the sequence zero so an inventory produced
        under old credentials cannot authorize a new job.
        """

        producer = getattr(self._runtime, "mac_inventory", None)
        instance_id = getattr(producer, "instance_id", "")
        if not isinstance(instance_id, str):
            instance_id = ""
        try:
            inspection = await asyncio.wait_for(
                self._runtime.mac_inventory_sync.inspection(),
                timeout=self._authority_timeout_seconds,
            )
            document = inspection.get("inventory")
            sync = inspection.get("sync")
            if not isinstance(document, Mapping) or not isinstance(sync, Mapping):
                raise ValueError
            document_instance = document.get("inventory_instance_id")
            sync_instance = sync.get("inventory_instance_id")
            sequence = document.get("inventory_sequence")
            if (
                not instance_id
                or document_instance != instance_id
                or sync_instance != instance_id
                or isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 1
            ):
                raise ValueError
            pairing = await self.current_pairing()
            if (
                not pairing.active
                or document.get("pairing_id") != pairing.pairing_id
                or document.get("credential_generation")
                != pairing.credential_generation
            ):
                raise ValueError
            return InventoryAuthority(
                inventory_instance_id=instance_id,
                inventory_sequence=sequence,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException:
            return InventoryAuthority(
                inventory_instance_id=instance_id,
                inventory_sequence=0,
            )

    async def resolve_storage(
        self,
        storage_location_id: str,
        binding_generation: int,
    ) -> LocalStorageAuthority | None:
        """Re-resolve and freshly inspect one exact opaque storage binding."""

        try:
            binding = await asyncio.wait_for(
                self._runtime.mac_inventory_index.resolve_storage(
                    storage_location_id,
                    binding_generation,
                ),
                timeout=self._authority_timeout_seconds,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException:
            return None
        if binding is None:
            return None

        locations = tuple(self._runtime.config.storage.locations)
        location = next(
            (
                candidate
                for candidate in locations
                if candidate.name == binding.local_key
            ),
            None,
        )
        if location is None:
            return None

        # The private index is a fence, not an alias lookup.  Never inspect a
        # newly configured path when the durable opaque binding names older
        # path/volume/scope authority.
        exact_binding = bool(
            binding.storage_location_id == storage_location_id
            and binding.binding_generation == binding_generation
            and binding.exact_path == location.path
            and binding.volume_uuid == location.volume_uuid
            and binding.scope_id == location.scope_id
        )
        if not exact_binding:
            return _storage_binding_changed(
                binding=binding,
                location=location,
                reserve_bytes=self._storage_reserve_bytes,
            )

        try:
            status = await asyncio.wait_for(
                self._runtime.filesystem.inspect(
                    location.path,
                    name=location.name,
                    expected_volume_uuid=location.volume_uuid,
                    scope_id=location.scope_id,
                    scope_path=location.path,
                ),
                timeout=self._authority_timeout_seconds,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException:
            return _storage_unavailable(
                binding=binding,
                location=location,
                reserve_bytes=self._storage_reserve_bytes,
            )

        observed_path = status.path if isinstance(status.path, str) else ""
        availability = _storage_availability(status)
        free_bytes = _optional_nonnegative_int(status.free_bytes)
        return LocalStorageAuthority(
            storage_location_id=binding.storage_location_id,
            binding_generation=binding.binding_generation,
            local_storage_name=location.name,
            indexed_lexical_root=binding.exact_path,
            indexed_volume_uuid=binding.volume_uuid,
            indexed_scope_id=binding.scope_id,
            configured_lexical_root=location.path,
            configured_volume_uuid=location.volume_uuid,
            configured_scope_id=location.scope_id,
            observed_lexical_root=observed_path,
            observed_volume_uuid=(
                status.volume_uuid if isinstance(status.volume_uuid, str) else None
            ),
            availability=availability,
            exists=bool(status.exists),
            is_directory=bool(status.is_directory),
            writable=bool(status.writable),
            volume_matches=bool(status.volume_matches),
            free_bytes=free_bytes,
            reserve_bytes=self._storage_reserve_bytes,
            # DesiredInstallExecutor.approve is the sole entrance to this
            # provider.  The Hub cannot set or bypass this local approval.
            remote_installs_allowed=True,
        )

    async def current_runtime(
        self,
        engine: str,
        catalog_digest: str,
    ) -> RuntimeAuthority | None:
        """Inspect one enabled runtime without starting it or loading a model."""

        try:
            engine_name = EngineName(engine)
        except (TypeError, ValueError):
            return None
        if engine_name not in _DESIRED_ENGINES:
            return None

        try:
            catalog = await asyncio.wait_for(
                self._runtime.compatibility_catalog.snapshot(),
                timeout=self._authority_timeout_seconds,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException:
            return None
        if (
            getattr(catalog, "source", None) != "signed"
            or getattr(catalog, "catalog_digest", None) != catalog_digest
            or not _SHA256_RE.fullmatch(catalog_digest)
        ):
            return None

        enabled = bool(self._runtime.config.engine_enabled(engine_name))
        installed: Mapping[str, Any] = {}
        try:
            statuses = await asyncio.wait_for(
                self._runtime.runtime_updates.installed_status(),
                timeout=self._installed_status_timeout_seconds,
            )
            candidate = statuses.get(engine_name.value, {})
            if isinstance(candidate, Mapping):
                installed = candidate
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException:
            installed = {}

        present = installed.get("installed") is True
        version = _runtime_version(engine_name, installed)
        adapter = self._runtime.adapters.get(engine_name)
        healthy = False
        fingerprint = (
            _runtime_compatibility_fingerprint(installed)
            if engine_name in _MANAGER_OWNED_ENGINES
            else None
        )
        features = (
            _runtime_features(installed)
            if engine_name in _MANAGER_OWNED_ENGINES
            else ()
        )
        omlx_scheduler_slots: int | None = None
        omlx_memory_guard_enabled: bool | None = None
        if enabled and present and adapter is not None:
            try:
                async with asyncio.timeout(self._authority_timeout_seconds):
                    inspect_launch = None
                    if engine_name == EngineName.OMLX:
                        inspect_launch = getattr(
                            adapter,
                            "inspect_launch_contract",
                            None,
                        )
                        if not callable(inspect_launch):
                            raise RuntimeError(
                                "desired_install_omlx_contract_unavailable"
                            )
                    observations = [
                        adapter.inspect(
                            deadline=Deadline.after(
                                self._authority_timeout_seconds
                            )
                        ),
                    ]
                    if engine_name == EngineName.OMLX:
                        assert inspect_launch is not None
                        observations.append(
                            inspect_launch(
                                deadline=Deadline.after(
                                    self._authority_timeout_seconds
                                )
                            )
                        )
                    results = await asyncio.gather(*observations)
                    snapshot = results[0]
                    if engine_name == EngineName.OMLX:
                        evidence = results[1]
                        slots = getattr(evidence, "scheduler_slots", None)
                        guard = getattr(evidence, "memory_guard_enabled", None)
                        if (
                            not isinstance(slots, int)
                            or isinstance(slots, bool)
                            or not 1 <= slots <= 1024
                            or not isinstance(guard, bool)
                        ):
                            raise RuntimeError(
                                "desired_install_omlx_contract_unavailable"
                            )
                        omlx_scheduler_slots = slots
                        omlx_memory_guard_enabled = guard
                allowed_states = {ServiceState.READY}
                if engine_name in _MANAGER_OWNED_ENGINES:
                    allowed_states.add(ServiceState.STOPPED)
                healthy = bool(
                    snapshot.authoritative
                    and snapshot.engine == engine_name
                    and snapshot.service_state in allowed_states
                    # Ordinary inference deliberately keeps legacy/configured
                    # manager-owned runtimes usable. Remote DesiredInstall is
                    # narrower: it requires freshly validated managed-runtime
                    # provenance plus the local executable integrity check.
                    and (
                        engine_name not in _MANAGER_OWNED_ENGINES
                        or fingerprint is not None
                    )
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except BaseException:
                healthy = False
                omlx_scheduler_slots = None
                omlx_memory_guard_enabled = None

        return RuntimeAuthority(
            engine=engine_name.value,
            enabled=enabled,
            healthy=healthy,
            release_tier=ENGINE_RELEASE_TIER[engine_name],
            version=version,
            runtime_fingerprint=fingerprint,
            # Feature evidence must come from the locally verified runtime
            # record. A signed recipe may require a feature; it cannot claim
            # that the installed binary implements it.
            features=features,
            catalog_digest=catalog_digest,
            omlx_scheduler_slots=omlx_scheduler_slots,
            omlx_memory_guard_enabled=omlx_memory_guard_enabled,
        )


def _storage_binding_changed(
    *,
    binding: Any,
    location: Any,
    reserve_bytes: int,
) -> LocalStorageAuthority:
    """Build a local-only record that deterministically fails the bind fence."""

    return LocalStorageAuthority(
        storage_location_id=str(binding.storage_location_id),
        binding_generation=int(binding.binding_generation),
        local_storage_name=str(location.name),
        indexed_lexical_root=str(binding.exact_path),
        indexed_volume_uuid=binding.volume_uuid,
        indexed_scope_id=binding.scope_id,
        configured_lexical_root=str(location.path),
        configured_volume_uuid=location.volume_uuid,
        configured_scope_id=location.scope_id,
        observed_lexical_root="",
        observed_volume_uuid=None,
        availability="unavailable",
        exists=False,
        is_directory=False,
        writable=False,
        volume_matches=False,
        free_bytes=None,
        reserve_bytes=reserve_bytes,
        remote_installs_allowed=True,
    )


def _storage_unavailable(
    *,
    binding: Any,
    location: Any,
    reserve_bytes: int,
) -> LocalStorageAuthority:
    """Represent a failed exact probe without retaining its diagnostics."""

    return LocalStorageAuthority(
        storage_location_id=str(binding.storage_location_id),
        binding_generation=int(binding.binding_generation),
        local_storage_name=str(location.name),
        indexed_lexical_root=str(binding.exact_path),
        indexed_volume_uuid=binding.volume_uuid,
        indexed_scope_id=binding.scope_id,
        configured_lexical_root=str(location.path),
        configured_volume_uuid=location.volume_uuid,
        configured_scope_id=location.scope_id,
        observed_lexical_root=str(location.path),
        observed_volume_uuid=None,
        availability="unavailable",
        exists=False,
        is_directory=False,
        writable=False,
        volume_matches=False,
        free_bytes=None,
        reserve_bytes=reserve_bytes,
        remote_installs_allowed=True,
    )


def _storage_availability(status: Any) -> str:
    if not bool(status.exists):
        return "missing"
    if not bool(status.volume_matches):
        return "wrong_volume"
    if not bool(status.is_directory):
        return "unavailable"
    if not bool(status.writable):
        return "read_only"
    return "available"


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _runtime_version(
    engine: EngineName,
    installed: Mapping[str, Any],
) -> str | None:
    value = installed.get("version")
    if not isinstance(value, str) or not _SAFE_VERSION_RE.fullmatch(value):
        value = None
    if value is None and engine == EngineName.DS4:
        revision = installed.get("revision")
        if isinstance(revision, str) and _SAFE_VERSION_RE.fullmatch(revision):
            value = f"git:{revision}"
    return value


def _runtime_compatibility_fingerprint(
    installed: Mapping[str, Any],
) -> str | None:
    value = installed.get("compatibility_fingerprint")
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None


def _runtime_features(installed: Mapping[str, Any]) -> tuple[str, ...]:
    """Normalize only locally verified feature labels from runtime metadata."""

    values = installed.get("features")
    if not isinstance(values, (list, tuple)):
        return ()
    features = {
        value
        for value in values
        if isinstance(value, str) and _SAFE_FEATURE_RE.fullmatch(value)
    }
    return tuple(sorted(features))


__all__ = [
    "DEFAULT_AUTHORITY_TIMEOUT_SECONDS",
    "DEFAULT_INSTALLED_STATUS_TIMEOUT_SECONDS",
    "DEFAULT_STORAGE_RESERVE_BYTES",
    "NativeDesiredInstallAuthorities",
]
