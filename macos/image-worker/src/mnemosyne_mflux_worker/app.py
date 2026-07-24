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

SUPPORTED_MODELS: dict[str, str] = {
    "schnell": "black-forest-labs/FLUX.1-schnell",
    "dev": "black-forest-labs/FLUX.1-dev",
    "krea-dev": "black-forest-labs/FLUX.1-Krea-dev",
    "flux2-klein-4b": "black-forest-labs/FLUX.2-klein-4B",
    "flux2-klein-9b": "black-forest-labs/FLUX.2-klein-9B",
    "flux2-klein-9b-kv": "black-forest-labs/FLUX.2-klein-9b-kv",
    "flux2-klein-base-4b": "black-forest-labs/FLUX.2-klein-base-4B",
    "flux2-klein-base-9b": "black-forest-labs/FLUX.2-klein-base-9B",
    "qwen-image": "Qwen/Qwen-Image",
    "krea-2": _KREA_2_REPO,
    "fibo": "briaai/FIBO",
    "fibo-lite": "briaai/Fibo-lite",
    "z-image": "Tongyi-MAI/Z-Image",
    "z-image-turbo": "Tongyi-MAI/Z-Image-Turbo",
    "ernie-image": "baidu/ERNIE-Image",
    "ernie-image-turbo": "baidu/ERNIE-Image-Turbo",
    "ideogram-4-fp8": "ideogram-ai/ideogram-4-fp8",
}

_MODEL_CONFIG_FACTORIES: dict[str, str] = {
    "schnell": "schnell",
    "dev": "dev",
    "krea-dev": "krea_dev",
    "flux2-klein-4b": "flux2_klein_4b",
    "flux2-klein-9b": "flux2_klein_9b",
    "flux2-klein-9b-kv": "flux2_klein_9b_kv",
    "flux2-klein-base-4b": "flux2_klein_base_4b",
    "flux2-klein-base-9b": "flux2_klein_base_9b",
    "qwen-image": "qwen_image",
    "krea-2": "krea2",
    "fibo": "fibo",
    "fibo-lite": "fibo_lite",
    "z-image": "z_image",
    "z-image-turbo": "z_image_turbo",
    "ernie-image": "ernie_image",
    "ernie-image-turbo": "ernie_image_turbo",
    "ideogram-4-fp8": "ideogram4_fp8",
}

_FLUX_1_FAMILIES = frozenset({"schnell", "dev", "krea-dev"})
_FLUX_2_FAMILIES = frozenset(
    {
        "flux2-klein-4b",
        "flux2-klein-9b",
        "flux2-klein-9b-kv",
        "flux2-klein-base-4b",
        "flux2-klein-base-9b",
    }
)
_NEGATIVE_PROMPT_FAMILIES = frozenset(
    {
        *_FLUX_1_FAMILIES,
        "qwen-image",
        "krea-2",
        "fibo",
        "fibo-lite",
        "z-image",
        "z-image-turbo",
        "ernie-image",
        "ernie-image-turbo",
    }
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
    expected_repo = SUPPORTED_MODELS.get(family)
    if expected_repo is None:
        raise ValueError(f"unsupported MFLUX family '{family}'")
    expanded = Path(model).expanduser()
    local_path = str(expanded) if expanded.exists() else None
    if local_path is None and model != expected_repo:
        raise ValueError(f"{family} supports {expected_repo} or a local model path")
    if family == "krea-2":
        local_path = _resolve_snapshot(
            model,
            supported_repo=_KREA_2_REPO,
            allow_patterns=_KREA_2_PATTERNS,
        )
        from mflux.models.krea2 import Krea2

        return Krea2(quantize=quantize, model_path=local_path)
    from mflux.models.common.config import ModelConfig

    model_config = getattr(ModelConfig, _MODEL_CONFIG_FACTORIES[family])()
    if family in _FLUX_1_FAMILIES:
        from mflux.models.flux.variants.txt2img.flux import Flux1

        return Flux1(
            quantize=quantize,
            model_path=local_path,
            model_config=model_config,
        )
    if family in _FLUX_2_FAMILIES:
        from mflux.models.flux2.variants.txt2img.flux2_klein import Flux2Klein

        return Flux2Klein(
            quantize=quantize,
            model_path=local_path,
            model_config=model_config,
        )
    if family == "qwen-image":
        from mflux.models.qwen.variants.txt2img.qwen_image import QwenImage

        return QwenImage(
            quantize=quantize,
            model_path=local_path,
            model_config=model_config,
        )
    if family in {"fibo", "fibo-lite"}:
        from mflux.models.fibo.variants.txt2img.fibo import FIBO

        return FIBO(
            quantize=quantize,
            model_path=local_path,
            model_config=model_config,
        )
    if family in {"z-image", "z-image-turbo"}:
        from mflux.models.z_image.variants.z_image import ZImage

        return ZImage(
            quantize=quantize,
            model_path=local_path,
            model_config=model_config,
        )
    if family in {"ernie-image", "ernie-image-turbo"}:
        from mflux.models.ernie_image.variants.txt2img.ernie_image import ErnieImage

        return ErnieImage(
            quantize=quantize,
            model_path=local_path,
            model_config=model_config,
        )
    if family == "ideogram-4-fp8":
        from mflux.models.ideogram4.variants.txt2img.ideogram4 import Ideogram4

        return Ideogram4(
            quantize=quantize,
            model_path=local_path,
            model_config=model_config,
        )
    raise AssertionError(f"missing pipeline dispatch for supported family '{family}'")


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
            if (
                payload.negative_prompt is not None
                and state.family in _NEGATIVE_PROMPT_FAMILIES
            ):
                kwargs["negative_prompt"] = payload.negative_prompt
            try:
                generated = await asyncio.to_thread(state.pipeline.generate_image, **kwargs)
                buffer = BytesIO()
                image = getattr(generated, "image", generated)
                image.save(buffer, format="PNG")
            except Exception as exc:
                raise HTTPException(500, f"image generation failed: {exc}") from exc
            return {
                "created": int(time.time()),
                "data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode("ascii")}],
            }

    return app


app = create_app()


__all__ = ["SUPPORTED_MODELS", "app", "create_app", "create_pipeline"]
