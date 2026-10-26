"""Cockpit: boot, responsive breakpoints, and the approval modal."""

from __future__ import annotations

import asyncio

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from panda_cli.cockpit import ApprovalScreen, PandaCockpit
from panda_core.tools import ApprovalRequest
from panda_core.workspace import Workspace


@pytest.fixture(autouse=True)
def _still_cockpit(monkeypatch):
    monkeypatch.setenv("PANDA_REDUCED_MOTION", "1")


def request() -> ApprovalRequest:
    return ApprovalRequest(
        tool="run_shell",
        action="shell",
        resource="pytest -q",
        rule_id="shell-default-ask",
        risk="medium",
        reason="shell is never implicit",
        preview="pytest -q",
        preview_kind="command",
    )


def test_cockpit_boots_with_chrome(workspace) -> None:
    async def scenario() -> None:
        app = PandaCockpit(workspace)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#composer", Input).has_focus
            assert app.query_one("#mascot", Static)
            assert app.query_one("#header", Static)
            assert app.runtime is not None
            assert app.runtime.session.session_id.startswith("s_")
            transcript = app.query_one("#transcript", VerticalScroll)
            assert len(transcript.children) >= 1

    asyncio.run(scenario())


def test_narrow_layout_hides_sidebar_and_hints(workspace) -> None:
    async def scenario() -> None:
        app = PandaCockpit(workspace)
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            assert app.query_one("#sidebar").display is False
            assert app.query_one("#hints").display is True

        tiny = PandaCockpit(workspace)
        async with tiny.run_test(size=(70, 24)) as pilot:
            await pilot.pause()
            assert "tight" in tiny.query_one("#header", Static).classes
            assert tiny.query_one("#hints").display is False

    asyncio.run(scenario())


def test_approval_modal_allow_and_deny(workspace) -> None:
    async def scenario() -> None:
        app = PandaCockpit(workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            decisions: list[bool] = []
            for key in ("y", "n"):
                app.push_screen(ApprovalScreen(request()), callback=decisions.append)
                await pilot.pause()
                await pilot.press(key)
                await pilot.pause()
            assert decisions == [True, False]

    asyncio.run(scenario())


def test_timers_stay_silent_after_teardown(workspace) -> None:
    """Regression: the 2s status timer must not crash while the app unmounts.

    Textual removes the screen's children before the message loop stops, so a
    timer that fires late queries widgets that no longer exist (`NoMatches`).
    """

    async def scenario() -> None:
        app = PandaCockpit(workspace)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
        app._poll_status()
        app._refresh_header()
        app._refresh_sidebar()
        app._refresh_hints()
        app._apply_breakpoint(220)  # a resize delivered after unmount
        app._advance_frame()

    asyncio.run(scenario())


def render_text(widget: Static) -> str:
    """Plain text of a Static, whichever renderable Textual hands back."""

    renderable = widget.render()
    plain = getattr(renderable, "plain", None)
    return plain if isinstance(plain, str) else str(renderable)


def transcript_text(app: PandaCockpit) -> str:
    """Plain text of every transcript block: welcome, notices, tool cards."""

    transcript = app.query_one("#transcript", VerticalScroll)
    return "\n".join(
        render_text(child) for child in transcript.children if isinstance(child, Static)
    )


def test_fullscreen_resize_keeps_chrome_inside_the_viewport(workspace) -> None:
    """Regression: maximizing must not raise NoMatches nor overflow the header."""

    async def scenario() -> None:
        app = PandaCockpit(workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.resize_terminal(220, 60)
            await pilot.pause()
            assert app.size.width == 220
            header = render_text(app.query_one("#header", Static))
            hints = render_text(app.query_one("#hints", Static))
            assert len(header) <= app.size.width
            assert len(hints) <= app.size.width
            assert app.query_one("#hints", Static).display is True

        wide = PandaCockpit(workspace)
        async with wide.run_test(size=(220, 60)) as pilot:
            await pilot.pause()
            header = render_text(wide.query_one("#header", Static))
            assert len(header) <= 220

    asyncio.run(scenario())


def test_ctrl_c_cancels_the_turn_without_quitting(workspace) -> None:
    """Ctrl+C is a cancel gesture; Textual's default help_quit must not win."""

    async def scenario() -> None:
        app = PandaCockpit(workspace)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            bindings = app.active_bindings
            assert bindings["ctrl+c"].binding.action == "cancel_turn"
            assert bindings["ctrl+c"].binding.priority is True
            assert bindings["ctrl+q"].binding.action == "quit_app"

            started = asyncio.Event()

            async def slow_ask(_text: str):
                started.set()
                await asyncio.sleep(30)
                raise AssertionError("a cancelled turn must not run to completion")

            assert app.runtime is not None
            app.runtime.ask = slow_ask  # type: ignore[method-assign]
            app._handle_input("hello")
            await started.wait()
            assert app._busy is True

            await pilot.press("ctrl+c")
            await pilot.pause()
            await pilot.pause()

            assert app.is_running is True
            assert len(app.screen_stack) == 1
            assert app._busy is False
            assert app._turn_worker is not None
            assert app._turn_worker.is_running is False
            assert "turn cancelled" in transcript_text(app)

    asyncio.run(scenario())


def test_cockpit_boots_outside_an_initialized_workspace(tmp_path) -> None:
    """`panda` must open anywhere, with a notice, instead of exiting."""

    async def scenario() -> None:
        app = PandaCockpit(Workspace.from_root(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.runtime is not None
            text = transcript_text(app)
            assert "workspace not initialized" in text
            assert "panda init" in text
            assert "validator missing" in text

    asyncio.run(scenario())

