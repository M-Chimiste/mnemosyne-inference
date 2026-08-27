"""Official-source discovery and rollback-safe native engine updates.

MFLUX is installed from its official PyPI package into an isolated managed
directory. DS4 is built from an exact commit downloaded from its official
GitHub repository. oMLX remains externally owned and is updated by its own app
or Homebrew. No Unified Inference repository or release manifest is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
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
    MLXcelConfig,
    MistralRSConfig,
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
LLAMA_CPP_RELEASE_API_URL = (
    "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
)
LLAMA_CPP_RELEASE_TAG_API_URL = (
    "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/"
)
LLAMA_CPP_RELEASE_DOWNLOAD_PREFIX = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
)
LLAMA_CPP_STABLE_POINTER_NAME = "nightly-tag.txt"
MAX_LLAMA_CPP_STABLE_POINTER_BYTES = 32
MAX_SOURCE_ARCHIVE_BYTES = 2 * 1024**3
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_ENGINES = ("llama.cpp", "omlx", "mflux", "ds4")
_EXTERNAL_PREVIEW_ENGINES = ("mlxcel", "mistral.rs")
_OMLX_RELEASES_URL = "https://github.com/jundot/omlx/releases"
_MLXCEL_INSTALL_URL = "https://github.com/lablup/mlxcel#install-with-homebrew-macoslinux"
_MISTRAL_RS_INSTALL_URL = "https://ericlbuehler.github.io/mistral.rs/quickstart/"
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
class ActiveRuntime:
    engine: str
    version: str
    source_revision: str | None
    root: Path
    entrypoint: Mapping[str, str]
    capabilities: tuple[Mapping[str, Any], ...] = ()

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


def _load_runtime_directory(directory: Path, *, expected_engine: str) -> ActiveRuntime:
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
    runtime = ActiveRuntime(
        engine=engine,
        version=version,
        source_revision=source_revision,
        root=directory,
        entrypoint=dict(entrypoint),
        capabilities=tuple(dict(item) for item in capabilities_value),
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
    elif engine == "llama.cpp":
        binary = runtime.path("binary")
        working_directory = runtime.path("working_directory")
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeUpdateError(f"llama.cpp binary is not executable: {binary}")
        if not working_directory.is_dir():
            raise RuntimeUpdateError(
                f"llama.cpp working directory is missing: {working_directory}"
            )
    return runtime


def resolve_active_runtime(
    engine: str, *, root: str | Path | None = None
) -> ActiveRuntime | None:
    """Resolve the active managed runtime, returning no override if invalid."""

    normalized = _validate_engine(engine)
    runtime_root = _runtime_root(root)
    pointer = _active_pointer(runtime_root, normalized)
    try:
        payload = _read_json(pointer)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return None
        version = _validate_version(payload.get("version"))
        directory = runtime_root / normalized / version
        runtime = _load_runtime_directory(directory, expected_engine=normalized)
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
        mlxcel: MLXcelConfig | None = None,
        mistral_rs: MistralRSConfig | None = None,
        root: str | Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.llama_cpp = llama_cpp or LlamaCppConfig()
        self.omlx = omlx
        self.mflux = mflux
        self.ds4 = ds4
        self.mlxcel = mlxcel or MLXcelConfig()
        self.mistral_rs = mistral_rs or MistralRSConfig()
        self.root = _runtime_root(root)
        self.channel = "official"
        self._client = client or httpx.AsyncClient(timeout=30, follow_redirects=True)
        self._owns_client = client is None
        self._releases: dict[str, RuntimeRelease] = {}
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
        payload = await self._fetch_json(LLAMA_CPP_RELEASE_API_URL)
        if not isinstance(payload, dict):
            raise RuntimeUpdateError("official llama.cpp release response was invalid")
        stable_tag = _validate_version(payload.get("tag_name"))
        if payload.get("draft") or payload.get("prerelease"):
            raise RuntimeUpdateError("official latest llama.cpp release was not stable")
        release_url = str(
            payload.get("html_url")
            or f"https://github.com/ggml-org/llama.cpp/releases/tag/{stable_tag}"
        )
        tag = stable_tag
        expected_name = f"llama-{tag}-bin-macos-arm64.tar.gz"
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise RuntimeUpdateError("official llama.cpp release omitted assets")
        asset = next(
            (
                value
                for value in assets
                if isinstance(value, dict) and value.get("name") == expected_name
            ),
            None,
        )
        if asset is None:
            tag = await self._official_llama_cpp_stable_pointer(
                payload,
                stable_tag=stable_tag,
            )
            payload = await self._fetch_json(f"{LLAMA_CPP_RELEASE_TAG_API_URL}{tag}")
            if not isinstance(payload, dict):
                raise RuntimeUpdateError(
                    "official llama.cpp referenced release response was invalid"
                )
            if payload.get("draft") or payload.get("tag_name") != tag:
                raise RuntimeUpdateError(
                    "official llama.cpp stable pointer referenced an invalid release"
                )
            expected_name = f"llama-{tag}-bin-macos-arm64.tar.gz"
            assets = payload.get("assets")
            if not isinstance(assets, list):
                raise RuntimeUpdateError(
                    "official referenced llama.cpp release omitted assets"
                )
            asset = next(
                (
                    value
                    for value in assets
                    if isinstance(value, dict) and value.get("name") == expected_name
                ),
                None,
            )
            if asset is None:
                raise RuntimeUpdateError(
                    f"official referenced llama.cpp release omitted {expected_name}"
                )
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
        revision = payload.get("target_commitish")
        return RuntimeRelease(
            engine="llama.cpp",
            version=tag,
            source_revision=str(revision) if isinstance(revision, str) else None,
            source_url=source_url,
            release_notes_url=release_url,
            sha256=digest.split(":", 1)[1].casefold(),
            asset_size=size,
        )

    async def _official_llama_cpp_stable_pointer(
        self,
        payload: Mapping[str, Any],
        *,
        stable_tag: str,
    ) -> str:
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise RuntimeUpdateError("official llama.cpp release omitted assets")
        pointer = next(
            (
                value
                for value in assets
                if isinstance(value, dict)
                and value.get("name") == LLAMA_CPP_STABLE_POINTER_NAME
            ),
            None,
        )
        if pointer is None:
            expected_name = f"llama-{stable_tag}-bin-macos-arm64.tar.gz"
            raise RuntimeUpdateError(
                "official llama.cpp release omitted "
                f"{expected_name} and {LLAMA_CPP_STABLE_POINTER_NAME}"
            )
        expected_url = (
            f"{LLAMA_CPP_RELEASE_DOWNLOAD_PREFIX}{stable_tag}/"
            f"{LLAMA_CPP_STABLE_POINTER_NAME}"
        )
        source_url = pointer.get("browser_download_url")
        digest = pointer.get("digest")
        size = pointer.get("size")
        if source_url != expected_url:
            raise RuntimeUpdateError(
                "official llama.cpp stable pointer URL was invalid"
            )
        if not isinstance(digest, str) or not re.fullmatch(
            r"sha256:[0-9a-fA-F]{64}", digest
        ):
            raise RuntimeUpdateError(
                "official llama.cpp stable pointer did not publish a SHA-256 digest"
            )
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or size > MAX_LLAMA_CPP_STABLE_POINTER_BYTES
        ):
            raise RuntimeUpdateError(
                "official llama.cpp stable pointer size was invalid"
            )

        body = bytearray()
        async with self._client.stream(
            "GET",
            source_url,
            headers={"Accept": "text/plain", "User-Agent": "Unified-Inference/1"},
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_LLAMA_CPP_STABLE_POINTER_BYTES:
                    raise RuntimeUpdateError(
                        "official llama.cpp stable pointer exceeded its size limit"
                    )
        if len(body) != size:
            raise RuntimeUpdateError(
                "official llama.cpp stable pointer size did not match its metadata"
            )
        actual_digest = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(actual_digest, digest.split(":", 1)[1].casefold()):
            raise RuntimeUpdateError(
                "official llama.cpp stable pointer SHA-256 did not match its metadata"
            )
        try:
            tag = bytes(body).decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeUpdateError(
                "official llama.cpp stable pointer was not ASCII"
            ) from exc
        if not re.fullmatch(r"b[1-9][0-9]*", tag):
            raise RuntimeUpdateError(
                "official llama.cpp stable pointer did not name a build release"
            )
        return tag

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

    async def _official_ds4(self) -> RuntimeRelease:
        payload = await self._fetch_json(DS4_COMMIT_API_URL)
        if not isinstance(payload, dict) or not isinstance(payload.get("sha"), str):
            raise RuntimeUpdateError("official DS4 commit response was invalid")
        revision = str(payload["sha"])
        if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
            raise RuntimeUpdateError("official DS4 commit SHA was invalid")
        version = revision[:12].casefold()
        commit_url = str(
            payload.get("html_url")
            or f"https://github.com/antirez/ds4/commit/{revision}"
        )
        return RuntimeRelease(
            engine="ds4",
            version=version,
            source_revision=revision.casefold(),
            source_url=f"https://codeload.github.com/antirez/ds4/tar.gz/{revision}",
            release_notes_url=commit_url,
        )

    async def _refresh_releases(self) -> None:
        results = await asyncio.gather(
            self._official_llama_cpp(),
            self._official_omlx(),
            self._official_mflux(),
            self._official_ds4(),
            return_exceptions=True,
        )
        self._diagnostics.clear()
        self._releases.clear()
        for engine, result in zip(_ENGINES, results, strict=True):
            if isinstance(result, BaseException):
                self._diagnostics[engine] = f"official upstream check failed: {result}"
            elif engine == "omlx":
                self._upstream_omlx = result
            else:
                assert isinstance(result, RuntimeRelease)
                self._releases[engine] = result
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
    ) -> tuple[str | None, str | None, str | None]:
        managed = resolve_active_runtime("llama.cpp", root=self.root)
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

    async def _installed_mflux(self) -> tuple[str | None, str | None, str | None]:
        managed = resolve_active_runtime("mflux", root=self.root)
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

    async def _installed_ds4(self) -> tuple[str | None, str | None, str | None]:
        managed = resolve_active_runtime("ds4", root=self.root)
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

        async def external_binary(config: MLXcelConfig | MistralRSConfig):
            binary = Path(config.binary).expanduser()
            if not binary.is_file() or not os.access(binary, os.X_OK):
                return None, None, str(binary)
            try:
                output = await _run_version_command(str(binary), "--version")
            except RuntimeUpdateError:
                output = None
            match = re.search(
                r"\d+(?:\.\d+)+(?:[-+._A-Za-z0-9]*)?",
                output or "",
            )
            return match.group(0) if match else "unknown", None, str(binary)

        installed_values = await asyncio.gather(
            self._installed_llama_cpp(),
            self._installed_omlx(),
            self._installed_mflux(),
            self._installed_ds4(),
            external_binary(self.mlxcel),
            external_binary(self.mistral_rs),
        )
        return {
            engine: {
                "installed": version is not None,
                "version": version,
                "revision": revision,
                "path": path,
                "installation_kind": (
                    _omlx_installation_kind(path)
                    if engine == "omlx"
                    else "external_cli"
                    if engine in _EXTERNAL_PREVIEW_ENGINES
                    else "managed_or_configured"
                ),
            }
            for engine, (version, revision, path) in zip(
                (*_ENGINES, *_EXTERNAL_PREVIEW_ENGINES),
                installed_values,
                strict=True,
            )
        }

    async def check(self, *, refresh: bool = True) -> dict[str, Any]:
        async with self._check_lock:
            if refresh or self._last_checked_at is None:
                await self._refresh_releases()
            local_status = await self.installed_status()
            statuses: list[dict[str, Any]] = []
            for engine in (*_ENGINES, *_EXTERNAL_PREVIEW_ENGINES):
                installed_version = local_status[engine]["version"]
                installed_revision = local_status[engine]["revision"]
                installed_path = local_status[engine]["path"]
                if engine in _EXTERNAL_PREVIEW_ENGINES:
                    release = None
                    upstream_version = None
                    upstream_url = (
                        _MLXCEL_INSTALL_URL
                        if engine == "mlxcel"
                        else _MISTRAL_RS_INSTALL_URL
                    )
                    official_installer_url = upstream_url
                    # These binaries remain externally owned. Do not claim an
                    # available version without querying and validating their
                    # distinct upstream release contracts.
                    update_available = False
                elif engine == "omlx":
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
                            "mlxcel": "mlxcel",
                            "mistral.rs": "mistral.rs",
                        }[engine],
                        "ownership": (
                            "external"
                            if engine == "omlx"
                            or engine in _EXTERNAL_PREVIEW_ENGINES
                            else "managed_or_external"
                        ),
                        "installed": installed_version is not None,
                        "installed_version": installed_version,
                        "installed_revision": installed_revision,
                        "installed_path": installed_path,
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
                            False
                            if engine in _EXTERNAL_PREVIEW_ENGINES
                            else self._rollback_version(engine) is not None
                        ),
                        "management_note": (
                            "Version and Apple Silicon binaries come directly from official ggml-org/llama.cpp GitHub releases."
                            if engine == "llama.cpp"
                            else (
                                "The official oMLX app is recommended and includes precompiled custom kernels. oMLX remains independently owned and updated."
                                if engine == "omlx"
                                else (
                                    "Version and dependencies come directly from the official MFLUX package on PyPI."
                                    if engine == "mflux"
                                    else (
                                        "Version and source come directly from the official antirez/ds4 repository."
                                        if engine == "ds4"
                                        else (
                                            "Install and update the official Homebrew formula with brew upgrade mlxcel. Unified Inference owns only its child server process."
                                            if engine == "mlxcel"
                                            else "Use the official installer and mistralrs update. Unified Inference owns only its child server process."
                                        )
                                    )
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
                            else (
                                "Install the official external CLI, then check again."
                                if engine in _EXTERNAL_PREVIEW_ENGINES
                                and installed_version is None
                                else self._diagnostics.get(engine)
                            )
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
                            else "external_manual"
                            if engine in _EXTERNAL_PREVIEW_ENGINES
                            else "managed"
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
            await asyncio.to_thread(_safe_extract, archive, extracted)
            roots = [item for item in extracted.iterdir() if item.is_dir()]
            if len(roots) != 1:
                raise RuntimeUpdateError("official DS4 archive had an unexpected layout")
            os.replace(roots[0], source)
            shutil.rmtree(extracted)
            await _run_checked("/usr/bin/make", "ds4-server", cwd=source)
            binary = source / "ds4-server"
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise RuntimeUpdateError("official DS4 build did not produce ds4-server")
            await _run_checked(str(binary), "--help", cwd=source, timeout=30)
            _atomic_json(
                staging / "runtime.json",
                {
                    "schema_version": 1,
                    "engine": "ds4",
                    "version": release.version,
                    "source_revision": release.source_revision,
                    "core_protocol": CORE_RUNTIME_PROTOCOL,
                    "entrypoint": {
                        "binary": "source/ds4-server",
                        "working_directory": "source",
                    },
                },
            )
            return _load_runtime_directory(staging, expected_engine="ds4")
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
            )
            missing = [flag for flag in required_flags if flag not in help_text]
            if missing:
                raise RuntimeUpdateError(
                    "llama.cpp server contract changed; missing flags: "
                    + ", ".join(missing)
                )
            _atomic_json(
                staging / "runtime.json",
                {
                    "schema_version": 1,
                    "engine": "llama.cpp",
                    "version": release.version,
                    "source_revision": release.source_revision,
                    "source_sha256": release.sha256,
                    "core_protocol": CORE_RUNTIME_PROTOCOL,
                    "entrypoint": {
                        "binary": str(binary.relative_to(staging)),
                        "working_directory": str(binary.parent.relative_to(staging)),
                    },
                },
            )
            return _load_runtime_directory(staging, expected_engine="llama.cpp")
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

    async def prepare(self, engine: str, version: str | None = None) -> PreparedRuntime:
        normalized = _validate_engine(engine)
        if normalized == "omlx":
            raise RuntimeUpdateError("oMLX must be updated through its official updater")
        async with self._install_lock:
            if normalized not in self._releases:
                await self._refresh_releases()
            release = self._releases.get(normalized)
            if release is None or not release.compatible:
                detail = self._diagnostics.get(normalized, "official release unavailable")
                raise RuntimeUpdateError(f"no compatible official {normalized} update: {detail}")
            if version is not None and version != release.version:
                raise RuntimeUpdateError(
                    f"{version} is not the current official {normalized} version"
                )
            engine_root = self.root / normalized
            final = engine_root / release.version
            if final.exists():
                runtime = _load_runtime_directory(final, expected_engine=normalized)
                if runtime.source_revision != release.source_revision:
                    raise RuntimeUpdateError(
                        "existing runtime folder does not match the official source revision"
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
                    runtime=_load_runtime_directory(final, expected_engine=normalized),
                )
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def activate(self, prepared: PreparedRuntime) -> ActiveRuntime:
        engine = _validate_engine(prepared.release.engine)
        runtime = _load_runtime_directory(prepared.runtime.root, expected_engine=engine)
        current = resolve_active_runtime(engine, root=self.root)
        _atomic_json(
            _active_pointer(self.root, engine),
            {
                "schema_version": 1,
                "version": runtime.version,
                "previous_version": current.version if current else None,
                "activated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return runtime

    def _rollback_version(self, engine: str) -> str | None:
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
            _load_runtime_directory(self.root / engine / version, expected_engine=engine)
            return version
        except (OSError, ValueError, json.JSONDecodeError, RuntimeUpdateError):
            return None

    def rollback(self, engine: str) -> ActiveRuntime:
        normalized = _validate_engine(engine)
        version = self._rollback_version(normalized)
        if version is None:
            raise RuntimeUpdateError(f"no previous {normalized} runtime is available")
        current = resolve_active_runtime(normalized, root=self.root)
        runtime = _load_runtime_directory(
            self.root / normalized / version, expected_engine=normalized
        )
        _atomic_json(
            _active_pointer(self.root, normalized),
            {
                "schema_version": 1,
                "version": runtime.version,
                "previous_version": current.version if current else None,
                "activated_at": datetime.now(timezone.utc).isoformat(),
                "rollback": True,
            },
        )
        return runtime


__all__ = [
    "ActiveRuntime",
    "CORE_RUNTIME_PROTOCOL",
    "PreparedRuntime",
    "RuntimeRelease",
    "RuntimeUpdateError",
    "RuntimeUpdateManager",
    "resolve_active_runtime",
]
