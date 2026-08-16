from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from mnemosyne_macos.model_library import (
    download_size,
    gguf_files,
    image_profile_defaults,
    model_details,
    recommended_models,
    search_models,
    validate_install_candidate,
)
from mnemosyne_macos.models import EngineName


def test_ds4_and_mflux_only_offer_curated_artifacts() -> None:
    ds4 = recommended_models(EngineName.DS4)
    assert len(ds4) == 9
    assert {item.repo_id for item in ds4} == {
        "antirez/deepseek-v4-gguf",
        "antirez/GLM-5.2-GGUF",
        "unsloth/GLM-5.2-GGUF",
    }
    assert all(
        item.filename
        and item.download_files
        and item.compatibility == "verified"
        and item.suggested_role == "generation"
        for item in ds4
    )
    assert sum("GLM 5.2" in item.display_name for item in ds4) == 4
    assert sum("0731" in item.display_name for item in ds4) == 4
    assert all("DSpark" not in item.display_name for item in ds4)
    assert {item.family for item in ds4} == {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "glm-5.2",
    }


def test_ds4_search_verifies_exact_hub_files_sizes_and_revision(monkeypatch) -> None:
    catalog = recommended_models(EngineName.DS4)
    by_repo: dict[str, set[str]] = {}
    for model in catalog:
        by_repo.setdefault(model.repo_id, set()).update(model.download_files)
    seen: list[str] = []

    class FakeAPI:
        def __init__(self, token=None):
            assert token == "secret"

        def model_info(self, repo_id, **kwargs):
            seen.append(repo_id)
            assert kwargs == {"files_metadata": True}
            return SimpleNamespace(
                sha=f"resolved-{len(seen)}",
                downloads=123,
                likes=7,
                siblings=[
                    SimpleNamespace(rfilename=filename, size=100 + index)
                    for index, filename in enumerate(sorted(by_repo[repo_id]))
                ],
            )

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    results = search_models("", engine=EngineName.DS4, token="secret")

    assert len(results) == 9
    assert seen == [
        "antirez/deepseek-v4-gguf",
        "unsloth/GLM-5.2-GGUF",
        "antirez/GLM-5.2-GGUF",
    ]
    assert all(item.installable and item.resolved_revision for item in results)
    assert all(item.downloads == 123 and item.likes == 7 for item in results)
    sharded = next(item for item in results if "11 shards" in item.display_name)
    assert len(sharded.download_files) == 11
    assert sharded.size_bytes == sum(range(100, 111))
    assert "verified 11 exact files" in sharded.compatibility_reason


def test_ds4_search_marks_a_stale_declared_file_unavailable(monkeypatch) -> None:
    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, repo_id, **kwargs):
            assert repo_id == "antirez/GLM-5.2-GGUF"
            return SimpleNamespace(
                sha="resolved",
                siblings=[],
                downloads=0,
                likes=0,
            )

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    results = search_models("routed q4", engine=EngineName.DS4)

    assert len(results) == 1
    assert results[0].installable is False
    assert results[0].compatibility == "unavailable"
    assert "Update DS4" in results[0].compatibility_reason


def test_ds4_search_requires_a_managed_runtime_that_declares_the_target(
    monkeypatch, tmp_path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "download_model.sh").write_text(
        'REPO="antirez/deepseek-v4-gguf"\n'
        'OLD_FILE="DeepSeek-V4-Flash-old.gguf"\n',
        encoding="utf-8",
    )
    candidate = next(
        item
        for item in recommended_models(EngineName.DS4)
        if item.quantization == "MXFP4"
    )

    class FakeRuntime:
        def path(self, key):
            assert key == "working_directory"
            return source

    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, repo_id, **kwargs):
            return SimpleNamespace(
                sha="resolved",
                siblings=[
                    SimpleNamespace(rfilename=candidate.filename, size=123)
                ],
            )

    monkeypatch.setattr(
        "mnemosyne_macos.model_library.resolve_active_runtime",
        lambda engine: FakeRuntime() if engine == "ds4" else None,
    )
    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)

    results = search_models("native mxfp4", engine=EngineName.DS4)

    assert len(results) == 1
    assert results[0].installable is False
    assert "installed managed DS4 runtime" in results[0].compatibility_reason
    with pytest.raises(ValueError, match="update DS4 before downloading"):
        validate_install_candidate(
            engine=EngineName.DS4,
            repo_id=candidate.repo_id,
            filename=candidate.filename,
        )


def test_ds4_sharded_install_pins_all_exact_files(monkeypatch) -> None:
    sharded = next(
        item
        for item in recommended_models(EngineName.DS4)
        if item.repo_id == "unsloth/GLM-5.2-GGUF"
    )

    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, repo_id, **kwargs):
            assert repo_id == sharded.repo_id
            assert kwargs == {"revision": "main", "files_metadata": True}
            return SimpleNamespace(
                sha="c" * 40,
                siblings=[
                    SimpleNamespace(rfilename=filename, size=100)
                    for filename in sharded.download_files
                ],
            )

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    candidate = validate_install_candidate(
        engine=EngineName.DS4,
        repo_id=sharded.repo_id,
        filename=sharded.filename,
        revision="main",
    )

    assert candidate.resolved_revision == "c" * 40
    assert candidate.download_files == sharded.download_files
    assert candidate.size_bytes == 1_100


def test_verified_install_candidate_pins_resolved_hub_revision(monkeypatch) -> None:
    seen: dict = {}

    class FakeAPI:
        def __init__(self, token=None):
            seen["token"] = token

        def model_info(self, repo_id, **kwargs):
            seen["repo_id"] = repo_id
            seen.update(kwargs)
            return SimpleNamespace(sha="a" * 40)

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    candidate = validate_install_candidate(
        engine=EngineName.MFLUX,
        repo_id="black-forest-labs/FLUX.1-schnell",
        filename=None,
        revision="main",
    )

    assert candidate.resolved_revision == "a" * 40
    assert seen["repo_id"] == "black-forest-labs/FLUX.1-schnell"
    assert seen["revision"] == "main"
    assert seen["files_metadata"] is True


def test_active_mflux_pack_can_extend_curated_catalog(monkeypatch, tmp_path) -> None:
    root = tmp_path / "runtimes"
    runtime = root / "mflux" / "0.20.0"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "worker" / "mnemosyne_mflux_worker").mkdir(parents=True)
    python = runtime / "bin" / "python3"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    (runtime / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "mflux",
                "version": "0.20.0",
                "source_revision": "abc123",
                "core_protocol": 1,
                "entrypoint": {
                    "python": "bin/python3",
                    "worker_path": "worker",
                },
                "capabilities": [
                    {
                        "repo_id": "example/New-Image",
                        "display_name": "New Image",
                        "family": "new-image",
                        "default_quantize": 8,
                        "default_num_inference_steps": 6,
                        "default_guidance_scale": 1.5,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "mflux" / "current.json").write_text(
        json.dumps({"schema_version": 1, "version": "0.20.0"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MNEMOSYNE_RUNTIME_ROOT", str(root))

    models = recommended_models(EngineName.MFLUX)
    dynamic = next(item for item in models if item.repo_id == "example/New-Image")
    assert dynamic.family == "new-image"
    assert dynamic.default_num_inference_steps == 6
    assert dynamic.compatibility == "verified"

    images = recommended_models(EngineName.MFLUX)
    assert len(images) == 19
    assert {item.family for item in images if item.installable} == {
        "schnell",
        "dev",
        "krea-dev",
        "flux2-klein-4b",
        "flux2-klein-9b",
        "flux2-klein-9b-kv",
        "flux2-klein-base-4b",
        "flux2-klein-base-9b",
        "qwen-image",
        "krea-2",
        "fibo",
        "fibo-lite",
        "z-image",
        "z-image-turbo",
        "ernie-image",
        "ernie-image-turbo",
        "ideogram-4-fp8",
        "new-image",
    }
    raw = next(item for item in images if item.repo_id == "krea/Krea-2-Raw")
    assert raw.installable is False
    assert raw.compatibility == "unavailable"

    turbo = next(item for item in images if item.repo_id == "krea/Krea-2-Turbo")
    assert turbo.default_num_inference_steps == 8
    assert turbo.default_guidance_scale == 1.0
    assert image_profile_defaults(turbo) == {
        "family": "krea-2",
        "quantize": 8,
        "width": 1024,
        "height": 1024,
        "num_inference_steps": 8,
        "guidance_scale": 1.0,
    }

    with pytest.raises(ValueError, match="turbo.safetensors"):
        validate_install_candidate(
            engine=EngineName.MFLUX,
            repo_id="krea/Krea-2-Raw",
            filename=None,
        )

    with pytest.raises(ValueError, match="limited to verified"):
        validate_install_candidate(
            engine=EngineName.MFLUX,
            repo_id="someone/random-diffusion-model",
            filename=None,
        )


def test_omlx_search_filters_adapters_and_reports_compatibility(monkeypatch) -> None:
    seen: dict = {}

    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def list_models(self, **kwargs):
            seen.update(kwargs)
            return [
                SimpleNamespace(
                    id="mlx-community/GLM-5.2-4bit",
                    tags=["mlx", "transformers", "4bit"],
                    pipeline_tag="text-generation",
                    downloads=123,
                    likes=9,
                    usedStorage=456,
                ),
                SimpleNamespace(
                    id="someone/adapter-mlx",
                    tags=["mlx", "adapter"],
                    downloads=1,
                    likes=0,
                    usedStorage=10,
                ),
            ]

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    results = search_models("glm", engine=EngineName.OMLX)

    assert [item.repo_id for item in results] == ["mlx-community/GLM-5.2-4bit"]
    assert results[0].compatibility == "likely"
    assert results[0].quantization == "4bit"
    assert results[0].suggested_role == "generation"
    assert results[0].size_bytes == 456
    assert seen == {
        "search": "glm",
        "filter": "mlx",
        "sort": "downloads",
        "limit": 20,
        "full": True,
    }


def test_omlx_install_candidate_retains_resolved_hub_revision(monkeypatch) -> None:
    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, *_args, **_kwargs):
            return SimpleNamespace(
                sha="b" * 40,
                tags=["mlx", "text-generation"],
                pipeline_tag="text-generation",
            )

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    candidate = validate_install_candidate(
        engine=EngineName.OMLX,
        repo_id="mlx-community/Model-4bit",
        filename=None,
        revision="main",
    )

    assert candidate.resolved_revision == "b" * 40


def test_mlxcel_search_keeps_mlx_candidates_engine_scoped(monkeypatch) -> None:
    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def list_models(self, **kwargs):
            assert kwargs["filter"] == "mlx"
            return [
                SimpleNamespace(
                    id="mlx-community/Qwen-4bit",
                    tags=["mlx", "4bit"],
                    pipeline_tag="text-generation",
                    downloads=10,
                    likes=2,
                    usedStorage=123,
                )
            ]

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    results = search_models("qwen", engine=EngineName.MLXCEL)

    assert len(results) == 1
    assert results[0].engine == "mlxcel"
    assert results[0].suggested_role == "generation"
    assert "Final architecture compatibility" in results[0].compatibility_reason


def test_mistral_rs_install_requires_pinned_safetensors_snapshot(monkeypatch) -> None:
    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, repo_id, **kwargs):
            assert repo_id == "owner/model"
            assert kwargs == {"revision": "main", "files_metadata": True}
            return SimpleNamespace(
                sha="c" * 40,
                tags=["text-generation"],
                siblings=[
                    SimpleNamespace(rfilename="config.json", size=100),
                    SimpleNamespace(rfilename="model.safetensors", size=1_000),
                ],
            )

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    candidate = validate_install_candidate(
        engine=EngineName.MISTRAL_RS,
        repo_id="owner/model",
        filename=None,
        revision="main",
    )

    assert candidate.resolved_revision == "c" * 40
    assert candidate.engine == "mistral.rs"
    assert candidate.suggested_role == "generation"


def test_mistral_rs_rejects_repositories_without_safetensors(monkeypatch) -> None:
    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, *_args, **_kwargs):
            return SimpleNamespace(
                sha="d" * 40,
                tags=["text-generation"],
                siblings=[SimpleNamespace(rfilename="config.json", size=100)],
            )

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    with pytest.raises(ValueError, match="Safetensors weights"):
        validate_install_candidate(
            engine=EngineName.MISTRAL_RS,
            repo_id="owner/model",
            filename=None,
        )


def test_download_size_uses_exact_file_or_complete_snapshot(monkeypatch) -> None:
    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, *_args, **_kwargs):
            return SimpleNamespace(
                siblings=[
                    SimpleNamespace(rfilename="model.gguf", size=100),
                    SimpleNamespace(rfilename="config.json", size=20),
                    SimpleNamespace(rfilename="unknown", size=None),
                ]
            )

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)

    assert download_size("owner/repo") == 120
    assert download_size("owner/repo", filename="model.gguf") == 100


def test_model_details_combines_card_config_and_hub_metadata(monkeypatch) -> None:
    class FakeCard(dict):
        pass

    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, repo_id, **kwargs):
            assert repo_id == "owner/model"
            assert kwargs["revision"] == "main"
            assert "cardData" in kwargs["expand"]
            assert "config" in kwargs["expand"]
            return SimpleNamespace(
                sha="resolved-commit",
                tags=["gguf", "license:apache-2.0"],
                config={
                    "architectures": ["Qwen2ForCausalLM"],
                    "max_position_embeddings": 131_072,
                },
                gguf={"total": 7_615_000_000},
                card_data=FakeCard(license="apache-2.0"),
                pipeline_tag="text-generation",
                last_modified="2026-07-25T12:00:00Z",
            )

    class FakeFilesystem:
        def __init__(self, token=None):
            self.token = token

        def open(self, path, mode):
            assert mode == "rb"
            assert path == "owner/model@resolved-commit/README.md"
            return io.BytesIO(
                b"# Model\n\nA long-context model for local inference."
            )

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    monkeypatch.setattr(
        "mnemosyne_macos.model_library.HfFileSystem",
        FakeFilesystem,
    )

    details = model_details(
        "owner/model",
        engine=EngineName.OMLX,
        revision="main",
    )

    assert details.resolved_revision == "resolved-commit"
    assert details.architecture == "Qwen2ForCausalLM"
    assert details.context_length == 131_072
    assert details.parameter_count == 7_615_000_000
    assert details.summary == "A long-context model for local inference."
    assert details.model_card_markdown == (
        "# Model\n\nA long-context model for local inference."
    )
    assert details.license == "apache-2.0"


def test_llama_cpp_search_requires_exact_gguf_file_selection(monkeypatch) -> None:
    seen: dict = {}

    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def list_models(self, **kwargs):
            seen.update(kwargs)
            return [
                SimpleNamespace(
                    id="bartowski/Qwen2.5-VL-GGUF",
                    tags=["gguf", "text-generation"],
                    pipeline_tag="text-generation",
                    downloads=500,
                    likes=20,
                    usedStorage=1234,
                ),
                SimpleNamespace(
                    id="someone/vision-lora-GGUF",
                    tags=["gguf", "lora"],
                    downloads=1,
                    likes=0,
                    usedStorage=10,
                ),
            ]

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)

    results = search_models("qwen", engine=EngineName.LLAMA_CPP)

    assert [item.repo_id for item in results] == ["bartowski/Qwen2.5-VL-GGUF"]
    assert results[0].requires_file_selection is True
    assert results[0].installable is False
    assert results[0].compatibility == "select"
    assert results[0].suggested_role == "generation"
    assert seen == {
        "search": "qwen",
        "filter": "gguf",
        "sort": "downloads",
        "limit": 20,
        "full": True,
    }


def test_gguf_file_discovery_suggests_embeddings_from_hub_metadata(
    monkeypatch,
) -> None:
    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, *_args, **_kwargs):
            return SimpleNamespace(
                sha="embedding-commit",
                pipeline_tag="feature-extraction",
                tags=["gguf", "sentence-transformers"],
                siblings=[
                    SimpleNamespace(
                        rfilename="embeddinggemma-300M-Q8_0.gguf",
                        size=333_590_944,
                    )
                ],
            )

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)

    candidates = gguf_files("ggml-org/embeddinggemma-300M-GGUF")

    assert len(candidates) == 1
    assert candidates[0].suggested_role == "embeddings"


def test_gguf_file_discovery_expands_shards_and_scopes_projectors(
    monkeypatch,
) -> None:
    siblings = [
        SimpleNamespace(
            rfilename="vision-Q4_K_M-00001-of-00002.gguf",
            size=100,
        ),
        SimpleNamespace(
            rfilename="vision-Q4_K_M-00002-of-00002.gguf",
            size=110,
        ),
        SimpleNamespace(rfilename="mmproj-vision-f16.gguf", size=20),
        SimpleNamespace(rfilename="nested/text-Q8_0.gguf", size=300),
        SimpleNamespace(rfilename="nested/mmproj-text-f16.gguf", size=40),
        SimpleNamespace(
            rfilename="incomplete-Q5_K_M-00001-of-00002.gguf",
            size=50,
        ),
        SimpleNamespace(rfilename="README.md", size=5),
    ]

    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, repo_id, **kwargs):
            assert repo_id == "owner/vision-GGUF"
            assert kwargs == {"revision": "main", "files_metadata": True}
            return SimpleNamespace(sha="resolved-commit", siblings=siblings)

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)

    candidates = gguf_files("owner/vision-GGUF", revision="main")
    assert [item.filename for item in candidates] == [
        "nested/text-Q8_0.gguf",
        "vision-Q4_K_M-00001-of-00002.gguf",
    ]

    nested, sharded = candidates
    assert nested.download_files == ("nested/text-Q8_0.gguf",)
    assert nested.projector_options == ("nested/mmproj-text-f16.gguf",)
    assert nested.projector_filename == "nested/mmproj-text-f16.gguf"
    assert nested.quantization == "Q8_0"
    assert nested.size_bytes == 300
    assert nested.resolved_revision == "resolved-commit"

    assert sharded.download_files == (
        "vision-Q4_K_M-00001-of-00002.gguf",
        "vision-Q4_K_M-00002-of-00002.gguf",
    )
    assert sharded.projector_options == ("mmproj-vision-f16.gguf",)
    assert sharded.projector_filename == "mmproj-vision-f16.gguf"
    assert sharded.quantization == "Q4_K_M"
    assert sharded.size_bytes == 210
    assert all("incomplete" not in (item.filename or "") for item in candidates)


def test_validate_llama_cpp_candidate_adds_only_the_selected_projector(
    monkeypatch,
) -> None:
    siblings = [
        SimpleNamespace(rfilename="vision-Q4_K_M.gguf", size=100),
        SimpleNamespace(rfilename="mmproj-vision-f16.gguf", size=20),
        SimpleNamespace(rfilename="mmproj-vision-Q8_0.gguf", size=15),
        SimpleNamespace(rfilename="nested/mmproj-other-f16.gguf", size=30),
    ]

    class FakeAPI:
        def __init__(self, token=None):
            self.token = token

        def model_info(self, *_args, **_kwargs):
            return SimpleNamespace(sha="commit", siblings=siblings)

    monkeypatch.setattr("mnemosyne_macos.model_library.HfApi", FakeAPI)
    monkeypatch.setattr(
        "mnemosyne_macos.model_library.model_details",
        lambda *_args, **_kwargs: SimpleNamespace(
            architecture="qwen2vl",
            context_length=32_768,
            parameter_count=7_000_000_000,
        ),
    )

    selected = validate_install_candidate(
        engine=EngineName.LLAMA_CPP,
        repo_id="owner/vision-GGUF",
        filename="vision-Q4_K_M.gguf",
        projector_filename="mmproj-vision-Q8_0.gguf",
        revision="main",
    )
    assert selected.projector_filename == "mmproj-vision-Q8_0.gguf"
    assert selected.download_files == (
        "vision-Q4_K_M.gguf",
        "mmproj-vision-Q8_0.gguf",
    )
    assert selected.resolved_revision == "commit"
    assert selected.context_length == 32_768

    with pytest.raises(ValueError, match="not published beside"):
        validate_install_candidate(
            engine=EngineName.LLAMA_CPP,
            repo_id="owner/vision-GGUF",
            filename="vision-Q4_K_M.gguf",
            projector_filename="nested/mmproj-other-f16.gguf",
        )

    with pytest.raises(ValueError, match="select an exact GGUF quant"):
        validate_install_candidate(
            engine=EngineName.LLAMA_CPP,
            repo_id="owner/vision-GGUF",
            filename=None,
        )

    text_only = validate_install_candidate(
        engine=EngineName.LLAMA_CPP,
        repo_id="owner/vision-GGUF",
        filename="vision-Q4_K_M.gguf",
        include_projector=False,
    )
    assert text_only.projector_filename is None
    assert text_only.download_files == ("vision-Q4_K_M.gguf",)
