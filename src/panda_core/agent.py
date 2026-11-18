"""Autonomous turn/step loop for the Panda harness.

The agent owns the conversation and the tool loop; it does not render. Every
model-visible message and tool result is written to the hash-chained session
ledger before the next step, so a transcript can be reconstructed from
provenance alone. Approvals are explicit events, never implicit consent.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panda_core.errors import PandaError, ProviderError
from panda_core.limits import DEFAULT_MAX_STEPS
from panda_core.provider import (
    DoneEvent,
    OpenCodeProvider,
    ProviderEvent,
    TextDelta,
    ToolCallDelta,
    Usage,
    UsageEvent,
)
from panda_core.session import SessionLog
from panda_core.tools import ApprovalRequest, ToolRegistry, ToolResult
from panda_core.workspace import Workspace

DEFAULT_SYSTEM_CHARS = 24_000
LOG_STRING_LIMIT = 8_000
LOG_LIST_LIMIT = 200
# Context trimming never elides the tool results of the newest messages.
TRIM_KEEP_RECENT = 6
ELIDE_MIN_CHARS = 200
ELIDED_MARKER = "[tool result elided"
# Context estimate for one image part (~1,600 tokens), instead of its base64.
IMAGE_CHARS = 6_400

SYSTEM_PROMPT = """You are Panda, a fail-closed biomedical evidence harness.

You operate inside a research corpus and you are judged by provenance, not by
enthusiasm. Follow these rules in order:

1. Every claim carries a `study_id` and a `track` (`public`, `simulated`, `real`).
2. Every number resolves to an `evidence_id`; every claim resolves to
   `evidence_ids` and `number_ids`. If a link does not resolve, say so.
3. Evidence must resolve to `source_path:source_line`, a hash, and a license.
   Restricted or non-commercial sources are metadata-only: never copy payloads.
4. Never fabricate a citation, a number, a license, or a validation result.
5. Run the `validate` tool after changing corpus content. Its report is
   authoritative; do not reimplement or rationalize its checks. A failing check
   is a stop condition.
6. A policy block is final. Do not retry the same action through another tool.
7. Never read, print, or persist secrets. Never push, publish, or rewrite git
   history. Those require a human outside this harness.
8. This is research tooling. Do not give clinical advice or interpret results
   as medical guidance.
9. Separate observation, inference, and unknown. State what would falsify a
   claim.

Workspace root: {root}
Validator: {validator} (exists: {validator_exists})
Provider: {provider} ({base_url})
Model: {model}
Active study: {study}

Corpus rules live in the workspace instructions below. When they conflict with
your habits, the corpus wins. Cite concrete locations as `path:line`.

Workspace instructions:
{instructions}
"""


HookText = Awaitable[str | None]


@dataclass
class AgentHooks:
    """Optional loop hooks; each may return text to add to the conversation.

    Returned text is always appended as a new `user` message at the end of
    the history, never written into earlier messages: editing history would
    invalidate signed Claude thinking blocks.

    - `before_model(agent)`: before each model call (clock warnings, phase
      notes).
    - `after_tool(agent, call, result)`: after each tool result; notes from one
      step are joined into a single message after all of its tool results.
    - `before_finish(agent, text)`: when the model answers without tool calls;
      returned text reopens the turn (gates that failed), at most
      `max_finish_reopens` times per turn.
    """

    before_model: Callable[[Agent], HookText] | None = None
    after_tool: Callable[[Agent, Any, ToolResult], HookText] | None = None
    before_finish: Callable[[Agent, str], HookText] | None = None
    max_finish_reopens: int = 3


@dataclass(frozen=True)
class AgentEvent:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResult:
    status: str
    text: str = ""
    steps: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    context_tokens: int = 0
    context_window: int = 0
    session_id: str | None = None

    @property
    def context_ratio(self) -> float:
        if not self.context_window:
            return 0.0
        return min(1.0, self.context_tokens / self.context_window)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "text": self.text,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "usage": self.usage.to_dict(),
            "context_tokens": self.context_tokens,
            "context_window": self.context_window,
            "context_ratio": round(self.context_ratio, 4),
            "session_id": self.session_id,
        }

def _shrink(value: Any, *, limit: int = LOG_STRING_LIMIT) -> Any:
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + f"\n[truncated {len(value) - limit} chars]"
    if isinstance(value, dict):
        return {str(key): _shrink(item, limit=limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_shrink(item, limit=limit) for item in value[:LOG_LIST_LIMIT]]
        if len(value) > LOG_LIST_LIMIT:
            items.append(f"[truncated {len(value) - LOG_LIST_LIMIT} items]")
        return items
    return value


def _usage_sum(left: Usage, right: Usage) -> Usage:
    return Usage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        cached_tokens=left.cached_tokens + right.cached_tokens,
        cache_write_tokens=left.cache_write_tokens + right.cache_write_tokens,
        cost_usd=(
            None
            if left.cost_usd is None and right.cost_usd is None
            else (left.cost_usd or 0.0) + (right.cost_usd or 0.0)
        ),
    )


def _content_chars(content: list[Any]) -> int:
    total = 0
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            total += IMAGE_CHARS
        elif isinstance(part, dict):
            total += len(str(part.get("text") or "")) + 32
    return total


def _messages_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            # Count image parts by a fixed estimate, not their base64 size.
            rest = {key: value for key, value in message.items() if key != "content"}
            total += _content_chars(content) + len(json.dumps(rest, default=str))
            continue
        try:
            total += len(json.dumps(message, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            total += len(str(message))
    return total


def _tool_content(result: ToolResult) -> str | list[dict[str, Any]]:
    """Tool message content; images travel as OpenAI-style data-URL parts."""

    images = getattr(result, "images", None)
    if not images:
        return result.content
    parts: list[dict[str, Any]] = [{"type": "text", "text": result.content}]
    for media_type, data in images:
        url = f"data:{media_type};base64,{data}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _read_instructions(workspace: Workspace, limit: int = DEFAULT_SYSTEM_CHARS) -> str:
    path: Path = workspace.instructions_path
    if not path.is_file():
        return "(no workspace instructions file)"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(workspace instructions unreadable)"
    return text[:limit] + ("\n[truncated]" if len(text) > limit else "")


def build_system_prompt(
    workspace: Workspace,
    *,
    active_study: str | None = None,
    instructions: str | None = None,
) -> str:
    config = workspace.config
    provider = config.provider
    return SYSTEM_PROMPT.format(
        root=workspace.root.as_posix(),
        validator=config.validator,
        validator_exists=str(workspace.validator_path.is_file()).lower(),
        provider=provider.name,
        base_url=provider.base_url,
        model=config.model,
        study=active_study or "(none selected; use study_list first)",
        instructions=instructions if instructions is not None else _read_instructions(workspace),
    )


class Agent:
    def __init__(
        self,
        workspace: Workspace,
        provider: OpenCodeProvider,
        tools: ToolRegistry,
        *,
        session: SessionLog | None = None,
        model: str | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        active_study: str | None = None,
        approver: Callable[[ApprovalRequest], Awaitable[bool]] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        private: bool = False,
        context_window: int | None = None,
        deadline_seconds: float | None = None,
        hooks: AgentHooks | None = None,
        freeze_tools: bool = False,
        effort: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.provider = provider
        self.tools = tools
        self.session = session if session is not None else tools.ctx.session
        self.model = model or workspace.config.model
        self.max_steps = max(1, int(max_steps))
        # Optional wall-clock budget per turn; the loop stops at whichever of
        # max_steps or the deadline comes first.
        self.deadline_seconds = deadline_seconds
        self.hooks = hooks or AgentHooks()
        # Changing the tool list mid-session invalidates signed Claude
        # thinking; a frozen list is captured on the first model call.
        self.freeze_tools = freeze_tools
        self._frozen_tools: list[dict[str, Any]] | None = None
        # Per-request effort override (per-phase effort); None keeps the
        # provider setting.
        self.effort = effort
        self.active_study = active_study
        self.private = private
        self._approver = approver
        self._on_event = on_event
        self.context_window = context_window or workspace.config.provider.context_window
        self.messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": build_system_prompt(workspace, active_study=active_study),
            }
        ]
        self.turn_index = 0
        self.last_usage = Usage()
        self.usage_total = Usage()
        self.last_context_tokens = 0
        self.last_steps = 0
        self._cancelled = False
        # The tool context is shared: one policy, one session, one approver.
        if self.session is not None:
            self.tools.ctx.session = self.session
        self.tools.ctx.approve = self._approve
        self.tools.ctx.active_study = active_study

    # ---- observability ----
    def _emit(self, kind: str, **data: Any) -> None:
        if self._on_event is not None:
            # A UI callback must never break a turn.
            with contextlib.suppress(Exception):
                self._on_event(AgentEvent(kind=kind, data=data))

    def _log(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        if self.session is None:
            return
        try:
            if self.session.is_closed():
                return
            self.session.append(event_type, _shrink(data or {}))
        except (PandaError, OSError):
            # Provenance failure must not fabricate success, but the turn can
            # still return the model output; the error is surfaced separately.
            self._emit("log_error", event_type=event_type)

    async def _approve(self, request: ApprovalRequest) -> bool:
        self._log("approval.request", request.to_dict())
        self._emit("approval_request", **request.to_dict())
        approved = False
        if self._approver is not None:
            try:
                approved = bool(await self._approver(request))
            except Exception:  # noqa: BLE001
                approved = False
        self._log(
            "approval.decision",
            {"tool": request.tool, "action": request.action, "approved": approved},
        )
        self._emit(
            "approval_decision",
            tool=request.tool,
            action=request.action,
            approved=approved,
        )
        return approved

    @property
    def context_ratio(self) -> float:
        if not self.context_window:
            return 0.0
        return min(1.0, self.last_context_tokens / self.context_window)
    # ---- context ----
    def _context_budget_chars(self) -> int:
        # Conservative chars-per-token (4) with 10% headroom for tool payloads.
        return max(4_000, int(self.context_window * 4 * 0.9))

    def estimate_context_tokens(self) -> int:
        return max(0, _messages_chars(self.messages) // 4)

    def _instruction_index(self) -> int | None:
        """Index of the first user message: the task instruction, never trimmed."""

        for index, message in enumerate(self.messages):
            if message.get("role") == "user":
                return index
        return None

    def _elide_tool_results(self, budget: int) -> bool:
        """Replace old tool results with a stub, oldest first, until under budget."""

        edited = False
        cutoff = len(self.messages) - TRIM_KEEP_RECENT
        for index in range(1, max(1, cutoff)):
            if _messages_chars(self.messages) <= budget:
                break
            message = self.messages[index]
            content = message.get("content")
            if message.get("role") != "tool":
                continue
            if isinstance(content, list):
                size = _content_chars(content)
            elif isinstance(content, str) and not content.startswith(ELIDED_MARKER):
                size = len(content)
            else:
                continue
            if size < ELIDE_MIN_CHARS:
                continue
            self.messages[index] = {
                **message,
                "content": (
                    f"{ELIDED_MARKER}: {size} chars to save context; "
                    "re-run the tool if it is still needed]"
                ),
            }
            edited = True
        return edited

    def _drop_thinking(self) -> None:
        """Remove replayed Claude thinking blocks from the whole history.

        A signed thinking block is valid only while everything before it is
        unchanged; after any non-append edit, replaying it is a 400. Dropping
        every block is always accepted.
        """

        for message in self.messages:
            meta = message.get("provider_meta")
            if isinstance(meta, dict) and "anthropic_content" in meta:
                rest = {key: value for key, value in meta.items() if key != "anthropic_content"}
                if rest:
                    message["provider_meta"] = rest
                else:
                    message.pop("provider_meta", None)

    def _tool_specs(self) -> list[dict[str, Any]]:
        if not self.freeze_tools:
            return self.tools.to_openai()
        if self._frozen_tools is None:
            self._frozen_tools = self.tools.to_openai()
        return self._frozen_tools

    def _append_hook_message(self, hook: str, text: str | None) -> bool:
        """Append hook text as a new user message at the end of the history."""

        if not text or not text.strip():
            return False
        self.messages.append({"role": "user", "content": text})
        if self.private:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            self._log("hook.message", {"hook": hook, "sha256": digest, "chars": len(text)})
        else:
            self._log("hook.message", {"hook": hook, "content": text, "chars": len(text)})
        self._emit("hook_message", hook=hook, chars=len(text))
        return True

    def _trim(self) -> None:
        budget = self._context_budget_chars()
        edited = False
        if _messages_chars(self.messages) > budget:
            edited = self._elide_tool_results(budget)
        pin = self._instruction_index()
        start = pin + 1 if pin is not None else 1
        while len(self.messages) > start + 1 and _messages_chars(self.messages) > budget:
            head = self.messages.pop(start)
            edited = True
            if head.get("role") == "assistant" and head.get("tool_calls"):
                # Keep tool results paired with their assistant message.
                while len(self.messages) > start and self.messages[start].get("role") == "tool":
                    self.messages.pop(start)
            while len(self.messages) > start and self.messages[start].get("role") == "tool":
                self.messages.pop(start)
        if edited:
            self._drop_thinking()
        self.last_context_tokens = self.estimate_context_tokens()

    # ---- turn ----
    async def run_turn(self, user_message: str) -> TurnResult:
        text = user_message.strip()
        if not text:
            raise ValueError("user message is empty")
        self.turn_index += 1
        turn = self.turn_index
        self._log(
            "turn.start",
            {
                "turn": turn,
                "model": self.model,
                "max_steps": self.max_steps,
                "deadline_seconds": self.deadline_seconds,
                "active_study": self.active_study,
            },
        )
        if self.private:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            self._log("message.user", {"sha256": digest, "chars": len(text), "private": True})
        else:
            self._log("message.user", {"content": text, "chars": len(text)})
        self._emit("turn_start", turn=turn, model=self.model)
        self.messages.append({"role": "user", "content": text})

        steps = 0
        tool_calls = 0
        texts: list[str] = []
        usage = Usage()
        status = "completed"
        deadline = (
            time.monotonic() + self.deadline_seconds
            if self.deadline_seconds is not None
            else None
        )
        reopens = 0
        try:
            while steps < self.max_steps:
                if deadline is not None and time.monotonic() >= deadline:
                    status = "deadline"
                    break
                steps += 1
                if self.hooks.before_model is not None:
                    self._append_hook_message(
                        "before_model", await self.hooks.before_model(self)
                    )
                self._trim()
                step_parts: list[str] = []
                calls: list[Any] = []
                usage_box: list[Usage] = [Usage()]
                meta_box: list[dict[str, Any] | None] = [None]
                if not await self._stream_step(
                    step_parts, usage_box, calls, meta_box, deadline
                ):
                    # The step ran out of time: its partial output is discarded
                    # so the history never holds an incomplete turn.
                    status = "deadline"
                    break
                step_usage = usage_box[0]
                step_text = "".join(step_parts)
                if step_text:
                    texts.append(step_text)
                assistant: dict[str, Any] = {"role": "assistant", "content": step_text or None}
                if meta_box[0]:
                    assistant["provider_meta"] = meta_box[0]
                if calls:
                    assistant["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                            **(
                                {"provider_meta": call.metadata}
                                if getattr(call, "metadata", None)
                                else {}
                            ),
                        }
                        for call in calls
                    ]
                self.messages.append(assistant)
                self._log(
                    "message.assistant",
                    {
                        "content": step_text,
                        "chars": len(step_text),
                        "tool_calls": [call.name for call in calls],
                    },
                )
                if step_usage.total_tokens or step_usage.prompt_tokens:
                    usage = _usage_sum(usage, step_usage)
                    self.last_usage = step_usage
                    self.last_context_tokens = step_usage.prompt_tokens
                else:
                    self.last_context_tokens = self.estimate_context_tokens()
                self.usage_total = _usage_sum(self.usage_total, step_usage)
                if not calls:
                    if (
                        self.hooks.before_finish is not None
                        and reopens < self.hooks.max_finish_reopens
                        and steps < self.max_steps
                        and self._append_hook_message(
                            "before_finish", await self.hooks.before_finish(self, step_text)
                        )
                    ):
                        reopens += 1
                        continue
                    break
                notes: list[str] = []
                for call in calls:
                    tool_calls += 1
                    self._log(
                        "tool.call",
                        {"id": call.id, "name": call.name, "arguments": call.arguments},
                    )
                    self._emit(
                        "tool_call",
                        id=call.id,
                        name=call.name,
                        arguments=call.arguments,
                    )
                    result = await self._execute_tool(call)
                    self._log(
                        "tool.result",
                        {
                            "id": call.id,
                            "name": call.name,
                            "ok": result.ok,
                            "blocked": getattr(result, "blocked", False),
                            "content": result.content,
                        },
                    )
                    self._emit(
                        "tool_result",
                        id=call.id,
                        name=call.name,
                        ok=result.ok,
                        blocked=getattr(result, "blocked", False),
                        content=result.content,
                    )
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": _tool_content(result),
                        }
                    )
                    if self.hooks.after_tool is not None:
                        note = await self.hooks.after_tool(self, call, result)
                        if note and note.strip():
                            notes.append(note)
                # One message after all results keeps tool results contiguous.
                self._append_hook_message("after_tool", "\n\n".join(notes))
                if steps >= self.max_steps:
                    status = "max_steps"
        except asyncio.CancelledError:
            self._log(
                "turn.end",
                {"turn": turn, "status": "cancelled", "steps": steps, "tool_calls": tool_calls},
            )
            self._emit("turn_end", status="cancelled", steps=steps)
            raise
        except PandaError as exc:
            self._log("error", {"code": exc.code, "message": exc.message})
            self._emit("error", code=exc.code, message=exc.message)
            raise
        self._log(
            "turn.end",
            {
                "turn": turn,
                "status": status,
                "steps": steps,
                "tool_calls": tool_calls,
                "usage": usage.to_dict(),
                "text_chars": sum(len(part) for part in texts),
            },
        )
        self.last_steps = steps
        self._emit("turn_end", status=status, steps=steps)
        return TurnResult(
            status=status,
            text="\n\n".join(texts).strip(),
            steps=steps,
            tool_calls=tool_calls,
            usage=usage,
            context_tokens=self.last_context_tokens,
            context_window=self.context_window,
            session_id=self.session.session_id if self.session else None,
        )

    async def _stream_step(
        self,
        step_parts: list[str],
        usage_box: list[Usage],
        calls: list[Any],
        meta_box: list[dict[str, Any] | None],
        deadline: float | None,
    ) -> bool:
        """Run one provider call; False when the turn deadline cut it short."""

        kwargs: dict[str, Any] = {"model": self.model, "session_id": self.session_id}
        if self.effort is not None:
            kwargs["effort"] = self.effort
        tools = self._tool_specs()
        if deadline is None:
            async for event in self.provider.stream(self.messages, tools, **kwargs):
                self._handle_provider_event(event, step_parts, usage_box, calls, meta_box)
            return True
        kwargs["deadline"] = deadline
        remaining = max(0.0, deadline - time.monotonic())
        try:
            async with asyncio.timeout(remaining) as scope:
                async for event in self.provider.stream(self.messages, tools, **kwargs):
                    self._handle_provider_event(
                        event, step_parts, usage_box, calls, meta_box
                    )
        except TimeoutError:
            if scope.expired():
                return False
            raise
        return True

    def _handle_provider_event(
        self,
        event: ProviderEvent,
        step_parts: list[str],
        usage_box: list[Usage],
        calls: list[Any],
        meta_box: list[dict[str, Any] | None] | None = None,
    ) -> None:
        if isinstance(event, TextDelta):
            step_parts.append(event.text)
            self._emit("text_delta", text=event.text)
        elif isinstance(event, UsageEvent):
            usage_box[0] = event.usage
            self._emit("usage", **event.usage.to_dict())
        elif isinstance(event, ToolCallDelta):
            self._emit(
                "tool_delta",
                index=event.index,
                name=event.name or "",
                arguments=event.arguments,
            )
        elif isinstance(event, DoneEvent):
            calls.extend(event.tool_calls)
            if meta_box is not None:
                meta_box[0] = getattr(event, "metadata", None)

    async def _execute_tool(self, call: Any) -> ToolResult:
        try:
            arguments = call.parsed_arguments()
        except ProviderError as exc:
            return ToolResult(
                ok=False,
                content=json.dumps({"ok": False, "error": exc.message}, ensure_ascii=False),
            )
        return await self.tools.call(call.name, arguments)

    # ---- conversation control ----
    def set_model(self, model: str) -> None:
        self.model = model
        self._drop_thinking()
        self.messages[0] = {
            "role": "system",
            "content": build_system_prompt(self.workspace, active_study=self.active_study),
        }

    def set_active_study(self, study_id: str | None) -> None:
        self.active_study = study_id
        self.tools.ctx.active_study = study_id
        self._drop_thinking()
        self.messages[0] = {
            "role": "system",
            "content": build_system_prompt(self.workspace, active_study=study_id),
        }

    def reset_conversation(self) -> None:
        self.messages = [self.messages[0]]
        self.last_context_tokens = 0
        self.last_usage = Usage()

    @property
    def session_id(self) -> str | None:
        return self.session.session_id if self.session else None


__all__ = [
    "DEFAULT_MAX_STEPS",
    "Agent",
    "AgentEvent",
    "AgentHooks",
    "SYSTEM_PROMPT",
    "TurnResult",
    "build_system_prompt",
]