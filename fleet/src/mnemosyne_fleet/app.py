from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .config import FleetConfig, load_config
from .dashboard import DASHBOARD_HTML
from .proxy import FleetProxy, error_response
from .registry import NodeRegistry
from .scheduler import Scheduler
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
) -> FastAPI:
    if config is None:
        path = config_path or str(
            Path.home() / ".config" / "mnemosyne-fleet" / "config.toml"
        )
        config = load_config(path)
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
    )
    scheduler = Scheduler(registry=registry, models=config.models, nodes=config.nodes)
    registry.set_on_change(scheduler.wake)
    proxy = FleetProxy(
        scheduler=scheduler,
        store=store,
        client=proxy_client,
        max_body_bytes=config.server.max_body_bytes,
    )
    usage = UsageReader(config.ledger.dsn)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await store.initialize(
            node_ids=tuple(node.node_id for node in config.nodes),
            models=tuple((model.name, model.deployment_id) for model in config.models),
        )
        if start_polling:
            await registry.start()
        try:
            yield
        finally:
            await registry.stop()
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
    app.state.store = store
    app.state.proxy = proxy
    app.state.usage = usage

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
            "routes": await store.recent_routes(limit=100),
            "usage_configured": usage.configured,
        }

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
                    aliases.setdefault((node["node_id"], alias), set()).add(
                        model["name"]
                    )
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
                    "scheduler": scheduler.status(),
                    "routes": await store.recent_routes(limit=100),
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
