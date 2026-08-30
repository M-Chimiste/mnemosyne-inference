from __future__ import annotations

import hashlib
import sqlite3

import pytest

from mnemosyne_macos.mac_inventory_store import MacInventoryIndex


@pytest.mark.asyncio
async def test_storage_ids_survive_restart_and_exact_binding_changes_advance_generation(
    tmp_path,
) -> None:
    database = tmp_path / "state.db"
    index = MacInventoryIndex(database)
    await index.initialize()
    first = (
        await index.reconcile_storage(
            [("archive", "/Volumes/Archive/nested/models", "VOL-ONE", "a" * 64)]
        )
    )["archive"]
    await index.close()

    restarted = MacInventoryIndex(database)
    await restarted.initialize()
    unchanged = (
        await restarted.reconcile_storage(
            [("archive", "/Volumes/Archive/nested/models", "VOL-ONE", "a" * 64)]
        )
    )["archive"]
    assert (
        await restarted.resolve_storage(
            unchanged.storage_location_id,
            unchanged.binding_generation,
        )
    ) == unchanged
    assert unchanged.storage_location_id == first.storage_location_id
    assert unchanged.binding_generation == 1

    path_changed = (
        await restarted.reconcile_storage(
            [("archive", "/Volumes/Archive/other/models", "VOL-ONE", "a" * 64)]
        )
    )["archive"]
    volume_changed = (
        await restarted.reconcile_storage(
            [("archive", "/Volumes/Archive/other/models", "VOL-TWO", "a" * 64)]
        )
    )["archive"]
    scope_changed = (
        await restarted.reconcile_storage(
            [("archive", "/Volumes/Archive/other/models", "VOL-TWO", "b" * 64)]
        )
    )["archive"]
    assert path_changed.storage_location_id == first.storage_location_id
    assert volume_changed.storage_location_id == first.storage_location_id
    assert scope_changed.storage_location_id == first.storage_location_id
    assert [path_changed.binding_generation, volume_changed.binding_generation, scope_changed.binding_generation] == [2, 3, 4]
    assert scope_changed.exact_path == "/Volumes/Archive/other/models"
    assert scope_changed.volume_uuid == "VOL-TWO"
    assert scope_changed.scope_id == "b" * 64
    assert await restarted.resolve_storage(first.storage_location_id, 1) is None
    assert (
        await restarted.resolve_storage(
            scope_changed.storage_location_id,
            scope_changed.binding_generation,
        )
    ) == scope_changed

    # The public ID is not any direct path/name/volume/scope digest.
    forbidden = {
        hashlib.md5(value.encode()).hexdigest()  # noqa: S324 - negative canary only
        for value in (
            "archive",
            scope_changed.exact_path,
            scope_changed.volume_uuid,
            scope_changed.scope_id,
        )
    } | {
        hashlib.sha256(value.encode()).hexdigest()
        for value in (
            "archive",
            scope_changed.exact_path,
            scope_changed.volume_uuid,
            scope_changed.scope_id,
        )
    }
    assert scope_changed.storage_location_id.replace("-", "") not in forbidden


@pytest.mark.asyncio
async def test_removed_storage_and_ambiguous_installation_bindings_never_reuse_ids(
    tmp_path,
) -> None:
    database = tmp_path / "state.db"
    index = MacInventoryIndex(database)
    await index.initialize()
    first_storage = (
        await index.reconcile_storage([("external", "/Volumes/One/models", None, None)])
    )["external"]
    await index.reconcile_storage([])
    second_storage = (
        await index.reconcile_storage([("external", "/Volumes/One/models", None, None)])
    )["external"]
    assert second_storage.storage_location_id != first_storage.storage_location_id

    first_install = (
        await index.reconcile_installations({"profile:model:primary": "first"})
    )["profile:model:primary"]
    unchanged = (
        await index.reconcile_installations({"profile:model:primary": "first"})
    )["profile:model:primary"]
    replaced = (
        await index.reconcile_installations({"profile:model:primary": "second"})
    )["profile:model:primary"]
    assert unchanged.installation_id == first_install.installation_id
    assert replaced.installation_id != first_install.installation_id

    with sqlite3.connect(database) as connection:
        storage_history = connection.execute(
            "SELECT storage_location_id, active FROM native_mac_inventory_storage_v1"
        ).fetchall()
        install_history = connection.execute(
            "SELECT installation_id, active FROM native_mac_inventory_installations_v1"
        ).fetchall()
    assert len({row[0] for row in storage_history}) == len(storage_history)
    assert len({row[0] for row in install_history}) == len(install_history)
    assert sum(int(row[1]) for row in storage_history) == 1
    assert sum(int(row[1]) for row in install_history) == 1
