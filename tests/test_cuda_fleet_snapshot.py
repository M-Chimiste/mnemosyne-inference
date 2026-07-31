from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

import vllm_manager
from config import Config, ModelProfile
from profiles import ResolvedProfile, resolve_profile


SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "fleet_protocol"
    / "v1"
    / "snapshot.schema.json"
)


def test_fleet_snapshot_is_not_exposed_without_credential(inference_client):
    assert inference_client.get("/fleet/v1/snapshot").status_code == 404


def test_fleet_snapshot_requires_its_own_bearer(inference_client, monkeypatch):
    monkeypatch.setenv("FLEET_API_KEY", "fleet-secret")
    vllm_manager._config.fleet.node_id = "cuda-test"

    response = inference_client.get("/fleet/v1/snapshot")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert inference_client.get(
        "/fleet/v1/snapshot",
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401


def test_fleet_snapshot_fails_closed_when_inference_auth_is_open(
    inference_client,
    monkeypatch,
):
    monkeypatch.setenv("FLEET_API_KEY", "fleet-secret")
    monkeypatch.delenv("INFERENCE_API_KEY", raising=False)
    vllm_manager._config.fleet.node_id = "cuda-test"

    response = inference_client.get(
        "/fleet/v1/snapshot",
        headers={"Authorization": "Bearer fleet-secret"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "fleet_inference_auth_unconfigured"
    )


def test_snapshot_matches_strict_schema_and_redacts_private_state(
    inference_client,
    monkeypatch,
):
    monkeypatch.setenv("FLEET_API_KEY", "fleet-secret")
    monkeypatch.setenv("INFERENCE_API_KEY", "inference-secret")
    monkeypatch.setenv("TOKEN_SIDECAR_POSTGRES_DSN", "postgresql://user:secret@db/ledger")
    vllm_manager._config.fleet.node_id = "cuda-test"
    vllm_manager._config.models = [
        ModelProfile(
            alias="fleet-model",
            model="org/fleet-model",
            revision="0123456789abcdef0123456789abcdef01234567",
            extra_args=["--max-num-seqs", "4"],
        )
    ]
    vllm_manager._catalog.apply_config(
        vllm_manager._config.models,
        vllm_manager._config.storage.default,
        {
            location.name: location.path
            for location in vllm_manager._config.storage.locations
        },
    )
    vllm_manager._pg_last_error = "postgresql://user:secret@db/ledger"

    first = inference_client.get(
        "/fleet/v1/snapshot",
        headers={"Authorization": "Bearer fleet-secret"},
    )
    second = inference_client.get(
        "/fleet/v1/snapshot",
        headers={"Authorization": "Bearer fleet-secret"},
    )
    assert first.status_code == 200
    payload = first.json()
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)

    assert payload["node"]["node_id"] == "cuda-test"
    assert payload["node"]["platform"] == "cuda"
    assert payload["health"]["authoritative"] is True
    assert payload["admission"]["queue_limit"] >= 1
    assert payload["deployments"][0]["fleet_eligible"] is True
    assert payload["deployments"][0]["capacity"]["effective_limit"] == 4
    assert second.json()["snapshot_sequence"] > payload["snapshot_sequence"]

    serialized = json.dumps(payload)
    assert "fleet-secret" not in serialized
    assert "postgresql://" not in serialized
    assert "secret@" not in serialized
    assert str(vllm_manager._config.storage.locations[0].path) not in serialized
    assert "--max-num-seqs" not in serialized


def test_llamacpp_identity_matches_cross_platform_golden_vector():
    vector = next(
        item
        for item in json.loads(
            (
                SCHEMA_PATH.parent / "identity_vectors.json"
            ).read_text()
        )["vectors"]
        if item["name"] == "llamacpp-exact-gguf-selection"
    )
    profile = ResolvedProfile(
        alias="local-alias",
        served_model_name="local-alias",
        engine_model_path="/private/models/model.gguf",
        gpus=[0],
        quantization=None,
        max_model_len=16384,
        gpu_memory_utilization=0.9,
        trust_remote_code=False,
        storage_name="private",
        storage_path="/private/models",
        extra_args=("--parallel", "4"),
        revision="89abcdef0123456789abcdef0123456789abcdef",
        backend="llama.cpp",
        gguf_filename="Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
        kind="language",
        capabilities=("chat.completions", "responses"),
        upstream_model="bartowski/Qwen2.5-Coder-7B-Instruct-GGUF",
    )

    deployment_id, identity, authoritative = vllm_manager._profile_deployment(profile)
    expected_identity = dict(vector["input"])
    expected_identity.pop("load_config")
    expected_identity["capabilities"] = ["chat/completions", "responses"]
    expected_identity["load_config_digest"] = vector["expected_load_config_digest"]
    expected_identity["protocol"] = 1
    assert deployment_id == vector["expected_deployment_id"]
    assert identity == expected_identity
    assert authoritative is True


def test_real_cuda_config_matches_generation_projector_golden_vector(tmp_path):
    from catalog import open_catalog

    vector = next(
        item
        for item in json.loads(
            (SCHEMA_PATH.parent / "identity_vectors.json").read_text()
        )["vectors"]
        if item["name"] == "llamacpp-generation-with-projector"
    )
    source = vector["input"]
    selected = source["artifact"]["selected_files"]
    projector = next(name for name in selected if "mmproj" in name.lower())
    primary = next(name for name in selected if name != projector)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / primary).write_bytes(b"gguf")
    (snapshot / projector).write_bytes(b"projector")

    cfg = Config.model_validate({
        "storage": {
            "default": "tmp",
            "locations": [{"name": "tmp", "path": str(tmp_path)}],
        },
        "defaults": {"trust_remote_code": False},
        "models": [{
            "alias": "coder",
            "model": source["upstream_model"],
            "revision": source["resolved_revision"],
            "backend": "llama.cpp",
            "gguf_filename": primary,
            "projector_filename": projector,
            "gpus": [0],
            "max_model_len": 16384,
            "extra_args": ["--parallel", "4"],
            "capabilities": source["capabilities"],
        }],
    })
    catalog = open_catalog(":memory:")
    try:
        catalog._raw_insert_model(
            alias="coder",
            hf_model_id=source["upstream_model"],
            source="config",
            gpus="[0]",
            storage_location="tmp",
            status="installed",
            cache_path=str(snapshot),
            resolved_sha=source["resolved_revision"],
            backend="llama.cpp",
            gguf_filename=primary,
        )
        profile = resolve_profile("coder", cfg, catalog)
    finally:
        catalog.close()

    assert profile.capabilities == (
        "chat.completions",
        "completions",
        "responses",
    )
    assert profile.projector_model_path == str(snapshot / projector)
    deployment_id, identity, authoritative = vllm_manager._profile_deployment(
        profile
    )
    assert deployment_id == vector["expected_deployment_id"]
    assert identity["resolved_revision"] == source["resolved_revision"].lower()
    assert identity["artifact"]["selected_files"] == selected
    assert identity["load_config_digest"] == (
        vector["expected_load_config_digest"]
    )
    assert authoritative is True


def test_llamacpp_artifact_excludes_automatically_discovered_shards():
    profile = ResolvedProfile(
        alias="sharded",
        served_model_name="sharded",
        engine_model_path="/private/model-00001-of-00002.gguf",
        gpus=[0],
        quantization=None,
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        trust_remote_code=False,
        storage_name="private",
        storage_path="/private",
        extra_args=(),
        revision="a" * 40,
        backend="llama.cpp",
        gguf_filename="model-Q4_K_M-00001-of-00002.gguf",
        projector_filename="mmproj-F16.gguf",
        projector_model_path="/private/mmproj-F16.gguf",
        upstream_model="org/model",
    )

    assert vllm_manager._profile_artifact(profile)["selected_files"] == [
        "mmproj-F16.gguf",
        "model-Q4_K_M-00001-of-00002.gguf",
    ]


def test_malformed_capacity_flag_cannot_collapse_semantic_identity():
    base = dict(
        alias="a",
        served_model_name="a",
        engine_model_path="/private/model.gguf",
        gpus=[0],
        quantization="Q4_K_M",
        max_model_len=4096,
        gpu_memory_utilization=0.9,
        trust_remote_code=False,
        storage_name="private",
        storage_path="/private",
        revision="89abcdef0123456789abcdef0123456789abcdef",
        backend="llama.cpp",
        gguf_filename="model.Q4_K_M.gguf",
        upstream_model="org/model",
    )
    malformed = ResolvedProfile(
        **base,
        extra_args=("--parallel", "--chat-template", "custom"),
    )
    semantic = ResolvedProfile(
        **base,
        extra_args=("--chat-template", "custom"),
    )
    assert (
        vllm_manager._profile_deployment(malformed)[0]
        != vllm_manager._profile_deployment(semantic)[0]
    )
