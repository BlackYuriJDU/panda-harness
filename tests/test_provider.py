"""Provider: SSE normalization, tool calls, usage, auth errors, model catalogue."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from panda_core.config import ProviderSettings
from panda_core.errors import ProviderError
from panda_core.provider import (
    DoneEvent,
    OpenCodeProvider,
    TextDelta,
    ToolCallDelta,
    UsageEvent,
    classify_protocol,
    is_free_model,
    parse_usage,
)


def sse(*chunks: dict[str, Any]) -> str:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return body + "data: [DONE]\n\n"


def provider_with(handler, *, api_key: str = "test-key", model: str = "deepseek-v4.1-flash"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = ProviderSettings()
    return OpenCodeProvider(settings, model=f"opencode/{model}", client=client, api_key=api_key)


async def collect(
    provider: OpenCodeProvider, messages=None, tools=None, model: str | None = None
) -> list[Any]:
    return [
        event
        async for event in provider.stream(
            messages or [{"role": "user", "content": "hi"}], tools=tools, model=model
        )
    ]


def test_protocol_classification() -> None:
    assert classify_protocol("claude-opus-5") == "anthropic"
    assert classify_protocol("gpt-5.5") == "responses"
    assert classify_protocol("gemini-3.8-flash") == "google"
    assert classify_protocol("deepseek-v4.1-flash") == "openai-chat"
    assert classify_protocol("qwen3.8-max") == "openai-chat"
    assert is_free_model("deepseek-v4.1-flash-free")
    assert not is_free_model("deepseek-v4.1-flash")


def test_model_list_from_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/models")
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-5.5", "owned_by": "opencode"},
                    {"id": "deepseek-v4.1-flash", "owned_by": "opencode"},
                    {"id": "claude-opus-5", "owned_by": "opencode"},
                    {"id": "deepseek-v4.1-flash-free", "owned_by": "opencode"},
                ],
            },
        )

    provider = provider_with(handler)
    models = asyncio.run(provider.list_models())
    assert [model.id for model in models] == [
        "claude-opus-5",
        "deepseek-v4.1-flash",
        "deepseek-v4.1-flash-free",
        "gpt-5.5",
    ]
    by_id = {model.id: model for model in models}
    assert by_id["deepseek-v4.1-flash"].supported is True
    assert by_id["claude-opus-5"].protocol == "anthropic"
    assert by_id["claude-opus-5"].supported is False
    assert by_id["gpt-5.5"].protocol == "responses"
    assert by_id["deepseek-v4.1-flash-free"].free is True
    assert by_id["deepseek-v4.1-flash"].qualified() == "opencode/deepseek-v4.1-flash"
def test_text_stream_and_done() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4.1-flash"
        assert payload["stream"] is True
        return httpx.Response(
            200,
            text=sse(
                {"model": "deepseek-v4.1-flash", "choices": [{"delta": {"content": "Olá"}}]},
                {"choices": [{"delta": {"content": ", mundo"}}]},
                {
                    "choices": [
                        {"delta": {}, "finish_reason": "stop"},
                    ]
                },
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = provider_with(handler)
    events = asyncio.run(collect(provider))
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    assert text == "Olá, mundo"
    assert done.finish_reason == "stop"
    assert done.tool_calls == ()


def test_tool_call_fragments_are_assembled() -> None:
    fragments = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": '{"path":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"corpus/README.md"}'}}
                        ]
                    }
                }
            ]
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse(*fragments))

    provider = provider_with(handler)
    events = asyncio.run(collect(provider))
    deltas = [event for event in events if isinstance(event, ToolCallDelta)]
    assert deltas
    assert deltas[0].name == "read_file"
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    assert len(done.tool_calls) == 1
    call = done.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "read_file"
    assert call.parsed_arguments() == {"path": "corpus/README.md"}


def test_usage_normalization_includes_cache_and_cost() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=sse(
                {
                    "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 8,
                        "total_tokens": 128,
                        "prompt_tokens_details": {"cached_tokens": 64},
                        "cost": 0.0012,
                    },
                }
            ),
        )

    provider = provider_with(handler)
    events = asyncio.run(collect(provider))
    usage = [event.usage for event in events if isinstance(event, UsageEvent)][-1]
    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 8
    assert usage.total_tokens == 128
    assert usage.cached_tokens == 64
    assert usage.cost_usd == pytest.approx(0.0012)


def test_parse_usage_without_total_computes_it() -> None:
    usage = parse_usage({"prompt_tokens": 5, "completion_tokens": 2})
    assert usage is not None
    assert usage.total_tokens == 7
    assert usage.cost_usd is None
    assert parse_usage({}) is None
@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "rate limit"),
        (500, "unavailable"),
        (404, "404"),
        (400, "rejected"),
    ],
)
def test_http_errors_are_stable_and_secret_free(status: int, needle: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream said no")

    provider = provider_with(handler)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(collect(provider))
    message = raised.value.message
    assert needle in message
    assert "test-key" not in message
    assert raised.value.exit_code == 4
    assert raised.value.code == "provider_error"


def test_missing_api_key_names_only_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    provider = OpenCodeProvider(ProviderSettings(), model="opencode/deepseek-v4.1-flash")
    with pytest.raises(ProviderError) as raised:
        asyncio.run(collect(provider))
    assert "OPENCODE_API_KEY" in raised.value.message
    assert "not set" in raised.value.message


def test_unsupported_protocol_fails_before_network() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=sse())

    provider = provider_with(handler, model="claude-opus-5")
    with pytest.raises(ProviderError) as raised:
        asyncio.run(collect(provider))
    assert "anthropic" in raised.value.message
    assert raised.value.details["supported_protocols"] == ["openai-chat"]
    assert calls["n"] == 0


def test_foreign_provider_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse())

    provider = provider_with(handler)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(collect(provider, model="openrouter/anthropic/claude-sonnet-5.5"))
    assert "not configured" in raised.value.message


def test_malformed_stream_chunk_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: {not json}\n\n")

    provider = provider_with(handler)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(collect(provider))
    assert "malformed" in raised.value.message


def test_transport_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    provider = provider_with(handler)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(collect(provider))
    assert "transport" in raised.value.message