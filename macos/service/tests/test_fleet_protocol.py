from __future__ import annotations

import json
from pathlib import Path

import pytest

from mnemosyne_macos.fleet_protocol import (
    canonical_json,
    deployment_identity,
    derive_macos_capacity,
    normalize_capabilities,
    portable_load_config,
    semantic_extra_args,
)
from mnemosyne_macos.config import MacConfig
from mnemosyne_macos.install_store import InstallRecord
from mnemosyne_macos.runtime import _fleet_deployment_identity


PROTOCOL_ROOT = Path(__file__).resolve().parents[3] / "fleet_protocol" / "v1"


def test_ds4_parallel_slots_drive_capacity_but_not_deployment_identity() -> None:
    target = MacConfig.model_validate(
        {
            "engines": {"ds4": {"enabled": True}},
            "models": [
                {
                    "alias": "glm",
                    "engine": "ds4",
                    "model": "/models/GLM-5.2.gguf",
                    "load": {"parallel": 4},
                }
            ],
        }
    ).profiles()["glm"]

    capacity = derive_macos_capacity(
        target,
        configured_max_concurrency=None,
    )

    assert capacity.derived_limit == 4
    assert capacity.effective_limit == 4
    assert capacity.source == "ds4-batched-sessions"
    assert capacity.confidence == "configured"
    assert "parallel" not in portable_load_config(target)


def test_native_identity_matches_cross_platform_golden_vectors() -> None:
    document = json.loads(
        (PROTOCOL_ROOT / "identity_vectors.json").read_text(encoding="utf-8")
    )

    assert document["schema_version"] == 1
    for vector in document["vectors"]:
        deployment_id, load_digest, identity = deployment_identity(
            **vector["input"]
        )
        assert deployment_id == vector["expected_deployment_id"], vector["name"]
        assert load_digest == vector["expected_load_config_digest"], vector["name"]
        assert deployment_id.startswith("sha256:")
        assert canonical_json(identity) == canonical_json(
            json.loads(canonical_json(identity))
        )


def test_capabilities_are_normalized_sorted_and_unique() -> None:
    assert normalize_capabilities(
        ["/v1/responses", "chat.completions", "responses"]
    ) == ("chat/completions", "responses")


def test_llama_load_identity_excludes_paths_and_capacity_tuning() -> None:
    target = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "coder",
                    "engine": "llama.cpp",
                    "model": "/Volumes/one/model.gguf",
                    "load": {
                        "context_length": 16384,
                        "pooling": "mean",
                        "parallel": 8,
                        "threads": 12,
                        "gpu_layers": 99,
                        "projector_path": "/Volumes/one/mmproj.gguf",
                        "extra_args": [
                            "--parallel",
                            "4",
                            "--threads=16",
                            "--batch-size",
                            "512",
                            "--ctx-size=32768",
                            "--pooling-type",
                            "last",
                            "--some-semantic-mode",
                            "--parallel=2",
                            "-np=1",
                        ],
                    },
                }
            ]
        }
    ).profiles()["coder"]

    assert portable_load_config(target) == {
        "context_length": 32768,
        "pooling": "last",
        "semantic_extra_args": ["--some-semantic-mode"],
    }
    assert semantic_extra_args(["-np", "4", "--keep"]) == ["--keep"]


def test_malformed_capacity_and_resource_flags_remain_semantic() -> None:
    assert semantic_extra_args(
        [
            "--parallel",
            "invalid",
            "--parallel=0",
            "-np",
            "--keep",
            "--parallel",
        ]
    ) == [
        "--parallel",
        "invalid",
        "--parallel=0",
        "-np",
        "--keep",
        "--parallel",
    ]
    target = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "coder",
                    "engine": "llama.cpp",
                    "model": "/models/coder.gguf",
                    "load": {
                        "context_length": 4096,
                        "extra_args": [
                            "--threads",
                            "--semantic-mode",
                            "--ctx-size=invalid",
                            "--pooling",
                            "invalid",
                            "--no-kv-offload=true",
                        ],
                    },
                }
            ]
        }
    ).profiles()["coder"]

    assert portable_load_config(target) == {
        "context_length": 4096,
        "pooling": None,
        "semantic_extra_args": [
            "--threads",
            "--semantic-mode",
            "--ctx-size=invalid",
            "--pooling",
            "invalid",
            "--no-kv-offload=true",
        ],
    }


def test_managed_install_provenance_makes_exact_gguf_fleet_eligible(
    tmp_path,
) -> None:
    destination = tmp_path / "managed"
    config = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "coder",
                    "engine": "llama.cpp",
                    "model": str(destination / "model-Q4_K_M.gguf"),
                    "storage": "internal",
                    "load": {
                        "context_length": 16384,
                        "projector_path": str(destination / "mmproj-F16.gguf"),
                    },
                }
            ]
        }
    )
    profile = config.models[0]
    target = config.profiles()["coder"]
    install = InstallRecord(
        id="install-1",
        repo_id="publisher/coder-GGUF",
        engine="llama.cpp",
        storage="internal",
        alias="coder",
        destination=str(destination),
        status="installed",
        revision="0123456789ABCDEF0123456789ABCDEF01234567",
        filename="model-Q4_K_M.gguf",
        projector_filename="mmproj-F16.gguf",
        files_json=json.dumps(
            [
                "model-Q4_K_M.gguf",
                "model-00002-of-00002-Q4_K_M.gguf",
                "mmproj-F16.gguf",
                "README.md",
            ]
        ),
    )

    deployment_id, identity, eligible = _fleet_deployment_identity(
        node_id="mac-node",
        profile=profile,
        target=target,
        install=install,
    )

    assert eligible is True
    assert deployment_id.startswith("sha256:")
    assert identity["upstream_model"] == "publisher/coder-GGUF"
    assert identity["resolved_revision"] == install.revision.lower()
    assert identity["artifact"] == {
        "format": "gguf",
        "selected_files": ["mmproj-F16.gguf", "model-Q4_K_M.gguf"],
        "quantization": "Q4_K_M",
        "content_digest": None,
    }
    serialized = canonical_json(identity)
    assert str(tmp_path) not in serialized
    assert "projector_path" not in serialized
    cuda_style_id, _digest, _document = deployment_identity(
        engine="llama.cpp",
        upstream_model="publisher/coder-GGUF",
        resolved_revision=install.revision.lower(),
        artifact={
            "format": "gguf",
            "selected_files": ["mmproj-F16.gguf", "model-Q4_K_M.gguf"],
            "quantization": "Q4_K_M",
            "content_digest": None,
        },
        kind="language",
        capabilities=(endpoint.value for endpoint in target.capabilities),
        load_config={
            "context_length": 16384,
            "pooling": None,
            "semantic_extra_args": [],
        },
    )
    assert deployment_id == cuda_style_id
    shared = next(
        vector
        for vector in json.loads(
            (PROTOCOL_ROOT / "identity_vectors.json").read_text(encoding="utf-8")
        )["vectors"]
        if vector["name"] == "llamacpp-generation-with-projector"
    )
    assert deployment_id == shared["expected_deployment_id"]


@pytest.mark.parametrize(
    (
        "profile_model",
        "profile_projector",
        "install_filename",
        "install_projector",
    ),
    [
        ("alternate.gguf", None, "model.gguf", None),
        ("model.gguf", None, None, None),
        ("model.gguf", "mmproj.gguf", "model.gguf", None),
        ("model.gguf", None, "model.gguf", "mmproj.gguf"),
        ("model.gguf", "alternate-mmproj.gguf", "model.gguf", "mmproj.gguf"),
        ("../outside.gguf", None, "../outside.gguf", None),
        (r"weights\model.gguf", None, r"weights\model.gguf", None),
        (
            "model.gguf",
            "../outside-mmproj.gguf",
            "model.gguf",
            "../outside-mmproj.gguf",
        ),
    ],
)
def test_gguf_managed_provenance_requires_exact_safe_selected_paths(
    tmp_path,
    profile_model: str,
    profile_projector: str | None,
    install_filename: str | None,
    install_projector: str | None,
) -> None:
    destination = tmp_path / "managed"
    load = (
        {"projector_path": str(destination / profile_projector)}
        if profile_projector is not None
        else {}
    )
    config = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "coder",
                    "engine": "llama.cpp",
                    "model": str(destination / profile_model),
                    "storage": "internal",
                    "load": load,
                }
            ]
        }
    )
    install = InstallRecord(
        id="install-mismatch",
        repo_id="publisher/coder-GGUF",
        engine="llama.cpp",
        storage="internal",
        alias="coder",
        destination=str(destination),
        status="installed",
        revision="a" * 40,
        filename=install_filename,
        projector_filename=install_projector,
    )

    _deployment_id, identity, eligible = _fleet_deployment_identity(
        node_id="mac-node",
        profile=config.models[0],
        target=config.profiles()["coder"],
        install=install,
    )

    assert eligible is False
    assert identity["upstream_model"] == "node-local:mac-node:coder"
    assert identity["artifact"]["selected_files"] == []


def test_gguf_managed_provenance_accepts_exact_nested_selection_only(
    tmp_path,
) -> None:
    destination = tmp_path / "managed"
    filename = "weights/model-Q4_K_M.gguf"
    projector = "vision/mmproj-F16.gguf"
    config = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "coder",
                    "engine": "llama.cpp",
                    "model": str(destination / filename),
                    "storage": "internal",
                    "load": {
                        "projector_path": str(destination / projector),
                    },
                }
            ]
        }
    )
    install = InstallRecord(
        id="install-nested",
        repo_id="publisher/coder-GGUF",
        engine="llama.cpp",
        storage="internal",
        alias="coder",
        destination=str(destination),
        status="installed",
        revision="A" * 40,
        filename=filename,
        projector_filename=projector,
        files_json=json.dumps(
            [
                filename,
                "weights/model-00002-of-00002-Q4_K_M.gguf",
                projector,
            ]
        ),
    )

    _deployment_id, identity, eligible = _fleet_deployment_identity(
        node_id="mac-node",
        profile=config.models[0],
        target=config.profiles()["coder"],
        install=install,
    )

    assert eligible is True
    assert identity["resolved_revision"] == "a" * 40
    assert identity["artifact"]["selected_files"] == [
        projector,
        filename,
    ]


def test_native_llama_projection_matches_shared_cuda_golden_vector(
    tmp_path,
) -> None:
    vectors = json.loads(
        (PROTOCOL_ROOT / "identity_vectors.json").read_text(encoding="utf-8")
    )["vectors"]
    vector = next(
        item for item in vectors if item["name"] == "llamacpp-exact-gguf-selection"
    )
    source = vector["input"]
    filename = source["artifact"]["selected_files"][0]
    destination = tmp_path / "managed"
    config = MacConfig.model_validate(
        {
            "models": [
                {
                    "alias": "coder",
                    "engine": "llama.cpp",
                    "model": str(destination / filename),
                    "storage": "internal",
                    "capabilities": source["capabilities"],
                    "load": {"context_length": 16384},
                }
            ]
        }
    )
    install = InstallRecord(
        id="install-golden",
        repo_id=source["upstream_model"],
        engine="llama.cpp",
        storage="internal",
        alias="coder",
        destination=str(destination),
        status="installed",
        revision=source["resolved_revision"],
        filename=filename,
        files_json=json.dumps([filename]),
    )

    deployment_id, identity, eligible = _fleet_deployment_identity(
        node_id="mac-node",
        profile=config.models[0],
        target=config.profiles()["coder"],
        install=install,
    )

    assert eligible is True
    assert deployment_id == vector["expected_deployment_id"]
    assert identity["load_config_digest"] == (
        vector["expected_load_config_digest"]
    )


def test_symbolic_or_mismatched_install_provenance_is_not_fleet_eligible(
    tmp_path,
) -> None:
    destination = tmp_path / "managed"
    config = MacConfig.model_validate(
        {
            "engines": {"omlx": {"enabled": True}},
            "models": [
                {
                    "alias": "coder",
                    "engine": "omlx",
                    "model": destination.name,
                    "storage": "internal",
                }
            ],
        }
    )
    install = InstallRecord(
        id="install-1",
        repo_id="publisher/coder-MLX",
        engine="omlx",
        storage="internal",
        alias="coder",
        destination=str(destination),
        status="installed",
        revision="main",
    )

    _deployment_id, _identity, eligible = _fleet_deployment_identity(
        node_id="mac-node",
        profile=config.models[0],
        target=config.profiles()["coder"],
        install=install,
    )

    assert eligible is False


def test_corrupt_managed_provenance_cannot_disclose_local_paths(tmp_path) -> None:
    destination = tmp_path / "managed"
    config = MacConfig.model_validate(
        {
            "engines": {"omlx": {"enabled": True}},
            "models": [
                {
                    "alias": "coder",
                    "engine": "omlx",
                    "model": destination.name,
                    "storage": "internal",
                }
            ],
        }
    )
    install = InstallRecord(
        id="install-1",
        repo_id=str(tmp_path / "private-repository-name"),
        engine="omlx",
        storage="internal",
        alias="coder",
        destination=str(destination),
        status="installed",
        revision="a" * 40,
    )

    _deployment_id, identity, eligible = _fleet_deployment_identity(
        node_id="mac-node",
        profile=config.models[0],
        target=config.profiles()["coder"],
        install=install,
    )

    assert eligible is False
    assert identity["upstream_model"] == "node-local:mac-node:coder"
    assert str(tmp_path) not in canonical_json(identity)
