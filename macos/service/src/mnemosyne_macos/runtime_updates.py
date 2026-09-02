"""Official-source discovery and rollback-safe native engine updates.

MFLUX is installed from its official PyPI package into an isolated managed
directory. DS4 is built from an exact commit downloaded from its official
GitHub repository. oMLX remains externally owned and is updated by its own app
or Homebrew. No Unified Inference repository or release manifest is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import tarfile
import tempfile
import threading
import time
from typing import Any, Mapping
from uuid import uuid4

import httpx

from .config import (
    DS4Config,
    LlamaCppConfig,
    MFluxConfig,
    OMLXConfig,
)
from .models import ENGINE_RELEASE_TIER, EngineName


CORE_RUNTIME_PROTOCOL = 1
DEFAULT_RUNTIME_ROOT = (
    Path.home() / "Library" / "Application Support" / "Mnemosyne" / "runtimes"
)
MFLUX_PYPI_JSON_URL = "https://pypi.org/pypi/mflux/json"
MFLUX_PYPI_INDEX_URL = "https://pypi.org/simple"
OMLX_RELEASES_API_URL = "https://api.github.com/repos/jundot/omlx/releases?per_page=20"
DS4_COMMIT_API_URL = "https://api.github.com/repos/antirez/ds4/commits/main"
DS4_GLM53_PREVIEW_BRANCH = "glm-5.3-flash"
DS4_GLM53_PREVIEW_CHANNEL = "glm-5.3-flash"
DS4_GLM53_PREVIEW_COMMIT_API_URL = (
    "https://api.github.com/repos/antirez/ds4/commits/"
    f"{DS4_GLM53_PREVIEW_BRANCH}"
)
DS4_GLM53_PREVIEW_REPO = "antirez/glm-5.3-flash-gguf"
LLAMA_CPP_RELEASES_API_URL = (
    "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20"
)
LLAMA_CPP_RELEASE_DOWNLOAD_PREFIX = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
)
_LLAMA_CPP_BUILD_TAG_RE = re.compile(r"^b([1-9][0-9]*)$")
MAX_SOURCE_ARCHIVE_BYTES = 2 * 1024**3
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_ENGINES = ("llama.cpp", "omlx", "ds4", "mflux")
_OMLX_RELEASES_URL = "https://github.com/jundot/omlx/releases"
_LIFECYCLE_LIMIT = 256
_LIFECYCLE_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LIFECYCLE_EVENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_LIFECYCLE_FAILURE_CODES = {
    "activation_barrier",
    "build_or_smoke",
    "incompatible",
    "integrity",
    "invalid_journal",
    "runtime_error",
    "unsafe_archive",
}
_SOURCE_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/=@-]{0,159}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUNTIME_COMPATIBILITY_SCHEMA_VERSION = 2
_LLAMA_CPP_COMPATIBILITY_CONTRACT = "official-llama.cpp-macos-arm64-v1"
_DS4_COMPATIBILITY_CONTRACT = "official-ds4-source-build-v1"
_RUNTIME_COMPATIBILITY_FEATURES: Mapping[str, tuple[str, ...]] = {
    _LLAMA_CPP_COMPATIBILITY_CONTRACT: (
        "apple-metal",
        "flash-attention",
    ),
    # DS4's exact commit/build provenance is useful compatibility evidence,
    # but the current source build does not yet prove a portable feature list.
    # Keep it empty rather than deriving capabilities from a catalog claim.
    _DS4_COMPATIBILITY_CONTRACT: (),
}
_DS4_GLM53_SOURCE_CONTRACT: tuple[tuple[str, str, str, int], ...] = (
    ("glm53-q2", "GLM53_Q2_FILE", "Q2", 128),
    ("glm53-q4", "GLM53_Q4_FILE", "Q4_K", 256),
)


class RuntimeUpdateError(RuntimeError):
    """A runtime update could not be checked, prepared, or activated."""


def _omlx_macos_major_ranges(asset_name: str) -> tuple[range, ...]:
    matches = re.finditer(
        r"(?:^|[-_])macos(\d+)(?:-(\d+))?(?=$|[-_.])",
        asset_name.casefold(),
    )
    ranges: list[range] = []
    for match in matches:
        lower = int(match.group(1))
        upper = int(match.group(2) or lower)
        if upper >= lower:
            ranges.append(range(lower, upper + 1))
    return tuple(ranges)


def _official_omlx_installer_url(
    release: Mapping[str, Any],
    *,
    macos_major: int | None = None,
) -> str | None:
    """Select the official DMG matching this Mac without downloading it."""

    assets = release.get("assets")
    tag = release.get("tag_name")
    if not isinstance(assets, list) or not isinstance(tag, str):
        return None
    if macos_major is None:
        version = platform.mac_ver()[0]
        try:
            macos_major = int(version.split(".", 1)[0]) if version else None
        except ValueError:
            macos_major = None

    prefix = f"https://github.com/jundot/omlx/releases/download/{tag}/"
    dmgs: list[tuple[Mapping[str, Any], tuple[range, ...]]] = []
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        size = asset.get("size")
        digest = asset.get("digest")
        if (
            not isinstance(name, str)
            or not name.casefold().endswith(".dmg")
            or not isinstance(url, str)
            or not url.startswith(prefix)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)
        ):
            continue
        dmgs.append((asset, _omlx_macos_major_ranges(name)))

    selected: Mapping[str, Any] | None = None
    if macos_major is not None:
        selected = next(
            (
                asset
                for asset, ranges in dmgs
                if any(
                    values.start == macos_major
                    and values.stop == macos_major + 1
                    for values in ranges
                )
            ),
            None,
        )
        if selected is None:
            selected = next(
                (
                    asset
                    for asset, ranges in dmgs
                    if any(macos_major in values for values in ranges)
                ),
                None,
            )
    if selected is None and len(dmgs) == 1:
        selected = dmgs[0][0]
    url = selected.get("browser_download_url") if selected else None
    return str(url) if isinstance(url, str) else None


def _omlx_cli_candidates() -> tuple[Path, ...]:
    """Return CLI locations visible to both shells and a packaged LaunchAgent."""

    candidates: list[Path] = []
    discovered = shutil.which("omlx")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend(
        (
            Path.home() / ".omlx" / "bin" / "omlx",
            Path("/opt/homebrew/bin/omlx"),
            Path("/usr/local/bin/omlx"),
            Path("/opt/homebrew/opt/omlx/bin/omlx"),
            Path("/usr/local/opt/omlx/bin/omlx"),
        )
    )
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def _omlx_installation_kind(path: str | None) -> str:
    """Classify ownership without trusting PATH aliases or arbitrary commands."""

    if not path:
        return "not_installed"
    if path.startswith(("http://", "https://")):
        return "running_external"
    candidate = Path(path).expanduser()
    if candidate.suffix.casefold() == ".app":
        return "official_app"
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate
    normalized = str(resolved).casefold()
    if "/cellar/omlx/head-" in normalized:
        return "homebrew_head"
    if "/cellar/omlx/" in normalized or "/homebrew/opt/omlx/" in normalized:
        return "homebrew_stable"
    if "/.omlx/bin/omlx" in normalized:
        return "official_app_cli"
    return "external_cli"


def _homebrew_executable() -> Path | None:
    for candidate in (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


@dataclass(frozen=True)
class RuntimeRelease:
    engine: str
    version: str
    source_revision: str | None
    source_url: str
    release_notes_url: str
    minimum_macos: str = "15.0"
    architecture: str = "arm64"
    minimum_core_protocol: int = 1
    maximum_core_protocol: int = 1
    sha256: str | None = None
    asset_size: int | None = None
    channel: str = "official"
    source_branch: str | None = None

    @property
    def compatible(self) -> bool:
        protocol_compatible = (
            self.architecture == "arm64"
            and self.minimum_core_protocol
            <= CORE_RUNTIME_PROTOCOL
            <= self.maximum_core_protocol
        )
        if not protocol_compatible:
            return False
        if platform.system() != "Darwin":
            return True
        if platform.machine().casefold() not in {"arm64", "aarch64"}:
            return False
        macos_version = platform.mac_ver()[0]
        return not macos_version or _version_key(macos_version) >= _version_key(
            self.minimum_macos
        )


@dataclass(frozen=True)
class RuntimeCompatibilityEvidence:
    """Immutable, path-free proof used only for signed compatibility checks.

    ``compatibility_fingerprint`` is intentionally distinct from
    ``EngineAdapter``'s local benchmark fingerprint.  It is built from verified
    source provenance and a closed verification contract, so relocating a
    runtime or restoring file mtimes cannot change it.  The executable digest
    is deliberately excluded from that cross-Mac identity: DS4 is compiled
    locally, so equivalent exact source builds need one catalog-checkable
    provenance identity even when their Mach-O bytes differ.

    ``local_integrity_fingerprint`` binds that portable identity to the exact
    executable installed on this Mac.  Active pointers retain an independent
    copy of the local value, so changing both a runtime binary and its adjacent
    manifest seal does not preserve local compatibility authority.  It is
    never exported as a portable catalog fingerprint.
    """

    engine: str
    version: str
    source_revision: str | None
    channel: str
    source_branch: str | None
    source_sha256: str
    source_size_bytes: int
    executable_sha256: str
    verification_contract: str
    features: tuple[str, ...]
    compatibility_fingerprint: str
    local_integrity_fingerprint: str

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": _RUNTIME_COMPATIBILITY_SCHEMA_VERSION,
            "engine": self.engine,
            "version": self.version,
            "source_revision": self.source_revision,
            "channel": self.channel,
            "source_branch": self.source_branch,
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "executable_sha256": self.executable_sha256,
            "verification_contract": self.verification_contract,
            "features": list(self.features),
            "compatibility_fingerprint": self.compatibility_fingerprint,
            "local_integrity_fingerprint": self.local_integrity_fingerprint,
        }


@dataclass(frozen=True)
class ActiveRuntime:
    engine: str
    version: str
    source_revision: str | None
    root: Path
    entrypoint: Mapping[str, str]
    capabilities: tuple[Mapping[str, Any], ...] = ()
    channel: str = "official"
    source_branch: str | None = None
    source_contract_sha256: str | None = None
    compatibility_evidence: RuntimeCompatibilityEvidence | None = None

    def path(self, key: str) -> Path:
        relative = self.entrypoint.get(key)
        if not relative:
            raise RuntimeUpdateError(
                f"managed {self.engine} runtime does not declare entrypoint.{key}"
            )
        candidate = (self.root / relative).resolve(strict=False)
        if not candidate.is_relative_to(self.root.resolve(strict=False)):
            raise RuntimeUpdateError(
                f"managed {self.engine} entrypoint escapes its runtime folder"
            )
        return candidate


@dataclass(frozen=True)
class PreparedRuntime:
    release: RuntimeRelease
    runtime: ActiveRuntime


def _runtime_root(value: str | Path | None = None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    override = os.environ.get("MNEMOSYNE_RUNTIME_ROOT", "").strip()
    return Path(override).expanduser() if override else DEFAULT_RUNTIME_ROOT


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _runtime_failure_code(error: BaseException) -> str:
    text = str(error).casefold()
    if any(
        marker in text
        for marker in (
            "sha-256",
            "checksum",
            "digest",
            "integrity",
            "published size",
            "asset size",
        )
    ):
        return "integrity"
    if any(
        marker in text
        for marker in (
            "unsafe",
            "path traversal",
            "escaping symlink",
            "escaping hard link",
            "escapes its runtime folder",
            "unexpected layout",
            "device or fifo",
        )
    ):
        return "unsafe_archive"
    if any(
        marker in text
        for marker in (
            "command failed",
            "command timed out",
            "build",
            "did not produce",
            "import",
            "not executable",
        )
    ):
        return "build_or_smoke"
    if any(
        marker in text
        for marker in (
            "not the current official",
            "no compatible official",
            "protocol",
            "source revision",
            "unsupported runtime",
        )
    ):
        return "incompatible"
    if any(
        marker in text
        for marker in (
            "maintenance",
            "drain",
            "lease",
            "resident",
            "empty",
        )
    ):
        return "activation_barrier"
    return "runtime_error"


def _validate_engine(engine: str) -> str:
    normalized = engine.casefold()
    if normalized not in _ENGINES:
        raise RuntimeUpdateError(f"unsupported runtime-update engine '{engine}'")
    return normalized


def _validate_version(version: object) -> str:
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise RuntimeUpdateError("runtime version contains unsupported characters")
    return version


def _active_pointer(root: Path, engine: str) -> Path:
    return root / engine / "current.json"


def _pointer_integrity_fingerprint(
    payload: object,
    key: str,
) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) and _SHA256_ID_RE.fullmatch(value) else None


def _runtime_compatibility_fingerprint(
    *,
    engine: str,
    version: str,
    source_revision: str | None,
    channel: str,
    source_branch: str | None,
    source_sha256: str,
    source_size_bytes: int,
    verification_contract: str,
    features: tuple[str, ...],
) -> str:
    material = {
        "schema_version": _RUNTIME_COMPATIBILITY_SCHEMA_VERSION,
        "engine": engine,
        "version": version,
        "source_revision": source_revision,
        "channel": channel,
        "source_branch": source_branch,
        "source_sha256": source_sha256,
        "source_size_bytes": source_size_bytes,
        "verification_contract": verification_contract,
        "features": list(features),
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _runtime_local_integrity_fingerprint(
    *,
    compatibility_fingerprint: str,
    executable_sha256: str,
) -> str:
    """Bind portable provenance to one exact local executable."""

    material = {
        "schema_version": _RUNTIME_COMPATIBILITY_SCHEMA_VERSION,
        "compatibility_fingerprint": compatibility_fingerprint,
        "executable_sha256": executable_sha256,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _new_runtime_compatibility_evidence(
    release: RuntimeRelease,
    *,
    source_sha256: str,
    source_size_bytes: int,
    executable: Path,
) -> RuntimeCompatibilityEvidence:
    """Create evidence only for the two closed managed-runtime contracts."""

    engine = _validate_engine(release.engine)
    version = _validate_version(release.version)
    digest = source_sha256.casefold()
    if not _SHA256_ID_RE.fullmatch(digest):
        raise RuntimeUpdateError("managed runtime source SHA-256 is invalid")
    if (
        isinstance(source_size_bytes, bool)
        or not isinstance(source_size_bytes, int)
        or not 0 < source_size_bytes <= MAX_SOURCE_ARCHIVE_BYTES
    ):
        raise RuntimeUpdateError("managed runtime source size is invalid")

    if engine == "llama.cpp":
        expected_name = f"llama-{version}-bin-macos-arm64.tar.gz"
        expected_url = (
            f"{LLAMA_CPP_RELEASE_DOWNLOAD_PREFIX}{version}/{expected_name}"
        )
        if (
            release.channel != "official"
            or release.source_branch is not None
            or release.architecture != "arm64"
            or release.source_url != expected_url
            or release.sha256 is None
            or digest != f"sha256:{release.sha256.casefold()}"
            or release.asset_size != source_size_bytes
        ):
            raise RuntimeUpdateError(
                "llama.cpp compatibility provenance is incomplete"
            )
        contract = _LLAMA_CPP_COMPATIBILITY_CONTRACT
    elif engine == "ds4":
        revision = release.source_revision
        allowed_source = bool(
            isinstance(revision, str)
            and _GIT_COMMIT_RE.fullmatch(revision)
            and version == revision[:12]
            and release.source_url
            == f"https://codeload.github.com/antirez/ds4/tar.gz/{revision}"
            and (
                (
                    release.channel == "official"
                    and release.source_branch == "main"
                )
                or (
                    release.channel == DS4_GLM53_PREVIEW_CHANNEL
                    and release.source_branch == DS4_GLM53_PREVIEW_BRANCH
                )
            )
        )
        if not allowed_source:
            raise RuntimeUpdateError("DS4 compatibility provenance is incomplete")
        contract = _DS4_COMPATIBILITY_CONTRACT
    else:
        raise RuntimeUpdateError(
            f"managed compatibility evidence is unavailable for {engine}"
        )

    try:
        executable_digest = "sha256:" + _sha256_file(executable)
    except OSError as exc:
        raise RuntimeUpdateError(
            "managed runtime executable could not be sealed"
        ) from exc
    features = _RUNTIME_COMPATIBILITY_FEATURES[contract]
    fingerprint = _runtime_compatibility_fingerprint(
        engine=engine,
        version=version,
        source_revision=release.source_revision,
        channel=release.channel,
        source_branch=release.source_branch,
        source_sha256=digest,
        source_size_bytes=source_size_bytes,
        verification_contract=contract,
        features=features,
    )
    local_integrity_fingerprint = _runtime_local_integrity_fingerprint(
        compatibility_fingerprint=fingerprint,
        executable_sha256=executable_digest,
    )
    return RuntimeCompatibilityEvidence(
        engine=engine,
        version=version,
        source_revision=release.source_revision,
        channel=release.channel,
        source_branch=release.source_branch,
        source_sha256=digest,
        source_size_bytes=source_size_bytes,
        executable_sha256=executable_digest,
        verification_contract=contract,
        features=features,
        compatibility_fingerprint=fingerprint,
        local_integrity_fingerprint=local_integrity_fingerprint,
    )


def _load_runtime_compatibility_evidence(
    value: object,
    *,
    engine: str,
    version: str,
    source_revision: str | None,
    channel: str,
    source_branch: str | None,
    outer_source_sha256: object,
    outer_source_size_bytes: object,
    executable: Path,
) -> RuntimeCompatibilityEvidence | None:
    """Validate optional proof without making a legacy runtime unusable."""

    if not isinstance(value, dict):
        return None
    expected_keys = {
        "schema_version",
        "engine",
        "version",
        "source_revision",
        "channel",
        "source_branch",
        "source_sha256",
        "source_size_bytes",
        "executable_sha256",
        "verification_contract",
        "features",
        "compatibility_fingerprint",
        "local_integrity_fingerprint",
    }
    if set(value) != expected_keys:
        return None
    try:
        contract = value["verification_contract"]
        features_value = value["features"]
        source_digest = value["source_sha256"]
        executable_digest = value["executable_sha256"]
        source_size = value["source_size_bytes"]
        fingerprint = value["compatibility_fingerprint"]
        local_integrity_fingerprint = value["local_integrity_fingerprint"]
        normalized_outer_source = (
            outer_source_sha256.casefold()
            if isinstance(outer_source_sha256, str)
            and _SHA256_ID_RE.fullmatch(outer_source_sha256.casefold())
            else (
                "sha256:" + outer_source_sha256.casefold()
                if isinstance(outer_source_sha256, str)
                and re.fullmatch(r"[0-9a-fA-F]{64}", outer_source_sha256)
                else None
            )
        )
        if (
            value["schema_version"] != _RUNTIME_COMPATIBILITY_SCHEMA_VERSION
            or value["engine"] != engine
            or value["version"] != version
            or value["source_revision"] != source_revision
            or value["channel"] != channel
            or value["source_branch"] != source_branch
            or not isinstance(contract, str)
            or contract not in _RUNTIME_COMPATIBILITY_FEATURES
            or not isinstance(features_value, list)
            or not all(isinstance(item, str) for item in features_value)
            or tuple(features_value) != _RUNTIME_COMPATIBILITY_FEATURES[contract]
            or not isinstance(source_digest, str)
            or not _SHA256_ID_RE.fullmatch(source_digest)
            or normalized_outer_source != source_digest
            or isinstance(source_size, bool)
            or not isinstance(source_size, int)
            or not 0 < source_size <= MAX_SOURCE_ARCHIVE_BYTES
            or isinstance(outer_source_size_bytes, bool)
            or not isinstance(outer_source_size_bytes, int)
            or outer_source_size_bytes != source_size
            or not isinstance(executable_digest, str)
            or not _SHA256_ID_RE.fullmatch(executable_digest)
            or not isinstance(fingerprint, str)
            or not _SHA256_ID_RE.fullmatch(fingerprint)
            or not isinstance(local_integrity_fingerprint, str)
            or not _SHA256_ID_RE.fullmatch(local_integrity_fingerprint)
        ):
            return None
        if engine == "llama.cpp":
            if (
                contract != _LLAMA_CPP_COMPATIBILITY_CONTRACT
                or channel != "official"
                or source_branch is not None
                or (
                    source_revision is not None
                    and _SOURCE_REVISION_RE.fullmatch(source_revision) is None
                )
            ):
                return None
        elif engine == "ds4":
            if (
                contract != _DS4_COMPATIBILITY_CONTRACT
                or not isinstance(source_revision, str)
                or _GIT_COMMIT_RE.fullmatch(source_revision) is None
                or version != source_revision[:12]
                or not (
                    (channel == "official" and source_branch == "main")
                    or (
                        channel == DS4_GLM53_PREVIEW_CHANNEL
                        and source_branch == DS4_GLM53_PREVIEW_BRANCH
                    )
                )
            ):
                return None
        else:
            return None
        expected_fingerprint = _runtime_compatibility_fingerprint(
            engine=engine,
            version=version,
            source_revision=source_revision,
            channel=channel,
            source_branch=source_branch,
            source_sha256=source_digest,
            source_size_bytes=source_size,
            verification_contract=contract,
            features=tuple(features_value),
        )
        if not hmac.compare_digest(expected_fingerprint, fingerprint):
            return None
        expected_local_integrity_fingerprint = (
            _runtime_local_integrity_fingerprint(
                compatibility_fingerprint=fingerprint,
                executable_sha256=executable_digest,
            )
        )
        if not hmac.compare_digest(
            expected_local_integrity_fingerprint,
            local_integrity_fingerprint,
        ):
            return None
        current_executable_digest = "sha256:" + _sha256_file(executable)
        if not hmac.compare_digest(current_executable_digest, executable_digest):
            return None
    except (OSError, TypeError, ValueError):
        return None
    return RuntimeCompatibilityEvidence(
        engine=engine,
        version=version,
        source_revision=source_revision,
        channel=channel,
        source_branch=source_branch,
        source_sha256=source_digest,
        source_size_bytes=source_size,
        executable_sha256=executable_digest,
        verification_contract=contract,
        features=tuple(features_value),
        compatibility_fingerprint=fingerprint,
        local_integrity_fingerprint=local_integrity_fingerprint,
    )


def _ds4_glm53_source_contract(
    source: Path,
) -> tuple[tuple[Mapping[str, Any], ...], str]:
    """Derive the closed preview model contract from one exact DS4 source tree."""

    script_path = source / "download_model.sh"
    try:
        script_bytes = script_path.read_bytes()
    except OSError as exc:
        raise RuntimeUpdateError(
            "DS4 GLM 5.3 preview source is missing download_model.sh"
        ) from exc
    if not script_bytes or len(script_bytes) > 512 * 1024:
        raise RuntimeUpdateError(
            "DS4 GLM 5.3 preview download_model.sh has an invalid size"
        )
    try:
        script = script_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeUpdateError(
            "DS4 GLM 5.3 preview download_model.sh is not UTF-8"
        ) from exc

    def assignment(name: str) -> str:
        matches = re.findall(
            rf'(?m)^{re.escape(name)}="([^"\r\n]+)"\s*$',
            script,
        )
        if len(matches) != 1:
            raise RuntimeUpdateError(
                f"DS4 GLM 5.3 preview source must declare exactly one {name}"
            )
        return matches[0]

    if assignment("GLM53_REPO") != DS4_GLM53_PREVIEW_REPO:
        raise RuntimeUpdateError(
            "DS4 GLM 5.3 preview source declared an unsupported model repository"
        )

    capabilities: list[Mapping[str, Any]] = []
    for target, variable, quantization, minimum_memory_gb in (
        _DS4_GLM53_SOURCE_CONTRACT
    ):
        filename = assignment(variable)
        if (
            len(filename) > 255
            or filename != Path(filename).name
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.gguf", filename)
            or "glm-5.3-flash" not in filename.casefold()
            or (quantization == "Q2" and "q2" not in filename.casefold())
            or (quantization == "Q4_K" and "q4" not in filename.casefold())
            or "fp8" in filename.casefold()
            or "vision" in filename.casefold()
        ):
            raise RuntimeUpdateError(
                f"DS4 GLM 5.3 preview source declared an invalid {target} filename"
            )
        block_match = re.search(
            rf"(?ms)^\s*{re.escape(target)}\)\s*(.*?)^\s*;;\s*$",
            script,
        )
        block = block_match.group(1) if block_match else ""
        if not all(
            re.search(marker, block, flags=re.MULTILINE)
            for marker in (
                r"^\s*REPO=\$GLM53_REPO\s*$",
                rf"^\s*MODEL_FILE=\${re.escape(variable)}\s*$",
                r"^\s*FORCE_HF_DOWNLOAD=1\s*$",
            )
        ):
            raise RuntimeUpdateError(
                f"DS4 GLM 5.3 preview source does not bind {target} to its official file"
            )
        capabilities.append(
            {
                "kind": "language-model",
                "family": "glm-5.3-flash",
                "target": target,
                "repo_id": DS4_GLM53_PREVIEW_REPO,
                "filename": filename,
                "quantization": quantization,
                "minimum_memory_gb": minimum_memory_gb,
                "release_tier": "experimental",
            }
        )
    return tuple(capabilities), hashlib.sha256(script_bytes).hexdigest()


def ds4_glm53_preview_capabilities(
    runtime: ActiveRuntime,
) -> tuple[Mapping[str, Any], ...]:
    """Validate and return a managed preview runtime's source-bound model list."""

    if runtime.engine != "ds4" or runtime.channel != DS4_GLM53_PREVIEW_CHANNEL:
        return ()
    if (
        runtime.source_branch != DS4_GLM53_PREVIEW_BRANCH
        or not isinstance(runtime.source_revision, str)
        or not _GIT_COMMIT_RE.fullmatch(runtime.source_revision)
        or not isinstance(runtime.source_contract_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", runtime.source_contract_sha256)
    ):
        raise RuntimeUpdateError(
            "managed DS4 GLM 5.3 preview provenance is incomplete"
        )
    capabilities, digest = _ds4_glm53_source_contract(
        runtime.path("working_directory")
    )
    if not hmac.compare_digest(digest, runtime.source_contract_sha256):
        raise RuntimeUpdateError(
            "managed DS4 GLM 5.3 preview source contract changed after preparation"
        )
    if tuple(runtime.capabilities) != capabilities:
        raise RuntimeUpdateError(
            "managed DS4 GLM 5.3 preview manifest does not match its source contract"
        )
    return capabilities


def _load_runtime_directory(
    directory: Path,
    *,
    expected_engine: str,
    verify_compatibility_evidence: bool = False,
) -> ActiveRuntime:
    manifest_path = directory / "runtime.json"
    try:
        payload = _read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeUpdateError(f"invalid runtime metadata at {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeUpdateError("runtime.json must use schema_version 1")
    engine = _validate_engine(str(payload.get("engine", "")))
    if engine != expected_engine:
        raise RuntimeUpdateError(
            f"runtime is for {engine}, not requested engine {expected_engine}"
        )
    version = _validate_version(payload.get("version"))
    protocol = payload.get("core_protocol")
    if protocol != CORE_RUNTIME_PROTOCOL:
        raise RuntimeUpdateError(
            f"runtime requires core protocol {protocol}; this app supports "
            f"{CORE_RUNTIME_PROTOCOL}"
        )
    source_revision = payload.get("source_revision")
    if source_revision is not None and not isinstance(source_revision, str):
        raise RuntimeUpdateError("runtime source_revision must be a string")
    entrypoint = payload.get("entrypoint")
    if not isinstance(entrypoint, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in entrypoint.items()
    ):
        raise RuntimeUpdateError("runtime entrypoint must be a string mapping")
    capabilities_value = payload.get("capabilities", [])
    if not isinstance(capabilities_value, list) or not all(
        isinstance(item, dict) for item in capabilities_value
    ):
        raise RuntimeUpdateError("runtime capabilities must be a list of objects")
    channel = payload.get("channel", "official")
    source_branch = payload.get("source_branch")
    source_contract_sha256 = payload.get("source_contract_sha256")
    outer_source_sha256 = payload.get("source_sha256")
    outer_source_size_bytes = payload.get("source_size_bytes")
    compatibility_value = payload.get("compatibility_evidence")
    if not isinstance(channel, str) or not channel:
        raise RuntimeUpdateError("runtime channel must be a non-empty string")
    if source_branch is not None and not isinstance(source_branch, str):
        raise RuntimeUpdateError("runtime source_branch must be a string")
    if source_contract_sha256 is not None and not isinstance(
        source_contract_sha256, str
    ):
        raise RuntimeUpdateError("runtime source_contract_sha256 must be a string")
    runtime = ActiveRuntime(
        engine=engine,
        version=version,
        source_revision=source_revision,
        root=directory,
        entrypoint=dict(entrypoint),
        capabilities=tuple(dict(item) for item in capabilities_value),
        channel=channel,
        source_branch=source_branch,
        source_contract_sha256=source_contract_sha256,
    )
    if engine == "mflux":
        worker = runtime.path("worker_path")
        if not (worker / "mnemosyne_mflux_worker").is_dir():
            raise RuntimeUpdateError(f"MFLUX worker package is missing below {worker}")
        if "site_packages" in runtime.entrypoint:
            site_packages = runtime.path("site_packages")
            if not site_packages.is_dir():
                raise RuntimeUpdateError(
                    f"MFLUX managed packages are missing: {site_packages}"
                )
        else:
            # Backward compatibility for packs produced by the earlier updater.
            python = runtime.path("python")
            if not python.is_file() or not os.access(python, os.X_OK):
                raise RuntimeUpdateError(f"MFLUX Python is not executable: {python}")
    elif engine == "ds4":
        binary = runtime.path("binary")
        working_directory = runtime.path("working_directory")
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeUpdateError(f"DS4 binary is not executable: {binary}")
        if not working_directory.is_dir():
            raise RuntimeUpdateError(
                f"DS4 working directory is missing: {working_directory}"
            )
        if runtime.channel == DS4_GLM53_PREVIEW_CHANNEL:
            ds4_glm53_preview_capabilities(runtime)
        elif runtime.channel != "official":
            raise RuntimeUpdateError(
                f"unsupported managed DS4 runtime channel '{runtime.channel}'"
            )
    elif engine == "llama.cpp":
        binary = runtime.path("binary")
        working_directory = runtime.path("working_directory")
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeUpdateError(f"llama.cpp binary is not executable: {binary}")
        if not working_directory.is_dir():
            raise RuntimeUpdateError(
                f"llama.cpp working directory is missing: {working_directory}"
            )
    if engine in {"llama.cpp", "ds4"} and verify_compatibility_evidence:
        runtime = replace(
            runtime,
            compatibility_evidence=_load_runtime_compatibility_evidence(
                compatibility_value,
                engine=runtime.engine,
                version=runtime.version,
                source_revision=runtime.source_revision,
                channel=runtime.channel,
                source_branch=runtime.source_branch,
                outer_source_sha256=outer_source_sha256,
                outer_source_size_bytes=outer_source_size_bytes,
                executable=binary,
            ),
        )
    return runtime


def resolve_active_runtime(
    engine: str,
    *,
    root: str | Path | None = None,
    verify_compatibility_evidence: bool = False,
) -> ActiveRuntime | None:
    """Resolve the active managed runtime, returning no override if invalid.

    Ordinary engine and catalog lookups deliberately skip executable hashing.
    Signed compatibility authority opts into the more expensive integrity
    proof and runs that lookup off the asyncio event loop.
    """

    normalized = _validate_engine(engine)
    runtime_root = _runtime_root(root)
    pointer = _active_pointer(runtime_root, normalized)
    try:
        payload = _read_json(pointer)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return None
        version = _validate_version(payload.get("version"))
        directory = runtime_root / normalized / version
        runtime = _load_runtime_directory(
            directory,
            expected_engine=normalized,
            verify_compatibility_evidence=verify_compatibility_evidence,
        )
        if (
            verify_compatibility_evidence
            and runtime.compatibility_evidence is not None
        ):
            pointer_integrity = payload.get("local_integrity_fingerprint")
            if (
                not isinstance(pointer_integrity, str)
                or not _SHA256_ID_RE.fullmatch(pointer_integrity)
                or not hmac.compare_digest(
                    pointer_integrity,
                    runtime.compatibility_evidence.local_integrity_fingerprint,
                )
            ):
                runtime = replace(runtime, compatibility_evidence=None)
        return runtime if runtime.version == version else None
    except (OSError, ValueError, json.JSONDecodeError, RuntimeUpdateError):
        return None


def _safe_extract(archive: Path, destination: Path) -> None:
    root = destination.resolve(strict=False)
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            if member.isdev() or member.isfifo():
                raise RuntimeUpdateError("source archive contains a device or FIFO")
            target = (root / member.name).resolve(strict=False)
            if not target.is_relative_to(root):
                raise RuntimeUpdateError("source archive contains a path traversal")
            if member.issym():
                link_target = (target.parent / member.linkname).resolve(strict=False)
                if not link_target.is_relative_to(root):
                    raise RuntimeUpdateError("source archive contains an escaping symlink")
            if member.islnk():
                link_target = (root / member.linkname).resolve(strict=False)
                if not link_target.is_relative_to(root):
                    raise RuntimeUpdateError("source archive contains an escaping hard link")
        try:
            bundle.extractall(destination, members=members, filter="data")
        except (tarfile.FilterError, OSError) as exc:
            raise RuntimeUpdateError(
                f"source archive contains an unsafe member: {exc}"
            ) from exc


def _sha256_file(path: Path) -> str:
    """Hash a runtime artifact without copying the entire file into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _version_key(value: str | None) -> tuple[tuple[int, object], ...]:
    if not value:
        return ()
    normalized = value.removeprefix("v").removeprefix(".")
    return tuple(
        (0, int(piece)) if piece.isdigit() else (1, piece.casefold())
        for piece in re.findall(r"\d+|[A-Za-z]+", normalized)
    )


def _clean_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def _python_subprocess_environment(
    python: str | Path,
    *,
    bundled_python_env: str,
) -> dict[str, str]:
    """Isolate Python layers while retaining the packaged stdlib location.

    The relocatable image-layer interpreter uses the same bundled CPython
    runtime as the service and needs the bootstrap-provided ``PYTHONHOME`` to
    locate that stdlib. Development virtualenvs and arbitrary configured
    interpreters must not inherit it.
    """

    environment = _clean_subprocess_environment()
    bundled_python = os.environ.get(bundled_python_env, "").strip()
    python_home = os.environ.get("PYTHONHOME", "").strip()
    if bundled_python and python_home:
        selected = Path(python).expanduser().resolve(strict=False)
        bundled = Path(bundled_python).expanduser().resolve(strict=False)
        if selected == bundled:
            environment["PYTHONHOME"] = python_home
    return environment


async def _run_version_command(
    *argv: str,
    timeout: float = 5.0,
    env: Mapping[str, str] | None = None,
) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=dict(env) if env is not None else _clean_subprocess_environment(),
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await _terminate_process_group(process)
        raise
    except asyncio.TimeoutError:
        await _terminate_process_group(process)
        return None
    text = output.decode("utf-8", errors="replace").strip()
    return text if process.returncode == 0 and text else None


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=5)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)


async def _run_checked(
    *argv: str,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 3600,
) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else _clean_subprocess_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise RuntimeUpdateError(f"could not start {argv[0]}: {exc}") from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await _terminate_process_group(process)
        raise
    except asyncio.TimeoutError:
        await _terminate_process_group(process)
        raise RuntimeUpdateError(f"command timed out: {' '.join(argv[:3])}")
    text = output.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeUpdateError(
            f"command failed ({process.returncode}): {' '.join(argv[:3])}\n{text[-4000:]}"
        )
    return text


class RuntimeUpdateManager:
    """Check and install engine updates directly from official upstreams."""

    def __init__(
        self,
        *,
        omlx: OMLXConfig,
        mflux: MFluxConfig,
        ds4: DS4Config,
        llama_cpp: LlamaCppConfig | None = None,
        root: str | Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.llama_cpp = llama_cpp or LlamaCppConfig()
        self.omlx = omlx
        self.mflux = mflux
        self.ds4 = ds4
        self.root = _runtime_root(root)
        self.channel = "official"
        self._client = client or httpx.AsyncClient(timeout=30, follow_redirects=True)
        self._owns_client = client is None
        self._releases: dict[str, RuntimeRelease] = {}
        self._ds4_glm53_preview_release: RuntimeRelease | None = None
        self._upstream_omlx: tuple[str | None, str | None, str | None] = (
            None,
            None,
            None,
        )
        self._diagnostics: dict[str, str] = {}
        self._last_checked_at: float | None = None
        self._check_lock = asyncio.Lock()
        self._install_lock = asyncio.Lock()
        self._lifecycle_lock = threading.RLock()
        self._service_instance_id = str(uuid4())

    @property
    def _lifecycle_path(self) -> Path:
        return self.root / "lifecycle.json"

    def lifecycle_evidence(self) -> dict[str, Any]:
        """Return bounded, credential-free runtime transition evidence."""

        with self._lifecycle_lock:
            try:
                payload = _read_json(self._lifecycle_path)
            except FileNotFoundError:
                return {
                    "schema_version": 1,
                    "valid": True,
                    "dropped_events": 0,
                    "events": [],
                }
            except (OSError, ValueError, json.JSONDecodeError):
                return {
                    "schema_version": 1,
                    "valid": False,
                    "dropped_events": 0,
                    "events": [],
                    "diagnostic": "runtime lifecycle journal is unreadable",
                }
            dropped_events = (
                payload.get("dropped_events") if isinstance(payload, dict) else None
            )
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or not isinstance(dropped_events, int)
                or isinstance(dropped_events, bool)
                or dropped_events < 0
                or not isinstance(payload.get("events"), list)
            ):
                return {
                    "schema_version": 1,
                    "valid": False,
                    "dropped_events": 0,
                    "events": [],
                    "diagnostic": "runtime lifecycle journal has an unsupported schema",
                }
            events = payload["events"]
            previous_sequence = 0
            valid = len(events) <= _LIFECYCLE_LIMIT
            for event in events:
                if not isinstance(event, dict):
                    valid = False
                    break
                sequence = event.get("sequence")
                event_id = event.get("event_id")
                service_instance_id = event.get("service_instance_id")
                engine = event.get("engine")
                action = event.get("action")
                outcome = event.get("outcome")
                created_at = event.get("created_at")
                failure_code = event.get("failure_code")
                if (
                    not isinstance(sequence, int)
                    or isinstance(sequence, bool)
                    or sequence <= previous_sequence
                    or not isinstance(event_id, str)
                    or not _LIFECYCLE_EVENT_ID_RE.fullmatch(event_id)
                    or not isinstance(service_instance_id, str)
                    or not _LIFECYCLE_EVENT_ID_RE.fullmatch(service_instance_id)
                    or engine not in _ENGINES
                    or not isinstance(action, str)
                    or not _LIFECYCLE_ACTION_RE.fullmatch(action)
                    or outcome not in {"started", "succeeded", "failed"}
                    or not isinstance(created_at, str)
                    or (
                        failure_code is not None
                        and failure_code not in _LIFECYCLE_FAILURE_CODES
                    )
                ):
                    valid = False
                    break
                try:
                    datetime.fromisoformat(created_at)
                except ValueError:
                    valid = False
                    break
                for key in (
                    "requested_version",
                    "prepared_version",
                    "active_version_before",
                    "active_version_after",
                ):
                    value = event.get(key)
                    if value is not None and (
                        not isinstance(value, str)
                        or not _VERSION_RE.fullmatch(value)
                    ):
                        valid = False
                        break
                source_revision = event.get("source_revision")
                if source_revision is not None and (
                    not isinstance(source_revision, str)
                    or not _SOURCE_REVISION_RE.fullmatch(source_revision)
                ):
                    valid = False
                if not valid:
                    break
                previous_sequence = sequence
            if not valid:
                return {
                    "schema_version": 1,
                    "valid": False,
                    "dropped_events": int(dropped_events),
                    "events": [],
                    "diagnostic": "runtime lifecycle journal contains an invalid event",
                }
            return {
                "schema_version": 1,
                "valid": True,
                "dropped_events": int(dropped_events),
                "events": [dict(event) for event in events],
            }

    def record_lifecycle(
        self,
        *,
        engine: str,
        action: str,
        outcome: str,
        requested_version: str | None = None,
        prepared_version: str | None = None,
        active_version_before: str | None = None,
        active_version_after: str | None = None,
        source_revision: str | None = None,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        """Append one bounded event without persisting arbitrary diagnostics."""

        normalized = _validate_engine(engine)
        if not _LIFECYCLE_ACTION_RE.fullmatch(action):
            raise ValueError("runtime lifecycle action is invalid")
        if outcome not in {"started", "succeeded", "failed"}:
            raise ValueError("runtime lifecycle outcome is invalid")

        def safe_version(value: str | None) -> str | None:
            return value if value is not None and _VERSION_RE.fullmatch(value) else None

        safe_revision = (
            source_revision
            if isinstance(source_revision, str)
            and _SOURCE_REVISION_RE.fullmatch(source_revision)
            else None
        )

        with self._lifecycle_lock:
            existing = self.lifecycle_evidence()
            events = (
                [dict(event) for event in existing["events"]]
                if existing["valid"]
                else []
            )
            dropped = (
                int(existing["dropped_events"])
                if existing["valid"]
                else 0
            )
            next_sequence = (
                int(events[-1]["sequence"]) + 1 if events else 1
            )
            if not existing["valid"]:
                events.append(
                    {
                        "sequence": next_sequence,
                        "event_id": str(uuid4()),
                        "service_instance_id": self._service_instance_id,
                        "engine": normalized,
                        "action": "journal_reset",
                        "outcome": "failed",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "failure_code": "invalid_journal",
                    }
                )
                next_sequence += 1
            event = {
                "sequence": next_sequence,
                "event_id": str(uuid4()),
                "service_instance_id": self._service_instance_id,
                "engine": normalized,
                "action": action,
                "outcome": outcome,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "requested_version": safe_version(requested_version),
                "prepared_version": safe_version(prepared_version),
                "active_version_before": safe_version(active_version_before),
                "active_version_after": safe_version(active_version_after),
                "source_revision": safe_revision,
                "failure_code": (
                    _runtime_failure_code(error) if error is not None else None
                ),
            }
            events.append(event)
            if len(events) > _LIFECYCLE_LIMIT:
                overflow = len(events) - _LIFECYCLE_LIMIT
                events = events[overflow:]
                dropped += overflow
            _atomic_json(
                self._lifecycle_path,
                {
                    "schema_version": 1,
                    "dropped_events": dropped,
                    "events": events,
                },
            )
            return dict(event)

    def active_version(self, engine: str) -> str | None:
        runtime = resolve_active_runtime(engine, root=self.root)
        return runtime.version if runtime is not None else None

    def runtime_compatibility_evidence(
        self,
        engine: str,
    ) -> RuntimeCompatibilityEvidence | None:
        """Return only validated managed provenance, never local path identity."""

        normalized = engine.casefold()
        if normalized not in {"llama.cpp", "ds4"}:
            return None
        runtime = resolve_active_runtime(
            normalized,
            root=self.root,
            verify_compatibility_evidence=True,
        )
        return runtime.compatibility_evidence if runtime is not None else None

    def record_validation(self, engine: str) -> dict[str, Any] | None:
        normalized = _validate_engine(engine)
        if normalized == "omlx":
            return None
        active = resolve_active_runtime(normalized, root=self.root)
        if active is None:
            return None
        return self.record_lifecycle(
            engine=normalized,
            action="inference_validated",
            outcome="succeeded",
            prepared_version=active.version,
            active_version_before=active.version,
            active_version_after=active.version,
            source_revision=active.source_revision,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _fetch_json(self, url: str) -> object:
        response = await self._client.get(
            url,
            headers={"Accept": "application/json", "User-Agent": "Unified-Inference/1"},
        )
        response.raise_for_status()
        return response.json()

    async def _official_omlx(
        self,
    ) -> tuple[str | None, str | None, str | None]:
        payload = await self._fetch_json(OMLX_RELEASES_API_URL)
        if not isinstance(payload, list):
            raise RuntimeUpdateError("official oMLX releases response was not a list")
        stable = [
            item
            for item in payload
            if isinstance(item, dict)
            and not item.get("draft")
            and not item.get("prerelease")
            and isinstance(item.get("tag_name"), str)
        ]
        if not stable:
            return None, _OMLX_RELEASES_URL, None
        item = max(stable, key=lambda value: _version_key(str(value["tag_name"])))
        version = str(item["tag_name"]).removeprefix("v").removeprefix(".")
        return (
            version,
            str(item.get("html_url") or _OMLX_RELEASES_URL),
            _official_omlx_installer_url(item),
        )

    async def _official_llama_cpp(self) -> RuntimeRelease:
        payload = await self._fetch_json(LLAMA_CPP_RELEASES_API_URL)
        if not isinstance(payload, list):
            raise RuntimeUpdateError("official llama.cpp releases response was invalid")

        candidates: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
        for release in payload:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            tag = release.get("tag_name")
            match = (
                _LLAMA_CPP_BUILD_TAG_RE.fullmatch(tag)
                if isinstance(tag, str)
                else None
            )
            if match is None:
                continue
            assets = release.get("assets")
            if not isinstance(assets, list):
                continue
            expected_name = f"llama-{tag}-bin-macos-arm64.tar.gz"
            asset = next(
                (
                    value
                    for value in assets
                    if isinstance(value, dict) and value.get("name") == expected_name
                ),
                None,
            )
            # GitHub publishes the release object while its platform assets are
            # still uploading. Select the newest complete Apple Silicon build
            # instead of temporarily making runtime updates unavailable.
            if asset is not None:
                candidates.append((int(match.group(1)), release, asset))

        if not candidates:
            raise RuntimeUpdateError(
                "official llama.cpp releases omitted a complete macOS ARM64 build"
            )

        _build, release, asset = max(candidates, key=lambda item: item[0])
        tag = _validate_version(release.get("tag_name"))
        release_url = str(
            release.get("html_url")
            or f"https://github.com/ggml-org/llama.cpp/releases/tag/{tag}"
        )
        expected_name = f"llama-{tag}-bin-macos-arm64.tar.gz"
        source_url = asset.get("browser_download_url")
        digest = asset.get("digest")
        size = asset.get("size")
        expected_source_url = (
            f"{LLAMA_CPP_RELEASE_DOWNLOAD_PREFIX}{tag}/{expected_name}"
        )
        if source_url != expected_source_url:
            raise RuntimeUpdateError("official llama.cpp asset URL was invalid")
        if not isinstance(digest, str) or not re.fullmatch(
            r"sha256:[0-9a-fA-F]{64}", digest
        ):
            raise RuntimeUpdateError(
                "official llama.cpp asset did not publish a SHA-256 digest"
            )
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise RuntimeUpdateError("official llama.cpp asset size was invalid")
        revision = release.get("target_commitish")
        return RuntimeRelease(
            engine="llama.cpp",
            version=tag,
            source_revision=str(revision) if isinstance(revision, str) else None,
            source_url=source_url,
            release_notes_url=release_url,
            sha256=digest.split(":", 1)[1].casefold(),
            asset_size=size,
        )

    async def _official_mflux(self) -> RuntimeRelease:
        payload = await self._fetch_json(MFLUX_PYPI_JSON_URL)
        if not isinstance(payload, dict) or not isinstance(payload.get("info"), dict):
            raise RuntimeUpdateError("official MFLUX PyPI response was invalid")
        version = _validate_version(payload["info"].get("version"))
        return RuntimeRelease(
            engine="mflux",
            version=version,
            source_revision=f"pypi:mflux=={version}",
            source_url=f"https://pypi.org/project/mflux/{version}/",
            release_notes_url=f"https://pypi.org/project/mflux/{version}/",
        )

    async def _official_ds4(
        self,
        *,
        branch: str = "main",
        channel: str = "official",
    ) -> RuntimeRelease:
        if (branch, channel) == ("main", "official"):
            commit_api_url = DS4_COMMIT_API_URL
        elif (branch, channel) == (
            DS4_GLM53_PREVIEW_BRANCH,
            DS4_GLM53_PREVIEW_CHANNEL,
        ):
            commit_api_url = DS4_GLM53_PREVIEW_COMMIT_API_URL
        else:
            raise RuntimeUpdateError("unsupported official DS4 runtime channel")
        payload = await self._fetch_json(commit_api_url)
        if not isinstance(payload, dict) or not isinstance(payload.get("sha"), str):
            raise RuntimeUpdateError("official DS4 commit response was invalid")
        revision = str(payload["sha"]).casefold()
        if not _GIT_COMMIT_RE.fullmatch(revision):
            raise RuntimeUpdateError("official DS4 commit SHA was invalid")
        version = revision[:12].casefold()
        return RuntimeRelease(
            engine="ds4",
            version=version,
            source_revision=revision,
            source_url=f"https://codeload.github.com/antirez/ds4/tar.gz/{revision}",
            release_notes_url=f"https://github.com/antirez/ds4/commit/{revision}",
            channel=channel,
            source_branch=branch,
        )

    async def _refresh_releases(self) -> None:
        results = await asyncio.gather(
            self._official_llama_cpp(),
            self._official_omlx(),
            self._official_ds4(),
            self._official_mflux(),
            self._official_ds4(
                branch=DS4_GLM53_PREVIEW_BRANCH,
                channel=DS4_GLM53_PREVIEW_CHANNEL,
            ),
            return_exceptions=True,
        )
        self._diagnostics.clear()
        self._releases.clear()
        self._ds4_glm53_preview_release = None
        for engine, result in zip(_ENGINES, results[: len(_ENGINES)], strict=True):
            if isinstance(result, BaseException):
                self._diagnostics[engine] = f"official upstream check failed: {result}"
            elif engine == "omlx":
                self._upstream_omlx = result
            else:
                assert isinstance(result, RuntimeRelease)
                self._releases[engine] = result
        preview_result = results[-1]
        if isinstance(preview_result, BaseException):
            self._diagnostics[DS4_GLM53_PREVIEW_CHANNEL] = (
                f"official preview check failed: {preview_result}"
            )
        else:
            assert isinstance(preview_result, RuntimeRelease)
            self._ds4_glm53_preview_release = preview_result
        self._last_checked_at = time.time()

    async def _installed_omlx(self) -> tuple[str | None, str | None, str | None]:
        app_candidates = (
            Path("/Applications/oMLX.app/Contents/Info.plist"),
            Path.home() / "Applications" / "oMLX.app" / "Contents" / "Info.plist",
        )
        app_install: tuple[str, None, str] | None = None
        for candidate in app_candidates:
            try:
                with candidate.open("rb") as stream:
                    value = plistlib.load(stream).get("CFBundleShortVersionString")
                if isinstance(value, str) and value:
                    app_install = (value, None, str(candidate.parents[1]))
                    break
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
        headers: dict[str, str] = {}
        token = os.environ.get(self.omlx.api_key_env, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # The packaged app has historically exposed its version from /health,
        # while the official pip/wheel server reports it from /api/status.
        # Both are read-only vendor endpoints and keep runtime discovery
        # external to Unified Inference's own release cadence.
        running_version: str | None = None
        for endpoint in ("/health", "/api/status"):
            try:
                response = await self._client.get(
                    f"{self.omlx.base_url.rstrip('/')}{endpoint}",
                    headers=headers,
                    timeout=min(3.0, self.omlx.request_timeout_seconds),
                )
                if response.status_code != 200:
                    continue
                payload = response.json()
                if isinstance(payload, dict):
                    for key in ("version", "app_version", "server_version"):
                        value = payload.get(key)
                        if isinstance(value, str) and value:
                            running_version = value.removeprefix("v")
                            break
                    if running_version is not None:
                        break
            except (httpx.HTTPError, ValueError):
                continue
        cli_install: tuple[str, None, str] | None = None
        for executable in _omlx_cli_candidates():
            if not executable.is_file() or not os.access(executable, os.X_OK):
                continue
            output = await _run_version_command(str(executable), "--version")
            match = re.search(r"\d+(?:\.\d+)+(?:[-+._A-Za-z0-9]*)?", output or "")
            if match:
                cli_install = (match.group(0), None, str(executable))
                break
        if running_version is not None:
            if app_install is not None and _version_key(app_install[0]) == _version_key(
                running_version
            ):
                return app_install
            if cli_install is not None and _version_key(cli_install[0]) == _version_key(
                running_version
            ):
                return running_version, None, cli_install[2]
            return running_version, None, self.omlx.base_url
        if app_install is not None:
            return app_install
        if cli_install is not None:
            return cli_install
        return None, None, None

    async def _installed_llama_cpp(
        self,
        managed: ActiveRuntime | None,
    ) -> tuple[str | None, str | None, str | None]:
        if managed is not None:
            return managed.version, managed.source_revision, str(managed.root)
        binary = Path(self.llama_cpp.binary).expanduser()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return None, None, str(binary)
        output = await _run_version_command(str(binary), "--version")
        if not output:
            return None, None, str(binary)
        build = re.search(r"\bversion:\s*(\d+)\b", output, re.IGNORECASE)
        revision = re.search(r"\(([0-9a-fA-F]{7,40})\)", output)
        return (
            f"b{build.group(1)}" if build else None,
            revision.group(1).casefold() if revision else None,
            str(binary),
        )

    def _mflux_python(self) -> Path | None:
        configured = self.mflux.python or os.environ.get(
            self.mflux.python_env, ""
        ).strip()
        return Path(configured).expanduser() if configured else None

    def _mflux_environment(self, python: Path) -> dict[str, str]:
        return _python_subprocess_environment(
            python,
            bundled_python_env=self.mflux.python_env,
        )

    def _mflux_worker_source(self) -> Path | None:
        value = os.environ.get(self.mflux.source_path_env, "").strip()
        source = Path(value).expanduser() if value else None
        if source and (source / "mnemosyne_mflux_worker").is_dir():
            return source
        return None

    async def _installed_mflux(
        self,
        managed: ActiveRuntime | None,
    ) -> tuple[str | None, str | None, str | None]:
        if managed is not None:
            return managed.version, managed.source_revision, str(managed.root)
        python = self._mflux_python()
        if python is None:
            return None, None, None
        script = (
            "import json; from importlib.metadata import distribution; "
            "d=distribution('mflux'); u=json.loads(d.read_text('direct_url.json') or '{}'); "
            "r=u.get('vcs_info',{}).get('commit_id'); "
            "print(json.dumps({'version':d.version,'revision':r}))"
        )
        output = await _run_version_command(
            str(python),
            "-c",
            script,
            env=self._mflux_environment(python),
        )
        if not output:
            return None, None, str(python)
        try:
            payload = json.loads(output.splitlines()[-1])
            revision = payload.get("revision")
            return (
                str(payload["version"]),
                str(revision) if revision else None,
                str(python),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None, None, str(python)

    async def _installed_ds4(
        self,
        managed: ActiveRuntime | None,
    ) -> tuple[str | None, str | None, str | None]:
        if managed is not None:
            return managed.version, managed.source_revision, str(managed.root)
        binary = Path(self.ds4.binary).expanduser()
        if not binary.is_file():
            return None, None, str(binary)
        working = Path(self.ds4.working_directory).expanduser()
        revision = None
        if (working / ".git").exists():
            revision = await _run_version_command(
                "git", "-C", str(working), "rev-parse", "HEAD"
            )
            if revision:
                revision = revision.splitlines()[-1].strip()
        digest = await asyncio.to_thread(_sha256_file, binary)
        return (revision or digest)[:12], revision, str(binary)

    def _release_is_newer(
        self,
        release: RuntimeRelease,
        installed_version: str | None,
        installed_revision: str | None,
    ) -> bool:
        if installed_version is None:
            return True
        if release.engine == "ds4":
            return installed_revision != release.source_revision
        return _version_key(release.version) > _version_key(installed_version)

    async def installed_status(self) -> dict[str, dict[str, Any]]:
        """Inspect local runtimes without contacting any upstream service."""

        managed_llama, managed_mflux, managed_ds4 = await asyncio.gather(
            asyncio.to_thread(
                resolve_active_runtime,
                "llama.cpp",
                root=self.root,
                verify_compatibility_evidence=True,
            ),
            asyncio.to_thread(
                resolve_active_runtime,
                "mflux",
                root=self.root,
            ),
            asyncio.to_thread(
                resolve_active_runtime,
                "ds4",
                root=self.root,
                verify_compatibility_evidence=True,
            ),
        )
        installed_values = await asyncio.gather(
            self._installed_llama_cpp(managed_llama),
            self._installed_omlx(),
            self._installed_ds4(managed_ds4),
            self._installed_mflux(managed_mflux),
        )
        statuses = {
            engine: {
                "installed": version is not None,
                "version": version,
                "revision": revision,
                "path": path,
                "installation_kind": (
                    _omlx_installation_kind(path)
                    if engine == "omlx"
                    else "managed_or_configured"
                ),
                "compatibility_fingerprint": None,
                "features": [],
            }
            for engine, (version, revision, path) in zip(
                _ENGINES,
                installed_values,
                strict=True,
            )
        }
        statuses["ds4"]["managed_channel"] = (
            managed_ds4.channel if managed_ds4 is not None else None
        )
        for engine, managed in (
            ("llama.cpp", managed_llama),
            ("ds4", managed_ds4),
        ):
            evidence = (
                managed.compatibility_evidence if managed is not None else None
            )
            if evidence is not None:
                statuses[engine]["compatibility_fingerprint"] = (
                    evidence.compatibility_fingerprint
                )
                statuses[engine]["features"] = list(evidence.features)
        return statuses

    async def check(self, *, refresh: bool = True) -> dict[str, Any]:
        async with self._check_lock:
            if refresh or self._last_checked_at is None:
                await self._refresh_releases()
            local_status = await self.installed_status()
            statuses: list[dict[str, Any]] = []
            for engine in _ENGINES:
                installed_version = local_status[engine]["version"]
                installed_revision = local_status[engine]["revision"]
                installed_path = local_status[engine]["path"]
                if engine == "omlx":
                    (
                        upstream_version,
                        upstream_url,
                        official_installer_url,
                    ) = self._upstream_omlx
                    release = None
                    update_available = bool(
                        upstream_version
                        and (
                            installed_version is None
                            or _version_key(upstream_version)
                            > _version_key(installed_version)
                        )
                    )
                else:
                    release = self._releases.get(engine)
                    upstream_version = release.version if release else None
                    upstream_url = release.release_notes_url if release else None
                    official_installer_url = None
                    update_available = bool(
                        release
                        and self._release_is_newer(
                            release, installed_version, installed_revision
                        )
                    )
                statuses.append(
                    {
                        "engine": engine,
                        "release_tier": ENGINE_RELEASE_TIER[
                            EngineName(engine)
                        ],
                        "display_name": {
                            "llama.cpp": "llama.cpp",
                            "omlx": "oMLX",
                            "mflux": "MFLUX",
                            "ds4": "DS4",
                        }[engine],
                        "ownership": (
                            "external"
                            if engine == "omlx"
                            else "managed_or_external"
                        ),
                        "installed": installed_version is not None,
                        "installed_version": installed_version,
                        "installed_revision": installed_revision,
                        "installed_path": installed_path,
                        "installed_channel": local_status[engine].get(
                            "managed_channel"
                        ),
                        "installation_kind": local_status[engine].get(
                            "installation_kind"
                        ),
                        "latest_upstream_version": upstream_version,
                        "latest_upstream_revision": release.source_revision if release else None,
                        "latest_upstream_url": upstream_url,
                        "official_installer_url": official_installer_url,
                        "available_version": release.version if release else None,
                        "available_revision": release.source_revision if release else None,
                        "release_notes_url": upstream_url,
                        "update_available": update_available,
                        "can_install": (
                            engine in {"llama.cpp", "mflux", "ds4"}
                            or (
                                engine == "omlx"
                                and local_status[engine].get("installation_kind")
                                == "homebrew_stable"
                                and self.omlx.enabled
                            )
                        )
                        and update_available,
                        "can_rollback": (
                            self._rollback_version(engine) is not None
                        ),
                        "management_note": (
                            "The newest complete numbered build and Apple Silicon "
                            "binary come directly from official ggml-org/llama.cpp "
                            "GitHub releases."
                            if engine == "llama.cpp"
                            else (
                                "The official oMLX app is recommended and includes precompiled custom kernels. oMLX remains independently owned and updated."
                                if engine == "omlx"
                                else (
                                    "Version and dependencies come directly from the official MFLUX package on PyPI."
                                    if engine == "mflux"
                                    else "Version and source come directly from the official antirez/ds4 repository."
                                )
                            )
                        ),
                        "diagnostic": (
                            "Enable oMLX before a supervised Homebrew update so Unified Inference can drain and validate its control plane."
                            if engine == "omlx"
                            and update_available
                            and local_status[engine].get("installation_kind")
                            == "homebrew_stable"
                            and not self.omlx.enabled
                            else self._diagnostics.get(engine)
                        ),
                        "upgrade_strategy": (
                            {
                                "official_app": "vendor_app_updater",
                                "official_app_cli": "vendor_app_updater",
                                "homebrew_stable": "supervised_homebrew",
                                "homebrew_head": "migrate_to_stable",
                                "running_external": "external_manual",
                                "external_cli": "external_manual",
                                "not_installed": "official_installer",
                            }.get(
                                str(
                                    local_status[engine].get(
                                        "installation_kind", "not_installed"
                                    )
                                ),
                                "managed",
                            )
                            if engine == "omlx"
                            else "managed"
                        ),
                        "managed_channels": (
                            [
                                {
                                    "channel": DS4_GLM53_PREVIEW_CHANNEL,
                                    "source_branch": DS4_GLM53_PREVIEW_BRANCH,
                                    "release_tier": "experimental",
                                    "available_version": (
                                        self._ds4_glm53_preview_release.version
                                        if self._ds4_glm53_preview_release
                                        else None
                                    ),
                                    "available_revision": (
                                        self._ds4_glm53_preview_release.source_revision
                                        if self._ds4_glm53_preview_release
                                        else None
                                    ),
                                    "release_notes_url": (
                                        self._ds4_glm53_preview_release.release_notes_url
                                        if self._ds4_glm53_preview_release
                                        else None
                                    ),
                                    "update_available": bool(
                                        self._ds4_glm53_preview_release
                                        and (
                                            local_status["ds4"].get(
                                                "managed_channel"
                                            )
                                            != DS4_GLM53_PREVIEW_CHANNEL
                                            or installed_revision
                                            != self._ds4_glm53_preview_release.source_revision
                                        )
                                    ),
                                    "can_install": bool(
                                        self._ds4_glm53_preview_release
                                        and (
                                            local_status["ds4"].get(
                                                "managed_channel"
                                            )
                                            != DS4_GLM53_PREVIEW_CHANNEL
                                            or installed_revision
                                            != self._ds4_glm53_preview_release.source_revision
                                        )
                                    ),
                                    "diagnostic": self._diagnostics.get(
                                        DS4_GLM53_PREVIEW_CHANNEL
                                    ),
                                }
                            ]
                            if engine == "ds4"
                            else []
                        ),
                    }
                )
            return {
                "channel": self.channel,
                "manifest_url": None,
                "source_policy": "official_upstreams",
                "checked_at": self._last_checked_at,
                "core_protocol": CORE_RUNTIME_PROTOCOL,
                "engines": statuses,
            }

    async def upgrade_omlx_homebrew(
        self,
        version: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Delegate one explicit stable update to the exact Homebrew owner."""

        async with self._install_lock:
            if self._last_checked_at is None:
                await self._refresh_releases()
            latest = self._upstream_omlx[0]
            requested = version or latest
            if not requested or not latest or requested != latest:
                raise RuntimeUpdateError(
                    "oMLX update must target the current official stable release"
                )
            installed = await self.installed_status()
            state = installed["omlx"]
            kind = state.get("installation_kind")
            if kind == "homebrew_head":
                raise RuntimeUpdateError(
                    "Homebrew HEAD cannot be upgraded reproducibly; migrate to the official oMLX app or stable formula"
                )
            if kind != "homebrew_stable":
                raise RuntimeUpdateError(
                    "supervised oMLX upgrades require a stable Homebrew installation"
                )
            brew = _homebrew_executable()
            if brew is None:
                raise RuntimeUpdateError("the owning Homebrew executable is unavailable")
            cli_path = state.get("path")
            if not isinstance(cli_path, str):
                raise RuntimeUpdateError("the installed oMLX CLI path is unavailable")
            cli = Path(cli_path).expanduser()
            if not cli.is_file() or not os.access(cli, os.X_OK):
                raise RuntimeUpdateError("the installed oMLX CLI is not executable")

            stop_attempted = False
            failure: BaseException | None = None
            try:
                # A failed/timeout response has an ambiguous process outcome,
                # so always attempt the bounded owner restart after invoking
                # stop rather than assuming the service stayed up.
                stop_attempted = True
                await _run_checked(str(cli), "stop", timeout=120)
                await _run_checked(str(brew), "update", timeout=900)
                await _run_checked(str(brew), "upgrade", "omlx", timeout=1800)
            except BaseException as exc:
                failure = exc
            finally:
                if stop_attempted:
                    refreshed_cli = next(
                        (
                            candidate
                            for candidate in _omlx_cli_candidates()
                            if candidate.is_file() and os.access(candidate, os.X_OK)
                        ),
                        cli,
                    )
                    try:
                        await _run_checked(str(refreshed_cli), "start", timeout=120)
                    except BaseException as restart_error:
                        if failure is None:
                            failure = restart_error
            if failure is not None:
                raise failure

            deadline = time.monotonic() + 90
            observed_version: str | None = None
            observed_path: str | None = None
            while time.monotonic() < deadline:
                observed_version, _revision, observed_path = await self._installed_omlx()
                if (
                    observed_version is not None
                    and _version_key(observed_version) >= _version_key(requested)
                ):
                    return observed_version, observed_path
                await asyncio.sleep(1)
            raise RuntimeUpdateError(
                "updated oMLX did not return with the requested stable version"
            )

    async def _download_source(self, url: str, destination: Path) -> None:
        downloaded = 0
        async with self._client.stream(
            "GET",
            url,
            headers={"User-Agent": "Unified-Inference/1"},
            follow_redirects=True,
            timeout=None,
        ) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_SOURCE_ARCHIVE_BYTES:
                raise RuntimeUpdateError("runtime archive exceeds the size limit")
            with destination.open("wb") as stream:
                async for chunk in response.aiter_bytes():
                    downloaded += len(chunk)
                    if downloaded > MAX_SOURCE_ARCHIVE_BYTES:
                        raise RuntimeUpdateError("runtime archive exceeds the size limit")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())

    def _capabilities(self, source: Path) -> list[dict[str, Any]]:
        for candidate in (source / "capabilities.json", source.parent / "capabilities.json"):
            try:
                value = _read_json(candidate)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return [dict(item) for item in value]
        return []

    async def _pip_wheel(self, python: Path) -> Path:
        script = (
            "import ensurepip,pathlib; "
            "print(next((pathlib.Path(ensurepip.__file__).parent/'_bundled').glob('pip-*.whl')))"
        )
        output = await _run_version_command(
            str(python),
            "-c",
            script,
            env=self._mflux_environment(python),
        )
        if not output:
            raise RuntimeUpdateError("the MFLUX Python runtime does not include ensurepip")
        wheel = Path(output.splitlines()[-1])
        if not wheel.is_file():
            raise RuntimeUpdateError(f"the bundled pip wheel is missing: {wheel}")
        return wheel

    async def _prepare_mflux(
        self, release: RuntimeRelease, staging: Path
    ) -> ActiveRuntime:
        python = self._mflux_python()
        if python is None or not python.is_file() or not os.access(python, os.X_OK):
            raise RuntimeUpdateError(
                "MFLUX update requires the bundled image Python runtime"
            )
        worker_source = self._mflux_worker_source()
        if worker_source is None:
            raise RuntimeUpdateError("the bundled MFLUX worker source is unavailable")
        site_packages = staging / "site-packages"
        worker = staging / "worker"
        site_packages.mkdir(parents=True)
        worker.mkdir(parents=True)
        await asyncio.to_thread(
            shutil.copytree,
            worker_source / "mnemosyne_mflux_worker",
            worker / "mnemosyne_mflux_worker",
        )
        pip_wheel = await self._pip_wheel(python)
        environment = self._mflux_environment(python)
        environment["PYTHONPATH"] = str(pip_wheel)
        await _run_checked(
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--index-url",
            MFLUX_PYPI_INDEX_URL,
            "--upgrade",
            "--target",
            str(site_packages),
            f"mflux=={release.version}",
            env=environment,
        )
        _atomic_json(
            staging / "runtime.json",
            {
                "schema_version": 1,
                "engine": "mflux",
                "version": release.version,
                "source_revision": release.source_revision,
                "core_protocol": CORE_RUNTIME_PROTOCOL,
                "entrypoint": {
                    "site_packages": "site-packages",
                    "worker_path": "worker",
                },
                "capabilities": self._capabilities(worker_source),
            },
        )
        runtime = _load_runtime_directory(staging, expected_engine="mflux")
        await self._smoke(runtime)
        return runtime

    async def _prepare_ds4(
        self, release: RuntimeRelease, staging: Path, downloads: Path
    ) -> ActiveRuntime:
        archive = downloads / f"{uuid4().hex}.tar.gz"
        extracted = staging / ".source"
        source = staging / "source"
        downloads.mkdir(parents=True, exist_ok=True, mode=0o700)
        extracted.mkdir(parents=True)
        try:
            await self._download_source(release.source_url, archive)
            source_size = archive.stat().st_size
            source_sha256 = "sha256:" + await asyncio.to_thread(
                _sha256_file,
                archive,
            )
            await asyncio.to_thread(_safe_extract, archive, extracted)
            roots = [item for item in extracted.iterdir() if item.is_dir()]
            if len(roots) != 1:
                raise RuntimeUpdateError("official DS4 archive had an unexpected layout")
            if (
                release.channel == DS4_GLM53_PREVIEW_CHANNEL
                and roots[0].name != f"ds4-{release.source_revision}"
            ):
                raise RuntimeUpdateError(
                    "official DS4 preview archive did not match its resolved commit"
                )
            os.replace(roots[0], source)
            shutil.rmtree(extracted)
            await _run_checked("/usr/bin/make", "ds4-server", cwd=source)
            binary = source / "ds4-server"
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise RuntimeUpdateError("official DS4 build did not produce ds4-server")
            await _run_checked(str(binary), "--help", cwd=source, timeout=30)
            capabilities: tuple[Mapping[str, Any], ...] = ()
            source_contract_sha256: str | None = None
            if release.channel == DS4_GLM53_PREVIEW_CHANNEL:
                if (
                    release.source_branch != DS4_GLM53_PREVIEW_BRANCH
                    or not isinstance(release.source_revision, str)
                    or not _GIT_COMMIT_RE.fullmatch(release.source_revision)
                ):
                    raise RuntimeUpdateError(
                        "DS4 GLM 5.3 preview release provenance is invalid"
                    )
                capabilities, source_contract_sha256 = (
                    _ds4_glm53_source_contract(source)
                )
            elif release.channel != "official":
                raise RuntimeUpdateError(
                    f"unsupported managed DS4 runtime channel '{release.channel}'"
                )
            compatibility_evidence = await asyncio.to_thread(
                _new_runtime_compatibility_evidence,
                release,
                source_sha256=source_sha256,
                source_size_bytes=source_size,
                executable=binary,
            )
            _atomic_json(
                staging / "runtime.json",
                {
                    "schema_version": 1,
                    "engine": "ds4",
                    "version": release.version,
                    "source_revision": release.source_revision,
                    "channel": release.channel,
                    "source_branch": release.source_branch,
                    "source_contract_sha256": source_contract_sha256,
                    "source_sha256": source_sha256,
                    "source_size_bytes": source_size,
                    "core_protocol": CORE_RUNTIME_PROTOCOL,
                    "entrypoint": {
                        "binary": "source/ds4-server",
                        "working_directory": "source",
                    },
                    "capabilities": list(capabilities),
                    "compatibility_evidence": compatibility_evidence.to_manifest(),
                },
            )
            return await asyncio.to_thread(
                _load_runtime_directory,
                staging,
                expected_engine="ds4",
                verify_compatibility_evidence=True,
            )
        finally:
            with contextlib.suppress(FileNotFoundError):
                archive.unlink()

    async def _prepare_llama_cpp(
        self, release: RuntimeRelease, staging: Path, downloads: Path
    ) -> ActiveRuntime:
        if release.sha256 is None or release.asset_size is None:
            raise RuntimeUpdateError("llama.cpp release integrity metadata is missing")
        archive = downloads / f"{uuid4().hex}.tar.gz"
        extracted = staging / ".archive"
        downloads.mkdir(parents=True, exist_ok=True, mode=0o700)
        extracted.mkdir(parents=True)
        try:
            await self._download_source(release.source_url, archive)
            stat_size = archive.stat().st_size
            if stat_size != release.asset_size:
                raise RuntimeUpdateError(
                    f"llama.cpp archive size mismatch: expected {release.asset_size}, got {stat_size}"
                )
            digest = await asyncio.to_thread(_sha256_file, archive)
            if not hmac.compare_digest(digest.casefold(), release.sha256.casefold()):
                raise RuntimeUpdateError("llama.cpp archive SHA-256 verification failed")
            await asyncio.to_thread(_safe_extract, archive, extracted)
            roots = [item for item in extracted.iterdir() if item.is_dir()]
            if len(roots) != 1:
                raise RuntimeUpdateError("official llama.cpp archive had an unexpected layout")
            runtime_files = staging / "runtime"
            os.replace(roots[0], runtime_files)
            shutil.rmtree(extracted)
            binaries = [
                item
                for item in runtime_files.rglob("llama-server")
                if item.is_file()
            ]
            if len(binaries) != 1:
                raise RuntimeUpdateError(
                    "official llama.cpp archive did not contain exactly one llama-server"
                )
            binary = binaries[0]
            binary.chmod(binary.stat().st_mode | 0o111)
            await _run_checked(str(binary), "--version", cwd=binary.parent, timeout=30)
            help_text = await _run_checked(
                str(binary), "--help", cwd=binary.parent, timeout=30
            )
            required_flags = (
                "--model",
                "--alias",
                "--host",
                "--port",
                "--mmproj",
                "--embedding",
                "--reranking",
                "--no-webui",
                "--flash-attn",
            )
            missing = [flag for flag in required_flags if flag not in help_text]
            if missing:
                raise RuntimeUpdateError(
                    "llama.cpp server contract changed; missing flags: "
                    + ", ".join(missing)
                )
            compatibility_evidence = await asyncio.to_thread(
                _new_runtime_compatibility_evidence,
                release,
                source_sha256=f"sha256:{digest.casefold()}",
                source_size_bytes=stat_size,
                executable=binary,
            )
            _atomic_json(
                staging / "runtime.json",
                {
                    "schema_version": 1,
                    "engine": "llama.cpp",
                    "version": release.version,
                    "source_revision": release.source_revision,
                    "source_sha256": release.sha256,
                    "source_size_bytes": stat_size,
                    "core_protocol": CORE_RUNTIME_PROTOCOL,
                    "entrypoint": {
                        "binary": str(binary.relative_to(staging)),
                        "working_directory": str(binary.parent.relative_to(staging)),
                    },
                    "compatibility_evidence": compatibility_evidence.to_manifest(),
                },
            )
            return await asyncio.to_thread(
                _load_runtime_directory,
                staging,
                expected_engine="llama.cpp",
                verify_compatibility_evidence=True,
            )
        finally:
            with contextlib.suppress(FileNotFoundError):
                archive.unlink()

    async def _smoke(self, runtime: ActiveRuntime) -> None:
        if runtime.engine != "mflux":
            return
        python = self._mflux_python()
        if python is None:
            raise RuntimeUpdateError("MFLUX Python became unavailable")
        paths = [runtime.path("worker_path")]
        if "site_packages" in runtime.entrypoint:
            paths.insert(0, runtime.path("site_packages"))
        environment = self._mflux_environment(python)
        environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths)
        await _run_checked(
            str(python),
            "-c",
            "import mflux; import mnemosyne_mflux_worker",
            env=environment,
            timeout=120,
        )

    async def prepare(
        self,
        engine: str,
        version: str | None = None,
        *,
        channel: str | None = None,
    ) -> PreparedRuntime:
        normalized = _validate_engine(engine)
        if normalized == "omlx":
            raise RuntimeUpdateError("oMLX must be updated through its official updater")
        requested_channel = channel or "official"
        if normalized != "ds4" and requested_channel != "official":
            raise RuntimeUpdateError(
                f"runtime channel '{requested_channel}' is not available for {normalized}"
            )
        if normalized == "ds4" and requested_channel not in {
            "official",
            DS4_GLM53_PREVIEW_CHANNEL,
        }:
            raise RuntimeUpdateError(
                f"unsupported official DS4 runtime channel '{requested_channel}'"
            )
        async with self._install_lock:
            if (
                normalized not in self._releases
                or (
                    requested_channel == DS4_GLM53_PREVIEW_CHANNEL
                    and self._ds4_glm53_preview_release is None
                )
            ):
                await self._refresh_releases()
            release = (
                self._ds4_glm53_preview_release
                if normalized == "ds4"
                and requested_channel == DS4_GLM53_PREVIEW_CHANNEL
                else self._releases.get(normalized)
            )
            if release is None or not release.compatible:
                detail = self._diagnostics.get(
                    requested_channel
                    if requested_channel == DS4_GLM53_PREVIEW_CHANNEL
                    else normalized,
                    "official release unavailable",
                )
                raise RuntimeUpdateError(
                    f"no compatible official {normalized} {requested_channel} update: {detail}"
                )
            if release.channel != requested_channel:
                raise RuntimeUpdateError(
                    "resolved runtime release did not match the requested channel"
                )
            if version is not None and version != release.version:
                raise RuntimeUpdateError(
                    f"{version} is not the current official {normalized} version"
                )
            engine_root = self.root / normalized
            final = engine_root / release.version
            if final.exists():
                runtime = await asyncio.to_thread(
                    _load_runtime_directory,
                    final,
                    expected_engine=normalized,
                    verify_compatibility_evidence=(
                        normalized in {"llama.cpp", "ds4"}
                    ),
                )
                if (
                    runtime.source_revision != release.source_revision
                    or runtime.channel != release.channel
                    or (
                        release.channel == DS4_GLM53_PREVIEW_CHANNEL
                        and runtime.source_branch != release.source_branch
                    )
                ):
                    raise RuntimeUpdateError(
                        "existing runtime folder does not match the official source provenance"
                    )
                if (
                    normalized in {"llama.cpp", "ds4"}
                    and runtime.compatibility_evidence is None
                ):
                    raise RuntimeUpdateError(
                        "existing runtime folder lacks verified compatibility evidence"
                    )
                return PreparedRuntime(release=release, runtime=runtime)

            engine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            staging = engine_root / ".staging" / uuid4().hex
            staging.mkdir(parents=True, mode=0o700)
            try:
                if normalized == "mflux":
                    runtime = await self._prepare_mflux(release, staging)
                elif normalized == "llama.cpp":
                    runtime = await self._prepare_llama_cpp(
                        release, staging, engine_root / ".downloads"
                    )
                else:
                    runtime = await self._prepare_ds4(
                        release, staging, engine_root / ".downloads"
                    )
                os.replace(staging, final)
                return PreparedRuntime(
                    release=release,
                    runtime=await asyncio.to_thread(
                        _load_runtime_directory,
                        final,
                        expected_engine=normalized,
                        verify_compatibility_evidence=(
                            normalized in {"llama.cpp", "ds4"}
                        ),
                    ),
                )
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def activate(self, prepared: PreparedRuntime) -> ActiveRuntime:
        engine = _validate_engine(prepared.release.engine)
        managed_compatibility = engine in {"llama.cpp", "ds4"}
        runtime = _load_runtime_directory(
            prepared.runtime.root,
            expected_engine=engine,
            verify_compatibility_evidence=managed_compatibility,
        )
        if managed_compatibility:
            prepared_evidence = prepared.runtime.compatibility_evidence
            runtime_evidence = runtime.compatibility_evidence
            if (
                prepared_evidence is None
                or runtime_evidence is None
                or not hmac.compare_digest(
                    prepared_evidence.local_integrity_fingerprint,
                    runtime_evidence.local_integrity_fingerprint,
                )
            ):
                raise RuntimeUpdateError(
                    "prepared runtime local integrity changed before activation"
                )
        current = resolve_active_runtime(engine, root=self.root)
        try:
            current_pointer = _read_json(_active_pointer(self.root, engine))
        except (OSError, ValueError, json.JSONDecodeError):
            current_pointer = None
        current_integrity = (
            _pointer_integrity_fingerprint(
                current_pointer,
                "local_integrity_fingerprint",
            )
            if current is not None
            and isinstance(current_pointer, Mapping)
            and current_pointer.get("version") == current.version
            else None
        )
        pointer_value: dict[str, Any] = {
            "schema_version": 1,
            "version": runtime.version,
            "previous_version": current.version if current else None,
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }
        if runtime.compatibility_evidence is not None:
            pointer_value["local_integrity_fingerprint"] = (
                runtime.compatibility_evidence.local_integrity_fingerprint
            )
        if current is not None and current_integrity is not None:
            pointer_value["previous_local_integrity_fingerprint"] = (
                current_integrity
            )
        _atomic_json(
            _active_pointer(self.root, engine),
            pointer_value,
        )
        return runtime

    def _rollback_pointer(self, engine: str) -> tuple[str, str | None] | None:
        if engine == "omlx":
            return None
        pointer = _active_pointer(self.root, engine)
        try:
            payload = _read_json(pointer)
            if not isinstance(payload, dict):
                return None
            version = payload.get("previous_version")
            if not isinstance(version, str):
                return None
            _load_runtime_directory(
                self.root / engine / version,
                expected_engine=engine,
                verify_compatibility_evidence=False,
            )
            if (
                "previous_local_integrity_fingerprint" in payload
                and _pointer_integrity_fingerprint(
                    payload,
                    "previous_local_integrity_fingerprint",
                )
                is None
            ):
                return None
            return (
                version,
                _pointer_integrity_fingerprint(
                    payload,
                    "previous_local_integrity_fingerprint",
                ),
            )
        except (OSError, ValueError, json.JSONDecodeError, RuntimeUpdateError):
            return None

    def _rollback_version(self, engine: str) -> str | None:
        target = self._rollback_pointer(engine)
        return target[0] if target is not None else None

    def rollback(self, engine: str) -> ActiveRuntime:
        normalized = _validate_engine(engine)
        target = self._rollback_pointer(normalized)
        if target is None:
            raise RuntimeUpdateError(f"no previous {normalized} runtime is available")
        version, expected_integrity = target
        current = resolve_active_runtime(normalized, root=self.root)
        try:
            current_pointer = _read_json(_active_pointer(self.root, normalized))
        except (OSError, ValueError, json.JSONDecodeError):
            current_pointer = None
        current_integrity = (
            _pointer_integrity_fingerprint(
                current_pointer,
                "local_integrity_fingerprint",
            )
            if current is not None
            and isinstance(current_pointer, Mapping)
            and current_pointer.get("version") == current.version
            else None
        )
        managed_compatibility = normalized in {"llama.cpp", "ds4"}
        runtime = _load_runtime_directory(
            self.root / normalized / version,
            expected_engine=normalized,
            verify_compatibility_evidence=managed_compatibility,
        )
        if managed_compatibility:
            evidence = runtime.compatibility_evidence
            if expected_integrity is None:
                # A legacy pointer may still restore ordinary inference, but it
                # cannot mint fresh signed-catalog authority during rollback.
                runtime = replace(runtime, compatibility_evidence=None)
            elif (
                evidence is None
                or not hmac.compare_digest(
                    expected_integrity,
                    evidence.local_integrity_fingerprint,
                )
            ):
                raise RuntimeUpdateError(
                    "rollback runtime local integrity no longer matches its activation"
                )
        pointer_value: dict[str, Any] = {
            "schema_version": 1,
            "version": runtime.version,
            "previous_version": current.version if current else None,
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "rollback": True,
        }
        if expected_integrity is not None:
            pointer_value["local_integrity_fingerprint"] = expected_integrity
        if current is not None and current_integrity is not None:
            pointer_value["previous_local_integrity_fingerprint"] = (
                current_integrity
            )
        _atomic_json(
            _active_pointer(self.root, normalized),
            pointer_value,
        )
        return runtime


__all__ = [
    "ActiveRuntime",
    "CORE_RUNTIME_PROTOCOL",
    "DS4_GLM53_PREVIEW_BRANCH",
    "DS4_GLM53_PREVIEW_CHANNEL",
    "DS4_GLM53_PREVIEW_REPO",
    "PreparedRuntime",
    "RuntimeCompatibilityEvidence",
    "RuntimeRelease",
    "RuntimeUpdateError",
    "RuntimeUpdateManager",
    "ds4_glm53_preview_capabilities",
    "resolve_active_runtime",
]
