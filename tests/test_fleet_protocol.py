from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet_protocol import (
    canonical_json,
    deployment_identity,
    derive_cuda_capacity,
    effective_capacity,
    gguf_quantization,
    identity_is_authoritative,
    normalize_capabilities,
    positive_int_flag,
    semantic_extra_args,
)

PROTOCOL_V1 = Path(__file__).resolve().parents[1] / "fleet_protocol" / "v1"


def test_canonical_json_is_stable_and_compact():
    assert canonical_json({"z": 1, "a": {"b": 2}}) == '{"a":{"b":2},"z":1}'


def test_capabilities_normalize_legacy_cuda_spellings():
    assert normalize_capabilities(
        ["responses", "chat.completions", "images/generations"]
    ) == ("chat/completions", "images/generations", "responses")


def test_unknown_capability_is_rejected():
    with pytest.raises(ValueError, match="unsupported fleet capability"):
        normalize_capabilities(["totally/new"])


def test_deployment_identity_excludes_alias_and_placement():
    first = deployment_identity(
        engine="vllm",
        upstream_model="org/model",
        resolved_revision="abc123",
        artifact={"format": "safetensors", "quantization": "awq"},
        kind="language",
        capabilities=["chat.completions"],
        load_config={"max_model_len": 4096, "gpus": [0]},
    )
    second = deployment_identity(
        engine="vllm",
        upstream_model="org/model",
        resolved_revision="abc123",
        artifact={"quantization": "awq", "format": "safetensors"},
        kind="language",
        capabilities=["chat/completions"],
        load_config={"gpus": [0], "max_model_len": 4096},
    )
    assert first == second
    deployment_id, load_digest, identity = first
    assert deployment_id.startswith("sha256:")
    assert load_digest.startswith("sha256:")
    assert "alias" not in json.dumps(identity)
    assert "storage" not in json.dumps(identity)


def test_strict_identity_changes_with_revision_or_load_config():
    base = dict(
        engine="llama.cpp",
        upstream_model="org/model",
        artifact={"format": "gguf", "filename": "model.gguf"},
        kind="language",
        capabilities=["chat/completions"],
    )
    revision_a = deployment_identity(
        **base,
        resolved_revision="a",
        load_config={"parallel": 2},
    )[0]
    revision_b = deployment_identity(
        **base,
        resolved_revision="b",
        load_config={"parallel": 2},
    )[0]
    load_b = deployment_identity(
        **base,
        resolved_revision="a",
        load_config={"parallel": 4},
    )[0]
    assert len({revision_a, revision_b, load_b}) == 3


def test_immutable_hex_revision_is_canonicalized_to_lowercase():
    kwargs = {
        "engine": "llama.cpp",
        "upstream_model": "org/model",
        "artifact": {
            "format": "gguf",
            "selected_files": ["model.gguf"],
            "quantization": None,
            "content_digest": None,
        },
        "kind": "language",
        "capabilities": ["chat/completions"],
        "load_config": {"context_length": 4096},
    }
    uppercase = deployment_identity(
        **kwargs,
        resolved_revision="ABCDEF0123456789ABCDEF0123456789ABCDEF01",
    )
    lowercase = deployment_identity(
        **kwargs,
        resolved_revision="abcdef0123456789abcdef0123456789abcdef01",
    )

    assert uppercase == lowercase
    assert uppercase[2]["resolved_revision"] == (
        "abcdef0123456789abcdef0123456789abcdef01"
    )


def test_authoritative_identity_requires_immutable_provenance():
    assert identity_is_authoritative(
        resolved_revision="a" * 40,
        artifact={"content_digest": None},
    )
    assert identity_is_authoritative(
        resolved_revision=None,
        artifact={"content_digest": "sha256:" + "b" * 64},
    )
    assert not identity_is_authoritative(
        resolved_revision="main",
        artifact={"content_digest": None},
    )


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("model.Q4_K_M.gguf", "Q4_K_M"),
        ("vision-bf16.gguf", "BF16"),
        ("weights.IQ3_XS.gguf", "IQ3_XS"),
        ("model.gguf", None),
    ],
)
def test_gguf_quantization_is_portable(filename, expected):
    assert gguf_quantization(filename) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--max-num-seqs", "8"], 8),
        (["--max-num-seqs=12"], 12),
        (["--max-num-seqs", "bad"], None),
        (["--max-num-seqs", "4", "--max-num-seqs=6"], 6),
    ],
)
def test_positive_int_flag(args, expected):
    assert positive_int_flag(args, "--max-num-seqs") == expected


def test_semantic_extra_args_remove_only_valid_capacity_controls():
    assert semantic_extra_args(
        [
            "--parallel",
            "4",
            "--reasoning-format",
            "deepseek",
            "--parallel=2",
        ],
        backend="llama.cpp",
    ) == ("--reasoning-format", "deepseek")
    assert semantic_extra_args(
        ["--parallel=bad", "--chat-template", "custom"],
        backend="llama.cpp",
    ) == ("--parallel=bad", "--chat-template", "custom")


def test_configured_max_is_a_ceiling():
    capacity = effective_capacity(
        derived_limit=8,
        configured_max_concurrency=3,
        active=2,
        queued=1,
        source="test",
        confidence="authoritative",
    )
    assert capacity.effective_limit == 3
    assert capacity.available == 1
    assert capacity.saturation == pytest.approx(2 / 3, abs=1e-6)


def test_closed_admission_reports_no_available_capacity():
    capacity = effective_capacity(
        derived_limit=8,
        configured_max_concurrency=None,
        active=2,
        queued=1,
        source="test",
        confidence="authoritative",
        admission_open=False,
    )
    assert capacity.effective_limit == 8
    assert capacity.available == 0


def test_cuda_capacity_uses_engine_specific_sources():
    vllm = derive_cuda_capacity(
        backend="vllm",
        extra_args=["--max-num-seqs", "16"],
        configured_max_concurrency=6,
        active=4,
        queued=2,
    )
    assert vllm.derived_limit == 16
    assert vllm.effective_limit == 6
    assert vllm.source == "vllm-max-num-seqs"

    llama = derive_cuda_capacity(
        backend="llama.cpp",
        extra_args=["--parallel=3"],
        configured_max_concurrency=None,
        active=1,
        queued=0,
    )
    assert llama.effective_limit == 3
    assert llama.source == "llama.cpp-parallel"


def test_unknown_capacity_fails_conservatively():
    capacity = derive_cuda_capacity(
        backend="future-engine",
        extra_args=[],
        configured_max_concurrency=None,
        active=0,
        queued=0,
    )
    assert capacity.effective_limit == 1
    assert capacity.confidence == "conservative"


def test_cross_platform_identity_vectors_match_v1_contract():
    document = json.loads((PROTOCOL_V1 / "identity_vectors.json").read_text())
    assert document["schema_version"] == 1
    for vector in document["vectors"]:
        deployment_id, load_digest, _identity = deployment_identity(
            **vector["input"]
        )
        assert deployment_id == vector["expected_deployment_id"], vector["name"]
        assert load_digest == vector["expected_load_config_digest"], vector["name"]


def test_snapshot_schema_is_well_formed_json_and_strict_at_top_level():
    schema = json.loads((PROTOCOL_V1 / "snapshot.schema.json").read_text())
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["additionalProperties"] is False
