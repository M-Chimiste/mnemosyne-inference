"""Durable universal model catalog built from authenticated node snapshots.

Only authoritative, Fleet-eligible deployments can create routing mappings.
Node aliases suggest public names but never contribute to deployment identity.
An operator removal creates an exact suppression so ordinary snapshot refreshes
cannot silently republish the mapping; an explicit add clears that fence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import time

from .config import CAPABILITIES, ModelConfig
from .registry import NodeRegistry
from .scheduler import ModelMutationError, Scheduler
from .store import FleetStore, ManagedModelRecord


DEFAULT_QUEUE_DEPTH = 128
DEFAULT_QUEUE_TIMEOUT_SECONDS = 120.0
_DEPLOYMENT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class ModelCatalogError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class CatalogCandidate:
    origin_alias: str
    aliases: tuple[str, ...]
    deployment_id: str
    capabilities: tuple[str, ...]
    engine: str
    upstream_model: str
    resolved_revision: str | None
    kind: str
    enrollment_ids: tuple[str, ...]
    node_ids: tuple[str, ...]

    @property
    def key(self) -> tuple[str, tuple[str, ...]]:
        return (self.deployment_id, self.capabilities)

    def payload(self, *, published_as: str | None, suppressed: bool) -> dict:
        return {
            "origin_alias": self.origin_alias,
            "aliases": list(self.aliases),
            "deployment_id": self.deployment_id,
            "capabilities": list(self.capabilities),
            "engine": self.engine,
            "upstream_model": self.upstream_model,
            "resolved_revision": self.resolved_revision,
            "kind": self.kind,
            "enrollment_ids": list(self.enrollment_ids),
            "node_ids": list(self.node_ids),
            "published_as": published_as,
            "suppressed": suppressed,
        }


def _public_name(value: object) -> str:
    if not isinstance(value, str):
        raise ModelCatalogError("model_catalog_public_name_invalid", status_code=422)
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized)
    ):
        raise ModelCatalogError("model_catalog_public_name_invalid", status_code=422)
    return normalized


def _candidate_key(
    origin_alias: object,
    deployment_id: object,
    capabilities: object,
) -> tuple[str, str, tuple[str, ...]]:
    alias = _public_name(origin_alias)
    if not isinstance(deployment_id, str) or _DEPLOYMENT_ID.fullmatch(deployment_id) is None:
        raise ModelCatalogError("model_catalog_deployment_invalid", status_code=422)
    if (
        not isinstance(capabilities, (list, tuple, frozenset, set))
        or not capabilities
        or any(not isinstance(value, str) for value in capabilities)
    ):
        raise ModelCatalogError("model_catalog_capabilities_invalid", status_code=422)
    normalized_capabilities = tuple(sorted(set(capabilities)))
    if (
        len(normalized_capabilities) != len(capabilities)
        or not set(normalized_capabilities).issubset(CAPABILITIES)
    ):
        raise ModelCatalogError("model_catalog_capabilities_invalid", status_code=422)
    return alias, deployment_id, normalized_capabilities


def _model_config(record: ManagedModelRecord) -> ModelConfig:
    _candidate_key(
        record.origin_alias,
        record.deployment_id,
        record.capabilities,
    )
    name = _public_name(record.public_model)
    if record.source not in {"auto", "admin"}:
        raise ModelCatalogError("model_catalog_store_invalid", status_code=503)
    if not 0 <= record.queue_depth <= 100_000:
        raise ModelCatalogError("model_catalog_store_invalid", status_code=503)
    if not 0 < record.queue_timeout_seconds <= 86_400:
        raise ModelCatalogError("model_catalog_store_invalid", status_code=503)
    return ModelConfig(
        name=name,
        deployment_id=record.deployment_id,
        capabilities=frozenset(record.capabilities),
        queue_depth=record.queue_depth,
        queue_timeout_seconds=record.queue_timeout_seconds,
    )


class UniversalModelCatalog:
    """Auto-publish and administer exact model mappings for one Fleet process."""

    def __init__(
        self,
        *,
        store: FleetStore,
        scheduler: Scheduler,
        registry: NodeRegistry,
        configured_models: tuple[ModelConfig, ...],
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._registry = registry
        self._configured = {model.name: model for model in configured_models}
        self._managed: dict[str, ManagedModelRecord] = {}
        self._suppressions: set[tuple[str, tuple[str, ...]]] = set()
        self._lock = asyncio.Lock()
        self._initialized = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    def _candidates(self) -> tuple[CatalogCandidate, ...]:
        grouped: dict[
            tuple[str, tuple[str, ...]],
            dict[str, object],
        ] = {}
        for record in self._registry.live_records():
            for deployment in record.snapshot.deployments:
                if (
                    deployment.identity_confidence != "authoritative"
                    or not deployment.fleet_eligible
                ):
                    continue
                try:
                    alias, deployment_id, capabilities = _candidate_key(
                        deployment.alias,
                        deployment.deployment_id,
                        deployment.identity.capabilities,
                    )
                except ModelCatalogError:
                    continue
                key = (deployment_id, capabilities)
                identity = deployment.identity
                current = grouped.get(key)
                if current is None:
                    current = {
                        "engine": identity.engine,
                        "upstream_model": identity.upstream_model,
                        "resolved_revision": identity.resolved_revision,
                        "kind": identity.kind,
                        "aliases": set(),
                        "enrollment_ids": set(),
                        "node_ids": set(),
                    }
                    grouped[key] = current
                current["aliases"].add(alias)  # type: ignore[union-attr]
                current["enrollment_ids"].add(record.enrollment.enrollment_id)  # type: ignore[union-attr]
                current["node_ids"].add(record.enrollment.reporting_node_id)  # type: ignore[union-attr]
        return tuple(
            CatalogCandidate(
                origin_alias=min(value["aliases"]),  # type: ignore[arg-type]
                aliases=tuple(sorted(value["aliases"])),  # type: ignore[arg-type]
                deployment_id=key[0],
                capabilities=key[1],
                engine=str(value["engine"]),
                upstream_model=str(value["upstream_model"]),
                resolved_revision=(
                    None
                    if value["resolved_revision"] is None
                    else str(value["resolved_revision"])
                ),
                kind=str(value["kind"]),
                enrollment_ids=tuple(sorted(value["enrollment_ids"])),  # type: ignore[arg-type]
                node_ids=tuple(sorted(value["node_ids"])),  # type: ignore[arg-type]
            )
            for key, value in sorted(grouped.items())
        )

    def _deployment_index(self) -> dict[tuple[str, tuple[str, ...]], str]:
        return {
            (
                record.deployment_id,
                tuple(record.capabilities),
            ): public_model
            for public_model, record in self._managed.items()
        }

    def _same_mapping(self, public_model: str, candidate: CatalogCandidate) -> bool:
        model = self._configured.get(public_model)
        if model is None:
            record = self._managed.get(public_model)
            model = None if record is None else _model_config(record)
        return bool(
            model is not None
            and model.deployment_id == candidate.deployment_id
            and tuple(sorted(model.capabilities)) == candidate.capabilities
        )

    def _allocated_name(self, candidate: CatalogCandidate) -> str:
        base = _public_name(candidate.origin_alias)
        if base not in self._configured and base not in self._managed:
            return base
        if self._same_mapping(base, candidate):
            return base
        digest = candidate.deployment_id.removeprefix("sha256:")
        for length in (8, 12, 16, 24, 64):
            suffix = f"--{digest[:length]}"
            name = f"{base[: 256 - len(suffix)]}{suffix}"
            if name not in self._configured and name not in self._managed:
                return name
            if self._same_mapping(name, candidate):
                return name
        raise ModelCatalogError("model_catalog_name_exhausted", status_code=503)

    async def initialize(self) -> None:
        async with self._lock:
            records = await self._store.managed_models()
            suppressions = await self._store.model_suppressions()
            self._suppressions = {
                (row.deployment_id, tuple(row.capabilities))
                for row in suppressions
            }
            for record in records:
                model = _model_config(record)
                configured = self._configured.get(model.name)
                if configured is not None and configured != model:
                    raise ModelCatalogError(
                        "model_catalog_config_conflict",
                        status_code=503,
                    )
                await self._scheduler.add_model(model)
                self._managed[record.public_model] = record
            self._initialized = True
        await self.reconcile()

    async def reconcile(self) -> tuple[str, ...]:
        if not self._initialized:
            return ()
        added: list[str] = []
        async with self._lock:
            deployment_index = self._deployment_index()
            for candidate in self._candidates():
                if candidate.key in self._suppressions:
                    continue
                if candidate.key in deployment_index:
                    continue
                if any(
                    model.deployment_id == candidate.deployment_id
                    and tuple(sorted(model.capabilities))
                    == candidate.capabilities
                    for model in self._configured.values()
                ):
                    continue
                public_model = self._allocated_name(candidate)
                if self._same_mapping(public_model, candidate):
                    continue
                now = time.time()
                record = ManagedModelRecord(
                    public_model=public_model,
                    origin_alias=candidate.origin_alias,
                    deployment_id=candidate.deployment_id,
                    capabilities=candidate.capabilities,
                    queue_depth=DEFAULT_QUEUE_DEPTH,
                    queue_timeout_seconds=DEFAULT_QUEUE_TIMEOUT_SECONDS,
                    source="auto",
                    created_at=now,
                    updated_at=now,
                )
                await self._store.put_managed_model(record)
                try:
                    await self._scheduler.add_model(_model_config(record))
                except BaseException:
                    await self._store.remove_managed_model(
                        public_model,
                        suppress=False,
                    )
                    raise
                self._managed[public_model] = record
                deployment_index[candidate.key] = public_model
                added.append(public_model)
        return tuple(added)

    async def status(self) -> dict:
        async with self._lock:
            candidates = self._candidates()
            deployment_index = self._deployment_index()
            configured_index = {
                (model.deployment_id, tuple(sorted(model.capabilities))): model.name
                for model in sorted(
                    self._configured.values(),
                    key=lambda value: value.name,
                )
            }
            mappings = [
                {
                    "public_model": model.name,
                    "origin_alias": model.name,
                    "deployment_id": model.deployment_id,
                    "capabilities": sorted(model.capabilities),
                    "queue_depth": model.queue_depth,
                    "queue_timeout_seconds": model.queue_timeout_seconds,
                    "source": "config",
                    "removable": False,
                }
                for model in self._configured.values()
            ]
            mappings.extend(
                {
                    "public_model": record.public_model,
                    "origin_alias": record.origin_alias,
                    "deployment_id": record.deployment_id,
                    "capabilities": list(record.capabilities),
                    "queue_depth": record.queue_depth,
                    "queue_timeout_seconds": record.queue_timeout_seconds,
                    "source": record.source,
                    "removable": True,
                }
                for record in self._managed.values()
            )
            return {
                "schema_version": 1,
                "automatic_publication": True,
                "mappings": sorted(mappings, key=lambda row: row["public_model"]),
                "candidates": [
                    candidate.payload(
                        published_as=(
                            deployment_index.get(candidate.key)
                            or configured_index.get(candidate.key)
                        ),
                        suppressed=candidate.key in self._suppressions,
                    )
                    for candidate in candidates
                ],
            }

    async def remove(self, public_model: str) -> None:
        name = _public_name(public_model)
        async with self._lock:
            if name in self._configured:
                raise ModelCatalogError("model_catalog_config_mapping_locked")
            record = self._managed.get(name)
            if record is None:
                raise ModelCatalogError(
                    "model_catalog_mapping_unknown",
                    status_code=404,
                )
            try:
                removed_model = await self._scheduler.remove_model(name)
            except ModelMutationError as exc:
                raise ModelCatalogError(exc.code) from exc
            try:
                removed = await self._store.remove_managed_model(
                    name,
                    suppress=True,
                )
                if removed is None:
                    raise ModelCatalogError(
                        "model_catalog_store_conflict",
                        status_code=503,
                    )
            except BaseException:
                await self._scheduler.add_model(removed_model)
                raise
            self._managed.pop(name)
            self._suppressions.add(
                (
                    record.deployment_id,
                    tuple(record.capabilities),
                )
            )

    async def add(
        self,
        *,
        public_model: object,
        origin_alias: object,
        deployment_id: object,
        capabilities: object,
    ) -> str:
        name = _public_name(public_model)
        alias, exact_deployment_id, exact_capabilities = _candidate_key(
            origin_alias,
            deployment_id,
            capabilities,
        )
        key = (exact_deployment_id, exact_capabilities)
        async with self._lock:
            candidate = next(
                (
                    row
                    for row in self._candidates()
                    if row.key == key and alias in row.aliases
                ),
                None,
            )
            if candidate is None:
                raise ModelCatalogError(
                    "model_catalog_candidate_not_live",
                    status_code=409,
                )
            configured = self._configured.get(name)
            if configured is not None:
                if (
                    configured.deployment_id == candidate.deployment_id
                    and tuple(sorted(configured.capabilities))
                    == candidate.capabilities
                ):
                    return name
                raise ModelCatalogError("model_catalog_mapping_conflict")
            existing = self._managed.get(name)
            if existing is not None:
                if (
                    existing.deployment_id,
                    tuple(existing.capabilities),
                ) == key:
                    return name
                raise ModelCatalogError("model_catalog_mapping_conflict")
            deployment_existing = self._deployment_index().get(key)
            if deployment_existing is not None:
                return deployment_existing
            now = time.time()
            record = ManagedModelRecord(
                public_model=name,
                origin_alias=candidate.origin_alias,
                deployment_id=candidate.deployment_id,
                capabilities=candidate.capabilities,
                queue_depth=DEFAULT_QUEUE_DEPTH,
                queue_timeout_seconds=DEFAULT_QUEUE_TIMEOUT_SECONDS,
                source="admin",
                created_at=now,
                updated_at=now,
            )
            await self._store.put_managed_model(record)
            try:
                await self._scheduler.add_model(_model_config(record))
            except BaseException:
                await self._store.remove_managed_model(name, suppress=True)
                raise
            self._managed[name] = record
            self._suppressions.discard(key)
            return name


__all__ = [
    "CatalogCandidate",
    "ModelCatalogError",
    "UniversalModelCatalog",
]
