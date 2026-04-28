"""Phase 2 — pure helpers in runtime.py.

Verified contracts (project_docs/plans/phase_2.md §4, §8.1):
  - argv builder: mandatory flags always present, conditional flags gated,
    extra_args appended verbatim last.
  - env builder: gpus='all' explicitly removes inherited CUDA_VISIBLE_DEVICES;
    explicit list sets it. HF_HOME always written. base_env never mutated.
  - tp_size derivation: explicit list wins over default_tp; 'all' uses
    visible_gpus length when probed, default_tp on empty/None.
"""
from __future__ import annotations

import sys

import pytest

from profiles import ResolvedProfile
from runtime import RuntimeState, build_vllm_argv, build_vllm_env, derive_tp_size


def _profile(
    *,
    alias: str = "test-alias",
    model: str = "Qwen/Qwen2.5-7B-Instruct",
    gpus="all",
    quantization=None,
    max_model_len=None,
    gpu_memory_utilization: float = 0.90,
    trust_remote_code: bool = True,
    storage_name: str = "default",
    storage_path: str = "/storage/default",
    extra_args: tuple[str, ...] = (),
) -> ResolvedProfile:
    return ResolvedProfile(
        alias=alias,
        model=model,
        gpus=gpus,
        quantization=quantization,
        max_model_len=max_model_len,
        gpu_memory_utilization=gpu_memory_utilization,
        trust_remote_code=trust_remote_code,
        storage_name=storage_name,
        storage_path=storage_path,
        extra_args=extra_args,
    )


# ── build_vllm_argv ───────────────────────────────────────────────────


def test_argv_mandatory_flags_present():
    argv = build_vllm_argv(_profile(), host="127.0.0.1", port=8001, tp_size=2)
    assert argv[:3] == [sys.executable, "-m", "vllm.entrypoints.openai.api_server"]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "Qwen/Qwen2.5-7B-Instruct"
    assert "--host" in argv and argv[argv.index("--host") + 1] == "127.0.0.1"
    assert "--port" in argv and argv[argv.index("--port") + 1] == "8001"
    assert "--tensor-parallel-size" in argv
    assert argv[argv.index("--tensor-parallel-size") + 1] == "2"
    assert "--gpu-memory-utilization" in argv
    assert argv[argv.index("--gpu-memory-utilization") + 1] == "0.9"
    assert "--disable-log-requests" in argv


def test_argv_trust_remote_code_when_true():
    argv = build_vllm_argv(
        _profile(trust_remote_code=True), host="h", port=1, tp_size=1
    )
    assert "--trust-remote-code" in argv


def test_argv_trust_remote_code_omitted_when_false():
    argv = build_vllm_argv(
        _profile(trust_remote_code=False), host="h", port=1, tp_size=1
    )
    assert "--trust-remote-code" not in argv


def test_argv_quantization_omitted_when_none():
    argv = build_vllm_argv(_profile(quantization=None), host="h", port=1, tp_size=1)
    assert "--quantization" not in argv


def test_argv_quantization_present_when_set():
    argv = build_vllm_argv(_profile(quantization="awq"), host="h", port=1, tp_size=1)
    assert argv[argv.index("--quantization") + 1] == "awq"


def test_argv_max_model_len_omitted_when_none():
    argv = build_vllm_argv(_profile(max_model_len=None), host="h", port=1, tp_size=1)
    assert "--max-model-len" not in argv


def test_argv_max_model_len_present_when_set():
    argv = build_vllm_argv(_profile(max_model_len=32768), host="h", port=1, tp_size=1)
    assert argv[argv.index("--max-model-len") + 1] == "32768"


def test_argv_extra_args_appended_verbatim_last():
    argv = build_vllm_argv(
        _profile(extra_args=("--limit-mm-per-prompt", "image=4")),
        host="h", port=1, tp_size=1,
    )
    assert argv[-2:] == ["--limit-mm-per-prompt", "image=4"]


def test_argv_extra_args_can_override_our_flags():
    # User restating --gpu-memory-utilization 0.5 after our 0.9 — vLLM takes
    # the last occurrence. We emit the user's value LAST so it wins.
    argv = build_vllm_argv(
        _profile(gpu_memory_utilization=0.9, extra_args=("--gpu-memory-utilization", "0.5")),
        host="h", port=1, tp_size=1,
    )
    last_gmu = max(i for i, x in enumerate(argv) if x == "--gpu-memory-utilization")
    assert argv[last_gmu + 1] == "0.5"


# ── build_vllm_env ────────────────────────────────────────────────────


def test_env_all_omits_cvd_when_unset():
    env = build_vllm_env(_profile(gpus="all"), base_env={})
    assert "CUDA_VISIBLE_DEVICES" not in env
    assert env["HF_HOME"] == "/storage/default"


def test_env_all_explicitly_pops_inherited_cvd():
    """The trap from review: base_env may already carry CUDA_VISIBLE_DEVICES
    (set by the container, the manager process, or a parent shell). For
    gpus='all' we MUST remove it so vLLM sees every GPU."""
    base = {"CUDA_VISIBLE_DEVICES": "0", "OTHER": "keep"}
    env = build_vllm_env(_profile(gpus="all"), base_env=base)
    assert "CUDA_VISIBLE_DEVICES" not in env
    assert env["OTHER"] == "keep"


def test_env_explicit_single_gpu():
    env = build_vllm_env(_profile(gpus=[1]), base_env={})
    assert env["CUDA_VISIBLE_DEVICES"] == "1"


def test_env_explicit_multi_gpu():
    env = build_vllm_env(_profile(gpus=[0, 1]), base_env={})
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1"


def test_env_explicit_overrides_inherited_cvd():
    base = {"CUDA_VISIBLE_DEVICES": "9"}
    env = build_vllm_env(_profile(gpus=[0]), base_env=base)
    assert env["CUDA_VISIBLE_DEVICES"] == "0"


def test_env_hf_home_set_to_storage_path():
    env = build_vllm_env(_profile(storage_path="/mnt/nvme/hf"), base_env={})
    assert env["HF_HOME"] == "/mnt/nvme/hf"


def test_env_does_not_mutate_base_env():
    base = {"CUDA_VISIBLE_DEVICES": "0", "FOO": "bar"}
    snapshot = dict(base)
    build_vllm_env(_profile(gpus="all"), base_env=base)
    assert base == snapshot
    build_vllm_env(_profile(gpus=[0, 1]), base_env=base)
    assert base == snapshot


# ── derive_tp_size ────────────────────────────────────────────────────


def test_tp_explicit_list_wins_over_default():
    assert derive_tp_size(_profile(gpus=[0, 1]), visible_gpus=[0, 1, 2, 3], default_tp=4) == 2


def test_tp_explicit_single_gpu():
    assert derive_tp_size(_profile(gpus=[1]), visible_gpus=[0, 1], default_tp=4) == 1


def test_tp_all_uses_visible_count():
    assert derive_tp_size(_profile(gpus="all"), visible_gpus=[0, 1], default_tp=4) == 2


def test_tp_all_falls_back_when_probe_none():
    assert derive_tp_size(_profile(gpus="all"), visible_gpus=None, default_tp=4) == 4


def test_tp_all_falls_back_when_probe_empty():
    """Empty list from probe is treated the same as None — no GPUs visible
    means we can't compute an honest tp from the probe."""
    assert derive_tp_size(_profile(gpus="all"), visible_gpus=[], default_tp=4) == 4


# ── RuntimeState ──────────────────────────────────────────────────────


def test_runtime_state_defaults_are_empty():
    rs = RuntimeState()
    assert rs.resident_alias is None
    assert rs.resident_profile is None
    assert rs.resident_tp_size is None
    assert rs.model_load_time is None
    assert rs.last_used_at is None
    assert rs.request_count_delta == 0
    assert rs.inflight == 0
