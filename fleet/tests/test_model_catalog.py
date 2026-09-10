from __future__ import annotations

import time

from mnemosyne_fleet.config import ModelConfig, NodeConfig
from mnemosyne_fleet.model_catalog import UniversalModelCatalog
from mnemosyne_fleet.protocol import Snapshot
from mnemosyne_fleet.registry import NodeRecord
from mnemosyne_fleet.scheduler import Scheduler, UnknownModelError
from mnemosyne_fleet.store import FleetStore

from .helpers import identity, snapshot_payload


class CatalogRegistry:
    def __init__(self, records: list[NodeRecord]) -> None:
        self.records = records

    def live_records(self) -> tuple[NodeRecord, ...]:
        return tuple(self.records)


def record(
    node_id: str,
    *,
    alias: str,
    identity_value: dict[str, object],
    deployment_id: str,
    authoritative: bool = True,
    fleet_eligible: bool = True,
) -> NodeRecord:
    payload = snapshot_payload(
        node_id,
        alias=alias,
        identity_value=identity_value,
        deployment_id=deployment_id,
    )
    payload["deployments"][0]["identity_confidence"] = (
        "authoritative" if authoritative else "unverified"
    )
    payload["deployments"][0]["fleet_eligible"] = fleet_eligible
    enrollment = NodeConfig(
        node_id=node_id,
        url=f"http://{node_id}",
        fleet_token=f"snapshot-{node_id}",
        inference_token=f"dispatch-{node_id}",
    )
    monotonic = time.monotonic()
    return NodeRecord(
        enrollment=enrollment,
        snapshot=Snapshot.model_validate(payload),
        received_at=time.time(),
        received_monotonic=monotonic,
        poll_started_monotonic=monotonic - 0.01,
    )


async def test_catalog_auto_publishes_replicas_resolves_collisions_and_persists(
    tmp_path,
) -> None:
    first_identity, first_deployment = identity()
    second_identity, second_deployment = identity(quantization="Q8_0")
    ignored_identity, ignored_deployment = identity(quantization="Q2_K")
    registry = CatalogRegistry(
        [
            record(
                "athena",
                alias="glm-flash",
                identity_value=first_identity,
                deployment_id=first_deployment,
            ),
            record(
                "metis",
                alias="metis-copy",
                identity_value=first_identity,
                deployment_id=first_deployment,
            ),
            record(
                "apollo",
                alias="glm-flash",
                identity_value=second_identity,
                deployment_id=second_deployment,
            ),
            record(
                "unverified",
                alias="private-path-model",
                identity_value=ignored_identity,
                deployment_id=ignored_deployment,
                authoritative=False,
                fleet_eligible=False,
            ),
        ]
    )
    store = FleetStore(tmp_path / "fleet.db")
    await store.initialize(node_ids=(), models=())
    scheduler = Scheduler(registry=registry, models=(), nodes=())
    catalog = UniversalModelCatalog(
        store=store,
        scheduler=scheduler,
        registry=registry,
        configured_models=(),
    )

    await catalog.initialize()

    status = await catalog.status()
    mappings = status["mappings"]
    assert len(mappings) == 2
    assert {row["deployment_id"] for row in mappings} == {
        first_deployment,
        second_deployment,
    }
    assert "glm-flash" in {row["public_model"] for row in mappings}
    assert any(
        row["public_model"].startswith("glm-flash--") for row in mappings
    )
    assert len(status["candidates"]) == 2
    replica = next(
        row
        for row in status["candidates"]
        if row["deployment_id"] == first_deployment
    )
    assert replica["node_ids"] == ["athena", "metis"]
    assert replica["aliases"] == ["glm-flash", "metis-copy"]
    assert all(row["origin_alias"] != "private-path-model" for row in mappings)

    removed = mappings[0]
    await catalog.remove(removed["public_model"])
    try:
        scheduler.model(removed["public_model"])
    except UnknownModelError:
        pass
    else:
        raise AssertionError("suppressed mapping remained routable")
    await catalog.reconcile()
    suppressed = next(
        row
        for row in (await catalog.status())["candidates"]
        if row["deployment_id"] == removed["deployment_id"]
    )
    assert suppressed["suppressed"] is True
    assert suppressed["published_as"] is None

    restored = await catalog.add(
        public_model="restored-glm",
        origin_alias=removed["origin_alias"],
        deployment_id=removed["deployment_id"],
        capabilities=removed["capabilities"],
    )
    assert restored == "restored-glm"
    assert scheduler.model(restored).deployment_id == removed["deployment_id"]

    restarted_scheduler = Scheduler(registry=registry, models=(), nodes=())
    restarted = UniversalModelCatalog(
        store=store,
        scheduler=restarted_scheduler,
        registry=registry,
        configured_models=(),
    )
    await restarted.initialize()
    restarted_names = {
        row["public_model"] for row in (await restarted.status())["mappings"]
    }
    assert "restored-glm" in restarted_names
    assert len(restarted_names) == 2


async def test_config_owned_exact_deployment_prevents_automatic_synonyms(
    tmp_path,
) -> None:
    identity_value, deployment_id = identity(capabilities=("responses",))
    registry = CatalogRegistry(
        [
            record(
                "athena",
                alias="athena-local-name",
                identity_value=identity_value,
                deployment_id=deployment_id,
            )
        ]
    )
    configured = ModelConfig(
        name="universal-name",
        deployment_id=deployment_id,
        capabilities=frozenset({"responses"}),
        queue_depth=8,
        queue_timeout_seconds=10,
    )
    store = FleetStore(tmp_path / "fleet.db")
    await store.initialize(
        node_ids=(),
        models=((configured.name, configured.deployment_id),),
    )
    scheduler = Scheduler(
        registry=registry,
        models=(configured,),
        nodes=(),
    )
    catalog = UniversalModelCatalog(
        store=store,
        scheduler=scheduler,
        registry=registry,
        configured_models=(configured,),
    )

    await catalog.initialize()

    status = await catalog.status()
    assert [row["public_model"] for row in status["mappings"]] == [
        "universal-name"
    ]
    assert status["candidates"][0]["published_as"] == "universal-name"
    assert await store.managed_models() == ()


async def rename_fixture(tmp_path):
    value, deployment = identity()
    registry = CatalogRegistry([
        record(node, alias="qwen-2", identity_value=value, deployment_id=deployment)
        for node in ("athena", "metis")
    ])
    store = FleetStore(tmp_path / "rename.db")
    await store.initialize(node_ids=(), models=())
    scheduler = Scheduler(registry=registry, models=(), nodes=())
    catalog = UniversalModelCatalog(store=store, scheduler=scheduler,
                                    registry=registry, configured_models=())
    await catalog.initialize()
    return catalog, store, scheduler, registry, deployment


async def test_rename_preserves_replicas_and_survives_restart(tmp_path):
    catalog, store, scheduler, registry, deployment = await rename_fixture(tmp_path)
    before = (await store.managed_models())[0]
    assert await catalog.rename("qwen-2", "qwen", deployment_id=deployment) == "qwen"
    after = (await store.managed_models())[0]
    from dataclasses import replace
    assert after == replace(before, public_model="qwen", updated_at=after.updated_at)
    assert await store.model_suppressions() == ()
    await catalog.reconcile()
    status = await catalog.status()
    assert [m["public_model"] for m in status["mappings"]] == ["qwen"]
    assert status["candidates"][0]["node_ids"] == ["athena", "metis"]
    assert status["candidates"][0]["published_as"] == "qwen"
    scheduler2 = Scheduler(registry=registry, models=(), nodes=())
    restored = UniversalModelCatalog(store=store, scheduler=scheduler2,
                                    registry=registry, configured_models=())
    await restored.initialize()
    assert [m.name for m in scheduler2.models()] == ["qwen"]
    assert scheduler2.model("qwen").deployment_id == deployment


async def test_rename_conflicts_validation_and_stale_identity(tmp_path):
    import pytest
    from mnemosyne_fleet.model_catalog import ModelCatalogError
    catalog, store, scheduler, registry, deployment = await rename_fixture(tmp_path)
    value, other = identity(quantization="Q8_0")
    registry.records.append(record("other", alias="taken", identity_value=value,
                                   deployment_id=other))
    await catalog.reconcile()
    for target, expected in [("taken", "mapping_conflict"), (" ", "public_name_invalid"),
                             ("x\nname", "public_name_invalid"), ("x" * 257, "public_name_invalid")]:
        with pytest.raises(ModelCatalogError, match=expected):
            await catalog.rename("qwen-2", target, deployment_id=deployment)
    with pytest.raises(ModelCatalogError, match="mapping_changed"):
        await catalog.rename("qwen-2", "qwen", deployment_id=other)
    with pytest.raises(ModelCatalogError, match="mapping_unknown"):
        await catalog.rename("unknown", "qwen", deployment_id=deployment)
    assert await catalog.rename("qwen-2", "qwen-2", deployment_id=deployment) == "qwen-2"
    assert scheduler.model("qwen-2").deployment_id == deployment
    assert len(await store.managed_models()) == 2


async def test_rename_store_failure_keeps_original_route(tmp_path, monkeypatch):
    import pytest
    from mnemosyne_fleet.model_catalog import ModelCatalogError
    catalog, store, scheduler, registry, deployment = await rename_fixture(tmp_path)
    async def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(store, "rename_managed_model", fail)
    with pytest.raises(ModelCatalogError, match="store_conflict"):
        await catalog.rename("qwen-2", "qwen", deployment_id=deployment)
    assert [m.name for m in scheduler.models()] == ["qwen-2"]
    assert [m.public_model for m in await store.managed_models()] == ["qwen-2"]


async def test_cancelled_rename_finishes_commit_before_admission(tmp_path, monkeypatch):
    import asyncio
    import pytest
    catalog, store, scheduler, registry, deployment = await rename_fixture(tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = store.rename_managed_model
    async def delayed(*args, **kwargs):
        entered.set()
        await finish.wait()
        await original(*args, **kwargs)
    monkeypatch.setattr(store, "rename_managed_model", delayed)
    rename = asyncio.create_task(catalog.rename("qwen-2", "qwen", deployment_id=deployment))
    await entered.wait()
    rename.cancel()
    # Admission cannot observe a half-committed name swap.
    admission = asyncio.create_task(scheduler.acquire(public_model="qwen-2", capability="responses"))
    await asyncio.sleep(0)
    assert not admission.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await rename
    with pytest.raises(UnknownModelError):
        await admission
    assert [m.name for m in scheduler.models()] == ["qwen"]
    assert [m.public_model for m in await store.managed_models()] == ["qwen"]


async def test_rename_busy_and_config_owned_are_unchanged(tmp_path):
    import pytest
    from types import SimpleNamespace
    from mnemosyne_fleet.model_catalog import ModelCatalogError
    catalog, store, scheduler, registry, deployment = await rename_fixture(tmp_path)
    scheduler._reservations["stream"] = SimpleNamespace(public_model="qwen-2")
    with pytest.raises(ModelCatalogError, match="in_use"):
        await catalog.rename("qwen-2", "qwen", deployment_id=deployment)
    scheduler._reservations.clear()
    scheduler._queues["qwen-2"].append(object())
    with pytest.raises(ModelCatalogError, match="in_use"):
        await catalog.rename("qwen-2", "qwen", deployment_id=deployment)
    scheduler._queues.clear()
    catalog._configured["qwen-2"] = scheduler.model("qwen-2")
    with pytest.raises(ModelCatalogError, match="config_mapping_locked"):
        await catalog.rename("qwen-2", "qwen", deployment_id=deployment)
    assert [m.public_model for m in await store.managed_models()] == ["qwen-2"]


async def test_store_rename_collision_rolls_back_and_concurrent_renames_serialize(tmp_path):
    import asyncio
    import sqlite3
    import pytest
    from mnemosyne_fleet.model_catalog import ModelCatalogError
    catalog, store, scheduler, registry, deployment = await rename_fixture(tmp_path)
    value, other = identity(quantization="Q8_0")
    registry.records.append(record("other", alias="taken", identity_value=value,
                                   deployment_id=other))
    await catalog.reconcile()
    before = await store.managed_models()
    with pytest.raises(sqlite3.IntegrityError):
        await store.rename_managed_model("qwen-2", "taken", updated_at=time.time())
    assert await store.managed_models() == before
    outcomes = await asyncio.gather(
        catalog.rename("qwen-2", "qwen", deployment_id=deployment),
        catalog.rename("qwen-2", "another", deployment_id=deployment),
        return_exceptions=True,
    )
    assert sum(isinstance(item, str) for item in outcomes) == 1
    assert sum(isinstance(item, ModelCatalogError) for item in outcomes) == 1
    assert {m.name for m in scheduler.models()} == {m.public_model for m in await store.managed_models()}
    assert len(scheduler.models()) == 2
