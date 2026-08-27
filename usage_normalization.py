"""Engine-neutral token-usage normalization for the CUDA manager.

The CUDA inference plane can expose OpenAI Chat/Completions, Responses,
Embeddings, Rerank, and Anthropic-compatible Messages routes.  Their token
fields and streaming envelopes are not identical, so the proxy normalizes
them before handing one stable shape to the existing SQLite/outbox pipeline.

This module is intentionally pure and root-local.  The independently packaged
native service has an equivalent implementation in its own package.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class UsageProtocol(str, Enum):
    OPENAI = "openai"
    OPENAI_RESPONSES = "openai-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"


@dataclass(frozen=True)
class NormalizedUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    raw: Mapping[str, Any] = field(default_factory=dict)


_TOKEN_KEYS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
)


def _optional_token_int(value: Any) -> int | None:
    """Parse a non-negative integral token count, rejecting booleans."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return converted if converted >= 0 else None


def _token_int(value: Any) -> int:
    parsed = _optional_token_int(value)
    return parsed if parsed is not None else 0


def _protocol_for_endpoint(endpoint: str | None) -> UsageProtocol:
    endpoint_name = (endpoint or "").rstrip("/").lower().rsplit("/", 1)[-1]
    if endpoint_name == "messages":
        return UsageProtocol.ANTHROPIC_MESSAGES
    if endpoint_name == "responses":
        return UsageProtocol.OPENAI_RESPONSES
    return UsageProtocol.OPENAI


def _usage_block(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    direct = payload.get("usage")
    if isinstance(direct, Mapping):
        return direct

    # Responses completion events and Anthropic message-start events nest
    # usage under their protocol envelope.
    for envelope_name in ("response", "message", "delta"):
        envelope = payload.get(envelope_name)
        if isinstance(envelope, Mapping):
            usage = envelope.get("usage")
            if isinstance(usage, Mapping):
                return usage

    # Some rerank implementations return the counters as the response object.
    if any(key in payload for key in _TOKEN_KEYS):
        return payload
    return None


def normalize_usage_block(
    usage: Mapping[str, Any],
    *,
    protocol: UsageProtocol,
) -> NormalizedUsage | None:
    """Normalize one located usage block without inventing missing usage."""

    if not any(key in usage for key in _TOKEN_KEYS):
        return None

    if protocol == UsageProtocol.ANTHROPIC_MESSAGES:
        # Anthropic reports uncached, cache-created, and cache-read input as
        # separate counters.  Together they are the complete prompt cost.
        prompt = (
            _token_int(usage.get("input_tokens"))
            + _token_int(usage.get("cache_creation_input_tokens"))
            + _token_int(usage.get("cache_read_input_tokens"))
        )
        completion = _token_int(usage.get("output_tokens"))
    else:
        prompt_key = "prompt_tokens" if "prompt_tokens" in usage else "input_tokens"
        completion_key = (
            "completion_tokens" if "completion_tokens" in usage else "output_tokens"
        )
        prompt = _token_int(usage.get(prompt_key))
        completion = _token_int(usage.get(completion_key))

    reported_total = _optional_token_int(usage.get("total_tokens"))
    total = reported_total if reported_total is not None else prompt + completion
    return NormalizedUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        raw=dict(usage),
    )


def normalize_usage(
    payload: Mapping[str, Any],
    *,
    endpoint: str | None,
) -> NormalizedUsage | None:
    """Extract and normalize usage from a decoded response or SSE payload."""

    usage = _usage_block(payload)
    if usage is None:
        return None
    return normalize_usage_block(
        usage,
        protocol=_protocol_for_endpoint(endpoint),
    )


_SSE_BOUNDARY_RE = re.compile(br"\r?\n\r?\n")


def _decode_sse_payload(event: bytes) -> Mapping[str, Any] | None:
    data_lines: list[str] = []
    for raw_line in event.decode("utf-8", errors="replace").splitlines():
        if raw_line.startswith("data:"):
            data_lines.append(raw_line[5:].lstrip())
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    if data.strip() == "[DONE]":
        return None
    try:
        payload = json.loads(data)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


class StreamingUsageParser:
    """Incrementally capture the final normalized usage from an SSE stream."""

    def __init__(self, *, endpoint: str) -> None:
        self.protocol = _protocol_for_endpoint(endpoint)
        self._buffer = bytearray()
        self._latest: NormalizedUsage | None = None
        self._anthropic_usage: dict[str, Any] = {}

    @property
    def usage(self) -> NormalizedUsage | None:
        return self._latest

    def feed(self, chunk: bytes) -> None:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("SSE chunks must be bytes-like")
        self._buffer.extend(bytes(chunk))
        while True:
            boundary = _SSE_BOUNDARY_RE.search(self._buffer)
            if boundary is None:
                break
            event = bytes(self._buffer[: boundary.start()])
            del self._buffer[: boundary.end()]
            self._consume_event(event)

    def finish(self) -> NormalizedUsage | None:
        """Consume an unterminated final event and return the latest totals."""

        if self._buffer:
            event = bytes(self._buffer)
            self._buffer.clear()
            self._consume_event(event)
        return self._latest

    def _consume_event(self, event: bytes) -> None:
        payload = _decode_sse_payload(event)
        if payload is None:
            return
        block = _usage_block(payload)
        if block is None:
            return

        if self.protocol == UsageProtocol.ANTHROPIC_MESSAGES:
            # Messages splits prompt/cache counters across message_start and
            # cumulative output usage across later message_delta events.
            for key, value in block.items():
                if key in _TOKEN_KEYS:
                    self._anthropic_usage[key] = value
            normalized = normalize_usage_block(
                self._anthropic_usage,
                protocol=UsageProtocol.ANTHROPIC_MESSAGES,
            )
        else:
            normalized = normalize_usage_block(block, protocol=self.protocol)
        if normalized is not None:
            self._latest = normalized


__all__ = [
    "NormalizedUsage",
    "StreamingUsageParser",
    "UsageProtocol",
    "normalize_usage",
    "normalize_usage_block",
]
