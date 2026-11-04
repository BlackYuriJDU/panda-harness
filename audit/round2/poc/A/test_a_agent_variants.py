"""Trilha A — variantes de AGT-001 / AGT-002."""

from __future__ import annotations

import asyncio
import json

from panda_core.agent import Agent
from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.provider import DoneEvent, ToolCall
from panda_core.session import SessionLog
from panda_core.tools import ToolContext, ToolRegistry


class Script:
    def __init__(self, first):
        self.first = first
        self.calls = []

    async def stream(self, messages, tools=None, model=None, session_id=None):
        self.calls.append([dict(m) for m in messages])
        if len(self.calls) == 1:
            yield self.first
        else:
            yield DoneEvent(finish_reason="stop")


def make(workspace, first, **kw):
    session = SessionLog.create(workspace.sessions_dir, label="poc")
    policy, _ = load_policy_or_default(workspace.policy_path)
    registry = ToolRegistry(ToolContext(workspace=workspace,
                                        policy=PolicyEngine(policy, root=workspace.root),
                                        session=session))
    script = Script(first)
    return Agent(workspace, script, registry, session=session, max_steps=3, **kw), script, session


# R2-121: argumentos JSON profundamente aninhados geram RecursionError em
# parsed_arguments, fora do try que protege o handler: o turno quebra e a tool call
# fica sem resposta no histórico (AGT-001 por outra rota).
def test_deeply_nested_arguments_become_a_failed_result(workspace):
    deep = "[" * 200_000 + "]" * 200_000
    call = ToolCall(id="c1", name="read_file", arguments=deep)
    agent, script, _ = make(workspace, DoneEvent(finish_reason="tool_calls", tool_calls=(call,)))
    try:
        result = asyncio.run(agent.run_turn("x"))
        status = result.status
    except Exception as exc:  # noqa: BLE001
        status = f"raised {type(exc).__name__}"
    assert status == "completed", f"turno não terminou normalmente: {status}"


# AGT-002 (refutação esperada): tarefa maior que a janela continua no primeiro envio.
def test_a_task_larger_than_the_window_is_still_sent(workspace):
    task = "TASK-START " + "x" * 60_000 + " TASK-END"
    agent, script, _ = make(workspace, DoneEvent(finish_reason="stop"), context_window=2_000)
    asyncio.run(agent.run_turn(task))
    sent = json.dumps(script.calls[0])
    assert "TASK-START" in sent and "TASK-END" in sent
