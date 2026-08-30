from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from .compatibility_catalog import (
    CatalogStore,
    CatalogStoreError,
    VerifiedCatalog,
    built_in_empty_catalog,
)
from .compatibility_catalog_update import (
    CatalogUpdateClient,
    CatalogUpdateResult,
)


class FleetCatalogService:
    """Failure-isolated lifecycle around one verified catalog store.

    The service publishes metadata and advisory placement inputs only. It has
    no reference to Fleet's registry, scheduler, proxy, routes, engines, model
    mappings, or download state.
    """

    def __init__(
        self,
        *,
        store: CatalogStore,
        updater: CatalogUpdateClient,
        update_interval_seconds: float,
        clock: Callable[[], int | float] = time.time,
    ) -> None:
        if (
            isinstance(update_interval_seconds, bool)
            or not isinstance(update_interval_seconds, (int, float))
            or not math.isfinite(float(update_interval_seconds))
            or not 60 <= float(update_interval_seconds) <= 86_400
        ):
            raise ValueError("catalog_update_interval_invalid")
        self._store = store
        self._updater = updater
        self._update_interval_seconds = float(update_interval_seconds)
        self._clock = clock
        self._active = built_in_empty_catalog()
        self._initialized = False
        self._load_error_code: str | None = None
        self._refresh_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        await self._reload_active()
        self._initialized = True

    async def start(self) -> None:
        if not self._initialized:
            raise RuntimeError("catalog_service_uninitialized")
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(
                self._periodic_loop(),
                name="mnemosyne-fleet-catalog-periodic-update",
            )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._updater.aclose()

    async def check(self) -> CatalogUpdateResult:
        result = await self._updater.check()
        await self._reload_active()
        return result

    def active(self) -> VerifiedCatalog:
        return self._active

    async def current(self) -> VerifiedCatalog:
        """Revalidate expiry and rollback state before granting read authority."""

        await self._reload_active()
        return self._active

    def status_payload(self) -> dict[str, Any]:
        active = self._active
        update = asdict(self._updater.status())
        return {
            "schema_version": 1,
            "enabled": True,
            "initialized": self._initialized,
            "available": active.source == "signed",
            "load_error_code": self._load_error_code,
            "active": {
                "source": active.source,
                "catalog_id": active.catalog_id,
                "catalog_version": active.catalog_version,
                "catalog_sequence": active.catalog_sequence,
                "catalog_digest": active.catalog_digest,
                "issued_at": active.issued_at,
                "expires_at": active.expires_at,
                "signing_key_ids": list(active.signing_key_ids),
            },
            "update": update,
        }

    async def _reload_active(self) -> None:
        async with self._refresh_lock:
            now: int | float | None = None
            try:
                now = self._clock()
                active = await asyncio.to_thread(self._store.load, now=now)
            except CatalogStoreError:
                if not self._signed_active_is_current(now):
                    self._active = built_in_empty_catalog()
                self._load_error_code = "catalog_store_unavailable"
            except Exception:
                if not self._signed_active_is_current(now):
                    self._active = built_in_empty_catalog()
                self._load_error_code = "catalog_internal_error"
            else:
                self._active = active
                self._load_error_code = None

    def _signed_active_is_current(self, now: object) -> bool:
        active = self._active
        if active.source != "signed" or active.expires_at is None:
            return False
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            return False
        numeric = float(now)
        return (
            math.isfinite(numeric)
            and active.issued_at <= numeric < active.expires_at
        )

    async def _periodic_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._update_interval_seconds,
                )
            except TimeoutError:
                try:
                    await self.check()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The updater already collapses ordinary failures to fixed
                    # results. This final isolation protects the routing app
                    # from programmer or platform failures in this side path.
                    continue


__all__ = ["FleetCatalogService"]
