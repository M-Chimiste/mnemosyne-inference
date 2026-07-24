from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import signal
import sys
import tarfile

import httpx
import pytest

from mnemosyne_macos.config import (
    DS4Config,
    LlamaCppConfig,
    MFluxConfig,
    OMLXConfig,
)
import mnemosyne_macos.runtime_updates as runtime_updates
from mnemosyne_macos.runtime_updates import (
    RuntimeUpdateError,
    RuntimeUpdateManager,
    _safe_extract,
    resolve_active_runtime,
)


@pytest.mark.asyncio
async def test_version_probe_terminates_its_process_group_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4242
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, None]:
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def wait(self) -> int:
            self.returncode = -signal.SIGTERM
            return self.returncode

    process = FakeProcess()
    signals: list[tuple[int, int]] = []

    async def create_process(*_argv: str, **_kwargs: object) -> FakeProcess:
        return process

    def kill_group(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(runtime_updates.os, "killpg", kill_group)

    assert (
        await runtime_updates._run_version_command("version-probe", timeout=0.001)
        is None
    )
    assert signals == [(process.pid, signal.SIGTERM)]


def _ds4_source_archive(revision: str) -> bytes:
    buffer = io.BytesIO()
    root = f"ds4-{revision}"
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in (
            (f"{root}/Makefile", b"ds4-server:\n\t@true\n"),
            (f"{root}/ds4_server.c", b"/* official source fixture */\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _llama_binary_archive(version: str) -> bytes:
    buffer = io.BytesIO()
    root = f"llama-{version}-bin-macos-arm64"
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = b"#!/bin/sh\nexit 0\n"
        info = tarfile.TarInfo(f"{root}/llama-server")
        info.size = len(payload)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _llama_release_payload(
    archive: bytes,
    *,
    version: str = "b7777",
    digest: str | None = None,
    size: int | None = None,
) -> dict:
    asset_name = f"llama-{version}-bin-macos-arm64.tar.gz"
    return {
        "tag_name": version,
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/ggml-org/llama.cpp/releases/tag/{version}",
        "target_commitish": "main",
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": (
                    "https://github.com/ggml-org/llama.cpp/releases/download/"
                    f"{version}/{asset_name}"
                ),
                "digest": (
                    f"sha256:{digest}"
                    if digest is not None
                    else f"sha256:{hashlib.sha256(archive).hexdigest()}"
                ),
                "size": len(archive) if size is None else size,
            }
        ],
    }


def _official_handler(
    *,
    mflux_version: str = "0.19.0",
    ds4_revision: str = "a" * 40,
    llama_version: str = "b7777",
    llama_digest: str | None = None,
) -> tuple[httpx.MockTransport, list[str]]:
    requests: list[str] = []
    archives = {ds4_revision: _ds4_source_archive(ds4_revision)}
    llama_archive = _llama_binary_archive(llama_version)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if request.url.path.endswith("/ggml-org/llama.cpp/releases/latest"):
            return httpx.Response(
                200,
                json=_llama_release_payload(
                    llama_archive,
                    version=llama_version,
                    digest=llama_digest,
                ),
            )
        if request.url.path.endswith("/jundot/omlx/releases"):
            return httpx.Response(
                200,
                json=[
                    {
                        "tag_name": "v0.3.12",
                        "draft": False,
                        "prerelease": False,
                        "html_url": "https://github.com/jundot/omlx/releases/tag/v0.3.12",
                    }
                ],
            )
        if request.url.host == "pypi.org":
            return httpx.Response(200, json={"info": {"version": mflux_version}})
        if request.url.path.endswith("/antirez/ds4/commits/main"):
            return httpx.Response(
                200,
                json={
                    "sha": ds4_revision,
                    "html_url": f"https://github.com/antirez/ds4/commit/{ds4_revision}",
                },
            )
        if request.url.host == "codeload.github.com":
            revision = request.url.path.rsplit("/", 1)[-1]
            body = archives.get(revision)
            return httpx.Response(200, content=body) if body else httpx.Response(404)
        if (
            request.url.host == "github.com"
            and request.url.path.endswith(
                f"/llama-{llama_version}-bin-macos-arm64.tar.gz"
            )
        ):
            return httpx.Response(200, content=llama_archive)
        return httpx.Response(404)

    return httpx.MockTransport(handler), requests


@pytest.mark.asyncio
async def test_update_check_uses_only_official_upstreams(tmp_path: Path) -> None:
    transport, requests = _official_handler()
    client = httpx.AsyncClient(transport=transport)
    manager = RuntimeUpdateManager(
        omlx=OMLXConfig(base_url="http://127.0.0.1:17322"),
        mflux=MFluxConfig(),
        ds4=DS4Config(binary=str(tmp_path / "missing")),
        root=tmp_path / "runtimes",
        client=client,
    )
    try:
        snapshot = await manager.check()
        assert snapshot["channel"] == "official"
        assert snapshot["manifest_url"] is None
        assert snapshot["source_policy"] == "official_upstreams"
        by_engine = {item["engine"]: item for item in snapshot["engines"]}
        assert by_engine["llama.cpp"]["available_version"] == "b7777"
        assert by_engine["llama.cpp"]["can_install"] is True
        assert "ggml-org/llama.cpp" in by_engine["llama.cpp"]["release_notes_url"]
        assert by_engine["omlx"]["latest_upstream_version"] == "0.3.12"
        assert by_engine["mflux"]["available_version"] == "0.19.0"
        assert by_engine["mflux"]["can_install"] is True
        assert by_engine["ds4"]["available_revision"] == "a" * 40
        assert by_engine["ds4"]["can_install"] is True
        assert all("M-Chimiste" not in url for url in requests)
        assert {httpx.URL(url).host for url in requests} <= {
            "api.github.com",
            "pypi.org",
            "127.0.0.1",
        }
    finally:
        await manager.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_omlx_wheel_version_comes_from_official_status_endpoint(
    tmp_path: Path,
) -> None:
    transport, _requests = _official_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/api/status":
            return httpx.Response(200, json={"status": "ok", "version": "0.5.3"})
        return transport.handle_request(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = RuntimeUpdateManager(
        omlx=OMLXConfig(base_url="http://127.0.0.1:17322"),
        mflux=MFluxConfig(),
        ds4=DS4Config(binary=str(tmp_path / "missing")),
        root=tmp_path / "runtimes",
        client=client,
    )
    try:
        snapshot = await manager.check()
        by_engine = {item["engine"]: item for item in snapshot["engines"]}
        assert by_engine["omlx"]["installed_version"] == "0.5.3"
        assert by_engine["omlx"]["installed_path"] == "http://127.0.0.1:17322"
    finally:
        await manager.aclose()
        await client.aclose()


def test_runtime_archive_rejects_symlink_pivot(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        symlink = tarfile.TarInfo("pivot")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "../outside"
        archive.addfile(symlink)

        payload = b"must stay contained"
        nested = tarfile.TarInfo("pivot/written.txt")
        nested.size = len(payload)
        archive.addfile(nested, io.BytesIO(payload))

    with pytest.raises(RuntimeUpdateError, match="escaping symlink|unsafe member"):
        _safe_extract(archive_path, tmp_path / "destination")
    assert not (tmp_path / "outside" / "written.txt").exists()


@pytest.mark.asyncio
async def test_official_ds4_builds_activate_and_rollback_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"revision": "a" * 40}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jundot/omlx/releases"):
            return httpx.Response(200, json=[])
        if request.url.host == "pypi.org":
            return httpx.Response(200, json={"info": {"version": "0.19.0"}})
        if request.url.path.endswith("/antirez/ds4/commits/main"):
            revision = state["revision"]
            return httpx.Response(
                200,
                json={
                    "sha": revision,
                    "html_url": f"https://github.com/antirez/ds4/commit/{revision}",
                },
            )
        if request.url.host == "codeload.github.com":
            revision = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, content=_ds4_source_archive(revision))
        return httpx.Response(404)

    async def fake_run_checked(
        *argv: str,
        cwd: Path | None = None,
        env=None,
        timeout: float = 3600,
    ) -> str:
        del env, timeout
        if argv[:2] == ("/usr/bin/make", "ds4-server"):
            assert cwd is not None
            binary = cwd / "ds4-server"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
        return "ok"

    monkeypatch.setattr(runtime_updates, "_run_checked", fake_run_checked)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = RuntimeUpdateManager(
        omlx=OMLXConfig(),
        mflux=MFluxConfig(),
        ds4=DS4Config(binary=str(tmp_path / "missing")),
        root=tmp_path / "runtimes",
        client=client,
    )
    try:
        await manager.check()
        first = await manager.prepare("ds4")
        assert resolve_active_runtime("ds4", root=manager.root) is None
        manager.activate(first)
        assert resolve_active_runtime("ds4", root=manager.root).version == "a" * 12

        state["revision"] = "b" * 40
        await manager.check(refresh=True)
        second = await manager.prepare("ds4")
        manager.activate(second)
        active = resolve_active_runtime("ds4", root=manager.root)
        assert active is not None and active.version == "b" * 12
        assert manager._rollback_version("ds4") == "a" * 12

        rolled_back = manager.rollback("ds4")
        assert rolled_back.version == "a" * 12
        pointer = json.loads((manager.root / "ds4" / "current.json").read_text())
        assert pointer["previous_version"] == "b" * 12
    finally:
        await manager.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_mflux_installs_official_pypi_package_with_bundled_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, _ = _official_handler(mflux_version="0.19.0")
    client = httpx.AsyncClient(transport=transport)
    worker_source = tmp_path / "image-worker"
    package = worker_source / "mnemosyne_mflux_worker"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (worker_source / "capabilities.json").write_text(
        json.dumps(
            [
                {
                    "repo_id": "Qwen/Qwen-Image",
                    "display_name": "Qwen Image",
                    "family": "qwen-image",
                    "default_num_inference_steps": 20,
                    "default_guidance_scale": 4.0,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_MFLUX_SOURCE", str(worker_source))

    async def fake_run_checked(
        *argv: str,
        cwd: Path | None = None,
        env=None,
        timeout: float = 3600,
    ) -> str:
        del cwd, env, timeout
        if "install" in argv:
            target = Path(argv[argv.index("--target") + 1])
            (target / "mflux").mkdir(parents=True)
            (target / "mflux" / "__init__.py").write_text("", encoding="utf-8")
        return "ok"

    monkeypatch.setattr(runtime_updates, "_run_checked", fake_run_checked)
    manager = RuntimeUpdateManager(
        omlx=OMLXConfig(),
        mflux=MFluxConfig(
            python=sys.executable,
            source_path_env="TEST_MFLUX_SOURCE",
        ),
        ds4=DS4Config(),
        root=tmp_path / "runtimes",
        client=client,
    )
    try:
        prepared = await manager.prepare("mflux")
        assert prepared.runtime.source_revision == "pypi:mflux==0.19.0"
        assert prepared.runtime.capabilities[0]["repo_id"] == "Qwen/Qwen-Image"
        assert "site_packages" in prepared.runtime.entrypoint
        manager.activate(prepared)
        active = resolve_active_runtime("mflux", root=manager.root)
        assert active is not None and active.version == "0.19.0"
    finally:
        await manager.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_official_llama_cpp_release_verifies_archive_and_records_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, requests = _official_handler(llama_version="b8123")
    client = httpx.AsyncClient(transport=transport)

    async def fake_run_checked(
        *argv: str,
        cwd: Path | None = None,
        env=None,
        timeout: float = 3600,
    ) -> str:
        del cwd, env, timeout
        if argv[-1] == "--version":
            return "version: 8123 (deadbeef)"
        if argv[-1] == "--help":
            return " ".join(
                (
                    "--model",
                    "--alias",
                    "--host",
                    "--port",
                    "--mmproj",
                    "--embedding",
                    "--reranking",
                    "--no-webui",
                )
            )
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(runtime_updates, "_run_checked", fake_run_checked)
    manager = RuntimeUpdateManager(
        llama_cpp=LlamaCppConfig(binary=str(tmp_path / "missing-llama-server")),
        omlx=OMLXConfig(),
        mflux=MFluxConfig(),
        ds4=DS4Config(),
        root=tmp_path / "runtimes",
        client=client,
    )
    try:
        snapshot = await manager.check()
        status = next(
            item for item in snapshot["engines"] if item["engine"] == "llama.cpp"
        )
        assert status["available_version"] == "b8123"
        assert status["can_install"] is True
        assert status["management_note"].endswith(
            "official ggml-org/llama.cpp GitHub releases."
        )

        prepared = await manager.prepare("llama.cpp")
        assert prepared.release.source_url.startswith(
            "https://github.com/ggml-org/llama.cpp/releases/download/"
        )
        assert prepared.release.sha256 is not None
        assert prepared.release.asset_size is not None
        assert prepared.runtime.engine == "llama.cpp"
        binary = prepared.runtime.path("binary")
        assert binary.name == "llama-server"
        assert binary.stat().st_mode & 0o111

        metadata = json.loads(
            (prepared.runtime.root / "runtime.json").read_text(encoding="utf-8")
        )
        assert metadata["source_sha256"] == prepared.release.sha256
        assert metadata["source_revision"] == "main"
        assert metadata["entrypoint"]["working_directory"] == "runtime"

        manager.activate(prepared)
        active = resolve_active_runtime("llama.cpp", root=manager.root)
        assert active is not None
        assert active.version == "b8123"
        assert active.path("binary") == binary
        assert any("llama-b8123-bin-macos-arm64.tar.gz" in url for url in requests)
    finally:
        await manager.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_llama_cpp_prepare_rejects_archive_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _requests = _official_handler(
        llama_version="b9000",
        llama_digest="0" * 64,
    )
    client = httpx.AsyncClient(transport=transport)

    async def must_not_run(*_argv, **_kwargs) -> str:
        raise AssertionError("an unverified archive must never execute")

    monkeypatch.setattr(runtime_updates, "_run_checked", must_not_run)
    manager = RuntimeUpdateManager(
        llama_cpp=LlamaCppConfig(binary=str(tmp_path / "missing-llama-server")),
        omlx=OMLXConfig(),
        mflux=MFluxConfig(),
        ds4=DS4Config(),
        root=tmp_path / "runtimes",
        client=client,
    )
    try:
        await manager.check()
        with pytest.raises(RuntimeUpdateError, match="SHA-256 verification failed"):
            await manager.prepare("llama.cpp")
        assert not (manager.root / "llama.cpp" / "b9000").exists()
    finally:
        await manager.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_official_llama_cpp_discovery_requires_published_integrity_metadata(
    tmp_path: Path,
) -> None:
    archive = _llama_binary_archive("b1")
    payload = _llama_release_payload(archive, version="b1")
    payload["assets"][0]["digest"] = None
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json=payload)
                if request.url.path.endswith(
                    "/ggml-org/llama.cpp/releases/latest"
                )
                else httpx.Response(404)
            )
        )
    )
    manager = RuntimeUpdateManager(
        llama_cpp=LlamaCppConfig(binary=str(tmp_path / "missing-llama-server")),
        omlx=OMLXConfig(),
        mflux=MFluxConfig(),
        ds4=DS4Config(),
        root=tmp_path / "runtimes",
        client=client,
    )
    try:
        with pytest.raises(RuntimeUpdateError, match="did not publish a SHA-256"):
            await manager._official_llama_cpp()
    finally:
        await manager.aclose()
        await client.aclose()


def test_invalid_active_pointer_falls_back_without_escaping_root(tmp_path: Path) -> None:
    pointer = tmp_path / "mflux" / "current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps({"schema_version": 1, "version": "../../outside"}),
        encoding="utf-8",
    )
    assert resolve_active_runtime("mflux", root=tmp_path) is None
