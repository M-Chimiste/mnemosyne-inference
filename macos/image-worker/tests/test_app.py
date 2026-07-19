from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from mnemosyne_mflux_worker.app import _resolve_snapshot, create_app


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
