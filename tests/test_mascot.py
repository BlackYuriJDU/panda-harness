"""Mascot frames and the telemetry strip."""

from __future__ import annotations

from pathlib import Path

import pytest

from panda_cli import mascot
from panda_cli.brand import PALETTE

GOLDEN = Path(__file__).parent / "golden" / "mascot-idle.txt"


def plain(lines) -> list[str]:
    return [line.plain.rstrip() for line in lines]


def test_every_frame_is_twelve_rows_of_twenty_eight_cells() -> None:
    for state in mascot.FRAMES:
        lines = mascot.mascot_lines(state)
        assert len(lines) == 12, state
        assert {len(line.plain) for line in lines} == {28}, state


def test_all_frames_are_distinct() -> None:
    rendered = {state: "\n".join(plain(mascot.mascot_lines(state))) for state in mascot.FRAMES}
    assert len(set(rendered.values())) == len(mascot.FRAMES)


def test_idle_frame_matches_golden() -> None:
    expected = GOLDEN.read_text(encoding="utf-8").splitlines()
    assert plain(mascot.mascot_lines("idle")) == expected


def test_unknown_frame_is_rejected() -> None:
    with pytest.raises(ValueError):
        mascot.mascot_lines("dancing")


def test_frames_use_the_brand_palette_without_red() -> None:
    assert not any("red" in name for name in PALETTE)
    for state in mascot.FRAMES:
        styles = {str(span.style) for line in mascot.mascot_lines(state) for span in line.spans}
        assert styles, state
        assert any(
            PALETTE["green"] in style or PALETTE["amber"] in style for style in styles
        ), state


def test_bar_is_bounded() -> None:
    assert mascot.bar(0.0, 10) == "░" * 10
    assert mascot.bar(1.0, 10) == "█" * 10
    assert mascot.bar(0.5, 10) == "█" * 5 + "░" * 5
    assert mascot.bar(-3, 10) == "░" * 10
    assert mascot.bar(9, 10) == "█" * 10


def test_token_formatting() -> None:
    assert mascot.format_tokens(0) == "0"
    assert mascot.format_tokens(999) == "999"
    assert mascot.format_tokens(1_200) == "1.2k"
    assert mascot.format_tokens(12_400) == "12.4k"
    assert mascot.format_tokens(2_500_000) == "2.5M"


def test_metrics_panel_has_the_six_contract_lines() -> None:
    metrics = mascot.MascotMetrics(
        state="working",
        effort=0.62,
        model="openrouter/anthropic/claude-sonnet-5.5",
        cache=0.78,
        context=0.41,
        tokens=12_400,
    )
    lines = mascot.metrics_lines(metrics)
    assert [line.plain.split()[0] for line in lines] == [
        "PANDA",
        "effort",
        "model",
        "cache",
        "context",
        "tokens",
    ]
    assert "working" in lines[0].plain
    assert "…" in lines[2].plain


def test_context_pressure_turns_amber() -> None:
    metrics = mascot.MascotMetrics(state="idle", context=0.92)
    context_line = next(
        line for line in mascot.metrics_lines(metrics) if line.plain.startswith("context")
    )
    assert any(str(span.style) == PALETTE["amber"] for span in context_line.spans)


def test_low_cache_turns_amber() -> None:
    metrics = mascot.MascotMetrics(state="idle", cache=0.1)
    cache_line = next(
        line for line in mascot.metrics_lines(metrics) if line.plain.startswith("cache")
    )
    assert any(str(span.style) == PALETTE["amber"] for span in cache_line.spans)


def test_blocked_state_uses_amber_label() -> None:
    metrics = mascot.MascotMetrics(state="blocked")
    assert any(
        str(span.style) == PALETTE["amber"] for span in mascot.metrics_lines(metrics)[0].spans
    )


def test_unknown_metrics_state_is_rejected() -> None:
    with pytest.raises(ValueError):
        mascot.MascotMetrics(state="dancing")


def test_strip_is_twelve_rows_and_keeps_the_panel() -> None:
    metrics = mascot.MascotMetrics(state="verifying", effort=1.4, cache=-1, tokens=999)
    lines = mascot.render_strip(metrics)
    assert len(lines) == 12
    assert all(len(line.plain) > 28 for line in lines[:6])
    assert all(len(line.plain) == 28 for line in lines[6:])
    text = "\n".join(line.plain for line in lines)
    assert "PANDA" in text
    assert "verifying" in text
    assert "tokens" in text
    assert "100%" in text  # clamped from 1.4
