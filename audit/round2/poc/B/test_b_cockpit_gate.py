"""Trilha B — ApprovalScreen (o que o operador vê x o que roda)."""

from __future__ import annotations

import asyncio
from typing import Any

from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.tools import ApprovalRequest, ToolContext, ToolRegistry


def test_run_shell_approval_shows_the_working_directory(workspace) -> None:  # type: ignore[no-untyped-def]
    (workspace.root / "corpus" / "evil").mkdir()
    seen: list[ApprovalRequest] = []

    async def approver(request: ApprovalRequest) -> bool:
        seen.append(request)
        return False

    policy, _ = load_policy_or_default(workspace.policy_path)
    registry = ToolRegistry(ToolContext(workspace=workspace, policy=PolicyEngine(policy),
                                        approve=approver))
    asyncio.run(registry.call("run_shell", {"argv": ["make", "test"], "cwd": "corpus/evil"}))
    assert seen, "esperava um pedido de aprovação"
    shown = " ".join([seen[0].resource, seen[0].preview, seen[0].reason])
    assert "corpus/evil" in shown, seen[0].to_dict()


def test_approval_preview_shows_the_tail_of_a_long_unbroken_line() -> None:
    from textual.app import App
    from textual.geometry import Region
    from textual.widgets import Static

    from panda_cli.cockpit import ApprovalScreen

    command = "python3 -c " + "A" * 400 + "TAIL_EVIL_PAYLOAD"
    request = ApprovalRequest(tool="run_shell", action="shell", resource=command,
                              rule_id="shell-default-ask", risk="medium", reason="",
                              preview=command, preview_kind="command")
    seen: dict[str, Any] = {}

    class Probe(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(ApprovalScreen(request))

    async def scenario() -> None:
        app = Probe()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause(0.6)
            screen = app.screen
            widget = screen.query_one("#approval-preview", Static)
            strips = widget.render_lines(Region(0, 0, widget.size.width, widget.size.height))
            seen["text"] = "".join(seg.text for strip in strips for seg in strip)
            seen["hidden"] = screen._hidden_lines()

    asyncio.run(scenario())
    assert "TAIL_EVIL_PAYLOAD" in seen["text"] or seen["hidden"] > 0, seen


def test_write_file_is_not_applied_when_the_file_changes_during_approval(workspace) -> None:  # type: ignore[no-untyped-def]
    policy, _ = load_policy_or_default(workspace.policy_path)
    registry = ToolRegistry(ToolContext(workspace=workspace, policy=PolicyEngine(policy),
                                        approve=None))
    # tests/* é escopo de revisão (ask)
    review = workspace.root / "tests" / "x.md"
    review.parent.mkdir(exist_ok=True)
    review.write_text("a\n", encoding="utf-8")

    async def approver2(request: ApprovalRequest) -> bool:
        review.write_text("changed\n", encoding="utf-8")
        return True

    registry.ctx.approve = approver2
    result = asyncio.run(registry.call("write_file", {"path": "tests/x.md", "content": "mine\n"}))
    assert not result.ok and review.read_text(encoding="utf-8") == "changed\n", result.content
