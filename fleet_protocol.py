"""Versioned, secret-free Mnemosyne fleet protocol helpers.

This module is deliberately independent of FastAPI, SQLite, and engine
implementations.  Node runtimes project their local profiles into these
primitive helpers and expose the resulting documents over the read-only fleet
snapshot endpoint.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence


FLEET_SCHEMA_VERSION = 1
_IMMUTABLE_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_SHA256_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
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
    """Return the canonical JSON representation used by fleet identities."""

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
    """Build one strict deployment identity and its load-config digest.

    Node placement, public aliases, storage paths, and concurrency settings are
    intentionally absent.  Callers must mark identity confidence separately
    when ``resolved_revision`` or artifact identity is not immutable.
    """

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


def identity_is_authoritative(
    *,
    resolved_revision: str | None,
    artifact: Mapping[str, Any],
) -> bool:
    """Whether a deployment has portable immutable provenance.

    Symbolic revisions such as ``main`` are intentionally not accepted.
    Content-addressed local artifacts may omit a repository revision.
    """

    if resolved_revision and _IMMUTABLE_REVISION_RE.fullmatch(resolved_revision):
        return True
    content_digest = artifact.get("content_digest")
    return isinstance(content_digest, str) and bool(
        _SHA256_ID_RE.fullmatch(content_digest)
    )


def gguf_quantization(filename: str | None) -> str | None:
    if not filename:
        return None
    match = _GGUF_QUANTIZATION_RE.search(filename.rsplit("/", 1)[-1])
    return match.group(1).upper() if match is not None else None


def positive_int_flag(
    args: Sequence[str],
    *names: str,
) -> int | None:
    """Extract the last positive integer value for a CLI option.

    Both ``--flag 4`` and ``--flag=4`` forms are accepted.  Malformed values
    are ignored so an engine remains loadable through its existing escape
    hatch; the fleet layer then uses a conservative capacity.
    """

    result: int | None = None
    accepted = set(names)
    index = 0
    while index < len(args):
        item = str(args[index])
        matched_value: str | None = None
        if item in accepted and index + 1 < len(args):
            matched_value = str(args[index + 1])
            index += 1
        else:
            for name in accepted:
                prefix = f"{name}="
                if item.startswith(prefix):
                    matched_value = item[len(prefix) :]
                    break
        if matched_value is not None:
            try:
                candidate = int(matched_value)
            except (TypeError, ValueError):
                pass
            else:
                if candidate > 0:
                    result = candidate
        index += 1
    return result


def semantic_extra_args(
    args: Sequence[str],
    *,
    backend: str,
) -> tuple[str, ...]:
    """Remove only recognized capacity controls from an ordered arg list.

    Capacity is intentionally not part of strict deployment identity. Every
    unknown argument stays byte-for-byte ordered because it may alter the
    externally observable inference contract.
    """

    capacity_options = {
        "vllm": frozenset({"--max-num-seqs"}),
        "llama.cpp": frozenset({"--parallel", "-np"}),
    }.get(backend, frozenset())
    output: list[str] = []
    index = 0
    while index < len(args):
        item = str(args[index])
        matched = next(
            (
                name
                for name in capacity_options
                if item == name or item.startswith(f"{name}=")
            ),
            None,
        )
        if matched is None:
            output.append(item)
            index += 1
            continue
        if item.startswith(f"{matched}="):
            try:
                parsed = int(item[len(matched) + 1 :])
            except ValueError:
                output.append(item)
            else:
                if parsed <= 0:
                    output.append(item)
            index += 1
            continue
        if item == matched and index + 1 < len(args):
            try:
                parsed = int(str(args[index + 1]))
            except (TypeError, ValueError):
                output.append(item)
                index += 1
            else:
                if parsed > 0:
                    index += 2
                else:
                    output.append(item)
                    index += 1
            continue
        output.append(item)
        index += 1
    return tuple(output)


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
    admission_open: bool = True,
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
    # A theoretical engine slot is not necessarily admissible. During a
    # drain, a degraded state, or while a different deployment owns the FIFO
    # head, schedulers must see zero availability even if the arithmetic
    # limit has room.
    available = max(0, effective - active) if admission_open else 0
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


def derive_cuda_capacity(
    *,
    backend: str,
    extra_args: Sequence[str],
    configured_max_concurrency: int | None,
    active: int,
    queued: int,
    admission_open: bool = True,
) -> Capacity:
    """Derive a safe CUDA-manager capacity from the selected engine profile."""

    if backend == "vllm":
        derived = positive_int_flag(extra_args, "--max-num-seqs")
        source = "vllm-max-num-seqs" if derived is not None else "vllm-conservative"
        confidence = "configured" if derived is not None else "conservative"
    elif backend == "llama.cpp":
        derived = positive_int_flag(extra_args, "--parallel", "-np")
        source = "llama.cpp-parallel" if derived is not None else "llama.cpp-conservative"
        confidence = "configured" if derived is not None else "conservative"
    elif backend == "sglang-diffusion":
        derived = 1
        source = "sglang-diffusion-isolated-worker"
        confidence = "authoritative"
    else:
        derived = 1
        source = "unknown-engine-conservative"
        confidence = "conservative"
    return effective_capacity(
        derived_limit=derived or 1,
        configured_max_concurrency=configured_max_concurrency,
        active=active,
        queued=queued,
        source=source,
        confidence=confidence,
        admission_open=admission_open,
    )


__all__ = [
    "Capacity",
    "FLEET_SCHEMA_VERSION",
    "canonical_json",
    "deployment_identity",
    "derive_cuda_capacity",
    "effective_capacity",
    "gguf_quantization",
    "identity_is_authoritative",
    "normalize_capabilities",
    "positive_int_flag",
    "semantic_extra_args",
    "sha256_id",
]
