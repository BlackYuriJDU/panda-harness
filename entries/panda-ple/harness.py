"""Panda harness for PLE: one agent per task, acting in the task container.

`solve(session)` is called by `taskbridge` for every task connection and returns
the final message. It probes the container, freezes the MCP tool list, runs the
Panda agent loop against the model gateway, reports token usage, and always
keeps a fallback final message current, so a cancel at the time limit still
sends a useful summary.

Nothing about the task is written to the serve container's disk: the agent's
ledger lives in memory, and evaluator log lines carry only metadata unless
PANDA_PLE_LOG=verbose (for local dev runs only).

Settings (all optional, `env:` in submission.yaml):
  PANDA_PLE_EFFORT      reasoning effort override (low|medium|high|xhigh|max)
  PANDA_PLE_VISION      "1" enables the view_image tool
  PANDA_PLE_LOG         "metadata" (default) or "verbose"
  PANDA_PLE_MAX_STEPS   model steps per task (default 1000)
  PANDA_PLE_WRAP_UP_SEC seconds kept for the final summary (default 240)
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any

from panda_core.agent import Agent, AgentEvent
from panda_core.errors import PandaError
from panda_core.ledger import MemoryLedger
from panda_core.profiles import ple_provider_settings
from panda_core.protocols import PROTOCOL_ANTHROPIC
from panda_core.provider import OpenCodeProvider

import taskbridge
from panda_ple.mcp_tools import McpTools
from panda_ple.probe import run_probe
from panda_ple.prompt import build_prompt
from panda_ple.remote_tools import BACKUP_ROOT, RemoteToolset

ProviderFactory = Callable[[Any, str], OpenCodeProvider]
# Tests replace this to talk to a scripted model; production uses the gateway.
PROVIDER_FACTORY: ProviderFactory = lambda settings, model: OpenCodeProvider(  # noqa: E731
    settings, model=model
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Reporter:
    """Forwards agent events to the evaluator: usage, metadata logs, fallback summary."""

    def __init__(
        self, session: taskbridge.Session, tools: RemoteToolset, model: str, protocol: str
    ) -> None:
        self.session = session
        self.tools = tools
        self.model = model
        self.protocol = protocol
        self.verbose = os.environ.get("PANDA_PLE_LOG", "metadata") == "verbose"
        self.agent: Agent | None = None
        self.model_output = False
        self.steps = 0
        self.tool_calls = 0
        self.started = time.monotonic()
        self._pending: set[asyncio.Task[Any]] = set()

    def _send(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def log(self, text: str) -> None:
        self._send(self.session.log(f"[panda +{time.monotonic() - self.started:.0f}s] {text}"))

    def __call__(self, event: AgentEvent) -> None:
        data = event.data
        if event.kind == "usage":
            self.model_output = True
            self.steps += 1
            prompt = int(data.get("prompt_tokens") or 0)
            cached = int(data.get("cached_tokens") or 0)
            written = int(data.get("cache_write_tokens") or 0)
            # Anthropic counts cache reads and writes outside input_tokens;
            # the evaluator expects the total input.
            anthropic = self.protocol == PROTOCOL_ANTHROPIC
            total_input = prompt + cached + written if anthropic else prompt
            self._send(
                self.session.usage(
                    self.model,
                    input_tokens=total_input,
                    output_tokens=int(data.get("completion_tokens") or 0),
                    cache_read_tokens=cached,
                )
            )
        elif event.kind == "text_delta":
            self.model_output = True
        elif event.kind == "tool_call":
            self.tool_calls += 1
            if self.verbose:
                self.log(f"tool {data.get('name')}: {str(data.get('arguments'))[:300]}")
            else:
                self.log(f"step {self.steps}: tool call {self.tool_calls} ({data.get('name')})")
        elif event.kind == "tool_result":
            self.session.final_message = self.summary()
        elif event.kind == "turn_end":
            self.log(f"turn end: {data.get('status')} after {data.get('steps')} steps")

    def last_text(self) -> str:
        if self.agent is None:
            return ""
        for message in reversed(self.agent.messages):
            content = message.get("content")
            if message.get("role") == "assistant" and isinstance(content, str) and content.strip():
                return content.strip()
        return ""

    def summary(self, text: str | None = None) -> str:
        body = (text if text is not None else self.last_text()) or (
            "The run stopped before a final summary."
        )
        if self.tools.written:
            files = "\n".join(f"- {path}" for path in self.tools.written)
            body += f"\n\nFiles written during this run:\n{files}"
        return body

    async def flush(self) -> None:
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=5)


async def solve(session: taskbridge.Session) -> str:
    settings, model_ref = ple_provider_settings()
    effort = os.environ.get("PANDA_PLE_EFFORT", "").strip() or None
    provider = PROVIDER_FACTORY(settings, model_ref)
    model_id = provider.model_id(model_ref)
    protocol = provider.protocol_for(model_id)
    vision = os.environ.get("PANDA_PLE_VISION", "0") == "1"
    wrap_up = _env_int("PANDA_PLE_WRAP_UP_SEC", 240)

    # Everything the model sees at the start is gathered before its first call:
    # the tool list, MCP catalog included, never changes afterwards.
    probe = await run_probe(session)
    mcp = await McpTools.discover(session)
    tools = RemoteToolset(
        session,
        vision=vision,
        mcp=mcp,
        backup_root=os.environ.get("PANDA_PLE_BACKUP_ROOT") or BACKUP_ROOT,
    )
    reporter = Reporter(session, tools, model_id, protocol)
    reporter.log(
        f"start: model={model_id} protocol={protocol} mcp_tools={len(mcp.tools)} "
        f"mcp_direct={mcp.direct} vision={vision}"
    )
    left = session.time_left()
    agent = Agent(
        None,
        provider,
        tools,
        session=MemoryLedger(),
        model=model_ref,
        context_window=settings.context_window,
        system_prompt=build_prompt(
            workdir=session.workdir, time_left=left, probe=probe, mcp=mcp.summary()
        ),
        max_steps=_env_int("PANDA_PLE_MAX_STEPS", 1000),
        deadline_seconds=None if left is None else max(30.0, left - wrap_up),
        on_event=reporter,
        private=True,
        freeze_tools=True,
        effort=effort,
    )
    reporter.agent = agent
    # session.final_message stays empty until the model has produced output:
    # taskbridge then reports a failure as `error`, so the evaluator can start
    # the task again instead of grading an empty run.
    try:
        await agent.run_turn(session.instruction)
    except PandaError as exc:
        reporter.log(f"stopped by {exc.code}")
        await reporter.flush()
        if not reporter.model_output:
            # Nothing happened yet: failing lets the evaluator start the task again.
            session.final_message = ""
            raise
        return reporter.summary()
    finally:
        await provider.aclose()
    await reporter.flush()
    return reporter.summary(reporter.last_text())


__all__ = ["PROVIDER_FACTORY", "Reporter", "solve"]
