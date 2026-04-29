"""Phase 8 D1 — JSON log formatter shape."""
from __future__ import annotations

import io
import json
import logging

import pytest

import logsetup


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Snapshot/restore root handlers + level so assertions don't bleed
    into the rest of the test suite."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def _capture_one_record(record: logging.LogRecord) -> str:
    formatter = logsetup.JsonLogFormatter()
    return formatter.format(record)


def _make_record(**overrides) -> logging.LogRecord:
    base = dict(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    base.update(overrides)
    return logging.LogRecord(**base)


def test_formatter_emits_required_fields():
    line = _capture_one_record(_make_record())
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["msg"] == "hello world"
    assert payload["ts"].endswith("Z") and "T" in payload["ts"]


def test_formatter_includes_exc_info():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        exc_info = sys.exc_info()
    record = _make_record(exc_info=exc_info, msg="caught", args=())
    payload = json.loads(_capture_one_record(record))
    assert "exc_info" in payload
    assert "ValueError: boom" in payload["exc_info"]


def test_configure_logging_defaults_to_json(monkeypatch):
    monkeypatch.delenv("MNEMOSYNE_LOG_FORMAT", raising=False)
    stream = io.StringIO()
    chosen = logsetup.configure_logging(stream=stream)
    assert chosen == "json"
    logging.getLogger("vllm-manager.test").info("structured")
    out = stream.getvalue().strip().splitlines()
    assert out, "expected at least one log line"
    payload = json.loads(out[-1])
    assert payload["msg"] == "structured"
    assert payload["logger"] == "vllm-manager.test"


def test_configure_logging_text_mode_falls_back(monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_LOG_FORMAT", "text")
    stream = io.StringIO()
    chosen = logsetup.configure_logging(stream=stream)
    assert chosen == "text"
    logging.getLogger("vllm-manager.test").info("plain")
    out = stream.getvalue().strip()
    # Not JSON — the legacy text format prefixes a timestamp + [INFO] tag.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert "[INFO]" in out
    assert "plain" in out
