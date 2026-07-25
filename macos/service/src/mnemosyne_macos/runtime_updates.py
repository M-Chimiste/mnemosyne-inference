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
import time
from typing import Any, Mapping
from uuid import uuid4

import httpx

from .config import DS4Config, LlamaCppConfig, MFluxConfig, OMLXConfig


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
MAX_SOURCE_ARCHIVE_BYTES = 2 * 1024**3
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_ENGINES = ("llama.cpp", "omlx", "mflux", "ds4")


class RuntimeUpdateError(RuntimeError):
    """A runtime update could not be checked, prepared, or activated."""


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
        self._upstream_omlx: tuple[str | None, str | None] = (None, None)
        self._diagnostics: dict[str, str] = {}
        self._last_checked_at: float | None = None
        self._check_lock = asyncio.Lock()
        self._install_lock = asyncio.Lock()

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

    async def _official_omlx(self) -> tuple[str | None, str | None]:
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
            return None, "https://github.com/jundot/omlx/releases"
        item = max(stable, key=lambda value: _version_key(str(value["tag_name"])))
        version = str(item["tag_name"]).removeprefix("v").removeprefix(".")
        return version, str(
            item.get("html_url") or "https://github.com/jundot/omlx/releases"
        )

    async def _official_llama_cpp(self) -> RuntimeRelease:
        payload = await self._fetch_json(LLAMA_CPP_RELEASE_API_URL)
        if not isinstance(payload, dict):
            raise RuntimeUpdateError("official llama.cpp release response was invalid")
        tag = _validate_version(payload.get("tag_name"))
        if payload.get("draft") or payload.get("prerelease"):
            raise RuntimeUpdateError("official latest llama.cpp release was not stable")
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
            raise RuntimeUpdateError(
                f"official llama.cpp release omitted {expected_name}"
            )
        source_url = asset.get("browser_download_url")
        digest = asset.get("digest")
        size = asset.get("size")
        if not isinstance(source_url, str) or not source_url.startswith(
            "https://github.com/ggml-org/llama.cpp/releases/download/"
        ):
            raise RuntimeUpdateError("official llama.cpp asset URL was invalid")
        if not isinstance(digest, str) or not re.fullmatch(
            r"sha256:[0-9a-fA-F]{64}", digest
        ):
            raise RuntimeUpdateError(
                "official llama.cpp asset did not publish a SHA-256 digest"
            )
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise RuntimeUpdateError("official llama.cpp asset size was invalid")
        release_url = str(
            payload.get("html_url")
            or f"https://github.com/ggml-org/llama.cpp/releases/tag/{tag}"
        )
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
        candidates = (
            Path("/Applications/oMLX.app/Contents/Info.plist"),
            Path.home() / "Applications" / "oMLX.app" / "Contents" / "Info.plist",
        )
        for candidate in candidates:
            try:
                with candidate.open("rb") as stream:
                    value = plistlib.load(stream).get("CFBundleShortVersionString")
                if isinstance(value, str) and value:
                    return value, None, str(candidate.parents[2])
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
                            return value.removeprefix("v"), None, self.omlx.base_url
            except (httpx.HTTPError, ValueError):
                continue
        executable = shutil.which("omlx")
        if executable:
            output = await _run_version_command(executable, "--version")
            match = re.search(r"\d+(?:\.\d+)+(?:[-+._A-Za-z0-9]*)?", output or "")
            if match:
                return match.group(0), None, executable
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

    async def check(self, *, refresh: bool = True) -> dict[str, Any]:
        async with self._check_lock:
            if refresh or self._last_checked_at is None:
                await self._refresh_releases()
            installed_values = await asyncio.gather(
                self._installed_llama_cpp(),
                self._installed_omlx(),
                self._installed_mflux(),
                self._installed_ds4(),
            )
            statuses: list[dict[str, Any]] = []
            for index, engine in enumerate(_ENGINES):
                installed_version, installed_revision, installed_path = installed_values[index]
                if engine == "omlx":
                    upstream_version, upstream_url = self._upstream_omlx
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
                    update_available = bool(
                        release
                        and self._release_is_newer(
                            release, installed_version, installed_revision
                        )
                    )
                statuses.append(
                    {
                        "engine": engine,
                        "display_name": {
                            "llama.cpp": "llama.cpp",
                            "omlx": "oMLX",
                            "mflux": "MFLUX",
                            "ds4": "DS4",
                        }[engine],
                        "ownership": "external" if engine == "omlx" else "managed_or_external",
                        "installed": installed_version is not None,
                        "installed_version": installed_version,
                        "installed_revision": installed_revision,
                        "installed_path": installed_path,
                        "latest_upstream_version": upstream_version,
                        "latest_upstream_revision": release.source_revision if release else None,
                        "latest_upstream_url": upstream_url,
                        "available_version": release.version if release else None,
                        "available_revision": release.source_revision if release else None,
                        "release_notes_url": upstream_url,
                        "update_available": update_available,
                        "can_install": engine in {"llama.cpp", "mflux", "ds4"}
                        and update_available,
                        "can_rollback": self._rollback_version(engine) is not None,
                        "management_note": (
                            "Version and Apple Silicon binaries come directly from official ggml-org/llama.cpp GitHub releases."
                            if engine == "llama.cpp"
                            else (
                                "Version comes from official oMLX GitHub releases. Update with the oMLX app or Homebrew."
                                if engine == "omlx"
                                else (
                                    "Version and dependencies come directly from the official MFLUX package on PyPI."
                                    if engine == "mflux"
                                    else "Version and source come directly from the official antirez/ds4 repository."
                                )
                            )
                        ),
                        "diagnostic": self._diagnostics.get(engine),
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
