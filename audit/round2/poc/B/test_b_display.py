"""Trilha B — display.py e superfícies que chegam ao terminal. Afirma o resultado SEGURO."""

from __future__ import annotations

import asyncio
import io

import pytest
from rich.console import Console

from panda_core.display import sanitize_display

ESC_PAYLOAD = "A\x1b]52;c;U1lOVEg=\x07B\x1b[2JC\x9b31mD"


@pytest.mark.parametrize(
    "char",
    [" ", " ", "؜", "᠎", "­", "\U000e0041", "\U000e007f", "️",
     "ᅟ", "ㅤ"],
)
def test_sanitizer_makes_invisible_or_line_breaking_marks_visible(char: str) -> None:
    out = sanitize_display(f"rm{char}-rf")
    assert char not in out, repr(out)


def test_cockpit_tool_card_and_markdown_do_not_emit_terminal_sequences() -> None:
    from rich.markdown import Markdown
    from textual.app import App
    from textual.geometry import Region
    from textual.widgets import Static

    from panda_cli.cockpit import ToolCard

    class Probe(App[None]):
        def compose(self):  # type: ignore[no-untyped-def]
            yield ToolCard("run_shell", ESC_PAYLOAD)
            yield Static(Markdown("model says " + ESC_PAYLOAD))

    emitted: list[str] = []

    async def scenario() -> None:
        app = Probe()
        async with app.run_test(size=(100, 10)) as pilot:
            await pilot.pause()
            console = Console(file=io.StringIO(), force_terminal=True, color_system="truecolor")
            for widget in app.query(Static):
                for strip in widget.render_lines(Region(0, 0, widget.size.width, widget.size.height)):
                    emitted.append(strip.render(console))

    asyncio.run(scenario())
    data = "".join(emitted)
    assert "\x1b[2J" not in data and "\x1b]52" not in data and "\x9b" not in data


def test_validation_report_does_not_emit_terminal_sequences_from_validator_output() -> None:
    from panda_cli import render
    from panda_core.validator import parse_report

    report_json = (
        '{"ok": false, "checks": [{"name": "claims", "ok": false, "skipped": false, "reason": "",'
        ' "errors": ["claim C1 cites \\u001b]0;pwned\\u0007\\u001b[2J missing E9"],'
        ' "warnings": []}]}'
    )
    buffer = io.StringIO()
    original = render.console
    render.console = lambda: Console(file=buffer, force_terminal=True, width=200)  # type: ignore[assignment]
    try:
        report = parse_report(report_json)
        render.validation_report(report)
    finally:
        render.console = original  # type: ignore[assignment]
    assert "\x1b]0;" not in buffer.getvalue() and "\x1b[2J" not in buffer.getvalue()


def test_json_output_escapes_bidi_and_c1() -> None:
    from contextlib import redirect_stdout

    from panda_cli import render

    out = io.StringIO()
    with redirect_stdout(out):
        render.print_json({"text": "ok ‮ txt.exe \x9b31m"})
    assert "‮" not in out.getvalue() and "\x9b" not in out.getvalue()
