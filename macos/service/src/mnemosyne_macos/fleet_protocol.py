"""Versioned, secret-free protocol helpers for native fleet snapshots.

The native package deliberately owns its implementation of the wire contract:
the packaged macOS service cannot import the CUDA manager's repository-root
modules. Cross-platform golden tests keep the canonical JSON and capability
vocabulary aligned.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from .models import EngineName, ResolvedTarget


FLEET_SCHEMA_VERSION = 1
_IMMUTABLE_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_GGUF_QUANTIZATION_RE = re.compile(
    r"(?:^|[-_.])("
    r"F(?:16|32)|BF16|"
    r"Q[2-8](?:_[0-9])?(?:_[Kk](?:_[SML])?)?|"
    r"IQ[1-4](?:_[A-Z0-9]+)+"
    r")(?:[-_.]|$)",
    re.IGNORECASE,
)
_CAPABILITY_ALIASES = {
    "chat.completions": "chat/completions",
    "chat/completions": "chat/completions",
    "completions": "completions",
    "embeddings": "embeddings",
    "images.generations": "images/generations",
    "images/generations": "images/generations",
    "messages": "messages",
    "rerank": "rerank",
    "responses": "responses",
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_id(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def normalize_capabilities(values: Iterable[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in values:
        value = str(raw).strip().lower().removeprefix("/v1/")
        mapped = _CAPABILITY_ALIASES.get(value)
        if mapped is None:
            raise ValueError(f"unsupported fleet capability '{raw}'")
        normalized.add(mapped)
    if not normalized:
        raise ValueError("fleet deployment must advertise at least one capability")
    return tuple(sorted(normalized))


def deployment_identity(
    *,
    engine: str,
    upstream_model: str,
    resolved_revision: str | None,
    artifact: Mapping[str, Any],
    kind: str,
    capabilities: Iterable[str],
    load_config: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    normalized_capabilities = normalize_capabilities(capabilities)
    normalized_revision = (
        resolved_revision.lower()
        if resolved_revision is not None
        and _IMMUTABLE_REVISION_RE.fullmatch(resolved_revision)
        else resolved_revision
    )
    load_config_digest = sha256_id(dict(load_config))
    identity = {
        "protocol": FLEET_SCHEMA_VERSION,
        "engine": str(engine),
        "upstream_model": str(upstream_model),
        "resolved_revision": normalized_revision,
        "artifact": dict(artifact),
        "kind": str(kind),
        "capabilities": list(normalized_capabilities),
        "load_config_digest": load_config_digest,
    }
    return sha256_id(identity), load_config_digest, identity


def immutable_revision(value: str | None) -> bool:
    return bool(value and _IMMUTABLE_REVISION_RE.fullmatch(value))


def gguf_quantization(filename: str | None) -> str | None:
    if not filename:
        return None
    match = _GGUF_QUANTIZATION_RE.search(filename.rsplit("/", 1)[-1])
    return match.group(1).upper() if match is not None else None


def semantic_extra_args(values: Iterable[str]) -> list[str]:
    """Remove only valid capacity-only llama.cpp flags from identity."""

    source = [str(value) for value in values]
    result: list[str] = []
    index = 0
    while index < len(source):
        value = source[index]
        name = next(
            (
                candidate
                for candidate in ("--parallel", "-np")
                if value == candidate or value.startswith(f"{candidate}=")
            ),
            None,
        )
        if name is None:
            result.append(value)
            index += 1
            continue
        if value.startswith(f"{name}="):
            try:
                parsed = int(value[len(name) + 1 :])
            except ValueError:
                result.append(value)
            else:
                if parsed <= 0:
                    result.append(value)
            index += 1
            continue
        if index + 1 < len(source):
            try:
                parsed = int(source[index + 1])
            except ValueError:
                result.append(value)
                index += 1
            else:
                if parsed > 0:
                    index += 2
                else:
                    result.append(value)
                    index += 1
            continue
        result.append(value)
        index += 1
    return result


def _last_positive_int_flag(values: list[str], *names: str) -> int | None:
    result: int | None = None
    accepted = set(names)
    index = 0
    while index < len(values):
        item = values[index]
        candidate: str | None = None
        if item in accepted and index + 1 < len(values):
            candidate = values[index + 1]
            index += 1
        else:
            for name in accepted:
                prefix = f"{name}="
                if item.startswith(prefix):
                    candidate = item[len(prefix) :]
                    break
        if candidate is not None:
            try:
                parsed = int(candidate)
            except ValueError:
                pass
            else:
                if parsed > 0:
                    result = parsed
        index += 1
    return result


def _last_valid_flag(
    values: list[str],
    names: set[str],
    validator,
) -> str | None:
    result: str | None = None
    index = 0
    while index < len(values):
        item = values[index]
        name, equals, inline = item.partition("=")
        if name not in names:
            index += 1
            continue
        if equals:
            if validator(inline):
                result = inline
            index += 1
            continue
        if index + 1 < len(values) and validator(values[index + 1]):
            result = values[index + 1]
            index += 2
            continue
        index += 1
    return result


def _valid_int(value: str, *, minimum: int = 1) -> bool:
    try:
        return int(value) >= minimum
    except ValueError:
        return False


def _valid_non_option(value: str) -> bool:
    return bool(value) and not value.startswith("-")


def _strip_valid_flags(
    values: list[str],
    value_flags,
    boolean_flags: set[str],
) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(values):
        item = values[index]
        name, equals, inline = item.partition("=")
        if name in boolean_flags and not equals:
            index += 1
            continue
        validator = value_flags.get(name)
        if validator is None:
            result.append(item)
            index += 1
            continue
        if equals:
            if validator(inline):
                index += 1
            else:
                result.append(item)
                index += 1
            continue
        if index + 1 < len(values) and validator(values[index + 1]):
            index += 2
            continue
        result.append(item)
        index += 1
    return result


def _llama_semantic_load_config(load: Mapping[str, Any]) -> dict[str, Any]:
    args = semantic_extra_args(load.get("extra_args", []))
    positive = lambda value: _valid_int(value, minimum=1)
    nonnegative = lambda value: _valid_int(value, minimum=0)
    pooling_values = {
        "none",
        "mean",
        "cls",
        "last",
        "rank",
        "unspecified",
    }
    enum_pooling = lambda value: value.lower() in pooling_values
    value_flags = {
        "--threads": positive,
        "-t": positive,
        "--threads-batch": positive,
        "-tb": positive,
        "--batch-size": positive,
        "-b": positive,
        "--ubatch-size": positive,
        "-ub": positive,
        "--n-gpu-layers": lambda value: _valid_int(value, minimum=-1),
        "--gpu-layers": lambda value: _valid_int(value, minimum=-1),
        "-ngl": lambda value: _valid_int(value, minimum=-1),
        "--split-mode": lambda value: value in {"none", "layer", "row"},
        "--tensor-split": _valid_non_option,
        "--main-gpu": nonnegative,
        "--flash-attn": lambda value: value.lower()
        in {"on", "off", "auto", "true", "false", "0", "1"},
        "--ctx-size": positive,
        "-c": positive,
        "--pooling": enum_pooling,
        "--pooling-type": enum_pooling,
    }
    return {
        "context_length": (
            _last_positive_int_flag(args, "--ctx-size", "-c")
            or load.get("context_length")
        ),
        "pooling": (
            _last_valid_flag(
                args,
                {"--pooling", "--pooling-type"},
                enum_pooling,
            )
            or load.get("pooling")
        ),
        "semantic_extra_args": _strip_valid_flags(
            args,
            value_flags,
            {"--no-kv-offload", "--kv-unified"},
        ),
    }


def portable_load_config(target: ResolvedTarget) -> dict[str, Any]:
    """Return the path-neutral, output-semantic v1 load identity."""

    load = dict(target.load_options)
    if target.key.engine == EngineName.LLAMA_CPP:
        # Projectors are immutable artifacts, while parallelism, CPU/GPU
        # placement, batching, and KV placement are node capacity concerns.
        return _llama_semantic_load_config(load)
    if target.key.engine == EngineName.OMLX:
        return {}
    if target.key.engine == EngineName.MFLUX:
        return {
            "family": load.get("family"),
            "quantize": load.get("quantize"),
            "image_defaults": dict(target.image_defaults),
        }

    # DS4 is native-only in v1. Retain its output-affecting settings while
    # replacing the machine-local KV path with a path-neutral mode bit.
    load.pop("parallel", None)
    if "kv_disk_directory" in load:
        load["kv_disk_enabled"] = bool(load.pop("kv_disk_directory", None))
    load["semantic_extra_args"] = semantic_extra_args(
        load.pop("extra_args", [])
    )
    return load


@dataclass(frozen=True)
class Capacity:
    derived_limit: int
    configured_max_concurrency: int | None
    effective_limit: int
    active: int
    queued: int
    available: int
    source: str
    confidence: str
    saturation: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def effective_capacity(
    *,
    derived_limit: int,
    configured_max_concurrency: int | None,
    active: int,
    queued: int,
    source: str,
    confidence: str,
    accepting: bool = True,
) -> Capacity:
    if derived_limit < 1:
        raise ValueError("derived concurrency limit must be positive")
    if configured_max_concurrency is not None and configured_max_concurrency < 1:
        raise ValueError("configured max_concurrency must be positive or null")
    if active < 0 or queued < 0:
        raise ValueError("active and queued request counts must be non-negative")
    effective = (
        min(derived_limit, configured_max_concurrency)
        if configured_max_concurrency is not None
        else derived_limit
    )
    available = max(0, effective - active) if accepting else 0
    return Capacity(
        derived_limit=derived_limit,
        configured_max_concurrency=configured_max_concurrency,
        effective_limit=effective,
        active=active,
        queued=queued,
        available=available,
        source=source,
        confidence=confidence,
        saturation=round(active / effective, 6),
    )


def derive_macos_capacity(
    target: ResolvedTarget,
    *,
    configured_max_concurrency: int | None,
    active: int = 0,
    queued: int = 0,
    accepting: bool = True,
) -> Capacity:
    """Derive a conservative admission limit from native adapter contracts."""

    if target.key.engine == EngineName.LLAMA_CPP:
        configured_parallel = target.load_options.get("parallel")
        if (
            isinstance(configured_parallel, int)
            and not isinstance(configured_parallel, bool)
            and configured_parallel > 0
        ):
            derived = configured_parallel
            source = "llama.cpp-parallel"
            confidence = "configured"
        else:
            derived = 1
            source = "llama.cpp-conservative"
            confidence = "conservative"
    elif target.key.engine == EngineName.MFLUX:
        derived = 1
        source = "mflux-serial-worker"
        confidence = "authoritative"
    elif target.key.engine == EngineName.DS4:
        derived = 1
        source = "ds4-conservative"
        confidence = "conservative"
    else:
        derived = 1
        source = "omlx-conservative"
        confidence = "conservative"
    return effective_capacity(
        derived_limit=derived,
        configured_max_concurrency=configured_max_concurrency,
        active=active,
        queued=queued,
        source=source,
        confidence=confidence,
        accepting=accepting,
    )


__all__ = [
    "Capacity",
    "FLEET_SCHEMA_VERSION",
    "canonical_json",
    "deployment_identity",
    "derive_macos_capacity",
    "effective_capacity",
    "gguf_quantization",
    "immutable_revision",
    "normalize_capabilities",
    "portable_load_config",
    "semantic_extra_args",
    "sha256_id",
]
