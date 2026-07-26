"""FastAPI inference and control applications for the native Mac service."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Mapping

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .config import MacConfig, suggested_model_alias
from .coordinator import CoordinatorError, QueueTimeout
from .engines.base import AdapterError
from .filesystem import FilesystemProbeError
from .image_api import ImageRequestError, normalize_image_request
from .local_models import LocalModelError
from .local_sources import discover_local_model_sources
from .model_library import (
    download_size,
    gguf_files,
    image_profile_defaults,
    model_details,
    recommended_models,
    search_models,
    validate_install_candidate,
)
from .models import DEFAULT_CAPABILITIES, Endpoint, EngineName
from .proxy import (
    InvalidProxyRequest,
    StreamingEventFilter,
    downstream_headers,
    prepare_request_body,
    upstream_headers,
)
from .runtime import (
    ConfigurationConflict,
    NativeRuntime,
    RestartRequired,
    RuntimeConfigurationError,
)
from .runtime_updates import RuntimeUpdateError
from .security_scopes import SecurityScopeError
from .storage import install_destination
from .usage import StreamingUsageParser, UsageEvent, usage_event_from_payload


logger = logging.getLogger("mnemosyne-macos.http")


def _lexical_path(value: str) -> str:
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.expanduser(value)))
    )


class LoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str


class ModelSelfTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    include_vision: bool = True
    unload_after: bool = False


class SaveConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]
    revision: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class DeleteManagedModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class InstallModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    engine: EngineName
    storage: str | None = None
    alias: str | None = None
    revision: str | None = None
    filename: str | None = None
    projector_filename: str | None = None
    include_projector: bool = True
    capabilities: set[Endpoint] | None = None


_INSTALL_ROLE_CAPABILITIES: dict[str, frozenset[Endpoint]] = {
    "generation": DEFAULT_CAPABILITIES[EngineName.LLAMA_CPP],
    "embeddings": frozenset({Endpoint.EMBEDDINGS}),
    "rerank": frozenset({Endpoint.RERANK}),
    "image": frozenset({Endpoint.IMAGES_GENERATIONS}),
}


def _validated_install_capabilities(
    *,
    engine: EngineName,
    requested: set[Endpoint] | None,
    suggested_role: str | None,
    has_projector: bool,
) -> frozenset[Endpoint]:
    if requested is None:
        role = (
            "image"
            if engine == EngineName.MFLUX
            else "generation"
            if engine == EngineName.DS4
            else suggested_role or "generation"
        )
        capabilities = _INSTALL_ROLE_CAPABILITIES.get(role)
        if capabilities is None:
            raise ValueError("model metadata suggested an unsupported role")
    else:
        capabilities = frozenset(requested)

    allowed_roles: dict[EngineName, set[str]] = {
        EngineName.LLAMA_CPP: {"generation", "embeddings", "rerank"},
        EngineName.OMLX: {"generation", "embeddings", "rerank"},
        EngineName.DS4: {"generation"},
        EngineName.MFLUX: {"image"},
    }
    matching_role = next(
        (
            role
            for role, role_capabilities in _INSTALL_ROLE_CAPABILITIES.items()
            if capabilities == role_capabilities
        ),
        None,
    )
    if matching_role is None or matching_role not in allowed_roles.get(engine, set()):
        allowed = ", ".join(sorted(allowed_roles.get(engine, set()))) or "none"
        raise ValueError(
            f"{engine.value} installs require one supported model role: {allowed}"
        )
    if has_projector and matching_role != "generation":
        raise ValueError("a llama.cpp vision projector requires the Generation role")
    return capabilities


class InstallRuntimeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str | None = None


class LocalModelScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    bookmark_data: str | None = Field(default=None, max_length=2 * 1024 * 1024)


class LocalModelImportSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    alias: str | None = None
    projector_id: str | None = None
    include_projector: bool = True


class LocalModelImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    scope_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    selections: list[LocalModelImportSelection]


class StorageInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    bookmark_data: str | None = Field(default=None, max_length=2 * 1024 * 1024)


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


async def _abort_image_request(
    runtime: NativeRuntime,
    upstream: Any | None,
    lease: Any | None,
) -> None:
    """Release the lease and terminate MFLUX so cancelled work frees memory."""
    await _close_proxy_resources(upstream, lease)
    try:
        await runtime.coordinator.unload()
    except Exception:
        logger.exception("failed to unload image worker after request abort")


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
    if isinstance(exc, ImageRequestError):
        return 400
    if isinstance(exc, TimeoutError):
        return 504
    if isinstance(exc, (RestartRequired, ConfigurationConflict)):
        return 409
    if isinstance(exc, RuntimeUpdateError):
        return 400
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


async def _resolve_target(runtime: NativeRuntime, model: str) -> Any:
    """Resolve storage-backed profiles without blocking either HTTP plane."""

    try:
        return await runtime.resolve_target(model)
    except FilesystemProbeError as exc:
        raise HTTPException(
            503,
            str(exc),
        ) from exc


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
            "version": __version__,
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
            target = await _resolve_target(runtime, requested_model)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if endpoint not in target.capabilities:
            raise HTTPException(
                400,
                f"model '{target.alias}' does not support /v1/{endpoint.value}",
            )
        if endpoint == Endpoint.IMAGES_GENERATIONS:
            try:
                body = normalize_image_request(
                    body,
                    wire_model=target.wire_model,
                    defaults=target.image_defaults,
                    max_pixels=runtime.config.server.image_max_pixels,
                )
            except ImageRequestError as exc:
                raise HTTPException(400, str(exc)) from exc

        started_at = time.monotonic()
        lease = None
        upstream: httpx.Response | None = None
        try:
            lease_timeout = (
                max(
                    runtime.config.server.swap_queue_timeout_seconds,
                    runtime.config.server.startup_timeout_seconds,
                )
                if endpoint == Endpoint.IMAGES_GENERATIONS
                else None
            )
            lease = await runtime.coordinator.acquire(
                target,
                timeout_seconds=lease_timeout,
            )
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
            if endpoint == Endpoint.IMAGES_GENERATIONS:
                async with asyncio.timeout(
                    runtime.config.server.image_request_timeout_seconds
                ):
                    upstream = await runtime.proxy_client.send(upstream_request, stream=True)
            else:
                upstream = await runtime.proxy_client.send(upstream_request, stream=True)
        except asyncio.CancelledError:
            if target.key.engine == EngineName.MFLUX:
                cleanup = asyncio.create_task(
                    _abort_image_request(runtime, upstream, lease),
                    name="mnemosyne-abort-image-request",
                )
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
            else:
                await _close_proxy_resources(upstream, lease)
            raise
        except TimeoutError as exc:
            if target.key.engine == EngineName.MFLUX:
                await _abort_image_request(runtime, upstream, lease)
            else:
                await _close_proxy_resources(upstream, lease)
            raise HTTPException(504, "image generation timed out") from exc
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
            if endpoint != Endpoint.IMAGES_GENERATIONS:
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

    @app.post("/v1/images/generations")
    async def images_generations(request: Request) -> Response:
        return await proxy(request, Endpoint.IMAGES_GENERATIONS)

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

    @app.get("/manager/readiness")
    async def readiness() -> dict:
        return await runtime.readiness()

    @app.get("/manager/models")
    async def models() -> dict:
        status_value = await runtime.coordinator.status()
        return {
            "models": runtime.model_list(),
            "resident_alias": status_value.resident_alias,
        }

    @app.delete("/manager/models/{alias}")
    async def delete_managed_model(
        alias: str,
        payload: DeleteManagedModelRequest,
    ) -> dict:
        try:
            config, revision, deleted_files = await runtime.delete_managed_model(
                alias,
                expected_revision=payload.revision,
            )
            return {
                "saved": True,
                "applied": True,
                "restart_required": False,
                "model_count": len(config.models),
                "revision": revision,
                "config": config.model_dump(mode="json"),
                "deleted_files": deleted_files,
            }
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.get("/manager/storage")
    async def storage_locations() -> dict:
        statuses = await asyncio.gather(
            *(
                runtime.filesystem.inspect(
                    location.path,
                    name=location.name,
                    expected_volume_uuid=location.volume_uuid,
                    scope_id=location.scope_id,
                )
                for location in runtime.config.storage.locations
            )
        )
        return {
            "default": runtime.config.storage.default,
            "locations": [status.to_dict() for status in statuses],
        }

    @app.get("/manager/storage/inspect")
    async def inspect_storage_path(path: str = Query(..., min_length=1)) -> dict:
        scoped = runtime.storage_scope_for_path(path)
        status = await runtime.filesystem.inspect(
            path,
            scope_id=scoped[0] if scoped else None,
            scope_path=scoped[1] if scoped else None,
        )
        return status.to_dict()

    @app.post("/manager/storage/inspect")
    async def inspect_selected_storage(payload: StorageInspectRequest) -> dict:
        try:
            scope_id = (
                await runtime.register_security_scope(
                    payload.path,
                    payload.bookmark_data,
                )
                if payload.bookmark_data is not None
                else None
            )
            status = await runtime.filesystem.inspect(
                payload.path,
                scope_id=scope_id,
            )
            value = status.to_dict()
            value["scope_id"] = scope_id
            return value
        except (FilesystemProbeError, ValueError, SecurityScopeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/manager/model-library/recommendations")
    async def library_recommendations(engine: EngineName | None = None) -> dict:
        return {"models": [model.to_dict() for model in recommended_models(engine)]}

    @app.get("/manager/model-library/local-sources")
    def local_model_sources() -> dict:
        # This is deliberately independent of the LM Studio adapter. It reads
        # only LM Studio's configured download root and conventional legacy
        # roots; Finder still grants access before any model files are scanned.
        return {
            "schema_version": 1,
            "sources": [
                source.to_dict() for source in discover_local_model_sources()
            ],
        }

    @app.post("/manager/model-library/local-scan")
    async def local_model_scan(payload: LocalModelScanRequest) -> dict:
        try:
            scope_id = (
                await runtime.register_security_scope(
                    payload.path,
                    payload.bookmark_data,
                )
                if payload.bookmark_data is not None
                else None
            )
            candidates = await runtime.discover_local_models(
                payload.path,
                scope_id=scope_id,
            )
            status = await runtime.filesystem.inspect(
                payload.path,
                scope_id=scope_id,
            )
            rows: list[dict[str, Any]] = []
            for candidate in candidates:
                value = candidate.to_dict()
                existing = next(
                    (
                        profile
                        for profile in runtime.config.models
                        if (
                            profile.engine == EngineName.LLAMA_CPP
                            and _lexical_path(profile.model)
                            == _lexical_path(candidate.model_path)
                        )
                        or (
                            profile.engine == EngineName.OMLX
                            and os.path.basename(profile.model)
                            == os.path.basename(candidate.model_path)
                        )
                    ),
                    None,
                )
                legacy = next(
                    (
                        profile
                        for profile in runtime.config.migration.legacy_lmstudio_profiles
                        if profile.model == candidate.source_key
                    ),
                    None,
                )
                value["existing_alias"] = (
                    existing.alias
                    if existing is not None
                    else legacy.alias
                    if legacy is not None
                    else None
                )
                value["already_imported"] = existing is not None
                rows.append(value)
            return {
                "schema_version": 1,
                "root": status.path,
                "mount_path": status.mount_path,
                "volume_uuid": status.volume_uuid,
                "scope_id": scope_id,
                "models": rows,
            }
        except (
            FilesystemProbeError,
            LocalModelError,
            ValueError,
            SecurityScopeError,
        ) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/manager/model-library/imports")
    async def import_local_models(payload: LocalModelImportRequest) -> dict:
        try:
            return await runtime.adopt_local_models(
                payload.path,
                [item.model_dump(mode="json") for item in payload.selections],
                scope_id=payload.scope_id,
            )
        except (LocalModelError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.get("/manager/model-library/search")
    async def library_search(
        engine: EngineName,
        q: str = Query(default="", max_length=200),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> dict:
        try:
            models = await asyncio.to_thread(
                search_models,
                q,
                engine=engine,
                limit=limit,
            )
            return {"models": [model.to_dict() for model in models]}
        except Exception as exc:
            raise HTTPException(502, f"Hugging Face search failed: {exc}") from exc

    @app.get("/manager/model-library/files")
    async def library_files(
        engine: EngineName,
        repo_id: str = Query(..., min_length=3, max_length=300),
        revision: str | None = Query(default=None, max_length=200),
    ) -> dict:
        if engine != EngineName.LLAMA_CPP:
            raise HTTPException(400, "file selection is currently supported for llama.cpp")
        try:
            models = await asyncio.to_thread(
                gguf_files,
                repo_id,
                revision=revision,
            )
            return {"models": [model.to_dict() for model in models]}
        except Exception as exc:
            raise HTTPException(502, f"Hugging Face file discovery failed: {exc}") from exc

    @app.get("/manager/model-library/details")
    async def library_details(
        engine: EngineName,
        repo_id: str = Query(..., min_length=3, max_length=300),
        filename: str | None = Query(default=None, max_length=500),
        revision: str | None = Query(default=None, max_length=200),
    ) -> dict:
        try:
            details = await asyncio.to_thread(
                model_details,
                repo_id,
                engine=engine,
                filename=filename,
                revision=revision,
            )
            return details.to_dict()
        except Exception as exc:
            raise HTTPException(
                502,
                f"Hugging Face model details failed: {exc}",
            ) from exc

    @app.get("/manager/model-library/installs")
    async def library_installs(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        records = await runtime.installer.list(limit=limit)
        return {"installs": [record.to_dict() for record in records]}

    @app.get("/manager/model-library/install-evidence")
    async def library_install_evidence(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        return {
            "schema_version": 1,
            "installs": await runtime.installer.evidence(limit=limit),
        }

    @app.get("/manager/model-library/installs/{install_id}")
    async def library_install(install_id: str) -> dict:
        try:
            return (await runtime.installer.get(install_id)).to_dict()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/manager/model-library/installs", status_code=202)
    async def library_install_start(payload: InstallModelRequest) -> dict:
        try:
            candidate = await asyncio.to_thread(
                validate_install_candidate,
                engine=payload.engine,
                repo_id=payload.repo_id,
                filename=payload.filename,
                projector_filename=payload.projector_filename,
                include_projector=payload.include_projector,
                revision=payload.revision,
            )
            capabilities = _validated_install_capabilities(
                engine=payload.engine,
                requested=payload.capabilities,
                suggested_role=candidate.suggested_role,
                has_projector=candidate.projector_filename is not None,
            )
            storage_name = payload.storage or runtime.config.storage.default
            location = next(
                (
                    item
                    for item in runtime.config.storage.locations
                    if item.name == storage_name
                ),
                None,
            )
            if location is None:
                raise ValueError(f"unknown storage location '{storage_name}'")
            storage_status = await runtime.filesystem.inspect(
                location.path,
                name=location.name,
                expected_volume_uuid=location.volume_uuid,
                scope_id=location.scope_id,
            )
            if (
                not storage_status.exists
                or not storage_status.is_directory
                or not storage_status.writable
                or not storage_status.volume_matches
            ):
                raise ValueError(
                    storage_status.diagnostic
                    or f"storage '{storage_name}' is unavailable"
                )
            root = Path(storage_status.path)
            total_bytes = await asyncio.to_thread(
                download_size,
                payload.repo_id,
                filename=payload.filename,
                filenames=candidate.download_files,
                revision=candidate.resolved_revision or payload.revision,
            )
            if (
                total_bytes is not None
                and storage_status.free_bytes is not None
                and total_bytes > storage_status.free_bytes
            ):
                raise ValueError(
                    "the selected model requires more space than the storage folder has free"
                )
            destination = install_destination(root, payload.engine, payload.repo_id)
            alias = payload.alias or _available_alias(
                payload.repo_id.rsplit("/", 1)[-1], set(runtime.profiles)
            )
            if alias in runtime.profiles:
                raise ValueError(f"model alias '{alias}' already exists")
            # Reuse the profile validator so API callers cannot bypass alias rules.
            from .config import ModelProfile

            if payload.engine == EngineName.MFLUX:
                ModelProfile(
                    alias=alias,
                    engine=payload.engine,
                    model=str(destination),
                    kind="image",
                    image=image_profile_defaults(candidate),
                )
            elif payload.engine == EngineName.LLAMA_CPP:
                if not candidate.filename:
                    raise ValueError("select an exact GGUF file")
                load: dict[str, Any] = {}
                if candidate.context_length is not None:
                    load["context_length"] = candidate.context_length
                if candidate.projector_filename:
                    load["projector_path"] = str(
                        destination / candidate.projector_filename
                    )
                ModelProfile(
                    alias=alias,
                    engine=payload.engine,
                    model=str(destination / candidate.filename),
                    storage=payload.storage or runtime.config.storage.default,
                    served_model_name=alias,
                    capabilities=set(capabilities),
                    load=load,
                )
            else:
                ModelProfile(
                    alias=alias,
                    engine=payload.engine,
                    model=payload.repo_id,
                    capabilities=set(capabilities),
                )
            record = await runtime.installer.create(
                repo_id=payload.repo_id,
                engine=payload.engine.value,
                storage=storage_name,
                alias=alias,
                destination=str(destination),
                revision=candidate.resolved_revision or payload.revision,
                filename=payload.filename,
                projector_filename=candidate.projector_filename,
                context_length=candidate.context_length,
                download_files=candidate.download_files,
                capabilities=tuple(
                    sorted(endpoint.value for endpoint in capabilities)
                ),
                family=candidate.family,
                total_bytes=total_bytes,
            )
            return record.to_dict()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"model install could not start: {exc}") from exc

    @app.post("/manager/model-library/installs/{install_id}/cancel")
    async def library_install_cancel(install_id: str) -> dict:
        try:
            return (await runtime.installer.cancel(install_id)).to_dict()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/manager/model-library/installs/{install_id}/retry", status_code=202)
    async def library_install_retry(install_id: str) -> dict:
        try:
            return (await runtime.installer.retry(install_id)).to_dict()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete(
        "/manager/model-library/installs/{install_id}",
        status_code=204,
        response_class=Response,
    )
    async def library_install_dismiss(install_id: str) -> Response:
        try:
            await runtime.installer.dismiss(install_id)
            return Response(status_code=204)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/manager/load")
    async def load(payload: LoadRequest) -> dict:
        try:
            target = await _resolve_target(runtime, payload.model)
            lease = await runtime.coordinator.acquire(
                target,
                timeout_seconds=(
                    max(
                        runtime.config.server.swap_queue_timeout_seconds,
                        runtime.config.server.startup_timeout_seconds,
                    )
                    if target.key.engine == EngineName.MFLUX
                    else None
                ),
            )
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

    @app.post("/manager/self-test")
    async def self_test(payload: ModelSelfTestRequest) -> dict:
        try:
            return await runtime.self_test(
                payload.model,
                include_vision=payload.include_vision,
                unload_after=payload.unload_after,
            )
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.post("/manager/reload")
    async def reload_config() -> dict:
        try:
            await runtime.reload_profiles()
            return {"reloaded": True, "models": runtime.model_list()}
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.get("/manager/runtime-updates")
    async def runtime_updates(refresh: bool = Query(default=False)) -> dict:
        try:
            return await runtime.check_runtime_updates(refresh=refresh)
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.post("/manager/runtime-updates/check")
    async def check_runtime_updates() -> dict:
        try:
            return await runtime.check_runtime_updates(refresh=True)
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.post("/manager/runtime-updates/{engine}/install")
    async def install_runtime_update(
        engine: str, payload: InstallRuntimeUpdateRequest
    ) -> dict:
        try:
            return await runtime.install_runtime_update(
                engine, version=payload.version
            )
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.post("/manager/runtime-updates/{engine}/rollback")
    async def rollback_runtime_update(engine: str) -> dict:
        try:
            return await runtime.rollback_runtime_update(engine)
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.get("/manager/config")
    async def get_configuration() -> dict:
        config, revision, applied_revision, restart_required = (
            await runtime.configuration_snapshot()
        )
        return {
            "config": config.model_dump(mode="json"),
            "revision": revision,
            "applied_revision": applied_revision,
            "restart_required": restart_required,
        }

    @app.put("/manager/config")
    async def save_configuration(payload: SaveConfigurationRequest) -> dict:
        if runtime.config_path is None:
            raise HTTPException(503, "runtime has no configured YAML path")
        try:
            config = MacConfig.model_validate(payload.config)
            restart_required, revision = await runtime.save_configuration(
                config,
                expected_revision=payload.revision,
            )
            return {
                "saved": True,
                "applied": not restart_required,
                "restart_required": restart_required,
                "model_count": len(config.models),
                "revision": revision,
                "config": config.model_dump(mode="json"),
            }
        except Exception as exc:
            status = 409 if isinstance(exc, ConfigurationConflict) else 400
            raise HTTPException(status, str(exc)) from exc

    @app.get("/manager/usage")
    async def usage(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
        return {
            "rows": await runtime.usage.list_usage(limit=limit),
            "token_sidecar": await runtime.usage.status(),
        }

    return app


def _available_alias(name: str, existing: set[str]) -> str:
    base = suggested_model_alias(name)
    candidate = base
    number = 2
    while candidate in existing:
        suffix = f"-{number}"
        candidate = f"{base[: 64 - len(suffix)].rstrip('-')}{suffix}"
        number += 1
    return candidate


__all__ = ["create_control_app", "create_inference_app"]
