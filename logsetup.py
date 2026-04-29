"""Mnemosyne Inference — logging setup.

Phase 8 D1. PRD §5.7: structured JSON logs to stdout. Default format is
JSON; set ``MNEMOSYNE_LOG_FORMAT=text`` for the legacy human-readable
format (handy in interactive shells and tests).

The formatter consumes existing :class:`logging.LogRecord` objects, so
no call site in the codebase needs to change. ``extra=`` dict fields
passed to logger calls are folded into the JSON object alongside the
standard ``ts``/``level``/``logger``/``msg`` keys.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Iterable

# Standard LogRecord attributes — anything outside this set is treated
# as user-supplied ``extra=`` data and serialized into the JSON object.
_RESERVED_RECORD_KEYS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
})


class JsonLogFormatter(logging.Formatter):
    """One JSON object per record on stdout.

    Shape: ``{"ts": "...Z", "level": "...", "logger": "...", "msg": "..."}``.
    Adds ``exc_info`` (rendered traceback string) when present and folds
    any ``extra=`` fields the call site passed.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        # ISO-8601 with milliseconds + 'Z' suffix.
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"
        payload: dict[str, object] = {
            "ts": ts_str,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = record.stack_info
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS or key in payload:
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False)


def _resolve_format(env_value: str | None) -> str:
    """Default to JSON; honor an explicit text/json setting."""
    if env_value is None:
        return "json"
    normalized = env_value.strip().lower()
    if normalized in ("text", "plain", "human"):
        return "text"
    return "json"


def configure_logging(
    *,
    level: int = logging.INFO,
    stream=None,
    extra_handlers: Iterable[logging.Handler] | None = None,
) -> str:
    """Install the chosen handler on the root logger and return the format
    actually used (``"json"`` or ``"text"``).

    Idempotent — clears existing handlers so re-invoking from a test
    fixture or a reload path does not stack duplicates.
    """
    chosen = _resolve_format(os.environ.get("MNEMOSYNE_LOG_FORMAT"))
    handler = logging.StreamHandler(stream or sys.stdout)
    if chosen == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    if extra_handlers:
        for h in extra_handlers:
            root.addHandler(h)
    root.setLevel(level)
    return chosen
