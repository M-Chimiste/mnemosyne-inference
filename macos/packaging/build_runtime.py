#!/usr/bin/env python3
"""Build relocatable Python layers for the native Mnemosyne app."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


PACKAGING_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGING_DIR.parent.parent
TEMPLATE = PACKAGING_DIR / "venvstacks.toml"
SERVICE_DIR = REPO_ROOT / "macos" / "service"
SERVICE_LOCK = SERVICE_DIR / "uv.lock"
RESOLVED = PACKAGING_DIR / "_venvstacks.resolved.toml"
DEFAULT_EXPORT = PACKAGING_DIR / "_export"
MARKER = 'requirements = [] # __MNEMOSYNE_REQUIREMENTS__'
PROVENANCE_MARKER = "# __MNEMOSYNE_LOCK_PROVENANCE__"
EXACT_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?"
    r"==[^\s;*]+(?:\s*;\s*.+)?$"
)


def uv_driver() -> str:
    command = shutil.which("uv")
    if command is None:
        raise RuntimeError(
            "uv is required to export the committed macos/service/uv.lock"
        )
    return command


def parse_locked_requirements(exported: str) -> tuple[str, ...]:
    """Return exact, production-only requirement pins emitted by uv."""

    requirements: list[str] = []
    for raw_line in exported.splitlines():
        requirement = raw_line.strip()
        if not requirement or requirement.startswith("#"):
            continue
        if not EXACT_REQUIREMENT.fullmatch(requirement):
            raise RuntimeError(
                "uv export emitted a non-exact or unsupported requirement: "
                f"{requirement!r}"
            )
        requirements.append(requirement)
    if not requirements:
        raise RuntimeError(f"no production requirements were exported from {SERVICE_LOCK}")
    if len(requirements) != len(set(requirements)):
        raise RuntimeError(f"duplicate requirements were exported from {SERVICE_LOCK}")
    return tuple(sorted(requirements, key=str.casefold))


def locked_requirements() -> tuple[str, ...]:
    """Export exact production pins without resolving beyond the committed lock."""

    if not SERVICE_LOCK.is_file():
        raise RuntimeError(f"committed service lock is missing: {SERVICE_LOCK}")
    command = [
        uv_driver(),
        "export",
        "--project",
        str(SERVICE_DIR),
        "--locked",
        "--offline",
        "--no-cache",
        "--no-dev",
        "--no-emit-project",
        "--no-hashes",
        "--no-header",
        "--no-annotate",
        "--format",
        "requirements.txt",
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            "could not export the committed service lock with `uv export --locked`: "
            f"{detail}"
        )
    return parse_locked_requirements(completed.stdout)


def lock_digest() -> str:
    return hashlib.sha256(SERVICE_LOCK.read_bytes()).hexdigest()


def resolved_config(requirements: Sequence[str] | None = None) -> str:
    if requirements is None:
        requirements = locked_requirements()
    rendered = "requirements = [\n" + "".join(
        f"  {json.dumps(requirement)},\n" for requirement in requirements
    ) + "]"
    template = TEMPLATE.read_text(encoding="utf-8")
    if MARKER not in template:
        raise RuntimeError(f"requirements marker missing from {TEMPLATE}")
    if PROVENANCE_MARKER not in template:
        raise RuntimeError(f"lock provenance marker missing from {TEMPLATE}")
    provenance = (
        "# Exact production pins exported from macos/service/uv.lock\n"
        f"# uv.lock sha256: {lock_digest()}"
    )
    return template.replace(PROVENANCE_MARKER, provenance, 1).replace(
        MARKER, rendered, 1
    )


def driver() -> list[str]:
    if importlib.util.find_spec("venvstacks") is not None:
        return [sys.executable, "-m", "venvstacks"]
    if shutil.which("uvx") is not None:
        return ["uvx", "venvstacks"]
    if shutil.which("pipx") is not None:
        return ["pipx", "run", "venvstacks"]
    raise RuntimeError("venvstacks is unavailable; install it or install uv/pipx")


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def build(output_dir: Path) -> None:
    RESOLVED.write_text(resolved_config(), encoding="utf-8")
    command = driver()
    try:
        run(command + ["lock", str(RESOLVED), "--if-needed"])
        run(command + ["build", str(RESOLVED)])
        run(
            command
            + [
                "local-export",
                str(RESOLVED),
                "--output-dir",
                str(output_dir),
                "--force",
            ]
        )
    finally:
        RESOLVED.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_EXPORT,
        help="venvstacks export destination",
    )
    parser.add_argument(
        "--print-resolved",
        action="store_true",
        help="print generated venvstacks TOML without downloading anything",
    )
    parser.add_argument(
        "--check-lock",
        action="store_true",
        help="validate that uv.lock is current and exports only exact production pins",
    )
    args = parser.parse_args()
    if args.check_lock:
        requirements = locked_requirements()
        print(
            f"validated {len(requirements)} production pins from "
            f"macos/service/uv.lock (sha256:{lock_digest()})"
        )
        return
    if args.print_resolved:
        print(resolved_config(), end="")
        return
    build(args.output_dir.resolve())


if __name__ == "__main__":
    main()
