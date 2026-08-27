from __future__ import annotations

import json

import pytest

from usage_normalization import StreamingUsageParser, normalize_usage


@pytest.mark.parametrize(
    ("endpoint", "payload", "expected"),
    [
        (
            "/v1/chat/completions",
            {
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                }
            },
            (11, 7, 18),
        ),
        (
            "/v1/responses",
            {
                "type": "response.completed",
                "response": {
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 4,
                        "total_tokens": 24,
                    }
                },
            },
            (20, 4, 24),
        ),
        (
            "/v1/messages",
            {
                "type": "message",
                "usage": {
                    "input_tokens": 5,
                    "cache_creation_input_tokens": 13,
                    "cache_read_input_tokens": 21,
                    "output_tokens": 8,
                },
            },
            (39, 8, 47),
        ),
        (
            "/v1/rerank",
            {"prompt_tokens": 9, "total_tokens": 9},
            (9, 0, 9),
        ),
    ],
)
def test_normalizes_supported_language_usage(endpoint, payload, expected) -> None:
    usage = normalize_usage(payload, endpoint=endpoint)

    assert usage is not None
    assert (
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    ) == expected


def test_invalid_counts_are_conservative_and_preserve_raw_usage() -> None:
    usage = normalize_usage(
        {
            "usage": {
                "prompt_tokens": -2,
                "completion_tokens": "3",
                "total_tokens": "not-a-number",
            }
        },
        endpoint="/v1/chat/completions",
    )

    assert usage is not None
    assert (
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    ) == (0, 3, 3)
    assert usage.raw["prompt_tokens"] == -2


def test_responses_stream_parses_nested_usage_with_unterminated_tail() -> None:
    parser = StreamingUsageParser(endpoint="/v1/responses")
    event = {
        "type": "response.completed",
        "response": {
            "usage": {
                "input_tokens": 31,
                "output_tokens": 9,
                "total_tokens": 40,
            }
        },
    }

    wire = f"event: response.completed\r\ndata: {json.dumps(event)}".encode()
    parser.feed(wire[:23])
    parser.feed(wire[23:])
    usage = parser.finish()

    assert usage is not None
    assert (
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    ) == (31, 9, 40)


def test_messages_stream_merges_start_and_delta_usage() -> None:
    parser = StreamingUsageParser(endpoint="/v1/messages")
    start = {
        "type": "message_start",
        "message": {
            "usage": {
                "input_tokens": 3,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 7,
                "output_tokens": 0,
            }
        },
    }
    delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 12},
    }

    parser.feed(f"event: message_start\ndata: {json.dumps(start)}\n\n".encode())
    parser.feed(f"event: message_delta\ndata: {json.dumps(delta)}".encode())
    usage = parser.finish()

    assert usage is not None
    assert (
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    ) == (15, 12, 27)
