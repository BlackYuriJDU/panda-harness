"""Generic endpoint profile, per-model parameters, Claude thinking, caching, retries."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from panda_core.config import ProviderSettings
from panda_core.errors import ProviderError
from panda_core.provider import (
    DoneEvent,
    OpenCodeProvider,
    TextDelta,
    convert_messages_for_anthropic,
    convert_messages_for_google,
    convert_messages_for_responses,
    sanitize_messages_for_openai,
)

GATEWAY = "https://gateway.example"


def generic_settings(**overrides: Any) -> ProviderSettings:
    values: dict[str, Any] = {
        "name": "google-cloud",
        "base_url": f"{GATEWAY}/v1",
        "api_key_env": "MODEL_API_KEY",
        "endpoint_profile": "generic",
        "anthropic_base_url": GATEWAY,
        "google_base_url": GATEWAY,
    }
    values.update(overrides)
    return ProviderSettings(**values)


def make(handler, model: str, **overrides: Any) -> OpenCodeProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenCodeProvider(
        generic_settings(**overrides),
        model=f"google-cloud/{model}",
        client=client,
        api_key="test-key",
    )


def no_sleep(provider: OpenCodeProvider, sleeps: list[float] | None = None) -> None:
    async def fake(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    provider._sleep = fake  # type: ignore[method-assign]


async def collect(provider: OpenCodeProvider, messages=None, tools=None, **kwargs) -> list[Any]:
    return [
        event
        async for event in provider.stream(
            messages or [{"role": "user", "content": "hi"}], tools=tools, **kwargs
        )
    ]


def chat_sse(*chunks: dict[str, Any], done: bool = True) -> str:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return body + ("data: [DONE]\n\n" if done else "")


def anthropic_sse(*events: dict[str, Any]) -> str:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    )


CHAT_OK = chat_sse(
    {"choices": [{"delta": {"content": "ok"}}]},
    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
)
ANTHROPIC_OK = anthropic_sse(
    {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}},
    {"type": "message_stop"},
)
GOOGLE_OK = (
    "data: "
    + json.dumps(
        {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
    )
    + "\n\n"
)


class Capture:
    def __init__(self, body: str) -> None:
        self.body = body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, text=self.body)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


# ---- G1: routes and headers ----


def test_default_profile_is_unchanged() -> None:
    settings = ProviderSettings()
    assert settings.endpoint_profile == "opencode-go"
    assert settings.protocol == "auto"
    assert settings.effort is None
    assert settings.prompt_cache is False
    assert settings.retry_partial is False
    assert settings.max_retry_delay_seconds == 15.0


@pytest.mark.parametrize(
    ("model", "body", "url"),
    [
        ("glm-5.2", CHAT_OK, f"{GATEWAY}/v1/chat/completions"),
        ("claude-opus-5-5", ANTHROPIC_OK, f"{GATEWAY}/v1/messages"),
        (
            "gemini-4-argon",
            GOOGLE_OK,
            f"{GATEWAY}/v1beta/models/gemini-4-argon:streamGenerateContent?alt=sse",
        ),
    ],
)
def test_generic_routes_and_headers(model: str, body: str, url: str) -> None:
    capture = Capture(body)
    provider = make(capture, model)
    asyncio.run(collect(provider))
    request = capture.requests[-1]
    assert str(request.url) == url
    assert "x-opencode-session" not in request.headers
    assert request.headers["authorization"] == "Bearer test-key"
    if model.startswith("claude"):
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"]
    else:
        assert "x-api-key" not in request.headers
    if model.startswith("gemini"):
        assert request.headers["x-goog-api-key"] == "test-key"
    else:
        assert "x-goog-api-key" not in request.headers


def test_generic_models_url() -> None:
    provider = make(Capture(""), "glm-5.2")
    assert provider.models_url == f"{GATEWAY}/v1/models"
    assert provider.responses_url == f"{GATEWAY}/v1/responses"


def test_generic_routes_gpt_oss_to_chat() -> None:
    capture = Capture(CHAT_OK)
    provider = make(capture, "gpt-oss-120b")
    assert provider.protocol_for("gpt-oss-120b") == "openai-chat"
    asyncio.run(collect(provider))
    assert str(capture.requests[-1].url) == f"{GATEWAY}/v1/chat/completions"


def test_protocol_override_wins() -> None:
    provider = make(Capture(CHAT_OK), "claude-opus-5-5", protocol="openai-chat")
    assert provider.protocol_for("claude-opus-5-5") == "openai-chat"


def test_invalid_endpoint_settings_are_rejected() -> None:
    with pytest.raises(ValueError):
        ProviderSettings(endpoint_profile="other")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProviderSettings(anthropic_base_url="http://gateway.example")
    with pytest.raises(ValueError):
        ProviderSettings(effort="huge")  # type: ignore[arg-type]


# ---- G2: per-model parameters ----


def test_opus_has_no_temperature_on_the_chat_protocol() -> None:
    capture = Capture(CHAT_OK)
    provider = make(capture, "claude-opus-5-5", protocol="openai-chat")
    asyncio.run(collect(provider, tools=[{"type": "function", "function": {"name": "x"}}]))
    payload = capture.payload
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "budget_tokens"):
        assert forbidden not in payload
    assert payload["tool_choice"] == "auto"


def test_opus_has_no_sampling_or_thinking_config_on_messages() -> None:
    capture = Capture(ANTHROPIC_OK)
    provider = make(capture, "claude-opus-5-5")
    asyncio.run(collect(provider, tools=[{"type": "function", "function": {"name": "x"}}]))
    payload = capture.payload
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert forbidden not in payload


def test_claude_on_the_google_protocol_has_no_temperature() -> None:
    capture = Capture(GOOGLE_OK)
    provider = make(capture, "claude-opus-5-5", protocol="google")
    asyncio.run(collect(provider))
    assert "temperature" not in capture.payload["generationConfig"]


def test_argon_never_gets_a_thinking_budget() -> None:
    capture = Capture(GOOGLE_OK)
    provider = make(capture, "gemini-4-argon", effort="high")
    asyncio.run(collect(provider))
    text = capture.requests[-1].content.decode()
    assert "thinkingBudget" not in text
    assert "thinkingConfig" not in text
    assert capture.payload["generationConfig"]["temperature"] == 0.2


def test_non_claude_chat_keeps_temperature() -> None:
    capture = Capture(CHAT_OK)
    provider = make(capture, "glm-5.2")
    asyncio.run(collect(provider))
    assert capture.payload["temperature"] == 0.2
    assert "reasoning_effort" not in capture.payload


@pytest.mark.parametrize(
    ("model", "body", "protocol", "check"),
    [
        ("glm-5.2", CHAT_OK, "auto", lambda p: p["reasoning_effort"] == "high"),
        (
            "claude-opus-5-5",
            ANTHROPIC_OK,
            "auto",
            lambda p: p["output_config"] == {"effort": "high"},
        ),
        ("glm-5.2", "", "responses", lambda p: p["reasoning"] == {"effort": "high"}),
    ],
)
def test_effort_goes_in_the_protocol_field(model, body, protocol, check) -> None:
    capture = Capture(body)
    provider = make(capture, model, effort="high", protocol=protocol)
    asyncio.run(collect(provider))
    assert check(capture.payload)


# ---- G5: Claude thinking ----

THINKING_TURN = anthropic_sse(
    {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {}}},
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "thinking_delta", "thinking": "plan "}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "thinking_delta", "thinking": "first"}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "signature_delta", "signature": "sig-A"}},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "text_delta", "text": "Reading."}},
    {"type": "content_block_start", "index": 2,
     "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
    {"type": "content_block_delta", "index": 2,
     "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"a.txt\"}"}},
    {"type": "content_block_start", "index": 3,
     "content_block": {"type": "redacted_thinking", "data": "opaque"}},
    {"type": "content_block_start", "index": 4,
     "content_block": {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {}}},
    {"type": "content_block_delta", "index": 4,
     "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"b.txt\"}"}},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
    {"type": "message_stop"},
)

EXPECTED_BLOCKS = [
    {"type": "thinking", "thinking": "plan first", "signature": "sig-A"},
    {"type": "text", "text": "Reading."},
    {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}},
    {"type": "redacted_thinking", "data": "opaque"},
    {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {"path": "b.txt"}},
]


def test_thinking_blocks_are_captured_in_order() -> None:
    provider = make(Capture(THINKING_TURN), "claude-opus-5-5")
    events = asyncio.run(collect(provider))
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    assert done.metadata == {"anthropic_content": EXPECTED_BLOCKS}
    assert [call.id for call in done.tool_calls] == ["toolu_1", "toolu_2"]
    # Thinking never leaks into visible text.
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Reading."


def test_turn_without_thinking_has_no_metadata() -> None:
    provider = make(Capture(ANTHROPIC_OK), "claude-opus-5-5")
    done = [e for e in asyncio.run(collect(provider)) if isinstance(e, DoneEvent)][-1]
    assert done.metadata is None


def history_with_thinking() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": "Reading.",
            "tool_calls": [
                {"id": "toolu_1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"path\": \"a.txt\"}"}},
                {"id": "toolu_2", "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"path\": \"b.txt\"}"}},
            ],
            "provider_meta": {"anthropic_content": EXPECTED_BLOCKS},
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "A"},
        {"role": "tool", "tool_call_id": "toolu_2", "content": "B"},
    ]


def test_thinking_is_echoed_verbatim_to_messages() -> None:
    history = history_with_thinking()
    _, converted = convert_messages_for_anthropic(history)
    assert converted[1] == {"role": "assistant", "content": EXPECTED_BLOCKS}
    # The stored history is never mutated by conversion or cache marking.
    capture = Capture(ANTHROPIC_OK)
    asyncio.run(collect(make(capture, "claude-opus-5-5", prompt_cache=True), messages=history))
    assert history[2]["provider_meta"]["anthropic_content"] == EXPECTED_BLOCKS
    assert capture.payload["messages"][1]["content"] == EXPECTED_BLOCKS


def test_thinking_never_reaches_other_protocols() -> None:
    history = history_with_thinking()
    for text in (
        json.dumps(sanitize_messages_for_openai(history)),
        json.dumps(convert_messages_for_responses(history)),
        json.dumps(convert_messages_for_google(history)),
    ):
        assert "sig-A" not in text
        assert "opaque" not in text


# ---- G6: prompt caching ----


def test_cache_control_marks_system_and_newest_block_for_claude() -> None:
    capture = Capture(ANTHROPIC_OK)
    provider = make(capture, "claude-opus-5-5", prompt_cache=True)
    asyncio.run(collect(provider, messages=history_with_thinking()))
    payload = capture.payload
    assert payload["system"] == [
        {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}
    ]
    last = payload["messages"][-1]["content"]
    assert last[-1]["cache_control"] == {"type": "ephemeral"}
    marked = [
        block
        for message in payload["messages"]
        for block in message["content"]
        if "cache_control" in block
    ]
    assert len(marked) == 1
    assert all(
        "cache_control" not in block
        for message in payload["messages"]
        for block in message["content"]
        if block["type"] in {"thinking", "redacted_thinking"}
    )


def test_cache_control_on_chat_for_claude() -> None:
    capture = Capture(CHAT_OK)
    provider = make(capture, "claude-opus-5-5", protocol="openai-chat", prompt_cache=True)
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    asyncio.run(collect(provider, messages=messages))
    sent = capture.payload["messages"]
    for message in sent:
        assert message["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize(
    ("model", "body", "overrides"),
    [
        ("glm-5.2", CHAT_OK, {"prompt_cache": True}),
        ("claude-opus-5-5", ANTHROPIC_OK, {}),
    ],
)
def test_no_cache_control_unless_claude_and_enabled(model, body, overrides) -> None:
    capture = Capture(body)
    asyncio.run(collect(make(capture, model, **overrides)))
    assert "cache_control" not in capture.requests[-1].content.decode()


# ---- G3/G4: retries ----


class Sequence_:
    """Answer each request with the next scripted body (or status)."""

    def __init__(self, *responses: tuple[int, str]) -> None:
        self.responses = list(responses)
        self.count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        status, body = self.responses.pop(0)
        return httpx.Response(status, text=body)


CUT_CHAT = chat_sse({"choices": [{"delta": {"content": "partial"}}]}, done=False)
ERROR_CHAT = (
    'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
    'data: {"error":{"message":"boom"}}\n\n'
)


def test_retry_partial_discards_a_cut_stream_and_repeats() -> None:
    handler = Sequence_((200, CUT_CHAT), (200, CHAT_OK))
    provider = make(handler, "glm-5.2", retry_partial=True)
    no_sleep(provider)
    events = asyncio.run(collect(provider))
    assert handler.count == 2
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["ok"]


class BrokenStream(httpx.AsyncByteStream):
    """Send real output, then drop the connection."""

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("connection reset")


def broken_then_ok() -> tuple[dict[str, int], Any]:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, stream=BrokenStream())
        return httpx.Response(200, text=CHAT_OK)

    return calls, handler


def test_retry_partial_repeats_a_transport_failure_after_output() -> None:
    calls, handler = broken_then_ok()
    provider = make(handler, "glm-5.2", retry_partial=True)
    no_sleep(provider)
    events = asyncio.run(collect(provider))
    assert calls["count"] == 2
    # The consumer never sees the discarded partial attempt.
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["ok"]


def test_transport_failure_after_output_is_final_by_default() -> None:
    calls, handler = broken_then_ok()
    provider = make(handler, "glm-5.2")
    no_sleep(provider)
    with pytest.raises(ProviderError):
        asyncio.run(collect(provider))
    assert calls["count"] == 1


def test_retry_partial_repeats_an_anthropic_stream_without_stop_reason() -> None:
    cut = ANTHROPIC_OK.split("event: message_delta")[0]
    handler = Sequence_((200, cut), (200, ANTHROPIC_OK))
    provider = make(handler, "claude-opus-5-5", retry_partial=True)
    no_sleep(provider)
    events = asyncio.run(collect(provider))
    assert handler.count == 2
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["ok"]


def test_retry_partial_never_repeats_a_400() -> None:
    handler = Sequence_((400, '{"error":"temperature"}'), (200, CHAT_OK))
    provider = make(handler, "glm-5.2", retry_partial=True)
    no_sleep(provider)
    with pytest.raises(ProviderError):
        asyncio.run(collect(provider))
    assert handler.count == 1


def test_mid_stream_error_is_not_retryable_by_default_even_in_generic() -> None:
    handler = Sequence_((200, ERROR_CHAT), (200, CHAT_OK))
    provider = make(handler, "glm-5.2")
    no_sleep(provider)
    with pytest.raises(ProviderError):
        asyncio.run(collect(provider))
    assert handler.count == 1


def test_cut_stream_is_accepted_without_retry_partial() -> None:
    handler = Sequence_((200, CUT_CHAT))
    provider = make(handler, "glm-5.2")
    events = asyncio.run(collect(provider))
    assert handler.count == 1
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["partial"]


def test_retry_delay_is_capped_by_settings() -> None:
    handler = Sequence_((503, ""), (200, CHAT_OK))
    provider = make(
        handler, "glm-5.2", retry_base_seconds=30, max_retry_delay_seconds=2.0
    )
    sleeps: list[float] = []
    no_sleep(provider, sleeps)
    asyncio.run(collect(provider))
    assert sleeps and sleeps[0] <= 2.0 * 1.3


def test_retry_never_waits_past_the_deadline() -> None:
    handler = Sequence_((503, ""), (200, CHAT_OK))
    provider = make(handler, "glm-5.2", retry_base_seconds=10)
    sleeps: list[float] = []
    no_sleep(provider, sleeps)
    with pytest.raises(ProviderError):
        asyncio.run(collect(provider, deadline=time.monotonic() + 1.0))
    assert sleeps == []
    assert handler.count == 1
