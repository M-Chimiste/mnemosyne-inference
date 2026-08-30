from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pytest

from mnemosyne_macos.config import MacConfig
from mnemosyne_macos.coordinator import CoordinatorState
from mnemosyne_macos.install_provenance import (
    ArtifactAuthority,
    CleanupInstallation,
    DestinationStateBefore,
    ExclusiveManagedProof,
    InstallationProvenance,
    MANAGED_CREATION_MARKER_PATH,
    OwnedFile,
    PROVENANCE_REVISION,
    ProvenanceProofRejected,
    canonical_owned_files_json,
    decide_cleanup_eligibility,
    destination_binding_digest,
    owned_manifest_digest,
    provenance_from_exclusive_proof,
)
from mnemosyne_macos.install_store import InstallRecord
from mnemosyne_macos.mac_inventory import (
    DefaultMacHardwareProbe,
    NONE_CATALOG_DIGEST,
    MacInventoryProducer,
)
from mnemosyne_macos.mac_inventory_store import MacInventoryIndex, StorageBinding
from mnemosyne_macos.models import EngineName
from mnemosyne_macos.storage import StorageStatus


PROTOCOL_V1 = Path(__file__).resolve().parents[3] / "mac_pool_protocol" / "v1"
MANAGED_RUNTIME_FINGERPRINT = "sha256:" + "e" * 64


def _display_payload(*rows: dict[str, object]) -> bytes:
    return json.dumps({"SPDisplaysDataType": list(rows)}).encode("utf-8")


def _apple_display_row(**overrides: object) -> dict[str, object]:
    return {
        "_name": "must-not-be-retained",
        "sppci_device_type": "spdisplays_gpu",
        "spdisplays_vendor": "sppci_vendor_Apple",
        "sppci_bus": "spdisplays_builtin",
        "spdisplays_metal": "spdisplays_supported",
        "sppci_model": "Apple M4 Max",
        "sppci_cores": "40",
        **overrides,
    }


def test_default_hardware_probe_accepts_one_exact_apple_metal_gpu() -> None:
    facts = DefaultMacHardwareProbe._parse_display_facts(
        _display_payload(_apple_display_row())
    )
    assert facts.metal_supported is True
    assert facts.gpu_cores == 40
    assert facts.soc_family == "Apple M4 Max"


@pytest.mark.parametrize(
    "payload",
    [
        b"{malformed",
        b"x" * (256 * 1024 + 1),
        json.dumps({"SPDisplaysDataType": {}}).encode("utf-8"),
        _display_payload(
            _apple_display_row(sppci_cores="16"),
            _apple_display_row(sppci_cores="40"),
        ),
    ],
)
def test_default_hardware_probe_rejects_malformed_or_ambiguous_profiler(
    payload: bytes,
) -> None:
    facts = DefaultMacHardwareProbe._parse_display_facts(payload)
    assert facts.metal_supported is False
    assert facts.gpu_cores is None


def test_default_hardware_probe_keeps_metal_but_not_malformed_gpu_count() -> None:
    facts = DefaultMacHardwareProbe._parse_display_facts(
        _display_payload(_apple_display_row(sppci_cores="forty"))
    )
    assert facts.metal_supported is True
    assert facts.gpu_cores is None
    assert facts.soc_family == "Apple M4 Max"


def test_default_hardware_probe_profiler_timeout_fails_closed(
    monkeypatch,
) -> None:
    original_is_file = Path.is_file
    profiler = Path("/usr/sbin/system_profiler")
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda value: True if value == profiler else original_is_file(value),
    )

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 3
        assert kwargs["stderr"] is subprocess.DEVNULL
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    facts = DefaultMacHardwareProbe._display_facts()
    assert facts.metal_supported is False
    assert facts.gpu_cores is None


def test_default_hardware_probe_v2_retains_only_closed_display_facts(
    monkeypatch,
) -> None:
    sysctls = {
        "hw.memsize": str(128 * 1024**3),
        "hw.perflevel0.physicalcpu": "12",
        "hw.perflevel1.physicalcpu": "4",
    }
    monkeypatch.setattr(
        DefaultMacHardwareProbe,
        "_sysctl",
        classmethod(lambda _cls, name: sysctls.get(name)),
    )
    monkeypatch.setattr(
        DefaultMacHardwareProbe,
        "_display_facts",
        classmethod(
            lambda cls: cls._parse_display_facts(
                _display_payload(_apple_display_row())
            )
        ),
    )
    monkeypatch.setattr(
        DefaultMacHardwareProbe,
        "_os_version",
        staticmethod(lambda: (15, 6)),
    )
    monkeypatch.setattr(
        DefaultMacHardwareProbe,
        "_power",
        staticmethod(lambda: ("ac", False)),
    )
    result = DefaultMacHardwareProbe._probe_sync()
    assert result["probe_version"] == 2
    # machdep.cpu.brand_string is absent on some Apple Silicon releases; the
    # same unambiguous built-in GPU row supplies the sanitized SoC fact.
    assert result["soc_family"] == "Apple M4 Max"
    assert result["gpu_cores"] == 40
    assert "must-not-be-retained" not in json.dumps(result)


@dataclass
class FakeHardwareProbe:
    async def probe(self) -> dict[str, object]:
        return {
            "probe_version": 1,
            "soc_family": "Apple M4 Pro",
            "architecture": "arm64",
            "performance_cores": 10,
            "efficiency_cores": 4,
            "gpu_cores": 20,
            "unified_memory_bytes": 48 * 1024**3,
            "allocatable_memory_bytes": 38 * 1024**3,
            "os_major": 15,
            "os_minor": 6,
            "power_source": "ac",
            "low_power_mode": False,
            "pressure_class": "nominal",
            "observed_at": 1_785_528_000.0,
            "evidence_class": "measured",
        }


class FakeFilesystem:
    def __init__(self, statuses: dict[str, StorageStatus]) -> None:
        self.statuses = statuses

    async def inspect(self, _path: str, *, name: str, **_kwargs) -> StorageStatus:
        return self.statuses[name]


class FakeInstaller:
    def __init__(
        self,
        records: list[InstallRecord],
        *,
        provenances: dict[str, InstallationProvenance] | None = None,
    ) -> None:
        self.records = records
        self.provenances = {} if provenances is None else provenances
        self.authority_records: dict[str, InstallRecord] = {}

    async def evidence(self, *, limit: int) -> list[dict[str, object]]:
        assert limit == 10_000
        return [
            {
                **record.to_dict(),
                "dismissed": bool(record.hidden),
                "events": [],
            }
            for record in self.records
        ]

    async def require_cleanup_authority(
        self,
        installation_id: str,
    ) -> tuple[InstallRecord, InstallationProvenance]:
        record = next(
            (item for item in self.records if item.id == installation_id),
            None,
        )
        provenance = self.provenances.get(installation_id)
        if record is None or provenance is None:
            raise RuntimeError("provenance_missing")
        authority_record = self.authority_records.get(installation_id, record)
        decision = decide_cleanup_eligibility(
            installation_id,
            CleanupInstallation(
                installation_id=authority_record.id,
                status=authority_record.status,
                destination=authority_record.destination,
                resolved_revision=authority_record.revision,
                total_bytes=authority_record.total_bytes,
            ),
            provenance,
        )
        if not decision.eligible:
            raise ProvenanceProofRejected(decision.reason)
        return authority_record, provenance


class FakeRuntimeUpdates:
    async def installed_status(self) -> dict[str, dict[str, object]]:
        return {
            engine.value: {
                "installed": engine in {EngineName.LLAMA_CPP, EngineName.OMLX},
                "version": "1.2.3" if engine == EngineName.OMLX else "b7000",
                "revision": None,
                "compatibility_fingerprint": (
                    MANAGED_RUNTIME_FINGERPRINT
                    if engine == EngineName.LLAMA_CPP
                    else None
                ),
                # This path-bearing source value must never be copied.
                "path": "/Applications/private/runtime/bin",
            }
            for engine in EngineName
        }


class FakeAsyncValue:
    def __init__(self, value) -> None:
        self.value = value

    async def status(self):
        return self.value


class FakeCatalog:
    def __init__(self, version: str, digest: str) -> None:
        self.version = version
        self.digest = digest

    async def inventory_identity(self) -> tuple[str, str]:
        return self.version, self.digest


def _runtime(
    tmp_path: Path,
    *,
    lexical_external: str,
    volume_uuid: str,
    scope_id: str,
    managed_destination: str,
) -> SimpleNamespace:
    internal = tmp_path / "internal-models"
    internal.mkdir(exist_ok=True)
    config = MacConfig.model_validate(
        {
            "engines": {
                "llama_cpp": {"enabled": False},
                "omlx": {"enabled": True},
            },
            "paths": {"state_database": str(tmp_path / "state.db")},
            "storage": {
                "default": "internal",
                "locations": [
                    {"name": "internal", "path": str(internal)},
                    {
                        "name": "archive",
                        "path": lexical_external,
                        "volume_uuid": volume_uuid,
                        "scope_id": scope_id,
                    },
                ],
            },
            "models": [
                {
                    "alias": "disabled-local",
                    "engine": "llama.cpp",
                    "model": f"{lexical_external}/weights/private-model.gguf",
                    "storage": "archive",
                    "enabled": False,
                },
                {
                    "alias": "external-omlx",
                    "engine": "omlx",
                    "model": "upstream-model-id",
                },
            ],
        }
    )
    managed = InstallRecord(
        id="77777777-7777-4777-8777-777777777777",
        repo_id="private-owner/private-repo",
        engine="llama.cpp",
        storage="archive",
        alias="failed-secret-model",
        destination=managed_destination,
        status="failed",
        revision="main",
        filename="private.gguf",
        bytes_downloaded=123,
        total_bytes=456,
        error=f"failed while opening {managed_destination}: bearer-secret",
        created_at=1_785_527_000.0,
        updated_at=1_785_527_100.0,
    )
    statuses = {
        "internal": StorageStatus(
            name="internal",
            path=str(internal),
            exists=True,
            is_directory=True,
            writable=True,
            mount_path="/",
            volume_uuid="INTERNAL-SECRET",
            expected_volume_uuid=None,
            volume_matches=True,
            total_bytes=2_000_000,
            free_bytes=1_000_000,
            diagnostic=None,
        ),
        "archive": StorageStatus(
            name="archive",
            path=lexical_external,
            exists=False,
            is_directory=False,
            writable=False,
            mount_path=None,
            volume_uuid=None,
            expected_volume_uuid=volume_uuid,
            volume_matches=False,
            total_bytes=None,
            free_bytes=None,
            diagnostic=f"missing {lexical_external} on {volume_uuid}",
        ),
    }
    coordinator = SimpleNamespace(
        resident_alias=None,
        resident_engine=None,
        resident_model=None,
        transition_target=None,
        transition_engine=None,
        state=CoordinatorState.IDLE,
    )
    participation = SimpleNamespace(state=SimpleNamespace(value="paused"))
    usage = {
        "enabled": True,
        "writer_ready": False,
        "outbox_pending": 12,
        "last_flush_at": None,
        "last_error_code": None,
        "last_error": f"could not connect through {lexical_external}",
    }
    return SimpleNamespace(
        config=config,
        filesystem=FakeFilesystem(statuses),
        runtime_updates=FakeRuntimeUpdates(),
        _runtime_fingerprints={
            EngineName.LLAMA_CPP: f"binary:{managed_destination}",
            EngineName.OMLX: "omlx:1.2.3",
        },
        installer=FakeInstaller([managed]),
        coordinator=FakeAsyncValue(coordinator),
        fleet_participation=FakeAsyncValue(participation),
        usage=FakeAsyncValue(usage),
    )


def _signed_install_record(tmp_path: Path) -> InstallRecord:
    expected_files = (
        OwnedFile(
            path="weights.safetensors",
            size_bytes=8,
            sha256="sha256:" + "1" * 64,
        ),
    )
    return InstallRecord(
        id="88888888-8888-4888-8888-888888888888",
        repo_id="publisher/signed-model",
        engine="omlx",
        storage="internal",
        alias="external-omlx",
        destination=str(tmp_path / "internal-models" / "upstream-model-id"),
        status="installed",
        revision="a" * 40,
        files_json='["weights.safetensors"]',
        expected_files_json=canonical_owned_files_json(expected_files),
        expected_manifest_digest=owned_manifest_digest(expected_files),
        bytes_downloaded=8,
        total_bytes=8,
        created_at=1_785_527_000.0,
        updated_at=1_785_527_100.0,
    )


def _signed_install_provenance(
    record: InstallRecord,
    binding: StorageBinding,
) -> InstallationProvenance:
    owned_files = (
        OwnedFile(
            path="weights.safetensors",
            size_bytes=8,
            sha256="sha256:" + "1" * 64,
        ),
        OwnedFile(
            path=MANAGED_CREATION_MARKER_PATH,
            size_bytes=1,
            sha256="sha256:" + "4" * 64,
        ),
    )
    owned_files = tuple(sorted(owned_files, key=lambda item: item.path))
    binding_fields = {
        "storage_location_id": binding.storage_location_id,
        "storage_binding_generation": binding.binding_generation,
        "storage_lexical_root": binding.exact_path,
        "lexical_destination": record.destination,
        "storage_volume_uuid": binding.volume_uuid,
        "storage_scope_id": binding.scope_id,
    }
    return provenance_from_exclusive_proof(
        ExclusiveManagedProof(
            installation_id=record.id,
            **binding_fields,
            destination_binding_digest=destination_binding_digest(
                **binding_fields,
            ),
            catalog_id="catalog:apple-silicon",
            logical_model_id="model:signed-omlx",
            artifact_id="artifact:signed-omlx-4bit",
            recipe_id="recipe:signed-omlx-4bit",
            resolved_revision=record.revision or "",
            catalog_digest="sha256:" + "2" * 64,
            artifact_authority=ArtifactAuthority.SIGNED_CATALOG,
            source_identity_digest="sha256:" + "3" * 64,
            manifest_digest=owned_manifest_digest(owned_files),
            owned_files=owned_files,
            destination_state_before=DestinationStateBefore.ABSENT,
            destination_created_by_transaction=True,
            preexisting_entries=(),
            extra_entries=(),
            creation_transaction_id="99999999-9999-4999-8999-999999999999",
            directory_device=42,
            directory_inode=84,
            provenance_revision=PROVENANCE_REVISION,
        )
    )


@pytest.mark.asyncio
async def test_inventory_is_schema_valid_and_never_leaks_nested_symlink_or_private_state(
    tmp_path,
) -> None:
    physical = tmp_path / "physical-volume" / "deep" / "models"
    physical.mkdir(parents=True)
    link = tmp_path / "selected-link"
    link.symlink_to(tmp_path / "physical-volume", target_is_directory=True)
    lexical_external = str(link / "deep" / "models")
    resolved_external = str(physical.resolve())
    volume_uuid = "PRIVATE-VOLUME-UUID-1234"
    scope_id = "a" * 64
    managed_destination = f"{lexical_external}/llama.cpp/private-owner/private-repo"
    runtime = _runtime(
        tmp_path,
        lexical_external=lexical_external,
        volume_uuid=volume_uuid,
        scope_id=scope_id,
        managed_destination=managed_destination,
    )
    producer = MacInventoryProducer(
        runtime,
        MacInventoryIndex(tmp_path / "state.db"),
        hardware_probe=FakeHardwareProbe(),
        wall_clock=lambda: 1_785_528_001.0,
        instance_id="11111111-1111-4111-8111-111111111111",
    )
    await producer.initialize()
    document = await producer.next_document(
        pairing_id="22222222-2222-4222-8222-222222222222",
        credential_generation=4,
    )

    schema = json.loads(
        (PROTOCOL_V1 / "mac_inventory.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(document)
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    for forbidden in (
        lexical_external,
        resolved_external,
        str(link),
        volume_uuid,
        scope_id,
        managed_destination,
        "private-owner",
        "private-repo",
        "bearer-secret",
        "/Applications/private/runtime/bin",
        "INTERNAL-SECRET",
    ):
        assert forbidden not in encoded

    assert document["service"]["catalog_version"] == "none"
    assert document["service"]["catalog_digest"] == NONE_CATALOG_DIGEST
    assert document["service"]["supported_job_versions"] == []
    llama_runtime = next(
        row for row in document["runtimes"] if row["engine"] == "llama.cpp"
    )
    assert llama_runtime["runtime_fingerprint"] == MANAGED_RUNTIME_FINGERPRINT
    assert llama_runtime["runtime_fingerprint"] != runtime._runtime_fingerprints[
        EngineName.LLAMA_CPP
    ]
    assert document["participation"] == {
        "state": "paused",
        "remote_install_policy": "ask",
    }
    archive = next(
        row
        for row in document["storage_locations"]
        if row["availability"] == "missing"
    )
    assert archive["diagnostic_code"] == "volume_missing"
    disabled = next(
        row for row in document["installations"] if row["aliases"] == ["disabled-local"]
    )
    assert disabled["availability"] == "storage_missing"
    assert disabled["runtime_compatibility"] == "engine_disabled"
    failed = next(
        row
        for row in document["installations"]
        if row["installation_id"] == "77777777-7777-4777-8777-777777777777"
    )
    assert failed["source_kind"] == "managed_download"
    assert failed["ownership_class"] == "unknown"
    assert failed["diagnostic_code"] == "download_failed"
    assert document["job_acknowledgements"] == []


@pytest.mark.asyncio
async def test_inventory_retires_trashed_exact_install_without_hiding_live_history(
    tmp_path,
) -> None:
    external = tmp_path / "external" / "models"
    external.mkdir(parents=True)
    runtime = _runtime(
        tmp_path,
        lexical_external=str(external),
        volume_uuid="VOL-LIFECYCLE",
        scope_id="d" * 64,
        managed_destination=str(external / "managed"),
    )

    def managed(
        installation_id: str,
        *,
        alias: str,
        status: str,
        destination: Path,
        hidden: int = 0,
        filename: str | None = None,
    ) -> InstallRecord:
        return InstallRecord(
            id=installation_id,
            repo_id="private-owner/private-repo",
            engine="llama.cpp" if filename is not None else "omlx",
            storage="archive" if filename is not None else "internal",
            alias=alias,
            destination=str(destination),
            status=status,
            revision="a" * 40,
            filename=filename,
            bytes_downloaded=8,
            total_bytes=8,
            hidden=hidden,
            created_at=1_785_527_000.0,
            updated_at=1_785_527_100.0,
        )

    installed_id = "11111111-1111-4111-8111-111111111111"
    downloaded_id = "22222222-2222-4222-8222-222222222222"
    failed_id = "33333333-3333-4333-8333-333333333333"
    hidden_id = "44444444-4444-4444-8444-444444444444"
    trashed_id = "55555555-5555-4555-8555-555555555555"
    runtime.installer = FakeInstaller(
        [
            managed(
                installed_id,
                alias="active-installed",
                status="installed",
                destination=tmp_path / "internal-models" / "active-installed",
            ),
            managed(
                downloaded_id,
                alias="downloaded-model",
                status="downloaded",
                destination=tmp_path / "internal-models" / "downloaded-model",
            ),
            managed(
                failed_id,
                alias="failed-model",
                status="failed",
                destination=tmp_path / "internal-models" / "failed-model",
            ),
            managed(
                hidden_id,
                alias="hidden-installed",
                status="installed",
                destination=tmp_path / "internal-models" / "hidden-installed",
                hidden=1,
            ),
            managed(
                trashed_id,
                alias="disabled-local",
                status="trashed",
                destination=external / "weights",
                filename="private-model.gguf",
            ),
        ]
    )
    producer = MacInventoryProducer(
        runtime,
        MacInventoryIndex(tmp_path / "state.db"),
        hardware_probe=FakeHardwareProbe(),
        wall_clock=lambda: 1_785_528_002.0,
        instance_id="66666666-6666-4666-8666-666666666666",
    )
    await producer.initialize()
    document = await producer.next_document(
        pairing_id="77777777-7777-4777-8777-777777777777",
        credential_generation=1,
    )

    rows = {
        row["installation_id"]: row for row in document["installations"]
    }
    assert rows[installed_id]["lifecycle"] == "registered"
    assert rows[downloaded_id]["lifecycle"] == "downloaded_unregistered"
    assert rows[failed_id]["lifecycle"] == "failed"
    assert rows[failed_id]["diagnostic_code"] == "download_failed"
    # Dismissing local history is presentation-only and must not erase live
    # authoritative inventory evidence.
    assert rows[hidden_id]["lifecycle"] == "registered"

    # A trashed ledger row is retained locally for audit, but the full wire
    # snapshot retires it by omission. Its exact stale configured target is
    # suppressed as well, so it cannot satisfy placement or appear installed.
    assert trashed_id not in rows
    assert all(
        row["aliases"] != ["disabled-local"]
        for row in document["installations"]
    )
    assert any(
        row["aliases"] == ["external-omlx"]
        for row in document["installations"]
    )
    await producer.close()


@pytest.mark.asyncio
async def test_one_catalog_snapshot_drives_service_and_conservative_runtime_rows(
    tmp_path,
) -> None:
    external = tmp_path / "external" / "models"
    external.mkdir(parents=True)
    runtime = _runtime(
        tmp_path,
        lexical_external=str(external),
        volume_uuid="VOL-CATALOG",
        scope_id="c" * 64,
        managed_destination=str(external / "managed"),
    )
    digest = "sha256:" + "9" * 64
    runtime.compatibility_catalog = FakeCatalog("catalog-2026.08", digest)
    producer = MacInventoryProducer(
        runtime,
        MacInventoryIndex(tmp_path / "state.db"),
        hardware_probe=FakeHardwareProbe(),
        wall_clock=lambda: 1_785_528_005.0,
        instance_id="12121212-1212-4212-8212-121212121212",
    )
    await producer.initialize()
    document = await producer.next_document(
        pairing_id="22222222-2222-4222-8222-222222222222",
        credential_generation=1,
    )

    assert document["service"]["catalog_version"] == "catalog-2026.08"
    assert document["service"]["catalog_digest"] == digest
    assert {row["catalog_digest"] for row in document["runtimes"]} == {digest}
    omlx = next(
        row for row in document["installations"] if row["aliases"] == ["external-omlx"]
    )
    # Version/fingerprint presence is useful evidence, but inventory does not
    # claim that an exact signed recipe has been matched on this Mac.
    assert omlx["runtime_compatibility"] == "compatible_unverified"
    disabled = next(
        row for row in document["installations"] if row["aliases"] == ["disabled-local"]
    )
    assert disabled["runtime_compatibility"] == "engine_disabled"
    await producer.close()


@pytest.mark.asyncio
async def test_inventory_ids_persist_while_process_instance_and_sequence_restart(
    tmp_path,
) -> None:
    external = tmp_path / "external" / "nested"
    external.mkdir(parents=True)
    kwargs = {
        "lexical_external": str(external),
        "volume_uuid": "VOL-A",
        "scope_id": "b" * 64,
        "managed_destination": str(external / "managed"),
    }
    first_runtime = _runtime(tmp_path, **kwargs)
    first = MacInventoryProducer(
        first_runtime,
        MacInventoryIndex(tmp_path / "state.db"),
        hardware_probe=FakeHardwareProbe(),
        wall_clock=lambda: 1_785_528_010.0,
        instance_id="33333333-3333-4333-8333-333333333333",
    )
    await first.initialize()
    first_doc = await first.next_document(
        pairing_id="22222222-2222-4222-8222-222222222222",
        credential_generation=1,
    )
    await first.close()

    restarted_runtime = _runtime(tmp_path, **kwargs)
    restarted = MacInventoryProducer(
        restarted_runtime,
        MacInventoryIndex(tmp_path / "state.db"),
        hardware_probe=FakeHardwareProbe(),
        wall_clock=lambda: 1_785_528_020.0,
        instance_id="44444444-4444-4444-8444-444444444444",
    )
    await restarted.initialize()
    restarted_doc = await restarted.next_document(
        pairing_id="22222222-2222-4222-8222-222222222222",
        credential_generation=1,
    )
    assert first_doc["inventory_sequence"] == restarted_doc["inventory_sequence"] == 1
    assert first_doc["inventory_instance_id"] != restarted_doc["inventory_instance_id"]
    assert {
        row["storage_location_id"]: row["binding_generation"]
        for row in first_doc["storage_locations"]
    } == {
        row["storage_location_id"]: row["binding_generation"]
        for row in restarted_doc["storage_locations"]
    }
    first_config_ids = {
        tuple(row["aliases"]): row["installation_id"]
        for row in first_doc["installations"]
        if row["source_kind"] != "managed_download"
    }
    restarted_config_ids = {
        tuple(row["aliases"]): row["installation_id"]
        for row in restarted_doc["installations"]
        if row["source_kind"] != "managed_download"
    }
    assert first_config_ids == restarted_config_ids
    await restarted.close()


@pytest.mark.asyncio
async def test_signed_install_provenance_projects_path_free_exact_reuse_identity(
    tmp_path,
) -> None:
    external = tmp_path / "external" / "models"
    external.mkdir(parents=True)
    runtime = _runtime(
        tmp_path,
        lexical_external=str(external),
        volume_uuid="VOL-SIGNED",
        scope_id="e" * 64,
        managed_destination=str(external / "managed"),
    )
    record = _signed_install_record(tmp_path)
    installer = FakeInstaller([record])
    runtime.installer = installer
    runtime.compatibility_catalog = FakeCatalog(
        "catalog-2026.08",
        "sha256:" + "2" * 64,
    )
    index = MacInventoryIndex(tmp_path / "state.db")
    producer = MacInventoryProducer(
        runtime,
        index,
        hardware_probe=FakeHardwareProbe(),
        wall_clock=lambda: 1_785_528_030.0,
        instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    await producer.initialize()
    binding = (await index.active_storage_bindings())["internal"]
    provenance = _signed_install_provenance(record, binding)
    installer.provenances[record.id] = provenance

    document = await producer.next_document(
        pairing_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        credential_generation=1,
    )
    row = next(
        item
        for item in document["installations"]
        if item["installation_id"] == record.id
    )

    assert row["logical_model_id"] == "model:signed-omlx"
    assert row["artifact_id"] == "artifact:signed-omlx-4bit"
    assert row["recipe_id"] == "recipe:signed-omlx-4bit"
    assert row["deployment_id"] is None
    assert row["identity_confidence"] == "authoritative"
    assert row["ownership_class"] == "exclusive_managed"
    assert row["verification"] == {
        "state": "digest_verified",
        "evidence_class": "measured",
        "verified_at": record.updated_at,
    }
    assert row["storage_location_id"] == binding.storage_location_id
    assert row["storage_binding_generation"] == binding.binding_generation

    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    for private_value in (
        binding.exact_path,
        record.destination,
        provenance.catalog_id,
        provenance.source_identity_digest,
        provenance.manifest_digest,
    ):
        assert private_value not in encoded

    schema = json.loads(
        (PROTOCOL_V1 / "mac_inventory.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(document)
    await producer.close()


@pytest.mark.parametrize(
    "proof_failure",
    (
        "missing",
        "malformed",
        "binding_mismatch",
        "immutable_mismatch",
        "legacy_expected_missing",
    ),
)
@pytest.mark.asyncio
async def test_signed_install_provenance_failures_remain_unverified(
    tmp_path,
    proof_failure: str,
) -> None:
    external = tmp_path / "external" / "models"
    external.mkdir(parents=True)
    runtime = _runtime(
        tmp_path,
        lexical_external=str(external),
        volume_uuid="VOL-FAIL-CLOSED",
        scope_id="f" * 64,
        managed_destination=str(external / "managed"),
    )
    record = _signed_install_record(tmp_path)
    if proof_failure == "legacy_expected_missing":
        record = replace(
            record,
            expected_files_json=None,
            expected_manifest_digest=None,
        )
    installer = FakeInstaller([record])
    runtime.installer = installer
    index = MacInventoryIndex(tmp_path / "state.db")
    producer = MacInventoryProducer(
        runtime,
        index,
        hardware_probe=FakeHardwareProbe(),
        wall_clock=lambda: 1_785_528_040.0,
        instance_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    )
    await producer.initialize()
    binding = (await index.active_storage_bindings())["internal"]
    provenance = _signed_install_provenance(record, binding)
    if proof_failure == "malformed":
        installer.provenances[record.id] = replace(
            provenance,
            manifest_digest="sha256:" + "f" * 64,
        )
    elif proof_failure == "binding_mismatch":
        stale_binding = replace(
            binding,
            binding_generation=binding.binding_generation + 1,
        )
        installer.provenances[record.id] = _signed_install_provenance(
            record,
            stale_binding,
        )
    elif proof_failure == "immutable_mismatch":
        installer.provenances[record.id] = provenance
        installer.authority_records[record.id] = replace(
            record,
            alias="changed-after-evidence",
        )
    elif proof_failure == "legacy_expected_missing":
        installer.provenances[record.id] = provenance

    document = await producer.next_document(
        pairing_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        credential_generation=1,
    )
    row = next(
        item
        for item in document["installations"]
        if item["installation_id"] == record.id
    )

    assert row["logical_model_id"] is None
    assert row["artifact_id"] is None
    assert row["recipe_id"] is None
    assert row["deployment_id"] is None
    assert row["identity_confidence"] == "unverified"
    assert row["ownership_class"] == "unknown"
    assert row["verification"]["state"] != "digest_verified"
    await producer.close()
