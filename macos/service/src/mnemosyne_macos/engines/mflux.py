"""Managed native MFLUX worker adapter for Apple Silicon image generation."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import signal
import sys
from typing import Any, Awaitable, Callable, Protocol

import httpx

from .base import AdapterError, Deadline, EngineAdapter
from .http import _is_connection_refused
from ..config import MFluxConfig
from ..runtime_updates import _python_subprocess_environment, resolve_active_runtime
from ..scoped_process import wrap_scoped_argv
from ..models import (
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
    ServiceState,
)


class ProcessLike(Protocol):
    pid: int
    returncode: int | None

    async def wait(self) -> int: ...


SpawnProcess = Callable[..., Awaitable[ProcessLike]]


async def _spawn(*argv: str, **kwargs: Any) -> ProcessLike:
    return await asyncio.create_subprocess_exec(*argv, **kwargs)


class MFluxAdapter(EngineAdapter):
    engine = EngineName.MFLUX
    ownership = "managed_process"

    def __init__(
        self,
        config: MFluxConfig,
        *,
        client: httpx.AsyncClient | None = None,
        spawn_process: SpawnProcess = _spawn,
        poll_interval_seconds: float = 0.2,
        runtime_root: str | Path | None = None,
        security_scope_root: str | Path | None = None,
    ) -> None:
        self.config = config
        self.base_url = f"http://{config.host}:{config.port}"
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._spawn_process = spawn_process
        self._poll_interval_seconds = poll_interval_seconds
        self._runtime_root = runtime_root
        self._security_scope_root = security_scope_root
        self._process: ProcessLike | None = None

    def _managed_runtime(self):
        # Managed PyPI packages and worker code may layer over either the
        # configured interpreter or the bundled image interpreter. An explicit
        # Python path chooses the interpreter; it must not bypass an activated
        # dependency update.
        return resolve_active_runtime("mflux", root=self._runtime_root)

    def _python(self) -> str:
        if self.config.python:
            return str(Path(self.config.python).expanduser())
        managed = self._managed_runtime()
        if managed is not None and "python" in managed.entrypoint:
            return str(managed.path("python"))
        configured = os.environ.get(self.config.python_env, "").strip()
        return configured or sys.executable

    def _environment(self) -> dict[str, str]:
        env = _python_subprocess_environment(
            self._python(),
            bundled_python_env=self.config.python_env,
        )
        managed = self._managed_runtime()
        paths: list[str] = []
        if managed is not None:
            if "site_packages" in managed.entrypoint:
                paths.append(str(managed.path("site_packages")))
            paths.append(str(managed.path("worker_path")))
        else:
            source = os.environ.get(self.config.source_path_env, "").strip()
            if source:
                paths.append(source)
        if paths:
            env["PYTHONPATH"] = os.pathsep.join(paths)
        return env

    async def _status(self, *, deadline: Deadline) -> dict[str, Any]:
        remaining = min(deadline.remaining(), self.config.request_timeout_seconds)
        if remaining <= 0:
            raise AdapterError(self.engine, "inspect", "deadline expired", retryable=True)
        response = await self._client.get(f"{self.base_url}/status", timeout=remaining)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise AdapterError(self.engine, "inspect", "status response must be an object")
        return payload

    def _empty_snapshot(self) -> EngineSnapshot:
        return EngineSnapshot(
            engine=self.engine,
            residents=(),
            authoritative=True,
            service_state=ServiceState.STOPPED,
        )

    async def validate_control(self, *, deadline: Deadline) -> EngineSnapshot:
        return await self.inspect(deadline=deadline)

    async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
        if self._process is not None and self._process.returncode is not None:
            self._process = None
        try:
            payload = await self._status(deadline=deadline)
        except httpx.ConnectError as exc:
            if _is_connection_refused(exc) and self._process is None:
                return self._empty_snapshot()
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=False,
                service_state=ServiceState.UNREACHABLE,
                diagnostic=f"MFLUX worker is unreachable: {exc}",
            )
        except (httpx.HTTPError, ValueError, AdapterError) as exc:
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=False,
                service_state=ServiceState.INCOMPATIBLE,
                diagnostic=f"invalid MFLUX worker status: {exc}",
            )
        if payload.get("service") != "mnemosyne-mflux-worker":
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=False,
                service_state=ServiceState.INCOMPATIBLE,
                diagnostic="port is occupied by an unmanaged or incompatible service",
            )
        if self._process is None:
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=False,
                service_state=ServiceState.INCOMPATIBLE,
                diagnostic="MFLUX worker is running but is not owned by this service",
            )
        if not payload.get("loaded"):
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=True,
                service_state=ServiceState.READY,
            )
        model = payload.get("model")
        digest = payload.get("load_config_digest")
        if not isinstance(model, str) or not model or not isinstance(digest, str):
            return EngineSnapshot(
                engine=self.engine,
                residents=(),
                authoritative=False,
                service_state=ServiceState.INCOMPATIBLE,
                diagnostic="MFLUX worker omitted resident identity",
            )
        return EngineSnapshot(
            engine=self.engine,
            residents=(
                ResidentInstance(
                    engine=self.engine,
                    canonical_model_id=model,
                    instance_id=str(self._process.pid),
                    raw={"load_config_digest": digest, **payload},
                ),
            ),
            authoritative=True,
            service_state=ServiceState.READY,
        )

    async def _wait_ready(self, *, deadline: Deadline) -> None:
        while deadline.remaining() > 0:
            if self._process is None or self._process.returncode is not None:
                code = None if self._process is None else self._process.returncode
                raise AdapterError(self.engine, "start", f"worker exited with status {code}")
            try:
                response = await self._client.get(
                    f"{self.base_url}/health",
                    timeout=min(deadline.remaining(), self.config.request_timeout_seconds),
                )
                if response.status_code == 200:
                    payload = response.json()
                    if payload.get("service") == "mnemosyne-mflux-worker":
                        return
                    raise AdapterError(self.engine, "start", "incompatible service answered worker port")
            except httpx.ConnectError:
                pass
            except httpx.HTTPError:
                pass
            await asyncio.sleep(min(self._poll_interval_seconds, deadline.remaining()))
        raise AdapterError(self.engine, "start", "worker startup timed out", retryable=True)

    async def load(self, target: ResolvedTarget, *, deadline: Deadline) -> LoadedHandle:
        if target.key.engine != self.engine:
            raise AdapterError(self.engine, "load", "target belongs to another engine")
        if self._process is not None:
            await self._terminate_owned()
        argv = [
            self._python(),
            "-m",
            "mnemosyne_mflux_worker",
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--parent-pid",
            str(os.getpid()),
        ]
        environment = self._environment()
        service_source = str(Path(__file__).resolve().parents[2])
        spawn_argv = wrap_scoped_argv(
            argv,
            scope_root=self._security_scope_root,
            scope_id=target.scope_id,
            scope_path=target.storage_path,
            # Consume the service-owned bookmark with the service Python. The
            # wrapper then execs the dependency-isolated image interpreter.
            remove_pythonpath=(service_source,),
        )
        if spawn_argv != argv:
            existing_pythonpath = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                value
                for value in (service_source, existing_pythonpath)
                if value
            )
        try:
            self._process = await self._spawn_process(
                *spawn_argv,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            await self._wait_ready(deadline=deadline)
            response = await self._client.post(
                f"{self.base_url}/load",
                json={
                    "alias": target.alias,
                    "model": target.key.canonical_model_id,
                    "load_config_digest": target.key.load_config_digest,
                    **dict(target.load_options),
                },
                # Pipeline construction downloads/opens tens of gigabytes on
                # first use. It is governed by the coordinator's transition
                # deadline, not the short control-request timeout.
                timeout=deadline.remaining(),
            )
            if response.is_error:
                raise AdapterError(
                    self.engine,
                    "load",
                    f"worker returned HTTP {response.status_code}: {response.text[:500]}",
                )
            snapshot = await self.inspect(deadline=deadline)
            if not snapshot.authoritative or len(snapshot.residents) != 1:
                raise AdapterError(self.engine, "load", snapshot.diagnostic or "worker did not load model")
            instance = snapshot.residents[0]
            if (
                instance.canonical_model_id != target.key.canonical_model_id
                or instance.raw.get("load_config_digest") != target.key.load_config_digest
            ):
                raise AdapterError(self.engine, "load", "worker loaded a different target")
            return LoadedHandle(
                target=target,
                instance=instance,
                base_url=self.base_url,
                wire_model=target.wire_model,
            )
        except asyncio.CancelledError:
            await self._terminate_owned()
            raise
        except AdapterError:
            await self._terminate_owned()
            raise
        except Exception as exc:
            await self._terminate_owned()
            raise AdapterError(self.engine, "load", str(exc)) from exc

    async def _terminate_owned(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self.config.shutdown_grace_seconds)
            return
        except asyncio.TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.wait()

    async def unload(self, instance: ResidentInstance, *, deadline: Deadline) -> None:
        if self._process is None:
            raise AdapterError(self.engine, "unload", "worker ownership is unavailable")
        with contextlib.suppress(Exception):
            await self._client.post(
                f"{self.base_url}/unload",
                timeout=min(deadline.remaining(), self.config.request_timeout_seconds),
            )
        await self._terminate_owned()

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        if endpoint != Endpoint.IMAGES_GENERATIONS:
            raise AdapterError(self.engine, "route", f"unsupported endpoint {endpoint}")
        return ProxyRoute(
            base_url=handle.base_url,
            path="/v1/images/generations",
            wire_model=handle.wire_model,
            supports_stream_usage=False,
        )

    async def aclose(self) -> None:
        await self._terminate_owned()
        if self._owns_client:
            await self._client.aclose()


__all__ = ["MFluxAdapter"]
