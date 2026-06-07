"""Repo format probe — detect GGUF vs transformer weights and group GGUF shards.

Stdlib + dataclasses only. Imported by catalog.py, hf_search.py, vllm_manager.py,
and the standalone download_worker.py subprocess (which only needs the pure
filename grouper). Keep this module's import surface minimal so the worker's
cold start stays fast.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Canonical sharded GGUF naming: model-NAME-NNNNN-of-NNNNN.gguf.
# Non-conforming sharded layouts (e.g. ".split.001") are treated as single-file.
_SHARD_RE = re.compile(r"^(?P<base>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$")
_GGUF_EXT = ".gguf"
_TRANSFORMER_EXTS = (".safetensors", ".bin")

# Backend constants — strings instead of an Enum so they round-trip through
# JSON, SQLite TEXT columns, and Pydantic without conversion.
BACKEND_VLLM = "vllm"
BACKEND_LLAMA_CPP = "llama.cpp"
BACKEND_NONE = "none"


@dataclass(frozen=True)
class GgufGroup:
    """One installable GGUF unit. For sharded models, represents the whole
    shard set; for unsharded, the single file."""
    primary_filename: str          # The first shard, or the lone file.
    all_filenames: tuple[str, ...]  # Full shard set (length 1 when unsharded).
    shard_count: int


@dataclass(frozen=True)
class SiblingMeta:
    """Subset of huggingface_hub's RepoSibling that probe_repo_format needs.
    Mirrors `(rfilename, size)` so callers don't depend on the HF type."""
    rfilename: str
    size: Optional[int]


@dataclass(frozen=True)
class GgufCandidate:
    """A GgufGroup decorated with metadata for the install-form dropdown."""
    label: str                      # e.g. "Q4_K_M (4.4 GB)" — UI display.
    primary_filename: str
    all_filenames: tuple[str, ...]
    shard_count: int
    size_bytes: Optional[int]       # sum across shards; None if any size missing.


@dataclass(frozen=True)
class RepoFormatProbe:
    has_gguf: bool
    has_transformer_weights: bool
    recommended_backend: str        # BACKEND_VLLM | BACKEND_LLAMA_CPP | BACKEND_NONE
    gguf_candidates: tuple[GgufCandidate, ...] = field(default_factory=tuple)


def group_gguf_filenames(filenames: list[str]) -> list[GgufGroup]:
    """Group a list of filenames into GGUF candidates.

    Sharded files are recognized by the canonical `*-NNNNN-of-NNNNN.gguf`
    pattern. Each shard group is keyed on `(base, total)` so two different
    shard counts under the same base are distinct groups (they shouldn't
    normally co-exist in a single repo).

    Returns a stable, alpha-sorted-by-primary list. Files without `.gguf`
    extension are silently dropped.
    """
    # Sort once up front so shard sets land in index order naturally and the
    # final list ordering stays deterministic.
    ggufs = sorted(name for name in filenames if name.endswith(_GGUF_EXT))
    shard_groups: dict[tuple[str, str], list[tuple[int, str]]] = {}
    singletons: list[str] = []
    for name in ggufs:
        m = _SHARD_RE.match(name)
        if m:
            key = (m.group("base"), m.group("total"))
            shard_groups.setdefault(key, []).append((int(m.group("idx")), name))
        else:
            singletons.append(name)

    groups: list[GgufGroup] = []
    for (_base, total_str), shards in shard_groups.items():
        shards.sort(key=lambda t: t[0])
        all_names = tuple(name for _idx, name in shards)
        # Use the lowest-indexed shard as primary, regardless of whether the
        # set is complete — the engine wants the first-shard path.
        groups.append(GgufGroup(
            primary_filename=all_names[0],
            all_filenames=all_names,
            shard_count=int(total_str),
        ))
    for name in singletons:
        groups.append(GgufGroup(
            primary_filename=name,
            all_filenames=(name,),
            shard_count=1,
        ))
    groups.sort(key=lambda g: g.primary_filename)
    return groups


def _format_size_label(size_bytes: Optional[int]) -> str:
    """Render `size_bytes` as a short GB string for UI labels. None → empty."""
    if size_bytes is None:
        return ""
    gb = size_bytes / 1e9
    if gb >= 10:
        return f"{gb:.0f} GB"
    return f"{gb:.1f} GB"


def _quant_label_from_filename(filename: str) -> str:
    """Best-effort short label for the dropdown — e.g. 'Q4_K_M' from
    'model-Q4_K_M.gguf'. Falls back to the bare filename when no recognizable
    quant marker is present.
    """
    stem = filename
    if stem.endswith(_GGUF_EXT):
        stem = stem[: -len(_GGUF_EXT)]
    # Strip trailing shard suffix if still present (group may pass primary).
    m = _SHARD_RE.match(filename)
    if m:
        stem = m.group("base")
    # Common quant tokens on community GGUFs: Q4_K_M, Q5_K_S, Q8_0, F16, BF16, IQ3_XXS, ...
    quant_re = re.compile(r"(?P<q>(IQ|Q|F|BF)\d+(?:_\w+)?)")
    matches = list(quant_re.finditer(stem))
    if matches:
        return matches[-1].group("q")
    return filename


def _candidate_for(group: GgufGroup, sizes: dict[str, Optional[int]]) -> GgufCandidate:
    total: Optional[int] = 0
    for name in group.all_filenames:
        sz = sizes.get(name)
        if sz is None:
            total = None
            break
        total += int(sz)
    quant = _quant_label_from_filename(group.primary_filename)
    size_str = _format_size_label(total)
    if group.shard_count > 1:
        if size_str:
            label = f"{quant} (sharded, {group.shard_count} files, {size_str})"
        else:
            label = f"{quant} (sharded, {group.shard_count} files)"
    else:
        label = f"{quant} ({size_str})" if size_str else quant
    return GgufCandidate(
        label=label,
        primary_filename=group.primary_filename,
        all_filenames=group.all_filenames,
        shard_count=group.shard_count,
        size_bytes=total,
    )


def probe_repo_format(siblings: list[SiblingMeta]) -> RepoFormatProbe:
    """Decide the recommended backend and produce the GGUF candidate list.

    Decision rule (deterministic):
      - has_transformer_weights → "vllm" (mixed-format prefers vLLM).
      - has_gguf and not has_transformer_weights → "llama.cpp".
      - Neither → "none". The install endpoint rejects this with 400; HF
        search marks the repo not compatible.
    """
    filenames = [s.rfilename for s in siblings if s.rfilename]
    sizes: dict[str, Optional[int]] = {s.rfilename: s.size for s in siblings if s.rfilename}

    has_gguf = any(name.endswith(_GGUF_EXT) for name in filenames)
    has_transformer = any(name.endswith(_TRANSFORMER_EXTS) for name in filenames)

    if has_transformer:
        backend = BACKEND_VLLM
    elif has_gguf:
        backend = BACKEND_LLAMA_CPP
    else:
        backend = BACKEND_NONE

    candidates: tuple[GgufCandidate, ...] = ()
    if has_gguf:
        groups = group_gguf_filenames(filenames)
        candidates = tuple(_candidate_for(g, sizes) for g in groups)

    return RepoFormatProbe(
        has_gguf=has_gguf,
        has_transformer_weights=has_transformer,
        recommended_backend=backend,
        gguf_candidates=candidates,
    )


def expand_shard_filenames(primary_filename: str, all_filenames: list[str]) -> list[str]:
    """Given a chosen primary GGUF, return the full shard set names that
    *should* exist according to the filename pattern.

    For a sharded primary (`*-NNNNN-of-NNNNN.gguf`), this synthesizes every
    `<base>-MMMMM-of-NNNNN.gguf` for MMMMM in [1..NNNNN] regardless of
    whether each one is present in `all_filenames` — so callers can detect
    missing shards. `all_filenames` is consulted only to filter when the
    pattern is non-canonical (degenerate fallback).

    Returns `[primary_filename]` when the primary isn't sharded.
    """
    m = _SHARD_RE.match(primary_filename)
    if not m:
        return [primary_filename]
    base = m.group("base")
    total = int(m.group("total"))
    return [f"{base}-{i:05d}-of-{total:05d}.gguf" for i in range(1, total + 1)]
