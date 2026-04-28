"""Phase 1 — reload endpoint, new read endpoints, cache-only filter."""
from __future__ import annotations

import json
from textwrap import dedent

import pytest

import vllm_manager


# ── helpers ──────────────────────────────────────────────────────────

def _cfg_with_models(tmp_path, *models_yaml: str) -> str:
    body = "\n".join(f"  - {m}" if not m.startswith("- ") else f"  {m}" for m in models_yaml) if models_yaml else ""
    return dedent(f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_path}
        models:
        """) + (body + "\n" if models_yaml else "")


def _write_cfg(tmp_paths, contents: str) -> None:
    (tmp_paths / "config.yaml").write_text(contents)


# ── reload endpoint ─────────────────────────────────────────────────

def test_reload_with_valid_config_returns_counts(client, tmp_paths):
    _write_cfg(tmp_paths, dedent(f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_paths}
        models:
          - alias: qw
            model: org/qw
    """))
    r = client.post("/manager/reload")
    assert r.status_code == 200
    body = r.json()
    assert body["sync"]["added"] == 1
    assert body["sync"]["updated"] == 0
    assert "reconcile" in body


def test_reload_with_malformed_config_keeps_old(client, tmp_paths):
    initial = client.get("/manager/profiles").json()["profiles"]
    assert initial == []

    # Schema-violating: storage.default references unknown location.
    _write_cfg(tmp_paths, dedent(f"""\
        storage:
          default: missing
          locations:
            - name: foo
              path: {tmp_paths}
    """))
    r = client.post("/manager/reload")
    assert r.status_code == 400

    after = client.get("/manager/profiles").json()["profiles"]
    assert after == initial


def test_reload_when_apply_config_raises_keeps_old_config_and_catalog(client, tmp_paths, monkeypatch):
    """Mocked apply_config failure must NOT swap _config or commit DB changes."""
    original_config = vllm_manager._config
    assert original_config is not None
    before_rows = [row.to_api_dict() for row in vllm_manager._catalog.list_models()]

    _write_cfg(tmp_paths, dedent(f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_paths}
        models:
          - alias: changed
            model: org/changed
    """))

    def boom(*a, **kw):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(vllm_manager._catalog, "apply_config", boom)
    r = client.post("/manager/reload")
    assert r.status_code == 400
    assert "simulated db failure" in r.json()["detail"]
    # Critical: _config must still be the original.
    assert vllm_manager._config is original_config
    assert [row.to_api_dict() for row in vllm_manager._catalog.list_models()] == before_rows


# ── /manager/profiles ───────────────────────────────────────────────

def test_profiles_empty_initially(client):
    body = client.get("/manager/profiles").json()
    assert body == {"profiles": []}


def test_profiles_after_reload(client, tmp_paths):
    _write_cfg(tmp_paths, dedent(f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_paths}
        models:
          - alias: qw
            model: org/qw
            quantization: awq
            gpus: [0]
            max_model_len: 1024
            extra_args:
              - --foo
    """))
    client.post("/manager/reload")
    body = client.get("/manager/profiles").json()
    assert body == {
        "profiles": [
            {
                "alias": "qw",
                "model": "org/qw",
                "quantization": "awq",
                "gpus": [0],
                "storage": "tmp",
                "max_model_len": 1024,
                "extra_args": ["--foo"],
            }
        ]
    }


def test_profiles_excludes_synthetic_cache_rows(client, tmp_paths):
    """Even with synthetic rows in the catalog, /manager/profiles only
    reflects YAML-declared aliases."""
    vllm_manager._catalog._raw_insert_model(
        alias="__cache__:abcdef0123456789",
        hf_model_id="org/cached",
        source="ui_install",
        gpus='"all"',
        storage_location="tmp",
    )
    body = client.get("/manager/profiles").json()
    assert body == {"profiles": []}


# ── /manager/storage ────────────────────────────────────────────────

def test_storage_lists_locations(client, tmp_paths):
    body = client.get("/manager/storage").json()
    locs = body["locations"]
    assert len(locs) == 1
    loc = locs[0]
    assert loc["name"] == "tmp"
    assert loc["path"] == str(tmp_paths)
    assert isinstance(loc["free_bytes"], int) and loc["free_bytes"] > 0
    assert isinstance(loc["total_bytes"], int) and loc["total_bytes"] > 0
    assert loc["writable"] is True
    assert loc["is_default"] is True


def test_storage_is_default_set_on_exactly_one_location(client, tmp_paths):
    other = tmp_paths / "other"
    other.mkdir()
    _write_cfg(tmp_paths, dedent(f"""\
        storage:
          default: a
          locations:
            - name: a
              path: {tmp_paths}
            - name: b
              path: {other}
    """))
    client.post("/manager/reload")
    body = client.get("/manager/storage").json()
    flags = [loc["is_default"] for loc in body["locations"]]
    assert flags.count(True) == 1


# ── /manager/catalog with include_cache_only filter ─────────────────

def _seed_catalog_with_cache_only(client, tmp_paths):
    """Sync one config alias; insert one synthetic row directly."""
    _write_cfg(tmp_paths, dedent(f"""\
        storage:
          default: tmp
          locations:
            - name: tmp
              path: {tmp_paths}
        models:
          - alias: qw
            model: org/qw
    """))
    client.post("/manager/reload")
    vllm_manager._catalog._raw_insert_model(
        alias="__cache__:abcdef0123456789",
        hf_model_id="org/cached",
        source="ui_install",
        gpus='"all"',
        storage_location="tmp",
    )


def test_catalog_default_excludes_cache_only(client, tmp_paths):
    _seed_catalog_with_cache_only(client, tmp_paths)
    body = client.get("/manager/catalog").json()
    aliases = [m["alias"] for m in body["models"]]
    assert aliases == ["qw"]


def test_catalog_explicit_false_excludes_cache_only(client, tmp_paths):
    _seed_catalog_with_cache_only(client, tmp_paths)
    body = client.get("/manager/catalog?include_cache_only=false").json()
    aliases = [m["alias"] for m in body["models"]]
    assert aliases == ["qw"]


def test_catalog_include_cache_only_true(client, tmp_paths):
    _seed_catalog_with_cache_only(client, tmp_paths)
    body = client.get("/manager/catalog?include_cache_only=true").json()
    aliases = sorted(m["alias"] for m in body["models"])
    assert aliases == ["__cache__:abcdef0123456789", "qw"]


def test_catalog_invalid_filter_returns_422(client):
    r = client.get("/manager/catalog?include_cache_only=banana")
    assert r.status_code == 422
    assert "include_cache_only" in r.json()["detail"]


def test_catalog_row_shape(client, tmp_paths):
    _seed_catalog_with_cache_only(client, tmp_paths)
    body = client.get("/manager/catalog").json()
    row = body["models"][0]
    assert set(row.keys()) == {
        "alias", "hf_model_id", "source", "quantization", "gpus",
        "max_model_len", "storage_location", "cache_path", "size_bytes",
        "status", "installed_at", "last_used_at", "request_count", "extra_args",
        "revision", "resolved_sha",
    }
    assert row["alias"] == "qw"
    assert row["hf_model_id"] == "org/qw"
    assert row["source"] == "config"
    assert row["status"] == "partial"
    assert row["request_count"] == 0
    assert row["extra_args"] == []
    assert row["gpus"] == "all"
    assert row["revision"] == "main"
    assert row["resolved_sha"] is None
