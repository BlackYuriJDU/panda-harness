"""Agent loop: text, tool calls, approvals, max steps, cancellation, ledger."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest

from panda_core.agent import Agent
from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.provider import DoneEvent, ProviderError, TextDelta, ToolCall, Usage, UsageEvent
from panda_core.session import SessionLog
from panda_core.tools import ApprovalRequest, ToolContext, ToolRegistry


class FakeProvider:
    def __init__(self, script: list[list[Any]]) -> None:
        self.script = list(script)
        self.calls: list[list[dict[str, Any]]] = []
        self.sessions: list[str | None] = []

    async def stream(self, messages, tools=None, model=None, session_id=None):
        self.calls.append([dict(message) for message in messages])
        self.sessions.append(session_id)
        events = self.script.pop(0) if self.script else [DoneEvent(finish_reason="stop")]
        for event in events:
            if isinstance(event, Exception):
                raise event
            yield event


def make_agent(
    workspace,
    script: list[list[Any]],
    *,
    approver=None,
    max_steps: int = 4,
    private: bool = False,
    context_window: int | None = None,
) -> tuple[Agent, SessionLog, FakeProvider]:
    session = SessionLog.create(workspace.sessions_dir, label="agent-test")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(
        workspace=workspace,
        policy=PolicyEngine(policy, root=workspace.root),
        session=session,
    )
    registry = ToolRegistry(ctx)
    provider = FakeProvider(script)
    agent = Agent(
        workspace,
        provider,
        registry,
        session=session,
        max_steps=max_steps,
        approver=approver,
        private=private,
        context_window=context_window,
    )
    return agent, session, provider


def event_types(session: SessionLog) -> list[str]:
    return [event.type for event in session.events()]


def test_text_turn_logs_the_contract(tmp_path, workspace) -> None:
    agent, session, _ = make_agent(
        workspace,
        [[TextDelta("hello "), TextDelta("world"), DoneEvent(finish_reason="stop")]],
    )
    result = asyncio.run(agent.run_turn("say hello"))
    assert result.status == "completed"
    assert result.text == "hello world"
    assert result.steps == 1
    assert session.verify().ok
    types = event_types(session)
    for expected in ("turn.start", "message.user", "message.assistant", "turn.end"):
        assert expected in types
    payload = json.loads(session.path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["type"] == "session.start"


def test_provider_receives_the_panda_session_id(workspace) -> None:
    agent, session, provider = make_agent(
        workspace,
        [[TextDelta("ok"), DoneEvent(finish_reason="stop")]],
    )
    asyncio.run(agent.run_turn("hi"))
    assert provider.sessions == [session.session_id]


def test_tool_call_round_trip(workspace) -> None:
    call = ToolCall(id="call_1", name="read_file", arguments='{"path":"README.md"}')
    agent, session, provider = make_agent(
        workspace,
        [
            [DoneEvent(finish_reason="tool_calls", tool_calls=(call,))],
            [TextDelta("read it"), DoneEvent(finish_reason="stop")],
        ],
    )
    result = asyncio.run(agent.run_turn("read the readme"))
    assert result.status == "completed"
    assert result.tool_calls == 1
    assert "read it" in result.text
    types = event_types(session)
    assert "tool.call" in types
    assert "tool.result" in types
    tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
    assert tool_messages and tool_messages[0]["tool_call_id"] == "call_1"
    assert "fixture corpus" in tool_messages[0]["content"]


def test_tool_call_metadata_reaches_the_next_provider_call(workspace) -> None:
    call = ToolCall(
        id="call_9",
        name="read_file",
        arguments='{"path":"README.md"}',
        metadata={"google": {"thought_signature": "sig"}},
    )
    agent, _, provider = make_agent(
        workspace,
        [
            [DoneEvent(finish_reason="tool_calls", tool_calls=(call,))],
            [TextDelta("done"), DoneEvent(finish_reason="stop")],
        ],
    )
    asyncio.run(agent.run_turn("read it"))

    assistant = next(m for m in agent.messages if m.get("role") == "assistant")
    expected = {"google": {"thought_signature": "sig"}}
    assert assistant["tool_calls"][0]["provider_meta"] == expected
    sent = next(m for m in provider.calls[1] if m.get("role") == "assistant")
    assert sent["tool_calls"][0]["provider_meta"] == expected

def test_approval_denied_blocks_write(workspace) -> None:
    call = ToolCall(
        id="call_2",
        name="write_file",
        arguments='{"path":"preprint/draft.md","content":"x"}',
    )

    async def deny(request: ApprovalRequest) -> bool:
        return False

    agent, session, _ = make_agent(
        workspace,
        [
            [DoneEvent(finish_reason="tool_calls", tool_calls=(call,))],
            [TextDelta("blocked"), DoneEvent(finish_reason="stop")],
        ],
        approver=deny,
    )
    result = asyncio.run(agent.run_turn("write a draft"))
    assert result.status == "completed"
    assert not (workspace.root / "preprint/draft.md").exists()
    types = event_types(session)
    assert "approval.request" in types
    assert "approval.decision" in types
    decisions = [
        event.data for event in session.events() if event.type == "approval.decision"
    ]
    assert decisions[-1]["approved"] is False
    results = [event.data for event in session.events() if event.type == "tool.result"]
    assert results[-1]["blocked"] is True
    assert "approval denied" in results[-1]["content"]


def test_approval_accepted_writes(workspace) -> None:
    call = ToolCall(
        id="call_3",
        name="write_file",
        arguments='{"path":"preprint/ok.md","content":"approved"}',
    )

    async def allow(request: ApprovalRequest) -> bool:
        return True

    agent, session, _ = make_agent(
        workspace,
        [
            [DoneEvent(finish_reason="tool_calls", tool_calls=(call,))],
            [TextDelta("done"), DoneEvent(finish_reason="stop")],
        ],
        approver=allow,
    )
    asyncio.run(agent.run_turn("write the draft"))
    assert (workspace.root / "preprint/ok.md").read_text(encoding="utf-8") == "approved"
    assert session.verify().ok


def test_hard_denied_read_never_executes(workspace) -> None:
    (workspace.root / ".env").write_text("SECRET=1", encoding="utf-8")
    call = ToolCall(id="call_4", name="read_file", arguments='{"path":".env"}')
    agent, _, _ = make_agent(
        workspace,
        [
            [DoneEvent(finish_reason="tool_calls", tool_calls=(call,))],
            [TextDelta("cannot"), DoneEvent(finish_reason="stop")],
        ],
    )
    asyncio.run(agent.run_turn("read env"))
    tool_message = [m for m in agent.messages if m.get("role") == "tool"][-1]
    assert '"blocked":true' in tool_message["content"]
    assert "SECRET" not in tool_message["content"]


def test_max_steps_stops_the_loop(workspace) -> None:
    call = ToolCall(id="call_5", name="read_file", arguments='{"path":"README.md"}')
    agent, session, _ = make_agent(
        workspace,
        [
            [DoneEvent(finish_reason="tool_calls", tool_calls=(call,))],
            [DoneEvent(finish_reason="tool_calls", tool_calls=(call,))],
        ],
        max_steps=2,
    )
    result = asyncio.run(agent.run_turn("loop"))
    assert result.status == "max_steps"
    assert result.steps == 2
    assert event_types(session).count("tool.result") == 2


def test_private_turn_does_not_persist_text(workspace) -> None:
    agent, session, _ = make_agent(
        workspace,
        [[TextDelta("ok"), DoneEvent(finish_reason="stop")]],
        private=True,
    )
    task = "a very private instruction"
    asyncio.run(agent.run_turn(task))
    contents = session.path.read_text(encoding="utf-8")
    assert task not in contents
    assert hashlib.sha256(task.encode("utf-8")).hexdigest() in contents


def test_usage_event_is_logged(workspace) -> None:
    agent, session, _ = make_agent(
        workspace,
        [
            [
                TextDelta("hi"),
                UsageEvent(usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12)),
                DoneEvent(finish_reason="stop"),
            ]
        ],
    )
    result = asyncio.run(agent.run_turn("hi"))
    assert result.usage.total_tokens == 12
    payloads = [event.data for event in session.events() if event.type == "turn.end"]
    assert payloads[-1]["usage"]["total_tokens"] == 12


def test_provider_error_is_logged_and_reraised(workspace) -> None:
    agent, session, _ = make_agent(workspace, [[ProviderError("boom")]])
    with pytest.raises(ProviderError):
        asyncio.run(agent.run_turn("fail"))
    types = event_types(session)
    assert "error" in types
    assert "turn.end" not in types
def test_cancellation_is_logged_and_bounded(workspace) -> None:
    class HangingProvider:
        async def stream(self, messages, tools=None, model=None, session_id=None):
            await asyncio.sleep(10)
            yield DoneEvent()

    session = SessionLog.create(workspace.sessions_dir, label="cancel")
    policy, _ = load_policy_or_default(workspace.policy_path)
    context = ToolContext(
        workspace=workspace,
        policy=PolicyEngine(policy, root=workspace.root),
        session=session,
    )
    registry = ToolRegistry(context)
    agent = Agent(workspace, HangingProvider(), registry, session=session, max_steps=2)

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(agent.run_turn("hang"), timeout=0.05)

    asyncio.run(scenario())
    ends = [event.data for event in session.events() if event.type == "turn.end"]
    assert ends and ends[-1]["status"] == "cancelled"


def test_context_trimming_keeps_system_and_recent(workspace) -> None:
    agent, _, _ = make_agent(
        workspace,
        [[TextDelta("ok"), DoneEvent(finish_reason="stop")]],
        context_window=1_000,
    )
    agent.messages.append({"role": "user", "content": "start"})
    for index in range(30):
        agent.messages.append({"role": "assistant", "content": "x" * 1_000 + str(index)})
        agent.messages.append({"role": "user", "content": "y" * 1_000 + str(index)})
    before = len(agent.messages)
    asyncio.run(agent.run_turn("latest"))
    assert agent.messages[0]["role"] == "system"
    assert "biomedical evidence harness" in agent.messages[0]["content"]
    assert len(agent.messages) < before
    assert {"role": "user", "content": "latest"} in agent.messages
    assert agent.messages[-1] == {"role": "assistant", "content": "ok"}


def test_system_prompt_carries_the_rules(workspace) -> None:
    from panda_core.agent import build_system_prompt

    prompt = build_system_prompt(workspace, active_study="gse160208")
    assert "fail-closed" in prompt
    assert "clinical advice" in prompt
    assert "gse160208" in prompt
    assert str(workspace.root.as_posix()) in prompt
    assert "validate" in prompt