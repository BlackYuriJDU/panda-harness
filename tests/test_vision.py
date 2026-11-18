"""Opt-in vision: `view_image`, image parts in history, and adapter conversion."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from panda_core.agent import Agent, _messages_chars
from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.provider import (
    DoneEvent,
    TextDelta,
    ToolCall,
    convert_messages_for_anthropic,
    convert_messages_for_google,
    convert_messages_for_responses,
    sanitize_messages_for_openai,
)
from panda_core.session import SessionLog
from panda_core.tools import ToolContext, ToolRegistry

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PNG_B64 = base64.b64encode(PNG).decode("ascii")
URL = f"data:image/png;base64,{PNG_B64}"


def registry(workspace, *, vision: bool) -> ToolRegistry:
    session = SessionLog.create(workspace.sessions_dir, label="vision")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(
        workspace=workspace,
        policy=PolicyEngine(policy, root=workspace.root),
        session=session,
        vision=vision,
    )
    return ToolRegistry(ctx)


def test_view_image_is_registered_only_with_the_flag(workspace) -> None:
    assert "view_image" not in registry(workspace, vision=False).names()
    assert "view_image" in registry(workspace, vision=True).names()


def test_view_image_returns_the_image(workspace) -> None:
    (workspace.root / "drawing.png").write_bytes(PNG)
    tools = registry(workspace, vision=True)
    result = asyncio.run(tools.call("view_image", {"path": "drawing.png"}))
    assert result.ok
    assert result.images == [("image/png", PNG_B64)]
    assert json.loads(result.content)["media_type"] == "image/png"
    assert PNG_B64 not in result.content
    assert result.to_dict()["images"] == 1


def test_view_image_fails_closed(workspace) -> None:
    (workspace.root / "notes.png").write_text("not an image", encoding="utf-8")
    tools = registry(workspace, vision=True)
    for path in ("notes.png", "missing.png", "../outside.png"):
        result = asyncio.run(tools.call("view_image", {"path": path}))
        assert not result.ok
        assert not result.images


def test_text_tool_results_are_unchanged(workspace) -> None:
    result = asyncio.run(registry(workspace, vision=False).call("workspace_info", {}))
    assert "images" not in result.to_dict()


class Recorder:
    def __init__(self, script: list[list[Any]]) -> None:
        self.script = list(script)
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(self, messages, tools=None, model=None, session_id=None):
        self.calls.append([dict(message) for message in messages])
        for event in self.script.pop(0):
            yield event


def test_agent_sends_image_parts_and_logs_no_bytes(workspace) -> None:
    (workspace.root / "drawing.png").write_bytes(PNG)
    tools = registry(workspace, vision=True)
    call = ToolCall(id="c1", name="view_image", arguments='{"path":"drawing.png"}')
    provider = Recorder(
        [
            [DoneEvent(finish_reason="tool_calls", tool_calls=(call,))],
            [TextDelta("a riser"), DoneEvent(finish_reason="stop")],
        ]
    )
    agent = Agent(workspace, provider, tools, session=tools.ctx.session)
    asyncio.run(agent.run_turn("what is drawn?"))
    tool = next(m for m in provider.calls[1] if m.get("role") == "tool")
    assert tool["content"][1] == {"type": "image_url", "image_url": {"url": URL}}
    ledger = (agent.session.path).read_text(encoding="utf-8")
    assert PNG_B64 not in ledger


def test_image_parts_count_a_fixed_size_in_the_context_budget() -> None:
    big = "A" * 400_000
    message = {
        "role": "tool",
        "tool_call_id": "c1",
        "content": [
            {"type": "text", "text": "ok"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big}"}},
        ],
    }
    assert _messages_chars([message]) < 10_000


HISTORY = [
    {"role": "user", "content": [{"type": "text", "text": "look"},
                                 {"type": "image_url", "image_url": {"url": URL}}]},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "view_image", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": [
        {"type": "text", "text": "image ok"}, {"type": "image_url", "image_url": {"url": URL}}]},
    {"role": "tool", "tool_call_id": "c2", "content": "text result"},
]


def test_anthropic_receives_image_blocks() -> None:
    _, converted = convert_messages_for_anthropic(HISTORY)
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": PNG_B64}}
    assert converted[0]["content"] == [{"type": "text", "text": "look"}, image]
    results = converted[2]["content"]
    assert results[0]["content"] == [{"type": "text", "text": "image ok"}, image]
    assert results[1]["content"] == "text result"


def test_google_receives_inline_data() -> None:
    _, contents = convert_messages_for_google(HISTORY)
    inline = {"inlineData": {"mimeType": "image/png", "data": PNG_B64}}
    assert contents[0]["parts"] == [{"text": "look"}, inline]
    assert inline in contents[2]["parts"]
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "view_image"


def test_chat_moves_tool_images_after_the_tool_results() -> None:
    sent = sanitize_messages_for_openai(HISTORY)
    roles = [message["role"] for message in sent]
    assert roles == ["user", "assistant", "tool", "tool", "user"]
    assert sent[0]["content"][1]["image_url"]["url"] == URL
    assert sent[2]["content"] == "image ok"
    assert sent[4]["content"][1] == {"type": "image_url", "image_url": {"url": URL}}


def test_responses_reports_dropped_images() -> None:
    _, items = convert_messages_for_responses(HISTORY)
    output = next(
        item for item in items
        if item.get("type") == "function_call_output" and item["call_id"] == "c1"
    )["output"]
    assert "1 image(s) not sent" in output
    assert PNG_B64 not in json.dumps(items)
