"""Composition root for the native macOS coordinator service."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import ipaddress
import os
from pathlib import Path
import time
from typing import Mapping

import httpx

from .config import MacConfig, load_config
from .coordinator import CoordinatorStatus, ResidencyCoordinator
from .engines.base import EngineAdapter
from .engines.ds4 import DS4Adapter
from .engines.lmstudio import LMStudioAdapter
from .engines.mflux import MFluxAdapter
from .engines.omlx import OMLXAdapter
from .models import EngineName, ResolvedTarget
from .usage import UsageEvent
from .usage_delivery import UsageService


class RuntimeConfigurationError(RuntimeError):
    pass


class RestartRequired(RuntimeConfigurationError):
    pass


def _is_loopback(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_exposure(config: MacConfig) -> None:
    if not _is_loopback(config.server.inference_bind):
        key = os.environ.get(config.server.inference_api_key_env, "").strip()
        if not key:
            raise RuntimeConfigurationError(
                "a non-loopback inference bind requires an inference API key"
            )
    if not _is_loopback(config.server.control_bind):
        password = os.environ.get(config.server.control_password_env, "").strip()
        if not password:
            raise RuntimeConfigurationError(
                "a non-loopback control bind requires an admin password"
            )


def build_adapters(config: MacConfig) -> dict[EngineName, EngineAdapter]:
    adapters: dict[EngineName, EngineAdapter] = {}
    if config.engines.lmstudio.enabled:
        adapters[EngineName.LMSTUDIO] = LMStudioAdapter(config.engines.lmstudio)
    if config.engines.omlx.enabled:
        adapters[EngineName.OMLX] = OMLXAdapter(config.engines.omlx)
    if config.engines.ds4.enabled:
        adapters[EngineName.DS4] = DS4Adapter(config.engines.ds4)
    if config.engines.mflux.enabled:
        adapters[EngineName.MFLUX] = MFluxAdapter(config.engines.mflux)
    return adapters


class NativeRuntime:
    """Own shared state used by the inference and control HTTP planes."""

    def __init__(
        self,
        config: MacConfig,
        *,
        config_path: str | Path | None = None,
        env_path: str | Path | None = None,
        adapters: Mapping[EngineName, EngineAdapter] | None = None,
        proxy_client: httpx.AsyncClient | None = None,
        usage_service: UsageService | None = None,
    ) -> None:
        validate_exposure(config)
        self.config = config
        self.config_path = Path(config_path).expanduser() if config_path else None
        self.env_path = Path(env_path).expanduser() if env_path else None
        self.profiles: dict[str, ResolvedTarget] = config.profiles()
        self.adapters = dict(adapters) if adapters is not None else build_adapters(config)
        missing = sorted(
            {profile.key.engine for profile in self.profiles.values()} - self.adapters.keys()
        )
        if missing:
            raise RuntimeConfigurationError(
                f"model profiles reference unconfigured engines: {missing}"
            )
        self.coordinator = ResidencyCoordinator(
            self.adapters,
            queue_timeout_seconds=config.server.swap_queue_timeout_seconds,
            transition_timeout_seconds=config.server.startup_timeout_seconds,
            cleanup_timeout_seconds=max(30, config.server.shutdown_grace_seconds),
            shutdown_grace_seconds=config.server.shutdown_grace_seconds,
        )
        self.proxy_client = proxy_client or httpx.AsyncClient(timeout=None)
        self._owns_proxy_client = proxy_client is None
        self.usage = usage_service or UsageService.open(
            config.paths.state_database,
            config.token_sidecar,
        )
        self._started = False
        self._maintenance_task: asyncio.Task[None] | None = None
        self._reload_lock = asyncio.Lock()
        self.startup_error: str | None = None

    async def start(self, *, raise_on_degraded: bool = False) -> None:
        if self._started:
            return
        self._started = True
        try:
            await self.coordinator.initialize()
        except Exception as exc:
            self.startup_error = str(exc)
            if raise_on_degraded:
                try:
                    await self.stop()
                except Exception:
                    pass
                raise
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="mnemosyne-macos-maintenance"
        )
        await self.usage.start()

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
        try:
            await self.coordinator.shutdown()
        finally:
            try:
                await self.usage.close()
            finally:
                if self._owns_proxy_client:
                    await self.proxy_client.aclose()

    async def _maintenance_loop(self) -> None:
        last_reconcile = 0.0
        while True:
            idle_seconds = self.config.server.idle_unload_seconds
            reconcile_seconds = self.config.server.reconcile_interval_seconds
            interval = min(
                reconcile_seconds,
                30.0 if idle_seconds is None else max(1.0, idle_seconds / 2),
            )
            await asyncio.sleep(interval)
            try:
                now = time.monotonic()
                if now - last_reconcile >= reconcile_seconds:
                    await self.coordinator.audit()
                    self.startup_error = None
                    last_reconcile = now
                if idle_seconds is not None:
                    await self.coordinator.evict_if_idle(float(idle_seconds))
            except Exception:
                # Status exposes coordinator diagnostics. A future logging
                # integration records the traceback without killing upkeep.
                continue

    def resolve(self, alias: str) -> ResolvedTarget:
        try:
            return self.profiles[alias]
        except KeyError as exc:
            raise KeyError(f"unknown model alias '{alias}'") from exc

    async def status(self) -> dict:
        value: CoordinatorStatus = await self.coordinator.status()
        payload = asdict(value)
        payload["status"] = payload["state"]
        payload["in_flight_requests"] = payload["inflight"]
        payload["startup_error"] = self.startup_error
        payload["ports"] = {
            "inference": self.config.server.inference_port,
            "control": self.config.server.control_port,
        }
        payload["token_sidecar"] = await self.usage.status()
        return payload

    async def record_usage(self, event: UsageEvent) -> None:
        await self.usage.record(event)

    def model_list(self) -> list[dict]:
        return [
            {
                "id": target.alias,
                "object": "model",
                "owned_by": "mnemosyne",
                "engine": target.key.engine,
                "upstream_model": target.key.canonical_model_id,
                "capabilities": sorted(endpoint.value for endpoint in target.capabilities),
                "model_kind": target.kind,
                "load_config_digest": target.key.load_config_digest,
            }
            for target in self.profiles.values()
        ]

    async def reload_profiles(self) -> None:
        if self.config_path is None:
            raise RuntimeConfigurationError("runtime has no configured YAML path")
        async with self._reload_lock:
            new_config = load_config(self.config_path, env_path=self.env_path)
            validate_exposure(new_config)
            if new_config.server != self.config.server:
                raise RestartRequired("server settings changed; restart the LaunchAgent")
            if new_config.engines != self.config.engines:
                raise RestartRequired("engine settings changed; restart the LaunchAgent")
            if new_config.paths != self.config.paths:
                raise RestartRequired("storage paths changed; restart the LaunchAgent")
            if new_config.token_sidecar != self.config.token_sidecar:
                raise RestartRequired("token sidecar settings changed; restart the LaunchAgent")
            self.config = new_config
            self.profiles = new_config.profiles()


__all__ = [
    "NativeRuntime",
    "RestartRequired",
    "RuntimeConfigurationError",
    "build_adapters",
    "validate_exposure",
]
