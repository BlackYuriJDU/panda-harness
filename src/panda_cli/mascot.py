"""Stateful Panda mascot and telemetry strip.

The mascot is rasterized from primitives on a 28x24 grid and rendered as 28x12
half-block cells. Eight poses share one body, so the character stays consistent
instead of drifting between hand-drawn frames.

The strip pairs the mascot with a six-line panel: state, effort, model, cache,
context, and tokens. It is a renderer, not a telemetry collector; callers supply
the metrics they actually have.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

from panda_cli.brand import PALETTE, half_block_lines

WIDTH = 28
HEIGHT = 24
ANIMATION_FPS = 12
BLINK_PERIOD = 16
FRAMES = (
    "idle",
    "thinking",
    "working",
    "verifying",
    "success",
    "blocked",
    "alert",
    "sleeping",
)

_CHAR_STYLE = {
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

_WHITE = PALETTE["white"]
_SHADOW = PALETTE["white.shadow"]
_GREEN = PALETTE["green"]
_GREEN_HI = PALETTE["green.hi"]
_AMBER = PALETTE["amber"]


def _grid() -> list[list[str]]:
    return [["." for _ in range(WIDTH)] for _ in range(HEIGHT)]


def _plot(grid: list[list[str]], x: int, y: int, char: str) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        grid[y][x] = char


def _ellipse(
    grid: list[list[str]],
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    char: str,
) -> None:
    for y in range(HEIGHT):
        for x in range(WIDTH):
            dx = (x + 0.5 - cx) / rx
            dy = (y + 0.5 - cy) / ry
            if dx * dx + dy * dy <= 1.0:
                grid[y][x] = char


def _ring(
    grid: list[list[str]],
    cx: float,
    cy: float,
    radius: float,
    char: str,
    thickness: float = 0.9,
) -> None:
    for y in range(HEIGHT):
        for x in range(WIDTH):
            dx = x + 0.5 - cx
            dy = y + 0.5 - cy
            distance = (dx * dx + dy * dy) ** 0.5
            if abs(distance - radius) <= thickness / 2:
                grid[y][x] = char


def _capsule(
    grid: list[list[str]],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    char: str,
) -> None:
    vx = x2 - x1
    vy = y2 - y1
    length_sq = vx * vx + vy * vy
    for y in range(HEIGHT):
        for x in range(WIDTH):
            px = x + 0.5
            py = y + 0.5
            if length_sq == 0:
                t = 0.0
            else:
                t = ((px - x1) * vx + (py - y1) * vy) / length_sq
                t = max(0.0, min(1.0, t))
            dx = px - (x1 + t * vx)
            dy = py - (y1 + t * vy)
            if dx * dx + dy * dy <= radius * radius:
                grid[y][x] = char


def _eye_open(grid: list[list[str]], cx: float, cy: float, look: tuple[int, int]) -> None:
    _ellipse(grid, cx, cy, 1.15, 1.35, "W")
    _plot(grid, int(cx) + look[0], int(cy) + look[1], "K")


def _eye_closed(grid: list[list[str]], cx: float, cy: float) -> None:
    for offset in (-1, 0, 1):
        _plot(grid, int(cx) + offset, int(cy) + 1, "W")


def _eye_happy(grid: list[list[str]], cx: float, cy: float) -> None:
    center = int(cx)
    top = int(cy)
    _plot(grid, center - 1, top + 1, "W")
    _plot(grid, center, top, "W")
    _plot(grid, center + 1, top + 1, "W")


def _eye_wide(grid: list[list[str]], cx: float, cy: float) -> None:
    _ellipse(grid, cx, cy, 1.5, 1.8, "W")
    _plot(grid, int(cx), int(cy), "K")


def _mouth(grid: list[list[str]], mode: str) -> None:
    if mode == "smile":
        _plot(grid, 12, 15, "K")
        for x in (13, 14, 15):
            _plot(grid, x, 16, "K")
        _plot(grid, 16, 15, "K")
    elif mode == "open":
        _ellipse(grid, 14, 16, 1.8, 1.2, "K")
        _plot(grid, 14, 16, "A")
    elif mode == "flat":
        for x in range(12, 17):
            _plot(grid, x, 16, "K")
    else:  # tiny
        for x in (13, 14, 15):
            _plot(grid, x, 16, "K")


def _scarf(grid: list[list[str]]) -> None:
    for x in range(7, 22):
        _plot(grid, x, 17, "G")
    for x in range(8, 21):
        _plot(grid, x, 18, "G")
    _plot(grid, 21, 18, "g")
    _plot(grid, 21, 19, "G")
    _plot(grid, 22, 19, "g")
    _plot(grid, 22, 20, "G")
    _plot(grid, 22, 21, "g")


def _arms(grid: list[list[str]], pose: str) -> None:
    if pose == "down":
        _capsule(grid, 6.8, 17.2, 5.6, 21.4, 2.0, "K")
        _capsule(grid, 21.2, 17.2, 22.4, 21.4, 2.0, "K")
        _ellipse(grid, 5.5, 21.4, 1.3, 1.2, "k")
        _ellipse(grid, 22.5, 21.4, 1.3, 1.2, "k")
    elif pose == "up":
        _capsule(grid, 7.0, 17.2, 4.0, 11.2, 2.0, "K")
        _capsule(grid, 21.0, 17.2, 24.0, 11.2, 2.0, "K")
        _ellipse(grid, 4.0, 10.9, 1.4, 1.3, "k")
        _ellipse(grid, 24.0, 10.9, 1.4, 1.3, "k")
    elif pose == "think":
        _capsule(grid, 6.8, 17.2, 5.6, 21.4, 2.0, "K")
        _capsule(grid, 21.0, 17.2, 17.2, 14.2, 1.7, "K")
        _ellipse(grid, 17.0, 14.0, 1.4, 1.2, "k")
        _ellipse(grid, 5.5, 21.4, 1.3, 1.2, "k")
    elif pose == "type":
        _capsule(grid, 7.2, 17.4, 11.2, 21.2, 1.8, "K")
        _capsule(grid, 20.8, 17.4, 16.8, 21.2, 1.8, "K")
        _ellipse(grid, 11.2, 21.2, 1.2, 1.1, "k")
        _ellipse(grid, 16.8, 21.2, 1.2, 1.1, "k")
        for x in range(10, 19):
            _plot(grid, x, 22, "g")
    elif pose == "verify":
        _capsule(grid, 6.8, 17.2, 5.6, 21.4, 2.0, "K")
        _capsule(grid, 21.0, 17.4, 22.3, 14.6, 1.7, "K")
        _ellipse(grid, 5.5, 21.4, 1.3, 1.2, "k")
        _ellipse(grid, 22.3, 14.4, 1.3, 1.2, "k")
        _ring(grid, 24.0, 13.8, 2.4, "G", thickness=1.1)
        _capsule(grid, 25.5, 15.5, 27.2, 18.6, 0.8, "G")
    elif pose == "cross":
        _capsule(grid, 7.2, 17.6, 14.0, 20.6, 1.9, "K")
        _capsule(grid, 20.8, 17.6, 14.0, 20.6, 1.9, "K")
        _ellipse(grid, 14.0, 20.6, 2.0, 1.6, "k")
    elif pose == "out":
        _capsule(grid, 7.2, 17.6, 3.2, 16.8, 1.9, "K")
        _capsule(grid, 20.8, 17.6, 24.8, 16.8, 1.9, "K")
        _ellipse(grid, 3.2, 16.8, 1.3, 1.2, "k")
        _ellipse(grid, 24.8, 16.8, 1.3, 1.2, "k")
    else:
        raise ValueError(f"unknown arm pose: {pose}")


def _prop(grid: list[list[str]], name: str) -> None:
    if name == "dots":
        for x, y in ((24, 7), (26, 8), (24, 9)):
            _plot(grid, x, y, "A")
    elif name == "prompt":
        _capsule(grid, 24.0, 8.0, 26.2, 9.2, 0.7, "G")
        _capsule(grid, 26.2, 9.2, 24.0, 10.4, 0.7, "G")
    elif name == "check":
        _capsule(grid, 23.0, 18.0, 24.4, 19.6, 0.8, "H")
        _capsule(grid, 24.4, 19.6, 27.0, 16.4, 0.8, "H")
    elif name == "cross":
        _capsule(grid, 24.0, 8.0, 27.0, 11.0, 0.8, "A")
        _capsule(grid, 27.0, 8.0, 24.0, 11.0, 0.8, "A")
    elif name == "bang":
        _capsule(grid, 26.0, 9.0, 26.0, 13.4, 0.8, "A")
        _plot(grid, 26, 15, "A")
    elif name == "zzz":
        _z(grid, 24, 9, 3, "G")
        _z(grid, 25, 12, 3, "g")
    elif name:
        raise ValueError(f"unknown prop: {name}")


def _z(grid: list[list[str]], x: int, y: int, size: int, char: str) -> None:
    for offset in range(size):
        _plot(grid, x + offset, y, char)
        _plot(grid, x + offset, y + size - 1, char)
    for step in range(1, size - 1):
        _plot(grid, x + size - 1 - step, y + step, char)


def _panda(state: str, tick: int = 0) -> list[list[str]]:
    grid = _grid()

    # Feet, then body: the body hides the top of the feet.
    _ellipse(grid, 9.0, 23.0, 3.2, 1.8, "K")
    _ellipse(grid, 19.0, 23.0, 3.2, 1.8, "K")
    _ellipse(grid, 14.0, 20.5, 7.4, 5.6, "K")
    _ellipse(grid, 14.0, 20.7, 6.6, 4.8, "W")

    # Ears first; the head overlaps their inner edge.
    _ellipse(grid, 6.0, 3.2, 3.4, 3.3, "K")
    _ellipse(grid, 22.0, 3.2, 3.4, 3.3, "K")
    _ellipse(grid, 6.0, 3.2, 1.5, 1.4, "k")
    _ellipse(grid, 22.0, 3.2, 1.5, 1.4, "k")
    _ellipse(grid, 14.0, 10.5, 8.5, 7.6, "K")
    _ellipse(grid, 14.0, 10.6, 7.6, 6.7, "W")

    _ellipse(grid, 10.0, 10.3, 2.3, 2.8, "K")
    _ellipse(grid, 18.0, 10.3, 2.3, 2.8, "K")

    if state == "thinking":
        _eye_open(grid, 9.8, 9.9, (1, -1))
        _eye_open(grid, 18.2, 9.9, (1, -1))
        _mouth(grid, "flat")
    elif state == "working":
        _eye_open(grid, 9.8, 9.9, (0, 1))
        _eye_open(grid, 18.2, 9.9, (0, 1))
        _mouth(grid, "flat")
    elif state == "verifying":
        _eye_open(grid, 9.8, 9.9, (0, 0))
        _eye_open(grid, 18.2, 9.9, (0, 0))
        _mouth(grid, "flat")
    elif state == "success":
        _eye_happy(grid, 9.8, 9.9)
        _eye_happy(grid, 18.2, 9.9)
        _mouth(grid, "open")
        _plot(grid, 6, 13, "a")
        _plot(grid, 21, 13, "a")
    elif state == "blocked":
        _eye_closed(grid, 9.8, 9.9)
        _eye_closed(grid, 18.2, 9.9)
        _mouth(grid, "flat")
    elif state == "alert":
        _eye_wide(grid, 9.8, 9.9)
        _eye_wide(grid, 18.2, 9.9)
        _mouth(grid, "open")
    elif state == "sleeping":
        _eye_closed(grid, 9.8, 9.9)
        _eye_closed(grid, 18.2, 9.9)
        _mouth(grid, "tiny")
    else:
        _eye_open(grid, 9.8, 9.9, (0, 0))
        _eye_open(grid, 18.2, 9.9, (0, 0))
        _mouth(grid, "smile")

    # Nose.
    for x in (13, 14, 15):
        _plot(grid, x, 13, "K")
    _plot(grid, 14, 14, "K")

    _scarf(grid)

    arm_pose = {
        "idle": "down",
        "thinking": "think",
        "working": "type",
        "verifying": "verify",
        "success": "up",
        "blocked": "cross",
        "alert": "out",
        "sleeping": "down",
    }[state]
    _arms(grid, arm_pose)

    _prop(
        grid,
        {
            "idle": "",
            "thinking": "dots",
            "working": "prompt",
            "verifying": "",
            "success": "check",
            "blocked": "cross",
            "alert": "bang",
            "sleeping": "zzz",
        }[state],
    )

    # Animation is opt-in through `tick`; tick=0 is byte-identical to the
    # static frame that the golden tests pin.
    if tick > 0 and tick % BLINK_PERIOD in (0, 1) and state not in {"sleeping", "blocked"}:
        _eye_closed(grid, 9.8, 9.9)
        _eye_closed(grid, 18.2, 9.9)
    if tick > 0 and state in {"thinking", "working", "verifying"}:
        phase = tick % 8
        for index, (x, y) in enumerate(((24, 6), (26, 8), (25, 10))):
            if (tick + index) % 4 == 0:
                continue
            _plot(grid, x + (phase % 2), y - (phase // 4), "A")
    if tick > 0 and tick % 4 in (1, 3):
        grid = [["." for _ in range(WIDTH)], *grid[:-1]]
    return grid


def frame_grid(state: str = "idle", tick: int = 0) -> list[str]:
    """Return the raw 28x24 character grid for one frame.

    `tick=0` (the default) is the static frame. Positive ticks add a fixed-
    timestep bob, blink, and particle animation for the cockpit.
    """

    if state not in FRAMES:
        raise ValueError(f"unknown mascot state: {state}")
    return ["".join(row) for row in _panda(state, tick=max(0, int(tick)))]


def mascot_lines(state: str = "idle", tick: int = 0) -> list[Text]:
    return half_block_lines(frame_grid(state, tick), _CHAR_STYLE)


STATE_ICON = {
    "idle": "·",
    "thinking": "…",
    "working": "▸",
    "verifying": "◎",
    "success": "✓",
    "blocked": "×",
    "alert": "!",
    "sleeping": "z",
}


@dataclass(frozen=True)
class MascotMetrics:
    state: str = "idle"
    effort: float | None = None
    model: str = ""
    cache: float | None = None
    context: float | None = None
    tokens: int | None = None

    def __post_init__(self) -> None:
        if self.state not in FRAMES:
            raise ValueError(f"unknown mascot state: {self.state}")

    def clamped(self, field: str) -> float:
        value = getattr(self, field)
        if value is None:
            return 0.0
        return max(0.0, min(1.0, float(value)))


def bar(value: float, width: int = 10) -> str:
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "█" * filled + "░" * (width - filled)


def format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(max(0, tokens))


def _bar_style(label: str, value: float) -> str:
    if label == "context" and value >= 0.85:
        return _AMBER
    if label == "cache" and value < 0.35:
        return _AMBER
    return _GREEN


def _metric_line(label: str, value: Text) -> Text:
    line = Text.assemble((f"{label:<8}", _SHADOW), value)
    return line


def metrics_lines(metrics: MascotMetrics) -> list[Text]:
    state_style = _AMBER if metrics.state in {"blocked", "alert"} else _GREEN
    icon = STATE_ICON.get(metrics.state, "·")
    lines = [
        Text.assemble(
            ("PANDA", f"bold {_WHITE}"),
            ("  ", ""),
            (f"{icon} {metrics.state}", state_style),
        )
    ]

    for label in ("effort", "cache", "context"):
        value = getattr(metrics, label)
        if value is None:
            lines.append(_metric_line(label, Text("—", style=_SHADOW)))
            continue
        clamped = metrics.clamped(label)
        lines.append(
            _metric_line(
                label,
                Text.assemble(
                    (bar(clamped), _bar_style(label, clamped)),
                    (f"  {round(clamped * 100):>3}%", _SHADOW),
                ),
            )
        )

    model = metrics.model or "not selected"
    if len(model) > 20:
        model = model[:19] + "…"
    lines.insert(2, _metric_line("model", Text(model, style=_WHITE)))

    tokens = "—" if metrics.tokens is None else format_tokens(metrics.tokens)
    lines.append(_metric_line("tokens", Text(tokens, style=_WHITE)))
    return lines


def render_strip(metrics: MascotMetrics) -> list[Text]:
    """Mascot on the left, telemetry panel on the right, twelve rows tall."""

    mascot = mascot_lines(metrics.state)
    panel = metrics_lines(metrics)
    lines: list[Text] = []
    for index, mark in enumerate(mascot):
        line = Text()
        line.append_text(mark)
        if index < len(panel) and panel[index].plain:
            line.append("  ")
            line.append_text(panel[index])
        lines.append(line)
    for extra in panel[len(mascot) :]:
        lines.append(Text(" " * WIDTH + "  ").append_text(extra))
    return lines


__all__ = [
    "FRAMES",
    "HEIGHT",
    "WIDTH",
    "MascotMetrics",
    "STATE_ICON",
    "bar",
    "format_tokens",
    "frame_grid",
    "mascot_lines",
    "metrics_lines",
    "render_strip",
]
