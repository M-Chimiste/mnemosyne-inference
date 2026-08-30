from __future__ import annotations

import asyncio
import base64
import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

import mnemosyne_fleet.secret_store as secret_store_module
from mnemosyne_fleet.secret_store import (
    CredentialBundle,
    CredentialSecret,
    SecretStore,
    SecretStoreConfigurationError,
    SecretStoreConflictError,
    SecretStoreIntegrityError,
    SecretStorePathError,
    SecretStoreValidationError,
)


def _master_key(fill: int = 7, *, padded: bool = False) -> str:
    encoded = base64.urlsafe_b64encode(bytes([fill]) * 32).decode("ascii")
    return encoded if padded else encoded.rstrip("=")


def _bundle(*, suffix: str = "one", generation: int = 1) -> CredentialBundle:
    return CredentialBundle(
        pairing_id=f"pairing-{suffix}",
        generation=generation,
        snapshot=CredentialSecret(
            secret_ref=f"ref-snapshot-{suffix}",
            secret=f"snapshot-secret-{suffix}-7yL0jwHHQnWW9B9U",
        ),
        dispatch=CredentialSecret(
            secret_ref=f"ref-dispatch-{suffix}",
            secret=f"dispatch-secret-{suffix}-NKbjli7lmF7Oz6hm",
        ),
        management=CredentialSecret(
            secret_ref=f"ref-management-{suffix}",
            secret=f"management-secret-{suffix}-FgA8PIl5um37TCAx",
        ),
    )


async def _new_store(
    tmp_path: Path,
    *,
    key: str | None = None,
    store_id: str = "nyx-fleet-pairing-secrets",
) -> tuple[SecretStore, Path]:
    path = tmp_path / "private" / "pairing-secrets.db"
    store = SecretStore(
        path,
        store_id=store_id,
        master_key=_master_key() if key is None else key,
    )
    await store.initialize()
    return store, path


@pytest.mark.asyncio
async def test_private_file_permissions_and_delete_journaling(tmp_path: Path) -> None:
    store, path = await _new_store(tmp_path)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path != tmp_path / "fleet.db"
    assert not path.with_name(f"{path.name}-wal").exists()
    assert not path.with_name(f"{path.name}-shm").exists()

    with store._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["parent", "database"])
async def test_group_or_world_accessible_paths_are_rejected(
    tmp_path: Path,
    target: str,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "pairing-secrets.db"
    if target == "parent":
        parent.chmod(0o750)
    else:
        path.touch(mode=0o600)
        path.chmod(0o604)

    store = SecretStore(path, store_id="store", master_key=_master_key())
    with pytest.raises(SecretStorePathError) as caught:
        await store.initialize()
    assert caught.value.code == "secret_store_insecure_path"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["parent", "database"])
async def test_symlink_paths_are_rejected(tmp_path: Path, target: str) -> None:
    real_parent = tmp_path / "real-private"
    real_parent.mkdir(mode=0o700)
    if target == "parent":
        linked_parent = tmp_path / "linked-private"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        path = linked_parent / "pairing-secrets.db"
    else:
        path = real_parent / "pairing-secrets.db"
        target_file = real_parent / "actual.db"
        target_file.touch(mode=0o600)
        path.symlink_to(target_file)

    store = SecretStore(path, store_id="store", master_key=_master_key())
    with pytest.raises(SecretStorePathError) as caught:
        await store.initialize()
    assert caught.value.code == "secret_store_insecure_path"


@pytest.mark.asyncio
async def test_wrong_owner_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(
        secret_store_module,
        "_current_uid",
        lambda: os.getuid() + 1,
    )

    store = SecretStore(
        parent / "pairing-secrets.db",
        store_id="store",
        master_key=_master_key(),
    )
    with pytest.raises(SecretStorePathError) as caught:
        await store.initialize()
    assert caught.value.code == "secret_store_insecure_path"


def test_master_key_must_be_exact_32_byte_base64url(tmp_path: Path) -> None:
    path = tmp_path / "unused.db"
    SecretStore(path, store_id="store", master_key=_master_key(padded=False))
    SecretStore(path, store_id="store", master_key=_master_key(padded=True))

    for invalid in (
        "",
        base64.urlsafe_b64encode(b"short").decode("ascii"),
        "+" + _master_key()[1:],
        _master_key() + "garbage",
    ):
        with pytest.raises(SecretStoreConfigurationError) as caught:
            SecretStore(path, store_id="store", master_key=invalid)
        assert caught.value.code == "secret_store_invalid_master_key"
        if invalid:
            assert invalid not in str(caught.value)


@pytest.mark.asyncio
async def test_atomic_bundle_is_distinct_idempotent_and_restart_durable(
    tmp_path: Path,
) -> None:
    store, path = await _new_store(tmp_path)
    bundle = _bundle()

    assert len(
        {
            bundle.snapshot.secret,
            bundle.dispatch.secret,
            bundle.management.secret,
        }
    ) == 3
    assert await store.create_bundle(bundle) == bundle
    with sqlite3.connect(path) as conn:
        before = conn.execute(
            """
            SELECT role, secret_ref, nonce, ciphertext
            FROM pairing_secrets
            ORDER BY role
            """
        ).fetchall()
    assert len(before) == 3
    assert {len(row[2]) for row in before} == {12}
    assert len({row[2] for row in before}) == 3

    # Exact replay returns the original values without rotating ciphertext.
    assert await store.create_bundle(bundle) == bundle
    with sqlite3.connect(path) as conn:
        after = conn.execute(
            """
            SELECT role, secret_ref, nonce, ciphertext
            FROM pairing_secrets
            ORDER BY role
            """
        ).fetchall()
    assert after == before

    replacement = CredentialBundle(
        pairing_id=bundle.pairing_id,
        generation=bundle.generation,
        snapshot=bundle.snapshot,
        dispatch=bundle.dispatch,
        management=CredentialSecret(
            secret_ref=bundle.management.secret_ref,
            secret="a-different-management-secret",
        ),
    )
    with pytest.raises(SecretStoreConflictError) as caught:
        await store.create_bundle(replacement)
    assert caught.value.code == "secret_store_bundle_conflict"
    assert await store.load_bundle(bundle.pairing_id, bundle.generation) == bundle

    restarted = SecretStore(
        path,
        store_id="nyx-fleet-pairing-secrets",
        master_key=_master_key(),
    )
    await restarted.initialize()
    assert await restarted.load_bundle(bundle.pairing_id, bundle.generation) == bundle

    assert await restarted.delete_bundle(bundle.pairing_id, bundle.generation)
    assert await restarted.load_bundle(bundle.pairing_id, bundle.generation) is None
    assert not await restarted.delete_bundle(bundle.pairing_id, bundle.generation)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM pairing_secrets").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["empty", "duplicate", "oversized"])
async def test_bundle_rejects_invalid_secret_sets(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    store, path = await _new_store(tmp_path)
    valid = _bundle()
    management_secret = valid.management.secret
    if invalid_kind == "empty":
        management_secret = ""
    elif invalid_kind == "duplicate":
        management_secret = valid.snapshot.secret
    elif invalid_kind == "oversized":
        management_secret = "x" * 4097
    invalid = CredentialBundle(
        pairing_id=valid.pairing_id,
        generation=valid.generation,
        snapshot=valid.snapshot,
        dispatch=valid.dispatch,
        management=CredentialSecret(
            secret_ref=valid.management.secret_ref,
            secret=management_secret,
        ),
    )

    with pytest.raises(SecretStoreValidationError) as caught:
        await store.create_bundle(invalid)
    assert caught.value.code == "secret_store_invalid_bundle"
    if management_secret:
        assert management_secret not in str(caught.value)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM pairing_secrets").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper_target", ["associated_data", "ciphertext"])
async def test_wrong_key_and_tamper_fail_with_same_fixed_error(
    tmp_path: Path,
    tamper_target: str,
) -> None:
    store, path = await _new_store(tmp_path)
    bundle = _bundle()

    wrong_key = SecretStore(
        path,
        store_id="nyx-fleet-pairing-secrets",
        master_key=_master_key(8),
    )
    with pytest.raises(SecretStoreIntegrityError) as wrong_key_error:
        await wrong_key.initialize()
    assert wrong_key_error.value.code == "secret_store_integrity_failure"
    assert str(wrong_key_error.value) == "secret_store_integrity_failure"

    await store.create_bundle(bundle)

    with sqlite3.connect(path) as conn:
        if tamper_target == "associated_data":
            conn.execute(
                """
                UPDATE pairing_secrets
                SET secret_ref='tampered-opaque-ref'
                WHERE role='snapshot'
                """
            )
        else:
            ciphertext = bytearray(
                conn.execute(
                    """
                    SELECT ciphertext
                    FROM pairing_secrets
                    WHERE role='snapshot'
                    """
                ).fetchone()[0]
            )
            ciphertext[0] ^= 1
            conn.execute(
                """
                UPDATE pairing_secrets
                SET ciphertext=?
                WHERE role='snapshot'
                """,
                (bytes(ciphertext),),
            )
    tampered = SecretStore(
        path,
        store_id="nyx-fleet-pairing-secrets",
        master_key=_master_key(),
    )
    with pytest.raises(SecretStoreIntegrityError) as tamper_error:
        await tampered.initialize()
    assert tamper_error.value.code == "secret_store_integrity_failure"
    assert str(tamper_error.value) == "secret_store_integrity_failure"


@pytest.mark.asyncio
async def test_plaintext_never_reaches_database_or_repr(tmp_path: Path) -> None:
    store, path = await _new_store(tmp_path)
    bundle = _bundle(suffix="plaintext-scan")
    await store.create_bundle(bundle)
    loaded = await store.load_bundle(bundle.pairing_id, bundle.generation)
    assert loaded == bundle

    secret_values = (
        bundle.snapshot.secret,
        bundle.dispatch.secret,
        bundle.management.secret,
        _master_key(),
    )
    rendered = repr(bundle) + repr(loaded) + repr(store)
    database_bytes = path.read_bytes()
    for secret in secret_values:
        assert secret not in rendered
        assert secret.encode("utf-8") not in database_bytes

    assert await store.delete_bundle(bundle.pairing_id, bundle.generation)
    deleted_bytes = path.read_bytes()
    for secret in secret_values:
        assert secret.encode("utf-8") not in deleted_bytes
    assert not list(path.parent.glob("*-journal"))
    assert not list(path.parent.glob("*-wal"))


@pytest.mark.asyncio
async def test_cancellation_waits_for_one_atomic_bundle_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, path = await _new_store(tmp_path)
    bundle = _bundle(suffix="cancelled")
    started = threading.Event()
    release = threading.Event()
    original = SecretStore._create_bundle_sync

    def delayed_create(
        target: SecretStore,
        pending: CredentialBundle,
    ) -> CredentialBundle:
        started.set()
        if not release.wait(timeout=5):
            raise AssertionError("test worker was not released")
        return original(target, pending)

    monkeypatch.setattr(SecretStore, "_create_bundle_sync", delayed_create)
    task = asyncio.create_task(store.create_bundle(bundle))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cancelled caller cannot observe a partial three-role write.
    assert await store.load_bundle(bundle.pairing_id, bundle.generation) == bundle
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM pairing_secrets").fetchone()[0] == 3
