"""Remove build-machine Swift search paths from staged executables before signing."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# The menu's only non-system framework is bundled Sparkle. Swift is supplied
# by the OS on every supported macOS version. Never ship Xcode/toolchain or
# checkout paths, even if a later rpath would resolve the bundled framework.
PORTABLE_SWIFT_RPATHS = frozenset({
    "/usr/lib/swift",
    "@loader_path",
    "@executable_path/../Frameworks",
})


def rpaths(load_commands: str) -> tuple[str, ...]:
    return tuple(re.findall(r"^\s*path (.+?) \(offset \d+\)", load_commands, re.MULTILINE))


def nonportable_rpaths(load_commands: str) -> tuple[str, ...]:
    return tuple(path for path in rpaths(load_commands) if path not in PORTABLE_SWIFT_RPATHS)


def normalize(executable: Path) -> None:
    def inspect() -> str:
        return subprocess.run(
            ["/usr/bin/otool", "-l", str(executable)],
            check=True, capture_output=True, text=True,
        ).stdout

    for path in dict.fromkeys(nonportable_rpaths(inspect())):
        subprocess.run(
            ["/usr/bin/install_name_tool", "-delete_rpath", path, str(executable)],
            check=True,
        )
    if nonportable_rpaths(inspect()):
        raise ValueError(f"{executable.name} still contains nonportable Swift rpaths")


if __name__ == "__main__":
    for value in sys.argv[1:]:
        normalize(Path(value))
