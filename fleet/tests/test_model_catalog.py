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
