"""Pure-function tests for repo_probe — GGUF grouping + format probe."""
from __future__ import annotations

from repo_probe import (
    BACKEND_LLAMA_CPP,
    BACKEND_NONE,
    BACKEND_VLLM,
    SiblingMeta,
    expand_shard_filenames,
    group_gguf_filenames,
    probe_repo_format,
)


# ── group_gguf_filenames ─────────────────────────────────────────────


def test_group_single_gguf_file():
    groups = group_gguf_filenames(["model-Q4_K_M.gguf"])
    assert len(groups) == 1
    g = groups[0]
    assert g.primary_filename == "model-Q4_K_M.gguf"
    assert g.all_filenames == ("model-Q4_K_M.gguf",)
    assert g.shard_count == 1


def test_group_multiple_singletons():
    groups = group_gguf_filenames([
        "model-Q4_K_M.gguf", "model-Q5_K_S.gguf", "model-Q8_0.gguf",
    ])
    primaries = [g.primary_filename for g in groups]
    # alpha sorted by primary
    assert primaries == sorted(primaries)
    assert all(g.shard_count == 1 for g in groups)


def test_group_three_shards():
    files = [
        "model-Q8_0-00001-of-00003.gguf",
        "model-Q8_0-00002-of-00003.gguf",
        "model-Q8_0-00003-of-00003.gguf",
    ]
    groups = group_gguf_filenames(files)
    assert len(groups) == 1
    g = groups[0]
    assert g.shard_count == 3
    assert g.primary_filename == "model-Q8_0-00001-of-00003.gguf"
    assert list(g.all_filenames) == sorted(files)


def test_group_mixed_singletons_and_shards():
    files = [
        "model-Q4_K_M.gguf",
        "model-Q8_0-00001-of-00002.gguf",
        "model-Q8_0-00002-of-00002.gguf",
    ]
    groups = group_gguf_filenames(files)
    assert len(groups) == 2
    # Singletons + shard groups all sorted by primary filename.
    sharded = next(g for g in groups if g.shard_count > 1)
    single = next(g for g in groups if g.shard_count == 1)
    assert sharded.shard_count == 2
    assert single.primary_filename == "model-Q4_K_M.gguf"


def test_group_drops_non_gguf_filenames():
    files = ["model.safetensors", "config.json", "model-Q4_K_M.gguf"]
    groups = group_gguf_filenames(files)
    assert len(groups) == 1
    assert groups[0].primary_filename == "model-Q4_K_M.gguf"


def test_group_partial_shard_set_still_grouped():
    """A repo with a missing shard should still group; reconcile catches the
    missing-on-disk case at install time."""
    files = [
        "model-Q4_K_M-00001-of-00003.gguf",
        "model-Q4_K_M-00003-of-00003.gguf",
    ]
    groups = group_gguf_filenames(files)
    assert len(groups) == 1
    g = groups[0]
    # The pure grouper records the shard_count from the filename pattern.
    assert g.shard_count == 3
    assert g.primary_filename == "model-Q4_K_M-00001-of-00003.gguf"
    assert len(g.all_filenames) == 2


# ── probe_repo_format ────────────────────────────────────────────────


def test_probe_gguf_only_recommends_llama_cpp():
    siblings = [
        SiblingMeta("README.md", 100),
        SiblingMeta("model-Q4_K_M.gguf", 4_400_000_000),
        SiblingMeta("model-Q8_0.gguf", 8_000_000_000),
    ]
    probe = probe_repo_format(siblings)
    assert probe.has_gguf is True
    assert probe.has_transformer_weights is False
    assert probe.recommended_backend == BACKEND_LLAMA_CPP
    primaries = sorted(c.primary_filename for c in probe.gguf_candidates)
    assert primaries == ["model-Q4_K_M.gguf", "model-Q8_0.gguf"]


def test_probe_transformer_only_recommends_vllm():
    siblings = [
        SiblingMeta("model-00001-of-00002.safetensors", 5_000_000_000),
        SiblingMeta("model-00002-of-00002.safetensors", 5_000_000_000),
        SiblingMeta("config.json", 1024),
    ]
    probe = probe_repo_format(siblings)
    assert probe.has_gguf is False
    assert probe.has_transformer_weights is True
    assert probe.recommended_backend == BACKEND_VLLM
    assert probe.gguf_candidates == ()


def test_probe_mixed_format_prefers_vllm():
    """has_transformer_weights wins over has_gguf — mixed-format repos
    default to vLLM, but the GGUF candidate list is still populated so
    users can override on install."""
    siblings = [
        SiblingMeta("model.safetensors", 5_000_000_000),
        SiblingMeta("model-Q4_K_M.gguf", 4_000_000_000),
    ]
    probe = probe_repo_format(siblings)
    assert probe.has_gguf is True
    assert probe.has_transformer_weights is True
    assert probe.recommended_backend == BACKEND_VLLM
    assert len(probe.gguf_candidates) == 1


def test_probe_empty_repo_recommends_none():
    siblings = [SiblingMeta("README.md", 50), SiblingMeta("config.json", 100)]
    probe = probe_repo_format(siblings)
    assert probe.recommended_backend == BACKEND_NONE
    assert probe.gguf_candidates == ()


def test_probe_sized_candidate_label():
    siblings = [SiblingMeta("model-Q4_K_M.gguf", 4_400_000_000)]
    probe = probe_repo_format(siblings)
    cand = probe.gguf_candidates[0]
    assert cand.size_bytes == 4_400_000_000
    assert "Q4_K_M" in cand.label
    assert "GB" in cand.label


def test_probe_missing_size_propagates_none():
    siblings = [SiblingMeta("model-Q4_K_M.gguf", None)]
    probe = probe_repo_format(siblings)
    cand = probe.gguf_candidates[0]
    assert cand.size_bytes is None
    # Label should not contain a size suffix when size is None.
    assert "GB" not in cand.label


def test_probe_sharded_candidate_total_bytes():
    siblings = [
        SiblingMeta("model-Q8_0-00001-of-00002.gguf", 6_000_000_000),
        SiblingMeta("model-Q8_0-00002-of-00002.gguf", 6_000_000_000),
    ]
    probe = probe_repo_format(siblings)
    assert len(probe.gguf_candidates) == 1
    cand = probe.gguf_candidates[0]
    assert cand.shard_count == 2
    assert cand.size_bytes == 12_000_000_000
    assert "sharded" in cand.label


def test_probe_sharded_candidate_partial_size_falls_back_to_none():
    """If any shard's size is unknown, the total reports as None — better to
    skip the precheck than to under-report."""
    siblings = [
        SiblingMeta("model-Q8_0-00001-of-00002.gguf", 6_000_000_000),
        SiblingMeta("model-Q8_0-00002-of-00002.gguf", None),
    ]
    probe = probe_repo_format(siblings)
    cand = probe.gguf_candidates[0]
    assert cand.size_bytes is None


# ── expand_shard_filenames ───────────────────────────────────────────


def test_expand_unsharded_returns_singleton():
    shards = expand_shard_filenames(
        "model-Q4_K_M.gguf",
        ["model-Q4_K_M.gguf", "config.json"],
    )
    assert shards == ["model-Q4_K_M.gguf"]


def test_expand_finds_full_shard_set():
    repo = [
        "model-Q8_0-00001-of-00003.gguf",
        "model-Q8_0-00002-of-00003.gguf",
        "model-Q8_0-00003-of-00003.gguf",
        "model-Q4_K_M.gguf",  # different group, must not mix in
    ]
    shards = expand_shard_filenames("model-Q8_0-00001-of-00003.gguf", repo)
    assert shards == [
        "model-Q8_0-00001-of-00003.gguf",
        "model-Q8_0-00002-of-00003.gguf",
        "model-Q8_0-00003-of-00003.gguf",
    ]


def test_expand_synthesizes_missing_shards():
    """expand_shard_filenames is the *expected* shard set, regardless of
    what's actually in the supplied list — that's how reconcile detects
    missing-on-disk shards."""
    shards = expand_shard_filenames(
        "model-Q8_0-00001-of-00003.gguf",
        ["model-Q8_0-00001-of-00003.gguf"],
    )
    assert shards == [
        "model-Q8_0-00001-of-00003.gguf",
        "model-Q8_0-00002-of-00003.gguf",
        "model-Q8_0-00003-of-00003.gguf",
    ]
