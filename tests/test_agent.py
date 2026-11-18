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

# ---- PLE readiness: instruction pinning, thinking hygiene, wall-clock loop ----

THINKING_META = {
    "anthropic_content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "ok"},
    ]
}


def _fit_budget(agent: Agent, extra_chars: int) -> None:
    """Size the context window so the budget is the current history plus extra."""

    from panda_core.agent import _messages_chars

    agent.context_window = int((_messages_chars(agent.messages) + extra_chars) / 3.6) + 1


def test_trimming_never_drops_the_task_instruction(workspace) -> None:
    agent, _, provider = make_agent(
        workspace, [[TextDelta("ok"), DoneEvent(finish_reason="stop")]]
    )
    agent.messages.append({"role": "user", "content": "TASK: size the cables"})
    _fit_budget(agent, 3_000)
    for index in range(30):
        agent.messages.append({"role": "assistant", "content": "x" * 1_000 + str(index)})
        agent.messages.append({"role": "user", "content": "y" * 1_000 + str(index)})
    asyncio.run(agent.run_turn("latest"))
    assert agent.messages[1] == {"role": "user", "content": "TASK: size the cables"}
    sent = provider.calls[-1]
    assert sent[1] == {"role": "user", "content": "TASK: size the cables"}
    assert {"role": "user", "content": "latest"} in sent
    assert len(sent) < 63


def test_trimming_elides_old_tool_results_before_dropping_messages(workspace) -> None:
    agent, _, _ = make_agent(workspace, [[TextDelta("ok"), DoneEvent(finish_reason="stop")]])
    agent.messages.append({"role": "user", "content": "TASK"})
    _fit_budget(agent, 2_500)
    agent.messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        }
    )
    agent.messages.append({"role": "tool", "tool_call_id": "c1", "content": "z" * 6_000})
    for index in range(4):
        agent.messages.append({"role": "assistant", "content": f"note {index}"})
        agent.messages.append({"role": "user", "content": f"more {index}"})
    before = len(agent.messages)
    asyncio.run(agent.run_turn("latest"))
    # Nothing was dropped: eliding the stale tool result was enough.
    assert len(agent.messages) == before + 2
    tool = next(m for m in agent.messages if m.get("role") == "tool")
    assert tool["tool_call_id"] == "c1"
    assert tool["content"].startswith("[tool result elided: 6000 chars")


def test_append_only_history_keeps_thinking(workspace) -> None:
    agent, _, provider = make_agent(
        workspace, [[TextDelta("ok"), DoneEvent(finish_reason="stop")]]
    )
    agent.messages.append({"role": "user", "content": "TASK"})
    agent.messages.append({"role": "assistant", "content": "ok", "provider_meta": THINKING_META})
    asyncio.run(agent.run_turn("next"))
    sent = next(m for m in provider.calls[-1] if m.get("role") == "assistant")
    assert sent["provider_meta"] == THINKING_META


def test_trimming_drops_every_thinking_block(workspace) -> None:
    agent, _, provider = make_agent(
        workspace, [[TextDelta("ok"), DoneEvent(finish_reason="stop")]]
    )
    agent.messages.append({"role": "user", "content": "TASK"})
    _fit_budget(agent, 3_000)
    for index in range(10):
        agent.messages.append(
            {"role": "assistant", "content": "x" * 1_000, "provider_meta": THINKING_META}
        )
        agent.messages.append({"role": "user", "content": f"more {index}"})
    asyncio.run(agent.run_turn("latest"))
    assert all("provider_meta" not in m for m in provider.calls[-1])


def test_set_model_drops_thinking_but_keeps_other_meta(workspace) -> None:
    agent, _, _ = make_agent(workspace, [])
    agent.messages.append({"role": "user", "content": "TASK"})
    agent.messages.append(
        {
            "role": "assistant",
            "content": "ok",
            "provider_meta": {**THINKING_META, "other": 1},
        }
    )
    agent.set_model("opencode-go/glm-5.2")
    assert agent.messages[-1]["provider_meta"] == {"other": 1}
    agent.messages[-1]["provider_meta"] = dict(THINKING_META)
    agent.set_active_study(None)
    assert "provider_meta" not in agent.messages[-1]


def test_turn_metadata_reaches_the_next_provider_call(workspace) -> None:
    call = ToolCall(id="toolu_1", name="read_file", arguments='{"path":"README.md"}')
    agent, _, provider = make_agent(
        workspace,
        [
            [DoneEvent(finish_reason="tool_calls", tool_calls=(call,), metadata=THINKING_META)],
            [TextDelta("done"), DoneEvent(finish_reason="stop")],
        ],
    )
    asyncio.run(agent.run_turn("read it"))
    sent = next(m for m in provider.calls[1] if m.get("role") == "assistant")
    assert sent["provider_meta"] == THINKING_META


class SlowProvider:
    def __init__(self) -> None:
        self.deadlines: list[float | None] = []

    async def stream(self, messages, tools=None, model=None, session_id=None, deadline=None):
        self.deadlines.append(deadline)
        yield TextDelta("partial")
        await asyncio.sleep(10)
        yield DoneEvent(finish_reason="stop")


def _agent_with(workspace, provider, **kwargs) -> tuple[Agent, SessionLog]:
    session = SessionLog.create(workspace.sessions_dir, label="deadline")
    policy, _ = load_policy_or_default(workspace.policy_path)
    context = ToolContext(
        workspace=workspace,
        policy=PolicyEngine(policy, root=workspace.root),
        session=session,
    )
    return Agent(workspace, provider, ToolRegistry(context), session=session, **kwargs), session


def test_loop_stops_on_the_wall_clock_without_a_partial_turn(workspace) -> None:
    provider = SlowProvider()
    agent, session = _agent_with(workspace, provider, max_steps=1_000, deadline_seconds=0.05)
    result = asyncio.run(asyncio.wait_for(agent.run_turn("task"), timeout=5))
    assert result.status == "deadline"
    assert result.steps == 1
    assert agent.messages[-1] == {"role": "user", "content": "task"}
    assert provider.deadlines and provider.deadlines[0] is not None
    ends = [event.data for event in session.events() if event.type == "turn.end"]
    assert ends[-1]["status"] == "deadline"


def test_expired_deadline_stops_before_calling_the_model(workspace) -> None:
    provider = SlowProvider()
    agent, _ = _agent_with(workspace, provider, deadline_seconds=0)
    result = asyncio.run(agent.run_turn("task"))
    assert result.status == "deadline"
    assert result.steps == 0
    assert provider.deadlines == []


# ---- Onda 1.5: hooks, frozen tools, per-request effort ----


class KwargsProvider(FakeProvider):
    def __init__(self, script: list[list[Any]]) -> None:
        super().__init__(script)
        self.kwargs: list[dict[str, Any]] = []
        self.tool_names: list[list[str]] = []

    async def stream(self, messages, tools=None, **kwargs):
        self.kwargs.append(kwargs)
        self.tool_names.append([tool["function"]["name"] for tool in tools or []])
        async for event in super().stream(
            messages, tools, model=kwargs.get("model"), session_id=kwargs.get("session_id")
        ):
            yield event


def test_before_finish_reopens_the_turn_with_a_message_at_the_end(workspace) -> None:
    from panda_core.agent import AgentHooks

    seen: list[str] = []

    async def gate(agent: Agent, text: str) -> str | None:
        seen.append(text)
        return None if len(seen) > 1 else "GATE: FAIL — deliverable missing"

    agent, session, provider = make_agent(
        workspace,
        [
            [TextDelta("done?"), DoneEvent(finish_reason="stop")],
            [TextDelta("fixed"), DoneEvent(finish_reason="stop")],
        ],
    )
    agent.hooks = AgentHooks(before_finish=gate)
    result = asyncio.run(agent.run_turn("task"))
    assert result.status == "completed"
    assert result.steps == 2
    assert seen == ["done?", "fixed"]
    second = provider.calls[1]
    assert second[-1] == {"role": "user", "content": "GATE: FAIL — deliverable missing"}
    assert second[-2]["role"] == "assistant"
    assert "hook.message" in event_types(session)


def test_before_finish_reopens_are_bounded(workspace) -> None:
    from panda_core.agent import AgentHooks

    async def always_fail(agent: Agent, text: str) -> str:
        return "GATE: FAIL"

    agent, _, provider = make_agent(
        workspace,
        [[TextDelta("x"), DoneEvent(finish_reason="stop")]] * 10,
        max_steps=10,
    )
    agent.hooks = AgentHooks(before_finish=always_fail, max_finish_reopens=2)
    result = asyncio.run(agent.run_turn("task"))
    assert result.steps == 3
    assert len(provider.calls) == 3


def test_after_tool_notes_follow_all_tool_results(workspace) -> None:
    from panda_core.agent import AgentHooks

    calls = (
        ToolCall(id="c1", name="read_file", arguments='{"path":"README.md"}'),
        ToolCall(id="c2", name="read_file", arguments='{"path":"README.md"}'),
    )

    async def loop_detector(agent: Agent, call: Any, result: Any) -> str | None:
        return f"note {call.id}"

    agent, _, provider = make_agent(
        workspace,
        [
            [DoneEvent(finish_reason="tool_calls", tool_calls=calls)],
            [TextDelta("done"), DoneEvent(finish_reason="stop")],
        ],
    )
    agent.hooks = AgentHooks(after_tool=loop_detector)
    asyncio.run(agent.run_turn("task"))
    tail = [m["role"] for m in provider.calls[1][-4:]]
    assert tail == ["assistant", "tool", "tool", "user"]
    assert provider.calls[1][-1]["content"] == "note c1\n\nnote c2"


def test_before_model_message_is_appended_before_each_call(workspace) -> None:
    from panda_core.agent import AgentHooks

    async def clock(agent: Agent) -> str:
        return "45% of the clock used"

    agent, _, provider = make_agent(workspace, [[TextDelta("ok"), DoneEvent()]])
    agent.hooks = AgentHooks(before_model=clock)
    asyncio.run(agent.run_turn("task"))
    assert provider.calls[0][-2:] == [
        {"role": "user", "content": "task"},
        {"role": "user", "content": "45% of the clock used"},
    ]


def test_hooks_never_edit_earlier_messages(workspace) -> None:
    from panda_core.agent import AgentHooks

    async def gate(agent: Agent, text: str) -> str | None:
        return "fix it" if text == "first" else None

    agent, _, provider = make_agent(
        workspace,
        [
            [TextDelta("first"), DoneEvent(finish_reason="stop")],
            [TextDelta("second"), DoneEvent(finish_reason="stop")],
        ],
    )
    agent.hooks = AgentHooks(before_finish=gate)
    asyncio.run(agent.run_turn("task"))
    first, second = provider.calls
    assert second[: len(first)] == first


def test_frozen_tools_ignore_later_registry_changes(workspace) -> None:
    call = ToolCall(id="c1", name="read_file", arguments='{"path":"README.md"}')
    session = SessionLog.create(workspace.sessions_dir, label="freeze")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(
        workspace=workspace, policy=PolicyEngine(policy, root=workspace.root), session=session
    )
    registry = ToolRegistry(ctx)
    provider = KwargsProvider(
        [
            [DoneEvent(finish_reason="tool_calls", tool_calls=(call,))],
            [TextDelta("done"), DoneEvent(finish_reason="stop")],
        ]
    )
    agent = Agent(workspace, provider, registry, session=session, freeze_tools=True)
    original = registry.to_openai

    def grown() -> list[dict[str, Any]]:
        extra = {"type": "function", "function": {"name": "late_tool", "parameters": {}}}
        return [*original(), extra]

    async def scenario() -> None:
        stream = provider.stream

        async def wrapped(messages, tools=None, **kwargs):
            registry.to_openai = grown  # type: ignore[method-assign]
            async for event in stream(messages, tools, **kwargs):
                yield event

        provider.stream = wrapped  # type: ignore[method-assign]
        await agent.run_turn("task")

    asyncio.run(scenario())
    assert provider.tool_names[0] == provider.tool_names[1]
    assert "late_tool" not in provider.tool_names[1]


def test_effort_is_sent_only_when_set(workspace) -> None:
    session = SessionLog.create(workspace.sessions_dir, label="effort")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(
        workspace=workspace, policy=PolicyEngine(policy, root=workspace.root), session=session
    )
    provider = KwargsProvider([[TextDelta("a"), DoneEvent()], [TextDelta("b"), DoneEvent()]])
    agent = Agent(workspace, provider, ToolRegistry(ctx), session=session)
    asyncio.run(agent.run_turn("one"))
    agent.effort = "max"
    asyncio.run(agent.run_turn("two"))
    assert "effort" not in provider.kwargs[0]
    assert provider.kwargs[1]["effort"] == "max"
