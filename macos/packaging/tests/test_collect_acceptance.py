from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from macos.packaging.collect_acceptance import (
    _app_runtime_links,
    _download_lifecycle_summary,
    _exercise_launch_agent,
    _guided_setup_summary,
    _launch_agent,
    _login_cycle_summary,
    _lmstudio_adoption_summary,
    _packaged_engine_defaults,
    _postgres_drained,
    _protected_model_summary,
    _redact_text,
    _redact_url,
    _runtime_lifecycle_summary,
    _usage_summary,
    _write_report,
    collect_live,
    redact,
)


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_app_runtime_links_require_packaged_sparkle_and_bundle_rpath(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Unified Inference.app"
            executable = app / "Contents" / "MacOS" / "UnifiedInference"
            sparkle = (
                app
                / "Contents"
                / "Frameworks"
                / "Sparkle.framework"
                / "Versions"
                / "B"
                / "Sparkle"
            )
            executable.parent.mkdir(parents=True)
            sparkle.parent.mkdir(parents=True)
            executable.touch()
            sparkle.touch()
            with patch(
                "macos.packaging.collect_acceptance._run",
                side_effect=[
                    {
                        "ok": True,
                        "diagnostic": (
                            "@rpath/Sparkle.framework/Versions/B/Sparkle"
                        ),
                    },
                    {
                        "ok": True,
                        "diagnostic": (
                            "path @executable_path/../Frameworks (offset 12)"
                        ),
                    },
                ],
            ) as run:
                self.assertTrue(_app_runtime_links(app)["accepted"])
                self.assertEqual(
                    run.call_args_list[0].kwargs["output_limit"],
                    256 * 1024,
                )
                self.assertEqual(
                    run.call_args_list[1].kwargs["output_limit"],
                    256 * 1024,
                )

            sparkle.unlink()
            with patch(
                "macos.packaging.collect_acceptance._run",
                return_value={"ok": True, "diagnostic": ""},
            ):
                self.assertFalse(_app_runtime_links(app)["accepted"])

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

    def test_download_lifecycle_requires_observed_target_mac_transitions(self) -> None:
        payload = {
            "installs": [
                {
                    "id": "cancel-retry",
                    "repo_id": "owner/model",
                    "engine": "llama.cpp",
                    "alias": "model",
                    "status": "deleted",
                    "revision": "a" * 40,
                    "dismissed": True,
                    "events": [
                        {"event": "created", "status": "queued"},
                        {"event": "status", "status": "downloading"},
                        {"event": "status", "status": "cancelled"},
                        {"event": "status", "status": "queued"},
                        {"event": "status", "status": "downloading"},
                        {"event": "status", "status": "registering"},
                        {"event": "status", "status": "installed"},
                        {"event": "history_dismissed", "status": "installed"},
                        {"event": "status", "status": "deleted"},
                    ],
                },
                {
                    "id": "registration-retry",
                    "repo_id": "owner/other",
                    "engine": "omlx",
                    "alias": "other",
                    "status": "installed",
                    "revision": "b" * 40,
                    "dismissed": False,
                    "events": [
                        {"event": "created", "status": "queued"},
                        {"event": "status", "status": "downloaded"},
                        {"event": "status", "status": "registering"},
                        {"event": "status", "status": "installed"},
                    ],
                },
            ]
        }

        result = _download_lifecycle_summary(payload)

        self.assertTrue(result["accepted"])
        self.assertTrue(all(result["checks"].values()))
        payload["installs"][0]["events"] = [
            event
            for event in payload["installs"][0]["events"]
            if event["status"] != "cancelled"
        ]
        self.assertFalse(_download_lifecycle_summary(payload)["accepted"])

    def test_protected_model_requires_scope_volume_and_healthy_storage(self) -> None:
        config = {
            "config": {
                "storage": {
                    "locations": [
                        {
                            "name": "athena",
                            "path": "/Volumes/Athena/models",
                            "scope_id": "a" * 64,
                            "volume_uuid": "volume-uuid",
                        }
                    ]
                },
                "models": [
                    {
                        "alias": "vision",
                        "engine": "llama.cpp",
                        "storage": "athena",
                    }
                ],
            }
        }
        storage = {
            "locations": [
                {
                    "name": "athena",
                    "exists": True,
                    "is_directory": True,
                    "writable": True,
                    "volume_matches": True,
                }
            ]
        }

        result = _protected_model_summary(config, storage, alias="vision")

        self.assertTrue(result["accepted"])
        config["config"]["storage"]["locations"][0]["scope_id"] = "not-a-scope"
        self.assertFalse(
            _protected_model_summary(config, storage, alias="vision")["accepted"]
        )

    def test_lmstudio_adoption_requires_native_profile_and_offline_listener(self) -> None:
        config = {
            "config": {
                "engines": {"llama_cpp": {"enabled": True}},
                "storage": {
                    "locations": [
                        {
                            "name": "lm-models",
                            "path": "~/.lmstudio/models",
                        }
                    ]
                },
                "models": [
                    {
                        "alias": "adopted",
                        "engine": "llama.cpp",
                        "storage": "lm-models",
                        "model": "~/.lmstudio/models/owner/model/model.gguf",
                    }
                ],
                "migration": {"legacy_lmstudio_profiles": []},
            }
        }
        sources = {
            "sources": [
                {
                    "id": "lmstudio-models",
                    "path": "~/.lmstudio/models",
                    "source": "lmstudio-conventional",
                }
            ]
        }

        result = _lmstudio_adoption_summary(
            config,
            sources,
            alias="adopted",
            lmstudio_offline=True,
        )

        self.assertTrue(result["accepted"])
        self.assertFalse(
            _lmstudio_adoption_summary(
                config,
                sources,
                alias="adopted",
                lmstudio_offline=False,
            )["accepted"]
        )

    def test_runtime_lifecycle_requires_restart_validated_update_and_rollback(
        self,
    ) -> None:
        payload = {
            "journal": {
                "valid": True,
                "dropped_events": 0,
                "events": [
                    {
                        "sequence": 1,
                        "engine": "llama.cpp",
                        "action": "activated",
                        "outcome": "succeeded",
                        "service_instance_id": "instance-a",
                        "active_version_before": "b100",
                        "active_version_after": "b200",
                    },
                    {
                        "sequence": 2,
                        "engine": "llama.cpp",
                        "action": "inference_validated",
                        "outcome": "succeeded",
                        "service_instance_id": "instance-b",
                        "active_version_before": "b200",
                        "active_version_after": "b200",
                    },
                    {
                        "sequence": 3,
                        "engine": "llama.cpp",
                        "action": "rolled_back",
                        "outcome": "succeeded",
                        "service_instance_id": "instance-b",
                        "active_version_before": "b200",
                        "active_version_after": "b100",
                    },
                    {
                        "sequence": 4,
                        "engine": "llama.cpp",
                        "action": "inference_validated",
                        "outcome": "succeeded",
                        "service_instance_id": "instance-c",
                        "active_version_before": "b100",
                        "active_version_after": "b100",
                    },
                    {
                        "sequence": 5,
                        "engine": "llama.cpp",
                        "action": "install_rejected",
                        "outcome": "failed",
                        "service_instance_id": "instance-c",
                        "active_version_before": "b100",
                        "active_version_after": "b100",
                        "failure_code": "integrity",
                    },
                ],
            },
            "installed": {"llama.cpp": {"version": "b100"}},
        }

        result = _runtime_lifecycle_summary(payload, engine="llama.cpp")

        self.assertTrue(result["accepted"])
        self.assertTrue(all(result["checks"].values()))
        payload["journal"]["events"][1]["service_instance_id"] = "instance-a"
        self.assertFalse(
            _runtime_lifecycle_summary(payload, engine="llama.cpp")["accepted"]
        )

    def test_guided_setup_requires_this_build_to_present_then_complete(self) -> None:
        preferences = {
            "didCompleteNativeSetupV1": "1",
            "nativeSetupFirstPresentedVersionV1": "0.9.0",
            "nativeSetupFirstPresentedBuildV1": "45",
            "nativeSetupFirstPresentedAtV1": "100.0",
            "nativeSetupCompletedVersionV1": "0.9.0",
            "nativeSetupCompletedBuildV1": "45",
            "nativeSetupCompletedAtV1": "200.0",
        }

        result = _guided_setup_summary(
            preferences,
            expected_version="0.9.0",
            expected_build="45",
        )

        self.assertTrue(result["accepted"])
        preferences["nativeSetupCompletedBuildV1"] = "44"
        self.assertFalse(
            _guided_setup_summary(
                preferences,
                expected_version="0.9.0",
                expected_build="45",
            )["accepted"]
        )

    def test_launch_agent_exercise_requires_a_new_healthy_process(self) -> None:
        before = {
            "registered": True,
            "state": "running",
            "pid": 100,
            "runs": 1,
            "last_exit_code": None,
        }
        after = {
            "registered": True,
            "state": "running",
            "pid": 200,
            "runs": 2,
            "last_exit_code": 0,
        }
        with (
            patch(
                "macos.packaging.collect_acceptance._launch_agent",
                side_effect=[before, after, after],
            ),
            patch(
                "macos.packaging.collect_acceptance._run",
                return_value={"ok": True, "returncode": 0, "diagnostic": None},
            ) as run,
            patch(
                "macos.packaging.collect_acceptance._json_request",
                return_value={"ok": True, "status": 200, "payload": {}},
            ),
        ):
            result = _exercise_launch_agent(
                "keepalive",
                control_url="http://127.0.0.1:17321",
                public_url="http://127.0.0.1:1240",
                admin_password=None,
                timeout=1,
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(
            run.call_args.args[0],
            [
                "launchctl",
                "kill",
                "SIGTERM",
                f"gui/{os.getuid()}/com.mnemosyne.inference.agent",
            ],
        )

    def test_launch_agent_snapshot_includes_gui_audit_session(self) -> None:
        with patch(
            "macos.packaging.collect_acceptance._run",
            return_value={
                "ok": True,
                "returncode": 0,
                "diagnostic": (
                    "state = running\n"
                    "asid = 100108\n"
                    "runs = 3\n"
                    "pid = 88799\n"
                    "last exit code = 0\n"
                ),
            },
        ):
            result = _launch_agent()

        self.assertEqual(result["asid"], 100108)
        self.assertEqual(result["pid"], 88799)
        self.assertEqual(result["state"], "running")

    def test_login_cycle_requires_private_same_build_baseline_and_new_asid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "before-login.json"
            baseline.write_text(
                json.dumps(
                    {
                        "accepted": True,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "host": "Theseus",
                        "artifact": {
                            "app": {
                                "version": "0.9.0",
                                "build": "45",
                            }
                        },
                        "live": {
                            "accepted": True,
                            "launch_agent": {
                                "registered": True,
                                "state": "running",
                                "asid": 100,
                                "pid": 1000,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            baseline.chmod(0o600)

            result = _login_cycle_summary(
                baseline,
                current_agent={
                    "registered": True,
                    "state": "running",
                    "asid": 200,
                    "pid": 2000,
                },
                expected_version="0.9.0",
                expected_build="45",
                current_host="Theseus",
            )

            self.assertTrue(result["accepted"])
            self.assertTrue(all(result["checks"].values()))
            self.assertFalse(
                _login_cycle_summary(
                    baseline,
                    current_agent={
                        "registered": True,
                        "state": "running",
                        "asid": 100,
                        "pid": 2000,
                    },
                    expected_version="0.9.0",
                    expected_build="45",
                    current_host="Theseus",
                )["accepted"]
            )
            baseline.chmod(0o644)
            self.assertFalse(
                _login_cycle_summary(
                    baseline,
                    current_agent={
                        "registered": True,
                        "state": "running",
                        "asid": 200,
                        "pid": 2000,
                    },
                    expected_version="0.9.0",
                    expected_build="45",
                    current_host="Theseus",
                )["accepted"]
            )

    def test_live_collector_composes_restart_reconcile_and_strict_evidence(
        self,
    ) -> None:
        readiness = {
            "product_version": "0.9.0",
            "core": {"ready": True},
            "ready_for_inference": True,
            "engines": [
                {
                    "engine": "llama.cpp",
                    "enabled": True,
                    "authoritative": True,
                    "ready": True,
                }
            ],
        }
        config = {
            "config": {
                "schema_version": 2,
                "engines": {"llama_cpp": {"enabled": True}},
                "storage": {
                    "locations": [
                        {
                            "name": "athena",
                            "path": "/Volumes/Athena/models",
                            "scope_id": "a" * 64,
                            "volume_uuid": "volume-uuid",
                        }
                    ]
                },
                "models": [
                    {
                        "alias": "vision",
                        "engine": "llama.cpp",
                        "storage": "athena",
                        "load": {"projector_path": "/Volumes/Athena/mmproj.gguf"},
                    }
                ],
                "migration": {"legacy_lmstudio_profiles": []},
            },
            "revision": "same",
            "applied_revision": "same",
            "restart_required": False,
        }
        install_evidence = {
            "installs": [
                {
                    "id": "install",
                    "repo_id": "owner/model",
                    "engine": "llama.cpp",
                    "alias": "vision",
                    "status": "deleted",
                    "revision": "b" * 40,
                    "dismissed": True,
                    "events": [
                        {"event": "status", "status": "cancelled"},
                        {"event": "status", "status": "queued"},
                        {"event": "status", "status": "installed"},
                        {"event": "status", "status": "downloaded"},
                        {"event": "status", "status": "registering"},
                        {"event": "status", "status": "installed"},
                        {"event": "history_dismissed", "status": "installed"},
                        {"event": "status", "status": "deleted"},
                    ],
                }
            ]
        }
        runtime_evidence = {
            "journal": {
                "valid": True,
                "dropped_events": 0,
                "events": [
                    {
                        "sequence": 1,
                        "engine": "llama.cpp",
                        "action": "activated",
                        "outcome": "succeeded",
                        "service_instance_id": "instance-a",
                        "active_version_before": "b100",
                        "active_version_after": "b200",
                    },
                    {
                        "sequence": 2,
                        "engine": "llama.cpp",
                        "action": "inference_validated",
                        "outcome": "succeeded",
                        "service_instance_id": "instance-b",
                        "active_version_before": "b200",
                        "active_version_after": "b200",
                    },
                    {
                        "sequence": 3,
                        "engine": "llama.cpp",
                        "action": "rolled_back",
                        "outcome": "succeeded",
                        "service_instance_id": "instance-b",
                        "active_version_before": "b200",
                        "active_version_after": "b100",
                    },
                    {
                        "sequence": 4,
                        "engine": "llama.cpp",
                        "action": "inference_validated",
                        "outcome": "succeeded",
                        "service_instance_id": "instance-c",
                        "active_version_before": "b100",
                        "active_version_after": "b100",
                    },
                    {
                        "sequence": 5,
                        "engine": "llama.cpp",
                        "action": "install_rejected",
                        "outcome": "failed",
                        "service_instance_id": "instance-c",
                        "active_version_before": "b100",
                        "active_version_after": "b100",
                        "failure_code": "integrity",
                    },
                ],
            },
            "installed": {"llama.cpp": {"version": "b100"}},
        }

        def response(url: str, **kwargs: object) -> dict[str, object]:
            if url.endswith("/health"):
                payload: object = {"version": "0.9.0"}
            elif url.endswith("/manager/readiness"):
                payload = readiness
            elif url.endswith("/manager/status"):
                payload = {
                    "state": "idle",
                    "diagnostic": None,
                    "startup_error": None,
                }
            elif url.endswith("/manager/models"):
                payload = {"models": [{"alias": "vision"}]}
            elif "/manager/usage" in url:
                payload = {"rows": [], "token_sidecar": {"enabled": False}}
            elif url.endswith("/manager/storage"):
                payload = {
                    "locations": [
                        {
                            "name": "athena",
                            "exists": True,
                            "is_directory": True,
                            "writable": True,
                            "volume_matches": True,
                        }
                    ]
                }
            elif url.endswith("/manager/config"):
                payload = config
            elif "/manager/model-library/install-evidence" in url:
                payload = install_evidence
            elif url.endswith("/manager/model-library/local-sources"):
                payload = {"sources": []}
            elif url.endswith("/manager/runtime-updates/evidence"):
                payload = runtime_evidence
            elif url.endswith("/manager/reconcile"):
                payload = {
                    "state": "idle",
                    "diagnostic": None,
                    "startup_error": None,
                }
            elif url.endswith("/manager/self-test"):
                payload = {
                    "success": True,
                    "engine": "llama.cpp",
                    "release_tier": "stable",
                    "vision": True,
                    "usage": {"total_tokens": 7},
                    "usage_recorded": True,
                    "runtime_validation_recorded": True,
                }
            else:
                raise AssertionError(url)
            return {"ok": True, "status": 200, "payload": payload}

        agent = {
            "registered": True,
            "state": "running",
            "pid": 200,
            "runs": 2,
            "last_exit_code": 0,
        }
        with (
            patch(
                "macos.packaging.collect_acceptance._json_request",
                side_effect=response,
            ),
            patch(
                "macos.packaging.collect_acceptance._launch_agent",
                return_value=agent,
            ),
            patch(
                "macos.packaging.collect_acceptance._exercise_launch_agent",
                return_value={"accepted": True, "mode": "restart"},
            ),
            patch(
                "macos.packaging.collect_acceptance._guided_setup_preferences",
                return_value={
                    "didCompleteNativeSetupV1": "1",
                    "nativeSetupFirstPresentedVersionV1": "0.9.0",
                    "nativeSetupFirstPresentedBuildV1": "45",
                    "nativeSetupFirstPresentedAtV1": "100",
                    "nativeSetupCompletedVersionV1": "0.9.0",
                    "nativeSetupCompletedBuildV1": "45",
                    "nativeSetupCompletedAtV1": "200",
                },
            ),
        ):
            result = collect_live(
                expected_version="0.9.0",
                expected_build="45",
                control_url="http://127.0.0.1:17321",
                public_url="http://127.0.0.1:1240",
                admin_password=None,
                self_test_model="vision",
                include_vision=True,
                expected_engine="llama.cpp",
                require_vision=True,
                require_postgres_drain=False,
                postgres_timeout=1,
                launch_agent_exercise="restart",
                exercise_reconcile=True,
                require_protected_model=True,
                require_download_lifecycle=True,
                require_runtime_lifecycle="llama.cpp",
                require_guided_setup=True,
            )

        self.assertTrue(result["accepted"])
        self.assertTrue(all(result["checks"].values()))
        self.assertTrue(result["protected_model"]["accepted"])
        self.assertTrue(result["download_lifecycle"]["accepted"])
        self.assertTrue(result["runtime_lifecycle"]["accepted"])
        self.assertTrue(result["guided_setup"]["accepted"])

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
