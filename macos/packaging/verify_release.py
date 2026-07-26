#!/usr/bin/env python3
"""Verify the native product's single release version and built artifact."""

from __future__ import annotations

import argparse
import json
import plistlib
from pathlib import Path
import re
import sys


MACOS_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = MACOS_ROOT / "VERSION"
ACCEPTANCE_FILE = MACOS_ROOT / "acceptance" / "v1.json"
INFO_PLIST = MACOS_ROOT / "packaging" / "Info.plist"
SERVICE_PROJECT = MACOS_ROOT / "service" / "pyproject.toml"
SERVICE_LOCK = MACOS_ROOT / "service" / "uv.lock"
SERVICE_INIT = MACOS_ROOT / "service" / "src" / "mnemosyne_macos" / "__init__.py"
WORKER_PROJECT = MACOS_ROOT / "image-worker" / "pyproject.toml"
WORKER_LOCK = MACOS_ROOT / "image-worker" / "uv.lock"
WORKER_INIT = (
    MACOS_ROOT
    / "image-worker"
    / "src"
    / "mnemosyne_mflux_worker"
    / "__init__.py"
)
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _project_version(path: Path) -> str:
    payload = path.read_text(encoding="utf-8")
    project = re.search(
        r"(?ms)^\[project\]\s*$\n(.*?)(?=^\[|\Z)",
        payload,
    )
    if project is None:
        raise ValueError(f"{path} has no [project] table")
    version = re.search(
        r'^version\s*=\s*"([^"]+)"\s*$',
        project.group(1),
        re.MULTILINE,
    )
    if version is None:
        raise ValueError(f"{path} has no project version")
    return version.group(1)


def _init_version(path: Path) -> str:
    match = re.search(
        r'^__version__\s*=\s*"([^"]+)"\s*$',
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise ValueError(f"{path} does not define __version__")
    return match.group(1)


def _locked_project_version(path: Path, package: str) -> str:
    payload = path.read_text(encoding="utf-8")
    match = re.search(
        rf'\[\[package\]\]\s+name = "{re.escape(package)}"\s+version = "([^"]+)"',
        payload,
    )
    if match is None:
        raise ValueError(f"{path} does not lock {package}")
    return match.group(1)


def _plist_version(path: Path) -> str:
    with path.open("rb") as stream:
        return str(plistlib.load(stream)["CFBundleShortVersionString"])


def _validate_acceptance(payload: dict, expected: str) -> None:
    if payload.get("candidate_version") != expected:
        raise ValueError(
            f"{ACCEPTANCE_FILE} candidate_version must be {expected}"
        )
    gates = payload.get("gates")
    if not isinstance(gates, list) or not gates:
        raise ValueError(f"{ACCEPTANCE_FILE} must contain acceptance gates")
    malformed = [
        gate
        for gate in gates
        if not isinstance(gate, dict)
        or not isinstance(gate.get("id"), str)
        or gate.get("status") not in {"passed", "pending"}
        or not isinstance(gate.get("required"), bool)
        or not isinstance(gate.get("evidence"), str)
    ]
    if malformed:
        raise ValueError(f"{ACCEPTANCE_FILE} contains malformed gates")

    major = int(expected.split(".", 1)[0])
    pending = [
        str(gate["id"])
        for gate in gates
        if gate["required"] and gate["status"] != "passed"
    ]
    if major >= 1 and (not payload.get("release_ready") or pending):
        details = ", ".join(pending) if pending else "release_ready is false"
        raise ValueError(f"V1 acceptance is not complete: {details}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--app",
        type=Path,
        help="also verify a staged Unified Inference.app",
    )
    parser.add_argument(
        "--tag",
        help="also require this release tag (for example v1.0.0)",
    )
    args = parser.parse_args()

    expected = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(expected):
        raise ValueError(f"{VERSION_FILE} must contain MAJOR.MINOR.PATCH")
    _validate_acceptance(
        json.loads(ACCEPTANCE_FILE.read_text(encoding="utf-8")),
        expected,
    )

    versions = {
        "Info.plist": _plist_version(INFO_PLIST),
        "service project": _project_version(SERVICE_PROJECT),
        "service package": _init_version(SERVICE_INIT),
        "service lock": _locked_project_version(
            SERVICE_LOCK, "mnemosyne-macos"
        ),
        "image worker project": _project_version(WORKER_PROJECT),
        "image worker package": _init_version(WORKER_INIT),
        "image worker lock": _locked_project_version(
            WORKER_LOCK, "mnemosyne-mflux-worker"
        ),
    }
    if args.app is not None:
        versions["staged app"] = _plist_version(
            args.app / "Contents" / "Info.plist"
        )
    mismatches = {
        source: version
        for source, version in versions.items()
        if version != expected
    }
    if mismatches:
        details = ", ".join(
            f"{source}={version}" for source, version in mismatches.items()
        )
        raise ValueError(f"native release version is {expected}, but {details}")

    if args.tag is not None and args.tag != f"v{expected}":
        raise ValueError(
            f"release tag {args.tag!r} does not match native version v{expected}"
        )

    print(f"Unified Inference native release version {expected} is consistent.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ValueError) as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
