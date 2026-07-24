from __future__ import annotations

import json
from pathlib import Path

import pytest

from mnemosyne_macos.local_models import LocalModelError, scan_local_models


def _gguf(path: Path, payload: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + payload)
    return path


def test_scan_local_models_groups_shards_and_offers_nearby_projectors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    directory = root / "publisher" / "vision-model"
    first = _gguf(directory / "vision-Q4_K_M-00001-of-00002.gguf", b"first")
    second = _gguf(directory / "vision-Q4_K_M-00002-of-00002.gguf", b"second")
    projector = _gguf(directory / "mmproj-vision-f16.gguf", b"projector")
    (directory / "mmproj-invalid.gguf").write_bytes(b"NOPE")

    models = scan_local_models(root)

    assert len(models) == 1
    model = models[0]
    assert model.engine == "llama.cpp"
    assert model.source_key == "publisher/vision-model"
    assert model.model_path == str(first.resolve())
    assert model.all_paths == (str(first.resolve()), str(second.resolve()))
    assert model.shard_count == 2
    assert model.quantization == "Q4_K_M"
    assert model.size_bytes == first.stat().st_size + second.stat().st_size
    assert model.compatibility == "structural"
    assert [item.path for item in model.projector_options] == [
        str(projector.resolve())
    ]


def test_scan_local_models_marks_incomplete_shards_and_bad_headers_unavailable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    directory = root / "publisher" / "broken"
    _gguf(directory / "incomplete-Q5_K_M-00001-of-00002.gguf")
    bad = directory / "bad-Q8_0.gguf"
    bad.write_bytes(b"not a GGUF")

    by_name = {
        Path(model.model_path).name: model for model in scan_local_models(root)
    }

    incomplete = by_name["incomplete-Q5_K_M-00001-of-00002.gguf"]
    assert incomplete.compatibility == "unavailable"
    assert "found 1 of 2" in incomplete.compatibility_reason
    invalid = by_name["bad-Q8_0.gguf"]
    assert invalid.compatibility == "unavailable"
    assert "valid GGUF header" in invalid.compatibility_reason


def test_scan_local_models_finds_mlx_weight_directories_without_loading(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    directory = root / "mlx-community" / "GLM-5-4bit"
    directory.mkdir(parents=True)
    config = directory / "config.json"
    config.write_text(
        '{"model_type":"glm","architectures":["GlmForCausalLM"]}',
        encoding="utf-8",
    )
    first = directory / "model-00001-of-00002.safetensors"
    second = directory / "model-00002-of-00002.safetensors"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    (directory / "tokenizer.json").write_text("{}", encoding="utf-8")

    models = scan_local_models(root)

    assert len(models) == 1
    model = models[0]
    assert model.engine == "omlx"
    assert model.source_key == "mlx-community/GLM-5-4bit"
    assert model.model_path == str(directory.resolve())
    assert model.all_paths == (
        str(first),
        str(second),
        str(config),
    )
    assert model.shard_count == 2
    assert model.size_bytes == 6
    assert model.compatibility == "likely"
    assert model.capabilities == (
        "chat/completions",
        "completions",
        "responses",
        "messages",
    )
    assert "embeddings" not in model.capabilities
    assert "rerank" not in model.capabilities


@pytest.mark.parametrize(
    ("directory_name", "config", "expected"),
    [
        (
            "bge-m3",
            {
                "model_type": "xlm-roberta",
                "architectures": ["XLMRobertaModel"],
            },
            ("embeddings",),
        ),
        (
            "qwen3-reranker-4bit",
            {
                "model_type": "qwen3",
                "architectures": ["Qwen3ForCausalLM"],
            },
            ("rerank",),
        ),
    ],
)
def test_scan_local_models_assigns_only_detected_omlx_capabilities(
    tmp_path: Path,
    directory_name: str,
    config: dict,
    expected: tuple[str, ...],
) -> None:
    root = tmp_path / "Models"
    directory = root / "mlx-community" / directory_name
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    (directory / "model.safetensors").write_bytes(b"weights")

    model = scan_local_models(root)[0]

    assert model.compatibility == "likely"
    assert model.capabilities == expected


def test_scan_local_models_marks_ambiguous_omlx_metadata_unavailable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    directory = root / "mlx-community" / "mystery-4bit"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(
        '{"model_type":"mystery"}', encoding="utf-8"
    )
    (directory / "model.safetensors").write_bytes(b"weights")

    model = scan_local_models(root)[0]

    assert model.compatibility == "unavailable"
    assert model.capabilities == ()
    assert "does not unambiguously identify" in model.compatibility_reason


def test_scan_local_models_marks_duplicate_omlx_leaf_ids_unavailable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    for owner in ("owner-one", "owner-two"):
        directory = root / owner / "same-model"
        directory.mkdir(parents=True)
        (directory / "config.json").write_text(
            '{"architectures":["ExampleForCausalLM"]}', encoding="utf-8"
        )
        (directory / "model.safetensors").write_bytes(owner.encode())

    models = scan_local_models(root)

    assert len(models) == 2
    assert all(model.compatibility == "unavailable" for model in models)
    assert all(
        "Duplicate oMLX model ID 'same-model'" in model.compatibility_reason
        for model in models
    )


def test_scan_local_models_ignores_symlink_escapes_for_files_and_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Models"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    outside_gguf = _gguf(outside / "outside-Q4_K_M.gguf")
    (root / "linked.gguf").symlink_to(outside_gguf)

    outside_mlx = outside / "mlx-model"
    outside_mlx.mkdir()
    (outside_mlx / "config.json").write_text("{}", encoding="utf-8")
    outside_weight = outside_mlx / "model.safetensors"
    outside_weight.write_bytes(b"outside")
    (root / "linked-directory").symlink_to(outside_mlx, target_is_directory=True)

    partial = root / "publisher" / "symlinked-weight"
    partial.mkdir(parents=True)
    (partial / "config.json").write_text("{}", encoding="utf-8")
    (partial / "model.safetensors").symlink_to(outside_weight)

    assert scan_local_models(root) == []


def test_scan_local_models_enforces_file_and_result_bounds(tmp_path: Path) -> None:
    root = tmp_path / "Models"
    _gguf(root / "one.gguf")
    _gguf(root / "two.gguf")

    with pytest.raises(LocalModelError, match="more than 1 files"):
        scan_local_models(root, max_files=1)
    with pytest.raises(LocalModelError, match="more than 1 models"):
        scan_local_models(root, max_models=1)


def test_scan_local_models_requires_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(LocalModelError, match="unavailable"):
        scan_local_models(tmp_path / "missing")
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("fixture", encoding="utf-8")
    with pytest.raises(LocalModelError, match="not a directory"):
        scan_local_models(file_path)
