from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_V1 = REPOSITORY_ROOT / "compatibility_catalog" / "v1"


def _documents() -> tuple[dict[str, Any], dict[str, Any]]:
    schema = json.loads(
        (PROTOCOL_V1 / "catalog.schema.json").read_text(encoding="utf-8")
    )
    golden = json.loads(
        (PROTOCOL_V1 / "catalog.golden.json").read_text(encoding="utf-8")
    )
    return schema, golden


def _gguf_layout_golden() -> dict[str, Any]:
    return json.loads(
        (PROTOCOL_V1 / "catalog.gguf-layout.golden.json").read_text(
            encoding="utf-8"
        )
    )


def _walk_schema(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_schema(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_schema(child)


def test_catalog_v1_schema_and_test_signed_golden_agree() -> None:
    schema, golden = _documents()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(golden)
    assert schema["properties"]["schema_version"]["const"] == 1
    assert golden["schema_version"] == 1
    assert golden["catalog"]["catalog_sequence"] > 0
    assert golden["catalog"]["issued_at"] < golden["catalog"]["expires_at"]


def test_additive_gguf_layout_golden_is_exact_and_legacy_golden_is_unchanged() -> None:
    schema, legacy = _documents()
    layout_catalog = _gguf_layout_golden()
    Draft202012Validator(schema).validate(layout_catalog)

    # The original one-file v1 fixture remains byte/identity compatible.
    assert legacy["catalog_digest"] == (
        "sha256:e4c1a007806ae238bb4bafe96777f85b2541daa30f295edee90ba78bb1d6f39e"
    )
    assert "gguf_layout" not in legacy["catalog"]["artifacts"][0]

    artifact = layout_catalog["catalog"]["artifacts"][0]
    layout = artifact["gguf_layout"]
    selected = [layout["primary_file"], *layout["required_shards"]]
    if layout["selected_projector_file"] is not None:
        selected.append(layout["selected_projector_file"])
    assert set(selected) == {item["path"] for item in artifact["files"]}
    assert len(selected) == len(set(selected))
    assert layout_catalog["catalog_digest"] != legacy["catalog_digest"]


def test_every_wire_object_is_closed_and_collections_are_bounded() -> None:
    schema, _golden = _documents()
    for node in _walk_schema(schema):
        node_type = node.get("type")
        if node_type == "object":
            assert node.get("additionalProperties") is False
        if node_type == "array":
            assert isinstance(node.get("maxItems"), int)
            assert node["maxItems"] > 0
        if node_type == "string" or (
            isinstance(node_type, list) and "string" in node_type
        ):
            assert isinstance(node.get("maxLength"), int)
            assert node["maxLength"] > 0


@pytest.mark.parametrize(
    ("container", "field", "value"),
    [
        ("catalog", "download_url", "https://attacker.invalid/model"),
        ("artifact", "path", "/Volumes/Athena/models"),
        ("artifact", "destination", "/tmp/model"),
        ("source", "url", "https://attacker.invalid/model"),
        ("source", "credential", "secret"),
        ("recipe", "engine_args", ["--override"]),
        ("recipe", "command", "llama-server --override"),
        ("launch", "extra_args", ["--override"]),
        ("launch", "environment", {"TOKEN": "secret"}),
    ],
)
def test_catalog_rejects_location_and_execution_authority(
    container: str,
    field: str,
    value: object,
) -> None:
    schema, golden = _documents()
    targets = {
        "catalog": golden["catalog"],
        "artifact": golden["catalog"]["artifacts"][0],
        "source": golden["catalog"]["artifacts"][0]["source"],
        "recipe": golden["catalog"]["recipes"][0],
        "launch": golden["catalog"]["recipes"][0]["launch"],
    }
    targets[container][field] = value
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(golden)


@pytest.mark.parametrize(
    "source_identity",
    [
        "https://huggingface.co/org/model",
        "http://github.com/org/repo",
        "file:///Volumes/Athena/model",
        "../private/model",
        "/absolute/model",
        "org/model/extra",
    ],
)
def test_repository_identity_cannot_be_a_browser_url_or_path(
    source_identity: str,
) -> None:
    schema, golden = _documents()
    golden["catalog"]["artifacts"][0]["source"][
        "repository_id"
    ] = source_identity
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(golden)


@pytest.mark.parametrize(
    "selected_file",
    [
        "/Volumes/Athena/model.gguf",
        "../model.gguf",
        "weights/../../model.gguf",
        "weights//model.gguf",
        "weights\\model.gguf",
        "file:///tmp/model.gguf",
    ],
)
def test_artifact_file_is_only_a_safe_repository_relative_identity(
    selected_file: str,
) -> None:
    schema, golden = _documents()
    golden["catalog"]["artifacts"][0]["files"][0]["path"] = selected_file
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(golden)


def test_catalog_is_generic_across_the_three_mac_engines() -> None:
    schema, golden = _documents()
    validator = Draft202012Validator(schema)
    cases = (
        (
            "llama.cpp",
            "gguf",
            {
                "engine": "llama.cpp",
                "parallel_slots": 1,
                "gpu_offload": "automatic",
                "flash_attention": "automatic",
            },
        ),
        (
            "omlx",
            "mlx",
            {
                "engine": "omlx",
                "scheduler_slots": 1,
                "memory_guard": "required",
            },
        ),
        (
            "ds4",
            "ds4-weights",
            {
                "engine": "ds4",
                "batched_sessions": 1,
                "execution_mode": "single-node",
            },
        ),
    )
    for engine, artifact_format, launch in cases:
        candidate = copy.deepcopy(golden)
        recipe = candidate["catalog"]["recipes"][0]
        recipe["engine"] = engine
        recipe["runtime"]["engine"] = engine
        recipe["launch"] = launch
        candidate["catalog"]["artifacts"][0]["format"] = artifact_format
        validator.validate(candidate)


def test_fixture_uses_only_a_public_test_key_and_no_live_model_claims() -> None:
    _schema, golden = _documents()
    keys = json.loads((PROTOCOL_V1 / "test_keys.json").read_text())
    assert set(keys) == {"schema_version", "keys"}
    assert set(keys["keys"][0]) == {"key_id", "algorithm", "public_key"}
    assert "private" not in json.dumps(keys).lower()
    identities = json.dumps(golden["catalog"]).lower()
    assert "deepseek-v4" not in identities
    assert "glm-5.3" not in identities


def test_independent_consumer_implementations_are_byte_identical() -> None:
    canonical = (REPOSITORY_ROOT / "compatibility_catalog" / "catalog.py").read_bytes()
    fleet = (
        REPOSITORY_ROOT
        / "fleet"
        / "src"
        / "mnemosyne_fleet"
        / "compatibility_catalog.py"
    ).read_bytes()
    native = (
        REPOSITORY_ROOT
        / "macos"
        / "service"
        / "src"
        / "mnemosyne_macos"
        / "compatibility_catalog.py"
    ).read_bytes()
    assert fleet == canonical == native

    canonical_update = (
        REPOSITORY_ROOT / "compatibility_catalog" / "catalog_update.py"
    ).read_bytes()
    fleet_update = (
        REPOSITORY_ROOT
        / "fleet"
        / "src"
        / "mnemosyne_fleet"
        / "compatibility_catalog_update.py"
    ).read_bytes()
    native_update = (
        REPOSITORY_ROOT
        / "macos"
        / "service"
        / "src"
        / "mnemosyne_macos"
        / "compatibility_catalog_update.py"
    ).read_bytes()
    assert fleet_update == canonical_update == native_update


def test_wheel_manifests_embed_the_normative_schema() -> None:
    fleet = (REPOSITORY_ROOT / "fleet" / "pyproject.toml").read_text()
    native = (
        REPOSITORY_ROOT / "macos" / "service" / "pyproject.toml"
    ).read_text()
    assert "compatibility_catalog/v1/catalog.schema.json" in fleet
    assert "compatibility_catalog/v1/catalog.schema.json" in native
