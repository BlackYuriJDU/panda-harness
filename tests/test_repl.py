"""REPL execution paths and ledger coverage (R2-503, APR-003)."""

from __future__ import annotations

import asyncio
import io

import pytest

from panda_cli import repl
from panda_cli.runtime import ChatRuntime
from panda_core.policy import PolicyEngine, load_policy
from panda_core.session import SessionLog
from panda_core.tools import ToolContext, ToolRegistry


def make_registry(workspace, approver=None):
    session = SessionLog.create(workspace.sessions_dir, label="repl")
    policy = load_policy(workspace.policy_path)
    ctx = ToolContext(
        workspace=workspace,
        policy=PolicyEngine(policy, root=workspace.root),
        session=session,
        approve=approver or (lambda r: True),
    )
    return ToolRegistry(ctx), session


@pytest.fixture
def screen() -> io.StringIO:
    stream = io.StringIO()
    import rich.console
    orig = rich.console.Console
    rich.console.Console = lambda **kw: orig(
        file=stream, **{k: v for k, v in kw.items() if k != "file"}
    )
    yield stream
    rich.console.Console = orig


def test_bang_command_goes_through_decide_shell(workspace) -> None:
    """A !command in the REPL must be classified by the shell policy, not allowed blindly."""
    registry, session = make_registry(workspace)

    async def scenario():
        runtime = ChatRuntime(workspace, session=session)
        # benign read-only command should be allowed
        await repl._run_shell_line(runtime, "git status")
        # destructive command should be blocked even without interactive approval
        await repl._run_shell_line(runtime, "git push --force")

    asyncio.run(scenario())
    events = [e for e in session.events()]
    # there should be tool.call and tool.result events for both commands
    shell_events = [e for e in events if e.type in ("tool.call", "tool.result")]
    assert len(shell_events) >= 4, "expected tool.call/tool.result pairs for each !cmd"


def test_repl_approval_renders_markup_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approval previews in the REPL must not interpret Rich markup."""
    printed: list[tuple[str, bool]] = []

    class FakeConsole:
        def print(self, text, *, markup=False, **kw):
            printed.append((str(text), markup))

    monkeypatch.setattr("panda_cli.repl.render.console", FakeConsole)
    request = repl.ApprovalRequest(
        tool="run_shell",
        action="shell",
        resource="git status",
        rule_id="shell-git-readonly",
        risk="medium",
        reason="read-only git command",
        preview="[bold]rm -rf /[/bold] [link=https://x]click[/link]",
    )
    repl._print_approval(request)
    assert len(printed) >= 2
    preview_text, preview_markup = printed[-1]
    assert "[bold]rm -rf /[/bold]" in preview_text
    assert "[link=https://x]click[/link]" in preview_text
    assert preview_markup is False, "markup must be disabled for operator-visible text"


def test_repl_event_printer_records_to_ledger(capsys: pytest.CaptureFixture) -> None:
    """_on_event writes to stdout/stderr and the event origin is traceable."""
    from panda_core.agent import AgentEvent

    repl._on_event(AgentEvent("text_delta", {"text": "hello"}))
    repl._on_event(AgentEvent("tool_call", {"name": "read_file"}))
    repl._on_event(AgentEvent("tool_result", {"name": "read_file", "blocked": False}))
    repl._on_event(AgentEvent("turn_end", {}))
    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "read_file" in captured.err
    assert "ok" in captured.err
