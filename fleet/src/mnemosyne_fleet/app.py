from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import secrets
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .catalog_service import FleetCatalogService
from .batch import BatchAPIError, BatchManager
from .compatibility_catalog import (
    CatalogStore,
    CatalogVerifier,
    TrustedCatalogKey,
)
from .compatibility_catalog_update import CatalogUpdateClient
from .config import FleetConfig, load_config
from .dashboard import DASHBOARD_HTML
from .desired_install_api import (
    DesiredInstallAPIError,
    build_run_document,
    current_candidate_for_job,
    job_record_payload,
    new_job_id,
    parse_desired_install_intent,
    select_exact_candidate,
    validate_desired_install_intent,
)
from .desired_install_protocol import DesiredInstallProtocolError
from .desired_install_store import (
    DesiredInstallConflictError,
    DesiredInstallIntegrityError,
    DesiredInstallNotFoundError,
    DesiredInstallRecord,
    DesiredInstallStore,
    DesiredInstallStoreError,
)
from .inventory_protocol import InventoryProtocolError, parse_inventory_payload
from .inventory_store import (
    InventoryRecord,
    InventoryStore,
    InventoryStoreConflictError,
    InventoryStoreError,
)
from .locator_policy import LocatorPolicy, LocatorPolicyError
from .model_catalog import ModelCatalogError, UniversalModelCatalog
from .pairing_api import (
    ActivationAcknowledgement,
    ClaimApproval,
    ClaimProvision,
    ClaimRejection,
    EnrollmentPolicyUpdate,
    EnrollmentRevocation,
    EnrollmentSelfManagement,
    InvitationClaim,
    InvitationCreate,
    PairingAPIError,
    bearer_token,
    parse_pairing_payload,
)
from .pairing_coordinator import (
    ActivationProbe,
    PairingCoordinator,
    PairingCoordinatorError,
)
from .pairing_probe import probe_activation_candidate
from .paired_transport import create_pinned_node_client
from .pairing_store import (
    ClaimRecord,
    EnrollmentRecord,
    PairingStore,
    PairingStoreConflictError,
    PairingStoreError,
    PairingStoreIntegrityError,
    PairingStoreTerminalError,
    PairingStoreValidationError,
)
from .placement import (
    PlacementInputError,
    PlacementProtocolError,
    PlacementScorer,
    RecipeRequirements,
)
from .placement_api import (
    PlacementAPIError,
    inventory_backed_candidates,
    new_recommendation_id,
    parse_placement_intent,
)
from .proxy import FleetProxy, error_response
from .registry import NodeRegistry
from .scheduler import Scheduler
from .secret_store import SecretStore, SecretStoreError
from .store import FleetStore
from .usage import UsageReader


ROUTES: dict[str, str] = {
    "/v1/chat/completions": "chat/completions",
    "/v1/completions": "completions",
    "/v1/responses": "responses",
    "/v1/messages": "messages",
    "/v1/embeddings": "embeddings",
    "/v1/rerank": "rerank",
    "/v1/images/generations": "images/generations",
}

FLEET_KEEPALIVE_CONNECTIONS = 20
_log = logging.getLogger("mnemosyne-fleet.app")


async def _unavailable_activation_probe(_candidate) -> None:
    raise PairingCoordinatorError("pairing_transport_unavailable")


def _pairing_json_error(status_code: int, code: str) -> JSONResponse:
    messages = {
        400: "The pairing request is invalid.",
        401: "The pairing credential was not accepted.",
        404: "The pairing resource was not found.",
        409: "The pairing request conflicts with durable state.",
        410: "The pairing transaction is no longer available.",
        413: "The pairing request is too large.",
        415: "The pairing content type or encoding is unsupported.",
        422: "The pairing request failed validation.",
        429: "The pairing service is at its pending limit.",
        503: "The pairing service is temporarily unavailable.",
    }
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "code": code,
                "message": messages.get(
                    status_code,
                    "The pairing request could not be completed.",
                ),
            }
        },
    )


def _pairing_exception_response(
    error: BaseException,
    *,
    public: bool,
) -> JSONResponse:
    if isinstance(error, PairingAPIError):
        return _pairing_json_error(error.status_code, error.code)
    if isinstance(error, LocatorPolicyError):
        if public:
            return _pairing_json_error(401, "pairing_claim_rejected")
        return _pairing_json_error(422, error.code)
    if isinstance(error, PairingStoreValidationError):
        return _pairing_json_error(422, "pairing_invalid_request")
    if isinstance(error, PairingStoreConflictError):
        if error.code == "pairing_pending_limit_reached":
            return _pairing_json_error(429, error.code)
        return _pairing_json_error(409, error.code)
    if isinstance(error, PairingStoreTerminalError):
        if public:
            if error.code in {
                "pairing_claim_rejected",
                "pairing_activation_rejected",
                "pairing_management_authentication_rejected",
            }:
                return _pairing_json_error(401, error.code)
            return _pairing_json_error(410, "pairing_transaction_terminal")
        if error.code.endswith("_unknown"):
            return _pairing_json_error(404, error.code)
        return _pairing_json_error(410, error.code)
    if isinstance(
        error,
        (
            PairingStoreIntegrityError,
            PairingCoordinatorError,
            SecretStoreError,
        ),
    ):
        return _pairing_json_error(503, "pairing_unavailable")
    if isinstance(error, PairingStoreError):
        return _pairing_json_error(503, "pairing_unavailable")
    return _pairing_json_error(503, "pairing_unavailable")


def _inventory_json_error(status_code: int, code: str) -> JSONResponse:
    messages = {
        400: "The inventory document is invalid.",
        401: "The inventory credential was not accepted.",
        404: "The inventory observation was not found.",
        409: "The inventory observation conflicts with newer durable state.",
        413: "The inventory document is too large.",
        415: "The inventory content type or encoding is unsupported.",
        503: "The inventory service is temporarily unavailable.",
    }
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "code": code,
                "message": messages.get(
                    status_code,
                    "The inventory request could not be completed.",
                ),
            }
        },
    )


def _inventory_exception_response(error: BaseException) -> JSONResponse:
    if isinstance(error, InventoryProtocolError):
        return _inventory_json_error(error.status_code, error.code)
    if isinstance(error, InventoryStoreConflictError):
        return _inventory_json_error(409, error.code)
    if isinstance(error, InventoryStoreError):
        if error.code == "inventory_invalid_request":
            return _inventory_json_error(400, error.code)
        return _inventory_json_error(503, "inventory_unavailable")
    if isinstance(
        error,
        (
            PairingStoreError,
            PairingCoordinatorError,
            SecretStoreError,
        ),
    ):
        return _inventory_json_error(503, "inventory_unavailable")
    return _inventory_json_error(503, "inventory_unavailable")


def _catalog_json_error(status_code: int, code: str) -> JSONResponse:
    messages = {
        400: "The catalog request is invalid.",
        401: "The catalog credential was not accepted.",
        404: "The requested catalog entry was not found.",
        413: "The catalog request is too large.",
        415: "The catalog request encoding is unsupported.",
        422: "The requested service contract is not supported.",
        503: "The catalog or placement service is temporarily unavailable.",
    }
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "code": code,
                "message": messages.get(
                    status_code,
                    "The catalog request could not be completed.",
                ),
            }
        },
    )


def _placement_exception_response(error: BaseException) -> JSONResponse:
    if isinstance(error, PlacementAPIError):
        return _catalog_json_error(error.status_code, error.code)
    if isinstance(error, PlacementInputError):
        if error.code in {
            "placement_logical_model_missing",
            "placement_recipe_missing",
        }:
            return _catalog_json_error(404, error.code)
        if error.code in {
            "placement_capability_unsupported",
            "placement_context_unsupported",
            "placement_concurrency_unsupported",
        }:
            return _catalog_json_error(422, error.code)
        if error.code in {
            "placement_request_invalid",
            "placement_request_recipe_mismatch",
        }:
            return _catalog_json_error(400, error.code)
        return _catalog_json_error(503, error.code)
    if isinstance(error, PlacementProtocolError):
        return _catalog_json_error(503, "placement_unavailable")
    if isinstance(error, (InventoryStoreError, PairingStoreError)):
        return _catalog_json_error(503, "placement_unavailable")
    return _catalog_json_error(503, "placement_unavailable")


def _desired_install_json_error(status_code: int, code: str) -> JSONResponse:
    messages = {
        400: "The desired install request is invalid.",
        401: "The desired install credential was not accepted.",
        404: "The desired install job was not found.",
        409: "The desired install request conflicts with current authority.",
        413: "The desired install request is too large.",
        415: "The desired install content type or encoding is unsupported.",
        428: "The current desired install revision is required.",
        429: "The desired install journal is at its active limit.",
        503: "The desired install service is temporarily unavailable.",
    }
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "code": code,
                "message": messages.get(
                    status_code,
                    "The desired install request could not be completed.",
                ),
            }
        },
    )


def _desired_install_exception_response(error: BaseException) -> JSONResponse:
    if isinstance(error, DesiredInstallAPIError):
        return _desired_install_json_error(error.status_code, error.code)
    if isinstance(error, DesiredInstallNotFoundError):
        return _desired_install_json_error(404, error.code)
    if isinstance(error, DesiredInstallConflictError):
        status = (
            429
            if error.code == "desired_install_active_limit_reached"
            else 409
        )
        return _desired_install_json_error(status, error.code)
    if isinstance(error, DesiredInstallProtocolError):
        return _desired_install_json_error(400, error.code)
    if (
        isinstance(error, DesiredInstallStoreError)
        and not isinstance(error, DesiredInstallIntegrityError)
        and error.code
        in {
            "desired_install_invalid_request",
            "desired_install_ack_invalid",
        }
    ):
        return _desired_install_json_error(400, error.code)
    if isinstance(error, PlacementInputError):
        return _placement_exception_response(error)
    if isinstance(
        error,
        (
            DesiredInstallIntegrityError,
            DesiredInstallStoreError,
            InventoryStoreError,
            PairingStoreError,
        ),
    ):
        return _desired_install_json_error(503, "desired_install_unavailable")
    return _desired_install_json_error(503, "desired_install_unavailable")


def _desired_install_expected_revision(request: Request) -> int:
    """Require one strong numeric If-Match value for a job mutation."""

    value = request.headers.get("if-match")
    if value is None:
        raise DesiredInstallAPIError(
            428, "desired_install_revision_required"
        )
    if (
        len(value) < 3
        or value[0] != '"'
        or value[-1] != '"'
        or not value[1:-1].isdigit()
        or value[1:-1].startswith("0")
    ):
        raise DesiredInstallAPIError(
            400, "desired_install_revision_invalid"
        )
    revision = int(value[1:-1])
    if not 1 <= revision <= 2_147_483_647:
        raise DesiredInstallAPIError(
            400, "desired_install_revision_invalid"
        )
    return revision


def _claim_payload(record: ClaimRecord) -> dict[str, object]:
    return {
        "schema_version": 1,
        "claim_id": record.claim_id,
        "invitation_id": record.invitation_id,
        "pairing_id": record.pairing_id,
        "display_name": record.display_name,
        "reporting_node_id": record.reporting_node_id,
        "service_version": record.service_version,
        "platform": record.platform,
        "protocol_version": record.protocol_version,
        "state": record.state,
        "claimed_at": record.claimed_at,
        "expires_at": record.expires_at,
    }


def _enrollment_payload(record: EnrollmentRecord) -> dict[str, object]:
    return {
        "schema_version": 1,
        "pairing_id": record.pairing_id,
        "reporting_node_id": record.reporting_node_id,
        "display_name": record.display_name,
        "platform": record.platform,
        "service_version": record.service_version,
        "protocol_version": record.protocol_version,
        "service_class": record.service_class,
        "state": record.state,
        "hub_enabled": record.hub_enabled,
        "credential_generation": record.credential_generation,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "revoked_at": record.revoked_at,
        "failure_code": record.failure_code,
    }


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization")
    if not value or not value.startswith("Bearer "):
        return None
    return value[7:]


def create_app(
    config: FleetConfig | None = None,
    *,
    config_path: str | None = None,
    registry_client: httpx.AsyncClient | None = None,
    proxy_client: httpx.AsyncClient | None = None,
    start_polling: bool = True,
    pairing_locator_policy: LocatorPolicy | None = None,
    pairing_activation_probe: ActivationProbe | None = None,
    paired_registry_client_factory: Callable[
        ..., httpx.AsyncClient
    ] = create_pinned_node_client,
    paired_proxy_client_factory: Callable[
        ..., httpx.AsyncClient
    ] = create_pinned_node_client,
    catalog_update_transport: httpx.AsyncBaseTransport | None = None,
    catalog_clock: Callable[[], int | float] = time.time,
    start_catalog_updates: bool = True,
) -> FastAPI:
    if config is None:
        path = config_path or str(
            Path.home() / ".config" / "mnemosyne-fleet" / "config.toml"
        )
        config = load_config(path)
    if config.placement.remote_installs_enabled and not (
        config.catalog.enabled and config.pairing.enabled
    ):
        raise ValueError(
            "remote-install placement requires catalog and pairing"
        )
    if config.pairing.enabled and pairing_locator_policy is None:
        pairing = config.pairing
        pairing_locator_policy = LocatorPolicy(
            cidr_allowlists={
                "https": pairing.https_cidr_allowlist,
                "tailscale": pairing.tailscale_cidr_allowlist,
                "trusted_lan_http": pairing.trusted_lan_http_cidr_allowlist,
            },
            allowed_ports=pairing.allowed_node_ports,
            resolution_timeout_seconds=pairing.dns_resolution_timeout_seconds,
        )
    owned_registry_client = registry_client is None
    owned_proxy_client = proxy_client is None
    registry_client = registry_client or httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=None,
            max_keepalive_connections=FLEET_KEEPALIVE_CONNECTIONS,
        ),
    )
    proxy_client = proxy_client or httpx.AsyncClient(
        timeout=httpx.Timeout(config.server.request_timeout_seconds, connect=5.0),
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=None,
            max_keepalive_connections=FLEET_KEEPALIVE_CONNECTIONS,
        ),
    )
    store = FleetStore(
        config.server.database_path,
        history_limit=config.server.route_history_limit,
    )
    registry = NodeRegistry(
        nodes=config.nodes,
        client=registry_client,
        poll_interval_seconds=config.server.poll_interval_seconds,
        ttl_seconds=config.server.snapshot_ttl_seconds,
        paired_locator_policy=pairing_locator_policy,
        paired_client_factory=paired_registry_client_factory,
    )
    scheduler = Scheduler(registry=registry, models=config.models, nodes=config.nodes)
    model_catalog = UniversalModelCatalog(
        store=store,
        scheduler=scheduler,
        registry=registry,
        configured_models=config.models,
    )

    async def registry_changed() -> None:
        try:
            if model_catalog.initialized:
                await model_catalog.reconcile()
        finally:
            await scheduler.wake()

    registry.set_on_change(registry_changed)
    proxy = FleetProxy(
        scheduler=scheduler,
        store=store,
        client=proxy_client,
        max_body_bytes=config.server.max_body_bytes,
        paired_locator_policy=pairing_locator_policy,
        paired_client_factory=paired_proxy_client_factory,
    )
    batches = BatchManager(
        proxy=proxy,
        routes=ROUTES,
        config=config.batch,
        max_submission_bytes=config.server.max_body_bytes,
    )
    usage = UsageReader(config.ledger.dsn)
    overview_rate_cache: dict[str, object] = {
        "rows": {},
        "error_code": None,
    }
    pairing_coordinator: PairingCoordinator | None = None
    inventory_store: InventoryStore | None = None
    desired_install_store: DesiredInstallStore | None = None
    pairing_runtime: dict[str, object] = {
        "enabled": config.pairing.enabled,
        "available": False,
        "error_code": None,
    }
    inventory_runtime: dict[str, object] = {
        "enabled": config.pairing.enabled,
        "available": False,
        "error_code": None,
    }
    catalog_service: FleetCatalogService | None = None
    if config.catalog.enabled:
        catalog = config.catalog
        if (
            catalog.state_directory is None
            or catalog.update_origin is None
            or catalog.update_path is None
            or not catalog.trusted_keys
        ):
            raise ValueError("enabled catalog configuration is incomplete")
        trusted_keys = {
            row.key_id: TrustedCatalogKey.from_base64url(
                key_id=row.key_id,
                public_key=row.public_key,
                valid_from=row.valid_from,
                valid_until=row.valid_until,
                minimum_catalog_sequence=row.minimum_catalog_sequence,
                maximum_catalog_sequence=row.maximum_catalog_sequence,
            )
            for row in catalog.trusted_keys
        }
        catalog_store = CatalogStore(
            catalog.state_directory,
            CatalogVerifier(trusted_keys),
        )
        catalog_updater = CatalogUpdateClient(
            store=catalog_store,
            origin=catalog.update_origin,
            path=catalog.update_path,
            total_timeout_seconds=catalog.total_timeout_seconds,
            connect_timeout_seconds=catalog.connect_timeout_seconds,
            max_attempts=catalog.max_attempts,
            retry_delay_seconds=catalog.retry_delay_seconds,
            transport=catalog_update_transport,
            clock=catalog_clock,
        )
        catalog_service = FleetCatalogService(
            store=catalog_store,
            updater=catalog_updater,
            update_interval_seconds=catalog.update_interval_seconds,
            clock=catalog_clock,
        )
    if config.pairing.enabled:
        pairing = config.pairing
        if (
            pairing.metadata_database_path is None
            or pairing.secret_database_path is None
            or pairing.master_key is None
        ):
            raise ValueError("enabled pairing configuration is incomplete")
        if pairing_locator_policy is None:  # pragma: no cover - proven above
            raise ValueError("enabled pairing locator policy is unavailable")
        if pairing_activation_probe is None:
            async def pairing_activation_probe(candidate) -> None:
                await probe_activation_candidate(
                    candidate,
                    timeout_seconds=pairing.activation_timeout_seconds,
                )
        secret_store = SecretStore(
            pairing.secret_database_path,
            store_id=pairing.secret_store_id,
            master_key=pairing.master_key,
        )
        pairing_store = PairingStore(
            pairing.metadata_database_path,
            store_id="mnemosyne-fleet-pairing-metadata-v1",
            secret_store=secret_store,
        )
        forbidden_credentials = [
            config.server.api_key,
            config.server.admin_api_key,
            pairing.master_key,
        ]
        forbidden_credentials.extend(
            credential
            for node in config.nodes
            for credential in (node.fleet_token, node.inference_token)
        )
        if config.ledger.dsn:
            forbidden_credentials.append(config.ledger.dsn)
        pairing_coordinator = PairingCoordinator(
            pairing_store=pairing_store,
            secret_store=secret_store,
            locator_policy=pairing_locator_policy,
            registry=registry,
            activation_probe=pairing_activation_probe,
            forbidden_credentials=forbidden_credentials,
        )
        inventory_store = InventoryStore(
            pairing.inventory_database_path
            or config.server.database_path.parent / "mac-inventory.db",
            freshness_ttl_seconds=pairing.inventory_ttl_seconds,
        )
    desired_install_runtime: dict[str, object] = {
        "enabled": config.placement.remote_installs_enabled,
        "available": False,
        "error_code": None,
    }
    if config.placement.remote_installs_enabled:
        database_path = config.placement.desired_install_database_path
        if database_path is None:
            database_path = (
                config.server.database_path.parent
                / "private"
                / "desired-installs.db"
            )
        desired_install_store = DesiredInstallStore(
            database_path,
            maximum_active_jobs=(
                config.placement.maximum_active_desired_installs
            ),
            history_limit=config.placement.desired_install_history_limit,
            wall_clock=catalog_clock,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await store.initialize(
            node_ids=tuple(
                node.reporting_node_id for node in registry.enrollments()
            ),
            models=tuple(
                (model.name, model.deployment_id) for model in config.models
            ),
        )
        await model_catalog.initialize()
        if inventory_store is not None:
            try:
                await inventory_store.initialize()
            except Exception:
                inventory_runtime["available"] = False
                inventory_runtime["error_code"] = "inventory_unavailable"
                _log.error("Fleet inventory initialization failed closed")
            else:
                inventory_runtime["available"] = True
                inventory_runtime["error_code"] = None
        if pairing_coordinator is not None:
            try:
                reconciliation = await pairing_coordinator.initialize()
            except Exception:
                pairing_runtime["available"] = False
                pairing_runtime["error_code"] = "pairing_unavailable"
                _log.error("Fleet pairing initialization failed closed")
            else:
                pairing_runtime["available"] = True
                pairing_runtime["error_code"] = None
                pairing_runtime["published"] = reconciliation.published
                pairing_runtime["failed_closed"] = reconciliation.failed_closed
        if desired_install_store is not None:
            try:
                await desired_install_store.initialize()
            except Exception:
                desired_install_runtime["available"] = False
                desired_install_runtime["error_code"] = (
                    "desired_install_unavailable"
                )
                _log.error("Fleet desired-install initialization failed closed")
            else:
                desired_install_runtime["available"] = True
                desired_install_runtime["error_code"] = None
        if catalog_service is not None:
            await catalog_service.initialize()
            if start_catalog_updates:
                await catalog_service.start()
        if start_polling:
            await registry.start()
        try:
            yield
        finally:
            await batches.shutdown()
            await registry.stop()
            if catalog_service is not None:
                await catalog_service.stop()
            if owned_registry_client:
                await registry_client.aclose()
            if owned_proxy_client:
                await proxy_client.aclose()

    app = FastAPI(
        title="Mnemosyne Fleet",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.registry = registry
    app.state.scheduler = scheduler
    app.state.model_catalog = model_catalog
    app.state.store = store
    app.state.proxy = proxy
    app.state.batches = batches
    app.state.usage = usage
    app.state.pairing = pairing_coordinator
    app.state.pairing_runtime = pairing_runtime
    app.state.inventory = inventory_store
    app.state.inventory_runtime = inventory_runtime
    app.state.catalog = catalog_service
    app.state.desired_installs = desired_install_store
    app.state.desired_install_runtime = desired_install_runtime

    @app.middleware("http")
    async def fleet_security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/fleet"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "DENY"
            if "content-security-policy" not in response.headers:
                response.headers["Content-Security-Policy"] = (
                    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                )
        return response

    async def require_inference(request: Request) -> None:
        token = _bearer(request)
        if token is None or not hmac.compare_digest(token, config.server.api_key):
            raise _unauthorized()

    async def require_admin(request: Request) -> None:
        token = _bearer(request)
        if token is None or not hmac.compare_digest(token, config.server.admin_api_key):
            raise _unauthorized()

    def pairing_service() -> PairingCoordinator:
        if (
            pairing_coordinator is None
            or pairing_runtime.get("available") is not True
        ):
            raise PairingCoordinatorError("pairing_unavailable")
        return pairing_coordinator

    def inventory_service() -> tuple[PairingCoordinator, InventoryStore]:
        if (
            inventory_store is None
            or inventory_runtime.get("available") is not True
        ):
            raise InventoryStoreError("inventory_unavailable")
        return pairing_service(), inventory_store

    def catalog_runtime() -> FleetCatalogService:
        if catalog_service is None:
            raise RuntimeError("catalog_unavailable")
        return catalog_service

    def desired_install_service() -> DesiredInstallStore:
        if (
            desired_install_store is None
            or desired_install_runtime.get("available") is not True
        ):
            raise DesiredInstallIntegrityError(
                "desired_install_store_unavailable"
            )
        return desired_install_store

    def mac_pool_status_payload() -> dict[str, object]:
        """Expose only feature state needed by the path-free admin UI.

        These switches deliberately describe management-side availability;
        they do not add a node to the inference registry, grant placement
        authority, or reveal any configured endpoint, credential, or local
        state path.
        """

        catalog_status = (
            None if catalog_service is None else catalog_service.status_payload()
        )
        catalog_available = bool(
            catalog_status is not None and catalog_status.get("available") is True
        )
        pairing_available = pairing_runtime.get("available") is True
        inventory_available = inventory_runtime.get("available") is True
        desired_available = desired_install_runtime.get("available") is True
        return {
            "schema_version": 1,
            "pairing": {
                "enabled": bool(pairing_runtime.get("enabled")),
                "available": pairing_available,
                "error_code": pairing_runtime.get("error_code"),
            },
            "inventory": {
                "enabled": bool(inventory_runtime.get("enabled")),
                "available": inventory_available,
                "error_code": inventory_runtime.get("error_code"),
            },
            "catalog": {
                "enabled": config.catalog.enabled,
                "available": catalog_available,
                "error_code": (
                    None
                    if catalog_status is None
                    else catalog_status.get("load_error_code")
                ),
            },
            "remote_installs": {
                "enabled": bool(desired_install_runtime.get("enabled")),
                "available": bool(
                    desired_available
                    and pairing_available
                    and inventory_available
                    and catalog_available
                ),
                "error_code": desired_install_runtime.get("error_code"),
            },
        }

    async def recompute_desired_placement(intent, *, created_at: float):
        active = await catalog_runtime().current()
        placement_request = intent.placement_request(
            recommendation_id=new_recommendation_id(),
            created_at=created_at,
            valid_for_seconds=config.placement.recommendation_valid_seconds,
        )
        requirements = RecipeRequirements.from_verified_catalog(
            active,
            request=placement_request,
            runtime_install_mode="not_allowed",
        )
        coordinator, inventory = inventory_service()
        candidates = await inventory_backed_candidates(
            coordinator=coordinator,
            inventory_store=inventory,
        )
        recommendation = PlacementScorer().score(
            placement_request,
            requirements,
            candidates,
        )
        return requirements, recommendation

    def desired_requirements_match(
        record: DesiredInstallRecord,
        requirements: RecipeRequirements,
    ) -> bool:
        document = record.document.value
        return (
            document["catalog_version"] == requirements.catalog_version
            and document["catalog_digest"] == requirements.catalog_digest
            and document["logical_model_id"] == requirements.logical_model_id
            and document["recipe_id"] == requirements.recipe_id
            and document["artifact_id"] == requirements.artifact_id
            and document["engine"] == requirements.engine
            and document["capabilities"] == list(requirements.capabilities)
            and document["guaranteed_context_tokens"]
            == requirements.guaranteed_context_tokens
        )

    async def desired_jobs_for_inventory(document) -> list[dict[str, object]]:
        if 1 not in document.value["service"]["supported_job_versions"]:
            return []
        journal = desired_install_service()
        records = await journal.pending_for_delivery(
            pairing_id=document.pairing_id,
            credential_generation=document.credential_generation,
            inventory_instance_id=document.inventory_instance_id,
            inventory_sequence=document.inventory_sequence,
            limit=64,
        )
        if not records:
            return []
        created_at = float(catalog_clock())
        delivered: list[dict[str, object]] = []
        for record in records:
            try:
                intent = validate_desired_install_intent(record.intent)
                requirements, recommendation = await recompute_desired_placement(
                    intent,
                    created_at=created_at,
                )
                if not desired_requirements_match(record, requirements):
                    continue
                if record.document.desired_state == "run":
                    current_candidate_for_job(recommendation, record)
                else:
                    # Cancellation never authorizes cleanup or deletion. It is
                    # delivered only to the exact still-enrolled identity and
                    # storage binding. Resource pressure/runtime gates are not
                    # grounds to suppress a stop command.
                    matches = [
                        candidate
                        for candidate in recommendation.value["candidates"]
                        if (
                            candidate["basis"]["pairing_id"]
                            == record.document.pairing_id
                            and candidate["basis"]["credential_generation"]
                            == record.document.credential_generation
                            and candidate["basis"]["inventory_instance_id"]
                            == record.document.inventory_instance_id
                            and candidate["basis"]["inventory_sequence"]
                            >= record.document.inventory_sequence
                            and candidate["basis"]["storage_location_id"]
                            == record.document.storage_location_id
                            and candidate["basis"]["storage_binding_generation"]
                            == record.document.storage_binding_generation
                            and candidate["basis"]["catalog_digest"]
                            == record.document.value["catalog_digest"]
                        )
                    ]
                    identity_gates = {
                        "catalog_mismatch",
                        "credential_generation_changed",
                        "hub_remote_installs_disabled",
                        "hub_restarted",
                        "inventory_identity_mismatch",
                        "pairing_inactive",
                        "pairing_revoked",
                        "stale_inventory",
                        "storage_binding_changed",
                    }
                    if len(matches) != 1 or identity_gates.intersection(
                        matches[0]["hard_gate_codes"]
                    ):
                        continue
            except (
                DesiredInstallAPIError,
                PlacementInputError,
                PlacementProtocolError,
            ):
                continue
            marked = await journal.mark_delivered(
                job_id=record.document.job_id,
                job_revision=record.document.job_revision,
                delivered_at=created_at,
            )
            delivered.append(marked.document.value)
        return delivered

    def inventory_record_payload(
        record: InventoryRecord,
        enrollment: EnrollmentRecord | None,
        *,
        include_inventory: bool,
    ) -> dict[str, object]:
        _, inventory = inventory_service()
        active = (
            enrollment is not None
            and enrollment.lifecycle_state == "active"
        )
        generation = (
            None if enrollment is None else enrollment.credential_generation
        )
        payload: dict[str, object] = {
            "pairing_id": record.pairing_id,
            "credential_generation": record.credential_generation,
            "inventory_instance_id": record.inventory_instance_id,
            "inventory_sequence": record.inventory_sequence,
            "observed_at": record.observed_at,
            "received_at": record.received_at,
            "freshness": inventory.freshness(
                record,
                enrollment_active=active,
                active_credential_generation=generation,
            ),
            "summary": {
                "service": record.inventory["service"],
                "hardware": record.inventory["hardware"],
                "participation": record.inventory["participation"],
                "usage_delivery": record.inventory["usage_delivery"],
                "storage_location_count": len(
                    record.inventory["storage_locations"]
                ),
                "runtime_count": len(record.inventory["runtimes"]),
                "installation_count": len(record.inventory["installations"]),
                "job_acknowledgement_count": len(
                    record.inventory["job_acknowledgements"]
                ),
            },
        }
        if include_inventory:
            payload["inventory"] = record.inventory
        return payload

    async def fleet_overview_payload(
        *,
        refresh_token_rates: bool = True,
    ) -> dict[str, object]:
        """Join secret-free live, inventory, route, and ledger observations."""

        node_rows = registry.status()
        scheduler_row = scheduler.status()
        recent_routes = await store.recent_routes(limit=500)
        inventories: dict[str, dict[str, object]] = {}
        if (
            inventory_store is not None
            and inventory_runtime.get("available") is True
        ):
            try:
                records = await inventory_store.records(limit=1000)
            except InventoryStoreError:
                records = ()
            inventories = {
                record.pairing_id: record.inventory for record in records
            }

        rate_by_node: dict[object, dict[str, object]] = {}
        rate_error_code: str | None = None
        if usage.configured:
            if refresh_token_rates:
                try:
                    rate_by_node = {
                        row["node_id"]: row
                        for row in await usage.token_rates(minutes=5)
                    }
                    overview_rate_cache["rows"] = rate_by_node
                    overview_rate_cache["error_code"] = None
                except Exception:
                    rate_error_code = "ledger_unavailable"
                    overview_rate_cache["error_code"] = rate_error_code
            else:
                rate_by_node = overview_rate_cache["rows"]
                rate_error_code = overview_rate_cache["error_code"]

        errors_by_enrollment: dict[object, list[dict[str, object]]] = {}
        for route in recent_routes:
            if route["failure_code"] is None and (
                route["status_code"] is None
                or int(route["status_code"]) < 400
            ):
                continue
            errors = errors_by_enrollment.setdefault(
                route["enrollment_id"],
                [],
            )
            if len(errors) < 3:
                errors.append(
                    {
                        "observed_at": route["completed_at"] or route["started_at"],
                        "code": route["failure_code"]
                        or f"http_{route['status_code']}",
                        "model": route["public_model"],
                    }
                )

        overview_nodes: list[dict[str, object]] = []
        active_by_enrollment = scheduler_row["active_by_enrollment"]
        for node in node_rows:
            enrollment_id = node["enrollment_id"]
            inventory = inventories.get(str(enrollment_id))
            participation = (
                None if inventory is None else inventory["participation"]
            )
            installations = (
                [] if inventory is None else inventory["installations"]
            )
            storage_locations = (
                [] if inventory is None else inventory["storage_locations"]
            )
            installed_models = [
                {
                    "name": (
                        (installation.get("aliases") or [None])[0]
                        or installation.get("logical_model_id")
                        or installation["installation_id"]
                    ),
                    "engine": installation["engine"],
                    "residency": installation["residency"],
                    "availability": installation["availability"],
                }
                for installation in installations[:256]
            ]
            if inventory is None:
                installed_models = [
                    {
                        "name": deployment["alias"],
                        "engine": deployment["engine"],
                        "residency": "warm" if deployment["warm"] else "cold",
                        "availability": (
                            "available" if deployment["loadable"] else "unavailable"
                        ),
                    }
                    for deployment in node.get("deployments", [])[:256]
                ]
            available_storage = [
                storage
                for storage in storage_locations
                if storage["availability"] == "available"
            ]
            rate = rate_by_node.get(node["reporting_node_id"])
            joined_state = (
                participation["state"]
                if participation is not None
                else (
                    "joined"
                    if node.get("health", {}).get("accepting") is True
                    else "paused"
                )
            )
            overview_nodes.append(
                {
                    "node_id": node["node_id"],
                    "enrollment_id": enrollment_id,
                    "source": node["source"],
                    "service_class": node["service_class"],
                    "online": node["online"],
                    "joined_state": joined_state,
                    "last_seen": node["last_seen"],
                    "hardware": None if inventory is None else inventory["hardware"],
                    "storage": {
                        "location_count": len(storage_locations),
                        "available_location_count": len(available_storage),
                        "free_bytes": None if inventory is None else sum(
                            int(storage["free_bytes"] or 0)
                            for storage in available_storage
                        ),
                        "total_bytes": None if inventory is None else sum(
                            int(storage["total_bytes"] or 0)
                            for storage in available_storage
                        ),
                    },
                    "installed_model_count": len(installations)
                    if inventory is not None
                    else len(node.get("deployments", [])),
                    "installed_models": installed_models,
                    "installed_models_truncated": (
                        len(installations)
                        if inventory is not None
                        else len(node.get("deployments", []))
                    )
                    > 256,
                    "resident_model": {
                        "alias": node.get("residency", {}).get("alias"),
                        "deployment_id": node.get("residency", {}).get(
                            "deployment_id"
                        ),
                        "engine": node.get("residency", {}).get("engine"),
                    },
                    "active_requests": active_by_enrollment.get(enrollment_id, 0),
                    "node_queue_depth": node.get("admission", {}).get(
                        "queue_depth", 0
                    ),
                    "tokens_per_second": None
                    if rate is None
                    else rate["tokens_per_second"],
                    "token_rate_window_seconds": None
                    if rate is None
                    else rate["window_seconds"],
                    "recent_errors": errors_by_enrollment.get(enrollment_id, []),
                    "snapshot_error_code": node["error_code"],
                }
            )

        return {
            "schema_version": 1,
            "observed_at": time.time(),
            "token_rate_configured": usage.configured,
            "token_rate_error_code": rate_error_code,
            "nodes": overview_nodes,
            "totals": {
                "online_nodes": sum(row["online"] is True for row in overview_nodes),
                "joined_nodes": sum(
                    row["joined_state"] == "joined" for row in overview_nodes
                ),
                "active_requests": scheduler_row["active_total"],
                "gateway_queue_depth": sum(
                    queue["depth"] for queue in scheduler_row["queues"].values()
                ),
            },
            "queues": scheduler_row["queues"],
            "batches": batches.summary(),
        }

    if config.catalog.enabled:

        @app.get(
            "/fleet/api/v1/catalog/status",
            dependencies=[Depends(require_admin)],
        )
        async def catalog_status():
            runtime = catalog_runtime()
            await runtime.current()
            return runtime.status_payload()

        @app.get(
            "/fleet/api/v1/catalog/models",
            dependencies=[Depends(require_admin)],
        )
        async def catalog_models(
            offset: int = Query(default=0, ge=0, le=100_000),
            limit: int = Query(default=200, ge=1, le=500),
        ):
            active = await catalog_runtime().current()
            catalog_value = active.catalog()
            rows = catalog_value["logical_models"]
            page = rows[offset : offset + limit]
            next_offset = offset + len(page)
            return {
                "schema_version": 1,
                "catalog_version": active.catalog_version,
                "catalog_digest": active.catalog_digest,
                "catalog_source": active.source,
                "offset": offset,
                "limit": limit,
                "total": len(rows),
                "next_offset": next_offset if next_offset < len(rows) else None,
                "models": page,
            }

        @app.get(
            "/fleet/api/v1/catalog/recipes",
            dependencies=[Depends(require_admin)],
        )
        async def catalog_recipes(
            logical_model_id: str | None = Query(
                default=None,
                min_length=1,
                max_length=128,
            ),
            offset: int = Query(default=0, ge=0, le=100_000),
            limit: int = Query(default=200, ge=1, le=500),
        ):
            active = await catalog_runtime().current()
            catalog_value = active.catalog()
            rows = catalog_value["recipes"]
            if logical_model_id is not None:
                rows = [
                    row
                    for row in rows
                    if row["logical_model_id"] == logical_model_id
                ]
            page = rows[offset : offset + limit]
            next_offset = offset + len(page)
            return {
                "schema_version": 1,
                "catalog_version": active.catalog_version,
                "catalog_digest": active.catalog_digest,
                "catalog_source": active.source,
                "offset": offset,
                "limit": limit,
                "total": len(rows),
                "next_offset": next_offset if next_offset < len(rows) else None,
                "recipes": page,
            }

        @app.post(
            "/fleet/api/v1/catalog/check",
            dependencies=[Depends(require_admin)],
        )
        async def check_catalog_update():
            runtime = catalog_runtime()
            result = await runtime.check()
            payload = {
                "schema_version": 1,
                "result": asdict(result),
                "catalog": runtime.status_payload(),
            }
            if result.outcome == "failed":
                return JSONResponse(status_code=503, content=payload)
            return payload

        if config.placement.remote_installs_enabled:

            @app.post(
                "/fleet/api/v1/placement/recommendations",
                dependencies=[Depends(require_admin)],
            )
            async def recommend_placement(request: Request):
                try:
                    intent = await parse_placement_intent(
                        request,
                        recommendation_id=new_recommendation_id(),
                        created_at=catalog_clock(),
                        valid_for_seconds=(
                            config.placement.recommendation_valid_seconds
                        ),
                    )
                    active = await catalog_runtime().current()
                    requirements = RecipeRequirements.from_verified_catalog(
                        active,
                        request=intent,
                        runtime_install_mode="not_allowed",
                    )
                    coordinator, inventory = inventory_service()
                    candidates = await inventory_backed_candidates(
                        coordinator=coordinator,
                        inventory_store=inventory,
                    )
                    recommendation = PlacementScorer().score(
                        intent,
                        requirements,
                        candidates,
                    )
                except Exception as error:
                    return _placement_exception_response(error)
                return JSONResponse(content=recommendation.value)

            @app.post(
                "/fleet/api/v1/desired-installs",
                dependencies=[Depends(require_admin)],
            )
            async def create_desired_install(request: Request):
                try:
                    intent = await parse_desired_install_intent(request)
                    journal = desired_install_service()
                    replay = await journal.find_idempotent(
                        idempotency_key=intent.idempotency_key,
                        intent_digest=intent.intent_digest,
                    )
                    if replay is not None:
                        payload = job_record_payload(
                            replay,
                            now=float(catalog_clock()),
                        )
                        payload["idempotent_replay"] = True
                        return JSONResponse(content=payload)
                    created_at = float(catalog_clock())
                    if (
                        not math.isfinite(created_at)
                        or not 0 <= created_at <= 4_102_444_800
                    ):
                        raise DesiredInstallAPIError(
                            503, "desired_install_clock_unavailable"
                        )
                    requirements, recommendation = (
                        await recompute_desired_placement(
                            intent,
                            created_at=created_at,
                        )
                    )
                    select_exact_candidate(
                        recommendation,
                        basis=intent.candidate_basis,
                    )
                    _, inventory = inventory_service()
                    selected_inventory = await inventory.record(
                        intent.candidate_basis["pairing_id"]
                    )
                    if (
                        selected_inventory is None
                        or selected_inventory.inventory_instance_id
                        != intent.candidate_basis["inventory_instance_id"]
                        or selected_inventory.inventory_sequence
                        != intent.candidate_basis["inventory_sequence"]
                        or 1
                        not in selected_inventory.inventory["service"][
                            "supported_job_versions"
                        ]
                    ):
                        raise DesiredInstallAPIError(
                            409, "desired_install_basis_changed"
                        )
                    document = build_run_document(
                        intent,
                        requirements,
                        job_id=new_job_id(),
                        created_at=created_at,
                        valid_for_seconds=(
                            config.placement.desired_install_valid_seconds
                        ),
                    )
                    creation = await journal.create(
                        document,
                        intent_digest=intent.intent_digest,
                        intent=intent.value,
                    )
                    payload = job_record_payload(
                        creation.record,
                        now=created_at,
                    )
                    payload["idempotent_replay"] = creation.replayed
                except Exception as error:
                    return _desired_install_exception_response(error)
                return JSONResponse(
                    status_code=200 if creation.replayed else 201,
                    content=payload,
                )

            @app.get(
                "/fleet/api/v1/desired-installs",
                dependencies=[Depends(require_admin)],
            )
            async def list_desired_installs(
                offset: int = Query(default=0, ge=0, le=100_000),
                limit: int = Query(default=100, ge=1, le=500),
            ):
                try:
                    journal = desired_install_service()
                    records, total = await journal.list(
                        offset=offset,
                        limit=limit,
                    )
                    now = float(catalog_clock())
                    if not math.isfinite(now):
                        raise DesiredInstallAPIError(
                            503, "desired_install_clock_unavailable"
                        )
                    jobs = [
                        job_record_payload(record, now=now)
                        for record in records
                    ]
                except Exception as error:
                    return _desired_install_exception_response(error)
                next_offset = offset + len(jobs)
                return {
                    "schema_version": 1,
                    "offset": offset,
                    "limit": limit,
                    "total": total,
                    "next_offset": (
                        next_offset if next_offset < total else None
                    ),
                    "jobs": jobs,
                }

            @app.get(
                "/fleet/api/v1/desired-installs/{job_id}",
                dependencies=[Depends(require_admin)],
            )
            async def get_desired_install(job_id: str):
                try:
                    record = await desired_install_service().get(job_id)
                    if record is None:
                        raise DesiredInstallNotFoundError(
                            "desired_install_job_unknown"
                        )
                    payload = job_record_payload(
                        record,
                        now=float(catalog_clock()),
                    )
                except Exception as error:
                    return _desired_install_exception_response(error)
                return payload

            @app.post(
                "/fleet/api/v1/desired-installs/{job_id}/cancel",
                dependencies=[Depends(require_admin)],
            )
            async def cancel_desired_install(job_id: str, request: Request):
                try:
                    expected_revision = _desired_install_expected_revision(
                        request
                    )
                    declared = request.headers.get("content-length")
                    if declared is not None and int(declared) != 0:
                        raise DesiredInstallAPIError(
                            400, "desired_install_request_invalid"
                        )
                    async for chunk in request.stream():
                        if chunk:
                            raise DesiredInstallAPIError(
                                400, "desired_install_request_invalid"
                            )
                    issued_at = float(catalog_clock())
                    record = await desired_install_service().cancel(
                        job_id,
                        expected_revision=expected_revision,
                        issued_at=issued_at,
                        valid_for_seconds=(
                            config.placement.desired_install_valid_seconds
                        ),
                    )
                    payload = job_record_payload(record, now=issued_at)
                except (TypeError, ValueError):
                    return _desired_install_json_error(
                        400, "desired_install_request_invalid"
                    )
                except Exception as error:
                    return _desired_install_exception_response(error)
                return payload

    if config.pairing.enabled:

        @app.post(
            "/fleet/api/v1/pairing/invitations",
            dependencies=[Depends(require_admin)],
        )
        async def create_pairing_invitation(request: Request):
            try:
                payload = await parse_pairing_payload(request, InvitationCreate)
                issued = await pairing_service().issue_invitation(payload)
            except Exception as error:
                return _pairing_exception_response(error, public=False)
            return JSONResponse(
                status_code=201,
                content={
                    "schema_version": 1,
                    "invitation_id": issued.invitation_id,
                    "pairing_secret": issued.pairing_secret,
                    "hub_origin": config.pairing.public_origin,
                    "expires_at": issued.expires_at,
                    "state": issued.state,
                },
            )

        @app.post("/fleet/pairing/v1/claims")
        async def claim_pairing_invitation(request: Request):
            try:
                payload = await parse_pairing_payload(request, InvitationClaim)
                claim = await pairing_service().claim_invitation(payload)
            except Exception as error:
                return _pairing_exception_response(error, public=True)
            response = _claim_payload(claim)
            response["locator_accepted"] = True
            return response

        @app.get(
            "/fleet/api/v1/pairing/claims",
            dependencies=[Depends(require_admin)],
        )
        async def pending_pairing_claims(
            limit: int = Query(default=100, ge=1, le=1000),
        ):
            try:
                claims = await pairing_service().pending_claims(limit=limit)
            except Exception as error:
                return _pairing_exception_response(error, public=False)
            return {
                "schema_version": 1,
                "claims": [_claim_payload(claim) for claim in claims],
            }

        @app.post(
            "/fleet/api/v1/pairing/claims/{claim_id}/approve",
            dependencies=[Depends(require_admin)],
        )
        async def approve_pairing_claim(claim_id: str, request: Request):
            try:
                payload = await parse_pairing_payload(request, ClaimApproval)
                enrollment = await pairing_service().approve_claim(
                    claim_id,
                    payload,
                )
            except Exception as error:
                return _pairing_exception_response(error, public=False)
            return _enrollment_payload(enrollment)

        @app.post(
            "/fleet/api/v1/pairing/claims/{claim_id}/reject",
            dependencies=[Depends(require_admin)],
        )
        async def reject_pairing_claim(claim_id: str, request: Request):
            try:
                payload = await parse_pairing_payload(request, ClaimRejection)
                await pairing_service().reject_claim(
                    claim_id=claim_id,
                    request_id=payload.request_id,
                )
            except Exception as error:
                return _pairing_exception_response(error, public=False)
            return {
                "schema_version": 1,
                "claim_id": claim_id,
                "state": "rejected",
            }

        @app.post("/fleet/pairing/v1/claims/{claim_id}/provision")
        async def provision_pairing_claim(claim_id: str, request: Request):
            try:
                payload = await parse_pairing_payload(request, ClaimProvision)
                provisioned = await pairing_service().provision_claim(
                    claim_id,
                    payload,
                )
            except Exception as error:
                return _pairing_exception_response(error, public=True)
            bundle = provisioned.credentials
            return {
                "schema_version": 1,
                "claim_id": provisioned.claim_id,
                "pairing_id": provisioned.pairing_id,
                "reporting_node_id": provisioned.reporting_node_id,
                "credential_generation": provisioned.credential_generation,
                "credentials": {
                    "snapshot_bearer": bundle.snapshot.secret,
                    "dispatch_bearer": bundle.dispatch.secret,
                    "management_bearer": bundle.management.secret,
                },
                "state": provisioned.state,
            }

        @app.post(
            "/fleet/management/v1/pairings/{pairing_id}/activation-ack"
        )
        async def acknowledge_pairing_activation(
            pairing_id: str,
            request: Request,
        ):
            try:
                payload = await parse_pairing_payload(
                    request,
                    ActivationAcknowledgement,
                )
                enrollment = await pairing_service().activate(
                    pairing_id=pairing_id,
                    management_bearer=bearer_token(request),
                    payload=payload,
                )
            except Exception as error:
                return _pairing_exception_response(error, public=True)
            response = _enrollment_payload(enrollment)
            response["activation_complete"] = True
            return response

        @app.post(
            "/fleet/management/v1/pairings/{pairing_id}/inventory-sync"
        )
        async def sync_mac_inventory(pairing_id: str, request: Request):
            try:
                try:
                    if str(uuid.UUID(pairing_id)) != pairing_id:
                        raise ValueError
                except (ValueError, AttributeError):
                    return _inventory_json_error(
                        400,
                        "inventory_invalid_request",
                    )
                coordinator, inventory = inventory_service()
                management_bearer = bearer_token(request)
                authenticated = (
                    await coordinator.authenticate_active_management_bearer(
                        pairing_id=pairing_id,
                        management_bearer=management_bearer,
                    )
                )
                if authenticated is None:
                    return _inventory_json_error(
                        401,
                        "inventory_authentication_rejected",
                    )
                document = await parse_inventory_payload(request)
                if (
                    document.pairing_id != pairing_id
                    or document.credential_generation
                    != authenticated.credential_generation
                ):
                    return _inventory_json_error(
                        401,
                        "inventory_authentication_rejected",
                    )
                # Recheck the exact generation after parsing so rotation or
                # revocation cannot race a large document into persistence.
                enrollment = await coordinator.authenticate_active_management(
                    pairing_id=pairing_id,
                    credential_generation=document.credential_generation,
                    management_bearer=management_bearer,
                )
                if enrollment is None:
                    return _inventory_json_error(
                        401,
                        "inventory_authentication_rejected",
                    )
                await inventory.accept(document)
                desired_jobs: list[dict[str, object]] = []
                if config.placement.remote_installs_enabled:
                    journal = desired_install_service()
                    await journal.accept_acknowledgements(
                        pairing_id=document.pairing_id,
                        credential_generation=document.credential_generation,
                        acknowledgements=document.value[
                            "job_acknowledgements"
                        ],
                    )
                    desired_jobs = await desired_jobs_for_inventory(document)
            except (
                DesiredInstallAPIError,
                DesiredInstallProtocolError,
                DesiredInstallStoreError,
            ) as error:
                return _desired_install_exception_response(error)
            except Exception as error:
                return _inventory_exception_response(error)
            return {
                "schema_version": 1,
                "ack": {
                    "pairing_id": document.pairing_id,
                    "credential_generation": document.credential_generation,
                    "inventory_instance_id": document.inventory_instance_id,
                    "inventory_sequence": document.inventory_sequence,
                },
                "desired_jobs": desired_jobs,
            }

        @app.get(
            "/fleet/api/v1/inventory",
            dependencies=[Depends(require_admin)],
        )
        async def list_mac_inventory(
            limit: int = Query(default=100, ge=1, le=1000),
        ):
            try:
                coordinator, inventory = inventory_service()
                enrollments = {
                    enrollment.pairing_id: enrollment
                    for enrollment in await coordinator.enrollments()
                }
                records = await inventory.records(limit=limit)
                devices = [
                    inventory_record_payload(
                        record,
                        enrollments.get(record.pairing_id),
                        include_inventory=False,
                    )
                    for record in records
                ]
            except Exception as error:
                return _inventory_exception_response(error)
            return {
                "schema_version": 1,
                "observed_at": time.time(),
                "devices": devices,
            }

        @app.get(
            "/fleet/api/v1/inventory/{pairing_id}",
            dependencies=[Depends(require_admin)],
        )
        async def read_mac_inventory(pairing_id: str):
            try:
                coordinator, inventory = inventory_service()
                record = await inventory.record(pairing_id)
                if record is None:
                    return _inventory_json_error(
                        404,
                        "inventory_observation_unknown",
                    )
                enrollment = await coordinator.pairing_store.enrollment(
                    pairing_id
                )
                device = inventory_record_payload(
                    record,
                    enrollment,
                    include_inventory=True,
                )
            except Exception as error:
                return _inventory_exception_response(error)
            return {"schema_version": 1, "device": device}

        @app.post(
            "/fleet/management/v1/pairings/{pairing_id}/self-disable"
        )
        async def self_disable_pairing_enrollment(
            pairing_id: str,
            request: Request,
        ):
            try:
                payload = await parse_pairing_payload(
                    request,
                    EnrollmentSelfManagement,
                )
                enrollment = await pairing_service().self_disable(
                    pairing_id=pairing_id,
                    management_bearer=bearer_token(request),
                    payload=payload,
                )
            except Exception as error:
                return _pairing_exception_response(error, public=True)
            return _enrollment_payload(enrollment)

        @app.post(
            "/fleet/management/v1/pairings/{pairing_id}/self-revoke"
        )
        async def self_revoke_pairing_enrollment(
            pairing_id: str,
            request: Request,
        ):
            try:
                payload = await parse_pairing_payload(
                    request,
                    EnrollmentSelfManagement,
                )
                enrollment = await pairing_service().self_revoke(
                    pairing_id=pairing_id,
                    management_bearer=bearer_token(request),
                    payload=payload,
                )
            except Exception as error:
                return _pairing_exception_response(error, public=True)
            return _enrollment_payload(enrollment)

        @app.get(
            "/fleet/api/v1/pairing/enrollments",
            dependencies=[Depends(require_admin)],
        )
        async def pairing_enrollments():
            try:
                enrollments = await pairing_service().enrollments()
            except Exception as error:
                return _pairing_exception_response(error, public=False)
            return {
                "schema_version": 1,
                "enrollments": [
                    _enrollment_payload(enrollment)
                    for enrollment in enrollments
                ],
            }

        @app.put(
            "/fleet/api/v1/pairing/enrollments/{pairing_id}/enabled",
            dependencies=[Depends(require_admin)],
        )
        async def update_pairing_enrollment_policy(
            pairing_id: str,
            request: Request,
        ):
            try:
                payload = await parse_pairing_payload(
                    request,
                    EnrollmentPolicyUpdate,
                )
                enrollment = await pairing_service().set_hub_enabled(
                    pairing_id=pairing_id,
                    request_id=payload.request_id,
                    enabled=payload.enabled,
                )
            except Exception as error:
                return _pairing_exception_response(error, public=False)
            return _enrollment_payload(enrollment)

        @app.post(
            "/fleet/api/v1/pairing/enrollments/{pairing_id}/revoke",
            dependencies=[Depends(require_admin)],
        )
        async def revoke_pairing_enrollment(
            pairing_id: str,
            request: Request,
        ):
            try:
                payload = await parse_pairing_payload(
                    request,
                    EnrollmentRevocation,
                )
                enrollment = await pairing_service().revoke(
                    pairing_id=pairing_id,
                    request_id=payload.request_id,
                )
            except Exception as error:
                return _pairing_exception_response(error, public=False)
            return _enrollment_payload(enrollment)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/fleet/")
    async def dashboard() -> HTMLResponse:
        nonce = secrets.token_urlsafe(18)
        response = HTMLResponse(DASHBOARD_HTML.replace("__CSP_NONCE__", nonce))
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        return response

    @app.get("/v1/models", dependencies=[Depends(require_inference)])
    async def models() -> dict[str, object]:
        matrix = scheduler.model_matrix()
        return {
            "object": "list",
            "data": [
                {
                    "id": row["name"],
                    "object": "model",
                    "owned_by": "mnemosyne-fleet",
                }
                for row in matrix
                if any(node["eligible"] for node in row["nodes"])
            ],
        }

    def batch_error(error: BatchAPIError) -> JSONResponse:
        messages = {
            "batch_disabled": "Fleet batch execution is disabled.",
            "batch_not_found": "The batch was not found or has expired.",
            "batch_active_limit_reached": "Fleet has reached its active batch limit.",
            "batch_request_too_large": "The batch submission is too large.",
            "batch_json_required": "The batch submission must be JSON.",
            "batch_content_encoding_unsupported": "The batch content encoding is unsupported.",
            "batch_invalid_request": "The batch submission is invalid.",
        }
        return JSONResponse(
            status_code=error.status_code,
            headers={"Cache-Control": "no-store"},
            content={
                "error": {
                    "type": "fleet_batch_error",
                    "code": error.code,
                    "message": messages.get(error.code, "The batch request failed."),
                }
            },
        )

    @app.post("/v1/batches", dependencies=[Depends(require_inference)])
    async def create_batch(request: Request):
        try:
            payload = await batches.submit(request)
        except BatchAPIError as error:
            return batch_error(error)
        return JSONResponse(
            status_code=202,
            headers={"Cache-Control": "no-store"},
            content=payload,
        )

    @app.get("/v1/batches/{batch_id}", dependencies=[Depends(require_inference)])
    async def batch_status(batch_id: str):
        try:
            payload = await batches.status(batch_id)
        except BatchAPIError as error:
            return batch_error(error)
        return JSONResponse(headers={"Cache-Control": "no-store"}, content=payload)

    @app.get(
        "/v1/batches/{batch_id}/results",
        dependencies=[Depends(require_inference)],
    )
    async def batch_results(batch_id: str):
        try:
            payload = await batches.results(batch_id)
        except BatchAPIError as error:
            return batch_error(error)
        return JSONResponse(headers={"Cache-Control": "no-store"}, content=payload)

    @app.post(
        "/v1/batches/{batch_id}/cancel",
        dependencies=[Depends(require_inference)],
    )
    async def cancel_batch(batch_id: str):
        try:
            payload = await batches.cancel(batch_id)
        except BatchAPIError as error:
            return batch_error(error)
        return JSONResponse(headers={"Cache-Control": "no-store"}, content=payload)

    async def proxy_route(request: Request):
        capability = ROUTES[request.url.path]
        return await proxy.handle(request, capability=capability)

    for route in ROUTES:
        app.add_api_route(
            route,
            proxy_route,
            methods=["POST"],
            dependencies=[Depends(require_inference)],
        )

    @app.get("/fleet/api/status", dependencies=[Depends(require_admin)])
    async def fleet_status() -> dict[str, object]:
        return {
            "observed_at": time.time(),
            "nodes": registry.status(),
            "models": scheduler.model_matrix(),
            "scheduler": scheduler.status(),
            "batches": batches.summary(),
            "routes": await store.recent_routes(limit=100),
            "model_catalog": await model_catalog.status(),
            "usage_configured": usage.configured,
            "mac_pool": mac_pool_status_payload(),
            "overview": await fleet_overview_payload(),
        }

    @app.get("/fleet/api/overview", dependencies=[Depends(require_admin)])
    async def fleet_overview() -> dict[str, object]:
        return await fleet_overview_payload()

    def model_catalog_error(error: ModelCatalogError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "detail": {
                    "code": error.code,
                    "message": "The universal model catalog request was not accepted.",
                }
            },
        )

    async def model_catalog_payload(request: Request) -> dict[str, object]:
        maximum = 64 * 1024
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise ModelCatalogError(
                "model_catalog_content_encoding_unsupported",
                status_code=415,
            )
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != (
            "application/json"
        ):
            raise ModelCatalogError(
                "model_catalog_json_required",
                status_code=415,
            )
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_length = int(declared)
            except ValueError as exc:
                raise ModelCatalogError(
                    "model_catalog_request_invalid",
                    status_code=400,
                ) from exc
            if declared_length < 0 or declared_length > maximum:
                raise ModelCatalogError(
                    "model_catalog_request_too_large",
                    status_code=413,
                )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > maximum:
                raise ModelCatalogError(
                    "model_catalog_request_too_large",
                    status_code=413,
                )
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ModelCatalogError(
                "model_catalog_request_invalid",
                status_code=400,
            ) from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ModelCatalogError(
                "model_catalog_request_invalid",
                status_code=422,
            )
        return value

    @app.get(
        "/fleet/api/model-catalog",
        dependencies=[Depends(require_admin)],
    )
    async def universal_model_catalog() -> dict[str, object]:
        return await model_catalog.status()

    @app.post(
        "/fleet/api/model-catalog",
        dependencies=[Depends(require_admin)],
    )
    async def publish_model_catalog_entry(request: Request):
        try:
            payload = await model_catalog_payload(request)
            allowed = {
                "schema_version",
                "public_model",
                "origin_alias",
                "deployment_id",
                "capabilities",
            }
            if set(payload) != allowed:
                raise ModelCatalogError(
                    "model_catalog_request_invalid",
                    status_code=422,
                )
            public_model = await model_catalog.add(
                public_model=payload.get("public_model"),
                origin_alias=payload.get("origin_alias"),
                deployment_id=payload.get("deployment_id"),
                capabilities=payload.get("capabilities"),
            )
        except ModelCatalogError as error:
            return model_catalog_error(error)
        return JSONResponse(
            status_code=201,
            content={
                "schema_version": 1,
                "public_model": public_model,
                "catalog": await model_catalog.status(),
            },
        )

    @app.post(
        "/fleet/api/model-catalog/remove",
        dependencies=[Depends(require_admin)],
    )
    async def suppress_model_catalog_entry(request: Request):
        try:
            payload = await model_catalog_payload(request)
            if set(payload) != {"schema_version", "public_model"}:
                raise ModelCatalogError(
                    "model_catalog_request_invalid",
                    status_code=422,
                )
            await model_catalog.remove(payload.get("public_model"))
        except ModelCatalogError as error:
            return model_catalog_error(error)
        return Response(status_code=204)

    @app.get("/fleet/api/usage", dependencies=[Depends(require_admin)])
    async def fleet_usage(
        hours: int = Query(default=24, ge=1, le=720),
    ):
        if not usage.configured:
            return {"configured": False, "hours": hours, "rows": []}
        try:
            rows = await usage.aggregate(hours=hours)
        except Exception:
            return JSONResponse(
                status_code=503,
                content={
                    "configured": True,
                    "hours": hours,
                    "rows": [],
                    "error_code": "ledger_unavailable",
                },
            )
        aliases: dict[tuple[object, object], set[object]] = {}
        for model in scheduler.model_matrix():
            for node in model["nodes"]:
                if not node["strict_match"]:
                    continue
                for alias in node["aliases"]:
                    aliases.setdefault(
                        (node["reporting_node_id"], alias),
                        set(),
                    ).add(model["name"])
        for row in rows:
            public_models = sorted(
                str(value)
                for value in aliases.get(
                    (row["node_id"], row["model"]),
                    set(),
                )
            )
            # A single node alias cannot prove which public synonym was used
            # when multiple logical models deliberately map to one strict
            # deployment. Preserve that ambiguity rather than inventing
            # attribution.
            row["public_models"] = public_models
            row["public_model"] = (
                public_models[0] if len(public_models) == 1 else None
            )
        return {"configured": True, "hours": hours, "rows": rows}

    @app.get("/fleet/api/events", dependencies=[Depends(require_admin)])
    async def events(request: Request) -> StreamingResponse:
        async def stream():
            while not await request.is_disconnected():
                payload = {
                    "observed_at": time.time(),
                    "nodes": registry.status(),
                    "models": scheduler.model_matrix(),
                    "model_catalog": await model_catalog.status(),
                    "scheduler": scheduler.status(),
                    "batches": batches.summary(),
                    "overview": await fleet_overview_payload(
                        refresh_token_rates=False
                    ),
                    "routes": await store.recent_routes(limit=100),
                    "mac_pool": mac_pool_status_payload(),
                }
                yield f"event: fleet\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    return app


def _unauthorized():
    from fastapi import HTTPException

    return HTTPException(
        status_code=401,
        detail={
            "error": {
                "message": "A valid bearer token is required.",
                "type": "authentication_error",
                "code": "unauthorized",
            }
        },
        headers={"WWW-Authenticate": "Bearer"},
    )
