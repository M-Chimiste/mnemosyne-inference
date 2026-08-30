from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import httpx
import pytest

from mnemosyne_macos.config import MFluxConfig, MacConfig
from mnemosyne_macos.engines.base import Deadline
from mnemosyne_macos.engines.mflux import MFluxAdapter
from mnemosyne_macos.models import EngineName, ServiceState


@pytest.mark.asyncio
async def test_default_mflux_client_ignores_ambient_proxies() -> None:
    adapter = MFluxAdapter(MFluxConfig(enabled=True))
    try:
        assert adapter._client._trust_env is False
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_managed_runtime_overrides_bundled_mflux_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "mflux" / "0.19.0"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "worker" / "mnemosyne_mflux_worker").mkdir(parents=True)
    python = runtime / "bin" / "python3"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    (runtime / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "mflux",
                "version": "0.19.0",
                "source_revision": "abc",
                "core_protocol": 1,
                "entrypoint": {
                    "python": "bin/python3",
                    "worker_path": "worker",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "mflux" / "current.json").write_text(
        json.dumps({"schema_version": 1, "version": "0.19.0"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MNEMOSYNE_MFLUX_PYTHON", "/bundled/python")
    monkeypatch.setenv("MNEMOSYNE_MFLUX_PYTHONPATH", "/bundled/worker")

    adapter = MFluxAdapter(MFluxConfig(enabled=True), runtime_root=tmp_path)
    try:
        assert adapter._python() == str(python)
        assert adapter._environment()["PYTHONPATH"] == str(runtime / "worker")
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_official_pypi_runtime_layers_packages_over_bundled_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "mflux" / "0.20.0"
    (runtime / "site-packages").mkdir(parents=True)
    (runtime / "worker" / "mnemosyne_mflux_worker").mkdir(parents=True)
    (runtime / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "mflux",
                "version": "0.20.0",
                "source_revision": "pypi:mflux==0.20.0",
                "core_protocol": 1,
                "entrypoint": {
                    "site_packages": "site-packages",
                    "worker_path": "worker",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "mflux" / "current.json").write_text(
        json.dumps({"schema_version": 1, "version": "0.20.0"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MNEMOSYNE_MFLUX_PYTHON", "/bundled/python")
    monkeypatch.setenv("MNEMOSYNE_MFLUX_PYTHONPATH", "/bundled/worker")

    adapter = MFluxAdapter(MFluxConfig(enabled=True), runtime_root=tmp_path)
    try:
        assert adapter._python() == "/bundled/python"
        assert adapter._environment()["PYTHONPATH"] == os.pathsep.join(
            [str(runtime / "site-packages"), str(runtime / "worker")]
        )
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_official_pypi_runtime_layers_over_explicit_interpreter(
    tmp_path: Path
) -> None:
    runtime = tmp_path / "mflux" / "0.20.0"
    (runtime / "site-packages").mkdir(parents=True)
    (runtime / "worker" / "mnemosyne_mflux_worker").mkdir(parents=True)
    (runtime / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "mflux",
                "version": "0.20.0",
                "source_revision": "pypi:mflux==0.20.0",
                "core_protocol": 1,
                "entrypoint": {
                    "site_packages": "site-packages",
                    "worker_path": "worker",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "mflux" / "current.json").write_text(
        json.dumps({"schema_version": 1, "version": "0.20.0"}),
        encoding="utf-8",
    )

    adapter = MFluxAdapter(
        MFluxConfig(enabled=True, python="/custom/image/python"),
        runtime_root=tmp_path,
    )
    try:
        assert adapter._python() == "/custom/image/python"
        assert adapter._environment()["PYTHONPATH"] == os.pathsep.join(
            [str(runtime / "site-packages"), str(runtime / "worker")]
        )
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_worker_environment_isolated_from_service_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONHOME", "/service/runtime")
    monkeypatch.setenv("PYTHONPATH", "/service/source:/service/site-packages")
    monkeypatch.setenv("MNEMOSYNE_MFLUX_PYTHONPATH", "/image/worker/source")

    client = httpx.AsyncClient()
    adapter = MFluxAdapter(
        MFluxConfig(enabled=True),
        client=client,
        runtime_root=tmp_path / "runtimes",
    )
    try:
        environment = adapter._environment()
        assert "PYTHONHOME" not in environment
        assert environment["PYTHONPATH"] == "/image/worker/source"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_packaged_worker_retains_bundled_pythonhome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_python = "/Applications/Unified/Python/framework-mnemosyne-image/bin/python3"
    monkeypatch.setenv("PYTHONHOME", "/Applications/Unified/Python/cpython-3.12")
    monkeypatch.setenv("PYTHONPATH", "/service/source:/service/site-packages")
    monkeypatch.setenv("MNEMOSYNE_MFLUX_PYTHON", image_python)
    monkeypatch.setenv("MNEMOSYNE_MFLUX_PYTHONPATH", "/image/worker/source")

    client = httpx.AsyncClient()
    adapter = MFluxAdapter(
        MFluxConfig(enabled=True),
        client=client,
        runtime_root=tmp_path / "runtimes",
    )
    try:
        environment = adapter._environment()
        assert adapter._python() == image_python
        assert environment["PYTHONHOME"] == "/Applications/Unified/Python/cpython-3.12"
        assert environment["PYTHONPATH"] == "/image/worker/source"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_scoped_worker_uses_service_launcher_then_isolated_image_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MNEMOSYNE_MFLUX_PYTHONPATH", "/image/worker/source")
    target = MacConfig.model_validate(
        {
            "engines": {"mflux": {"enabled": True}},
            "storage": {
                "default": "protected",
                "locations": [
                    {
                        "name": "protected",
                        "path": str(tmp_path / "Models"),
                        "scope_id": "a" * 64,
                    }
                ],
            },
            "models": [
                {
                    "alias": "image-model",
                    "engine": "mflux",
                    "model": "publisher/image-model",
                    "storage": "protected",
                    "kind": "image",
                    "image": {"family": "qwen-image"},
                }
            ],
        }
    ).profiles()["image-model"]
    loaded = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal loaded
        if request.url.path == "/health":
            return httpx.Response(200, json={"service": "mnemosyne-mflux-worker"})
        if request.url.path == "/load":
            loaded = True
            return httpx.Response(200, json={"loaded": True})
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "service": "mnemosyne-mflux-worker",
                    "loaded": loaded,
                    "model": target.key.canonical_model_id if loaded else None,
                    "load_config_digest": target.key.load_config_digest if loaded else None,
                },
            )
        return httpx.Response(404)

    class Process:
        pid = 999_999
        returncode: int | None = None

        async def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

    process = Process()
    observed: dict[str, object] = {}

    async def spawn(*argv: str, **kwargs: object) -> Process:
        observed["argv"] = list(argv)
        observed["env"] = kwargs["env"]
        return process

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MFluxAdapter(
        MFluxConfig(enabled=True, python="/image/python"),
        client=client,
        spawn_process=spawn,
        runtime_root=tmp_path / "runtimes",
        security_scope_root=tmp_path / "scopes",
        poll_interval_seconds=0,
    )
    try:
        await adapter.load(target, deadline=Deadline.after(2))
        argv = observed["argv"]
        assert isinstance(argv, list)
        service_source = str(Path(__file__).resolve().parents[1] / "src")
        assert argv[0] == sys.executable
        assert argv[1:3] == ["-m", "mnemosyne_macos.scope_exec"]
        assert ["--remove-pythonpath", service_source] == argv[
            argv.index("--remove-pythonpath") : argv.index("--remove-pythonpath") + 2
        ]
        separator = argv.index("--")
        assert argv[separator + 1] == "/image/python"
        environment = observed["env"]
        assert isinstance(environment, dict)
        assert environment["PYTHONPATH"] == os.pathsep.join(
            [service_source, "/image/worker/source"]
        )
    finally:
        process.returncode = 0
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_connection_refused_is_authoritative_empty_when_no_worker_is_owned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MFluxAdapter(MFluxConfig(enabled=True), client=client)
    try:
        snapshot = await adapter.inspect(deadline=Deadline.after(1))
        assert snapshot.authoritative is True
        assert snapshot.empty is True
        assert snapshot.service_state == ServiceState.STOPPED
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unowned_worker_on_reserved_port_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"service": "mnemosyne-mflux-worker", "loaded": False},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MFluxAdapter(MFluxConfig(enabled=True), client=client)
    try:
        snapshot = await adapter.inspect(deadline=Deadline.after(1))
        assert snapshot.authoritative is False
        assert snapshot.service_state == ServiceState.INCOMPATIBLE
        assert "not owned" in (snapshot.diagnostic or "")
        assert snapshot.engine == EngineName.MFLUX
    finally:
        await client.aclose()
