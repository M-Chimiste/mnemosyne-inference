"""Engine-neutral token-usage normalization and event construction.

The native coordinator speaks more than one wire dialect even though all
usage ultimately lands in the same local analytics table and Postgres ledger.
This module deliberately knows nothing about engine processes, FastAPI, or
SQLite.  It accepts decoded JSON responses (or raw SSE bytes) and produces one
small normalized shape for the durable accounting layer.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class UsageProtocol(StrEnum):
    """The usage conventions understood by :func:`normalize_usage`."""

    OPENAI = "openai"
    OPENAI_RESPONSES = "openai-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    """Portable token totals plus the source usage block for diagnostics."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def raw_json(self) -> str | None:
        if not self.raw:
            return None
        return json.dumps(
            dict(self.raw),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One completed inference request ready for durable persistence.

    ``engine`` is intentionally a string rather than an engine enum so adding
    another adapter does not require changing the accounting subsystem.  The
    SQLite outbox stores it in a ``backend`` column to stay wire-compatible
    with the existing root ``PgWriter`` row shape.
    """

    usage: NormalizedUsage
    endpoint: str
    engine: str
    requested_model: str | None = None
    alias: str | None = None
    streamed: bool = False
    response_ms: float = 0.0
    status_code: int = 200
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.event_id or not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        if not self.engine or not self.engine.strip():
            raise ValueError("engine must not be empty")
        if not self.endpoint or not self.endpoint.strip():
            raise ValueError("endpoint must not be empty")
        if isinstance(self.timestamp, bool) or not math.isfinite(self.timestamp):
            raise ValueError("timestamp must be finite")
        if (
            isinstance(self.response_ms, bool)
            or not math.isfinite(self.response_ms)
            or self.response_ms < 0
        ):
            raise ValueError("response_ms must be finite and non-negative")
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise ValueError("status_code must be an integer")

    @property
    def backend(self) -> str:
        """Compatibility name used by the existing SQLite/Postgres pipeline."""

        return self.engine

    @property
    def normalized_endpoint(self) -> str:
        endpoint = self.endpoint.strip()
        return endpoint if endpoint.startswith("/") else f"/{endpoint}"


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
    """Parse a token count without accepting booleans, fractions, or negatives."""

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


def _coerce_protocol(
    protocol: UsageProtocol | str | None,
    *,
    endpoint: str | None,
    payload: Mapping[str, Any],
) -> UsageProtocol:
    if protocol is not None:
        value = str(protocol).lower().replace("_", "-")
        aliases = {
            "openai": UsageProtocol.OPENAI,
            "responses": UsageProtocol.OPENAI_RESPONSES,
            "openai-responses": UsageProtocol.OPENAI_RESPONSES,
            "anthropic": UsageProtocol.ANTHROPIC_MESSAGES,
            "messages": UsageProtocol.ANTHROPIC_MESSAGES,
            "anthropic-messages": UsageProtocol.ANTHROPIC_MESSAGES,
        }
        if value not in aliases:
            raise ValueError(f"unsupported usage protocol: {protocol!r}")
        return aliases[value]

    normalized_endpoint = (endpoint or "").rstrip("/").lower()
    endpoint_name = normalized_endpoint.rsplit("/", 1)[-1]
    if endpoint_name == "messages":
        return UsageProtocol.ANTHROPIC_MESSAGES
    if endpoint_name == "responses":
        return UsageProtocol.OPENAI_RESPONSES

    event_type = str(payload.get("type") or "").lower()
    if (
        event_type == "message"
        or event_type.startswith("message_")
        or event_type.startswith("content_block_")
    ):
        return UsageProtocol.ANTHROPIC_MESSAGES
    if event_type.startswith("response."):
        return UsageProtocol.OPENAI_RESPONSES
    return UsageProtocol.OPENAI


def _usage_block(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Locate a usage mapping in common response and streaming envelopes."""

    direct = payload.get("usage")
    if isinstance(direct, Mapping):
        return direct

    for envelope_name in ("response", "message", "delta"):
        envelope = payload.get(envelope_name)
        if isinstance(envelope, Mapping):
            usage = envelope.get("usage")
            if isinstance(usage, Mapping):
                return usage

    # Also accept a bare usage block.  This is useful for adapter-specific
    # APIs that return stats without a surrounding response object.
    if any(key in payload for key in _TOKEN_KEYS):
        return payload
    return None


def normalize_usage_block(
    usage: Mapping[str, Any],
    *,
    protocol: UsageProtocol | str = UsageProtocol.OPENAI,
) -> NormalizedUsage | None:
    """Normalize one already-located usage block.

    Anthropic prompt caching reports uncached, cache-created, and cache-read
    input tokens as separate top-level counters.  Their sum is the complete
    prompt token count.  OpenAI cache counters live in a details object and
    are already included in ``prompt_tokens``/``input_tokens``, so they are
    intentionally not added again.
    """

    dialect = _coerce_protocol(protocol, endpoint=None, payload=usage)
    if not any(key in usage for key in _TOKEN_KEYS):
        return None

    if dialect == UsageProtocol.ANTHROPIC_MESSAGES:
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

    parsed_total = _optional_token_int(usage.get("total_tokens"))
    total = parsed_total if parsed_total is not None else prompt + completion
    return NormalizedUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        raw=dict(usage),
    )


def normalize_usage(
    payload: Mapping[str, Any],
    *,
    protocol: UsageProtocol | str | None = None,
    endpoint: str | None = None,
) -> NormalizedUsage | None:
    """Extract and normalize usage from a decoded response/event payload."""

    dialect = _coerce_protocol(protocol, endpoint=endpoint, payload=payload)
    usage = _usage_block(payload)
    if usage is None:
        return None
    return normalize_usage_block(usage, protocol=dialect)


def usage_event_from_payload(
    payload: Mapping[str, Any],
    *,
    endpoint: str,
    engine: str,
    requested_model: str | None = None,
    alias: str | None = None,
    streamed: bool = False,
    response_ms: float = 0.0,
    status_code: int = 200,
    protocol: UsageProtocol | str | None = None,
    event_id: str | None = None,
    timestamp: float | None = None,
) -> UsageEvent | None:
    """Build a complete event when ``payload`` contains recognizable usage."""

    usage = normalize_usage(payload, protocol=protocol, endpoint=endpoint)
    if usage is None:
        return None
    kwargs: dict[str, Any] = {}
    if event_id is not None:
        kwargs["event_id"] = event_id
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return UsageEvent(
        usage=usage,
        endpoint=endpoint,
        engine=engine,
        requested_model=requested_model,
        alias=alias,
        streamed=streamed,
        response_ms=response_ms,
        status_code=status_code,
        **kwargs,
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
    """Incrementally capture the final usage from an SSE byte stream.

    ``feed`` accepts arbitrary transport chunks; SSE event boundaries do not
    need to line up with those chunks.  For OpenAI-style streams, the latest
    complete usage block wins.  Anthropic Messages emits prompt usage in
    ``message_start`` and cumulative output usage in later ``message_delta``
    events, so those blocks are merged before normalization.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        protocol: UsageProtocol | str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.protocol = _coerce_protocol(protocol, endpoint=endpoint, payload={})
        self._buffer = bytearray()
        self._latest: NormalizedUsage | None = None
        self._anthropic_usage: dict[str, Any] = {}

    @property
    def usage(self) -> NormalizedUsage | None:
        return self._latest

    def feed(self, chunk: bytes) -> list[NormalizedUsage]:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("SSE chunks must be bytes-like")
        self._buffer.extend(bytes(chunk))
        found: list[NormalizedUsage] = []
        while True:
            boundary = _SSE_BOUNDARY_RE.search(self._buffer)
            if boundary is None:
                break
            event = bytes(self._buffer[: boundary.start()])
            del self._buffer[: boundary.end()]
            usage = self._consume_event(event)
            if usage is not None:
                found.append(usage)
        return found

    def finish(self) -> NormalizedUsage | None:
        """Consume an unterminated final event and return the latest totals."""

        if self._buffer:
            event = bytes(self._buffer)
            self._buffer.clear()
            self._consume_event(event)
        return self._latest

    def _consume_event(self, event: bytes) -> NormalizedUsage | None:
        payload = _decode_sse_payload(event)
        if payload is None:
            return None
        block = _usage_block(payload)
        if block is None:
            return None

        if self.protocol == UsageProtocol.ANTHROPIC_MESSAGES:
            # Message usage counters are cumulative within their respective
            # fields. Preserve prior prompt/cache counters while accepting the
            # latest output count from message_delta.
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
        return normalized


__all__ = [
    "NormalizedUsage",
    "StreamingUsageParser",
    "UsageEvent",
    "UsageProtocol",
    "normalize_usage",
    "normalize_usage_block",
    "usage_event_from_payload",
]
