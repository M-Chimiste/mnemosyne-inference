"""Failure-isolated HTTPS updater for the signed compatibility catalog.

This file is vendored byte-for-byte into the independent Fleet and native Mac
packages. It fetches only the signed catalog envelope and delegates all trust,
rollback, and atomic persistence decisions to ``CatalogStore``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import re
import time
from typing import Callable, Final, Literal
from urllib.parse import urlsplit

import httpx

if __package__ == "compatibility_catalog":
    from .catalog import (
        MAX_CATALOG_BYTES,
        CatalogError,
        CatalogParseError,
        CatalogRollbackError,
        CatalogStore,
        CatalogStoreError,
        CatalogTrustError,
        CatalogValidationError,
        VerifiedCatalog,
    )
else:
    from .compatibility_catalog import (
        MAX_CATALOG_BYTES,
        CatalogError,
        CatalogParseError,
        CatalogRollbackError,
        CatalogStore,
        CatalogStoreError,
        CatalogTrustError,
        CatalogValidationError,
        VerifiedCatalog,
    )


CATALOG_UPDATE_CLIENT_VERSION: Final[int] = 1
DEFAULT_TOTAL_TIMEOUT_SECONDS: Final[float] = 20.0
DEFAULT_CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_MAX_ATTEMPTS: Final[int] = 2
MAX_RESPONSE_HEADER_BYTES: Final[int] = 32 * 1024
MAX_ETAG_BYTES: Final[int] = 128
_USER_AGENT: Final[str] = "Mnemosyne-Catalog-Updater/1"
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ETAG = re.compile(r'^(?:W/)?"[\x21\x23-\x7e]*"$')

CatalogUpdateOutcome = Literal[
    "updated",
    "unchanged",
    "not_modified",
    "failed",
]
CatalogUpdateState = Literal["idle", "checking", "closing", "closed"]

_PUBLIC_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "catalog_update_catalog_expired",
        "catalog_update_catalog_invalid",
        "catalog_update_catalog_not_yet_valid",
        "catalog_update_catalog_untrusted",
        "catalog_update_closed",
        "catalog_update_close_failed",
        "catalog_update_downgrade_rejected",
        "catalog_update_encoding_rejected",
        "catalog_update_http_error",
        "catalog_update_internal_error",
        "catalog_update_media_type_rejected",
        "catalog_update_network_error",
        "catalog_update_redirect_rejected",
        "catalog_update_response_headers_too_large",
        "catalog_update_response_invalid",
        "catalog_update_response_too_large",
        "catalog_update_store_error",
        "catalog_update_timeout",
        "catalog_update_version_conflict",
    }
)


class CatalogUpdateError(RuntimeError):
    """A fixed-code lifecycle error with no endpoint or response detail."""

    def __init__(self, code: str) -> None:
        if code not in _PUBLIC_ERROR_CODES:
            code = "catalog_update_internal_error"
        self.code = code
        super().__init__(code)


class _FetchFailure(Exception):
    def __init__(self, code: str) -> None:
        if code not in _PUBLIC_ERROR_CODES:
            code = "catalog_update_internal_error"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CatalogUpdateResult:
    outcome: CatalogUpdateOutcome
    changed: bool
    checked_at: int
    error_code: str | None
    catalog_sequence: int | None
    catalog_digest: str | None
    catalog_source: Literal["signed", "built_in"] | None


@dataclass(frozen=True, slots=True)
class CatalogUpdateStatus:
    state: CatalogUpdateState
    last_outcome: CatalogUpdateOutcome | Literal["never"]
    last_error_code: str | None
    last_checked_at: int | None
    active_catalog_sequence: int | None
    active_catalog_digest: str | None
    active_catalog_source: Literal["signed", "built_in"] | None
    conditional_request_ready: bool


@dataclass(frozen=True, slots=True)
class _FetchedCatalog:
    body: bytes | None = field(repr=False)
    etag: str | None = field(repr=False)
    not_modified: bool


class CatalogUpdateClient:
    """Single-flight, read-only HTTP client around ``CatalogStore.activate``."""

    def __init__(
        self,
        *,
        store: CatalogStore,
        origin: str,
        path: str,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_delay_seconds: float = 0.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], int | float] = time.time,
    ) -> None:
        self._url = _canonical_catalog_url(origin, path)
        self._total_timeout = _bounded_float(
            total_timeout_seconds,
            minimum=0.05,
            maximum=300.0,
        )
        connect_timeout = _bounded_float(
            connect_timeout_seconds,
            minimum=0.05,
            maximum=self._total_timeout,
        )
        retry_delay = _bounded_float(
            retry_delay_seconds,
            minimum=0.0,
            maximum=5.0,
        )
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 3
        ):
            raise ValueError("catalog_update_policy_invalid")
        if not callable(clock):
            raise ValueError("catalog_update_policy_invalid")

        self._store = store
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._clock = clock
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            auth=None,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": _USER_AGENT,
            },
            timeout=httpx.Timeout(
                self._total_timeout,
                connect=connect_timeout,
                read=self._total_timeout,
                write=self._total_timeout,
                pool=connect_timeout,
            ),
        )
        self._guard = asyncio.Lock()
        self._flight: asyncio.Task[CatalogUpdateResult] | None = None
        self._close_task: asyncio.Task[bool] | None = None
        self._state: CatalogUpdateState = "idle"
        self._last_result: CatalogUpdateResult | None = None
        self._etag: str | None = None

    def status(self) -> CatalogUpdateStatus:
        result = self._last_result
        return CatalogUpdateStatus(
            state=self._state,
            last_outcome="never" if result is None else result.outcome,
            last_error_code=None if result is None else result.error_code,
            last_checked_at=None if result is None else result.checked_at,
            active_catalog_sequence=(
                None if result is None else result.catalog_sequence
            ),
            active_catalog_digest=(
                None if result is None else result.catalog_digest
            ),
            active_catalog_source=(
                None if result is None else result.catalog_source
            ),
            conditional_request_ready=self._etag is not None,
        )

    async def check(self) -> CatalogUpdateResult:
        """Coalesce concurrent checks and isolate all ordinary update failures."""

        async with self._guard:
            if self._state in {"closing", "closed"}:
                return self._closed_result()
            flight = self._flight
            if flight is None:
                self._state = "checking"
                flight = asyncio.create_task(
                    self._check_once(),
                    name="mnemosyne-catalog-update",
                )
                self._flight = flight
                flight.add_done_callback(self._schedule_flight_cleanup)
        result = await asyncio.shield(flight)
        await self._clear_flight(flight)
        return result

    async def aclose(self) -> None:
        """Close after an in-flight check; caller cancellation cannot leak it."""

        async with self._guard:
            if self._state == "closed":
                return
            close_task = self._close_task
            if close_task is None:
                self._state = "closing"
                close_task = asyncio.create_task(
                    self._close_after_flight(self._flight),
                    name="mnemosyne-catalog-update-close",
                )
                self._close_task = close_task
        succeeded = await asyncio.shield(close_task)
        if not succeeded:
            raise CatalogUpdateError("catalog_update_close_failed")

    async def __aenter__(self) -> "CatalogUpdateClient":
        return self

    async def __aexit__(self, _type, _value, _traceback) -> None:
        await self.aclose()

    async def _check_once(self) -> CatalogUpdateResult:
        try:
            checked_at = _coerce_timestamp(self._clock())
        except Exception:
            return self._record_failure(
                0,
                "catalog_update_internal_error",
                None,
            )
        try:
            baseline = await asyncio.to_thread(
                self._store.load,
                now=checked_at,
            )
        except CatalogStoreError:
            return self._record_failure(
                checked_at,
                "catalog_update_store_error",
                None,
            )
        except Exception:
            return self._record_failure(
                checked_at,
                "catalog_update_internal_error",
                None,
            )

        conditional = self._etag if baseline.source == "signed" else None
        try:
            fetched = await self._fetch(conditional)
            if fetched.not_modified:
                if fetched.etag is not None:
                    self._etag = fetched.etag
                return self._record_success(
                    checked_at,
                    "not_modified",
                    False,
                    baseline,
                )
            if fetched.body is None:
                raise _FetchFailure("catalog_update_response_invalid")
            activation = await asyncio.to_thread(
                self._store.activate,
                fetched.body,
                now=checked_at,
            )
            self._etag = fetched.etag
            return self._record_success(
                checked_at,
                "updated" if activation.changed else "unchanged",
                activation.changed,
                activation.catalog,
            )
        except _FetchFailure as exc:
            return self._record_failure(checked_at, exc.code, baseline)
        except CatalogRollbackError as exc:
            code = (
                "catalog_update_downgrade_rejected"
                if exc.code == "catalog_downgrade_rejected"
                else "catalog_update_version_conflict"
            )
            return self._record_failure(checked_at, code, baseline)
        except CatalogTrustError as exc:
            code = {
                "catalog_expired": "catalog_update_catalog_expired",
                "catalog_not_yet_valid": (
                    "catalog_update_catalog_not_yet_valid"
                ),
            }.get(exc.code, "catalog_update_catalog_untrusted")
            return self._record_failure(checked_at, code, baseline)
        except (CatalogParseError, CatalogValidationError):
            return self._record_failure(
                checked_at,
                "catalog_update_catalog_invalid",
                baseline,
            )
        except CatalogStoreError:
            return self._record_failure(
                checked_at,
                "catalog_update_store_error",
                baseline,
            )
        except CatalogError:
            return self._record_failure(
                checked_at,
                "catalog_update_catalog_invalid",
                baseline,
            )
        except Exception:
            return self._record_failure(
                checked_at,
                "catalog_update_internal_error",
                baseline,
            )

    async def _fetch(self, conditional_etag: str | None) -> _FetchedCatalog:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._total_timeout
        last_code = "catalog_update_network_error"
        for attempt in range(self._max_attempts):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _FetchFailure("catalog_update_timeout")
            try:
                async with asyncio.timeout(remaining):
                    return await self._fetch_attempt(conditional_etag)
            except TimeoutError:
                last_code = "catalog_update_timeout"
            except httpx.TimeoutException:
                last_code = "catalog_update_timeout"
            except httpx.TransportError:
                last_code = "catalog_update_network_error"
            if attempt + 1 >= self._max_attempts:
                break
            remaining = deadline - loop.time()
            if self._retry_delay > 0 and remaining > 0:
                try:
                    async with asyncio.timeout(remaining):
                        await asyncio.sleep(self._retry_delay)
                except TimeoutError:
                    raise _FetchFailure("catalog_update_timeout") from None
        raise _FetchFailure(last_code)

    async def _fetch_attempt(
        self,
        conditional_etag: str | None,
    ) -> _FetchedCatalog:
        headers = {}
        if conditional_etag is not None:
            headers["If-None-Match"] = conditional_etag
        async with self._client.stream(
            "GET",
            self._url,
            headers=headers,
        ) as response:
            if not _headers_are_bounded(response.headers):
                raise _FetchFailure(
                    "catalog_update_response_headers_too_large"
                )
            if response.status_code == 304:
                if conditional_etag is None:
                    raise _FetchFailure("catalog_update_response_invalid")
                return _FetchedCatalog(
                    body=None,
                    etag=_safe_etag(response.headers.get("etag")),
                    not_modified=True,
                )
            if 300 <= response.status_code < 400:
                raise _FetchFailure("catalog_update_redirect_rejected")
            if response.status_code != 200:
                raise _FetchFailure("catalog_update_http_error")

            encoding = response.headers.get("content-encoding")
            if encoding is not None and encoding.strip().lower() != "identity":
                raise _FetchFailure("catalog_update_encoding_rejected")
            if not _is_json_media_type(response.headers.get("content-type")):
                raise _FetchFailure("catalog_update_media_type_rejected")
            declared = response.headers.get("content-length")
            if declared is not None:
                if not declared.isascii() or not declared.isdecimal():
                    raise _FetchFailure("catalog_update_response_invalid")
                if int(declared) > MAX_CATALOG_BYTES:
                    raise _FetchFailure("catalog_update_response_too_large")

            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > MAX_CATALOG_BYTES:
                    raise _FetchFailure("catalog_update_response_too_large")
                body.extend(chunk)
            if declared is not None and int(declared) != len(body):
                raise _FetchFailure("catalog_update_response_invalid")
            return _FetchedCatalog(
                body=bytes(body),
                etag=_safe_etag(response.headers.get("etag")),
                not_modified=False,
            )

    def _record_success(
        self,
        checked_at: int,
        outcome: Literal["updated", "unchanged", "not_modified"],
        changed: bool,
        catalog: VerifiedCatalog,
    ) -> CatalogUpdateResult:
        result = CatalogUpdateResult(
            outcome=outcome,
            changed=changed,
            checked_at=checked_at,
            error_code=None,
            catalog_sequence=catalog.catalog_sequence,
            catalog_digest=catalog.catalog_digest,
            catalog_source=catalog.source,
        )
        self._last_result = result
        return result

    def _record_failure(
        self,
        checked_at: int,
        code: str,
        catalog: VerifiedCatalog | None,
    ) -> CatalogUpdateResult:
        if code not in _PUBLIC_ERROR_CODES:
            code = "catalog_update_internal_error"
        result = CatalogUpdateResult(
            outcome="failed",
            changed=False,
            checked_at=checked_at,
            error_code=code,
            catalog_sequence=(
                None if catalog is None else catalog.catalog_sequence
            ),
            catalog_digest=None if catalog is None else catalog.catalog_digest,
            catalog_source=None if catalog is None else catalog.source,
        )
        self._last_result = result
        return result

    def _closed_result(self) -> CatalogUpdateResult:
        previous = self._last_result
        try:
            checked_at = _coerce_timestamp(self._clock())
        except Exception:
            checked_at = 0
        return CatalogUpdateResult(
            outcome="failed",
            changed=False,
            checked_at=checked_at,
            error_code="catalog_update_closed",
            catalog_sequence=(
                None if previous is None else previous.catalog_sequence
            ),
            catalog_digest=(
                None if previous is None else previous.catalog_digest
            ),
            catalog_source=(
                None if previous is None else previous.catalog_source
            ),
        )

    def _schedule_flight_cleanup(
        self,
        flight: asyncio.Task[CatalogUpdateResult],
    ) -> None:
        try:
            asyncio.get_running_loop().create_task(
                self._clear_flight(flight),
                name="mnemosyne-catalog-update-cleanup",
            )
        except RuntimeError:
            pass

    async def _clear_flight(
        self,
        flight: asyncio.Task[CatalogUpdateResult],
    ) -> None:
        async with self._guard:
            if self._flight is flight:
                self._flight = None
                if self._state == "checking":
                    self._state = "idle"

    async def _close_after_flight(
        self,
        flight: asyncio.Task[CatalogUpdateResult] | None,
    ) -> bool:
        succeeded = True
        try:
            if flight is not None:
                try:
                    await asyncio.shield(flight)
                except Exception:
                    succeeded = False
            try:
                await self._client.aclose()
            except Exception:
                succeeded = False
        finally:
            async with self._guard:
                self._flight = None
                self._state = "closed"
        return succeeded


def _canonical_catalog_url(origin: str, path: str) -> str:
    if (
        not isinstance(origin, str)
        or not isinstance(path, str)
        or not origin.isascii()
        or not path.isascii()
        or len(origin) > 512
        or not 1 <= len(path) <= 512
    ):
        raise ValueError("catalog_update_endpoint_invalid")
    parsed = urlsplit(origin)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("catalog_update_endpoint_invalid") from None
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname is None
        or hostname != hostname.lower()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != ""
        or parsed.query != ""
        or parsed.fragment != ""
        or port == 0
        or port == 443
        or not _valid_hostname(hostname)
    ):
        raise ValueError("catalog_update_endpoint_invalid")
    canonical_origin = f"https://{hostname}"
    if port is not None:
        canonical_origin += f":{port}"
    if origin != canonical_origin:
        raise ValueError("catalog_update_endpoint_invalid")

    if (
        not path.startswith("/")
        or path.endswith("/")
        or "//" in path
        or "\\" in path
        or "?" in path
        or "#" in path
        or "%" in path
    ):
        raise ValueError("catalog_update_endpoint_invalid")
    segments = path[1:].split("/")
    if any(
        segment in {"", ".", ".."} or _PATH_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise ValueError("catalog_update_endpoint_invalid")
    return canonical_origin + path


def _valid_hostname(hostname: str) -> bool:
    if not 1 <= len(hostname) <= 253 or hostname.endswith("."):
        return False
    labels = hostname.split(".")
    return all(_HOST_LABEL.fullmatch(label) is not None for label in labels)


def _bounded_float(value: object, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("catalog_update_policy_invalid")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError("catalog_update_policy_invalid")
    return result


def _coerce_timestamp(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("catalog_update_clock_invalid")
    numeric = float(value)
    if not math.isfinite(numeric) or not 0 <= numeric <= 4_102_444_800:
        raise ValueError("catalog_update_clock_invalid")
    return int(numeric)


def _headers_are_bounded(headers: httpx.Headers) -> bool:
    total = 0
    for name, value in headers.multi_items():
        total += len(name.encode("latin-1")) + len(value.encode("latin-1")) + 4
        if total > MAX_RESPONSE_HEADER_BYTES:
            return False
    return True


def _is_json_media_type(value: str | None) -> bool:
    if value is None or len(value) > 128 or not value.isascii():
        return False
    parts = [part.strip().lower() for part in value.split(";")]
    if parts[0] != "application/json":
        return False
    return all(part == "charset=utf-8" for part in parts[1:])


def _safe_etag(value: str | None) -> str | None:
    if (
        value is None
        or len(value.encode("latin-1", errors="ignore")) > MAX_ETAG_BYTES
        or not value.isascii()
        or _ETAG.fullmatch(value) is None
    ):
        return None
    return value


__all__ = [
    "CATALOG_UPDATE_CLIENT_VERSION",
    "CatalogUpdateClient",
    "CatalogUpdateError",
    "CatalogUpdateResult",
    "CatalogUpdateStatus",
]
