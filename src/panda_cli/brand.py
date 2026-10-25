"""Panda Spark brand mark and terminal rendering.

The mark is a 13x13 pixel glyph rendered as 13x7 half-block cells. It is the
minimal identity asset; the full mascot strip is a later, separate asset layer.
"""

from __future__ import annotations

import os
import sys

from rich.text import Text

PALETTE = {
    "ink": "#141414",
    "black": "#2A2A2A",
    "black.hi": "#3D3D3D",
    "white": "#F2F2F2",
    "white.shadow": "#C9C9D9",
    "white.hi": "#FFFFFF",
    "green": "#2ECC71",
    "green.hi": "#58E894",
    "green.dim": "#1B7F4B",
    "amber": "#F5A623",
    "amber.dim": "#8A5A12",
}

# 13 columns x 13 rows. '.' transparent; G/g/H green tones; W/w white;
# K/k black; A/a amber.
SPARK = (
    "......G......",
    "...G..G..A...",
    "....G.G.G....",
    ".....GGG.....",
    "..GGGWWWGGG..",
    "...GKKKKKG...",
    "..GKKWWWKKG..",
    "...GKKKKKG...",
    "..GGGWWWGGG..",
    ".....GGG.....",
    "....G.G.G....",
    "...A..G..G...",
    "......G......",
)

_CHAR_STYLE = {
    "G": PALETTE["green"],
    "H": PALETTE["green.hi"],
    "g": PALETTE["green.dim"],
    "W": PALETTE["white"],
    "w": PALETTE["white.shadow"],
    "K": PALETTE["black"],
    "k": PALETTE["black.hi"],
    "A": PALETTE["amber"],
    "a": PALETTE["amber.dim"],
}


def _style(char: str) -> str | None:
    return _CHAR_STYLE.get(char)


def half_block_lines(rows: list[str], styles: dict[str, str]) -> list[Text]:
    """Render a character grid as half-block cells.

    Each output row combines two grid rows: upper half-block for the top
    character, lower half-block for the bottom one. Adjacent styled pixels use a
    foreground/background pair, so a 13x13 mark becomes 13x7 cells.
    """

    rows = list(rows)
    if not rows:
        return []
    width = max(len(row) for row in rows)
    if len(rows) % 2:
        rows.append("." * width)
    lines: list[Text] = []
    for index in range(0, len(rows), 2):
        top = rows[index].ljust(width, ".")
        bottom = rows[index + 1].ljust(width, ".")
        line = Text()
        for top_char, bottom_char in zip(top, bottom, strict=True):
            top_style = styles.get(top_char)
            bottom_style = styles.get(bottom_char)
            if top_style is None and bottom_style is None:
                line.append(" ")
            elif top_style is not None and bottom_style is None:
                line.append("▀", style=top_style)
            elif top_style is None and bottom_style is not None:
                line.append("▄", style=bottom_style)
            else:
                line.append("▀", style=f"{top_style} on {bottom_style}")
        lines.append(line)
    return lines


def spark_lines() -> list[Text]:
    return half_block_lines(list(SPARK), _CHAR_STYLE)


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() in {"dumb", "unknown"}:
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def brand_lines(version: str) -> list[Text]:
    mark = spark_lines()
    if not color_enabled():
        return [Text(f"PANDA {version}", style="bold")]

    side: list[Text] = [
        Text.assemble(
            ("PANDA", f"bold {PALETTE['white']}"),
            ("  ", ""),
            (version, PALETTE["green"]),
        ),
        Text("evidence harness", style=PALETTE["white.shadow"]),
        Text("fail-closed by construction", style=PALETTE["green.dim"]),
    ]
    lines: list[Text] = []
    for index, mark_line in enumerate(mark):
        line = Text()
        line.append_text(mark_line)
        if index < len(side):
            line.append("  ")
            line.append_text(side[index])
        lines.append(line)
    return lines
