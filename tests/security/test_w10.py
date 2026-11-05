"""Wave 10: sanitize at the sink; the approval card shows exactly what runs.

R2-221, R2-222, R2-223, R2-306, R2-310, R2-229, R2-117, R2-125, N-03.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from tests.security.test_w4 import ARMED, approval, run_modal, status_text

ESC = "\x1b"
OSC8 = f"{ESC}]8;;https://evil.example{ESC}\\click{ESC}]8;;{ESC}\\"
OSC52 = f"{ESC}]52;c;U1lOVEg={ESC}\\"
HOSTILE = f"plain {OSC8} {OSC52} {ESC}[2J \x9b31m [link=https://evil.example]x[/link] [/]"


@pytest.mark.parametrize(
    "char", [" ", " ", "؜", "᠎", "­", "\U000e0041", "️", "ㅤ"]
)
def test_sanitizer_makes_invisible_or_line_breaking_marks_visible(char: str) -> None:
    from panda_core.display import sanitize_display

    assert char not in sanitize_display(f"a{char}b")


def test_the_cli_sink_keeps_colour_and_drops_everything_else() -> None:
    from panda_core.display import SafeWriter

    buffer = io.StringIO()
    SafeWriter(buffer).write(f"{ESC}[1;32mok{ESC}[0m {HOSTILE}")
    out = buffer.getvalue()
    assert f"{ESC}[1;32m" in out
    for bad in (f"{ESC}]", f"{ESC}[2J", "\x9b"):
        assert bad not in out


def test_cli_console_does_not_interpret_corpus_text_as_markup(capsys) -> None:
    from panda_cli import render

    render.console().print("[link=https://evil.example]study[/link] [/]")
    out = capsys.readouterr().out
    assert "[link=https://evil.example]study[/link]" in out
    assert f"{ESC}]8" not in out


def test_print_error_is_sanitized(capsys) -> None:
    from panda_cli import render

    render.print_error(RuntimeError(HOSTILE))
    err = capsys.readouterr().err
    assert f"{ESC}]" not in err and "\x9b" not in err


def test_json_output_escapes_bidi_and_c1(capsys) -> None:
    from panda_cli import render

    render.print_json({"name": "a‮b\x85c"})
    out = capsys.readouterr().out
    assert "‮" not in out and "\x85" not in out
    assert json.loads(out)["name"] == "a‮b\x85c"


def test_safe_text_sanitizes_every_append() -> None:
    from panda_core.display import SafeText

    text = SafeText(f"x{ESC}[2J")
    text.append(OSC52 + " ")
    assert ESC not in text.plain and " " not in text.plain


def test_cockpit_markdown_has_no_clickable_model_links() -> None:
    from rich.console import Console

    from panda_cli.cockpit import _markdown

    console = Console(file=io.StringIO(), force_terminal=True, color_system="truecolor")
    console.print(_markdown(f"see [docs](https://evil.example) {OSC52}"))
    out = console.file.getvalue()
    assert f"{ESC}]8;" not in out and "52;c" not in out


# --------------------------------------------------------------------------- consent card
def test_run_shell_approval_shows_argv_boundaries_and_working_directory(workspace) -> None:
    from panda_cli.runtime import ChatRuntime

    seen = []

    async def person(request) -> bool:
        seen.append(request)
        return False

    (workspace.root / "corpus" / "evil").mkdir(parents=True, exist_ok=True)
    runtime = ChatRuntime(workspace, approver=person, label="w10")
    try:
        for argv in (["make", "a b"], ["make", "a", "b"]):
            asyncio.run(runtime.tools.call("run_shell", {"argv": argv, "cwd": "corpus/evil"}))
    finally:
        runtime.close()
    first, second = seen
    assert first.preview != second.preview  # R2-117: different argv, different preview
    assert first.argv == ("make", "a b") and first.cwd == "corpus/evil"  # R2-229
    assert first.payload_sha256 and first.payload_sha256 != second.payload_sha256
    decisions = [e.data for e in runtime.session.events() if e.type == "approval.request"]
    assert decisions[0]["cwd"] == "corpus/evil" and decisions[0]["argv"] == ["make", "a b"]


def test_the_card_labels_a_workspace_reason(workspace) -> None:
    async def script(pilot, screen, decisions):
        summary = screen.query_one("#approval-summary").render()
        text = getattr(summary, "plain", str(summary))
        assert "text from the workspace policy" in text
        assert "corpus/evil" in text and "4 tokens" in text

    request = approval(
        "make a b c", argv=("make", "a", "b", "c"), cwd="corpus/evil",
        rule_id="lab-sync", rule_source="workspace", reason="pre-approved",
    )
    run_modal(workspace, request, script)


def test_a_truncated_preview_cannot_be_approved(workspace) -> None:
    async def script(pilot, screen, decisions):
        await pilot.pause(ARMED)
        screen.query_one("#approval-preview-scroll").scroll_end(animate=False)
        await pilot.pause(0.2)
        await pilot.press("y")
        await pilot.press("y")
        await pilot.pause()
        assert decisions == []
        assert "cannot be approved" in status_text(screen)

    run_modal(workspace, approval("+x", preview_kind="diff", preview_complete=False), script)


def test_a_large_diff_is_shown_whole_up_to_the_limit(workspace) -> None:
    from panda_core.tools import PREVIEW_LIMIT, _diff_preview

    after = "\n".join(f"line {n}" for n in range(3000))
    preview = _diff_preview("corpus/x.md", "", after)
    assert len(preview) < PREVIEW_LIMIT and "[truncated" not in preview
    assert "line 2999" in preview
