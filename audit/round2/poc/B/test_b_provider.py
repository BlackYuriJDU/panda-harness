"""Trilha B — provider.py: eventos terminais, ids e reparo de histórico (4 protocolos)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from panda_core.errors import PandaError
from panda_core.provider import (
    OpenCodeProvider,
    ProviderSettings,
    convert_messages_for_anthropic,
    convert_messages_for_google,
    convert_messages_for_responses,
)


def _provider(body: str, model: str) -> OpenCodeProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenCodeProvider(ProviderSettings(), model=f"opencode-go/{model}", client=client,
                            api_key="SYNTH_SECRET_KEY_0000")


async def _collect(provider: OpenCodeProvider) -> list[Any]:
    return [e async for e in provider.stream([{"role": "user", "content": "oi"}])]


def _sse(*chunks: Any) -> str:
    return "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks)


CUT_THEN_DONE = {
    "claude-sonnet-5": _sse(
        {"type": "message_start", "message": {"model": "claude", "usage": {}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Resposta parcial"}},
        "[DONE]",
    ),
    "gpt-5.5": _sse(
        {"type": "response.created", "response": {"model": "gpt"}},
        {"type": "response.output_text.delta", "delta": "Resposta parcial"},
        "[DONE]",
    ),
    "gemini-4-pro": _sse(
        {"candidates": [{"content": {"parts": [{"text": "Resposta parcial"}]}}]},
        "[DONE]",
    ),
}


@pytest.mark.parametrize("model", sorted(CUT_THEN_DONE))
def test_done_marker_is_not_a_terminal_event_for_non_openai_protocols(model: str) -> None:
    provider = _provider(CUT_THEN_DONE[model], model)
    with pytest.raises(PandaError):
        asyncio.run(_collect(provider))


def _history_with_repeated_provider_ids() -> list[dict[str, Any]]:
    call = {"id": "call_0", "type": "function",
            "function": {"name": "read_file", "arguments": "{\"path\":\"a\"}"}}
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "t"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_0", "content": "A"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_0", "content": "B"},
    ]


def test_anthropic_history_has_unique_tool_use_ids_when_provider_repeats_ids() -> None:
    _, turns = convert_messages_for_anthropic(_history_with_repeated_provider_ids())
    ids = [b["id"] for t in turns for b in t["content"]
           if isinstance(b, dict) and b.get("type") == "tool_use"]
    assert len(ids) == len(set(ids)), ids


def test_responses_history_keeps_each_output_with_its_call_when_ids_repeat() -> None:
    _, items = convert_messages_for_responses(_history_with_repeated_provider_ids())
    calls = [i["call_id"] for i in items if i.get("type") == "function_call"]
    assert len(calls) == len(set(calls)), calls


def test_google_history_answers_a_dangling_function_call() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "t"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "user", "content": "continue"},
    ]
    _, contents = convert_messages_for_google(messages)
    for position, turn in enumerate(contents):
        calls = [p for p in turn["parts"] if "functionCall" in p]
        if not calls:
            continue
        following = contents[position + 1]["parts"] if position + 1 < len(contents) else []
        responses = [p for p in following if "functionResponse" in p]
        assert len(responses) == len(calls), (calls, following)
