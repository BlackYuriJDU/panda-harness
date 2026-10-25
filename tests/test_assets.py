"""Committed SVG assets stay in sync with the terminal grids."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from panda_cli.brand import PALETTE, SPARK
from panda_cli.mascot import FRAMES, frame_grid

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
NS = "{http://www.w3.org/2000/svg}"
ALLOWED_FILLS = set(PALETTE.values())


def parse(name: str) -> ET.Element:
    return ET.parse(ASSETS / name).getroot()


def test_all_expected_assets_exist() -> None:
    names = {f"mascot-{state}.svg" for state in FRAMES}
    names |= {"spark.svg", "icon.svg", "head.svg", "splash.svg"}
    assert names <= {path.name for path in ASSETS.glob("*.svg")}


@pytest.mark.parametrize("path", sorted(ASSETS.glob("*.svg")), ids=lambda path: path.name)
def test_svg_is_well_formed_and_safe(path: Path) -> None:
    root = ET.parse(path).getroot()
    assert root.tag == f"{NS}svg"
    assert root.attrib.get("viewBox")
    assert not list(root.iter(f"{NS}script"))
    for element in root.iter():
        for key, value in element.attrib.items():
            if key.endswith("href"):
                assert not value.startswith(("http://", "https://", "//")), (path.name, value)
    fills = {
        element.attrib["fill"]
        for element in root.iter()
        if "fill" in element.attrib
    }
    assert fills <= ALLOWED_FILLS, (path.name, fills - ALLOWED_FILLS)


@pytest.mark.parametrize("state", FRAMES)
def test_mascot_assets_match_the_terminal_grid(state: str) -> None:
    root = parse(f"mascot-{state}.svg")
    assert root.attrib["viewBox"] == "0 0 28 24"
    rects = list(root.iter(f"{NS}rect"))
    expected = sum(1 for row in frame_grid(state) for char in row if char != ".")
    assert len(rects) == expected


def test_spark_and_icon_share_the_mark() -> None:
    assert parse("spark.svg").attrib["viewBox"] == f"0 0 {len(SPARK)} 13"
    icon = parse("icon.svg")
    assert icon.attrib["width"] == "256"
    assert icon.attrib["height"] == "256"
    assert icon.attrib["viewBox"] == "0 0 17 17"
    assert any(
        element.attrib.get("fill") == PALETTE["ink"] for element in icon.iter(f"{NS}rect")
    )


def test_splash_carries_the_wordmark() -> None:
    root = parse("splash.svg")
    text = " ".join(root.itertext())
    assert "PANDA" in text
    assert "evidence harness" in text
    assert "fail-closed by construction" in text
