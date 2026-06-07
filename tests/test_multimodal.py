"""Phase 8 D2 — multimodal proxy passthrough.

Verifies PRD §5.5: OpenAI-format `image_url` content blocks travel through
`_proxy` byte-for-byte. The proxy never inspects the body beyond peeking at
`model`, so the assertion is that whatever bytes the client posts are the
exact bytes the upstream stub receives.
"""
from __future__ import annotations

import json

import vllm_manager


class _FakeResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def aread(self) -> bytes:
        return b'{"choices":[{"message":{"content":"a cat"}}]}'

    async def aiter_bytes(self):
        yield await self.aread()

    async def aclose(self):
        return None


class _FakeClient:
    async def aclose(self):
        return None


def test_proxy_passes_image_content_blocks_unchanged(rich_client, monkeypatch):
    client, _stub = rich_client
    captured: dict[str, bytes] = {}

    async def _open_upstream(_request, _path, body):
        captured["body"] = body
        return _FakeClient(), _FakeResponse()

    monkeypatch.setattr(vllm_manager, "_open_upstream", _open_upstream)

    payload = {
        "model": "a-model",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.invalid/cat.jpg"},
                    },
                ],
            }
        ],
    }
    r = client.post("/v1/chat/completions", json=payload)
    assert r.status_code == 200
    # `_canonicalize_model_field` rewrites the alias `a-model` to the
    # served model name (`org/a-model` per rich_config). Everything else —
    # the image_url content block in particular — must round-trip verbatim.
    forwarded = json.loads(captured["body"])
    assert forwarded["model"] == "org/a-model"
    assert forwarded["messages"] == payload["messages"]
