"""Failure-isolated control-plane integration for native lifecycle planning.

This layer may inspect local authority, build path-redacted previews, and
prepare the immutable core journal.  It deliberately has no lifecycle
executor: it never stops a service, unregisters a LaunchAgent, removes the
application, mutates configuration, or deletes/moves state or model files.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import UUID, uuid4

from . import __version__
from .config import ModelEngineAlternative, ModelProfile, StorageLocationConfig
from .filesystem import FilesystemProbeError
from .install_provenance import InstallationProvenance
from .mac_inventory_store import MacInventoryIndexError, StorageBinding
from .models import EngineName
from .native_lifecycle import (
    ExclusiveProofState,
    HubRevocationState,
    LifecycleTransaction,
    NativeLifecycleError,
    NativeLifecycleIntegrityError,
    NativeLifecycleJournal,
    NativeLifecycleNotFoundError,
    OutboxDecision,
    PRODUCT_IDENTITY,
    RetentionMode,
    RetentionWeight,
    WeightOwnership,
    build_uninstall_plan,
    helper_authorization_submission_from_mapping,
)
from .native_lifecycle_helper_transport import LifecycleHelperTransport


_MAX_INSTALLATIONS = 10_000
_MAX_INCOMPLETE = 1_024
_FRESH_MANIFEST_TIMEOUT_SECONDS = 60.0
_SHA256 = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


class NativeLifecycleRuntimeError(NativeLifecycleError):
    """Fixed-code integration failure safe for a loopback HTTP response."""


@dataclass(frozen=True, slots=True)
class _ConfiguredReference:
    source_key: str
    alias: str
    engine: EngineName
    model: str
    projector: str | None
    storage: str | None
    ownership: WeightOwnership


class NativeLifecycleManager:
    """Read/preview/prepare facade around :class:`NativeLifecycleJournal`."""

    def __init__(
        self,
        runtime: Any,
        journal: NativeLifecycleJournal | None,
        helper_transport: LifecycleHelperTransport | None = None,
    ) -> None:
        self.runtime = runtime
        self.journal = journal
        self.helper_transport = helper_transport
        self.available = False
        self.error_code: str | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize auxiliary state without ever degrading inference startup."""

        if self.journal is None:
            self.available = False
            self.error_code = "native_lifecycle_journal_unavailable"
            return
        try:
            # The storage index is the only source of opaque location IDs.
            # Reinitialization is idempotent when inventory already started it.
            await self.runtime.mac_inventory_index.initialize()
            await asyncio.to_thread(self.journal.initialize)
            await asyncio.to_thread(self.journal.list_incomplete)
        except NativeLifecycleError as exc:
            self.available = False
            self.error_code = exc.code
        except Exception:
            self.available = False
            self.error_code = "native_lifecycle_journal_unavailable"
        else:
            self.available = True
            self.error_code = None

    async def close(self) -> None:
        self.available = False
        if self.journal is not None:
            try:
                await asyncio.to_thread(self.journal.close)
            except Exception:
                pass

    async def status(self) -> dict[str, object]:
        if not self.available or self.journal is None:
            return {
                "schema_version": 2,
                "available": False,
                "error_code": self.error_code
                or "native_lifecycle_journal_unavailable",
                "execution_available": False,
                "authorization_available": False,
                "authorization_pending_count": 0,
                "migration_preview_available": False,
                "incomplete_count": 0,
                "incomplete": [],
            }
        try:
            rows = await asyncio.to_thread(self.journal.list_incomplete)
            authorization_pending_count = await asyncio.to_thread(
                self.journal.helper_authorization_pending_count
            )
            if len(rows) > _MAX_INCOMPLETE:
                raise NativeLifecycleIntegrityError(
                    "native_lifecycle_journal_corrupt"
                )
        except NativeLifecycleError as exc:
            self.available = False
            self.error_code = exc.code
            return await self.status()
        except Exception:
            self.available = False
            self.error_code = "native_lifecycle_journal_unavailable"
            return await self.status()
        return {
            "schema_version": 2,
            "available": True,
            "error_code": None,
            # Preparation is the hard boundary in this release slice.
            "execution_available": False,
            # Receipt acceptance remains fail-closed until a signed-helper
            # OS-peer proof verifier is provisioned. An unkeyed digest of the
            # public challenge is never treated as owner authorization.
            "authorization_available": (
                self.journal.helper_authorization_available
                and self.helper_transport is not None
            ),
            "authorization_pending_count": authorization_pending_count,
            "migration_preview_available": False,
            "incomplete_count": len(rows),
            "incomplete": [_transaction_public(row) for row in rows],
        }

    async def authorization_status(
        self, transaction_id: str
    ) -> dict[str, object]:
        self._require_available()
        assert self.journal is not None
        status = await asyncio.to_thread(
            self.journal.helper_authorization_status, transaction_id
        )
        if self.helper_transport is None:
            status["can_request"] = False
            if status.get("state") == "ready":
                status["state"] = "unavailable"
        return status

    async def issue_authorization_challenge(
        self, transaction_id: str
    ) -> dict[str, object]:
        self._require_available()
        assert self.journal is not None
        if (
            not self.journal.helper_authorization_available
            or self.helper_transport is None
        ):
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_helper_authority_unavailable"
            )
        async with self._lock:
            mutation = await asyncio.to_thread(
                self.journal.issue_helper_authorization_challenge,
                transaction_id,
            )
        return {
            "schema_version": 2,
            "transaction_id": mutation.challenge.transaction_id,
            "authorization_available": True,
            "execution_available": False,
            "replayed": mutation.replayed,
            "challenge": mutation.challenge.to_public_dict(),
        }

    async def submit_authorization_receipt(
        self,
        transaction_id: str,
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        self._require_available()
        assert self.journal is not None
        submission = helper_authorization_submission_from_mapping(receipt)
        if submission.transaction_id != transaction_id:
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_helper_authority_mismatch"
            )
        async with self._lock:
            accepted = await asyncio.to_thread(
                self.journal.accept_helper_authorization_receipt,
                submission,
            )
        return {
            "schema_version": 2,
            "authorized": True,
            "replayed": accepted.replayed,
            "execution_available": False,
            "transaction": _transaction_public(accepted.transaction),
        }

    async def perform_authorization(
        self, transaction_id: str
    ) -> dict[str, object]:
        """Run the one-shot helper ceremony with the service as direct peer.

        The transport only exchanges the closed challenge and receipt.  A
        separately provisioned proof authority must still validate that
        receipt in the journal.  Production currently supplies no authority,
        so this method fails before launching a helper on normal installs.
        """

        self._require_available()
        assert self.journal is not None
        if (
            not self.journal.helper_authorization_available
            or self.helper_transport is None
        ):
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_helper_authority_unavailable"
            )

        async with self._lock:
            mutation = await asyncio.to_thread(
                self.journal.issue_helper_authorization_challenge,
                transaction_id,
            )
            challenge = mutation.challenge
            try:
                receipt = await self.helper_transport.authorize(challenge)
                submission = helper_authorization_submission_from_mapping(
                    receipt
                )
                if submission.transaction_id != transaction_id:
                    raise NativeLifecycleRuntimeError(
                        "native_lifecycle_helper_authority_mismatch"
                    )
            except asyncio.CancelledError:
                await asyncio.shield(
                    asyncio.to_thread(
                        self.journal.cancel_helper_authorization_challenge,
                        transaction_id=challenge.transaction_id,
                        nonce=challenge.nonce,
                        session_id=challenge.session_id,
                    )
                )
                raise
            except Exception:
                # No submission reached the journal, so invalidating this
                # exact challenge is safe and prevents a late helper receipt
                # from being replayed after a transport failure.
                await asyncio.to_thread(
                    self.journal.cancel_helper_authorization_challenge,
                    transaction_id=challenge.transaction_id,
                    nonce=challenge.nonce,
                    session_id=challenge.session_id,
                )
                raise

            # A failure here can be an ambiguous durable SQLite commit. Never
            # cancel or blindly replay the challenge after submission begins.
            accepted = await asyncio.to_thread(
                self.journal.accept_helper_authorization_receipt,
                submission,
            )
        return {
            "schema_version": 2,
            "authorized": True,
            "replayed": accepted.replayed,
            "execution_available": False,
            "transaction": _transaction_public(accepted.transaction),
        }

    async def cancel_authorization_challenge(
        self,
        *,
        transaction_id: str,
        nonce: str,
        session_id: str,
    ) -> dict[str, object]:
        self._require_available()
        assert self.journal is not None
        async with self._lock:
            cancelled = await asyncio.to_thread(
                self.journal.cancel_helper_authorization_challenge,
                transaction_id=transaction_id,
                nonce=nonce,
                session_id=session_id,
            )
        return {
            "schema_version": 2,
            "transaction_id": cancelled.transaction_id,
            "cancelled": True,
            "replayed": cancelled.replayed,
            "execution_available": False,
        }

    async def preview_uninstall(
        self,
        retention_mode: RetentionMode | str,
        *,
        transaction_id: str | None = None,
    ) -> dict[str, object]:
        self._require_available()
        async with self._lock:
            plan = await self._build_uninstall_plan(
                retention_mode=retention_mode,
                transaction_id=transaction_id or str(uuid4()),
            )
        return {
            "schema_version": 2,
            "preparable": True,
            "execution_available": False,
            "plan": plan.to_public_dict(),
        }

    async def prepare_uninstall(
        self,
        transaction_id: str,
        retention_mode: RetentionMode | str,
    ) -> dict[str, object]:
        self._require_available()
        assert self.journal is not None
        async with self._lock:
            plan = await self._build_uninstall_plan(
                retention_mode=retention_mode,
                transaction_id=transaction_id,
            )
            mutation = await asyncio.to_thread(self.journal.prepare, plan)
        return {
            "schema_version": 2,
            "prepared": True,
            "replayed": mutation.replayed,
            "execution_available": False,
            "transaction": _transaction_public(mutation.transaction),
        }

    async def read(self, transaction_id: str) -> dict[str, object]:
        self._require_available()
        assert self.journal is not None
        try:
            transaction = await asyncio.to_thread(self.journal.get, transaction_id)
        except NativeLifecycleError:
            raise
        except Exception:
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_journal_unavailable"
            ) from None
        if transaction is None:
            raise NativeLifecycleNotFoundError(
                "native_lifecycle_transaction_unknown"
            )
        return _transaction_public(transaction)

    async def preview_migration(self) -> dict[str, object]:
        """Fail closed until a signed candidate and rollback snapshot exist."""

        self._require_available()
        raise NativeLifecycleRuntimeError(
            "native_lifecycle_migration_evidence_unavailable"
        )

    def _require_available(self) -> None:
        if not self.available or self.journal is None:
            raise NativeLifecycleRuntimeError(
                self.error_code or "native_lifecycle_journal_unavailable"
            )

    async def _build_uninstall_plan(
        self,
        *,
        retention_mode: RetentionMode | str,
        transaction_id: str,
    ):
        try:
            mode = RetentionMode(retention_mode)
        except (TypeError, ValueError):
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_retention_mode_invalid"
            ) from None

        try:
            async with asyncio.timeout(120.0):
                usage = await self.runtime.usage.status()
                outbox_count = int(usage.get("outbox_depth") or 0)
                if outbox_count < 0:
                    raise ValueError
                # Every pilot uninstall mode retains the private database and
                # environment.  This keeps node identity, local usage, and
                # undelivered token events recoverable across reinstall.
                outbox_decision = OutboxDecision.PRESERVE_WITH_STATE

                bindings = (
                    await self.runtime.mac_inventory_index.active_storage_bindings()
                )
                if not _bindings_match_config(
                    bindings,
                    self.runtime.config.storage.locations,
                ):
                    raise NativeLifecycleRuntimeError(
                        "native_lifecycle_storage_authority_stale"
                    )
                weights = await self._weight_inventory(mode, bindings)
                runtime_inventory = await self.runtime.runtime_updates.installed_status()
                pairing = await self.runtime.fleet_pairing_status()
                participation = await self.runtime.fleet_participation.status()
        except NativeLifecycleError:
            raise
        except TimeoutError:
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_inventory_timeout"
            ) from None
        except (MacInventoryIndexError, FilesystemProbeError, OSError, ValueError):
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_inventory_unavailable"
            ) from None
        except Exception:
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_inventory_unavailable"
            ) from None

        config_document = self.runtime.config.model_dump(mode="json")
        state_database = Path(self.runtime.config.paths.state_database).expanduser()
        config_path = getattr(self.runtime, "config_path", None)
        env_path = getattr(self.runtime, "env_path", None)
        lifecycle_root = (
            Path(config_path).expanduser().parent / "state" / "native-lifecycle"
            if config_path is not None
            else None
        )
        private_members = [
            _path_identity(config_path),
            _path_identity(env_path),
            _path_identity(state_database),
            _path_identity(Path(f"{state_database}-wal")),
            _path_identity(Path(f"{state_database}-shm")),
        ]
        private_members = [
            item
            for item in private_members
            if item is not None
            and (
                lifecycle_root is None
                or not _lexically_within(
                    str(lifecycle_root), str(item.get("path", ""))
                )
            )
        ]
        outbox_evidence = _digest(
            {
                "state_database": _path_identity(state_database),
                "node_id": usage.get("node_id"),
                "node_id_source": usage.get("node_id_source"),
                "outbox_count": outbox_count,
            }
        )
        return build_uninstall_plan(
            transaction_id=transaction_id,
            retention_mode=mode,
            current_build_digest=_digest(
                {
                    "version": __version__,
                    "application_bundle_id": PRODUCT_IDENTITY.application_bundle_id,
                    "launch_agent_label": PRODUCT_IDENTITY.launch_agent_label,
                    "service_code_requirement_id": (
                        PRODUCT_IDENTITY.service_code_requirement_id
                    ),
                }
            ),
            private_state_fingerprint=_digest(
                {
                    "config": config_document,
                    "members": private_members,
                }
            ),
            runtime_root_fingerprint=_digest(runtime_inventory),
            security_scope_store_fingerprint=_digest(
                {
                    "root": _path_identity(
                        getattr(self.runtime.security_scopes, "root", None)
                    ),
                    "bindings": [
                        {
                            "name": item.name,
                            "path": item.path,
                            "volume_uuid": item.volume_uuid,
                            "scope_id": item.scope_id,
                        }
                        for item in self.runtime.config.storage.locations
                    ],
                }
            ),
            pairing_state_fingerprint=_digest(
                {
                    "pairing": pairing,
                    "participation": participation.to_dict(),
                }
            ),
            token_outbox_count=outbox_count,
            outbox_decision=outbox_decision,
            outbox_evidence_digest=outbox_evidence,
            weights=weights,
            hub_revocation_state=HubRevocationState.NOT_REQUESTED,
        )

    async def _weight_inventory(
        self,
        mode: RetentionMode,
        bindings: Mapping[str, StorageBinding],
    ) -> tuple[RetentionWeight, ...]:
        evidence = await self.runtime.installer.evidence(limit=_MAX_INSTALLATIONS + 1)
        if len(evidence) > _MAX_INSTALLATIONS:
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_weight_limit_exceeded"
            )
        references = _configured_references(self.runtime.config)
        consumed: set[str] = set()
        weights: list[RetentionWeight] = []
        locations = {
            item.name: item for item in self.runtime.config.storage.locations
        }

        for raw in evidence:
            if raw.get("status") == "trashed":
                continue
            install_id = str(raw.get("id") or "")
            destination = str(raw.get("destination") or "")
            storage_name = str(raw.get("storage") or "")
            if not _canonical_uuid(install_id) or not _safe_exact_path(destination):
                raise NativeLifecycleRuntimeError(
                    "native_lifecycle_weight_inventory_invalid"
                )
            provenance = await self._provenance(install_id)
            binding = bindings.get(storage_name)
            authority = _weight_authority(provenance, binding)
            if (
                authority is None
                or not authority.exact_path
                or not _lexically_within(authority.exact_path, destination)
            ):
                raise NativeLifecycleRuntimeError(
                    "native_lifecycle_weight_storage_unbound"
                )
            location = locations.get(storage_name)
            cleanup_eligible = await asyncio.to_thread(
                self.runtime.installer.store.cleanup_eligibility,
                install_id,
            )
            fresh_digest: str | None = None
            if (
                mode is RetentionMode.FULL_EXCLUSIVE_MANAGED
                and cleanup_eligible.eligible
                and provenance is not None
                and binding is not None
                and location is not None
                and _binding_matches(provenance, binding, location)
                and provenance.owned_files is not None
            ):
                try:
                    await self.runtime.filesystem.verify_exact_manifest(
                        root=location.path,
                        path=destination,
                        files=provenance.owned_files,
                        expected_volume_uuid=location.volume_uuid,
                        scope_id=location.scope_id,
                        expected_directory_device=provenance.directory_device,
                        expected_directory_inode=provenance.directory_inode,
                        timeout_seconds=_FRESH_MANIFEST_TIMEOUT_SECONDS,
                    )
                except (FilesystemProbeError, OSError, ValueError):
                    fresh_digest = None
                else:
                    fresh_digest = _digest(provenance.to_dict())

            matching = [
                ref
                for ref in references
                if ref.source_key not in consumed and _matches_install(ref, raw)
            ]
            consumed.update(ref.source_key for ref in matching)
            ownership = (
                WeightOwnership.EXCLUSIVE_MANAGED
                if cleanup_eligible.eligible
                else WeightOwnership.UNKNOWN
            )
            # More than one configured consumer makes the destination shared,
            # regardless of otherwise valid managed provenance.
            if len(matching) > 1:
                ownership = WeightOwnership.SHARED
                fresh_digest = None
            total = raw.get("total_bytes")
            byte_count = (
                total
                if isinstance(total, int) and not isinstance(total, bool) and total >= 0
                else None
            )
            payload = _payload_digest(provenance) or _digest(
                {
                    "repo_id": raw.get("repo_id"),
                    "engine": raw.get("engine"),
                    "revision": raw.get("revision"),
                    "files": raw.get("download_files"),
                }
            )
            weights.append(
                RetentionWeight(
                    asset_fingerprint=_digest(
                        {"source": "managed_install", "installation_id": install_id}
                    ),
                    payload_fingerprint=payload,
                    exact_lexical_path=destination,
                    storage_location_id=authority.storage_location_id,
                    storage_binding_generation=authority.binding_generation,
                    volume_uuid=authority.volume_uuid,
                    scope_id=authority.scope_id,
                    ownership=ownership,
                    installation_id=(
                        install_id
                        if ownership is WeightOwnership.EXCLUSIVE_MANAGED
                        else None
                    ),
                    exclusive_proof_state=(
                        ExclusiveProofState.FRESH_EXACT
                        if fresh_digest is not None
                        else ExclusiveProofState.NOT_PROVEN
                    ),
                    exclusive_proof_digest=fresh_digest,
                    byte_count=byte_count,
                )
            )

        scan_cache: dict[str, tuple[object, list[object]]] = {}
        for reference in references:
            if reference.source_key in consumed:
                continue
            paths = await self._configured_paths(reference, locations, scan_cache)
            for index, path in enumerate(paths):
                binding = _binding_for_path(
                    reference.storage,
                    path,
                    bindings,
                )
                if binding is None:
                    raise NativeLifecycleRuntimeError(
                        "native_lifecycle_weight_storage_unbound"
                    )
                weights.append(
                    RetentionWeight(
                        asset_fingerprint=_digest(
                            {"source": reference.source_key, "part": index}
                        ),
                        payload_fingerprint=_digest(
                            {
                                "engine": reference.engine.value,
                                "model": reference.model,
                                "part": index,
                            }
                        ),
                        exact_lexical_path=path,
                        storage_location_id=binding.storage_location_id,
                        storage_binding_generation=binding.binding_generation,
                        volume_uuid=binding.volume_uuid,
                        scope_id=binding.scope_id,
                        ownership=reference.ownership,
                    )
                )
        return _merge_exact_duplicates(weights)

    async def _provenance(
        self, installation_id: str
    ) -> InstallationProvenance | None:
        try:
            return await asyncio.to_thread(
                self.runtime.installer.store.get_provenance,
                installation_id,
            )
        except Exception:
            return None

    async def _configured_paths(
        self,
        reference: _ConfiguredReference,
        locations: Mapping[str, StorageLocationConfig],
        scan_cache: dict[str, tuple[object, list[object]]],
    ) -> tuple[str, ...]:
        model = reference.model
        if os.path.isabs(model):
            paths = [model]
        elif reference.engine is EngineName.OMLX and reference.storage is not None:
            location = locations.get(reference.storage)
            if location is None:
                raise NativeLifecycleRuntimeError(
                    "native_lifecycle_weight_storage_unbound"
                )
            if reference.storage not in scan_cache:
                scan_cache[reference.storage] = await self.runtime.filesystem.scan(
                    location.path,
                    scope_id=location.scope_id,
                    scope_path=location.path,
                    max_files=100_000,
                    max_models=2_000,
                )
            _status, candidates = scan_cache[reference.storage]
            matches = [
                item.model_path
                for item in candidates
                if item.engine == EngineName.OMLX.value
                and Path(item.model_path).name == model
            ]
            if len(matches) != 1:
                raise NativeLifecycleRuntimeError(
                    "native_lifecycle_weight_inventory_unavailable"
                )
            paths = matches
        elif reference.storage is None:
            # A non-path external engine identifier is not a local weight
            # location and therefore is outside local uninstall authority.
            return ()
        else:
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_weight_inventory_unavailable"
            )
        if reference.projector is not None:
            if not os.path.isabs(reference.projector):
                raise NativeLifecycleRuntimeError(
                    "native_lifecycle_weight_inventory_invalid"
                )
            paths.append(reference.projector)
        if any(not _safe_exact_path(item) for item in paths):
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_weight_inventory_invalid"
            )
        return tuple(dict.fromkeys(paths))


def _configured_references(config: Any) -> tuple[_ConfiguredReference, ...]:
    references: list[_ConfiguredReference] = []
    for profile in config.models:
        references.append(_reference_from_profile(profile))
        references.extend(
            _reference_from_alternative(profile, item)
            for item in profile.alternatives
        )
    # Legacy LM Studio rows are inert hints, not proof that a path exists.
    # Include only exact absolute hints already inside registered storage.
    for index, legacy in enumerate(config.migration.legacy_lmstudio_profiles):
        if os.path.isabs(legacy.model):
            references.append(
                _ConfiguredReference(
                    source_key=f"legacy:{legacy.alias}:{index}",
                    alias=legacy.alias,
                    engine=EngineName.LLAMA_CPP,
                    model=legacy.model,
                    projector=legacy.load.projector_path,
                    storage=None,
                    ownership=WeightOwnership.LM_STUDIO,
                )
            )
    return tuple(references)


def _reference_from_profile(profile: ModelProfile) -> _ConfiguredReference:
    return _ConfiguredReference(
        source_key=f"profile:{profile.alias}:primary",
        alias=profile.alias,
        engine=profile.engine,
        model=profile.model,
        projector=profile.load.projector_path,
        storage=profile.storage,
        ownership=(
            WeightOwnership.IMPORTED
            if profile.storage is not None
            else WeightOwnership.EXTERNAL
        ),
    )


def _reference_from_alternative(
    profile: ModelProfile, alternative: ModelEngineAlternative
) -> _ConfiguredReference:
    return _ConfiguredReference(
        source_key=f"profile:{profile.alias}:alternative:{alternative.engine.value}",
        alias=profile.alias,
        engine=alternative.engine,
        model=alternative.model,
        projector=alternative.load.projector_path,
        storage=alternative.storage,
        ownership=(
            WeightOwnership.IMPORTED
            if alternative.storage is not None
            else WeightOwnership.EXTERNAL
        ),
    )


def _matches_install(reference: _ConfiguredReference, raw: Mapping[str, object]) -> bool:
    if (
        reference.alias != raw.get("alias")
        or reference.engine.value != raw.get("engine")
        or reference.storage != raw.get("storage")
    ):
        return False
    destination = str(raw.get("destination") or "")
    filename = raw.get("filename")
    if reference.engine in {EngineName.LLAMA_CPP, EngineName.DS4}:
        if not isinstance(filename, str) or not filename:
            return False
        if _lexical(reference.model) != _lexical(os.path.join(destination, filename)):
            return False
        selected = raw.get("projector_filename")
        if selected is None:
            return reference.projector is None
        return bool(
            isinstance(selected, str)
            and reference.projector is not None
            and _lexical(reference.projector)
            == _lexical(os.path.join(destination, selected))
        )
    if reference.engine is EngineName.OMLX:
        return reference.model == Path(destination).name
    return _lexical(reference.model) == _lexical(destination)


def _weight_authority(
    provenance: InstallationProvenance | None,
    binding: StorageBinding | None,
) -> StorageBinding | None:
    if (
        provenance is not None
        and provenance.storage_location_id is not None
        and provenance.storage_binding_generation is not None
        and _canonical_uuid(provenance.storage_location_id)
    ):
        return StorageBinding(
            local_key=binding.local_key if binding is not None else "retired",
            storage_location_id=provenance.storage_location_id,
            binding_generation=provenance.storage_binding_generation,
            exact_path=provenance.storage_lexical_root or "",
            volume_uuid=provenance.storage_volume_uuid,
            scope_id=provenance.storage_scope_id,
        )
    return binding


def _binding_matches(
    provenance: InstallationProvenance,
    binding: StorageBinding,
    location: StorageLocationConfig,
) -> bool:
    return bool(
        provenance.storage_location_id == binding.storage_location_id
        and provenance.storage_binding_generation == binding.binding_generation
        and provenance.storage_lexical_root == binding.exact_path == location.path
        and provenance.storage_volume_uuid == binding.volume_uuid == location.volume_uuid
        and provenance.storage_scope_id == binding.scope_id == location.scope_id
        and binding.local_key == location.name
    )


def _bindings_match_config(
    bindings: Mapping[str, StorageBinding],
    locations: list[StorageLocationConfig],
) -> bool:
    configured = {item.name: item for item in locations}
    if set(bindings) != set(configured):
        return False
    return all(
        binding.local_key == name
        and binding.exact_path == configured[name].path
        and binding.volume_uuid == configured[name].volume_uuid
        and binding.scope_id == configured[name].scope_id
        for name, binding in bindings.items()
    )


def _binding_for_path(
    storage: str | None,
    path: str,
    bindings: Mapping[str, StorageBinding],
) -> StorageBinding | None:
    if storage is not None:
        binding = bindings.get(storage)
        return binding if binding is not None and _lexically_within(binding.exact_path, path) else None
    matches = [
        item for item in bindings.values() if _lexically_within(item.exact_path, path)
    ]
    return max(matches, key=lambda item: len(item.exact_path)) if matches else None


def _merge_exact_duplicates(weights: list[RetentionWeight]) -> tuple[RetentionWeight, ...]:
    grouped: dict[str, list[RetentionWeight]] = {}
    for item in weights:
        grouped.setdefault(item.exact_lexical_path, []).append(item)
    result: list[RetentionWeight] = []
    for path, items in grouped.items():
        first = items[0]
        if len(items) == 1:
            result.append(first)
            continue
        authority = {
            (
                item.storage_location_id,
                item.storage_binding_generation,
                item.volume_uuid,
                item.scope_id,
            )
            for item in items
        }
        if len(authority) != 1:
            raise NativeLifecycleRuntimeError(
                "native_lifecycle_weight_inventory_conflict"
            )
        result.append(
            RetentionWeight(
                asset_fingerprint=_digest(
                    sorted(item.asset_fingerprint for item in items)
                ),
                payload_fingerprint=_digest(
                    sorted(item.payload_fingerprint for item in items)
                ),
                exact_lexical_path=path,
                storage_location_id=first.storage_location_id,
                storage_binding_generation=first.storage_binding_generation,
                volume_uuid=first.volume_uuid,
                scope_id=first.scope_id,
                ownership=WeightOwnership.SHARED,
                byte_count=max(
                    (item.byte_count for item in items if item.byte_count is not None),
                    default=None,
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: item.asset_fingerprint))


def _transaction_public(transaction: LifecycleTransaction) -> dict[str, object]:
    return {
        "schema_version": 2,
        "contract_version": transaction.contract_version,
        "transaction_id": transaction.transaction_id,
        "kind": transaction.kind.value,
        "phase": transaction.phase.value,
        "terminal": transaction.terminal,
        "needs_recovery": transaction.needs_recovery,
        "created_at": transaction.created_at,
        "updated_at": transaction.updated_at,
        "error_code": transaction.error_code,
        "plan": transaction.plan.to_public_dict(),
    }


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _payload_digest(provenance: InstallationProvenance | None) -> str | None:
    if provenance is None or provenance.manifest_digest is None:
        return None
    match = _SHA256.fullmatch(provenance.manifest_digest)
    return match.group(1) if match is not None else None


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value.casefold() else None


def _safe_exact_path(value: str) -> bool:
    return bool(
        value
        and os.path.isabs(value)
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
        and os.path.normpath(value) == value
        and value != "/"
    )


def _lexical(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(value))))


def _lexically_within(root: str, path: str) -> bool:
    try:
        return os.path.commonpath([_lexical(root), _lexical(path)]) == _lexical(root)
    except ValueError:
        return False


def _path_identity(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    result: dict[str, object] = {"path": str(path)}
    try:
        status = path.lstat()
    except FileNotFoundError:
        result["exists"] = False
    except OSError:
        result["exists"] = None
    else:
        result.update(
            {
                "exists": True,
                "device": status.st_dev,
                "inode": status.st_ino,
                "mode": status.st_mode,
                "size": status.st_size,
                "mtime_ns": status.st_mtime_ns,
            }
        )
    return result


__all__ = ["NativeLifecycleManager", "NativeLifecycleRuntimeError"]
