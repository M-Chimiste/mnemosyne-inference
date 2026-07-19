"""Validation and normalization for the local OpenAI-compatible Images API."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


class ImageRequestError(ValueError):
    pass


_SIZE_RE = re.compile(r"^(\d{2,4})x(\d{2,4})$")


def _integer(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ImageRequestError(f"'{field}' must be an integer")
    if value < minimum or value > maximum:
        raise ImageRequestError(
            f"'{field}' must be between {minimum} and {maximum}"
        )
    return value


def _number(value: Any, *, field: str, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ImageRequestError(f"'{field}' must be a number")
    result = float(value)
    if result < minimum or result > maximum:
        raise ImageRequestError(
            f"'{field}' must be between {minimum:g} and {maximum:g}"
        )
    return result


def normalize_image_request(
    body: bytes,
    *,
    wire_model: str,
    defaults: Mapping[str, Any],
    max_pixels: int,
) -> bytes:
    """Return a bounded, canonical JSON request accepted by both engines."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ImageRequestError("request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ImageRequestError("request body must be a JSON object")

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ImageRequestError("request body must contain a non-empty 'model' string")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageRequestError("request body must contain a non-empty 'prompt' string")

    n = payload.get("n", 1)
    if not isinstance(n, int) or isinstance(n, bool) or n != 1:
        raise ImageRequestError("only n=1 is supported")
    if payload.get("stream") not in (None, False):
        raise ImageRequestError("streaming image generation is not supported")
    response_format = payload.get("response_format", "b64_json")
    if response_format != "b64_json":
        raise ImageRequestError("only response_format='b64_json' is supported")
    output_format = payload.get("output_format", "png")
    if output_format not in (None, "png"):
        raise ImageRequestError("only PNG image output is supported")

    default_width = int(defaults.get("width", 1024))
    default_height = int(defaults.get("height", 1024))
    size = payload.get("size")
    if size is None:
        width = payload.get("width", default_width)
        height = payload.get("height", default_height)
    else:
        if not isinstance(size, str) or (match := _SIZE_RE.fullmatch(size)) is None:
            raise ImageRequestError("'size' must use WIDTHxHEIGHT format")
        width, height = int(match.group(1)), int(match.group(2))
        if payload.get("width") not in (None, width) or payload.get("height") not in (None, height):
            raise ImageRequestError("'size' conflicts with explicit width or height")
    width = _integer(width, field="width", minimum=64, maximum=4096)
    height = _integer(height, field="height", minimum=64, maximum=4096)
    if width % 16 or height % 16:
        raise ImageRequestError("image width and height must be multiples of 16")
    if width * height > max_pixels:
        raise ImageRequestError(
            f"requested image has {width * height} pixels; limit is {max_pixels}"
        )

    steps = _integer(
        payload.get("num_inference_steps", defaults.get("num_inference_steps", 30)),
        field="num_inference_steps",
        minimum=1,
        maximum=200,
    )
    guidance = _number(
        payload.get("guidance_scale", defaults.get("guidance_scale", 4.0)),
        field="guidance_scale",
        minimum=0,
        maximum=50,
    )
    seed = payload.get("seed")
    if seed is not None:
        seed = _integer(seed, field="seed", minimum=0, maximum=4_294_967_295)
    negative_prompt = payload.get("negative_prompt")
    if negative_prompt is not None and not isinstance(negative_prompt, str):
        raise ImageRequestError("'negative_prompt' must be a string")

    payload.update(
        {
            "model": wire_model,
            "prompt": prompt,
            "n": 1,
            "size": f"{width}x{height}",
            "width": width,
            "height": height,
            "response_format": "b64_json",
            "output_format": "png",
            "num_inference_steps": steps,
        }
    )
    guidance_parameter = defaults.get("guidance_parameter", "guidance_scale")
    if guidance_parameter not in {"guidance_scale", "true_cfg_scale"}:
        raise ImageRequestError("profile has an invalid guidance parameter")
    payload.pop("guidance_scale", None)
    payload.pop("true_cfg_scale", None)
    payload[guidance_parameter] = guidance
    if seed is not None:
        payload["seed"] = seed
    if negative_prompt is not None:
        payload["negative_prompt"] = negative_prompt
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


__all__ = ["ImageRequestError", "normalize_image_request"]
