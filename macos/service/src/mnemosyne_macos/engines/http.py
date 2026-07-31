"""Shared HTTP plumbing for externally managed native engine services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import os
from enum import StrEnum
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .base import AdapterError, Deadline, EngineAdapter
from ..models import EngineName, ServiceState


class HttpFailureKind(StrEnum):
    DEADLINE = "deadline"
    TIMEOUT = "timeout"
    CONNECTION_REFUSED = "connection_refused"
    NETWORK = "network"
    UNAUTHORIZED = "unauthorized"
    HTTP_STATUS = "http_status"
    INVALID_RESPONSE = "invalid_response"


class HttpAdapterError(AdapterError):
    """Typed HTTP failure retained across adapter inspection boundaries."""

    def __init__(
        self,
        engine: EngineName,
        operation: str,
        detail: str,
        *,
        kind: HttpFailureKind,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        super().__init__(
            engine,
            operation,
            detail,
            retryable=retryable,
        )


@dataclass(frozen=True)
class JsonObjectResponse:
    status_code: int
    payload: dict[str, Any]


def _is_connection_refused(exc: BaseException) -> bool:
    """Recognize loopback refusal without treating every connect error as empty."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        if getattr(current, "errno", None) == errno.ECONNREFUSED:
            return True
        message = str(current).casefold()
        if "connection refused" in message or "errno 61" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


class HttpEngineAdapter(EngineAdapter):
    ownership = "external_service"

    def __init__(
        self,
        *,
        engine: EngineName,
        base_url: str,
        api_key_env: str,
        request_timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self.engine = engine
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.request_timeout_seconds = request_timeout_seconds
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        self.poll_interval_seconds = poll_interval_seconds
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._owns_client = client is None

        host = urlsplit(self.base_url).hostname or ""
        self._loopback = host == "localhost"
        if not self._loopback:
            try:
                import ipaddress

                self._loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                self._loopback = False

    def _api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()

    def _bearer_headers(self) -> dict[str, str]:
        key = self._api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        deadline: Deadline,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        response = await self._request_json_response(
            method,
            path,
            operation=operation,
            deadline=deadline,
            headers=headers,
            json_body=json_body,
            ok_statuses=ok_statuses,
        )
        return response.payload

    async def _request_json_response(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        deadline: Deadline,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (200,),
    ) -> JsonObjectResponse:
        remaining = min(deadline.remaining(), self.request_timeout_seconds)
        if remaining <= 0:
            raise HttpAdapterError(
                self.engine,
                operation,
                "deadline expired",
                kind=HttpFailureKind.DEADLINE,
                retryable=True,
            )
        try:
            response = await self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=dict(headers or {}),
                json=dict(json_body) if json_body is not None else None,
                timeout=remaining,
            )
        except httpx.TimeoutException as exc:
            raise HttpAdapterError(
                self.engine,
                operation,
                f"{type(exc).__name__}: {exc}",
                kind=HttpFailureKind.TIMEOUT,
                retryable=True,
            ) from exc
        except httpx.ConnectError as exc:
            refused = _is_connection_refused(exc)
            raise HttpAdapterError(
                self.engine,
                operation,
                f"{type(exc).__name__}: {exc}",
                kind=(
                    HttpFailureKind.CONNECTION_REFUSED
                    if refused
                    else HttpFailureKind.NETWORK
                ),
                retryable=True,
            ) from exc
        except httpx.NetworkError as exc:
            raise HttpAdapterError(
                self.engine,
                operation,
                f"{type(exc).__name__}: {exc}",
                kind=HttpFailureKind.NETWORK,
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            # Protocol errors, redirect failures, and other transport-level
            # request failures are uncertain. They must never be interpreted
            # as an empty engine inventory.
            raise HttpAdapterError(
                self.engine,
                operation,
                f"{type(exc).__name__}: {exc}",
                kind=HttpFailureKind.NETWORK,
                retryable=True,
            ) from exc
        if response.status_code not in ok_statuses:
            detail = response.text.strip()[:500] or "empty response"
            unauthorized = response.status_code in (401, 403)
            raise HttpAdapterError(
                self.engine,
                operation,
                f"HTTP {response.status_code}: {detail}",
                kind=(
                    HttpFailureKind.UNAUTHORIZED
                    if unauthorized
                    else HttpFailureKind.HTTP_STATUS
                ),
                retryable=response.status_code >= 500,
                status_code=response.status_code,
            )
        if not response.content:
            return JsonObjectResponse(response.status_code, {})
        try:
            payload = response.json()
        except ValueError as exc:
            raise HttpAdapterError(
                self.engine,
                operation,
                "response was not valid JSON",
                kind=HttpFailureKind.INVALID_RESPONSE,
            ) from exc
        if not isinstance(payload, dict):
            raise HttpAdapterError(
                self.engine,
                operation,
                "response JSON must be an object",
                kind=HttpFailureKind.INVALID_RESPONSE,
            )
        return JsonObjectResponse(response.status_code, payload)

    def _failure_state(self, exc: AdapterError) -> tuple[ServiceState, bool]:
        """Map a request failure to service state and inventory authority.

        Only a positively identified connection refusal on the configured
        loopback endpoint proves that no engine service is listening. Timeouts,
        generic network failures, auth failures, and malformed responses never
        establish an empty resident set.
        """

        if isinstance(exc, HttpAdapterError):
            if exc.kind == HttpFailureKind.UNAUTHORIZED:
                return ServiceState.UNAUTHORIZED, False
            if (
                exc.kind == HttpFailureKind.CONNECTION_REFUSED
                and self._loopback
            ):
                return ServiceState.STOPPED, True
            if exc.kind in (
                HttpFailureKind.INVALID_RESPONSE,
                HttpFailureKind.HTTP_STATUS,
            ) and not exc.retryable:
                return ServiceState.INCOMPATIBLE, False
        return ServiceState.UNREACHABLE, False

    async def _poll_delay(self, deadline: Deadline) -> bool:
        remaining = deadline.remaining()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(self.poll_interval_seconds, remaining))
        return deadline.remaining() > 0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
