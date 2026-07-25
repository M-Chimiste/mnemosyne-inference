from __future__ import annotations

import plistlib
import unittest
from pathlib import Path


PACKAGING_ROOT = Path(__file__).resolve().parents[1]
AGENT_PLIST = (
    PACKAGING_ROOT
    / "LaunchAgents"
    / "com.mnemosyne.inference.agent.plist"
)
BUILD_SCRIPT = PACKAGING_ROOT / "build_app.sh"
DMG_BUILD_SCRIPT = PACKAGING_ROOT / "build_dmg.sh"
INFO_PLIST = PACKAGING_ROOT / "Info.plist"
APP_ICON = PACKAGING_ROOT / "AppIcon.icns"


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

    def test_dmg_builder_verifies_drag_install_layout(self) -> None:
        script = DMG_BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'ln -s /Applications "$SOURCE_DIR/Applications"',
            script,
        )
        self.assertIn("hdiutil verify", script)
        self.assertIn("hdiutil attach", script)
        self.assertIn("xcrun notarytool submit", script)
        self.assertIn("xcrun stapler staple", script)
        self.assertIn("xcrun stapler validate", script)
        self.assertIn(
            'codesign --verify --deep --strict --verbose=2 "$MOUNTED_APP"',
            script,
        )
        self.assertLess(
            script.index("hdiutil verify"),
            script.index('mv -f "$TEMP_DMG" "$OUTPUT_PATH"'),
        )


if __name__ == "__main__":
    unittest.main()
