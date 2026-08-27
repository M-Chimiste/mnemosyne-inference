"""OpenAI/Anthropic pass-through helpers for the unified inference plane."""

from __future__ import annotations

import json
import math
import re
from typing import Mapping

from .models import Endpoint, EngineName, ProxyRoute


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
        "x-mnemosyne-error",
    }
)

_CHAT_TEMPLATE_ENDPOINTS = frozenset(
    {
        Endpoint.CHAT_COMPLETIONS,
        Endpoint.RESPONSES,
        Endpoint.MESSAGES,
    }
)
_REASONING_BUDGET_ENDPOINTS = frozenset(
    {
        Endpoint.CHAT_COMPLETIONS,
        Endpoint.COMPLETIONS,
        Endpoint.RESPONSES,
        Endpoint.MESSAGES,
    }
)
_REASONING_CONTROL_ENGINES = frozenset(
    {
        EngineName.LLAMA_CPP,
        EngineName.OMLX,
    }
)
_REASONING_BUDGET_FIELDS = (
    "thinking_budget",
    "reasoning_budget_tokens",
    "thinking_budget_tokens",
)


def _is_reasoning_effort(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _reasoning_effort(payload: dict, endpoint: Endpoint) -> object | None:
    values: list[tuple[str, object]] = []
    if payload.get("reasoning_effort") is not None:
        values.append(("reasoning_effort", payload["reasoning_effort"]))
    if endpoint == Endpoint.RESPONSES:
        reasoning = payload.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, dict):
            raise InvalidProxyRequest("'reasoning' must be a JSON object")
        if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
            values.append(("reasoning.effort", reasoning["effort"]))

    if not values:
        return None
    for field, value in values:
        if not _is_reasoning_effort(value):
            raise InvalidProxyRequest(
                f"'{field}' must be a non-empty string or finite number"
            )
    if any(value != values[0][1] for _, value in values[1:]):
        raise InvalidProxyRequest("reasoning effort fields must agree")
    return values[0][1]


def _reasoning_budget(payload: dict) -> int | None:
    values = [
        (field, payload[field])
        for field in _REASONING_BUDGET_FIELDS
        if payload.get(field) is not None
    ]
    if not values:
        return None
    for field, value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidProxyRequest(f"'{field}' must be a non-negative integer")
    if any(value != values[0][1] for _, value in values[1:]):
        raise InvalidProxyRequest("reasoning budget fields must agree")
    return values[0][1]


def _normalize_reasoning_controls(
    payload: dict,
    *,
    engine: EngineName,
    endpoint: Endpoint,
) -> None:
    """Translate a small portable reasoning surface for supported engines.

    Request bodies otherwise remain opaque. This translation happens only
    after model/engine selection because oMLX and llama.cpp use different
    names for the same engine-enforced reasoning token ceiling.
    """

    if engine not in _REASONING_CONTROL_ENGINES:
        return

    if endpoint in _CHAT_TEMPLATE_ENDPOINTS:
        raw_template_kwargs = payload.get("chat_template_kwargs")
        if raw_template_kwargs is not None and not isinstance(
            raw_template_kwargs, dict
        ):
            raise InvalidProxyRequest(
                "'chat_template_kwargs' must be a JSON object"
            )
        template_kwargs = dict(raw_template_kwargs or {})

        for field in ("enable_thinking", "preserve_thinking"):
            if field not in payload:
                continue
            value = payload.pop(field)
            if value is None:
                continue
            if not isinstance(value, bool):
                raise InvalidProxyRequest(f"'{field}' must be a boolean")
            if field in template_kwargs and template_kwargs[field] != value:
                raise InvalidProxyRequest(
                    f"top-level '{field}' and chat_template_kwargs must agree"
                )
            template_kwargs[field] = value

        effort = _reasoning_effort(payload, endpoint)
        if effort is not None:
            existing_effort = template_kwargs.get("reasoning_effort")
            if existing_effort is not None and existing_effort != effort:
                raise InvalidProxyRequest(
                    "reasoning effort fields and chat_template_kwargs must agree"
                )
            template_kwargs["reasoning_effort"] = effort

        if template_kwargs:
            payload["chat_template_kwargs"] = template_kwargs

    if endpoint not in _REASONING_BUDGET_ENDPOINTS:
        return
    budget = _reasoning_budget(payload)
    if budget is None:
        return
    for field in _REASONING_BUDGET_FIELDS:
        payload.pop(field, None)
    if engine == EngineName.OMLX:
        payload["thinking_budget"] = budget
    else:
        payload["reasoning_budget_tokens"] = budget


def prepare_request_body(
    body: bytes,
    *,
    route: ProxyRoute,
    endpoint: Endpoint,
    engine: EngineName,
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
    _normalize_reasoning_controls(payload, engine=engine, endpoint=endpoint)
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
