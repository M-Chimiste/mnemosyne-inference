from __future__ import annotations

import asyncio

import pytest

import mnemosyne_macos.installer as installer_module
from mnemosyne_macos.installer import NativeInstaller


class FakeDownloadProcess:
    pid = 4321
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(0)
        return b'{"status":"complete"}', b""


@pytest.mark.asyncio
async def test_profile_registration_retry_does_not_redownload_after_restart(
    monkeypatch,
    tmp_path,
) -> None:
    destination = tmp_path / "models" / "owner" / "model"
    destination.mkdir(parents=True)
    (destination / "model.gguf").write_bytes(b"downloaded weights")
    database = tmp_path / "state" / "mnemosyne.db"
    spawn_calls: list[tuple[str, ...]] = []

    async def create_process(*argv: str, **_kwargs: object) -> FakeDownloadProcess:
        spawn_calls.append(argv)
        return FakeDownloadProcess()

    monkeypatch.setattr(
        installer_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )

    registration_attempts = 0

    async def register_profile(_record) -> None:
        nonlocal registration_attempts
        registration_attempts += 1
        if registration_attempts == 1:
            raise RuntimeError("config temporarily unavailable")

    first = NativeInstaller(database, on_installed=register_profile)
    install = await first.create(
        repo_id="owner/model",
        engine="llama.cpp",
        storage="internal",
        alias="model",
        destination=str(destination),
        revision="resolved-commit",
        filename="model.gguf",
        family=None,
        total_bytes=18,
    )
    await first._tasks[install.id]

    failed_registration = await first.get(install.id)
    assert failed_registration.status == "downloaded"
    assert failed_registration.bytes_downloaded == 18
    assert failed_registration.error == (
        "download completed but profile registration failed: "
        "config temporarily unavailable"
    )
    assert len(spawn_calls) == 1
    await first.stop()

    second = NativeInstaller(database, on_installed=register_profile)
    await second.start()
    retrying = await second.retry(install.id)
    assert retrying.status == "registering"
    await second._tasks[install.id]

    completed = await second.get(install.id)
    assert completed.status == "installed"
    assert completed.error is None
    assert registration_attempts == 2
    assert len(spawn_calls) == 1
    await second.stop()


@pytest.mark.asyncio
async def test_install_history_dismissal_refuses_active_downloads(tmp_path) -> None:
    installer = NativeInstaller(tmp_path / "state.db")
    record = installer.store.create(
        repo_id="owner/model",
        engine="llama.cpp",
        storage="internal",
        alias="model",
        destination=str(tmp_path / "models" / "owner" / "model"),
        revision="abc123",
        filename="model.gguf",
        family=None,
        total_bytes=4096,
    )

    with pytest.raises(ValueError, match="active install"):
        await installer.dismiss(record.id)

    installer.store.update(record.id, status="installed", bytes_downloaded=4096)
    dismissed = await installer.dismiss(record.id)

    assert dismissed.status == "installed"
    assert await installer.list() == []
    installer.store.close()
