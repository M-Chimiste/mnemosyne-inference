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


if __name__ == "__main__":
    unittest.main()
