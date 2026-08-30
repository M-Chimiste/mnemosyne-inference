#!/usr/bin/env python3
"""Verify the native product's single release version and built artifact."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import plistlib
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

# Release verification must remain observational even when invoked without the
# packaging shell wrappers. In particular, importing the sealed CPython
# runtime must never add __pycache__ members to an already signed app.
sys.dont_write_bytecode = True

try:
    from . import build_runtime as runtime_packaging
except ImportError:
    import build_runtime as runtime_packaging


MACOS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MACOS_ROOT.parent
VERSION_FILE = MACOS_ROOT / "VERSION"
ACCEPTANCE_FILE = MACOS_ROOT / "acceptance" / "v1.json"
INFO_PLIST = MACOS_ROOT / "packaging" / "Info.plist"
LIFECYCLE_HELPER_INFO_PLIST = (
    MACOS_ROOT / "packaging" / "LifecycleHelper-Info.plist"
)
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
SPARKLE_DEPENDENCY = "@rpath/Sparkle.framework/Versions/B/Sparkle"
APP_FRAMEWORK_RPATH = "@executable_path/../Frameworks"
SERVICE_SOURCE = MACOS_ROOT / "service" / "src" / "mnemosyne_macos"
SERVICE_SOURCE_ROOT = SERVICE_SOURCE.parent
IMAGE_WORKER_SOURCE = (
    MACOS_ROOT / "image-worker" / "src" / "mnemosyne_mflux_worker"
)
IMAGE_WORKER_SOURCE_ROOT = IMAGE_WORKER_SOURCE.parent
IMAGE_CAPABILITIES = MACOS_ROOT / "image-worker" / "capabilities.json"
CATALOG_SCHEMA = REPO_ROOT / "compatibility_catalog" / "v1" / "catalog.schema.json"
DESIRED_INSTALL_SCHEMA = (
    REPO_ROOT / "mac_pool_protocol" / "v1" / "desired_install.schema.json"
)
AGENT_PLIST = (
    MACOS_ROOT
    / "packaging"
    / "LaunchAgents"
    / "com.mnemosyne.inference.agent.plist"
)
SERVICE_HELPER_IDENTIFIER = "com.mnemosyne.inference.service"
TRASH_HELPER_IDENTIFIER = "com.mnemosyne.inference.file-trash"
LIFECYCLE_HELPER_IDENTIFIER = "com.mnemosyne.inference.lifecycle-helper"
LIFECYCLE_HELPER_WRAPPER_RELATIVE_PATH = Path(
    "Contents/Helpers/MnemosyneLifecycleAuthorization.app"
)
LIFECYCLE_HELPER_RELATIVE_PATH = Path(
    "Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/"
    "mnemosyne-lifecycle-helper"
)
LIFECYCLE_HELPER_PROFILE_RELATIVE_PATH = (
    LIFECYCLE_HELPER_WRAPPER_RELATIVE_PATH
    / "Contents"
    / "embedded.provisionprofile"
)
LIFECYCLE_HELPER_ENTITLEMENT_KEYS = frozenset(
    {
        "com.apple.application-identifier",
        "com.apple.developer.team-identifier",
        "keychain-access-groups",
    }
)
LIFECYCLE_RUNNER_IDENTIFIER = "com.mnemosyne.inference.lifecycle-runner"
LIFECYCLE_RUNNER_RELATIVE_PATH = Path(
    "Contents/MacOS/mnemosyne-lifecycle-runner"
)
LIFECYCLE_PEER_MANIFEST_RELATIVE_PATH = Path(
    "Contents/Resources/lifecycle-helper-peer-v2.json"
)
LIFECYCLE_PEER_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "helper_protocol_version",
        "app_bundle_identifier",
        "app_short_version",
        "app_build_number",
        "app_build_digest",
        "expected_team_identifier",
        "helper_relative_path",
        "helper_identifier",
        "helper_team_identifier",
        "helper_cdhash",
        "helper_code_requirement_digest",
        "helper_build_digest",
        "runner_relative_path",
        "runner_identifier",
        "runner_team_identifier",
        "runner_cdhash",
        "runner_code_requirement_digest",
        "runner_build_digest",
        "service_python_relative_path",
        "service_python_identifier",
        "service_python_team_identifier",
        "service_python_cdhash",
        "service_python_code_requirement_digest",
        "service_python_authoritative",
    }
)
MUTABLE_INFO_KEYS = {
    "CFBundleShortVersionString",
    "CFBundleVersion",
    "SUFeedURL",
    "SUPublicEDKey",
}
REQUIRED_MAC_POOL_ACCEPTANCE_GATE_IDS = frozenset(
    {
        "automated-native-suites",
        "developer-id-notarized-dmg",
        "ds4-preview-isolation",
        "durable-download-lifecycle",
        "executable-migration-rollback-uninstall",
        "github-macos-ci",
        "guided-clean-install",
        "llamacpp-real-text-and-vision",
        "lmstudio-finder-migration",
        "login-and-launchagent-lifecycle",
        "mac-pool-inventory-placement-download",
        "mac-pool-jit-batching-accounting",
        "mac-pool-pairing-and-self-revoke",
        "mac-pool-participation-limited-overflow",
        "managed-runtime-update-rollback",
        "mflux-preview-isolation",
        "model-first-runtime-preparation",
        "omlx-durable-startup-auth-recovery",
        "omlx-real-request-and-usage",
        "postgres-idempotent-delivery",
        "protected-folder-restart",
        "signed-compatibility-catalog",
        "signed-update-and-recovery",
    }
)


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

    gate_ids = [str(gate["id"]) for gate in gates]
    seen: set[str] = set()
    duplicate_ids: set[str] = set()
    for gate_id in gate_ids:
        if gate_id in seen:
            duplicate_ids.add(gate_id)
        seen.add(gate_id)
    if duplicate_ids:
        raise ValueError(
            f"{ACCEPTANCE_FILE} contains duplicate gate IDs: "
            f"{', '.join(sorted(duplicate_ids))}"
        )

    required_ids = {
        str(gate["id"])
        for gate in gates
        if gate["required"]
    }
    missing_required = sorted(
        REQUIRED_MAC_POOL_ACCEPTANCE_GATE_IDS - required_ids
    )
    unexpected_required = sorted(
        required_ids - REQUIRED_MAC_POOL_ACCEPTANCE_GATE_IDS
    )
    if missing_required or unexpected_required:
        raise ValueError(
            f"{ACCEPTANCE_FILE} required gate set mismatch; "
            f"missing={missing_required}, unexpected={unexpected_required}"
        )

    major = int(expected.split(".", 1)[0])
    pending = [
        str(gate["id"])
        for gate in gates
        if gate["required"] and gate["status"] != "passed"
    ]
    if major >= 1 and (not payload.get("release_ready") or pending):
        details = ", ".join(pending) if pending else "release_ready is false"
        raise ValueError(f"V1 acceptance is not complete: {details}")


def _mach_o_output(*arguments: str) -> str:
    result = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).strip()
        raise ValueError(
            f"{' '.join(arguments)} failed: {diagnostic or result.returncode}"
        )
    return result.stdout


def _validate_app_runtime_links(app: Path) -> None:
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
    if not executable.is_file():
        raise ValueError(f"{executable} is missing")
    if not sparkle.is_file():
        raise ValueError(f"{sparkle} is missing")
    if not trash_helper.is_file() or not trash_helper.stat().st_mode & 0o111:
        raise ValueError(f"{trash_helper} is missing or not executable")
    if (
        not lifecycle_helper.is_file()
        or not lifecycle_helper.stat().st_mode & 0o111
    ):
        raise ValueError(f"{lifecycle_helper} is missing or not executable")
    if (
        not lifecycle_runner.is_file()
        or not lifecycle_runner.stat().st_mode & 0o111
    ):
        raise ValueError(f"{lifecycle_runner} is missing or not executable")

    dependencies = _mach_o_output("/usr/bin/otool", "-L", str(executable))
    load_commands = _mach_o_output("/usr/bin/otool", "-l", str(executable))
    if SPARKLE_DEPENDENCY not in dependencies:
        raise ValueError(
            f"{executable} does not link the packaged Sparkle framework"
        )
    if f"path {APP_FRAMEWORK_RPATH} " not in load_commands:
        raise ValueError(
            f"{executable} cannot resolve Sparkle from Contents/Frameworks"
        )


def _codesign_details(path: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/codesign", "-d", "--verbose=4", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    details = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        raise ValueError(f"could not inspect code-signing identity for {path}: {details.strip()}")
    return details


def _codesign_identifier(path: Path) -> str:
    match = re.search(r"^Identifier=(.+)$", _codesign_details(path), re.MULTILINE)
    if match is None:
        raise ValueError(f"code signature has no identifier: {path}")
    return match.group(1).strip()


def _codesign_is_adhoc(path: Path) -> bool:
    return bool(re.search(r"^Signature=adhoc$", _codesign_details(path), re.MULTILINE))


def _codesign_field(path: Path, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}=(.+)$",
        _codesign_details(path),
        re.MULTILINE,
    )
    if match is None:
        raise ValueError(f"code signature has no {name}: {path}")
    return match.group(1).strip()


def _codesign_team_identifier(path: Path) -> str:
    return _codesign_field(path, "TeamIdentifier")


def _codesign_cdhash(path: Path) -> str:
    value = _codesign_field(path, "CDHash").lower()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
        raise ValueError(f"code signature has an invalid CDHash: {path}")
    return value


def _codesign_requirement(path: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/codesign", "-d", "-r-", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    details = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        raise ValueError(
            f"could not inspect designated requirement for {path}: {details.strip()}"
        )
    match = re.search(
        r"^(?:#\s*)?designated => (.+)$",
        details,
        re.MULTILINE,
    )
    if match is None:
        raise ValueError(f"code signature has no designated requirement: {path}")
    return match.group(1).strip()


def _plist_from_command_output(
    result: subprocess.CompletedProcess[bytes],
    *,
    operation: str,
) -> dict:
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).decode(
            "utf-8", errors="replace"
        ).strip()
        raise ValueError(
            f"{operation} failed: {diagnostic or result.returncode}"
        )
    for raw in (result.stdout, result.stderr):
        starts = [
            offset
            for marker in (b"bplist00", b"<?xml")
            if (offset := raw.find(marker)) >= 0
        ]
        if not starts:
            continue
        try:
            value = plistlib.loads(raw[min(starts) :])
        except (plistlib.InvalidFileException, ValueError):
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"{operation} returned no property list")


def _codesign_entitlements(path: Path) -> dict:
    result = subprocess.run(
        [
            "/usr/bin/codesign",
            "-d",
            "--entitlements",
            ":-",
            str(path),
        ],
        check=False,
        capture_output=True,
    )
    combined = result.stdout + result.stderr
    if result.returncode == 0 and b"<?xml" not in combined and b"bplist00" not in combined:
        return {}
    return _plist_from_command_output(
        result,
        operation=f"could not inspect code-signing entitlements for {path}",
    )


def _decode_provisioning_profile(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"lifecycle helper provisioning profile is missing: {path}")
    result = subprocess.run(
        ["/usr/bin/security", "cms", "-D", "-i", str(path)],
        check=False,
        capture_output=True,
    )
    return _plist_from_command_output(
        result,
        operation=f"could not decode lifecycle helper profile {path}",
    )


def _lifecycle_helper_profile_entitlements(
    path: Path,
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, object]]:
    profile = _decode_provisioning_profile(path)
    teams = profile.get("TeamIdentifier")
    prefixes = profile.get("ApplicationIdentifierPrefix")
    if (
        not isinstance(teams, list)
        or len(teams) != 1
        or not isinstance(teams[0], str)
        or re.fullmatch(r"[A-Z0-9]{10}", teams[0]) is None
    ):
        raise ValueError("lifecycle helper profile has no exact Team Identifier")
    team = teams[0]
    if (
        not isinstance(prefixes, list)
        or len(prefixes) != 1
        or not isinstance(prefixes[0], str)
        or prefixes[0].rstrip(".") != team
    ):
        raise ValueError(
            "lifecycle helper profile App ID prefix does not match its team"
        )
    if profile.get("ProvisionsAllDevices") is not True:
        raise ValueError(
            "lifecycle helper profile is not a Developer ID distribution profile"
        )
    platforms = profile.get("Platform")
    if (
        not isinstance(platforms, list)
        or not platforms
        or set(platforms) != {"OSX"}
    ):
        raise ValueError("lifecycle helper profile is not restricted to macOS")
    expiration = profile.get("ExpirationDate")
    if not isinstance(expiration, datetime):
        raise ValueError("lifecycle helper profile has no expiration")
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if expiration <= current:
        raise ValueError("lifecycle helper provisioning profile has expired")

    profile_entitlements = profile.get("Entitlements")
    if not isinstance(profile_entitlements, dict):
        raise ValueError("lifecycle helper profile has no entitlements")
    app_identifier = f"{team}.{LIFECYCLE_HELPER_IDENTIFIER}"
    expected: dict[str, object] = {
        "com.apple.application-identifier": app_identifier,
        "com.apple.developer.team-identifier": team,
        "keychain-access-groups": [app_identifier],
    }
    for key, value in expected.items():
        if profile_entitlements.get(key) != value:
            raise ValueError(
                f"lifecycle helper profile does not authorize exact {key}"
            )
    if profile_entitlements.get("get-task-allow") not in (None, False):
        raise ValueError("lifecycle helper distribution profile enables debugging")
    return team, expected


def _write_lifecycle_helper_entitlements(
    profile: Path,
    destination: Path,
) -> Path:
    _team, entitlements = _lifecycle_helper_profile_entitlements(profile)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("lifecycle helper entitlements output must not be a symlink")
    encoded = plistlib.dumps(entitlements, fmt=plistlib.FMT_XML, sort_keys=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    return destination


def _codesign_is_developer_id_application(path: Path) -> bool:
    return bool(
        re.search(
            r"^Authority=Developer ID Application:",
            _codesign_details(path),
            re.MULTILINE,
        )
    )


def _validate_distribution_signature(path: Path, *, team: str) -> None:
    details = _codesign_details(path)
    if not re.search(
        r"^Authority=Developer ID Application:", details, re.MULTILINE
    ):
        raise ValueError(f"lifecycle authority lacks Developer ID signing: {path}")
    if _codesign_team_identifier(path) != team:
        raise ValueError(f"lifecycle authority Team Identifier differs: {path}")
    if "(runtime)" not in details:
        raise ValueError(f"lifecycle authority lacks hardened runtime: {path}")
    timestamp = re.search(r"^Timestamp=(.+)$", details, re.MULTILINE)
    if timestamp is None or timestamp.group(1).strip().lower() in {
        "none",
        "not set",
    }:
        raise ValueError(f"lifecycle authority lacks secure timestamp: {path}")


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_digest(value: dict[str, str]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _bundle_info(app: Path) -> dict:
    with (app / "Contents" / "Info.plist").open("rb") as stream:
        value = plistlib.load(stream)
    if not isinstance(value, dict):
        raise ValueError("staged Info.plist is malformed")
    return value


def _bundled_service_python(app: Path) -> Path:
    python_root = app / "Contents" / "Resources" / "Python"
    candidates = [python_root / "bin" / "python3"]
    if python_root.is_dir():
        candidates.extend(
            path / "bin" / "python3"
            for path in sorted(python_root.iterdir(), key=lambda item: item.name)
            if path.is_dir() and path.name.startswith("cpython-")
        )
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            try:
                candidate.resolve(strict=True).relative_to(
                    python_root.resolve(strict=True)
                )
            except (OSError, RuntimeError, ValueError):
                raise ValueError(
                    "bundled service Python escapes its signed runtime"
                ) from None
            return candidate
    raise ValueError("bundled service Python executable is missing")


def _lifecycle_peer_manifest_payload(app: Path) -> dict[str, object]:
    helper = app / LIFECYCLE_HELPER_RELATIVE_PATH
    runner = app / LIFECYCLE_RUNNER_RELATIVE_PATH
    service_python = _bundled_service_python(app)
    if (
        helper.is_symlink()
        or not helper.is_file()
        or not helper.stat().st_mode & 0o111
    ):
        raise ValueError(f"{helper} is missing or not executable")
    if (
        runner.is_symlink()
        or not runner.is_file()
        or not runner.stat().st_mode & 0o111
    ):
        raise ValueError(f"{runner} is missing or not executable")
    info = _bundle_info(app)
    bundle_identifier = str(info.get("CFBundleIdentifier", ""))
    short_version = str(info.get("CFBundleShortVersionString", ""))
    build_number = str(info.get("CFBundleVersion", ""))
    if (
        bundle_identifier != "com.mnemosyne.inference.menu"
        or VERSION_PATTERN.fullmatch(short_version) is None
        or re.fullmatch(r"[1-9][0-9]*", build_number) is None
    ):
        raise ValueError("staged app build identity is invalid")

    helper_identifier = _codesign_identifier(helper)
    helper_team = _codesign_team_identifier(helper)
    runner_identifier = _codesign_identifier(runner)
    runner_team = _codesign_team_identifier(runner)
    service_python_team = _codesign_team_identifier(service_python)
    if helper_identifier != LIFECYCLE_HELPER_IDENTIFIER:
        raise ValueError(
            "staged lifecycle helper has the wrong code-signing identifier"
        )
    if runner_identifier != LIFECYCLE_RUNNER_IDENTIFIER:
        raise ValueError(
            "staged lifecycle runner has the wrong code-signing identifier"
        )
    if len({helper_team, runner_team, service_python_team}) != 1:
        raise ValueError(
            "lifecycle helper, runner, and bundled service Python teams differ"
        )

    app_build_digest = _canonical_digest(
        {
            "app_build_number": build_number,
            "app_bundle_identifier": bundle_identifier,
            "app_short_version": short_version,
            "team_identifier": helper_team,
        }
    )
    helper_cdhash = _codesign_cdhash(helper)
    helper_build_digest = _canonical_digest(
        {
            "app_build_digest": app_build_digest,
            "cdhash": helper_cdhash,
            "identifier": helper_identifier,
            "team_identifier": helper_team,
        }
    )
    runner_cdhash = _codesign_cdhash(runner)
    runner_build_digest = _canonical_digest(
        {
            "app_build_digest": app_build_digest,
            "cdhash": runner_cdhash,
            "identifier": runner_identifier,
            "team_identifier": runner_team,
        }
    )
    return {
        "schema_version": 2,
        "helper_protocol_version": 2,
        "app_bundle_identifier": bundle_identifier,
        "app_short_version": short_version,
        "app_build_number": build_number,
        "app_build_digest": app_build_digest,
        "expected_team_identifier": helper_team,
        "helper_relative_path": LIFECYCLE_HELPER_RELATIVE_PATH.as_posix(),
        "helper_identifier": helper_identifier,
        "helper_team_identifier": helper_team,
        "helper_cdhash": helper_cdhash,
        "helper_code_requirement_digest": _sha256_text(
            _codesign_requirement(helper)
        ),
        "helper_build_digest": helper_build_digest,
        "runner_relative_path": LIFECYCLE_RUNNER_RELATIVE_PATH.as_posix(),
        "runner_identifier": runner_identifier,
        "runner_team_identifier": runner_team,
        "runner_cdhash": runner_cdhash,
        "runner_code_requirement_digest": _sha256_text(
            _codesign_requirement(runner)
        ),
        "runner_build_digest": runner_build_digest,
        "service_python_relative_path": service_python.relative_to(app).as_posix(),
        "service_python_identifier": _codesign_identifier(service_python),
        "service_python_team_identifier": service_python_team,
        "service_python_cdhash": _codesign_cdhash(service_python),
        "service_python_code_requirement_digest": _sha256_text(
            _codesign_requirement(service_python)
        ),
        "service_python_authoritative": False,
    }


def _write_lifecycle_peer_manifest(app: Path) -> Path:
    destination = app / LIFECYCLE_PEER_MANIFEST_RELATIVE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("lifecycle peer manifest must not be a symlink")
    payload = _lifecycle_peer_manifest_payload(app)
    encoded = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o644,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
        os.chmod(destination, 0o644)
        directory_descriptor = os.open(
            destination.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    return destination


def _validate_lifecycle_peer_manifest(
    app: Path,
    *,
    allow_bare: bool,
) -> None:
    helper = app / LIFECYCLE_HELPER_RELATIVE_PATH
    helper_wrapper = app / LIFECYCLE_HELPER_WRAPPER_RELATIVE_PATH
    runner = app / LIFECYCLE_RUNNER_RELATIVE_PATH
    bootstrap = app / "Contents" / "MacOS" / "mnemosyne-service-bootstrap"
    trash_helper = app / "Contents" / "MacOS" / "mnemosyne-file-trash"
    if (
        helper.is_symlink()
        or not helper.is_file()
        or not helper.stat().st_mode & 0o111
    ):
        raise ValueError(f"{helper} is missing or not executable")
    if (
        runner.is_symlink()
        or not runner.is_file()
        or not runner.stat().st_mode & 0o111
    ):
        raise ValueError(f"{runner} is missing or not executable")
    if _codesign_identifier(helper) != LIFECYCLE_HELPER_IDENTIFIER:
        raise ValueError(
            "staged lifecycle helper has the wrong code-signing identifier"
        )
    if _codesign_identifier(runner) != LIFECYCLE_RUNNER_IDENTIFIER:
        raise ValueError(
            "staged lifecycle runner has the wrong code-signing identifier"
        )
    manifest_path = app / LIFECYCLE_PEER_MANIFEST_RELATIVE_PATH
    if allow_bare and not manifest_path.exists():
        return
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("signed lifecycle peer manifest is missing")
    try:
        payload = json.loads(manifest_path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("signed lifecycle peer manifest is malformed") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != LIFECYCLE_PEER_MANIFEST_KEYS
    ):
        raise ValueError("signed lifecycle peer manifest has an open schema")
    expected = _lifecycle_peer_manifest_payload(app)
    if payload != expected:
        raise ValueError(
            "signed lifecycle peer manifest does not match staged code"
        )

    app_team = _codesign_team_identifier(app)
    app_identifier = _codesign_identifier(app)
    nested_teams = {
        _codesign_team_identifier(helper_wrapper),
        _codesign_team_identifier(helper),
        _codesign_team_identifier(runner),
        _codesign_team_identifier(_bundled_service_python(app)),
        _codesign_team_identifier(bootstrap),
        _codesign_team_identifier(trash_helper),
    }
    if (
        app_identifier != expected["app_bundle_identifier"]
        or app_team != expected["expected_team_identifier"]
        or nested_teams != {app_team}
    ):
        raise ValueError(
            "app, lifecycle helper, lifecycle runner, service Python, and "
            "first-party helper teams differ"
        )

    app_details = _codesign_details(app)
    developer_id = bool(
        re.search(
            r"^Authority=Developer ID Application:",
            app_details,
            re.MULTILINE,
        )
    )
    if developer_id:
        for path in (
            app,
            helper_wrapper,
            helper,
            runner,
            _bundled_service_python(app),
            bootstrap,
            trash_helper,
        ):
            if "(runtime)" not in _codesign_details(path):
                raise ValueError(
                    f"Developer ID code lacks the hardened runtime: {path}"
                )


def _validate_complete_app_seal(app: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).strip()
        raise ValueError(
            "staged application code seal is invalid: "
            f"{diagnostic or result.returncode}"
        )


def _validate_bundled_bootstrap_isolation(app: Path) -> None:
    """Prove the full app starts its sealed service under a hostile Python env."""

    # The probe deliberately runs from a private temporary working directory;
    # preserve a caller-supplied relative --app by anchoring it first.
    app = app.absolute()
    bootstrap = app / "Contents" / "MacOS" / "mnemosyne-service-bootstrap"
    resources = app / "Contents" / "Resources"
    config = resources / "config.yaml.example"
    environment_file = resources / ".env.example"
    if (
        bootstrap.is_symlink()
        or not bootstrap.is_file()
        or not bootstrap.stat().st_mode & 0o111
        or not config.is_file()
        or not environment_file.is_file()
    ):
        raise ValueError("staged bootstrap isolation inputs are incomplete")

    with tempfile.TemporaryDirectory(
        prefix="mnemosyne-bootstrap-verification-",
        dir="/private/tmp",
    ) as temporary:
        environment = {
            "CFFIXED_USER_HOME": temporary,
            "HOME": temporary,
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": temporary,
            "MNEMOSYNE_MACOS_CONFIG_PATH": str(config),
            "MNEMOSYNE_MACOS_ENV_PATH": str(environment_file),
            # Every value below must be ignored by a complete bundle.
            "MNEMOSYNE_PYTHON_OVERRIDE": "/usr/bin/false",
            "PYTHONDONTWRITEBYTECODE": "0",
            "PYTHONHOME": "/hostile/python-home",
            "PYTHONINSPECT": "1",
            "PYTHONPATH": "/hostile/python-path",
            "PYTHONSTARTUP": "/hostile/python-startup.py",
            "PYTHONUSERBASE": "/hostile/python-user-base",
            "PYTHONWARNINGS": "error",
        }
        try:
            result = subprocess.run(
                [
                    str(bootstrap),
                    "--check-config",
                    "--config",
                    str(config),
                    "--env",
                    str(environment_file),
                ],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(
                "staged bootstrap isolation probe could not complete: "
                f"{type(exc).__name__}"
            ) from None
    output = result.stdout.strip()
    if result.returncode != 0 or not output.startswith("valid: inference="):
        diagnostic = (result.stderr or result.stdout).strip()
        raise ValueError(
            "staged bootstrap did not use its isolated bundled runtime: "
            f"{diagnostic[:1000] or result.returncode}"
        )
    if result.stderr.strip() or "/hostile/" in output:
        raise ValueError("staged bootstrap isolation probe returned unexpected output")


def _validate_internal_symlinks(root: Path, *, label: str) -> None:
    if root.is_symlink():
        raise ValueError(f"{label} root must not be a symlink: {root}")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        raise ValueError(f"{label} root is missing: {root}") from None
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_symlink():
            continue
        target = path.readlink()
        if target.is_absolute():
            raise ValueError(f"{label} contains an absolute symlink: {path}")
        try:
            path.resolve(strict=True).relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            raise ValueError(f"{label} contains an escaping or dangling symlink: {path}") from None


def _regular_payload_files(root: Path, *, label: str) -> dict[Path, Path]:
    files: dict[Path, Path] = {}
    for path in root.rglob("*"):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise ValueError(f"{label} must not contain symlinks: {path}")
        if stat.S_ISDIR(details.st_mode):
            continue
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(f"{label} contains an unsupported entry: {path}")
        files[path.relative_to(root)] = path
    return files


def _validate_source_copy(
    source: Path,
    destination: Path,
    *,
    extra_files: dict[Path, Path] | None = None,
) -> None:
    source_files = _regular_payload_files(source, label=f"{source} source payload")
    actual = _regular_payload_files(destination, label=f"{destination} staged payload")
    expected = {
        relative: path for relative, path in source_files.items() if path.suffix == ".py"
    }
    expected.update(extra_files or {})
    if set(actual) != set(expected):
        missing = sorted(str(path) for path in set(expected) - set(actual))
        extra = sorted(str(path) for path in set(actual) - set(expected))
        raise ValueError(
            f"{destination} source inventory mismatch; "
            f"missing={missing}, extra={extra}"
        )
    changed = [
        str(relative)
        for relative, source_path in expected.items()
        if source_path.read_bytes() != actual[relative].read_bytes()
    ]
    if changed:
        raise ValueError(
            f"{destination} sources differ from release input: {changed}"
        )


def _validate_embedded_python(resources: Path, *, allow_bare: bool) -> None:
    python_root = resources / "Python"
    if not python_root.exists():
        if allow_bare:
            return
        raise ValueError(f"{python_root} is missing; full release verification forbids bare apps")
    try:
        runtime_packaging.validate_export(
            python_root,
            allow_codesign_mutation=True,
        )
    except RuntimeError as exc:
        raise ValueError(f"embedded Python runtime is invalid: {exc}") from exc


def _validate_fixed_info_plist(staged_path: Path) -> None:
    with INFO_PLIST.open("rb") as stream:
        source_info = plistlib.load(stream)
    with staged_path.open("rb") as stream:
        staged_info = plistlib.load(stream)
    fixed_source = {
        key: value for key, value in source_info.items() if key not in MUTABLE_INFO_KEYS
    }
    fixed_staged = {
        key: value for key, value in staged_info.items() if key not in MUTABLE_INFO_KEYS
    }
    if fixed_staged != fixed_source:
        raise ValueError("staged Info.plist fixed product identity differs from release input")
    if not str(staged_info.get("CFBundleVersion", "")).isdigit() or int(
        staged_info.get("CFBundleVersion", 0)
    ) < 1:
        raise ValueError("staged Info.plist build number must be a positive integer")
    if not str(staged_info.get("SUFeedURL", "")).startswith("https://"):
        raise ValueError("staged Info.plist Sparkle feed must use HTTPS")


def _validate_fixed_lifecycle_helper_info(app: Path) -> None:
    wrapper_info_path = (
        app / LIFECYCLE_HELPER_WRAPPER_RELATIVE_PATH / "Contents" / "Info.plist"
    )
    if wrapper_info_path.is_symlink() or not wrapper_info_path.is_file():
        raise ValueError("lifecycle helper wrapper Info.plist is missing")
    with LIFECYCLE_HELPER_INFO_PLIST.open("rb") as stream:
        source = plistlib.load(stream)
    with wrapper_info_path.open("rb") as stream:
        staged = plistlib.load(stream)
    mutable = {"CFBundleShortVersionString", "CFBundleVersion"}
    if (
        {key: value for key, value in staged.items() if key not in mutable}
        != {key: value for key, value in source.items() if key not in mutable}
    ):
        raise ValueError(
            "lifecycle helper wrapper fixed identity differs from release input"
        )
    app_info = _bundle_info(app)
    for key in mutable:
        if staged.get(key) != app_info.get(key):
            raise ValueError(
                "lifecycle helper wrapper version differs from the outer app"
            )


def _validate_lifecycle_helper_wrapper(
    app: Path,
    *,
    require_profiled_authority: bool,
) -> bool:
    wrapper = app / LIFECYCLE_HELPER_WRAPPER_RELATIVE_PATH
    helper = app / LIFECYCLE_HELPER_RELATIVE_PATH
    profile = app / LIFECYCLE_HELPER_PROFILE_RELATIVE_PATH
    if wrapper.is_symlink() or not wrapper.is_dir():
        raise ValueError("lifecycle helper wrapper is missing")
    if (
        helper.is_symlink()
        or not helper.is_file()
        or not helper.stat().st_mode & 0o111
    ):
        raise ValueError(f"{helper} is missing or not executable")
    _validate_fixed_lifecycle_helper_info(app)
    if _codesign_identifier(wrapper) != LIFECYCLE_HELPER_IDENTIFIER:
        raise ValueError(
            "lifecycle helper wrapper has the wrong code-signing identifier"
        )
    if _codesign_identifier(helper) != LIFECYCLE_HELPER_IDENTIFIER:
        raise ValueError(
            "staged lifecycle helper has the wrong code-signing identifier"
        )

    entitlements = _codesign_entitlements(helper)
    if profile.is_symlink():
        raise ValueError("lifecycle helper provisioning profile is invalid")
    if not profile.exists():
        if entitlements:
            raise ValueError(
                "unprofiled lifecycle helper must not claim entitlements"
            )
        if require_profiled_authority:
            raise ValueError(
                "distribution requires the lifecycle helper provisioning profile"
            )
        return False
    if not profile.is_file():
        raise ValueError("lifecycle helper provisioning profile is invalid")
    team, expected_entitlements = _lifecycle_helper_profile_entitlements(profile)
    if set(entitlements) != LIFECYCLE_HELPER_ENTITLEMENT_KEYS:
        raise ValueError(
            "lifecycle helper signature has an open entitlement allowlist"
        )
    if entitlements != expected_entitlements:
        raise ValueError(
            "lifecycle helper signature entitlements differ from its profile"
        )
    if _codesign_team_identifier(helper) != team:
        raise ValueError(
            "lifecycle helper signature team differs from its profile"
        )
    if _codesign_team_identifier(wrapper) != team:
        raise ValueError(
            "lifecycle helper wrapper team differs from its profile"
        )
    if _codesign_team_identifier(app) != team:
        raise ValueError("outer app team differs from lifecycle helper profile")
    for path in (helper, wrapper, app):
        _validate_distribution_signature(path, team=team)
    return True


def _validate_distribution_assessment(app: Path) -> None:
    commands = (
        ("/usr/bin/xcrun", "stapler", "validate", str(app)),
        (
            "/usr/sbin/spctl",
            "--assess",
            "--type",
            "execute",
            "--verbose=2",
            str(app),
        ),
    )
    for command in commands:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            diagnostic = (result.stderr or result.stdout).strip()
            raise ValueError(
                "lifecycle authority requires notarization and Gatekeeper "
                f"acceptance: {diagnostic or result.returncode}"
            )


def _validate_app_payload(
    app: Path,
    *,
    allow_bare: bool = False,
    require_distribution: bool = False,
) -> None:
    _validate_internal_symlinks(app, label="application bundle")
    contents = app / "Contents"
    resources = contents / "Resources"
    service = resources / "Service" / "mnemosyne_macos"
    image_worker = resources / "ImageWorker" / "mnemosyne_mflux_worker"
    bootstrap = contents / "MacOS" / "mnemosyne-service-bootstrap"
    trash_helper = contents / "MacOS" / "mnemosyne-file-trash"
    lifecycle_helper = app / LIFECYCLE_HELPER_RELATIVE_PATH
    lifecycle_runner = app / LIFECYCLE_RUNNER_RELATIVE_PATH
    if bootstrap.is_symlink() or not bootstrap.is_file() or not bootstrap.stat().st_mode & 0o111:
        raise ValueError(f"{bootstrap} is missing or not executable")
    if trash_helper.is_symlink() or not trash_helper.is_file() or not trash_helper.stat().st_mode & 0o111:
        raise ValueError(f"{trash_helper} is missing or not executable")
    if (
        lifecycle_helper.is_symlink()
        or not lifecycle_helper.is_file()
        or not lifecycle_helper.stat().st_mode & 0o111
    ):
        raise ValueError(f"{lifecycle_helper} is missing or not executable")
    if (
        lifecycle_runner.is_symlink()
        or not lifecycle_runner.is_file()
        or not lifecycle_runner.stat().st_mode & 0o111
    ):
        raise ValueError(f"{lifecycle_runner} is missing or not executable")
    if _codesign_identifier(bootstrap) != SERVICE_HELPER_IDENTIFIER:
        raise ValueError("staged service bootstrap has the wrong code-signing identifier")
    if _codesign_identifier(trash_helper) != TRASH_HELPER_IDENTIFIER:
        raise ValueError("staged Trash helper has the wrong code-signing identifier")
    if _codesign_identifier(lifecycle_helper) != LIFECYCLE_HELPER_IDENTIFIER:
        raise ValueError(
            "staged lifecycle helper has the wrong code-signing identifier"
        )
    if _codesign_identifier(lifecycle_runner) != LIFECYCLE_RUNNER_IDENTIFIER:
        raise ValueError(
            "staged lifecycle runner has the wrong code-signing identifier"
        )

    service_extras = {
        Path("mnemosyne_macos/schemas/compatibility_catalog.schema.json"): CATALOG_SCHEMA,
        Path("mnemosyne_macos/schemas/desired_install.schema.json"): DESIRED_INSTALL_SCHEMA,
    }
    _validate_source_copy(
        SERVICE_SOURCE_ROOT,
        resources / "Service",
        extra_files=service_extras,
    )
    _validate_source_copy(
        IMAGE_WORKER_SOURCE_ROOT,
        resources / "ImageWorker",
        extra_files={Path("capabilities.json"): IMAGE_CAPABILITIES},
    )
    exact_resources = {
        resources / "config.yaml.example": MACOS_ROOT / "config.yaml.example",
        resources / ".env.example": MACOS_ROOT / ".env.example",
        resources / "ImageWorker" / "capabilities.json": IMAGE_CAPABILITIES,
        service / "schemas" / "compatibility_catalog.schema.json": CATALOG_SCHEMA,
        service / "schemas" / "desired_install.schema.json": DESIRED_INSTALL_SCHEMA,
        contents
        / "Library"
        / "LaunchAgents"
        / "com.mnemosyne.inference.agent.plist": AGENT_PLIST,
    }
    for staged, source in exact_resources.items():
        if staged.is_symlink() or not staged.is_file() or staged.read_bytes() != source.read_bytes():
            raise ValueError(f"{staged} is missing or differs from release input")

    _validate_fixed_info_plist(contents / "Info.plist")
    _validate_lifecycle_helper_wrapper(
        app,
        require_profiled_authority=require_distribution,
    )

    with (
        contents
        / "Library"
        / "LaunchAgents"
        / "com.mnemosyne.inference.agent.plist"
    ).open("rb") as stream:
        agent = plistlib.load(stream)
    if agent.get("BundleProgram") != (
        "Contents/MacOS/mnemosyne-service-bootstrap"
    ):
        raise ValueError("staged LaunchAgent does not target the direct bootstrap")
    _validate_embedded_python(resources, allow_bare=allow_bare)
    _validate_lifecycle_peer_manifest(app, allow_bare=allow_bare)


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
    parser.add_argument(
        "--allow-bare",
        action="store_true",
        help="allow a UI-only staged app without the relocatable Python runtime",
    )
    parser.add_argument(
        "--require-distribution",
        action="store_true",
        help=(
            "require the credentialed lifecycle helper profile/signature and "
            "notarized Gatekeeper-accepted app"
        ),
    )
    parser.add_argument(
        "--provisioning-profile",
        type=Path,
        help="externally supplied lifecycle helper provisioning profile",
    )
    parser.add_argument(
        "--write-lifecycle-helper-entitlements",
        type=Path,
        help="write the exact entitlements authorized by --provisioning-profile",
    )
    parser.add_argument(
        "--write-lifecycle-peer-manifest",
        action="store_true",
        help=(
            "write the sealed role manifest after nested "
            "helper/runner/Python signing"
        ),
    )
    args = parser.parse_args()
    if args.write_lifecycle_helper_entitlements is not None:
        if (
            args.provisioning_profile is None
            or args.app is not None
            or args.tag is not None
            or args.allow_bare
            or args.require_distribution
            or args.write_lifecycle_peer_manifest
        ):
            raise ValueError(
                "--write-lifecycle-helper-entitlements requires only "
                "--provisioning-profile"
            )
        destination = _write_lifecycle_helper_entitlements(
            args.provisioning_profile,
            args.write_lifecycle_helper_entitlements,
        )
        print(f"Wrote lifecycle helper entitlements at {destination}")
        return 0
    if args.provisioning_profile is not None:
        raise ValueError(
            "--provisioning-profile requires "
            "--write-lifecycle-helper-entitlements"
        )
    if args.write_lifecycle_peer_manifest:
        if (
            args.app is None
            or args.allow_bare
            or args.require_distribution
            or args.tag is not None
        ):
            raise ValueError(
                "--write-lifecycle-peer-manifest requires only one full --app"
            )
        destination = _write_lifecycle_peer_manifest(args.app)
        print(f"Wrote lifecycle peer manifest at {destination}")
        return 0
    if args.allow_bare and args.app is None:
        raise ValueError("--allow-bare requires --app")
    if args.allow_bare and args.tag is not None:
        raise ValueError("--allow-bare cannot be used with release-tag verification")
    if args.require_distribution and args.app is None:
        raise ValueError("--require-distribution requires --app")
    if args.require_distribution and args.allow_bare:
        raise ValueError("--require-distribution forbids --allow-bare")

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
        if args.allow_bare and not _codesign_is_adhoc(args.app):
            raise ValueError("--allow-bare is restricted to ad-hoc-signed development apps")
        versions["staged app"] = _plist_version(
            args.app / "Contents" / "Info.plist"
        )
        _validate_complete_app_seal(args.app)
        _validate_app_runtime_links(args.app)
        _validate_app_payload(
            args.app,
            allow_bare=args.allow_bare,
            require_distribution=args.require_distribution,
        )
        if not args.allow_bare:
            _validate_bundled_bootstrap_isolation(args.app)
        # Recheck after every runtime/source inspection so release verification
        # proves it did not mutate the signed bundle it just accepted.
        _validate_complete_app_seal(args.app)
        if args.require_distribution:
            _validate_distribution_assessment(args.app)
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
