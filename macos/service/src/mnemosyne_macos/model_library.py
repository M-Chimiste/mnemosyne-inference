"""Hugging Face discovery with conservative, engine-specific compatibility."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import re
from typing import Any, Iterable

from huggingface_hub import HfApi

from .models import EngineName
from .runtime_updates import resolve_active_runtime


@dataclass(frozen=True, slots=True)
class LibraryModel:
    repo_id: str
    engine: str
    display_name: str
    model_kind: str
    compatibility: str
    compatibility_reason: str
    downloads: int | None = None
    likes: int | None = None
    size_bytes: int | None = None
    quantization: str | None = None
    filename: str | None = None
    projector_filename: str | None = None
    projector_options: tuple[str, ...] = ()
    download_files: tuple[str, ...] = ()
    resolved_revision: str | None = None
    requires_file_selection: bool = False
    family: str | None = None
    recommended_memory_gb: int | None = None
    installable: bool = True
    suggested_role: str | None = None
    default_quantize: int | None = None
    default_width: int | None = None
    default_height: int | None = None
    default_num_inference_steps: int | None = None
    default_guidance_scale: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_DS4_REPO = "antirez/deepseek-v4-gguf"
_DS4_VARIANTS: tuple[LibraryModel, ...] = (
    LibraryModel(
        repo_id=_DS4_REPO,
        engine=EngineName.DS4.value,
        display_name="DeepSeek V4 Flash — Q2 imatrix",
        model_kind="language",
        compatibility="verified",
        compatibility_reason="Exact DS4 project weight; recommended for 96/128 GB Macs.",
        quantization="IQ2_XXS / Q2_K",
        filename="DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix.gguf",
        recommended_memory_gb=96,
    ),
    LibraryModel(
        repo_id=_DS4_REPO,
        engine=EngineName.DS4.value,
        display_name="DeepSeek V4 Flash — mixed Q2/Q4 imatrix",
        model_kind="language",
        compatibility="verified",
        compatibility_reason="Exact DS4 project weight with the final six expert layers at Q4.",
        quantization="mixed Q2/Q4",
        filename="DeepSeek-V4-Flash-Layers37-42Q4KExperts-OtherExpertLayersIQ2XXSGateUp-Q2KDown-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-fixed.gguf",
        recommended_memory_gb=96,
    ),
    LibraryModel(
        repo_id=_DS4_REPO,
        engine=EngineName.DS4.value,
        display_name="DeepSeek V4 Flash — Q4 imatrix",
        model_kind="language",
        compatibility="verified",
        compatibility_reason="Exact DS4 project weight; intended for Macs with at least 256 GB memory.",
        quantization="Q4_K",
        filename="DeepSeek-V4-Flash-Q4KExperts-F16HC-F16Compressor-F16Indexer-Q8Attn-Q8Shared-Q8Out-chat-v2-imatrix.gguf",
        recommended_memory_gb=256,
    ),
    LibraryModel(
        repo_id=_DS4_REPO,
        engine=EngineName.DS4.value,
        display_name="DeepSeek V4 Pro — Q2 imatrix",
        model_kind="language",
        compatibility="verified",
        compatibility_reason="Exact DS4 project weight; intended for 512 GB Macs.",
        quantization="IQ2_XXS / Q2_K",
        filename="DeepSeek-V4-Pro-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-Instruct-imatrix.gguf",
        recommended_memory_gb=512,
    ),
)


def _mflux_model(
    repo_id: str,
    display_name: str,
    family: str,
    *,
    steps: int,
    guidance: float,
    quantize: int | None = 8,
    width: int = 1024,
    height: int = 1024,
    compatibility: str = "supported",
    reason: str = "Exact text-to-image configuration supported by the bundled MFLUX worker.",
) -> LibraryModel:
    return LibraryModel(
        repo_id=repo_id,
        engine=EngineName.MFLUX.value,
        display_name=display_name,
        model_kind="image",
        compatibility=compatibility,
        compatibility_reason=reason,
        family=family,
        suggested_role="image",
        default_quantize=quantize,
        default_width=width,
        default_height=height,
        default_num_inference_steps=steps,
        default_guidance_scale=guidance,
    )


# This list intentionally mirrors only the text-to-image ModelConfig entries in
# the pinned MFLUX runtime. Edit, fill, depth, ControlNet, Redux, and restoration
# models require request shapes that /v1/images/generations does not expose.
_MFLUX_MODELS: tuple[LibraryModel, ...] = (
    _mflux_model(
        "black-forest-labs/FLUX.1-schnell",
        "FLUX.1 Schnell",
        "schnell",
        steps=4,
        guidance=0.0,
    ),
    _mflux_model(
        "black-forest-labs/FLUX.1-dev",
        "FLUX.1 Dev",
        "dev",
        steps=25,
        guidance=3.5,
    ),
    _mflux_model(
        "black-forest-labs/FLUX.1-Krea-dev",
        "FLUX.1 Krea Dev",
        "krea-dev",
        steps=25,
        guidance=3.5,
    ),
    _mflux_model(
        "black-forest-labs/FLUX.2-klein-4B",
        "FLUX.2 Klein 4B",
        "flux2-klein-4b",
        steps=4,
        guidance=1.0,
    ),
    _mflux_model(
        "black-forest-labs/FLUX.2-klein-9B",
        "FLUX.2 Klein 9B",
        "flux2-klein-9b",
        steps=4,
        guidance=1.0,
    ),
    _mflux_model(
        "black-forest-labs/FLUX.2-klein-9b-kv",
        "FLUX.2 Klein 9B KV",
        "flux2-klein-9b-kv",
        steps=4,
        guidance=1.0,
    ),
    _mflux_model(
        "black-forest-labs/FLUX.2-klein-base-4B",
        "FLUX.2 Klein Base 4B",
        "flux2-klein-base-4b",
        steps=50,
        guidance=4.0,
    ),
    _mflux_model(
        "black-forest-labs/FLUX.2-klein-base-9B",
        "FLUX.2 Klein Base 9B",
        "flux2-klein-base-9b",
        steps=50,
        guidance=4.0,
    ),
    _mflux_model(
        "Qwen/Qwen-Image",
        "Qwen Image",
        "qwen-image",
        steps=20,
        guidance=4.0,
        compatibility="verified",
        reason="Explicit text-to-image model in the bundled MFLUX worker.",
    ),
    _mflux_model(
        "krea/Krea-2-Turbo",
        "Krea 2 Turbo",
        "krea-2",
        steps=8,
        guidance=1.0,
        compatibility="verified",
        reason="Explicitly supported and smoke-tested with the bundled MFLUX worker.",
    ),
    LibraryModel(
        repo_id="krea/Krea-2-Raw",
        engine=EngineName.MFLUX.value,
        display_name="Krea 2 Raw",
        model_kind="image",
        compatibility="unavailable",
        compatibility_reason=(
            "Krea publishes this second checkpoint, but the pinned MFLUX loader currently "
            "accepts only Krea 2 Turbo's turbo.safetensors layout."
        ),
        installable=False,
        default_width=1024,
        default_height=1024,
        default_num_inference_steps=52,
        default_guidance_scale=3.5,
    ),
    _mflux_model(
        "briaai/FIBO",
        "FIBO",
        "fibo",
        steps=50,
        guidance=4.0,
    ),
    _mflux_model(
        "briaai/Fibo-lite",
        "FIBO Lite",
        "fibo-lite",
        steps=8,
        guidance=1.0,
    ),
    _mflux_model(
        "Tongyi-MAI/Z-Image",
        "Z-Image",
        "z-image",
        steps=50,
        guidance=3.5,
    ),
    _mflux_model(
        "Tongyi-MAI/Z-Image-Turbo",
        "Z-Image Turbo",
        "z-image-turbo",
        steps=9,
        guidance=0.0,
    ),
    _mflux_model(
        "baidu/ERNIE-Image",
        "ERNIE Image",
        "ernie-image",
        steps=50,
        guidance=4.0,
    ),
    _mflux_model(
        "baidu/ERNIE-Image-Turbo",
        "ERNIE Image Turbo",
        "ernie-image-turbo",
        steps=8,
        guidance=1.0,
    ),
    _mflux_model(
        "ideogram-ai/ideogram-4-fp8",
        "Ideogram 4 FP8",
        "ideogram-4-fp8",
        steps=20,
        guidance=7.0,
        quantize=None,
    ),
)


def _managed_mflux_models() -> list[LibraryModel]:
    """Read text-to-image capabilities shipped with the managed runtime."""

    runtime = resolve_active_runtime("mflux")
    if runtime is None:
        return []
    models: list[LibraryModel] = []
    for item in runtime.capabilities:
        if item.get("kind", "text-to-image") != "text-to-image":
            continue
        repo_id = item.get("repo_id")
        display_name = item.get("display_name")
        family = item.get("family")
        steps = item.get("default_num_inference_steps")
        guidance = item.get("default_guidance_scale")
        if not (
            isinstance(repo_id, str)
            and "/" in repo_id
            and isinstance(display_name, str)
            and display_name
            and isinstance(family, str)
            and family
            and isinstance(steps, int)
            and not isinstance(steps, bool)
            and 1 <= steps <= 200
            and isinstance(guidance, (int, float))
            and not isinstance(guidance, bool)
            and 0 <= float(guidance) <= 50
        ):
            continue
        quantize = item.get("default_quantize", 8)
        if quantize not in {None, 3, 4, 5, 6, 8}:
            continue
        width = item.get("default_width", 1024)
        height = item.get("default_height", 1024)
        if not all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and 64 <= value <= 4096
            and value % 16 == 0
            for value in (width, height)
        ):
            continue
        models.append(
            _mflux_model(
                repo_id,
                display_name,
                family,
                steps=steps,
                guidance=float(guidance),
                quantize=quantize,
                width=width,
                height=height,
                compatibility="verified",
                reason=(
                    f"Declared by the unified worker for managed MFLUX runtime {runtime.version}."
                ),
            )
        )
    return models


def recommended_models(engine: EngineName | None = None) -> list[LibraryModel]:
    models = list((*_DS4_VARIANTS, *_MFLUX_MODELS))
    for managed in _managed_mflux_models():
        models = [
            item
            for item in models
            if not (
                item.engine == managed.engine
                and item.repo_id == managed.repo_id
                and item.filename == managed.filename
            )
        ]
        models.append(managed)
    if engine is None:
        return models
    return [model for model in models if model.engine == engine.value]


def verified_model(
    *, engine: EngineName, repo_id: str, filename: str | None = None
) -> LibraryModel | None:
    for model in recommended_models(engine):
        if (
            model.repo_id == repo_id
            and model.filename == filename
            and model.installable
        ):
            return model
    return None


def image_profile_defaults(model: LibraryModel) -> dict[str, Any]:
    """Return the validated profile defaults attached to a curated image model."""

    if model.engine != EngineName.MFLUX.value or not model.family or not model.installable:
        raise ValueError("model does not describe an installable MFLUX image profile")
    return {
        "family": model.family,
        "quantize": model.default_quantize,
        "width": model.default_width or 1024,
        "height": model.default_height or 1024,
        "num_inference_steps": model.default_num_inference_steps or 30,
        "guidance_scale": (
            model.default_guidance_scale
            if model.default_guidance_scale is not None
            else 4.0
        ),
    }


def search_models(
    query: str,
    *,
    engine: EngineName,
    limit: int = 20,
    token: str | None = None,
) -> list[LibraryModel]:
    """Search only the portion of the Hub that the selected engine can use."""

    normalized = query.strip().casefold()
    if engine == EngineName.LMSTUDIO:
        return []
    if engine == EngineName.LLAMA_CPP:
        api = HfApi(token=token or _hf_token())
        raw_models: Iterable[Any] = api.list_models(
            search=query.strip() or None,
            filter="gguf",
            sort="downloads",
            limit=limit,
            full=True,
        )
        results: list[LibraryModel] = []
        for raw in raw_models:
            repo_id = getattr(raw, "id", None) or getattr(raw, "modelId", None)
            if not isinstance(repo_id, str) or "/" not in repo_id:
                continue
            tags = {str(tag).casefold() for tag in (getattr(raw, "tags", None) or [])}
            if {"adapter", "lora", "peft"} & tags:
                continue
            results.append(
                LibraryModel(
                    repo_id=repo_id,
                    engine=engine.value,
                    display_name=repo_id.rsplit("/", 1)[-1],
                    model_kind="language",
                    compatibility="select",
                    compatibility_reason=(
                        "Choose an exact GGUF quant and, for vision, a matching projector."
                    ),
                    downloads=_optional_int(getattr(raw, "downloads", None)),
                    likes=_optional_int(getattr(raw, "likes", None)),
                    size_bytes=_optional_int(getattr(raw, "usedStorage", None)),
                    installable=False,
                    requires_file_selection=True,
                    suggested_role=_suggested_role(
                        repo_id,
                        tags,
                        getattr(raw, "pipeline_tag", None),
                    ),
                )
            )
        return results
    if engine in {EngineName.DS4, EngineName.MFLUX}:
        return [
            model
            for model in recommended_models(engine)
            if not normalized
            or normalized in model.repo_id.casefold()
            or normalized in model.display_name.casefold()
        ][:limit]
    if engine != EngineName.OMLX:
        return []

    api = HfApi(token=token or _hf_token())
    raw_models: Iterable[Any] = api.list_models(
        search=query.strip() or None,
        filter="mlx",
        sort="downloads",
        limit=limit,
        full=True,
    )
    results: list[LibraryModel] = []
    for raw in raw_models:
        repo_id = getattr(raw, "id", None) or getattr(raw, "modelId", None)
        if not isinstance(repo_id, str) or "/" not in repo_id:
            continue
        tags = {str(tag).casefold() for tag in (getattr(raw, "tags", None) or [])}
        if {"adapter", "lora", "peft"} & tags:
            continue
        if "mlx" not in tags and "mlx" not in repo_id.casefold():
            continue
        results.append(
            LibraryModel(
                repo_id=repo_id,
                engine=engine.value,
                display_name=repo_id.rsplit("/", 1)[-1],
                model_kind="language",
                compatibility="likely",
                compatibility_reason=(
                    "Published as an MLX model. Final load compatibility is verified by oMLX."
                ),
                downloads=_optional_int(getattr(raw, "downloads", None)),
                likes=_optional_int(getattr(raw, "likes", None)),
                size_bytes=_optional_int(getattr(raw, "usedStorage", None)),
                quantization=_quantization(repo_id, tags),
                suggested_role=_suggested_role(
                    repo_id,
                    tags,
                    getattr(raw, "pipeline_tag", None),
                ),
            )
        )
    return results


def validate_install_candidate(
    *,
    engine: EngineName,
    repo_id: str,
    filename: str | None,
    projector_filename: str | None = None,
    revision: str | None = None,
    token: str | None = None,
) -> LibraryModel:
    verified = verified_model(engine=engine, repo_id=repo_id, filename=filename)
    if verified is not None:
        return verified
    unavailable = next(
        (
            model
            for model in recommended_models(engine)
            if model.repo_id == repo_id and model.filename == filename
        ),
        None,
    )
    if unavailable is not None and not unavailable.installable:
        raise ValueError(unavailable.compatibility_reason)
    if engine == EngineName.LLAMA_CPP:
        if not filename:
            raise ValueError("select an exact GGUF quant before downloading")
        candidates = gguf_files(
            repo_id,
            revision=revision,
            token=token,
        )
        candidate = next((item for item in candidates if item.filename == filename), None)
        if candidate is None:
            raise ValueError("the selected GGUF file is not an installable primary model")
        if projector_filename is not None:
            if projector_filename not in candidate.projector_options:
                raise ValueError(
                    "the selected projector is not published beside that GGUF model"
                )
            files = tuple((*candidate.download_files, projector_filename))
            candidate = LibraryModel(
                **{
                    **candidate.to_dict(),
                    "projector_filename": projector_filename,
                    "download_files": files,
                }
            )
        return candidate
    if engine != EngineName.OMLX:
        raise ValueError(f"{engine.value} downloads are limited to verified models")

    api = HfApi(token=token or _hf_token())
    info = api.model_info(repo_id, files_metadata=False)
    tags = {str(tag).casefold() for tag in (getattr(info, "tags", None) or [])}
    if "mlx" not in tags and "mlx" not in repo_id.casefold():
        raise ValueError("the selected repository is not published as an MLX model")
    if {"adapter", "lora", "peft"} & tags:
        raise ValueError("adapter-only repositories cannot be installed as standalone oMLX models")
    return LibraryModel(
        repo_id=repo_id,
        engine=engine.value,
        display_name=repo_id.rsplit("/", 1)[-1],
        model_kind="language",
        compatibility="likely",
        compatibility_reason="Published as an MLX model; oMLX performs final validation on load.",
        quantization=_quantization(repo_id, tags),
        suggested_role=_suggested_role(
            repo_id,
            tags,
            getattr(info, "pipeline_tag", None),
        ),
    )


def download_size(
    repo_id: str,
    *,
    filename: str | None = None,
    filenames: Iterable[str] | None = None,
    revision: str | None = None,
    token: str | None = None,
) -> int | None:
    """Return the Hub's file-metadata size for the exact planned download."""

    info = HfApi(token=token or _hf_token()).model_info(
        repo_id,
        revision=revision,
        files_metadata=True,
    )
    siblings = getattr(info, "siblings", None) or []
    selected = set(filenames or ())
    if filename is not None:
        selected.add(filename)
    sizes = [
        int(sibling.size)
        for sibling in siblings
        if isinstance(getattr(sibling, "size", None), int)
        and (not selected or getattr(sibling, "rfilename", None) in selected)
    ]
    return sum(sizes) if sizes else None


_SHARD_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<part>\d{5})-of-(?P<total>\d{5})\.gguf$",
    re.IGNORECASE,
)
_QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:UD-)?(?:IQ|Q|TQ|BF|F|FP)\d+(?:_[A-Za-z0-9]+)*)",
    re.IGNORECASE,
)


def _is_projector(filename: str) -> bool:
    basename = filename.rsplit("/", 1)[-1].casefold()
    return basename.startswith("mmproj") or "-mmproj" in basename


def _gguf_quantization(filename: str) -> str | None:
    stem = filename.rsplit("/", 1)[-1].removesuffix(".gguf")
    matches = _QUANT_RE.findall(stem)
    return matches[-1].upper() if matches else None


def gguf_files(
    repo_id: str,
    *,
    revision: str | None = None,
    token: str | None = None,
) -> list[LibraryModel]:
    """Return one installable row per primary GGUF quant/shard group."""

    info = HfApi(token=token or _hf_token()).model_info(
        repo_id,
        revision=revision,
        files_metadata=True,
    )
    resolved_revision = getattr(info, "sha", None)
    tags = {str(tag).casefold() for tag in (getattr(info, "tags", None) or [])}
    suggested_role = _suggested_role(
        repo_id,
        tags,
        getattr(info, "pipeline_tag", None),
    )
    siblings = getattr(info, "siblings", None) or []
    sizes = {
        str(sibling.rfilename): int(sibling.size)
        for sibling in siblings
        if isinstance(getattr(sibling, "rfilename", None), str)
        and isinstance(getattr(sibling, "size", None), int)
    }
    filenames = sorted(
        name for name in sizes if name.casefold().endswith(".gguf")
    )
    projectors = tuple(name for name in filenames if _is_projector(name))
    primaries = [name for name in filenames if not _is_projector(name)]
    results: list[LibraryModel] = []
    for filename in primaries:
        shard = _SHARD_RE.match(filename)
        if shard and int(shard.group("part")) != 1:
            continue
        if shard:
            prefix = shard.group("prefix")
            total = int(shard.group("total"))
            group = tuple(
                name
                for name in primaries
                if (match := _SHARD_RE.match(name))
                and match.group("prefix") == prefix
                and int(match.group("total")) == total
            )
            if len(group) != total:
                continue
        else:
            group = (filename,)
        directory = filename.rpartition("/")[0]
        nearby_projectors = tuple(
            value
            for value in projectors
            if value.rpartition("/")[0] == directory
        )
        # Pairing is always explicit. A nearby file named mmproj is a useful
        # option, but filename proximity alone is not proof of compatibility.
        download_files = group
        results.append(
            LibraryModel(
                repo_id=repo_id,
                engine=EngineName.LLAMA_CPP.value,
                display_name=filename.rsplit("/", 1)[-1],
                model_kind="language",
                compatibility="supported",
                compatibility_reason=(
                    "Published GGUF files; Unified Inference validates the header and "
                    "llama.cpp performs final architecture validation on first load."
                ),
                size_bytes=sum(sizes[name] for name in download_files),
                quantization=_gguf_quantization(filename),
                filename=filename,
                projector_filename=None,
                projector_options=nearby_projectors,
                download_files=tuple(download_files),
                resolved_revision=(
                    str(resolved_revision) if resolved_revision is not None else revision
                ),
                suggested_role=suggested_role,
            )
        )
    return results


def _hf_token() -> str | None:
    return (
        os.environ.get("HF_TOKEN", "").strip()
        or os.environ.get("HUGGING_FACE_HUB_TOKEN", "").strip()
        or None
    )


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _suggested_role(
    repo_id: str,
    tags: set[str],
    pipeline_tag: Any = None,
) -> str:
    """Suggest one safe UI role from Hub metadata without claiming compatibility."""

    pipeline = pipeline_tag.casefold() if isinstance(pipeline_tag, str) else ""
    if (
        pipeline in {"feature-extraction", "sentence-similarity"}
        or "text-embeddings-inference" in tags
        or "sentence-transformers" in tags
        or re.search(r"(^|[/_.-])embeddings?($|[/_.-])", repo_id.casefold())
    ):
        return "embeddings"
    if (
        pipeline == "text-ranking"
        or "text-ranking" in tags
        or {"rerank", "reranker", "reranking"} & tags
        or re.search(
            r"(^|[/_.-])rerank(?:er)?($|[/_.-])",
            repo_id.casefold(),
        )
    ):
        return "rerank"
    return "generation"


def _quantization(repo_id: str, tags: set[str]) -> str | None:
    haystack = " ".join((repo_id.casefold(), *sorted(tags)))
    for label in ("2bit", "3bit", "4bit", "6bit", "8bit", "bf16", "fp16", "fp8"):
        if label in haystack:
            return label
    return None


__all__ = [
    "LibraryModel",
    "download_size",
    "image_profile_defaults",
    "gguf_files",
    "recommended_models",
    "search_models",
    "validate_install_candidate",
    "verified_model",
]
