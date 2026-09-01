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
import re
import time
from typing import Any, Literal, Mapping, TypeVar
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from . import __version__
from .config import MacConfig, suggested_model_alias
from .coordinator import CoordinatorError, CoordinatorState, QueueFull, QueueTimeout
from .desired_install_store import (
    DesiredInstallConflictError,
    DesiredInstallIntegrityError,
    DesiredInstallNotFoundError,
    DesiredInstallStoreError,
)
from .desired_install_executor import DesiredInstallExecutorError
from .engines.base import AdapterError
from .filesystem import FilesystemProbeError
from .fleet_participation import (
    FleetParticipationClosed,
    FleetParticipationLease,
    FleetParticipationStatus,
    FleetParticipationUnavailable,
)
from .fleet_pairing_client import (
    MAX_PAIRING_REQUEST_BYTES,
    PairingClientError,
    PairingClientErrorCode,
    PairingInvitation,
)
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
from .native_lifecycle import (
    NativeLifecycleCapacityError,
    NativeLifecycleConflictError,
    NativeLifecycleError,
    NativeLifecycleIntegrityError,
    NativeLifecycleNotFoundError,
)
from .models import ACTIVE_ENGINE_NAMES, DEFAULT_CAPABILITIES, Endpoint, EngineName
from .performance import RequestPerformanceTimer
from .proxy import (
    InvalidProxyRequest,
    StreamingEventFilter,
    downstream_headers,
    prepare_request_body,
    upstream_headers,
)
from .runtime import (
    ConfigurationConflict,
    ModelCleanupRejected,
    NativeRuntime,
    RestartRequired,
    RuntimeConfigurationError,
)
from .runtime_updates import RuntimeUpdateError
from .security_scopes import SecurityScopeError
from .storage import install_destination
from .usage import StreamingUsageParser, UsageEvent, usage_event_from_payload
from .usage_delivery import (
    UsageEventDuplicate,
    UsageOutboxFull,
    UsageReservationLease,
)


logger = logging.getLogger("mnemosyne-macos.http")


def _lexical_path(value: str) -> str:
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.expanduser(value)))
    )


async def _await_task_to_known_outcome(task: asyncio.Task):
    """Defer repeated caller cancellation until an owned task is complete."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
        except Exception:
            break

    if task.cancelled():
        return task.result()
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


class LoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str


class SetFleetParticipationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class FleetPairingControlRequest(BaseModel):
    """Secret-bearing loopback request parsed without reflective validation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )

    schema_version: Literal[1]
    invitation_id: str = Field(min_length=36, max_length=36)
    pairing_secret: SecretStr = Field(min_length=32, max_length=4096, repr=False)
    hub_origin: str = Field(min_length=1, max_length=2048)
    locator: SecretStr = Field(min_length=1, max_length=2048, repr=False)


class FleetPairingPresenceRequest(BaseModel):
    """Secret-free local request to start a short-code Hub ceremony."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )

    schema_version: Literal[1]
    request_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )
    hub_origin: str = Field(min_length=1, max_length=2048)
    locator: SecretStr = Field(min_length=1, max_length=2048, repr=False)
    transport: Literal["https", "tailscale", "trusted_lan_http"]


class FleetPairingManagementRequest(BaseModel):
    """One idempotent, secret-free local pairing-management request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    request_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )


class RefuseDesiredInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    job_revision: int = Field(ge=1, le=2_147_483_647)


class NativeUninstallPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    retention_mode: Literal[
        "app_only",
        "remove_state_runtimes_keep_weights",
        "remove_exclusive_managed",
    ]


class NativeUninstallPrepareRequest(NativeUninstallPreviewRequest):
    transaction_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )


class NativeLifecycleAuthorizationChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]


class NativeLifecycleAuthorizationPerformRequest(BaseModel):
    """Closed trigger for the service-owned helper transport."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]


class NativeLifecycleAuthorizationCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    nonce: str = Field(
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )
    session_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )


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
    installation_id: str | None = Field(
        default=None,
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )


class BenchmarkModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warmup_runs: int = Field(default=1, ge=0, le=5)
    sample_runs: int = Field(default=3, ge=1, le=20)
    # Output length is part of the benchmark suite contract. Keeping it fixed
    # prevents incomparable throughput runs from sharing one evidence key.
    max_tokens: int = Field(default=128, ge=128, le=128)


class ContextProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_tokens: int | None = Field(default=None, ge=4_096, le=1_048_576)


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


_GENERATION_WITH_MESSAGES = frozenset(
    {
        Endpoint.CHAT_COMPLETIONS,
        Endpoint.COMPLETIONS,
        Endpoint.RESPONSES,
        Endpoint.MESSAGES,
    }
)
_INSTALL_ROLE_CAPABILITY_SETS: dict[
    EngineName,
    dict[str, tuple[frozenset[Endpoint], ...]],
] = {
    EngineName.LLAMA_CPP: {
        "generation": (
            DEFAULT_CAPABILITIES[EngineName.LLAMA_CPP],
            _GENERATION_WITH_MESSAGES,
        ),
        "embeddings": (frozenset({Endpoint.EMBEDDINGS}),),
        "rerank": (frozenset({Endpoint.RERANK}),),
    },
    EngineName.OMLX: {
        "generation": (_GENERATION_WITH_MESSAGES,),
        "embeddings": (frozenset({Endpoint.EMBEDDINGS}),),
        "rerank": (frozenset({Endpoint.RERANK}),),
    },
    EngineName.DS4: {
        "generation": (_GENERATION_WITH_MESSAGES,),
    },
    EngineName.MFLUX: {
        "image": (frozenset({Endpoint.IMAGES_GENERATIONS}),),
    },
}


def _validated_install_capabilities(
    *,
    engine: EngineName,
    requested: set[Endpoint] | None,
    suggested_role: str | None,
    has_projector: bool,
) -> frozenset[Endpoint]:
    if engine not in ACTIVE_ENGINE_NAMES:
        raise ValueError(f"{engine.value} is retired on macOS")
    role_capabilities = _INSTALL_ROLE_CAPABILITY_SETS.get(engine, {})
    if requested is None:
        role = (
            "image"
            if engine == EngineName.MFLUX
            else "generation"
            if engine == EngineName.DS4
            else suggested_role or "generation"
        )
        accepted = role_capabilities.get(role)
        if accepted is None:
            raise ValueError("model metadata suggested an unsupported role")
        capabilities = accepted[0]
    else:
        capabilities = frozenset(requested)

    matching_role = next(
        (
            role
            for role, accepted in role_capabilities.items()
            if capabilities in accepted
        ),
        None,
    )
    if matching_role is None:
        allowed = ", ".join(sorted(role_capabilities)) or "none"
        raise ValueError(
            f"{engine.value} installs require one supported model role: {allowed}"
        )
    if has_projector and matching_role != "generation":
        raise ValueError("a llama.cpp vision projector requires the Generation role")
    return capabilities


class InstallRuntimeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str | None = None
    channel: Literal["official", "glm-5.3-flash"] | None = None


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
    fleet_lease: FleetParticipationLease | None = None,
    usage_reservation: UsageReservationLease | None = None,
) -> None:
    """Close the upstream response and release residency under cancellation.

    Neither a transport close failure nor task cancellation may skip the
    coordinator decrement. ``ModelLease.release`` shields its own decrement;
    this helper preserves cancellation after both cleanup attempts run.
    """

    async def drain() -> None:
        cancellation: asyncio.CancelledError | None = None
        try:
            if usage_reservation is not None:
                try:
                    await usage_reservation.finish()
                except asyncio.CancelledError as exc:
                    cancellation = exc
                except Exception:
                    logger.exception("failed to finish durable usage reservation")
            if upstream is not None:
                try:
                    await upstream.aclose()
                except asyncio.CancelledError as exc:
                    cancellation = exc
                except Exception:
                    logger.warning("failed to close upstream response", exc_info=True)
        finally:
            try:
                if lease is not None:
                    try:
                        await lease.release()
                    except asyncio.CancelledError as exc:
                        cancellation = cancellation or exc
                    except Exception:
                        logger.exception("failed to release model lease")
            finally:
                if fleet_lease is not None:
                    try:
                        await fleet_lease.release()
                    except asyncio.CancelledError as exc:
                        cancellation = cancellation or exc
                    except Exception:
                        logger.exception(
                            "failed to release Fleet participation lease"
                        )
        if cancellation is not None:
            raise cancellation

    cleanup = asyncio.create_task(
        drain(),
        name="mnemosyne-close-proxy-resources",
    )
    await _await_task_to_known_outcome(cleanup)


async def _audit_after_upstream_failure(runtime: NativeRuntime) -> None:
    try:
        await runtime.coordinator.audit()
    except Exception:
        logger.warning("residency audit after upstream failure did not converge")


async def _abort_image_request(
    runtime: NativeRuntime,
    upstream: Any | None,
    lease: Any | None,
    fleet_lease: FleetParticipationLease | None = None,
    usage_reservation: UsageReservationLease | None = None,
) -> None:
    """Fence admission before terminating MFLUX after an interrupted request."""

    cancellation: asyncio.CancelledError | None = None
    try:
        if usage_reservation is not None:
            try:
                await usage_reservation.finish()
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                logger.exception("failed to finish durable usage reservation")
        if lease is not None:
            try:
                await lease.abort()
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                logger.exception("failed to abort and unload image resident")
        if upstream is not None:
            try:
                await upstream.aclose()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception:
                logger.warning("failed to close aborted image response", exc_info=True)
    finally:
        if fleet_lease is not None:
            try:
                await fleet_lease.release()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception:
                logger.exception("failed to release Fleet participation lease")
    if cancellation is not None:
        raise cancellation


async def _abort_image_request_shielded(
    runtime: NativeRuntime,
    upstream: Any | None,
    lease: Any | None,
    fleet_lease: FleetParticipationLease | None = None,
    usage_reservation: UsageReservationLease | None = None,
) -> None:
    """Let the MFLUX epoch fence/unload finish if its caller is cancelled."""

    cleanup = asyncio.create_task(
        _abort_image_request(
            runtime,
            upstream,
            lease,
            fleet_lease,
            usage_reservation,
        ),
        name="mnemosyne-abort-image-request",
    )
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError as cancellation:
        # The independently owned cleanup task keeps running. Usually the
        # cancellation that reached this wrapper has already been delivered,
        # so this second shield waits for the fence/unload before cancellation
        # is re-raised. A repeated cancellation still cannot cancel cleanup.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(cleanup)
        raise cancellation


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
    if isinstance(exc, QueueFull):
        return 429
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
    if isinstance(exc, ModelCleanupRejected):
        return 400
    if isinstance(exc, (CoordinatorError, AdapterError, RuntimeConfigurationError)):
        return 503
    return 502


def _desired_install_http_exception(
    exc: DesiredInstallStoreError | DesiredInstallExecutorError,
) -> HTTPException:
    if isinstance(exc, DesiredInstallNotFoundError) or exc.code == (
        "desired_install_job_unknown"
    ):
        status = 404
        message = "The desired install job is not known."
    elif isinstance(exc, DesiredInstallConflictError) or exc.code in {
        "desired_install_not_awaiting_local_approval",
        "desired_install_installation_unbound",
        "desired_install_installation_invalid",
    }:
        status = 409
        message = "The desired install job changed or the local action is no longer available."
    elif isinstance(exc, DesiredInstallIntegrityError):
        status = 503
        message = "The desired install journal is unavailable."
    elif isinstance(exc, DesiredInstallExecutorError):
        status = 503
        message = "The desired install executor is unavailable."
    else:
        status = 400
        message = "The desired install request is invalid."
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": message},
    )


def _native_lifecycle_http_exception(exc: NativeLifecycleError) -> HTTPException:
    if isinstance(exc, NativeLifecycleNotFoundError):
        status = 404
        message = "The native lifecycle transaction is not known."
    elif exc.code == "native_lifecycle_helper_authority_invalid":
        status = 400
        message = "The lifecycle helper receipt is invalid."
    elif isinstance(exc, NativeLifecycleConflictError) or exc.code in {
        "native_lifecycle_outbox_blocked",
        "native_lifecycle_storage_authority_stale",
        "native_lifecycle_transaction_conflict",
        "native_lifecycle_helper_authority_cancelled",
        "native_lifecycle_helper_authority_conflict",
        "native_lifecycle_helper_authority_expired",
        "native_lifecycle_helper_authority_mismatch",
        "native_lifecycle_helper_authority_replayed",
    }:
        status = 409
        message = "The lifecycle plan conflicts with current local evidence."
    elif isinstance(
        exc,
        (NativeLifecycleIntegrityError, NativeLifecycleCapacityError),
    ) or exc.code in {
        "native_lifecycle_inventory_timeout",
        "native_lifecycle_inventory_unavailable",
        "native_lifecycle_journal_unavailable",
        "native_lifecycle_migration_evidence_unavailable",
        "native_lifecycle_helper_authority_capacity_exhausted",
        "native_lifecycle_helper_authority_unavailable",
    }:
        status = 503
        message = "Native lifecycle planning is unavailable."
    else:
        status = 400
        message = "The native lifecycle request is invalid."
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": message},
    )


async def _native_lifecycle_helper_receipt(
    request: Request,
) -> Mapping[str, object]:
    """Read one bounded flat receipt while rejecting duplicate JSON keys."""

    body = await request.body()
    if not body or len(body) > 16 * 1024:
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )

    def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            body,
            object_pairs_hook=closed_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        ) from None
    if not isinstance(payload, dict):
        raise NativeLifecycleConflictError(
            "native_lifecycle_helper_authority_invalid"
        )
    return payload


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
    event_id: str,
    reservation: UsageReservationLease | None,
) -> Any:
    if not (200 <= status_code < 300) or endpoint == Endpoint.IMAGES_GENERATIONS:
        return None
    fleet_usage_required = bool(
        reservation is not None and reservation.fleet_route
    )
    if usage is None:
        if fleet_usage_required:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "usage_missing",
                    "message": (
                        "The selected engine completed without required token usage."
                    ),
                },
            )
        return None
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
            event_id=event_id,
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
            event_id=event_id,
        )
    if event is not None:
        try:
            await runtime.record_usage(event, reservation=reservation)
        except UsageOutboxFull as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "usage_outbox_full",
                    "message": (
                        "Token accounting is temporarily at capacity; "
                        "delivery must catch up before another completion."
                    ),
                },
                headers={"Retry-After": "5"},
            ) from exc
        except Exception as exc:
            logger.exception("failed to persist token usage event")
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "usage_persistence_failed",
                    "message": (
                        "The completion could not be committed to durable "
                        "token accounting."
                    ),
                },
            ) from exc
        return event.usage
    if fleet_usage_required:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "usage_missing",
                "message": (
                    "The selected engine completed without required token usage."
                ),
            },
        )
    return None


class _TerminalSSEGate:
    """Hold recognized terminal SSE events until usage accounting commits.

    Non-terminal events are forwarded immediately. The gate is deliberately
    independent of usage visibility filtering: whether a synthetic usage
    event is shown or hidden, a recognized terminal event is not sent before
    the request's stable usage event has been committed.
    """

    _boundary = re.compile(br"\r?\n\r?\n")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._terminal = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        output: list[bytes] = []
        while True:
            match = self._boundary.search(self._buffer)
            if match is None:
                break
            event = bytes(self._buffer[: match.end()])
            del self._buffer[: match.end()]
            if self._terminal or _is_terminal_sse_event(event):
                self._terminal.extend(event)
            else:
                output.append(event)
        return output

    def finish(self) -> bytes:
        terminal = bytes(self._terminal) + bytes(self._buffer)
        self._terminal.clear()
        self._buffer.clear()
        return terminal


def _is_terminal_sse_event(event: bytes) -> bool:
    event_name: str | None = None
    data_lines: list[str] = []
    for raw_line in event.decode("utf-8", errors="replace").splitlines():
        if raw_line.startswith("event:"):
            event_name = raw_line[6:].strip().casefold()
        elif raw_line.startswith("data:"):
            data_lines.append(raw_line[5:].lstrip())
    if event_name in {
        "message_stop",
        "response.completed",
        "response.failed",
        "response.incomplete",
    }:
        return True
    if not data_lines:
        return False
    data = "\n".join(data_lines).strip()
    if data == "[DONE]":
        return True
    try:
        payload = json.loads(data)
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(payload, Mapping)
        and str(payload.get("type") or "").casefold()
        in {
            "message_stop",
            "response.completed",
            "response.failed",
            "response.incomplete",
        }
    )


class _StreamingProxyOwnership:
    """Idempotent full-ASGI ownership of native upstream and model lease."""

    def __init__(
        self,
        *,
        runtime: NativeRuntime,
        upstream: httpx.Response,
        lease: Any,
        fleet_lease: FleetParticipationLease | None,
        target: Any,
        endpoint: Endpoint,
        requested_model: str,
        started_at: float,
        usage_parser: StreamingUsageParser,
        performance_timer: RequestPerformanceTimer,
        usage_event_id: str,
        usage_reservation: UsageReservationLease | None,
    ) -> None:
        self.runtime = runtime
        self.upstream = upstream
        self.lease = lease
        self.fleet_lease = fleet_lease
        self.target = target
        self.endpoint = endpoint
        self.requested_model = requested_model
        self.started_at = started_at
        self.usage_parser = usage_parser
        self.performance_timer = performance_timer
        self.usage_event_id = usage_event_id
        self.usage_reservation = usage_reservation
        self.body_failed = False
        self.upstream_failed = False
        self._accounting_task: asyncio.Task[Any] | None = None
        self._completion_task: asyncio.Task[None] | None = None

    def note_failure(self, *, upstream_failed: bool) -> None:
        self.body_failed = True
        self.upstream_failed = self.upstream_failed or upstream_failed

    async def _persist_usage(self) -> Any:
        if self.body_failed:
            return None
        usage = self.usage_parser.finish()
        recorded = await _record_usage(
            self.runtime,
            usage,
            endpoint=self.endpoint,
            engine=str(self.target.key.engine),
            requested_model=self.requested_model,
            alias=self.target.alias,
            streamed=True,
            started_at=self.started_at,
            status_code=self.upstream.status_code,
            event_id=self.usage_event_id,
            reservation=self.usage_reservation,
        )
        if recorded is None and self.usage_reservation is not None:
            await self.usage_reservation.finish()
        return usage

    async def persist_usage(self) -> Any:
        if self._accounting_task is None:
            self._accounting_task = asyncio.create_task(
                self._persist_usage(),
                name="mnemosyne-streaming-usage-commit",
            )
        return await _await_task_to_known_outcome(self._accounting_task)

    async def _finish(self) -> None:
        usage = None
        try:
            usage = await self.persist_usage()
        finally:
            try:
                if (
                    self.target.key.engine == EngineName.MFLUX
                    and self.body_failed
                ):
                    await _abort_image_request_shielded(
                        self.runtime,
                        self.upstream,
                        self.lease,
                        self.fleet_lease,
                        self.usage_reservation,
                    )
                else:
                    await _close_proxy_resources(
                        self.upstream,
                        self.lease,
                        self.fleet_lease,
                        self.usage_reservation,
                    )
            finally:
                if (
                    self.upstream_failed
                    and self.target.key.engine != EngineName.MFLUX
                ):
                    await _audit_after_upstream_failure(self.runtime)
                if (
                    self.runtime.is_engine_alternative(self.target)
                    and (
                        self.upstream_failed
                        or self.upstream.status_code >= 500
                    )
                ):
                    self.runtime.invalidate_automatic_selection(self.target.alias)
                self.performance_timer.finish(
                    status_code=self.upstream.status_code,
                    error_code=(
                        "upstream_stream_failure"
                        if self.upstream_failed
                        else "stream_interrupted"
                        if self.body_failed
                        else "upstream_status"
                        if self.upstream.status_code >= 400
                        else None
                    ),
                    completion_tokens=(
                        usage.completion_tokens if usage is not None else None
                    ),
                )

    async def complete(self) -> None:
        if self._completion_task is None:
            self._completion_task = asyncio.create_task(
                self._finish(),
                name="mnemosyne-streaming-proxy-finish",
            )
        await _await_task_to_known_outcome(self._completion_task)


class _OwnedStreamingResponse(StreamingResponse):
    """Ensure cleanup even if cancellation precedes body iteration."""

    def __init__(
        self,
        content,
        *,
        owner: _StreamingProxyOwnership,
        status_code: int,
        headers: Mapping[str, str],
    ) -> None:
        super().__init__(
            content,
            status_code=status_code,
            headers=dict(headers),
        )
        self._owner = owner

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            self._owner.note_failure(upstream_failed=False)
            raise
        except BaseException:
            self._owner.note_failure(upstream_failed=False)
            raise
        finally:
            await self._owner.complete()


def _inference_authorized(
    runtime: NativeRuntime,
    request: Request,
    *,
    fleet_routed: bool | None = None,
) -> bool:
    # A paired Hub receives a dispatch-only credential. It is deliberately
    # useless for ordinary unmarked local inference, while static v1
    # enrollments retain their existing INFERENCE_API_KEY fallback.
    if fleet_routed is None:
        fleet_routed = bool(
            request.headers.getlist("x-mnemosyne-fleet-route")
        )
    if fleet_routed:
        expected = os.environ.get(
            runtime.config.server.fleet_inference_api_key_env,
            "",
        ).strip()
        if not expected:
            expected = os.environ.get(
                runtime.config.server.inference_api_key_env,
                "",
            ).strip()
    else:
        expected = os.environ.get(
            runtime.config.server.inference_api_key_env,
            "",
        ).strip()
    if not expected:
        return not fleet_routed
    prefix = "Bearer "
    supplied = request.headers.get("authorization", "")
    return supplied.startswith(prefix) and hmac.compare_digest(
        supplied[len(prefix) :], expected
    )


def _fleet_dispatch_configured(runtime: NativeRuntime) -> bool:
    return bool(
        os.environ.get(
            runtime.config.server.fleet_inference_api_key_env,
            "",
        ).strip()
        or os.environ.get(
            runtime.config.server.inference_api_key_env,
            "",
        ).strip()
    )


def _fleet_authorized(runtime: NativeRuntime, request: Request) -> bool:
    expected = os.environ.get(runtime.config.server.fleet_api_key_env, "").strip()
    if not expected:
        return False
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


def _is_fleet_routed(request: Request) -> bool:
    """Recognize only an unambiguous canonical UUID route marker."""

    values = request.headers.getlist("x-mnemosyne-fleet-route")
    if not values:
        return False
    if len(values) != 1:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_fleet_route",
                "message": "Fleet route marker must be one canonical UUID",
            },
        )
    value = values[0]
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        parsed = None
    if len(value) != 36 or parsed is None or str(parsed) != value.lower():
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_fleet_route",
                "message": "Fleet route marker must be one canonical UUID",
            },
        )
    return True


def _fleet_participation_payload(
    status: FleetParticipationStatus,
) -> dict[str, Any]:
    """Return the stable, intentionally small native control API shape."""

    return {
        "enabled": status.joined,
        "state": status.state.value,
        "active_requests": status.active_fleet_requests,
        "updated_at": status.updated_at,
    }


_FleetPairingLocalPayloadT = TypeVar(
    "_FleetPairingLocalPayloadT",
    bound=BaseModel,
)


async def _parse_fleet_pairing_local_request(
    request: Request,
    payload_type: type[_FleetPairingLocalPayloadT],
) -> _FleetPairingLocalPayloadT:
    """Parse a bounded secret-bearing body without echoing invalid input."""

    content_encoding = request.headers.get("content-encoding", "identity").lower()
    content_type = request.headers.get("content-type", "")
    if (
        content_encoding != "identity"
        or content_type.split(";", 1)[0].strip().lower() != "application/json"
    ):
        raise HTTPException(
            status_code=415,
            detail={
                "code": "pairing_json_required",
                "message": "Pairing requires an unencoded JSON request.",
            },
        )
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = -1
        if declared_size < 0:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "pairing_invalid_request",
                    "message": "The pairing request is invalid.",
                },
            )
        if declared_size > MAX_PAIRING_REQUEST_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "pairing_request_too_large",
                    "message": "The pairing request is too large.",
                },
            )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_PAIRING_REQUEST_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "pairing_request_too_large",
                    "message": "The pairing request is too large.",
                },
            )
    try:
        return payload_type.model_validate_json(bytes(body))
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "pairing_invalid_request",
                "message": "The pairing request is invalid.",
            },
        ) from None


async def _parse_fleet_pairing_control_request(
    request: Request,
) -> FleetPairingControlRequest:
    return await _parse_fleet_pairing_local_request(
        request,
        FleetPairingControlRequest,
    )


async def _parse_fleet_pairing_presence_request(
    request: Request,
) -> FleetPairingPresenceRequest:
    return await _parse_fleet_pairing_local_request(
        request,
        FleetPairingPresenceRequest,
    )


def _pairing_invitation(payload: FleetPairingControlRequest) -> PairingInvitation:
    return PairingInvitation(
        invitation_id=payload.invitation_id,
        pairing_secret=payload.pairing_secret.get_secret_value(),
        hub_origin=payload.hub_origin,
        locator=payload.locator.get_secret_value(),
    )


async def _pairing_client_error_response(
    runtime: NativeRuntime,
    error: PairingClientError,
) -> JSONResponse:
    status = await runtime.fleet_pairing_status()
    if error.code == PairingClientErrorCode.APPROVAL_PENDING:
        return JSONResponse(
            status_code=202,
            headers={"Cache-Control": "no-store"},
            content={
                "schema_version": 1,
                "accepted": True,
                "next_action": "resume_after_approval",
                "pairing": status,
            },
        )
    return JSONResponse(
        status_code=error.status_code,
        headers={"Cache-Control": "no-store"},
        content={
            "detail": {
                "code": error.code.value,
                "message": error.public_message,
                "retryable": error.retryable,
            },
            "pairing": status,
        },
    )


async def _resolve_target(
    runtime: NativeRuntime,
    model: str,
    endpoint: Endpoint | None = None,
) -> Any:
    """Resolve storage-backed profiles without blocking either HTTP plane."""

    try:
        return await runtime.resolve_target(model, endpoint=endpoint)
    except FilesystemProbeError as exc:
        raise HTTPException(
            503,
            str(exc),
        ) from exc
    except RuntimeConfigurationError as exc:
        # Includes a signed external oMLX contract that is no longer proved.
        # This is a pre-work availability failure; no adapter load has run.
        raise HTTPException(503, str(exc)) from exc


def create_inference_app(runtime: NativeRuntime) -> FastAPI:
    app = FastAPI(
        title="Mnemosyne macOS Inference",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def inference_auth(request: Request, call_next):
        if request.url.path.startswith("/v1/"):
            try:
                fleet_routed = _is_fleet_routed(request)
            except HTTPException as exc:
                return JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )
            request.state.fleet_routed = fleet_routed
            if not _inference_authorized(
                runtime,
                request,
                fleet_routed=fleet_routed,
            ):
                return JSONResponse(
                    {"detail": "invalid or missing inference bearer token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if fleet_routed and not runtime.fleet_dispatch_credential_active(
                activation_probe=(
                    request.method == "GET" and request.url.path == "/v1/models"
                )
            ):
                return JSONResponse(
                    {
                        "detail": {
                            "code": "fleet_pairing_not_active",
                            "message": "Fleet pairing is not active for inference",
                        }
                    },
                    status_code=503,
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
    async def models(request: Request) -> dict:
        fleet_routed = getattr(request.state, "fleet_routed", None)
        if fleet_routed is None:
            fleet_routed = _is_fleet_routed(request)
        return {
            "object": "list",
            "data": (
                runtime.fleet_probe_model_list()
                if fleet_routed
                else runtime.model_list()
            ),
        }

    @app.get("/fleet/v1/snapshot")
    async def fleet_snapshot(request: Request) -> dict:
        configured = os.environ.get(
            runtime.config.server.fleet_api_key_env,
            "",
        ).strip()
        if not configured:
            raise HTTPException(503, "fleet snapshot authentication is not configured")
        if not _fleet_authorized(runtime, request):
            raise HTTPException(
                status_code=401,
                detail="invalid or missing fleet bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not _fleet_dispatch_configured(runtime):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "fleet_inference_auth_unconfigured",
                    "message": (
                        "configure a node-specific Fleet dispatch or inference "
                        "API key before enabling fleet discovery"
                    ),
                },
            )
        if not runtime.fleet_snapshot_credential_active():
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "fleet_pairing_not_active",
                    "message": "Fleet pairing is not active for discovery",
                },
            )
        return await runtime.fleet_snapshot()

    async def proxy(request: Request, endpoint: Endpoint) -> Response:
        fleet_lease: FleetParticipationLease | None = None
        fleet_routed = getattr(request.state, "fleet_routed", None)
        if fleet_routed is None:
            # Direct endpoint tests and internal callers may bypass ASGI
            # middleware; retain the same strict marker validation there.
            fleet_routed = _is_fleet_routed(request)
        if fleet_routed:
            try:
                fleet_lease = await runtime.fleet_participation.acquire()
            except (
                FleetParticipationClosed,
                FleetParticipationUnavailable,
            ) as exc:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "node_busy",
                        "message": str(exc),
                    },
                    headers={
                        "Retry-After": "1",
                        "X-Mnemosyne-Error": "node_busy",
                    },
                ) from exc
        fleet_route_id = (
            request.headers.get("x-mnemosyne-fleet-route")
            if fleet_routed
            else None
        )
        usage_event_id = fleet_route_id or str(uuid4())
        usage_reservation: UsageReservationLease | None = None
        requires_accounting = endpoint != Endpoint.IMAGES_GENERATIONS
        try:
            if fleet_routed:
                try:
                    usage_reservation = await runtime.usage.reserve(
                        usage_event_id,
                        fleet_route=True,
                        requires_accounting=requires_accounting,
                    )
                except UsageEventDuplicate as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "duplicate_fleet_route",
                            "message": (
                                "This Fleet route is already active or complete."
                            ),
                        },
                    ) from exc
                except UsageOutboxFull as exc:
                    # A snapshot can become stale after another request takes
                    # the final durable accounting slot. This denial is still
                    # proven pre-work, so Nyx may safely try another Mac.
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "code": "node_busy",
                            "message": (
                                "Token accounting is temporarily at capacity."
                            ),
                        },
                        headers={
                            "Retry-After": "1",
                            "X-Mnemosyne-Error": "node_busy",
                        },
                    ) from exc
            body = await request.body()
            requested_model = _json_model(body)
            try:
                target = await _resolve_target(runtime, requested_model, endpoint)
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            if endpoint not in target.capabilities:
                raise HTTPException(
                    400,
                    f"model '{target.alias}' does not support /v1/{endpoint.value}",
                )
            if requires_accounting and usage_reservation is None:
                try:
                    usage_reservation = await runtime.usage.reserve(
                        usage_event_id,
                        fleet_route=False,
                        requires_accounting=True,
                    )
                except UsageOutboxFull as exc:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "usage_outbox_full",
                            "message": (
                                "Token accounting is temporarily at capacity; "
                                "delivery must catch up before inference."
                            ),
                        },
                        headers={"Retry-After": "5"},
                    ) from exc
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
            performance_timer = runtime.performance.start(
                alias=target.alias,
                engine=target.key.engine.value,
                endpoint=f"/v1/{endpoint.value}",
                streamed=False,
            )
            before_admission = await runtime.coordinator.status()
            cold_start = not (
                before_admission.resident_alias == target.alias
                and before_admission.resident_engine == target.key.engine
                and before_admission.state == CoordinatorState.READY
            )
        except BaseException:
            await _close_proxy_resources(
                None,
                None,
                fleet_lease,
                usage_reservation,
            )
            raise
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
            try:
                lease = await runtime.coordinator.acquire(
                    target,
                    timeout_seconds=lease_timeout,
                )
            except (QueueFull, QueueTimeout):
                raise
            except (AdapterError, CoordinatorError):
                primary = runtime.resolve(target.alias)
                status = await runtime.coordinator.status()
                can_fallback = bool(
                    target.key.engine != primary.key.engine
                    and status.initialized
                    and status.accepting
                    and status.resident_alias is None
                )
                if not can_fallback:
                    raise
                # The selected candidate failed before an upstream request
                # existed. Automatic choices lose their stale evidence; an
                # explicit user pin remains saved. Recover this untouched
                # request through the original fixed target.
                runtime.invalidate_automatic_selection(target.alias)
                target = await runtime.resolve_fixed_target(
                    target.alias,
                    endpoint=endpoint,
                )
                performance_timer.engine = target.key.engine.value
                retry_status = await runtime.coordinator.status()
                cold_start = not (
                    retry_status.resident_alias == target.alias
                    and retry_status.resident_engine == target.key.engine
                    and retry_status.state == CoordinatorState.READY
                )
                lease = await runtime.coordinator.acquire(
                    target,
                    timeout_seconds=lease_timeout,
                )
            performance_timer.mark_admitted(cold_start=cold_start)
            route = lease.route(endpoint)
            prepared, requested_model, streamed, client_asked_usage = prepare_request_body(
                body,
                route=route,
                endpoint=endpoint,
                engine=target.key.engine,
            )
            performance_timer.streamed = streamed
            upstream_request = runtime.proxy_client.build_request(
                method=request.method,
                url=f"{route.base_url}{route.path}",
                headers=upstream_headers(request.headers, route),
                content=prepared,
                params=list(request.query_params.multi_items()),
            )
            if usage_reservation is not None:
                await usage_reservation.mark_started()
            if endpoint == Endpoint.IMAGES_GENERATIONS:
                async with asyncio.timeout(
                    runtime.config.server.image_request_timeout_seconds
                ):
                    upstream = await runtime.proxy_client.send(upstream_request, stream=True)
            else:
                upstream = await runtime.proxy_client.send(upstream_request, stream=True)
            performance_timer.mark_upstream_headers()
            if (
                upstream.status_code >= 500
                and runtime.is_engine_alternative(target)
            ):
                # Work may already have begun, so never replay this request.
                # The evidence is removed only for subsequent requests.
                runtime.invalidate_automatic_selection(target.alias)
        except asyncio.CancelledError:
            performance_timer.finish(status_code=499, error_code="cancelled")
            if target.key.engine == EngineName.MFLUX:
                await _abort_image_request_shielded(
                    runtime,
                    upstream,
                    lease,
                    fleet_lease,
                    usage_reservation,
                )
            else:
                await _close_proxy_resources(
                    upstream,
                    lease,
                    fleet_lease,
                    usage_reservation,
                )
            raise
        except TimeoutError as exc:
            performance_timer.finish(status_code=504, error_code="request_timeout")
            if target.key.engine == EngineName.MFLUX:
                await _abort_image_request_shielded(
                    runtime,
                    upstream,
                    lease,
                    fleet_lease,
                    usage_reservation,
                )
            else:
                await _close_proxy_resources(
                    upstream,
                    lease,
                    fleet_lease,
                    usage_reservation,
                )
            raise HTTPException(504, "image generation timed out") from exc
        except QueueFull as exc:
            performance_timer.finish(status_code=429, error_code="queue_full")
            await _close_proxy_resources(
                upstream,
                lease,
                fleet_lease,
                usage_reservation,
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "node_busy",
                    "message": str(exc),
                },
                headers={
                    "Retry-After": "1",
                    "X-Mnemosyne-Error": "node_busy",
                },
            ) from exc
        except HTTPException as exc:
            performance_timer.finish(
                status_code=exc.status_code,
                error_code="request_rejected",
            )
            await _close_proxy_resources(
                upstream,
                lease,
                fleet_lease,
                usage_reservation,
            )
            raise
        except Exception as exc:
            status_code = _error_status(exc)
            performance_timer.finish(
                status_code=status_code,
                error_code=(
                    "upstream_transport"
                    if isinstance(exc, httpx.HTTPError)
                    else "inference_failure"
                ),
            )
            await _close_proxy_resources(
                upstream,
                lease,
                fleet_lease,
                usage_reservation,
            )
            if isinstance(exc, httpx.HTTPError):
                if runtime.is_engine_alternative(target):
                    runtime.invalidate_automatic_selection(target.alias)
                await _audit_after_upstream_failure(runtime)
            raise HTTPException(_error_status(exc), str(exc)) from exc

        assert lease is not None and upstream is not None
        response_headers = downstream_headers(upstream.headers)
        is_event_stream = "text/event-stream" in upstream.headers.get(
            "content-type", ""
        )
        performance_timer.streamed = streamed or is_event_stream
        if streamed or is_event_stream:
            usage_parser = StreamingUsageParser(endpoint=f"/v1/{endpoint.value}")
            event_filter = StreamingEventFilter(
                hide_usage_only_events=(
                    not client_asked_usage
                    and endpoint in {Endpoint.CHAT_COMPLETIONS, Endpoint.COMPLETIONS}
                )
            )
            owner = _StreamingProxyOwnership(
                runtime=runtime,
                upstream=upstream,
                lease=lease,
                fleet_lease=fleet_lease,
                target=target,
                endpoint=endpoint,
                requested_model=requested_model,
                started_at=started_at,
                usage_parser=usage_parser,
                performance_timer=performance_timer,
                usage_event_id=usage_event_id,
                usage_reservation=usage_reservation,
            )
            terminal_gate = (
                _TerminalSSEGate()
                if endpoint != Endpoint.IMAGES_GENERATIONS and is_event_stream
                else None
            )

            async def stream_body():
                try:
                    async for chunk in upstream.aiter_bytes():
                        performance_timer.mark_first_byte()
                        usage_parser.feed(chunk)
                        for forwarded in event_filter.feed(chunk):
                            if terminal_gate is None:
                                yield forwarded
                            else:
                                for visible in terminal_gate.feed(forwarded):
                                    yield visible
                    tail = event_filter.finish()
                    if tail:
                        if terminal_gate is None:
                            yield tail
                        else:
                            for visible in terminal_gate.feed(tail):
                                yield visible
                except asyncio.CancelledError:
                    owner.note_failure(upstream_failed=False)
                    raise
                except BaseException:
                    owner.note_failure(upstream_failed=True)
                    raise
                else:
                    if terminal_gate is not None:
                        terminal = terminal_gate.finish()
                        try:
                            await owner.persist_usage()
                        except BaseException:
                            owner.note_failure(upstream_failed=False)
                            raise
                        if terminal:
                            yield terminal
                finally:
                    await owner.complete()

            return _OwnedStreamingResponse(
                stream_body(),
                owner=owner,
                status_code=upstream.status_code,
                headers=response_headers,
            )

        upstream_failed = False
        body_failed = False
        try:
            content = await upstream.aread()
            performance_timer.mark_first_byte()
            decoded: Mapping[str, Any] | None = None
            try:
                candidate = json.loads(content)
                if isinstance(candidate, Mapping):
                    decoded = candidate
            except (TypeError, ValueError):
                pass
            if endpoint != Endpoint.IMAGES_GENERATIONS:
                normalized_usage = await _record_usage(
                    runtime,
                    decoded,
                    endpoint=endpoint,
                    engine=str(target.key.engine),
                    requested_model=requested_model,
                    alias=target.alias,
                    streamed=False,
                    started_at=started_at,
                    status_code=upstream.status_code,
                    event_id=usage_event_id,
                    reservation=usage_reservation,
                )
            else:
                normalized_usage = None
            if normalized_usage is None and usage_reservation is not None:
                await usage_reservation.finish()
            performance_timer.finish(
                status_code=upstream.status_code,
                error_code=(
                    "upstream_status" if upstream.status_code >= 400 else None
                ),
                completion_tokens=(
                    normalized_usage.completion_tokens
                    if normalized_usage is not None
                    else None
                ),
            )
            return Response(
                content=content,
                status_code=upstream.status_code,
                headers=response_headers,
            )
        except asyncio.CancelledError:
            body_failed = True
            performance_timer.finish(status_code=499, error_code="cancelled")
            raise
        except httpx.HTTPError as exc:
            body_failed = True
            upstream_failed = True
            performance_timer.finish(
                status_code=502,
                error_code="upstream_response",
            )
            raise HTTPException(502, f"upstream response failed: {exc}") from exc
        except Exception:
            body_failed = True
            performance_timer.finish(status_code=500, error_code="response_failure")
            raise
        finally:
            if target.key.engine == EngineName.MFLUX and body_failed:
                await _abort_image_request_shielded(
                    runtime,
                    upstream,
                    lease,
                    fleet_lease,
                    usage_reservation,
                )
            else:
                await _close_proxy_resources(
                    upstream,
                    lease,
                    fleet_lease,
                    usage_reservation,
                )
            if upstream_failed and target.key.engine != EngineName.MFLUX:
                await _audit_after_upstream_failure(runtime)
            if upstream_failed and runtime.is_engine_alternative(target):
                runtime.invalidate_automatic_selection(target.alias)

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

    @app.get("/manager/native-lifecycle")
    async def native_lifecycle_status() -> JSONResponse:
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=await runtime.native_lifecycle_status(),
        )

    @app.post("/manager/native-lifecycle/uninstall/preview")
    async def native_uninstall_preview(
        request: NativeUninstallPreviewRequest,
    ) -> JSONResponse:
        try:
            payload = await runtime.preview_native_uninstall(
                request.retention_mode
            )
        except NativeLifecycleError as exc:
            raise _native_lifecycle_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.post("/manager/native-lifecycle/uninstall/prepare")
    async def native_uninstall_prepare(
        request: NativeUninstallPrepareRequest,
    ) -> JSONResponse:
        try:
            payload = await runtime.prepare_native_uninstall(
                request.transaction_id,
                request.retention_mode,
            )
        except NativeLifecycleError as exc:
            raise _native_lifecycle_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.get("/manager/native-lifecycle/transactions/{transaction_id}")
    async def native_lifecycle_transaction(transaction_id: str) -> JSONResponse:
        try:
            payload = await runtime.native_lifecycle_transaction(transaction_id)
        except NativeLifecycleError as exc:
            raise _native_lifecycle_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.get(
        "/manager/native-lifecycle/transactions/{transaction_id}/authorization"
    )
    async def native_lifecycle_authorization_status(
        transaction_id: str,
    ) -> JSONResponse:
        try:
            payload = await runtime.native_lifecycle_authorization_status(
                transaction_id
            )
        except NativeLifecycleError as exc:
            raise _native_lifecycle_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.post(
        "/manager/native-lifecycle/transactions/{transaction_id}/authorization/challenge"
    )
    async def native_lifecycle_authorization_challenge(
        transaction_id: str,
        _request: NativeLifecycleAuthorizationChallengeRequest,
    ) -> JSONResponse:
        try:
            payload = (
                await runtime.issue_native_lifecycle_authorization_challenge(
                    transaction_id
                )
            )
        except NativeLifecycleError as exc:
            raise _native_lifecycle_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.post(
        "/manager/native-lifecycle/transactions/{transaction_id}/authorization/perform"
    )
    async def native_lifecycle_authorization_perform(
        transaction_id: str,
        _request: NativeLifecycleAuthorizationPerformRequest,
    ) -> JSONResponse:
        try:
            payload = await runtime.perform_native_lifecycle_authorization(
                transaction_id
            )
        except NativeLifecycleError as exc:
            raise _native_lifecycle_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.post(
        "/manager/native-lifecycle/transactions/{transaction_id}/authorization/receipt"
    )
    async def native_lifecycle_authorization_receipt(
        transaction_id: str,
        request: Request,
    ) -> JSONResponse:
        try:
            receipt = await _native_lifecycle_helper_receipt(request)
            payload = (
                await runtime.submit_native_lifecycle_authorization_receipt(
                    transaction_id, receipt
                )
            )
        except NativeLifecycleError as exc:
            raise _native_lifecycle_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.post(
        "/manager/native-lifecycle/transactions/{transaction_id}/authorization/cancel"
    )
    async def native_lifecycle_authorization_cancel(
        transaction_id: str,
        request: NativeLifecycleAuthorizationCancelRequest,
    ) -> JSONResponse:
        try:
            payload = (
                await runtime.cancel_native_lifecycle_authorization_challenge(
                    transaction_id=transaction_id,
                    nonce=request.nonce,
                    session_id=request.session_id,
                )
            )
        except NativeLifecycleError as exc:
            raise _native_lifecycle_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.get("/manager/native-lifecycle/migration/preview")
    async def native_migration_preview() -> JSONResponse:
        try:
            payload = await runtime.preview_native_migration()
        except NativeLifecycleError as exc:
            raise _native_lifecycle_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.get("/manager/fleet/participation")
    async def fleet_participation() -> dict:
        return _fleet_participation_payload(
            await runtime.fleet_participation.status()
        )

    @app.put("/manager/fleet/participation")
    async def set_fleet_participation(
        payload: SetFleetParticipationRequest,
    ) -> dict:
        return _fleet_participation_payload(
            await runtime.fleet_participation.set_joined(payload.enabled)
        )

    @app.get("/manager/fleet/pairing")
    async def fleet_pairing() -> dict[str, object]:
        return await runtime.fleet_pairing_status()

    @app.get("/manager/fleet/inventory")
    async def fleet_inventory() -> JSONResponse:
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=await runtime.mac_inventory_status(),
        )

    @app.get("/manager/fleet/desired-installs")
    async def desired_install_list(
        offset: int = Query(default=0, ge=0, le=10_000),
        limit: int = Query(default=100, ge=1, le=256),
    ) -> JSONResponse:
        try:
            payload = await runtime.list_desired_installs(
                offset=offset,
                limit=limit,
            )
        except DesiredInstallStoreError as exc:
            raise _desired_install_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.get("/manager/fleet/desired-installs/{job_id}")
    async def desired_install_read(job_id: str) -> JSONResponse:
        try:
            payload = await runtime.desired_install(job_id)
        except DesiredInstallStoreError as exc:
            raise _desired_install_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.post("/manager/fleet/desired-installs/{job_id}/refuse")
    async def desired_install_refuse(
        job_id: str,
        request: RefuseDesiredInstallRequest,
    ) -> JSONResponse:
        try:
            payload = await runtime.refuse_desired_install(
                job_id,
                job_revision=request.job_revision,
            )
        except DesiredInstallStoreError as exc:
            raise _desired_install_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.post("/manager/fleet/desired-installs/{job_id}/approve")
    async def desired_install_approve(
        job_id: str,
        request: RefuseDesiredInstallRequest,
    ) -> JSONResponse:
        try:
            payload = await runtime.approve_desired_install(
                job_id,
                job_revision=request.job_revision,
            )
        except (DesiredInstallStoreError, DesiredInstallExecutorError) as exc:
            raise _desired_install_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.post("/manager/fleet/desired-installs/{job_id}/cancel")
    async def desired_install_cancel(
        job_id: str,
        request: RefuseDesiredInstallRequest,
    ) -> JSONResponse:
        try:
            payload = await runtime.cancel_desired_install(
                job_id,
                job_revision=request.job_revision,
            )
        except (DesiredInstallStoreError, DesiredInstallExecutorError) as exc:
            raise _desired_install_http_exception(exc) from exc
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.get("/manager/catalog")
    async def compatibility_catalog_status() -> JSONResponse:
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=await runtime.compatibility_catalog_status(),
        )

    @app.get("/manager/catalog/models")
    async def compatibility_catalog_models() -> JSONResponse:
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=await runtime.compatibility_catalog_metadata(),
        )

    @app.post("/manager/catalog/check")
    async def compatibility_catalog_check() -> JSONResponse:
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=await runtime.check_compatibility_catalog(),
        )

    @app.post("/manager/fleet/pairing/request")
    async def request_fleet_pairing(request: Request):
        payload = await _parse_fleet_pairing_presence_request(request)
        try:
            issued = await runtime.request_fleet_pairing_invitation(
                request_id=payload.request_id,
                hub_origin=payload.hub_origin,
                locator=payload.locator.get_secret_value(),
                transport=payload.transport,
            )
        except PairingClientError as exc:
            return await _pairing_client_error_response(runtime, exc)
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=issued,
        )

    @app.post("/manager/fleet/pairing/begin")
    async def begin_fleet_pairing(request: Request):
        payload = await _parse_fleet_pairing_control_request(request)
        try:
            workflow = await runtime.begin_fleet_pairing(
                _pairing_invitation(payload)
            )
        except PairingClientError as exc:
            return await _pairing_client_error_response(runtime, exc)
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content={
                "schema_version": 1,
                "accepted": True,
                "next_action": None,
                "workflow": workflow,
            },
        )

    @app.post("/manager/fleet/pairing/resume")
    async def resume_fleet_pairing(request: Request):
        payload = await _parse_fleet_pairing_control_request(request)
        try:
            workflow = await runtime.resume_fleet_pairing(
                _pairing_invitation(payload)
            )
        except PairingClientError as exc:
            return await _pairing_client_error_response(runtime, exc)
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content={
                "schema_version": 1,
                "accepted": True,
                "next_action": None,
                "workflow": workflow,
            },
        )

    @app.post("/manager/fleet/pairing/discard-rejected")
    async def discard_rejected_fleet_pairing() -> JSONResponse:
        try:
            pairing = await runtime.discard_rejected_fleet_pairing_attempt()
        except PairingClientError as exc:
            return await _pairing_client_error_response(runtime, exc)
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=pairing,
        )

    @app.post("/manager/fleet/pairing/revoke")
    async def revoke_fleet_pairing(
        payload: FleetPairingManagementRequest,
    ) -> JSONResponse:
        try:
            result = await runtime.revoke_fleet_pairing(
                request_id=payload.request_id,
            )
        except PairingClientError as exc:
            return await _pairing_client_error_response(runtime, exc)
        return JSONResponse(
            headers={"Cache-Control": "no-store"},
            content=result,
        )

    @app.get("/manager/performance")
    async def performance() -> dict:
        return runtime.performance.snapshot()

    @app.get("/manager/benchmarks")
    async def benchmarks(alias: str | None = Query(default=None)) -> dict:
        return runtime.benchmark_snapshot(alias)

    @app.post("/manager/benchmarks/{alias}")
    async def benchmark_model(alias: str, payload: BenchmarkModelRequest) -> dict:
        try:
            return await runtime.benchmark_model(
                alias,
                warmup_runs=payload.warmup_runs,
                sample_runs=payload.sample_runs,
                max_tokens=payload.max_tokens,
            )
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.delete("/manager/benchmarks/{alias}")
    async def clear_benchmarks(alias: str) -> dict:
        return {
            "alias": alias,
            "deleted": runtime.reject_benchmark_evidence(alias),
        }

    @app.get("/manager/contexts")
    async def contexts(alias: str | None = Query(default=None)) -> dict:
        return runtime.context_snapshot(alias)

    @app.post("/manager/contexts/{alias}/profile")
    async def profile_context(alias: str, payload: ContextProfileRequest) -> dict:
        try:
            return await runtime.profile_model_context(
                alias,
                target_tokens=payload.target_tokens,
            )
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.delete("/manager/contexts/{alias}")
    async def clear_contexts(alias: str) -> dict:
        return {
            "alias": alias,
            "deleted": runtime.reject_context_evidence(alias),
        }

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
            (
                config,
                revision,
                deleted_files,
                files_disposition,
            ) = await runtime.delete_managed_model(
                alias,
                expected_revision=payload.revision,
                installation_id=payload.installation_id,
            )
            return {
                "saved": True,
                "applied": True,
                "restart_required": False,
                "model_count": len(config.models),
                "revision": revision,
                "config": config.model_dump(mode="json"),
                "deleted_files": deleted_files,
                "files_disposition": files_disposition,
            }
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.get("/manager/storage")
    async def storage_locations() -> dict:
        async def inspect_location(location) -> dict:
            try:
                status = await runtime.filesystem.inspect(
                    location.path,
                    name=location.name,
                    expected_volume_uuid=location.volume_uuid,
                    scope_id=location.scope_id,
                )
                return status.to_dict()
            except FilesystemProbeError as exc:
                # One stale or temporarily unavailable Finder grant must not
                # turn the complete Settings storage card into an HTTP 500.
                # Retain the exact configured path and return a typed,
                # unavailable row so the user can repair that location.
                return {
                    "name": location.name,
                    "path": str(Path(location.path).expanduser().absolute()),
                    "exists": False,
                    "is_directory": False,
                    "writable": False,
                    "mount_path": None,
                    "volume_uuid": None,
                    "expected_volume_uuid": location.volume_uuid,
                    "volume_matches": False,
                    "total_bytes": None,
                    "free_bytes": None,
                    "diagnostic": str(exc),
                }

        statuses = await asyncio.gather(
            *(inspect_location(location) for location in runtime.config.storage.locations)
        )
        return {
            "default": runtime.config.storage.default,
            "locations": statuses,
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
        scope_id: str | None = None
        try:
            # A selected folder that the unsandboxed service can already use
            # needs no persistent security scope. Persisting one anyway makes
            # ordinary folders depend on a bookmark contract intended for
            # protected/sandboxed access and can force needless reselection.
            unscoped = None
            try:
                unscoped = await runtime.filesystem.inspect(payload.path)
            except FilesystemProbeError:
                pass
            if (
                unscoped is not None
                and unscoped.exists
                and unscoped.is_directory
                and unscoped.writable
                and unscoped.volume_matches
            ):
                value = unscoped.to_dict()
                value["scope_id"] = None
                return value

            if payload.bookmark_data is None:
                if unscoped is not None:
                    return unscoped.to_dict()
                raise FilesystemProbeError(
                    "the selected model folder could not be inspected"
                )

            scope_id = await runtime.register_security_scope(
                payload.path,
                payload.bookmark_data,
            )
            status = await runtime.filesystem.inspect(
                payload.path,
                scope_id=scope_id,
            )
            if not (
                status.exists
                and status.is_directory
                and status.writable
                and status.volume_matches
            ):
                raise FilesystemProbeError(
                    status.diagnostic or "the selected model folder is unavailable"
                )
            value = status.to_dict()
            value["scope_id"] = scope_id
            return value
        except (FilesystemProbeError, ValueError, SecurityScopeError) as exc:
            if scope_id is not None:
                await runtime.discard_security_scope(scope_id)
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
        engine: EngineName | None = None,
        q: str = Query(default="", max_length=200),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> dict:
        if engine is not None and engine not in ACTIVE_ENGINE_NAMES:
            raise HTTPException(410, f"{engine.value} is retired on macOS")
        try:
            if engine is not None:
                models = await asyncio.to_thread(
                    search_models,
                    q,
                    engine=engine,
                    limit=limit,
                )
            else:
                searches = [
                    asyncio.to_thread(
                        search_models,
                        q,
                        engine=candidate,
                        limit=limit,
                    )
                    for candidate in ACTIVE_ENGINE_NAMES
                ]
                grouped = await asyncio.gather(*searches)
                models = [model for group in grouped for model in group]
                models.sort(
                    key=lambda model: (
                        -(model.downloads or 0),
                        model.display_name.casefold(),
                        model.engine,
                        model.filename or "",
                    )
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
        if engine not in ACTIVE_ENGINE_NAMES:
            raise HTTPException(410, f"{engine.value} is retired on macOS")
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
            if payload.engine not in ACTIVE_ENGINE_NAMES:
                raise ValueError(f"{payload.engine.value} is retired on macOS")
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
            elif payload.engine in {EngineName.DS4, EngineName.LLAMA_CPP}:
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
                    served_model_name=(
                        candidate.family
                        if payload.engine == EngineName.DS4
                        else alias
                    ),
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

    @app.get("/manager/runtime-updates/evidence")
    async def runtime_update_evidence() -> dict:
        try:
            return await runtime.runtime_update_evidence()
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.get("/manager/engines/omlx/cache")
    async def omlx_cache_health() -> dict:
        try:
            return await runtime.omlx_cache_health()
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.post("/manager/engines/omlx/cache/reset")
    async def reset_omlx_cache() -> dict:
        try:
            return await runtime.reset_omlx_cache()
        except Exception as exc:
            raise HTTPException(_error_status(exc), str(exc)) from exc

    @app.post("/manager/runtime-updates/{engine}/install")
    async def install_runtime_update(
        engine: str, payload: InstallRuntimeUpdateRequest
    ) -> dict:
        try:
            return await runtime.install_runtime_update(
                engine,
                version=payload.version,
                channel=payload.channel,
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
            raw_config = dict(payload.config)
            if "catalog" not in raw_config:
                # Older signed menu apps do not know this optional field.
                # Preserve the persisted updater/trust policy rather than
                # silently disabling it when those clients save unrelated
                # inference or storage settings.
                persisted, _revision, _applied, _restart = (
                    await runtime.configuration_snapshot()
                )
                raw_config["catalog"] = persisted.catalog.model_dump(
                    mode="json"
                )
            config = MacConfig.model_validate(raw_config)
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
