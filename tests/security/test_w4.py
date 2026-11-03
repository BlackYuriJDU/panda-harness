"""Wave 4: the approval surface, the last human gate (APR-001/002/003/004, UI-001/002).

What the operator reads must be what will run, and nothing the model writes may
rewrite the screen. Synthetic payloads only.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from rich.console import Console

from panda_cli import render, repl
from panda_core.display import sanitize_display
from panda_core.tools import ApprovalRequest

ESC = "\x1b"
OSC_CLIPBOARD = f"{ESC}]52;c;U1lOVEhfU0VDUkVU\x07"
CLEAR_SCREEN = f"{ESC}[2J"
SET_TITLE = f"{ESC}]0;pwned\x07"


def approval(preview: str = "git status", resource: str = "git status", **extra) -> ApprovalRequest:
    values = {
        "tool": "run_shell",
        "action": "shell",
        "resource": resource,
        "rule_id": "shell-default-ask",
        "risk": "medium",
        "reason": "shell is never implicit",
        "preview": preview,
        "preview_kind": "command",
    }
    values.update(extra)
    return ApprovalRequest(**values)


@pytest.fixture()
def screen(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Capture what the REPL prints, with styling on so a hidden style would show."""

    buffer = io.StringIO()

    def console() -> Console:
        return Console(
            file=buffer, force_terminal=True, color_system="truecolor", width=200,
            highlight=False, emoji=False,
        )

    monkeypatch.setattr(render, "console", console)
    return buffer


# --------------------------------------------------------------------------- sanitizer
@pytest.mark.parametrize(
    "dirty",
    [OSC_CLIPBOARD, CLEAR_SCREEN, SET_TITLE, f"{ESC}[31mred{ESC}[0m", f"{ESC}P1$r{ESC}\\",
     "a\rb", "bell\x07", "nul\x00byte", "c1\x9bcontrol", "del\x7f"],
)
def test_sanitizer_removes_terminal_control_sequences(dirty: str) -> None:
    clean = sanitize_display(dirty)
    assert ESC not in clean
    assert not any(ord(ch) < 32 and ch not in "\n\t" for ch in clean)
    assert not any(0x7F <= ord(ch) <= 0x9F for ch in clean)


def test_sanitizer_drops_the_payload_of_an_osc_sequence() -> None:
    assert "U1lOVEhfU0VDUkVU" not in sanitize_display("x" + OSC_CLIPBOARD + "y")
    assert sanitize_display("x" + OSC_CLIPBOARD + "y") == "xy"


def test_sanitizer_keeps_ordinary_text_newlines_tabs_and_unicode() -> None:
    text = "café 日本 → ok\n\tindented [brackets] {braces}"
    assert sanitize_display(text) == text


def test_sanitizer_makes_bidi_and_zero_width_characters_visible() -> None:
    shown = sanitize_display("rm ‮txt.sh‬​")
    assert "‮" not in shown and "​" not in shown
    assert "U+202E" in shown and "U+200B" in shown


# --------------------------------------------------------------------------- APR-003
def test_repl_approval_shows_markup_literally(screen: io.StringIO) -> None:
    request = approval(
        preview=(
            "[conceal]rm -rf corpus[/conceal] [link=https://evil.example]ok[/link] "
            "[/x] [b]x[/b]"
        ),
        resource="[bold]git[/bold] push",
    )
    repl._print_approval(request)
    out = screen.getvalue()
    assert "[conceal]rm -rf corpus[/conceal]" in out
    assert "[link=https://evil.example]ok[/link]" in out
    assert "[/x]" in out and "[b]x[/b]" in out
    assert "[bold]git[/bold] push" in out
    assert f"{ESC}]8;" not in out, "a hyperlink was created from model text"
    assert f"{ESC}[8m" not in out, "the text was concealed"
    assert "[approval]" in out


def test_repl_approval_does_not_raise_on_a_stray_closing_tag(screen: io.StringIO) -> None:
    repl._print_approval(approval(preview="[/nothing-open]"))
    assert "[/nothing-open]" in screen.getvalue()


def test_repl_approval_strips_terminal_control_sequences(screen: io.StringIO) -> None:
    repl._print_approval(
        approval(preview=f"echo hi{OSC_CLIPBOARD}{CLEAR_SCREEN}{SET_TITLE}", resource="echo\x07")
    )
    out = screen.getvalue()
    assert "U1lOVEhfU0VDUkVU" not in out and "pwned" not in out
    for sequence in (f"{ESC}]52", f"{ESC}[2J", f"{ESC}]0;"):
        assert sequence not in out


def test_repl_prints_tool_output_without_interpreting_markup(
    screen: io.StringIO, workspace
) -> None:
    from panda_cli.runtime import ChatRuntime

    runtime = ChatRuntime(workspace)
    (workspace.root / "corpus" / "m.md").write_text("[link=https://evil.example]x[/link]\n")
    asyncio.run(repl._run_shell_line(runtime, "cat corpus/m.md"))  # blocked, content is JSON
    assert f"{ESC}]8;" not in screen.getvalue()


# --------------------------------------------------------------------------- UI-002
def test_repl_event_printer_strips_control_sequences(capsys: pytest.CaptureFixture) -> None:
    from panda_core.agent import AgentEvent

    repl._on_event(AgentEvent("text_delta", {"text": f"hello{OSC_CLIPBOARD}{CLEAR_SCREEN}"}))
    repl._on_event(AgentEvent("tool_call", {"name": f"read_file{SET_TITLE}"}))
    repl._on_event(AgentEvent("tool_result", {"name": f"x{CLEAR_SCREEN}", "blocked": False}))
    repl._on_event(AgentEvent("error", {"message": f"boom{OSC_CLIPBOARD}"}))
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert ESC not in stream and "\x07" not in stream
    assert "hello" in captured.out


def test_panda_run_strips_control_sequences_from_the_model(
    workspace, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture
) -> None:
    from typer.testing import CliRunner

    from panda_cli.main import app
    from panda_core.provider import DoneEvent, OpenCodeProvider, TextDelta, ToolCall

    async def fake(self, messages, tools=None, model=None, session_id=None):
        yield TextDelta(f"answer{OSC_CLIPBOARD}{CLEAR_SCREEN}")
        call = ToolCall(id="c1", name=f"no_such_tool{SET_TITLE}", arguments="{}")
        yield DoneEvent(finish_reason="tool_calls", tool_calls=(call,))

    monkeypatch.setattr(OpenCodeProvider, "stream", fake)
    monkeypatch.setenv("OPENCODE_API_KEY", "SYNTH_SECRET_KEY")
    args = ["run", "--root", str(workspace.root), "--max-steps", "1", "go"]
    result = CliRunner().invoke(app, args)
    combined = result.output + capfd.readouterr().out
    assert ESC not in combined and "\x07" not in combined
    assert "answer" in combined


def test_panda_run_json_output_is_data_not_terminal_text(workspace, monkeypatch) -> None:
    from typer.testing import CliRunner

    from panda_cli.main import app
    from panda_core.provider import DoneEvent, OpenCodeProvider, TextDelta

    async def fake(self, messages, tools=None, model=None, session_id=None):
        yield TextDelta(f"a{CLEAR_SCREEN}")
        yield DoneEvent(finish_reason="stop")

    monkeypatch.setattr(OpenCodeProvider, "stream", fake)
    monkeypatch.setenv("OPENCODE_API_KEY", "SYNTH_SECRET_KEY")
    result = CliRunner().invoke(app, ["run", "--root", str(workspace.root), "--json", "go"])
    assert ESC not in result.stdout  # json.dumps escapes it
    assert json.loads(result.stdout)["ok"] is True


# --------------------------------------------------------------------------- APR-004
def runtime_with(workspace, *, auto: bool):
    from panda_cli.runtime import ChatRuntime

    return ChatRuntime(workspace, auto=auto)


@pytest.mark.parametrize(
    "rule_id", ["shell-push", "shell-git-push", "git-push"]
)
def test_auto_never_approves_a_push(workspace, rule_id: str) -> None:
    runtime = runtime_with(workspace, auto=True)
    assert asyncio.run(runtime._approve(approval("git push origin main", rule_id=rule_id))) is False


def test_the_push_policy_is_never_auto_end_to_end(workspace) -> None:
    runtime = runtime_with(workspace, auto=True)
    decision = runtime.policy.decide("shell", "git push origin main")
    assert decision.effect == "ask"
    request = approval("git push origin main", rule_id=decision.rule_id)
    assert asyncio.run(runtime._approve(request)) is False


def test_auto_still_approves_an_ordinary_ask(workspace) -> None:
    runtime = runtime_with(workspace, auto=True)
    request = approval("pytest -q", rule_id="shell-default-ask")
    assert asyncio.run(runtime._approve(request)) is True


def test_a_human_approver_can_still_approve_a_push(workspace) -> None:
    from panda_cli.runtime import ChatRuntime

    async def human(request) -> bool:
        return True

    runtime = ChatRuntime(workspace, approver=human)
    request = approval("git push origin main", rule_id="shell-push")
    assert asyncio.run(runtime._approve(request)) is True


def test_a_refused_auto_push_is_recorded_in_the_ledger(workspace) -> None:
    runtime = runtime_with(workspace, auto=True)
    request = approval("git push origin main", rule_id="shell-push")
    asyncio.run(runtime.agent._approve(request))
    decisions = [e for e in runtime.session.events() if e.type == "approval.decision"]
    assert decisions and decisions[-1].data["approved"] is False
