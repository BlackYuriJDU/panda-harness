"""Line REPL fallback for terminals without the full-screen cockpit.

The REPL uses the same runtime, tools, policy, and ledger as the cockpit. It
exists so `--plain`, CI, and minimal terminals never lose access to the agent.
"""

from __future__ import annotations

import asyncio

from panda_cli import render
from panda_cli.runtime import ChatRuntime
from panda_core.agent import DEFAULT_MAX_STEPS, AgentEvent
from panda_core.errors import PandaError
from panda_core.registry import find_study, load_registry
from panda_core.session import SessionLog
from panda_core.tools import ApprovalRequest
from panda_core.workspace import Workspace

HELP = """commands
  /help                 this help
  /model [id]           show or switch the model
  /models               list models with protocol/support badges
  /study list           list registered studies
  /study use <id>       set the active study
  /status               provider, model, session, usage
  /validate             run the canonical validator
  /policy               show policy rule ids and effects
  /session              show recent session events
  /clear                reset the conversation (ledger stays)
  /quit                 close the session and exit
  !<argv>               run a command through policy and approval
"""


def _print_approval(request: ApprovalRequest) -> None:
    render.console().print(
        f"[approval] {request.tool}: {request.action} {request.resource}\n"
        f"  rule: {request.rule_id or '-'}  risk: {request.risk}\n"
        f"  reason: {request.reason}"
    )
    if request.preview:
        label = "diff" if request.preview_kind == "diff" else "command"
        render.console().print(f"  {label}:\n{request.preview}")


async def _ask_approval(request: ApprovalRequest) -> bool:
    _print_approval(request)
    answer = await asyncio.to_thread(input, "approve? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}
def _banner(runtime: ChatRuntime) -> None:
    status = runtime.status()
    key = "set" if status["api_key_present"] else "not set"
    render.console().print(
        f"panda · {status['provider']} · {status['model']} · key {key} · "
        f"{len(runtime.tools.names())} tools"
    )
    render.console().print(f"session {status['session_id']} · /help for commands", style="dim")


def _on_event(event: AgentEvent) -> None:
    import sys

    if event.kind == "text_delta":
        sys.stdout.write(event.data.get("text", ""))
        sys.stdout.flush()
    elif event.kind == "tool_call":
        sys.stderr.write(f"\n[tool] {event.data.get('name')}\n")
        sys.stderr.flush()
    elif event.kind == "tool_result":
        state = "blocked" if event.data.get("blocked") else "ok"
        sys.stderr.write(f"[tool] {event.data.get('name')}: {state}\n")
        sys.stderr.flush()
    elif event.kind == "error":
        sys.stderr.write(f"\n[error] {event.data.get('message')}\n")
        sys.stderr.flush()
    elif event.kind == "turn_end":
        sys.stdout.write("\n")
        sys.stdout.flush()


async def _run_shell_line(runtime: ChatRuntime, text: str) -> None:
    from panda_core.process import split_command

    argv = split_command(text)
    if not argv:
        render.console().print("empty command", style="yellow")
        return
    result = await runtime.tools.call("run_shell", {"argv": argv})
    render.console().print(result.content)


async def _command(runtime: ChatRuntime, line: str) -> bool:
    parts = line.split()
    command = parts[0].lower()
    argument = " ".join(parts[1:]).strip()
    if command in {"/quit", "/exit"}:
        return True
    if command == "/help":
        render.console().print(HELP)
        return False
    if command == "/model":
        if not argument:
            render.console().print(runtime.model)
            return False
        try:
            runtime.set_model(argument)
        except PandaError as exc:
            render.print_error(exc)
            return False
        render.console().print(f"model: {runtime.model}")
        return False
    if command == "/models":
        models = await runtime.models()
        rows = [
            {
                "id": model.qualified(runtime.workspace.config.provider.name),
                "protocol": model.protocol,
                "supported": model.supported,
                "free": model.free,
            }
            for model in models
        ]
        render.models_table(
            rows,
            source=runtime.models_source,
            captured_at=runtime.snapshot_captured_at,
            error=runtime.models_error,
        )
        return False
    if command == "/study":
        if argument.startswith("use "):
            study_id = argument[4:].strip()
            try:
                find_study(runtime.workspace.root, study_id)
            except PandaError as exc:
                render.print_error(exc)
                return False
            runtime.set_active_study(study_id)
            render.console().print(f"active study: {study_id}")
            return False
        try:
            studies = load_registry(runtime.workspace.root)
        except PandaError as exc:
            render.print_error(exc)
            return False
        render.study_table(studies)
        return False
    if command == "/status":
        render.print_json(runtime.status())
        return False
    if command == "/validate":
        result = await runtime.validate()
        summary = result.data.get("summary", {})
        render.console().print(
            f"validation {'ok' if result.ok else 'failed'} · "
            f"{summary.get('passed', 0)} passed · {summary.get('failed', 0)} failed · "
            f"{summary.get('skipped', 0)} skipped"
        )
        for check in result.data.get("checks", []):
            if not check.get("ok") and not check.get("skipped"):
                render.console().print(f"  failed: {check.get('name')}")
        return False
    if command == "/policy":
        for row in runtime.policy_rows():
            render.console().print(
                f"{row['id']:<26} {row['effect']:<10} {row['risk']:<9} {row['actions']}"
            )
        return False
    if command == "/session":
        for event in runtime.session_rows()[-15:]:
            render.console().print(f"{event['seq']:>4}  {event['type']:<20} {event['ts']}")
        return False
    if command == "/clear":
        runtime.reset()
        render.console().print("conversation cleared")
        return False
    render.console().print("unknown command; try /help", style="yellow")
    return False


def run_plain_chat(
    workspace: Workspace,
    session: SessionLog,
    *,
    model: str | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    private: bool = False,
    auto: bool = False,
) -> None:
    async def _loop() -> None:
        runtime = ChatRuntime(
            workspace,
            model=model,
            max_steps=max_steps,
            private=private,
            auto=auto,
            session=session,
            approver=None if auto else _ask_approval,
            on_event=_on_event,
        )
        interrupted = False
        try:
            _banner(runtime)
            while True:
                try:
                    line = await asyncio.to_thread(input, "panda> ")
                except EOFError:
                    break
                text = line.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    if await _command(runtime, text):
                        break
                    continue
                if text.startswith("!"):
                    await _run_shell_line(runtime, text[1:].strip())
                    continue
                try:
                    await runtime.ask(text)
                except KeyboardInterrupt:
                    interrupted = True
                    break
                except PandaError as exc:
                    render.print_error(exc)
        finally:
            await runtime.provider.aclose()
            runtime.close("interrupted" if interrupted else "completed")

    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        raise SystemExit(130) from None


__all__ = ["HELP", "run_plain_chat"]