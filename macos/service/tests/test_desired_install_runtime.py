from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from mnemosyne_macos.config import DS4Config, LlamaCppConfig, MFluxConfig, OMLXConfig
from mnemosyne_macos.desired_install_runtime import (
    DEFAULT_STORAGE_RESERVE_BYTES,
    NativeDesiredInstallAuthorities,
)
from mnemosyne_macos.fleet_pairing import PairingState
from mnemosyne_macos.mac_inventory_store import StorageBinding
from mnemosyne_macos.models import EngineName, EngineSnapshot, ServiceState
import mnemosyne_macos.runtime_updates as runtime_updates
from mnemosyne_macos.runtime_updates import RuntimeUpdateManager
from mnemosyne_macos.storage import StorageStatus


PAIRING_ID = "11111111-1111-4111-8111-111111111111"
INVENTORY_INSTANCE_ID = "22222222-2222-4222-8222-222222222222"
STORAGE_LOCATION_ID = "33333333-3333-4333-8333-333333333333"
CATALOG_DIGEST = "sha256:" + "a" * 64
COMPATIBILITY_FINGERPRINT = "sha256:" + "c" * 64
LEXICAL_ROOT = "/Volumes/Models Link/nested/symlink-models"


class FakePairingStore:
    def __init__(self, record: Any) -> None:
        self.record = record

    async def status(self) -> Any:
        if isinstance(self.record, BaseException):
            raise self.record
        return self.record


class FakeInventorySync:
    def __init__(self, inspection: Any) -> None:
        self.value = inspection

    async def inspection(self) -> Any:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class FakeInventoryIndex:
    def __init__(self, binding: StorageBinding | None) -> None:
        self.binding = binding
        self.calls: list[tuple[str, int]] = []

    async def resolve_storage(
        self,
        storage_location_id: str,
        binding_generation: int,
    ) -> StorageBinding | None:
        self.calls.append((storage_location_id, binding_generation))
        return self.binding


class FakeFilesystem:
    def __init__(self, result: StorageStatus | BaseException) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def inspect(self, path: str, **kwargs: Any) -> StorageStatus:
        self.calls.append({"path": path, **kwargs})
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeCatalog:
    source = "signed"
    catalog_digest = CATALOG_DIGEST

    def __init__(self, *, engine: str = "llama.cpp", tier: str = "stable") -> None:
        self.engine = engine
        self.tier = tier

    def catalog(self) -> dict[str, object]:
        return {
            "recipes": [
                {
                    "runtime": {
                        "engine": self.engine,
                        "release_tier": self.tier,
                        "required_features": [
                            "flash-attention",
                            "apple-metal",
                        ],
                    }
                },
                {
                    # A different engine cannot donate feature authority.
                    "runtime": {
                        "engine": "omlx",
                        "release_tier": "stable",
                        "required_features": ["omlx-only"],
                    }
                },
            ]
        }


class FakeCatalogRuntime:
    def __init__(self, catalog: Any) -> None:
        self.value = catalog

    async def snapshot(self) -> Any:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class FakeRuntimeUpdates:
    def __init__(self, status: Any) -> None:
        self.value = status

    async def installed_status(self) -> Any:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class FakeAdapter:
    def __init__(
        self,
        *,
        engine: EngineName,
        state: ServiceState,
        authoritative: bool = True,
        fingerprint: str | None = "current-runtime",
        delay: float = 0,
        scheduler_slots: int = 2,
        memory_guard_enabled: bool = True,
    ) -> None:
        self.engine = engine
        self.state = state
        self.authoritative = authoritative
        self.fingerprint = fingerprint
        self.delay = delay
        self.scheduler_slots = scheduler_slots
        self.memory_guard_enabled = memory_guard_enabled
        self.inspect_calls = 0
        self.fingerprint_calls = 0
        self.launch_contract_calls = 0
        self.load_calls = 0

    async def inspect(self, *, deadline: Any) -> EngineSnapshot:
        del deadline
        self.inspect_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return EngineSnapshot(
            engine=self.engine,
            residents=(),
            authoritative=self.authoritative,
            service_state=self.state,
        )

    async def runtime_fingerprint(self, *, deadline: Any) -> str | None:
        del deadline
        self.fingerprint_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.fingerprint

    async def inspect_launch_contract(self, *, deadline: Any) -> Any:
        del deadline
        self.launch_contract_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(
            scheduler_slots=self.scheduler_slots,
            memory_guard_enabled=self.memory_guard_enabled,
        )

    async def load(self, *_args: Any, **_kwargs: Any) -> None:
        self.load_calls += 1
        raise AssertionError("authority inspection must never load an engine")


class FakeConfig:
    def __init__(self, location: Any, *, enabled: bool = True) -> None:
        self.storage = SimpleNamespace(locations=[location])
        self.enabled = enabled

    def engine_enabled(self, _engine: EngineName) -> bool:
        return self.enabled


def _pairing_record(**changes: Any) -> SimpleNamespace:
    values = {
        "state": PairingState.PAIRED,
        "pairing_id": PAIRING_ID,
        "credential_epoch": 7,
        "credentials_owned": True,
        "credential_write_pending": False,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _inventory_document(**changes: Any) -> dict[str, object]:
    value: dict[str, object] = {
        "inventory_instance_id": INVENTORY_INSTANCE_ID,
        "inventory_sequence": 12,
        "pairing_id": PAIRING_ID,
        "credential_generation": 7,
    }
    value.update(changes)
    return value


def _inventory_inspection(**document_changes: Any) -> dict[str, object]:
    return {
        "sync": {"inventory_instance_id": INVENTORY_INSTANCE_ID},
        "inventory": _inventory_document(**document_changes),
    }


def _location(**changes: Any) -> SimpleNamespace:
    values = {
        "name": "external-models",
        "path": LEXICAL_ROOT,
        "volume_uuid": "volume-a",
        "scope_id": "f" * 64,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _binding(**changes: Any) -> StorageBinding:
    values = {
        "local_key": "external-models",
        "storage_location_id": STORAGE_LOCATION_ID,
        "binding_generation": 4,
        "exact_path": LEXICAL_ROOT,
        "volume_uuid": "volume-a",
        "scope_id": "f" * 64,
    }
    values.update(changes)
    return StorageBinding(**values)


def _storage_status(**changes: Any) -> StorageStatus:
    values = {
        "name": "external-models",
        "path": LEXICAL_ROOT,
        "exists": True,
        "is_directory": True,
        "writable": True,
        "mount_path": "/Volumes/Models",
        "volume_uuid": "VOLUME-A",
        "expected_volume_uuid": "volume-a",
        "volume_matches": True,
        "total_bytes": 100 * 1024**3,
        "free_bytes": 80 * 1024**3,
        "diagnostic": None,
    }
    values.update(changes)
    return StorageStatus(**values)


def _runtime(
    *,
    location: Any | None = None,
    binding: StorageBinding | None = None,
    filesystem_result: StorageStatus | BaseException | None = None,
    engine: EngineName = EngineName.LLAMA_CPP,
    adapter: FakeAdapter | None = None,
    enabled: bool = True,
    installed: Any | None = None,
    catalog: Any | None = None,
    inventory: Any | None = None,
    pairing: Any | None = None,
) -> SimpleNamespace:
    location = location or _location()
    adapter = adapter or FakeAdapter(engine=engine, state=ServiceState.STOPPED)
    installed = installed or {
        engine.value: {
            "installed": True,
            "version": "b6500",
            "revision": None,
            "features": ["flash-attention", "apple-metal"],
            "compatibility_fingerprint": COMPATIBILITY_FINGERPRINT,
            # Deliberately path-bearing; it must never enter RuntimeAuthority.
            "path": "/private/runtime/path",
        }
    }
    return SimpleNamespace(
        fleet_pairing=FakePairingStore(pairing or _pairing_record()),
        mac_inventory=SimpleNamespace(instance_id=INVENTORY_INSTANCE_ID),
        mac_inventory_sync=FakeInventorySync(
            inventory if inventory is not None else _inventory_inspection()
        ),
        mac_inventory_index=FakeInventoryIndex(binding or _binding()),
        config=FakeConfig(location, enabled=enabled),
        filesystem=FakeFilesystem(filesystem_result or _storage_status()),
        compatibility_catalog=FakeCatalogRuntime(catalog or FakeCatalog()),
        runtime_updates=FakeRuntimeUpdates(installed),
        adapters={engine: adapter},
        # The adapter must not trust this cached value.
        _runtime_fingerprints={engine: "stale-cached-runtime"},
    )


@pytest.mark.asyncio
async def test_pairing_requires_complete_owned_active_generation() -> None:
    runtime = _runtime()
    providers = NativeDesiredInstallAuthorities(runtime)

    current = await providers.current_pairing()

    assert current.active is True
    assert current.pairing_id == PAIRING_ID
    assert current.credential_generation == 7

    runtime.fleet_pairing.record = _pairing_record(credential_write_pending=True)
    assert (await providers.current_pairing()).active is False
    runtime.fleet_pairing.record = RuntimeError("/Users/alice/private/.env")
    unavailable = await providers.current_pairing()
    assert unavailable.active is False
    assert unavailable.pairing_id is None


@pytest.mark.asyncio
async def test_inventory_uses_latest_local_sequence_and_same_pairing() -> None:
    runtime = _runtime(
        inventory=_inventory_inspection(inventory_sequence=19),
    )
    providers = NativeDesiredInstallAuthorities(runtime)

    current = await providers.current_inventory()

    assert current.inventory_instance_id == INVENTORY_INSTANCE_ID
    assert current.inventory_sequence == 19

    runtime.mac_inventory_sync.value = _inventory_inspection(
        inventory_sequence=20,
        credential_generation=6,
    )
    stale = await providers.current_inventory()
    assert stale.inventory_instance_id == INVENTORY_INSTANCE_ID
    assert stale.inventory_sequence == 0


@pytest.mark.asyncio
async def test_storage_preserves_exact_nested_symlink_spelling_and_scope() -> None:
    runtime = _runtime()
    providers = NativeDesiredInstallAuthorities(runtime)

    authority = await providers.resolve_storage(STORAGE_LOCATION_ID, 4)

    assert authority is not None
    assert authority.indexed_lexical_root == LEXICAL_ROOT
    assert authority.configured_lexical_root == LEXICAL_ROOT
    assert authority.observed_lexical_root == LEXICAL_ROOT
    assert authority.configured_scope_id == "f" * 64
    assert authority.observed_volume_uuid == "VOLUME-A"
    assert authority.availability == "available"
    assert authority.free_bytes == 80 * 1024**3
    assert authority.reserve_bytes == DEFAULT_STORAGE_RESERVE_BYTES
    assert authority.remote_installs_allowed is True
    assert runtime.filesystem.calls == [
        {
            "path": LEXICAL_ROOT,
            "name": "external-models",
            "expected_volume_uuid": "volume-a",
            "scope_id": "f" * 64,
            "scope_path": LEXICAL_ROOT,
        }
    ]


@pytest.mark.asyncio
async def test_stale_storage_binding_never_probes_new_configured_path() -> None:
    new_root = "/Volumes/New Disk/models"
    runtime = _runtime(location=_location(path=new_root))
    providers = NativeDesiredInstallAuthorities(runtime)

    authority = await providers.resolve_storage(STORAGE_LOCATION_ID, 4)

    assert authority is not None
    assert authority.indexed_lexical_root == LEXICAL_ROOT
    assert authority.configured_lexical_root == new_root
    assert authority.observed_lexical_root == ""
    assert runtime.filesystem.calls == []


@pytest.mark.asyncio
async def test_wrong_volume_and_probe_failure_fail_closed_without_fallback() -> None:
    runtime = _runtime(
        filesystem_result=_storage_status(
            volume_uuid="volume-b",
            volume_matches=False,
        )
    )
    providers = NativeDesiredInstallAuthorities(runtime)

    remounted = await providers.resolve_storage(STORAGE_LOCATION_ID, 4)

    assert remounted is not None
    assert remounted.availability == "wrong_volume"
    assert remounted.volume_matches is False

    runtime.filesystem.result = RuntimeError(
        "permission denied at /Volumes/Models Link/nested/symlink-models"
    )
    unavailable = await providers.resolve_storage(STORAGE_LOCATION_ID, 4)
    assert unavailable is not None
    assert unavailable.availability == "unavailable"
    assert unavailable.free_bytes is None
    assert unavailable.writable is False


@pytest.mark.asyncio
async def test_unknown_or_retired_storage_id_returns_none() -> None:
    runtime = _runtime()
    runtime.mac_inventory_index.binding = None
    providers = NativeDesiredInstallAuthorities(runtime)

    assert await providers.resolve_storage(STORAGE_LOCATION_ID, 4) is None


@pytest.mark.asyncio
async def test_manager_owned_stopped_runtime_is_authoritative_and_never_loaded() -> None:
    adapter = FakeAdapter(
        engine=EngineName.LLAMA_CPP,
        state=ServiceState.STOPPED,
        fingerprint="current-runtime",
    )
    runtime = _runtime(adapter=adapter)
    providers = NativeDesiredInstallAuthorities(runtime)

    authority = await providers.current_runtime("llama.cpp", CATALOG_DIGEST)

    assert authority is not None
    assert authority.enabled is True
    assert authority.healthy is True
    assert authority.version == "b6500"
    assert authority.release_tier == "stable"
    assert authority.runtime_fingerprint == COMPATIBILITY_FINGERPRINT
    assert authority.features == ("apple-metal", "flash-attention")
    assert authority.catalog_digest == CATALOG_DIGEST
    assert adapter.inspect_calls == 1
    assert adapter.fingerprint_calls == 0
    assert adapter.load_calls == 0
    assert "/private/runtime/path" not in repr(authority)


@pytest.mark.asyncio
async def test_managed_runtime_compatibility_evidence_flows_to_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtimes"
    version = "b6500"
    runtime_root = root / "llama.cpp" / version
    runtime_files = runtime_root / "runtime"
    runtime_files.mkdir(parents=True)
    binary = runtime_files / "llama-server"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    source_payload = b"official llama.cpp artifact fixture"
    source_digest = hashlib.sha256(source_payload).hexdigest()
    release = runtime_updates.RuntimeRelease(
        engine="llama.cpp",
        version=version,
        source_revision="main",
        source_url=(
            "https://github.com/ggml-org/llama.cpp/releases/download/"
            f"{version}/llama-{version}-bin-macos-arm64.tar.gz"
        ),
        release_notes_url=(
            f"https://github.com/ggml-org/llama.cpp/releases/tag/{version}"
        ),
        sha256=source_digest,
        asset_size=len(source_payload),
    )
    evidence = runtime_updates._new_runtime_compatibility_evidence(
        release,
        source_sha256=f"sha256:{source_digest}",
        source_size_bytes=len(source_payload),
        executable=binary,
    )
    (runtime_root / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "llama.cpp",
                "version": version,
                "source_revision": "main",
                "source_sha256": source_digest,
                "source_size_bytes": len(source_payload),
                "core_protocol": 1,
                "entrypoint": {
                    "binary": "runtime/llama-server",
                    "working_directory": "runtime",
                },
                "compatibility_evidence": evidence.to_manifest(),
            }
        ),
        encoding="utf-8",
    )
    (root / "llama.cpp" / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "previous_version": None,
                "activated_at": "2026-08-30T00:00:00+00:00",
                "local_integrity_fingerprint": (
                    evidence.local_integrity_fingerprint
                ),
            }
        ),
        encoding="utf-8",
    )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(404))
    )
    manager = RuntimeUpdateManager(
        llama_cpp=LlamaCppConfig(binary=str(tmp_path / "missing-llama")),
        omlx=OMLXConfig(base_url="http://127.0.0.1:17322"),
        mflux=MFluxConfig(),
        ds4=DS4Config(binary=str(tmp_path / "missing-ds4")),
        root=root,
        client=client,
    )
    adapter = FakeAdapter(engine=EngineName.LLAMA_CPP, state=ServiceState.STOPPED)
    runtime = _runtime(adapter=adapter)
    runtime.runtime_updates = manager
    providers = NativeDesiredInstallAuthorities(runtime)
    try:
        manager_status = await manager.installed_status()
        authority = await providers.current_runtime("llama.cpp", CATALOG_DIGEST)
    finally:
        await manager.aclose()
        await client.aclose()

    assert manager_status["llama.cpp"]["compatibility_fingerprint"] == (
        evidence.compatibility_fingerprint
    )
    assert manager_status["llama.cpp"]["features"] == [
        "apple-metal",
        "flash-attention",
    ]
    assert authority is not None
    assert authority.runtime_fingerprint == evidence.compatibility_fingerprint
    assert authority.features == ("apple-metal", "flash-attention")
    assert str(root) not in repr(authority)
    assert adapter.fingerprint_calls == 0
    assert adapter.load_calls == 0


@pytest.mark.asyncio
async def test_omlx_must_be_ready_while_manager_owned_runtime_may_be_stopped() -> None:
    adapter = FakeAdapter(engine=EngineName.OMLX, state=ServiceState.STOPPED)
    runtime = _runtime(
        engine=EngineName.OMLX,
        adapter=adapter,
        installed={
            "omlx": {
                "installed": True,
                "version": "1.2.3",
                "revision": None,
                "path": "/Applications/oMLX.app",
            }
        },
        catalog=FakeCatalog(engine="omlx", tier="stable"),
    )
    providers = NativeDesiredInstallAuthorities(runtime)

    stopped = await providers.current_runtime("omlx", CATALOG_DIGEST)
    assert stopped is not None
    assert stopped.healthy is False

    adapter.state = ServiceState.READY
    ready = await providers.current_runtime("omlx", CATALOG_DIGEST)
    assert ready is not None
    assert ready.healthy is True
    assert ready.runtime_fingerprint is None
    assert ready.features == ()
    assert ready.omlx_scheduler_slots == 2
    assert ready.omlx_memory_guard_enabled is True
    assert adapter.launch_contract_calls == 2
    assert adapter.load_calls == 0


@pytest.mark.asyncio
async def test_runtime_rejects_unsigned_or_changed_catalog_before_inspection() -> None:
    adapter = FakeAdapter(engine=EngineName.LLAMA_CPP, state=ServiceState.STOPPED)
    catalog = FakeCatalog()
    catalog.source = "built_in"
    runtime = _runtime(adapter=adapter, catalog=catalog)
    providers = NativeDesiredInstallAuthorities(runtime)

    assert await providers.current_runtime("llama.cpp", CATALOG_DIGEST) is None
    assert adapter.inspect_calls == 0

    catalog.source = "signed"
    assert await providers.current_runtime(
        "llama.cpp", "sha256:" + "b" * 64
    ) is None
    assert adapter.inspect_calls == 0


@pytest.mark.asyncio
async def test_runtime_disabled_missing_nonauthoritative_and_timeout_are_unhealthy() -> None:
    adapter = FakeAdapter(
        engine=EngineName.LLAMA_CPP,
        state=ServiceState.STOPPED,
        authoritative=False,
    )
    runtime = _runtime(adapter=adapter, enabled=False)
    providers = NativeDesiredInstallAuthorities(runtime)
    disabled = await providers.current_runtime("llama.cpp", CATALOG_DIGEST)
    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.healthy is False
    assert adapter.inspect_calls == 0

    runtime.config.enabled = True
    runtime.runtime_updates.value = {
        "llama.cpp": {"installed": False, "version": None, "revision": None}
    }
    missing = await providers.current_runtime("llama.cpp", CATALOG_DIGEST)
    assert missing is not None
    assert missing.healthy is False
    assert adapter.inspect_calls == 0

    runtime.runtime_updates.value = {
        "llama.cpp": {"installed": True, "version": "b6500", "revision": None}
    }
    nonauthoritative = await providers.current_runtime(
        "llama.cpp", CATALOG_DIGEST
    )
    assert nonauthoritative is not None
    assert nonauthoritative.healthy is False

    adapter.authoritative = True
    adapter.delay = 0.05
    bounded = NativeDesiredInstallAuthorities(
        runtime,
        authority_timeout_seconds=0.01,
    )
    timed_out = await bounded.current_runtime("llama.cpp", CATALOG_DIGEST)
    assert timed_out is not None
    assert timed_out.healthy is False
    assert adapter.load_calls == 0


@pytest.mark.asyncio
async def test_ds4_revision_is_used_only_when_bounded_version_is_absent() -> None:
    adapter = FakeAdapter(engine=EngineName.DS4, state=ServiceState.STOPPED)
    runtime = _runtime(
        engine=EngineName.DS4,
        adapter=adapter,
        installed={
            "ds4": {
                "installed": True,
                "version": None,
                "revision": "abcdef123456",
                "features": [],
                "compatibility_fingerprint": COMPATIBILITY_FINGERPRINT,
                "path": "/private/ds4",
            }
        },
        catalog=FakeCatalog(engine="ds4", tier="preview"),
    )
    providers = NativeDesiredInstallAuthorities(runtime)

    authority = await providers.current_runtime("ds4", CATALOG_DIGEST)

    assert authority is not None
    assert authority.healthy is True
    assert authority.release_tier == "preview"
    assert authority.version == "git:abcdef123456"
    assert adapter.load_calls == 0


@pytest.mark.asyncio
async def test_legacy_manager_owned_runtime_stays_usable_but_has_no_install_authority() -> None:
    adapter = FakeAdapter(engine=EngineName.LLAMA_CPP, state=ServiceState.STOPPED)
    runtime = _runtime(
        adapter=adapter,
        installed={
            "llama.cpp": {
                "installed": True,
                "version": "b6500",
                "revision": None,
                "features": [],
                "compatibility_fingerprint": None,
                "path": "/private/legacy-llama",
            }
        },
    )
    providers = NativeDesiredInstallAuthorities(runtime)

    authority = await providers.current_runtime("llama.cpp", CATALOG_DIGEST)

    assert authority is not None
    assert authority.enabled is True
    assert authority.version == "b6500"
    assert authority.runtime_fingerprint is None
    assert authority.features == ()
    assert authority.healthy is False
    assert adapter.inspect_calls == 1
    assert adapter.load_calls == 0
