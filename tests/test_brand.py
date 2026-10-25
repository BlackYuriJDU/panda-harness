"""Brand assets: Spark geometry, palette discipline, and plain fallback."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from panda_cli import brand

GOLDEN = Path(__file__).parent / "golden" / "spark.txt"
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def test_spark_is_13_by_13_and_uses_known_tones() -> None:
    assert len(brand.SPARK) == 13
    assert {len(row) for row in brand.SPARK} == {13}
    allowed = set("GgHhWwKkAa.")
    assert {char for row in brand.SPARK for char in row} <= allowed


def test_spark_palette_is_green_black_white_amber() -> None:
    for name, value in brand.PALETTE.items():
        assert HEX.match(value), name
    assert "red" not in brand.PALETTE
    assert brand.PALETTE["green"] == "#2ECC71"
    assert brand.PALETTE["amber"] == "#F5A623"


def test_spark_renders_seven_rows_of_thirteen_cells() -> None:
    lines = brand.spark_lines()
    assert len(lines) == 7
    assert {len(line.plain) for line in lines} == {13}
    assert all("\n" not in line.plain for line in lines)


def test_spark_matches_golden_layout() -> None:
    actual = "\n".join(line.plain.rstrip() for line in brand.spark_lines())
    expected = "\n".join(
        line.rstrip() for line in GOLDEN.read_text(encoding="utf-8").splitlines()
    )
    assert actual == expected


def test_spark_uses_foreground_and_background_styles() -> None:
    lines = brand.spark_lines()
    assert any("▀" in line.plain or "▄" in line.plain for line in lines)
    styles = {str(span.style) for line in lines for span in line.spans}
    assert any(" on " in style for style in styles)


def test_plain_brand_is_a_single_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brand, "color_enabled", lambda: False)
    lines = brand.brand_lines("0.1.0a1")
    assert len(lines) == 1
    assert lines[0].plain == "PANDA 0.1.0a1"


def test_color_brand_places_mark_beside_the_wordmark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(brand, "color_enabled", lambda: True)
    lines = brand.brand_lines("0.1.0a1")
    assert len(lines) == 7
    assert "PANDA" in lines[0].plain
    assert "0.1.0a1" in lines[0].plain
    assert "evidence harness" in lines[1].plain
    assert "fail-closed" in lines[2].plain


def test_no_color_environment_disables_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert brand.color_enabled() is False


def test_dumb_terminal_disables_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert brand.color_enabled() is False
