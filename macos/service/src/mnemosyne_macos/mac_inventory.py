"""Path-free MacInventory v1 production from node-local authority.

The producer intentionally reads existing configuration, installer, runtime,
residency, storage-probe, participation, and usage state.  It never mutates any
of them and never loads, copies, relocates, or deletes model weights.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
from typing import Any, Awaitable, Callable, Mapping, Protocol
from uuid import UUID, uuid4

from . import __version__
from .config import ModelEngineAlternative, ModelProfile, StorageLocationConfig
from .filesystem import FilesystemProbeError
from .install_provenance import (
    ArtifactAuthority,
    InstallationProvenance,
    MANAGED_CREATION_MARKER_PATH,
    OwnershipClass,
    PROVENANCE_REVISION,
    ProvenanceDataError,
    SourceKind,
    allowed_hf_local_metadata_paths,
    is_hf_local_metadata_path,
    owned_manifest_digest,
)
from .install_store import InstallRecord
from .mac_inventory_store import MacInventoryIndex, StorageBinding, canonical_uuid
from .models import (
    ACTIVE_ENGINE_NAMES,
    DEFAULT_CAPABILITIES,
    ENGINE_RELEASE_TIER,
    EngineName,
    ModelKind,
)


MAC_INVENTORY_SCHEMA_VERSION = 1
MAX_MAC_INVENTORY_BYTES = 2 * 1024 * 1024
NONE_CATALOG_VERSION = "none"
NONE_CATALOG_DIGEST = "sha256:" + hashlib.sha256(b"none").hexdigest()
DEFAULT_REMOTE_INSTALL_POLICY = "ask"
_RETIRED_INSTALL_STATUSES = frozenset({"trashed"})
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_SAFE_ALIAS = re.compile(r"^[^/\\\x00-\x1f]{1,128}$")
_SAFE_SOC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$")
_APPLE_SOC = re.compile(r"^Apple M[1-9][0-9]?(?: (?:Pro|Max|Ultra))?$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIB = 1024**3
_MAX_SYSTEM_PROFILER_BYTES = 256 * 1024
_SYSTEM_PROFILER_TIMEOUT_SECONDS = 3


class MacInventoryError(RuntimeError):
    """Fixed-code inventory failure that contains no local source value."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HardwareProbe(Protocol):
    async def probe(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class _DisplayFacts:
    """Closed Apple-GPU facts; never profiler source text or diagnostics."""

    metal_supported: bool
    gpu_cores: int | None
    soc_family: str | None = None


@dataclass(frozen=True, slots=True)
class _Candidate:
    source_key: str
    alias: str
    engine: EngineName
    model: str
    storage: str | None
    capabilities: tuple[str, ...]
    model_kind: str
    context_tokens: int | None
    enabled: bool
    source_kind: str
    ownership_class: str
    binding_fingerprint: str


@dataclass(frozen=True, slots=True)
class _SignedInstallIdentity:
    """Path-free projection of one fully validated signed install proof."""

    logical_model_id: str
    artifact_id: str
    recipe_id: str
    verified_at: float | None


class DefaultMacHardwareProbe:
    """Small bounded probe whose output cannot contain host identity."""

    async def probe(self) -> dict[str, object]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._probe_sync), 5.0)
        except (TimeoutError, OSError, subprocess.SubprocessError):
            return self._conservative()

    @staticmethod
    def _sysctl(name: str) -> str | None:
        executable = Path("/usr/sbin/sysctl")
        if not executable.is_file():
            return None
        try:
            result = subprocess.run(
                [str(executable), "-n", name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                timeout=1,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip()
        return value if value else None

    @classmethod
    def _integer(cls, name: str) -> int | None:
        value = cls._sysctl(name)
        try:
            parsed = int(value) if value is not None else None
        except ValueError:
            return None
        return parsed if parsed is not None and parsed >= 0 else None

    @staticmethod
    def _parse_display_facts(raw: bytes) -> _DisplayFacts:
        """Accept one exact built-in Apple Metal GPU or fail closed.

        ``system_profiler`` can include attached displays, external GPUs, and
        localized presentation values. Only its fixed JSON keys and exact
        internal enum values are consumed. The original payload is discarded.
        """

        if not raw or len(raw) > _MAX_SYSTEM_PROFILER_BYTES:
            return _DisplayFacts(metal_supported=False, gpu_cores=None)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return _DisplayFacts(metal_supported=False, gpu_cores=None)
        if not isinstance(payload, dict):
            return _DisplayFacts(metal_supported=False, gpu_cores=None)
        displays = payload.get("SPDisplaysDataType")
        if not isinstance(displays, list):
            return _DisplayFacts(metal_supported=False, gpu_cores=None)
        candidates = [
            row
            for row in displays
            if isinstance(row, dict)
            and row.get("sppci_device_type") == "spdisplays_gpu"
            and row.get("spdisplays_vendor") == "sppci_vendor_Apple"
            and row.get("sppci_bus") == "spdisplays_builtin"
        ]
        if len(candidates) != 1:
            return _DisplayFacts(metal_supported=False, gpu_cores=None)
        candidate = candidates[0]
        raw_soc = candidate.get("sppci_model")
        soc_family = (
            raw_soc
            if isinstance(raw_soc, str) and _APPLE_SOC.fullmatch(raw_soc)
            else None
        )
        if candidate.get("spdisplays_metal") != "spdisplays_supported":
            return _DisplayFacts(
                metal_supported=False,
                gpu_cores=None,
                soc_family=soc_family,
            )
        raw_cores = candidate.get("sppci_cores")
        try:
            cores = int(raw_cores) if isinstance(raw_cores, str) else None
        except ValueError:
            cores = None
        if cores is None or not 1 <= cores <= 2048:
            cores = None
        return _DisplayFacts(
            metal_supported=True,
            gpu_cores=cores,
            soc_family=soc_family,
        )

    @classmethod
    def _display_facts(cls) -> _DisplayFacts:
        executable = Path("/usr/sbin/system_profiler")
        if not executable.is_file():
            return _DisplayFacts(metal_supported=False, gpu_cores=None)
        try:
            result = subprocess.run(
                [
                    str(executable),
                    "SPDisplaysDataType",
                    "-json",
                    "-detailLevel",
                    "mini",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=_SYSTEM_PROFILER_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            # subprocess.run terminates and waits for a child that exceeds its
            # timeout, so an unresponsive profiler cannot outlive this probe.
            return _DisplayFacts(metal_supported=False, gpu_cores=None)
        stdout = result.stdout
        if not isinstance(stdout, bytes):
            return _DisplayFacts(metal_supported=False, gpu_cores=None)
        return cls._parse_display_facts(stdout)

    @staticmethod
    def _memory_fallback() -> int:
        try:
            pages = int(os.sysconf("SC_PHYS_PAGES"))
            size = int(os.sysconf("SC_PAGE_SIZE"))
            value = pages * size
            return value if value >= 0 else 0
        except (OSError, ValueError, TypeError):
            return 0

    @staticmethod
    def _os_version() -> tuple[int, int]:
        value = platform.mac_ver()[0]
        try:
            pieces = [int(item) for item in value.split(".")[:2]]
        except ValueError:
            pieces = []
        return (
            min(99, max(1, pieces[0] if pieces else 1)),
            min(99, max(0, pieces[1] if len(pieces) > 1 else 0)),
        )

    @staticmethod
    def _power() -> tuple[str, bool]:
        executable = Path("/usr/bin/pmset")
        if not executable.is_file():
            return "unknown", False
        try:
            result = subprocess.run(
                [str(executable), "-g", "batt"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                timeout=1,
                text=True,
            )
            output = result.stdout.casefold()
            source = (
                "ac"
                if "ac power" in output
                else "battery"
                if "battery power" in output
                else "unknown"
            )
        except (OSError, subprocess.SubprocessError):
            source = "unknown"
        try:
            result = subprocess.run(
                [str(executable), "-g"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                timeout=1,
                text=True,
            )
            match = re.search(r"(?m)^\s*lowpowermode\s+(\d+)\s*$", result.stdout)
            low_power = bool(match and int(match.group(1)))
        except (OSError, subprocess.SubprocessError, ValueError):
            low_power = False
        return source, low_power

    @classmethod
    def _probe_sync(cls) -> dict[str, object]:
        now = time.time()
        display = cls._display_facts()
        reported_soc = cls._sysctl("machdep.cpu.brand_string")
        soc = (
            reported_soc
            if isinstance(reported_soc, str) and _APPLE_SOC.fullmatch(reported_soc)
            else display.soc_family or "Apple Silicon"
        )
        memory = cls._integer("hw.memsize")
        if memory is None:
            memory = cls._memory_fallback()
        # Until the owner-facing reserve is configurable, retain both a fixed
        # desktop reserve and a proportional reserve, taking the larger. This
        # is a calculated placement budget, not a claim about free memory.
        reserve = max(8 * _GIB, memory // 8)
        allocatable = max(0, memory - reserve)
        performance = cls._integer("hw.perflevel0.physicalcpu")
        efficiency = cls._integer("hw.perflevel1.physicalcpu")
        power, low_power = cls._power()
        os_major, os_minor = cls._os_version()
        return {
            # Probe v2 binds a positive Metal fact to one unambiguous built-in
            # Apple GPU. GPU cores are retained only when that same row carries
            # a bounded numeric value; they are never derived from an SoC name.
            "probe_version": 2 if display.metal_supported else 1,
            "soc_family": soc,
            "architecture": "arm64",
            "performance_cores": _bounded_optional_int(performance, 256),
            "efficiency_cores": _bounded_optional_int(efficiency, 256),
            "gpu_cores": _bounded_optional_int(display.gpu_cores, 2048),
            "unified_memory_bytes": _bounded_bytes(memory),
            "allocatable_memory_bytes": _bounded_bytes(min(memory, allocatable)),
            "os_major": os_major,
            "os_minor": os_minor,
            "power_source": power,
            "low_power_mode": low_power,
            "pressure_class": "unknown",
            "observed_at": _timestamp(now),
            "evidence_class": "calculated",
        }

    @classmethod
    def _conservative(cls) -> dict[str, object]:
        now = time.time()
        memory = cls._memory_fallback()
        os_major, os_minor = cls._os_version()
        return {
            "probe_version": 1,
            "soc_family": "Apple Silicon",
            "architecture": "arm64",
            "performance_cores": None,
            "efficiency_cores": None,
            "gpu_cores": None,
            "unified_memory_bytes": _bounded_bytes(memory),
            "allocatable_memory_bytes": _bounded_bytes(max(0, memory - max(8 * _GIB, memory // 8))),
            "os_major": os_major,
            "os_minor": os_minor,
            "power_source": "unknown",
            "low_power_mode": False,
            "pressure_class": "unknown",
            "observed_at": _timestamp(now),
            "evidence_class": "conservative",
        }


class MacInventoryProducer:
    """Build strict, canonical, path-free inventory observations."""

    def __init__(
        self,
        runtime: Any,
        index: MacInventoryIndex,
        *,
        hardware_probe: HardwareProbe | None = None,
        wall_clock: Callable[[], float] = time.time,
        instance_id: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.index = index
        self.hardware_probe = hardware_probe or DefaultMacHardwareProbe()
        self.wall_clock = wall_clock
        self.instance_id = _canonical_uuid(instance_id or str(uuid4()))
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._initialized = False
        self._last_document: dict[str, object] | None = None

    async def initialize(self) -> None:
        await self.index.initialize()
        # Storage identity lifecycle belongs to ordinary inventory startup,
        # not to read-only placement/lifecycle preview consumers.  This makes
        # the active binding set authoritative even while an unpaired Mac has
        # no outbound inventory destination yet.
        await self.index.reconcile_storage(
            (
                location.name,
                location.path,
                location.volume_uuid,
                location.scope_id,
            )
            for location in self.runtime.config.storage.locations
        )
        self._initialized = True

    async def close(self) -> None:
        await self.index.close()
        self._initialized = False

    @property
    def last_document(self) -> dict[str, object] | None:
        return _deep_copy(self._last_document)

    async def next_document(
        self,
        *,
        pairing_id: str,
        credential_generation: int,
    ) -> dict[str, object]:
        if not self._initialized:
            raise MacInventoryError("inventory_index_unavailable")
        pairing_id = _canonical_uuid(pairing_id)
        if (
            isinstance(credential_generation, bool)
            or not isinstance(credential_generation, int)
            or credential_generation < 1
            or credential_generation > 2147483647
        ):
            raise MacInventoryError("inventory_pairing_invalid")
        async with self._lock:
            self._sequence += 1
            if self._sequence > 9007199254740991:
                raise MacInventoryError("inventory_sequence_exhausted")
            document = await self._build_document(
                pairing_id=pairing_id,
                credential_generation=credential_generation,
                sequence=self._sequence,
            )
            _canonical_bytes(document)
            self._last_document = _deep_copy(document)
            return _deep_copy(document)

    async def _build_document(
        self,
        *,
        pairing_id: str,
        credential_generation: int,
        sequence: int,
    ) -> dict[str, object]:
        observed_at = _timestamp(self.wall_clock())
        config = self.runtime.config
        catalog_version, catalog_digest = await _catalog_identity(self.runtime)
        storage_bindings = await self.index.reconcile_storage(
            (
                location.name,
                location.path,
                location.volume_uuid,
                location.scope_id,
            )
            for location in config.storage.locations
        )

        hardware_task = asyncio.create_task(self.hardware_probe.probe())
        storage_task = asyncio.create_task(
            self._storage_rows(storage_bindings, observed_at=observed_at)
        )
        runtime_task = asyncio.create_task(
            self._runtime_rows(
                observed_at=observed_at,
                catalog_digest=catalog_digest,
            )
        )
        install_task = asyncio.create_task(
            self._install_records(storage_bindings=storage_bindings)
        )
        coordinator_task = asyncio.create_task(self.runtime.coordinator.status())
        participation_task = asyncio.create_task(self.runtime.fleet_participation.status())
        usage_task = asyncio.create_task(self.runtime.usage.status())
        try:
            (
                hardware,
                storage_rows,
                runtime_rows,
                install_state,
                coordinator,
                participation,
                usage,
            ) = await asyncio.wait_for(
                asyncio.gather(
                    hardware_task,
                    storage_task,
                    runtime_task,
                    install_task,
                    coordinator_task,
                    participation_task,
                    usage_task,
                ),
                timeout=45.0,
            )
        except TimeoutError:
            for task in (
                hardware_task,
                storage_task,
                runtime_task,
                install_task,
                coordinator_task,
                participation_task,
                usage_task,
            ):
                task.cancel()
            await asyncio.gather(
                hardware_task,
                storage_task,
                runtime_task,
                install_task,
                coordinator_task,
                participation_task,
                usage_task,
                return_exceptions=True,
            )
            raise MacInventoryError("inventory_collection_timeout") from None

        installs, signed_install_identities = install_state
        candidates = _configured_candidates(config)
        installation_rows = await self._installation_rows(
            candidates=candidates,
            installs=installs,
            signed_install_identities=signed_install_identities,
            storage_bindings=storage_bindings,
            storage_rows=storage_rows,
            runtime_rows=runtime_rows,
            coordinator=coordinator,
            observed_at=observed_at,
        )
        hardware = _safe_hardware(hardware, observed_at=observed_at)
        usage_delivery = _usage_delivery(usage)
        document: dict[str, object] = {
            "schema_version": MAC_INVENTORY_SCHEMA_VERSION,
            "inventory_instance_id": self.instance_id,
            "inventory_sequence": sequence,
            "observed_at": observed_at,
            "pairing_id": pairing_id,
            "credential_generation": credential_generation,
            "service": {
                "version": _safe_version(__version__) or "unknown",
                "platform": "macos",
                "architecture": "arm64",
                "supported_inventory_versions": [1],
                # Capability is advertised only after the local executor,
                # approval/cancellation controls, and exact signed launch
                # materialization are operational. Parsing the sync envelope
                # alone never grants the Hub download authority.
                "supported_job_versions": (
                    [1]
                    if bool(
                        getattr(
                            self.runtime,
                            "_desired_install_executor_available",
                            False,
                        )
                    )
                    else []
                ),
                "catalog_version": catalog_version,
                "catalog_digest": catalog_digest,
            },
            "hardware": hardware,
            "participation": {
                "state": str(participation.state.value),
                "remote_install_policy": DEFAULT_REMOTE_INSTALL_POLICY,
            },
            "storage_locations": sorted(
                storage_rows,
                key=lambda item: str(item["storage_location_id"]),
            ),
            "runtimes": sorted(runtime_rows, key=lambda item: str(item["engine"])),
            "installations": sorted(
                installation_rows,
                key=lambda item: str(item["installation_id"]),
            ),
            "usage_delivery": usage_delivery,
            "job_acknowledgements": [],
        }
        return document

    async def _storage_rows(
        self,
        bindings: Mapping[str, StorageBinding],
        *,
        observed_at: float,
    ) -> list[dict[str, object]]:
        config_by_name = {
            location.name: location for location in self.runtime.config.storage.locations
        }

        async def inspect(
            local_key: str,
            binding: StorageBinding,
        ) -> dict[str, object]:
            location = config_by_name[local_key]
            try:
                status = await self.runtime.filesystem.inspect(
                    location.path,
                    name=location.name,
                    expected_volume_uuid=location.volume_uuid,
                    scope_id=location.scope_id,
                )
            except (FilesystemProbeError, OSError, ValueError, RuntimeError):
                availability = (
                    "permission_required" if location.scope_id is not None else "unhealthy"
                )
                diagnostic = (
                    "permission_required" if location.scope_id is not None else "probe_failed"
                )
                return _storage_wire_row(
                    binding,
                    kind="other",
                    availability=availability,
                    writable=False,
                    total_bytes=None,
                    free_bytes=None,
                    observed_at=observed_at,
                    diagnostic_code=diagnostic,
                )
            if not status.exists:
                availability, diagnostic = "missing", "volume_missing"
            elif not status.volume_matches:
                availability, diagnostic = "wrong_volume", "wrong_volume"
            elif not status.is_directory:
                availability, diagnostic = "unhealthy", "probe_failed"
            elif not status.writable:
                availability, diagnostic = "read_only", "read_only"
            else:
                availability, diagnostic = "available", None
            total = _optional_bytes(status.total_bytes)
            free = _optional_bytes(status.free_bytes)
            if total is not None and free is not None:
                free = min(total, free)
            kind = (
                "internal"
                if status.mount_path == "/"
                else "external"
                if status.mount_path is not None
                else "other"
            )
            return _storage_wire_row(
                binding,
                kind=kind,
                availability=availability,
                writable=bool(status.writable and status.volume_matches),
                total_bytes=total,
                free_bytes=free,
                observed_at=observed_at,
                diagnostic_code=diagnostic,
            )

        return list(
            await asyncio.gather(
                *(inspect(local_key, binding) for local_key, binding in bindings.items())
            )
        )

    async def _runtime_rows(
        self,
        *,
        observed_at: float,
        catalog_digest: str,
    ) -> list[dict[str, object]]:
        try:
            installed = await asyncio.wait_for(
                self.runtime.runtime_updates.installed_status(),
                20.0,
            )
        except (TimeoutError, OSError, RuntimeError, ValueError):
            installed = {}
        fingerprints = getattr(self.runtime, "_runtime_fingerprints", {})
        rows: list[dict[str, object]] = []
        for engine in ACTIVE_ENGINE_NAMES:
            enabled = bool(self.runtime.config.engine_enabled(engine))
            status = installed.get(engine.value, {})
            present = bool(status.get("installed"))
            version = _safe_version(status.get("version"))
            if version is None and engine == EngineName.DS4:
                revision = _safe_version(status.get("revision"))
                version = f"git:{revision}" if revision is not None else None
            if engine in {EngineName.LLAMA_CPP, EngineName.DS4}:
                # Placement and final DesiredInstall approval must compare the
                # same portable, locally integrity-verified identity. The
                # adapter fingerprint is path/mtime based benchmark freshness
                # evidence and must never be substituted here.
                fingerprint = _digest_fingerprint(
                    status.get("compatibility_fingerprint")
                )
            else:
                fingerprint = _digest_fingerprint(fingerprints.get(engine))
            if not enabled:
                catalog_status = "disabled"
                diagnostic = "engine_disabled"
            elif not present:
                catalog_status = "missing"
                diagnostic = "runtime_missing"
            else:
                # Presence plus a bounded version/fingerprint is useful
                # placement evidence, but does not claim that any exact
                # recipe has been matched or proven on this Mac.
                catalog_status = "available"
                diagnostic = "version_unknown" if version is None else None
            ownership = (
                "external"
                if engine in {EngineName.OMLX, EngineName.MLXCEL, EngineName.MISTRAL_RS}
                else "managed"
            )
            rows.append(
                {
                    "engine": engine.value,
                    "release_tier": ENGINE_RELEASE_TIER[engine],
                    "enabled": enabled,
                    "ownership": ownership,
                    "version": version,
                    "runtime_fingerprint": fingerprint,
                    "health": "unknown",
                    "catalog_status": catalog_status,
                    "catalog_digest": catalog_digest,
                    "observed_at": observed_at,
                    "diagnostic_code": diagnostic,
                }
            )
        return rows

    async def _install_records(
        self,
        *,
        storage_bindings: Mapping[str, StorageBinding],
    ) -> tuple[list[InstallRecord], dict[str, _SignedInstallIdentity]]:
        # Evidence includes hidden history, preserving managed provenance after
        # local UI dismissal. The hard schema row cap is enforced below.
        values = await self.runtime.installer.evidence(limit=10_000)
        records: list[InstallRecord] = []
        evidence_fingerprints: dict[str, str] = {}
        for value in values:
            try:
                evidence_fingerprint = _install_evidence_fingerprint(value)
                normalized = dict(value)
                normalized.pop("dismissed", None)
                normalized.pop("events", None)
                # The typed launch contract is retained in the local install
                # row and resulting profile. Inventory recipes are identified
                # by opaque catalog IDs; never echo this path-adjacent local
                # registration payload or feed the decoded helper field back
                # into InstallRecord construction.
                normalized.pop("launch_contract", None)
                download_files = normalized.pop("download_files", [])
                capabilities = normalized.pop("capabilities", None)
                normalized["files_json"] = (
                    json.dumps(download_files, separators=(",", ":"))
                    if isinstance(download_files, list) and download_files
                    else None
                )
                normalized["capabilities_json"] = (
                    json.dumps(capabilities, separators=(",", ":"))
                    if isinstance(capabilities, list) and capabilities
                    else None
                )
                normalized["hidden"] = int(value.get("hidden", 0) or 0)
                record = InstallRecord(**normalized)
                records.append(record)
                evidence_fingerprints[record.id] = evidence_fingerprint
            except (TypeError, ValueError, UnicodeError):
                continue
        identities: dict[str, _SignedInstallIdentity] = {}
        require_authority = getattr(
            self.runtime.installer,
            "require_cleanup_authority",
            None,
        )
        if not callable(require_authority):
            return records, identities

        # The cleanup-authority API is the existing full validation boundary
        # for immutable install/provenance evidence.  Inventory projects only
        # its path-free signed identity; it neither re-reads nor re-hashes the
        # artifact manifest here.
        for record in records:
            if record.status != "installed":
                continue
            storage_binding = storage_bindings.get(record.storage)
            if storage_binding is None:
                continue
            try:
                authority_record, provenance = await require_authority(record.id)
                identity = _validated_signed_install_identity(
                    record,
                    authority_record=authority_record,
                    provenance=provenance,
                    storage_binding=storage_binding,
                    evidence_fingerprint=evidence_fingerprints[record.id],
                )
            except Exception:
                # Missing, malformed, conflicting, or otherwise rejected
                # provenance must not make the inventory snapshot unavailable.
                # The row remains present but unverified and non-reusable.
                continue
            if identity is not None:
                identities[record.id] = identity
        return records, identities

    async def _installation_rows(
        self,
        *,
        candidates: list[_Candidate],
        installs: list[InstallRecord],
        signed_install_identities: Mapping[str, _SignedInstallIdentity],
        storage_bindings: Mapping[str, StorageBinding],
        storage_rows: list[dict[str, object]],
        runtime_rows: list[dict[str, object]],
        coordinator: Any,
        observed_at: float,
    ) -> list[dict[str, object]]:
        storage_state = {
            binding.local_key: next(
                row
                for row in storage_rows
                if row["storage_location_id"] == binding.storage_location_id
            )
            for binding in storage_bindings.values()
        }
        runtime_state = {str(row["engine"]): row for row in runtime_rows}
        candidates_by_pair: dict[tuple[str, str], list[_Candidate]] = {}
        for candidate in candidates:
            candidates_by_pair.setdefault((candidate.alias, candidate.engine.value), []).append(
                candidate
            )

        install_records = installs[:10_000]
        consumed: set[str] = set()
        matched_candidates: dict[str, _Candidate] = {}

        # Prefer current installed evidence before considering retired ledger
        # history. This makes a later replacement authoritative even when a
        # retained trashed row happens to sort ahead of it in local evidence.
        for record in install_records:
            if record.status != "installed":
                continue
            candidate = _match_managed_candidate(
                record,
                candidates_by_pair.get((record.alias, record.engine), []),
                consumed,
            )
            if candidate is not None:
                consumed.add(candidate.source_key)
                matched_candidates[record.id] = candidate

        # A fresh inventory snapshot is the authoritative full set, so a
        # successfully trashed install is retired by omission rather than
        # misrepresented as a failed installation. Suppress only its exact
        # lexical configured target while the local config transaction catches
        # up; unrelated or replacement targets with the same alias survive.
        for record in install_records:
            if record.status not in _RETIRED_INSTALL_STATUSES:
                continue
            candidate = _match_install_candidate(
                record,
                candidates_by_pair.get((record.alias, record.engine), []),
                consumed,
            )
            if candidate is not None:
                consumed.add(candidate.source_key)

        managed_candidates = [
            (record, matched_candidates.get(record.id))
            for record in install_records
            if record.status not in _RETIRED_INSTALL_STATUSES
        ]

        remaining = [candidate for candidate in candidates if candidate.source_key not in consumed]
        identity_inputs = {
            candidate.source_key: candidate.binding_fingerprint
            for candidate in remaining
        }
        legacy_managed_keys: dict[str, str] = {}
        for record, _candidate in managed_candidates:
            if canonical_uuid(record.id) is None:
                source_key = f"managed-ledger:{record.id}"
                legacy_managed_keys[record.id] = source_key
                identity_inputs[source_key] = _private_fingerprint(
                    {"ledger_id": record.id, "created_at": record.created_at}
                )
        identities = await self.index.reconcile_installations(identity_inputs)

        managed_rows = [
            _managed_install_row(
                record,
                installation_id=(
                    canonical_uuid(record.id)
                    or identities[legacy_managed_keys[record.id]].installation_id
                ),
                candidate=candidate,
                storage_binding=storage_bindings.get(record.storage),
                storage_status=storage_state.get(record.storage),
                runtime_status=runtime_state.get(record.engine),
                coordinator=coordinator,
                observed_at=observed_at,
                signed_identity=signed_install_identities.get(record.id),
            )
            for record, candidate in managed_candidates
        ]
        configured_rows = [
            _configured_install_row(
                candidate,
                installation_id=identities[candidate.source_key].installation_id,
                storage_binding=(
                    storage_bindings.get(candidate.storage)
                    if candidate.storage is not None
                    else None
                ),
                storage_status=(
                    storage_state.get(candidate.storage)
                    if candidate.storage is not None
                    else None
                ),
                runtime_status=runtime_state.get(candidate.engine.value),
                coordinator=coordinator,
                observed_at=observed_at,
            )
            for candidate in remaining
        ]
        rows = managed_rows + configured_rows
        if len(rows) > 10_000:
            raise MacInventoryError("inventory_installation_limit_exceeded")
        return rows


def _configured_candidates(config: Any) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for profile in config.models:
        candidates.append(
            _candidate_from_profile(
                profile,
                source_key=f"profile:{profile.alias}:primary",
                profile_enabled=bool(profile.enabled),
            )
        )
        for alternative in profile.alternatives:
            candidates.append(
                _candidate_from_alternative(
                    profile,
                    alternative,
                    source_key=(
                        f"profile:{profile.alias}:alternative:"
                        f"{alternative.engine.value}"
                    ),
                )
            )
    for index, legacy in enumerate(config.migration.legacy_lmstudio_profiles):
        capabilities = tuple(
            sorted(
                item.value
                for item in (
                    legacy.capabilities
                    or DEFAULT_CAPABILITIES[EngineName.LLAMA_CPP]
                )
            )
        )
        signature = _private_fingerprint(
            {
                "alias": legacy.alias,
                "model": legacy.model,
                "load": legacy.load.model_dump(mode="json"),
                "enabled": legacy.enabled,
            }
        )
        candidates.append(
            _Candidate(
                source_key=f"legacy:{legacy.alias}:{index}",
                alias=legacy.alias,
                engine=EngineName.LLAMA_CPP,
                model=legacy.model,
                storage=None,
                capabilities=capabilities,
                model_kind=ModelKind.LANGUAGE.value,
                context_tokens=legacy.load.context_length,
                enabled=False,
                source_kind="legacy_migration",
                ownership_class="unknown",
                binding_fingerprint=signature,
            )
        )
    return candidates


async def _catalog_identity(runtime: Any) -> tuple[str, str]:
    """Read one immutable active-catalog identity without leaking failures."""

    owner = getattr(runtime, "compatibility_catalog", None)
    identity = getattr(owner, "inventory_identity", None)
    if not callable(identity):
        return NONE_CATALOG_VERSION, NONE_CATALOG_DIGEST
    try:
        version, digest = await identity()
    except Exception:
        return NONE_CATALOG_VERSION, NONE_CATALOG_DIGEST
    if _safe_version(version) is None or not isinstance(digest, str):
        return NONE_CATALOG_VERSION, NONE_CATALOG_DIGEST
    if _SHA256.fullmatch(digest) is None:
        return NONE_CATALOG_VERSION, NONE_CATALOG_DIGEST
    return version, digest


def _candidate_from_profile(
    profile: ModelProfile,
    *,
    source_key: str,
    profile_enabled: bool,
) -> _Candidate:
    capabilities = tuple(
        sorted(
            item.value
            for item in (profile.capabilities or DEFAULT_CAPABILITIES[profile.engine])
        )
    )
    source_kind = "local_import" if profile.storage is not None else "external_reference"
    ownership = "user_owned" if profile.storage is not None else "external_owned"
    signature = _private_fingerprint(
        {
            "alias": profile.alias,
            "engine": profile.engine.value,
            "model": profile.model,
            "storage": profile.storage,
            "load": profile.load.model_dump(mode="json"),
            "context": profile.context.model_dump(mode="json"),
            "kind": profile.kind.value,
        }
    )
    return _Candidate(
        source_key=source_key,
        alias=profile.alias,
        engine=profile.engine,
        model=profile.model,
        storage=profile.storage,
        capabilities=capabilities,
        model_kind=profile.kind.value,
        context_tokens=_context_tokens(profile.context, profile.load.context_length),
        enabled=profile_enabled,
        source_kind=source_kind,
        ownership_class=ownership,
        binding_fingerprint=signature,
    )


def _candidate_from_alternative(
    profile: ModelProfile,
    alternative: ModelEngineAlternative,
    *,
    source_key: str,
) -> _Candidate:
    capabilities = tuple(
        sorted(
            item.value
            for item in (
                alternative.capabilities or DEFAULT_CAPABILITIES[alternative.engine]
            )
        )
    )
    source_kind = (
        "local_import" if alternative.storage is not None else "external_reference"
    )
    ownership = "user_owned" if alternative.storage is not None else "external_owned"
    signature = _private_fingerprint(
        {
            "alias": profile.alias,
            "engine": alternative.engine.value,
            "model": alternative.model,
            "storage": alternative.storage,
            "load": alternative.load.model_dump(mode="json"),
            "context": alternative.context.model_dump(mode="json"),
        }
    )
    return _Candidate(
        source_key=source_key,
        alias=profile.alias,
        engine=alternative.engine,
        model=alternative.model,
        storage=alternative.storage,
        capabilities=capabilities,
        model_kind=ModelKind.LANGUAGE.value,
        context_tokens=_context_tokens(
            alternative.context,
            alternative.load.context_length,
        ),
        enabled=bool(profile.enabled and alternative.enabled),
        source_kind=source_kind,
        ownership_class=ownership,
        binding_fingerprint=signature,
    )


def _match_managed_candidate(
    record: InstallRecord,
    candidates: list[_Candidate],
    consumed: set[str],
) -> _Candidate | None:
    if record.status != "installed":
        return None
    return _match_install_candidate(record, candidates, consumed)


def _match_install_candidate(
    record: InstallRecord,
    candidates: list[_Candidate],
    consumed: set[str],
) -> _Candidate | None:
    for candidate in candidates:
        if candidate.source_key in consumed:
            continue
        expected = (
            str(Path(record.destination) / record.filename)
            if record.filename
            and record.engine in {EngineName.LLAMA_CPP.value, EngineName.DS4.value}
            else Path(record.destination).name
            if record.engine == EngineName.OMLX.value
            else record.destination
        )
        if _lexical(candidate.model) == _lexical(expected):
            return candidate
    return None


def _validated_signed_install_identity(
    record: InstallRecord,
    *,
    authority_record: object,
    provenance: object,
    storage_binding: StorageBinding,
    evidence_fingerprint: str,
) -> _SignedInstallIdentity | None:
    """Project only exact current revision-2 signed-catalog authority."""

    if not isinstance(authority_record, InstallRecord) or not isinstance(
        provenance,
        InstallationProvenance,
    ):
        return None
    if (
        authority_record.id != record.id
        or authority_record.status != record.status
        or _install_evidence_fingerprint(authority_record.to_dict())
        != evidence_fingerprint
    ):
        return None
    if (
        record.status != "installed"
        or provenance.installation_id != record.id
        or provenance.source_kind is not SourceKind.MANAGED_DOWNLOAD
        or provenance.ownership_class is not OwnershipClass.EXCLUSIVE_MANAGED
        or provenance.artifact_authority is not ArtifactAuthority.SIGNED_CATALOG
        or provenance.provenance_revision != PROVENANCE_REVISION
        or provenance.lexical_destination != record.destination
        or provenance.resolved_revision != record.revision
        or not _immutable_revision(provenance.resolved_revision)
    ):
        return None
    if (
        storage_binding.local_key != record.storage
        or provenance.storage_location_id != storage_binding.storage_location_id
        or provenance.storage_binding_generation
        != storage_binding.binding_generation
        or provenance.storage_lexical_root != storage_binding.exact_path
        or provenance.storage_volume_uuid != storage_binding.volume_uuid
        or provenance.storage_scope_id != storage_binding.scope_id
    ):
        return None
    if not all(
        _safe_version(value) is not None
        for value in (
            provenance.catalog_id,
            provenance.logical_model_id,
            provenance.artifact_id,
            provenance.recipe_id,
        )
    ):
        return None
    if not all(
        isinstance(value, str) and _SHA256.fullmatch(value) is not None
        for value in (
            provenance.catalog_digest,
            provenance.source_identity_digest,
            provenance.manifest_digest,
            provenance.destination_binding_digest,
        )
    ):
        return None
    try:
        expected_files = authority_record.expected_files
        selected_paths_value = json.loads(authority_record.files_json or "[]")
        selected_paths = tuple(selected_paths_value)
        if (
            expected_files is None
            or not isinstance(authority_record.expected_manifest_digest, str)
            or _SHA256.fullmatch(authority_record.expected_manifest_digest) is None
            or authority_record.expected_manifest_digest
            != owned_manifest_digest(expected_files)
            or selected_paths != tuple(item.path for item in expected_files)
            or any(not isinstance(path, str) for path in selected_paths)
            or authority_record.total_bytes
            != sum(item.size_bytes for item in expected_files)
            or provenance.owned_files is None
        ):
            return None
        allowed_metadata = allowed_hf_local_metadata_paths(selected_paths)
        payload_files = tuple(
            item
            for item in provenance.owned_files
            if not is_hf_local_metadata_path(item.path)
        )
        metadata_paths = {
            item.path
            for item in provenance.owned_files
            if is_hf_local_metadata_path(item.path)
        }
        if (
            payload_files != expected_files
            or MANAGED_CREATION_MARKER_PATH not in metadata_paths
            or not metadata_paths.issubset(allowed_metadata)
        ):
            return None
    except (AttributeError, TypeError, ValueError, ProvenanceDataError):
        return None
    return _SignedInstallIdentity(
        logical_model_id=provenance.logical_model_id,
        artifact_id=provenance.artifact_id,
        recipe_id=provenance.recipe_id,
        verified_at=_optional_timestamp(record.updated_at),
    )


def _install_evidence_fingerprint(value: Mapping[str, object]) -> str:
    """Hash the exact immutable install identity without exposing local data."""

    fields = (
        "id",
        "repo_id",
        "engine",
        "storage",
        "alias",
        "destination",
        "status",
        "revision",
        "filename",
        "projector_filename",
        "context_length",
        "download_files",
        "capabilities",
        "family",
        "launch_contract",
        "total_bytes",
        "created_at",
    )
    return _private_fingerprint({field: value.get(field) for field in fields})


def _managed_install_row(
    record: InstallRecord,
    *,
    installation_id: str,
    candidate: _Candidate | None,
    storage_binding: StorageBinding | None,
    storage_status: Mapping[str, object] | None,
    runtime_status: Mapping[str, object] | None,
    coordinator: Any,
    observed_at: float,
    signed_identity: _SignedInstallIdentity | None,
) -> dict[str, object]:
    capabilities = (
        candidate.capabilities
        if candidate is not None
        else tuple(sorted(record.capabilities or _default_capabilities(record.engine)))
    )
    lifecycle = {
        "queued": "queued",
        "downloading": "downloading",
        "partial": "partial",
        "registering": "verifying",
        "downloaded": "downloaded_unregistered",
        "installed": "registered",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(record.status, "failed")
    runtime_compatibility = _runtime_compatibility(runtime_status)
    availability, diagnostic = _installation_availability(
        storage_status=storage_status,
        runtime_compatibility=runtime_compatibility,
        lifecycle=lifecycle,
    )
    immutable = _immutable_revision(record.revision)
    verification_state = (
        "digest_verified"
        if signed_identity is not None
        else "revision_verified"
        if immutable
        else "unverified"
    )
    total = _optional_bytes(record.total_bytes)
    downloaded = _bounded_bytes(record.bytes_downloaded)
    if total is not None:
        downloaded = min(downloaded, total)
    return {
        "installation_id": installation_id,
        "aliases": _aliases(record.alias),
        "logical_model_id": (
            signed_identity.logical_model_id
            if signed_identity is not None
            else None
        ),
        "artifact_id": (
            signed_identity.artifact_id if signed_identity is not None else None
        ),
        "recipe_id": (
            signed_identity.recipe_id if signed_identity is not None else None
        ),
        # Signed artifact provenance does not prove the complete serving/load
        # identity required by Fleet snapshot v1.
        "deployment_id": None,
        "identity_confidence": (
            "authoritative" if signed_identity is not None else "unverified"
        ),
        "engine": record.engine if record.engine in {item.value for item in EngineName} else "llama.cpp",
        "model_kind": candidate.model_kind if candidate is not None else "language",
        "capabilities": list(capabilities),
        "guaranteed_context_tokens": _context_bound(
            candidate.context_tokens if candidate is not None else record.context_length
        ),
        "source_kind": "managed_download",
        # Cleanup authority is deliberately not inferred merely from source.
        "ownership_class": (
            "exclusive_managed" if signed_identity is not None else "unknown"
        ),
        "lifecycle": lifecycle,
        "availability": availability,
        "residency": _residency(candidate, coordinator, registered=lifecycle == "registered"),
        "storage_location_id": (
            storage_binding.storage_location_id if storage_binding is not None else None
        ),
        "storage_binding_generation": (
            storage_binding.binding_generation if storage_binding is not None else None
        ),
        "runtime_compatibility": runtime_compatibility,
        "verification": {
            "state": verification_state,
            "evidence_class": (
                "measured" if signed_identity is not None or immutable else "conservative"
            ),
            "verified_at": (
                signed_identity.verified_at
                if signed_identity is not None
                else _optional_timestamp(record.updated_at)
                if immutable
                else None
            ),
        },
        "bytes_downloaded": downloaded,
        "total_bytes": total,
        "observed_at": observed_at,
        "diagnostic_code": (
            "download_failed"
            if lifecycle == "failed"
            else "cancelled"
            if lifecycle == "cancelled"
            else diagnostic
        ),
    }


def _configured_install_row(
    candidate: _Candidate,
    *,
    installation_id: str,
    storage_binding: StorageBinding | None,
    storage_status: Mapping[str, object] | None,
    runtime_status: Mapping[str, object] | None,
    coordinator: Any,
    observed_at: float,
) -> dict[str, object]:
    runtime_compatibility = _runtime_compatibility(
        runtime_status,
        candidate_enabled=candidate.enabled,
    )
    availability, diagnostic = _installation_availability(
        storage_status=storage_status,
        runtime_compatibility=runtime_compatibility,
        lifecycle="configured",
    )
    return {
        "installation_id": installation_id,
        "aliases": _aliases(candidate.alias),
        "logical_model_id": None,
        "artifact_id": None,
        "recipe_id": None,
        "deployment_id": None,
        "identity_confidence": "unverified",
        "engine": candidate.engine.value,
        "model_kind": candidate.model_kind,
        "capabilities": list(candidate.capabilities),
        "guaranteed_context_tokens": _context_bound(candidate.context_tokens),
        "source_kind": candidate.source_kind,
        "ownership_class": candidate.ownership_class,
        "lifecycle": "configured",
        "availability": availability,
        "residency": _residency(candidate, coordinator, registered=True),
        "storage_location_id": (
            storage_binding.storage_location_id if storage_binding is not None else None
        ),
        "storage_binding_generation": (
            storage_binding.binding_generation if storage_binding is not None else None
        ),
        "runtime_compatibility": runtime_compatibility,
        "verification": {
            "state": "unverified",
            "evidence_class": "conservative",
            "verified_at": None,
        },
        "bytes_downloaded": 0,
        "total_bytes": None,
        "observed_at": observed_at,
        "diagnostic_code": diagnostic,
    }


def _runtime_compatibility(
    runtime_status: Mapping[str, object] | None,
    *,
    candidate_enabled: bool = True,
) -> str:
    if not candidate_enabled:
        return "engine_disabled"
    if runtime_status is None:
        return "runtime_missing"
    status = runtime_status.get("catalog_status")
    if status == "disabled":
        return "engine_disabled"
    if status == "missing":
        return "runtime_missing"
    if status == "known_bad":
        return "known_bad"
    if status == "unsupported_os":
        return "unsupported_os"
    if status == "unhealthy":
        return "unhealthy"
    return "compatible_unverified"


def _installation_availability(
    *,
    storage_status: Mapping[str, object] | None,
    runtime_compatibility: str,
    lifecycle: str,
) -> tuple[str, str | None]:
    if storage_status is not None:
        storage = storage_status.get("availability")
        if storage == "missing":
            return "storage_missing", "storage_missing"
        if storage == "wrong_volume":
            return "wrong_volume", "wrong_volume"
        if storage == "permission_required":
            return "permission_required", "permission_required"
        if storage in {"read_only", "unhealthy"}:
            return "unknown", None
    if runtime_compatibility == "engine_disabled":
        return "engine_disabled", "engine_disabled"
    if runtime_compatibility != "compatible_unverified":
        return "runtime_incompatible", "runtime_incompatible"
    if lifecycle in {"failed", "cancelled"}:
        return "unknown", None
    return ("available", None) if storage_status is not None else ("unknown", None)


def _residency(
    candidate: _Candidate | None,
    coordinator: Any,
    *,
    registered: bool,
) -> str:
    if candidate is None or not registered:
        return "cold"
    if (
        coordinator.resident_alias == candidate.alias
        and coordinator.resident_engine == candidate.engine
        and _lexical(coordinator.resident_model or "") == _lexical(candidate.model)
    ):
        state = str(coordinator.state.value)
        if state == "draining":
            return "draining"
        if state == "unloading":
            return "unloading"
        return "warm"
    if (
        coordinator.transition_target == candidate.alias
        and coordinator.transition_engine == candidate.engine
    ):
        return "loading"
    return "cold"


def _storage_wire_row(
    binding: StorageBinding,
    *,
    kind: str,
    availability: str,
    writable: bool,
    total_bytes: int | None,
    free_bytes: int | None,
    observed_at: float,
    diagnostic_code: str | None,
) -> dict[str, object]:
    return {
        "storage_location_id": binding.storage_location_id,
        "binding_generation": binding.binding_generation,
        "kind": kind,
        "share_label": None,
        "availability": availability,
        "writable": writable,
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "write_speed_class": "unknown",
        "remote_install_policy": DEFAULT_REMOTE_INSTALL_POLICY,
        "observed_at": observed_at,
        "evidence_class": "measured",
        "diagnostic_code": diagnostic_code,
    }


def _usage_delivery(value: Mapping[str, object]) -> dict[str, object]:
    code = value.get("last_error_code")
    if code not in {
        None,
        "ledger_unconfigured",
        "writer_unavailable",
        "outbox_full",
        "delivery_failed",
        "delivery_timeout",
    }:
        code = "delivery_failed"
    if code is None and value.get("enabled") and not value.get("writer_ready"):
        code = "ledger_unconfigured"
    last_flush = value.get("last_flush_at")
    return {
        "enabled": bool(value.get("enabled")),
        "writer_ready": bool(value.get("writer_ready")),
        "outbox_pending": min(10_000_000, max(0, int(value.get("outbox_pending") or value.get("outbox_depth") or 0))),
        "last_flush_at": (
            _timestamp(float(last_flush))
            if isinstance(last_flush, (int, float)) and not isinstance(last_flush, bool)
            else None
        ),
        "last_error_code": code,
    }


def _safe_hardware(
    value: Mapping[str, object],
    *,
    observed_at: float,
) -> dict[str, object]:
    soc = value.get("soc_family")
    if not isinstance(soc, str) or not _SAFE_SOC.fullmatch(soc):
        soc = "Apple Silicon"
    unified = _bounded_bytes(value.get("unified_memory_bytes"))
    allocatable = min(unified, _bounded_bytes(value.get("allocatable_memory_bytes")))
    power = value.get("power_source")
    pressure = value.get("pressure_class")
    evidence = value.get("evidence_class")
    return {
        "probe_version": _bounded_positive_int(value.get("probe_version"), 65535, fallback=1),
        "soc_family": soc,
        "architecture": "arm64",
        "performance_cores": _bounded_optional_int(value.get("performance_cores"), 256),
        "efficiency_cores": _bounded_optional_int(value.get("efficiency_cores"), 256),
        "gpu_cores": _bounded_optional_int(value.get("gpu_cores"), 2048),
        "unified_memory_bytes": unified,
        "allocatable_memory_bytes": allocatable,
        "os_major": _bounded_positive_int(value.get("os_major"), 99, fallback=1),
        "os_minor": _bounded_nonnegative_int(value.get("os_minor"), 99),
        "power_source": power if power in {"ac", "battery", "unknown"} else "unknown",
        "low_power_mode": bool(value.get("low_power_mode")),
        "pressure_class": pressure if pressure in {"nominal", "fair", "serious", "critical", "unknown"} else "unknown",
        "observed_at": (
            _timestamp(float(value["observed_at"]))
            if isinstance(value.get("observed_at"), (int, float))
            and not isinstance(value.get("observed_at"), bool)
            else observed_at
        ),
        "evidence_class": evidence if evidence in {"measured", "catalog_tested", "calculated", "conservative"} else "conservative",
    }


def _default_capabilities(engine: str) -> tuple[str, ...]:
    try:
        parsed = EngineName(engine)
    except ValueError:
        parsed = EngineName.LLAMA_CPP
    return tuple(sorted(item.value for item in DEFAULT_CAPABILITIES[parsed]))


def _aliases(value: str) -> list[str]:
    return [value] if isinstance(value, str) and _SAFE_ALIAS.fullmatch(value) else []


def _context_tokens(context: Any, load_context: int | None) -> int | None:
    value = (
        context.fixed_tokens
        if context.mode == "fixed"
        else context.native_tokens
        if context.mode == "native"
        else load_context
    )
    return _context_bound(value)


def _context_bound(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 100_000_000 else None


def _immutable_revision(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value))


def _digest_fingerprint(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if _SHA256.fullmatch(value):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_version(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_VERSION.fullmatch(value) else None


def _private_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _lexical(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(value))))


def _canonical_uuid(value: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        raise MacInventoryError("inventory_identity_invalid") from None
    canonical = str(parsed)
    if value.casefold() != canonical:
        raise MacInventoryError("inventory_identity_invalid")
    return canonical


def _timestamp(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise MacInventoryError("inventory_clock_invalid")
    value = float(value)
    if not 0 <= value <= 4_102_444_800:
        raise MacInventoryError("inventory_clock_invalid")
    return value


def _optional_timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return _timestamp(float(value))
    except MacInventoryError:
        return None


def _bounded_bytes(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return min(1_152_921_504_606_846_976, max(0, int(value)))


def _optional_bytes(value: object) -> int | None:
    if value is None:
        return None
    return _bounded_bytes(value)


def _bounded_optional_int(value: object, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return min(maximum, max(0, value))


def _bounded_positive_int(value: object, maximum: int, *, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return fallback
    return min(maximum, value)


def _bounded_nonnegative_int(value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(maximum, max(0, value))


def _canonical_bytes(document: Mapping[str, object]) -> bytes:
    try:
        encoded = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise MacInventoryError("inventory_document_invalid") from None
    if len(encoded) > MAX_MAC_INVENTORY_BYTES:
        raise MacInventoryError("inventory_document_too_large")
    return encoded


def _deep_copy(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


__all__ = [
    "DEFAULT_REMOTE_INSTALL_POLICY",
    "DefaultMacHardwareProbe",
    "HardwareProbe",
    "MAC_INVENTORY_SCHEMA_VERSION",
    "MAX_MAC_INVENTORY_BYTES",
    "MacInventoryError",
    "MacInventoryProducer",
    "NONE_CATALOG_DIGEST",
    "NONE_CATALOG_VERSION",
]
