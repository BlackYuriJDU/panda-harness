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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panda_core.errors import PandaError, ProviderError, SessionIntegrityError, UsageError
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
from panda_core.safe_read import read_workspace_file
from panda_core.session import SessionLog
from panda_core.tools import ApprovalRequest, ToolRegistry, ToolResult
from panda_core.treehash import tree_hash
from panda_core.workspace import Workspace

DEFAULT_SYSTEM_CHARS = 24_000
LOG_STRING_LIMIT = 8_000
LOG_LIST_LIMIT = 200

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
7. Never read, print, or persist secrets. Never rewrite git history. Pushing or
   publishing is not yours to decide: it is always a person's approval, and
   `--auto` does not grant it.
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

KEEP_RECENT_MESSAGES = 6
INCOMPLETE_REASONS = frozenset({"length", "max_tokens", "content_filter", "incomplete", "refusal"})


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


def _messages_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        try:
            total += len(json.dumps(message, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            total += len(str(message))
    return total


def _read_instructions(workspace: Workspace, limit: int = DEFAULT_SYSTEM_CHARS) -> str:
    path: Path = workspace.instructions_path
    if not (path.is_file() or path.is_symlink()):
        return "(no workspace instructions file)"
    try:
        # A symlink raises ConfigError (TRUST-005); the content is redacted
        # before it reaches the system prompt.
        text = read_workspace_file(workspace.root, path, redact=True)
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
    ) -> None:
        self.workspace = workspace
        self.provider = provider
        self.tools = tools
        self.session = session if session is not None else tools.ctx.session
        self.model = model or workspace.config.model
        self.max_steps = max(1, int(max_steps))
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
        self._turn_active = False
        self._tree_cache: dict[str, tuple[int, int, str]] = {}
        self._pinned: dict[str, Any] | None = None
        self._summary: dict[str, Any] | None = None
        self._trimmed_total = 0
        self._trimmed_tools: dict[str, int] = {}
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

    def _log(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        best_effort: bool = False,
    ) -> None:
        """Record an event; a ledger that cannot record stops the turn (PROV-001).

        No record, no action: when the write fails the exception propagates before
        the model's next step or any tool can run. `best_effort` is only for the
        error paths that are already unwinding, where a second failure must not
        mask the first.
        """

        if self.session is None:
            return
        critical = event_type == "tool.call" or event_type.startswith("approval.")
        try:
            if self.session.is_closed():
                if critical and not best_effort:
                    raise SessionIntegrityError(
                        f"cannot record {event_type}: the session is closed",
                        hint="start a new session",
                    )
                return
            self.session.append(event_type, _shrink(data or {}))
        except SessionIntegrityError:
            if best_effort:
                self._emit("log_error", event_type=event_type)
                return
            raise
        except (PandaError, OSError) as exc:
            self._emit("log_error", event_type=event_type)
            if best_effort:
                return
            raise SessionIntegrityError(
                f"cannot write the session ledger ({event_type}): {exc}",
                hint="free disk space or run `panda session verify`; no action was taken",
            ) from exc

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

    def _oldest_removable(self) -> int | None:
        """Index of the oldest message that trimming may drop, or None.

        The system prompt, the task of the current turn, the running summary and
        the most recent messages are never dropped (AGT-002).
        """

        for index in range(1, max(1, len(self.messages) - KEEP_RECENT_MESSAGES)):
            message = self.messages[index]
            if message is not self._pinned and message is not self._summary:
                return index
        return None

    def _trim(self) -> None:
        budget = self._context_budget_chars()
        removed: list[dict[str, Any]] = []
        while _messages_chars(self.messages) > budget:
            index = self._oldest_removable()
            if index is None:
                break
            end = index + 1
            if self.messages[index].get("role") in {"assistant", "tool"}:
                # A tool result goes with the assistant message that asked for it.
                while end < len(self.messages) and self.messages[end].get("role") == "tool":
                    end += 1
            removed.extend(self.messages[index:end])
            del self.messages[index:end]
        if removed:
            self._record_trim(removed)
        self.last_context_tokens = self.estimate_context_tokens()

    def _record_trim(self, removed: list[dict[str, Any]]) -> None:
        for message in removed:
            for call in message.get("tool_calls") or []:
                name = call.get("function", {}).get("name", "?")
                self._trimmed_tools[name] = self._trimmed_tools.get(name, 0) + 1
        self._trimmed_total += len(removed)
        tools = ", ".join(f"{n} x{c}" for n, c in sorted(self._trimmed_tools.items()))
        content = f"[contexto resumido: {self._trimmed_total} mensagens]" + (
            f"\nferramentas usadas nas mensagens removidas: {tools}" if tools else ""
        )
        if self._summary is None:
            self._summary = {"role": "user", "content": content}
            self.messages.insert(1, self._summary)
        else:
            self._summary["content"] = content
        self._log(
            "context.trim",
            {
                "removed": len(removed),
                "removed_total": self._trimmed_total,
                "removed_sha256": [
                    hashlib.sha256(
                        json.dumps(message, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    ).hexdigest()
                    for message in removed
                ],
                "chars_after": _messages_chars(self.messages),
                "budget": self._context_budget_chars(),
            },
        )

    # ---- turn ----
    @property
    def busy(self) -> bool:
        return self._turn_active

    def _require_idle(self, what: str) -> None:
        if self._turn_active:
            raise UsageError(
                f"cannot {what} while a turn is running",
                hint="wait for the turn to finish or cancel it first",
            )

    async def run_turn(self, user_message: str) -> TurnResult:
        self._turn_active = True
        try:
            return await self._run_turn(user_message)
        finally:
            self._turn_active = False

    async def _run_turn(self, user_message: str) -> TurnResult:
        text = user_message.strip()
        if not text:
            raise ValueError("user message is empty")
        self.turn_index += 1
        turn = self.turn_index
        steps = 0
        tool_calls = 0
        texts: list[str] = []
        usage = Usage()
        status = "completed"
        finish_reason: str | None = None
        try:
            self._log(
                "turn.start",
                {
                    "turn": turn,
                    "model": self.model,
                    "max_steps": self.max_steps,
                    "active_study": self.active_study,
                },
            )
            if self.private:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                self._log(
                    "message.user", {"sha256": digest, "chars": len(text), "private": True}
                )
            else:
                self._log("message.user", {"content": text, "chars": len(text)})
            self._emit("turn_start", turn=turn, model=self.model)
            task_message = {"role": "user", "content": text}
            self.messages.append(task_message)
            self._pinned = task_message
            while steps < self.max_steps:
                steps += 1
                self._trim()
                step_parts: list[str] = []
                calls: list[Any] = []
                usage_box: list[Usage] = [Usage()]
                finish_box: list[str | None] = [None]
                async for event in self.provider.stream(
                    self.messages,
                    self.tools.to_openai(),
                    model=self.model,
                    session_id=self.session_id,
                ):
                    self._handle_provider_event(event, step_parts, usage_box, calls, finish_box)
                step_usage = usage_box[0]
                dropped = 0
                if finish_box[0] in INCOMPLETE_REASONS:
                    # Cut off by the provider (AGT-003): tool-call arguments may be
                    # truncated, so none of them is run or kept in the history.
                    status, finish_reason = "incomplete", finish_box[0]
                    dropped, calls = len(calls), []
                step_text = "".join(step_parts)
                if step_text:
                    texts.append(step_text)
                assistant: dict[str, Any] = {"role": "assistant", "content": step_text or None}
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
                        # --private keeps the model's prose out too: it often repeats the task.
                        **(
                            {
                                "sha256": hashlib.sha256(step_text.encode("utf-8")).hexdigest(),
                                "private": True,
                            }
                            if self.private
                            else {"content": step_text}
                        ),
                        "chars": len(step_text),
                        "tool_calls": [call.name for call in calls],
                        **({"dropped_tool_calls": dropped} if dropped else {}),
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
                    break
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
                            **{
                                key: result.data[key]
                                for key in ("path", "sha256_before", "sha256_after")
                                if isinstance(getattr(result, "data", None), dict)
                                and key in result.data
                            },
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
                            "content": result.content,
                        }
                    )
                if steps >= self.max_steps:
                    status = "max_steps"
        except asyncio.CancelledError:
            self._close_dangling_tool_calls("cancelled")
            self._end_turn_quietly(turn, "cancelled", steps, tool_calls)
            raise
        except SessionIntegrityError as exc:
            self._close_dangling_tool_calls("ledger_failed")
            self._end_turn_quietly(turn, "ledger_failed", steps, tool_calls)
            self._emit("error", code=exc.code, message=exc.message)
            raise
        except PandaError as exc:
            self._close_dangling_tool_calls("error")
            self._log("error", {"code": exc.code, "message": exc.message}, best_effort=True)
            self._end_turn_quietly(turn, "error", steps, tool_calls)
            self._emit("error", code=exc.code, message=exc.message)
            raise
        except Exception as exc:
            # Whatever else escapes still leaves a closed turn in the ledger (AGT-001).
            self._close_dangling_tool_calls("error")
            self._log(
                "error",
                {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"},
                best_effort=True,
            )
            self._end_turn_quietly(turn, "error", steps, tool_calls)
            raise
        tree = await asyncio.to_thread(tree_hash, self.workspace.root, self._tree_cache)
        self._log(
            "turn.end",
            {
                "turn": turn,
                "status": status,
                "steps": steps,
                "tool_calls": tool_calls,
                "usage": usage.to_dict(),
                "text_chars": sum(len(part) for part in texts),
                "tree_sha256": tree.digest,
                "tree_files": tree.files,
                **({"tree_truncated": True} if tree.truncated else {}),
                **({"finish_reason": finish_reason} if finish_reason else {}),
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

    def _handle_provider_event(
        self,
        event: ProviderEvent,
        step_parts: list[str],
        usage_box: list[Usage],
        calls: list[Any],
        finish_box: list[str | None] | None = None,
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
            if finish_box is not None:
                finish_box[0] = event.finish_reason

    async def _execute_tool(self, call: Any) -> ToolResult:
        try:
            arguments = call.parsed_arguments()
        except ProviderError as exc:
            return ToolResult(
                ok=False,
                content=json.dumps({"ok": False, "error": exc.message}, ensure_ascii=False),
            )
        try:
            return await self.tools.call(call.name, arguments)
        except Exception as exc:  # noqa: BLE001 - a handler bug must not strand the history
            message = f"tool {call.name} failed unexpectedly: {type(exc).__name__}: {exc}"
            return ToolResult(
                ok=False,
                content=json.dumps({"ok": False, "error": message}, ensure_ascii=False),
            )

    def _close_dangling_tool_calls(self, reason: str) -> None:
        """Answer every tool call that never got a result, so the history stays valid."""

        answered: set[str] = set()
        for message in reversed(self.messages):
            if message.get("role") == "tool":
                answered.add(str(message.get("tool_call_id")))
            elif message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    if call["id"] not in answered:
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": json.dumps({"ok": False, "error": reason}),
                            }
                        )
                break

    def _end_turn_quietly(self, turn: int, status: str, steps: int, tool_calls: int) -> None:
        self._log(
            "turn.end",
            {"turn": turn, "status": status, "steps": steps, "tool_calls": tool_calls},
            best_effort=True,
        )
        self._emit("turn_end", status=status, steps=steps)

    # ---- conversation control ----
    def set_model(self, model: str) -> None:
        self._require_idle("change the model")
        self.model = model
        self.messages[0] = {
            "role": "system",
            "content": build_system_prompt(self.workspace, active_study=self.active_study),
        }

    def set_active_study(self, study_id: str | None) -> None:
        self._require_idle("change the active study")
        self.active_study = study_id
        self.tools.ctx.active_study = study_id
        self.messages[0] = {
            "role": "system",
            "content": build_system_prompt(self.workspace, active_study=study_id),
        }

    def reset_conversation(self) -> None:
        self._require_idle("reset the conversation")
        self.messages = [self.messages[0]]
        self._pinned = self._summary = None
        self._trimmed_total = 0
        self._trimmed_tools = {}
        self.last_context_tokens = 0
        self.last_usage = Usage()

    @property
    def session_id(self) -> str | None:
        return self.session.session_id if self.session else None


__all__ = [
    "DEFAULT_MAX_STEPS",
    "Agent",
    "AgentEvent",
    "SYSTEM_PROMPT",
    "TurnResult",
    "build_system_prompt",
]