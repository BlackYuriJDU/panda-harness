"""Trilha A — variantes de UI-002 / APR-003 (texto do corpus que chega ao terminal)."""

from __future__ import annotations

from typer.testing import CliRunner

from panda_cli.main import app

OSC52 = "\x1b]52;c;U1lOVEhfU0VDUkVU\x07"
CLEAR = "\x1b[2J"


# R2-122: `panda study show` imprime o corpo do card (corpus, B1) sem sanitize_display.
def test_study_show_strips_terminal_control_sequences(workspace):
    card = workspace.root / "corpus" / "studies" / "gse160208.md"
    card.write_text(card.read_text(encoding="utf-8") + f"\nhidden {OSC52} {CLEAR} tail\n",
                    encoding="utf-8")
    result = CliRunner().invoke(app, ["study", "show", "gse160208", "--root", str(workspace.root)])
    assert result.exit_code == 0, result.output
    assert "\x1b]52" not in result.output and "\x1b[2J" not in result.output, repr(result.output[-200:])


# R2-122: `panda study list` imprime células do INDEX (corpus) sem sanitize_display.
def test_study_list_strips_terminal_control_sequences(workspace):
    index = workspace.root / "corpus" / "studies" / "INDEX.md"
    index.write_text(index.read_text(encoding="utf-8").replace("Programas?", f"Programas{OSC52}?"),
                     encoding="utf-8")
    result = CliRunner().invoke(app, ["study", "list", "--root", str(workspace.root)])
    assert result.exit_code == 0, result.output
    assert "\x1b]52" not in result.output, repr(result.output)
