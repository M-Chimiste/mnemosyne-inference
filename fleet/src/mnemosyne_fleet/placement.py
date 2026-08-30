from __future__ import annotations

import hashlib
import importlib.resources
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Mapping, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError

from .compatibility_catalog import VerifiedCatalog
from .inventory_protocol import InventoryProtocolError, validate_inventory


PLACEMENT_SCHEMA_VERSION: Final[int] = 1
SCORER_VERSION: Final[str] = "mac-placement-v1"
MAX_RECOMMENDATION_BYTES: Final[int] = 2 * 1024 * 1024
MAX_PLACEMENT_REQUEST_BYTES: Final[int] = 16 * 1024
MAX_BYTE_COUNT: Final[int] = 1 << 60
STORAGE_FIXED_HEADROOM_BYTES: Final[int] = 5 * 1024**3
STORAGE_PROPORTIONAL_HEADROOM_PERCENT: Final[int] = 10

Capability = Literal[
    "chat/completions",
    "completions",
    "embeddings",
    "messages",
    "rerank",
    "responses",
]
Engine = Literal["ds4", "llama.cpp", "omlx"]
EvidenceClass = Literal[
    "measured",
    "catalog_tested",
    "calculated",
    "conservative",
]
ServiceClass = Literal["primary", "opportunistic", "overflow"]
RuntimeInstallMode = Literal["not_allowed", "managed", "local_approval"]
RecipeState = Literal["available", "known_bad", "revoked"]

_CAPABILITIES: Final[tuple[str, ...]] = (
    "chat/completions",
    "completions",
    "embeddings",
    "messages",
    "rerank",
    "responses",
)
_ENGINES: Final[tuple[str, ...]] = ("ds4", "llama.cpp", "omlx")
_EVIDENCE_CLASSES: Final[tuple[str, ...]] = (
    "measured",
    "catalog_tested",
    "calculated",
    "conservative",
)
_SERVICE_CLASSES: Final[tuple[str, ...]] = (
    "primary",
    "opportunistic",
    "overflow",
)
_RUNTIME_INSTALL_MODES: Final[tuple[str, ...]] = (
    "not_allowed",
    "managed",
    "local_approval",
)
_FRESHNESS_STATES: Final[tuple[str, ...]] = (
    "fresh",
    "expired",
    "hub_restarted",
)
_ENROLLMENT_STATES: Final[tuple[str, ...]] = (
    "active",
    "disabled",
    "revoked",
)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_DISPLAY_NAME = re.compile(r"^[^/\\\x00-\x1f]{1,64}$")
_SOC_COMPONENT = re.compile(r"[^a-z0-9]+")
_NUMERIC_VERSION = re.compile(r"^(?:v)?([0-9]+(?:\.[0-9]+)*)$")
_LLAMA_BUILD_VERSION = re.compile(r"^b([0-9]+)$")


class PlacementError(RuntimeError):
    """A fixed-code placement failure without caller-controlled text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PlacementInputError(PlacementError):
    pass


class PlacementProtocolError(PlacementError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryRequirements:
    weights_bytes: int
    runtime_overhead_bytes: int
    activation_overhead_bytes: int
    kv_bytes_per_token_per_slot: int
    safety_headroom_bytes: int
    max_recommended_concurrency: int
    evidence_class: EvidenceClass

    def __post_init__(self) -> None:
        for value in (
            self.weights_bytes,
            self.runtime_overhead_bytes,
            self.activation_overhead_bytes,
            self.kv_bytes_per_token_per_slot,
            self.safety_headroom_bytes,
        ):
            _require_integer(value, minimum=0, maximum=MAX_BYTE_COUNT)
        if self.weights_bytes < 1 or self.safety_headroom_bytes < 1:
            raise PlacementInputError("placement_recipe_invalid")
        _require_integer(
            self.max_recommended_concurrency,
            minimum=1,
            maximum=1024,
        )
        if self.evidence_class not in _EVIDENCE_CLASSES:
            raise PlacementInputError("placement_recipe_invalid")


@dataclass(frozen=True, slots=True)
class HardwareRequirements:
    architecture: str
    soc_families: tuple[str, ...]
    minimum_gpu_cores: int | None
    minimum_unified_memory_bytes: int
    minimum_macos: tuple[int, int]
    maximum_macos_exclusive: tuple[int, int] | None
    required_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.architecture != "arm64":
            raise PlacementInputError("placement_recipe_invalid")
        _require_sorted_unique(self.soc_families)
        if not self.soc_families:
            raise PlacementInputError("placement_recipe_invalid")
        for family in self.soc_families:
            if not re.fullmatch(r"apple-m[1-9][0-9]?(?:-(?:pro|max|ultra))?", family):
                raise PlacementInputError("placement_recipe_invalid")
        if self.minimum_gpu_cores is not None:
            _require_integer(self.minimum_gpu_cores, minimum=1, maximum=10_000)
        _require_integer(
            self.minimum_unified_memory_bytes,
            minimum=1,
            maximum=MAX_BYTE_COUNT,
        )
        _require_os_version(self.minimum_macos)
        if self.maximum_macos_exclusive is not None:
            _require_os_version(self.maximum_macos_exclusive)
            if self.maximum_macos_exclusive <= self.minimum_macos:
                raise PlacementInputError("placement_recipe_invalid")
        _require_sorted_unique(self.required_features)
        for feature in self.required_features:
            _require_safe_identifier(feature)


@dataclass(frozen=True, slots=True)
class RuntimeRequirements:
    release_tier: Literal["stable", "preview"]
    minimum_version: str
    maximum_version_exclusive: str | None
    known_bad_versions: tuple[str, ...]
    allowed_runtime_fingerprints: tuple[str, ...]
    known_bad_runtime_fingerprints: tuple[str, ...]
    required_features: tuple[str, ...]
    install_mode: RuntimeInstallMode = "not_allowed"

    def __post_init__(self) -> None:
        if self.release_tier not in {"stable", "preview"}:
            raise PlacementInputError("placement_recipe_invalid")
        _require_safe_version(self.minimum_version)
        if self.maximum_version_exclusive is not None:
            _require_safe_version(self.maximum_version_exclusive)
        _require_sorted_unique(self.known_bad_versions)
        for version in self.known_bad_versions:
            _require_safe_version(version)
        _require_sorted_unique(self.allowed_runtime_fingerprints)
        _require_sorted_unique(self.known_bad_runtime_fingerprints)
        for digest in (
            *self.allowed_runtime_fingerprints,
            *self.known_bad_runtime_fingerprints,
        ):
            _require_sha256(digest)
        if set(self.allowed_runtime_fingerprints) & set(
            self.known_bad_runtime_fingerprints
        ):
            raise PlacementInputError("placement_recipe_invalid")
        _require_sorted_unique(self.required_features)
        for feature in self.required_features:
            _require_safe_identifier(feature)
        if self.install_mode not in _RUNTIME_INSTALL_MODES:
            raise PlacementInputError("placement_recipe_invalid")


@dataclass(frozen=True, slots=True)
class LlamaCppLaunchRequirements:
    engine: Literal["llama.cpp"]
    parallel_slots: int
    gpu_offload: Literal["all", "automatic"]
    flash_attention: Literal["automatic", "disabled", "enabled"]

    def __post_init__(self) -> None:
        if (
            self.engine != "llama.cpp"
            or self.gpu_offload not in {"all", "automatic"}
            or self.flash_attention not in {"automatic", "disabled", "enabled"}
        ):
            raise PlacementInputError("placement_recipe_invalid")
        _require_integer(self.parallel_slots, minimum=1, maximum=1024)

    @property
    def concurrency_slots(self) -> int:
        return self.parallel_slots


@dataclass(frozen=True, slots=True)
class OmlxLaunchRequirements:
    engine: Literal["omlx"]
    scheduler_slots: int
    memory_guard: Literal["optional", "required"]

    def __post_init__(self) -> None:
        if self.engine != "omlx" or self.memory_guard not in {
            "optional",
            "required",
        }:
            raise PlacementInputError("placement_recipe_invalid")
        _require_integer(self.scheduler_slots, minimum=1, maximum=1024)

    @property
    def concurrency_slots(self) -> int:
        return self.scheduler_slots


@dataclass(frozen=True, slots=True)
class DS4LaunchRequirements:
    engine: Literal["ds4"]
    batched_sessions: int
    execution_mode: Literal["single-node"]

    def __post_init__(self) -> None:
        if self.engine != "ds4" or self.execution_mode != "single-node":
            raise PlacementInputError("placement_recipe_invalid")
        _require_integer(self.batched_sessions, minimum=1, maximum=1024)

    @property
    def concurrency_slots(self) -> int:
        return self.batched_sessions


LaunchRequirements = (
    LlamaCppLaunchRequirements | OmlxLaunchRequirements | DS4LaunchRequirements
)


@dataclass(frozen=True, slots=True)
class RecipeRequirements:
    catalog_version: str
    catalog_digest: str
    catalog_issued_at: float
    logical_model_id: str
    recipe_id: str
    artifact_id: str
    engine: Engine
    recipe_status: Literal["available", "known_bad", "revoked"]
    compatibility_tier: Literal["verified", "experimental"]
    capabilities: tuple[Capability, ...]
    guaranteed_context_tokens: int
    artifact_total_size_bytes: int
    memory: MemoryRequirements
    hardware: HardwareRequirements
    runtime: RuntimeRequirements
    launch: LaunchRequirements
    recipe_evidence_class: EvidenceClass
    context_evidence_class: EvidenceClass

    def __post_init__(self) -> None:
        _require_safe_version(self.catalog_version)
        _require_sha256(self.catalog_digest)
        _require_timestamp(self.catalog_issued_at)
        for identifier in (
            self.logical_model_id,
            self.recipe_id,
            self.artifact_id,
        ):
            _require_safe_identifier(identifier)
        if self.engine not in _ENGINES:
            raise PlacementInputError("placement_recipe_invalid")
        if self.recipe_status not in {"available", "known_bad", "revoked"}:
            raise PlacementInputError("placement_recipe_invalid")
        if self.compatibility_tier not in {"verified", "experimental"}:
            raise PlacementInputError("placement_recipe_invalid")
        _require_sorted_unique(self.capabilities)
        if not self.capabilities or not set(self.capabilities).issubset(_CAPABILITIES):
            raise PlacementInputError("placement_recipe_invalid")
        _require_integer(
            self.guaranteed_context_tokens,
            minimum=1,
            maximum=100_000_000,
        )
        _require_integer(
            self.artifact_total_size_bytes,
            minimum=1,
            maximum=MAX_BYTE_COUNT,
        )
        if self.memory.weights_bytes < self.artifact_total_size_bytes:
            raise PlacementInputError("placement_recipe_invalid")
        if (
            self.launch.engine != self.engine
            or self.launch.concurrency_slots
            > self.memory.max_recommended_concurrency
        ):
            raise PlacementInputError("placement_recipe_invalid")
        if (
            self.recipe_evidence_class not in _EVIDENCE_CLASSES
            or self.context_evidence_class not in _EVIDENCE_CLASSES
        ):
            raise PlacementInputError("placement_recipe_invalid")

    @classmethod
    def from_verified_catalog(
        cls,
        catalog: VerifiedCatalog,
        *,
        request: "PlacementRequest",
        recipe_state: RecipeState = "available",
        runtime_install_mode: RuntimeInstallMode = "not_allowed",
    ) -> "RecipeRequirements":
        """Resolve one exact request from an already verified signed catalog.

        Caller input supplies identity and requested service guarantees only.
        Artifact size, compatibility, memory, hardware, runtime, evidence, and
        launch facts are copied exclusively from the canonical catalog bytes.
        """

        if not isinstance(catalog, VerifiedCatalog):
            raise PlacementInputError("placement_catalog_unverified") from None
        if catalog.source == "built_in" or catalog.catalog_version == "offline-empty":
            raise PlacementInputError("placement_catalog_offline")
        if catalog.source != "signed":
            raise PlacementInputError("placement_catalog_unverified")
        if catalog.expires_at is None or catalog.expires_at <= request.created_at:
            raise PlacementInputError("placement_catalog_expired")
        # Verification-time skew does not extend placement authority. A
        # request must observe a catalog whose issue time is already current.
        if catalog.issued_at > request.created_at:
            raise PlacementInputError("placement_catalog_not_yet_valid")
        if recipe_state not in {"available", "known_bad", "revoked"}:
            raise PlacementInputError("placement_recipe_invalid")
        if runtime_install_mode not in _RUNTIME_INSTALL_MODES:
            raise PlacementInputError("placement_recipe_invalid")
        try:
            catalog_value = catalog.catalog()
        except (TypeError, ValueError, json.JSONDecodeError):
            raise PlacementInputError("placement_catalog_invalid") from None
        if not isinstance(catalog_value, dict):
            raise PlacementInputError("placement_catalog_invalid")

        models = _matching_catalog_rows(
            catalog_value,
            collection="logical_models",
            key="logical_model_id",
            value=request.logical_model_id,
            missing_code="placement_logical_model_missing",
        )
        recipes = _matching_catalog_rows(
            catalog_value,
            collection="recipes",
            key="recipe_id",
            value=request.recipe_id,
            missing_code="placement_recipe_missing",
        )
        model = models[0]
        recipe = recipes[0]
        # This is a denial-only Hub policy overlay. It is deliberately applied
        # only after the exact signed recipe is proven to exist; it can never
        # synthesize or repair a missing catalog row.
        if recipe_state == "known_bad":
            raise PlacementInputError("placement_recipe_known_bad")
        if recipe_state == "revoked":
            raise PlacementInputError("placement_recipe_revoked")
        try:
            recipe_model_id = recipe["logical_model_id"]
            artifact_id = recipe["artifact_id"]
        except (KeyError, TypeError):
            raise PlacementInputError("placement_catalog_reference_mismatch") from None
        if (
            recipe_model_id != request.logical_model_id
            or model.get("logical_model_id") != request.logical_model_id
        ):
            raise PlacementInputError("placement_catalog_reference_mismatch")
        artifacts = _matching_catalog_rows(
            catalog_value,
            collection="artifacts",
            key="artifact_id",
            value=artifact_id,
            missing_code="placement_catalog_reference_mismatch",
        )
        artifact = artifacts[0]
        if artifact.get("logical_model_id") != request.logical_model_id:
            raise PlacementInputError("placement_catalog_reference_mismatch")
        try:
            engine = recipe["engine"]
            artifact_format = artifact["format"]
            runtime = recipe["runtime"]
            launch = recipe["launch"]
            if (
                engine not in _ENGINES
                or artifact_format
                != {"llama.cpp": "gguf", "omlx": "mlx", "ds4": "ds4-weights"}[
                    engine
                ]
            ):
                raise PlacementInputError("placement_engine_format_unsupported")
            if runtime["engine"] != engine or launch["engine"] != engine:
                raise PlacementInputError("placement_catalog_reference_mismatch")
            memory = recipe["memory"]
            hardware = recipe["hardware"]
            context = recipe["context"]
            normalized = cls(
                catalog_version=catalog.catalog_version,
                catalog_digest=catalog.catalog_digest,
                catalog_issued_at=float(catalog.issued_at),
                logical_model_id=request.logical_model_id,
                recipe_id=request.recipe_id,
                artifact_id=artifact_id,
                engine=engine,
                recipe_status="available",
                compatibility_tier=recipe["compatibility_tier"],
                capabilities=tuple(recipe["capabilities"]),
                guaranteed_context_tokens=int(context["guaranteed_tokens"]),
                artifact_total_size_bytes=int(artifact["total_size_bytes"]),
                memory=MemoryRequirements(
                    weights_bytes=int(memory["weights_bytes"]),
                    runtime_overhead_bytes=int(memory["runtime_overhead_bytes"]),
                    activation_overhead_bytes=int(memory["activation_overhead_bytes"]),
                    kv_bytes_per_token_per_slot=int(
                        memory["kv_bytes_per_token_per_slot"]
                    ),
                    safety_headroom_bytes=int(memory["safety_headroom_bytes"]),
                    max_recommended_concurrency=int(
                        memory["max_recommended_concurrency"]
                    ),
                    evidence_class=_conservative_catalog_evidence(
                        memory["estimate_class"],
                        memory["evidence"]["evidence_class"],
                    ),
                ),
                hardware=HardwareRequirements(
                    architecture=hardware["architecture"],
                    soc_families=tuple(hardware["soc_families"]),
                    minimum_gpu_cores=hardware["minimum_gpu_cores"],
                    minimum_unified_memory_bytes=int(
                        hardware["minimum_unified_memory_bytes"]
                    ),
                    minimum_macos=(
                        int(hardware["minimum_macos"]["major"]),
                        int(hardware["minimum_macos"]["minor"]),
                    ),
                    maximum_macos_exclusive=(
                        None
                        if hardware["maximum_macos_exclusive"] is None
                        else (
                            int(hardware["maximum_macos_exclusive"]["major"]),
                            int(hardware["maximum_macos_exclusive"]["minor"]),
                        )
                    ),
                    required_features=tuple(hardware["required_features"]),
                ),
                runtime=RuntimeRequirements(
                    release_tier=runtime["release_tier"],
                    minimum_version=runtime["minimum_version"],
                    maximum_version_exclusive=runtime[
                        "maximum_version_exclusive"
                    ],
                    known_bad_versions=tuple(runtime["known_bad_versions"]),
                    allowed_runtime_fingerprints=tuple(
                        runtime["allowed_runtime_fingerprints"]
                    ),
                    known_bad_runtime_fingerprints=tuple(
                        runtime["known_bad_runtime_fingerprints"]
                    ),
                    required_features=tuple(runtime["required_features"]),
                    install_mode=runtime_install_mode,
                ),
                launch=_launch_requirements(engine, launch),
                recipe_evidence_class=_catalog_evidence_class(
                    recipe["evidence"]["evidence_class"]
                ),
                context_evidence_class=_catalog_evidence_class(
                    context["evidence"]["evidence_class"]
                ),
            )
        except PlacementInputError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError):
            raise PlacementInputError("placement_recipe_invalid") from None
        _enforce_request_contract(request, normalized, model)
        return normalized


@dataclass(frozen=True, slots=True)
class PlacementRequest:
    recommendation_id: str
    created_at: float
    valid_for_seconds: int
    logical_model_id: str
    recipe_id: str
    required_capabilities: tuple[Capability, ...]
    required_context_tokens: int
    required_concurrency: int
    allowed_service_classes: tuple[ServiceClass, ...] = (  # type: ignore[assignment]
        _SERVICE_CLASSES
    )

    def __post_init__(self) -> None:
        _require_uuid(self.recommendation_id)
        _require_timestamp(self.created_at)
        _require_integer(self.valid_for_seconds, minimum=1, maximum=300)
        _require_safe_identifier(self.logical_model_id)
        _require_safe_identifier(self.recipe_id)
        _require_sorted_unique(self.required_capabilities)
        if not self.required_capabilities or not set(
            self.required_capabilities
        ).issubset(_CAPABILITIES):
            raise PlacementInputError("placement_request_invalid")
        _require_integer(
            self.required_context_tokens,
            minimum=1,
            maximum=100_000_000,
        )
        _require_integer(self.required_concurrency, minimum=1, maximum=1024)
        if (
            not self.allowed_service_classes
            or len(set(self.allowed_service_classes))
            != len(self.allowed_service_classes)
            or any(
                item not in _SERVICE_CLASSES for item in self.allowed_service_classes
            )
        ):
            raise PlacementInputError("placement_request_invalid")
        canonical_classes = tuple(
            item for item in _SERVICE_CLASSES if item in self.allowed_service_classes
        )
        object.__setattr__(self, "allowed_service_classes", canonical_classes)
        if self.created_at + self.valid_for_seconds > 4_102_444_800:
            raise PlacementInputError("placement_request_invalid")

    def wire_value(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "recommendation_id": self.recommendation_id,
            "created_at": float(self.created_at),
            "valid_for_seconds": self.valid_for_seconds,
            "logical_model_id": self.logical_model_id,
            "recipe_id": self.recipe_id,
            "required_capabilities": list(self.required_capabilities),
            "required_context_tokens": self.required_context_tokens,
            "required_concurrency": self.required_concurrency,
            "allowed_service_classes": list(self.allowed_service_classes),
        }


_PLACEMENT_REQUEST_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "recommendation_id",
        "created_at",
        "valid_for_seconds",
        "logical_model_id",
        "recipe_id",
        "required_capabilities",
        "required_context_tokens",
        "required_concurrency",
        "allowed_service_classes",
    }
)


def validate_placement_request(value: object) -> PlacementRequest:
    """Validate the closed caller intent document without reflecting input."""

    if not isinstance(value, dict) or set(value) != _PLACEMENT_REQUEST_KEYS:
        raise PlacementInputError("placement_request_invalid")
    if value.get("schema_version") != 1:
        raise PlacementInputError("placement_request_invalid")
    capabilities = value.get("required_capabilities")
    service_classes = value.get("allowed_service_classes")
    if not isinstance(capabilities, list) or not isinstance(service_classes, list):
        raise PlacementInputError("placement_request_invalid")
    try:
        capabilities_canonical = capabilities == sorted(set(capabilities))
        services_canonical = service_classes == [
            item for item in _SERVICE_CLASSES if item in service_classes
        ] and len(service_classes) == len(set(service_classes))
    except TypeError:
        raise PlacementInputError("placement_request_invalid") from None
    if not capabilities_canonical or not services_canonical:
        raise PlacementInputError("placement_request_invalid")
    try:
        request = PlacementRequest(
            recommendation_id=value["recommendation_id"],
            created_at=value["created_at"],
            valid_for_seconds=value["valid_for_seconds"],
            logical_model_id=value["logical_model_id"],
            recipe_id=value["recipe_id"],
            required_capabilities=tuple(capabilities),
            required_context_tokens=value["required_context_tokens"],
            required_concurrency=value["required_concurrency"],
            allowed_service_classes=tuple(service_classes),
        )
    except (KeyError, TypeError, ValueError, PlacementInputError):
        raise PlacementInputError("placement_request_invalid") from None
    encoded = json.dumps(
        request.wire_value(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_PLACEMENT_REQUEST_BYTES:
        raise PlacementInputError("placement_request_too_large")
    return request


def parse_placement_request(raw: bytes | str) -> PlacementRequest:
    """Parse bounded UTF-8 JSON with duplicate/non-finite rejection."""

    if isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8")
        except UnicodeError:
            raise PlacementInputError("placement_request_invalid") from None
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise PlacementInputError("placement_request_invalid")
    if len(encoded) > MAX_PLACEMENT_REQUEST_BYTES:
        raise PlacementInputError("placement_request_too_large")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate member")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        raise PlacementInputError("placement_request_invalid") from None
    return validate_placement_request(value)


def _matching_catalog_rows(
    catalog: Mapping[str, Any],
    *,
    collection: str,
    key: str,
    value: str,
    missing_code: str,
) -> list[dict[str, Any]]:
    rows = catalog.get(collection)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PlacementInputError("placement_catalog_invalid")
    matches = [row for row in rows if row.get(key) == value]
    if not matches:
        raise PlacementInputError(missing_code)
    if len(matches) != 1:
        raise PlacementInputError("placement_catalog_reference_ambiguous")
    return matches


def _launch_requirements(engine: str, launch: Mapping[str, Any]) -> LaunchRequirements:
    if engine == "llama.cpp":
        return LlamaCppLaunchRequirements(
            engine="llama.cpp",
            parallel_slots=int(launch["parallel_slots"]),
            gpu_offload=launch["gpu_offload"],
            flash_attention=launch["flash_attention"],
        )
    if engine == "omlx":
        return OmlxLaunchRequirements(
            engine="omlx",
            scheduler_slots=int(launch["scheduler_slots"]),
            memory_guard=launch["memory_guard"],
        )
    if engine == "ds4":
        return DS4LaunchRequirements(
            engine="ds4",
            batched_sessions=int(launch["batched_sessions"]),
            execution_mode=launch["execution_mode"],
        )
    raise PlacementInputError("placement_engine_format_unsupported")


def _enforce_request_contract(
    request: PlacementRequest,
    requirements: RecipeRequirements,
    logical_model: Mapping[str, Any],
) -> None:
    model_capabilities = logical_model.get("capabilities")
    declared_context = logical_model.get("declared_max_context_tokens")
    if not isinstance(model_capabilities, list):
        raise PlacementInputError("placement_catalog_reference_mismatch")
    if not set(requirements.capabilities).issubset(model_capabilities):
        raise PlacementInputError("placement_catalog_reference_mismatch")
    if (
        declared_context is not None
        and (
            isinstance(declared_context, bool)
            or not isinstance(declared_context, int)
            or requirements.guaranteed_context_tokens > declared_context
        )
    ):
        raise PlacementInputError("placement_catalog_reference_mismatch")
    if not set(request.required_capabilities).issubset(
        requirements.capabilities
    ):
        raise PlacementInputError("placement_capability_unsupported")
    if request.required_context_tokens > requirements.guaranteed_context_tokens:
        raise PlacementInputError("placement_context_unsupported")
    if request.required_concurrency > min(
        requirements.memory.max_recommended_concurrency,
        requirements.launch.concurrency_slots,
    ):
        raise PlacementInputError("placement_concurrency_unsupported")


@dataclass(frozen=True, slots=True)
class PlacementCandidateInput:
    pairing_id: str
    pairing_display_name: str
    service_class: ServiceClass
    enrollment_state: Literal["active", "disabled", "revoked"]
    active_credential_generation: int | None
    freshness_state: Literal["fresh", "expired", "hub_restarted"]
    inventory_received_at: float
    basis_expires_at: float
    inventory: Mapping[str, Any] = field(repr=False)
    hub_remote_installs_enabled: bool = True
    storage_binding_fences: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        _require_uuid(self.pairing_id)
        if not _DISPLAY_NAME.fullmatch(self.pairing_display_name):
            raise PlacementInputError("placement_candidate_invalid")
        if self.service_class not in _SERVICE_CLASSES:
            raise PlacementInputError("placement_candidate_invalid")
        if self.enrollment_state not in _ENROLLMENT_STATES:
            raise PlacementInputError("placement_candidate_invalid")
        if self.active_credential_generation is not None:
            _require_integer(
                self.active_credential_generation,
                minimum=1,
                maximum=2_147_483_647,
            )
        if self.enrollment_state == "active" and self.active_credential_generation is None:
            raise PlacementInputError("placement_candidate_invalid")
        if self.freshness_state not in _FRESHNESS_STATES:
            raise PlacementInputError("placement_candidate_invalid")
        _require_timestamp(self.inventory_received_at)
        _require_timestamp(self.basis_expires_at)
        if self.basis_expires_at < self.inventory_received_at:
            raise PlacementInputError("placement_candidate_invalid")
        if not isinstance(self.hub_remote_installs_enabled, bool):
            raise PlacementInputError("placement_candidate_invalid")
        try:
            document = validate_inventory(dict(self.inventory))
        except InventoryProtocolError:
            raise PlacementInputError("placement_inventory_invalid") from None
        inventory = json.loads(document.canonical_json)
        hardware = inventory["hardware"]
        if hardware["allocatable_memory_bytes"] > hardware["unified_memory_bytes"]:
            raise PlacementInputError("placement_inventory_invalid")
        storage_ids: set[str] = set()
        for storage in inventory["storage_locations"]:
            storage_id = storage["storage_location_id"]
            if storage_id in storage_ids:
                raise PlacementInputError("placement_inventory_invalid")
            storage_ids.add(storage_id)
            total = storage["total_bytes"]
            free = storage["free_bytes"]
            if total is not None and free is not None and free > total:
                raise PlacementInputError("placement_inventory_invalid")
        runtime_engines = [row["engine"] for row in inventory["runtimes"]]
        if len(runtime_engines) != len(set(runtime_engines)):
            raise PlacementInputError("placement_inventory_invalid")
        fences: list[tuple[str, int]] = []
        for item in self.storage_binding_fences:
            if not isinstance(item, tuple) or len(item) != 2:
                raise PlacementInputError("placement_candidate_invalid")
            storage_id, generation = item
            _require_uuid(storage_id)
            _require_integer(generation, minimum=1, maximum=2_147_483_647)
            fences.append((storage_id, generation))
        if fences != sorted(set(fences)):
            raise PlacementInputError("placement_candidate_invalid")
        object.__setattr__(self, "inventory", inventory)


@dataclass(frozen=True, slots=True)
class PlacementRecommendationDocument:
    value: dict[str, Any] = field(repr=False)
    canonical_json: bytes = field(repr=False)
    payload_digest: str


@dataclass(slots=True)
class _CandidateWork:
    value: dict[str, Any]
    rank: tuple[Any, ...]


@dataclass(slots=True)
class _ReasonSet:
    reasons: dict[str, dict[str, Any]] = field(default_factory=dict)
    hard_gates: set[str] = field(default_factory=set)

    def add(
        self,
        code: str,
        *,
        evidence_class: EvidenceClass,
        observed_at: float,
        hard: bool = False,
    ) -> None:
        if code not in REASON_EXPLANATIONS:
            raise AssertionError(f"unknown fixed placement reason: {code}")
        current = self.reasons.get(code)
        candidate = {
            "code": code,
            "evidence_class": evidence_class,
            "observed_at": float(observed_at),
            "explanation": REASON_EXPLANATIONS[code],
        }
        if current is None or _evidence_rank(evidence_class) < _evidence_rank(
            current["evidence_class"]
        ):
            self.reasons[code] = candidate
        if hard:
            self.hard_gates.add(code)


REASON_EXPLANATIONS: Final[dict[str, str]] = {
    "architecture_unsupported": "The recipe does not support this architecture.",
    "capability_unsupported": "The recipe does not guarantee every requested API capability.",
    "catalog_mismatch": "The Mac has not activated the exact signed catalog used for this recommendation.",
    "concurrency_unsupported": "Requested concurrency exceeds the recipe's conservative memory contract.",
    "context_unsupported": "Requested context exceeds the recipe's guaranteed context contract.",
    "credential_generation_changed": "The inventory credential generation is no longer active.",
    "exact_artifact_reuse": "This exact signed artifact is already verified on the selected storage binding.",
    "gpu_requirement_unmet": "Available GPU-core evidence does not satisfy the recipe minimum.",
    "hardware_feature_evidence_missing": "The bound Mac facts do not prove every hardware feature required by this recipe.",
    "hub_remote_installs_disabled": "Hub policy does not allow a new remote install on this pairing.",
    "hub_restarted": "Nyx restarted after receiving this inventory; a fresh authenticated sync is required.",
    "insufficient_memory_budget": "The Mac's owner-defined allocatable memory is below conservative peak demand.",
    "insufficient_storage": "The selected storage lacks conservative download and safety headroom.",
    "inventory_identity_mismatch": "The inventory is not bound to the expected pairing identity.",
    "local_approval_required": "The Mac owner must approve this remote install locally before download.",
    "low_power_mode": "Low Power Mode lowers this otherwise eligible candidate's preference.",
    "memory_estimate_unavailable": "The conservative peak-memory calculation is outside the supported bound.",
    "memory_headroom_available": "The owner-defined memory budget retains conservative headroom.",
    "os_unsupported": "The installed macOS version is outside the recipe's supported range.",
    "pairing_inactive": "The Hub pairing is disabled and cannot receive a new desired install.",
    "pairing_revoked": "The Hub pairing is revoked and has no management authority.",
    "participation_draining": "Inference participation is draining; install eligibility remains independent.",
    "participation_paused": "Inference participation is paused; install eligibility remains independent.",
    "platform_unsupported": "Mac placement version 1 supports only macOS workers.",
    "power_ac": "The Mac reports AC power without Low Power Mode.",
    "power_battery": "Battery power lowers this otherwise eligible candidate's preference.",
    "pressure_deprioritized": "Current pressure evidence lowers this otherwise eligible candidate's preference.",
    "recipe_known_bad": "The signed catalog or Hub policy marks this exact recipe unavailable.",
    "recipe_not_verified": "Experimental recipes are not eligible for managed placement.",
    "remote_installs_local_only": "The Mac or selected storage permits local installs only.",
    "runtime_catalog_mismatch": "Runtime status was evaluated against a different signed catalog.",
    "runtime_compatible_unverified": "The runtime matches conservative version constraints without an exact fingerprint.",
    "runtime_compatible_verified": "The exact runtime fingerprint is approved by the signed recipe.",
    "runtime_disabled": "The required engine is disabled on this Mac.",
    "runtime_feature_evidence_missing": "The bound runtime facts do not prove every runtime feature required by this recipe.",
    "runtime_fingerprint_mismatch": "The runtime fingerprint is not approved by the signed recipe.",
    "runtime_install_approval_required": "The required external runtime needs a separate local preparation and approval step; DesiredInstall v1 cannot perform it.",
    "runtime_install_required": "The required managed runtime must be prepared locally before model registration; DesiredInstall v1 cannot perform it.",
    "runtime_known_bad": "The installed runtime version or fingerprint is explicitly known bad.",
    "runtime_unavailable": "The required runtime is absent or unavailable; DesiredInstall v1 cannot prepare runtimes.",
    "runtime_unhealthy": "The required runtime is not authoritatively healthy.",
    "runtime_version_mismatch": "The runtime version cannot prove the signed recipe's bounded requirement.",
    "service_class_excluded": "The requested placement policy excludes this Hub-owned service class.",
    "service_opportunistic": "This Mac is enrolled in the opportunistic service class.",
    "service_overflow": "This limited worker is enrolled in the overflow service class.",
    "service_primary": "This Mac is enrolled in the primary service class.",
    "soc_unsupported": "The Apple SoC family is not approved by the signed recipe.",
    "stale_inventory": "The authenticated inventory basis has expired and cannot authorize new placement.",
    "storage_binding_changed": "The storage binding generation changed after the expected basis.",
    "storage_capacity_unknown": "The selected storage does not have authoritative free-space evidence.",
    "storage_headroom_available": "The selected storage retains conservative post-download headroom.",
    "storage_not_registered": "This Mac has no registered storage binding to select.",
    "storage_not_writable": "The selected registered storage is not writable.",
    "storage_unavailable": "The selected registered storage is unavailable or unhealthy.",
    "unified_memory_requirement_unmet": "Installed unified memory is below the recipe's hardware minimum.",
}

HARD_GATE_CODES: Final[frozenset[str]] = frozenset(
    {
        "architecture_unsupported",
        "capability_unsupported",
        "catalog_mismatch",
        "concurrency_unsupported",
        "context_unsupported",
        "credential_generation_changed",
        "gpu_requirement_unmet",
        "hardware_feature_evidence_missing",
        "hub_remote_installs_disabled",
        "hub_restarted",
        "insufficient_memory_budget",
        "insufficient_storage",
        "inventory_identity_mismatch",
        "memory_estimate_unavailable",
        "os_unsupported",
        "pairing_inactive",
        "pairing_revoked",
        "platform_unsupported",
        "recipe_known_bad",
        "recipe_not_verified",
        "remote_installs_local_only",
        "runtime_catalog_mismatch",
        "runtime_disabled",
        "runtime_feature_evidence_missing",
        "runtime_fingerprint_mismatch",
        "runtime_known_bad",
        "runtime_unavailable",
        "runtime_unhealthy",
        "runtime_version_mismatch",
        "service_class_excluded",
        "soc_unsupported",
        "stale_inventory",
        "storage_binding_changed",
        "storage_capacity_unknown",
        "storage_not_registered",
        "storage_not_writable",
        "storage_unavailable",
        "unified_memory_requirement_unmet",
    }
)


def _load_schema() -> dict[str, Any]:
    packaged = importlib.resources.files("mnemosyne_fleet").joinpath(
        "schemas", "placement_recommendation.schema.json"
    )
    if packaged.is_file():
        raw = packaged.read_text(encoding="utf-8")
    else:
        canonical = (
            Path(__file__).resolve().parents[3]
            / "mac_pool_protocol"
            / "v1"
            / "placement_recommendation.schema.json"
        )
        raw = canonical.read_text(encoding="utf-8")
    schema = json.loads(raw)
    Draft202012Validator.check_schema(schema)
    return schema


_SCHEMA = _load_schema()
_SCHEMA_VALIDATOR = Draft202012Validator(_SCHEMA)


class PlacementScorer:
    """Pure deterministic Mac/storage placement scorer.

    Scoring neither mutates Fleet routing nor creates desired jobs. Candidate
    inventory is authoritative only for this short-lived advisory result; the
    selected Mac must revalidate every basis field before any local work.
    """

    def score(
        self,
        request: PlacementRequest,
        requirements: RecipeRequirements,
        candidates: Sequence[PlacementCandidateInput],
    ) -> PlacementRecommendationDocument:
        if len(candidates) > 4096:
            raise PlacementInputError("placement_too_many_candidates")
        if (
            request.logical_model_id != requirements.logical_model_id
            or request.recipe_id != requirements.recipe_id
        ):
            raise PlacementInputError("placement_request_recipe_mismatch")
        duplicate_pairings = [candidate.pairing_id for candidate in candidates]
        if len(duplicate_pairings) != len(set(duplicate_pairings)):
            raise PlacementInputError("placement_candidate_duplicate")

        global_reasons = _global_recipe_reasons(request, requirements)
        scored: list[_CandidateWork] = []
        for candidate in candidates:
            storage_rows: list[dict[str, Any] | None] = list(
                candidate.inventory["storage_locations"]
            )
            if not storage_rows:
                storage_rows = [None]
            for storage in storage_rows:
                scored.append(
                    self._score_candidate(
                        request,
                        requirements,
                        candidate,
                        storage,
                        global_reasons,
                    )
                )
        if len(scored) > 4096:
            raise PlacementInputError("placement_too_many_candidates")

        eligible = sorted(
            (candidate for candidate in scored if candidate.value["eligible"]),
            key=lambda candidate: candidate.rank,
        )
        ineligible = sorted(
            (candidate for candidate in scored if not candidate.value["eligible"]),
            key=lambda candidate: _basis_sort_key(candidate.value["basis"]),
        )
        ordered: list[dict[str, Any]] = []
        for index, candidate in enumerate(eligible, start=1):
            candidate.value["order"] = index
            ordered.append(candidate.value)
        ordered.extend(candidate.value for candidate in ineligible)

        requested_expiry = request.created_at + request.valid_for_seconds
        if eligible:
            expires_at = min(
                requested_expiry,
                *(candidate.value["basis"]["basis_expires_at"] for candidate in eligible),
            )
        else:
            # An all-ineligible result is useful for explanation but cannot be
            # selected, so it is immediately expired by construction.
            expires_at = request.created_at
        value = {
            "schema_version": PLACEMENT_SCHEMA_VERSION,
            "recommendation_id": request.recommendation_id,
            "scorer_version": SCORER_VERSION,
            "created_at": float(request.created_at),
            "expires_at": float(expires_at),
            "catalog_version": requirements.catalog_version,
            "catalog_digest": requirements.catalog_digest,
            "request": {
                "logical_model_id": requirements.logical_model_id,
                "recipe_id": requirements.recipe_id,
                "artifact_id": requirements.artifact_id,
                "engine": requirements.engine,
                "required_capabilities": list(request.required_capabilities),
                "required_context_tokens": request.required_context_tokens,
                "required_concurrency": request.required_concurrency,
                "allowed_service_classes": list(request.allowed_service_classes),
            },
            "candidates": ordered,
        }
        return validate_recommendation(value)

    def _score_candidate(
        self,
        request: PlacementRequest,
        requirements: RecipeRequirements,
        candidate: PlacementCandidateInput,
        storage: dict[str, Any] | None,
        global_reasons: tuple[tuple[str, EvidenceClass, float], ...],
    ) -> _CandidateWork:
        inventory = candidate.inventory
        hardware = inventory["hardware"]
        reasons = _ReasonSet()
        for code, evidence, observed_at in global_reasons:
            reasons.add(
                code,
                evidence_class=evidence,
                observed_at=observed_at,
                hard=True,
            )
        _add_authority_reasons(request, requirements, candidate, reasons)
        _add_hardware_reasons(requirements, inventory, reasons)
        runtime_state, runtime_evidence, runtime_rank = _evaluate_runtime(
            requirements,
            inventory,
            reasons,
        )

        peak_memory = _estimate_peak_memory(request, requirements)
        if peak_memory is None:
            memory_headroom = None
            reasons.add(
                "memory_estimate_unavailable",
                evidence_class="conservative",
                observed_at=requirements.catalog_issued_at,
                hard=True,
            )
        else:
            allocatable = int(hardware["allocatable_memory_bytes"])
            memory_headroom = allocatable - peak_memory
            if memory_headroom < 0:
                reasons.add(
                    "insufficient_memory_budget",
                    evidence_class=requirements.memory.evidence_class,
                    observed_at=requirements.catalog_issued_at,
                    hard=True,
                )
            else:
                reasons.add(
                    "memory_headroom_available",
                    evidence_class=requirements.memory.evidence_class,
                    observed_at=requirements.catalog_issued_at,
                )

        exact_reuse, artifact_evidence, artifact_observed_at = _exact_artifact_reuse(
            requirements,
            inventory,
            storage,
        )
        if exact_reuse:
            reasons.add(
                "exact_artifact_reuse",
                evidence_class=artifact_evidence,
                observed_at=artifact_observed_at,
            )
        estimated_download = 0 if exact_reuse else requirements.artifact_total_size_bytes
        storage_headroom, storage_evidence, requires_local_approval = _evaluate_storage(
            candidate,
            inventory,
            storage,
            estimated_download,
            exact_reuse,
            reasons,
        )
        if runtime_state == "install_requires_approval":
            requires_local_approval = True
        participation = inventory["participation"]["state"]
        if participation == "paused":
            reasons.add(
                "participation_paused",
                evidence_class="measured",
                observed_at=inventory["observed_at"],
            )
        elif participation == "draining":
            reasons.add(
                "participation_draining",
                evidence_class="measured",
                observed_at=inventory["observed_at"],
            )
        power_policy, power_rank = _power_policy(hardware, reasons)
        service_code = {
            "primary": "service_primary",
            "opportunistic": "service_opportunistic",
            "overflow": "service_overflow",
        }[candidate.service_class]
        reasons.add(
            service_code,
            evidence_class="conservative",
            observed_at=candidate.inventory_received_at,
        )

        eligible = not reasons.hard_gates
        storage_id = None if storage is None else storage["storage_location_id"]
        storage_generation = None if storage is None else storage["binding_generation"]
        basis = {
            "pairing_id": candidate.pairing_id,
            "credential_generation": int(inventory["credential_generation"]),
            "inventory_instance_id": inventory["inventory_instance_id"],
            "inventory_sequence": int(inventory["inventory_sequence"]),
            "inventory_received_at": float(candidate.inventory_received_at),
            "basis_expires_at": float(candidate.basis_expires_at),
            "storage_location_id": storage_id,
            "storage_binding_generation": storage_generation,
            "catalog_digest": requirements.catalog_digest,
        }
        reason_rows = [reasons.reasons[code] for code in sorted(reasons.reasons)]
        value = {
            "pairing_display_name": candidate.pairing_display_name,
            "service_class": candidate.service_class,
            "basis": basis,
            "eligible": eligible,
            "order": None,
            "hard_gate_codes": sorted(reasons.hard_gates),
            "artifact_reuse": "verified_exact" if exact_reuse else "none",
            "runtime_state": runtime_state,
            "estimated_download_bytes": estimated_download,
            "estimated_peak_memory_bytes": peak_memory,
            "memory_headroom_bytes": memory_headroom,
            "storage_headroom_bytes": storage_headroom,
            "requires_local_approval": requires_local_approval,
            "power_policy": power_policy,
            "evidence": {
                "artifact": artifact_evidence,
                "hardware": hardware["evidence_class"],
                "memory": requirements.memory.evidence_class,
                "runtime": runtime_evidence,
                "storage": storage_evidence,
            },
            "reasons": reason_rows,
        }
        gpu_cores = hardware["gpu_cores"]
        performance_cores = hardware["performance_cores"]
        efficiency_cores = hardware["efficiency_cores"]
        compute_score = (
            (0 if gpu_cores is None else int(gpu_cores) * 1000)
            + (0 if performance_cores is None else int(performance_cores) * 10)
            + (0 if efficiency_cores is None else int(efficiency_cores))
        )
        rank = (
            0 if exact_reuse else 1,
            runtime_rank,
            -(memory_headroom or 0),
            -compute_score,
            -(storage_headroom or 0),
            _storage_speed_rank(storage),
            _evidence_rank(hardware["evidence_class"]),
            _evidence_rank(storage_evidence),
            _evidence_rank(artifact_evidence),
            power_rank,
            _SERVICE_CLASSES.index(candidate.service_class),
            candidate.pairing_id,
            storage_id or "~",
        )
        return _CandidateWork(value=value, rank=rank)


def validate_recommendation(value: object) -> PlacementRecommendationDocument:
    try:
        _SCHEMA_VALIDATOR.validate(value)
    except SchemaValidationError:
        raise PlacementProtocolError("placement_recommendation_invalid") from None
    if not isinstance(value, dict):
        raise PlacementProtocolError("placement_recommendation_invalid")
    try:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise PlacementProtocolError("placement_recommendation_invalid") from None
    if len(canonical) > MAX_RECOMMENDATION_BYTES:
        raise PlacementProtocolError("placement_recommendation_too_large")
    candidates = value["candidates"]
    allowed_classes = value["request"]["allowed_service_classes"]
    if allowed_classes != [item for item in _SERVICE_CLASSES if item in allowed_classes]:
        raise PlacementProtocolError("placement_recommendation_order_invalid")
    if value["request"]["required_capabilities"] != sorted(
        value["request"]["required_capabilities"]
    ):
        raise PlacementProtocolError("placement_recommendation_order_invalid")
    keys: set[tuple[str, str | None]] = set()
    eligible_count = 0
    saw_ineligible = False
    for candidate in candidates:
        basis = candidate["basis"]
        key = (basis["pairing_id"], basis["storage_location_id"])
        if key in keys:
            raise PlacementProtocolError("placement_recommendation_basis_duplicate")
        keys.add(key)
        if basis["catalog_digest"] != value["catalog_digest"]:
            raise PlacementProtocolError("placement_recommendation_basis_invalid")
        reason_codes = [reason["code"] for reason in candidate["reasons"]]
        if reason_codes != sorted(reason_codes) or len(reason_codes) != len(
            set(reason_codes)
        ):
            raise PlacementProtocolError("placement_recommendation_order_invalid")
        for reason in candidate["reasons"]:
            if reason["explanation"] != REASON_EXPLANATIONS[reason["code"]]:
                raise PlacementProtocolError("placement_recommendation_reason_invalid")
        hard_gates = candidate["hard_gate_codes"]
        if hard_gates != sorted(hard_gates) or not set(hard_gates).issubset(
            HARD_GATE_CODES
        ):
            raise PlacementProtocolError("placement_recommendation_reason_invalid")
        if not set(hard_gates).issubset(reason_codes):
            raise PlacementProtocolError("placement_recommendation_reason_invalid")
        if candidate["eligible"]:
            if saw_ineligible:
                raise PlacementProtocolError("placement_recommendation_order_invalid")
            eligible_count += 1
            if candidate["order"] != eligible_count:
                raise PlacementProtocolError("placement_recommendation_order_invalid")
            if basis["basis_expires_at"] < value["expires_at"]:
                raise PlacementProtocolError("placement_recommendation_basis_invalid")
        else:
            saw_ineligible = True
    ineligible = candidates[eligible_count:]
    if ineligible != sorted(ineligible, key=lambda row: _basis_sort_key(row["basis"])):
        raise PlacementProtocolError("placement_recommendation_order_invalid")
    if eligible_count == 0 and value["expires_at"] != value["created_at"]:
        raise PlacementProtocolError("placement_recommendation_basis_invalid")
    if eligible_count > 0 and value["expires_at"] <= value["created_at"]:
        raise PlacementProtocolError("placement_recommendation_basis_invalid")
    return PlacementRecommendationDocument(
        value=json.loads(canonical),
        canonical_json=canonical,
        payload_digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
    )


def parse_recommendation_json(raw: bytes | str) -> PlacementRecommendationDocument:
    if isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8")
        except UnicodeError:
            raise PlacementProtocolError("placement_recommendation_invalid") from None
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise PlacementProtocolError("placement_recommendation_invalid")
    if len(encoded) > MAX_RECOMMENDATION_BYTES:
        raise PlacementProtocolError("placement_recommendation_too_large")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate member")
            result[key] = item
        return result

    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite number")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        raise PlacementProtocolError("placement_recommendation_invalid") from None
    return validate_recommendation(value)


def _global_recipe_reasons(
    request: PlacementRequest,
    requirements: RecipeRequirements,
) -> tuple[tuple[str, EvidenceClass, float], ...]:
    reasons: list[tuple[str, EvidenceClass, float]] = []
    if requirements.recipe_status != "available":
        reasons.append(
            (
                "recipe_known_bad",
                requirements.recipe_evidence_class,
                requirements.catalog_issued_at,
            )
        )
    if requirements.compatibility_tier != "verified":
        reasons.append(
            (
                "recipe_not_verified",
                requirements.recipe_evidence_class,
                requirements.catalog_issued_at,
            )
        )
    if not set(request.required_capabilities).issubset(requirements.capabilities):
        reasons.append(
            (
                "capability_unsupported",
                requirements.recipe_evidence_class,
                requirements.catalog_issued_at,
            )
        )
    if request.required_context_tokens > requirements.guaranteed_context_tokens:
        reasons.append(
            (
                "context_unsupported",
                requirements.context_evidence_class,
                requirements.catalog_issued_at,
            )
        )
    if request.required_concurrency > min(
        requirements.memory.max_recommended_concurrency,
        requirements.launch.concurrency_slots,
    ):
        reasons.append(
            (
                "concurrency_unsupported",
                requirements.memory.evidence_class,
                requirements.catalog_issued_at,
            )
        )
    return tuple(reasons)


def _add_authority_reasons(
    request: PlacementRequest,
    requirements: RecipeRequirements,
    candidate: PlacementCandidateInput,
    reasons: _ReasonSet,
) -> None:
    inventory = candidate.inventory
    if inventory["pairing_id"] != candidate.pairing_id:
        reasons.add(
            "inventory_identity_mismatch",
            evidence_class="conservative",
            observed_at=candidate.inventory_received_at,
            hard=True,
        )
    if candidate.enrollment_state == "revoked":
        reasons.add(
            "pairing_revoked",
            evidence_class="conservative",
            observed_at=candidate.inventory_received_at,
            hard=True,
        )
    elif candidate.enrollment_state != "active":
        reasons.add(
            "pairing_inactive",
            evidence_class="conservative",
            observed_at=candidate.inventory_received_at,
            hard=True,
        )
    if inventory["credential_generation"] != candidate.active_credential_generation:
        reasons.add(
            "credential_generation_changed",
            evidence_class="conservative",
            observed_at=candidate.inventory_received_at,
            hard=True,
        )
    if candidate.freshness_state == "hub_restarted":
        reasons.add(
            "hub_restarted",
            evidence_class="conservative",
            observed_at=candidate.inventory_received_at,
            hard=True,
        )
    elif (
        candidate.freshness_state != "fresh"
        or candidate.basis_expires_at <= request.created_at
    ):
        reasons.add(
            "stale_inventory",
            evidence_class="conservative",
            observed_at=candidate.inventory_received_at,
            hard=True,
        )
    service = inventory["service"]
    if (
        service["catalog_digest"] != requirements.catalog_digest
        or service["catalog_version"] != requirements.catalog_version
    ):
        reasons.add(
            "catalog_mismatch",
            evidence_class="conservative",
            observed_at=candidate.inventory_received_at,
            hard=True,
        )
    if candidate.service_class not in request.allowed_service_classes:
        reasons.add(
            "service_class_excluded",
            evidence_class="conservative",
            observed_at=candidate.inventory_received_at,
            hard=True,
        )
    if not candidate.hub_remote_installs_enabled:
        reasons.add(
            "hub_remote_installs_disabled",
            evidence_class="conservative",
            observed_at=candidate.inventory_received_at,
            hard=True,
        )


def _add_hardware_reasons(
    requirements: RecipeRequirements,
    inventory: Mapping[str, Any],
    reasons: _ReasonSet,
) -> None:
    service = inventory["service"]
    hardware = inventory["hardware"]
    evidence = hardware["evidence_class"]
    observed_at = hardware["observed_at"]
    if service["platform"] != "macos":
        reasons.add(
            "platform_unsupported",
            evidence_class=evidence,
            observed_at=observed_at,
            hard=True,
        )
    if (
        service["architecture"] != requirements.hardware.architecture
        or hardware["architecture"] != requirements.hardware.architecture
    ):
        reasons.add(
            "architecture_unsupported",
            evidence_class=evidence,
            observed_at=observed_at,
            hard=True,
        )
    os_version = (int(hardware["os_major"]), int(hardware["os_minor"]))
    if os_version < requirements.hardware.minimum_macos or (
        requirements.hardware.maximum_macos_exclusive is not None
        and os_version >= requirements.hardware.maximum_macos_exclusive
    ):
        reasons.add(
            "os_unsupported",
            evidence_class=evidence,
            observed_at=observed_at,
            hard=True,
        )
    if _normalize_soc_family(hardware["soc_family"]) not in set(
        requirements.hardware.soc_families
    ):
        reasons.add(
            "soc_unsupported",
            evidence_class=evidence,
            observed_at=observed_at,
            hard=True,
        )
    minimum_gpu = requirements.hardware.minimum_gpu_cores
    gpu_cores = hardware["gpu_cores"]
    if minimum_gpu is not None and (gpu_cores is None or gpu_cores < minimum_gpu):
        reasons.add(
            "gpu_requirement_unmet",
            evidence_class=evidence,
            observed_at=observed_at,
            hard=True,
        )
    if (
        hardware["unified_memory_bytes"]
        < requirements.hardware.minimum_unified_memory_bytes
    ):
        reasons.add(
            "unified_memory_requirement_unmet",
            evidence_class=evidence,
            observed_at=observed_at,
            hard=True,
        )
    # Inventory v1 intentionally has no arbitrary feature bag. Only a closed
    # scorer-owned interpretation of already-bound Apple facts is accepted;
    # unknown catalog feature strings remain a hard gate rather than becoming
    # an unbound producer/scorer side channel.
    proven_features = _proven_hardware_features(hardware)
    if not set(requirements.hardware.required_features).issubset(proven_features):
        reasons.add(
            "hardware_feature_evidence_missing",
            evidence_class="conservative",
            observed_at=observed_at,
            hard=True,
        )


def _evaluate_runtime(
    requirements: RecipeRequirements,
    inventory: Mapping[str, Any],
    reasons: _ReasonSet,
) -> tuple[str, EvidenceClass, int]:
    runtime_rows = [
        row for row in inventory["runtimes"] if row["engine"] == requirements.engine
    ]
    observed_at = inventory["observed_at"]
    if len(runtime_rows) != 1 or runtime_rows[0]["catalog_status"] == "missing":
        return _runtime_install_fallback(requirements, reasons, observed_at)
    runtime = runtime_rows[0]
    observed_at = runtime["observed_at"]
    if not runtime["enabled"] or runtime["catalog_status"] == "disabled":
        reasons.add(
            "runtime_disabled",
            evidence_class="measured",
            observed_at=observed_at,
            hard=True,
        )
        return "unavailable", "measured", 4
    if runtime["catalog_digest"] != requirements.catalog_digest:
        reasons.add(
            "runtime_catalog_mismatch",
            evidence_class="conservative",
            observed_at=observed_at,
            hard=True,
        )
    if runtime["catalog_status"] == "known_bad":
        reasons.add(
            "runtime_known_bad",
            evidence_class="catalog_tested",
            observed_at=observed_at,
            hard=True,
        )
    if runtime["catalog_status"] in {"unsupported_os", "unknown"}:
        reasons.add(
            "runtime_unavailable",
            evidence_class="conservative",
            observed_at=observed_at,
            hard=True,
        )
    if runtime["catalog_status"] == "unhealthy" or runtime["health"] in {
        "degraded",
        "unhealthy",
    }:
        reasons.add(
            "runtime_unhealthy",
            evidence_class="measured",
            observed_at=observed_at,
            hard=True,
        )

    version = runtime["version"]
    fingerprint = runtime["runtime_fingerprint"]
    if not set(requirements.runtime.required_features).issubset(
        _proven_runtime_features(requirements, inventory, runtime)
    ):
        reasons.add(
            "runtime_feature_evidence_missing",
            evidence_class="conservative",
            observed_at=observed_at,
            hard=True,
        )
    if version in requirements.runtime.known_bad_versions or (
        fingerprint in requirements.runtime.known_bad_runtime_fingerprints
    ):
        reasons.add(
            "runtime_known_bad",
            evidence_class="catalog_tested",
            observed_at=observed_at,
            hard=True,
        )
    fingerprint_verified = False
    if requirements.runtime.allowed_runtime_fingerprints:
        if fingerprint not in requirements.runtime.allowed_runtime_fingerprints:
            reasons.add(
                "runtime_fingerprint_mismatch",
                evidence_class="catalog_tested",
                observed_at=observed_at,
                hard=True,
            )
        else:
            fingerprint_verified = True
    if runtime["release_tier"] != requirements.runtime.release_tier:
        reasons.add(
            "runtime_version_mismatch",
            evidence_class="calculated",
            observed_at=observed_at,
            hard=True,
        )
    if not fingerprint_verified and not _version_in_range(
        version,
        requirements.runtime.minimum_version,
        requirements.runtime.maximum_version_exclusive,
    ):
        reasons.add(
            "runtime_version_mismatch",
            evidence_class="conservative",
            observed_at=observed_at,
            hard=True,
        )
    if any(code.startswith("runtime_") and code in HARD_GATE_CODES for code in reasons.hard_gates):
        return "unavailable", "conservative", 4
    health_unverified = runtime["health"] == "unknown"
    if fingerprint_verified and not health_unverified:
        reasons.add(
            "runtime_compatible_verified",
            evidence_class="catalog_tested",
            observed_at=observed_at,
        )
        return "compatible_verified", "catalog_tested", 0
    reasons.add(
        "runtime_compatible_unverified",
        evidence_class="conservative" if health_unverified else "calculated",
        observed_at=observed_at,
    )
    return (
        "compatible_unverified",
        "conservative" if health_unverified else "calculated",
        1,
    )


def _proven_hardware_features(hardware: Mapping[str, Any]) -> frozenset[str]:
    """Derive only closed Apple facts already bound by Inventory v1.

    Native probe contract v2 means ``system_profiler`` returned exactly one
    built-in Apple GPU with its fixed Metal-supported value. Physical memory
    on an exact Apple-M-series arm64 SoC is unified. No SoC-name lookup table,
    arbitrary producer feature string, or Hub-side guess is accepted.
    """

    normalized_soc = _normalize_soc_family(hardware["soc_family"])
    exact_apple_silicon = bool(
        hardware["architecture"] == "arm64"
        and re.fullmatch(r"apple-m[1-9][0-9]?(?:-(?:pro|max|ultra))?", normalized_soc)
    )
    features: set[str] = set()
    if exact_apple_silicon and int(hardware["unified_memory_bytes"]) > 0:
        features.add("unified-memory")
    if exact_apple_silicon and int(hardware["probe_version"]) == 2:
        features.add("metal")
    return frozenset(features)


def _proven_runtime_features(
    requirements: RecipeRequirements,
    inventory: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> frozenset[str]:
    """Map a closed feature vocabulary from exact runtime/launch facts."""

    fingerprint = runtime["runtime_fingerprint"]
    fingerprint_match = bool(
        requirements.runtime.allowed_runtime_fingerprints
        and fingerprint in requirements.runtime.allowed_runtime_fingerprints
    )
    version_match = _version_in_range(
        runtime["version"],
        requirements.runtime.minimum_version,
        requirements.runtime.maximum_version_exclusive,
    )
    bounded_runtime = bool(
        runtime["engine"] == requirements.engine
        and runtime["release_tier"] == requirements.runtime.release_tier
        and runtime["catalog_status"] == "available"
        and runtime["enabled"]
        and (fingerprint_match or version_match)
    )
    if not bounded_runtime:
        return frozenset()
    features: set[str] = set()
    if "metal" in _proven_hardware_features(inventory["hardware"]):
        features.add("apple-metal")
    if (
        requirements.engine == "llama.cpp"
        and isinstance(requirements.launch, LlamaCppLaunchRequirements)
        and requirements.launch.flash_attention in {"automatic", "enabled"}
    ):
        features.add("flash-attention")
    return frozenset(features)


def _runtime_install_fallback(
    requirements: RecipeRequirements,
    reasons: _ReasonSet,
    observed_at: float,
) -> tuple[str, EvidenceClass, int]:
    if requirements.runtime.install_mode == "managed":
        reasons.add(
            "runtime_install_required",
            evidence_class="catalog_tested",
            observed_at=observed_at,
        )
        reasons.add(
            "runtime_unavailable",
            evidence_class="conservative",
            observed_at=observed_at,
            hard=True,
        )
        return "install_managed", "conservative", 2
    if requirements.runtime.install_mode == "local_approval":
        reasons.add(
            "runtime_install_approval_required",
            evidence_class="conservative",
            observed_at=observed_at,
        )
        reasons.add(
            "runtime_unavailable",
            evidence_class="conservative",
            observed_at=observed_at,
            hard=True,
        )
        return "install_requires_approval", "conservative", 3
    reasons.add(
        "runtime_unavailable",
        evidence_class="conservative",
        observed_at=observed_at,
        hard=True,
    )
    return "unavailable", "conservative", 4


def _estimate_peak_memory(
    request: PlacementRequest,
    requirements: RecipeRequirements,
) -> int | None:
    memory = requirements.memory
    value = (
        memory.weights_bytes
        + memory.runtime_overhead_bytes
        + memory.activation_overhead_bytes
        + memory.kv_bytes_per_token_per_slot
        * request.required_context_tokens
        * request.required_concurrency
        + memory.safety_headroom_bytes
    )
    return value if 0 <= value <= MAX_BYTE_COUNT else None


def _exact_artifact_reuse(
    requirements: RecipeRequirements,
    inventory: Mapping[str, Any],
    storage: Mapping[str, Any] | None,
) -> tuple[bool, EvidenceClass, float]:
    if storage is None:
        return False, requirements.recipe_evidence_class, requirements.catalog_issued_at
    matches = []
    for installation in inventory["installations"]:
        if (
            installation["logical_model_id"] == requirements.logical_model_id
            and installation["recipe_id"] == requirements.recipe_id
            and installation["artifact_id"] == requirements.artifact_id
            and installation["engine"] == requirements.engine
            and installation["identity_confidence"] == "authoritative"
            and installation["lifecycle"] == "registered"
            and installation["availability"] == "available"
            and installation["storage_location_id"]
            == storage["storage_location_id"]
            and installation["storage_binding_generation"]
            == storage["binding_generation"]
            and installation["verification"]["state"]
            in {"digest_verified", "self_tested"}
        ):
            matches.append(installation)
    if not matches:
        return False, requirements.recipe_evidence_class, requirements.catalog_issued_at
    best = max(
        matches,
        key=lambda row: (
            _verification_rank(row["verification"]["state"]),
            row["verification"]["verified_at"] or 0,
            row["installation_id"],
        ),
    )
    return (
        True,
        best["verification"]["evidence_class"],
        float(best["verification"]["verified_at"] or best["observed_at"]),
    )


def _evaluate_storage(
    candidate: PlacementCandidateInput,
    inventory: Mapping[str, Any],
    storage: Mapping[str, Any] | None,
    estimated_download: int,
    exact_reuse: bool,
    reasons: _ReasonSet,
) -> tuple[int | None, EvidenceClass, bool]:
    node_policy = inventory["participation"]["remote_install_policy"]
    if storage is None:
        reasons.add(
            "storage_not_registered",
            evidence_class="conservative",
            observed_at=inventory["observed_at"],
            hard=True,
        )
        if node_policy == "local-only":
            reasons.add(
                "remote_installs_local_only",
                evidence_class="measured",
                observed_at=inventory["observed_at"],
                hard=True,
            )
        return None, "conservative", node_policy == "ask"
    evidence: EvidenceClass = storage["evidence_class"]
    observed_at = storage["observed_at"]
    if storage["availability"] != "available":
        reasons.add(
            "storage_unavailable",
            evidence_class=evidence,
            observed_at=observed_at,
            hard=True,
        )
    if not storage["writable"]:
        reasons.add(
            "storage_not_writable",
            evidence_class=evidence,
            observed_at=observed_at,
            hard=True,
        )
    fences = dict(candidate.storage_binding_fences)
    expected_generation = fences.get(storage["storage_location_id"])
    if expected_generation is not None and expected_generation != storage["binding_generation"]:
        reasons.add(
            "storage_binding_changed",
            evidence_class="measured",
            observed_at=observed_at,
            hard=True,
        )
    storage_policy = storage["remote_install_policy"]
    if node_policy == "local-only" or storage_policy == "local-only":
        reasons.add(
            "remote_installs_local_only",
            evidence_class="measured",
            observed_at=observed_at,
            hard=True,
        )
    requires_approval = node_policy == "ask" or storage_policy == "ask"
    if requires_approval:
        reasons.add(
            "local_approval_required",
            evidence_class="measured",
            observed_at=observed_at,
        )
    free_bytes = storage["free_bytes"]
    total_bytes = storage["total_bytes"]
    if free_bytes is None or total_bytes is None:
        reasons.add(
            "storage_capacity_unknown",
            evidence_class=evidence,
            observed_at=observed_at,
            hard=True,
        )
        return None, evidence, requires_approval
    safety_headroom = (
        0
        if exact_reuse
        else max(
            STORAGE_FIXED_HEADROOM_BYTES,
            (estimated_download * STORAGE_PROPORTIONAL_HEADROOM_PERCENT + 99) // 100,
        )
    )
    required = estimated_download + safety_headroom
    storage_headroom = int(free_bytes) - required
    if storage_headroom < 0:
        reasons.add(
            "insufficient_storage",
            evidence_class=evidence,
            observed_at=observed_at,
            hard=True,
        )
    else:
        reasons.add(
            "storage_headroom_available",
            evidence_class=evidence,
            observed_at=observed_at,
        )
    return storage_headroom, evidence, requires_approval


def _power_policy(
    hardware: Mapping[str, Any],
    reasons: _ReasonSet,
) -> tuple[str, int]:
    observed_at = hardware["observed_at"]
    evidence = hardware["evidence_class"]
    power = hardware["power_source"]
    low_power = bool(hardware["low_power_mode"])
    pressure = hardware["pressure_class"]
    if power == "ac" and not low_power:
        reasons.add(
            "power_ac",
            evidence_class=evidence,
            observed_at=observed_at,
        )
    elif power == "battery":
        reasons.add(
            "power_battery",
            evidence_class=evidence,
            observed_at=observed_at,
        )
    if low_power:
        reasons.add(
            "low_power_mode",
            evidence_class=evidence,
            observed_at=observed_at,
        )
    if pressure in {"serious", "critical"}:
        reasons.add(
            "pressure_deprioritized",
            evidence_class=evidence,
            observed_at=observed_at,
        )
    if power == "unknown" or pressure == "unknown":
        return "unknown", 2
    if power == "ac" and not low_power and pressure in {"nominal", "fair"}:
        return "preferred", 0
    return "deprioritized", 1


def _storage_speed_rank(storage: Mapping[str, Any] | None) -> int:
    if storage is None:
        return 4
    return {"fast": 0, "moderate": 1, "slow": 2, "unknown": 3}[
        storage["write_speed_class"]
    ]


def _verification_rank(state: str) -> int:
    return {
        "unverified": 0,
        "revision_verified": 1,
        "digest_verified": 2,
        "self_tested": 3,
    }[state]


def _evidence_rank(value: str) -> int:
    return {
        "measured": 0,
        "catalog_tested": 1,
        "calculated": 2,
        "conservative": 3,
    }[value]


def _catalog_evidence_class(value: str) -> EvidenceClass:
    if value == "upstream_documented":
        return "conservative"
    if value not in _EVIDENCE_CLASSES:
        raise PlacementInputError("placement_recipe_invalid")
    return value  # type: ignore[return-value]


def _conservative_catalog_evidence(*values: str) -> EvidenceClass:
    normalized = tuple(_catalog_evidence_class(value) for value in values)
    return max(normalized, key=_evidence_rank)


def _normalize_soc_family(value: str) -> str:
    normalized = _SOC_COMPONENT.sub("-", value.strip().lower()).strip("-")
    if not normalized.startswith("apple-"):
        normalized = "apple-" + normalized
    return normalized


def _version_in_range(
    version: str | None,
    minimum: str,
    maximum_exclusive: str | None,
) -> bool:
    if version is None:
        return False
    if version == minimum and maximum_exclusive != minimum:
        return True
    current_key = _version_key(version)
    minimum_key = _version_key(minimum)
    if current_key is None or minimum_key is None or current_key[0] != minimum_key[0]:
        return False
    if current_key[1] < minimum_key[1]:
        return False
    if maximum_exclusive is None:
        return True
    maximum_key = _version_key(maximum_exclusive)
    if maximum_key is None or maximum_key[0] != current_key[0]:
        return False
    return current_key[1] < maximum_key[1]


def _version_key(value: str) -> tuple[str, tuple[int, ...]] | None:
    match = _LLAMA_BUILD_VERSION.fullmatch(value)
    if match:
        return ("llama-build", (int(match.group(1)),))
    match = _NUMERIC_VERSION.fullmatch(value)
    if match:
        parts_list = [int(part) for part in match.group(1).split(".")]
        while len(parts_list) > 1 and parts_list[-1] == 0:
            parts_list.pop()
        parts = tuple(parts_list)
        return ("numeric", parts)
    return None


def _basis_sort_key(basis: Mapping[str, Any]) -> tuple[str, str]:
    return (basis["pairing_id"], basis["storage_location_id"] or "~")


def _require_integer(value: Any, *, minimum: int, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise PlacementInputError("placement_input_invalid")


def _require_timestamp(value: Any) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or value > 4_102_444_800
    ):
        raise PlacementInputError("placement_input_invalid")


def _require_uuid(value: Any) -> None:
    try:
        if str(uuid.UUID(str(value))) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PlacementInputError("placement_input_invalid") from None


def _require_sha256(value: Any) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PlacementInputError("placement_input_invalid")


def _require_safe_identifier(value: Any) -> None:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise PlacementInputError("placement_input_invalid")


def _require_safe_version(value: Any) -> None:
    if not isinstance(value, str) or not _SAFE_VERSION.fullmatch(value):
        raise PlacementInputError("placement_input_invalid")


def _require_sorted_unique(values: Sequence[Any]) -> None:
    if list(values) != sorted(set(values)):
        raise PlacementInputError("placement_input_invalid")


def _require_os_version(value: Any) -> None:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or any(
            isinstance(part, bool)
            or not isinstance(part, int)
            or part < (1 if index == 0 else 0)
            or part > 99
            for index, part in enumerate(value)
        )
    ):
        raise PlacementInputError("placement_recipe_invalid")


__all__ = [
    "DS4LaunchRequirements",
    "HARD_GATE_CODES",
    "HardwareRequirements",
    "LlamaCppLaunchRequirements",
    "MAX_PLACEMENT_REQUEST_BYTES",
    "MAX_RECOMMENDATION_BYTES",
    "MemoryRequirements",
    "OmlxLaunchRequirements",
    "PLACEMENT_SCHEMA_VERSION",
    "PlacementCandidateInput",
    "PlacementError",
    "PlacementInputError",
    "PlacementProtocolError",
    "PlacementRecommendationDocument",
    "PlacementRequest",
    "PlacementScorer",
    "REASON_EXPLANATIONS",
    "RecipeRequirements",
    "RuntimeRequirements",
    "SCORER_VERSION",
    "parse_placement_request",
    "parse_recommendation_json",
    "validate_placement_request",
    "validate_recommendation",
]
