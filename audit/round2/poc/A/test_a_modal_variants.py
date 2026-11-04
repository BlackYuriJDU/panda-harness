"""Trilha A — variantes de APR-001/APR-002 (C-4) no modal do cockpit."""

from __future__ import annotations

import asyncio

from panda_cli.cockpit import ApprovalScreen, PandaCockpit
from panda_core.tools import ApprovalRequest, _diff_preview


def run_modal(workspace, request, script):
    decisions: list[bool] = []

    async def scenario() -> None:
        app = PandaCockpit(workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = ApprovalScreen(request)
            app.push_screen(screen, callback=decisions.append)
            await pilot.pause()
            await script(pilot, screen, decisions)

    asyncio.run(scenario())
    return decisions


# R2-125: a prévia de write/edit é cortada em 6000 caracteres (_diff_preview) e o modal
# ainda permite aprovar; o conteúdo além do corte nunca é mostrado (C-4).
def test_a_truncated_preview_cannot_be_approved(workspace):
    content = "".join(f"line {n}\n" for n in range(2000)) + "HIDDEN_PAYLOAD_AFTER_CUT\n"
    preview = _diff_preview("corpus/big.md", "", content)
    assert "HIDDEN_PAYLOAD_AFTER_CUT" not in preview and "[truncated " in preview
    request = ApprovalRequest(tool="write_file", action="write", resource="corpus/big.md",
                              rule_id="write-scopes", risk="medium", reason="", preview=preview,
                              preview_kind="diff")

    async def script(pilot, screen, decisions):
        await pilot.pause(0.6)
        screen.query_one("#approval-preview-scroll").scroll_end(animate=False)
        await pilot.pause(0.3)
        await pilot.press("y")
        await pilot.pause()

    assert run_modal(workspace, request, script) == [], "aprovado sem ver o conteúdo cortado"
