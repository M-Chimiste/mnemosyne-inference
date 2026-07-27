"""Bounded metadata extraction for local and Hub-backed model artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import io
import re
import struct
from typing import Any, BinaryIO, Callable, Mapping, TypeVar


_GGUF_SCALAR_FORMATS: dict[int, str] = {
    0: "<B",   # UINT8
    1: "<b",   # INT8
    2: "<H",   # UINT16
    3: "<h",   # INT16
    4: "<I",   # UINT32
    5: "<i",   # INT32
    6: "<f",   # FLOAT32
    7: "<?",   # BOOL
    10: "<Q",  # UINT64
    11: "<q",  # INT64
    12: "<d",  # FLOAT64
}
_GGUF_STRING = 8
_GGUF_ARRAY = 9
_MAX_GGUF_METADATA_ENTRIES = 16_384
_MAX_GGUF_STRING_BYTES = 4 * 1024 * 1024
_MAX_GGUF_ARRAY_ITEMS = 1_000_000
_MAX_CONTEXT_LENGTH = 10_000_000
_MAX_MODEL_CARD_BYTES = 96 * 1024
_MARKDOWN_FRONT_MATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_MARKDOWN_BADGE = re.compile(r"^\s*(?:\[?!?\[[^\n]*\]\([^\n]*\)\s*)+$")
_PROJECTOR_PRECISION_PRIORITY = (
    "F32",
    "F16",
    "BF16",
    "Q8_0",
    "Q6_K",
    "Q5_K_M",
    "Q5_K_S",
    "Q4_K_M",
    "Q4_K_S",
)
_ProjectorT = TypeVar("_ProjectorT")


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    architecture: str | None = None
    context_length: int | None = None
    parameter_count: int | None = None
    name: str | None = None
    description: str | None = None
    model_card_markdown: str | None = None
    license: str | None = None


class MetadataReadError(ValueError):
    """The artifact metadata is malformed or exceeds the bounded reader."""


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    value = stream.read(size)
    if not isinstance(value, bytes) or len(value) != size:
        raise MetadataReadError("truncated GGUF metadata")
    return value


def _read_u32(stream: BinaryIO) -> int:
    return struct.unpack("<I", _read_exact(stream, 4))[0]


def _read_u64(stream: BinaryIO) -> int:
    return struct.unpack("<Q", _read_exact(stream, 8))[0]


def _read_gguf_string(stream: BinaryIO) -> str:
    size = _read_u64(stream)
    if size > _MAX_GGUF_STRING_BYTES:
        raise MetadataReadError("GGUF metadata string is too large")
    return _read_exact(stream, size).decode("utf-8", errors="replace")


def _skip(stream: BinaryIO, size: int) -> None:
    if size < 0:
        raise MetadataReadError("invalid GGUF metadata size")
    try:
        stream.seek(size, io.SEEK_CUR)
    except (AttributeError, OSError):
        remaining = size
        while remaining:
            chunk = stream.read(min(remaining, 64 * 1024))
            if not isinstance(chunk, bytes) or not chunk:
                raise MetadataReadError("truncated GGUF metadata")
            remaining -= len(chunk)


def _read_scalar(stream: BinaryIO, value_type: int) -> Any:
    if value_type == _GGUF_STRING:
        return _read_gguf_string(stream)
    format_value = _GGUF_SCALAR_FORMATS.get(value_type)
    if format_value is None:
        raise MetadataReadError(f"unsupported GGUF metadata type {value_type}")
    return struct.unpack(format_value, _read_exact(stream, struct.calcsize(format_value)))[0]


def _skip_value(stream: BinaryIO, value_type: int) -> None:
    if value_type == _GGUF_STRING:
        _skip(stream, _read_u64(stream))
        return
    if value_type != _GGUF_ARRAY:
        format_value = _GGUF_SCALAR_FORMATS.get(value_type)
        if format_value is None:
            raise MetadataReadError(f"unsupported GGUF metadata type {value_type}")
        _skip(stream, struct.calcsize(format_value))
        return

    element_type = _read_u32(stream)
    count = _read_u64(stream)
    if count > _MAX_GGUF_ARRAY_ITEMS:
        raise MetadataReadError("GGUF metadata array is too large")
    if element_type == _GGUF_STRING:
        for _ in range(count):
            _skip(stream, _read_u64(stream))
        return
    format_value = _GGUF_SCALAR_FORMATS.get(element_type)
    if format_value is None:
        raise MetadataReadError(
            f"unsupported GGUF metadata array type {element_type}"
        )
    _skip(stream, struct.calcsize(format_value) * count)


def read_gguf_metadata(stream: BinaryIO) -> dict[str, Any]:
    """Read useful scalar GGUF metadata without touching tensor payloads."""

    if _read_exact(stream, 4) != b"GGUF":
        raise MetadataReadError("file does not have a GGUF header")
    version = _read_u32(stream)
    if version not in {2, 3}:
        raise MetadataReadError(f"unsupported GGUF version {version}")
    _tensor_count = _read_u64(stream)
    metadata_count = _read_u64(stream)
    if metadata_count > _MAX_GGUF_METADATA_ENTRIES:
        raise MetadataReadError("GGUF contains too many metadata entries")

    values: dict[str, Any] = {}
    for _ in range(metadata_count):
        key = _read_gguf_string(stream)
        value_type = _read_u32(stream)
        wanted = (
            key.startswith("general.")
            or key.endswith(".context_length")
            or key.endswith(".context_length_train")
        )
        if wanted and value_type != _GGUF_ARRAY:
            values[key] = _read_scalar(stream, value_type)
        else:
            try:
                _skip_value(stream, value_type)
            except MetadataReadError:
                # Tokenizer arrays can be enormous and are irrelevant once
                # useful model metadata has already been read.
                break
    return values


def metadata_from_gguf_values(values: Mapping[str, Any]) -> ModelMetadata:
    architecture = _clean_string(values.get("general.architecture"))
    context_candidates: list[int] = []
    preferred_keys = (
        f"{architecture}.context_length" if architecture else None,
        f"{architecture}.context_length_train" if architecture else None,
    )
    for key in preferred_keys:
        if key is not None:
            value = _bounded_positive_int(values.get(key))
            if value is not None:
                context_candidates.append(value)
    if not context_candidates:
        for key, raw in values.items():
            if key.endswith((".context_length", ".context_length_train")):
                value = _bounded_positive_int(raw)
                if value is not None:
                    context_candidates.append(value)

    return ModelMetadata(
        architecture=architecture,
        context_length=max(context_candidates) if context_candidates else None,
        parameter_count=_positive_int(values.get("general.parameter_count")),
        name=_clean_string(values.get("general.name")),
        description=_clean_string(values.get("general.description")),
        license=_clean_string(values.get("general.license")),
    )


def metadata_from_gguf_stream(stream: BinaryIO) -> ModelMetadata:
    try:
        return metadata_from_gguf_values(read_gguf_metadata(stream))
    except (MetadataReadError, OSError, struct.error):
        return ModelMetadata()


def metadata_from_config(config: Mapping[str, Any]) -> ModelMetadata:
    """Extract conservative architecture and context signals from HF config."""

    configurations = [config]
    for key in ("text_config", "language_config", "llm_config"):
        nested = config.get(key)
        if isinstance(nested, Mapping):
            configurations.append(nested)

    architecture: str | None = None
    for candidate in configurations:
        raw_architectures = candidate.get("architectures")
        if isinstance(raw_architectures, str):
            architecture = _clean_string(raw_architectures)
        elif isinstance(raw_architectures, list):
            architecture = next(
                (
                    cleaned
                    for value in raw_architectures
                    if (cleaned := _clean_string(value)) is not None
                ),
                None,
            )
        if architecture:
            break
    if architecture is None:
        architecture = _clean_string(config.get("model_type"))

    context_keys = (
        "max_position_embeddings",
        "model_max_length",
        "max_sequence_length",
        "seq_length",
        "context_length",
        "n_positions",
    )
    context_length: int | None = None
    for key in context_keys:
        candidates = [
            value
            for candidate in configurations
            if (value := _bounded_positive_int(candidate.get(key))) is not None
        ]
        if candidates:
            context_length = max(candidates)
            break

    parameter_count = next(
        (
            value
            for key in ("num_parameters", "parameter_count", "n_params")
            if (value := _positive_int(config.get(key))) is not None
        ),
        None,
    )
    return ModelMetadata(
        architecture=architecture,
        context_length=context_length,
        parameter_count=parameter_count,
        name=_clean_string(config.get("_name_or_path")),
    )


def bounded_markdown(value: str | bytes | None) -> str | None:
    if isinstance(value, bytes):
        value = value[:_MAX_MODEL_CARD_BYTES].decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    encoded = value.encode("utf-8")[:_MAX_MODEL_CARD_BYTES]
    cleaned = encoded.decode("utf-8", errors="ignore").strip()
    return cleaned or None


def markdown_summary(markdown: str | None, *, max_characters: int = 700) -> str | None:
    """Return the first useful prose paragraph from a bounded model card."""

    if not markdown:
        return None
    body = _MARKDOWN_FRONT_MATTER.sub("", markdown, count=1)
    paragraphs = re.split(r"\n\s*\n", body)
    for paragraph in paragraphs:
        lines = [
            line.strip()
            for line in paragraph.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "<!--"))
        ]
        if not lines:
            continue
        candidate = " ".join(lines)
        if _MARKDOWN_BADGE.fullmatch(candidate):
            continue
        candidate = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", candidate)
        candidate = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", candidate)
        candidate = re.sub(r"[`*_~]+", "", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if len(candidate) < 24:
            continue
        return candidate[:max_characters].rstrip()
    return None


def recommended_projector(
    values: list[_ProjectorT] | tuple[_ProjectorT, ...],
    *,
    name: Callable[[_ProjectorT], str],
) -> _ProjectorT | None:
    """Choose a deterministic high-fidelity projector while retaining opt-out."""

    if not values:
        return None

    def rank(value: _ProjectorT) -> tuple[int, str]:
        filename = str(name(value))
        upper = filename.upper()
        precision = next(
            (
                index
                for index, label in enumerate(_PROJECTOR_PRECISION_PRIORITY)
                if re.search(rf"(^|[-_.]){re.escape(label)}($|[-_.])", upper)
            ),
            len(_PROJECTOR_PRECISION_PRIORITY),
        )
        return precision, filename.casefold()

    return min(values, key=rank)


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    integer = int(value)
    return integer if integer > 0 else None


def _bounded_positive_int(value: Any) -> int | None:
    integer = _positive_int(value)
    return integer if integer is not None and integer <= _MAX_CONTEXT_LENGTH else None


__all__ = [
    "MetadataReadError",
    "ModelMetadata",
    "bounded_markdown",
    "markdown_summary",
    "metadata_from_config",
    "metadata_from_gguf_stream",
    "metadata_from_gguf_values",
    "recommended_projector",
    "read_gguf_metadata",
]
