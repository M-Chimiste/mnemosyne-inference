"""Fail-closed local executor core for Hub DesiredInstall v1 jobs.

Receiving a DesiredInstall document grants no filesystem authority.  This
module is invoked only by an explicit local approval surface and re-resolves
every authority from local providers immediately before asking the existing
native installer to create one install.  It deliberately owns no background
task, network client, configuration writer, engine adapter, or residency
operation.

The wire document contains only opaque identifiers.  Repository, revision,
file, digest, runtime, and launch facts come from the active signed catalog;
the storage name and lexical destination come from the selected Mac.  A
catalog layout that cannot be represented by ``NativeInstaller.create`` is
refused instead of being guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
import uuid

from .compatibility_catalog import (
    artifact_manifest_digest,
    canonical_json,
    catalog_digest,
)
from .desired_install_store import (
    DesiredInstallConflictError,
    DesiredInstallRecord,
    DesiredInstallStoreError,
)
from .install_launch import (
    InstallLaunchContract,
    OMLXInstallLaunch,
    install_launch_from_json,
    validate_install_launch,
)
from .install_provenance import (
    ArtifactAuthority,
    OwnedFile,
    allowed_hf_local_metadata_paths,
    is_hf_local_metadata_path,
    owned_files_from_canonical_json,
)
from .models import ACTIVE_ENGINE_NAMES, EngineName
from .storage import StorageError, install_destination


_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_NUMERIC_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)$")
_LLAMA_BUILD_RE = re.compile(r"^[bB](\d+)$")
_MAC_ALIAS_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_ACTIVE_INSTALL_STATES = frozenset(
    {"preparing", "queued", "downloading", "registering"}
)
_EXECUTION_STATES = (
    "accepted",
    "downloading",
    "verifying",
    "downloaded_unregistered",
    "registered",
    "completed",
)
_EXECUTION_RANK = {state: index for index, state in enumerate(_EXECUTION_STATES)}

EXECUTOR_ERROR_CODES = frozenset(
    {
        "desired_install_job_unknown",
        "desired_install_not_awaiting_local_approval",
        "desired_install_installation_unbound",
        "desired_install_installation_invalid",
        "desired_install_catalog_install_interface_missing",
        "desired_install_internal_error",
    }
)


class DesiredInstallExecutorError(RuntimeError):
    """One fixed local code without provider, path, or installer diagnostics."""

    def __init__(self, code: str) -> None:
        if code not in EXECUTOR_ERROR_CODES:
            code = "desired_install_internal_error"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PairingAuthority:
    active: bool
    pairing_id: str | None
    credential_generation: int | None


@dataclass(frozen=True, slots=True)
class InventoryAuthority:
    inventory_instance_id: str
    inventory_sequence: int


@dataclass(frozen=True, slots=True)
class LocalStorageAuthority:
    """Local-only resolution of one opaque storage binding plus fresh evidence."""

    storage_location_id: str
    binding_generation: int
    local_storage_name: str
    indexed_lexical_root: str
    indexed_volume_uuid: str | None
    indexed_scope_id: str | None
    configured_lexical_root: str
    configured_volume_uuid: str | None
    configured_scope_id: str | None
    observed_lexical_root: str
    observed_volume_uuid: str | None
    availability: str
    exists: bool
    is_directory: bool
    writable: bool
    volume_matches: bool
    free_bytes: int | None
    reserve_bytes: int
    remote_installs_allowed: bool


@dataclass(frozen=True, slots=True)
class RuntimeAuthority:
    engine: str
    enabled: bool
    healthy: bool
    release_tier: str
    version: str | None
    runtime_fingerprint: str | None
    features: tuple[str, ...]
    catalog_digest: str
    # Populated only from oMLX's authenticated GET global-settings response.
    # ``None`` is not a conservative fallback: a signed oMLX recipe must
    # refuse before destination creation when either field is unproved.
    omlx_scheduler_slots: int | None = None
    omlx_memory_guard_enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class InstallerRequest:
    installation_id: str
    repo_id: str
    engine: str
    storage: str
    alias: str
    destination: str
    revision: str
    filename: str | None
    projector_filename: str | None
    context_length: int
    download_files: tuple[str, ...]
    expected_files: tuple[OwnedFile, ...]
    expected_manifest_digest: str
    capabilities: tuple[str, ...]
    family: str
    total_bytes: int
    launch_contract: InstallLaunchContract
    artifact_authority: ArtifactAuthority
    source_identity_digest: str
    catalog_id: str
    logical_model_id: str
    artifact_id: str
    recipe_id: str
    catalog_digest: str
    require_exclusive_proof: bool


# Backward-compatible public name for the catalog's exact immutable file row.
ExpectedArtifactFile = OwnedFile


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    request: InstallerRequest
    catalog_version: str
    storage_location_id: str
    storage_binding_generation: int
    storage_lexical_root: str
    storage_volume_uuid: str | None
    storage_scope_id: str | None
    artifact_manifest_digest: str
    expected_files: tuple[OwnedFile, ...]


class DesiredInstallStoreProtocol(Protocol):
    async def get(self, job_id: str) -> DesiredInstallRecord | None: ...

    async def transition(
        self,
        *,
        job_id: str,
        job_revision: int,
        state: str,
        bytes_downloaded: int,
        total_bytes: int | None,
        result_code: str | None,
        installation_id: str | None = None,
    ) -> Any: ...


class CatalogSnapshotProtocol(Protocol):
    source: str
    catalog_id: str
    catalog_version: str
    catalog_digest: str

    def catalog(self) -> dict[str, Any]: ...


class CatalogProvider(Protocol):
    async def snapshot(self) -> CatalogSnapshotProtocol: ...


class PairingProvider(Protocol):
    async def current_pairing(self) -> PairingAuthority: ...


class InventoryProvider(Protocol):
    async def current_inventory(self) -> InventoryAuthority: ...


class StorageProvider(Protocol):
    async def resolve_storage(
        self,
        storage_location_id: str,
        binding_generation: int,
    ) -> LocalStorageAuthority | None: ...


class RuntimeProvider(Protocol):
    async def current_runtime(
        self,
        engine: str,
        catalog_digest: str,
    ) -> RuntimeAuthority | None: ...


class InstallObservation(Protocol):
    id: str
    repo_id: str
    engine: str
    storage: str
    alias: str
    destination: str
    status: str
    revision: str | None
    filename: str | None
    projector_filename: str | None
    context_length: int | None
    files_json: str | None
    expected_files_json: str | None
    expected_manifest_digest: str | None
    capabilities_json: str | None
    family: str | None
    launch_json: str | None
    bytes_downloaded: int
    total_bytes: int | None


class InstallerProtocol(Protocol):
    async def create(
        self,
        *,
        installation_id: str,
        repo_id: str,
        engine: str,
        storage: str,
        alias: str,
        destination: str,
        revision: str | None,
        filename: str | None,
        projector_filename: str | None = None,
        context_length: int | None = None,
        download_files: list[str] | tuple[str, ...] = (),
        expected_files: list[OwnedFile] | tuple[OwnedFile, ...] | None = None,
        expected_manifest_digest: str | None = None,
        capabilities: list[str] | tuple[str, ...] | None = None,
        family: str | None,
        total_bytes: int | None,
        launch_contract: InstallLaunchContract | Mapping[str, Any] | None = None,
        artifact_authority: ArtifactAuthority | None = None,
        source_identity_digest: str | None = None,
        catalog_id: str | None = None,
        logical_model_id: str | None = None,
        artifact_id: str | None = None,
        recipe_id: str | None = None,
        catalog_digest: str | None = None,
        require_exclusive_proof: bool = False,
    ) -> InstallObservation: ...

    async def get_by_id(self, installation_id: str) -> InstallObservation: ...

    async def cancel(self, install_id: str) -> InstallObservation: ...

    async def retry(self, install_id: str) -> InstallObservation: ...

    async def require_cleanup_authority(
        self,
        installation_id: str,
    ) -> tuple[InstallObservation, Any]: ...


@dataclass(frozen=True, slots=True)
class _Fence(Exception):
    code: str
    result_code: str


class _RetryableCreateError(RuntimeError):
    """Exact pre-bound ID has no installer row yet and may be retried."""


class DesiredInstallExecutor:
    """Dependency-injected, single-job execution and reconciliation core."""

    def __init__(
        self,
        *,
        store: DesiredInstallStoreProtocol,
        catalog: CatalogProvider,
        pairing: PairingProvider,
        inventory: InventoryProvider,
        storage: StorageProvider,
        runtimes: RuntimeProvider,
        installer: InstallerProtocol,
        installation_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._pairing = pairing
        self._inventory = inventory
        self._storage = storage
        self._runtimes = runtimes
        self._installer = installer
        self._installation_id_factory = (
            installation_id_factory
            if installation_id_factory is not None
            else lambda: str(uuid.uuid4())
        )

    async def approve(self, job_id: str) -> DesiredInstallRecord:
        """Validate one locally approved waiting job and start exactly one install."""

        record = await self._require_record(job_id)
        if (
            record.state != "awaiting_local_approval"
            or record.result_code != "local_approval_required"
            or record.terminal
            or record.installation_id is not None
        ):
            raise DesiredInstallExecutorError(
                "desired_install_not_awaiting_local_approval"
            )

        try:
            installation_id = self._installation_id_factory()
            if not _canonical_uuid(installation_id):
                raise DesiredInstallExecutorError(
                    "desired_install_installation_invalid"
                )
            plan = await self._build_plan(record, installation_id)
        except _Fence as fence:
            return await self._refuse(record, fence.result_code)
        except DesiredInstallStoreError:
            raise
        except Exception:
            return await self._fail(record, "internal_error")

        accepted = await self._transition(
            record,
            state="accepted",
            bytes_downloaded=0,
            total_bytes=plan.request.total_bytes,
            result_code=None,
            installation_id=plan.request.installation_id,
        )

        # Re-resolve all mutable local authority after approval is durably
        # recorded.  A storage remount, pairing rotation, inventory refresh,
        # catalog activation, or runtime change cannot ride the earlier check.
        try:
            current = await self._require_record(job_id)
            if current.state != "accepted" or current.terminal:
                return current
            revalidated = await self._build_plan(
                current,
                plan.request.installation_id,
            )
            if revalidated != plan:
                raise _Fence(
                    "desired_install_authority_changed",
                    "inventory_basis_stale",
                )
        except _Fence as fence:
            return await self._refuse(accepted, fence.result_code)
        except DesiredInstallStoreError:
            raise
        except Exception:
            return await self._fail(accepted, "internal_error")

        try:
            observation = await self._create_or_adopt(plan.request)
            self._validate_observation(observation, plan.request)
        except _RetryableCreateError as exc:
            # The exact installation ID was bound before create.  A missing
            # row can therefore be retried after this process restarts without
            # allocating a second ID or destination.
            raise DesiredInstallExecutorError(
                "desired_install_internal_error"
            ) from exc
        except Exception:
            await self._best_effort_cancel(plan.request.installation_id)
            latest = await self._require_record(job_id)
            if latest.terminal:
                return latest
            return await self._fail(latest, "internal_error")

        latest = await self._require_record(job_id)
        if latest.terminal:
            if latest.state == "cancelled":
                await self._best_effort_cancel(observation.id)
            return latest
        try:
            return await self._transition(
                latest,
                state="downloading",
                bytes_downloaded=_bounded_progress(
                    observation.bytes_downloaded,
                    plan.request.total_bytes,
                ),
                total_bytes=plan.request.total_bytes,
                result_code=None,
                installation_id=observation.id,
            )
        except DesiredInstallConflictError:
            # A Hub cancellation may win after NativeInstaller.create.  It is
            # a stop intent only; never delete the newly created destination.
            await self._best_effort_cancel(observation.id)
            return await self._require_record(job_id)

    async def reconcile(self, job_id: str) -> DesiredInstallRecord:
        """Fold one install observation into the durable path-free job journal."""

        record = await self._require_record(job_id)
        if record.terminal:
            if record.state == "cancelled" and record.installation_id is not None:
                if not await self._best_effort_cancel(record.installation_id):
                    # The desired-install receipt is already durably terminal,
                    # but an ambiguous installer stop must remain retryable.
                    # It never authorizes deletion or a replacement install.
                    raise DesiredInstallExecutorError(
                        "desired_install_internal_error"
                    )
            return record
        if record.state == "awaiting_local_approval":
            return record
        if record.installation_id is None:
            return await self._fail(record, "internal_error")

        try:
            plan = await self._build_plan(record, record.installation_id)
        except _Fence as fence:
            await self._best_effort_cancel(record.installation_id)
            return await self._refuse(record, fence.result_code)
        except DesiredInstallStoreError:
            raise
        except Exception:
            await self._best_effort_cancel(record.installation_id)
            return await self._fail(record, "internal_error")

        try:
            if record.state == "accepted":
                observation = await self._create_or_adopt(plan.request)
            else:
                observation = await self._installer.get_by_id(
                    record.installation_id
                )
            self._validate_observation(observation, plan.request)
            if observation.status in {"preparing", "partial", "downloaded"}:
                observation = await self._retry_or_adopt(
                    observation,
                    plan.request,
                )
                self._validate_observation(observation, plan.request)
        except _RetryableCreateError as exc:
            raise DesiredInstallExecutorError(
                "desired_install_internal_error"
            ) from exc
        except Exception:
            await self._best_effort_cancel(record.installation_id)
            return await self._fail(record, "internal_error")

        progress = _bounded_progress(
            observation.bytes_downloaded,
            plan.request.total_bytes,
        )
        if observation.status in {"queued", "downloading"}:
            return await self._advance(
                record, "downloading", progress, plan.request.total_bytes
            )
        if observation.status == "registering":
            return await self._advance(
                record, "verifying", progress, plan.request.total_bytes
            )
        if observation.status == "downloaded":
            return await self._advance(
                record,
                "downloaded_unregistered",
                progress,
                plan.request.total_bytes,
            )
        if observation.status == "installed":
            if progress != plan.request.total_bytes:
                return await self._fail(
                    record,
                    "verification_failed",
                    bytes_downloaded=progress,
                    total_bytes=plan.request.total_bytes,
                )
            try:
                verified_install, provenance = (
                    await self._installer.require_cleanup_authority(
                        plan.request.installation_id
                    )
                )
                self._validate_observation(verified_install, plan.request)
                if verified_install.status != "installed":
                    raise DesiredInstallExecutorError(
                        "desired_install_installation_invalid"
                    )
                _validate_completed_proof(provenance, plan)
            except Exception:
                return await self._fail(
                    record,
                    "verification_failed",
                    bytes_downloaded=progress,
                    total_bytes=plan.request.total_bytes,
                )
            current = await self._advance(
                record, "completed", progress, plan.request.total_bytes
            )
            # Reaching completed only observes NativeInstaller's successful
            # profile registration.  This module has no engine/load API, so
            # the newly registered model remains cold until ordinary JIT use.
            return current
        if observation.status == "cancelled":
            return await self._transition(
                record,
                state="cancelled",
                bytes_downloaded=progress,
                total_bytes=plan.request.total_bytes,
                result_code="cancelled_locally",
            )
        if observation.status == "failed":
            return await self._fail(
                record,
                "verification_failed",
                bytes_downloaded=progress,
                total_bytes=plan.request.total_bytes,
            )
        return await self._fail(
            record,
            "internal_error",
            bytes_downloaded=progress,
            total_bytes=plan.request.total_bytes,
        )

    async def cancel_locally(self, job_id: str) -> DesiredInstallRecord:
        """Stop one local install if active; never remove its files or profile."""

        record = await self._require_record(job_id)
        if record.terminal:
            return record
        if record.installation_id is not None:
            await self._best_effort_cancel(record.installation_id)
        try:
            return await self._transition(
                record,
                state="cancelled",
                bytes_downloaded=record.bytes_downloaded,
                total_bytes=record.total_bytes,
                result_code="cancelled_locally",
            )
        except DesiredInstallConflictError:
            return await self._require_record(job_id)

    async def _build_plan(
        self,
        record: DesiredInstallRecord,
        installation_id: str,
    ) -> ExecutionPlan:
        document = record.document
        if not _canonical_uuid(installation_id) or (
            record.installation_id is not None
            and record.installation_id != installation_id
        ):
            raise DesiredInstallExecutorError(
                "desired_install_installation_invalid"
            )
        pairing = await self._pairing.current_pairing()
        if (
            not pairing.active
            or pairing.pairing_id != document.pairing_id
            or pairing.credential_generation != document.credential_generation
        ):
            raise _Fence(
                "desired_install_pairing_generation_changed",
                "pairing_generation_changed",
            )

        inventory = await self._inventory.current_inventory()
        if (
            inventory.inventory_instance_id != document.inventory_instance_id
            or inventory.inventory_sequence < document.inventory_sequence
        ):
            raise _Fence(
                "desired_install_inventory_basis_stale",
                "inventory_basis_stale",
            )

        snapshot = await self._catalog.snapshot()
        selection = _catalog_selection(snapshot, record)

        if EngineName(document.engine) not in ACTIVE_ENGINE_NAMES:
            raise _Fence(
                "desired_install_catalog_install_interface_missing",
                "recipe_unknown",
            )

        local_storage = await self._storage.resolve_storage(
            document.storage_location_id,
            document.storage_binding_generation,
        )
        _validate_storage(local_storage, record, selection.total_bytes)
        assert local_storage is not None

        runtime = await self._runtimes.current_runtime(
            document.engine,
            document.catalog_digest,
        )
        _validate_runtime(
            runtime,
            selection.runtime,
            record,
            launch_contract=selection.launch_contract,
        )

        try:
            destination = str(
                install_destination(
                    Path(local_storage.configured_lexical_root),
                    EngineName(document.engine),
                    selection.repository_id,
                )
            )
        except (StorageError, ValueError):
            raise _Fence(
                "desired_install_catalog_install_interface_missing",
                "recipe_unknown",
            ) from None

        alias = document.alias
        if alias is None or not _MAC_ALIAS_RE.fullmatch(alias):
            raise _Fence(
                "desired_install_alias_unusable",
                "local_policy_refused",
            )

        request = InstallerRequest(
            installation_id=installation_id,
            repo_id=selection.repository_id,
            engine=document.engine,
            storage=local_storage.local_storage_name,
            alias=alias,
            destination=destination,
            revision=selection.revision,
            filename=selection.filename,
            projector_filename=selection.projector_filename,
            context_length=selection.context_length,
            download_files=selection.download_files,
            expected_files=selection.expected_files,
            expected_manifest_digest=selection.manifest_digest,
            capabilities=document.capabilities,
            family=selection.family,
            total_bytes=selection.total_bytes,
            launch_contract=selection.launch_contract,
            artifact_authority=ArtifactAuthority.SIGNED_CATALOG,
            source_identity_digest=selection.source_identity_digest,
            catalog_id=snapshot.catalog_id,
            logical_model_id=document.logical_model_id,
            artifact_id=document.artifact_id,
            recipe_id=document.recipe_id,
            catalog_digest=document.catalog_digest,
            require_exclusive_proof=True,
        )
        return ExecutionPlan(
            request=request,
            catalog_version=document.catalog_version,
            storage_location_id=document.storage_location_id,
            storage_binding_generation=document.storage_binding_generation,
            storage_lexical_root=local_storage.configured_lexical_root,
            storage_volume_uuid=local_storage.configured_volume_uuid,
            storage_scope_id=local_storage.configured_scope_id,
            artifact_manifest_digest=selection.manifest_digest,
            expected_files=selection.expected_files,
        )

    async def _create(self, request: InstallerRequest) -> InstallObservation:
        return await self._installer.create(
            installation_id=request.installation_id,
            repo_id=request.repo_id,
            engine=request.engine,
            storage=request.storage,
            alias=request.alias,
            destination=request.destination,
            revision=request.revision,
            filename=request.filename,
            projector_filename=request.projector_filename,
            context_length=request.context_length,
            download_files=request.download_files,
            expected_files=request.expected_files,
            expected_manifest_digest=request.expected_manifest_digest,
            capabilities=request.capabilities,
            family=request.family,
            total_bytes=request.total_bytes,
            launch_contract=request.launch_contract,
            artifact_authority=request.artifact_authority,
            source_identity_digest=request.source_identity_digest,
            catalog_id=request.catalog_id,
            logical_model_id=request.logical_model_id,
            artifact_id=request.artifact_id,
            recipe_id=request.recipe_id,
            catalog_digest=request.catalog_digest,
            require_exclusive_proof=request.require_exclusive_proof,
        )

    async def _create_or_adopt(
        self,
        request: InstallerRequest,
    ) -> InstallObservation:
        """Adopt or idempotently create the exact pre-bound installation ID."""

        try:
            existing = await self._installer.get_by_id(request.installation_id)
        except KeyError:
            existing = None
        if existing is not None:
            return existing
        try:
            return await self._create(request)
        except Exception as create_error:
            # NativeInstaller.create may fail after its SQLite transaction
            # committed.  One exact lookup distinguishes that case without a
            # second allocation or blind retry in the same turn.
            try:
                existing = await self._installer.get_by_id(
                    request.installation_id
                )
            except KeyError:
                raise _RetryableCreateError from create_error
            return existing

    async def _retry_or_adopt(
        self,
        observation: InstallObservation,
        request: InstallerRequest,
    ) -> InstallObservation:
        """Resume one exact interrupted row without allocating or redownloading."""

        previous_status = observation.status
        try:
            return await self._installer.retry(request.installation_id)
        except Exception as retry_error:
            try:
                latest = await self._installer.get_by_id(
                    request.installation_id
                )
            except KeyError:
                raise _RetryableCreateError from retry_error
            # retry() persists its replacement state before scheduling.  If
            # the row did not advance, retain the durable DesiredInstall state
            # and let a later explicit reconciliation retry the same ID.
            if latest.status == previous_status:
                raise _RetryableCreateError from retry_error
            return latest

    async def _advance(
        self,
        record: DesiredInstallRecord,
        target: str,
        bytes_downloaded: int,
        total_bytes: int,
    ) -> DesiredInstallRecord:
        current = record
        if current.state not in _EXECUTION_RANK:
            return await self._fail(current, "internal_error")
        current_rank = _EXECUTION_RANK[current.state]
        target_rank = _EXECUTION_RANK[target]
        if target_rank <= current_rank:
            return await self._transition(
                current,
                state=current.state,
                bytes_downloaded=max(current.bytes_downloaded, bytes_downloaded),
                total_bytes=total_bytes,
                result_code=None,
            )
        for state in _EXECUTION_STATES[current_rank + 1 : target_rank + 1]:
            current = await self._transition(
                current,
                state=state,
                bytes_downloaded=max(current.bytes_downloaded, bytes_downloaded),
                total_bytes=total_bytes,
                result_code=None,
            )
        return current

    async def _refuse(
        self,
        record: DesiredInstallRecord,
        result_code: str,
    ) -> DesiredInstallRecord:
        return await self._transition(
            record,
            state="refused",
            bytes_downloaded=record.bytes_downloaded,
            total_bytes=record.total_bytes,
            result_code=result_code,
        )

    async def _fail(
        self,
        record: DesiredInstallRecord,
        result_code: str,
        *,
        bytes_downloaded: int | None = None,
        total_bytes: int | None = None,
    ) -> DesiredInstallRecord:
        return await self._transition(
            record,
            state="failed",
            bytes_downloaded=(
                record.bytes_downloaded
                if bytes_downloaded is None
                else max(record.bytes_downloaded, bytes_downloaded)
            ),
            total_bytes=record.total_bytes if total_bytes is None else total_bytes,
            result_code=result_code,
        )

    async def _transition(
        self,
        record: DesiredInstallRecord,
        *,
        state: str,
        bytes_downloaded: int,
        total_bytes: int | None,
        result_code: str | None,
        installation_id: str | None = None,
    ) -> DesiredInstallRecord:
        transition = await self._store.transition(
            job_id=record.document.job_id,
            job_revision=record.document.job_revision,
            state=state,
            bytes_downloaded=bytes_downloaded,
            total_bytes=total_bytes,
            result_code=result_code,
            installation_id=installation_id,
        )
        return transition.record

    async def _require_record(self, job_id: str) -> DesiredInstallRecord:
        record = await self._store.get(job_id)
        if record is None:
            raise DesiredInstallExecutorError("desired_install_job_unknown")
        return record

    async def _best_effort_cancel(self, installation_id: str) -> bool:
        """Issue a stop-only intent and report whether inactivity was proved."""

        try:
            observation = await self._installer.get_by_id(installation_id)
            if observation.status in _ACTIVE_INSTALL_STATES:
                observation = await self._installer.cancel(installation_id)
            return observation.status not in _ACTIVE_INSTALL_STATES
        except Exception:
            # Cancellation is stop-only.  An ambiguous stop never authorizes
            # deletion or a second install, and reconciliation remains fixed-
            # code/content-free.
            return False

    @staticmethod
    def _validate_observation(
        observation: InstallObservation,
        request: InstallerRequest,
    ) -> None:
        try:
            files = _string_tuple_from_json(observation.files_json)
            expected_files = owned_files_from_canonical_json(
                observation.expected_files_json or ""
            )
            capabilities = _string_tuple_from_json(observation.capabilities_json)
            launch = install_launch_from_json(
                observation.engine,
                observation.launch_json,
            )
            valid = (
                _canonical_uuid(observation.id)
                and observation.id == request.installation_id
                and observation.repo_id == request.repo_id
                and observation.engine == request.engine
                and observation.storage == request.storage
                and observation.alias == request.alias
                and observation.destination == request.destination
                and observation.revision == request.revision
                and observation.filename == request.filename
                and observation.projector_filename == request.projector_filename
                and observation.context_length == request.context_length
                and files == request.download_files
                and expected_files == request.expected_files
                and observation.expected_manifest_digest
                == request.expected_manifest_digest
                and capabilities == request.capabilities
                and observation.family == request.family
                and observation.total_bytes == request.total_bytes
                and launch == request.launch_contract
                and isinstance(observation.bytes_downloaded, int)
                and not isinstance(observation.bytes_downloaded, bool)
                and observation.bytes_downloaded >= 0
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise DesiredInstallExecutorError(
                "desired_install_installation_invalid"
            )


@dataclass(frozen=True, slots=True)
class _CatalogSelection:
    repository_id: str
    revision: str
    filename: str | None
    projector_filename: str | None
    download_files: tuple[str, ...]
    context_length: int
    family: str
    total_bytes: int
    source_identity_digest: str
    runtime: Mapping[str, Any]
    launch_contract: InstallLaunchContract
    manifest_digest: str
    expected_files: tuple[OwnedFile, ...]


def _catalog_selection(
    snapshot: CatalogSnapshotProtocol,
    record: DesiredInstallRecord,
) -> _CatalogSelection:
    document = record.document
    try:
        catalog = snapshot.catalog()
        if (
            snapshot.source != "signed"
            or snapshot.catalog_version != document.catalog_version
            or snapshot.catalog_digest != document.catalog_digest
            or snapshot.catalog_id != catalog["catalog_id"]
            or catalog["catalog_version"] != snapshot.catalog_version
            or catalog_digest(catalog) != snapshot.catalog_digest
        ):
            raise _Fence(
                "desired_install_catalog_changed",
                "catalog_changed",
            )
        models = _unique_rows(
            catalog["logical_models"], "logical_model_id"
        )
        artifacts = _unique_rows(catalog["artifacts"], "artifact_id")
        recipes = _unique_rows(catalog["recipes"], "recipe_id")
        model = models.get(document.logical_model_id)
        recipe = recipes.get(document.recipe_id)
        if model is None or recipe is None:
            raise _Fence(
                "desired_install_recipe_unknown",
                "recipe_unknown",
            )
        artifact = artifacts.get(document.artifact_id)
        if artifact is None:
            raise _Fence(
                "desired_install_artifact_mismatch",
                "artifact_mismatch",
            )
        context = recipe["context"]
        capabilities = tuple(recipe["capabilities"])
        if (
            recipe["logical_model_id"] != document.logical_model_id
            or recipe["artifact_id"] != document.artifact_id
            or recipe["engine"] != document.engine
            or recipe["runtime"]["engine"] != document.engine
            or recipe["launch"]["engine"] != document.engine
            or artifact["logical_model_id"] != document.logical_model_id
            or capabilities != document.capabilities
            or tuple(sorted(set(capabilities))) != capabilities
            or context["guaranteed_tokens"]
            != document.guaranteed_context_tokens
            or not set(capabilities).issubset(set(model["capabilities"]))
        ):
            raise _Fence(
                "desired_install_artifact_mismatch",
                "artifact_mismatch",
            )

        expected_format = {
            "llama.cpp": "gguf",
            "omlx": "mlx",
            "ds4": "ds4-weights",
        }.get(document.engine)
        files = artifact["files"]
        if (
            expected_format is None
            or not isinstance(files, list)
            or not 1 <= len(files) <= 4096
            or any(
                not isinstance(item, Mapping)
                or not _safe_relative_path(item.get("path"))
                or not _positive_int(item.get("size_bytes"))
                or not _safe_digest(item.get("sha256"))
                for item in files
            )
            or artifact["format"] != expected_format
            or artifact["manifest_digest"] != artifact_manifest_digest(files)
            or artifact["total_size_bytes"]
            != sum(item["size_bytes"] for item in files)
            or not _positive_int(artifact["total_size_bytes"])
            or not _safe_digest(artifact["manifest_digest"])
        ):
            raise _Fence(
                "desired_install_artifact_mismatch",
                "artifact_mismatch",
            )
        source = artifact["source"]
        if (
            source.get("kind") != "huggingface"
            or source.get("transport") != "https"
            or source.get("registry") != "huggingface.co"
            or not _safe_repository_id(source.get("repository_id"))
            or not _safe_revision(source.get("revision"))
            or not _safe_identifier(model.get("family"))
            or not _positive_int(context.get("guaranteed_tokens"))
        ):
            raise _Fence(
                "desired_install_catalog_install_interface_missing",
                "recipe_unknown",
            )
        download_files = tuple(item["path"] for item in files)
        if (
            not download_files
            or download_files != tuple(sorted(set(download_files)))
            or any(is_hf_local_metadata_path(path) for path in download_files)
        ):
            raise _Fence(
                "desired_install_artifact_mismatch",
                "artifact_mismatch",
            )
        filename: str | None = None
        projector_filename: str | None = None
        if document.engine in {"llama.cpp", "ds4"}:
            filename, projector_filename = _catalog_gguf_selection(
                artifact,
                engine=document.engine,
                download_files=download_files,
            )
        elif artifact.get("gguf_layout") is not None:
            raise _Fence(
                "desired_install_artifact_mismatch",
                "artifact_mismatch",
            )
        identity = {
            "artifact_id": document.artifact_id,
            "authority": ArtifactAuthority.SIGNED_CATALOG.value,
            "catalog_digest": document.catalog_digest,
            "catalog_id": snapshot.catalog_id,
            "files": files,
            "logical_model_id": document.logical_model_id,
            "manifest_digest": artifact["manifest_digest"],
            "recipe_id": document.recipe_id,
            "source": source,
            "total_size_bytes": artifact["total_size_bytes"],
        }
        if artifact.get("gguf_layout") is not None:
            identity["gguf_layout"] = artifact["gguf_layout"]
        source_identity_digest = "sha256:" + hashlib.sha256(
            canonical_json(identity)
        ).hexdigest()
        launch_contract = validate_install_launch(
            document.engine,
            recipe["launch"],
        )
        return _CatalogSelection(
            repository_id=source["repository_id"],
            revision=source["revision"],
            filename=filename,
            projector_filename=projector_filename,
            download_files=download_files,
            context_length=context["guaranteed_tokens"],
            family=model["family"],
            total_bytes=artifact["total_size_bytes"],
            source_identity_digest=source_identity_digest,
            runtime=recipe["runtime"],
            launch_contract=launch_contract,
            manifest_digest=artifact["manifest_digest"],
            expected_files=tuple(
                OwnedFile(
                    path=item["path"],
                    size_bytes=item["size_bytes"],
                    sha256=item["sha256"],
                )
                for item in files
            ),
        )
    except _Fence:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _Fence(
            "desired_install_catalog_changed",
            "catalog_changed",
        ) from None


def _catalog_gguf_selection(
    artifact: Mapping[str, Any],
    *,
    engine: str,
    download_files: tuple[str, ...],
) -> tuple[str, str | None]:
    """Resolve only explicit signed launch roles, retaining legacy one-file v1.

    The artifact is the selection boundary.  Every listed file is required;
    an optional projector is represented by a distinct signed artifact whose
    layout names that one selected projector.  This function never infers a
    primary shard or projector from path ordering or naming conventions.
    """

    layout = artifact.get("gguf_layout")
    if layout is None:
        if len(download_files) == 1 and download_files[0].casefold().endswith(
            ".gguf"
        ):
            return download_files[0], None
        raise _Fence(
            "desired_install_catalog_install_interface_missing",
            "recipe_unknown",
        )
    try:
        if not isinstance(layout, Mapping) or set(layout) != {
            "kind",
            "primary_file",
            "required_shards",
            "selected_projector_file",
        }:
            raise ValueError
        primary = layout["primary_file"]
        shards_value = layout["required_shards"]
        projector = layout["selected_projector_file"]
        if (
            layout["kind"] != "gguf-file-set"
            or not _safe_relative_path(primary)
            or not isinstance(shards_value, list)
            or any(not _safe_relative_path(item) for item in shards_value)
            or shards_value != sorted(set(shards_value))
            or (projector is not None and not _safe_relative_path(projector))
        ):
            raise ValueError
        shards = tuple(shards_value)
        selected = (primary, *shards)
        if projector is not None:
            selected = (*selected, projector)
        if (
            len(selected) != len(set(selected))
            or set(selected) != set(download_files)
            or any(not path.casefold().endswith(".gguf") for path in selected)
            or (engine == "ds4" and projector is not None)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _Fence(
            "desired_install_artifact_mismatch",
            "artifact_mismatch",
        ) from None
    return primary, projector


def _validate_completed_proof(provenance: Any, plan: ExecutionPlan) -> None:
    """Require exact revision-2 signed-catalog evidence before completion."""

    try:
        complete_owned_files = tuple(
            OwnedFile(
                path=item.path,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
            )
            for item in provenance.owned_files
        )
        allowed_metadata = allowed_hf_local_metadata_paths(
            tuple(item.path for item in plan.expected_files)
        )
        payload_files = tuple(
            item
            for item in complete_owned_files
            if not is_hf_local_metadata_path(item.path)
        )
        metadata_paths = {
            item.path
            for item in complete_owned_files
            if is_hf_local_metadata_path(item.path)
        }
        valid = (
            provenance.installation_id == plan.request.installation_id
            and _enum_value(provenance.source_kind) == "managed_download"
            and _enum_value(provenance.ownership_class) == "exclusive_managed"
            and _enum_value(provenance.artifact_authority) == "signed_catalog"
            and provenance.provenance_revision == 2
            and provenance.storage_location_id == plan.storage_location_id
            and provenance.storage_binding_generation
            == plan.storage_binding_generation
            and provenance.storage_lexical_root == plan.storage_lexical_root
            and provenance.storage_volume_uuid == plan.storage_volume_uuid
            and provenance.storage_scope_id == plan.storage_scope_id
            and provenance.lexical_destination == plan.request.destination
            and _safe_digest(provenance.destination_binding_digest)
            and provenance.catalog_id == plan.request.catalog_id
            and provenance.logical_model_id == plan.request.logical_model_id
            and provenance.artifact_id == plan.request.artifact_id
            and provenance.recipe_id == plan.request.recipe_id
            and provenance.resolved_revision == plan.request.revision
            and provenance.catalog_digest == plan.request.catalog_digest
            and provenance.source_identity_digest
            == plan.request.source_identity_digest
            # The cleanup manifest owns the complete tree, including the
            # downloader's resumable local metadata. Artifact authority is a
            # separate exact projection over signed payload files only.
            and _safe_digest(provenance.manifest_digest)
            and payload_files == plan.expected_files
            and metadata_paths.issubset(allowed_metadata)
            and sum(item.size_bytes for item in payload_files)
            == plan.request.total_bytes
            and _enum_value(provenance.destination_state_before) == "absent"
            and provenance.destination_created_by_transaction is True
            and tuple(provenance.preexisting_entries) == ()
            and tuple(provenance.extra_entries) == ()
            and _canonical_uuid(provenance.creation_transaction_id)
            and _positive_identity_int(provenance.directory_device)
            and _positive_identity_int(provenance.directory_inode)
        )
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise DesiredInstallExecutorError(
            "desired_install_installation_invalid"
        )

def _validate_storage(
    storage: LocalStorageAuthority | None,
    record: DesiredInstallRecord,
    artifact_bytes: int,
) -> None:
    document = record.document
    if storage is None:
        raise _Fence(
            "desired_install_storage_location_unknown",
            "storage_location_unknown",
        )
    if (
        storage.storage_location_id != document.storage_location_id
        or storage.binding_generation != document.storage_binding_generation
        or storage.indexed_lexical_root != storage.configured_lexical_root
        or storage.observed_lexical_root != storage.configured_lexical_root
        or storage.indexed_volume_uuid != storage.configured_volume_uuid
        or storage.indexed_scope_id != storage.configured_scope_id
        or not storage.local_storage_name
    ):
        raise _Fence(
            "desired_install_storage_binding_changed",
            "storage_binding_changed",
        )
    if storage.configured_volume_uuid is not None and (
        storage.observed_volume_uuid is None
        or storage.observed_volume_uuid.casefold()
        != storage.configured_volume_uuid.casefold()
    ):
        raise _Fence(
            "desired_install_storage_binding_changed",
            "storage_binding_changed",
        )
    if (
        storage.availability != "available"
        or not storage.exists
        or not storage.is_directory
        or not storage.volume_matches
    ):
        raise _Fence(
            "desired_install_storage_unavailable",
            "storage_unavailable",
        )
    if not storage.remote_installs_allowed:
        raise _Fence(
            "desired_install_local_policy_refused",
            "local_policy_refused",
        )
    if not storage.writable:
        raise _Fence(
            "desired_install_storage_read_only",
            "storage_read_only",
        )
    if (
        isinstance(storage.reserve_bytes, bool)
        or not isinstance(storage.reserve_bytes, int)
        or storage.reserve_bytes < 0
        or isinstance(storage.free_bytes, bool)
        or not isinstance(storage.free_bytes, int)
        or storage.free_bytes < artifact_bytes + storage.reserve_bytes
    ):
        raise _Fence(
            "desired_install_insufficient_storage",
            "insufficient_storage",
        )


def _validate_runtime(
    runtime: RuntimeAuthority | None,
    requirement: Mapping[str, Any],
    record: DesiredInstallRecord,
    *,
    launch_contract: InstallLaunchContract,
) -> None:
    if runtime is None:
        raise _Fence(
            "desired_install_runtime_unavailable",
            "runtime_unavailable",
        )
    try:
        features = tuple(sorted(set(runtime.features)))
        required_features = tuple(requirement["required_features"])
        allowed = tuple(requirement["allowed_runtime_fingerprints"])
        known_bad_fingerprints = tuple(
            requirement["known_bad_runtime_fingerprints"]
        )
        known_bad_versions = tuple(requirement["known_bad_versions"])
        fingerprint_verified = bool(allowed) and (
            runtime.runtime_fingerprint in allowed
        )
        compatible = (
            runtime.engine == record.document.engine
            and requirement["engine"] == record.document.engine
            and runtime.enabled
            and runtime.healthy
            and runtime.release_tier == requirement["release_tier"]
            and runtime.catalog_digest == record.document.catalog_digest
            and runtime.version not in known_bad_versions
            and runtime.runtime_fingerprint not in known_bad_fingerprints
            and set(required_features).issubset(features)
            and (
                fingerprint_verified
                or (
                    not allowed
                    and _version_in_range(
                        runtime.version,
                        requirement["minimum_version"],
                        requirement["maximum_version_exclusive"],
                    )
                )
            )
        )
        if isinstance(launch_contract, OMLXInstallLaunch):
            # The catalog names a service-global contract, so approval is
            # allowed only when the selected Mac's external oMLX already
            # proves the exact state. Installing a model never changes it.
            compatible = bool(
                compatible
                and runtime.omlx_scheduler_slots
                == launch_contract.scheduler_slots
                and isinstance(runtime.omlx_memory_guard_enabled, bool)
                and (
                    launch_contract.memory_guard == "optional"
                    or runtime.omlx_memory_guard_enabled is True
                )
            )
    except (KeyError, TypeError, ValueError, AttributeError):
        compatible = False
    if not compatible:
        raise _Fence(
            "desired_install_runtime_unavailable",
            "runtime_unavailable",
        )


def _unique_rows(value: object, key: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, Mapping[str, Any]] = {}
    for row in value:
        if not isinstance(row, Mapping):
            raise ValueError
        identifier = row.get(key)
        if not isinstance(identifier, str) or identifier in result:
            raise ValueError
        result[identifier] = row
    return result


def _version_in_range(
    version: str | None,
    minimum: str,
    maximum_exclusive: str | None,
) -> bool:
    if version is None:
        return False
    if version == minimum and maximum_exclusive != minimum:
        return True
    current = _version_key(version)
    lower = _version_key(minimum)
    if current is None or lower is None or current[0] != lower[0]:
        return False
    if current[1] < lower[1]:
        return False
    if maximum_exclusive is None:
        return True
    upper = _version_key(maximum_exclusive)
    return upper is not None and upper[0] == current[0] and current[1] < upper[1]


def _version_key(value: str) -> tuple[str, tuple[int, ...]] | None:
    match = _LLAMA_BUILD_RE.fullmatch(value)
    if match:
        return "llama-build", (int(match.group(1)),)
    match = _NUMERIC_VERSION_RE.fullmatch(value)
    if match:
        parts = [int(piece) for piece in match.group(1).split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return "numeric", tuple(parts)
    return None


def _canonical_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _safe_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value[0].isalnum()
        and all(
            character.isascii()
            and (character.isalnum() or character in "._:+-")
            for character in value
        )
    )


def _safe_relative_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
    ):
        return False
    return all(segment not in {"", ".", ".."} for segment in value.split("/"))


def _safe_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _positive_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= 1_152_921_504_606_846_976
    )


def _positive_identity_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= (1 << 63) - 1
    )


def _safe_repository_id(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 200:
        return False
    pieces = value.split("/")
    return len(pieces) == 2 and all(
        1 <= len(piece) <= 99
        and piece[0].isalnum()
        and all(
            character.isascii()
            and (character.isalnum() or character in "._-")
            for character in piece
        )
        for piece in pieces
    )


def _safe_revision(value: object) -> bool:
    return (
        isinstance(value, str)
        and 40 <= len(value) <= 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bounded_progress(value: object, total: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DesiredInstallExecutorError("desired_install_installation_invalid")
    return min(value, total)


def _string_tuple_from_json(value: str | None) -> tuple[str, ...]:
    import json

    decoded = json.loads(value) if value is not None else []
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) for item in decoded
    ):
        raise ValueError
    return tuple(decoded)


__all__ = [
    "EXECUTOR_ERROR_CODES",
    "CatalogProvider",
    "DesiredInstallExecutor",
    "DesiredInstallExecutorError",
    "ExecutionPlan",
    "ExpectedArtifactFile",
    "InstallerProtocol",
    "InstallerRequest",
    "InventoryAuthority",
    "InventoryProvider",
    "LocalStorageAuthority",
    "PairingAuthority",
    "PairingProvider",
    "RuntimeAuthority",
    "RuntimeProvider",
    "StorageProvider",
]
