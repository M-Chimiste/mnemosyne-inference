from __future__ import annotations

import json

import pytest

from mnemosyne_macos.usage import (
    NormalizedUsage,
    StreamingUsageParser,
    UsageEvent,
    UsageProtocol,
    normalize_usage,
    usage_event_from_payload,
)


def test_normalizes_openai_chat_usage() -> None:
    usage = normalize_usage(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        },
        endpoint="/v1/chat/completions",
    )

    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (11, 7, 18)
    # Cache detail is preserved but not double-counted: OpenAI includes it in
    # prompt_tokens already.
    assert usage.raw["prompt_tokens_details"] == {"cached_tokens": 3}


def test_normalizes_embeddings_without_completion_count() -> None:
    usage = normalize_usage(
        {"usage": {"prompt_tokens": 9, "total_tokens": 9}},
        endpoint="/v1/embeddings",
    )

    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (9, 0, 9)


@pytest.mark.parametrize(
    "payload",
    [
        {"usage": {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "usage": {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
            },
        },
    ],
)
def test_normalizes_openai_responses_envelopes(payload) -> None:
    usage = normalize_usage(payload, endpoint="/v1/responses")

    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (20, 4, 24)


def test_normalizes_anthropic_messages_and_cache_counters() -> None:
    payload = {
        "type": "message",
        "usage": {
            "input_tokens": 5,
            "cache_creation_input_tokens": 13,
            "cache_read_input_tokens": 21,
            "output_tokens": 8,
        },
    }

    usage = normalize_usage(payload, endpoint="/v1/messages")

    assert usage is not None
    assert usage.prompt_tokens == 39
    assert usage.completion_tokens == 8
    assert usage.total_tokens == 47


def test_invalid_counts_are_conservative_and_raw_is_preserved() -> None:
    usage = normalize_usage(
        {
            "usage": {
                "prompt_tokens": -2,
                "completion_tokens": "3",
                "total_tokens": "not-a-number",
            }
        }
    )

    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (0, 3, 3)
    assert usage.raw["prompt_tokens"] == -2


def test_payload_without_recognized_usage_returns_none() -> None:
    assert normalize_usage({"choices": []}) is None
    assert normalize_usage({"usage": {}}) is None


def test_usage_event_factory_and_validation() -> None:
    event = usage_event_from_payload(
        {"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
        endpoint="v1/chat/completions",
        engine="omlx",
        requested_model="org/model",
        alias="frontier",
        response_ms=12.5,
        event_id="stable-id",
        timestamp=100.25,
    )

    assert event is not None
    assert event.normalized_endpoint == "/v1/chat/completions"
    assert event.backend == "omlx"
    assert event.event_id == "stable-id"
    assert event.timestamp == 100.25

    with pytest.raises(ValueError, match="response_ms"):
        UsageEvent(
            usage=NormalizedUsage(1, 1, 2),
            endpoint="/v1/chat/completions",
            engine="omlx",
            response_ms=-1,
        )


def test_openai_stream_parser_handles_arbitrary_chunk_boundaries() -> None:
    parser = StreamingUsageParser(endpoint="/v1/chat/completions")
    wire = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":4,'
        b'"completion_tokens":2,"total_tokens":6}}\n\n'
        b'data: [DONE]\n\n'
    )

    observed = []
    for cut in (wire[:17], wire[17:63], wire[63:99], wire[99:]):
        observed.extend(parser.feed(cut))

    assert len(observed) == 1
    assert parser.finish() == observed[0]
    assert (observed[0].prompt_tokens, observed[0].completion_tokens) == (4, 2)


def test_responses_stream_parser_handles_nested_usage_and_crlf() -> None:
    parser = StreamingUsageParser(endpoint="/v1/responses")
    event = {
        "type": "response.completed",
        "response": {
            "usage": {"input_tokens": 31, "output_tokens": 9, "total_tokens": 40}
        },
    }

    parser.feed(f"event: response.completed\r\ndata: {json.dumps(event)}\r\n\r\n".encode())
    usage = parser.finish()

    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (31, 9, 40)


def test_anthropic_stream_merges_start_and_delta_usage() -> None:
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
    usage = parser.finish()  # final event deliberately has no blank terminator

    assert usage is not None
    assert usage.prompt_tokens == 15
    assert usage.completion_tokens == 12
    assert usage.total_tokens == 27


def test_stream_parser_ignores_malformed_events_and_done_marker() -> None:
    parser = StreamingUsageParser(
        endpoint="/v1/chat/completions",
        protocol=UsageProtocol.OPENAI,
    )
    assert parser.feed(b": keepalive\n\n") == []
    assert parser.feed(b"data: not-json\n\n") == []
    assert parser.feed(b"data: [DONE]\n\n") == []
    assert parser.finish() is None
