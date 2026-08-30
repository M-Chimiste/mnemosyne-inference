from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from mnemosyne_fleet.compatibility_catalog import CatalogVerifier, TrustedCatalogKey
from mnemosyne_fleet.desired_install_api import (
    DesiredInstallAPIError,
    select_exact_candidate,
)
from mnemosyne_fleet.placement import (
    _SCHEMA as FLEET_PLACEMENT_SCHEMA,
    HardwareRequirements,
    OmlxLaunchRequirements,
    MemoryRequirements,
    PlacementCandidateInput,
    PlacementInputError,
    PlacementProtocolError,
    PlacementRequest,
    PlacementScorer,
    RecipeRequirements,
    RuntimeRequirements,
    parse_recommendation_json,
    validate_recommendation,
)


ROOT = Path(__file__).resolve().parents[2]
INVENTORY_EXAMPLE = ROOT / "mac_pool_protocol" / "v1" / "mac_inventory.example.json"
CATALOG_DIGEST = "sha256:" + "1" * 64


def inventory() -> dict[str, Any]:
    return json.loads(INVENTORY_EXAMPLE.read_text(encoding="utf-8"))


def requirements(
    *,
    runtime_install_mode: str = "not_allowed",
    hardware_features: tuple[str, ...] = (),
    runtime_features: tuple[str, ...] = (),
) -> RecipeRequirements:
    return RecipeRequirements(
        catalog_version="2026.08.1",
        catalog_digest=CATALOG_DIGEST,
        catalog_issued_at=1_785_520_000.0,
        logical_model_id="model:deepseek-flash-v4",
        recipe_id="recipe:omlx-deepseek-flash-v4-mlx-4bit",
        artifact_id="artifact:deepseek-flash-v4-mlx-4bit",
        engine="omlx",
        recipe_status="available",
        compatibility_tier="verified",
        capabilities=("chat/completions", "completions", "messages", "responses"),
        guaranteed_context_tokens=131_072,
        artifact_total_size_bytes=72_000_000_000,
        memory=MemoryRequirements(
            weights_bytes=72_000_000_000,
            runtime_overhead_bytes=4_000_000_000,
            activation_overhead_bytes=2_000_000_000,
            kv_bytes_per_token_per_slot=1024,
            safety_headroom_bytes=4_000_000_000,
            max_recommended_concurrency=4,
            evidence_class="catalog_tested",
        ),
        hardware=HardwareRequirements(
            architecture="arm64",
            soc_families=("apple-m3-max",),
            minimum_gpu_cores=16,
            minimum_unified_memory_bytes=64_000_000_000,
            minimum_macos=(14, 0),
            maximum_macos_exclusive=(17, 0),
            required_features=hardware_features,
        ),
        runtime=RuntimeRequirements(
            release_tier="stable",
            minimum_version="1.0.0",
            maximum_version_exclusive="2.0.0",
            known_bad_versions=(),
            allowed_runtime_fingerprints=("sha256:" + "4" * 64,),
            known_bad_runtime_fingerprints=(),
            required_features=runtime_features,
            install_mode=runtime_install_mode,  # type: ignore[arg-type]
        ),
        launch=OmlxLaunchRequirements(
            engine="omlx",
            scheduler_slots=4,
            memory_guard="required",
        ),
        recipe_evidence_class="catalog_tested",
        context_evidence_class="catalog_tested",
    )


def request(
    *,
    capabilities: tuple[str, ...] = ("chat/completions", "responses"),
    context_tokens: int = 32_768,
    concurrency: int = 1,
    allowed_classes: tuple[str, ...] = (
        "primary",
        "opportunistic",
        "overflow",
    ),
) -> PlacementRequest:
    return PlacementRequest(
        recommendation_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        created_at=1_785_528_001.0,
        valid_for_seconds=60,
        logical_model_id="model:deepseek-flash-v4",
        recipe_id="recipe:omlx-deepseek-flash-v4-mlx-4bit",
        required_capabilities=capabilities,  # type: ignore[arg-type]
        required_context_tokens=context_tokens,
        required_concurrency=concurrency,
        allowed_service_classes=allowed_classes,  # type: ignore[arg-type]
    )


def candidate(
    value: dict[str, Any] | None = None,
    *,
    pairing_id: str | None = None,
    display_name: str = "Studio Mac",
    service_class: str = "primary",
    enrollment_state: str = "active",
    active_generation: int | None = 3,
    freshness_state: str = "fresh",
    hub_remote_installs_enabled: bool = True,
    fences: tuple[tuple[str, int], ...] = (),
) -> PlacementCandidateInput:
    value = inventory() if value is None else value
    if pairing_id is not None:
        value["pairing_id"] = pairing_id
    return PlacementCandidateInput(
        pairing_id=value["pairing_id"],
        pairing_display_name=display_name,
        service_class=service_class,  # type: ignore[arg-type]
        enrollment_state=enrollment_state,  # type: ignore[arg-type]
        active_credential_generation=active_generation,
        freshness_state=freshness_state,  # type: ignore[arg-type]
        inventory_received_at=1_785_528_000.0,
        basis_expires_at=1_785_528_060.0,
        inventory=value,
        hub_remote_installs_enabled=hub_remote_installs_enabled,
        storage_binding_fences=fences,
    )


def score(
    *candidates: PlacementCandidateInput,
    placement_request: PlacementRequest | None = None,
    recipe: RecipeRequirements | None = None,
) -> dict[str, Any]:
    return PlacementScorer().score(
        placement_request or request(),
        recipe or requirements(),
        candidates,
    ).value


def candidate_for(
    result: dict[str, Any], pairing_id: str, storage_id: str | None = None
) -> dict[str, Any]:
    return next(
        row
        for row in result["candidates"]
        if row["basis"]["pairing_id"] == pairing_id
        and row["basis"]["storage_location_id"] == storage_id
    )


def all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(all_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(all_keys(child) for child in value))
    return set()


def test_exact_verified_artifact_reuse_is_ranked_without_silent_selection() -> None:
    node = candidate()
    result = score(node)

    assert len(result["candidates"]) == 2
    assert [row["order"] for row in result["candidates"]] == [1, 2]
    first = result["candidates"][0]
    assert first["artifact_reuse"] == "verified_exact"
    assert first["estimated_download_bytes"] == 0
    assert first["basis"] == {
        "pairing_id": "28bfef6e-ce8d-4cd7-828e-79a3c99642eb",
        "credential_generation": 3,
        "inventory_instance_id": "4ea23b26-b02f-45bc-be1d-be47e40c1e76",
        "inventory_sequence": 42,
        "inventory_received_at": 1_785_528_000.0,
        "basis_expires_at": 1_785_528_060.0,
        "storage_location_id": "11111111-1111-4111-8111-111111111111",
        "storage_binding_generation": 1,
        "catalog_digest": CATALOG_DIGEST,
    }
    keys = all_keys(result)
    assert not any("selected" in key or "chosen" in key for key in keys)
    serialized = json.dumps(result)
    assert "hostname" not in serialized
    assert "/Volumes/" not in serialized


def test_scorer_output_matches_the_shared_golden_vector_and_runtime_schema() -> None:
    schema_path = (
        ROOT
        / "mac_pool_protocol"
        / "v1"
        / "placement_recommendation.schema.json"
    )
    golden_path = (
        ROOT
        / "mac_pool_protocol"
        / "v1"
        / "placement_recommendation.example.json"
    )
    assert FLEET_PLACEMENT_SCHEMA == json.loads(schema_path.read_text(encoding="utf-8"))
    assert score(candidate()) == json.loads(golden_path.read_text(encoding="utf-8"))
    validate_recommendation(json.loads(golden_path.read_text(encoding="utf-8")))


def test_signed_catalog_public_surface_normalizes_without_catalog_coupling() -> None:
    protocol = ROOT / "compatibility_catalog" / "v1"
    envelope = json.loads(
        (protocol / "catalog.golden.json").read_text(encoding="utf-8")
    )
    public_key = json.loads(
        (protocol / "test_keys.json").read_text(encoding="utf-8")
    )["keys"][0]
    key = TrustedCatalogKey.from_base64url(
        key_id=public_key["key_id"],
        public_key=public_key["public_key"],
    )
    verified = CatalogVerifier({key.key_id: key}).verify(
        envelope,
        now=1_790_000_000,
    )

    normalized = RecipeRequirements.from_verified_catalog(
        verified,
        request=PlacementRequest(
            recommendation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            created_at=1_790_000_000.0,
            valid_for_seconds=60,
            logical_model_id="example-flash-vnext",
            recipe_id="example-flash-vnext-llamacpp-q4",
            required_capabilities=("chat/completions", "responses"),
            required_context_tokens=8192,
            required_concurrency=2,
        ),
        runtime_install_mode="managed",
    )
    assert normalized.catalog_digest == envelope["catalog_digest"]
    assert normalized.logical_model_id == "example-flash-vnext"
    assert normalized.artifact_total_size_bytes == 1000
    assert normalized.memory.weights_bytes == 1200
    assert normalized.hardware.required_features == ("metal", "unified-memory")
    assert normalized.runtime.required_features == (
        "apple-metal",
        "flash-attention",
    )
    assert normalized.runtime.install_mode == "managed"


def test_input_permutation_has_identical_deterministic_candidate_order() -> None:
    nodes = []
    for index, pairing_id in enumerate(
        (
            "10000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000002",
            "10000000-0000-4000-8000-000000000003",
        )
    ):
        value = inventory()
        value["pairing_id"] = pairing_id
        value["inventory_instance_id"] = f"20000000-0000-4000-8000-00000000000{index + 1}"
        value["installations"] = []
        value["storage_locations"] = [copy.deepcopy(value["storage_locations"][0])]
        value["storage_locations"][0]["storage_location_id"] = (
            f"30000000-0000-4000-8000-00000000000{index + 1}"
        )
        nodes.append(candidate(value, display_name=f"Node {index + 1}"))

    forward = score(*nodes)
    reverse = score(*reversed(nodes))
    assert forward == reverse
    assert [row["basis"]["pairing_id"] for row in forward["candidates"]] == sorted(
        node.pairing_id for node in nodes
    )


def test_stale_restart_revocation_and_generation_change_are_independent_fences() -> None:
    cases = (
        (
            "40000000-0000-4000-8000-000000000001",
            {"freshness_state": "expired"},
            "stale_inventory",
        ),
        (
            "40000000-0000-4000-8000-000000000002",
            {"freshness_state": "hub_restarted"},
            "hub_restarted",
        ),
        (
            "40000000-0000-4000-8000-000000000003",
            {"enrollment_state": "revoked", "active_generation": None},
            "pairing_revoked",
        ),
        (
            "40000000-0000-4000-8000-000000000004",
            {"active_generation": 4},
            "credential_generation_changed",
        ),
    )
    nodes = []
    for index, (pairing_id, kwargs, _code) in enumerate(cases):
        value = inventory()
        value["pairing_id"] = pairing_id
        value["inventory_instance_id"] = f"50000000-0000-4000-8000-00000000000{index + 1}"
        value["storage_locations"] = [copy.deepcopy(value["storage_locations"][0])]
        value["storage_locations"][0]["storage_location_id"] = (
            f"60000000-0000-4000-8000-00000000000{index + 1}"
        )
        nodes.append(candidate(value, **kwargs))

    result = score(*nodes)
    assert result["expires_at"] == result["created_at"]
    for pairing_id, _kwargs, code in cases:
        row = next(
            item
            for item in result["candidates"]
            if item["basis"]["pairing_id"] == pairing_id
        )
        assert row["eligible"] is False
        assert row["order"] is None
        assert code in row["hard_gate_codes"]


def test_paused_and_draining_nodes_remain_install_eligible() -> None:
    paused = inventory()
    paused["participation"]["state"] = "paused"
    paused["storage_locations"] = [paused["storage_locations"][0]]
    result = score(candidate(paused))
    row = result["candidates"][0]
    assert row["eligible"] is True
    assert "participation_paused" in [reason["code"] for reason in row["reasons"]]
    assert "participation_paused" not in row["hard_gate_codes"]

    draining = inventory()
    draining["participation"]["state"] = "draining"
    draining["storage_locations"] = [draining["storage_locations"][0]]
    result = score(candidate(draining))
    row = result["candidates"][0]
    assert row["eligible"] is True
    assert "participation_draining" in [reason["code"] for reason in row["reasons"]]


def test_remote_install_policy_is_closed_and_ask_requires_local_approval() -> None:
    local_only = inventory()
    local_only["participation"]["remote_install_policy"] = "local-only"
    local_only["storage_locations"] = [local_only["storage_locations"][0]]
    row = score(candidate(local_only))["candidates"][0]
    assert row["eligible"] is False
    assert "remote_installs_local_only" in row["hard_gate_codes"]

    ask = inventory()
    ask["participation"]["remote_install_policy"] = "ask"
    ask["storage_locations"] = [ask["storage_locations"][0]]
    row = score(candidate(ask))["candidates"][0]
    assert row["eligible"] is True
    assert row["requires_local_approval"] is True

    disabled = inventory()
    disabled["storage_locations"] = [disabled["storage_locations"][0]]
    row = score(candidate(disabled, hub_remote_installs_enabled=False))["candidates"][0]
    assert "hub_remote_installs_disabled" in row["hard_gate_codes"]


def test_binding_generation_fence_never_follows_a_rebound_location() -> None:
    value = inventory()
    value["storage_locations"] = [value["storage_locations"][0]]
    storage_id = value["storage_locations"][0]["storage_location_id"]
    row = score(candidate(value, fences=((storage_id, 2),)))["candidates"][0]
    assert row["eligible"] is False
    assert "storage_binding_changed" in row["hard_gate_codes"]
    assert row["basis"]["storage_binding_generation"] == 1


def test_limited_memory_and_overflow_service_policy_have_explicit_effects() -> None:
    limited = inventory()
    limited["storage_locations"] = [limited["storage_locations"][0]]
    limited["hardware"]["allocatable_memory_bytes"] = 60_000_000_000
    limited_row = score(candidate(limited, service_class="overflow"))["candidates"][0]
    assert "insufficient_memory_budget" in limited_row["hard_gate_codes"]
    assert limited_row["service_class"] == "overflow"

    primary = inventory()
    primary["pairing_id"] = "70000000-0000-4000-8000-000000000001"
    primary["inventory_instance_id"] = "71000000-0000-4000-8000-000000000001"
    primary["installations"] = []
    primary["storage_locations"] = [primary["storage_locations"][0]]
    primary["storage_locations"][0]["storage_location_id"] = (
        "72000000-0000-4000-8000-000000000001"
    )
    overflow = copy.deepcopy(primary)
    overflow["pairing_id"] = "70000000-0000-4000-8000-000000000002"
    overflow["inventory_instance_id"] = "71000000-0000-4000-8000-000000000002"
    overflow["storage_locations"][0]["storage_location_id"] = (
        "72000000-0000-4000-8000-000000000002"
    )
    result = score(
        candidate(overflow, service_class="overflow", display_name="Overflow"),
        candidate(primary, service_class="primary", display_name="Primary"),
    )
    assert [row["service_class"] for row in result["candidates"]] == [
        "primary",
        "overflow",
    ]

    excluded = score(
        candidate(overflow, service_class="overflow", display_name="Overflow"),
        placement_request=request(allowed_classes=("primary",)),
    )["candidates"][0]
    assert "service_class_excluded" in excluded["hard_gate_codes"]


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda value: value["hardware"].update(os_major=13), "os_unsupported"),
        (lambda value: value["hardware"].update(gpu_cores=8), "gpu_requirement_unmet"),
        (lambda value: value["hardware"].update(gpu_cores=None), "gpu_requirement_unmet"),
        (
            lambda value: value["hardware"].update(unified_memory_bytes=32_000_000_000),
            "unified_memory_requirement_unmet",
        ),
        (
            lambda value: value["hardware"].update(soc_family="Apple M2 Pro"),
            "soc_unsupported",
        ),
        (
            lambda value: value["storage_locations"][0].update(availability="missing"),
            "storage_unavailable",
        ),
        (
            lambda value: value["storage_locations"][0].update(writable=False),
            "storage_not_writable",
        ),
        (
            lambda value: value["storage_locations"][0].update(
                free_bytes=1_000_000_000
            ),
            "insufficient_storage",
        ),
    ],
)
def test_hardware_and_storage_hard_gates_are_fixed(mutator, code: str) -> None:
    value = inventory()
    value["installations"] = []
    value["storage_locations"] = [value["storage_locations"][0]]
    mutator(value)
    if code == "unified_memory_requirement_unmet":
        value["hardware"]["allocatable_memory_bytes"] = min(
            value["hardware"]["allocatable_memory_bytes"],
            value["hardware"]["unified_memory_bytes"],
        )
    row = score(candidate(value))["candidates"][0]
    assert code in row["hard_gate_codes"]


def test_capability_context_concurrency_and_feature_contracts_fail_closed() -> None:
    value = inventory()
    value["storage_locations"] = [value["storage_locations"][0]]
    cases = (
        (
            request(capabilities=("embeddings",)),
            requirements(),
            "capability_unsupported",
        ),
        (request(context_tokens=200_000), requirements(), "context_unsupported"),
        (request(concurrency=5), requirements(), "concurrency_unsupported"),
        (
            request(),
            requirements(hardware_features=("apple-family-x",)),
            "hardware_feature_evidence_missing",
        ),
        (
            request(),
            requirements(runtime_features=("continuous-batching",)),
            "runtime_feature_evidence_missing",
        ),
    )
    for placement_request, recipe, code in cases:
        row = score(
            candidate(copy.deepcopy(value)),
            placement_request=placement_request,
            recipe=recipe,
        )["candidates"][0]
        assert code in row["hard_gate_codes"]


def test_closed_apple_feature_mapping_accepts_only_bound_v1_facts() -> None:
    value = inventory()
    value["storage_locations"] = [value["storage_locations"][0]]
    value["hardware"]["probe_version"] = 2
    row = score(
        candidate(value),
        recipe=requirements(
            hardware_features=("metal", "unified-memory"),
            runtime_features=("apple-metal",),
        ),
    )["candidates"][0]
    assert row["eligible"] is True
    assert "hardware_feature_evidence_missing" not in row["hard_gate_codes"]
    assert "runtime_feature_evidence_missing" not in row["hard_gate_codes"]

    no_metal_proof = copy.deepcopy(value)
    no_metal_proof["hardware"]["probe_version"] = 1
    rejected = score(
        candidate(no_metal_proof),
        recipe=requirements(
            hardware_features=("metal", "unified-memory"),
            runtime_features=("apple-metal",),
        ),
    )["candidates"][0]
    assert "hardware_feature_evidence_missing" in rejected["hard_gate_codes"]
    assert "runtime_feature_evidence_missing" in rejected["hard_gate_codes"]


def test_unknown_runtime_health_is_conservative_but_explicit_bad_health_gates() -> None:
    value = inventory()
    value["storage_locations"] = [value["storage_locations"][0]]
    runtime = next(row for row in value["runtimes"] if row["engine"] == "omlx")
    runtime["health"] = "unknown"
    unknown = score(candidate(copy.deepcopy(value)))["candidates"][0]
    assert unknown["eligible"] is True
    assert unknown["runtime_state"] == "compatible_unverified"
    assert unknown["evidence"]["runtime"] == "conservative"
    assert "runtime_unhealthy" not in unknown["hard_gate_codes"]

    runtime["health"] = "unhealthy"
    runtime["catalog_status"] = "unhealthy"
    runtime["diagnostic_code"] = "health_probe_failed"
    unhealthy = score(candidate(value))["candidates"][0]
    assert unhealthy["eligible"] is False
    assert "runtime_unhealthy" in unhealthy["hard_gate_codes"]


def test_runtime_missing_is_a_v1_hard_gate_even_with_future_install_policy() -> None:
    value = inventory()
    value["runtimes"] = [row for row in value["runtimes"] if row["engine"] != "omlx"]
    value["storage_locations"] = [value["storage_locations"][0]]

    unavailable = score(candidate(copy.deepcopy(value)))["candidates"][0]
    assert unavailable["runtime_state"] == "unavailable"
    assert "runtime_unavailable" in unavailable["hard_gate_codes"]

    managed = score(
        candidate(copy.deepcopy(value)),
        recipe=requirements(runtime_install_mode="managed"),
    )["candidates"][0]
    assert managed["eligible"] is False
    assert managed["runtime_state"] == "install_managed"
    assert "runtime_install_required" in {
        reason["code"] for reason in managed["reasons"]
    }
    assert "runtime_unavailable" in managed["hard_gate_codes"]

    approval = score(
        candidate(copy.deepcopy(value)),
        recipe=requirements(runtime_install_mode="local_approval"),
    )["candidates"][0]
    assert approval["eligible"] is False
    assert approval["runtime_state"] == "install_requires_approval"
    assert "runtime_install_approval_required" in {
        reason["code"] for reason in approval["reasons"]
    }
    assert "runtime_unavailable" in approval["hard_gate_codes"]


def test_desired_install_refuses_runtime_preparation_states_even_if_mislabeled() -> None:
    value = inventory()
    value["runtimes"] = [row for row in value["runtimes"] if row["engine"] != "omlx"]
    value["storage_locations"] = [value["storage_locations"][0]]
    recommendation = PlacementScorer().score(
        request(),
        requirements(runtime_install_mode="managed"),
        (candidate(value),),
    )
    selected = recommendation.value["candidates"][0]
    with pytest.raises(
        DesiredInstallAPIError,
        match="desired_install_candidate_ineligible",
    ):
        select_exact_candidate(recommendation, basis=selected["basis"])

    # Defense in depth: even a stale or buggy scorer cannot turn a runtime
    # preparation recommendation into a model-only DesiredInstall v1 job.
    selected["eligible"] = True
    selected["hard_gate_codes"] = []
    with pytest.raises(
        DesiredInstallAPIError,
        match="desired_install_candidate_ineligible",
    ):
        select_exact_candidate(recommendation, basis=selected["basis"])


def test_runtime_known_bad_and_catalog_mismatch_fail_closed() -> None:
    value = inventory()
    value["storage_locations"] = [value["storage_locations"][0]]
    runtime = next(row for row in value["runtimes"] if row["engine"] == "omlx")
    runtime["catalog_status"] = "known_bad"
    runtime["diagnostic_code"] = "runtime_known_bad"
    row = score(candidate(copy.deepcopy(value)))["candidates"][0]
    assert "runtime_known_bad" in row["hard_gate_codes"]

    mismatch = copy.deepcopy(value)
    mismatch["service"]["catalog_digest"] = "sha256:" + "9" * 64
    row = score(candidate(mismatch))["candidates"][0]
    assert "catalog_mismatch" in row["hard_gate_codes"]


def test_empty_storage_inventory_is_returned_as_an_explicit_ineligible_candidate() -> None:
    value = inventory()
    value["storage_locations"] = []
    value["installations"] = []
    row = score(candidate(value))["candidates"][0]
    assert row["basis"]["storage_location_id"] is None
    assert row["basis"]["storage_binding_generation"] is None
    assert "storage_not_registered" in row["hard_gate_codes"]


def test_candidate_input_rejects_unsafe_inventory_capacity_before_scoring() -> None:
    value = inventory()
    value["hardware"]["allocatable_memory_bytes"] = (
        value["hardware"]["unified_memory_bytes"] + 1
    )
    with pytest.raises(PlacementInputError, match="placement_inventory_invalid"):
        candidate(value)

    value = inventory()
    value["storage_locations"][0]["free_bytes"] = (
        value["storage_locations"][0]["total_bytes"] + 1
    )
    with pytest.raises(PlacementInputError, match="placement_inventory_invalid"):
        candidate(value)


def test_strict_validator_rejects_unknown_authority_and_arbitrary_explanations() -> None:
    result = score(candidate())
    result["selected_candidate"] = 1
    with pytest.raises(PlacementProtocolError, match="placement_recommendation_invalid"):
        validate_recommendation(result)

    result = score(candidate())
    result["candidates"][0]["reasons"][0]["explanation"] = "/private/model.gguf"
    with pytest.raises(PlacementProtocolError, match="placement_recommendation_reason_invalid"):
        validate_recommendation(result)


def test_parser_rejects_duplicate_members_and_non_finite_numbers() -> None:
    with pytest.raises(PlacementProtocolError, match="placement_recommendation_invalid"):
        parse_recommendation_json('{"schema_version":1,"schema_version":1}')
    with pytest.raises(PlacementProtocolError, match="placement_recommendation_invalid"):
        parse_recommendation_json('{"schema_version":NaN}')
