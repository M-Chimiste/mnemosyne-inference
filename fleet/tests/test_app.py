from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3
from dataclasses import replace

import httpx

from mnemosyne_fleet.app import create_app
from mnemosyne_fleet.config import LedgerConfig, NodeConfig

from .helpers import capacity, fleet_config, snapshot_payload


async def test_owned_clients_leave_active_capacity_to_the_scheduler(
    tmp_path,
    monkeypatch,
) -> None:
    captured: list[dict[str, object]] = []
    original_async_client = httpx.AsyncClient

    def recording_async_client(*args, **kwargs):
        captured.append(kwargs)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "mnemosyne_fleet.app.httpx.AsyncClient",
        recording_async_client,
    )
    app = create_app(fleet_config(tmp_path), start_polling=False)

    async with app.router.lifespan_context(app):
        assert len(captured) == 2
        for kwargs in captured:
            limits = kwargs["limits"]
            assert isinstance(limits, httpx.Limits)
            assert limits.max_connections is None
            assert limits.max_keepalive_connections == 20
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False


async def test_proxy_rewrites_only_model_and_uses_inference_secret(tmp_path) -> None:
    snapshot = snapshot_payload("node-a")
    seen: dict[str, object] = {}

    def registry_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fleet-a"
        return httpx.Response(200, json=snapshot)

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        seen["payload"] = json.loads(request.content)
        seen["query"] = request.url.query
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={"id": "result", "usage": {"total_tokens": 3}},
            headers={
                "x-upstream-request-id": "safe-id",
                "set-cookie": "node-cookie=must-not-pass",
            },
        )

    registry_client = httpx.AsyncClient(
        transport=httpx.MockTransport(registry_handler)
    )
    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(proxy_handler))
    config = fleet_config(tmp_path)
    app = create_app(
        config,
        registry_client=registry_client,
        proxy_client=proxy_client,
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
                params={"trace": "safe"},
                headers={
                    "Authorization": "Bearer client-key",
                    "Cookie": "client-session=secret",
                    "X-Api-Key": "another-client-secret",
                    "X-Forwarded-For": "spoofed",
                    "X-Mnemosyne-Error": "node_busy",
                    "X-Mnemosyne-Fleet-Route": "spoofed-route",
                },
                json={
                    "model": "qwen-coder",
                    "input": "content-never-persisted",
                    "temperature": 0.2,
                },
            )
            assert response.status_code == 200
            assert response.headers["x-upstream-request-id"] == "safe-id"
            assert "set-cookie" not in response.headers
            status = await client.get(
                "/fleet/api/status",
                headers={"Authorization": "Bearer admin-key"},
            )
            status_payload = status.json()
            serialized = json.dumps(status_payload)
            assert "fleet-a" not in serialized
            assert "infer-a" not in serialized
            assert "http://node-a" not in serialized
            assert "content-never-persisted" not in serialized
            inventory = status_payload["nodes"][0]["deployments"]
            assert inventory == [
                {
                    "alias": "node-a-qwen",
                    "deployment_id": snapshot["deployments"][0]["deployment_id"],
                    "engine": "llama.cpp",
                    "upstream_model": "org/qwen-coder",
                    "resolved_revision": "a" * 40,
                    "artifact": {
                        "format": "gguf",
                        "quantization": "Q4_K_M",
                        "content_digest": None,
                    },
                    "kind": "language",
                    "capabilities": [
                        "chat/completions",
                        "completions",
                        "responses",
                    ],
                    "load_config_digest": "sha256:" + "b" * 64,
                    "identity_confidence": "authoritative",
                    "fleet_eligible": True,
                    "loadable": True,
                    "warm": True,
                    "capacity": capacity(),
                }
            ]

    assert seen["authorization"] == "Bearer infer-a"
    assert seen["query"] == b"trace=safe"
    assert "cookie" not in seen["headers"]
    assert "x-api-key" not in seen["headers"]
    assert "x-forwarded-for" not in seen["headers"]
    assert "x-mnemosyne-error" not in seen["headers"]
    assert seen["headers"]["x-mnemosyne-fleet-route"] == (
        status_payload["routes"][0]["route_id"]
    )
    assert seen["payload"] == {
        "model": "node-a-qwen",
        "input": "content-never-persisted",
        "temperature": 0.2,
    }
    with sqlite3.connect(config.server.database_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(routes)").fetchall()
        }
        database_text = " ".join(
            str(value)
            for row in conn.execute("SELECT * FROM routes").fetchall()
            for value in row
        )
    assert "prompt" not in columns
    assert "output" not in columns
    assert "content-never-persisted" not in database_text


async def test_explicit_node_busy_can_fail_over_before_work(tmp_path) -> None:
    nodes = (
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
    snapshots = {"a": snapshot_payload("a"), "b": snapshot_payload("b")}
    calls: list[str] = []

    def registry_handler(request: httpx.Request) -> httpx.Response:
        node_id = request.url.host
        return httpx.Response(200, json=snapshots[node_id])

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "a":
            return httpx.Response(
                429,
                json={"detail": {"code": "node_busy"}},
                headers={
                    "Retry-After": "1",
                    "X-Mnemosyne-Error": "node_busy",
                },
            )
        return httpx.Response(200, json={"ok": True})

    registry_client = httpx.AsyncClient(
        transport=httpx.MockTransport(registry_handler)
    )
    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(proxy_handler))
    app = create_app(
        fleet_config(tmp_path, nodes=nodes),
        registry_client=registry_client,
        proxy_client=proxy_client,
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
    assert calls == ["a", "b"]


async def test_body_only_node_busy_is_not_failed_over(tmp_path) -> None:
    nodes = (
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
    snapshots = {"a": snapshot_payload("a"), "b": snapshot_payload("b")}
    calls: list[str] = []

    def registry_handler(request: httpx.Request) -> httpx.Response:
        node_id = request.url.host
        return httpx.Response(200, json=snapshots[node_id])

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(
            429,
            json={"detail": {"code": "node_busy"}},
            headers={"Retry-After": "7"},
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
    assert response.headers["retry-after"] == "7"
    assert response.json()["detail"]["code"] == "node_busy"
    assert calls == ["a"]


async def test_inference_and_admin_endpoints_require_separate_client_keys(tmp_path) -> None:
    app = create_app(fleet_config(tmp_path), start_polling=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            assert (await client.get("/v1/models")).status_code == 401
            assert (
                await client.get(
                    "/fleet/api/status",
                    headers={"Authorization": "Bearer client-key"},
                )
            ).status_code == 401
            assert (
                await client.get(
                    "/fleet/api/status",
                    headers={"Authorization": "Bearer admin-key"},
                )
            ).status_code == 200
            dashboard = await client.get("/fleet/")
            assert dashboard.status_code == 200
            assert "frame-ancestors 'none'" in dashboard.headers[
                "content-security-policy"
            ]
            assert dashboard.headers["x-content-type-options"] == "nosniff"
            assert dashboard.headers["referrer-policy"] == "no-referrer"
            assert dashboard.headers["cache-control"] == "no-store"
            assert "Discovered node model inventory" in dashboard.text
            assert "All enrolled candidates" in dashboard.text
            assert "online strict" in dashboard.text
            assert "Usage enabled" in dashboard.text


async def test_non_finite_json_is_rejected_before_routing(tmp_path) -> None:
    app = create_app(fleet_config(tmp_path), start_polling=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            response = await client.post(
                "/v1/responses",
                headers={
                    "Authorization": "Bearer client-key",
                    "Content-Type": "application/json",
                },
                content=b'{"model":"qwen-coder","temperature":NaN}',
            )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


async def test_overflowing_json_float_is_rejected_before_routing(tmp_path) -> None:
    app = create_app(fleet_config(tmp_path), start_polling=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            response = await client.post(
                "/v1/responses",
                headers={
                    "Authorization": "Bearer client-key",
                    "Content-Type": "application/json",
                },
                content=b'{"model":"qwen-coder","temperature":1e10000}',
            )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


async def test_unpaired_surrogate_is_rejected_before_reserving_capacity(
    tmp_path,
) -> None:
    app = create_app(fleet_config(tmp_path), start_polling=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            response = await client.post(
                "/v1/responses",
                headers={
                    "Authorization": "Bearer client-key",
                    "Content-Type": "application/json",
                },
                content=b'{"model":"qwen-coder","input":"\\ud800"}',
            )
            status = (
                await client.get(
                    "/fleet/api/status",
                    headers={"Authorization": "Bearer admin-key"},
                )
            ).json()

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"
    assert status["scheduler"]["active_total"] == 0
    assert status["routes"] == []


async def test_owned_node_clients_ignore_proxy_environment_and_redirects(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9999")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:9999")
    app = create_app(fleet_config(tmp_path), start_polling=False)

    assert app.state.registry._client._trust_env is False
    assert app.state.proxy._client._trust_env is False
    assert app.state.registry._client.follow_redirects is False
    assert app.state.proxy._client.follow_redirects is False

    async with app.router.lifespan_context(app):
        pass


async def test_upstream_redirect_is_returned_without_following(tmp_path) -> None:
    calls: list[str] = []

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("node-a"))

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            307,
            headers={"Location": "http://redirect-target/private"},
        )

    app = create_app(
        fleet_config(tmp_path),
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler),
            follow_redirects=False,
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler),
            follow_redirects=False,
        ),
        start_polling=False,
    )
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
            follow_redirects=False,
        ) as client:
            response = await client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer client-key"},
                json={"model": "qwen-coder", "input": "hello"},
            )
        assert response.status_code == 307
        assert response.headers["location"] == "http://redirect-target/private"
        assert app.state.scheduler.status()["active_total"] == 0

    assert calls == ["http://node-a/v1/responses"]


async def test_decoded_buffered_429_drops_stale_content_encoding(tmp_path) -> None:
    decoded = b'{"error":{"code":"rate_limited"}}'
    encoded = gzip.compress(decoded)

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("node-a"))

    def proxy_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=encoded,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
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
            response = await client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer client-key"},
                json={"model": "qwen-coder", "input": "hello"},
            )
        assert response.status_code == 429
        assert response.content == decoded
        assert "content-encoding" not in response.headers


async def test_dashboard_route_and_ledger_views_share_node_and_model_identity(
    tmp_path,
) -> None:
    snapshot = snapshot_payload("node-a")
    alternate = dict(snapshot["deployments"][0])
    alternate["alias"] = "node-a-qwen-alternate"
    snapshot["deployments"].append(alternate)

    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot)

    def proxy_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    config = replace(
        fleet_config(tmp_path),
        ledger=LedgerConfig(dsn="postgresql://read-only.invalid/ledger"),
    )
    app = create_app(
        config,
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        proxy_client=httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler)
        ),
        start_polling=False,
    )

    async def aggregate(*, hours: int):
        assert hours == 24
        return [
            {
                "node_id": "node-a",
                "model": "node-a-qwen-alternate",
                "request_count": 1,
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "avg_response_ms": 4.0,
            }
        ]

    app.state.usage.aggregate = aggregate
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
            status = (
                await client.get(
                    "/fleet/api/status",
                    headers={"Authorization": "Bearer admin-key"},
                )
            ).json()
            usage = (
                await client.get(
                    "/fleet/api/usage",
                    headers={"Authorization": "Bearer admin-key"},
                )
            ).json()

    assert status["routes"][0]["node_id"] == "node-a"
    assert status["routes"][0]["public_model"] == "qwen-coder"
    assert status["models"][0]["nodes"][0]["eligible"] is True
    assert usage["rows"][0]["node_id"] == "node-a"
    assert usage["rows"][0]["public_model"] == "qwen-coder"
    assert usage["rows"][0]["public_models"] == ["qwen-coder"]


async def test_usage_view_preserves_ambiguous_public_model_synonyms(
    tmp_path,
) -> None:
    def registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=snapshot_payload("node-a"))

    base = fleet_config(tmp_path)
    synonym = replace(base.models[0], name="qwen-coder-synonym")
    config = replace(
        base,
        models=(*base.models, synonym),
        ledger=LedgerConfig(dsn="postgresql://read-only.invalid/ledger"),
    )
    app = create_app(
        config,
        registry_client=httpx.AsyncClient(
            transport=httpx.MockTransport(registry_handler)
        ),
        start_polling=False,
    )

    async def aggregate(*, hours: int):
        assert hours == 24
        return [
            {
                "node_id": "node-a",
                "model": "node-a-qwen",
                "request_count": 1,
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "avg_response_ms": 4.0,
            }
        ]

    app.state.usage.aggregate = aggregate
    async with app.router.lifespan_context(app):
        await app.state.registry.poll_all_once()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fleet",
        ) as client:
            usage = (
                await client.get(
                    "/fleet/api/usage",
                    headers={"Authorization": "Bearer admin-key"},
                )
            ).json()

    assert usage["rows"][0]["public_model"] is None
    assert usage["rows"][0]["public_models"] == [
        "qwen-coder",
        "qwen-coder-synonym",
    ]


async def test_concurrent_first_wave_fans_out_across_advertised_node_limits(
    tmp_path,
) -> None:
    nodes = (
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
    entered = {"a": asyncio.Event(), "b": asyncio.Event()}
    release = asyncio.Event()
    active = {"a": 0, "b": 0}
    peaks = {"a": 0, "b": 0}

    def registry_handler(request: httpx.Request) -> httpx.Response:
        node_id = str(request.url.host)
        return httpx.Response(
            200,
            json=snapshot_payload(
                node_id,
                deployment_capacity=capacity(limit=1),
            ),
        )

    async def proxy_handler(request: httpx.Request) -> httpx.Response:
        node_id = str(request.url.host)
        active[node_id] += 1
        peaks[node_id] = max(peaks[node_id], active[node_id])
        entered[node_id].set()
        try:
            await release.wait()
            return httpx.Response(200, json={"node": node_id})
        finally:
            active[node_id] -= 1

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
            tasks = [
                asyncio.create_task(
                    client.post(
                        "/v1/responses",
                        headers={"Authorization": "Bearer client-key"},
                        json={"model": "qwen-coder", "input": str(index)},
                    )
                )
                for index in range(2)
            ]
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in entered.values())),
                timeout=1,
            )
            scheduler_status = app.state.scheduler.status()
            assert scheduler_status["active_total"] == 2
            assert scheduler_status["active_by_node"] == {"a": 1, "b": 1}
            release.set()
            responses = await asyncio.gather(*tasks)
            assert {response.json()["node"] for response in responses} == {
                "a",
                "b",
            }
            assert app.state.scheduler.status()["active_total"] == 0

    assert peaks == {"a": 1, "b": 1}
