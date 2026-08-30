"""Failure-isolated outbound synchronization for path-free Mac inventory."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
import json
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .desired_install_protocol import (
    MAX_JOB_ACKNOWLEDGEMENTS,
    DesiredInstallDocument,
    DesiredInstallProtocolError,
    validate_desired_install,
)
from .desired_install_store import DesiredInstallStore, DesiredInstallStoreError
from .fleet_pairing import FleetPairingStore, PairingState
from .mac_inventory import MAX_MAC_INVENTORY_BYTES, MacInventoryError, MacInventoryProducer


MAX_INVENTORY_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DESIRED_JOBS_PER_RESPONSE = 64
INVENTORY_SYNC_INTERVAL_SECONDS = 30.0


class InventorySyncErrorCode(StrEnum):
    NOT_PAIRED = "inventory_not_paired"
    LOCAL_UNAVAILABLE = "inventory_local_unavailable"
    HUB_ORIGIN_INVALID = "inventory_hub_origin_invalid"
    HUB_UNAVAILABLE = "inventory_hub_unavailable"
    HUB_REDIRECT_REFUSED = "inventory_hub_redirect_refused"
    HUB_AUTHENTICATION_REJECTED = "inventory_hub_authentication_rejected"
    HUB_RESPONSE_TOO_LARGE = "inventory_hub_response_too_large"
    HUB_RESPONSE_INVALID = "inventory_hub_response_invalid"
    ACK_MISMATCH = "inventory_ack_mismatch"
    DESIRED_JOBS_UNSUPPORTED = "inventory_desired_jobs_unsupported"
    DESIRED_JOBS_REJECTED = "inventory_desired_jobs_rejected"


class InventorySyncError(RuntimeError):
    """Fixed-code outbound error that never embeds source/response data."""

    def __init__(self, code: InventorySyncErrorCode, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class InventorySyncStatus:
    running: bool
    state: str
    inventory_instance_id: str
    pending_sequence: int | None
    last_acknowledged_sequence: int | None
    last_attempt_at: float | None
    last_success_at: float | None
    last_error_code: InventorySyncErrorCode | None

    def public_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "running": self.running,
            "state": self.state,
            "inventory_instance_id": self.inventory_instance_id,
            "pending_sequence": self.pending_sequence,
            "last_acknowledged_sequence": self.last_acknowledged_sequence,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "last_error_code": (
                self.last_error_code.value if self.last_error_code is not None else None
            ),
        }


class MacInventorySyncClient:
    """Retry one exact observation until the Hub acknowledges its identity."""

    def __init__(
        self,
        producer: MacInventoryProducer,
        pairing_store: FleetPairingStore,
        *,
        desired_install_store: DesiredInstallStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        interval_seconds: float = INVENTORY_SYNC_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("inventory sync interval must be positive")
        self.producer = producer
        self.pairing_store = pairing_store
        self._desired_install_store = desired_install_store
        self.interval_seconds = float(interval_seconds)
        timeout = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            verify=True,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            transport=transport,
        )
        # Long collection/network work is serialized independently from the
        # short status lock so the local control endpoint remains responsive.
        self._operation_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closed = False
        self._pending: dict[str, object] | None = None
        self._pending_pairing: tuple[str, int] | None = None
        self._pending_acknowledgement_cursor: str | None = None
        self._acknowledgement_cursor: str | None = None
        self._acknowledgement_pairing: tuple[str, int] | None = None
        self._last_acknowledged_sequence: int | None = None
        self._last_attempt_at: float | None = None
        self._last_success_at: float | None = None
        self._last_error_code: InventorySyncErrorCode | None = None
        self._syncing = False

    def attach_desired_install_store(self, store: DesiredInstallStore) -> None:
        """Attach an initialized passive journal before the sync loop starts."""

        if self._task is not None or self._pending is not None or self._closed:
            raise RuntimeError("inventory sync has already started")
        self._desired_install_store = store

    async def start(self) -> None:
        if self._closed or self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(),
            name="mnemosyne-mac-inventory-sync",
        )

    def trigger(self) -> None:
        if not self._closed:
            self._wake.set()

    async def stop(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            # Do not set the wake event before cancellation. If the inner
            # Event.wait completes in the same turn, asyncio.wait_for can
            # consume the cancellation and the loop would start another wait.
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        else:
            self._wake.set()
        await self._client.aclose()

    async def status(self) -> InventorySyncStatus:
        async with self._state_lock:
            return self._status_locked()

    async def inspection(self) -> dict[str, object]:
        async with self._state_lock:
            document = self._pending or self.producer.last_document
            return {
                "schema_version": 1,
                "sync": self._status_locked().public_payload(),
                "inventory": _copy(document),
            }

    async def sync_once(self) -> InventorySyncStatus:
        async with self._operation_lock:
            if self._closed:
                raise InventorySyncError(
                    InventorySyncErrorCode.LOCAL_UNAVAILABLE,
                    retryable=False,
                )
            async with self._state_lock:
                self._syncing = True
                self._last_attempt_at = time.time()
            try:
                pairing = await self.pairing_store.status()
                if (
                    pairing.state != PairingState.PAIRED
                    or pairing.pairing_id is None
                    or pairing.credential_epoch is None
                    or pairing.hub_origin is None
                ):
                    async with self._state_lock:
                        self._pending = None
                        self._pending_pairing = None
                    raise InventorySyncError(
                        InventorySyncErrorCode.NOT_PAIRED,
                        retryable=False,
                    )
                origin = _verified_https_origin(pairing.hub_origin)
                generation = int(pairing.credential_epoch)
                pairing_key = (pairing.pairing_id, generation)
                async with self._state_lock:
                    pending = self._pending
                    pending_pairing = self._pending_pairing
                if pending is None or pending_pairing != pairing_key:
                    pending = await self.producer.next_document(
                        pairing_id=pairing.pairing_id,
                        credential_generation=generation,
                    )
                    next_cursor: str | None = None
                    desired_store = self._desired_install_store
                    if desired_store is not None:
                        if self._acknowledgement_pairing != pairing_key:
                            self._acknowledgement_cursor = None
                            self._acknowledgement_pairing = pairing_key
                        page = await desired_store.acknowledgement_page(
                            after_job_id=self._acknowledgement_cursor,
                            limit=MAX_JOB_ACKNOWLEDGEMENTS,
                        )
                        acknowledgements: list[dict[str, Any]] = []
                        for item in page.acknowledgements:
                            record = await desired_store.get(item.job_id)
                            if record is None:
                                continue
                            if (
                                record.document.pairing_id
                                != pairing.pairing_id
                                or record.document.credential_generation
                                != generation
                            ):
                                continue
                            acknowledgements.append(
                                _copy(record.acknowledgement().value)
                            )
                        pending["job_acknowledgements"] = acknowledgements
                        next_cursor = page.next_cursor
                    async with self._state_lock:
                        self._pending = pending
                        self._pending_pairing = pairing_key
                        self._pending_acknowledgement_cursor = next_cursor
                credentials = await self.pairing_store.staged_credentials()
                await self._send(
                    origin=origin,
                    pairing_id=pairing.pairing_id,
                    bearer=credentials.management_key,
                    document=pending,
                )
                async with self._state_lock:
                    self._last_acknowledged_sequence = int(
                        pending["inventory_sequence"]
                    )
                    self._acknowledgement_cursor = (
                        self._pending_acknowledgement_cursor
                    )
                    self._last_success_at = time.time()
                    self._last_error_code = None
                    self._pending = None
                    self._pending_pairing = None
                    self._pending_acknowledgement_cursor = None
            except InventorySyncError as exc:
                async with self._state_lock:
                    self._last_error_code = exc.code
                raise
            except MacInventoryError:
                async with self._state_lock:
                    self._last_error_code = InventorySyncErrorCode.LOCAL_UNAVAILABLE
                raise InventorySyncError(
                    InventorySyncErrorCode.LOCAL_UNAVAILABLE,
                    retryable=True,
                ) from None
            except DesiredInstallStoreError:
                async with self._state_lock:
                    self._last_error_code = InventorySyncErrorCode.LOCAL_UNAVAILABLE
                raise InventorySyncError(
                    InventorySyncErrorCode.LOCAL_UNAVAILABLE,
                    retryable=True,
                ) from None
            except Exception:
                # Pairing store failures and private-environment failures are
                # intentionally collapsed; neither a secret nor a local path
                # may appear in this status or the service log.
                async with self._state_lock:
                    self._last_error_code = InventorySyncErrorCode.LOCAL_UNAVAILABLE
                raise InventorySyncError(
                    InventorySyncErrorCode.LOCAL_UNAVAILABLE,
                    retryable=True,
                ) from None
            finally:
                async with self._state_lock:
                    self._syncing = False
            return await self.status()

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.sync_once()
            except InventorySyncError:
                pass
            if self._closed:
                return
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    def _status_locked(self) -> InventorySyncStatus:
        if self._syncing:
            state = "syncing"
        elif self._last_error_code == InventorySyncErrorCode.NOT_PAIRED:
            state = "not_paired"
        elif self._last_error_code is not None:
            state = "retrying"
        elif self._last_success_at is not None:
            state = "healthy"
        else:
            state = "idle"
        return InventorySyncStatus(
            running=self._task is not None and not self._task.done(),
            state=state,
            inventory_instance_id=self.producer.instance_id,
            pending_sequence=(
                int(self._pending["inventory_sequence"])
                if self._pending is not None
                else None
            ),
            last_acknowledged_sequence=self._last_acknowledged_sequence,
            last_attempt_at=self._last_attempt_at,
            last_success_at=self._last_success_at,
            last_error_code=self._last_error_code,
        )

    async def _send(
        self,
        *,
        origin: str,
        pairing_id: str,
        bearer: str,
        document: Mapping[str, object],
    ) -> None:
        body = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(body) > MAX_MAC_INVENTORY_BYTES:
            raise InventorySyncError(
                InventorySyncErrorCode.LOCAL_UNAVAILABLE,
                retryable=False,
            )
        request = self._client.build_request(
            "POST",
            f"{origin}/fleet/management/v1/pairings/{pairing_id}/inventory-sync",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Content-Encoding": "identity",
                "Cache-Control": "no-store",
            },
            content=body,
        )
        try:
            response = await self._client.send(request, stream=True)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError):
            raise InventorySyncError(
                InventorySyncErrorCode.HUB_UNAVAILABLE,
                retryable=True,
            ) from None
        try:
            if 300 <= response.status_code < 400:
                raise InventorySyncError(
                    InventorySyncErrorCode.HUB_REDIRECT_REFUSED,
                    retryable=False,
                )
            if response.status_code in {401, 403}:
                raise InventorySyncError(
                    InventorySyncErrorCode.HUB_AUTHENTICATION_REJECTED,
                    retryable=False,
                )
            if response.status_code != 200:
                raise InventorySyncError(
                    InventorySyncErrorCode.HUB_UNAVAILABLE,
                    retryable=response.status_code in {408, 409, 425, 429, 500, 502, 503, 504},
                )
            if response.headers.get("content-encoding", "identity").casefold() != "identity":
                raise InventorySyncError(
                    InventorySyncErrorCode.HUB_RESPONSE_INVALID,
                    retryable=False,
                )
            media_type = (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .casefold()
            )
            if media_type != "application/json":
                raise InventorySyncError(
                    InventorySyncErrorCode.HUB_RESPONSE_INVALID,
                    retryable=False,
                )
            encoded = bytearray()
            async for chunk in response.aiter_bytes():
                encoded.extend(chunk)
                if len(encoded) > MAX_INVENTORY_RESPONSE_BYTES:
                    raise InventorySyncError(
                        InventorySyncErrorCode.HUB_RESPONSE_TOO_LARGE,
                        retryable=False,
                    )
            value = _strict_json(bytes(encoded))
            _validate_ack(value, document)
            desired = _parse_desired_jobs(value["desired_jobs"])
            if desired and self._desired_install_store is None:
                raise InventorySyncError(
                    InventorySyncErrorCode.DESIRED_JOBS_UNSUPPORTED,
                    retryable=False,
                )
            if desired:
                await self._receive_desired_jobs(
                    desired,
                    document=document,
                )
        finally:
            await response.aclose()

    async def _receive_desired_jobs(
        self,
        desired: tuple[DesiredInstallDocument, ...],
        *,
        document: Mapping[str, object],
    ) -> None:
        """Journal authenticated intents without executing or approving them."""

        store = self._desired_install_store
        if store is None:  # guarded by the caller; keeps the authority local
            raise InventorySyncError(
                InventorySyncErrorCode.DESIRED_JOBS_UNSUPPORTED,
                retryable=False,
            )
        try:
            pairing = await self.pairing_store.status()
            if (
                pairing.state != PairingState.PAIRED
                or pairing.pairing_id != document["pairing_id"]
                or pairing.credential_epoch
                != document["credential_generation"]
            ):
                raise DesiredInstallStoreError(
                    "desired_install_recipient_mismatch"
                )
            for item in desired:
                await store.receive(
                    item,
                    expected_pairing_id=str(document["pairing_id"]),
                    expected_credential_generation=int(
                        document["credential_generation"]
                    ),
                    expected_inventory_instance_id=str(
                        document["inventory_instance_id"]
                    ),
                    # The Hub acknowledgement above makes this exact sent
                    # observation the current acknowledged upper bound.
                    current_inventory_sequence=int(
                        document["inventory_sequence"]
                    ),
                )
        except DesiredInstallStoreError:
            raise InventorySyncError(
                InventorySyncErrorCode.DESIRED_JOBS_REJECTED,
                retryable=True,
            ) from None


def _verified_https_origin(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise InventorySyncError(
            InventorySyncErrorCode.HUB_ORIGIN_INVALID,
            retryable=False,
        )
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise InventorySyncError(
            InventorySyncErrorCode.HUB_ORIGIN_INVALID,
            retryable=False,
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise InventorySyncError(
            InventorySyncErrorCode.HUB_ORIGIN_INVALID,
            retryable=False,
        )
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    suffix = f":{port}" if port is not None else ""
    return f"https://{host}{suffix}"


def _strict_json(body: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise InventorySyncError(
            InventorySyncErrorCode.HUB_RESPONSE_INVALID,
            retryable=False,
        ) from None
    if not isinstance(value, dict):
        raise InventorySyncError(
            InventorySyncErrorCode.HUB_RESPONSE_INVALID,
            retryable=False,
        )
    return value


def _validate_ack(
    value: Mapping[str, Any],
    document: Mapping[str, object],
) -> None:
    if set(value) != {"schema_version", "ack", "desired_jobs"} or value.get("schema_version") != 1:
        raise InventorySyncError(
            InventorySyncErrorCode.HUB_RESPONSE_INVALID,
            retryable=False,
        )
    ack = value.get("ack")
    if not isinstance(ack, dict) or set(ack) != {
        "pairing_id",
        "credential_generation",
        "inventory_instance_id",
        "inventory_sequence",
    }:
        raise InventorySyncError(
            InventorySyncErrorCode.HUB_RESPONSE_INVALID,
            retryable=False,
        )
    if not isinstance(value.get("desired_jobs"), list):
        raise InventorySyncError(
            InventorySyncErrorCode.HUB_RESPONSE_INVALID,
            retryable=False,
        )
    expected = {
        "pairing_id": document["pairing_id"],
        "credential_generation": document["credential_generation"],
        "inventory_instance_id": document["inventory_instance_id"],
        "inventory_sequence": document["inventory_sequence"],
    }
    if ack != expected:
        raise InventorySyncError(
            InventorySyncErrorCode.ACK_MISMATCH,
            retryable=False,
        )


def _parse_desired_jobs(value: object) -> tuple[DesiredInstallDocument, ...]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_DESIRED_JOBS_PER_RESPONSE
    ):
        raise InventorySyncError(
            InventorySyncErrorCode.HUB_RESPONSE_INVALID,
            retryable=False,
        )
    result: list[DesiredInstallDocument] = []
    seen: set[str] = set()
    try:
        for item in value:
            document = validate_desired_install(item)
            if document.job_id in seen:
                raise DesiredInstallProtocolError("desired_install_invalid")
            seen.add(document.job_id)
            result.append(document)
    except DesiredInstallProtocolError:
        raise InventorySyncError(
            InventorySyncErrorCode.HUB_RESPONSE_INVALID,
            retryable=False,
        ) from None
    return tuple(result)


def _copy(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


__all__ = [
    "INVENTORY_SYNC_INTERVAL_SECONDS",
    "InventorySyncError",
    "InventorySyncErrorCode",
    "InventorySyncStatus",
    "MacInventorySyncClient",
    "MAX_DESIRED_JOBS_PER_RESPONSE",
    "MAX_INVENTORY_RESPONSE_BYTES",
]
