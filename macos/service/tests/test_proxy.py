from __future__ import annotations

import json

import pytest

from mnemosyne_macos.models import Endpoint, EngineName, ProxyRoute
from mnemosyne_macos.proxy import InvalidProxyRequest, prepare_request_body


def _prepare(payload: dict, *, engine: EngineName, endpoint: Endpoint) -> dict:
    prepared, requested_model, _streamed, _client_asked_usage = prepare_request_body(
        json.dumps(payload).encode("utf-8"),
        route=ProxyRoute(
            base_url="http://engine.test",
            path=f"/v1/{endpoint.value}",
            wire_model="engine-model",
        ),
        endpoint=endpoint,
        engine=engine,
    )
    assert requested_model == "public-model"
    return json.loads(prepared)


@pytest.mark.parametrize(
    ("engine", "budget_field"),
    [
        (EngineName.OMLX, "thinking_budget"),
        (EngineName.LLAMA_CPP, "reasoning_budget_tokens"),
    ],
)
def test_qwen_reasoning_controls_follow_the_selected_engine(
    engine: EngineName,
    budget_field: str,
) -> None:
    payload = _prepare(
        {
            "model": "public-model",
            "messages": [{"role": "user", "content": "Solve this."}],
            "reasoning_effort": "medium",
            "thinking_budget": 4096,
            "enable_thinking": True,
            "preserve_thinking": False,
            "chat_template_kwargs": {"custom_qwen_option": "kept"},
            "temperature": 1.0,
        },
        engine=engine,
        endpoint=Endpoint.CHAT_COMPLETIONS,
    )

    assert payload["model"] == "engine-model"
    assert payload["reasoning_effort"] == "medium"
    assert payload[budget_field] == 4096
    assert payload["chat_template_kwargs"] == {
        "custom_qwen_option": "kept",
        "enable_thinking": True,
        "preserve_thinking": False,
        "reasoning_effort": "medium",
    }
    assert payload["temperature"] == 1.0
    assert "enable_thinking" not in payload
    assert "preserve_thinking" not in payload
    assert set(payload).isdisjoint(
        set(("thinking_budget", "reasoning_budget_tokens", "thinking_budget_tokens"))
        - {budget_field}
    )


def test_llamacpp_budget_alias_is_portable_to_omlx() -> None:
    payload = _prepare(
        {
            "model": "public-model",
            "messages": [{"role": "user", "content": "Think."}],
            "thinking_budget_tokens": 2048,
        },
        engine=EngineName.OMLX,
        endpoint=Endpoint.CHAT_COMPLETIONS,
    )

    assert payload["thinking_budget"] == 2048
    assert "thinking_budget_tokens" not in payload


def test_responses_effort_is_copied_to_the_qwen_chat_template() -> None:
    payload = _prepare(
        {
            "model": "public-model",
            "input": "Solve this.",
            "reasoning": {"effort": "low", "summary": "auto"},
            "enable_thinking": True,
        },
        engine=EngineName.OMLX,
        endpoint=Endpoint.RESPONSES,
    )

    assert payload["reasoning"] == {"effort": "low", "summary": "auto"}
    assert payload["chat_template_kwargs"] == {
        "enable_thinking": True,
        "reasoning_effort": "low",
    }


def test_numeric_omlx_effort_remains_available_to_other_model_templates() -> None:
    payload = _prepare(
        {
            "model": "public-model",
            "messages": [{"role": "user", "content": "Think."}],
            "reasoning_effort": 0.75,
        },
        engine=EngineName.OMLX,
        endpoint=Endpoint.CHAT_COMPLETIONS,
    )

    assert payload["reasoning_effort"] == 0.75
    assert payload["chat_template_kwargs"]["reasoning_effort"] == 0.75


def test_reasoning_fields_stay_opaque_for_other_engines() -> None:
    payload = _prepare(
        {
            "model": "public-model",
            "messages": [{"role": "user", "content": "Think."}],
            "enable_thinking": True,
            "thinking_budget": 1024,
        },
        engine=EngineName.MISTRAL_RS,
        endpoint=Endpoint.CHAT_COMPLETIONS,
    )

    assert payload["enable_thinking"] is True
    assert payload["thinking_budget"] == 1024
    assert "chat_template_kwargs" not in payload


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"chat_template_kwargs": []}, "chat_template_kwargs"),
        ({"enable_thinking": "yes"}, "enable_thinking"),
        ({"thinking_budget": -1}, "thinking_budget"),
        (
            {"thinking_budget": 1024, "thinking_budget_tokens": 2048},
            "budget fields must agree",
        ),
        (
            {
                "reasoning_effort": "low",
                "chat_template_kwargs": {"reasoning_effort": "xhigh"},
            },
            "effort fields",
        ),
    ],
)
def test_invalid_reasoning_controls_are_rejected(
    overrides: dict,
    message: str,
) -> None:
    with pytest.raises(InvalidProxyRequest, match=message):
        _prepare(
            {
                "model": "public-model",
                "messages": [{"role": "user", "content": "Think."}],
                **overrides,
            },
            engine=EngineName.OMLX,
            endpoint=Endpoint.CHAT_COMPLETIONS,
        )
