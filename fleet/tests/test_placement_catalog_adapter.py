from __future__ import annotations

import base64
import copy
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mnemosyne_fleet.compatibility_catalog import (
    CatalogVerifier,
    TrustedCatalogKey,
    VerifiedCatalog,
    artifact_manifest_digest,
    built_in_empty_catalog,
    canonical_json,
    catalog_digest,
    signing_message,
)
from mnemosyne_fleet.placement import (
    DS4LaunchRequirements,
    LlamaCppLaunchRequirements,
    OmlxLaunchRequirements,
    PlacementInputError,
    PlacementRequest,
    RecipeRequirements,
    parse_placement_request,
    validate_placement_request,
)


ROOT = Path(__file__).resolve().parents[2]
CATALOG_V1 = ROOT / "compatibility_catalog" / "v1"
TEST_NOW = 1_790_000_000
TEST_SEED = bytes(range(1, 33))


def _golden_envelope() -> dict[str, Any]:
    return json.loads(
        (CATALOG_V1 / "catalog.golden.json").read_text(encoding="utf-8")
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _verifier() -> CatalogVerifier:
    public = json.loads(
        (CATALOG_V1 / "test_keys.json").read_text(encoding="utf-8")
    )["keys"][0]
    key = TrustedCatalogKey.from_base64url(
        key_id=public["key_id"],
        public_key=public["public_key"],
    )
    return CatalogVerifier({key.key_id: key})


def _resign(envelope: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(envelope)
    candidate["catalog_digest"] = catalog_digest(candidate["catalog"])
    signature = Ed25519PrivateKey.from_private_bytes(TEST_SEED).sign(
        signing_message(candidate["catalog"])
    )
    candidate["signatures"] = [
        {
            "key_id": "test-catalog-2026-a",
            "algorithm": "Ed25519",
            "signature": _encode(signature),
        }
    ]
    return candidate


def _verified_golden() -> VerifiedCatalog:
    return _verifier().verify(_golden_envelope(), now=TEST_NOW)


def _signed_catalog_for_engine(engine: str) -> tuple[VerifiedCatalog, str, str]:
    envelope = _golden_envelope()
    catalog = envelope["catalog"]
    suffix = engine.replace(".", "-")
    model_id = f"example-placement-{suffix}"
    artifact_id = f"example-placement-{suffix}-artifact"
    recipe_id = f"example-placement-{suffix}-recipe"
    model = catalog["logical_models"][0]
    artifact = catalog["artifacts"][0]
    recipe = catalog["recipes"][0]
    model.update(
        logical_model_id=model_id,
        display_name=f"Example placement {suffix}",
        capabilities=["chat/completions", "messages", "responses"],
        declared_max_context_tokens=32_768,
    )
    artifact.update(
        artifact_id=artifact_id,
        logical_model_id=model_id,
        format={
            "llama.cpp": "gguf",
            "omlx": "mlx",
            "ds4": "ds4-weights",
        }[engine],
        quantization="Q4_K_M" if engine == "llama.cpp" else None,
        files=[
            {
                "path": "weights.bin",
                "size_bytes": 1000,
                "sha256": "sha256:" + "1" * 64,
            }
        ],
        total_size_bytes=1000,
    )
    artifact["manifest_digest"] = artifact_manifest_digest(artifact["files"])
    recipe.update(
        recipe_id=recipe_id,
        logical_model_id=model_id,
        artifact_id=artifact_id,
        engine=engine,
        capabilities=["chat/completions", "messages", "responses"],
        compatibility_tier="verified",
    )
    recipe["context"]["guaranteed_tokens"] = 8192
    recipe["hardware"].update(
        architecture="arm64",
        soc_families=["apple-m3-max"],
        required_features=[],
    )
    recipe["memory"].update(
        weights_bytes=1200,
        runtime_overhead_bytes=200,
        activation_overhead_bytes=300,
        kv_bytes_per_token_per_slot=64,
        safety_headroom_bytes=1024,
        max_recommended_concurrency=4,
    )
    recipe["runtime"].update(
        engine=engine,
        release_tier="stable",
        minimum_version="b6000" if engine == "llama.cpp" else "1.0.0",
        maximum_version_exclusive=(
            "b9000" if engine == "llama.cpp" else "2.0.0"
        ),
        known_bad_versions=[],
        allowed_runtime_fingerprints=[],
        known_bad_runtime_fingerprints=[],
        required_features=[],
    )
    recipe["launch"] = {
        "llama.cpp": {
            "engine": "llama.cpp",
            "parallel_slots": 2,
            "gpu_offload": "all",
            "flash_attention": "automatic",
        },
        "omlx": {
            "engine": "omlx",
            "scheduler_slots": 3,
            "memory_guard": "required",
        },
        "ds4": {
            "engine": "ds4",
            "batched_sessions": 4,
            "execution_mode": "single-node",
        },
    }[engine]
    signed = _resign(envelope)
    return _verifier().verify(signed, now=TEST_NOW), model_id, recipe_id


def _request_value(
    *,
    logical_model_id: str = "example-flash-vnext",
    recipe_id: str = "example-flash-vnext-llamacpp-q4",
    capabilities: list[str] | None = None,
    context: int = 8192,
    concurrency: int = 2,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "recommendation_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "created_at": float(TEST_NOW),
        "valid_for_seconds": 60,
        "logical_model_id": logical_model_id,
        "recipe_id": recipe_id,
        "required_capabilities": capabilities
        if capabilities is not None
        else ["chat/completions", "responses"],
        "required_context_tokens": context,
        "required_concurrency": concurrency,
        "allowed_service_classes": [
            "primary",
            "opportunistic",
            "overflow",
        ],
    }


def _request(**overrides: Any) -> PlacementRequest:
    return validate_placement_request(_request_value(**overrides))


def _tampered_verified(
    verified: VerifiedCatalog,
    mutation: Callable[[dict[str, Any]], None],
) -> VerifiedCatalog:
    catalog = verified.catalog()
    mutation(catalog)
    encoded = canonical_json(catalog)
    return replace(
        verified,
        canonical_catalog=encoded,
        catalog_digest=catalog_digest(catalog),
    )


def _error_code(operation: Callable[[], Any]) -> str:
    with pytest.raises(PlacementInputError) as captured:
        operation()
    assert str(captured.value) == captured.value.code
    return captured.value.code


def test_closed_request_parser_accepts_only_bounded_canonical_user_intent() -> None:
    value = _request_value()
    request = parse_placement_request(json.dumps(value))
    assert request.wire_value() == value
    assert request.logical_model_id == "example-flash-vnext"
    assert request.recipe_id == "example-flash-vnext-llamacpp-q4"


@pytest.mark.parametrize(
    "forbidden",
    [
        "artifact_id",
        "engine",
        "artifact_total_size_bytes",
        "weights_bytes",
        "memory",
        "hardware",
        "runtime",
        "runtime_install_mode",
        "launch",
        "path",
        "command",
        "model_card",
    ],
)
def test_request_parser_never_accepts_catalog_or_local_authority_fields(
    forbidden: str,
) -> None:
    value = _request_value()
    value[forbidden] = "/private/secret-model.gguf --unsafe"
    assert _error_code(lambda: validate_placement_request(value)) == (
        "placement_request_invalid"
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":NaN}',
        "not-json",
    ],
)
def test_request_parser_rejects_duplicate_nonfinite_and_malformed_json(raw: str) -> None:
    assert _error_code(lambda: parse_placement_request(raw)) == (
        "placement_request_invalid"
    )


def test_request_parser_has_fixed_errors_and_never_reflects_arbitrary_text() -> None:
    secret = "/Volumes/Private/models --delete-everything"
    value = _request_value()
    value["path"] = secret
    with pytest.raises(PlacementInputError) as captured:
        parse_placement_request(json.dumps(value))
    assert captured.value.code == "placement_request_invalid"
    assert secret not in str(captured.value)

    oversized = json.dumps(value).encode() + b" " * 20_000
    assert _error_code(lambda: parse_placement_request(oversized)) == (
        "placement_request_too_large"
    )


def test_request_parser_rejects_unhashable_arrays_with_a_fixed_error() -> None:
    value = _request_value()
    value["required_capabilities"] = [{"path": "/private/model"}]
    assert _error_code(lambda: validate_placement_request(value)) == (
        "placement_request_invalid"
    )


def test_golden_llamacpp_recipe_maps_only_signed_catalog_facts() -> None:
    catalog = _verified_golden()
    request = _request()
    resolved = RecipeRequirements.from_verified_catalog(
        catalog,
        request=request,
        runtime_install_mode="managed",
    )

    recipe = catalog.catalog()["recipes"][0]
    assert resolved.logical_model_id == request.logical_model_id
    assert resolved.recipe_id == request.recipe_id
    assert resolved.artifact_id == recipe["artifact_id"]
    assert resolved.artifact_total_size_bytes == 1000
    assert resolved.memory.weights_bytes == 1200
    assert resolved.memory.runtime_overhead_bytes == 200
    assert resolved.hardware.soc_families == ("apple-m1", "apple-m2-pro")
    assert resolved.runtime.minimum_version == "b6000"
    assert resolved.runtime.known_bad_versions == ("b6123",)
    assert resolved.runtime.install_mode == "managed"
    assert resolved.context_evidence_class == "catalog_tested"
    assert resolved.recipe_evidence_class == "catalog_tested"
    assert resolved.launch == LlamaCppLaunchRequirements(
        engine="llama.cpp",
        parallel_slots=2,
        gpu_offload="all",
        flash_attention="automatic",
    )


@pytest.mark.parametrize(
    ("engine", "expected_launch"),
    [
        (
            "llama.cpp",
            LlamaCppLaunchRequirements(
                engine="llama.cpp",
                parallel_slots=2,
                gpu_offload="all",
                flash_attention="automatic",
            ),
        ),
        (
            "omlx",
            OmlxLaunchRequirements(
                engine="omlx",
                scheduler_slots=3,
                memory_guard="required",
            ),
        ),
        (
            "ds4",
            DS4LaunchRequirements(
                engine="ds4",
                batched_sessions=4,
                execution_mode="single-node",
            ),
        ),
    ],
)
def test_exact_llamacpp_omlx_and_ds4_launch_mapping(
    engine: str,
    expected_launch: object,
) -> None:
    catalog, model_id, recipe_id = _signed_catalog_for_engine(engine)
    request = _request(
        logical_model_id=model_id,
        recipe_id=recipe_id,
        concurrency=1,
    )
    resolved = RecipeRequirements.from_verified_catalog(catalog, request=request)
    assert resolved.engine == engine
    assert resolved.launch == expected_launch
    assert resolved.artifact_total_size_bytes == 1000
    assert resolved.memory.max_recommended_concurrency == 4
    assert resolved.hardware.required_features == ()
    assert resolved.runtime.required_features == ()


def test_catalog_upstream_documented_evidence_is_never_upgraded_to_measured() -> None:
    catalog, model_id, recipe_id = _signed_catalog_for_engine("omlx")

    def mutation(value: dict[str, Any]) -> None:
        value["recipes"][0]["evidence"]["evidence_class"] = (
            "upstream_documented"
        )
        value["recipes"][0]["context"]["evidence"]["evidence_class"] = (
            "upstream_documented"
        )
        value["recipes"][0]["memory"]["estimate_class"] = "catalog_tested"
        value["recipes"][0]["memory"]["evidence"]["evidence_class"] = (
            "upstream_documented"
        )

    catalog = _tampered_verified(catalog, mutation)
    resolved = RecipeRequirements.from_verified_catalog(
        catalog,
        request=_request(logical_model_id=model_id, recipe_id=recipe_id),
    )
    assert resolved.recipe_evidence_class == "conservative"
    assert resolved.context_evidence_class == "conservative"
    assert resolved.memory.evidence_class == "conservative"


@pytest.mark.parametrize(
    ("catalog_factory", "request_factory", "kwargs", "expected"),
    [
        (
            built_in_empty_catalog,
            _request,
            {},
            "placement_catalog_offline",
        ),
        (
            _verified_golden,
            lambda: _request(recipe_id="missing-recipe"),
            {},
            "placement_recipe_missing",
        ),
        (
            _verified_golden,
            _request,
            {"recipe_state": "known_bad"},
            "placement_recipe_known_bad",
        ),
        (
            _verified_golden,
            _request,
            {"recipe_state": "revoked"},
            "placement_recipe_revoked",
        ),
    ],
)
def test_offline_missing_known_bad_and_revoked_are_distinct_fixed_errors(
    catalog_factory,
    request_factory,
    kwargs: dict[str, Any],
    expected: str,
) -> None:
    catalog = catalog_factory()
    request = request_factory()
    assert _error_code(
        lambda: RecipeRequirements.from_verified_catalog(
            catalog,
            request=request,
            **kwargs,
        )
    ) == expected


def test_verified_catalog_is_rechecked_at_request_time_for_expiry_and_issue_time() -> None:
    catalog = _verified_golden()
    expired_request = _request_value()
    expired_request["created_at"] = float(catalog.expires_at)
    assert _error_code(
        lambda: RecipeRequirements.from_verified_catalog(
            catalog,
            request=validate_placement_request(expired_request),
        )
    ) == "placement_catalog_expired"

    no_expiry = replace(catalog, expires_at=None)
    assert _error_code(
        lambda: RecipeRequirements.from_verified_catalog(
            no_expiry,
            request=_request(),
        )
    ) == "placement_catalog_expired"

    future = replace(catalog, issued_at=TEST_NOW + 1)
    assert _error_code(
        lambda: RecipeRequirements.from_verified_catalog(
            future,
            request=_request(),
        )
    ) == "placement_catalog_not_yet_valid"


@pytest.mark.parametrize("recipe_state", ["known_bad", "revoked"])
def test_recipe_denial_overlay_cannot_make_a_missing_recipe_exist(
    recipe_state: str,
) -> None:
    assert _error_code(
        lambda: RecipeRequirements.from_verified_catalog(
            _verified_golden(),
            request=_request(recipe_id="missing-recipe"),
            recipe_state=recipe_state,  # type: ignore[arg-type]
        )
    ) == "placement_recipe_missing"


def test_ambiguous_and_mismatched_catalog_references_fail_closed() -> None:
    verified = _verified_golden()

    duplicate = _tampered_verified(
        verified,
        lambda value: value["recipes"].append(copy.deepcopy(value["recipes"][0])),
    )
    assert _error_code(
        lambda: RecipeRequirements.from_verified_catalog(
            duplicate,
            request=_request(),
        )
    ) == "placement_catalog_reference_ambiguous"

    mismatched = _tampered_verified(
        verified,
        lambda value: value["recipes"][0].update(logical_model_id="other-model"),
    )
    assert _error_code(
        lambda: RecipeRequirements.from_verified_catalog(
            mismatched,
            request=_request(),
        )
    ) == "placement_catalog_reference_mismatch"

    missing_artifact = _tampered_verified(
        verified,
        lambda value: value["recipes"][0].update(artifact_id="missing-artifact"),
    )
    assert _error_code(
        lambda: RecipeRequirements.from_verified_catalog(
            missing_artifact,
            request=_request(),
        )
    ) == "placement_catalog_reference_mismatch"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["artifacts"][0].update(format="mlx"),
        lambda value: value["recipes"][0].update(engine="future-engine"),
    ],
)
def test_unsupported_engine_or_artifact_format_has_one_fixed_error(mutation) -> None:
    catalog = _tampered_verified(_verified_golden(), mutation)
    assert _error_code(
        lambda: RecipeRequirements.from_verified_catalog(
            catalog,
            request=_request(),
        )
    ) == "placement_engine_format_unsupported"


@pytest.mark.parametrize(
    ("request_factory", "expected"),
    [
        (
            lambda: _request(capabilities=["embeddings"]),
            "placement_capability_unsupported",
        ),
        (
            lambda: _request(context=8193),
            "placement_context_unsupported",
        ),
        (
            lambda: _request(concurrency=3),
            "placement_concurrency_unsupported",
        ),
    ],
)
def test_requested_capability_context_and_concurrency_are_enforced_before_scoring(
    request_factory,
    expected: str,
) -> None:
    assert _error_code(
        lambda: RecipeRequirements.from_verified_catalog(
            _verified_golden(),
            request=request_factory(),
        )
    ) == expected


def test_adapter_output_and_errors_contain_no_paths_commands_or_catalog_text() -> None:
    resolved = RecipeRequirements.from_verified_catalog(
        _verified_golden(),
        request=_request(),
    )
    value = asdict(resolved)
    encoded = json.dumps(value, sort_keys=True)
    forbidden = (
        "repository_id",
        "source",
        "files",
        "path",
        "command",
        "display_name",
        "model_card",
        "/private/",
        "/Volumes/",
        "weights.bin",
        "--",
    )
    assert all(item not in encoded for item in forbidden)

    arbitrary = "private/path/model.gguf --dangerous"
    value = _request_value(recipe_id=arbitrary)
    with pytest.raises(PlacementInputError) as captured:
        validate_placement_request(value)
    assert captured.value.code == "placement_request_invalid"
    assert arbitrary not in str(captured.value)
