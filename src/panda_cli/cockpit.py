"""Full-screen Panda cockpit (Textual 6.x).

The cockpit is a renderer and an approval surface. It owns no policy and no
validation logic: `ChatRuntime` and the tool registry remain authoritative. The
layout is responsive (wide / medium / narrow / tiny), the mascot animates at a
fixed timestep, and input stays available while the model streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from typing import Any

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static
from textual.worker import Worker

from panda_cli import mascot
from panda_cli.brand import PALETTE, spark_lines
from panda_cli.runtime import ChatRuntime
from panda_core.agent import DEFAULT_MAX_STEPS
from panda_core.errors import PandaError
from panda_core.registry import find_study, load_registry
from panda_core.session import SessionLog
from panda_core.tools import ApprovalRequest
from panda_core.version import __version__
from panda_core.workspace import Workspace

GREEN = PALETTE["green"]
GREEN_DIM = PALETTE["green.dim"]
AMBER = PALETTE["amber"]
WHITE = PALETTE["white"]
SHADOW = PALETTE["white.shadow"]
INK = PALETTE["ink"]
MOUSE_OFF = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"

HELP_TEXT = """Panda cockpit commands

/help                 show this help
/model [id]           show or switch the model
/models               list models (protocol, support, free)
/study list           list registered studies
/study use <id>       set the active study
/status               provider, model, session, usage
/validate             run the canonical validator
/policy               show policy rules
/session              show recent session events
/clear                clear the transcript (ledger stays)
/quit                 close the session and exit
!<argv>               run a command through policy and approval

panda init            initialize a corpus in the current directory
Enter sends · Ctrl+C cancels the running turn · Ctrl+Q quits.
Input is never blocked by a running turn.
"""


def _dim(text: str) -> Text:
    return Text(text, style=SHADOW)


def _short(value: str, limit: int = 72) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _loads(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


class ToolCard(Static):
    def __init__(self, name: str, arguments: str = "") -> None:
        self.tool_name = name
        self.arguments = arguments
        self.state = "running"
        self.detail = ""
        super().__init__(self._render(), classes="block")

    def _render(self) -> Text:
        icon, style = {
            "running": ("▸", SHADOW),
            "ok": ("✓", GREEN),
            "blocked": ("×", AMBER),
            "failed": ("!", AMBER),
        }[self.state]
        line = Text()
        line.append(f"{icon} ", style=style)
        line.append(self.tool_name, style=f"bold {WHITE}")
        if self.arguments:
            line.append("  " + _short(self.arguments), style=SHADOW)
        if self.detail:
            line.append("\n  " + _short(self.detail, 160), style=SHADOW)
        return line

    def finish(self, *, ok: bool, blocked: bool, detail: str = "") -> None:
        self.state = "blocked" if blocked else ("ok" if ok else "failed")
        self.detail = detail
        for name in ("ok", "blocked", "failed"):
            self.remove_class(name)
        self.add_class(self.state)
        self.update(self._render())


class ValidationPanel(Static):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(self._render(payload), classes="block")
        if not payload.get("ok"):
            self.add_class("failed")

    @staticmethod
    def _render(payload: dict[str, Any]) -> Text:
        summary = payload.get("summary", {})
        ok = bool(payload.get("ok"))
        line = Text()
        line.append("VALIDATION ", style=f"bold {WHITE}")
        line.append("✓ ok" if ok else "× failed", style=f"bold {GREEN if ok else AMBER}")
        line.append(
            f"  {summary.get('passed', 0)} passed · {summary.get('failed', 0)} failed · "
            f"{summary.get('skipped', 0)} skipped",
            style=SHADOW,
        )
        for check in payload.get("checks", []):
            if not check.get("ok") and not check.get("skipped"):
                line.append(f"\n  × {check.get('name')}", style=AMBER)
                for error in check.get("errors", [])[:2]:
                    line.append(f"\n    {_short(str(error), 120)}", style=SHADOW)
        return line
class ApprovalScreen(ModalScreen[bool]):
    """One approval decision. Nothing executes until this returns True."""

    BINDINGS = [
        ("y", "allow", "Allow"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    CSS = """
    ApprovalScreen {
        align: center middle;
        background: rgba(20, 20, 20, 0.88);
    }
    #approval-dialog {
        width: 80%;
        max-width: 110;
        height: auto;
        max-height: 90%;
        border: round #F5A623;
        background: #141414;
        padding: 1 2;
    }
    #approval-title {
        color: #F5A623;
        text-style: bold;
        height: 1;
    }
    #approval-summary {
        color: #F2F2F2;
        height: auto;
        margin: 1 0 0 0;
    }
    #approval-preview {
        color: #C9C9D9;
        max-height: 16;
        height: auto;
        margin: 1 0;
    }
    #approval-buttons {
        height: 3;
        align: right middle;
    }
    #approval-buttons Button {
        margin-left: 2;
    }
    """

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        request = self.request
        summary = Text()
        summary.append("tool     ", style=SHADOW)
        summary.append(request.tool, style=f"bold {WHITE}")
        summary.append("\naction   ", style=SHADOW)
        summary.append(request.action, style=WHITE)
        summary.append("  ", style=WHITE)
        summary.append(request.resource, style=WHITE)
        summary.append("\nrule     ", style=SHADOW)
        summary.append(request.rule_id or "-", style=WHITE)
        summary.append("   risk ", style=SHADOW)
        summary.append(request.risk, style=AMBER if request.risk in {"high", "critical"} else WHITE)
        summary.append("\nreason   ", style=SHADOW)
        summary.append(request.reason or "-", style=SHADOW)
        preview = Text(request.preview or "(no preview)", style=SHADOW)
        yield Vertical(
            Static(Text("APPROVAL REQUIRED", style=f"bold {AMBER}"), id="approval-title"),
            Static(summary, id="approval-summary"),
            Static(preview, id="approval-preview"),
            Horizontal(
                Button("Allow", id="allow"),
                Button("Deny", id="deny", variant="warning"),
                id="approval-buttons",
            ),
            id="approval-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class PandaCockpit(App[None]):
    CSS = """
    Screen {
        background: #141414;
        color: #F2F2F2;
        overflow: hidden;
    }
    #header {
        height: 3;
        border: round #1B7F4B;
        padding: 0 1;
        content-align: left middle;
        color: #F2F2F2;
        overflow: hidden;
    }
    #header.tight {
        height: 1;
        border: none;
        padding: 0 1;
    }
    #body {
        height: 1fr;
        overflow: hidden;
    }
    #transcript {
        width: 1fr;
        padding: 0 1;
        scrollbar-size: 1 1;
        overflow-x: hidden;
    }
    #sidebar {
        width: 44;
        border-left: solid #2A2A2A;
        padding: 0 1;
        overflow-y: auto;
        overflow-x: hidden;
    }
    #sidebar.compact {
        width: 20;
    }
    #mascot {
        height: 12;
    }
    #sidebar.compact #mascot {
        height: 7;
    }
    #metrics {
        height: auto;
    }
    #composer {
        height: 3;
        border: round #2A2A2A;
        background: #141414;
    }
    #composer:focus {
        border: round #2ECC71;
    }
    #hints {
        height: 1;
        color: #C9C9D9;
        padding: 0 1;
        overflow: hidden;
    }
    ToolCard {
        border: round #2A2A2A;
        padding: 0 1;
        margin: 0 0 1 0;
        height: auto;
    }
    ToolCard.ok {
        border: round #1B7F4B;
    }
    ToolCard.blocked, ToolCard.failed {
        border: round #8A5A12;
    }
    ValidationPanel {
        border: round #1B7F4B;
        padding: 0 1;
        margin: 0 0 1 0;
        height: auto;
    }
    ValidationPanel.failed {
        border: round #8A5A12;
    }
    .block {
        margin: 0 0 1 0;
        height: auto;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_turn", "Cancel", priority=True, show=False),
        ("ctrl+q", "quit_app", "Quit"),
        ("ctrl+l", "clear_transcript", "Clear"),
    ]

    def __init__(
        self,
        workspace: Workspace,
        *,
        session: SessionLog | None = None,
        model: str | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        private: bool = False,
        auto: bool = False,
    ) -> None:
        super().__init__()
        self.workspace = workspace
        self.session = session
        self.model_override = model
        self.max_steps = max_steps
        self.private = private
        self.auto = auto
        self.runtime: ChatRuntime | None = None
        self._state = "idle"
        self._rest_state = "idle"
        self._tick = 0
        self._last_anim = time.monotonic()
        self._reduced_motion = bool(os.environ.get("PANDA_REDUCED_MOTION"))
        self._focused = True
        self._busy = False
        self._status: dict[str, Any] = {}
        self._assistant_widget: Static | None = None
        self._assistant_buffer: list[str] = []
        self._tool_cards: dict[str, ToolCard] = {}
        self._last_markdown = 0.0
        self._width = 140
        self._turn_worker: Worker[Any] | None = None
    # ---- layout ----
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="body"):
            yield VerticalScroll(id="transcript")
            with Vertical(id="sidebar"):
                yield Static(id="mascot")
                yield Static(id="metrics")
        yield Input(placeholder="Ask Panda, or /help for commands", id="composer")
        yield Static(id="hints")

    async def on_mount(self) -> None:
        self.runtime = ChatRuntime(
            self.workspace,
            model=self.model_override,
            max_steps=self.max_steps,
            private=self.private,
            auto=self.auto,
            session=self.session,
            approver=self._approve,
            on_event=self._on_agent_event,
            label="cockpit",
        )
        self._status = self.runtime.status()
        self._apply_breakpoint(self.size.width)
        self._refresh_header()
        self._refresh_sidebar()
        self._refresh_hints()
        self.set_interval(1 / mascot.ANIMATION_FPS, self._advance_frame)
        self.set_interval(2.0, self._poll_status)
        self.query_one("#composer", Input).focus()
        await self._welcome()

    async def _welcome(self) -> None:
        status = self._status
        model = str(status.get("model", "")).split("/", 1)[-1]
        text = Text()
        text.append("PANDA ", style=f"bold {WHITE}")
        text.append(__version__, style=GREEN)
        text.append("  autonomous evidence harness", style=SHADOW)
        text.append(f"\n{status.get('root', '')}", style=SHADOW)
        text.append(f"\n{status.get('provider', 'opencode-go')} · {model}", style=GREEN_DIM)
        text.append("  ·  ", style=SHADOW)
        text.append(str(status.get("session_id", "")), style=SHADOW)
        if not status.get("initialized", True):
            text.append(
                "\nworkspace not initialized · run `panda init` · writes follow the default policy",
                style=AMBER,
            )
        if not status.get("validator_exists", True):
            text.append(
                f"\nvalidator missing: {status.get('validator', 'corpus/validation/validate.mjs')}"
                " · /validate fails until it exists",
                style=AMBER,
            )
        text.append('\n"Evidence or it did not happen."  /help for commands', style=SHADOW)
        await self.query_one("#transcript", VerticalScroll).mount(
            Static(text, classes="block")
        )

    def on_resize(self, event: Any) -> None:
        if not self.is_running:
            return
        self._apply_breakpoint(event.size.width)

    def _apply_breakpoint(self, width: int) -> None:
        """Re-apply the responsive layout, tolerating a resize during teardown.

        A terminal resize arrives when the window is maximized or restored,
        and may also arrive while the app is shutting down, when the screen
        stack is empty and the chrome is already unmounted. Querying then
        raises `ScreenStackError` / `NoMatches`; a renderer must not crash.
        """
        if not self.is_running:
            return
        self._width = max(20, int(width))
        try:
            screen = self.screen
        except ScreenStackError:
            return
        wide = self._width >= 120
        medium = 100 <= self._width < 120
        narrow = 80 <= self._width < 100
        screen.set_class(wide, "wide")
        screen.set_class(medium, "medium")
        screen.set_class(narrow, "narrow")
        screen.set_class(self._width < 80, "tiny")
        sidebar = self._chrome("#sidebar", Vertical)
        if sidebar is not None:
            sidebar.display = self._width >= 100
            sidebar.set_class(not wide, "compact")
        header = self._chrome("#header", Static)
        if header is not None:
            header.set_class(self._width < 80, "tight")
        hints = self._chrome("#hints", Static)
        if hints is not None:
            hints.display = self._width >= 80
        self._refresh_header()
        self._refresh_sidebar()
        self._refresh_hints()

    # ---- chrome ----
    def _chrome(self, selector: str, widget_type: type[Any]) -> Any | None:
        """Query a chrome widget, returning None instead of raising after teardown."""
        try:
            return self.query_one(selector, widget_type)
        except (NoMatches, ScreenStackError):
            return None

    def _chrome_update(self, selector: str, renderable: Any) -> None:
        """Update a chrome widget, silently skipping it after teardown.

        Timers can fire while the app is being torn down, at which point the
        screen still exists but its children are already removed. Querying a
        gone widget raises `NoMatches`; a renderer must never crash on that.
        """
        widget = self._chrome(selector, Static)
        if widget is not None:
            widget.update(renderable)

    def _refresh_header(self) -> None:
        status = self._status
        model = str(status.get("model", "")).split("/", 1)[-1]
        branch = status.get("branch") or "no-git"
        study = status.get("active_study") or "no-study"
        validation = status.get("validation")
        if validation is None:
            validation_text, validation_style = "val —", SHADOW
        elif validation.get("ok"):
            validation_text, validation_style = "val ✓", GREEN
        else:
            validation_text, validation_style = "val ×", AMBER
        icon = mascot.STATE_ICON.get(self._state, "·")
        line = Text()
        line.append("PANDA", style=f"bold {WHITE}")
        if self._width >= 120:
            line.append(f"  {status.get('root', '')}", style=SHADOW)
        if self._width >= 100:
            line.append(f"  git:{branch}", style=GREEN_DIM)
            if status.get("dirty"):
                line.append("*", style=AMBER)
        line.append(f"  {status.get('provider', 'opencode-go')}/{model}", style=WHITE)
        if self._width >= 100:
            line.append(f"  study:{study}", style=SHADOW)
            line.append("  ", style=SHADOW)
            line.append(validation_text, style=validation_style)
        state_style = GREEN if self._state not in {"blocked", "alert"} else AMBER
        line.append(f"  {icon} {self._state}", style=state_style)
        line.truncate(max(8, self._width - 4), overflow="ellipsis")
        self._chrome_update("#header", line)

    def _refresh_hints(self) -> None:
        if self._width < 80:
            return
        hint = (
            "Enter send · /help commands · Ctrl+C cancel · Ctrl+L clear · Ctrl+Q quit"
        )
        if self._busy:
            hint = "streaming · input stays live · Ctrl+C cancel · Ctrl+Q quit"
        line = _dim(hint)
        line.truncate(max(8, self._width - 4), overflow="ellipsis")
        self._chrome_update("#hints", line)

    def _refresh_sidebar(self) -> None:
        if self.runtime is None:
            return
        tick = 0 if self._reduced_motion else self._tick
        if self._width >= 120:
            lines = mascot.mascot_lines(self._state, tick)
        else:
            lines = spark_lines()
            if tick % 4 in (1, 3):
                lines = [Text(" "), *lines[:-1]]
        block = Text()
        for index, line in enumerate(lines):
            if index:
                block.append("\n")
            block.append_text(line)
        self._chrome_update("#mascot", block)
        self._chrome_update("#metrics", self._metrics_text())

    def _metrics_text(self) -> Text:
        status = self._status
        usage = status.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        cached = int(usage.get("cached_tokens") or 0)
        total = int(usage.get("total_tokens") or 0)
        cost = usage.get("cost_usd")
        context_tokens = int(status.get("context_tokens") or 0)
        window = int(status.get("context_window") or 0)
        last_steps = int(status.get("last_steps") or 0)
        max_steps = int(status.get("max_steps") or 0)
        model = str(status.get("model", "")).split("/", 1)[-1]
        effort = last_steps / max_steps if last_steps and max_steps else None
        cache = cached / prompt if prompt else None
        context = context_tokens / window if context_tokens and window else None
        if self._width >= 120:
            metrics = mascot.MascotMetrics(
                state=self._state,
                effort=effort,
                model=model,
                cache=cache,
                context=context,
                tokens=total or None,
            )
            block = Text()
            for index, line in enumerate(mascot.metrics_lines(metrics)):
                if index:
                    block.append("\n")
                block.append_text(line)
            cost_text = "—" if cost is None else f"${cost:.4f}"
            block.append("\n")
            block.append("cost    ", style=SHADOW)
            block.append(cost_text, style=WHITE)
            return block
        icon = mascot.STATE_ICON.get(self._state, "·")
        compact = Text()
        state_style = GREEN if self._state not in {"blocked", "alert"} else AMBER
        compact.append(f"{icon} {self._state}", style=state_style)
        compact.append(f"\n{_short(model, 18)}", style=WHITE)
        ctx = "—" if context is None else f"{round(context * 100)}%"
        ctx_style = AMBER if context is not None and context >= 0.85 else SHADOW
        compact.append(f"\nctx {ctx}", style=ctx_style)
        compact.append(f"\ntok {'—' if not total else mascot.format_tokens(total)}", style=SHADOW)
        return compact
    # ---- animation and events ----
    def _advance_frame(self) -> None:
        if not self.is_running or self._reduced_motion or not self._focused:
            return
        now = time.monotonic()
        elapsed = now - self._last_anim
        steps = int(elapsed * mascot.ANIMATION_FPS)
        if steps <= 0:
            return
        steps = min(steps, 4)
        self._tick = (self._tick + steps) % 1_000_000
        self._last_anim += steps / mascot.ANIMATION_FPS
        self._refresh_sidebar()

    def _poll_status(self) -> None:
        # The 2s timer may fire while the app is tearing down.
        if not self.is_running or self.runtime is None:
            return
        self._status = self.runtime.status()
        self._refresh_header()
        self._refresh_sidebar()

    def on_app_blur(self, event: events.AppBlur) -> None:
        self._focused = False
        self._rest_state = self._state
        self._set_state("sleeping")

    def on_app_focus(self, event: events.AppFocus) -> None:
        self._focused = True
        self._last_anim = time.monotonic()
        self._set_state(self._rest_state)

    def _set_state(self, state: str) -> None:
        self._state = state
        if state != "sleeping":
            self._rest_state = state
        self._refresh_header()
        self._refresh_sidebar()

    def _mount_widget(self, widget: Any) -> None:
        async def _mount() -> None:
            transcript = self.query_one("#transcript", VerticalScroll)
            await transcript.mount(widget)
            transcript.scroll_end(animate=False)

        self.run_worker(_mount(), group="ui", exit_on_error=False)

    def _notice(self, text: str) -> None:
        block = Text()
        block.append("panda  ", style=f"bold {GREEN_DIM}")
        block.append(text, style=SHADOW)
        self._mount_widget(Static(block, classes="block"))

    async def _approve(self, request: ApprovalRequest) -> bool:
        self._set_state("verifying")
        try:
            return bool(await self.push_screen_wait(ApprovalScreen(request)))
        finally:
            self._set_state("working" if self._busy else "idle")

    def _on_agent_event(self, event: Any) -> None:
        kind = event.kind
        data = event.data
        if kind == "turn_start":
            self._set_state("thinking")
        elif kind == "text_delta":
            self._assistant_buffer.append(str(data.get("text", "")))
            self._set_state("working")
            now = time.monotonic()
            if self._assistant_widget is not None and now - self._last_markdown >= 0.06:
                self._last_markdown = now
                self._assistant_widget.update(
                    RichMarkdown("".join(self._assistant_buffer))
                )
        elif kind == "tool_call":
            card = ToolCard(str(data.get("name", "")), str(data.get("arguments", "")))
            self._tool_cards[str(data.get("id", ""))] = card
            self._tool_cards.setdefault("__last__", card)
            self._mount_widget(card)
            self._set_state("working")
        elif kind == "tool_result":
            card = self._tool_cards.get(str(data.get("id", "")))
            if card is not None:
                card.finish(
                    ok=bool(data.get("ok")),
                    blocked=bool(data.get("blocked")),
                    detail=str(data.get("content", ""))[:160],
                )
            if data.get("name") == "validate":
                payload = _loads(str(data.get("content", "")))
                if payload:
                    self._mount_widget(ValidationPanel(payload))
                    self._set_state("success" if payload.get("ok") else "blocked")
            elif data.get("blocked"):
                self._set_state("blocked")
        elif kind == "usage":
            self._status["usage"] = {
                key: data.get(key)
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "cached_tokens",
                    "cost_usd",
                )
            }
            self._status["context_tokens"] = int(data.get("prompt_tokens") or 0)
            self._refresh_sidebar()
        elif kind == "error":
            self._set_state("alert")
        elif kind == "turn_end":
            self._set_state("success" if data.get("status") == "completed" else "blocked")
    # ---- input ----
    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._handle_input(event.value)

    def _handle_input(self, value: str) -> None:
        text = value.strip()
        if not text:
            return
        composer = self.query_one("#composer", Input)
        composer.value = ""
        if text.startswith("/"):
            self._run_command(text)
            return
        if text.startswith("!"):
            self._run_shell(text[1:].strip())
            return
        if self._busy:
            self._notice("a turn is already running; Ctrl+C cancels it")
            return
        self._turn_worker = self._turn(text)

    async def _append_user(self, text: str) -> None:
        block = Text()
        block.append("you  ", style=f"bold {GREEN}")
        block.append(text, style=WHITE)
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(Static(block, classes="block"))
        transcript.scroll_end(animate=False)

    def _finalize_assistant(self) -> None:
        if self._assistant_widget is None:
            return
        text = "".join(self._assistant_buffer).strip()
        if text:
            self._assistant_widget.update(RichMarkdown(text))
        else:
            self._assistant_widget.remove()

    @work(exclusive=True, group="turn")
    async def _turn(self, text: str) -> None:
        if self.runtime is None:
            return
        self._busy = True
        self._refresh_hints()
        self._set_state("thinking")
        await self._append_user(text)
        self._assistant_buffer = []
        self._last_markdown = 0.0
        self._assistant_widget = Static(RichMarkdown("…"), classes="block")
        await self.query_one("#transcript", VerticalScroll).mount(self._assistant_widget)
        try:
            result = await self.runtime.ask(text)
            self._finalize_assistant()
            self._set_state("success" if result.status == "completed" else "blocked")
            if result.status == "max_steps":
                self._notice("max steps reached; the turn stopped without a final answer")
        except asyncio.CancelledError:
            self._set_state("blocked")
            self._notice("turn cancelled")
            raise
        except PandaError as exc:
            self._set_state("alert")
            self._notice(f"{exc.code}: {exc.message}")
        finally:
            self._busy = False
            self._status = self.runtime.status()
            self._refresh_header()
            self._refresh_sidebar()
            self._refresh_hints()

    # ---- commands ----
    @work(exclusive=False, group="command")
    async def _run_command(self, text: str) -> None:
        if self.runtime is None:
            return
        parts = text.split()
        command = parts[0].lower()
        argument = " ".join(parts[1:]).strip()
        if command in {"/quit", "/exit"}:
            self.action_quit_app()
            return
        if command == "/help":
            self._notice(HELP_TEXT)
            return
        if command == "/model":
            if not argument:
                self._notice(f"model: {self.runtime.model}")
                return
            try:
                self.runtime.set_model(argument)
            except PandaError as exc:
                self._notice(f"{exc.code}: {exc.message}")
                return
            self._status = self.runtime.status()
            self._notice(f"model: {self.runtime.model}")
            return
        if command == "/models":
            models = await self.runtime.models()
            block = Text()
            block.append("MODELS ", style=f"bold {WHITE}")
            block.append(
                f"{len(models)} · source {self.runtime.models_source}", style=SHADOW
            )
            if self.runtime.models_error:
                block.append("\nlive catalogue unavailable: ", style=AMBER)
                block.append(str(self.runtime.models_error), style=SHADOW)
            for model in models:
                block.append("\n")
                mark = "✓ " if model.supported else "× "
                mark_style = GREEN if model.supported else AMBER
                block.append(mark, style=mark_style)
                qualified = model.qualified(self.runtime.workspace.config.provider.name)
                block.append(qualified, style=WHITE)
                block.append(f"  {model.protocol}", style=SHADOW)
                if model.free:
                    block.append("  free", style=GREEN_DIM)
            self._mount_widget(Static(block, classes="block"))
            return
        if command == "/study":
            if argument.startswith("use "):
                study_id = argument[4:].strip()
                try:
                    find_study(self.runtime.workspace.root, study_id)
                except PandaError as exc:
                    self._notice(f"{exc.code}: {exc.message}")
                    return
                self.runtime.set_active_study(study_id)
                self._status = self.runtime.status()
                self._notice(f"active study: {study_id}")
                return
            try:
                studies = load_registry(self.runtime.workspace.root)
            except PandaError as exc:
                self._notice(f"{exc.code}: {exc.message}")
                return
            block = Text()
            block.append("STUDIES", style=f"bold {WHITE}")
            for study in studies:
                question = _short(study.question, 60)
                block.append(
                    f"\n{study.id:<24} {study.track:<10} {study.status:<8} {question}",
                    style=WHITE,
                )
            self._mount_widget(Static(block, classes="block"))
            return
        if command == "/status":
            status = self.runtime.status()
            usage = status.get("usage") or {}
            block = Text()
            block.append("STATUS", style=f"bold {WHITE}")
            for label, value in (
                ("provider", f"{status.get('provider')} · {status.get('base_url')}"),
                ("model", status.get("model")),
                ("session", status.get("session_id")),
                ("study", status.get("active_study") or "—"),
                ("context", f"{status.get('context_tokens')}/{status.get('context_window')}"),
                ("tokens", f"{usage.get('total_tokens', 0)} total"),
                ("cost", "—" if usage.get("cost_usd") is None else f"${usage['cost_usd']:.4f}"),
                (
                    "validation",
                    "—"
                    if status.get("validation") is None
                    else str(status["validation"].get("ok")),
                ),
            ):
                block.append(f"\n{label:<11}", style=SHADOW)
                block.append(str(value), style=WHITE)
            self._mount_widget(Static(block, classes="block"))
            return
        if command == "/validate":
            self._set_state("verifying")
            result = await self.runtime.validate()
            payload = result.data
            self._mount_widget(ValidationPanel(payload))
            self._set_state("success" if result.ok else "blocked")
            self._status = self.runtime.status()
            return
        if command == "/policy":
            block = Text()
            block.append("POLICY", style=f"bold {WHITE}")
            for row in self.runtime.policy_rows():
                actions = _short(row["actions"], 40)
                block.append(
                    f"\n{row['id']:<26} {row['effect']:<10} {row['risk']:<9} {actions}",
                    style=WHITE,
                )
            self._mount_widget(Static(block, classes="block"))
            return
        if command == "/session":
            block = Text()
            block.append("SESSION", style=f"bold {WHITE}")
            for row in self.runtime.session_rows()[-15:]:
                block.append(f"\n{row['seq']:>4}  {row['type']:<20} {row['ts']}", style=SHADOW)
            self._mount_widget(Static(block, classes="block"))
            return
        if command == "/clear":
            await self.action_clear_transcript()
            return
        self._notice("unknown command; try /help")

    @work(exclusive=False, group="shell")
    async def _run_shell(self, text: str) -> None:
        if self.runtime is None:
            return
        from panda_core.process import split_command

        argv = split_command(text)
        if not argv:
            self._notice("empty command")
            return
        card = ToolCard("run_shell", " ".join(argv))
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(card)
        transcript.scroll_end(animate=False)
        self._set_state("working")
        result = await self.runtime.tools.call("run_shell", {"argv": argv})
        card.finish(
            ok=result.ok,
            blocked=result.blocked,
            detail=str(result.content)[:160],
        )
        self._set_state("blocked" if result.blocked else "success")

    # ---- lifecycle ----
    def action_cancel_turn(self) -> None:
        """Ctrl+C cancels the running turn; it never exits the cockpit."""
        worker = self._turn_worker
        if worker is None or not worker.is_running:
            self._notice("nothing to cancel · Ctrl+Q quits")
            return
        with contextlib.suppress(Exception):
            if isinstance(self.screen, ApprovalScreen):
                self.screen.dismiss(False)
        worker.cancel()
        self._set_state("blocked")
        self._notice("cancelling…")

    async def action_clear_transcript(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        if self.runtime is not None:
            self.runtime.reset()
        self._assistant_widget = None
        self._assistant_buffer = []
        await self._welcome()

    def action_quit_app(self) -> None:
        if self.runtime is not None:
            self.runtime.close("completed")
        self.exit()

    def on_unmount(self) -> None:
        if self.runtime is not None:
            self.runtime.close("completed")
        # Safety net: a crash can skip the driver's own restore, leaving the
        # terminal in the alt screen with mouse tracking enabled.
        driver = getattr(self, "_driver", None)
        if driver is None:
            return
        with contextlib.suppress(Exception):
            driver.write(MOUSE_OFF)
            driver.flush()
        with contextlib.suppress(Exception):
            driver.stop_application_mode()


__all__ = ["ApprovalScreen", "PandaCockpit", "ToolCard", "ValidationPanel"]