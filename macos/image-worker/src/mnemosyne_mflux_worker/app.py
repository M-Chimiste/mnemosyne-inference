"""Private loopback API that owns exactly one MFLUX pipeline."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import gc
from pathlib import Path
import secrets
import time
from typing import Any, Callable, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class ImagePipeline(Protocol):
    def generate_image(self, **kwargs: Any) -> Any: ...


PipelineFactory = Callable[[str, str, int | None], ImagePipeline]

_KREA_2_REPO = "krea/Krea-2-Turbo"
_KREA_2_PATTERNS = (
    "turbo.safetensors",
    "vae/*.safetensors",
    "vae/*.json",
    "text_encoder/*.safetensors",
    "text_encoder/*.json",
    "tokenizer/**",
)


class LoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    model: str
    load_config_digest: str
    family: str
    quantize: int | None = None


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    prompt: str = Field(min_length=1)
    n: int = 1
    width: int = Field(ge=64, le=4096, multiple_of=16)
    height: int = Field(ge=64, le=4096, multiple_of=16)
    num_inference_steps: int = Field(ge=1, le=200)
    guidance_scale: float = Field(ge=0, le=50)
    seed: int | None = Field(default=None, ge=0, le=4_294_967_295)
    negative_prompt: str | None = None
    response_format: str = "b64_json"
    output_format: str = "png"


def _resolve_snapshot(
    model: str,
    *,
    supported_repo: str,
    allow_patterns: tuple[str, ...],
) -> str:
    expanded = Path(model).expanduser()
    if expanded.exists():
        return str(expanded)
    if model != supported_repo:
        raise ValueError(f"model must be {supported_repo} or a local model path")

    # Resolve the snapshot explicitly before handing it to MFLUX. This makes
    # interrupted Hugging Face/Xet transfers resume correctly instead of being
    # mistaken for a complete cached snapshot by upstream path resolution.
    from huggingface_hub import snapshot_download

    return snapshot_download(repo_id=model, allow_patterns=list(allow_patterns))


def create_pipeline(family: str, model: str, quantize: int | None) -> ImagePipeline:
    expanded = Path(model).expanduser()
    local_path = str(expanded) if expanded.exists() else None
    if family == "krea-2":
        local_path = _resolve_snapshot(
            model,
            supported_repo=_KREA_2_REPO,
            allow_patterns=_KREA_2_PATTERNS,
        )
        from mflux.models.krea2 import Krea2

        return Krea2(quantize=quantize, model_path=local_path)
    if family == "qwen-image":
        if local_path is None and model != "Qwen/Qwen-Image":
            raise ValueError("qwen-image supports Qwen/Qwen-Image or a local model path")
        from mflux.models.qwen.variants.txt2img.qwen_image import QwenImage

        return QwenImage(quantize=quantize, model_path=local_path)
    raise ValueError(f"unsupported MFLUX family '{family}'")


class WorkerState:
    def __init__(self, pipeline_factory: PipelineFactory) -> None:
        self.pipeline_factory = pipeline_factory
        self.pipeline: ImagePipeline | None = None
        self.alias: str | None = None
        self.model: str | None = None
        self.family: str | None = None
        self.load_config_digest: str | None = None
        self.lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        return {
            "service": "mnemosyne-mflux-worker",
            "loaded": self.pipeline is not None,
            "alias": self.alias,
            "model": self.model,
            "family": self.family,
            "load_config_digest": self.load_config_digest,
        }

    def clear(self) -> None:
        self.pipeline = None
        self.alias = None
        self.model = None
        self.family = None
        self.load_config_digest = None
        gc.collect()


def create_app(*, pipeline_factory: PipelineFactory = create_pipeline) -> FastAPI:
    app = FastAPI(title="Mnemosyne MFLUX Worker", docs_url=None, redoc_url=None)
    state = WorkerState(pipeline_factory)
    app.state.worker = state

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"service": "mnemosyne-mflux-worker", "status": "ok"}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return state.status()

    @app.post("/load")
    async def load(payload: LoadRequest) -> dict[str, Any]:
        async with state.lock:
            state.clear()
            try:
                pipeline = await asyncio.to_thread(
                    state.pipeline_factory,
                    payload.family,
                    payload.model,
                    payload.quantize,
                )
            except Exception as exc:
                state.clear()
                raise HTTPException(500, f"MFLUX load failed: {exc}") from exc
            state.pipeline = pipeline
            state.alias = payload.alias
            state.model = payload.model
            state.family = payload.family
            state.load_config_digest = payload.load_config_digest
            return state.status()

    @app.post("/unload")
    async def unload() -> dict[str, Any]:
        async with state.lock:
            state.clear()
            return state.status()

    @app.post("/v1/images/generations")
    async def generate(payload: GenerationRequest) -> dict[str, Any]:
        if payload.n != 1:
            raise HTTPException(400, "only n=1 is supported")
        if payload.response_format != "b64_json" or payload.output_format != "png":
            raise HTTPException(400, "only base64 PNG output is supported")
        async with state.lock:
            if state.pipeline is None or state.alias is None:
                raise HTTPException(503, "no image model is loaded")
            if payload.model != state.alias:
                raise HTTPException(400, "request model does not match the loaded alias")
            seed = payload.seed if payload.seed is not None else secrets.randbits(32)
            kwargs: dict[str, Any] = {
                "seed": seed,
                "prompt": payload.prompt,
                "num_inference_steps": payload.num_inference_steps,
                "height": payload.height,
                "width": payload.width,
                "guidance": payload.guidance_scale,
            }
            if payload.negative_prompt is not None:
                kwargs["negative_prompt"] = payload.negative_prompt
            try:
                generated = await asyncio.to_thread(state.pipeline.generate_image, **kwargs)
                buffer = BytesIO()
                generated.image.save(buffer, format="PNG")
            except Exception as exc:
                raise HTTPException(500, f"image generation failed: {exc}") from exc
            return {
                "created": int(time.time()),
                "data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode("ascii")}],
            }

    return app


app = create_app()


__all__ = ["app", "create_app", "create_pipeline"]
