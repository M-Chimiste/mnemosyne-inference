"""Killable, durable native model-install orchestration."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Awaitable, Callable
from uuid import UUID, uuid4

from .config import StorageConfig, StorageLocationConfig
from .filesystem import FilesystemProbe, FilesystemProbeError
from .install_launch import InstallLaunchInput, validate_install_launch
from .install_provenance import (
    ArtifactAuthority,
    ExclusiveManagedProof,
    InstallationProvenance,
    ManagedCreationClaim,
    ManagedCreationIntent,
    MANAGED_CREATION_MARKER_PATH,
    OwnedFile,
    ProvenanceDataError,
    ProvenanceError,
    allowed_hf_local_metadata_paths,
    destination_binding_digest,
    exclusive_proof_from_claim,
    is_hf_local_metadata_path,
    local_pinned_source_digest,
)
from .install_store import InstallRecord, InstallStore
from .mac_inventory_store import MacInventoryIndex, MacInventoryIndexError
from .models import EngineName
from .scoped_process import wrap_scoped_argv
from .storage import StorageError, install_destination


InstallCallback = Callable[[InstallRecord], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _RegistrationStorageBinding:
    """One process-local fallback for installs without durable creation authority."""

    storage_name: str
    lexical_root: str
    volume_uuid: str | None
    scope_id: str | None
    lexical_destination: str
    resolved_revision: str | None


class NativeInstaller:
    def __init__(
        self,
        database_path: str | Path,
        *,
        on_installed: InstallCallback | None = None,
        storage: StorageConfig | None = None,
        filesystem_probe: FilesystemProbe | None = None,
        inventory_index: MacInventoryIndex | None = None,
    ) -> None:
        self.store = InstallStore(database_path)
        self.on_installed = on_installed
        self.storage = storage
        self.filesystem = filesystem_probe
        self.inventory_index = inventory_index
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._registration_storage_bindings: dict[
            str, _RegistrationStorageBinding
        ] = {}
        self._semaphore = asyncio.Semaphore(1)

    async def start(self) -> None:
        await asyncio.to_thread(self.store.recover_interrupted)
        # A crash can occur after the durable intent, before or after the
        # marker-bound mkdir, but before the claim transaction. Recover only an
        # exact marker and current local binding; ambiguous content stays inert.
        preparing = await asyncio.to_thread(self.store.preparing)
        for record in preparing:
            try:
                await self._recover_preparing(record, retry_status="partial")
            except Exception:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self.store.mark_creation_recovery_required,
                        record.id,
                    )

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._registration_storage_bindings.clear()
        await asyncio.to_thread(self.store.close)

    async def create(
        self,
        *,
        installation_id: str | None = None,
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
        launch_contract: InstallLaunchInput | None = None,
        artifact_authority: ArtifactAuthority | None = None,
        source_identity_digest: str | None = None,
        catalog_id: str | None = None,
        logical_model_id: str | None = None,
        artifact_id: str | None = None,
        recipe_id: str | None = None,
        catalog_digest: str | None = None,
        require_exclusive_proof: bool = False,
    ) -> InstallRecord:
        typed_launch = (
            validate_install_launch(engine, launch_contract)
            if launch_contract is not None
            else None
        )
        if typed_launch is not None and (
            artifact_authority is not ArtifactAuthority.SIGNED_CATALOG
            or require_exclusive_proof is not True
        ):
            raise ValueError("install_launch_requires_signed_catalog_authority")
        if (
            artifact_authority is ArtifactAuthority.SIGNED_CATALOG
            and typed_launch is None
        ):
            raise ValueError("signed_catalog_install_launch_required")
        if installation_id is None:
            installation_id = str(uuid4())
        else:
            try:
                canonical_installation_id = str(UUID(installation_id))
            except (AttributeError, ValueError):
                raise ValueError("installation_id_invalid") from None
            if installation_id != canonical_installation_id:
                raise ValueError("installation_id_invalid")
        creation_intent = await self._build_creation_intent(
            installation_id=installation_id,
            repo_id=repo_id,
            engine=engine,
            storage_name=storage,
            destination=destination,
            revision=revision,
            filename=filename,
            download_files=download_files,
            artifact_authority=artifact_authority,
            source_identity_digest=source_identity_digest,
            catalog_id=catalog_id,
            logical_model_id=logical_model_id,
            artifact_id=artifact_id,
            recipe_id=recipe_id,
            catalog_digest=catalog_digest,
            require_exclusive_proof=require_exclusive_proof,
        )
        record = await asyncio.to_thread(
            self.store.create,
            installation_id=installation_id,
            creation_intent=creation_intent,
            defer_until_creation_claim=creation_intent is not None,
            repo_id=repo_id,
            engine=engine,
            storage=storage,
            alias=alias,
            destination=destination,
            revision=revision,
            filename=filename,
            projector_filename=projector_filename,
            context_length=context_length,
            download_files=download_files,
            expected_files=expected_files,
            expected_manifest_digest=expected_manifest_digest,
            capabilities=capabilities,
            family=family,
            launch_contract=typed_launch,
            total_bytes=total_bytes,
        )
        if creation_intent is not None:
            try:
                creation_claim = await self._materialize_creation_claim(
                    record,
                    creation_intent,
                )
                record = await asyncio.to_thread(
                    self.store.finalize_creation,
                    record.id,
                    creation_claim,
                )
            except BaseException:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self.store.mark_creation_recovery_required,
                        record.id,
                    )
                raise
        self._schedule(record.id)
        return record

    async def _build_creation_intent(
        self,
        *,
        installation_id: str,
        repo_id: str,
        engine: str,
        storage_name: str,
        destination: str,
        revision: str | None,
        filename: str | None,
        download_files: list[str] | tuple[str, ...],
        artifact_authority: ArtifactAuthority | None,
        source_identity_digest: str | None,
        catalog_id: str | None,
        logical_model_id: str | None,
        artifact_id: str | None,
        recipe_id: str | None,
        catalog_digest: str | None,
        require_exclusive_proof: bool,
    ) -> ManagedCreationIntent | None:
        """Resolve and persistable authority without touching the filesystem."""

        if (
            self.storage is None
            or self.filesystem is None
            or self.inventory_index is None
            or revision is None
        ):
            if require_exclusive_proof:
                raise RuntimeError("managed_install_proof_unavailable")
            return None
        location = next(
            (item for item in self.storage.locations if item.name == storage_name),
            None,
        )
        if location is None:
            if require_exclusive_proof:
                raise RuntimeError("managed_install_storage_binding_unavailable")
            return None
        try:
            bindings = await self.inventory_index.reconcile_storage(
                (
                    item.name,
                    item.path,
                    item.volume_uuid,
                    item.scope_id,
                )
                for item in self.storage.locations
            )
            binding = bindings.get(storage_name)
            if (
                binding is None
                or binding.local_key != location.name
                or binding.exact_path != location.path
                or binding.volume_uuid != location.volume_uuid
                or binding.scope_id != location.scope_id
            ):
                raise ProvenanceDataError("creation_claim_storage_mismatch")
            authority = (
                artifact_authority
                if artifact_authority is not None
                else ArtifactAuthority.LOCAL_PINNED_DISCOVERY
            )
            if source_identity_digest is None:
                if authority is not ArtifactAuthority.LOCAL_PINNED_DISCOVERY:
                    raise ProvenanceDataError("source_identity_invalid")
                selected_files = tuple(download_files)
                if not selected_files and filename:
                    selected_files = (filename,)
                source_identity_digest = local_pinned_source_digest(
                    repo_id=repo_id,
                    engine=engine,
                    resolved_revision=revision,
                    download_files=selected_files,
                )
            binding_digest = destination_binding_digest(
                storage_location_id=binding.storage_location_id,
                storage_binding_generation=binding.binding_generation,
                storage_lexical_root=binding.exact_path,
                lexical_destination=destination,
                storage_volume_uuid=binding.volume_uuid,
                storage_scope_id=binding.scope_id,
            )
            return ManagedCreationIntent(
                installation_id=installation_id,
                storage_location_id=binding.storage_location_id,
                storage_binding_generation=binding.binding_generation,
                storage_lexical_root=binding.exact_path,
                storage_volume_uuid=binding.volume_uuid,
                storage_scope_id=binding.scope_id,
                lexical_destination=destination,
                destination_binding_digest=binding_digest,
                resolved_revision=revision,
                artifact_authority=authority,
                source_identity_digest=source_identity_digest,
                catalog_id=catalog_id,
                logical_model_id=logical_model_id,
                artifact_id=artifact_id,
                recipe_id=recipe_id,
                catalog_digest=catalog_digest,
                creation_transaction_id=str(uuid4()),
                require_exclusive_proof=require_exclusive_proof,
            )
        except asyncio.CancelledError:
            raise
        except (
            FilesystemProbeError,
            MacInventoryIndexError,
            ProvenanceDataError,
            OSError,
            ValueError,
        ) as exc:
            if require_exclusive_proof:
                raise RuntimeError("managed_install_proof_unavailable") from exc
            return None

    async def _materialize_creation_claim(
        self,
        record: InstallRecord,
        intent: ManagedCreationIntent,
        *,
        recovering: bool = False,
    ) -> ManagedCreationClaim | None:
        if self.filesystem is None:
            raise RuntimeError("managed_install_proof_unavailable")
        location = self._storage_location(record)
        if location is None or (
            location.path != intent.storage_lexical_root
            or location.volume_uuid != intent.storage_volume_uuid
            or location.scope_id != intent.storage_scope_id
            or record.destination != intent.lexical_destination
            or record.revision != intent.resolved_revision
        ):
            raise ProvenanceDataError("creation_intent_binding_changed")
        prepared = await self.filesystem.prepare_managed_destination(
            root=location.path,
            path=record.destination,
            expected_volume_uuid=location.volume_uuid,
            scope_id=location.scope_id,
            installation_id=intent.installation_id,
            creation_transaction_id=intent.creation_transaction_id,
            destination_binding_digest=intent.destination_binding_digest,
            source_identity_digest=intent.source_identity_digest,
            allow_preexisting_unowned=(
                not recovering and not intent.require_exclusive_proof
            ),
        )
        if prepared.unowned_preexisting:
            if intent.require_exclusive_proof:
                raise ProvenanceDataError("creation_claim_destination_mismatch")
            return None
        if (
            prepared.path != record.destination
            or prepared.state_before != "absent"
            or prepared.created is not True
        ):
            raise ProvenanceDataError("creation_claim_destination_mismatch")
        return ManagedCreationClaim(
            installation_id=intent.installation_id,
            storage_location_id=intent.storage_location_id,
            storage_binding_generation=intent.storage_binding_generation,
            storage_lexical_root=intent.storage_lexical_root,
            storage_volume_uuid=intent.storage_volume_uuid,
            storage_scope_id=intent.storage_scope_id,
            lexical_destination=intent.lexical_destination,
            destination_binding_digest=intent.destination_binding_digest,
            resolved_revision=intent.resolved_revision,
            artifact_authority=intent.artifact_authority,
            source_identity_digest=intent.source_identity_digest,
            catalog_id=intent.catalog_id,
            logical_model_id=intent.logical_model_id,
            artifact_id=intent.artifact_id,
            recipe_id=intent.recipe_id,
            catalog_digest=intent.catalog_digest,
            creation_transaction_id=intent.creation_transaction_id,
            directory_device=prepared.directory_device,
            directory_inode=prepared.directory_inode,
        )

    async def _recover_preparing(
        self,
        record: InstallRecord,
        *,
        retry_status: str,
    ) -> InstallRecord:
        intent_record = await asyncio.to_thread(
            self.store.get_creation_intent,
            record.id,
        )
        if intent_record is None or intent_record.state not in {
            "pending",
            "recovery_required",
        }:
            raise ProvenanceDataError("creation_intent_missing")
        claim = await self._materialize_creation_claim(
            record,
            intent_record.intent,
            recovering=True,
        )
        return await asyncio.to_thread(
            self.store.finalize_creation,
            record.id,
            claim,
            retry_status=retry_status,
        )

    async def retry(self, install_id: str) -> InstallRecord:
        record = await asyncio.to_thread(self.store.get, install_id)
        intent_record = await asyncio.to_thread(
            self.store.get_creation_intent,
            install_id,
        )
        unresolved_creation = intent_record is not None and intent_record.state in {
            "pending",
            "recovery_required",
        }
        if unresolved_creation:
            if record.status not in {
                "preparing",
                "failed",
                "cancelled",
                "partial",
            }:
                raise ValueError("install creation cannot be retried from this state")
            if record.status != "preparing":
                record = await asyncio.to_thread(
                    self.store.update,
                    install_id,
                    status="preparing",
                    error=None,
                    pid=None,
                    download_speed_bps=None,
                )
            try:
                record = await self._recover_preparing(
                    record,
                    retry_status="queued",
                )
            except BaseException:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self.store.mark_creation_recovery_required,
                        record.id,
                    )
                raise
            self._schedule(install_id)
            return record
        if record.status not in {"failed", "cancelled", "partial", "downloaded"}:
            raise ValueError(
                "only failed, cancelled, partial, or downloaded installs can be retried"
            )
        active = self._tasks.get(install_id)
        if active is not None and not active.done():
            # A terminal state is persisted just before the original task
            # returns. Let its cleanup finish so a fast UI retry cannot leave
            # the record in "registering" without a replacement task.
            await asyncio.gather(active, return_exceptions=True)
        next_status = "registering" if record.status == "downloaded" else "queued"
        record = await asyncio.to_thread(
            self.store.update_if_status,
            install_id,
            record.status,
            status=next_status,
            error=None,
            pid=None,
            download_speed_bps=None,
        )
        self._schedule(install_id)
        return record

    async def cancel(self, install_id: str) -> InstallRecord:
        record = await asyncio.to_thread(self.store.get, install_id)
        if record.status == "preparing":
            # Cancellation is stop-only. Retain the durable creation intent
            # and any marker-bound filesystem effect for an explicit retry.
            return await asyncio.to_thread(
                self.store.update,
                install_id,
                status="cancelled",
                error="install cancelled before download",
                pid=None,
                download_speed_bps=None,
            )
        if record.status not in {"queued", "downloading", "registering"}:
            raise ValueError("install is not active")
        task = self._tasks.get(install_id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return await asyncio.to_thread(self.store.get, install_id)

    async def get(self, install_id: str) -> InstallRecord:
        return await self.get_by_id(install_id)

    async def get_by_id(self, installation_id: str) -> InstallRecord:
        return await asyncio.to_thread(self.store.get_by_id, installation_id)

    async def require_registration_storage_binding(
        self,
        record: InstallRecord,
        storage: StorageConfig,
    ) -> None:
        """Fence profile registration to the install-time storage authority.

        A Settings save may persist a replacement path under the same local
        storage name while a download is still finishing.  The worker must not
        turn that replacement into authority for bytes already written under
        the old exact lexical root. Managed installs use their immutable
        creation claim without making profile registration depend on the live
        inventory index; older installs may use only the immutable storage
        snapshot captured by the process that actually launched their current
        download attempt.
        """

        candidate = next(
            (item for item in storage.locations if item.name == record.storage),
            None,
        )
        claim = await asyncio.to_thread(
            self.store.get_creation_claim,
            record.id,
        )
        intent_record = (
            None
            if claim is not None
            else await asyncio.to_thread(
                self.store.get_creation_intent,
                record.id,
            )
        )
        authority = claim or (
            intent_record.intent if intent_record is not None else None
        )
        if authority is not None:
            if (
                candidate is None
                or candidate.path != authority.storage_lexical_root
                or candidate.volume_uuid != authority.storage_volume_uuid
                or candidate.scope_id != authority.storage_scope_id
                or record.destination != authority.lexical_destination
                or record.revision != authority.resolved_revision
            ):
                raise ProvenanceDataError(
                    "registration_storage_binding_changed"
                )
            return

        applied = self._registration_storage_bindings.get(record.id)
        expected_destination = None
        if candidate is not None:
            try:
                expected_destination = install_destination(
                    Path(candidate.path),
                    EngineName(record.engine),
                    record.repo_id,
                )
            except (StorageError, ValueError):
                pass
        if (
            candidate is None
            or applied is None
            or record.storage != applied.storage_name
            or candidate.name != applied.storage_name
            or candidate.path != applied.lexical_root
            or candidate.volume_uuid != applied.volume_uuid
            or candidate.scope_id != applied.scope_id
            or record.destination != applied.lexical_destination
            or record.revision != applied.resolved_revision
            or expected_destination is None
            or _lexical_path(expected_destination)
            != _lexical_path(record.destination)
        ):
            raise ProvenanceDataError("registration_storage_binding_changed")

    async def require_cleanup_authority(
        self,
        installation_id: str,
    ) -> tuple[InstallRecord, InstallationProvenance]:
        return await asyncio.to_thread(
            self.store.require_cleanup_authority,
            installation_id,
        )

    async def binding_records(
        self,
        *,
        engine: str,
        storage: str,
    ) -> list[InstallRecord]:
        return await asyncio.to_thread(
            self.store.binding_records,
            engine=engine,
            storage=storage,
        )

    async def mark_trashed(self, installation_id: str) -> InstallRecord:
        return await asyncio.to_thread(self.store.mark_trashed, installation_id)

    async def list(self, *, limit: int = 100) -> list[InstallRecord]:
        return await asyncio.to_thread(self.store.list, limit=limit)

    async def evidence(self, *, limit: int = 100) -> list[dict[str, object]]:
        return await asyncio.to_thread(self.store.evidence, limit=limit)

    async def dismiss(self, install_id: str) -> InstallRecord:
        record = await asyncio.to_thread(self.store.get, install_id)
        if record.status in {"preparing", "queued", "downloading", "registering"}:
            raise ValueError("an active install cannot be removed from history")
        return await asyncio.to_thread(self.store.dismiss, install_id)

    async def latest_for_alias(self, alias: str) -> InstallRecord | None:
        return await asyncio.to_thread(self.store.latest_for_alias, alias)

    def _schedule(self, install_id: str) -> None:
        if install_id in self._tasks and not self._tasks[install_id].done():
            raise ValueError("install is already active")
        task = asyncio.create_task(self._run(install_id), name=f"model-install-{install_id}")
        self._tasks[install_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(install_id, None))

    async def _run(self, install_id: str) -> None:
        process: asyncio.subprocess.Process | None = None
        try:
            async with self._semaphore:
                record = await asyncio.to_thread(self.store.get, install_id)
                if record.status == "registering":
                    await self._register_downloaded(record)
                    return
                location = self._storage_location(record)
                if location is not None:
                    # Rows created before durable creation claims (and current
                    # installs created while that optional authority is
                    # unavailable) still need an immutable install-attempt
                    # binding. Never reconstruct it from post-restart Settings:
                    # that would silently bless a replacement volume or scope.
                    self._registration_storage_bindings[record.id] = (
                        _RegistrationStorageBinding(
                            storage_name=location.name,
                            lexical_root=location.path,
                            volume_uuid=location.volume_uuid,
                            scope_id=location.scope_id,
                            lexical_destination=record.destination,
                            resolved_revision=record.revision,
                        )
                    )
                if location is not None and self.filesystem is not None:
                    destination = await self.filesystem.ensure_directory(
                        root=location.path,
                        path=record.destination,
                        expected_volume_uuid=location.volume_uuid,
                        scope_id=location.scope_id,
                    )
                    if _lexical_path(destination) != _lexical_path(record.destination):
                        raise RuntimeError("model destination changed during validation")
                argv = [
                    sys.executable,
                    "-m",
                    "mnemosyne_macos.download_worker",
                    "--repo-id",
                    record.repo_id,
                    "--destination",
                    record.destination,
                ]
                if record.revision:
                    argv.extend(["--revision", record.revision])
                try:
                    download_files = (
                        json.loads(record.files_json) if record.files_json else []
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    download_files = []
                if isinstance(download_files, list) and download_files:
                    for filename in download_files:
                        if isinstance(filename, str) and filename:
                            argv.extend(["--include-file", filename])
                elif record.filename:
                    argv.extend(["--filename", record.filename])
                if location is not None:
                    argv = wrap_scoped_argv(
                        argv,
                        scope_root=(
                            self.filesystem.scope_root
                            if self.filesystem is not None
                            else None
                        ),
                        scope_id=location.scope_id,
                        scope_path=location.path,
                    )
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                self._processes[install_id] = process
                await asyncio.to_thread(
                    self.store.update,
                    install_id,
                    status="downloading",
                    pid=process.pid,
                    error=None,
                    download_speed_bps=None,
                )
                communicate = asyncio.create_task(process.communicate())
                previous_downloaded = record.bytes_downloaded
                previous_sample_at = time.monotonic()
                smoothed_speed: float | None = None
                while not communicate.done():
                    await asyncio.sleep(1)
                    downloaded = await self._downloaded_size(record, location)
                    sampled_at = time.monotonic()
                    elapsed = max(0.001, sampled_at - previous_sample_at)
                    instantaneous_speed = max(
                        0.0,
                        float(downloaded - previous_downloaded) / elapsed,
                    )
                    smoothed_speed = (
                        instantaneous_speed
                        if smoothed_speed is None
                        else (smoothed_speed * 0.65) + (instantaneous_speed * 0.35)
                    )
                    await asyncio.to_thread(
                        self.store.update,
                        install_id,
                        bytes_downloaded=downloaded,
                        download_speed_bps=smoothed_speed,
                    )
                    previous_downloaded = downloaded
                    previous_sample_at = sampled_at
                stdout, stderr = await communicate
                downloaded = await self._downloaded_size(record, location)
                if process.returncode != 0:
                    message = stderr.decode("utf-8", errors="replace").strip()[-2000:]
                    raise RuntimeError(message or f"download worker exited with {process.returncode}")
                record = await asyncio.to_thread(
                    self.store.update,
                    install_id,
                    status="registering",
                    bytes_downloaded=downloaded,
                    pid=None,
                    error=None,
                    download_speed_bps=None,
                )
                await self._register_downloaded(record)
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                await _terminate(process)
            current = await asyncio.to_thread(self.store.get, install_id)
            if current.status in {"queued", "downloading"}:
                status = "cancelled"
                error = "download cancelled"
            elif current.status == "registering":
                status = "downloaded"
                error = "download completed but profile registration was cancelled"
            else:
                status = current.status
                error = current.error
            location = self._storage_location(current)
            downloaded = await self._downloaded_size(current, location)
            await asyncio.to_thread(
                self.store.update,
                install_id,
                status=status,
                bytes_downloaded=downloaded,
                pid=None,
                error=error,
                download_speed_bps=None,
            )
            raise
        except Exception as exc:
            current = await asyncio.to_thread(self.store.get, install_id)
            location = self._storage_location(current)
            try:
                downloaded = await self._downloaded_size(current, location)
            except Exception:
                downloaded = current.bytes_downloaded
            await asyncio.to_thread(
                self.store.update,
                install_id,
                status="failed",
                bytes_downloaded=downloaded,
                pid=None,
                error=str(exc),
                download_speed_bps=None,
            )
        finally:
            self._processes.pop(install_id, None)

    async def _register_downloaded(self, record: InstallRecord) -> None:
        """Finish profile creation without invoking the download worker again."""

        signed_expected = (
            record.expected_files_json is not None
            or record.expected_manifest_digest is not None
        )
        proof = None
        try:
            proof = await self._capture_exclusive_proof(record)
        except asyncio.CancelledError:
            raise
        except (
            FilesystemProbeError,
            MacInventoryIndexError,
            ProvenanceDataError,
            KeyError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            if signed_expected:
                raise RuntimeError("signed_artifact_verification_failed") from exc
            # Proof capture is deliberately fail-closed for cleanup authority,
            # but ordinary local installation remains available. An unknown
            # install is retained rather than silently becoming deletable.
            proof = None
        if signed_expected:
            if proof is None:
                raise RuntimeError("signed_artifact_verification_failed")
            try:
                await asyncio.to_thread(
                    self.store.record_verified_signed_proof,
                    record.id,
                    proof,
                )
            except (ProvenanceError, TypeError, ValueError) as exc:
                raise RuntimeError("signed_artifact_verification_failed") from exc
        if self.on_installed is not None:
            try:
                await self.on_installed(record)
            except Exception as exc:
                await asyncio.to_thread(
                    self.store.update,
                    record.id,
                    status="downloaded",
                    pid=None,
                    download_speed_bps=None,
                    error=f"download completed but profile registration failed: {exc}",
                )
                return
        installed = await asyncio.to_thread(
            self.store.update,
            record.id,
            status="installed",
            pid=None,
            error=None,
            download_speed_bps=None,
        )
        if proof is not None and not signed_expected:
            try:
                await asyncio.to_thread(
                    self.store.record_exclusive_managed_proof,
                    installed.id,
                    proof,
                )
            except (
                ProvenanceError,
                ValueError,
            ):
                # A completed inference profile never depends on cleanup
                # authority. Retaining the weights is the safe fallback.
                pass
        self._registration_storage_bindings.pop(record.id, None)

    async def _capture_exclusive_proof(
        self,
        record: InstallRecord,
    ) -> ExclusiveManagedProof | None:
        if self.filesystem is None or self.inventory_index is None:
            return None
        claim = await asyncio.to_thread(
            self.store.get_creation_claim,
            record.id,
        )
        if claim is None:
            return None
        location = self._storage_location(record)
        if location is None:
            return None
        binding = await self.inventory_index.resolve_storage(
            claim.storage_location_id,
            claim.storage_binding_generation,
        )
        if (
            binding is None
            or binding.local_key != record.storage
            or binding.local_key != location.name
            or binding.exact_path != claim.storage_lexical_root
            or binding.exact_path != location.path
            or binding.volume_uuid != claim.storage_volume_uuid
            or binding.volume_uuid != location.volume_uuid
            or binding.scope_id != claim.storage_scope_id
            or binding.scope_id != location.scope_id
            or claim.lexical_destination != record.destination
            or claim.resolved_revision != record.revision
        ):
            raise ProvenanceDataError("creation_claim_binding_changed")
        captured = await self.filesystem.capture_managed_manifest(
            root=location.path,
            path=record.destination,
            expected_volume_uuid=location.volume_uuid,
            scope_id=location.scope_id,
            expected_directory_device=claim.directory_device,
            expected_directory_inode=claim.directory_inode,
            timeout_seconds=_manifest_capture_timeout(record),
        )
        if (
            captured.path != record.destination
            or captured.directory_device != claim.directory_device
            or captured.directory_inode != claim.directory_inode
            or captured.total_bytes <= 0
        ):
            raise ProvenanceDataError("captured_manifest_identity_changed")
        expected_payload_paths = _record_download_files(record)
        if not expected_payload_paths and record.filename:
            expected_payload_paths = (record.filename,)
        allowed_metadata = allowed_hf_local_metadata_paths(
            expected_payload_paths
        )
        payload_paths = tuple(
            item.path
            for item in captured.files
            if not is_hf_local_metadata_path(item.path)
        )
        metadata_paths = {
            item.path
            for item in captured.files
            if is_hf_local_metadata_path(item.path)
        }
        if (
            payload_paths != expected_payload_paths
            or MANAGED_CREATION_MARKER_PATH not in metadata_paths
            or not metadata_paths.issubset(allowed_metadata)
        ):
            raise ProvenanceDataError("captured_manifest_ownership_ambiguous")
        return exclusive_proof_from_claim(claim, captured.files)

    def _storage_location(self, record: InstallRecord) -> StorageLocationConfig | None:
        if self.storage is None:
            return None
        location = next(
            (item for item in self.storage.locations if item.name == record.storage),
            None,
        )
        if location is None:
            raise RuntimeError(f"unknown storage location '{record.storage}'")
        return location

    async def _downloaded_size(
        self,
        record: InstallRecord,
        location: StorageLocationConfig | None,
    ) -> int:
        if location is not None and self.filesystem is not None:
            return await self.filesystem.directory_size(
                root=location.path,
                path=record.destination,
                scope_id=location.scope_id,
            )
        return await asyncio.to_thread(_directory_size, Path(record.destination))


async def _terminate(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
        return
    except asyncio.TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await process.wait()


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _directories, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(root) / filename).stat().st_size
            except OSError:
                continue
    return total


def _manifest_capture_timeout(record: InstallRecord) -> float:
    """Bound one full-tree hash while allowing large external-volume models."""

    expected_bytes = max(
        0,
        record.total_bytes or 0,
        record.bytes_downloaded,
    )
    # Budget for roughly 20 MiB/s plus startup and metadata overhead. The
    # helper remains killable on cancellation/shutdown and never blocks the
    # service event loop.
    return min(21_600.0, max(300.0, 120.0 + expected_bytes / (20 * 1024 * 1024)))


def _record_download_files(record: InstallRecord) -> tuple[str, ...]:
    try:
        value = json.loads(record.files_json) if record.files_json else []
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProvenanceDataError("captured_manifest_selection_invalid") from exc
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or len(value) != len(set(value))
    ):
        raise ProvenanceDataError("captured_manifest_selection_invalid")
    return tuple(sorted(value))


def _lexical_path(value: str | Path) -> str:
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.expanduser(str(value))))
    )


__all__ = ["NativeInstaller"]
