from __future__ import annotations

import json
from textwrap import dedent

import httpx
import pytest
from fastapi.testclient import TestClient

import config as config_mod
from catalog import _has_expected_weights
from image_api import ImageRequestError, normalize_image_request
from profiles import resolve_profile
from runtime import build_sglang_diffusion_argv
import vllm_manager
from tests.conftest import _reset_globals, _running_lifespan


def test_image_request_is_bounded_and_maps_qwen_guidance() -> None:
    body = normalize_image_request(
        json.dumps(
            {
                "model": "qwen-image",
                "prompt": "a lighthouse in a storm",
                "size": "1536x1024",
                "seed": 7,
                "guidance_scale": 3.5,
            }
        ).encode(),
        wire_model="Qwen/Qwen-Image",
        defaults={"num_inference_steps": 30, "guidance_parameter": "true_cfg_scale"},
        max_pixels=4_194_304,
    )
    payload = json.loads(body)
    assert payload["model"] == "Qwen/Qwen-Image"
    assert payload["width"] == 1536
    assert payload["height"] == 1024
    assert payload["true_cfg_scale"] == 3.5
    assert "guidance_scale" not in payload


@pytest.mark.parametrize(
    "override, message",
    [
        ({"n": 2}, "only n=1"),
        ({"n": 1.0}, "only n=1"),
        ({"size": "1000x1000"}, "multiples of 16"),
        ({"size": "4096x4096"}, "limit is"),
        ({"response_format": "url"}, "b64_json"),
        ({"stream": True}, "streaming"),
    ],
)
def test_image_request_rejects_unsupported_shapes(override: dict, message: str) -> None:
    request = {"model": "image", "prompt": "test", **override}
    with pytest.raises(ImageRequestError, match=message):
        normalize_image_request(
            json.dumps(request).encode(),
            wire_model="image",
            defaults={},
            max_pixels=4_194_304,
        )


def test_cuda_image_profile_and_sglang_argv(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_mod, "gpu_indices_or_none", lambda: None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        dedent(
            f"""
            storage:
              default: tmp
              locations:
                - name: tmp
                  path: {tmp_path}
            models:
              - alias: qwen-image
                model: Qwen/Qwen-Image
                backend: sglang-diffusion
                kind: image
                gpus: [0]
                image:
                  num_inference_steps: 30
                  guidance_scale: 4
                  guidance_parameter: true_cfg_scale
            """
        )
    )
    config = config_mod.load_config(config_path)
    profile = resolve_profile("qwen-image", config)
    assert profile.kind == "image"
    assert profile.capabilities == ("images.generations",)
    argv = build_sglang_diffusion_argv(
        profile,
        host="127.0.0.1",
        port=8002,
        num_gpus=1,
        bin_path="/opt/sglang/bin/sglang",
    )
    assert argv == [
        "/opt/sglang/bin/sglang",
        "serve",
        "--model-path",
        "Qwen/Qwen-Image",
        "--host",
        "127.0.0.1",
        "--port",
        "8002",
        "--num-gpus",
        "1",
    ]


def test_sglang_reconcile_finds_nested_diffusers_weights(tmp_path) -> None:
    component = tmp_path / "transformer"
    component.mkdir()
    (component / "diffusion_pytorch_model.safetensors").write_bytes(b"weights")
    assert _has_expected_weights(tmp_path, "sglang-diffusion", None) is True


def test_cuda_images_route_uses_profile_defaults_without_token_usage(
    tmp_paths,
    monkeypatch,
) -> None:
    config_path = tmp_paths / "config.yaml"
    config_path.write_text(
        dedent(
            f"""
            server:
              idle_unload_seconds: null
            storage:
              default: tmp
              locations:
                - name: tmp
                  path: {tmp_paths}
            models:
              - alias: qwen-image
                model: Qwen/Qwen-Image
                backend: sglang-diffusion
                kind: image
                gpus: [0]
                image:
                  num_inference_steps: 12
                  guidance_scale: 2.5
                  guidance_parameter: true_cfg_scale
            """
        )
    )
    seen: dict = {}

    async def fake_start(profile):
        vllm_manager._runtime.resident_alias = profile.alias
        vllm_manager._runtime.resident_profile = profile

    async def fake_upstream(request, path, body, **kwargs):
        seen["path"] = path
        seen["body"] = json.loads(body)
        seen["timeout"] = kwargs.get("timeout")
        client = httpx.AsyncClient()
        response = httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": "cG5n"}]},
            request=httpx.Request("POST", "http://inner/v1/images/generations"),
        )
        return client, response

    monkeypatch.setattr(vllm_manager, "_start_engine", fake_start)
    monkeypatch.setattr(vllm_manager, "_open_upstream", fake_upstream)
    _reset_globals()
    with _running_lifespan():
        with TestClient(vllm_manager.inference_app) as client:
            response = client.post(
                "/v1/images/generations",
                json={"model": "qwen-image", "prompt": "local image", "seed": 9},
            )
            assert response.status_code == 200
            assert response.json()["data"][0]["b64_json"] == "cG5n"
            assert seen["path"] == "v1/images/generations"
            assert seen["body"]["model"] == "Qwen/Qwen-Image"
            assert seen["body"]["num_inference_steps"] == 12
            assert seen["body"]["true_cfg_scale"] == 2.5
            assert seen["timeout"] == 1800.0
            assert not vllm_manager._runtime.usage_rows


def test_sglang_install_persists_image_capability(client, stub_downloader) -> None:
    response = client.post(
        "/manager/install",
        json={
            "alias": "krea-image",
            "model": "krea/Krea-2-Turbo",
            "backend": "sglang-diffusion",
            "kind": "image",
            "gpus": [0],
            "extra_args": [],
            "image": {
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 8,
                "guidance_scale": 1,
            },
        },
    )
    assert response.status_code == 202, response.text
    row = client.get("/manager/catalog").json()["models"][0]
    assert row["backend"] == "sglang-diffusion"
    assert row["model_kind"] == "image"
    assert row["capabilities"] == ["images.generations"]
    assert row["image_config"]["num_inference_steps"] == 8
