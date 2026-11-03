"""Make text from the model safe to show on a terminal (UI-002, APR-003).

The model, a file, or a tool result can contain terminal control sequences: OSC 52
writes to the clipboard, OSC 0 retitles the window, `ESC[2J` clears the screen, a
bare carriage return overwrites what was just printed. Printed raw, they let the
text shown to the operator differ from what will run. This removes them, and makes
invisible bidirectional and zero-width characters visible instead of dropping them,
so a reordered command is seen as such.
"""

from __future__ import annotations

import re

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_STRING_SEQ = re.compile(r"\x1b[P^_X][^\x1b]*(?:\x1b\\)?")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_PAIR = re.compile(r"\x1b[@-Z\\-_]")
# Everything except tab (09) and newline (0A): C0, DEL and C1.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def sanitize_display(text: str) -> str:
    """Return `text` without terminal control sequences; invisible marks become `<U+XXXX>`."""

    for pattern in (_OSC, _STRING_SEQ, _CSI, _ESC_PAIR):
        text = pattern.sub("", text)
    text = _CONTROL.sub("", text)
    return _INVISIBLE.sub(lambda match: f"<U+{ord(match.group()):04X}>", text)
