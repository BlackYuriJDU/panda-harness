"""Trilha B — grep tool + grep_worker. Afirma o resultado SEGURO."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

import panda_core.tools as tools_mod
from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.tools import ToolContext, ToolRegistry
from panda_core.workspace import Workspace


def _registry(workspace: Workspace) -> ToolRegistry:
    policy, _ = load_policy_or_default(workspace.policy_path)
    return ToolRegistry(ToolContext(workspace=workspace, policy=PolicyEngine(policy)))


def test_grep_worker_does_not_follow_a_file_swapped_to_a_secret_after_the_policy_check(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace.root
    (root / ".env").write_text("TOKENLESS=SYNTH_SECRET_GREP_123\n", encoding="utf-8")
    (root / "corpus" / "notes.md").write_text("TOKENLESS=public\n", encoding="utf-8")
    real = tools_mod.run_command

    def swap_then_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        target = root / "corpus" / "notes.md"
        target.unlink()
        os.symlink(root / ".env", target)  # troca após _readable_by_policy
        return real(*args, **kwargs)

    monkeypatch.setattr(tools_mod, "run_command", swap_then_run)
    result = asyncio.run(_registry(workspace).call(
        "grep", {"pattern": "TOKENLESS", "glob": "corpus/*.md"}
    ))
    assert "SYNTH_SECRET" not in result.content, result.content[:300]


def test_grep_file_enumeration_does_not_block_the_event_loop(workspace: Workspace) -> None:
    """A enumeração (_iter_files + política por arquivo) roda no loop, fora do worker."""

    base = workspace.root / "corpus" / "bulk"
    for d in range(60):
        sub = base / f"d{d}"
        sub.mkdir(parents=True)
        for f in range(150):
            (sub / f"f{f}.txt").write_text("x\n", encoding="utf-8")
    registry = _registry(workspace)

    async def scenario() -> float:
        worst = 0.0

        async def ticker() -> None:
            nonlocal worst
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                worst = max(worst, now - last)
                last = now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        await registry.call("grep", {"pattern": "zzz", "glob": "**/*"})
        task.cancel()
        return worst

    stall = asyncio.run(scenario())
    assert stall < 0.5, f"loop bloqueado por {stall:.2f}s durante a enumeração"
