from __future__ import annotations

import json
import plistlib
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from macos.packaging.verify_release import (
    APP_FRAMEWORK_RPATH,
    LIFECYCLE_HELPER_ENTITLEMENT_KEYS,
    LIFECYCLE_HELPER_IDENTIFIER,
    LIFECYCLE_HELPER_INFO_PLIST,
    LIFECYCLE_HELPER_PROFILE_RELATIVE_PATH,
    LIFECYCLE_HELPER_RELATIVE_PATH,
    LIFECYCLE_HELPER_WRAPPER_RELATIVE_PATH,
    LIFECYCLE_PEER_MANIFEST_KEYS,
    LIFECYCLE_PEER_MANIFEST_RELATIVE_PATH,
    LIFECYCLE_RUNNER_IDENTIFIER,
    LIFECYCLE_RUNNER_RELATIVE_PATH,
    HUB_HELPER_IDENTIFIER,
    REQUIRED_MAC_POOL_ACCEPTANCE_GATE_IDS,
    SERVICE_HELPER_IDENTIFIER,
    SPARKLE_DEPENDENCY,
    TRASH_HELPER_IDENTIFIER,
    _validate_acceptance,
    _validate_app_runtime_links,
    _validate_bundled_bootstrap_isolation,
    _validate_fixed_info_plist,
    _validate_internal_symlinks,
    _validate_lifecycle_peer_manifest,
    _validate_source_copy,
    _codesign_requirement,
    _lifecycle_helper_profile_entitlements,
    _validate_distribution_assessment,
    _validate_lifecycle_helper_wrapper,
    _write_lifecycle_helper_entitlements,
    _write_lifecycle_peer_manifest,
    main as verify_release_main,
)


PACKAGING_ROOT = Path(__file__).resolve().parents[1]
AGENT_PLIST = (
    PACKAGING_ROOT
    / "LaunchAgents"
    / "com.mnemosyne.inference.agent.plist"
)
HUB_AGENT_PLIST = (
    PACKAGING_ROOT
    / "LaunchAgents"
    / "com.mnemosyne.inference.hub.plist"
)
BUILD_SCRIPT = PACKAGING_ROOT / "build_app.sh"
DMG_BUILD_SCRIPT = PACKAGING_ROOT / "build_dmg.sh"
PILOT_UNINSTALL_SCRIPT = (
    PACKAGING_ROOT / "pilot_uninstall_preserving_data.command"
)
VERIFY_RELEASE_SCRIPT = PACKAGING_ROOT / "verify_release.py"
COLLECT_ACCEPTANCE_SCRIPT = PACKAGING_ROOT / "collect_acceptance.py"
BOOTSTRAP_SOURCE = (
    PACKAGING_ROOT.parent
    / "app"
    / "Sources"
    / "MnemosyneServiceBootstrap"
    / "main.swift"
)
HUB_BOOTSTRAP_SOURCE = (
    PACKAGING_ROOT.parent
    / "app"
    / "Sources"
    / "MnemosyneHubBootstrap"
    / "main.swift"
)
LIFECYCLE_RUNNER_SOURCE = (
    PACKAGING_ROOT.parent
    / "app"
    / "Sources"
    / "MnemosyneLifecycleRunner"
    / "main.swift"
)
SETTINGS_VIEW_MODEL_SOURCE = (
    PACKAGING_ROOT.parent
    / "app"
    / "Sources"
    / "MnemosyneMenu"
    / "SettingsViewModel.swift"
)
LIFECYCLE_AUTHORIZATION_SESSION_SOURCE = (
    PACKAGING_ROOT.parent
    / "app"
    / "Sources"
    / "MnemosyneAppCore"
    / "NativeLifecycleAuthorizationSession.swift"
)
CONTROL_API_SOURCE = (
    PACKAGING_ROOT.parent
    / "app"
    / "Sources"
    / "MnemosyneAppCore"
    / "ControlAPIClient.swift"
)
LEGACY_MENU_HELPER_AUTHORIZER_SOURCE = (
    PACKAGING_ROOT.parent
    / "app"
    / "Sources"
    / "MnemosyneAppCore"
    / "LifecycleHelperAuthorizer.swift"
)
INFO_PLIST = PACKAGING_ROOT / "Info.plist"
APP_ICON = PACKAGING_ROOT / "AppIcon.icns"
VERSION_FILE = PACKAGING_ROOT.parent / "VERSION"


class AppLayoutTests(unittest.TestCase):
    def test_dmg_uses_finder_replacement_and_stages_preserve_data_uninstall(self) -> None:
        build = DMG_BUILD_SCRIPT.read_text(encoding="utf-8")
        uninstall = PILOT_UNINSTALL_SCRIPT.read_text(encoding="utf-8")

        self.assertFalse(
            (PACKAGING_ROOT / "pilot_install_or_upgrade.command").exists()
        )
        subprocess.run(
            ["/bin/bash", "-n", str(PILOT_UNINSTALL_SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertNotIn("pilot_install_or_upgrade.command", build)
        self.assertNotIn("Install or Upgrade Unified Inference.command", build)
        self.assertIn("pilot_uninstall_preserving_data.command", build)
        self.assertIn('/bin/bash -n "$MOUNTED_UNINSTALL"', build)

        self.assertIn('Type UNINSTALL to continue:', uninstall)
        self.assertIn('/bin/mv "$RUNTIME_ROOT" "$RUNTIME_TRASH"', uninstall)
        self.assertIn('/bin/mv "$TARGET_APP" "$APP_TRASH"', uninstall)
        self.assertNotIn('/bin/mv "$SUPPORT_ROOT"', uninstall)
        self.assertNotIn('/bin/mv "$ENV_PATH"', uninstall)
        self.assertNotIn("rm -rf", uninstall)

    def test_launch_agent_uses_direct_bundle_relative_helper(self) -> None:
        with AGENT_PLIST.open("rb") as stream:
            agent = plistlib.load(stream)

        self.assertEqual(
            agent["BundleProgram"],
            "Contents/MacOS/mnemosyne-service-bootstrap",
        )
        self.assertNotIn("AssociatedBundleIdentifiers", agent)
        self.assertNotIn(".app/", agent["BundleProgram"])

        with HUB_AGENT_PLIST.open("rb") as stream:
            hub_agent = plistlib.load(stream)
        self.assertEqual(
            hub_agent["BundleProgram"],
            "Contents/MacOS/mnemosyne-hub-bootstrap",
        )
        self.assertEqual(hub_agent["Label"], "com.mnemosyne.inference.hub")
        self.assertTrue(hub_agent["RunAtLoad"])
        self.assertTrue(hub_agent["KeepAlive"])

    def test_build_stages_and_signs_wrapped_helper(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'SERVICE_BOOTSTRAP="$CONTENTS/MacOS/mnemosyne-service-bootstrap"',
            script,
        )
        self.assertIn(
            "--identifier com.mnemosyne.inference.service",
            script,
        )
        self.assertIn(
            'HUB_BOOTSTRAP="$CONTENTS/MacOS/mnemosyne-hub-bootstrap"',
            script,
        )
        self.assertIn(f"--identifier {HUB_HELPER_IDENTIFIER}", script)
        self.assertIn('ditto "$REPO_ROOT/fleet/src" "$RESOURCES/Fleet"', script)
        self.assertIn(
            'FILE_TRASH_HELPER="$CONTENTS/MacOS/mnemosyne-file-trash"',
            script,
        )
        self.assertIn(
            "--identifier com.mnemosyne.inference.file-trash",
            script,
        )
        self.assertIn(
            'LIFECYCLE_HELPER_WRAPPER="$CONTENTS/Helpers/MnemosyneLifecycleAuthorization.app"',
            script,
        )
        self.assertIn(
            'LIFECYCLE_HELPER="$LIFECYCLE_HELPER_CONTENTS/MacOS/mnemosyne-lifecycle-helper"',
            script,
        )
        self.assertIn(
            f"--identifier {LIFECYCLE_HELPER_IDENTIFIER}",
            script,
        )
        self.assertIn(
            'LIFECYCLE_RUNNER="$CONTENTS/MacOS/mnemosyne-lifecycle-runner"',
            script,
        )
        self.assertIn(
            f"--identifier {LIFECYCLE_RUNNER_IDENTIFIER}",
            script,
        )
        self.assertIn("--write-lifecycle-peer-manifest", script)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", script)
        self.assertIn("PYTHONNOUSERSITE=1", script)
        self.assertIn("/usr/bin/env -i", script)
        self.assertLess(
            script.index('sign_mach_o_tree "$RESOURCES/Python"'),
            script.index("--write-lifecycle-peer-manifest"),
        )
        self.assertLess(
            script.index(f"--identifier {LIFECYCLE_RUNNER_IDENTIFIER}"),
            script.index("--write-lifecycle-peer-manifest"),
        )
        self.assertLess(
            script.index("--write-lifecycle-peer-manifest"),
            script.index('codesign "${CODESIGN_ARGS[@]}" "$APP_DIR"'),
        )
        self.assertNotIn("MnemosyneService.app", script)
        self.assertFalse(
            (PACKAGING_ROOT / "MnemosyneService-Info.plist").exists()
        )
        self.assertIn("--options runtime", script)
        self.assertIn("--timestamp", script)

    def test_signed_bundle_cannot_be_mutated_by_python_bytecode(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        bootstrap = BOOTSTRAP_SOURCE.read_text(encoding="utf-8")
        hub_bootstrap = HUB_BOOTSTRAP_SOURCE.read_text(encoding="utf-8")

        self.assertIn("-name '*.pyc' -o -name '*.pyo'", script)
        self.assertIn("-name '__pycache__' -empty -delete", script)
        self.assertLess(
            script.index("-name '*.pyc' -o -name '*.pyo'"),
            script.index("sign_mach_o_tree()"),
        )
        self.assertIn(
            'environment["PYTHONDONTWRITEBYTECODE"] = "1"',
            bootstrap,
        )
        self.assertIn(
            'environment["PYTHONDONTWRITEBYTECODE"] = "1"',
            hub_bootstrap,
        )
        self.assertIn('"mnemosyne_fleet.main"', hub_bootstrap)

    def test_lifecycle_runner_is_an_inert_inherited_socket_adapter(self) -> None:
        source = LIFECYCLE_RUNNER_SOURCE.read_text(encoding="utf-8")

        self.assertIn('arguments[1] == "--session-fd"', source)
        self.assertIn("socketType == SOCK_STREAM", source)
        self.assertIn("AF_UNIX", source)
        self.assertIn("LOCAL_PEERTOKEN", source)
        self.assertIn("LifecycleRunnerInertAdapterV2.refusalFrame", source)
        self.assertIn("Darwin.exit(78)", source)
        self.assertNotIn("Process(", source)
        self.assertNotIn("launchctl", source)
        self.assertNotIn("removeItem", source)
        self.assertNotIn("moveItem", source)

    def test_lifecycle_authorization_is_service_mediated(self) -> None:
        settings = SETTINGS_VIEW_MODEL_SOURCE.read_text(encoding="utf-8")
        session = LIFECYCLE_AUTHORIZATION_SESSION_SOURCE.read_text(
            encoding="utf-8"
        )
        control_api = CONTROL_API_SOURCE.read_text(encoding="utf-8")
        bootstrap = BOOTSTRAP_SOURCE.read_text(encoding="utf-8")

        self.assertFalse(LEGACY_MENU_HELPER_AUTHORIZER_SOURCE.exists())
        self.assertNotIn("LifecycleHelperAuthorizing", settings)
        self.assertNotIn("BundledLifecycleHelperAuthorizer", settings)
        self.assertNotIn("socketpair", settings)
        self.assertIn("performNativeLifecycleAuthorization", session)
        self.assertNotIn("issueNativeLifecycleAuthorizationChallenge", session)
        self.assertNotIn("submitNativeLifecycleAuthorizationReceipt", session)
        self.assertIn("performNativeLifecycleAuthorization", control_api)
        self.assertNotIn(
            "issueNativeLifecycleAuthorizationChallenge", control_api
        )
        self.assertNotIn(
            "submitNativeLifecycleAuthorizationReceipt", control_api
        )
        self.assertNotIn(
            "cancelNativeLifecycleAuthorizationChallenge", control_api
        )
        self.assertIn(
            'environment["MNEMOSYNE_LIFECYCLE_HELPER"] =',
            bootstrap,
        )
        self.assertNotIn(
            'environment["MNEMOSYNE_LIFECYCLE_HELPER"] ??',
            bootstrap,
        )

    def test_bundled_bootstrap_scrubs_ambient_python_environment(self) -> None:
        bootstrap = BOOTSTRAP_SOURCE.read_text(encoding="utf-8")

        self.assertIn(
            'environment.keys.filter { $0.hasPrefix("PYTHON") }',
            bootstrap,
        )
        self.assertIn('environment["PYTHONHOME"] = pythonHome.path', bootstrap)
        self.assertIn('environment["PYTHONNOUSERSITE"] = "1"', bootstrap)
        self.assertIn('environment["PYTHONSAFEPATH"] = "1"', bootstrap)
        self.assertIn(
            'environment.removeValue(forKey: "MNEMOSYNE_PYTHON_OVERRIDE")',
            bootstrap,
        )
        self.assertGreater(
            bootstrap.index('environment["MNEMOSYNE_PYTHON_OVERRIDE"]'),
            bootstrap.index("for child in (children ?? []).sorted"),
        )
        self.assertIn('if !usesBundledRuntime,', bootstrap)
        self.assertIn('"-B",\n        "-P",\n        "-s",', bootstrap)

    def test_release_probe_runs_full_bootstrap_with_hostile_python_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Unified Inference.app"
            bootstrap = (
                app / "Contents" / "MacOS" / "mnemosyne-service-bootstrap"
            )
            resources = app / "Contents" / "Resources"
            bootstrap.parent.mkdir(parents=True)
            resources.mkdir(parents=True)
            bootstrap.touch(mode=0o755)
            (resources / "config.yaml.example").write_text("models: []\n")
            (resources / ".env.example").write_text("")
            completed = subprocess.CompletedProcess(
                args=[str(bootstrap)],
                returncode=0,
                stdout="valid: inference=1240 control=17321 models=0\n",
                stderr="",
            )

            with patch(
                "macos.packaging.verify_release.subprocess.run",
                return_value=completed,
            ) as run:
                _validate_bundled_bootstrap_isolation(app)

            arguments = run.call_args.args[0]
            environment = run.call_args.kwargs["env"]
            self.assertEqual(arguments[0], str(bootstrap.absolute()))
            self.assertIn("--check-config", arguments)
            self.assertEqual(
                environment["MNEMOSYNE_PYTHON_OVERRIDE"],
                "/usr/bin/false",
            )
            self.assertEqual(environment["PYTHONHOME"], "/hostile/python-home")
            self.assertEqual(environment["PYTHONPATH"], "/hostile/python-path")
            self.assertNotIn("USER", environment)

    def test_bundle_declares_and_stages_the_app_icon(self) -> None:
        with INFO_PLIST.open("rb") as stream:
            info = plistlib.load(stream)
        script = BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertEqual(info["CFBundleIconFile"], "AppIcon.icns")
        self.assertTrue(APP_ICON.is_file())
        self.assertEqual(APP_ICON.read_bytes()[:4], b"icns")
        self.assertIn(
            'install -m 644 "$SCRIPT_DIR/AppIcon.icns" "$RESOURCES/AppIcon.icns"',
            script,
        )

    def test_bundle_explains_optional_local_network_inference(self) -> None:
        with INFO_PLIST.open("rb") as stream:
            info = plistlib.load(stream)

        description = info["NSLocalNetworkUsageDescription"]
        self.assertIn("inference requests", description)
        self.assertIn("local network", description)

    def test_build_injects_and_verifies_the_single_native_version(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
        with INFO_PLIST.open("rb") as stream:
            info = plistlib.load(stream)

        self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertEqual(info["CFBundleShortVersionString"], version)
        self.assertIn('VERSION_FILE="$REPO_ROOT/macos/VERSION"', script)
        self.assertIn("CFBundleShortVersionString", script)
        self.assertIn("CFBundleVersion", script)
        self.assertIn('VERIFY_RELEASE_ARGS=(--app "$APP_DIR")', script)
        self.assertIn('VERIFY_RELEASE_ARGS+=(--allow-bare)', script)
        self.assertTrue(VERIFY_RELEASE_SCRIPT.is_file())
        verifier = VERIFY_RELEASE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("sys.dont_write_bytecode = True", verifier)
        self.assertEqual(
            verifier.count("_validate_complete_app_seal(args.app)"),
            2,
        )

    def test_build_stages_normative_pool_schemas_and_rejects_stale_runtime(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            '"$REPO_ROOT/compatibility_catalog/v1/catalog.schema.json"',
            script,
        )
        self.assertIn(
            '"$RESOURCES/Service/mnemosyne_macos/schemas/compatibility_catalog.schema.json"',
            script,
        )
        self.assertIn(
            '"$REPO_ROOT/mac_pool_protocol/v1/desired_install.schema.json"',
            script,
        )
        self.assertIn(
            'build_runtime.py" --check-export "$PYTHON_EXPORT"',
            script,
        )
        self.assertIn(
            'build_runtime.py" --check-export "$RESOURCES/Python"',
            script,
        )

    def test_bare_staging_cannot_use_a_stable_signing_identity(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            '"$BARE" -eq 1 && "$CODESIGN_IDENTITY" != "-"',
            script,
        )
        self.assertIn("Bare app staging is restricted to ad-hoc signing", script)

    def test_bare_verification_cannot_be_combined_with_a_release_tag(self) -> None:
        with (
            patch(
                "sys.argv",
                [
                    "verify_release.py",
                    "--app",
                    "/tmp/Unified Inference.app",
                    "--allow-bare",
                    "--tag",
                    "v0.9.0",
                ],
            ),
            self.assertRaisesRegex(ValueError, "cannot be used with release-tag"),
        ):
            verify_release_main()

    def test_staged_source_inventory_rejects_unknown_files_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            staged = root / "staged"
            source.mkdir()
            staged.mkdir()
            (source / "app.py").write_text("pass\n", encoding="utf-8")
            (staged / "app.py").write_text("pass\n", encoding="utf-8")
            (staged / "debug.txt").write_text("unexpected\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source inventory mismatch"):
                _validate_source_copy(source, staged)

            (staged / "debug.txt").unlink()
            (staged / "alias.py").symlink_to("app.py")
            with self.assertRaisesRegex(ValueError, "must not contain symlinks"):
                _validate_source_copy(source, staged)

    def test_app_symlinks_must_be_relative_and_contained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Example.app"
            app.mkdir()
            (app / "target").write_text("ok", encoding="utf-8")
            (app / "inside").symlink_to("target")
            _validate_internal_symlinks(app, label="test app")

            outside = Path(directory) / "outside"
            outside.write_text("no", encoding="utf-8")
            (app / "escape").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "absolute symlink"):
                _validate_internal_symlinks(app, label="test app")

    def test_fixed_info_identity_and_helper_identifiers_are_closed(self) -> None:
        with INFO_PLIST.open("rb") as stream:
            info = plistlib.load(stream)
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory) / "Info.plist"
            staged.write_bytes(plistlib.dumps(info))
            _validate_fixed_info_plist(staged)

            info["CFBundleIdentifier"] = "com.example.wrong"
            staged.write_bytes(plistlib.dumps(info))
            with self.assertRaisesRegex(ValueError, "fixed product identity"):
                _validate_fixed_info_plist(staged)

        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(f"--identifier {SERVICE_HELPER_IDENTIFIER}", script)
        self.assertIn(f"--identifier {TRASH_HELPER_IDENTIFIER}", script)
        self.assertIn(f"--identifier {LIFECYCLE_HELPER_IDENTIFIER}", script)
        self.assertIn(f"--identifier {LIFECYCLE_RUNNER_IDENTIFIER}", script)

    def test_v1_release_fails_closed_on_pending_acceptance(self) -> None:
        pending = {
            "candidate_version": "1.0.0",
            "release_ready": False,
            "gates": [
                {
                    "id": gate_id,
                    "required": True,
                    "status": "pending",
                    "evidence": "Target-Mac run is still required.",
                }
                for gate_id in sorted(
                    REQUIRED_MAC_POOL_ACCEPTANCE_GATE_IDS
                )
            ],
        }
        with self.assertRaisesRegex(ValueError, "V1 acceptance is not complete"):
            _validate_acceptance(pending, "1.0.0")

        candidate = dict(pending)
        candidate["candidate_version"] = "0.9.0"
        _validate_acceptance(candidate, "0.9.0")

    def test_acceptance_required_gate_ids_are_unique_and_exact(self) -> None:
        def candidate() -> dict:
            return {
                "candidate_version": "0.9.0",
                "release_ready": False,
                "gates": [
                    {
                        "id": gate_id,
                        "required": True,
                        "status": "pending",
                        "evidence": "Candidate evidence remains pending.",
                    }
                    for gate_id in sorted(
                        REQUIRED_MAC_POOL_ACCEPTANCE_GATE_IDS
                    )
                ],
            }

        duplicate = candidate()
        duplicate["gates"].append(dict(duplicate["gates"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate gate IDs"):
            _validate_acceptance(duplicate, "0.9.0")

        missing = candidate()
        missing["gates"].pop()
        with self.assertRaisesRegex(ValueError, "required gate set mismatch"):
            _validate_acceptance(missing, "0.9.0")

        downgraded = candidate()
        downgraded["gates"][0]["required"] = False
        with self.assertRaisesRegex(ValueError, "required gate set mismatch"):
            _validate_acceptance(downgraded, "0.9.0")

        unexpected = candidate()
        unexpected["gates"].append(
            {
                "id": "unreviewed-required-gate",
                "required": True,
                "status": "pending",
                "evidence": "This required ID is not canonical.",
            }
        )
        with self.assertRaisesRegex(ValueError, "required gate set mismatch"):
            _validate_acceptance(unexpected, "0.9.0")

        optional = candidate()
        optional["gates"].append(
            {
                "id": "future-preview-observation",
                "required": False,
                "status": "pending",
                "evidence": "Optional future evidence may remain additive.",
            }
        )
        _validate_acceptance(optional, "0.9.0")

    def test_committed_acceptance_ledger_has_canonical_required_gates(self) -> None:
        acceptance = PACKAGING_ROOT.parent / "acceptance" / "v1.json"
        payload = json.loads(acceptance.read_text(encoding="utf-8"))

        _validate_acceptance(payload, "0.9.0")
        required_ids = {
            gate["id"] for gate in payload["gates"] if gate["required"]
        }
        self.assertEqual(
            required_ids,
            REQUIRED_MAC_POOL_ACCEPTANCE_GATE_IDS,
        )

    def test_signed_builds_require_and_embed_secure_sparkle_updates(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        with INFO_PLIST.open("rb") as stream:
            info = plistlib.load(stream)

        self.assertTrue(info["SUFeedURL"].startswith("https://"))
        self.assertTrue(info["SUVerifyUpdateBeforeExtraction"])
        self.assertIn("SPARKLE_PUBLIC_ED_KEY", script)
        self.assertIn("Developer ID builds require SPARKLE_PUBLIC_ED_KEY", script)
        self.assertIn('ditto "$BIN_DIR/Sparkle.framework"', script)
        self.assertIn('"$SPARKLE_VERSION/XPCServices/Installer.xpc"', script)
        self.assertIn("--preserve-metadata=entitlements", script)
        self.assertIn('"$SPARKLE_VERSION/Updater.app"', script)
        self.assertIn('codesign "${CODESIGN_ARGS[@]}" "$SPARKLE_FRAMEWORK"', script)
        self.assertIn(
            '-add_rpath "@executable_path/../Frameworks"',
            script,
        )

    def test_lifecycle_helper_profile_contract_is_exact_and_unexpired(self) -> None:
        team = "ABCDE12345"
        app_identifier = f"{team}.{LIFECYCLE_HELPER_IDENTIFIER}"
        profile = {
            "TeamIdentifier": [team],
            "ApplicationIdentifierPrefix": [f"{team}."],
            "ProvisionsAllDevices": True,
            "Platform": ["OSX"],
            "ExpirationDate": datetime.now(timezone.utc) + timedelta(days=30),
            "Entitlements": {
                "com.apple.application-identifier": app_identifier,
                "com.apple.developer.team-identifier": team,
                "keychain-access-groups": [app_identifier],
                "get-task-allow": False,
            },
        }
        expected = {
            "com.apple.application-identifier": app_identifier,
            "com.apple.developer.team-identifier": team,
            "keychain-access-groups": [app_identifier],
        }
        with patch(
            "macos.packaging.verify_release._decode_provisioning_profile",
            return_value=profile,
        ):
            self.assertEqual(
                _lifecycle_helper_profile_entitlements(Path("profile")),
                (team, expected),
            )

            wrong_group = dict(profile)
            wrong_group["Entitlements"] = {
                **profile["Entitlements"],
                "keychain-access-groups": [f"{team}.com.example.other"],
            }
            with (
                patch(
                    "macos.packaging.verify_release._decode_provisioning_profile",
                    return_value=wrong_group,
                ),
                self.assertRaisesRegex(ValueError, "keychain-access-groups"),
            ):
                _lifecycle_helper_profile_entitlements(Path("profile"))

            expired = dict(profile)
            expired["ExpirationDate"] = datetime.now(timezone.utc) - timedelta(days=1)
            with (
                patch(
                    "macos.packaging.verify_release._decode_provisioning_profile",
                    return_value=expired,
                ),
                self.assertRaisesRegex(ValueError, "expired"),
            ):
                _lifecycle_helper_profile_entitlements(Path("profile"))

            debuggable = dict(profile)
            debuggable["Entitlements"] = {
                **profile["Entitlements"],
                "get-task-allow": True,
            }
            with (
                patch(
                    "macos.packaging.verify_release._decode_provisioning_profile",
                    return_value=debuggable,
                ),
                self.assertRaisesRegex(ValueError, "debugging"),
            ):
                _lifecycle_helper_profile_entitlements(Path("profile"))

    def test_lifecycle_helper_entitlement_output_is_private_and_closed(self) -> None:
        team = "ABCDE12345"
        entitlements = {
            "com.apple.application-identifier": (
                f"{team}.{LIFECYCLE_HELPER_IDENTIFIER}"
            ),
            "com.apple.developer.team-identifier": team,
            "keychain-access-groups": [
                f"{team}.{LIFECYCLE_HELPER_IDENTIFIER}"
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "helper-entitlements.plist"
            with patch(
                "macos.packaging.verify_release._lifecycle_helper_profile_entitlements",
                return_value=(team, entitlements),
            ):
                _write_lifecycle_helper_entitlements(
                    Path(directory) / "profile.provisionprofile",
                    destination,
                )
            with destination.open("rb") as stream:
                written = plistlib.load(stream)
            self.assertEqual(written, entitlements)
            self.assertEqual(set(written), LIFECYCLE_HELPER_ENTITLEMENT_KEYS)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_unprofiled_helper_wrapper_never_claims_distribution_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Unified Inference.app"
            outer_info = app / "Contents" / "Info.plist"
            wrapper = app / LIFECYCLE_HELPER_WRAPPER_RELATIVE_PATH
            helper_info = wrapper / "Contents" / "Info.plist"
            helper = app / LIFECYCLE_HELPER_RELATIVE_PATH
            outer_info.parent.mkdir(parents=True)
            helper.parent.mkdir(parents=True)
            with (PACKAGING_ROOT / "Info.plist").open("rb") as stream:
                outer = plistlib.load(stream)
            with LIFECYCLE_HELPER_INFO_PLIST.open("rb") as stream:
                nested = plistlib.load(stream)
            outer_info.write_bytes(plistlib.dumps(outer))
            helper_info.write_bytes(plistlib.dumps(nested))
            helper.touch(mode=0o755)

            def identifier(_path: Path) -> str:
                return LIFECYCLE_HELPER_IDENTIFIER

            with (
                patch(
                    "macos.packaging.verify_release._codesign_identifier",
                    side_effect=identifier,
                ),
                patch(
                    "macos.packaging.verify_release._codesign_entitlements",
                    return_value={},
                ),
            ):
                self.assertFalse(
                    _validate_lifecycle_helper_wrapper(
                        app,
                        require_profiled_authority=False,
                    )
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "requires the lifecycle helper provisioning profile",
                ):
                    _validate_lifecycle_helper_wrapper(
                        app,
                        require_profiled_authority=True,
                    )

    def test_profiled_wrapper_requires_exact_signature_entitlements_and_team(self) -> None:
        team = "ABCDE12345"
        app_identifier = f"{team}.{LIFECYCLE_HELPER_IDENTIFIER}"
        entitlements = {
            "com.apple.application-identifier": app_identifier,
            "com.apple.developer.team-identifier": team,
            "keychain-access-groups": [app_identifier],
        }
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Unified Inference.app"
            wrapper = app / LIFECYCLE_HELPER_WRAPPER_RELATIVE_PATH
            helper = app / LIFECYCLE_HELPER_RELATIVE_PATH
            profile = app / LIFECYCLE_HELPER_PROFILE_RELATIVE_PATH
            outer_info = app / "Contents" / "Info.plist"
            helper_info = wrapper / "Contents" / "Info.plist"
            outer_info.parent.mkdir(parents=True)
            helper.parent.mkdir(parents=True)
            with (PACKAGING_ROOT / "Info.plist").open("rb") as stream:
                outer = plistlib.load(stream)
            with LIFECYCLE_HELPER_INFO_PLIST.open("rb") as stream:
                nested = plistlib.load(stream)
            outer_info.write_bytes(plistlib.dumps(outer))
            helper_info.write_bytes(plistlib.dumps(nested))
            helper.touch(mode=0o755)
            profile.write_bytes(b"fixture profile")

            with (
                patch(
                    "macos.packaging.verify_release._codesign_identifier",
                    return_value=LIFECYCLE_HELPER_IDENTIFIER,
                ),
                patch(
                    "macos.packaging.verify_release._codesign_entitlements",
                    return_value=entitlements,
                ),
                patch(
                    "macos.packaging.verify_release._lifecycle_helper_profile_entitlements",
                    return_value=(team, entitlements),
                ),
                patch(
                    "macos.packaging.verify_release._codesign_team_identifier",
                    return_value=team,
                ),
                patch(
                    "macos.packaging.verify_release._validate_distribution_signature"
                ) as validate_signature,
            ):
                self.assertTrue(
                    _validate_lifecycle_helper_wrapper(
                        app,
                        require_profiled_authority=True,
                    )
                )
                self.assertEqual(validate_signature.call_count, 3)

            with (
                patch(
                    "macos.packaging.verify_release._codesign_identifier",
                    return_value=LIFECYCLE_HELPER_IDENTIFIER,
                ),
                patch(
                    "macos.packaging.verify_release._codesign_entitlements",
                    return_value={**entitlements, "com.example.extra": True},
                ),
                patch(
                    "macos.packaging.verify_release._lifecycle_helper_profile_entitlements",
                    return_value=(team, entitlements),
                ),
                self.assertRaisesRegex(ValueError, "open entitlement allowlist"),
            ):
                _validate_lifecycle_helper_wrapper(
                    app,
                    require_profiled_authority=True,
                )

    def test_distribution_assessment_requires_stapler_and_gatekeeper(self) -> None:
        app = Path("/tmp/Unified Inference.app")
        accepted = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="accepted", stderr=""
        )
        with patch(
            "macos.packaging.verify_release.subprocess.run",
            side_effect=[accepted, accepted],
        ) as run:
            _validate_distribution_assessment(app)
            self.assertEqual(run.call_count, 2)

        rejected = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="rejected"
        )
        with (
            patch(
                "macos.packaging.verify_release.subprocess.run",
                return_value=rejected,
            ),
            self.assertRaisesRegex(ValueError, "notarization and Gatekeeper"),
        ):
            _validate_distribution_assessment(app)

    def test_release_verifier_requires_resolvable_packaged_sparkle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Unified Inference.app"
            executable = app / "Contents" / "MacOS" / "UnifiedInference"
            trash_helper = app / "Contents" / "MacOS" / "mnemosyne-file-trash"
            lifecycle_helper = app / LIFECYCLE_HELPER_RELATIVE_PATH
            lifecycle_runner = app / LIFECYCLE_RUNNER_RELATIVE_PATH
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
            lifecycle_helper.parent.mkdir(parents=True)
            sparkle.parent.mkdir(parents=True)
            executable.touch()
            trash_helper.touch(mode=0o755)
            lifecycle_helper.touch(mode=0o755)
            lifecycle_runner.touch(mode=0o755)
            sparkle.touch()
            valid = [
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=f"{SPARKLE_DEPENDENCY}\\n",
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=f"path {APP_FRAMEWORK_RPATH} (offset 12)\\n",
                    stderr="",
                ),
            ]
            with patch(
                "macos.packaging.verify_release.subprocess.run",
                side_effect=valid,
            ):
                _validate_app_runtime_links(app)

            missing_rpath = [
                valid[0],
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="path @loader_path (offset 12)\\n",
                    stderr="",
                ),
            ]
            with (
                patch(
                    "macos.packaging.verify_release.subprocess.run",
                    side_effect=missing_rpath,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "cannot resolve Sparkle",
                ),
            ):
                _validate_app_runtime_links(app)

    def test_lifecycle_peer_manifest_binds_all_signed_roles_and_app_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Unified Inference.app"
            info = {
                "CFBundleIdentifier": "com.mnemosyne.inference.menu",
                "CFBundleShortVersionString": "0.9.0",
                "CFBundleVersion": "7",
            }
            info_path = app / "Contents" / "Info.plist"
            helper = app / LIFECYCLE_HELPER_RELATIVE_PATH
            runner = app / LIFECYCLE_RUNNER_RELATIVE_PATH
            bootstrap = (
                app / "Contents" / "MacOS" / "mnemosyne-service-bootstrap"
            )
            trash_helper = app / "Contents" / "MacOS" / "mnemosyne-file-trash"
            peer = (
                app
                / "Contents"
                / "Resources"
                / "Python"
                / "cpython-3.12"
                / "bin"
                / "python3"
            )
            info_path.parent.mkdir(parents=True)
            helper.parent.mkdir(parents=True, exist_ok=True)
            runner.parent.mkdir(parents=True, exist_ok=True)
            peer.parent.mkdir(parents=True)
            info_path.write_bytes(plistlib.dumps(info))
            helper.touch(mode=0o755)
            runner.touch(mode=0o755)
            bootstrap.touch(mode=0o755)
            trash_helper.touch(mode=0o755)
            peer.touch(mode=0o755)

            def identifier(path: Path) -> str:
                if path == helper:
                    return LIFECYCLE_HELPER_IDENTIFIER
                if path == runner:
                    return LIFECYCLE_RUNNER_IDENTIFIER
                if path == peer:
                    return "org.python.python"
                if path == app:
                    return "com.mnemosyne.inference.menu"
                if path == bootstrap:
                    return SERVICE_HELPER_IDENTIFIER
                if path == trash_helper:
                    return TRASH_HELPER_IDENTIFIER
                raise AssertionError(path)

            def cdhash(path: Path) -> str:
                if path == helper:
                    return "a" * 40
                if path == runner:
                    return "b" * 40
                return "c" * 40

            with (
                patch(
                    "macos.packaging.verify_release._codesign_identifier",
                    side_effect=identifier,
                ),
                patch(
                    "macos.packaging.verify_release._codesign_team_identifier",
                    return_value="ABCDE12345",
                ),
                patch(
                    "macos.packaging.verify_release._codesign_cdhash",
                    side_effect=cdhash,
                ),
                patch(
                    "macos.packaging.verify_release._codesign_requirement",
                    side_effect=lambda path: f"identifier {identifier(path)}",
                ),
                patch(
                    "macos.packaging.verify_release._codesign_details",
                    return_value="Signature=adhoc\nTeamIdentifier=ABCDE12345\n",
                ),
            ):
                manifest_path = _write_lifecycle_peer_manifest(app)
                payload = json.loads(manifest_path.read_bytes())
                self.assertEqual(set(payload), LIFECYCLE_PEER_MANIFEST_KEYS)
                self.assertEqual(
                    payload["service_python_relative_path"],
                    "Contents/Resources/Python/cpython-3.12/bin/python3",
                )
                self.assertEqual(
                    payload["helper_identifier"],
                    LIFECYCLE_HELPER_IDENTIFIER,
                )
                self.assertEqual(
                    payload["runner_identifier"],
                    LIFECYCLE_RUNNER_IDENTIFIER,
                )
                self.assertFalse(payload["service_python_authoritative"])
                self.assertEqual(payload["expected_team_identifier"], "ABCDE12345")
                _validate_lifecycle_peer_manifest(app, allow_bare=False)

                with (
                    patch(
                        "macos.packaging.verify_release._codesign_identifier",
                        side_effect=lambda path: (
                            "com.example.wrong-runner"
                            if path == runner
                            else identifier(path)
                        ),
                    ),
                    self.assertRaisesRegex(
                        ValueError,
                        "runner has the wrong code-signing identifier",
                    ),
                ):
                    _validate_lifecycle_peer_manifest(app, allow_bare=False)

                with (
                    patch(
                        "macos.packaging.verify_release._codesign_team_identifier",
                        side_effect=lambda path: (
                            "OTHER12345" if path == runner else "ABCDE12345"
                        ),
                    ),
                    self.assertRaisesRegex(ValueError, "service Python teams differ"),
                ):
                    _validate_lifecycle_peer_manifest(app, allow_bare=False)

                tampered = dict(payload)
                tampered["runner_cdhash"] = "d" * 40
                manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "does not match staged code"):
                    _validate_lifecycle_peer_manifest(app, allow_bare=False)
                manifest_path = _write_lifecycle_peer_manifest(app)
                payload = json.loads(manifest_path.read_bytes())

                with (
                    patch(
                        "macos.packaging.verify_release._codesign_team_identifier",
                        side_effect=lambda path: (
                            "OTHER12345" if path == bootstrap else "ABCDE12345"
                        ),
                    ),
                    self.assertRaisesRegex(ValueError, "helper teams differ"),
                ):
                    _validate_lifecycle_peer_manifest(app, allow_bare=False)

                def developer_id_details(path: Path) -> str:
                    runtime = "" if path == runner else " flags=0x10000(runtime)"
                    return (
                        "Authority=Developer ID Application: Example (ABCDE12345)\n"
                        f"CodeDirectory{runtime}\n"
                    )

                with (
                    patch(
                        "macos.packaging.verify_release._codesign_details",
                        side_effect=developer_id_details,
                    ),
                    self.assertRaisesRegex(ValueError, "lacks the hardened runtime"),
                ):
                    _validate_lifecycle_peer_manifest(app, allow_bare=False)

                payload["path"] = "/tmp/other-python"
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "open schema"):
                    _validate_lifecycle_peer_manifest(app, allow_bare=False)

    def test_ad_hoc_designated_requirement_comment_is_normalized(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codesign"],
            returncode=0,
            stdout="",
            stderr=(
                "Executable=/tmp/helper\n"
                '# designated => cdhash H"0123456789abcdef"\n'
            ),
        )
        with patch(
            "macos.packaging.verify_release.subprocess.run",
            return_value=completed,
        ):
            self.assertEqual(
                _codesign_requirement(Path("/tmp/helper")),
                'cdhash H"0123456789abcdef"',
            )

    def test_dmg_builder_verifies_drag_install_layout(self) -> None:
        script = DMG_BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'ln -s /Applications "$SOURCE_DIR/Applications"',
            script,
        )
        self.assertIn("hdiutil verify", script)
        self.assertIn("run_isolated_verify_release --app", script)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", script)
        self.assertIn("PYTHONNOUSERSITE=1", script)
        self.assertIn("/usr/bin/env -i", script)
        self.assertIn("hdiutil attach", script)
        self.assertIn("xcrun notarytool submit", script)
        self.assertIn('ditto -c -k --keepParent "$APP_PATH" "$APP_ZIP"', script)
        self.assertIn('xcrun stapler staple "$APP_PATH"', script)
        self.assertIn('xcrun stapler validate "$MOUNTED_APP"', script)
        self.assertIn("--type execute", script)
        self.assertIn("xcrun stapler staple", script)
        self.assertIn("xcrun stapler validate", script)
        self.assertIn("Authority=Developer ID Application:", script)
        self.assertIn("Notarization releases require an embedded Sparkle public key.", script)
        self.assertIn("Notarization releases require an HTTPS Sparkle feed.", script)
        self.assertIn("spctl", script)
        self.assertIn(
            'codesign --verify --deep --strict --verbose=2 "$MOUNTED_APP"',
            script,
        )
        self.assertEqual(
            script.count("run_isolated_verify_release --app"),
            2,
        )
        self.assertNotIn("--allow-bare", script)
        self.assertLess(
            script.index("hdiutil verify"),
            script.index('mv -f "$TEMP_DMG" "$OUTPUT_PATH"'),
        )
        self.assertTrue(COLLECT_ACCEPTANCE_SCRIPT.is_file())
        self.assertIn('collect_acceptance.py" "${ACCEPTANCE_ARGS[@]}"', script)
        self.assertIn("--require-distribution", script)
        self.assertGreater(
            script.index('collect_acceptance.py" "${ACCEPTANCE_ARGS[@]}"'),
            script.index('mv -f "$TEMP_DMG" "$OUTPUT_PATH"'),
        )


if __name__ == "__main__":
    unittest.main()
