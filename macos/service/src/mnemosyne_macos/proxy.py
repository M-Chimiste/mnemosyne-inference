"""OpenAI/Anthropic pass-through helpers for the unified inference plane."""

from __future__ import annotations

import json
import re
from typing import Mapping

from .models import Endpoint, ProxyRoute


class InvalidProxyRequest(ValueError):
    pass


_REQUEST_STRIP = frozenset(
    {
        "authorization",
        "api-key",
        "connection",
        "content-length",
        "cookie",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-api-key",
    }
)

_RESPONSE_STRIP = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def prepare_request_body(
    body: bytes,
    *,
    route: ProxyRoute,
    endpoint: Endpoint,
) -> tuple[bytes, str, bool, bool]:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise InvalidProxyRequest("request body must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise InvalidProxyRequest("request body must be a JSON object")
    requested_model = payload.get("model")
    if not isinstance(requested_model, str) or not requested_model:
        raise InvalidProxyRequest("request body must contain a non-empty 'model' string")

    payload["model"] = route.wire_model
    streamed = payload.get("stream") is True
    stream_options = payload.get("stream_options")
    client_asked_for_usage = (
        isinstance(stream_options, dict)
        and stream_options.get("include_usage") is True
    )
    if (
        streamed
        and route.supports_stream_usage
        and endpoint in {Endpoint.CHAT_COMPLETIONS, Endpoint.COMPLETIONS}
    ):
        options = dict(stream_options) if isinstance(stream_options, dict) else {}
        options["include_usage"] = True
        payload["stream_options"] = options
    return (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        requested_model,
        streamed,
        client_asked_for_usage,
    )


_SSE_BOUNDARY_RE = re.compile(br"\r?\n\r?\n")


class StreamingEventFilter:
    """Hide a usage-only SSE event injected solely for local accounting."""

    def __init__(self, *, hide_usage_only_events: bool) -> None:
        self.hide_usage_only_events = hide_usage_only_events
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        if not self.hide_usage_only_events:
            return [chunk]
        self._buffer.extend(chunk)
        output: list[bytes] = []
        while True:
            match = _SSE_BOUNDARY_RE.search(self._buffer)
            if match is None:
                break
            event = bytes(self._buffer[: match.start()])
            separator = bytes(self._buffer[match.start() : match.end()])
            del self._buffer[: match.end()]
            if not _is_usage_only_event(event):
                output.append(event + separator)
        return output

    def finish(self) -> bytes:
        tail = bytes(self._buffer)
        self._buffer.clear()
        return tail


def _is_usage_only_event(event: bytes) -> bool:
    data_lines: list[str] = []
    for raw_line in event.decode("utf-8", errors="replace").splitlines():
        if raw_line.startswith("data:"):
            data_lines.append(raw_line[5:].lstrip())
    if not data_lines:
        return False
    try:
        payload = json.loads("\n".join(data_lines))
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("usage"), dict)
        and payload.get("choices") == []
    )


def upstream_headers(
    incoming: Mapping[str, str],
    route: ProxyRoute,
) -> dict[str, str]:
    headers = {
        key: value
        for key, value in incoming.items()
        if key.lower() not in _REQUEST_STRIP
    }
    for key, value in route.headers.items():
        headers[key] = value
    headers.setdefault("content-type", "application/json")
    return headers


def downstream_headers(incoming: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in incoming.items()
        if key.lower() not in _RESPONSE_STRIP
    }


__all__ = [
    "InvalidProxyRequest",
    "StreamingEventFilter",
    "downstream_headers",
    "prepare_request_body",
    "upstream_headers",
]
