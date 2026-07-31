from __future__ import annotations

import hashlib
import importlib.resources
import json
import math
import re
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CAPABILITIES = frozenset(
    {
        "chat/completions",
        "completions",
        "responses",
        "messages",
        "embeddings",
        "rerank",
        "images/generations",
    }
)
_SCHEMA = json.loads(
    importlib.resources.files("mnemosyne_fleet")
    .joinpath("schemas", "snapshot.schema.json")
    .read_text(encoding="utf-8")
)
_SCHEMA_VALIDATOR = Draft202012Validator(_SCHEMA)


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class NodeDescriptor(ProtocolModel):
    node_id: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(min_length=1, max_length=128)
    platform: Literal["cuda", "macos"]
    version: str = Field(min_length=1, max_length=64)


class Health(ProtocolModel):
    state: Literal[
        "idle",
        "draining",
        "unloading",
        "loading",
        "verifying",
        "ready",
        "degraded",
        "stopping",
    ]
    accepting: bool
    authoritative: bool
    diagnostic_code: str | None = Field(
        pattern=r"^[a-z0-9_]{1,64}$",
    )


class Residency(ProtocolModel):
    alias: str | None = Field(default=None, min_length=1, max_length=128)
    deployment_id: str | None = Field(default=None, max_length=71)
    engine: str | None = Field(default=None, min_length=1, max_length=64)
    epoch: int = Field(ge=0)
    transition_target: str | None = Field(default=None, max_length=71)

    @field_validator("deployment_id", "transition_target")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.startswith("sha256:")
            or len(value) != 71
            or value != value.lower()
        ):
            raise ValueError("deployment reference must be lowercase sha256")
        if value is not None:
            try:
                int(value[7:], 16)
            except ValueError as exc:
                raise ValueError("deployment reference must contain hex") from exc
        return value


class Capacity(ProtocolModel):
    derived_limit: int = Field(ge=1, le=100_000)
    configured_max_concurrency: int | None = Field(default=None, ge=1, le=100_000)
    effective_limit: int = Field(ge=1, le=100_000)
    active: int = Field(ge=0, le=1_000_000)
    queued: int = Field(ge=0, le=1_000_000)
    available: int = Field(ge=0, le=100_000)
    source: str = Field(min_length=1, max_length=96)
    confidence: Literal["authoritative", "configured", "derived", "conservative"]
    saturation: float = Field(ge=0)

    @model_validator(mode="after")
    def capacity_is_consistent(self) -> "Capacity":
        ceiling = self.configured_max_concurrency
        expected = (
            self.derived_limit
            if ceiling is None
            else min(self.derived_limit, ceiling)
        )
        if self.effective_limit != expected:
            raise ValueError("effective_limit is inconsistent")
        arithmetic_available = max(0, self.effective_limit - self.active)
        if self.available not in {0, arithmetic_available}:
            raise ValueError("available is inconsistent")
        if not math.isfinite(self.saturation):
            raise ValueError("saturation must be finite")
        if not math.isclose(
            self.saturation,
            self.active / self.effective_limit,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError("saturation is inconsistent")
        return self


class Admission(ProtocolModel):
    queue_depth: int = Field(ge=0, le=1_000_000)
    queue_limit: int = Field(ge=1, le=1_000_000)
    queued_by_deployment: dict[str, int] = Field(default_factory=dict, max_length=10_000)

    @field_validator("queued_by_deployment")
    @classmethod
    def validate_queued_by_deployment(cls, value: dict[str, int]) -> dict[str, int]:
        for deployment_id, count in value.items():
            if (
                len(deployment_id) != 71
                or not deployment_id.startswith("sha256:")
                or deployment_id != deployment_id.lower()
                or count < 1
                or count > 1_000_000
            ):
                raise ValueError("invalid per-deployment queue entry")
            try:
                int(deployment_id[7:], 16)
            except ValueError as exc:
                raise ValueError("invalid per-deployment queue entry") from exc
        return value

    @model_validator(mode="after")
    def admission_is_consistent(self) -> "Admission":
        if self.queue_depth != sum(self.queued_by_deployment.values()):
            raise ValueError("queue_depth must equal queued_by_deployment")
        if self.queue_depth > self.queue_limit:
            raise ValueError("queue_depth exceeds queue_limit")
        return self


class Artifact(ProtocolModel):
    format: str = Field(min_length=1, max_length=64)
    selected_files: tuple[str, ...] = Field(max_length=128)
    quantization: str | None = Field(default=None, max_length=128)
    content_digest: str | None

    @field_validator("selected_files")
    @classmethod
    def validate_selected_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("selected_files must be sorted and unique")
        for item in value:
            segments = item.split("/")
            if (
                not item
                or len(item) > 512
                or item.startswith("/")
                or "\\" in item
                or any(segment in {"", ".", ".."} for segment in segments)
            ):
                raise ValueError("selected_files must contain safe relative paths")
        return value

    @field_validator("content_digest")
    @classmethod
    def validate_content_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _validate_sha256(value)
        return value


class DeploymentIdentity(ProtocolModel):
    protocol: Literal[1]
    engine: str = Field(min_length=1, max_length=64)
    upstream_model: str = Field(min_length=1, max_length=512)
    resolved_revision: str | None = Field(default=None, max_length=256)
    artifact: Artifact
    kind: str = Field(min_length=1, max_length=64)
    capabilities: tuple[str, ...] = Field(min_length=1)
    load_config_digest: str

    @field_validator("upstream_model")
    @classmethod
    def validate_upstream_model(cls, value: str) -> str:
        if value.startswith("/") or (
            len(value) >= 3 and value[0].isalpha() and value[1:3] == ":\\"
        ):
            raise ValueError("upstream_model must not be an absolute path")
        return value

    @field_validator("capabilities")
    @classmethod
    def validate_identity_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if (
            tuple(sorted(set(value))) != value
            or not set(value).issubset(CAPABILITIES)
        ):
            raise ValueError("invalid identity capabilities")
        return value

    @field_validator("load_config_digest")
    @classmethod
    def validate_load_digest(cls, value: str) -> str:
        _validate_sha256(value)
        return value


class Deployment(ProtocolModel):
    alias: str = Field(min_length=1, max_length=128)
    deployment_id: str = Field(min_length=71, max_length=71)
    identity: DeploymentIdentity
    identity_confidence: Literal["authoritative", "unverified"]
    fleet_eligible: bool
    loadable: bool
    warm: bool
    capacity: Capacity

    @field_validator("deployment_id")
    @classmethod
    def validate_deployment_id(cls, value: str) -> str:
        _validate_sha256(value)
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> "Deployment":
        canonical = json.dumps(
            self.identity.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        expected = "sha256:" + hashlib.sha256(canonical).hexdigest()
        if self.deployment_id != expected:
            raise ValueError("deployment_id does not match canonical identity")
        if self.fleet_eligible and self.identity_confidence != "authoritative":
            raise ValueError("fleet-eligible deployments must be authoritative")
        immutable_revision = bool(
            self.identity.resolved_revision
            and re.fullmatch(
                r"[0-9a-f]{40,64}",
                self.identity.resolved_revision,
            )
        )
        if (
            self.fleet_eligible
            and not immutable_revision
            and self.identity.artifact.content_digest is None
        ):
            raise ValueError(
                "fleet-eligible deployments require an immutable revision or content digest"
            )
        return self


class UsageDelivery(ProtocolModel):
    enabled: bool
    writer_ready: bool
    outbox_pending: int = Field(ge=0, le=100_000_000)
    last_flush_at: float | None = Field(default=None, ge=0)
    last_error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9_]{1,64}$",
    )


class Snapshot(ProtocolModel):
    schema_version: Literal[1]
    snapshot_sequence: int = Field(ge=0)
    observed_at: float = Field(ge=0)
    node: NodeDescriptor
    health: Health
    residency: Residency
    admission: Admission
    capacity: Capacity
    deployments: tuple[Deployment, ...] = Field(max_length=10_000)
    usage_delivery: UsageDelivery

    @model_validator(mode="after")
    def validate_admission_capacity(self) -> "Snapshot":
        if self.capacity.queued != self.admission.queue_depth:
            raise ValueError("capacity.queued must equal admission.queue_depth")

        aliases = [deployment.alias for deployment in self.deployments]
        if len(aliases) != len(set(aliases)):
            raise ValueError("deployment aliases must be unique")
        deployment_ids = {
            deployment.deployment_id for deployment in self.deployments
        }
        queued_ids = set(self.admission.queued_by_deployment)
        if not queued_ids.issubset(deployment_ids):
            raise ValueError("queued deployment is not advertised")
        for deployment in self.deployments:
            expected_queued = self.admission.queued_by_deployment.get(
                deployment.deployment_id,
                0,
            )
            if deployment.capacity.queued != expected_queued:
                raise ValueError("deployment queued capacity is inconsistent")

        warm = [deployment for deployment in self.deployments if deployment.warm]
        warm_ids = {deployment.deployment_id for deployment in warm}
        if len(warm_ids) > 1:
            raise ValueError("at most one distinct deployment may be warm")

        resident_id = self.residency.deployment_id
        resident_alias = self.residency.alias
        resident_engine = self.residency.engine
        if resident_id is None:
            if resident_alias is not None or resident_engine is not None:
                raise ValueError("empty residency must not advertise alias or engine")
            if warm:
                raise ValueError("warm deployment requires residency")
        else:
            if resident_alias is None or resident_engine is None:
                raise ValueError("resident deployment requires alias and engine")
            matching_warm = [
                deployment
                for deployment in warm
                if deployment.deployment_id == resident_id
            ]
            if not matching_warm:
                raise ValueError("resident deployment must be warm")
            if resident_alias not in {
                deployment.alias for deployment in matching_warm
            }:
                raise ValueError("resident alias must identify a warm deployment")
            if any(
                deployment.identity.engine != resident_engine
                for deployment in matching_warm
            ):
                raise ValueError("resident engine must match warm deployment")

        transition_target = self.residency.transition_target
        if transition_target is not None:
            if transition_target not in deployment_ids:
                raise ValueError("transition target is not advertised")
            if self.health.state not in {
                "draining",
                "unloading",
                "loading",
                "verifying",
            }:
                raise ValueError("transition target is inconsistent with health state")
            if self.capacity.available != 0:
                raise ValueError("transition target must close root capacity")

        if self.health.state == "idle" and resident_id is not None:
            raise ValueError("idle state must not have a resident deployment")
        if self.health.state == "ready" and resident_id is None:
            raise ValueError("ready state requires a resident deployment")
        if self.health.accepting and not self.health.authoritative:
            raise ValueError("accepting health must be authoritative")
        if (
            self.health.state in {"degraded", "stopping"}
            and self.health.accepting
        ):
            raise ValueError("degraded or stopping health cannot accept requests")

        arithmetic_available = max(
            0, self.capacity.effective_limit - self.capacity.active
        )
        queued_other_target = any(
            deployment_id != self.residency.deployment_id
            for deployment_id in self.admission.queued_by_deployment
        )
        fully_open = (
            self.health.accepting
            and self.health.authoritative
            and self.health.state == "ready"
            and self.residency.transition_target is None
            and not queued_other_target
        )
        if fully_open and self.capacity.available != arithmetic_available:
            raise ValueError("ready accepting capacity must advertise free permits")
        if not self.health.accepting and self.capacity.available != 0:
            raise ValueError("closed admission must advertise zero available capacity")
        return self


def validate_snapshot(
    payload: object,
    *,
    expected_node_id: str,
    ttl_seconds: float,
    now: float | None = None,
) -> Snapshot:
    try:
        _SCHEMA_VALIDATOR.validate(payload)
    except SchemaValidationError as exc:
        raise ValueError("snapshot does not conform to protocol v1") from exc
    snapshot = Snapshot.model_validate(payload)
    if snapshot.node.node_id != expected_node_id:
        raise ValueError("snapshot node_id does not match enrollment")
    # Liveness is intentionally based on Nyx's monotonic receipt time. Node
    # wall clocks are not trusted as an expiry authority.
    return snapshot


def _validate_sha256(value: str) -> None:
    if (
        not value.startswith("sha256:")
        or len(value) != 71
        or value != value.lower()
    ):
        raise ValueError("value must be lowercase sha256")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ValueError("value must contain hex") from exc
