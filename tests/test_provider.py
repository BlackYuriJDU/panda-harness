"""Provider: Go URLs/headers, SSE normalization, tools, usage, Anthropic adapter."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from panda_core.config import ProviderSettings
from panda_core.errors import ProviderError
from panda_core.model_catalog import MODEL_IDS
from panda_core.provider import (
    DoneEvent,
    OpenCodeProvider,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    UsageEvent,
    classify_protocol,
    convert_messages_for_anthropic,
    convert_tools_for_anthropic,
    is_free_model,
    parse_anthropic_usage,
    parse_usage,
)
from panda_core.version import __version__

BASE = "https://opencode.ai/inference/go"
USER_AGENT = f"panda-harness/{__version__}"


def sse(*chunks: dict[str, Any]) -> str:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return body + "data: [DONE]\n\n"


def anthropic_sse(*events: dict[str, Any]) -> str:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    )


def provider_with(handler, *, api_key="test-key", model="deepseek-v4.1-flash"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenCodeProvider(
        ProviderSettings(), model=f"opencode-go/{model}", client=client, api_key=api_key
    )


async def collect(
    provider: OpenCodeProvider,
    messages=None,
    tools=None,
    model: str | None = None,
    session_id: str | None = None,
) -> list[Any]:
    return [
        event
        async for event in provider.stream(
            messages or [{"role": "user", "content": "hi"}],
            tools=tools,
            model=model,
            session_id=session_id,
        )
    ]


def test_default_settings_point_at_go() -> None:
    settings = ProviderSettings()
    assert settings.name == "opencode-go"
    assert settings.base_url == BASE
    assert settings.api_key_env == "OPENCODE_API_KEY"


def test_protocol_classification() -> None:
    assert classify_protocol("claude-haiku-5-5") == "anthropic"
    assert classify_protocol("gpt-6-luna") == "responses"
    assert classify_protocol("grok-4.7") == "responses"
    assert classify_protocol("muse-spark-1.2-contributor") == "responses"
    assert classify_protocol("gemini-4-argon") == "google"
    assert classify_protocol("deepseek-v4.1-flash") == "openai-chat"
    assert classify_protocol("qwen3.8-max") == "openai-chat"
    assert is_free_model("beam-free")
    assert not is_free_model("deepseek-v4.1-flash")


def test_snapshot_matches_the_go_catalogue() -> None:
    assert len(MODEL_IDS) == 38
    assert "deepseek-v4.1-flash" in MODEL_IDS
    assert "claude-haiku-5-5" in MODEL_IDS
    assert "gemini-4-argon" in MODEL_IDS


def test_model_list_uses_go_url_and_required_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/v1/models"
        assert request.headers["user-agent"] == USER_AGENT
        assert request.headers["x-opencode-session"]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-6-luna", "owned_by": "opencode"},
                    {"id": "deepseek-v4.1-flash", "owned_by": "opencode"},
                    {"id": "claude-haiku-5-5", "owned_by": "opencode"},
                    {"id": "beam-free", "owned_by": "opencode"},
                ],
            },
        )

    provider = provider_with(handler)
    models = asyncio.run(provider.list_models())
    assert [model.id for model in models] == [
        "beam-free",
        "claude-haiku-5-5",
        "deepseek-v4.1-flash",
        "gpt-6-luna",
    ]
    by_id = {model.id: model for model in models}
    assert by_id["deepseek-v4.1-flash"].supported is True
    assert by_id["claude-haiku-5-5"].protocol == "anthropic"
    assert by_id["claude-haiku-5-5"].supported is True
    assert by_id["gpt-6-luna"].protocol == "responses"
    assert by_id["gpt-6-luna"].supported is False
    assert by_id["beam-free"].free is True
    assert by_id["deepseek-v4.1-flash"].qualified() == "opencode-go/deepseek-v4.1-flash"


def test_legacy_opencode_prefix_maps_to_go() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "ok"}}]}))

    provider = provider_with(handler)
    asyncio.run(collect(provider, model="opencode/deepseek-v4.1-flash"))
    assert seen == ["deepseek-v4.1-flash"]

def test_text_stream_and_done() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/openai/v1/chat/completions"
        assert request.headers["user-agent"] == USER_AGENT
        assert request.headers["x-opencode-session"]
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4.1-flash"
        assert payload["stream"] is True
        assert payload["temperature"] == ProviderSettings().temperature
        return httpx.Response(
            200,
            text=sse(
                {"model": "deepseek-v4.1-flash", "choices": [{"delta": {"content": "hello"}}]},
                {"choices": [{"delta": {"content": ", world"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = provider_with(handler)
    events = asyncio.run(collect(provider))
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    assert text == "hello, world"
    assert done.finish_reason == "stop"
    assert done.tool_calls == ()


def test_stream_uses_the_panda_session_id_when_supplied() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-opencode-session"])
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "ok"}}]}))

    provider = provider_with(handler)
    asyncio.run(collect(provider, session_id="ses_panda_1"))
    assert provider.session_id != "ses_panda_1"
    assert seen == ["ses_panda_1"]


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
                            {"index": 0, "function": {"arguments": '"README.md"}'}}
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
    assert call.parsed_arguments() == {"path": "README.md"}


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


def test_parse_anthropic_usage_normalizes_every_field() -> None:
    usage = parse_anthropic_usage(
        {
            "input_tokens": 18,
            "output_tokens": 4,
            "cache_read_input_tokens": 12,
            "cache_creation_input_tokens": 6,
            "opencode": {"list_cost": 0},
        }
    )
    assert usage is not None
    assert usage.prompt_tokens == 18
    assert usage.completion_tokens == 4
    assert usage.total_tokens == 22
    assert usage.cached_tokens == 12
    assert usage.cache_write_tokens == 6
    assert usage.cost_usd == 0.0

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
    provider = OpenCodeProvider(ProviderSettings(), model="opencode-go/deepseek-v4.1-flash")
    with pytest.raises(ProviderError) as raised:
        asyncio.run(collect(provider))
    assert "OPENCODE_API_KEY" in raised.value.message
    assert "not set" in raised.value.message


@pytest.mark.parametrize(
    ("model", "protocol"),
    [("opencode-go/gpt-6-luna", "responses"), ("opencode-go/gemini-4-argon", "google")],
)
def test_unsupported_protocol_fails_before_network(model: str, protocol: str) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=sse())

    provider = provider_with(handler, model="deepseek-v4.1-flash")
    with pytest.raises(ProviderError) as raised:
        asyncio.run(collect(provider, model=model))
    assert protocol in raised.value.message
    assert raised.value.details["supported_protocols"] == ["anthropic", "openai-chat"]
    assert calls["n"] == 0


def test_foreign_provider_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse())

    provider = provider_with(handler)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(collect(provider, model="openrouter/anthropic/claude-sonnet-5.5"))
    assert "not configured" in raised.value.message


def test_anthropic_request_uses_messages_shape_and_headers() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=anthropic_sse(
                {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ),
        )

    provider = provider_with(handler, model="claude-haiku-5-5")
    tool = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
    messages = [
        {"role": "system", "content": "rules one"},
        {"role": "system", "content": "rules two"},
        {"role": "user", "content": "read it"},
    ]
    events = asyncio.run(collect(provider, messages=messages, tools=[tool]))
    assert captured["url"] == f"{BASE}/anthropic/v1/messages"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["headers"]["user-agent"] == USER_AGENT
    assert captured["headers"]["x-opencode-session"]
    payload = captured["payload"]
    assert payload["model"] == "claude-haiku-5-5"
    assert payload["system"] == "rules one\n\nrules two"
    assert payload["stream"] is True
    assert payload["max_tokens"] == ProviderSettings().max_output_tokens
    # Claude rejects temperature on newer models; the adapter must omit it.
    assert "temperature" not in payload
    assert payload["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "read it"}]}
    ]
    assert payload["tools"] == [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "ok"

def test_anthropic_stream_normalizes_text_usage_and_cost() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=anthropic_sse(
                {
                    "type": "message_start",
                    "message": {
                        "model": "claude-haiku-5-5",
                        "usage": {
                            "input_tokens": 18,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "output_tokens": 1,
                            "opencode": {"list_cost": 0},
                        },
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 4},
                },
                {"type": "message_stop"},
            ),
        )

    provider = provider_with(handler, model="claude-haiku-5-5")
    events = asyncio.run(collect(provider))
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "ok"
    usage = [event.usage for event in events if isinstance(event, UsageEvent)][-1]
    assert usage.prompt_tokens == 18
    assert usage.completion_tokens == 4
    assert usage.total_tokens == 22
    assert usage.cost_usd == 0.0
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    assert done.finish_reason == "stop"
    assert done.model == "claude-haiku-5-5"


def test_anthropic_tool_use_is_assembled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=anthropic_sse(
                {
                    "type": "message_start",
                    "message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 9}},
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"README.md"}'},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 6},
                },
                {"type": "message_stop"},
            ),
        )

    provider = provider_with(handler, model="claude-haiku-5-5")
    events = asyncio.run(collect(provider))
    deltas = [event for event in events if isinstance(event, ToolCallDelta)]
    assert deltas and deltas[0].name == "read_file"
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    assert done.finish_reason == "tool_calls"
    assert done.tool_calls == (
        ToolCall(id="toolu_1", name="read_file", arguments='{"path":"README.md"}'),
    )


def test_anthropic_error_event_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=anthropic_sse(
                {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "try later"},
                }
            ),
        )

    provider = provider_with(handler, model="claude-haiku-5-5")
    with pytest.raises(ProviderError) as raised:
        asyncio.run(collect(provider))
    assert "mid-stream" in raised.value.message


def test_openai_history_converts_to_anthropic_messages() -> None:
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
        {"role": "tool", "tool_call_id": "call_1", "content": "more"},
    ]
    system, converted = convert_messages_for_anthropic(messages)
    assert system == ["rules"]
    assert converted == [
        {"role": "user", "content": [{"type": "text", "text": "read it"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "contents"},
                {"type": "tool_result", "tool_use_id": "call_1", "content": "more"},
            ],
        },
    ]


def test_tool_schema_conversion_defaults_to_an_object() -> None:
    converted = convert_tools_for_anthropic(
        [
            {"type": "function", "function": {"name": "noargs"}},
            {"name": "bare", "description": "plain"},
        ]
    )
    assert converted == [
        {"name": "noargs", "description": "", "input_schema": {"type": "object", "properties": {}}},
        {
            "name": "bare",
            "description": "plain",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


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
