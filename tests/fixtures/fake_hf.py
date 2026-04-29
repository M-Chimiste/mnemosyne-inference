"""Reusable fakes for HfApi + hf_hub_download in offline tests.

Phase 5 search tests, future install/UI tests, etc. all need to drive
`huggingface_hub` without hitting the network. This module centralizes
the stubs so they don't drift between suites.

Usage in a test:

    from tests.fixtures.fake_hf import FakeHfApi, FakeModelInfo, install_fakes

    fake_api = FakeHfApi(
        list_results={
            "text-generation": [
                FakeModelInfo("Qwen/Qwen2.5-7B", downloads=1000),
            ],
        },
        configs={"Qwen/Qwen2.5-7B": {"architectures": ["Qwen2ForCausalLM"]}},
    )
    install_fakes(monkeypatch, fake_api)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FakeSibling:
    rfilename: str
    size: Optional[int]


@dataclass
class FakeModelInfo:
    id: str
    sha: Optional[str] = None
    downloads: int = 0
    likes: int = 0
    last_modified: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    pipeline_tag: Optional[str] = "text-generation"
    siblings: list[FakeSibling] = field(default_factory=list)


class FakeHfApi:
    """Replacement for huggingface_hub.HfApi with the only methods hf_search
    uses. Captures call kwargs so tests can assert on them."""

    def __init__(
        self,
        *,
        list_results: Optional[dict[str, list[FakeModelInfo]]] = None,
        list_error: Optional[Exception] = None,
        model_info_map: Optional[dict[str, FakeModelInfo]] = None,
        model_info_error: Optional[Exception] = None,
    ):
        self.list_results = list_results or {}
        self.list_error = list_error
        self.model_info_map = model_info_map or {}
        self.model_info_error = model_info_error
        self.list_calls: list[dict] = []
        self.model_info_calls: list[dict] = []

    def list_models(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.list_error is not None:
            raise self.list_error
        tag = kwargs.get("pipeline_tag")
        return list(self.list_results.get(tag, []))

    def model_info(self, repo_id: str, **kwargs):
        self.model_info_calls.append({"repo_id": repo_id, **kwargs})
        if self.model_info_error is not None:
            raise self.model_info_error
        if repo_id in self.model_info_map:
            return self.model_info_map[repo_id]

        # Default: return a FakeModelInfo whose siblings line up with any
        # FakeModelInfo we put in list_results so size estimation has
        # something to sum.
        for rows in self.list_results.values():
            for m in rows:
                if m.id == repo_id:
                    return m
        return FakeModelInfo(id=repo_id)


def make_fake_download_factory(
    configs: dict[str, dict],
    errors: Optional[dict[str, Exception]] = None,
    calls: Optional[list[dict]] = None,
):
    """Build a callable that mimics huggingface_hub.hf_hub_download for
    config.json fetches. `configs` maps repo_id → parsed-JSON dict; `errors`
    maps repo_id → exception to raise instead.
    """
    errors = errors or {}

    def fake_download(*, repo_id: str, filename: str, cache_dir=None, token=None, **kw):
        if calls is not None:
            calls.append({
                "repo_id": repo_id,
                "filename": filename,
                "cache_dir": cache_dir,
                "token": token,
                **kw,
            })
        if filename != "config.json":
            raise AssertionError(f"unexpected filename {filename!r}")
        if repo_id in errors:
            raise errors[repo_id]
        if repo_id not in configs:
            from huggingface_hub.utils import EntryNotFoundError
            raise EntryNotFoundError(f"no config for {repo_id}")
        # Write the config to a tmp file under cache_dir/scratch and return
        # the path.
        scratch_root = cache_dir or "/tmp"
        scratch = os.path.join(scratch_root, "_hf_search_test_scratch")
        os.makedirs(scratch, exist_ok=True)
        # Use repo_id (sanitized) so two repos in one test don't collide.
        safe = repo_id.replace("/", "__")
        path = os.path.join(scratch, f"{safe}-config.json")
        with open(path, "w") as f:
            json.dump(configs[repo_id], f)
        return path

    return fake_download


def install_fakes(
    monkeypatch,
    fake_api: FakeHfApi,
    *,
    configs: Optional[dict[str, dict]] = None,
    config_errors: Optional[dict[str, Exception]] = None,
):
    """Patch hf_search._api and hf_search.hf_hub_download for the duration
    of a test. `configs` defaults to the per-row architectures pulled from
    fake_api.list_results so simple cases don't need to specify them twice.
    """
    import hf_search

    monkeypatch.setattr(hf_search, "_api", fake_api)

    download_calls: list[dict] = []
    cfg_map: dict[str, dict] = {}
    if configs:
        cfg_map.update(configs)
    fake_download = make_fake_download_factory(
        cfg_map,
        errors=config_errors,
        calls=download_calls,
    )
    fake_download.calls = download_calls
    fake_download.configs = cfg_map
    monkeypatch.setattr(hf_search, "hf_hub_download", fake_download)
    # Wipe per-process cache between tests so cross-test pollution is impossible.
    hf_search._clear_config_cache()
    return fake_download
