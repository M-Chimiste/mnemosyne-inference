from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from macos.packaging.collect_acceptance import (
    _packaged_engine_defaults,
    _postgres_drained,
    _redact_text,
    _redact_url,
    _usage_summary,
    _write_report,
    redact,
)


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_redaction_preserves_usage_metrics_and_removes_credentials(self) -> None:
        payload = {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
            "token": "private",
            "postgres_dsn": (
                "postgresql://writer:secret@nyx:5432/ledger?api_key=also-secret"
            ),
            "diagnostic": (
                "failed postgresql://writer:secret@nyx:5432/ledger?token=query-secret"
            ),
        }

        result = redact(payload)

        self.assertEqual(result["prompt_tokens"], 12)
        self.assertEqual(result["completion_tokens"], 4)
        self.assertEqual(result["total_tokens"], 16)
        self.assertEqual(result["token"], "[redacted]")
        self.assertEqual(result["postgres_dsn"], "[redacted]")
        self.assertNotIn("secret", result["diagnostic"])
        self.assertIn("[redacted]", result["diagnostic"])

    def test_text_redaction_removes_headers_and_secret_assignments(self) -> None:
        result = _redact_text(
            "Authorization: Bearer abc.def CUSTOM_PASSWORD=hunter2 "
            "HF_TOKEN=hf_private prompt_tokens=12"
        )

        self.assertNotIn("abc.def", result)
        self.assertNotIn("hunter2", result)
        self.assertNotIn("hf_private", result)
        self.assertIn("prompt_tokens=12", result)

    def test_url_redaction_keeps_routing_but_removes_userinfo_and_query_secret(self) -> None:
        value = _redact_url(
            "https://user:password@example.test:8443/path?limit=5&api_key=nope"
        )

        self.assertEqual(
            value,
            "https://[redacted]@example.test:8443/path?limit=5&api_key=%5Bredacted%5D",
        )

    def test_packaged_defaults_extract_only_engine_enablement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "engines:\n"
                "  llama_cpp:\n"
                "    enabled: true\n"
                "  omlx:\n"
                "    enabled: false\n"
                "  ds4:\n"
                "    enabled: false\n"
                "  mflux:\n"
                "    enabled: false\n",
                encoding="utf-8",
            )

            self.assertEqual(
                _packaged_engine_defaults(path),
                {
                    "llama_cpp": True,
                    "omlx": False,
                    "ds4": False,
                    "mflux": False,
                },
            )

    def test_usage_summary_returns_bounded_metrics_without_arbitrary_fields(self) -> None:
        payload = {
            "rows": [
                {
                    "event_id": "abc",
                    "ts": 123.0,
                    "alias": "model",
                    "backend": "llama.cpp",
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                    "request_body": "must not escape",
                }
            ],
            "token_sidecar": {"writer_ready": True, "postgres_dsn": "secret"},
        }

        result = _usage_summary(payload)

        assert result is not None
        self.assertNotIn("request_body", result["recent_rows"][0])
        self.assertEqual(result["recent_rows"][0]["total_tokens"], 7)
        self.assertEqual(result["recent_rows"][0]["timestamp"], 123.0)
        self.assertEqual(result["recent_rows"][0]["model"], "model")
        self.assertEqual(result["recent_rows"][0]["engine"], "llama.cpp")
        self.assertEqual(
            redact(result)["token_sidecar"]["postgres_dsn"],
            "[redacted]",
        )

    def test_postgres_drain_requires_a_new_successful_empty_flush(self) -> None:
        ready = {
            "token_sidecar": {
                "enabled": True,
                "writer_ready": True,
                "outbox_depth": 0,
                "outbox_pending": 0,
                "last_flush_at": 120.0,
                "last_error": None,
            }
        }

        self.assertTrue(_postgres_drained(ready, since=100.0))
        self.assertFalse(_postgres_drained(ready, since=121.0))
        ready["token_sidecar"]["outbox_depth"] = 1
        self.assertFalse(_postgres_drained(ready, since=100.0))

    def test_report_write_is_atomic_private_and_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.json"
            _write_report(path, {"accepted": True})

            self.assertEqual(json.loads(path.read_text()), {"accepted": True})
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])


if __name__ == "__main__":
    unittest.main()
