from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mnemosyne_macos.app import create_inference_app
from mnemosyne_macos.config import MacConfig, load_config, save_config
from mnemosyne_macos.engines.base import Deadline, EngineAdapter
from mnemosyne_macos.image_api import normalize_image_request
from mnemosyne_macos.models import (
    DEFAULT_CAPABILITIES,
    Endpoint,
    EngineName,
    EngineSnapshot,
    LoadedHandle,
    ProxyRoute,
    ResidentInstance,
    ResolvedTarget,
    ServiceState,
)
from mnemosyne_macos.proxy import prepare_request_body
from mnemosyne_macos.runtime import NativeRuntime
from mnemosyne_macos.storage import install_destination


def test_every_current_public_inference_route_remains_registered() -> None:
    # This literal set must not derive from Endpoint: otherwise deleting an
    # enum member and its route would make the compatibility test pass. The
    # snapshot route is always registered so an unconfigured deployment can
    # fail closed with an actionable response, but it is usable only when its
    # separate Fleet authentication is configured.
    app = create_inference_app(object())  # type: ignore[arg-type]
    registered = {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
    }

    assert registered == {
        ("/health", "GET"),
        ("/v1/models", "GET"),
        ("/fleet/v1/snapshot", "GET"),
        ("/v1/chat/completions", "POST"),
        ("/v1/completions", "POST"),
        ("/v1/responses", "POST"),
        ("/v1/messages", "POST"),
        ("/v1/embeddings", "POST"),
        ("/v1/rerank", "POST"),
        ("/v1/images/generations", "POST"),
    }


@pytest.mark.asyncio
async def test_fleet_snapshot_route_remains_separately_configuration_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet_key_env = "MNEMOSYNE_NONREGRESSION_FLEET_KEY"
    monkeypatch.delenv(fleet_key_env, raising=False)
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            server=SimpleNamespace(fleet_api_key_env=fleet_key_env)
        )
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    try:
        response = await client.get("/fleet/v1/snapshot")
    finally:
        await client.aclose()

    assert response.status_code == 503
    assert response.json() == {
        "detail": "fleet snapshot authentication is not configured"
    }


class _ColdLoadAdapter(EngineAdapter):
    """Small authoritative adapter for the public-ASGI compatibility floor."""

    engine = EngineName.OMLX
    ownership = "nonregression-test"

    def __init__(self) -> None:
        self.residents: list[ResidentInstance] = []
        self.loads = 0

    async def validate_control(self, *, deadline: Deadline) -> EngineSnapshot:
        return await self.inspect(deadline=deadline)

    async def inspect(self, *, deadline: Deadline) -> EngineSnapshot:
        del deadline
        return EngineSnapshot(
            engine=self.engine,
            residents=tuple(self.residents),
            authoritative=True,
            service_state=ServiceState.READY,
        )

    async def load(
        self,
        target: ResolvedTarget,
        *,
        deadline: Deadline,
    ) -> LoadedHandle:
        del deadline
        self.loads += 1
        resident = ResidentInstance(
            engine=self.engine,
            canonical_model_id=target.key.canonical_model_id,
            instance_id=f"nonregression-{self.loads}",
        )
        self.residents = [resident]
        return LoadedHandle(
            target=target,
            instance=resident,
            base_url="http://upstream.test",
            wire_model=target.wire_model,
        )

    async def unload(
        self,
        instance: ResidentInstance,
        *,
        deadline: Deadline,
    ) -> None:
        del deadline
        self.residents = [current for current in self.residents if current != instance]

    def route(self, handle: LoadedHandle, endpoint: Endpoint) -> ProxyRoute:
        return ProxyRoute(
            base_url=handle.base_url,
            path=f"/v1/{endpoint.value}",
            wire_model=handle.wire_model,
        )

    async def aclose(self) -> None:
        return None


def _single_route_config(tmp_path: Path, capability: str) -> MacConfig:
    return MacConfig.model_validate(
        {
            "server": {
                "idle_unload_seconds": None,
                "inference_api_key_env": (
                    "MNEMOSYNE_NONREGRESSION_INFERENCE_API_KEY"
                ),
                "fleet_api_key_env": "MNEMOSYNE_NONREGRESSION_FLEET_API_KEY",
                "fleet_inference_api_key_env": (
                    "MNEMOSYNE_NONREGRESSION_FLEET_INFERENCE_API_KEY"
                ),
            },
            "engines": {"omlx": {"enabled": True}},
            "paths": {"state_database": str(tmp_path / "state.db")},
            "models": [
                {
                    "alias": "public-model",
                    "engine": "omlx",
                    "model": "publisher/upstream-model",
                    "capabilities": [capability],
                }
            ],
        }
    )


_LANGUAGE_ROUTE_CASES = (
    pytest.param(
        "/v1/chat/completions",
        "chat/completions",
        {"messages": [{"role": "user", "content": "hello"}]},
        {
            "id": "chatcmpl-nonregression",
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "total_tokens": 6,
            },
        },
        (4, 2, 6),
        id="chat-completions",
    ),
    pytest.param(
        "/v1/completions",
        "completions",
        {"prompt": "hello"},
        {
            "id": "cmpl-nonregression",
            "choices": [{"text": "hi"}],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            },
        },
        (5, 3, 8),
        id="completions",
    ),
    pytest.param(
        "/v1/responses",
        "responses",
        {"input": "hello"},
        {
            "id": "resp-nonregression",
            "object": "response",
            "output": [],
            "usage": {
                "input_tokens": 6,
                "output_tokens": 4,
                "total_tokens": 10,
            },
        },
        (6, 4, 10),
        id="responses",
    ),
    pytest.param(
        "/v1/messages",
        "messages",
        {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 32},
        {
            "id": "msg-nonregression",
            "type": "message",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {
                "input_tokens": 7,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
                "output_tokens": 5,
            },
        },
        (12, 5, 17),
        id="messages",
    ),
    pytest.param(
        "/v1/embeddings",
        "embeddings",
        {"input": ["hello", "world"]},
        {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
            "usage": {"prompt_tokens": 8, "total_tokens": 8},
        },
        (8, 0, 8),
        id="embeddings",
    ),
    pytest.param(
        "/v1/rerank",
        "rerank",
        {"query": "hello", "documents": ["first", "second"]},
        {
            "results": [{"index": 1, "relevance_score": 0.9}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 0},
        },
        (9, 0, 9),
        id="rerank",
    ),
)


@pytest.mark.parametrize(
    ("route_path", "capability", "request_fields", "upstream_payload", "tokens"),
    _LANGUAGE_ROUTE_CASES,
)
@pytest.mark.asyncio
async def test_public_language_routes_preserve_cold_jit_forwarding_and_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route_path: str,
    capability: str,
    request_fields: dict,
    upstream_payload: dict,
    tokens: tuple[int, int, int],
) -> None:
    for env_name in (
        "MNEMOSYNE_NONREGRESSION_INFERENCE_API_KEY",
        "MNEMOSYNE_NONREGRESSION_FLEET_API_KEY",
        "MNEMOSYNE_NONREGRESSION_FLEET_INFERENCE_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)

    seen: dict = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = list(request.url.params.multi_items())
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=upstream_payload)

    adapter = _ColdLoadAdapter()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        _single_route_config(tmp_path, capability),
        adapters={EngineName.OMLX: adapter},
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    extension = {
        "route": route_path,
        "nested": [1, {"kept": True}],
        "vendor_flag": "opaque",
    }
    request_payload = {
        "model": "public-model",
        **request_fields,
        "mnemosyne_nonregression_extension": extension,
    }
    try:
        before = await runtime.coordinator.status()
        assert before.resident_alias is None

        response = await client.post(
            f"{route_path}?trace=first&trace=second&blank=",
            json=request_payload,
        )

        assert response.status_code == 200, response.text
        assert response.json() == upstream_payload
        assert seen == {
            "path": route_path,
            "query": [("trace", "first"), ("trace", "second"), ("blank", "")],
            "body": {
                **request_payload,
                "model": "publisher/upstream-model",
            },
        }
        assert adapter.loads == 1

        status = await runtime.coordinator.status()
        assert status.resident_alias == "public-model"
        assert status.inflight == 0

        rows = await runtime.usage.list_usage()
        assert len(rows) == 1
        row = rows[0]
        assert row["endpoint"] == route_path
        assert row["requested_model"] == "public-model"
        assert row["alias"] == "public-model"
        assert row["backend"] == "omlx"
        assert row["streamed"] == 0
        assert (
            row["prompt_tokens"],
            row["completion_tokens"],
            row["total_tokens"],
        ) == tokens
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


_DIALECT_SSE_CASES = (
    pytest.param(
        "/v1/responses",
        "responses",
        {"input": "stream this"},
        (
            b'event: response.output_text.delta\n'
            b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
            b'event: response.completed\n'
            b'data: {"type":"response.completed","response":{"usage":'
            b'{"input_tokens":11,"output_tokens":7,"total_tokens":18}}}\n\n'
        ),
        (11, 7, 18),
        id="responses-sse",
    ),
    pytest.param(
        "/v1/messages",
        "messages",
        {"messages": [{"role": "user", "content": "stream this"}], "max_tokens": 32},
        (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"usage":'
            b'{"input_tokens":10,"cache_creation_input_tokens":2,'
            b'"cache_read_input_tokens":3,"output_tokens":0}}}\n\n'
            b'event: message_delta\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":6}}\n\n'
        ),
        (15, 6, 21),
        id="messages-sse",
    ),
)


@pytest.mark.parametrize(
    ("route_path", "capability", "request_fields", "sse_body", "tokens"),
    _DIALECT_SSE_CASES,
)
@pytest.mark.asyncio
async def test_responses_and_messages_sse_keep_one_dialect_aware_usage_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route_path: str,
    capability: str,
    request_fields: dict,
    sse_body: bytes,
    tokens: tuple[int, int, int],
) -> None:
    monkeypatch.delenv(
        "MNEMOSYNE_NONREGRESSION_INFERENCE_API_KEY",
        raising=False,
    )
    seen: dict = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse_body,
        )

    adapter = _ColdLoadAdapter()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    runtime = NativeRuntime(
        _single_route_config(tmp_path, capability),
        adapters={EngineName.OMLX: adapter},
        proxy_client=upstream_client,
    )
    await runtime.start(raise_on_degraded=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_inference_app(runtime)),
        base_url="http://mnemosyne.test",
    )
    request_payload = {
        "model": "public-model",
        **request_fields,
        "stream": True,
        "mnemosyne_nonregression_extension": {
            "nested": {"kept": [1, 2, 3]}
        },
    }
    try:
        response = await client.post(
            f"{route_path}?cursor=first&cursor=second",
            json=request_payload,
        )

        assert response.status_code == 200, response.text
        assert response.content == sse_body
        assert seen == {
            "path": route_path,
            "body": {
                **request_payload,
                "model": "publisher/upstream-model",
            },
        }
        assert adapter.loads == 1
        assert (await runtime.coordinator.status()).inflight == 0

        rows = await runtime.usage.list_usage()
        assert len(rows) == 1
        row = rows[0]
        assert row["endpoint"] == route_path
        assert row["streamed"] == 1
        assert (
            row["prompt_tokens"],
            row["completion_tokens"],
            row["total_tokens"],
        ) == tokens
    finally:
        await client.aclose()
        await runtime.stop()
        await upstream_client.aclose()


def test_every_current_engine_keeps_its_default_capability_floor() -> None:
    generation = {
        Endpoint.CHAT_COMPLETIONS,
        Endpoint.COMPLETIONS,
        Endpoint.RESPONSES,
    }
    generation_with_messages = generation | {Endpoint.MESSAGES}

    assert DEFAULT_CAPABILITIES == {
        EngineName.LLAMA_CPP: frozenset(generation),
        EngineName.OMLX: frozenset(
            generation_with_messages | {Endpoint.EMBEDDINGS, Endpoint.RERANK}
        ),
        EngineName.DS4: frozenset(generation_with_messages),
        EngineName.MFLUX: frozenset({Endpoint.IMAGES_GENERATIONS}),
        EngineName.MLXCEL: frozenset(generation),
        EngineName.MISTRAL_RS: frozenset(generation_with_messages),
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        Endpoint.CHAT_COMPLETIONS,
        Endpoint.COMPLETIONS,
        Endpoint.RESPONSES,
        Endpoint.MESSAGES,
        Endpoint.EMBEDDINGS,
        Endpoint.RERANK,
    ],
)
def test_every_language_route_keeps_fields_outside_manager_owned_rewrites(
    endpoint: Endpoint,
) -> None:
    original_extension = {
        "nested": [1, {"kept": True}],
        "vendor_flag": "opaque",
    }
    body, requested_model, streamed, client_asked_for_usage = prepare_request_body(
        json.dumps(
            {
                "model": "public-model",
                "input": ["request-shape-is-endpoint-specific"],
                "mnemosyne_nonregression_extension": original_extension,
            }
        ).encode("utf-8"),
        route=ProxyRoute(
            base_url="http://engine.test",
            path=f"/v1/{endpoint.value}",
            wire_model="engine-model",
        ),
        endpoint=endpoint,
        # Mistral.rs does not use the manager-owned portable reasoning
        # translation. Capability admission is tested separately by runtime
        # and adapter suites; this assertion targets common wire opacity.
        engine=EngineName.MISTRAL_RS,
    )

    forwarded = json.loads(body)
    assert requested_model == "public-model"
    assert forwarded == {
        "model": "engine-model",
        "input": ["request-shape-is-endpoint-specific"],
        "mnemosyne_nonregression_extension": original_extension,
    }
    assert streamed is False
    assert client_asked_for_usage is False


def test_image_normalization_keeps_fields_outside_its_bounded_contract() -> None:
    extension = {"pipeline": "vendor-preview", "options": [1, 2, 3]}
    body = normalize_image_request(
        json.dumps(
            {
                "model": "public-image",
                "prompt": "local image",
                "seed": 7,
                "mnemosyne_nonregression_extension": extension,
            }
        ).encode("utf-8"),
        wire_model="engine-image",
        defaults={
            "width": 768,
            "height": 512,
            "num_inference_steps": 12,
            "guidance_scale": 2.5,
        },
        max_pixels=4096 * 4096,
    )

    forwarded = json.loads(body)
    assert forwarded["model"] == "engine-image"
    assert forwarded["prompt"] == "local image"
    assert forwarded["seed"] == 7
    assert forwarded["size"] == "768x512"
    assert forwarded["mnemosyne_nonregression_extension"] == extension


def test_configured_symlink_storage_remains_the_lexical_download_root(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Volumes" / "Athena" / "nested" / "weights"
    target.mkdir(parents=True)
    selected = tmp_path / "Library" / "Models" / "selected-link"
    selected.parent.mkdir(parents=True)
    selected.symlink_to(target, target_is_directory=True)
    config_path = tmp_path / "settings" / "config.yaml"
    config = MacConfig.model_validate(
        {
            "storage": {
                "default": "chosen",
                "locations": [
                    {
                        "name": "chosen",
                        "path": str(selected),
                        "volume_uuid": "ATHENA-UUID",
                    }
                ],
            }
        }
    )

    save_config(config, config_path)
    reloaded = load_config(config_path)
    configured_root = Path(reloaded.storage.locations[0].path)
    destination = install_destination(
        configured_root,
        EngineName.LLAMA_CPP,
        "publisher/model",
    )

    assert reloaded.storage.locations[0].path == str(selected)
    assert destination == selected / "llama.cpp" / "publisher" / "model"
    assert destination != target / "llama.cpp" / "publisher" / "model"
