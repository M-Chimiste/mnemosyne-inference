from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mnemosyne_fleet.protocol import validate_snapshot

from .helpers import identity, snapshot_payload


def test_packaged_schema_matches_canonical_protocol() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    canonical = json.loads(
        (
            repository_root
            / "fleet_protocol"
            / "v1"
            / "snapshot.schema.json"
        ).read_text(encoding="utf-8")
    )
    packaged = json.loads(
        (
            repository_root
            / "fleet"
            / "src"
            / "mnemosyne_fleet"
            / "schemas"
            / "snapshot.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert packaged == canonical
    assert canonical["properties"]["deployments"]["maxItems"] == 10_000
    assert (
        canonical["$defs"]["admission"]["properties"]["queued_by_deployment"][
            "maxProperties"
        ]
        == 10_000
    )
    assert (
        canonical["$defs"]["capacity"]["properties"]["effective_limit"][
            "maximum"
        ]
        == 100_000
    )


def test_shared_canonical_example_remains_accepted() -> None:
    example_path = (
        Path(__file__).resolve().parents[2]
        / "fleet_protocol"
        / "v1"
        / "snapshot.example.json"
    )
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    result = validate_snapshot(
        payload,
        expected_node_id="athena-cuda",
        ttl_seconds=1,
    )
    assert result.snapshot_sequence == 7


def test_valid_snapshot_uses_strict_canonical_identity() -> None:
    payload = snapshot_payload("node-a")
    result = validate_snapshot(
        payload,
        expected_node_id="node-a",
        ttl_seconds=1,
    )
    assert result.deployments[0].fleet_eligible is True
    assert result.deployments[0].identity_confidence == "authoritative"


def test_deployment_id_must_match_all_identity_fields() -> None:
    payload = snapshot_payload("node-a")
    payload["deployments"][0]["identity"]["artifact"]["quantization"] = "Q8_0"
    with pytest.raises(ValueError, match="canonical identity"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_symbolic_revision_without_content_digest_is_not_fleet_eligible() -> None:
    identity_value, deployment_id = identity(revision="main", content_digest=None)
    payload = snapshot_payload(
        "node-a",
        deployment_id=deployment_id,
        identity_value=identity_value,
    )
    with pytest.raises(ValueError, match="protocol v1"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_unknown_snapshot_fields_are_rejected() -> None:
    payload = snapshot_payload("node-a")
    payload["node"]["secret"] = "must-not-pass"
    with pytest.raises(ValueError, match="protocol v1"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_arrays_must_be_canonical() -> None:
    payload = snapshot_payload("node-a")
    payload["deployments"][0]["identity"]["capabilities"] = [
        "responses",
        "completions",
        "chat/completions",
    ]
    with pytest.raises(ValueError, match="identity capabilities"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


@pytest.mark.parametrize(
    "selected_file",
    ["./model.gguf", "weights//model.gguf", "weights/./model.gguf", "weights/"],
)
def test_artifact_files_reject_empty_or_dot_path_segments(
    selected_file: str,
) -> None:
    payload = snapshot_payload("node-a")
    payload["deployments"][0]["identity"]["artifact"]["selected_files"] = [
        selected_file
    ]
    with pytest.raises(ValueError, match="protocol v1"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_nonaccepting_snapshot_must_publish_zero_available() -> None:
    payload = snapshot_payload("node-a", accepting=False)
    payload["capacity"]["active"] = 1
    payload["capacity"]["available"] = 1
    payload["capacity"]["saturation"] = 0.5
    with pytest.raises(ValueError, match="closed admission"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_node_wall_clock_does_not_control_ttl() -> None:
    payload = snapshot_payload("node-a")
    payload["observed_at"] = 1
    result = validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)
    assert result.observed_at == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload["admission"].update(queue_depth=1),
            "queue_depth must equal",
        ),
        (
            lambda payload: payload["capacity"].update(queued=1),
            "capacity.queued must equal",
        ),
        (
            lambda payload: payload["deployments"][0]["capacity"].update(queued=1),
            "deployment queued capacity",
        ),
    ],
)
def test_queue_and_capacity_cross_fields_are_consistent(
    mutation,
    message: str,
) -> None:
    payload = snapshot_payload("node-a")
    mutation(payload)
    with pytest.raises(ValueError, match=message):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_queue_depth_cannot_exceed_node_queue_limit() -> None:
    payload = snapshot_payload(
        "node-a",
        queue_depth=2,
        queue_limit=1,
    )
    with pytest.raises(ValueError, match="exceeds queue_limit"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_warm_alias_synonyms_for_one_strict_deployment_are_valid() -> None:
    payload = snapshot_payload("node-a")
    synonym = copy.deepcopy(payload["deployments"][0])
    synonym["alias"] = "node-a-qwen-synonym"
    payload["deployments"].append(synonym)

    result = validate_snapshot(
        payload,
        expected_node_id="node-a",
        ttl_seconds=1,
    )
    assert {row.alias for row in result.deployments if row.warm} == {
        "node-a-qwen",
        "node-a-qwen-synonym",
    }


def test_deployment_aliases_must_be_unique() -> None:
    payload = snapshot_payload("node-a")
    payload["deployments"].append(copy.deepcopy(payload["deployments"][0]))
    with pytest.raises(ValueError, match="aliases must be unique"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_two_distinct_warm_deployments_are_rejected() -> None:
    payload = snapshot_payload("node-a")
    other_identity, other_id = identity(quantization="Q8_0")
    other = copy.deepcopy(payload["deployments"][0])
    other.update(
        alias="other",
        deployment_id=other_id,
        identity=other_identity,
    )
    payload["deployments"].append(other)

    with pytest.raises(ValueError, match="one distinct deployment"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_residency_alias_and_engine_must_match_warm_deployment() -> None:
    payload = snapshot_payload("node-a")
    payload["residency"]["alias"] = "not-advertised"
    with pytest.raises(ValueError, match="resident alias"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)

    payload = snapshot_payload("node-a")
    payload["residency"]["engine"] = "vllm"
    with pytest.raises(ValueError, match="resident engine"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_transition_target_must_be_known_transitional_and_close_capacity() -> None:
    payload = snapshot_payload("node-a", queue_depth=1)
    deployment_id = payload["deployments"][0]["deployment_id"]
    payload["health"]["state"] = "draining"
    payload["residency"]["transition_target"] = deployment_id
    payload["capacity"]["available"] = 0
    validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)

    open_capacity = snapshot_payload("node-a", queue_depth=1)
    open_capacity["health"]["state"] = "draining"
    open_capacity["residency"]["transition_target"] = open_capacity[
        "deployments"
    ][0]["deployment_id"]
    with pytest.raises(ValueError, match="close root capacity"):
        validate_snapshot(
            open_capacity,
            expected_node_id="node-a",
            ttl_seconds=1,
        )


def test_transition_may_outlive_its_last_waiter() -> None:
    payload = snapshot_payload("node-a")
    payload["health"]["state"] = "loading"
    payload["residency"]["transition_target"] = payload["deployments"][0][
        "deployment_id"
    ]
    payload["capacity"]["available"] = 0
    validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_transition_target_must_be_advertised_and_in_transition_state() -> None:
    payload = snapshot_payload("node-a")
    payload["health"]["state"] = "draining"
    payload["capacity"]["available"] = 0
    payload["residency"]["transition_target"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="not advertised"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)

    payload = snapshot_payload("node-a", queue_depth=1)
    payload["capacity"]["available"] = 0
    payload["residency"]["transition_target"] = payload["deployments"][0][
        "deployment_id"
    ]
    with pytest.raises(ValueError, match="health state"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_authoritative_revision_must_be_lowercase() -> None:
    identity_value, deployment_id = identity(revision="A" * 40)
    payload = snapshot_payload(
        "node-a",
        deployment_id=deployment_id,
        identity_value=identity_value,
    )
    with pytest.raises(ValueError, match="protocol v1"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)


def test_capacity_saturation_must_match_active_over_effective_limit() -> None:
    payload = snapshot_payload("node-a")
    payload["capacity"]["saturation"] = 0.5
    with pytest.raises(ValueError, match="saturation is inconsistent"):
        validate_snapshot(payload, expected_node_id="node-a", ttl_seconds=1)
