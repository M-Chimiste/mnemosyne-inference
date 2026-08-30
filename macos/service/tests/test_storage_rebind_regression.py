from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from typing import Any

import pytest

from mnemosyne_macos.config import MacConfig, ModelProfile, load_config, save_config
from mnemosyne_macos.filesystem import FilesystemProbe
import mnemosyne_macos.fs_worker as fs_worker_module
import mnemosyne_macos.installer as installer_module
from mnemosyne_macos.runtime import NativeRuntime
from mnemosyne_macos.storage import inspect_path, install_destination
from mnemosyne_macos.models import EngineName


class _BlockedDownload:
    pid = 43821
    returncode = 0

    def __init__(
        self,
        *,
        destination: Path,
        filename: str,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.destination = destination
        self.filename = filename
        self.started = started
        self.release = release

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        await self.release.wait()
        (self.destination / self.filename).write_bytes(b"GGUFexact-old-location")
        return b'{"status":"complete"}', b""


def _config(
    *,
    database: Path,
    storage_root: Path,
    volume_uuid: str | None,
    scope_id: str | None = None,
) -> MacConfig:
    return MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"llama_cpp": {"enabled": True}},
            "paths": {"state_database": str(database)},
            "token_sidecar": {"enabled": False},
            "storage": {
                "default": "selected",
                "locations": [
                    {
                        "name": "selected",
                        "path": str(storage_root),
                        "volume_uuid": volume_uuid,
                        "scope_id": scope_id,
                    }
                ],
            },
        }
    )


def _direct_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FilesystemProbe:
    probe = FilesystemProbe(
        scope_root=tmp_path / "state" / "security-scopes",
        timeout_seconds=5,
    )

    async def run_worker(
        *arguments: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        parsed = fs_worker_module._parser().parse_args(arguments)
        return {"ok": True, **fs_worker_module._run(parsed)}

    monkeypatch.setattr(probe, "_run", run_worker)
    return probe


@pytest.mark.asyncio
async def test_rebind_during_active_install_cannot_register_old_bytes_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_root = (
        tmp_path / "Volumes" / "Athena" / "nested" / "user-selected-weights"
    )
    physical_root.mkdir(parents=True)
    selected_root = tmp_path / "Library" / "Model Locations" / "athena-link"
    selected_root.parent.mkdir(parents=True)
    selected_root.symlink_to(physical_root, target_is_directory=True)
    replacement_root = tmp_path / "replacement-must-stay-empty"
    replacement_root.mkdir()
    volume_uuid = inspect_path(selected_root).volume_uuid
    database = tmp_path / "state" / "mnemosyne.db"
    config_path = tmp_path / "settings" / "config.yaml"
    config = _config(
        database=database,
        storage_root=selected_root,
        volume_uuid=volume_uuid,
    )
    save_config(config, config_path)

    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters={},
        filesystem_probe=_direct_probe(tmp_path, monkeypatch),
    )
    await runtime.start(raise_on_degraded=True)
    started = asyncio.Event()
    release = asyncio.Event()
    spawn_calls: list[tuple[str, ...]] = []
    filename = "model-Q4_K_M.gguf"

    async def create_process(*argv: str, **_kwargs: Any) -> _BlockedDownload:
        spawn_calls.append(argv)
        destination = Path(argv[argv.index("--destination") + 1])
        return _BlockedDownload(
            destination=destination,
            filename=filename,
            started=started,
            release=release,
        )

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    destination = install_destination(
        selected_root,
        EngineName.LLAMA_CPP,
        "owner/model-GGUF",
    )
    try:
        install = await runtime.installer.create(
            repo_id="owner/model-GGUF",
            engine=EngineName.LLAMA_CPP.value,
            storage="selected",
            alias="selected-model",
            destination=str(destination),
            revision="a" * 40,
            filename=filename,
            family=None,
            total_bytes=None,
        )
        await asyncio.wait_for(started.wait(), timeout=10)
        claim = runtime.installer.store.get_creation_claim(install.id)
        assert claim is not None
        assert claim.storage_lexical_root == str(selected_root)
        assert claim.lexical_destination == str(destination)

        rebound = _config(
            database=database,
            storage_root=replacement_root,
            volume_uuid=volume_uuid,
        )
        _persisted, expected_revision, _applied_revision, _pending = (
            await runtime.configuration_snapshot()
        )
        restart_required, _revision = await runtime.save_configuration(
            rebound,
            expected_revision=expected_revision,
        )
        assert restart_required is True
        assert runtime.config.storage == config.storage
        assert load_config(config_path).storage == rebound.storage

        release.set()
        await asyncio.wait_for(runtime.installer._tasks[install.id], timeout=10)
        completed = await runtime.installer.get(install.id)
        assert completed.status == "downloaded"
        assert "registration_storage_binding_changed" in (completed.error or "")
        assert load_config(config_path).models == []
        lexical_weight = destination / filename
        physical_weight = (
            physical_root
            / "llama.cpp"
            / "owner"
            / "model-GGUF"
            / filename
        )
        before = physical_weight.stat()
        assert lexical_weight.read_bytes() == b"GGUFexact-old-location"
        assert (lexical_weight.stat().st_dev, lexical_weight.stat().st_ino) == (
            before.st_dev,
            before.st_ino,
        )
        assert not (replacement_root / "llama.cpp").exists()
        assert len(spawn_calls) == 1
    finally:
        release.set()
        await runtime.stop()

    restarted_config = load_config(config_path)
    restarted = NativeRuntime(
        restarted_config,
        config_path=config_path,
        adapters={},
        filesystem_probe=_direct_probe(tmp_path, monkeypatch),
    )
    await restarted.start(raise_on_degraded=True)
    try:
        assert restarted.profiles == {}
        retrying = await restarted.installer.retry(install.id)
        assert retrying.status == "registering"
        await asyncio.wait_for(restarted.installer._tasks[install.id], timeout=10)

        retained = await restarted.installer.get(install.id)
        assert retained.status == "downloaded"
        assert "registration_storage_binding_changed" in (retained.error or "")
        assert load_config(config_path).models == []
        assert (destination / filename).read_bytes() == b"GGUFexact-old-location"
        assert not (replacement_root / "llama.cpp").exists()
        assert len(spawn_calls) == 1
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_legacy_downloaded_install_cannot_adopt_replaced_volume_or_scope_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "Volumes" / "Same Lexical Mount"
    destination = storage_root / "llama.cpp" / "owner" / "legacy-model"
    destination.mkdir(parents=True)
    filename = "legacy-Q4_K_M.gguf"
    weight = destination / filename
    weight.write_bytes(b"GGUFlegacy-old-binding")
    database = tmp_path / "state" / "mnemosyne.db"
    config_path = tmp_path / "settings" / "config.yaml"
    old_config = _config(
        database=database,
        storage_root=storage_root,
        volume_uuid="OLD-EXTERNAL-VOLUME",
        scope_id="a" * 64,
    )
    save_config(old_config, config_path)

    # Model a pre-creation-claim row left at the retryable post-download phase.
    prior = installer_module.NativeInstaller(database, storage=old_config.storage)
    legacy = prior.store.create(
        repo_id="owner/legacy-model",
        engine=EngineName.LLAMA_CPP.value,
        storage="selected",
        alias="legacy-model",
        destination=str(destination),
        revision="b" * 40,
        filename=filename,
        family=None,
        total_bytes=weight.stat().st_size,
    )
    prior.store.update(
        legacy.id,
        status="downloaded",
        bytes_downloaded=weight.stat().st_size,
    )
    await prior.stop()

    rebound = _config(
        database=database,
        storage_root=storage_root,
        volume_uuid="REPLACEMENT-EXTERNAL-VOLUME",
        scope_id="b" * 64,
    )
    save_config(rebound, config_path)
    restarted = NativeRuntime(
        rebound,
        config_path=config_path,
        adapters={},
        filesystem_probe=_direct_probe(tmp_path, monkeypatch),
    )

    async def skip_scope_activation() -> None:
        return None

    monkeypatch.setattr(
        restarted,
        "_activate_configured_security_scopes",
        skip_scope_activation,
    )
    await restarted.start(raise_on_degraded=True)

    async def unexpected_spawn(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a downloaded registration retry must not redownload")

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        unexpected_spawn,
    )
    try:
        retrying = await restarted.installer.retry(legacy.id)
        assert retrying.status == "registering"
        await asyncio.wait_for(restarted.installer._tasks[legacy.id], timeout=10)

        retained = await restarted.installer.get(legacy.id)
        assert retained.status == "downloaded"
        assert "registration_storage_binding_changed" in (retained.error or "")
        assert load_config(config_path).models == []
        assert weight.read_bytes() == b"GGUFlegacy-old-binding"
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_existing_ordinary_omlx_profile_must_match_install_storage_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_root = tmp_path / "selected"
    other_root = tmp_path / "other"
    selected_root.mkdir()
    other_model = other_root / "omlx" / "owner" / "model"
    other_model.mkdir(parents=True)
    (other_model / "config.json").write_text(
        '{"architectures":["ExampleForCausalLM"]}',
        encoding="utf-8",
    )
    (other_model / "model.safetensors").write_bytes(b"other-location")
    database = tmp_path / "state" / "mnemosyne.db"
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "server": {"idle_unload_seconds": None},
            "engines": {"omlx": {"enabled": True}},
            "paths": {"state_database": str(database)},
            "token_sidecar": {"enabled": False},
            "storage": {
                "default": "selected",
                "locations": [
                    {"name": "selected", "path": str(selected_root)},
                    {"name": "other", "path": str(other_root)},
                ],
            },
        }
    )
    save_config(config, config_path)
    runtime = NativeRuntime(
        config,
        config_path=config_path,
        adapters={},
        filesystem_probe=_direct_probe(tmp_path, monkeypatch),
    )
    await runtime.start(raise_on_degraded=True)
    started = asyncio.Event()
    release = asyncio.Event()
    filename = "model.safetensors"

    async def create_process(*argv: str, **_kwargs: Any) -> _BlockedDownload:
        destination = Path(argv[argv.index("--destination") + 1])
        return _BlockedDownload(
            destination=destination,
            filename=filename,
            started=started,
            release=release,
        )

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    destination = install_destination(
        selected_root,
        EngineName.OMLX,
        "owner/model",
    )
    try:
        install = await runtime.installer.create(
            repo_id="owner/model",
            engine=EngineName.OMLX.value,
            storage="selected",
            alias="downloaded-model",
            destination=str(destination),
            revision="c" * 40,
            filename=filename,
            family=None,
            total_bytes=None,
        )
        await asyncio.wait_for(started.wait(), timeout=10)
        (destination / "config.json").write_text(
            '{"architectures":["ExampleForCausalLM"]}',
            encoding="utf-8",
        )

        conflicting = config.model_copy(
            update={
                "models": [
                    ModelProfile(
                        alias="downloaded-model",
                        engine=EngineName.OMLX,
                        model="model",
                        storage="other",
                    )
                ]
            }
        )
        _persisted, expected_revision, _applied, _pending = (
            await runtime.configuration_snapshot()
        )
        restart_required, _revision = await runtime.save_configuration(
            conflicting,
            expected_revision=expected_revision,
        )
        assert restart_required is False

        release.set()
        await asyncio.wait_for(runtime.installer._tasks[install.id], timeout=10)
        retained = await runtime.installer.get(install.id)
        assert retained.status == "downloaded"
        assert "different storage binding" in (retained.error or "")
        persisted = load_config(config_path)
        assert persisted.models[0].storage == "other"
        assert (destination / filename).read_bytes() == b"GGUFexact-old-location"
        assert (other_model / "model.safetensors").read_bytes() == b"other-location"
    finally:
        release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_concurrent_legacy_registration_retries_cannot_downgrade_installed_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "selected"
    storage_root.mkdir()
    database = tmp_path / "state" / "mnemosyne.db"
    storage = _config(
        database=database,
        storage_root=storage_root,
        volume_uuid=None,
    ).storage
    installer = installer_module.NativeInstaller(database, storage=storage)
    destination = install_destination(
        storage_root,
        EngineName.LLAMA_CPP,
        "owner/legacy-model",
    )
    record = installer.store.create(
        repo_id="owner/legacy-model",
        engine=EngineName.LLAMA_CPP.value,
        storage="selected",
        alias="legacy-model",
        destination=str(destination),
        revision="d" * 40,
        filename="legacy.gguf",
        family=None,
        total_bytes=1,
    )
    installer.store.update(record.id, status="downloaded", bytes_downloaded=1)
    installer._registration_storage_bindings[record.id] = (
        installer_module._RegistrationStorageBinding(
            storage_name="selected",
            lexical_root=str(storage_root),
            volume_uuid=None,
            scope_id=None,
            lexical_destination=str(destination),
            resolved_revision="d" * 40,
        )
    )

    registration_attempts = 0

    async def register_profile(current: Any) -> None:
        nonlocal registration_attempts
        registration_attempts += 1
        await installer.require_registration_storage_binding(current, storage)

    installer.on_installed = register_profile
    original_get = installer.store.get
    paired_reads = threading.Barrier(2)
    read_counter_lock = threading.Lock()
    read_count = 0

    def read_same_downloaded_snapshot(install_id: str):
        nonlocal read_count
        current = original_get(install_id)
        with read_counter_lock:
            read_count += 1
            should_pair = read_count <= 2
        if should_pair:
            paired_reads.wait(timeout=5)
        return current

    monkeypatch.setattr(installer.store, "get", read_same_downloaded_snapshot)

    async def wait_for_first_registration() -> None:
        while True:
            current = await asyncio.to_thread(original_get, record.id)
            if (
                current.status == "installed"
                and record.id not in installer._registration_storage_bindings
            ):
                return
            await asyncio.sleep(0)

    first_registration = asyncio.create_task(wait_for_first_registration())

    class _SequencedTaskRegistry(dict[str, asyncio.Task[None]]):
        retry_checks = 0

        def get(
            self,
            key: str,
            default: asyncio.Task[None] | None = None,
        ) -> asyncio.Task[None] | None:
            self.retry_checks += 1
            if self.retry_checks == 1:
                return None
            if self.retry_checks == 2:
                return first_registration
            return super().get(key, default)

    installer._tasks = _SequencedTaskRegistry()
    try:
        results = await asyncio.gather(
            installer.retry(record.id),
            installer.retry(record.id),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1
        assert "status changed while retrying" in str(failures[0])
        assert (await installer.get(record.id)).status == "installed"
        assert registration_attempts == 1
    finally:
        first_registration.cancel()
        await asyncio.gather(first_registration, return_exceptions=True)
        await installer.stop()
