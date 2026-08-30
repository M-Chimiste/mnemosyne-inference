from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.requests import Request

from mnemosyne_fleet.app import create_app
from mnemosyne_fleet.config import NodeConfig
from mnemosyne_fleet.locator_policy import LocatorPolicy

from .helpers import fleet_config, snapshot_payload


class BrokenEventStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"id":"started"}\n\n'
        raise httpx.ReadError("connection lost after response bytes")

    async def aclose(self) -> None:
        return None


class FailingBodyStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        if False:
            yield b""
        raise httpx.ReadError("response body failed")

    async def aclose(self) -> None:
        return None


class BlockingEventStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.close_started.set()
        await self.allow_close.wait()
        self.closed.set()


class NeverStartedStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.iterated = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.iterated.set()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed.set()


class UnboundedNodeBusyBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.iterated = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.iterated.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed.set()


class Releasable429Body(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.allow_finish = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        yield b'{"error":"'
        await self.allow_finish.wait()
        yield b'rate_limited"}'

    async def aclose(self) -> None:
        self.closed.set()


class ReleasableSuccessBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.allow_finish = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        yield b'{"id":"'
        await self.allow_finish.wait()
        yield b'completed"}'

    async def aclose(self) -> None:
        self.closed.set()


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def _two_nodes() -> tuple[NodeConfig, NodeConfig]:
    return (
        NodeConfig(
            node_id="a",
            url="http://a",
            fleet_token="fleet-a",
            inference_token="infer-a",
        ),
        NodeConfig(
            node_id="b",
            url="http://b",
            fleet_token="fleet-b",
            inference_token="infer-b",
        ),
    )


async def test_deactivation_after_reservation_prevents_upstream_dispatch(
    tmp_path,
) -> None:
    node = NodeConfig(
        node_id="a",
        url="http://a:1240",
        fleet_token="fleet-a",
        inference_token="infer-a",
        source="paired",
        enrollment_id="11111111-1111-4111-8111-111111111111",
        locator_transport="trusted_lan_http",
    )
    seen_authorization: list[str] = []

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("a"))

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers["authorization"])
        return httpx.Response(200, json={"id": "completed"})

    app = create_app(
        fleet_config(tmp_path, nodes=(node,)),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
        pairing_locator_policy=LocatorPolicy(
            cidr_allowlists={
                "https": (),
                "tailscale": (),
                "trusted_lan_http": ("10.0.0.0/8",),
            },
            allowed_ports=(1240,),
            resolver=lambda _host, _port: ("10.20.30.40",),
        ),
        paired_registry_client_factory=lambda locator, **_kwargs: (
            httpx.AsyncClient(
                base_url=locator.origin,
                transport=httpx.MockTransport(registry_handler),
            )
        ),
        paired_proxy_client_factory=lambda locator, **_kwargs: (
            httpx.AsyncClient(
                base_url=locator.origin,
                transport=httpx.MockTransport(proxy_handler),
            )
        ),
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        original_acquire = app.state.scheduler.acquire

        async def acquire_then_deactivate(**kwargs):
            reservation = await original_acquire(**kwargs)
            assert await app.state.registry.deactivate_enrollment(
                reservation.enrollment_id,
                expected=reservation.enrollment,
            ) is reservation.enrollment
            return reservation

        app.state.scheduler.acquire = acquire_then_deactivate
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            response = await client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer client-key"},
                json={"model": "qwen-coder", "input": "hello"},
            )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "no_eligible_node"
        assert seen_authorization == []
        assert app.state.registry.node_count == 0
        assert app.state.scheduler.status()["active_total"] == 0
        assert await app.state.store.recent_routes(limit=10) == []


async def test_deactivation_after_admission_allows_stream_to_finish(
    tmp_path,
) -> None:
    node = NodeConfig(
        node_id="a",
        url="http://a",
        fleet_token="fleet-a",
        inference_token="infer-a",
    )
    body = ReleasableSuccessBody()

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("a"))

    def proxy_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=body,
        )

    app = create_app(
        fleet_config(tmp_path, nodes=(node,)),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer client-key"},
                    json={"model": "qwen-coder", "input": "hello"},
                )
            )
            await body.started.wait()
            assert app.state.scheduler.status()["active_total"] == 1
            assert await app.state.registry.deactivate_enrollment(
                node.enrollment_id,
                expected=node,
            ) is node
            body.allow_finish.set()
            response = await request

        assert response.status_code == 200
        assert response.json() == {"id": "completed"}
        assert body.closed.is_set()
        assert app.state.scheduler.status()["active_total"] == 0


async def test_broken_stream_after_headers_is_never_retried(tmp_path) -> None:
    nodes = _two_nodes()
    calls: list[str] = []

    def registry_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload(str(request.url.host)))

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.host))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=BrokenEventStream(),
        )

    app = create_app(
        fleet_config(tmp_path, nodes=nodes),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            with pytest.raises(Exception, match="connection lost"):
                await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer client-key"},
                    json={"model": "qwen-coder", "input": "hello", "stream": True},
                )

        routes = await app.state.store.recent_routes(limit=10)
        assert len(routes) == 1
        assert routes[0]["failure_code"] == "upstream_stream_error"

    assert calls == ["a"]


async def test_ambiguous_failure_before_headers_is_not_failed_over(tmp_path) -> None:
    nodes = _two_nodes()
    calls: list[str] = []

    def registry_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload(str(request.url.host)))

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.host))
        raise httpx.ReadTimeout("request may already have reached the node")

    app = create_app(
        fleet_config(tmp_path, nodes=nodes),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            response = await client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer client-key"},
                json={"model": "qwen-coder", "input": "hello"},
            )
            assert response.status_code == 502
            assert response.json()["error"]["code"] == "upstream_failure"

    assert calls == ["a"]


async def test_untrusted_429_body_failure_is_terminal_and_returns_capacity(
    tmp_path,
) -> None:
    nodes = _two_nodes()
    calls: list[str] = []

    def registry_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload(str(request.url.host)))

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.host))
        return httpx.Response(
            429,
            headers={"Content-Type": "application/json"},
            stream=FailingBodyStream(),
        )

    app = create_app(
        fleet_config(tmp_path, nodes=nodes),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            with pytest.raises(httpx.ReadError, match="response body failed"):
                await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer client-key"},
                    json={"model": "qwen-coder", "input": "hello"},
                )
        assert app.state.scheduler.status()["active_total"] == 0
        routes = await app.state.store.recent_routes(limit=10)
        assert routes[0]["failure_code"] == "upstream_stream_error"
    assert calls == ["a"]


async def test_proven_node_busy_closes_without_reading_unbounded_body(
    tmp_path,
) -> None:
    nodes = _two_nodes()
    body = UnboundedNodeBusyBody()
    calls: list[str] = []

    def registry_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload(str(request.url.host)))

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.host))
        if request.url.host == "a":
            return httpx.Response(
                429,
                headers={"X-Mnemosyne-Error": "node_busy"},
                stream=body,
            )
        return httpx.Response(200, json={"ok": True})

    app = create_app(
        fleet_config(tmp_path, nodes=nodes),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            response = await client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer client-key"},
                json={"model": "qwen-coder", "input": "hello"},
            )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert app.state.scheduler.status()["active_total"] == 0

    assert calls == ["a", "b"]
    assert not body.iterated.is_set()
    assert body.closed.is_set()


async def test_all_proven_busy_candidates_return_bounded_fleet_429(
    tmp_path,
) -> None:
    nodes = _two_nodes()
    bodies = {
        node.node_id: UnboundedNodeBusyBody()
        for node in nodes
    }
    calls: list[str] = []

    def registry_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload(str(request.url.host)))

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        node_id = str(request.url.host)
        calls.append(node_id)
        return httpx.Response(
            429,
            headers={"X-Mnemosyne-Error": "node_busy"},
            stream=bodies[node_id],
        )

    app = create_app(
        fleet_config(tmp_path, nodes=nodes),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            response = await client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer client-key"},
                json={"model": "qwen-coder", "input": "hello"},
            )

        assert response.status_code == 429
        assert response.headers["retry-after"] == "1"
        assert response.json()["error"]["code"] == "fleet_capacity_busy"
        assert app.state.scheduler.status()["active_total"] == 0
        routes = await app.state.store.recent_routes(limit=10)
        assert len(routes) == 2
        assert {row["failure_code"] for row in routes} == {"node_busy"}

    assert calls == ["a", "b"]
    assert all(not body.iterated.is_set() for body in bodies.values())
    assert all(body.closed.is_set() for body in bodies.values())


async def test_untrusted_429_stream_holds_reservation_until_complete(
    tmp_path,
) -> None:
    body = Releasable429Body()

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("node-a"))

    def proxy_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Content-Type": "application/json"},
            stream=body,
        )

    app = create_app(
        fleet_config(tmp_path),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer client-key"},
                    json={"model": "qwen-coder", "input": "hello"},
                )
            )
            await body.started.wait()
            assert app.state.scheduler.status()["active_total"] == 1
            body.allow_finish.set()
            response = await task
        assert response.status_code == 429
        assert response.json() == {"error": "rate_limited"}
        assert app.state.scheduler.status()["active_total"] == 0
        assert body.closed.is_set()


async def test_cancellation_during_route_start_cannot_leak_capacity(tmp_path) -> None:
    started = asyncio.Event()
    allow_start = asyncio.Event()
    finished = asyncio.Event()
    finish_codes: list[str | None] = []

    class BlockingStore:
        async def start_route(self, _record) -> None:
            started.set()
            await allow_start.wait()

        async def finish_route(
            self,
            _route_id: str,
            *,
            status_code: int | None,
            failure_code: str | None,
        ) -> None:
            finish_codes.append(failure_code)
            finished.set()

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("node-a"))

    app = create_app(
        fleet_config(tmp_path),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"ok": True})
            )
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        app.state.proxy._store = BlockingStore()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer client-key"},
                    json={"model": "qwen-coder", "input": "hello"},
                )
            )
            await started.wait()
            task.cancel()
            await _wait_for(
                lambda: app.state.scheduler.status()["active_total"] == 0
            )
            # A second cancellation may stop the request task, but the
            # separately owned completion remains responsible for metadata.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            allow_start.set()
            await asyncio.wait_for(finished.wait(), timeout=1)

    assert finish_codes == ["client_cancelled"]


async def test_cancellation_during_upstream_send_returns_capacity(tmp_path) -> None:
    send_started = asyncio.Event()

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("node-a"))

    async def proxy_handler(_request: httpx.Request) -> httpx.Response:
        send_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    app = create_app(
        fleet_config(tmp_path),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer client-key"},
                    json={"model": "qwen-coder", "input": "hello"},
                )
            )
            await send_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert app.state.scheduler.status()["active_total"] == 0
        routes = await app.state.store.recent_routes(limit=10)
        assert routes[0]["failure_code"] == "client_cancelled"


async def test_cancellation_after_headers_before_iterator_returns_capacity(
    tmp_path,
) -> None:
    stream = NeverStartedStream()

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("node-a"))

    def proxy_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream,
        )

    app = create_app(
        fleet_config(tmp_path),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )
    request_body = b'{"model":"qwen-coder","input":"hello","stream":true}'
    request_available = True
    receive_blocker = asyncio.Event()

    async def receive():
        nonlocal request_available
        if request_available:
            request_available = False
            return {
                "type": "http.request",
                "body": request_body,
                "more_body": False,
            }
        await receive_blocker.wait()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/responses",
        "raw_path": b"/v1/responses",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("fleet", 80),
    }
    response_start = asyncio.Event()
    allow_response_start = asyncio.Event()

    async def send(message) -> None:
        if message["type"] == "http.response.start":
            response_start.set()
            await allow_response_start.wait()

    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        response = await app.state.proxy.handle(
            Request(scope, receive),
            capability="responses",
        )
        assert app.state.scheduler.status()["active_total"] == 1
        task = asyncio.create_task(response(scope, receive, send))
        await response_start.wait()
        assert not stream.iterated.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app.state.scheduler.status()["active_total"] == 0
        assert stream.closed.is_set()
        assert not stream.iterated.is_set()
        routes = await app.state.store.recent_routes(limit=10)
        assert routes[0]["failure_code"] == "client_cancelled"


async def test_double_cancelled_stream_releases_before_close_and_successor_runs(
    tmp_path,
) -> None:
    stream = BlockingEventStream()
    call_count = 0

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("node-a"))

    def proxy_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=stream,
            )
        return httpx.Response(200, json={"ok": True})

    app = create_app(
        fleet_config(tmp_path),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            first = asyncio.create_task(
                client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer client-key"},
                    json={"model": "qwen-coder", "input": "first", "stream": True},
                )
            )
            await stream.started.wait()
            first.cancel()
            await asyncio.wait_for(stream.close_started.wait(), timeout=1)
            await _wait_for(
                lambda: app.state.scheduler.status()["active_total"] == 0
            )
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            second = await client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer client-key"},
                json={"model": "qwen-coder", "input": "second"},
            )
            assert second.status_code == 200
            assert second.json() == {"ok": True}

            stream.allow_close.set()
            await asyncio.wait_for(stream.closed.wait(), timeout=1)
            await _wait_for(
                lambda: app.state.scheduler.status()["active_total"] == 0
            )

        routes = await app.state.store.recent_routes(limit=10)
        first_route = next(
            row for row in routes if row["failure_code"] == "client_cancelled"
        )
        assert first_route["status_code"] == 200
    assert call_count == 2
