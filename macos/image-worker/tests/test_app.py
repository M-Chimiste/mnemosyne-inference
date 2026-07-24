from __future__ import annotations

import base64
import importlib
from pathlib import Path

import httpx
import pytest

from mnemosyne_mflux_worker.app import SUPPORTED_MODELS, _resolve_snapshot, create_app, create_pipeline


class FakeImage:
    def save(self, output, *, format: str) -> None:
        assert format == "PNG"
        output.write(b"fake-png")


class FakeGenerated:
    image = FakeImage()


class FakePipeline:
    def __init__(self) -> None:
        self.kwargs = None

    def generate_image(self, **kwargs):
        self.kwargs = kwargs
        return FakeGenerated()


class FakeDirectImagePipeline(FakePipeline):
    def generate_image(self, **kwargs):
        self.kwargs = kwargs
        return FakeImage()


_PIPELINE_CLASSES = {
    "schnell": ("mflux.models.flux.variants.txt2img.flux", "Flux1"),
    "dev": ("mflux.models.flux.variants.txt2img.flux", "Flux1"),
    "krea-dev": ("mflux.models.flux.variants.txt2img.flux", "Flux1"),
    "flux2-klein-4b": (
        "mflux.models.flux2.variants.txt2img.flux2_klein",
        "Flux2Klein",
    ),
    "flux2-klein-9b": (
        "mflux.models.flux2.variants.txt2img.flux2_klein",
        "Flux2Klein",
    ),
    "flux2-klein-9b-kv": (
        "mflux.models.flux2.variants.txt2img.flux2_klein",
        "Flux2Klein",
    ),
    "flux2-klein-base-4b": (
        "mflux.models.flux2.variants.txt2img.flux2_klein",
        "Flux2Klein",
    ),
    "flux2-klein-base-9b": (
        "mflux.models.flux2.variants.txt2img.flux2_klein",
        "Flux2Klein",
    ),
    "qwen-image": (
        "mflux.models.qwen.variants.txt2img.qwen_image",
        "QwenImage",
    ),
    "krea-2": ("mflux.models.krea2", "Krea2"),
    "fibo": ("mflux.models.fibo.variants.txt2img.fibo", "FIBO"),
    "fibo-lite": ("mflux.models.fibo.variants.txt2img.fibo", "FIBO"),
    "z-image": ("mflux.models.z_image.variants.z_image", "ZImage"),
    "z-image-turbo": ("mflux.models.z_image.variants.z_image", "ZImage"),
    "ernie-image": (
        "mflux.models.ernie_image.variants.txt2img.ernie_image",
        "ErnieImage",
    ),
    "ernie-image-turbo": (
        "mflux.models.ernie_image.variants.txt2img.ernie_image",
        "ErnieImage",
    ),
    "ideogram-4-fp8": (
        "mflux.models.ideogram4.variants.txt2img.ideogram4",
        "Ideogram4",
    ),
}


@pytest.mark.parametrize("family", sorted(SUPPORTED_MODELS))
def test_every_advertised_family_dispatches_to_a_bundled_pipeline(
    family: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_name, class_name = _PIPELINE_CLASSES[family]
    module = importlib.import_module(module_name)
    calls = []

    class FakeConstructor:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(module, class_name, FakeConstructor)
    pipeline = create_pipeline(family, str(tmp_path), 8)

    assert isinstance(pipeline, FakeConstructor)
    assert calls[0]["model_path"] == str(tmp_path)
    assert calls[0]["quantize"] == 8
    if family != "krea-2":
        assert calls[0]["model_config"].model_name == SUPPORTED_MODELS[family]


def test_supported_model_catalog_does_not_claim_krea_raw() -> None:
    assert "krea/Krea-2-Raw" not in SUPPORTED_MODELS.values()


def test_resolve_snapshot_resumes_supported_huggingface_model(monkeypatch) -> None:
    calls = []

    def fake_snapshot_download(*, repo_id, allow_patterns):
        calls.append((repo_id, allow_patterns))
        return "/cache/complete-snapshot"

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_snapshot_download)

    assert _resolve_snapshot(
        "krea/Krea-2-Turbo",
        supported_repo="krea/Krea-2-Turbo",
        allow_patterns=("turbo.safetensors", "vae/*.safetensors"),
    ) == "/cache/complete-snapshot"
    assert calls == [
        (
            "krea/Krea-2-Turbo",
            ["turbo.safetensors", "vae/*.safetensors"],
        )
    ]


def test_resolve_snapshot_uses_existing_local_path(tmp_path: Path) -> None:
    assert _resolve_snapshot(
        str(tmp_path),
        supported_repo="krea/Krea-2-Turbo",
        allow_patterns=("turbo.safetensors",),
    ) == str(tmp_path)


@pytest.mark.asyncio
async def test_worker_load_generate_and_unload() -> None:
    pipeline = FakePipeline()
    app = create_app(pipeline_factory=lambda family, model, quantize: pipeline)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker.test",
    ) as client:
        loaded = await client.post(
            "/load",
            json={
                "alias": "krea",
                "model": "krea/Krea-2-Turbo",
                "load_config_digest": "digest",
                "family": "krea-2",
                "quantize": 8,
            },
        )
        assert loaded.status_code == 200
        generated = await client.post(
            "/v1/images/generations",
            json={
                "model": "krea",
                "prompt": "a test",
                "n": 1,
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 8,
                "guidance_scale": 1,
                "seed": 42,
                "response_format": "b64_json",
                "output_format": "png",
            },
        )
        assert generated.status_code == 200
        assert base64.b64decode(generated.json()["data"][0]["b64_json"]) == b"fake-png"
        assert pipeline.kwargs == {
            "seed": 42,
            "prompt": "a test",
            "num_inference_steps": 8,
            "height": 1024,
            "width": 1024,
            "guidance": 1.0,
        }
        unloaded = await client.post("/unload")
        assert unloaded.json()["loaded"] is False


@pytest.mark.asyncio
async def test_worker_rejects_multiple_images() -> None:
    app = create_app(pipeline_factory=lambda family, model, quantize: FakePipeline())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker.test"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            json={
                "model": "x",
                "prompt": "x",
                "n": 2,
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 8,
                "guidance_scale": 1,
            },
        )
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_worker_accepts_pipelines_that_return_a_pil_image_directly() -> None:
    app = create_app(
        pipeline_factory=lambda family, model, quantize: FakeDirectImagePipeline()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker.test"
    ) as client:
        assert (
            await client.post(
                "/load",
                json={
                    "alias": "z-image",
                    "model": "Tongyi-MAI/Z-Image-Turbo",
                    "load_config_digest": "digest",
                    "family": "z-image-turbo",
                    "quantize": 8,
                },
            )
        ).status_code == 200
        response = await client.post(
            "/v1/images/generations",
            json={
                "model": "z-image",
                "prompt": "a test",
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 9,
                "guidance_scale": 0,
            },
        )
        assert response.status_code == 200
        assert base64.b64decode(response.json()["data"][0]["b64_json"]) == b"fake-png"
