"""Cockpit: boot, responsive breakpoints, and the approval modal."""

from __future__ import annotations

import asyncio

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from panda_cli.cockpit import ApprovalScreen, PandaCockpit
from panda_core.tools import ApprovalRequest


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

    asyncio.run(scenario())
