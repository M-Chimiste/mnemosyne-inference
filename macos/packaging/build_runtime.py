#!/usr/bin/env python3
"""Build relocatable Python layers for the native Mnemosyne app."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlparse


PACKAGING_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGING_DIR.parent.parent
TEMPLATE = PACKAGING_DIR / "venvstacks.toml"
SERVICE_DIR = REPO_ROOT / "macos" / "service"
SERVICE_LOCK = SERVICE_DIR / "uv.lock"
IMAGE_WORKER_DIR = REPO_ROOT / "macos" / "image-worker"
IMAGE_WORKER_LOCK = IMAGE_WORKER_DIR / "uv.lock"
DEFAULT_EXPORT = PACKAGING_DIR / "_export"
VCS_WHEEL_DIR = PACKAGING_DIR / "_wheels"
EXPORT_PROVENANCE_NAME = "mnemosyne-lock-provenance-v1.json"
EXPORT_MANIFEST_NAME = "mnemosyne-export-manifest-v1.json"
VCS_WHEEL_PROVENANCE_NAME = "mnemosyne-vcs-wheel-v1.json"
VENVSTACKS_VERSION = "0.7.0"
MARKER = 'requirements = [] # __MNEMOSYNE_REQUIREMENTS__'
IMAGE_MARKER = 'requirements = [] # __MNEMOSYNE_IMAGE_REQUIREMENTS__'
PROVENANCE_MARKER = "# __MNEMOSYNE_LOCK_PROVENANCE__"
EXACT_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?"
    r"==[^\s;*]+(?:\s*;\s*.+)?$"
)
EXACT_PINNED_REQUIREMENT = re.compile(
    r"^(?P<project>[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9,._-]+\])?)==(?P<version>[^\s;*]+)"
    r"(?P<marker>\s*;\s*.+)?$"
)
EXACT_GITHUB_REQUIREMENT = re.compile(
    r"^(?P<project>[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9,._-]+\])?)\s+@\s+git\+"
    r"(?P<url>https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"@(?P<commit>[0-9a-fA-F]{40})(?P<marker>\s*;\s*.+)?$"
)
MACH_O_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}
EXPECTED_PYTHON_IDENTITY = {
    "implementation": "cpython",
    "machine": "arm64",
    "platform": "darwin",
    "version": [3, 12, 10],
}
TARGET_MACOS_VERSION = (15, 0, 0)
TARGET_MACOS_VERSION_TEXT = "15.0"
TARGET_PYTHON_VERSION = (3, 12)
TARGET_ARCHITECTURE = "arm64"
_LOCK_PACKAGE_HEADER = re.compile(r"(?m)^\[\[package\]\]\s*$")
_LOCK_SCALAR = {
    "name": re.compile(r'(?m)^name = ("(?:\\.|[^"\\])*")\s*$'),
    "version": re.compile(r'(?m)^version = ("(?:\\.|[^"\\])*")\s*$'),
}
_LOCK_WHEELS = re.compile(r"(?ms)^wheels = \[\s*(.*?)^\]\s*$")
_LOCK_WHEEL = re.compile(
    r'\{\s*url = (?P<url>"(?:\\.|[^"\\])*")\s*,\s*'
    r'hash = "sha256:(?P<sha256>[0-9a-f]{64})"[^}]*\}'
)
_LC_VERSION_MIN_MACOSX = 0x24
_LC_BUILD_VERSION = 0x32
_PLATFORM_MACOS = 1
_MAX_MACH_O_LOAD_COMMAND_BYTES = 16 * 1024 * 1024
_MAX_MACH_O_SLICES = 32
RUNTIME_LAYERS = (
    ("framework-mnemosyne-base", SERVICE_DIR, SERVICE_LOCK),
    ("framework-mnemosyne-image", IMAGE_WORKER_DIR, IMAGE_WORKER_LOCK),
)
_FRAMEWORK_LAYER_METADATA = {
    "base_python": "../cpython-3.12/bin/python",
    "dynlib_dirs": ["../cpython-3.12/share/venv/dynlib"],
    "py_version": "3.12.10",
    "pylib_dirs": ["../cpython-3.12/lib/python3.12/site-packages"],
    "python": "bin/python",
    "site_dir": "lib/python3.12/site-packages",
}
_RELOCATABLE_FRAMEWORK_WRAPPER = """#!/bin/zsh
# Relocatable venvstacks framework launcher for Unified Inference.
set -eu
script_dir="$(cd "$(dirname "$(readlink -f "$0")")" 1> /dev/null 2>&1 && pwd)"
export PYTHONHOME="$script_dir/../../cpython-3.12"
add_dynlib_dir() { case ":${DYLD_LIBRARY_PATH:=$1}:" in *:"$1":*) ;; *) DYLD_LIBRARY_PATH="$DYLD_LIBRARY_PATH:$1" ;; esac; }
add_dynlib_dir "$script_dir/../../cpython-3.12/share/venv/dynlib"
export DYLD_LIBRARY_PATH
script_path="$script_dir/python"
symlink_path="$script_dir/python_"
test -f "$script_path" || { echo 1>&2 "Invalid wrapper script path: $script_path"; exit 1; }
test -L "$symlink_path" || { echo 1>&2 "Invalid base Python symlink: $symlink_path"; exit 2; }
test "$symlink_path" -ef "$script_path" && { echo 1>&2 "Symlink loop detected: $symlink_path -> $script_path"; exit 3; }
exec -a "$script_path" "$symlink_path" "$@"
"""
_RELOCATABLE_PYVENV_CONFIG = """home = ../../cpython-3.12/bin
include-system-site-packages = false
version = 3.12.10
executable = ../../cpython-3.12/bin/python
"""
_RELOCATABLE_SITE_CUSTOMIZE = '''"""Relocatable venvstacks peer-runtime path for Unified Inference."""

from pathlib import Path
from site import addsitedir

_runtime_root = Path(__file__).resolve().parents[4]
addsitedir(str(_runtime_root / "cpython-3.12/lib/python3.12/site-packages"))
'''


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
        if not (
            EXACT_REQUIREMENT.fullmatch(requirement)
            or EXACT_GITHUB_REQUIREMENT.fullmatch(requirement)
        ):
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


def locked_requirements(
    project_dir: Path = SERVICE_DIR,
    lock_path: Path = SERVICE_LOCK,
) -> tuple[str, ...]:
    """Export exact production pins without resolving beyond the committed lock."""

    if not lock_path.is_file():
        raise RuntimeError(f"committed lock is missing: {lock_path}")
    command = [
        uv_driver(),
        "export",
        "--project",
        str(project_dir),
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


def lock_digest(lock_path: Path = SERVICE_LOCK) -> str:
    return hashlib.sha256(lock_path.read_bytes()).hexdigest()


def _canonicalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _decode_lock_string(raw: str, *, lock_path: Path) -> str:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"invalid quoted value in committed lock: {lock_path}") from None
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"invalid quoted value in committed lock: {lock_path}")
    return value


def locked_wheel_artifacts(
    lock_path: Path,
) -> dict[tuple[str, str], tuple[dict[str, str], ...]]:
    """Read immutable registry wheel URLs and digests from a uv lock.

    The release builder intentionally stays runnable with Apple's Python 3.9,
    so this parses only uv's closed package/wheel records rather than adding a
    runtime TOML dependency to the packaging path.
    """

    try:
        text = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise RuntimeError(f"committed lock is unreadable: {lock_path}") from None
    if not re.search(r"(?m)^version = 1\s*$", text):
        raise RuntimeError(f"committed lock schema is unsupported: {lock_path}")

    result: dict[tuple[str, str], tuple[dict[str, str], ...]] = {}
    blocks = _LOCK_PACKAGE_HEADER.split(text)[1:]
    if not blocks:
        raise RuntimeError(f"committed lock has no packages: {lock_path}")
    for block in blocks:
        name_match = _LOCK_SCALAR["name"].search(block)
        version_match = _LOCK_SCALAR["version"].search(block)
        if name_match is None or version_match is None:
            continue
        if not re.search(
            r'(?m)^source = \{ registry = "https://[^"\r\n]+" \}\s*$',
            block,
        ):
            continue
        name = _canonicalize_name(
            _decode_lock_string(name_match.group(1), lock_path=lock_path)
        )
        version = _decode_lock_string(version_match.group(1), lock_path=lock_path)
        wheels_match = _LOCK_WHEELS.search(block)
        if wheels_match is None:
            continue
        wheels: list[dict[str, str]] = []
        for wheel_match in _LOCK_WHEEL.finditer(wheels_match.group(1)):
            url = _decode_lock_string(wheel_match.group("url"), lock_path=lock_path)
            parsed = urlparse(url)
            filename = Path(unquote(parsed.path)).name
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or not filename.endswith(".whl")
            ):
                raise RuntimeError(
                    f"committed lock has an unsafe wheel URL for {name}: {lock_path}"
                )
            wheels.append(
                {
                    "name": name,
                    "version": version,
                    "wheel_filename": filename,
                    "wheel_sha256": wheel_match.group("sha256"),
                    "wheel_url": url,
                }
            )
        if not wheels:
            continue
        key = (name, version)
        if key in result:
            raise RuntimeError(
                f"committed lock repeats one registry package version: {name}=={version}"
            )
        result[key] = tuple(wheels)
    return result


def _python_abi_rank(python_tag: str, abi_tag: str) -> tuple[int, int] | None:
    py_major, py_minor = TARGET_PYTHON_VERSION
    exact_cp = f"cp{py_major}{py_minor}"
    if python_tag == exact_cp:
        if abi_tag == exact_cp:
            return (0, 0)
        if abi_tag == "abi3":
            return (1, 0)
        if abi_tag == "none":
            return (2, 0)
        return None
    match = re.fullmatch(r"cp(?P<major>\d)(?P<minor>\d+)", python_tag)
    if match is not None and abi_tag == "abi3":
        version = (int(match.group("major")), int(match.group("minor")))
        if (3, 2) <= version < TARGET_PYTHON_VERSION:
            return (1, py_minor - version[1])
        return None
    if abi_tag != "none":
        return None
    if python_tag == f"py{py_major}{py_minor}":
        return (3, 0)
    if python_tag == f"py{py_major}" or python_tag == "py2.py3":
        return (4, 0)
    return None


def _macos_platform_rank(platform_tag: str) -> tuple[int, int, int] | None:
    if platform_tag == "any":
        return (1, 0, 0)
    match = re.fullmatch(
        r"macosx_(?P<major>\d+)_(?P<minor>\d+)_(?P<arch>arm64|universal2)",
        platform_tag,
    )
    if match is None:
        return None
    version = (int(match.group("major")), int(match.group("minor")), 0)
    if version > TARGET_MACOS_VERSION:
        return None
    architecture_rank = 0 if match.group("arch") == TARGET_ARCHITECTURE else 1
    return (0, -version[0] * 1000 - version[1], architecture_rank)


def wheel_target_rank(filename: str) -> tuple[int, ...] | None:
    """Return a deterministic compatibility rank for CPython 3.12/macOS 15 arm64."""

    if not filename.endswith(".whl"):
        return None
    try:
        _, python_tags, abi_tags, platform_tags = filename[:-4].rsplit("-", 3)
    except ValueError:
        return None
    ranks: list[tuple[int, ...]] = []
    for python_tag in python_tags.split("."):
        for abi_tag in abi_tags.split("."):
            python_rank = _python_abi_rank(python_tag, abi_tag)
            if python_rank is None:
                continue
            for platform_tag in platform_tags.split("."):
                platform_rank = _macos_platform_rank(platform_tag)
                if platform_rank is not None:
                    ranks.append((*python_rank, *platform_rank))
    return min(ranks) if ranks else None


def bind_locked_registry_wheels(
    requirements: Sequence[str],
    lock_path: Path,
    *,
    bindings: list[dict[str, str]] | None = None,
) -> tuple[str, ...]:
    """Bind applicable registry pins to target-compatible lock artifacts.

    Requirements for other operating systems may have no compatible Mac wheel;
    they retain their marker-bound pin and are ignored by the target resolver.
    Any applicable unbound artifact is rejected by export inspection.
    """

    artifacts = locked_wheel_artifacts(lock_path)
    rendered: list[str] = []
    for requirement in requirements:
        match = EXACT_PINNED_REQUIREMENT.fullmatch(requirement)
        if match is None:
            rendered.append(requirement)
            continue
        project = match.group("project")
        name = _canonicalize_name(project.split("[", 1)[0])
        version = match.group("version")
        candidates: list[tuple[tuple[int, ...], dict[str, str]]] = []
        for artifact in artifacts.get((name, version), ()):
            rank = wheel_target_rank(artifact["wheel_filename"])
            if rank is not None:
                candidates.append((rank, artifact))
        if not candidates:
            rendered.append(requirement)
            continue
        candidates.sort(key=lambda item: (item[0], item[1]["wheel_filename"]))
        best_rank = candidates[0][0]
        best = [artifact for rank, artifact in candidates if rank == best_rank]
        if len(best) != 1:
            raise RuntimeError(
                f"committed lock has ambiguous target wheels for {name}=={version}"
            )
        binding = dict(best[0])
        if bindings is not None:
            bindings.append(binding)
        marker = match.group("marker") or ""
        rendered.append(
            f"{project} @ {binding['wheel_url']}#sha256={binding['wheel_sha256']}"
            f"{marker}"
        )
    return tuple(rendered)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path, *, maximum_bytes: int) -> object:
    try:
        raw = path.read_bytes()
        if len(raw) > maximum_bytes:
            raise ValueError
        return json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise RuntimeError(f"invalid packaging metadata: {path}") from None


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o644)


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        raise RuntimeError(f"runtime export path escapes its root: {path}") from None


def _validate_internal_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise RuntimeError(f"runtime export root must not be a symlink: {root}")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        raise RuntimeError(f"runtime export is missing: {root}") from None
    if not resolved_root.is_dir():
        raise RuntimeError(f"runtime export is not a directory: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_symlink():
            continue
        target = path.readlink()
        if target.is_absolute():
            raise RuntimeError(
                f"runtime export symlink must be relative: "
                f"{_relative_to_root(path, root)}"
            )
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            raise RuntimeError(
                f"runtime export symlink escapes or is dangling: "
                f"{_relative_to_root(path, root)}"
            ) from None


def _remove_bytecode(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.suffix in {".pyc", ".pyo"}:
            path.unlink()
    for path in sorted(root.rglob("__pycache__"), reverse=True):
        if path.is_dir() and not path.is_symlink():
            try:
                path.rmdir()
            except OSError:
                pass


def _is_mach_o(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) in MACH_O_MAGICS
    except OSError:
        return False


def _read_binary_range(stream: object, offset: int, size: int, total: int) -> bytes:
    if offset < 0 or size < 0 or offset + size > total:
        raise RuntimeError("Mach-O range is outside its containing file")
    stream.seek(offset)
    value = stream.read(size)
    if len(value) != size:
        raise RuntimeError("Mach-O file ended before its declared range")
    return value


def _decode_mach_o_version(value: int) -> tuple[int, int, int]:
    return (value >> 16, (value >> 8) & 0xFF, value & 0xFF)


def _thin_mach_o_minimums(
    stream: object,
    *,
    offset: int,
    size: int,
    total: int,
) -> list[tuple[int, int, int]]:
    magic = _read_binary_range(stream, offset, 4, total)
    thin_formats = {
        b"\xce\xfa\xed\xfe": ("<", 28),
        b"\xcf\xfa\xed\xfe": ("<", 32),
        b"\xfe\xed\xfa\xce": (">", 28),
        b"\xfe\xed\xfa\xcf": (">", 32),
    }
    details = thin_formats.get(magic)
    if details is None:
        raise RuntimeError("fat Mach-O slice is not a supported thin Mach-O")
    endian, header_size = details
    header = _read_binary_range(stream, offset, header_size, total)
    ncmds = struct.unpack_from(f"{endian}I", header, 16)[0]
    command_bytes = struct.unpack_from(f"{endian}I", header, 20)[0]
    if (
        ncmds > 65535
        or command_bytes > _MAX_MACH_O_LOAD_COMMAND_BYTES
        or header_size + command_bytes > size
    ):
        raise RuntimeError("Mach-O load-command table is invalid or unbounded")
    commands = _read_binary_range(
        stream,
        offset + header_size,
        command_bytes,
        total,
    )
    cursor = 0
    minimums: list[tuple[int, int, int]] = []
    for _ in range(ncmds):
        if cursor + 8 > len(commands):
            raise RuntimeError("Mach-O load-command table is truncated")
        command, command_size = struct.unpack_from(f"{endian}II", commands, cursor)
        if command_size < 8 or cursor + command_size > len(commands):
            raise RuntimeError("Mach-O load command has an invalid size")
        if command == _LC_VERSION_MIN_MACOSX:
            if command_size < 16:
                raise RuntimeError("Mach-O macOS version command is truncated")
            raw_version = struct.unpack_from(f"{endian}I", commands, cursor + 8)[0]
            minimums.append(_decode_mach_o_version(raw_version))
        elif command == _LC_BUILD_VERSION:
            if command_size < 24:
                raise RuntimeError("Mach-O build-version command is truncated")
            platform = struct.unpack_from(f"{endian}I", commands, cursor + 8)[0]
            if platform == _PLATFORM_MACOS:
                raw_version = struct.unpack_from(f"{endian}I", commands, cursor + 12)[0]
                minimums.append(_decode_mach_o_version(raw_version))
        cursor += command_size
    if cursor != len(commands):
        raise RuntimeError("Mach-O load-command byte count is inconsistent")
    return minimums


def mach_o_minimum_versions(path: Path) -> tuple[tuple[int, int, int], ...]:
    """Return the declared macOS minimum for every Mach-O slice."""

    try:
        total = path.stat().st_size
        with path.open("rb") as stream:
            magic = _read_binary_range(stream, 0, 4, total)
            fat_formats = {
                b"\xca\xfe\xba\xbe": (">", False),
                b"\xbe\xba\xfe\xca": ("<", False),
                b"\xca\xfe\xba\xbf": (">", True),
                b"\xbf\xba\xfe\xca": ("<", True),
            }
            fat = fat_formats.get(magic)
            if fat is None:
                minimums = _thin_mach_o_minimums(
                    stream,
                    offset=0,
                    size=total,
                    total=total,
                )
            else:
                endian, is_64 = fat
                header = _read_binary_range(stream, 0, 8, total)
                slice_count = struct.unpack_from(f"{endian}I", header, 4)[0]
                if not 1 <= slice_count <= _MAX_MACH_O_SLICES:
                    raise RuntimeError("fat Mach-O has an invalid slice count")
                entry_size = 32 if is_64 else 20
                table = _read_binary_range(
                    stream,
                    8,
                    entry_size * slice_count,
                    total,
                )
                minimums = []
                for index in range(slice_count):
                    entry_offset = index * entry_size
                    if is_64:
                        slice_offset, slice_size = struct.unpack_from(
                            f"{endian}QQ", table, entry_offset + 8
                        )
                    else:
                        slice_offset, slice_size = struct.unpack_from(
                            f"{endian}II", table, entry_offset + 8
                        )
                    minimums.extend(
                        _thin_mach_o_minimums(
                            stream,
                            offset=slice_offset,
                            size=slice_size,
                            total=total,
                        )
                    )
    except OSError as exc:
        raise RuntimeError(f"could not inspect Mach-O file {path}: {type(exc).__name__}") from None
    if not minimums:
        raise RuntimeError(f"Mach-O file has no macOS deployment target: {path}")
    return tuple(minimums)


def validate_mach_o_deployment_targets(root: Path) -> None:
    offenders: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file() or not _is_mach_o(path):
            continue
        try:
            minimums = mach_o_minimum_versions(path)
        except RuntimeError as exc:
            raise RuntimeError(
                f"could not prove bundled Mach-O deployment target: "
                f"{_relative_to_root(path, root)}: {exc}"
            ) from None
        for minimum in minimums:
            if minimum > TARGET_MACOS_VERSION:
                rendered = ".".join(str(part) for part in minimum)
                offenders.append(f"{_relative_to_root(path, root)} ({rendered})")
    if offenders:
        raise RuntimeError(
            f"bundled Mach-O requires macOS newer than {TARGET_MACOS_VERSION_TEXT}: "
            f"{offenders[:20]}"
        )


def _export_entries(root: Path) -> list[dict[str, object]]:
    excluded = {EXPORT_MANIFEST_NAME, EXPORT_PROVENANCE_NAME}
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if "/" not in relative and relative in excluded:
            continue
        details = path.lstat()
        mode = stat.S_IMODE(details.st_mode)
        if stat.S_ISLNK(details.st_mode):
            entries.append(
                {
                    "mode": mode,
                    "path": relative,
                    "target": os.readlink(path),
                    "type": "symlink",
                }
            )
        elif stat.S_ISDIR(details.st_mode):
            entries.append({"mode": mode, "path": relative, "type": "directory"})
        elif stat.S_ISREG(details.st_mode):
            codesign_mutable = _is_mach_o(path) and bool(
                mode & 0o111 or path.suffix in {".so", ".dylib"}
            )
            entries.append(
                {
                    "codesign_mutable": codesign_mutable,
                    "mode": mode,
                    "path": relative,
                    "sha256": _sha256(path),
                    "size": details.st_size,
                    "type": "file",
                }
            )
        else:
            raise RuntimeError(f"unsupported runtime export entry: {relative}")
    return entries


def _compare_export_entries(
    expected: object,
    actual: list[dict[str, object]],
    *,
    allow_codesign_mutation: bool,
) -> None:
    if not isinstance(expected, list) or not all(isinstance(item, dict) for item in expected):
        raise RuntimeError("runtime export manifest has an invalid entry inventory")
    expected_by_path = {item.get("path"): item for item in expected}
    actual_by_path = {item.get("path"): item for item in actual}
    if len(expected_by_path) != len(expected) or len(actual_by_path) != len(actual):
        raise RuntimeError("runtime export manifest contains duplicate paths")
    if set(expected_by_path) != set(actual_by_path):
        missing = sorted(str(path) for path in set(expected_by_path) - set(actual_by_path))
        extra = sorted(str(path) for path in set(actual_by_path) - set(expected_by_path))
        raise RuntimeError(
            f"runtime export inventory mismatch; missing={missing}, extra={extra}"
        )
    changed: list[str] = []
    for path, expected_entry in expected_by_path.items():
        actual_entry = actual_by_path[path]
        if (
            allow_codesign_mutation
            and expected_entry.get("type") == "file"
            and expected_entry.get("codesign_mutable") is True
        ):
            comparable_expected = {
                key: value
                for key, value in expected_entry.items()
                if key not in {"sha256", "size"}
            }
            comparable_actual = {
                key: value
                for key, value in actual_entry.items()
                if key not in {"sha256", "size"}
            }
            if comparable_expected != comparable_actual or not actual_entry.get("codesign_mutable"):
                changed.append(str(path))
        elif expected_entry != actual_entry:
            changed.append(str(path))
    if changed:
        raise RuntimeError(f"runtime export content differs from its manifest: {changed}")


_IDENTITY_SCRIPT = r"""
import importlib.metadata
import json
import platform
import sys

print(json.dumps({
    "implementation": sys.implementation.name,
    "machine": platform.machine(),
    "platform": sys.platform,
    "runtime_paths": {
        "base_prefix": sys.base_prefix,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "sys_path": sys.path,
    },
    "version": list(sys.version_info[:3]),
    "distribution_count": len(list(importlib.metadata.distributions())),
}, sort_keys=True))
"""


_LAYER_INSPECTION_SCRIPT = r"""
import json
import platform
import re
import sys
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

payload = json.loads(sys.stdin.read())
requirements = payload["requirements"]
site = Path(payload["site_packages"])
expected = {}
for raw in requirements:
    requirement = Requirement(raw)
    if requirement.marker is not None and not requirement.marker.evaluate():
        continue
    name = canonicalize_name(requirement.name)
    if name in expected:
        raise RuntimeError(f"duplicate applicable requirement: {name}")
    if requirement.url is None:
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
            raise RuntimeError(f"non-exact applicable requirement: {raw}")
        expected[name] = {"name": name, "version": specifiers[0].version}
    else:
        match = re.fullmatch(
            r"git\+(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-fA-F]{40})",
            requirement.url,
        )
        if name != "mflux" or match is None:
            raise RuntimeError(f"unsupported VCS requirement: {raw}")
        expected[name] = {
            "commit": match.group(2).lower(),
            "name": name,
            "source_url": match.group(1),
        }

actual = {}
for distribution in metadata.distributions(path=[str(site)]):
    raw_name = distribution.metadata.get("Name")
    if not raw_name:
        raise RuntimeError("installed distribution has no Name metadata")
    name = canonicalize_name(raw_name)
    if name in actual:
        raise RuntimeError(f"duplicate installed distribution: {name}")
    entry = {"name": name, "version": distribution.version}
    direct_text = distribution.read_text("direct_url.json")
    if direct_text is None:
        raise RuntimeError(f"distribution has no sealed wheel provenance: {name}")
    direct = json.loads(direct_text)
    archive = direct.get("archive_info")
    hashes = archive.get("hashes") if isinstance(archive, dict) else None
    digest = hashes.get("sha256") if isinstance(hashes, dict) else None
    parsed = urlparse(str(direct.get("url", "")))
    filename = Path(unquote(parsed.path)).name
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError(f"distribution has invalid wheel digest: {name}")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", filename):
        raise RuntimeError(f"distribution has invalid wheel filename: {name}")
    if name in expected and "commit" in expected[name]:
        if parsed.scheme != "file" or parsed.query or parsed.fragment:
            raise RuntimeError(f"VCS distribution has invalid wheel provenance: {name}")
        entry.update({
            "commit": expected[name]["commit"],
            "source_url": expected[name]["source_url"],
            "wheel_filename": filename,
            "wheel_sha256": digest,
        })
    else:
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError(f"registry distribution has invalid wheel provenance: {name}")
        entry.update({
            "wheel_filename": filename,
            "wheel_sha256": digest,
            "wheel_url": direct["url"],
        })
    actual[name] = entry

if set(actual) != set(expected):
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    raise RuntimeError(f"distribution inventory mismatch; missing={missing}, extra={extra}")
for name, requirement in expected.items():
    if "version" in requirement and actual[name]["version"] != requirement["version"]:
        raise RuntimeError(
            f"distribution version mismatch: {name}={actual[name]['version']} "
            f"expected {requirement['version']}"
        )

print(json.dumps({
    "distributions": [actual[name] for name in sorted(actual)],
    "identity": {
        "implementation": sys.implementation.name,
        "machine": platform.machine(),
        "platform": sys.platform,
        "version": list(sys.version_info[:3]),
    },
    "runtime_paths": {
        "base_prefix": sys.base_prefix,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "sys_path": sys.path,
    },
}, sort_keys=True))
"""


def _run_embedded_python(
    executable: Path,
    script: str,
    *,
    payload: object | None = None,
    python_home: Path | None = None,
) -> dict:
    environment = {
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    arguments = [str(executable), "-B", "-P", "-s", "-c", script]
    if python_home is not None:
        environment["PYTHONHOME"] = str(python_home)
    try:
        completed = subprocess.run(
            arguments,
            input=json.dumps(payload) if payload is not None else None,
            cwd=executable.parent,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"embedded interpreter validation failed for {executable}: "
            f"{type(exc).__name__}"
        ) from None
    if completed.returncode != 0 or len(completed.stdout) > 4 * 1024 * 1024:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"embedded interpreter validation failed for {executable}: "
            f"{detail[:1000] or completed.returncode}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"embedded interpreter returned invalid validation data: {executable}"
        ) from None
    if not isinstance(value, dict):
        raise RuntimeError(f"embedded interpreter validation data is not an object: {executable}")
    return value


def _validate_runtime_paths(value: object, root: Path, *, label: str) -> None:
    if not isinstance(value, dict):
        raise RuntimeError(f"embedded interpreter returned invalid paths: {label}")
    raw_paths = value.get("sys_path")
    singular = [value.get("base_prefix"), value.get("executable"), value.get("prefix")]
    if not isinstance(raw_paths, list) or not all(
        isinstance(path, str) and path for path in [*singular, *raw_paths]
    ):
        raise RuntimeError(f"embedded interpreter returned invalid paths: {label}")
    resolved_root = root.resolve(strict=True)
    escaped: list[str] = []
    for raw_path in [*singular, *raw_paths]:
        path = Path(raw_path)
        if not path.is_absolute():
            escaped.append(raw_path)
            continue
        try:
            path.resolve(strict=False).relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            escaped.append(raw_path)
    if escaped:
        raise RuntimeError(
            f"embedded interpreter paths escape the runtime export: {label}: {escaped}"
        )


def _runtime_graph(root: Path) -> dict[str, object]:
    runtime_root = root / "cpython-3.12"
    runtime = runtime_root / "bin" / "python3"
    identity = _run_embedded_python(
        runtime,
        _IDENTITY_SCRIPT,
        python_home=runtime_root,
    )
    _validate_runtime_paths(
        identity.pop("runtime_paths", None),
        root,
        label="cpython-3.12",
    )
    distribution_count = identity.pop("distribution_count", None)
    if identity != EXPECTED_PYTHON_IDENTITY or distribution_count != 0:
        raise RuntimeError(f"embedded CPython identity is incompatible: {identity}")

    layers: dict[str, object] = {}
    for layer_name, project_dir, lock_path in RUNTIME_LAYERS:
        sites = sorted((root / layer_name / "lib").glob("python*/site-packages"))
        if len(sites) != 1 or not sites[0].is_dir() or sites[0].is_symlink():
            raise RuntimeError(f"runtime layer has no exact site-packages directory: {layer_name}")
        requirements = locked_requirements(project_dir, lock_path)
        graph = _run_embedded_python(
            root / layer_name / "bin" / "python3",
            _LAYER_INSPECTION_SCRIPT,
            payload={"requirements": requirements, "site_packages": str(sites[0])},
        )
        _validate_runtime_paths(
            graph.pop("runtime_paths", None),
            root,
            label=layer_name,
        )
        if graph.get("identity") != EXPECTED_PYTHON_IDENTITY:
            raise RuntimeError(f"runtime layer interpreter is incompatible: {layer_name}")
        layers[layer_name] = graph
    return {"layers": layers, "runtime_identity": identity}


def _assert_vcs_bindings(
    graph: dict[str, object],
    bindings: dict[str, list[dict[str, str]]],
) -> None:
    layers = graph.get("layers")
    if not isinstance(layers, dict):
        raise RuntimeError("runtime graph has no layers")
    for layer_name, _, _ in RUNTIME_LAYERS:
        layer = layers.get(layer_name)
        distributions = layer.get("distributions") if isinstance(layer, dict) else None
        if not isinstance(distributions, list):
            raise RuntimeError(f"runtime graph is malformed: {layer_name}")
        actual = {
            str(item.get("name")): item
            for item in distributions
            if isinstance(item, dict) and "commit" in item
        }
        expected = {
            str(item.get("name")): item for item in bindings.get(layer_name, [])
        }
        if actual != expected:
            raise RuntimeError(
                f"runtime VCS provenance differs from its validated wheel cache: {layer_name}"
            )


def _assert_registry_bindings(
    graph: dict[str, object],
    bindings: dict[str, list[dict[str, str]]],
) -> None:
    layers = graph.get("layers")
    if not isinstance(layers, dict):
        raise RuntimeError("runtime graph has no layers")
    for layer_name, _, _ in RUNTIME_LAYERS:
        layer = layers.get(layer_name)
        distributions = layer.get("distributions") if isinstance(layer, dict) else None
        if not isinstance(distributions, list):
            raise RuntimeError(f"runtime graph is malformed: {layer_name}")
        expected = {
            (item["name"], item["version"]): item
            for item in bindings.get(layer_name, [])
        }
        actual_registry = [
            item
            for item in distributions
            if isinstance(item, dict) and "commit" not in item
        ]
        changed: list[str] = []
        for item in actual_registry:
            key = (str(item.get("name")), str(item.get("version")))
            if expected.get(key) != item:
                changed.append(f"{key[0]}=={key[1]}")
        if changed:
            raise RuntimeError(
                "runtime registry provenance differs from target-compatible "
                f"committed lock artifacts: {layer_name}: {sorted(changed)}"
            )


def _current_registry_bindings() -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for layer_name, project_dir, lock_path in RUNTIME_LAYERS:
        layer_bindings: list[dict[str, str]] = []
        bind_locked_registry_wheels(
            locked_requirements(project_dir, lock_path),
            lock_path,
            bindings=layer_bindings,
        )
        result[layer_name] = layer_bindings
    return result


def make_export_relocatable(output_dir: Path) -> None:
    """Replace deployment-time absolute framework links with sealed relative ones."""

    for layer_name, _, _ in RUNTIME_LAYERS:
        layer = output_dir / layer_name
        metadata_path = layer / "share/venv/metadata/venvstacks_layer.json"
        metadata = _read_json(metadata_path, maximum_bytes=16 * 1024)
        if metadata != _FRAMEWORK_LAYER_METADATA:
            raise RuntimeError(
                f"runtime layer has an unsupported relocation contract: {layer_name}"
            )
        payloads = {
            layer / "bin/python": (_RELOCATABLE_FRAMEWORK_WRAPPER, 0o755),
            layer / "pyvenv.cfg": (_RELOCATABLE_PYVENV_CONFIG, 0o644),
            layer
            / "lib/python3.12/site-packages/sitecustomize.py": (
                _RELOCATABLE_SITE_CUSTOMIZE,
                0o644,
            ),
        }
        for path, (contents, mode) in payloads.items():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"runtime relocation target is unsafe: {path}")
            path.write_text(contents, encoding="utf-8")
            path.chmod(mode)


def _base_export_provenance() -> dict[str, object]:
    return {
        "image_worker_lock_sha256": lock_digest(IMAGE_WORKER_LOCK),
        "minimum_macos_version": TARGET_MACOS_VERSION_TEXT,
        "platform": "macosx_arm64",
        "python_implementation": "cpython@3.12.10",
        "relocatable_layer_schema_version": 1,
        "schema_version": 1,
        "service_lock_sha256": lock_digest(),
        "venvstacks_version": VENVSTACKS_VERSION,
    }


def write_export_provenance(
    output_dir: Path,
    *,
    vcs_bindings: dict[str, list[dict[str, str]]],
    registry_bindings: dict[str, list[dict[str, str]]] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    _validate_internal_symlinks(output_dir)
    _remove_bytecode(output_dir)
    validate_mach_o_deployment_targets(output_dir)
    graph = _runtime_graph(output_dir)
    _assert_vcs_bindings(graph, vcs_bindings)
    _assert_registry_bindings(
        graph,
        registry_bindings
        if registry_bindings is not None
        else _current_registry_bindings(),
    )
    _remove_bytecode(output_dir)
    manifest_path = output_dir / EXPORT_MANIFEST_NAME
    _write_json(
        manifest_path,
        {"entries": _export_entries(output_dir), "schema_version": 1},
    )
    destination = output_dir / EXPORT_PROVENANCE_NAME
    _write_json(
        destination,
        {
            **_base_export_provenance(),
            **graph,
            "manifest_sha256": _sha256(manifest_path),
        },
    )
    return destination


def validate_export(
    output_dir: Path,
    *,
    allow_codesign_mutation: bool = False,
) -> None:
    """Reject an absent, stale, escaped, tampered, or incomplete export."""

    root = output_dir.absolute()
    _validate_internal_symlinks(root)
    validate_mach_o_deployment_targets(root)
    bytecode = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix in {".pyc", ".pyo"}
    ]
    if bytecode:
        raise RuntimeError(f"runtime export contains generated bytecode: {bytecode[:20]}")

    provenance_path = root / EXPORT_PROVENANCE_NAME
    manifest_path = root / EXPORT_MANIFEST_NAME
    provenance = _read_json(provenance_path, maximum_bytes=16 * 1024 * 1024)
    manifest = _read_json(manifest_path, maximum_bytes=128 * 1024 * 1024)
    if not isinstance(provenance, dict) or not isinstance(manifest, dict):
        raise RuntimeError("runtime export packaging metadata must be JSON objects")
    if manifest.get("schema_version") != 1:
        raise RuntimeError("runtime export manifest schema is unsupported")
    if provenance.get("manifest_sha256") != _sha256(manifest_path):
        raise RuntimeError("runtime export manifest digest does not match provenance")
    _compare_export_entries(
        manifest.get("entries"),
        _export_entries(root),
        allow_codesign_mutation=allow_codesign_mutation,
    )

    graph = _runtime_graph(root)
    _assert_registry_bindings(graph, _current_registry_bindings())
    expected = {**_base_export_provenance(), **graph, "manifest_sha256": _sha256(manifest_path)}
    if provenance != expected:
        raise RuntimeError(
            "runtime export lock, interpreter, dependency, or VCS provenance is stale; "
            "rebuild it with python3 macos/packaging/build_runtime.py"
        )


def resolved_config(
    requirements: Sequence[str] | None = None,
    image_requirements: Sequence[str] | None = None,
) -> str:
    if requirements is None:
        requirements = locked_requirements()
    if image_requirements is None:
        image_requirements = locked_requirements(IMAGE_WORKER_DIR, IMAGE_WORKER_LOCK)
    rendered = "requirements = [\n" + "".join(
        f"  {json.dumps(requirement)},\n" for requirement in requirements
    ) + "]"
    image_rendered = "requirements = [\n" + "".join(
        f"  {json.dumps(requirement)},\n" for requirement in image_requirements
    ) + "]"
    template = TEMPLATE.read_text(encoding="utf-8")
    if MARKER not in template:
        raise RuntimeError(f"requirements marker missing from {TEMPLATE}")
    if PROVENANCE_MARKER not in template:
        raise RuntimeError(f"lock provenance marker missing from {TEMPLATE}")
    if IMAGE_MARKER not in template:
        raise RuntimeError(f"image requirements marker missing from {TEMPLATE}")
    provenance = (
        "# Exact production pins exported from macos/service/uv.lock\n"
        f"# service uv.lock sha256: {lock_digest()}\n"
        "# Exact production pins exported from macos/image-worker/uv.lock\n"
        f"# image-worker uv.lock sha256: {lock_digest(IMAGE_WORKER_LOCK)}"
    )
    return (
        template.replace(PROVENANCE_MARKER, provenance, 1)
        .replace(MARKER, rendered, 1)
        .replace(IMAGE_MARKER, image_rendered, 1)
    )


def driver() -> list[str]:
    if shutil.which("uvx") is not None:
        return [
            "uvx",
            "--from",
            f"venvstacks=={VENVSTACKS_VERSION}",
            "venvstacks",
        ]
    if shutil.which("pipx") is not None:
        return [
            "pipx",
            "run",
            "--spec",
            f"venvstacks=={VENVSTACKS_VERSION}",
            "venvstacks",
        ]
    raise RuntimeError("the pinned venvstacks builder requires uvx or pipx")


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def _wheel_binding(wheel: Path, match: re.Match[str]) -> dict[str, str]:
    if wheel.is_symlink() or not wheel.is_file():
        raise RuntimeError(f"VCS wheel must be a regular file: {wheel}")
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata_files = [
                name
                for name in archive.namelist()
                if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_files) != 1:
                raise RuntimeError(
                    f"VCS wheel must contain one distribution METADATA file: {wheel}"
                )
            metadata = Parser().parsestr(
                archive.read(metadata_files[0]).decode("utf-8")
            )
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError):
        raise RuntimeError(f"VCS wheel is unreadable: {wheel}") from None
    project = match.group("project").split("[", 1)[0]
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or _canonicalize_name(name) != _canonicalize_name(project):
        raise RuntimeError(f"VCS wheel project does not match {project}: {wheel}")
    if not version or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]*", version):
        raise RuntimeError(f"VCS wheel has no valid version: {wheel}")
    wheel_parts = wheel.name.removesuffix(".whl").split("-")
    if (
        len(wheel_parts) < 5
        or _canonicalize_name(wheel_parts[0]) != _canonicalize_name(project)
        or wheel_parts[1] != version
    ):
        raise RuntimeError(f"VCS wheel filename does not match its metadata: {wheel}")
    return {
        "commit": match.group("commit").lower(),
        "name": _canonicalize_name(project),
        "source_url": match.group("url"),
        "version": version,
        "wheel_filename": wheel.name,
        "wheel_sha256": _sha256(wheel),
    }


def _cached_vcs_wheel(
    wheel_dir: Path,
    match: re.Match[str],
) -> tuple[Path, dict[str, str]] | None:
    if wheel_dir.is_symlink() or not wheel_dir.is_dir():
        return None
    wheels = sorted(wheel_dir.glob("*.whl"))
    sidecar = wheel_dir / VCS_WHEEL_PROVENANCE_NAME
    if len(wheels) != 1 or sidecar.is_symlink() or not sidecar.is_file():
        return None
    try:
        binding = _wheel_binding(wheels[0], match)
        recorded = _read_json(sidecar, maximum_bytes=4096)
    except RuntimeError:
        return None
    if recorded != {"schema_version": 1, **binding}:
        return None
    return wheels[0], binding


def _validate_vcs_cache_path(wheel_dir: Path) -> None:
    """Require the commit cache to remain below ordinary owned directories."""

    cache_root = VCS_WHEEL_DIR.absolute()
    candidate = wheel_dir.absolute()
    if candidate.parent.parent != cache_root:
        raise RuntimeError(f"VCS wheel cache path is outside its owned layout: {wheel_dir}")
    cache_root.mkdir(parents=True, exist_ok=True)
    candidate.parent.mkdir(exist_ok=True)
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise RuntimeError(f"VCS wheel cache directory is unsafe: {cache_root}")
    if candidate.parent.is_symlink() or not candidate.parent.is_dir():
        raise RuntimeError(f"VCS wheel cache directory is unsafe: {candidate.parent}")
    try:
        expected_parent = cache_root.resolve(strict=True) / candidate.parent.name
        if candidate.parent.resolve(strict=True) != expected_parent:
            raise RuntimeError(
                f"VCS wheel cache directory is unsafe: {candidate.parent}"
            )
    except OSError:
        raise RuntimeError(
            f"VCS wheel cache directory is unsafe: {candidate.parent}"
        ) from None
    if candidate.is_symlink():
        raise RuntimeError(f"VCS wheel cache entry must not be a symlink: {candidate}")


def _build_vcs_wheel(
    wheel_dir: Path,
    match: re.Match[str],
) -> tuple[Path, dict[str, str]]:
    _validate_vcs_cache_path(wheel_dir)
    wheel_parent = wheel_dir.parent
    temporary = Path(
        tempfile.mkdtemp(
            dir=wheel_parent,
            prefix=f".{wheel_dir.name}.building-",
        )
    )
    try:
        source = temporary / "source"
        output = temporary / "wheel"
        output.mkdir()
        commit = match.group("commit").lower()
        run(["git", "init", str(source)])
        run(["git", "-C", str(source), "remote", "add", "origin", match.group("url")])
        run(["git", "-C", str(source), "fetch", "--depth", "1", "origin", commit])
        run(["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"])
        resolved = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().lower()
        if resolved != commit:
            raise RuntimeError(
                f"VCS checkout resolved {resolved}, expected exact commit {commit}"
            )
        run(
            [
                uv_driver(),
                "build",
                "--wheel",
                "--out-dir",
                str(output),
                str(source),
            ]
        )
        wheels = sorted(output.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(
                f"expected one wheel for Git commit {commit}, found {len(wheels)}"
            )
        binding = _wheel_binding(wheels[0], match)
        _write_json(
            output / VCS_WHEEL_PROVENANCE_NAME,
            {"schema_version": 1, **binding},
        )
        _validate_vcs_cache_path(wheel_dir)
        if wheel_dir.exists():
            shutil.rmtree(wheel_dir)
        output.rename(wheel_dir)
        cached = _cached_vcs_wheel(wheel_dir, match)
        if cached is None:
            raise RuntimeError(f"new VCS wheel cache did not validate: {wheel_dir}")
        return cached
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def materialize_vcs_wheels(
    requirements: Sequence[str],
    *,
    bindings: list[dict[str, str]] | None = None,
) -> tuple[str, ...]:
    """Build immutable Git requirements into wheels accepted by venvstacks.

    venvstacks installs hash-locked binary artifacts and intentionally cannot
    pass a VCS checkout through that boundary. Each accepted full-SHA GitHub
    pin is therefore built once into a commit-keyed local wheel directory and
    handed to the generated stack as an exact file URL.
    """

    materialized: list[str] = []
    for requirement in requirements:
        match = EXACT_GITHUB_REQUIREMENT.fullmatch(requirement)
        if match is None:
            materialized.append(requirement)
            continue

        project = _canonicalize_name(match.group("project").split("[", 1)[0])
        commit = match.group("commit").lower()
        wheel_dir = VCS_WHEEL_DIR / project / commit
        cached = _cached_vcs_wheel(wheel_dir, match)
        wheel, binding = cached if cached is not None else _build_vcs_wheel(wheel_dir, match)
        if bindings is not None:
            bindings.append(binding)
        marker = match.group("marker") or ""
        materialized.append(
            f"{match.group('project')} @ {wheel.resolve().as_uri()}{marker}"
        )
    return tuple(materialized)


def build(output_dir: Path) -> None:
    service_vcs: list[dict[str, str]] = []
    image_vcs: list[dict[str, str]] = []
    service_registry: list[dict[str, str]] = []
    image_registry: list[dict[str, str]] = []
    service_requirements = materialize_vcs_wheels(
        locked_requirements(),
        bindings=service_vcs,
    )
    image_requirements = materialize_vcs_wheels(
        locked_requirements(IMAGE_WORKER_DIR, IMAGE_WORKER_LOCK),
        bindings=image_vcs,
    )
    service_requirements = bind_locked_registry_wheels(
        service_requirements,
        SERVICE_LOCK,
        bindings=service_registry,
    )
    image_requirements = bind_locked_registry_wheels(
        image_requirements,
        IMAGE_WORKER_LOCK,
        bindings=image_registry,
    )
    resolved_fd, resolved_name = tempfile.mkstemp(
        dir=PACKAGING_DIR,
        prefix=".mnemosyne-venvstacks-",
        suffix=".toml",
    )
    os.close(resolved_fd)
    resolved_path = Path(resolved_name)
    resolved_path.write_text(
        resolved_config(service_requirements, image_requirements), encoding="utf-8"
    )
    resolved_path.chmod(0o600)
    command = driver()
    try:
        run(command + ["lock", str(resolved_path), "--if-needed"])
        # A version pin can retain the same version while moving to a wheel
        # built for a different deployment target. Reusing a prior environment
        # would let pip keep the old same-version artifact, so release builds
        # always start from clean generated layers.
        run(command + ["build", str(resolved_path), "--clean"])
        run(
            command
            + [
                "local-export",
                str(resolved_path),
                "--output-dir",
                str(output_dir),
                "--force",
            ]
        )
        make_export_relocatable(output_dir)
        write_export_provenance(
            output_dir,
            vcs_bindings={
                "framework-mnemosyne-base": service_vcs,
                "framework-mnemosyne-image": image_vcs,
            },
            registry_bindings={
                "framework-mnemosyne-base": service_registry,
                "framework-mnemosyne-image": image_registry,
            },
        )
        validate_export(output_dir)
    finally:
        resolved_path.unlink(missing_ok=True)


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
    parser.add_argument(
        "--check-export",
        type=Path,
        help="require a complete runtime export bound to the current locks",
    )
    args = parser.parse_args()
    if args.check_export is not None:
        validate_export(args.check_export)
        print(
            "validated runtime export for service lock "
            f"sha256:{lock_digest()} and image-worker lock "
            f"sha256:{lock_digest(IMAGE_WORKER_LOCK)}"
        )
        return
    if args.check_lock:
        requirements = locked_requirements()
        image_requirements = locked_requirements(IMAGE_WORKER_DIR, IMAGE_WORKER_LOCK)
        print(
            f"validated {len(requirements)} production pins from "
            f"macos/service/uv.lock (sha256:{lock_digest()}); "
            f"{len(image_requirements)} production pins from "
            f"macos/image-worker/uv.lock (sha256:{lock_digest(IMAGE_WORKER_LOCK)})"
        )
        return
    if args.print_resolved:
        print(resolved_config(), end="")
        return
    build(args.output_dir.resolve())


if __name__ == "__main__":
    main()
