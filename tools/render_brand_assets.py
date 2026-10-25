"""Generate the committed SVG brand assets from the terminal grids.

Run from the repository root:

    python tools/render_brand_assets.py

The terminal renderer (panda_cli.brand / panda_cli.mascot) is the source of
truth. If a pixel changes there, regenerate the assets and commit both.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from panda_cli.brand import PALETTE, SPARK  # noqa: E402
from panda_cli.mascot import FRAMES, frame_grid  # noqa: E402

ASSETS = ROOT / "assets"

COLORS = {
    "K": PALETTE["black"],
    "k": PALETTE["black.hi"],
    "W": PALETTE["white"],
    "w": PALETTE["white.shadow"],
    "G": PALETTE["green"],
    "H": PALETTE["green.hi"],
    "g": PALETTE["green.dim"],
    "A": PALETTE["amber"],
    "a": PALETTE["amber.dim"],
}

MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def _rects(rows: list[str]) -> str:
    parts: list[str] = []
    for y, row in enumerate(rows):
        for x, char in enumerate(row):
            color = COLORS.get(char)
            if color:
                parts.append(
                    f'<rect x="{x}" y="{y}" width="1" height="1" fill="{color}"/>'
                )
    return "".join(parts)


def pixel_svg(
    rows: list[str],
    *,
    scale: int = 1,
    background: str | None = None,
    size: tuple[int, int] | None = None,
) -> str:
    width = max(len(row) for row in rows)
    height = len(rows)
    out_width, out_height = size if size else (width * scale, height * scale)
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" width="{out_width}" height="{out_height}" '
        'shape-rendering="crispEdges">'
    ]
    if background:
        parts.append(f'<rect width="{width}" height="{height}" fill="{background}"/>')
    parts.append(_rects(rows))
    parts.append("</svg>")
    return "".join(parts)


def padded(rows: list[str], pad: int) -> list[str]:
    width = max(len(row) for row in rows) + pad * 2
    blank = "." * width
    return [blank] * pad + ["." * pad + row + "." * pad for row in rows] + [blank] * pad


def spark_svg() -> str:
    return pixel_svg(list(SPARK), scale=8)


def icon_svg() -> str:
    return pixel_svg(
        padded(list(SPARK), 2),
        background=PALETTE["ink"],
        size=(256, 256),
    )


def mascot_svg(state: str) -> str:
    return pixel_svg(frame_grid(state), scale=10)


def head_svg() -> str:
    rows = frame_grid("idle")[:18]
    return pixel_svg(rows, scale=10)


def splash_svg() -> str:
    width, height = 900, 300
    mascot = _rects(frame_grid("idle"))
    spark = _rects(list(SPARK))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
        f'<rect width="{width}" height="{height}" fill="{PALETTE["ink"]}"/>'
        f'<svg x="36" y="30" width="280" height="240" viewBox="0 0 28 24">{mascot}</svg>'
        f'<svg x="370" y="58" width="78" height="78" viewBox="0 0 13 13">{spark}</svg>'
        f'<text x="470" y="126" font-family="{MONO}" font-size="64" '
        f'letter-spacing="8" fill="{PALETTE["white"]}">PANDA</text>'
        f'<text x="472" y="170" font-family="{MONO}" font-size="20" '
        f'fill="{PALETTE["green"]}">evidence harness</text>'
        f'<text x="472" y="204" font-family="{MONO}" font-size="16" '
        f'fill="{PALETTE["white.shadow"]}">fail-closed by construction</text>'
        f'<text x="472" y="252" font-family="{MONO}" font-size="13" '
        f'fill="{PALETTE["green.dim"]}">github.com/BlackYuriJDU/panda-harness</text>'
        "</svg>"
    )


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    (ASSETS / "spark.svg").write_text(spark_svg(), encoding="utf-8")
    (ASSETS / "icon.svg").write_text(icon_svg(), encoding="utf-8")
    (ASSETS / "head.svg").write_text(head_svg(), encoding="utf-8")
    (ASSETS / "splash.svg").write_text(splash_svg(), encoding="utf-8")
    for state in FRAMES:
        (ASSETS / f"mascot-{state}.svg").write_text(mascot_svg(state), encoding="utf-8")
    print(f"wrote {len(FRAMES) + 4} assets to {ASSETS}")


if __name__ == "__main__":
    main()
