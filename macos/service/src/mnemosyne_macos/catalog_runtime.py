"""Failure-isolated lifecycle for the native signed compatibility catalog.

The catalog is advisory metadata only.  This owner never mutates model
profiles, runtimes, installs, storage, or residency, and every public payload
omits its update endpoint, trust material, and private state location.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import hashlib
import inspect
import logging
import math
import os
from pathlib import Path
import time

import httpx

from .compatibility_catalog import (
    CatalogStore,
    CatalogVerifier,
    TrustedCatalogKey,
    VerifiedCatalog,
    built_in_empty_catalog,
)
from .compatibility_catalog_update import CatalogUpdateClient
from .config import CompatibilityCatalogConfig


logger = logging.getLogger("mnemosyne-macos.catalog")

CATALOG_RUNTIME_SCHEMA_VERSION = 1
NONE_CATALOG_VERSION = "none"
NONE_CATALOG_DIGEST = "sha256:" + hashlib.sha256(b"none").hexdigest()

_PUBLIC_LOCAL_ERRORS = frozenset(
    {
        "catalog_config_anchor_unavailable",
        "catalog_internal_error",
        "catalog_state_unavailable",
        "catalog_trust_unavailable",
        "catalog_updates_disabled",
    }
)


class NativeCatalogRuntime:
    """Own one immutable catalog snapshot and its optional update loop."""

    def __init__(
        self,
        config: CompatibilityCatalogConfig,
        *,
        config_path: str | Path | None,
        environment: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], int | float] = time.time,
        update_interval_seconds: float | None = None,
        on_activation: Callable[
            [VerifiedCatalog], Awaitable[None] | None
        ]
        | None = None,
    ) -> None:
        self.enabled = bool(config.enabled)
        self._clock = clock
        self._on_activation = on_activation
        self._active = built_in_empty_catalog()
        self._snapshot_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._last_outcome = "never"
        self._last_error_code: str | None = None
        self._last_checked_at: int | None = None
        self._store: CatalogStore | None = None
        self._client: CatalogUpdateClient | None = None

        interval = (
            config.update_interval_seconds
            if update_interval_seconds is None
            else update_interval_seconds
        )
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(float(interval))
            or float(interval) <= 0
            or float(interval) > 604_800
        ):
            raise ValueError("catalog_update_interval_invalid")
        self._interval_seconds = float(interval)

        if not self.enabled:
            return
        if config_path is None:
            self._last_error_code = "catalog_config_anchor_unavailable"
            return

        # Catalog state is deliberately anchored beside the active YAML. It
        # never follows the user-configurable SQLite or model-storage roots.
        state_directory = (
            Path(config_path).expanduser().parent
            / "state"
            / "compatibility-catalog"
        )
        source_environment = os.environ if environment is None else environment
        try:
            trusted_keys: dict[str, TrustedCatalogKey] = {}
            for row in config.trusted_keys:
                encoded = source_environment.get(row.public_key_env, "")
                key = TrustedCatalogKey.from_base64url(
                    key_id=row.key_id,
                    public_key=encoded,
                    valid_from=row.valid_from,
                    valid_until=row.valid_until,
                    minimum_catalog_sequence=row.minimum_catalog_sequence,
                    maximum_catalog_sequence=row.maximum_catalog_sequence,
                )
                trusted_keys[key.key_id] = key
            verifier = CatalogVerifier(trusted_keys)
            store = CatalogStore(state_directory, verifier)
            client = CatalogUpdateClient(
                store=store,
                origin=config.update_origin or "",
                path=config.update_path or "",
                total_timeout_seconds=config.total_timeout_seconds,
                connect_timeout_seconds=config.connect_timeout_seconds,
                max_attempts=config.max_attempts,
                retry_delay_seconds=config.retry_delay_seconds,
                transport=transport,
                clock=clock,
            )
        except Exception:
            # Environment names, key bytes, endpoint values, and state paths
            # must never become diagnostics.
            self._last_error_code = "catalog_trust_unavailable"
            return
        self._store = store
        self._client = client

    async def start(self) -> None:
        if self._closed or self._started:
            return
        self._started = True
        if not self.enabled or self._store is None or self._client is None:
            return
        try:
            loaded = await asyncio.to_thread(
                self._store.load,
                now=self._timestamp(),
            )
        except Exception:
            self._last_error_code = "catalog_state_unavailable"
            logger.error("native catalog startup failed: catalog_state_unavailable")
        else:
            async with self._snapshot_lock:
                self._active = loaded
            self._last_error_code = None
        # Network work begins only after NativeRuntime has started all local
        # inference dependencies and explicitly calls this method.
        self._task = asyncio.create_task(
            self._run(),
            name="mnemosyne-macos-catalog-updates",
        )

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                # The HTTP client itself is bounded by the configured deadline;
                # shutdown must not prevent inference cleanup from continuing.
                logger.error("native catalog shutdown failed: catalog_internal_error")

    async def snapshot(self) -> VerifiedCatalog:
        async with self._snapshot_lock:
            return self._active

    async def inventory_identity(self) -> tuple[str, str]:
        snapshot = await self.snapshot()
        if snapshot.source != "signed":
            return NONE_CATALOG_VERSION, NONE_CATALOG_DIGEST
        return snapshot.catalog_version, snapshot.catalog_digest

    async def status(self) -> dict[str, object]:
        snapshot = await self.snapshot()
        client_status = self._client.status() if self._client is not None else None
        version, digest = _public_identity(snapshot)
        if not self.enabled:
            state = "disabled"
        elif self._client is None:
            state = "unavailable"
        elif client_status is not None and client_status.state == "checking":
            state = "checking"
        elif self._closed:
            state = "closed"
        else:
            state = "idle"
        return {
            "schema_version": CATALOG_RUNTIME_SCHEMA_VERSION,
            "enabled": self.enabled,
            "running": self._task is not None and not self._task.done(),
            "state": state,
            "last_outcome": self._last_outcome,
            "last_error_code": self._last_error_code,
            "last_checked_at": self._last_checked_at,
            "active": {
                "source": snapshot.source,
                "catalog_version": version,
                "catalog_sequence": snapshot.catalog_sequence,
                "catalog_digest": digest,
                "issued_at": snapshot.issued_at,
                "expires_at": snapshot.expires_at,
            },
        }

    async def metadata(self) -> dict[str, object]:
        snapshot = await self.snapshot()
        version, digest = _public_identity(snapshot)
        catalog = snapshot.catalog()
        models = catalog.get("logical_models", [])
        recipes = catalog.get("recipes", [])
        if not isinstance(models, list) or not isinstance(recipes, list):
            # This cannot happen for a verified snapshot, but keep this API
            # content-free and bounded if an injected implementation is bad.
            models = []
            recipes = []
        return {
            "schema_version": CATALOG_RUNTIME_SCHEMA_VERSION,
            "active": {
                "source": snapshot.source,
                "catalog_version": version,
                "catalog_sequence": snapshot.catalog_sequence,
                "catalog_digest": digest,
            },
            "models": models,
            "recipes": recipes,
        }

    async def check_now(self) -> dict[str, object]:
        if not self.enabled:
            self._last_error_code = "catalog_updates_disabled"
            return await self._check_payload(
                outcome="disabled",
                changed=False,
                error_code="catalog_updates_disabled",
                checked_at=None,
            )
        if self._client is None or self._store is None:
            code = self._safe_local_error(
                self._last_error_code or "catalog_trust_unavailable"
            )
            self._last_error_code = code
            return await self._check_payload(
                outcome="failed",
                changed=False,
                error_code=code,
                checked_at=None,
            )

        async with self._operation_lock:
            result = await self._client.check()
            self._last_outcome = result.outcome
            self._last_error_code = result.error_code
            self._last_checked_at = result.checked_at
            current = await self.snapshot()
            loaded: VerifiedCatalog | None = None
            snapshot_changed = False
            try:
                loaded = await asyncio.to_thread(
                    self._store.load,
                    now=result.checked_at,
                )
            except Exception:
                # A transient lock/contention failure must preserve the
                # already verified immutable in-memory LKG.
                if result.error_code is None:
                    self._last_outcome = "failed"
                    self._last_error_code = "catalog_state_unavailable"
            else:
                replace = (
                    loaded.source == "signed"
                    or current.source != "signed"
                    or current.expires_at is None
                    or current.expires_at <= result.checked_at
                )
                if replace:
                    snapshot_changed = (
                        loaded.catalog_digest != current.catalog_digest
                        or loaded.source != current.source
                    )
                    async with self._snapshot_lock:
                        self._active = loaded
                elif result.error_code is None and result.changed:
                    # A successful atomic activation must be readable before
                    # the process advertises it. Preserve the prior LKG and
                    # report only the fixed state code.
                    self._last_outcome = "failed"
                    self._last_error_code = "catalog_state_unavailable"
            if snapshot_changed and loaded is not None and loaded.source == "signed":
                await self._notify_activation(loaded)
            applied_update = bool(
                result.changed
                and loaded is not None
                and loaded.source == "signed"
                and loaded.catalog_digest == result.catalog_digest
            )
            return await self._check_payload(
                outcome=self._last_outcome,
                changed=applied_update,
                error_code=self._last_error_code,
                checked_at=self._last_checked_at,
            )

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.check_now()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._last_outcome = "failed"
                self._last_error_code = "catalog_internal_error"
                logger.error("native catalog check failed: catalog_internal_error")
            try:
                await asyncio.sleep(self._interval_seconds)
            except asyncio.CancelledError:
                raise

    async def _notify_activation(self, snapshot: VerifiedCatalog) -> None:
        callback = self._on_activation
        if callback is None:
            return
        try:
            value = callback(snapshot)
            if inspect.isawaitable(value):
                await value
        except Exception:
            # Inventory refresh is best effort and cannot revoke an already
            # verified atomic catalog activation.
            logger.error("native catalog inventory trigger failed: catalog_internal_error")

    async def _check_payload(
        self,
        *,
        outcome: str,
        changed: bool,
        error_code: str | None,
        checked_at: int | None,
    ) -> dict[str, object]:
        snapshot = await self.snapshot()
        version, digest = _public_identity(snapshot)
        return {
            "schema_version": CATALOG_RUNTIME_SCHEMA_VERSION,
            "outcome": outcome,
            "changed": changed,
            "error_code": error_code,
            "checked_at": checked_at,
            "active": {
                "source": snapshot.source,
                "catalog_version": version,
                "catalog_sequence": snapshot.catalog_sequence,
                "catalog_digest": digest,
            },
        }

    def _timestamp(self) -> int:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 4_102_444_800
        ):
            raise ValueError("catalog_clock_invalid")
        return int(value)

    @staticmethod
    def _safe_local_error(code: str) -> str:
        return code if code in _PUBLIC_LOCAL_ERRORS else "catalog_internal_error"


def _public_identity(snapshot: VerifiedCatalog) -> tuple[str, str]:
    if snapshot.source != "signed":
        return NONE_CATALOG_VERSION, NONE_CATALOG_DIGEST
    return snapshot.catalog_version, snapshot.catalog_digest


__all__ = [
    "CATALOG_RUNTIME_SCHEMA_VERSION",
    "NONE_CATALOG_DIGEST",
    "NONE_CATALOG_VERSION",
    "NativeCatalogRuntime",
]
