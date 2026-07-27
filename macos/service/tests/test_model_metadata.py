from __future__ import annotations

import io
import struct

from mnemosyne_macos.model_metadata import (
    markdown_summary,
    metadata_from_config,
    metadata_from_gguf_stream,
    recommended_projector,
)


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded


def _gguf(entries: list[tuple[str, int, object]]) -> bytes:
    payload = bytearray(b"GGUF")
    payload.extend(struct.pack("<IQQ", 3, 0, len(entries)))
    for key, value_type, value in entries:
        payload.extend(_string(key))
        payload.extend(struct.pack("<I", value_type))
        if value_type == 8:
            payload.extend(_string(str(value)))
        elif value_type == 10:
            payload.extend(struct.pack("<Q", int(value)))
        elif value_type == 4:
            payload.extend(struct.pack("<I", int(value)))
        else:
            raise AssertionError(f"unsupported fixture type {value_type}")
    return bytes(payload)


def test_gguf_reader_extracts_context_architecture_and_description() -> None:
    metadata = metadata_from_gguf_stream(
        io.BytesIO(
            _gguf(
                [
                    ("general.architecture", 8, "qwen2"),
                    ("general.name", 8, "Qwen Example"),
                    ("general.description", 8, "A local GGUF model."),
                    ("general.parameter_count", 10, 7_615_000_000),
                    ("qwen2.context_length", 4, 131_072),
                ]
            )
        )
    )

    assert metadata.architecture == "qwen2"
    assert metadata.name == "Qwen Example"
    assert metadata.description == "A local GGUF model."
    assert metadata.parameter_count == 7_615_000_000
    assert metadata.context_length == 131_072


def test_hf_config_metadata_prefers_nested_text_context() -> None:
    metadata = metadata_from_config(
        {
            "model_type": "qwen3_vl",
            "vision_config": {"image_size": 448},
            "text_config": {
                "architectures": ["Qwen3VLForConditionalGeneration"],
                "max_position_embeddings": 262_144,
            },
        }
    )

    assert metadata.architecture == "Qwen3VLForConditionalGeneration"
    assert metadata.context_length == 262_144


def test_model_card_summary_skips_front_matter_heading_and_badges() -> None:
    summary = markdown_summary(
        """---
license: apache-2.0
---
# Example

[![badge](https://example.invalid/badge.svg)](https://example.invalid)

This model is tuned for long-context local inference with tool use.
"""
    )

    assert summary == (
        "This model is tuned for long-context local inference with tool use."
    )


def test_projector_recommendation_prefers_high_fidelity_with_opt_out_elsewhere() -> None:
    choices = (
        "mmproj-model-Q5_K_M.gguf",
        "mmproj-model-F16.gguf",
        "mmproj-model-Q8_0.gguf",
    )

    assert recommended_projector(choices, name=lambda value: value) == choices[1]
