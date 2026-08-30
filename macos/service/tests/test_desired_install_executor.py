from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from mnemosyne_macos.compatibility_catalog import (
    artifact_manifest_digest,
    catalog_digest,
)
from mnemosyne_macos.desired_install_executor import (
    DesiredInstallExecutor,
    DesiredInstallExecutorError,
    InventoryAuthority,
    LocalStorageAuthority,
    PairingAuthority,
    RuntimeAuthority,
)
from mnemosyne_macos.desired_install_store import DesiredInstallStore
from mnemosyne_macos.install_launch import (
    LlamaCppInstallLaunch,
    OMLXInstallLaunch,
    install_launch_json,
)
from mnemosyne_macos.install_provenance import ArtifactAuthority, OwnedFile


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_PATH = (
    REPOSITORY_ROOT / "mac_pool_protocol" / "v1" / "desired_install.example.json"
)
FINGERPRINT = "sha256:" + "c" * 64
FILE_DIGEST = "sha256:" + "d" * 64


class Clock:
    wall = 1_785_528_051.0
    monotonic = 100.0
    boot = "executor-boot"


class CatalogProvider:
    def __init__(self, snapshot: Any) -> None:
        self.value = snapshot
        self.calls = 0

    async def snapshot(self) -> Any:
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class PairingProvider:
    def __init__(self, value: PairingAuthority) -> None:
        self.value = value

    async def current_pairing(self) -> PairingAuthority:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class InventoryProvider:
    def __init__(self, value: InventoryAuthority) -> None:
        self.value = value

    async def current_inventory(self) -> InventoryAuthority:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class StorageProvider:
    def __init__(self, value: LocalStorageAuthority | None) -> None:
        self.value = value
        self.calls: list[tuple[str, int]] = []

    async def resolve_storage(
        self, storage_location_id: str, binding_generation: int
    ) -> LocalStorageAuthority | None:
        self.calls.append((storage_location_id, binding_generation))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class RuntimeProvider:
    def __init__(self, value: RuntimeAuthority | None) -> None:
        self.value = value
        self.calls: list[tuple[str, str]] = []

    async def current_runtime(
        self, engine: str, catalog_digest: str
    ) -> RuntimeAuthority | None:
        self.calls.append((engine, catalog_digest))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


@dataclass
class InstallObservation:
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


class Installer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.records: dict[str, InstallObservation] = {}
        self.cancelled: list[str] = []
        self.retried: list[str] = []
        self.error: Exception | None = None
        self.error_after_record: Exception | None = None
        self.mutate_observation: Callable[[InstallObservation], InstallObservation] | None = None
        self.proof_factory: Callable[[InstallObservation, dict[str, Any]], Any] | None = None
        self.proofs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> InstallObservation:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        record = InstallObservation(
            id=kwargs["installation_id"],
            repo_id=kwargs["repo_id"],
            engine=kwargs["engine"],
            storage=kwargs["storage"],
            alias=kwargs["alias"],
            destination=kwargs["destination"],
            status="queued",
            revision=kwargs["revision"],
            filename=kwargs["filename"],
            projector_filename=kwargs["projector_filename"],
            context_length=kwargs["context_length"],
            files_json=json.dumps(
                list(kwargs["download_files"]), separators=(",", ":")
            ),
            expected_files_json=json.dumps(
                [
                    {
                        "path": item.path,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                    }
                    for item in kwargs["expected_files"]
                ],
                sort_keys=True,
                separators=(",", ":"),
            ),
            expected_manifest_digest=kwargs["expected_manifest_digest"],
            capabilities_json=json.dumps(
                list(kwargs["capabilities"]), separators=(",", ":")
            ),
            family=kwargs["family"],
            launch_json=install_launch_json(
                kwargs["engine"], kwargs["launch_contract"]
            ),
            bytes_downloaded=0,
            total_bytes=kwargs["total_bytes"],
        )
        if self.mutate_observation is not None:
            record = self.mutate_observation(record)
        self.records[record.id] = record
        if self.proof_factory is not None:
            self.proofs[record.id] = self.proof_factory(record, kwargs)
        if self.error_after_record is not None:
            raise self.error_after_record
        return record

    async def get_by_id(self, installation_id: str) -> InstallObservation:
        return self.records[installation_id]

    async def cancel(self, install_id: str) -> InstallObservation:
        self.cancelled.append(install_id)
        record = self.records[install_id]
        record = replace(record, status="cancelled")
        self.records[install_id] = record
        return record

    async def retry(self, install_id: str) -> InstallObservation:
        self.retried.append(install_id)
        record = self.records[install_id]
        if record.status in {"preparing", "partial"}:
            status = "queued"
        elif record.status == "downloaded":
            status = "registering"
        else:
            raise ValueError("not retryable")
        record = replace(record, status=status)
        self.records[install_id] = record
        return record

    async def require_cleanup_authority(
        self, installation_id: str
    ) -> tuple[InstallObservation, Any]:
        return self.records[installation_id], self.proofs[installation_id]


@dataclass
class Harness:
    store: DesiredInstallStore
    job: dict[str, Any]
    snapshot: Any
    catalog: CatalogProvider
    pairing: PairingProvider
    inventory: InventoryProvider
    storage: StorageProvider
    runtime: RuntimeProvider
    installer: Installer
    executor: DesiredInstallExecutor


def _catalog(*, engine: str = "llama.cpp") -> dict[str, Any]:
    file = {
        "path": "weights/model.gguf" if engine != "omlx" else "model.safetensors",
        "size_bytes": 100,
        "sha256": FILE_DIGEST,
    }
    artifact_format = {
        "llama.cpp": "gguf",
        "omlx": "mlx",
        "ds4": "ds4-weights",
    }[engine]
    launch: dict[str, Any]
    if engine == "llama.cpp":
        launch = {
            "engine": engine,
            "parallel_slots": 2,
            "gpu_offload": "all",
            "flash_attention": "automatic",
        }
    elif engine == "omlx":
        launch = {
            "engine": engine,
            "scheduler_slots": 2,
            "memory_guard": "required",
        }
    else:
        launch = {
            "engine": engine,
            "batched_sessions": 1,
            "execution_mode": "single-node",
        }
    capabilities = [
        "chat/completions",
        "completions",
        "messages",
        "responses",
    ]
    artifact = {
        "artifact_id": "artifact:test",
        "logical_model_id": "model:test",
        "format": artifact_format,
        "quantization": "q4",
        "source": {
            "kind": "huggingface",
            "transport": "https",
            "registry": "huggingface.co",
            "repository_id": "acme/model",
            "revision": "a" * 40,
        },
        "files": [file],
        "manifest_digest": artifact_manifest_digest([file]),
        "total_size_bytes": 100,
    }
    return {
        "catalog_id": "mnemosyne-apple-silicon",
        "catalog_version": "2026.08.1",
        "catalog_sequence": 1,
        "issued_at": 1_785_000_000,
        "expires_at": 1_790_000_000,
        "publisher": "mnemosyne",
        "build_revision": "b" * 40,
        "logical_models": [
            {
                "logical_model_id": "model:test",
                "display_name": "Test Model",
                "family": "test-family",
                "kind": "generation",
                "capabilities": capabilities,
                "declared_max_context_tokens": 8192,
            }
        ],
        "artifacts": [artifact],
        "recipes": [
            {
                "recipe_id": "recipe:test",
                "logical_model_id": "model:test",
                "artifact_id": "artifact:test",
                "engine": engine,
                "capabilities": capabilities,
                "compatibility_tier": "verified",
                "context": {
                    "guaranteed_tokens": 8192,
                    "native_max_tokens": 8192,
                    "evidence": {
                        "evidence_class": "tested",
                        "source_revision": "c" * 40,
                        "observed_at": 1_785_000_000,
                    },
                },
                "memory": {
                    "weights_bytes": 100,
                    "runtime_overhead_bytes": 100,
                    "kv_bytes_per_token_per_slot": 1,
                    "safety_headroom_bytes": 100,
                    "estimate_class": "tested",
                    "evidence": {
                        "evidence_class": "tested",
                        "source_revision": "c" * 40,
                        "observed_at": 1_785_000_000,
                    },
                },
                "hardware": {
                    "minimum_memory_bytes": 1024,
                    "recommended_memory_bytes": 2048,
                    "soc_families": ["apple-m3"],
                    "minimum_performance_cores": 1,
                    "minimum_gpu_cores": 1,
                    "required_features": [],
                    "minimum_macos": {"major": 15, "minor": 0},
                    "maximum_macos_exclusive": None,
                },
                "runtime": {
                    "engine": engine,
                    "release_tier": "stable" if engine != "ds4" else "preview",
                    "minimum_version": "b5000",
                    "maximum_version_exclusive": "b7000",
                    "known_bad_versions": ["b5500"],
                    "allowed_runtime_fingerprints": [FINGERPRINT],
                    "known_bad_runtime_fingerprints": ["sha256:" + "e" * 64],
                    "required_features": ["metal"],
                },
                "launch": launch,
                "evidence": {
                    "evidence_class": "tested",
                    "source_revision": "c" * 40,
                    "observed_at": 1_785_000_000,
                },
            }
        ],
    }


def _snapshot(catalog: dict[str, Any], *, source: str = "signed") -> Any:
    digest = catalog_digest(catalog)
    return SimpleNamespace(
        source=source,
        catalog_id=catalog["catalog_id"],
        catalog_version=catalog["catalog_version"],
        catalog_digest=digest,
        catalog=lambda: copy.deepcopy(catalog),
    )


def _job(snapshot: Any, *, engine: str = "llama.cpp") -> dict[str, Any]:
    value = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    value.update(
        {
            "catalog_version": snapshot.catalog_version,
            "catalog_digest": snapshot.catalog_digest,
            "logical_model_id": "model:test",
            "recipe_id": "recipe:test",
            "artifact_id": "artifact:test",
            "engine": engine,
            "guaranteed_context_tokens": 8192,
            "alias": "test-model",
        }
    )
    return value


async def _harness(tmp_path: Path, *, engine: str = "llama.cpp") -> Harness:
    raw_catalog = _catalog(engine=engine)
    snapshot = _snapshot(raw_catalog)
    job = _job(snapshot, engine=engine)
    clock = Clock()
    store = DesiredInstallStore(
        tmp_path / "desired.sqlite3",
        wall_clock=lambda: clock.wall,
        monotonic_clock=lambda: clock.monotonic,
        boot_identity=lambda: clock.boot,
    )
    await store.initialize()
    await store.receive(job)
    catalog = CatalogProvider(snapshot)
    pairing = PairingProvider(
        PairingAuthority(
            active=True,
            pairing_id=job["pairing_id"],
            credential_generation=job["credential_generation"],
        )
    )
    inventory = InventoryProvider(
        InventoryAuthority(
            inventory_instance_id=job["recommendation_basis"][
                "inventory_instance_id"
            ],
            inventory_sequence=job["recommendation_basis"]["inventory_sequence"],
        )
    )
    storage = StorageProvider(
        LocalStorageAuthority(
            storage_location_id=job["storage_location_id"],
            binding_generation=job["storage_binding_generation"],
            local_storage_name="external-models",
            indexed_lexical_root="/Volumes/Models Link/nested/models",
            indexed_volume_uuid="volume-a",
            indexed_scope_id="f" * 64,
            configured_lexical_root="/Volumes/Models Link/nested/models",
            configured_volume_uuid="volume-a",
            configured_scope_id="f" * 64,
            observed_lexical_root="/Volumes/Models Link/nested/models",
            observed_volume_uuid="VOLUME-A",
            availability="available",
            exists=True,
            is_directory=True,
            writable=True,
            volume_matches=True,
            free_bytes=10_000,
            reserve_bytes=1_000,
            remote_installs_allowed=True,
        )
    )
    runtime = RuntimeProvider(
        RuntimeAuthority(
            engine=engine,
            enabled=True,
            healthy=True,
            release_tier="stable" if engine != "ds4" else "preview",
            version="b6000",
            runtime_fingerprint=FINGERPRINT,
            features=("metal",),
            catalog_digest=snapshot.catalog_digest,
            omlx_scheduler_slots=2 if engine == "omlx" else None,
            omlx_memory_guard_enabled=True if engine == "omlx" else None,
        )
    )
    installer = Installer()
    installer.proof_factory = lambda observation, kwargs: SimpleNamespace(
        installation_id=observation.id,
        source_kind="managed_download",
        ownership_class="exclusive_managed",
        artifact_authority=ArtifactAuthority.SIGNED_CATALOG,
        provenance_revision=2,
        storage_location_id=job["storage_location_id"],
        storage_binding_generation=job["storage_binding_generation"],
        storage_lexical_root=storage.value.configured_lexical_root,
        storage_volume_uuid=storage.value.configured_volume_uuid,
        storage_scope_id=storage.value.configured_scope_id,
        lexical_destination=observation.destination,
        destination_binding_digest="sha256:" + "4" * 64,
        catalog_id=kwargs["catalog_id"],
        logical_model_id=kwargs["logical_model_id"],
        artifact_id=kwargs["artifact_id"],
        recipe_id=kwargs["recipe_id"],
        resolved_revision=kwargs["revision"],
        catalog_digest=kwargs["catalog_digest"],
        source_identity_digest=kwargs["source_identity_digest"],
        manifest_digest=raw_catalog["artifacts"][0]["manifest_digest"],
        owned_files=tuple(
            SimpleNamespace(
                path=item["path"],
                size_bytes=item["size_bytes"],
                sha256=item["sha256"],
            )
            for item in raw_catalog["artifacts"][0]["files"]
        ),
        destination_state_before="absent",
        destination_created_by_transaction=True,
        preexisting_entries=(),
        extra_entries=(),
        creation_transaction_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        directory_device=1,
        directory_inode=2,
    )
    executor = DesiredInstallExecutor(
        store=store,
        catalog=catalog,
        pairing=pairing,
        inventory=inventory,
        storage=storage,
        runtimes=runtime,
        installer=installer,
        installation_id_factory=(
            lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        ),
    )
    return Harness(
        store=store,
        job=job,
        snapshot=snapshot,
        catalog=catalog,
        pairing=pairing,
        inventory=inventory,
        storage=storage,
        runtime=runtime,
        installer=installer,
        executor=executor,
    )


@pytest.mark.asyncio
async def test_approval_maps_only_signed_and_local_authority_to_installer(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)

    record = await harness.executor.approve(harness.job["job_id"])

    assert record.state == "downloading"
    assert record.result_code is None
    assert record.installation_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert record.total_bytes == 100
    assert len(harness.installer.calls) == 1
    call = harness.installer.calls[0]
    assert call == {
        "installation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "repo_id": "acme/model",
        "engine": "llama.cpp",
        "storage": "external-models",
        "alias": "test-model",
        "destination": (
            "/Volumes/Models Link/nested/models/llama.cpp/acme/model"
        ),
        "revision": "a" * 40,
        "filename": "weights/model.gguf",
        "projector_filename": None,
        "context_length": 8192,
        "download_files": ("weights/model.gguf",),
        "expected_files": (
            OwnedFile(
                path="weights/model.gguf",
                size_bytes=100,
                sha256=FILE_DIGEST,
            ),
        ),
        "expected_manifest_digest": harness.snapshot.catalog()["artifacts"][0][
            "manifest_digest"
        ],
        "capabilities": (
            "chat/completions",
            "completions",
            "messages",
            "responses",
        ),
            "family": "test-family",
            "total_bytes": 100,
            "launch_contract": LlamaCppInstallLaunch(
                engine="llama.cpp",
                parallel_slots=2,
                gpu_offload="all",
                flash_attention="automatic",
            ),
            "artifact_authority": ArtifactAuthority.SIGNED_CATALOG,
        "source_identity_digest": call["source_identity_digest"],
        "catalog_id": "mnemosyne-apple-silicon",
        "logical_model_id": "model:test",
        "artifact_id": "artifact:test",
        "recipe_id": "recipe:test",
        "catalog_digest": harness.snapshot.catalog_digest,
        "require_exclusive_proof": True,
    }
    assert call["source_identity_digest"].startswith("sha256:")
    assert len(call["source_identity_digest"]) == 71
    assert harness.storage.calls == [
        (harness.job["storage_location_id"], 2),
        (harness.job["storage_location_id"], 2),
    ]
    assert harness.runtime.calls == [
        ("llama.cpp", harness.snapshot.catalog_digest),
        ("llama.cpp", harness.snapshot.catalog_digest),
    ]
    assert harness.catalog.calls == 2
    assert not hasattr(harness.executor, "load")


@pytest.mark.asyncio
async def test_installed_reconciliation_walks_every_state_and_stays_cold(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    observation = harness.installer.records[started.installation_id]
    harness.installer.records[started.installation_id] = replace(
        observation,
        status="installed",
        bytes_downloaded=100,
    )

    completed = await harness.executor.reconcile(harness.job["job_id"])

    assert completed.state == "completed"
    assert completed.bytes_downloaded == 100
    assert completed.installation_id == started.installation_id
    assert harness.installer.cancelled == []


@pytest.mark.asyncio
async def test_progress_is_monotonic_and_downloaded_resumes_registration(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    initial = harness.installer.records[started.installation_id]
    harness.installer.records[started.installation_id] = replace(
        initial, status="registering", bytes_downloaded=60
    )
    verifying = await harness.executor.reconcile(harness.job["job_id"])
    assert verifying.state == "verifying"
    assert verifying.bytes_downloaded == 60

    harness.installer.records[started.installation_id] = replace(
        initial, status="downloaded", bytes_downloaded=50
    )
    downloaded = await harness.executor.reconcile(harness.job["job_id"])
    assert downloaded.state == "verifying"
    assert downloaded.bytes_downloaded == 60
    assert harness.installer.retried == [started.installation_id]


@pytest.mark.asyncio
async def test_preparing_creation_intent_resumes_same_installation_id(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    initial = harness.installer.records[started.installation_id]
    harness.installer.records[started.installation_id] = replace(
        initial,
        status="preparing",
    )

    resumed = await harness.executor.reconcile(harness.job["job_id"])

    assert resumed.state == "downloading"
    assert resumed.installation_id == started.installation_id
    assert harness.installer.retried == [started.installation_id]


@pytest.mark.asyncio
async def test_hub_and_local_cancellation_are_stop_only(tmp_path: Path) -> None:
    hub = await _harness(tmp_path / "hub")
    started = await hub.executor.approve(hub.job["job_id"])
    cancel = copy.deepcopy(hub.job)
    cancel.update(
        {
            "job_revision": 2,
            "desired_state": "cancel",
            "created_at": hub.job["created_at"] + 100,
            "expires_at": hub.job["created_at"] + 400,
            "valid_for_seconds": 300,
        }
    )
    await hub.store.receive(cancel)
    result = await hub.executor.reconcile(hub.job["job_id"])
    assert result.state == "cancelled"
    assert result.result_code == "cancelled_by_hub"
    assert hub.installer.cancelled == [started.installation_id]

    local = await _harness(tmp_path / "local")
    locally_started = await local.executor.approve(local.job["job_id"])
    local_result = await local.executor.cancel_locally(local.job["job_id"])
    assert local_result.state == "cancelled"
    assert local_result.result_code == "cancelled_locally"
    assert local.installer.cancelled == [locally_started.installation_id]


@pytest.mark.asyncio
async def test_terminal_cancellation_retries_until_installer_stop_is_proved(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    observation = harness.installer.records.pop(started.installation_id)
    cancel = copy.deepcopy(harness.job)
    cancel.update(
        {
            "job_revision": 2,
            "desired_state": "cancel",
            "created_at": Clock.wall,
            "expires_at": Clock.wall + 300,
            "valid_for_seconds": 300,
        }
    )
    await harness.store.receive(cancel)

    with pytest.raises(DesiredInstallExecutorError) as ambiguous:
        await harness.executor.reconcile(harness.job["job_id"])
    assert ambiguous.value.code == "desired_install_internal_error"
    assert harness.installer.cancelled == []

    harness.installer.records[started.installation_id] = observation
    settled = await harness.executor.reconcile(harness.job["job_id"])
    assert settled.state == "cancelled"
    assert harness.installer.cancelled == [started.installation_id]

    replay = await harness.executor.reconcile(harness.job["job_id"])
    assert replay == settled
    assert harness.installer.cancelled == [started.installation_id]


@pytest.mark.asyncio
async def test_approval_never_replays_or_duplicates_nonwaiting_job(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    await harness.executor.approve(harness.job["job_id"])
    with pytest.raises(DesiredInstallExecutorError) as error:
        await harness.executor.approve(harness.job["job_id"])
    assert error.value.code == "desired_install_not_awaiting_local_approval"
    assert len(harness.installer.calls) == 1


AuthorityMutation = Callable[[Harness], None]


def _pairing_inactive(h: Harness) -> None:
    h.pairing.value = replace(h.pairing.value, active=False)


def _pairing_id(h: Harness) -> None:
    h.pairing.value = replace(
        h.pairing.value, pairing_id="99999999-9999-4999-8999-999999999999"
    )


def _pairing_generation(h: Harness) -> None:
    h.pairing.value = replace(h.pairing.value, credential_generation=4)


def _inventory_instance(h: Harness) -> None:
    h.inventory.value = replace(
        h.inventory.value,
        inventory_instance_id="99999999-9999-4999-8999-999999999999",
    )


def _inventory_sequence_behind_basis(h: Harness) -> None:
    h.inventory.value = replace(h.inventory.value, inventory_sequence=41)


def _catalog_source(h: Harness) -> None:
    h.snapshot.source = "built_in"


def _catalog_version(h: Harness) -> None:
    h.snapshot.catalog_version = "2026.08.2"


def _catalog_digest(h: Harness) -> None:
    h.snapshot.catalog_digest = "sha256:" + "9" * 64


def _model_missing(h: Harness) -> None:
    catalog = h.snapshot.catalog()
    catalog["logical_models"] = []
    h.catalog.value = _snapshot(catalog)
    # Keep the advertised document identity exact so this reaches the
    # reference fence rather than the earlier catalog-changed fence.
    h.catalog.value.catalog_digest = h.job["catalog_digest"]


def _recipe_missing(h: Harness) -> None:
    catalog = h.snapshot.catalog()
    catalog["recipes"] = []
    h.catalog.value = _snapshot(catalog)
    h.catalog.value.catalog_digest = h.job["catalog_digest"]


def _artifact_missing(h: Harness) -> None:
    catalog = h.snapshot.catalog()
    catalog["artifacts"] = []
    h.catalog.value = _snapshot(catalog)
    h.catalog.value.catalog_digest = h.job["catalog_digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "result_code"),
    [
        (_pairing_inactive, "pairing_generation_changed"),
        (_pairing_id, "pairing_generation_changed"),
        (_pairing_generation, "pairing_generation_changed"),
        (_inventory_instance, "inventory_basis_stale"),
        (_inventory_sequence_behind_basis, "inventory_basis_stale"),
        (_catalog_source, "catalog_changed"),
        (_catalog_version, "catalog_changed"),
        (_catalog_digest, "catalog_changed"),
        (_model_missing, "catalog_changed"),
        (_recipe_missing, "catalog_changed"),
        (_artifact_missing, "catalog_changed"),
    ],
)
async def test_authority_fences_prevent_installer_creation(
    tmp_path: Path,
    mutate: AuthorityMutation,
    result_code: str,
) -> None:
    harness = await _harness(tmp_path)
    mutate(harness)
    result = await harness.executor.approve(harness.job["job_id"])
    assert result.state == "refused"
    assert result.result_code == result_code
    assert harness.installer.calls == []


@pytest.mark.asyncio
async def test_newer_current_inventory_sequence_keeps_received_basis_eligible(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    harness.inventory.value = replace(
        harness.inventory.value,
        inventory_sequence=harness.job["recommendation_basis"][
            "inventory_sequence"
        ]
        + 1,
    )
    result = await harness.executor.approve(harness.job["job_id"])
    assert result.state == "downloading"
    assert len(harness.installer.calls) == 1


async def _catalog_harness(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    *,
    engine: str = "llama.cpp",
) -> Harness:
    value = _catalog(engine=engine)
    mutation(value)
    snapshot = _snapshot(value)
    job = _job(snapshot, engine=engine)
    clock = Clock()
    store = DesiredInstallStore(
        tmp_path / "desired.sqlite3",
        wall_clock=lambda: clock.wall,
        monotonic_clock=lambda: clock.monotonic,
        boot_identity=lambda: clock.boot,
    )
    await store.initialize()
    await store.receive(job)
    base = await _harness(tmp_path / "providers", engine=engine)
    await base.store.close()
    base.store = store
    base.job = job
    base.snapshot = snapshot
    base.catalog = CatalogProvider(snapshot)
    base.pairing.value = PairingAuthority(
        True, job["pairing_id"], job["credential_generation"]
    )
    base.inventory.value = InventoryAuthority(
        job["recommendation_basis"]["inventory_instance_id"],
        job["recommendation_basis"]["inventory_sequence"],
    )
    base.storage.value = replace(
        base.storage.value,
        storage_location_id=job["storage_location_id"],
        binding_generation=job["storage_binding_generation"],
    )
    base.runtime.value = replace(
        base.runtime.value,
        catalog_digest=snapshot.catalog_digest,
    )
    base.executor = DesiredInstallExecutor(
        store=store,
        catalog=base.catalog,
        pairing=base.pairing,
        inventory=base.inventory,
        storage=base.storage,
        runtimes=base.runtime,
        installer=base.installer,
        installation_id_factory=(
            lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        ),
    )
    return base


def _recipe_engine(value: dict[str, Any]) -> None:
    value["recipes"][0]["engine"] = "ds4"


def _recipe_capabilities(value: dict[str, Any]) -> None:
    value["recipes"][0]["capabilities"] = ["responses"]


def _recipe_context(value: dict[str, Any]) -> None:
    value["recipes"][0]["context"]["guaranteed_tokens"] = 4096


def _artifact_reference(value: dict[str, Any]) -> None:
    value["artifacts"][0]["logical_model_id"] = "model:other"


def _artifact_manifest(value: dict[str, Any]) -> None:
    value["artifacts"][0]["manifest_digest"] = "sha256:" + "1" * 64


def _artifact_total(value: dict[str, Any]) -> None:
    value["artifacts"][0]["total_size_bytes"] = 101


def _artifact_uses_hf_local_metadata_namespace(value: dict[str, Any]) -> None:
    artifact = value["artifacts"][0]
    artifact["files"][0]["path"] = ".cache/huggingface/model.gguf"
    artifact["manifest_digest"] = artifact_manifest_digest(artifact["files"])


def _unsupported_source(value: dict[str, Any]) -> None:
    value["artifacts"][0]["source"] = {
        "kind": "github_release",
        "transport": "https",
        "registry": "github.com",
        "repository_id": "acme/model",
        "revision": "a" * 40,
        "release_id": 1,
    }


@pytest.mark.asyncio
async def test_catalog_payload_cannot_claim_hf_local_metadata_namespace(
    tmp_path: Path,
) -> None:
    harness = await _catalog_harness(
        tmp_path,
        _artifact_uses_hf_local_metadata_namespace,
    )

    refused = await harness.executor.approve(harness.job["job_id"])

    assert refused.state == "refused"
    assert refused.result_code == "artifact_mismatch"
    assert harness.installer.calls == []


def _ambiguous_gguf(value: dict[str, Any]) -> None:
    other = {
        "path": "weights/projector.gguf",
        "size_bytes": 10,
        "sha256": "sha256:" + "2" * 64,
    }
    files = sorted(
        [*value["artifacts"][0]["files"], other],
        key=lambda row: row["path"],
    )
    value["artifacts"][0]["files"] = files
    value["artifacts"][0]["manifest_digest"] = artifact_manifest_digest(files)
    value["artifacts"][0]["total_size_bytes"] = 110
    value["recipes"][0]["memory"]["weights_bytes"] = 110


def _sharded_gguf_with_projector(value: dict[str, Any]) -> None:
    files = [
        {
            "path": "weights/model-00001-of-00002.gguf",
            "size_bytes": 40,
            "sha256": "sha256:" + "1" * 64,
        },
        {
            "path": "weights/model-00002-of-00002.gguf",
            "size_bytes": 40,
            "sha256": "sha256:" + "2" * 64,
        },
        {
            "path": "weights/mmproj-model-f16.gguf",
            "size_bytes": 20,
            "sha256": "sha256:" + "3" * 64,
        },
    ]
    files.sort(key=lambda row: row["path"])
    artifact = value["artifacts"][0]
    artifact["files"] = files
    artifact["gguf_layout"] = {
        "kind": "gguf-file-set",
        "primary_file": "weights/model-00001-of-00002.gguf",
        "required_shards": ["weights/model-00002-of-00002.gguf"],
        "selected_projector_file": "weights/mmproj-model-f16.gguf",
    }
    artifact["manifest_digest"] = artifact_manifest_digest(files)
    artifact["total_size_bytes"] = 100


def _sharded_gguf_without_projector(value: dict[str, Any]) -> None:
    _sharded_gguf_with_projector(value)
    artifact = value["artifacts"][0]
    artifact["files"] = [
        item
        for item in artifact["files"]
        if item["path"] != "weights/mmproj-model-f16.gguf"
    ]
    artifact["gguf_layout"]["selected_projector_file"] = None
    artifact["manifest_digest"] = artifact_manifest_digest(artifact["files"])
    artifact["total_size_bytes"] = 80
    value["recipes"][0]["memory"]["weights_bytes"] = 80


def _incomplete_gguf_layout(value: dict[str, Any]) -> None:
    _sharded_gguf_with_projector(value)
    value["artifacts"][0]["gguf_layout"]["required_shards"] = []


@pytest.mark.asyncio
async def test_signed_layout_maps_exact_shards_and_selected_projector(
    tmp_path: Path,
) -> None:
    harness = await _catalog_harness(tmp_path, _sharded_gguf_with_projector)

    started = await harness.executor.approve(harness.job["job_id"])

    assert started.state == "downloading"
    assert len(harness.installer.calls) == 1
    call = harness.installer.calls[0]
    assert call["filename"] == "weights/model-00001-of-00002.gguf"
    assert call["projector_filename"] == "weights/mmproj-model-f16.gguf"
    assert call["download_files"] == (
        "weights/mmproj-model-f16.gguf",
        "weights/model-00001-of-00002.gguf",
        "weights/model-00002-of-00002.gguf",
    )
    assert call["destination"] == (
        "/Volumes/Models Link/nested/models/llama.cpp/acme/model"
    )
    assert not hasattr(harness.executor, "load")


def _replace_layout_proof(harness: Harness, installation_id: str) -> None:
    artifact = harness.snapshot.catalog()["artifacts"][0]
    proof = harness.installer.proofs[installation_id]
    harness.installer.proofs[installation_id] = SimpleNamespace(
        **{
            **vars(proof),
            "manifest_digest": artifact["manifest_digest"],
            "owned_files": tuple(
                SimpleNamespace(
                    path=item["path"],
                    size_bytes=item["size_bytes"],
                    sha256=item["sha256"],
                )
                for item in artifact["files"]
            ),
        }
    )


@pytest.mark.asyncio
async def test_signed_layout_completion_requires_every_exact_file_proof(
    tmp_path: Path,
) -> None:
    complete = await _catalog_harness(
        tmp_path / "complete",
        _sharded_gguf_with_projector,
    )
    started = await complete.executor.approve(complete.job["job_id"])
    assert started.installation_id is not None
    _replace_layout_proof(complete, started.installation_id)
    complete.installer.records[started.installation_id] = replace(
        complete.installer.records[started.installation_id],
        status="installed",
        bytes_downloaded=100,
    )
    completed = await complete.executor.reconcile(complete.job["job_id"])
    assert completed.state == "completed"

    incomplete = await _catalog_harness(
        tmp_path / "incomplete",
        _sharded_gguf_with_projector,
    )
    incomplete_started = await incomplete.executor.approve(
        incomplete.job["job_id"]
    )
    assert incomplete_started.installation_id is not None
    _replace_layout_proof(incomplete, incomplete_started.installation_id)
    proof = incomplete.installer.proofs[incomplete_started.installation_id]
    incomplete.installer.proofs[incomplete_started.installation_id] = (
        SimpleNamespace(
            **{
                **vars(proof),
                "owned_files": proof.owned_files[:-1],
            }
        )
    )
    incomplete.installer.records[incomplete_started.installation_id] = replace(
        incomplete.installer.records[incomplete_started.installation_id],
        status="installed",
        bytes_downloaded=100,
    )
    failed = await incomplete.executor.reconcile(incomplete.job["job_id"])
    assert failed.state == "failed"
    assert failed.result_code == "verification_failed"


@pytest.mark.asyncio
async def test_ds4_signed_layout_maps_exact_required_shards_without_projector(
    tmp_path: Path,
) -> None:
    harness = await _catalog_harness(
        tmp_path,
        _sharded_gguf_without_projector,
        engine="ds4",
    )

    started = await harness.executor.approve(harness.job["job_id"])

    assert started.state == "downloading"
    call = harness.installer.calls[0]
    assert call["engine"] == "ds4"
    assert call["filename"] == "weights/model-00001-of-00002.gguf"
    assert call["projector_filename"] is None
    assert call["download_files"] == (
        "weights/model-00001-of-00002.gguf",
        "weights/model-00002-of-00002.gguf",
    )


@pytest.mark.asyncio
async def test_ds4_signed_layout_refuses_a_projector_before_install(
    tmp_path: Path,
) -> None:
    harness = await _catalog_harness(
        tmp_path,
        _sharded_gguf_with_projector,
        engine="ds4",
    )
    result = await harness.executor.approve(harness.job["job_id"])
    assert result.state == "refused"
    assert result.result_code == "artifact_mismatch"
    assert harness.installer.calls == []


@pytest.mark.asyncio
async def test_signed_layout_must_cover_the_complete_artifact_file_set(
    tmp_path: Path,
) -> None:
    harness = await _catalog_harness(tmp_path, _incomplete_gguf_layout)
    result = await harness.executor.approve(harness.job["job_id"])
    assert result.state == "refused"
    assert result.result_code == "artifact_mismatch"
    assert harness.installer.calls == []


@pytest.mark.asyncio
async def test_desired_job_catalog_digest_fences_layout_changes(
    tmp_path: Path,
) -> None:
    harness = await _catalog_harness(tmp_path, _sharded_gguf_with_projector)
    changed = harness.snapshot.catalog()
    changed["artifacts"][0]["gguf_layout"]["primary_file"] = (
        "weights/model-00002-of-00002.gguf"
    )
    changed["artifacts"][0]["gguf_layout"]["required_shards"] = [
        "weights/model-00001-of-00002.gguf"
    ]
    harness.catalog.value = _snapshot(changed)

    result = await harness.executor.approve(harness.job["job_id"])

    assert result.state == "refused"
    assert result.result_code == "catalog_changed"
    assert harness.installer.calls == []


def _unsafe_artifact_path(value: dict[str, Any]) -> None:
    files = value["artifacts"][0]["files"]
    files[0]["path"] = "../escape.gguf"
    value["artifacts"][0]["manifest_digest"] = artifact_manifest_digest(files)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "result_code"),
    [
        (_recipe_engine, "artifact_mismatch"),
        (_recipe_capabilities, "artifact_mismatch"),
        (_recipe_context, "artifact_mismatch"),
        (_artifact_reference, "artifact_mismatch"),
        (_artifact_manifest, "artifact_mismatch"),
        (_artifact_total, "artifact_mismatch"),
        (_unsupported_source, "recipe_unknown"),
        (_ambiguous_gguf, "recipe_unknown"),
        (_unsafe_artifact_path, "artifact_mismatch"),
    ],
)
async def test_signed_catalog_mapping_fails_closed_without_guessing(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    result_code: str,
) -> None:
    harness = await _catalog_harness(tmp_path, mutation)
    result = await harness.executor.approve(harness.job["job_id"])
    assert result.state == "refused"
    assert result.result_code == result_code
    assert harness.installer.calls == []


def _storage_missing(h: Harness) -> None:
    h.storage.value = None


def _storage_id(h: Harness) -> None:
    h.storage.value = replace(
        h.storage.value,
        storage_location_id="99999999-9999-4999-8999-999999999999",
    )


def _storage_generation(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, binding_generation=3)


def _storage_index_path(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, indexed_lexical_root="/other")


def _storage_observed_path(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, observed_lexical_root="/other")


def _storage_volume(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, indexed_volume_uuid="volume-b")


def _storage_observed_volume(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, observed_volume_uuid="volume-b")


def _storage_scope(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, indexed_scope_id="a" * 64)


def _storage_unavailable(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, availability="missing", exists=False)


def _storage_wrong_volume(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, volume_matches=False)


def _storage_read_only(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, writable=False)


def _storage_insufficient(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, free_bytes=1099)


def _storage_no_measurement(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, free_bytes=None)


def _storage_policy(h: Harness) -> None:
    h.storage.value = replace(h.storage.value, remote_installs_allowed=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "result_code"),
    [
        (_storage_missing, "storage_location_unknown"),
        (_storage_id, "storage_binding_changed"),
        (_storage_generation, "storage_binding_changed"),
        (_storage_index_path, "storage_binding_changed"),
        (_storage_observed_path, "storage_binding_changed"),
        (_storage_volume, "storage_binding_changed"),
        (_storage_observed_volume, "storage_binding_changed"),
        (_storage_scope, "storage_binding_changed"),
        (_storage_unavailable, "storage_unavailable"),
        (_storage_wrong_volume, "storage_unavailable"),
        (_storage_read_only, "storage_read_only"),
        (_storage_insufficient, "insufficient_storage"),
        (_storage_no_measurement, "insufficient_storage"),
        (_storage_policy, "local_policy_refused"),
    ],
)
async def test_storage_authority_and_fresh_evidence_are_all_required(
    tmp_path: Path,
    mutate: AuthorityMutation,
    result_code: str,
) -> None:
    harness = await _harness(tmp_path)
    mutate(harness)
    result = await harness.executor.approve(harness.job["job_id"])
    assert result.state == "refused"
    assert result.result_code == result_code
    assert harness.installer.calls == []


def _runtime_missing(h: Harness) -> None:
    h.runtime.value = None


def _runtime_disabled(h: Harness) -> None:
    h.runtime.value = replace(h.runtime.value, enabled=False)


def _runtime_unhealthy(h: Harness) -> None:
    h.runtime.value = replace(h.runtime.value, healthy=False)


def _runtime_engine(h: Harness) -> None:
    h.runtime.value = replace(h.runtime.value, engine="ds4")


def _runtime_tier(h: Harness) -> None:
    h.runtime.value = replace(h.runtime.value, release_tier="preview")


def _runtime_catalog(h: Harness) -> None:
    h.runtime.value = replace(h.runtime.value, catalog_digest="sha256:" + "7" * 64)


def _runtime_known_bad_version(h: Harness) -> None:
    h.runtime.value = replace(h.runtime.value, version="b5500")


def _runtime_known_bad_fingerprint(h: Harness) -> None:
    h.runtime.value = replace(
        h.runtime.value, runtime_fingerprint="sha256:" + "e" * 64
    )


def _runtime_fingerprint(h: Harness) -> None:
    h.runtime.value = replace(
        h.runtime.value, runtime_fingerprint="sha256:" + "8" * 64
    )


def _runtime_features(h: Harness) -> None:
    h.runtime.value = replace(h.runtime.value, features=())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        _runtime_missing,
        _runtime_disabled,
        _runtime_unhealthy,
        _runtime_engine,
        _runtime_tier,
        _runtime_catalog,
        _runtime_known_bad_version,
        _runtime_known_bad_fingerprint,
        _runtime_fingerprint,
        _runtime_features,
    ],
)
async def test_runtime_must_be_current_healthy_and_catalog_compatible(
    tmp_path: Path,
    mutate: AuthorityMutation,
) -> None:
    harness = await _harness(tmp_path)
    mutate(harness)
    result = await harness.executor.approve(harness.job["job_id"])
    assert result.state == "refused"
    assert result.result_code == "runtime_unavailable"
    assert harness.installer.calls == []


@pytest.mark.asyncio
async def test_version_range_is_used_only_when_catalog_has_no_fingerprint_allowlist(
    tmp_path: Path,
) -> None:
    def no_allowlist(value: dict[str, Any]) -> None:
        value["recipes"][0]["runtime"]["allowed_runtime_fingerprints"] = []

    compatible = await _catalog_harness(tmp_path / "compatible", no_allowlist)
    compatible.runtime.value = replace(
        compatible.runtime.value,
        version="b6000",
        runtime_fingerprint=None,
    )
    started = await compatible.executor.approve(compatible.job["job_id"])
    assert started.state == "downloading"

    too_old = await _catalog_harness(tmp_path / "old", no_allowlist)
    too_old.runtime.value = replace(
        too_old.runtime.value,
        version="b4999",
        runtime_fingerprint=None,
    )
    refused = await too_old.executor.approve(too_old.job["job_id"])
    assert refused.state == "refused"
    assert refused.result_code == "runtime_unavailable"


@pytest.mark.asyncio
async def test_optional_alias_is_refused_instead_of_inventing_registration_identity(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    harness.job.pop("alias")
    await harness.store.close()
    clock = Clock()
    harness.store = DesiredInstallStore(
        tmp_path / "without-alias.sqlite3",
        wall_clock=lambda: clock.wall,
        monotonic_clock=lambda: clock.monotonic,
        boot_identity=lambda: clock.boot,
    )
    await harness.store.initialize()
    await harness.store.receive(harness.job)
    harness.executor = DesiredInstallExecutor(
        store=harness.store,
        catalog=harness.catalog,
        pairing=harness.pairing,
        inventory=harness.inventory,
        storage=harness.storage,
        runtimes=harness.runtime,
        installer=harness.installer,
        installation_id_factory=(
            lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        ),
    )
    result = await harness.executor.approve(harness.job["job_id"])
    assert result.state == "refused"
    assert result.result_code == "local_policy_refused"
    assert harness.installer.calls == []


@pytest.mark.asyncio
async def test_provider_and_installer_failures_use_only_closed_internal_code(
    tmp_path: Path,
) -> None:
    provider = await _harness(tmp_path / "provider")
    provider.catalog.value = RuntimeError("secret path /Volumes/private")
    provider_result = await provider.executor.approve(provider.job["job_id"])
    assert provider_result.state == "failed"
    assert provider_result.result_code == "internal_error"

    installer = await _harness(tmp_path / "installer")
    installer.installer.error = RuntimeError("secret download response")
    with pytest.raises(DesiredInstallExecutorError) as create_error:
        await installer.executor.approve(installer.job["job_id"])
    assert create_error.value.code == "desired_install_internal_error"
    retryable = await installer.store.get(installer.job["job_id"])
    assert retryable is not None
    assert retryable.state == "accepted"
    assert retryable.terminal is False
    assert retryable.installation_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    installer.installer.error = None
    retried = await installer.executor.reconcile(installer.job["job_id"])
    assert retried.state == "downloading"
    assert len(installer.installer.calls) == 2
    assert {
        call["installation_id"] for call in installer.installer.calls
    } == {"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}


@pytest.mark.asyncio
async def test_invalid_installer_identity_is_stopped_and_never_bound(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    harness.installer.mutate_observation = lambda record: replace(
        record, destination="/tmp/remote-injected"
    )
    result = await harness.executor.approve(harness.job["job_id"])
    assert result.state == "failed"
    assert result.result_code == "internal_error"
    assert result.installation_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert harness.installer.cancelled == [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    ]


@pytest.mark.asyncio
async def test_reconcile_adopts_exact_existing_row_after_crash_before_progress_bind(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    installation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    receipt = await harness.store.get(harness.job["job_id"])
    assert receipt is not None
    accepted = await harness.store.transition(
        job_id=harness.job["job_id"],
        job_revision=1,
        state="accepted",
        bytes_downloaded=0,
        total_bytes=100,
        result_code=None,
        installation_id=installation_id,
    )
    assert accepted.record.installation_id == installation_id
    harness.installer.records[installation_id] = InstallObservation(
        id=installation_id,
        repo_id="acme/model",
        engine="llama.cpp",
        storage="external-models",
        alias="test-model",
        destination=(
            "/Volumes/Models Link/nested/models/llama.cpp/acme/model"
        ),
        status="queued",
        revision="a" * 40,
        filename="weights/model.gguf",
        projector_filename=None,
            context_length=8192,
            files_json='["weights/model.gguf"]',
            expected_files_json=(
                '[{"path":"weights/model.gguf","sha256":"'
                + FILE_DIGEST
                + '","size_bytes":100}]'
            ),
            expected_manifest_digest=artifact_manifest_digest(
                [
                    {
                        "path": "weights/model.gguf",
                        "size_bytes": 100,
                        "sha256": FILE_DIGEST,
                    }
                ]
            ),
            capabilities_json=(
            '["chat/completions","completions","messages","responses"]'
        ),
        family="test-family",
        launch_json=(
            '{"engine":"llama.cpp","flash_attention":"automatic",'
            '"gpu_offload":"all","parallel_slots":2}'
        ),
        bytes_downloaded=0,
        total_bytes=100,
    )

    adopted = await harness.executor.reconcile(harness.job["job_id"])

    assert adopted.state == "downloading"
    assert adopted.installation_id == installation_id
    assert harness.installer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recovered_status", "next_state"),
    [
        ("partial", "downloading"),
        ("downloaded", "verifying"),
    ],
)
async def test_restart_recovery_retries_exact_bound_native_row(
    tmp_path: Path,
    recovered_status: str,
    next_state: str,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    original = harness.installer.records[started.installation_id]
    harness.installer.records[started.installation_id] = replace(
        original,
        status=recovered_status,
        bytes_downloaded=100 if recovered_status == "downloaded" else 40,
    )

    resumed = await harness.executor.reconcile(harness.job["job_id"])

    assert resumed.state == next_state
    assert resumed.installation_id == started.installation_id
    assert harness.installer.retried == [started.installation_id]
    assert len(harness.installer.calls) == 1


@pytest.mark.asyncio
async def test_restart_adopts_exact_installed_row_as_cold_completed(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    installation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    waiting = await harness.store.get(harness.job["job_id"])
    assert waiting is not None
    plan = await harness.executor._build_plan(waiting, installation_id)
    existing = await harness.executor._create(plan.request)
    harness.installer.records[installation_id] = replace(
        existing,
        status="installed",
        bytes_downloaded=100,
    )
    accepted = await harness.store.transition(
        job_id=harness.job["job_id"],
        job_revision=1,
        state="accepted",
        bytes_downloaded=0,
        total_bytes=100,
        result_code=None,
        installation_id=installation_id,
    )
    assert accepted.record.state == "accepted"

    adopted = await harness.executor.reconcile(harness.job["job_id"])

    assert adopted.state == "completed"
    assert adopted.installation_id == installation_id
    assert len(harness.installer.calls) == 1
    assert harness.installer.retried == []


@pytest.mark.asyncio
async def test_immutable_mismatch_is_never_retried_after_restart(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    original = harness.installer.records[started.installation_id]
    harness.installer.records[started.installation_id] = replace(
        original,
        status="partial",
        destination="/tmp/not-the-selected-storage",
    )

    failed = await harness.executor.reconcile(harness.job["job_id"])

    assert failed.state == "failed"
    assert failed.result_code == "internal_error"
    assert harness.installer.retried == []
    assert len(harness.installer.calls) == 1


@pytest.mark.asyncio
async def test_persisted_launch_mismatch_is_never_adopted_or_retried(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    original = harness.installer.records[started.installation_id]
    harness.installer.records[started.installation_id] = replace(
        original,
        status="partial",
        launch_json=(
            '{"engine":"llama.cpp","flash_attention":"automatic",'
            '"gpu_offload":"all","parallel_slots":1}'
        ),
    )

    failed = await harness.executor.reconcile(harness.job["job_id"])

    assert failed.state == "failed"
    assert failed.result_code == "internal_error"
    assert harness.installer.retried == []
    assert harness.installer.cancelled == []


@pytest.mark.asyncio
async def test_persisted_signed_expectation_mismatch_is_never_adopted_or_retried(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    original = harness.installer.records[started.installation_id]
    harness.installer.records[started.installation_id] = replace(
        original,
        status="partial",
        expected_manifest_digest="sha256:" + "f" * 64,
    )

    failed = await harness.executor.reconcile(harness.job["job_id"])

    assert failed.state == "failed"
    assert failed.result_code == "internal_error"
    assert harness.installer.retried == []
    assert harness.installer.cancelled == []


@pytest.mark.asyncio
async def test_ambiguous_create_exception_adopts_committed_exact_row(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    harness.installer.error_after_record = RuntimeError("ambiguous local commit")

    result = await harness.executor.approve(harness.job["job_id"])

    assert result.state == "downloading"
    assert result.installation_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert len(harness.installer.calls) == 1


@pytest.mark.asyncio
async def test_reconcile_revalidates_authority_and_stops_stale_install(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    harness.inventory.value = replace(
        harness.inventory.value,
        inventory_instance_id="99999999-9999-4999-8999-999999999999",
    )
    result = await harness.executor.reconcile(harness.job["job_id"])
    assert result.state == "refused"
    assert result.result_code == "inventory_basis_stale"
    assert harness.installer.cancelled == [started.installation_id]


@pytest.mark.asyncio
async def test_installed_without_complete_progress_fails_verification(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    current = harness.installer.records[started.installation_id]
    harness.installer.records[started.installation_id] = replace(
        current, status="installed", bytes_downloaded=99
    )
    result = await harness.executor.reconcile(harness.job["job_id"])
    assert result.state == "failed"
    assert result.result_code == "verification_failed"


@pytest.mark.asyncio
async def test_installed_requires_exact_signed_manifest_and_storage_proof(
    tmp_path: Path,
) -> None:
    missing = await _harness(tmp_path / "missing")
    missing_started = await missing.executor.approve(missing.job["job_id"])
    assert missing_started.installation_id is not None
    missing_record = missing.installer.records[missing_started.installation_id]
    missing.installer.records[missing_started.installation_id] = replace(
        missing_record,
        status="installed",
        bytes_downloaded=100,
    )
    missing.installer.proofs.pop(missing_started.installation_id)
    missing_result = await missing.executor.reconcile(missing.job["job_id"])
    assert missing_result.state == "failed"
    assert missing_result.result_code == "verification_failed"

    mismatch = await _harness(tmp_path / "mismatch")
    mismatch_started = await mismatch.executor.approve(mismatch.job["job_id"])
    assert mismatch_started.installation_id is not None
    mismatch_record = mismatch.installer.records[mismatch_started.installation_id]
    mismatch.installer.records[mismatch_started.installation_id] = replace(
        mismatch_record,
        status="installed",
        bytes_downloaded=100,
    )
    proof = mismatch.installer.proofs[mismatch_started.installation_id]
    mismatch.installer.proofs[mismatch_started.installation_id] = SimpleNamespace(
        **(
            vars(proof)
            | {
                "storage_lexical_root": "/different/root",
                "manifest_digest": "sha256:" + "9" * 64,
            }
        )
    )
    mismatch_result = await mismatch.executor.reconcile(mismatch.job["job_id"])
    assert mismatch_result.state == "failed"
    assert mismatch_result.result_code == "verification_failed"
    assert mismatch.installer.cancelled == []


@pytest.mark.asyncio
async def test_installed_proof_keeps_closed_hf_metadata_in_cleanup_manifest(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    proof = harness.installer.proofs[started.installation_id]
    metadata_paths = (
        ".cache/huggingface/.gitignore",
        ".cache/huggingface/.gitignore.lock",
        ".cache/huggingface/.mnemosyne-managed-creation-v1.json",
        ".cache/huggingface/CACHEDIR.TAG",
        ".cache/huggingface/download/weights/model.gguf.lock",
        ".cache/huggingface/download/weights/model.gguf.metadata",
    )
    complete_tree = [
        *proof.owned_files,
        *(
            SimpleNamespace(path=path, size_bytes=1, sha256="sha256:" + "e" * 64)
            for path in metadata_paths
        ),
    ]
    harness.installer.proofs[started.installation_id] = SimpleNamespace(
        **{
            **vars(proof),
            "manifest_digest": "sha256:" + "f" * 64,
            "owned_files": tuple(sorted(complete_tree, key=lambda item: item.path)),
        }
    )
    harness.installer.records[started.installation_id] = replace(
        harness.installer.records[started.installation_id],
        status="installed",
        bytes_downloaded=100,
    )

    completed = await harness.executor.reconcile(harness.job["job_id"])

    assert completed.state == "completed"
    assert completed.result_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unexpected_path",
    [
        ".cache/huggingface/download/weights/other.gguf.metadata",
        ".cache/huggingface/unsigned-model.safetensors",
        "unsigned-model.safetensors",
    ],
)
async def test_installed_proof_rejects_unsigned_or_unexpected_owned_file(
    tmp_path: Path,
    unexpected_path: str,
) -> None:
    harness = await _harness(tmp_path)
    started = await harness.executor.approve(harness.job["job_id"])
    assert started.installation_id is not None
    proof = harness.installer.proofs[started.installation_id]
    complete_tree = [
        *proof.owned_files,
        SimpleNamespace(
            path=unexpected_path,
            size_bytes=1,
            sha256="sha256:" + "e" * 64,
        ),
    ]
    harness.installer.proofs[started.installation_id] = SimpleNamespace(
        **{
            **vars(proof),
            "manifest_digest": "sha256:" + "f" * 64,
            "owned_files": tuple(sorted(complete_tree, key=lambda item: item.path)),
        }
    )
    harness.installer.records[started.installation_id] = replace(
        harness.installer.records[started.installation_id],
        status="installed",
        bytes_downloaded=100,
    )

    failed = await harness.executor.reconcile(harness.job["job_id"])

    assert failed.state == "failed"
    assert failed.result_code == "verification_failed"


@pytest.mark.asyncio
async def test_omlx_launch_is_accepted_only_after_exact_runtime_proof(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path, engine="omlx")
    result = await harness.executor.approve(harness.job["job_id"])
    assert result.state == "downloading"
    assert result.result_code is None
    assert result.installation_id is not None
    assert len(harness.installer.calls) == 1
    assert harness.installer.calls[0]["launch_contract"] == OMLXInstallLaunch(
        engine="omlx",
        scheduler_slots=2,
        memory_guard="required",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slots", "guard"),
    [(1, True), (2, False), (None, True), (2, None)],
)
async def test_omlx_launch_refuses_unproved_or_drifted_globals_before_download(
    tmp_path: Path,
    slots: int | None,
    guard: bool | None,
) -> None:
    harness = await _harness(tmp_path, engine="omlx")
    harness.runtime.value = replace(
        harness.runtime.value,
        omlx_scheduler_slots=slots,
        omlx_memory_guard_enabled=guard,
    )

    result = await harness.executor.approve(harness.job["job_id"])

    assert result.state == "refused"
    assert result.result_code == "runtime_unavailable"
    assert result.installation_id is None
    assert harness.installer.calls == []
    assert harness.installer.records == {}
