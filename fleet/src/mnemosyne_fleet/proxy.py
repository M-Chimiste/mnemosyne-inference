from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .scheduler import (
    CapabilityError,
    FleetBusyError,
    Reservation,
    Scheduler,
    UnknownModelError,
)
from .store import FleetStore, RouteRecord


_REQUEST_HEADER_DENYLIST = {
    "authorization",
    "connection",
    "content-length",
    "content-encoding",
    "cookie",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "x-api-key",
    "x-mnemosyne-error",
    "x-mnemosyne-fleet-route",
}
_RESPONSE_HEADER_DENYLIST = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "set-cookie",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "x-mnemosyne-error",
}
_NODE_BUSY_RETRY = object()


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    retry_after: int | None = None,
) -> JSONResponse:
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "message": message,
                "type": "fleet_error",
                "code": code,
            }
        },
    )


class FleetProxy:
    def __init__(
        self,
        *,
        scheduler: Scheduler,
        store: FleetStore,
        client: httpx.AsyncClient,
        max_body_bytes: int,
    ) -> None:
        self._scheduler = scheduler
        self._store = store
        self._client = client
        self._max_body_bytes = max_body_bytes
        self._log = logging.getLogger("mnemosyne-fleet.proxy")

    def _response_headers(
        self,
        response: httpx.Response,
        *,
        decoded: bool,
    ) -> dict[str, str]:
        denylist = _RESPONSE_HEADER_DENYLIST
        if decoded:
            # httpx decodes buffered response bodies. Forwarding the old
            # content-encoding would make the downstream client decode the
            # already-decoded bytes a second time.
            denylist = denylist | {"content-encoding"}
        return {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in denylist
            and not key.lower().startswith("x-mnemosyne-")
        }

    async def _read_body(self, request: Request) -> bytes:
        output = bytearray()
        async for chunk in request.stream():
            output.extend(chunk)
            if len(output) > self._max_body_bytes:
                raise OverflowError
        return bytes(output)

    async def handle(self, request: Request, *, capability: str):
        try:
            raw_body = await self._read_body(request)
        except OverflowError:
            return error_response(413, "request_too_large", "Request body is too large.")
        try:
            payload = json.loads(
                raw_body,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite JSON number")
                ),
                parse_float=_finite_float,
            )
            # Python's decoder accepts escaped lone UTF-16 surrogates even
            # though they cannot be encoded as UTF-8. Prove the complete
            # decoded object is valid UTF-8 before acquiring any scheduler
            # reservation so a later wire rewrite cannot strand capacity.
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            UnicodeEncodeError,
            RecursionError,
            ValueError,
        ):
            return error_response(400, "invalid_json", "Request body must be valid JSON.")
        if not isinstance(payload, dict):
            return error_response(400, "invalid_request", "Request body must be a JSON object.")
        public_model = payload.get("model")
        if not isinstance(public_model, str) or not public_model.strip():
            return error_response(400, "model_required", "A non-empty model is required.")

        excluded_nodes: set[str] = set()
        all_retries_were_node_busy = True
        max_attempts = max(1, self._scheduler.node_count)
        for _attempt in range(max_attempts):
            try:
                reservation = await self._scheduler.acquire(
                    public_model=public_model,
                    capability=capability,
                    excluded_nodes=frozenset(excluded_nodes),
                )
            except UnknownModelError:
                return error_response(404, "model_not_found", "The requested model is unavailable.")
            except CapabilityError:
                return error_response(
                    400,
                    "capability_not_supported",
                    "The requested model does not support this endpoint.",
                )
            except FleetBusyError as exc:
                return error_response(
                    429,
                    exc.code,
                    "Fleet capacity is currently unavailable.",
                    retry_after=exc.retry_after,
                )

            result = await self._attempt(
                request=request,
                payload=payload,
                reservation=reservation,
            )
            if result is _NODE_BUSY_RETRY:
                excluded_nodes.add(reservation.node_id)
                continue
            if result is not None:
                return result
            all_retries_were_node_busy = False
            excluded_nodes.add(reservation.node_id)

        if excluded_nodes and all_retries_were_node_busy:
            return error_response(
                429,
                "fleet_capacity_busy",
                "Every eligible node is currently at capacity.",
                retry_after=1,
            )
        return error_response(
            503,
            "no_eligible_node",
            "No eligible node accepted the request.",
            retry_after=1,
        )

    async def _attempt(
        self,
        *,
        request: Request,
        payload: dict[str, object],
        reservation: Reservation,
    ):
        node = self._scheduler.enrollment(reservation.node_id)
        started_at = time.time()
        owner = _RouteOwnership(
            proxy=self,
            reservation=reservation,
            record=RouteRecord(
                route_id=reservation.route_id,
                started_at=started_at,
                completed_at=None,
                public_model=reservation.public_model,
                deployment_id=reservation.deployment_id,
                node_id=reservation.node_id,
                instance_id=reservation.instance_id,
                endpoint=request.url.path,
                queue_ms=reservation.queue_ms,
                response_ms=None,
                status_code=None,
                failure_code=None,
            ),
        )
        try:
            await owner.start()
        except asyncio.CancelledError:
            await owner.complete(
                status_code=None,
                failure_code="client_cancelled",
            )
            raise

        forwarded = dict(payload)
        forwarded["model"] = reservation.local_alias
        body = json.dumps(
            forwarded,
            # The request has already passed the UTF-8 scalar check. ASCII
            # escaping is defense in depth for the node-local alias inserted
            # after that check.
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _REQUEST_HEADER_DENYLIST
            and not key.lower().startswith("x-forwarded-")
            and not key.lower().startswith("x-mnemosyne-")
        }
        headers["Authorization"] = f"Bearer {node.inference_token}"
        headers["Content-Type"] = "application/json"
        headers["X-Mnemosyne-Fleet-Route"] = reservation.route_id
        try:
            upstream_url = f"{node.url}{request.url.path}"
            if request.url.query:
                upstream_url = f"{upstream_url}?{request.url.query}"
            upstream_request = self._client.build_request(
                method=request.method,
                url=upstream_url,
                headers=headers,
                content=body,
            )
        except Exception:
            await owner.complete(
                status_code=None,
                failure_code="gateway_request_build_failed",
            )
            return error_response(
                502,
                "upstream_failure",
                "The selected node request could not be created.",
            )

        try:
            response = await self._client.send(upstream_request, stream=True)
        except asyncio.CancelledError:
            await owner.complete(
                status_code=None,
                failure_code="client_cancelled",
            )
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            await owner.complete(
                status_code=None,
                failure_code="connect_before_headers",
            )
            return None
        except httpx.HTTPError:
            await owner.complete(
                status_code=None,
                failure_code="ambiguous_upstream_failure",
            )
            return error_response(
                502,
                "upstream_failure",
                "The selected node failed before returning a response.",
            )
        except Exception:
            await owner.complete(
                status_code=None,
                failure_code="gateway_proxy_error",
            )
            return error_response(
                502,
                "upstream_failure",
                "The selected node failed before returning a response.",
            )

        owner.attach_response(response)
        if response.status_code == 429 and self._is_node_busy(response):
            # The manager-owned proof header establishes rejection before
            # engine work. Do not wait for or buffer an arbitrary error body
            # before returning this reservation and trying another node.
            await owner.complete(
                status_code=429,
                failure_code="node_busy",
            )
            return _NODE_BUSY_RETRY

        # Any other response headers are an admission boundary. This timestamp
        # lets a later-started snapshot account for the request without
        # incorrectly forgetting reservations that were still pending when a
        # poll began.
        reservation.mark_admitted()
        try:
            response_was_consumed = response.is_stream_consumed
        except asyncio.CancelledError:
            # complete() is idempotent, including when cancellation is
            # delivered repeatedly while its shielded cleanup is running.
            await owner.complete(
                status_code=response.status_code,
                failure_code="client_cancelled",
            )
            raise
        except Exception:
            await owner.complete(
                status_code=response.status_code,
                failure_code="upstream_response_error",
            )
            return error_response(
                502,
                "upstream_failure",
                "The selected node response could not be processed.",
            )

        async def body_stream() -> AsyncIterator[bytes]:
            try:
                if response_was_consumed:
                    if response.content:
                        yield response.content
                else:
                    async for chunk in response.aiter_raw():
                        yield chunk
            except asyncio.CancelledError:
                owner.note_stream_failure("client_cancelled")
                raise
            except Exception:
                owner.note_stream_failure("upstream_stream_error")
                raise

        try:
            return _OwnedStreamingResponse(
                body_stream(),
                owner=owner,
                status_code=response.status_code,
                headers=self._response_headers(
                    response,
                    decoded=response_was_consumed,
                ),
                media_type=None,
            )
        except Exception:
            await owner.complete(
                status_code=response.status_code,
                failure_code="gateway_response_build_failed",
            )
            return error_response(
                502,
                "upstream_failure",
                "The selected node response could not be processed.",
            )

    def _is_node_busy(self, response: httpx.Response) -> bool:
        # A JSON error body may originate in an engine. Only the enrolled
        # manager's reserved header proves rejection happened before work.
        return response.headers.get("x-mnemosyne-error") == "node_busy"

    async def _finish_owned(
        self,
        owner: "_RouteOwnership",
        *,
        status_code: int | None,
        failure_code: str | None,
    ) -> None:
        # Return scheduler capacity before potentially slow metadata or socket
        # cleanup. Reservation.release itself is cancellation-safe.
        await owner.reservation.release()

        # Close the upstream socket before SQLite work. In particular, a
        # manager-proven node_busy response may have an unbounded body that
        # Fleet intentionally never reads; slow route metadata must not keep
        # that response attached to the connection pool.
        response = owner.response
        if response is not None:
            try:
                await response.aclose()
            except BaseException:
                self._log.warning("upstream response close failed")

        route_started = await owner.route_started()
        try:
            if route_started:
                await self._store.finish_route(
                    owner.reservation.route_id,
                    status_code=status_code,
                    failure_code=failure_code,
                )
        except BaseException:
            self._log.warning("route metadata completion failed")


class _RouteOwnership:
    """Exactly-once, cancellation-safe ownership of a route reservation."""

    def __init__(
        self,
        *,
        proxy: FleetProxy,
        reservation: Reservation,
        record: RouteRecord,
    ) -> None:
        self._proxy = proxy
        self.reservation = reservation
        self._record = record
        self._start_task: asyncio.Task[None] | None = None
        self._completion_task: asyncio.Task[None] | None = None
        self.response: httpx.Response | None = None
        self.stream_failure_code: str | None = None

    async def start(self) -> None:
        task = asyncio.create_task(
            self._proxy._store.start_route(self._record),
            name=f"fleet-route-start-{self.reservation.route_id}",
        )
        self._start_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._proxy._log.warning("route metadata start failed")

    def attach_response(self, response: httpx.Response) -> None:
        self.response = response

    def note_stream_failure(self, code: str) -> None:
        if self.stream_failure_code is None:
            self.stream_failure_code = code

    async def route_started(self) -> bool:
        task = self._start_task
        if task is None:
            return False
        try:
            await task
        except BaseException:
            return False
        return True

    async def complete(
        self,
        *,
        status_code: int | None,
        failure_code: str | None,
    ) -> None:
        task = self._completion_task
        if task is None:
            task = asyncio.create_task(
                self._proxy._finish_owned(
                    self,
                    status_code=status_code,
                    failure_code=failure_code,
                ),
                name=f"fleet-route-finish-{self.reservation.route_id}",
            )
            self._completion_task = task
        # The cleanup task owns the reservation even if this request task is
        # cancelled again while waiting for it.
        await asyncio.shield(task)


class _OwnedStreamingResponse(StreamingResponse):
    """Streaming response whose outer ASGI lifetime owns route cleanup.

    Cleanup therefore still runs if cancellation lands after upstream headers
    but before the body iterator gets its first turn.
    """

    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        owner: _RouteOwnership,
        status_code: int,
        headers: dict[str, str],
        media_type: str | None,
    ) -> None:
        super().__init__(
            content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )
        self._owner = owner

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            self._owner.note_stream_failure("client_cancelled")
            raise
        except Exception:
            self._owner.note_stream_failure("stream_delivery_error")
            raise
        finally:
            await self._owner.complete(
                status_code=self.status_code,
                failure_code=self._owner.stream_failure_code,
            )
