from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
import tarfile
import threading

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
    _official_omlx_installer_url,
    _omlx_cli_candidates,
    _omlx_installation_kind,
    _python_subprocess_environment,
    _safe_extract,
    resolve_active_runtime,
)


@pytest.mark.asyncio
async def test_runtime_lifecycle_journal_is_private_bounded_and_durable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtimes"
    manager = RuntimeUpdateManager(
        omlx=OMLXConfig(),
        mflux=MFluxConfig(),
        ds4=DS4Config(),
        root=root,
    )
    try:
        for index in range(260):
            manager.record_lifecycle(
                engine="llama.cpp",
                action="prepared",
                outcome="succeeded",
                prepared_version=f"b{index}",
                active_version_before="b0",
                active_version_after="b0",
                source_revision="main",
            )
        evidence = manager.lifecycle_evidence()
        assert evidence["valid"] is True
        assert len(evidence["events"]) == 256
        assert evidence["dropped_events"] == 4
        assert evidence["events"][0]["sequence"] == 5
        assert (root / "lifecycle.json").stat().st_mode & 0o777 == 0o600
    finally:
        await manager.aclose()

    reopened = RuntimeUpdateManager(
        omlx=OMLXConfig(),
        mflux=MFluxConfig(),
        ds4=DS4Config(),
        root=root,
    )
    try:
        assert reopened.lifecycle_evidence() == evidence
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_runtime_lifecycle_recovers_corrupt_journal_without_error_text(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtimes"
    root.mkdir()
    (root / "lifecycle.json").write_text("{not-json", encoding="utf-8")
    manager = RuntimeUpdateManager(
        omlx=OMLXConfig(),
        mflux=MFluxConfig(),
        ds4=DS4Config(),
        root=root,
    )
    try:
        assert manager.lifecycle_evidence()["valid"] is False
        manager.record_lifecycle(
            engine="llama.cpp",
            action="install_rejected",
            outcome="failed",
            requested_version="b9000",
            active_version_before="b8123",
            active_version_after="b8123",
            error=RuntimeUpdateError(
                "SHA-256 mismatch; password=must-never-be-persisted"
            ),
        )
        evidence = manager.lifecycle_evidence()
        assert evidence["valid"] is True
        assert [event["action"] for event in evidence["events"]] == [
            "journal_reset",
            "install_rejected",
        ]
        assert evidence["events"][-1]["failure_code"] == "integrity"
        assert (
            runtime_updates._runtime_failure_code(
                RuntimeUpdateError(
                    "managed llama.cpp entrypoint escapes its runtime folder"
                )
            )
            == "unsafe_archive"
        )
        rendered = (root / "lifecycle.json").read_text(encoding="utf-8")
        assert "must-never-be-persisted" not in rendered
        assert "password" not in rendered
    finally:
        await manager.aclose()


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


def test_packaged_image_python_retains_only_its_required_pythonhome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONHOME", "/Applications/Unified/Python/cpython-3.12")
    monkeypatch.setenv("PYTHONPATH", "/service/source:/service/site-packages")
    monkeypatch.setenv(
        "TEST_MFLUX_PYTHON",
        "/Applications/Unified/Python/framework-mnemosyne-image/bin/python3",
    )

    packaged = _python_subprocess_environment(
        "/Applications/Unified/Python/framework-mnemosyne-image/bin/python3",
        bundled_python_env="TEST_MFLUX_PYTHON",
    )
    external = _python_subprocess_environment(
        "/custom/image-venv/bin/python3",
        bundled_python_env="TEST_MFLUX_PYTHON",
    )

    assert packaged["PYTHONHOME"] == "/Applications/Unified/Python/cpython-3.12"
    assert "PYTHONPATH" not in packaged
    assert "PYTHONHOME" not in external
    assert "PYTHONPATH" not in external


@pytest.mark.asyncio
async def test_mflux_ensurepip_probe_uses_packaged_python_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python = tmp_path / "framework-mnemosyne-image" / "bin" / "python3"
    wheel = tmp_path / "pip-25.0.1-py3-none-any.whl"
    wheel.write_bytes(b"pip fixture")
    monkeypatch.setenv("TEST_MFLUX_PYTHON", str(python))
    monkeypatch.setenv("PYTHONHOME", "/Applications/Unified/Python/cpython-3.12")
    monkeypatch.setenv("PYTHONPATH", "/service/source:/service/site-packages")
    observed: dict[str, object] = {}

    async def run_version_command(
        *argv: str,
        timeout: float = 5.0,
        env: dict[str, str] | None = None,
    ) -> str:
        observed.update(argv=argv, timeout=timeout, env=env)
        return str(wheel)

    monkeypatch.setattr(
        runtime_updates,
        "_run_version_command",
        run_version_command,
    )
    manager = RuntimeUpdateManager(
        omlx=OMLXConfig(base_url="http://127.0.0.1:17322"),
        mflux=MFluxConfig(python_env="TEST_MFLUX_PYTHON"),
        ds4=DS4Config(binary=str(tmp_path / "missing-ds4")),
        root=tmp_path / "runtimes",
    )
    try:
        assert await manager._pip_wheel(python) == wheel
        assert observed["argv"][:2] == (str(python), "-c")
        environment = observed["env"]
        assert isinstance(environment, dict)
        assert (
            environment["PYTHONHOME"]
            == "/Applications/Unified/Python/cpython-3.12"
        )
        assert "PYTHONPATH" not in environment
    finally:
        await manager.aclose()


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


def _ds4_glm53_preview_source_archive(
    revision: str,
    *,
    branch_contract: bool = True,
) -> bytes:
    buffer = io.BytesIO()
    root = f"ds4-{revision}"
    contract = (
        'GLM53_REPO="antirez/glm-5.3-flash-gguf"\n'
        'GLM53_Q2_FILE="GLM-5.3-Flash-Q2.gguf"\n'
        'GLM53_Q4_FILE="GLM-5.3-Flash-Q4_K.gguf"\n'
        'case "$MODEL" in\n'
        '    glm53-q2)\n'
        '        REPO=$GLM53_REPO\n'
        '        MODEL_FILE=$GLM53_Q2_FILE\n'
        '        FORCE_HF_DOWNLOAD=1\n'
        '        ;;\n'
        + (
            '    glm53-q4)\n'
            '        REPO=$GLM53_REPO\n'
            '        MODEL_FILE=$GLM53_Q4_FILE\n'
            '        FORCE_HF_DOWNLOAD=1\n'
            '        ;;\n'
            if branch_contract
            else ""
        )
        + "esac\n"
    ).encode()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in (
            (f"{root}/Makefile", b"ds4-server:\n\t@true\n"),
            (f"{root}/ds4_server.c", b"/* official preview source fixture */\n"),
            (f"{root}/download_model.sh", contract),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
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


def _write_managed_llama_runtime(
    root: Path,
    *,
    binary_payload: bytes = b"#!/bin/sh\nexit 0\n",
) -> Path:
    version = "b8123"
    runtime_root = root / "llama.cpp" / version
    runtime_files = runtime_root / "runtime"
    runtime_files.mkdir(parents=True)
    binary = runtime_files / "llama-server"
    binary.write_bytes(binary_payload)
    binary.chmod(0o755)
    source_payload = b"verified official llama.cpp archive fixture"
    source_digest = hashlib.sha256(source_payload).hexdigest()
    release = runtime_updates.RuntimeRelease(
        engine="llama.cpp",
        version=version,
        source_revision="main",
        source_url=(
            "https://github.com/ggml-org/llama.cpp/releases/download/"
            f"{version}/llama-{version}-bin-macos-arm64.tar.gz"
        ),
        release_notes_url=(
            f"https://github.com/ggml-org/llama.cpp/releases/tag/{version}"
        ),
        sha256=source_digest,
        asset_size=len(source_payload),
    )
    evidence = runtime_updates._new_runtime_compatibility_evidence(
        release,
        source_sha256=f"sha256:{source_digest}",
        source_size_bytes=len(source_payload),
        executable=binary,
    )
    (runtime_root / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "llama.cpp",
                "version": version,
                "source_revision": "main",
                "source_sha256": source_digest,
                "source_size_bytes": len(source_payload),
                "core_protocol": 1,
                "entrypoint": {
                    "binary": "runtime/llama-server",
                    "working_directory": "runtime",
                },
                "compatibility_evidence": evidence.to_manifest(),
            }
        ),
        encoding="utf-8",
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "llama.cpp" / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "previous_version": None,
                "activated_at": "2026-08-30T00:00:00+00:00",
                "local_integrity_fingerprint": (
                    evidence.local_integrity_fingerprint
                ),
            }
        ),
        encoding="utf-8",
    )
    return runtime_root


def test_runtime_compatibility_identity_is_path_and_mtime_independent(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first" / "runtimes"
    second_root = tmp_path / "second" / "runtimes"
    first_directory = _write_managed_llama_runtime(first_root)
    second_directory = _write_managed_llama_runtime(second_root)
    os.utime(first_directory / "runtime" / "llama-server", (1, 1))
    os.utime(second_directory / "runtime" / "llama-server", (2_000_000, 2_000_000))

    first = runtime_updates._load_runtime_directory(
        first_directory,
        expected_engine="llama.cpp",
        verify_compatibility_evidence=True,
    )
    second = runtime_updates._load_runtime_directory(
        second_directory,
        expected_engine="llama.cpp",
        verify_compatibility_evidence=True,
    )

    assert first.compatibility_evidence is not None
    assert second.compatibility_evidence is not None
    assert (
        first.compatibility_evidence.compatibility_fingerprint
        == second.compatibility_evidence.compatibility_fingerprint
    )
    assert (
        first.compatibility_evidence.local_integrity_fingerprint
        == second.compatibility_evidence.local_integrity_fingerprint
    )
    assert first.compatibility_evidence.features == (
        "apple-metal",
        "flash-attention",
    )
    serialized = json.dumps(first.compatibility_evidence.to_manifest())
    assert str(first_root) not in serialized
    assert str(second_root) not in serialized

    revision = "d" * 40
    source_payload = b"one exact official DS4 source archive"
    source_digest = "sha256:" + hashlib.sha256(source_payload).hexdigest()
    ds4_release = runtime_updates.RuntimeRelease(
        engine="ds4",
        version=revision[:12],
        source_revision=revision,
        source_url=f"https://codeload.github.com/antirez/ds4/tar.gz/{revision}",
        release_notes_url=f"https://github.com/antirez/ds4/commit/{revision}",
        channel="official",
        source_branch="main",
    )
    first_binary = tmp_path / "first-ds4"
    second_binary = tmp_path / "second-ds4"
    first_binary.write_bytes(b"locally compiled DS4 binary A")
    second_binary.write_bytes(b"locally compiled DS4 binary B")
    first_ds4 = runtime_updates._new_runtime_compatibility_evidence(
        ds4_release,
        source_sha256=source_digest,
        source_size_bytes=len(source_payload),
        executable=first_binary,
    )
    second_ds4 = runtime_updates._new_runtime_compatibility_evidence(
        ds4_release,
        source_sha256=source_digest,
        source_size_bytes=len(source_payload),
        executable=second_binary,
    )
    assert first_ds4.compatibility_fingerprint == second_ds4.compatibility_fingerprint
    assert first_ds4.executable_sha256 != second_ds4.executable_sha256
    assert (
        first_ds4.local_integrity_fingerprint
        != second_ds4.local_integrity_fingerprint
    )
    assert first_ds4.features == second_ds4.features == ()


@pytest.mark.parametrize(
    "tamper",
    (
        "binary",
        "binary_and_manifest_seal",
        "features",
        "source_digest",
        "fingerprint",
    ),
)
def test_managed_runtime_compatibility_evidence_fails_closed_after_tamper(
    tmp_path: Path,
    tamper: str,
) -> None:
    runtime_directory = _write_managed_llama_runtime(tmp_path / tamper)
    manifest_path = runtime_directory / "runtime.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if tamper in {"binary", "binary_and_manifest_seal"}:
        binary = runtime_directory / "runtime" / "llama-server"
        binary.write_bytes(
            b"#!/bin/sh\nexit 1\n"
        )
        if tamper == "binary_and_manifest_seal":
            executable_sha256 = "sha256:" + hashlib.sha256(
                binary.read_bytes()
            ).hexdigest()
            manifest["compatibility_evidence"]["executable_sha256"] = (
                executable_sha256
            )
            manifest["compatibility_evidence"][
                "local_integrity_fingerprint"
            ] = runtime_updates._runtime_local_integrity_fingerprint(
                compatibility_fingerprint=manifest["compatibility_evidence"][
                    "compatibility_fingerprint"
                ],
                executable_sha256=executable_sha256,
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif tamper == "features":
        manifest["compatibility_evidence"]["features"].append("unverified")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif tamper == "source_digest":
        manifest["source_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        manifest["compatibility_evidence"]["compatibility_fingerprint"] = (
            "sha256:" + "0" * 64
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    ordinary = resolve_active_runtime(
        "llama.cpp",
        root=runtime_directory.parents[1],
    )
    verified = resolve_active_runtime(
        "llama.cpp",
        root=runtime_directory.parents[1],
        verify_compatibility_evidence=True,
    )

    # Compatibility authority fails closed without taking an otherwise usable
    # legacy/local runtime out of the ordinary inference path.
    assert ordinary is not None
    assert ordinary.compatibility_evidence is None
    assert ordinary.path("binary").is_file()
    assert verified is not None
    assert verified.compatibility_evidence is None


def test_legacy_active_pointer_does_not_mint_compatibility_authority(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtimes"
    runtime_directory = _write_managed_llama_runtime(runtime_root)
    pointer_path = runtime_root / "llama.cpp" / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer.pop("local_integrity_fingerprint")
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    ordinary = resolve_active_runtime("llama.cpp", root=runtime_root)
    verified = resolve_active_runtime(
        "llama.cpp",
        root=runtime_root,
        verify_compatibility_evidence=True,
    )

    assert ordinary is not None
    assert ordinary.root == runtime_directory
    assert verified is not None
    assert verified.compatibility_evidence is None


@pytest.mark.asyncio
async def test_ordinary_runtime_resolution_never_hashes_on_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtimes"
    runtime_directory = _write_managed_llama_runtime(runtime_root)
    binary = runtime_directory / "runtime" / "llama-server"
    expected_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    event_loop_thread = threading.get_ident()
    hash_threads: list[int] = []
    resolve_calls: list[tuple[str, int]] = []
    original_resolve = runtime_updates.resolve_active_runtime

    def observed_hash(path: Path) -> str:
        assert path == binary
        hash_threads.append(threading.get_ident())
        return expected_digest

    def observed_resolve(engine: str, **kwargs):
        resolve_calls.append((engine, threading.get_ident()))
        return original_resolve(engine, **kwargs)

    monkeypatch.setattr(runtime_updates, "_sha256_file", observed_hash)
    monkeypatch.setattr(
        runtime_updates,
        "resolve_active_runtime",
        observed_resolve,
    )

    ordinary = observed_resolve("llama.cpp", root=runtime_root)
    assert ordinary is not None
    assert ordinary.compatibility_evidence is None
    assert hash_threads == []
    resolve_calls.clear()

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(404))
    )
    manager = RuntimeUpdateManager(
        llama_cpp=LlamaCppConfig(binary=str(tmp_path / "missing-llama")),
        omlx=OMLXConfig(base_url="http://127.0.0.1:17322"),
        mflux=MFluxConfig(),
        ds4=DS4Config(binary=str(tmp_path / "missing-ds4")),
        root=runtime_root,
        client=client,
    )
    try:
        status = await manager.installed_status()
    finally:
        await manager.aclose()
        await client.aclose()

    assert status["llama.cpp"]["compatibility_fingerprint"] is not None
    assert len(hash_threads) == 1
    assert event_loop_thread not in hash_threads
    assert sorted(engine for engine, _thread in resolve_calls) == [
        "ds4",
        "llama.cpp",
        "mflux",
    ]
    assert all(thread != event_loop_thread for _engine, thread in resolve_calls)


@pytest.mark.asyncio
async def test_legacy_external_and_omlx_compatibility_remain_unknown(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtimes"
    legacy_directory = _write_managed_llama_runtime(runtime_root)
    manifest_path = legacy_directory / "runtime.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("compatibility_evidence")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    external_ds4 = tmp_path / "external-ds4"
    external_ds4.write_bytes(b"external DS4 build")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"version": "0.5.3"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = RuntimeUpdateManager(
        llama_cpp=LlamaCppConfig(binary=str(tmp_path / "external-llama")),
        omlx=OMLXConfig(base_url="http://127.0.0.1:17322"),
        mflux=MFluxConfig(),
        ds4=DS4Config(binary=str(external_ds4)),
        root=runtime_root,
        client=client,
    )
    try:
        status = await manager.installed_status()
    finally:
        await manager.aclose()
        await client.aclose()

    assert status["llama.cpp"]["installed"] is True
    assert status["ds4"]["installed"] is True
    assert status["omlx"]["installed"] is True
    for engine in ("llama.cpp", "ds4", "omlx"):
        assert status[engine]["compatibility_fingerprint"] is None
        assert status[engine]["features"] == []


def _llama_release_payload(
    archive: bytes,
    *,
    version: str = "b7777",
    digest: str | None = None,
    size: int | None = None,
    prerelease: bool = True,
) -> dict:
    asset_name = f"llama-{version}-bin-macos-arm64.tar.gz"
    return {
        "tag_name": version,
        "draft": False,
        "prerelease": prerelease,
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
        if request.url.path.endswith("/ggml-org/llama.cpp/releases"):
            return httpx.Response(
                200,
                json=[
                    _llama_release_payload(
                        llama_archive,
                        version=llama_version,
                        digest=llama_digest,
                    )
                ],
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
                        "assets": [
                            {
                                "name": "oMLX-0.3.12-macos15-sequoia.dmg",
                                "browser_download_url": (
                                    "https://github.com/jundot/omlx/releases/download/"
                                    "v0.3.12/oMLX-0.3.12-macos15-sequoia.dmg"
                                ),
                                "size": 500_000_000,
                                "digest": f"sha256:{'a' * 64}",
                            },
                            {
                                "name": "oMLX-0.3.12-macos26-27.dmg",
                                "browser_download_url": (
                                    "https://github.com/jundot/omlx/releases/download/"
                                    "v0.3.12/oMLX-0.3.12-macos26-27.dmg"
                                ),
                                "size": 500_000_000,
                                "digest": f"sha256:{'b' * 64}",
                            },
                        ],
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
async def test_llama_cpp_update_selects_newest_complete_official_build(
    tmp_path: Path,
) -> None:
    older_archive = _llama_binary_archive("b10621")
    newest_complete_archive = _llama_binary_archive("b10750")
    newest_uploading = _llama_release_payload(
        _llama_binary_archive("b10751"), version="b10751"
    )
    newest_uploading["assets"] = []
    semantic_release = {
        "tag_name": "v0.3.0",
        "draft": False,
        "prerelease": False,
        "assets": [{"name": "nightly-tag.txt"}],
    }
    draft = _llama_release_payload(
        _llama_binary_archive("b99999"), version="b99999"
    )
    draft["draft"] = True
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/ggml-org/llama.cpp/releases"):
            return httpx.Response(
                200,
                json=[
                    newest_uploading,
                    semantic_release,
                    draft,
                    _llama_release_payload(
                        newest_complete_archive, version="b10750"
                    ),
                    _llama_release_payload(older_archive, version="b10621"),
                ],
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
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
        assert status["available_version"] == "b10750"
        assert status["latest_upstream_url"].endswith("/releases/tag/b10750")
        assert status["update_available"] is True
        assert status["can_install"] is True
        assert any("/releases?per_page=20" in url for url in requests)
    finally:
        await manager.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_llama_cpp_update_requires_a_complete_official_build(
    tmp_path: Path,
) -> None:
    incomplete = _llama_release_payload(
        _llama_binary_archive("b10751"), version="b10751"
    )
    incomplete["assets"] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[incomplete])
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
        snapshot = await manager.check()
        status = next(
            item for item in snapshot["engines"] if item["engine"] == "llama.cpp"
        )
        assert status["available_version"] is None
        assert status["update_available"] is False
        assert "complete macOS ARM64 build" in status["diagnostic"]
    finally:
        await manager.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_update_check_uses_only_official_upstreams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_updates.platform,
        "mac_ver",
        lambda: ("26.0", ("", "", ""), ""),
    )
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
        assert by_engine["llama.cpp"]["release_tier"] == "stable"
        assert by_engine["omlx"]["release_tier"] == "stable"
        assert by_engine["mflux"]["release_tier"] == "preview"
        assert by_engine["ds4"]["release_tier"] == "preview"
        assert set(by_engine) == {"llama.cpp", "omlx", "mflux", "ds4"}
        assert by_engine["llama.cpp"]["available_version"] == "b7777"
        assert by_engine["llama.cpp"]["can_install"] is True
        assert "ggml-org/llama.cpp" in by_engine["llama.cpp"]["release_notes_url"]
        assert by_engine["omlx"]["latest_upstream_version"] == "0.3.12"
        assert by_engine["omlx"]["official_installer_url"].endswith(".dmg")
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
async def test_installed_status_never_contacts_upstream(
    tmp_path: Path,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = RuntimeUpdateManager(
        llama_cpp=LlamaCppConfig(binary=str(tmp_path / "missing-llama")),
        omlx=OMLXConfig(base_url="http://127.0.0.1:1"),
        mflux=MFluxConfig(),
        ds4=DS4Config(binary=str(tmp_path / "missing-ds4")),
        root=tmp_path / "runtimes",
        client=client,
    )
    try:
        status = await manager.installed_status()

        assert set(status) == {
            "llama.cpp",
            "omlx",
            "mflux",
            "ds4",
        }
        assert status["llama.cpp"]["installed"] is False
        assert status["ds4"]["installed"] is False
        # A bounded loopback oMLX probe is local runtime discovery, not an
        # upstream release query. Nothing may contact GitHub or PyPI.
        assert all(
            httpx.URL(url).host in {"127.0.0.1", "::1", "localhost"}
            for url in requests
        )
    finally:
        await manager.aclose()
        await client.aclose()


def test_omlx_installer_selects_exact_then_ranged_macos_asset() -> None:
    release = {
        "tag_name": "v0.5.3",
        "assets": [
            {
                "name": "oMLX-0.5.3-macos15-sequoia.dmg",
                "browser_download_url": (
                    "https://github.com/jundot/omlx/releases/download/"
                    "v0.5.3/oMLX-0.5.3-macos15-sequoia.dmg"
                ),
                "size": 1,
                "digest": f"sha256:{'a' * 64}",
            },
            {
                "name": "oMLX-0.5.3-macos26-27.dmg",
                "browser_download_url": (
                    "https://github.com/jundot/omlx/releases/download/"
                    "v0.5.3/oMLX-0.5.3-macos26-27.dmg"
                ),
                "size": 1,
                "digest": f"sha256:{'b' * 64}",
            },
        ],
    }

    assert _official_omlx_installer_url(release, macos_major=15).endswith(
        "macos15-sequoia.dmg"
    )
    assert _official_omlx_installer_url(release, macos_major=27).endswith(
        "macos26-27.dmg"
    )
    assert _official_omlx_installer_url(release, macos_major=25) is None


def test_omlx_cli_candidates_include_packaged_and_homebrew_locations() -> None:
    candidates = _omlx_cli_candidates()

    assert Path.home() / ".omlx" / "bin" / "omlx" in candidates
    assert Path("/opt/homebrew/bin/omlx") in candidates
    assert Path("/usr/local/bin/omlx") in candidates


def test_omlx_installation_ownership_distinguishes_stable_and_head(
    tmp_path: Path,
) -> None:
    stable = tmp_path / "Cellar" / "omlx" / "0.5.7" / "bin" / "omlx"
    head = tmp_path / "Cellar" / "omlx" / "HEAD-aed846f" / "bin" / "omlx"
    stable.parent.mkdir(parents=True)
    head.parent.mkdir(parents=True)
    stable.touch()
    head.touch()

    assert _omlx_installation_kind(str(stable)) == "homebrew_stable"
    assert _omlx_installation_kind(str(head)) == "homebrew_head"
    assert _omlx_installation_kind("/Applications/oMLX.app") == "official_app"
    assert (
        _omlx_installation_kind("http://127.0.0.1:17322")
        == "running_external"
    )


@pytest.mark.asyncio
async def test_supervised_omlx_homebrew_upgrade_uses_fixed_owner_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = tmp_path / "Cellar" / "omlx" / "0.5.6" / "bin" / "omlx"
    brew = tmp_path / "homebrew" / "bin" / "brew"
    cli.parent.mkdir(parents=True)
    brew.parent.mkdir(parents=True)
    cli.touch(mode=0o755)
    brew.touch(mode=0o755)
    commands: list[tuple[str, ...]] = []

    async def installed_status() -> dict:
        return {
            "omlx": {
                "installed": True,
                "version": "0.5.6",
                "revision": None,
                "path": str(cli),
                "installation_kind": "homebrew_stable",
            }
        }

    async def run_checked(*argv: str, **_kwargs: object) -> str:
        commands.append(argv)
        return "ok"

    async def installed_omlx() -> tuple[str, None, str]:
        return "0.5.7", None, str(cli)

    manager = RuntimeUpdateManager(
        omlx=OMLXConfig(),
        mflux=MFluxConfig(),
        ds4=DS4Config(),
        root=tmp_path / "runtimes",
    )
    manager._last_checked_at = 1
    manager._upstream_omlx = ("0.5.7", "https://example.invalid", None)
    monkeypatch.setattr(manager, "installed_status", installed_status)
    monkeypatch.setattr(manager, "_installed_omlx", installed_omlx)
    monkeypatch.setattr(runtime_updates, "_homebrew_executable", lambda: brew)
    monkeypatch.setattr(runtime_updates, "_omlx_cli_candidates", lambda: (cli,))
    monkeypatch.setattr(runtime_updates, "_run_checked", run_checked)
    try:
        version, path = await manager.upgrade_omlx_homebrew("0.5.7")
        assert version == "0.5.7"
        assert path == str(cli)
        assert commands == [
            (str(cli), "stop"),
            (str(brew), "update"),
            (str(brew), "upgrade", "omlx"),
            (str(cli), "start"),
        ]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_supervised_omlx_upgrade_restarts_after_ambiguous_stop_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = tmp_path / "Cellar" / "omlx" / "0.5.6" / "bin" / "omlx"
    brew = tmp_path / "homebrew" / "bin" / "brew"
    cli.parent.mkdir(parents=True)
    brew.parent.mkdir(parents=True)
    cli.touch(mode=0o755)
    brew.touch(mode=0o755)
    commands: list[tuple[str, ...]] = []

    async def installed_status() -> dict:
        return {
            "omlx": {
                "path": str(cli),
                "installation_kind": "homebrew_stable",
            }
        }

    async def run_checked(*argv: str, **_kwargs: object) -> str:
        commands.append(argv)
        if argv[1] == "stop":
            raise RuntimeUpdateError("command timed out: omlx stop")
        return "ok"

    manager = RuntimeUpdateManager(
        omlx=OMLXConfig(),
        mflux=MFluxConfig(),
        ds4=DS4Config(),
        root=tmp_path / "runtimes",
    )
    manager._last_checked_at = 1
    manager._upstream_omlx = ("0.5.7", "https://example.invalid", None)
    monkeypatch.setattr(manager, "installed_status", installed_status)
    monkeypatch.setattr(runtime_updates, "_homebrew_executable", lambda: brew)
    monkeypatch.setattr(runtime_updates, "_omlx_cli_candidates", lambda: (cli,))
    monkeypatch.setattr(runtime_updates, "_run_checked", run_checked)
    try:
        with pytest.raises(RuntimeUpdateError, match="command timed out"):
            await manager.upgrade_omlx_homebrew("0.5.7")
        assert commands == [(str(cli), "stop"), (str(cli), "start")]
    finally:
        await manager.aclose()


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
        assert first.runtime.compatibility_evidence is not None
        assert first.runtime.compatibility_evidence.features == ()
        first_fingerprint = (
            first.runtime.compatibility_evidence.compatibility_fingerprint
        )
        assert resolve_active_runtime("ds4", root=manager.root) is None
        manager.activate(first)
        assert resolve_active_runtime("ds4", root=manager.root).version == "a" * 12
        pointer = json.loads((manager.root / "ds4" / "current.json").read_text())
        assert pointer["local_integrity_fingerprint"] == (
            first.runtime.compatibility_evidence.local_integrity_fingerprint
        )
        assert "previous_local_integrity_fingerprint" not in pointer
        first_status = await manager.installed_status()
        assert first_status["ds4"]["compatibility_fingerprint"] == first_fingerprint
        assert first_status["ds4"]["features"] == []

        state["revision"] = "b" * 40
        await manager.check(refresh=True)
        second = await manager.prepare("ds4")
        assert second.runtime.compatibility_evidence is not None
        assert (
            second.runtime.compatibility_evidence.compatibility_fingerprint
            != first_fingerprint
        )
        manager.activate(second)
        active = resolve_active_runtime("ds4", root=manager.root)
        assert active is not None and active.version == "b" * 12
        pointer = json.loads((manager.root / "ds4" / "current.json").read_text())
        assert pointer["local_integrity_fingerprint"] == (
            second.runtime.compatibility_evidence.local_integrity_fingerprint
        )
        assert pointer["previous_local_integrity_fingerprint"] == (
            first.runtime.compatibility_evidence.local_integrity_fingerprint
        )
        assert manager._rollback_version("ds4") == "a" * 12

        rolled_back = manager.rollback("ds4")
        assert rolled_back.version == "a" * 12
        assert rolled_back.compatibility_evidence is not None
        assert (
            rolled_back.compatibility_evidence.compatibility_fingerprint
            == first_fingerprint
        )
        pointer = json.loads((manager.root / "ds4" / "current.json").read_text())
        assert pointer["previous_version"] == "b" * 12
        assert pointer["local_integrity_fingerprint"] == (
            first.runtime.compatibility_evidence.local_integrity_fingerprint
        )
        assert pointer["previous_local_integrity_fingerprint"] == (
            second.runtime.compatibility_evidence.local_integrity_fingerprint
        )
    finally:
        await manager.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_ds4_glm53_preview_channel_is_commit_and_source_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "c" * 40
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/jundot/omlx/releases"):
            return httpx.Response(200, json=[])
        if request.url.host == "pypi.org":
            return httpx.Response(200, json={"info": {"version": "0.19.0"}})
        if request.url.path.endswith("/antirez/ds4/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        if request.url.path.endswith(
            "/antirez/ds4/commits/glm-5.3-flash"
        ):
            return httpx.Response(200, json={"sha": revision})
        if request.url.host == "codeload.github.com":
            assert request.url.path.endswith(f"/{revision}")
            return httpx.Response(
                200,
                content=_ds4_glm53_preview_source_archive(revision),
            )
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
        snapshot = await manager.check()
        ds4 = next(item for item in snapshot["engines"] if item["engine"] == "ds4")
        preview = ds4["managed_channels"][0]
        assert preview == {
            "channel": "glm-5.3-flash",
            "source_branch": "glm-5.3-flash",
            "release_tier": "experimental",
            "available_version": revision[:12],
            "available_revision": revision,
            "release_notes_url": f"https://github.com/antirez/ds4/commit/{revision}",
            "update_available": True,
            "can_install": True,
            "diagnostic": None,
        }

        prepared = await manager.prepare(
            "ds4",
            revision[:12],
            channel=runtime_updates.DS4_GLM53_PREVIEW_CHANNEL,
        )
        assert prepared.release.source_revision == revision
        assert prepared.release.source_branch == "glm-5.3-flash"
        assert prepared.release.source_url.endswith(f"/tar.gz/{revision}")
        metadata = json.loads(
            (prepared.runtime.root / "runtime.json").read_text(encoding="utf-8")
        )
        assert metadata["channel"] == "glm-5.3-flash"
        assert metadata["source_branch"] == "glm-5.3-flash"
        assert re.fullmatch(r"[0-9a-f]{64}", metadata["source_contract_sha256"])
        assert [item["target"] for item in metadata["capabilities"]] == [
            "glm53-q2",
            "glm53-q4",
        ]
        assert [item["filename"] for item in metadata["capabilities"]] == [
            "GLM-5.3-Flash-Q2.gguf",
            "GLM-5.3-Flash-Q4_K.gguf",
        ]
        assert all(
            "fp8" not in item["filename"].casefold()
            and "vision" not in item["filename"].casefold()
            for item in metadata["capabilities"]
        )

        manager.activate(prepared)
        active = resolve_active_runtime("ds4", root=manager.root)
        assert active is not None
        assert active.channel == "glm-5.3-flash"
        assert len(runtime_updates.ds4_glm53_preview_capabilities(active)) == 2
        activated_snapshot = await manager.check(refresh=False)
        activated_ds4 = next(
            item
            for item in activated_snapshot["engines"]
            if item["engine"] == "ds4"
        )
        assert activated_ds4["installed_channel"] == "glm-5.3-flash"
        assert any(
            url.endswith("/antirez/ds4/commits/glm-5.3-flash")
            for url in requests
        )
    finally:
        await manager.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_ds4_glm53_preview_fails_closed_for_wrong_channel_commit_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "d" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jundot/omlx/releases"):
            return httpx.Response(200, json=[])
        if request.url.host == "pypi.org":
            return httpx.Response(200, json={"info": {"version": "0.19.0"}})
        if request.url.path.endswith("/antirez/ds4/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        if request.url.path.endswith(
            "/antirez/ds4/commits/glm-5.3-flash"
        ):
            return httpx.Response(200, json={"sha": revision})
        if request.url.host == "codeload.github.com":
            return httpx.Response(
                200,
                content=_ds4_glm53_preview_source_archive(
                    revision,
                    branch_contract=False,
                ),
            )
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
        with pytest.raises(RuntimeUpdateError, match="unsupported official DS4"):
            await manager.prepare("ds4", channel="arbitrary-branch")
        with pytest.raises(RuntimeUpdateError, match="does not bind glm53-q4"):
            await manager.prepare("ds4", channel="glm-5.3-flash")
    finally:
        await manager.aclose()
        await client.aclose()

    invalid_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"sha": "moving-branch"},
            )
            if request.url.path.endswith(
                "/antirez/ds4/commits/glm-5.3-flash"
            )
            else httpx.Response(404)
        )
    )
    invalid_manager = RuntimeUpdateManager(
        omlx=OMLXConfig(),
        mflux=MFluxConfig(),
        ds4=DS4Config(),
        root=tmp_path / "invalid-runtimes",
        client=invalid_client,
    )
    try:
        with pytest.raises(RuntimeUpdateError, match="no compatible official ds4"):
            await invalid_manager.prepare("ds4", channel="glm-5.3-flash")
    finally:
        await invalid_manager.aclose()
        await invalid_client.aclose()


def test_ds4_glm53_preview_runtime_rejects_wrong_manifest_branch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    source = root / "source"
    source.mkdir(parents=True)
    script = (
        'GLM53_REPO="antirez/glm-5.3-flash-gguf"\n'
        'GLM53_Q2_FILE="GLM-5.3-Flash-Q2.gguf"\n'
        'GLM53_Q4_FILE="GLM-5.3-Flash-Q4_K.gguf"\n'
        'case "$MODEL" in\n'
        'glm53-q2)\nREPO=$GLM53_REPO\nMODEL_FILE=$GLM53_Q2_FILE\n'
        'FORCE_HF_DOWNLOAD=1\n;;\n'
        'glm53-q4)\nREPO=$GLM53_REPO\nMODEL_FILE=$GLM53_Q4_FILE\n'
        'FORCE_HF_DOWNLOAD=1\n;;\nesac\n'
    )
    (source / "download_model.sh").write_text(script, encoding="utf-8")
    binary = source / "ds4-server"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    capabilities, digest = runtime_updates._ds4_glm53_source_contract(source)
    (root / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "ds4",
                "version": "e" * 12,
                "source_revision": "e" * 40,
                "channel": "glm-5.3-flash",
                "source_branch": "main",
                "source_contract_sha256": digest,
                "core_protocol": 1,
                "entrypoint": {
                    "binary": "source/ds4-server",
                    "working_directory": "source",
                },
                "capabilities": list(capabilities),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeUpdateError, match="provenance is incomplete"):
        runtime_updates._load_runtime_directory(root, expected_engine="ds4")


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
                    "--flash-attn",
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
        assert prepared.runtime.compatibility_evidence is not None
        assert prepared.runtime.compatibility_evidence.features == (
            "apple-metal",
            "flash-attention",
        )
        binary = prepared.runtime.path("binary")
        assert binary.name == "llama-server"
        assert binary.stat().st_mode & 0o111

        metadata = json.loads(
            (prepared.runtime.root / "runtime.json").read_text(encoding="utf-8")
        )
        assert metadata["source_sha256"] == prepared.release.sha256
        assert metadata["source_revision"] == "main"
        assert metadata["entrypoint"]["working_directory"] == "runtime"
        assert metadata["compatibility_evidence"] == (
            prepared.runtime.compatibility_evidence.to_manifest()
        )
        assert str(manager.root) not in json.dumps(
            metadata["compatibility_evidence"]
        )

        manager.activate(prepared)
        active = resolve_active_runtime("llama.cpp", root=manager.root)
        assert active is not None
        assert active.version == "b8123"
        assert active.path("binary") == binary
        installed = await manager.installed_status()
        assert installed["llama.cpp"]["compatibility_fingerprint"] == (
            prepared.runtime.compatibility_evidence.compatibility_fingerprint
        )
        assert installed["llama.cpp"]["features"] == [
            "apple-metal",
            "flash-attention",
        ]
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
async def test_llama_cpp_compatibility_rejects_missing_flash_attention_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _requests = _official_handler(llama_version="b8124")
    client = httpx.AsyncClient(transport=transport)

    async def fake_run_checked(
        *argv: str,
        cwd: Path | None = None,
        env=None,
        timeout: float = 3600,
    ) -> str:
        del cwd, env, timeout
        if argv[-1] == "--version":
            return "version: 8124 (deadbeef)"
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
        await manager.check()
        with pytest.raises(RuntimeUpdateError, match=r"missing flags: --flash-attn"):
            await manager.prepare("llama.cpp")
        assert not (manager.root / "llama.cpp" / "b8124").exists()
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
                httpx.Response(200, json=[payload])
                if request.url.path.endswith(
                    "/ggml-org/llama.cpp/releases"
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
