"""Wave 6: a corpus delivered with `.git` must not run programs when Panda opens it.

R2-106 (P0) and the hotfix half of R2-500. Synthetic data only, all in tmp_path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell hooks")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _hostile_repo(root: Path, marker: Path) -> None:
    """A repository whose own config names programs: filter, hook, fsmonitor."""

    script = root.parent / "payload.sh"
    script.write_text(f"#!/bin/sh\necho ran \"$0 $*\" >> '{marker}'\ncat\n", encoding="utf-8")
    script.chmod(0o755)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "x@example.invalid")
    _git(root, "config", "user.name", "x")
    (root / "notes.txt").write_text("one\n", encoding="utf-8")
    (root / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    _git(root, "add", "notes.txt", ".gitattributes")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "config", "filter.evil.clean", str(script))
    _git(root, "config", "filter.evil.smudge", str(script))
    _git(root, "config", "core.fsmonitor", str(script))
    hook = root / ".git" / "hooks" / "post-index-change"
    hook.write_text(f"#!/bin/sh\necho hook >> '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    # A copied corpus has stale stat data in the index: git must re-read the file.
    (root / "notes.txt").write_text("two\n", encoding="utf-8")
    os.utime(root / "notes.txt", (1, 1))


@POSIX
def test_opening_a_corpus_with_a_hostile_git_config_runs_no_program(
    workspace, tmp_path: Path
) -> None:
    marker = tmp_path / "ran.txt"
    _hostile_repo(workspace.root, marker)
    if marker.exists():
        marker.unlink()  # setup itself (git add) may run the filter; only the probe counts

    state = workspace.git_state()

    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert state["repo"] is True and state["branch"] == "main"
    assert state["commit"] and len(state["commit"]) == 40


def test_git_state_starts_no_process(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(workspace.root, "init", "-q", "-b", "trunk")
    _git(workspace.root, "-c", "user.email=x@example.invalid", "-c", "user.name=x",
         "commit", "-q", "--allow-empty", "-m", "seed")
    calls: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("git_state started a process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    state = workspace.git_state()
    assert calls == []
    assert state["repo"] is True and state["branch"] == "trunk"
    assert state["dirty"] is None  # unknown without running git; never guessed


def test_git_state_reads_packed_and_detached_heads(workspace) -> None:
    _git(workspace.root, "init", "-q", "-b", "main")
    _git(workspace.root, "-c", "user.email=x@example.invalid", "-c", "user.name=x",
         "commit", "-q", "--allow-empty", "-m", "seed")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace.root, check=True,
                          capture_output=True, text=True).stdout.strip()
    _git(workspace.root, "pack-refs", "--all")
    assert not (workspace.root / ".git" / "refs" / "heads" / "main").exists()
    assert workspace.git_state()["commit"] == head
    _git(workspace.root, "checkout", "-q", "--detach")
    state = workspace.git_state()
    assert state["branch"] is None and state["commit"] == head


def test_git_state_treats_a_hostile_head_as_unknown(workspace) -> None:
    git_dir = workspace.root / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/../../../../etc/passwd\n", encoding="utf-8")
    state = workspace.git_state()
    assert state == {"repo": True, "commit": None, "branch": None, "dirty": None}


def test_git_state_does_not_follow_a_gitfile_outside_the_workspace(
    workspace, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    _git(tmp_path, "init", "-q", "-b", "main", str(outside))
    (workspace.root / ".git").write_text(f"gitdir: {outside / '.git'}\n", encoding="utf-8")
    state = workspace.git_state()
    assert state["repo"] is True and state["branch"] is None and state["commit"] is None


# --------------------------------------------------------------------------- R2-500 hotfix
@pytest.mark.parametrize(
    "command",
    [
        ["git", "push", "origin", "main"],
        ["env", "git", "push", "origin", "main"],
        ["timeout", "5", "git", "push", "origin", "main"],
        ["git", "-C", ".", "push", "origin", "main"],
        ["/usr/bin/git", "push", "origin", "main"],
        ["twine", "upload", "dist/x.whl"],
        ["python", "-c", "print(1)"],
    ],
)
def test_auto_mode_never_approves_a_shell_command(workspace, command: list[str]) -> None:
    import asyncio

    from panda_cli.runtime import ChatRuntime
    from panda_core.tools import ApprovalRequest

    runtime = ChatRuntime(workspace, auto=True)
    try:
        decision = runtime.policy.decide_shell(command)
        request = ApprovalRequest(
            tool="run_shell",
            action="shell",
            resource=" ".join(command),
            rule_id=decision.rule_id,
            risk=decision.risk,
            reason=decision.reason,
            preview=" ".join(command),
        )
        assert asyncio.run(runtime._approve(request)) is False
    finally:
        runtime.close()


def test_neg_auto_mode_still_approves_a_scoped_write(workspace) -> None:
    import asyncio

    from panda_cli.runtime import ChatRuntime
    from panda_core.tools import ApprovalRequest

    runtime = ChatRuntime(workspace, auto=True)
    try:
        request = ApprovalRequest(
            tool="write_file",
            action="write",
            resource="preprint/draft.md",
            rule_id="write-preprint",
            risk="medium",
            reason="edits the preprint",
            preview="+ text",
        )
        assert asyncio.run(runtime._approve(request)) is True
    finally:
        runtime.close()
