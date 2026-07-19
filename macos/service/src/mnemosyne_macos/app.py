"""FastAPI inference and control applications for the native Mac service."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import time
from typing import Any, Mapping

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from .coordinator import CoordinatorError, QueueTimeout
from .engines.base import AdapterError
from .models import Endpoint
from .proxy import (
    InvalidProxyRequest,
    StreamingEventFilter,
    downstream_headers,
    prepare_request_body,
    upstream_headers,
)
from .runtime import NativeRuntime, RestartRequired, RuntimeConfigurationError
from .usage import StreamingUsageParser, UsageEvent, usage_event_from_payload


logger = logging.getLogger("mnemosyne-macos.http")


class LoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str


async def _close_proxy_resources(
    upstream: Any | None,
    lease: Any | None,
) -> None:
    """Close the upstream response and release residency under cancellation.

    Neither a transport close failure nor task cancellation may skip the
    coordinator decrement. ``ModelLease.release`` shields its own decrement;
    this helper preserves cancellation after both cleanup attempts run.
    """

    cancellation: asyncio.CancelledError | None = None
    try:
        if upstream is not None:
            try:
                await upstream.aclose()
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                logger.warning("failed to close upstream response", exc_info=True)
    finally:
        if lease is not None:
            try:
                await lease.release()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception:
                logger.exception("failed to release model lease")
    if cancellation is not None:
        raise cancellation


async def _audit_after_upstream_failure(runtime: NativeRuntime) -> None:
    try:
        await runtime.coordinator.audit()
    except Exception:
        logger.warning("residency audit after upstream failure did not converge")


def _json_model(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "request body must be a JSON object")
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(400, "request body must contain a non-empty 'model' string")
    return model


def _error_status(exc: Exception) -> int:
    if isinstance(exc, QueueTimeout):
        return 504
    if isinstance(exc, KeyError):
        return 404
    if isinstance(exc, InvalidProxyRequest):
        return 400
    if isinstance(exc, RestartRequired):
        return 409
    if isinstance(exc, (CoordinatorError, AdapterError, RuntimeConfigurationError)):
        return 503
    return 502


async def _record_usage(
    runtime: NativeRuntime,
    usage: Any,
    *,
    endpoint: Endpoint,
    engine: str,
    requested_model: str,
    alias: str,
    streamed: bool,
    started_at: float,
    status_code: int,
) -> None:
    if usage is None or not (200 <= status_code < 300):
        return
    event: UsageEvent | None
    if isinstance(usage, Mapping):
        event = usage_event_from_payload(
            usage,
            endpoint=f"/v1/{endpoint.value}",
            engine=engine,
            requested_model=requested_model,
            alias=alias,
            streamed=streamed,
            response_ms=(time.monotonic() - started_at) * 1000,
            status_code=status_code,
        )
    else:
        event = UsageEvent(
            usage=usage,
            endpoint=f"/v1/{endpoint.value}",
            engine=engine,
            requested_model=requested_model,
            alias=alias,
            streamed=streamed,
            response_ms=(time.monotonic() - started_at) * 1000,
            status_code=status_code,
        )
    if event is not None:
        try:
            await runtime.record_usage(event)
        except Exception:
            logger.exception("failed to persist token usage event")


def _inference_authorized(runtime: NativeRuntime, request: Request) -> bool:
    expected = os.environ.get(
        runtime.config.server.inference_api_key_env, ""
    ).strip()
    if not expected:
        return True
    prefix = "Bearer "
    supplied = request.headers.get("authorization", "")
    return supplied.startswith(prefix) and hmac.compare_digest(
        supplied[len(prefix) :], expected
    )


def _control_authorized(runtime: NativeRuntime, request: Request) -> bool:
    expected = os.environ.get(runtime.config.server.control_password_env, "").strip()
    if not expected:
        return True
    supplied = request.headers.get("authorization", "")
    if not supplied.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(supplied[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return username == "admin" and hmac.compare_digest(password, expected)


def create_inference_app(runtime: NativeRuntime) -> FastAPI:
    app = FastAPI(
        title="Mnemosyne macOS Inference",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def inference_auth(request: Request, call_next):
        if request.url.path.startswith("/v1/") and not _inference_authorized(
            runtime, request
        ):
            return JSONResponse(
                {"detail": "invalid or missing inference bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict:
        status = await runtime.status()
        return {
            "status": "ok" if status["state"] in {"idle", "ready"} else "degraded",
            "state": status["state"],
            "model_loaded": status["resident_alias"] is not None,
        }

    @app.get("/v1/models")
    async def models() -> dict:
        return {"object": "list", "data": runtime.model_list()}

    async def proxy(request: Request, endpoint: Endpoint) -> Response:
        body = await request.body()
        requested_model = _json_model(body)
        try:
            target = runtime.resolve(requested_model)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if endpoint not in target.capabilities:
            raise HTTPException(
                400,
                f"model '{target.alias}' does not support /v1/{endpoint.value}",
            )

        started_at = time.monotonic()
        lease = None
        upstream: httpx.Response | None = None
        try:
            lease = await runtime.coordinator.acquire(target)
            route = lease.route(endpoint)
            prepared, requested_model, streamed, client_asked_usage = prepare_request_body(
                body,
                route=route,
                endpoint=endpoint,
            )
            upstream_request = runtime.proxy_client.build_request(
                method=request.method,
                url=f"{route.base_url}{route.path}",
                headers=upstream_headers(request.headers, route),
                content=prepared,
                params=list(request.query_params.multi_items()),
            )
            upstream = await runtime.proxy_client.send(upstream_request, stream=True)
        except HTTPException:
            await _close_proxy_resources(upstream, lease)
            raise
        except Exception as exc:
            await _close_proxy_resources(upstream, lease)
            if isinstance(exc, httpx.HTTPError):
                await _audit_after_upstream_failure(runtime)
            raise HTTPException(_error_status(exc), str(exc)) from exc

        assert lease is not None and upstream is not None
        response_headers = downstream_headers(upstream.headers)
        is_event_stream = "text/event-stream" in upstream.headers.get(
            "content-type", ""
        )
        if streamed or is_event_stream:
            usage_parser = StreamingUsageParser(endpoint=f"/v1/{endpoint.value}")
            event_filter = StreamingEventFilter(
                hide_usage_only_events=(
                    not client_asked_usage
                    and endpoint in {Endpoint.CHAT_COMPLETIONS, Endpoint.COMPLETIONS}
                )
            )

            async def stream_body():
                upstream_failed = False
                try:
                    async for chunk in upstream.aiter_bytes():
                        usage_parser.feed(chunk)
                        for forwarded in event_filter.feed(chunk):
                            yield forwarded
                    tail = event_filter.finish()
                    if tail:
                        yield tail
                except httpx.HTTPError:
                    upstream_failed = True
                    raise
                finally:
                    try:
                        usage = usage_parser.finish()
                        await _record_usage(
                            runtime,
                            usage,
                            endpoint=endpoint,
                            engine=str(target.key.engine),
                            requested_model=requested_model,
                            alias=target.alias,
                            streamed=True,
                            started_at=started_at,
                            status_code=upstream.status_code,
                        )
                    finally:
                        await _close_proxy_resources(upstream, lease)
                        if upstream_failed:
                            await _audit_after_upstream_failure(runtime)

            return StreamingResponse(
                stream_body(),
                status_code=upstream.status_code,
                headers=response_headers,
            )

        upstream_failed = False
        try:
            content = await upstream.aread()
            decoded: Mapping[str, Any] | None = None
            try:
                candidate = json.loads(content)
                if isinstance(candidate, Mapping):
                    decoded = candidate
            except (TypeError, ValueError):
                pass
            await _record_usage(
                runtime,
                decoded,
                endpoint=endpoint,
                engine=str(target.key.engine),
                requested_model=requested_model,
                alias=target.alias,
                streamed=False,
                started_at=started_at,
                status_code=upstream.status_code,
            )
            return Response(
                content=content,
                status_code=upstream.status_code,
                headers=response_headers,
            )
        except httpx.HTTPError as exc:
            upstream_failed = True
            raise HTTPException(502, f"upstream response failed: {exc}") from exc
        finally:
            await _close_proxy_resources(upstream, lease)
            if upstream_failed:
                await _audit_after_upstream_failure(runtime)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await proxy(request, Endpoint.CHAT_COMPLETIONS)

    @app.post("/v1/completions")
    async def completions(request: Request) -> Response:
        return await proxy(request, Endpoint.COMPLETIONS)

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        return await proxy(request, Endpoint.RESPONSES)

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        return await proxy(request, Endpoint.MESSAGES)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        return await proxy(request, Endpoint.EMBEDDINGS)

    @app.post("/v1/rerank")
    async def rerank(request: Request) -> Response:
        return await proxy(request, Endpoint.RERANK)

    return app


def create_control_app(runtime: NativeRuntime) -> FastAPI:
    app = FastAPI(title="Mnemosyne macOS Control")

    @app.middleware("http")
    async def control_auth(request: Request, call_next):
        if not _control_authorized(runtime, request):
            return JSONResponse(
                {"detail": "invalid or missing admin credentials"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Mnemosyne"'},
            )
        return await call_next(request)

    @app.get("/manager/status")
    async def status() -> dict:
        return await runtime.status()

    @app.get("/manager/models")
    async def models() -> dict:
        status_value = await runtime.coordinator.status()
        return {
            "models": runtime.model_list(),
            "resident_alias": status_value.resident_alias,
        }

    @app.post("/manager/load")
    async def load(payload: LoadRequest) -> dict:
        try:
            target = runtime.resolve(payload.model)
            lease = await runtime.coordinator.acquire(target)
            await lease.release()
            return await runtime.status()
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.post("/manager/unload")
    async def unload() -> dict:
        try:
            await runtime.coordinator.unload()
            return await runtime.status()
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.post("/manager/reconcile")
    async def reconcile() -> dict:
        try:
            matched = await runtime.coordinator.reconcile()
            runtime.startup_error = None
            result = await runtime.status()
            result["matched"] = matched
            return result
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.post("/manager/reload")
    async def reload_config() -> dict:
        try:
            await runtime.reload_profiles()
            return {"reloaded": True, "models": runtime.model_list()}
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.get("/manager/usage")
    async def usage(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
        return {
            "rows": await runtime.usage.list_usage(limit=limit),
            "token_sidecar": await runtime.usage.status(),
        }

    return app


__all__ = ["create_control_app", "create_inference_app"]
