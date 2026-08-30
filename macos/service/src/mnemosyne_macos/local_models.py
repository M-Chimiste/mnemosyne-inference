"""Residency-neutral discovery of existing GGUF and MLX model folders."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import io
import json
import os
from pathlib import Path
import re
from typing import Any, Sequence

from .model_metadata import (
    bounded_markdown,
    markdown_summary,
    metadata_from_config,
    metadata_from_gguf_stream,
    recommended_projector,
)
from .models import EngineName


_SHARD_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<part>\d{5})-of-(?P<total>\d{5})\.gguf$",
    re.IGNORECASE,
)
_QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:UD-)?(?:IQ|Q|TQ|BF|F|FP)\d+(?:_[A-Za-z0-9]+)*)",
    re.IGNORECASE,
)

_LLAMA_GENERATION_CAPABILITIES = (
    "chat/completions",
    "completions",
    "responses",
)
_OMLX_GENERATION_CAPABILITIES = (
    "chat/completions",
    "completions",
    "responses",
    "messages",
)
_EMBEDDING_CAPABILITIES = ("embeddings",)
_RERANK_CAPABILITIES = ("rerank",)

# These are intentionally a conservative subset of oMLX v0.5.3's official
# model discovery classifier. Unknown metadata is not silently promoted to an
# all-purpose language profile: oMLX has distinct generation, embedding, and
# reranker engines, and routing a request to the wrong one fails only after an
# expensive load attempt.
_OMLX_EMBEDDING_ARCHITECTURES = {
    "BertModel",
    "BertForMaskedLM",
    "XLMRobertaModel",
    "XLMRobertaForMaskedLM",
    "ModernBertModel",
    "ModernBertForMaskedLM",
    "Qwen3ForTextEmbedding",
    "SiglipModel",
    "SiglipVisionModel",
    "SiglipTextModel",
}
_OMLX_EMBEDDING_MODEL_TYPES = {
    "bert",
    "xlm_roberta",
    "modernbert",
    "siglip",
    "colqwen2_5",
}
_OMLX_CAUSAL_EMBEDDING_ARCHITECTURES = {
    "Qwen2ForCausalLM",
    "Qwen3ForCausalLM",
}
_OMLX_RERANK_ARCHITECTURES = {
    "ModernBertForSequenceClassification",
    "XLMRobertaForSequenceClassification",
    "JinaForRanking",
}
_OMLX_CAUSAL_RERANK_ARCHITECTURES = {"Qwen3ForCausalLM"}
_OMLX_MULTIMODAL_SPECIALIZED_ARCHITECTURES = {
    "Qwen3VLForConditionalGeneration"
}
_OMLX_AUDIO_ARCHITECTURES = {
    "WhisperForConditionalGeneration",
    "Qwen3ASRForConditionalGeneration",
    "ParakeetForCTC",
    "Qwen2AudioForConditionalGeneration",
    "KokoroForConditionalGeneration",
    "Qwen3TTSForConditionalGeneration",
    "ChatterboxForConditionalGeneration",
    "VibeVoiceForConditionalGeneration",
    "VibeVoiceStreamingForConditionalGenerationInference",
    "KugelAudioForConditionalGeneration",
    "DeepFilterNetModel",
    "MossFormer2SEModel",
    "SAMAudio",
    "LFM2AudioModel",
}
_OMLX_AUDIO_MODEL_TYPES = {
    "whisper",
    "qwen3_asr",
    "parakeet",
    "qwen2_audio",
    "qwen3_tts",
    "kokoro",
    "chatterbox",
    "vibevoice",
    "vibevoice_streaming",
    "kugelaudio",
    "audiodit",
    "deepfilternet",
    "mossformer2_se",
    "sam_audio",
    "lfm_audio",
}
_OMLX_VLM_NATIVE_TEXT_MODEL_TYPES = {"cohere2_moe", "minimax_m3"}
_OMLX_HELPER_SUFFIXES = ("_assistant", "_mtp")
_OMLX_HELPER_ARCH_TOKENS = ("draft", "assistant", "mtp")


class LocalModelError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LocalProjector:
    id: str
    path: str
    filename: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LocalModel:
    id: str
    source_key: str
    engine: str
    display_name: str
    model_path: str
    all_paths: tuple[str, ...]
    shard_count: int
    quantization: str | None
    size_bytes: int
    compatibility: str
    compatibility_reason: str
    capabilities: tuple[str, ...]
    architecture: str | None = None
    context_length: int | None = None
    parameter_count: int | None = None
    summary: str | None = None
    model_card_markdown: str | None = None
    recommended_projector_id: str | None = None
    projector_options: tuple[LocalProjector, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["projector_options"] = [item.to_dict() for item in self.projector_options]
        return value


def _identifier(*parts: str) -> str:
    payload = json.dumps(parts, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _within(root: Path, candidate: Path) -> Path | None:
    # The selected root itself may be a Finder-selected symlink, but a model
    # candidate below that boundary must have one unambiguous lexical spelling.
    # Descendant symlinks are therefore never projected into persistent config.
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_relative_to(root) else None


def _lexical_projection(
    root: Path,
    lexical_root: Path,
    candidate: Path,
) -> Path:
    """Project one validated physical candidate beneath the selected spelling."""

    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LocalModelError("discovered model path escapes the selected folder") from exc
    projected = Path(os.path.abspath(str(lexical_root.joinpath(relative))))
    try:
        if projected != lexical_root and not projected.is_relative_to(lexical_root):
            raise ValueError
        observed = projected.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise LocalModelError(
            "discovered model path cannot be projected under the selected folder"
        ) from exc
    if observed != candidate:
        raise LocalModelError(
            "discovered model path changed during lexical projection"
        )
    return projected


def _is_projector(path: Path) -> bool:
    name = path.name.casefold()
    return name.startswith("mmproj") or "-mmproj" in name


def _gguf_magic(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"GGUF"
    except OSError:
        return False


def _quantization(filename: str) -> str | None:
    matches = _QUANT_RE.findall(Path(filename).stem)
    return matches[-1].upper() if matches else None


def _source_key(root: Path, model_path: Path) -> str:
    relative = model_path.relative_to(root)
    parts = relative.parts
    if len(parts) >= 3:
        return "/".join(parts[:2])
    if len(parts) == 2:
        return parts[0]
    return model_path.parent.name or model_path.stem


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_model_card(directory: Path) -> str | None:
    for filename in ("README.md", "readme.md", "MODEL_CARD.md", "model_card.md"):
        path = directory / filename
        try:
            if path.is_file():
                return bounded_markdown(path.read_bytes())
        except OSError:
            continue
    return None


def _gguf_capabilities(path: Path, *, has_projector: bool) -> tuple[str, ...]:
    if has_projector:
        return _LLAMA_GENERATION_CAPABILITIES
    name = path.stem.casefold()
    if re.search(r"(^|[-_.])rerank(?:er|ing)?($|[-_.])", name):
        return _RERANK_CAPABILITIES
    if re.search(r"(^|[-_.])embed(?:ding|dings)?($|[-_.])", name):
        return _EMBEDDING_CAPABILITIES
    return _LLAMA_GENERATION_CAPABILITIES


def _sentence_transformers_embedding(directory: Path) -> bool:
    modules_path = directory / "modules.json"
    if not modules_path.is_file():
        return False
    try:
        modules = json.loads(modules_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(modules, list):
        return False
    module_types = {
        item.get("type", "")
        for item in modules
        if isinstance(item, dict) and isinstance(item.get("type"), str)
    }
    return (
        "sentence_transformers.models.Transformer" in module_types
        and any(
            item.startswith("sentence_transformers.models.")
            and item != "sentence_transformers.models.Transformer"
            for item in module_types
        )
    )


def _omlx_capabilities(
    directory: Path,
    config_path: Path,
) -> tuple[str, str, tuple[str, ...]]:
    """Classify a local MLX directory without guessing across oMLX engines."""

    config = _read_json_object(config_path)
    if config is None:
        return (
            "unavailable",
            "config.json is not a valid JSON object, so oMLX model type is ambiguous.",
            (),
        )

    raw_architectures = config.get("architectures", [])
    if isinstance(raw_architectures, str):
        architectures = [raw_architectures]
    elif isinstance(raw_architectures, list) and all(
        isinstance(item, str) for item in raw_architectures
    ):
        architectures = raw_architectures
    else:
        return (
            "unavailable",
            "config.json has an invalid architectures field, so oMLX model type is ambiguous.",
            (),
        )
    for nested_key in ("text_config", "language_config"):
        nested = config.get(nested_key)
        if not isinstance(nested, dict):
            continue
        nested_architectures = nested.get("architectures", [])
        if isinstance(nested_architectures, str):
            architectures.append(nested_architectures)
        elif isinstance(nested_architectures, list):
            architectures.extend(
                item for item in nested_architectures if isinstance(item, str)
            )

    model_type_value = config.get("model_type", "")
    model_type = (
        model_type_value.lower().replace("-", "_")
        if isinstance(model_type_value, str)
        else ""
    )
    name = directory.name.casefold()
    helper = (
        model_type.endswith(_OMLX_HELPER_SUFFIXES)
        or "dflash_config" in config
        or any(
            token in architecture.casefold()
            for architecture in architectures
            for token in _OMLX_HELPER_ARCH_TOKENS
        )
    )
    if helper:
        return (
            "unavailable",
            "oMLX identifies this as a speculative-decoding helper, not a standalone API model.",
            (),
        )

    if (
        any(item in _OMLX_AUDIO_ARCHITECTURES for item in architectures)
        or model_type in _OMLX_AUDIO_MODEL_TYPES
    ):
        return (
            "unavailable",
            "oMLX identifies this as an audio model; Unified Inference does not expose audio routes yet.",
            (),
        )

    if any(item in _OMLX_RERANK_ARCHITECTURES for item in architectures):
        return (
            "likely",
            "Local metadata identifies an oMLX reranker; final compatibility is verified on load.",
            _RERANK_CAPABILITIES,
        )
    if (
        any(item in _OMLX_CAUSAL_RERANK_ARCHITECTURES for item in architectures)
        and ("rerank" in name or "reranker" in name)
    ) or (
        any(
            item in _OMLX_MULTIMODAL_SPECIALIZED_ARCHITECTURES
            for item in architectures
        )
        and ("rerank" in name or "reranker" in name)
    ):
        return (
            "likely",
            "Local metadata identifies a causal oMLX reranker; final compatibility is verified on load.",
            _RERANK_CAPABILITIES,
        )

    if (
        _sentence_transformers_embedding(directory)
        or any(item in _OMLX_EMBEDDING_ARCHITECTURES for item in architectures)
        or model_type in _OMLX_EMBEDDING_MODEL_TYPES
        or (
            any(
                item in _OMLX_CAUSAL_EMBEDDING_ARCHITECTURES
                for item in architectures
            )
            and ("embed" in name or "embedding" in name)
        )
        or (
            any(
                item in _OMLX_MULTIMODAL_SPECIALIZED_ARCHITECTURES
                for item in architectures
            )
            and ("embed" in name or "embedding" in name)
        )
    ):
        return (
            "likely",
            "Local metadata identifies an oMLX embedding model; final compatibility is verified on load.",
            _EMBEDDING_CAPABILITIES,
        )

    has_vision = (
        isinstance(config.get("vision_config"), dict)
        or isinstance(config.get("vit_config"), dict)
        or bool(config.get("mm_vision_tower"))
    )
    if has_vision or model_type in _OMLX_VLM_NATIVE_TEXT_MODEL_TYPES:
        return (
            "likely",
            "Local metadata identifies an oMLX generation model; final compatibility is verified on load.",
            _OMLX_GENERATION_CAPABILITIES,
        )

    auto_map = config.get("auto_map")
    auto_map_keys = set(auto_map) if isinstance(auto_map, dict) else set()
    generation_architecture = any(
        "causallm" in item.casefold()
        or item.endswith("ForConditionalGeneration")
        for item in architectures
    )
    generation_auto_map = any(
        key in {"AutoModelForCausalLM", "AutoModelForSeq2SeqLM"}
        for key in auto_map_keys
    )
    if generation_architecture or generation_auto_map:
        return (
            "likely",
            "Local metadata identifies an oMLX generation model; final compatibility is verified on load.",
            _OMLX_GENERATION_CAPABILITIES,
        )

    return (
        "unavailable",
        (
            "Local metadata does not unambiguously identify this as an oMLX "
            "generation, embedding, or reranker model."
        ),
        (),
    )


def mark_omlx_id_conflicts(
    candidates: list[LocalModel],
    other_candidates: Sequence[LocalModel] = (),
) -> list[LocalModel]:
    """Mark candidates whose leaf ID would hit oMLX's first-root-wins rule."""

    paths_by_id: dict[str, set[str]] = {}
    for candidate in [*candidates, *other_candidates]:
        if candidate.engine != EngineName.OMLX.value:
            continue
        paths_by_id.setdefault(Path(candidate.model_path).name, set()).add(
            candidate.model_path
        )

    marked: list[LocalModel] = []
    for candidate in candidates:
        if candidate.engine != EngineName.OMLX.value:
            marked.append(candidate)
            continue
        model_id = Path(candidate.model_path).name
        conflicts = paths_by_id.get(model_id, set()) - {candidate.model_path}
        if not conflicts:
            marked.append(candidate)
            continue
        marked.append(
            replace(
                candidate,
                compatibility="unavailable",
                compatibility_reason=(
                    f"Duplicate oMLX model ID '{model_id}' exists at more than "
                    "one path. oMLX exposes only the leaf directory name and "
                    "silently keeps the first configured root; choose a narrower "
                    "library or rename one model folder."
                ),
            )
        )
    return marked


def scan_local_models(
    path: str | Path,
    *,
    lexical_root: str | Path | None = None,
    max_files: int = 100_000,
    max_models: int = 2_000,
) -> list[LocalModel]:
    """Scan a Finder-selected root without loading, copying, or mutating models."""

    selected = Path(path).expanduser()
    try:
        root = selected.resolve(strict=True)
    except OSError as exc:
        raise LocalModelError(f"selected model folder is unavailable: {exc}") from exc
    if not root.is_dir():
        raise LocalModelError("selected model path is not a directory")
    selected_spelling = Path(
        os.path.abspath(
            os.path.expanduser(str(path if lexical_root is None else lexical_root))
        )
    )
    try:
        if selected_spelling.resolve(strict=True) != root:
            raise LocalModelError(
                "selected model folder changed during lexical projection"
            )
    except OSError as exc:
        raise LocalModelError(
            f"selected model folder is unavailable: {exc}"
        ) from exc

    ggufs: list[Path] = []
    mlx_directories: set[Path] = set()
    visited = 0
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not name.startswith(".") and not (current_path / name).is_symlink()
        ]
        visited += len(files)
        if visited > max_files:
            raise LocalModelError(
                f"selected folder contains more than {max_files:,} files; choose a narrower folder"
            )
        names = set(files)
        if "config.json" in names:
            resolved_directory = _within(root, current_path)
            resolved_config = _within(root, current_path / "config.json")
            contained_weights = [
                _within(root, current_path / name)
                for name in names
                if name.endswith((".safetensors", ".npz"))
            ]
            if (
                resolved_directory is not None
                and resolved_config is not None
                and resolved_config.is_file()
                and any(
                    candidate is not None and candidate.is_file()
                    for candidate in contained_weights
                )
            ):
                mlx_directories.add(resolved_directory)
        for filename in files:
            if not filename.casefold().endswith(".gguf"):
                continue
            resolved = _within(root, current_path / filename)
            if resolved is not None and resolved.is_file():
                ggufs.append(resolved)

    projectors = [path for path in ggufs if _is_projector(path) and _gguf_magic(path)]
    primaries = [path for path in ggufs if not _is_projector(path)]
    results: list[LocalModel] = []
    for primary in sorted(primaries):
        shard = _SHARD_RE.match(primary.name)
        if shard and int(shard.group("part")) != 1:
            continue
        if shard:
            prefix = shard.group("prefix")
            total = int(shard.group("total"))
            group = tuple(
                candidate
                for candidate in sorted(primaries)
                if candidate.parent == primary.parent
                and (match := _SHARD_RE.match(candidate.name))
                and match.group("prefix") == prefix
                and int(match.group("total")) == total
            )
            if len(group) != total:
                compatibility = "unavailable"
                reason = f"incomplete GGUF shard set: found {len(group)} of {total}"
            else:
                compatibility = "structural"
                reason = "Complete local GGUF shard set; final compatibility is verified on load."
        else:
            group = (primary,)
            compatibility = "structural"
            reason = "Local GGUF file; final architecture compatibility is verified on load."
        if not all(_gguf_magic(item) for item in group):
            compatibility = "unavailable"
            reason = "One or more selected files do not have a valid GGUF header."
        nearby_items: list[LocalProjector] = []
        for projector in sorted(projectors):
            if projector.parent != primary.parent:
                continue
            lexical_projector = _lexical_projection(
                root,
                selected_spelling,
                projector,
            )
            nearby_items.append(
                LocalProjector(
                    id=_identifier(
                        str(selected_spelling),
                        str(lexical_projector),
                    ),
                    path=str(lexical_projector),
                    filename=projector.name,
                    size_bytes=projector.stat().st_size,
                )
            )
        nearby = tuple(nearby_items)
        selected_projector = recommended_projector(
            nearby,
            name=lambda item: item.filename,
        )
        try:
            with primary.open("rb") as stream:
                metadata = metadata_from_gguf_stream(stream)
        except OSError:
            metadata = metadata_from_gguf_stream(io.BytesIO())
        model_card = _read_model_card(primary.parent)
        lexical_primary = _lexical_projection(root, selected_spelling, primary)
        all_paths = tuple(
            str(_lexical_projection(root, selected_spelling, item))
            for item in group
        )
        results.append(
            LocalModel(
                id=_identifier(str(selected_spelling), *all_paths),
                source_key=_source_key(root, primary),
                engine=EngineName.LLAMA_CPP.value,
                display_name=primary.parent.name or primary.stem,
                model_path=str(lexical_primary),
                all_paths=all_paths,
                shard_count=len(group),
                quantization=_quantization(primary.name),
                size_bytes=sum(item.stat().st_size for item in group),
                compatibility=compatibility,
                compatibility_reason=reason,
                capabilities=_gguf_capabilities(
                    primary,
                    has_projector=selected_projector is not None,
                ),
                architecture=metadata.architecture,
                context_length=metadata.context_length,
                parameter_count=metadata.parameter_count,
                summary=metadata.description or markdown_summary(model_card),
                model_card_markdown=model_card,
                recommended_projector_id=(
                    selected_projector.id if selected_projector is not None else None
                ),
                projector_options=nearby,
            )
        )

    for directory in sorted(mlx_directories):
        weights = tuple(
            sorted(
                candidate
                for item in directory.iterdir()
                if item.name.endswith((".safetensors", ".npz"))
                and (candidate := _within(root, item)) is not None
                and candidate.is_file()
            )
        )
        config_path = _within(root, directory / "config.json")
        if config_path is None or not config_path.is_file() or not weights:
            continue
        compatibility, reason, capabilities = _omlx_capabilities(
            directory, config_path
        )
        config = _read_json_object(config_path) or {}
        metadata = metadata_from_config(config)
        model_card = _read_model_card(directory)
        lexical_directory = _lexical_projection(
            root,
            selected_spelling,
            directory,
        )
        all_paths = tuple(
            str(_lexical_projection(root, selected_spelling, item))
            for item in (*weights, config_path)
        )
        results.append(
            LocalModel(
                id=_identifier(
                    str(selected_spelling),
                    str(lexical_directory),
                    *all_paths,
                ),
                source_key=_source_key(root, directory / "model.safetensors"),
                engine=EngineName.OMLX.value,
                display_name=directory.name,
                model_path=str(lexical_directory),
                all_paths=all_paths,
                shard_count=len(weights),
                quantization=None,
                size_bytes=sum(item.stat().st_size for item in weights),
                compatibility=compatibility,
                compatibility_reason=reason,
                capabilities=capabilities,
                architecture=metadata.architecture,
                context_length=metadata.context_length,
                parameter_count=metadata.parameter_count,
                summary=markdown_summary(model_card),
                model_card_markdown=model_card,
            )
        )

    if len(results) > max_models:
        raise LocalModelError(
            f"selected folder contains more than {max_models:,} models; choose a narrower folder"
        )
    return sorted(
        mark_omlx_id_conflicts(results),
        key=lambda item: (item.engine, item.display_name.casefold(), item.model_path),
    )


__all__ = [
    "LocalModel",
    "LocalModelError",
    "LocalProjector",
    "mark_omlx_id_conflicts",
    "scan_local_models",
]
