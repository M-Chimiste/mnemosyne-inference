from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from macos.packaging.verify_release import (
    APP_FRAMEWORK_RPATH,
    SPARKLE_DEPENDENCY,
    _validate_acceptance,
    _validate_app_runtime_links,
)


PACKAGING_ROOT = Path(__file__).resolve().parents[1]
AGENT_PLIST = (
    PACKAGING_ROOT
    / "LaunchAgents"
    / "com.mnemosyne.inference.agent.plist"
)
BUILD_SCRIPT = PACKAGING_ROOT / "build_app.sh"
DMG_BUILD_SCRIPT = PACKAGING_ROOT / "build_dmg.sh"
VERIFY_RELEASE_SCRIPT = PACKAGING_ROOT / "verify_release.py"
COLLECT_ACCEPTANCE_SCRIPT = PACKAGING_ROOT / "collect_acceptance.py"
BOOTSTRAP_SOURCE = (
    PACKAGING_ROOT.parent
    / "app"
    / "Sources"
    / "MnemosyneServiceBootstrap"
    / "main.swift"
)
INFO_PLIST = PACKAGING_ROOT / "Info.plist"
APP_ICON = PACKAGING_ROOT / "AppIcon.icns"
VERSION_FILE = PACKAGING_ROOT.parent / "VERSION"


class AppLayoutTests(unittest.TestCase):
    def test_launch_agent_uses_direct_bundle_relative_helper(self) -> None:
        with AGENT_PLIST.open("rb") as stream:
            agent = plistlib.load(stream)

        self.assertEqual(
            agent["BundleProgram"],
            "Contents/MacOS/mnemosyne-service-bootstrap",
        )
        self.assertNotIn("AssociatedBundleIdentifiers", agent)
        self.assertNotIn(".app/", agent["BundleProgram"])

    def test_build_stages_and_signs_direct_helper(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'SERVICE_BOOTSTRAP="$CONTENTS/MacOS/mnemosyne-service-bootstrap"',
            script,
        )
        self.assertIn(
            "--identifier com.mnemosyne.inference.service",
            script,
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
        self.assertIn('verify_release.py" --app "$APP_DIR"', script)
        self.assertTrue(VERIFY_RELEASE_SCRIPT.is_file())

    def test_v1_release_fails_closed_on_pending_acceptance(self) -> None:
        pending = {
            "candidate_version": "1.0.0",
            "release_ready": False,
            "gates": [
                {
                    "id": "hardware",
                    "required": True,
                    "status": "pending",
                    "evidence": "Target-Mac run is still required.",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "V1 acceptance is not complete"):
            _validate_acceptance(pending, "1.0.0")

        candidate = dict(pending)
        candidate["candidate_version"] = "0.9.0"
        _validate_acceptance(candidate, "0.9.0")

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

    def test_release_verifier_requires_resolvable_packaged_sparkle(self) -> None:
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

    def test_dmg_builder_verifies_drag_install_layout(self) -> None:
        script = DMG_BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'ln -s /Applications "$SOURCE_DIR/Applications"',
            script,
        )
        self.assertIn("hdiutil verify", script)
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
