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
from typing import Any

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_STRING_SEQ = re.compile(r"\x1b[P^_X][^\x1b]*(?:\x1b\\)?")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_PAIR = re.compile(r"\x1b[@-Z\\-_]")
# Everything except tab (09) and newline (0A): C0, DEL and C1.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Zero-width, bidi, line/paragraph separators, Arabic letter mark, Mongolian vowel
# separator, soft hyphen, Hangul fillers, variation selectors and tag characters: all
# can make what is shown differ from what is there (R2-222).
_INVISIBLE = re.compile(
    "[\u00ad\u061c\u115f\u1160\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2064"
    "\u2066-\u2069\u3164\ufe00-\ufe0f\ufeff\uffa0\U000e0000-\U000e007f"
    "\U000e0100-\U000e01ef]"
)


def sanitize_display(text: str) -> str:
    """Return `text` without terminal control sequences; invisible marks become `<U+XXXX>`."""

    for pattern in (_OSC, _STRING_SEQ, _CSI, _ESC_PAIR):
        text = pattern.sub("", text)
    text = _CONTROL.sub("", text)
    return _INVISIBLE.sub(lambda match: f"<U+{ord(match.group()):04X}>", text)


def render_untrusted(text: object, style: str = "") -> Any:
    """The one way untrusted text becomes a Rich renderable: literal and sanitized."""

    from rich.text import Text

    return Text(sanitize_display(str(text)), style=style)


def json_safe(text: str) -> str:
    """Escape invisible and C1 characters in JSON output (valid JSON, nothing hidden)."""

    def escape(match: re.Match[str]) -> str:
        encoded = match.group().encode("utf-16-be")
        units = range(0, len(encoded), 2)
        return "".join(f"\\u{encoded[i]:02x}{encoded[i + 1]:02x}" for i in units)

    return _JSON_UNSAFE.sub(escape, text)


_JSON_UNSAFE = re.compile(_INVISIBLE.pattern[:-1] + "\x7f-\x9f]")
# Only SGR (colour) survives the sink: Rich emits it, nothing else should reach the tty.
_NON_SGR_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-ln-~]")


class SafeWriter:
    """A text stream that strips non-colour terminal sequences from everything written.

    Wrapping the console's file sanitizes at the sink, so a call site that forgets
    to sanitize (R2-306, R2-223: tables, errors, validator output) cannot emit OSC 8
    links, OSC 52 clipboard writes or C1 controls.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        for pattern in (_OSC, _STRING_SEQ, _NON_SGR_CSI, _ESC_PAIR):
            text = pattern.sub("", text)
        text = _SINK_CONTROL.sub("", text)
        text = _INVISIBLE.sub(lambda match: f"<U+{ord(match.group()):04X}>", text)
        return self._stream.write(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


# Like _CONTROL but keeps ESC, which only survives as part of an SGR sequence.
_SINK_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f-\x9f]")


from rich.text import Text as _RichText  # noqa: E402 - after the patterns it uses


class SafeText(_RichText):
    """A Rich Text whose content is always sanitized, however it is built (R2-221).

    The cockpit builds every block with this type, so a new `append` of model,
    tool or corpus text cannot emit terminal sequences or hide characters.
    """

    def __init__(self, text: str = "", *args: Any, **kwargs: Any) -> None:
        super().__init__(sanitize_display(text), *args, **kwargs)

    def append(self, text: Any, style: Any = None) -> SafeText:  # type: ignore[override]
        if isinstance(text, str):
            text = sanitize_display(text)
        super().append(text, style)
        return self

