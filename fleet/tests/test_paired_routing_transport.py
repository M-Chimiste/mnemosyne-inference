from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
from starlette.requests import Request

from mnemosyne_fleet.config import NodeConfig
from mnemosyne_fleet.locator_policy import LocatorPolicy, ResolvedLocator
from mnemosyne_fleet.proxy import FleetProxy
from mnemosyne_fleet.registry import NodeRegistry
from mnemosyne_fleet.scheduler import Scheduler

from .helpers import fleet_config, snapshot_payload


class _CountingClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


class _ClientFactory:
    def __init__(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        self._handler = handler
        self.locators: list[ResolvedLocator] = []
        self.clients: list[_CountingClient] = []

    def __call__(self, locator: ResolvedLocator, **_kwargs) -> httpx.AsyncClient:
        self.locators.append(locator)
        client = _CountingClient(
            base_url=locator.origin,
            transport=httpx.MockTransport(self._handler),
            trust_env=False,
            follow_redirects=False,
        )
        self.clients.append(client)
        return client


class _MemoryStore:
    def __init__(self) -> None:
        self.started = []
        self.finished: list[tuple[str, int | None, str | None]] = []

    async def start_route(self, record) -> None:
        self.started.append(record)

    async def finish_route(
        self,
        route_id: str,
        *,
        status_code: int | None,
        failure_code: str | None,
    ) -> None:
        self.finished.append((route_id, status_code, failure_code))


class _HeldStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.allow_finish = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        yield b'data: {"id":"first"}\n\n'
        await self.allow_finish.wait()
        yield b'data: {"id":"done"}\n\n'

    async def aclose(self) -> None:
        self.closed.set()


class _SequenceResolver:
    def __init__(self, *answers: tuple[str, ...]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> tuple[str, ...]:
        self.calls.append((host, port))
        if not self.answers:
            raise AssertionError("unexpected locator resolution")
        return self.answers.pop(0)


def _policy(resolver) -> LocatorPolicy:
    return LocatorPolicy(
        cidr_allowlists={
            "https": (),
            "tailscale": (),
            "trusted_lan_http": ("10.0.0.0/8",),
        },
        allowed_ports=(1240,),
        resolver=resolver,
    )


def _paired_node() -> NodeConfig:
    return NodeConfig(
        node_id="paired-mac",
        url="http://paired-mac.internal:1240",
        fleet_token="paired-snapshot-secret",
        inference_token="paired-dispatch-secret",
        source="paired",
        enrollment_id="11111111-1111-4111-8111-111111111111",
        locator_transport="trusted_lan_http",
    )


def _request(payload: dict[str, object]) -> tuple[Request, dict, asyncio.Event]:
    body = json.dumps(payload).encode("utf-8")
    available = True
    receive_blocker = asyncio.Event()

    async def receive():
        nonlocal available
        if available:
            available = False
            return {
                "type": "http.request",
                "body": body,
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
    return Request(scope, receive), scope, receive_blocker


async def _paired_scheduler(
    tmp_path,
    *,
    policy: LocatorPolicy,
    polling_factory: _ClientFactory,
) -> tuple[NodeRegistry, Scheduler, httpx.AsyncClient]:
    node = _paired_node()

    def shared_handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("paired credentials reached the shared DNS client")

    shared_client = httpx.AsyncClient(
        transport=httpx.MockTransport(shared_handler)
    )
    config = fleet_config(tmp_path, nodes=(node,))
    registry = NodeRegistry(
        nodes=(node,),
        client=shared_client,
        poll_interval_seconds=config.server.poll_interval_seconds,
        ttl_seconds=config.server.snapshot_ttl_seconds,
        paired_locator_policy=policy,
        paired_client_factory=polling_factory,
    )
    assert await registry.poll_once(node)
    return (
        registry,
        Scheduler(registry=registry, models=config.models, nodes=config.nodes),
        shared_client,
    )


async def test_paired_poll_resolves_again_and_closes_each_fresh_client() -> None:
    resolver = _SequenceResolver(("10.20.30.40",), ("10.20.30.41",))
    sequence = 0
    seen_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sequence
        seen_authorization.append(request.headers["authorization"])
        sequence += 1
        return httpx.Response(
            200,
            json=snapshot_payload("paired-mac", sequence=sequence),
        )

    factory = _ClientFactory(handler)
    node = _paired_node()
    shared_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(
                AssertionError("shared client was used")
            )
        )
    )
    try:
        registry = NodeRegistry(
            nodes=(node,),
            client=shared_client,
            poll_interval_seconds=1,
            ttl_seconds=2,
            paired_locator_policy=_policy(resolver),
            paired_client_factory=factory,
        )
        assert await registry.poll_once(node)
        assert await registry.poll_once(node)
    finally:
        await shared_client.aclose()

    assert [locator.addresses for locator in factory.locators] == [
        ("10.20.30.40",),
        ("10.20.30.41",),
    ]
    assert seen_authorization == [
        "Bearer paired-snapshot-secret",
        "Bearer paired-snapshot-secret",
    ]
    assert [client.close_calls for client in factory.clients] == [1, 1]


async def test_disallowed_rebinding_stops_dispatch_before_credential_send(
    tmp_path,
) -> None:
    resolver = _SequenceResolver(
        ("10.20.30.40",),
        ("192.0.2.10",),
    )

    def poll_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == (
            "Bearer paired-snapshot-secret"
        )
        return httpx.Response(200, json=snapshot_payload("paired-mac"))

    poll_factory = _ClientFactory(poll_handler)
    registry, scheduler, shared_client = await _paired_scheduler(
        tmp_path,
        policy=_policy(resolver),
        polling_factory=poll_factory,
    )
    dispatch_factory = _ClientFactory(
        lambda _request: (_ for _ in ()).throw(
            AssertionError("dispatch credential was sent after rebinding")
        )
    )
    store = _MemoryStore()
    proxy = FleetProxy(
        scheduler=scheduler,
        store=store,  # type: ignore[arg-type]
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: (_ for _ in ()).throw(
                    AssertionError("shared proxy client was used")
                )
            )
        ),
        max_body_bytes=1024 * 1024,
        paired_locator_policy=registry._paired_locator_policy,
        paired_client_factory=dispatch_factory,
    )
    request, _scope, _blocker = _request(
        {"model": "qwen-coder", "input": "hello"}
    )
    try:
        response = await proxy.handle(request, capability="responses")
        assert response.status_code == 503
        assert json.loads(response.body)["error"]["code"] == "no_eligible_node"
        assert scheduler.status()["active_total"] == 0
        assert dispatch_factory.clients == []
        assert len(store.finished) == 1
        assert store.finished[0][2] == "paired_transport_rejected"
    finally:
        await proxy._client.aclose()
        await shared_client.aclose()


async def test_replacement_during_resolution_never_builds_a_stale_client() -> None:
    resolution_started = asyncio.Event()
    allow_old_resolution = asyncio.Event()

    async def resolver(host: str, _port: int) -> tuple[str, ...]:
        if host == "old-mac.internal":
            resolution_started.set()
            await allow_old_resolution.wait()
            return ("10.20.30.40",)
        assert host == "new-mac.internal"
        return ("10.20.30.41",)

    old = NodeConfig(
        node_id="paired-mac",
        url="http://old-mac.internal:1240",
        fleet_token="old-snapshot-secret",
        inference_token="old-dispatch-secret",
        source="paired",
        enrollment_id="11111111-1111-4111-8111-111111111111",
        locator_transport="trusted_lan_http",
    )
    replacement = NodeConfig(
        node_id="paired-mac",
        url="http://new-mac.internal:1240",
        fleet_token="new-snapshot-secret",
        inference_token="new-dispatch-secret",
        source="paired",
        enrollment_id=old.enrollment_id,
        locator_transport="trusted_lan_http",
    )
    seen_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers["authorization"])
        return httpx.Response(200, json=snapshot_payload("paired-mac"))

    factory = _ClientFactory(handler)
    shared_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(
                AssertionError("shared client was used")
            )
        )
    )
    registry = NodeRegistry(
        nodes=(old,),
        client=shared_client,
        poll_interval_seconds=1,
        ttl_seconds=2,
        paired_locator_policy=_policy(resolver),
        paired_client_factory=factory,
    )
    old_poll = asyncio.create_task(registry.poll_once(old))
    await resolution_started.wait()
    assert await registry.deactivate_enrollment(
        old.enrollment_id,
        expected=old,
    ) is old
    await registry.activate_enrollment(replacement)
    allow_old_resolution.set()

    assert await old_poll is False
    assert factory.clients == []
    assert await registry.poll_once(replacement)
    assert [locator.host for locator in factory.locators] == [
        "new-mac.internal"
    ]
    assert seen_authorization == ["Bearer new-snapshot-secret"]
    assert factory.clients[0].close_calls == 1
    await shared_client.aclose()


async def test_paired_redirect_is_terminal_and_closes_one_use_client(
    tmp_path,
) -> None:
    resolver = _SequenceResolver(
        ("10.20.30.40",),
        ("10.20.30.40",),
    )

    def poll_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("paired-mac"))

    registry, scheduler, shared_client = await _paired_scheduler(
        tmp_path,
        policy=_policy(resolver),
        polling_factory=_ClientFactory(poll_handler),
    )

    def reject_redirect(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == (
            "Bearer paired-dispatch-secret"
        )
        raise httpx.RemoteProtocolError("paired_redirect_forbidden")

    dispatch_factory = _ClientFactory(reject_redirect)
    shared_proxy_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(
                AssertionError("shared proxy client was used")
            )
        )
    )
    store = _MemoryStore()
    proxy = FleetProxy(
        scheduler=scheduler,
        store=store,  # type: ignore[arg-type]
        client=shared_proxy_client,
        max_body_bytes=1024 * 1024,
        paired_locator_policy=registry._paired_locator_policy,
        paired_client_factory=dispatch_factory,
    )
    request, _scope, _blocker = _request(
        {"model": "qwen-coder", "input": "hello"}
    )
    try:
        response = await proxy.handle(request, capability="responses")
        assert response.status_code == 502
        assert json.loads(response.body)["error"]["code"] == "upstream_failure"
        assert scheduler.status()["active_total"] == 0
        assert len(dispatch_factory.clients) == 1
        assert dispatch_factory.clients[0].close_calls == 1
        assert store.finished[0][2] == "ambiguous_upstream_failure"
    finally:
        await shared_proxy_client.aclose()
        await shared_client.aclose()


async def test_paired_client_lives_through_complete_response_stream_and_closes_once(
    tmp_path,
) -> None:
    resolver = _SequenceResolver(
        ("10.20.30.40",),
        ("10.20.30.40",),
    )

    def poll_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("paired-mac"))

    registry, scheduler, shared_client = await _paired_scheduler(
        tmp_path,
        policy=_policy(resolver),
        polling_factory=_ClientFactory(poll_handler),
    )
    held_stream = _HeldStream()

    def dispatch_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == (
            "Bearer paired-dispatch-secret"
        )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=held_stream,
        )

    dispatch_factory = _ClientFactory(dispatch_handler)
    shared_proxy_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(
                AssertionError("shared proxy client was used")
            )
        )
    )
    store = _MemoryStore()
    proxy = FleetProxy(
        scheduler=scheduler,
        store=store,  # type: ignore[arg-type]
        client=shared_proxy_client,
        max_body_bytes=1024 * 1024,
        paired_locator_policy=registry._paired_locator_policy,
        paired_client_factory=dispatch_factory,
    )
    request, scope, _receive_blocker = _request(
        {"model": "qwen-coder", "input": "hello", "stream": True}
    )
    response = await proxy.handle(request, capability="responses")
    assert response.status_code == 200
    assert len(dispatch_factory.clients) == 1
    paired_client = dispatch_factory.clients[0]
    assert paired_client.close_calls == 0
    sent: list[dict] = []

    async def send(message) -> None:
        sent.append(message)

    task = asyncio.create_task(response(scope, request.receive, send))
    await held_stream.started.wait()
    assert scheduler.status()["active_total"] == 1
    assert paired_client.close_calls == 0
    assert not paired_client.is_closed

    held_stream.allow_finish.set()
    await task
    assert scheduler.status()["active_total"] == 0
    assert held_stream.closed.is_set()
    assert paired_client.close_calls == 1
    assert paired_client.is_closed
    assert any(message["type"] == "http.response.body" for message in sent)

    # Completion may be requested by more than one cancellation/finalization
    # edge, but the owned cleanup task and client closure remain exactly once.
    await response._owner.complete(status_code=200, failure_code=None)
    assert paired_client.close_calls == 1
    await shared_proxy_client.aclose()
    await shared_client.aclose()


async def test_static_poll_and_dispatch_keep_existing_shared_clients(
    tmp_path,
) -> None:
    resolver_calls = 0

    def resolver(_host: str, _port: int) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        raise AssertionError("static nodes must not resolve through pairing policy")

    policy = _policy(resolver)
    paired_factory_calls = 0

    def paired_factory(_locator, **_kwargs):
        nonlocal paired_factory_calls
        paired_factory_calls += 1
        raise AssertionError("static nodes must not create paired clients")

    node = NodeConfig(
        node_id="static-mac",
        url="http://static-mac",
        fleet_token="static-snapshot-secret",
        inference_token="static-dispatch-secret",
    )

    def poll_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == (
            "Bearer static-snapshot-secret"
        )
        return httpx.Response(200, json=snapshot_payload("static-mac"))

    polling_client = httpx.AsyncClient(
        transport=httpx.MockTransport(poll_handler)
    )
    config = fleet_config(tmp_path, nodes=(node,))
    registry = NodeRegistry(
        nodes=(node,),
        client=polling_client,
        poll_interval_seconds=1,
        ttl_seconds=2,
        paired_locator_policy=policy,
        paired_client_factory=paired_factory,
    )
    assert await registry.poll_once(node)
    scheduler = Scheduler(registry=registry, models=config.models, nodes=config.nodes)

    def dispatch_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == (
            "Bearer static-dispatch-secret"
        )
        return httpx.Response(200, json={"ok": True})

    proxy_client = httpx.AsyncClient(
        transport=httpx.MockTransport(dispatch_handler)
    )
    proxy = FleetProxy(
        scheduler=scheduler,
        store=_MemoryStore(),  # type: ignore[arg-type]
        client=proxy_client,
        max_body_bytes=1024 * 1024,
        paired_locator_policy=policy,
        paired_client_factory=paired_factory,
    )
    request, scope, _receive_blocker = _request(
        {"model": "qwen-coder", "input": "hello"}
    )
    response = await proxy.handle(request, capability="responses")
    sent: list[dict] = []

    async def send(message) -> None:
        sent.append(message)

    await response(scope, request.receive, send)
    assert not polling_client.is_closed
    assert not proxy_client.is_closed
    assert resolver_calls == 0
    assert paired_factory_calls == 0
    assert scheduler.status()["active_total"] == 0
    assert any(message["type"] == "http.response.body" for message in sent)

    await polling_client.aclose()
    await proxy_client.aclose()
